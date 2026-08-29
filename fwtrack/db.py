"""Recording a build: straight to the database, or over HTTP to the ingest endpoint."""

import json
import os
import urllib.error
import urllib.request

import psycopg
from psycopg.types.json import Jsonb

from . import __version__
from .log import get_logger

logger = get_logger(__name__)

DSN_ENV = "FWTRACK_DSN"
URL_ENV = "FWTRACK_URL"
TOKEN_ENV = "FWTRACK_INGEST_TOKEN"
USER_AGENT = f"fw-footprint-tracker/{__version__}"

INSERT_BUILD = """
INSERT INTO builds (project, built_at, commit, branch, version, origin, dirty,
                    author, toolchain, tags)
VALUES (%(project)s, %(built_at)s, %(commit)s, %(branch)s, %(version)s,
        %(origin)s, %(dirty)s, %(author)s, %(toolchain)s, %(tags)s)
ON CONFLICT (project, commit, built_at, tags) DO UPDATE
    SET branch    = EXCLUDED.branch,
        version   = EXCLUDED.version,
        origin    = EXCLUDED.origin,
        dirty     = EXCLUDED.dirty,
        author    = EXCLUDED.author,
        toolchain = EXCLUDED.toolchain
RETURNING id
"""

INSERT_USAGE = """
INSERT INTO memory_usage (build_id, region, area, used, total)
VALUES (%(build_id)s, %(region)s, %(area)s, %(used)s, %(total)s)
ON CONFLICT (build_id, region) DO UPDATE
    SET area  = EXCLUDED.area,
        used  = EXCLUDED.used,
        total = EXCLUDED.total
"""

# updated_at only moves when the value really changed, so the column stays a
# useful record of when a budget was last revised.
UPSERT_BUDGET = """
INSERT INTO region_budgets (project, region, thresholds)
VALUES (%(project)s, %(region)s, %(thresholds)s)
ON CONFLICT (project, region) DO UPDATE
    SET thresholds = EXCLUDED.thresholds,
        updated_at = now()
    WHERE region_budgets.thresholds IS DISTINCT FROM EXCLUDED.thresholds
"""


# One row per dimension the project has ever recorded. The number of distinct
# values is what tells a dimension from an accident: a handful means a variant
# worth filtering by, hundreds means something like a commit hash that should
# never have been a tag.
TAG_COUNTS = """
SELECT key, count(*) AS builds, count(DISTINCT b.tags ->> key) AS values
FROM builds b, LATERAL jsonb_object_keys(b.tags) AS key
WHERE b.project = %s
GROUP BY key
ORDER BY key
"""

# Columns of `builds` that behave like a dimension on a dashboard: filterable,
# and occasionally renamed after a pipeline starts labelling runs differently.
# A fixed set because an identifier cannot be passed as a query parameter -- the
# name below is interpolated into the SQL, so it may only ever come from here.
FIELDS = frozenset({"branch", "origin", "author", "version", "toolchain"})

# Values a dimension actually takes, so a dropdown full of hashes can be seen
# for what it is before anything is changed.
TAG_VALUES = """
SELECT tags ->> %(tag)s AS value, count(*) AS builds
FROM builds
WHERE project = %(project)s AND tags ? %(tag)s
GROUP BY 1
ORDER BY 1
"""

# ::text for the same reason as in RENAME_TAG: jsonb_set takes text[], and the
# parameter has no type Postgres can infer on its own.
RENAME_VALUE = """
UPDATE builds SET tags = jsonb_set(tags, ARRAY[%(tag)s::text], to_jsonb(%(new)s::text))
WHERE project = %(project)s AND tags ->> %(tag)s = %(old)s
"""

DROP_TAG = """
UPDATE builds SET tags = tags - %(tag)s
WHERE project = %(project)s AND tags ? %(tag)s
"""

# ::text on the new name: jsonb_build_object takes "any", and without the cast
# Postgres cannot infer the parameter's type and refuses to plan the statement.
RENAME_TAG = """
UPDATE builds SET tags = (tags - %(old)s) || jsonb_build_object(%(new)s::text, tags ->> %(old)s)
WHERE project = %(project)s AND tags ? %(old)s
"""

# memory_usage goes with it: the foreign key is ON DELETE CASCADE.
DELETE_BUILD = "DELETE FROM builds WHERE id = %s RETURNING project, commit, built_at"


def record(build: dict, regions: list, url=None, token=None, dsn=None) -> int | None:
    """Store one build, over HTTP if a URL is known and straight to the database otherwise.

    Every setting can be passed in. Nothing here insists on environment
    variables, let alone a .env file: a caller holding its secrets in a vault,
    a CI variable or a config of its own passes them as arguments and none of
    the fallbacks below apply.
    """
    # Stripped: a secret pasted into a CI settings page routinely picks up a
    # trailing newline, and the only sign of it is an opaque "Invalid header
    # value" from deep inside urllib.
    url = (url or os.getenv(URL_ENV) or "").strip()
    if url:
        return post(url, build, regions, token)

    with connect(dsn) as conn:
        return write_build(conn, build, regions)


def api(method: str, path: str, payload: dict | None = None, url: str | None = None,
        token: str | None = None) -> dict:
    """One call to the ingest endpoint."""
    url = (url or os.getenv(URL_ENV) or "").strip()
    if not url:
        raise RuntimeError(f"Pass url=, or set {URL_ENV}")

    token = (token or os.getenv(TOKEN_ENV) or "").strip()
    if not token:
        raise RuntimeError(f"Pass token=, or set {TOKEN_ENV}, to reach {url}")

    data = None
    if payload is not None:
        data = json.dumps(
            payload,
            default=lambda o: o.isoformat() if hasattr(o, "isoformat") else str(o),
        ).encode()

    request = urllib.request.Request(
        f"{url.rstrip('/')}{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Ingest returned {e.code}: {e.read().decode()[:200]}") from e


def post(url: str, build: dict, regions: list, token: str | None = None) -> int | None:
    result = api("POST", "/ingest/builds", {"build": build, "regions": regions},
                 url=url, token=token)
    logger.info(f"Recorded build {result.get('build_id')} via {url}")
    return result.get("build_id")


def get_dsn() -> str:
    dsn = (os.getenv(DSN_ENV) or "").strip()
    if not dsn:
        raise RuntimeError(f"{DSN_ENV} is not set (see .env.example)")

    return dsn


def connect(dsn: str | None = None) -> psycopg.Connection:
    return psycopg.connect(dsn or get_dsn())


def write_build(conn: psycopg.Connection, build: dict, regions: list, budgets: bool = True) -> int:
    """Store one build and its regions, replacing any previous run of the same build.

    Both statements upsert so that re-running CI on the same commit refreshes the
    numbers instead of piling up duplicate rows.
    """
    # Spelled out rather than splatted: everything below that uses .get() is
    # optional at the endpoint, and a caller posting its own JSON should not get
    # a 500 for leaving one out.
    with conn.cursor() as cur:
        cur.execute(
            INSERT_BUILD,
            {
                "project": build["project"],
                "built_at": build["built_at"],
                "commit": build["commit"],
                "branch": build["branch"],
                "origin": build["origin"],
                "dirty": build["dirty"],
                "version": build.get("version"),
                "author": build.get("author"),
                "toolchain": build.get("toolchain"),
                "tags": Jsonb(build.get("tags") or {}),
            },
        )
        build_id = cur.fetchone()[0]

        cur.executemany(
            INSERT_USAGE,
            [
                {
                    "build_id": build_id,
                    "region": region["region"],
                    "area": region["area"],
                    "used": region["used"],
                    # Nullable in the schema: history imported from a tracker
                    # that never recorded region sizes has no honest value here.
                    "total": region.get("total"),
                }
                for region in regions
            ],
        )
        if not budgets:
            conn.commit()
            return build_id

        cur.executemany(
            UPSERT_BUDGET,
            [
                {
                    "project": build["project"],
                    "region": region["region"],
                    "thresholds": region["thresholds"],
                }
                for region in regions
                # Optional: a caller posting straight to the endpoint need not
                # have a threshold policy, and a region without one keeps
                # whatever budget was set for it before.
                if region.get("thresholds")
            ],
        )

    conn.commit()
    logger.info(f"Stored build {build_id} with {len(regions)} regions")
    return build_id


def schema_ready(conn: psycopg.Connection) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.memory_points') IS NOT NULL")
        return bool(cur.fetchone()[0])


def fetch_column(conn: psycopg.Connection, query: str, params: tuple = ()) -> list:
    """Run a query returning a single column and hand back its values."""
    with conn.cursor() as cur:
        cur.execute(query, params)
        return [row[0] for row in cur.fetchall()]


def discover_variant_tags(conn: psycopg.Connection, project: str) -> list:
    """Dimensions this project actually records, whatever it chose to call them."""
    return fetch_column(
        conn,
        "SELECT DISTINCT jsonb_object_keys(tags) FROM builds WHERE project = %s ORDER BY 1",
        (project,),
    )


def discover_areas(conn: psycopg.Connection, project: str) -> list:
    return fetch_column(
        conn,
        "SELECT DISTINCT area FROM memory_points WHERE project = %s ORDER BY 1",
        (project,),
    )


def discover_regions(conn: psycopg.Connection, project: str, area: str) -> list:
    return fetch_column(
        conn,
        "SELECT DISTINCT region FROM memory_points WHERE project = %s AND area = %s ORDER BY 1",
        (project, area),
    )


def region_limits(conn: psycopg.Connection, project: str) -> dict:
    """Size and warning levels of each region, as of the most recent build.

    Grafana thresholds are panel configuration rather than data, so they have to
    be baked in when the dashboard is generated. Taking them from the latest
    build means a change to either only reaches the dashboard on regeneration.
    """
    query = """
        SELECT DISTINCT ON (m.region) m.region, m.total, COALESCE(g.thresholds, '{}')
        FROM builds b
        JOIN memory_usage m ON m.build_id = b.id
        LEFT JOIN region_budgets g ON g.project = b.project AND g.region = m.region
        WHERE b.project = %s
        ORDER BY m.region, b.built_at DESC
    """
    with conn.cursor() as cur:
        cur.execute(query, (project,))
        return {region: (total, list(thresholds or [])) for region, total, thresholds in cur}


def tag_counts(conn: psycopg.Connection, project: str) -> list:
    """Every dimension of a project, with how many builds carry it."""
    with conn.cursor() as cur:
        cur.execute(TAG_COUNTS, (project,))
        return [
            {"tag": tag, "builds": builds, "values": values} for tag, builds, values in cur
        ]


def tag_values(conn: psycopg.Connection, project: str, tag: str) -> list:
    """Values of one dimension, with how many builds carry each.

    A dimension is a tag or one of FIELDS -- the same thing from the dashboard,
    so the same thing here.
    """
    with conn.cursor() as cur:
        if tag in FIELDS:
            cur.execute(
                f"SELECT COALESCE({tag}::text, '(none)') AS value, count(*) AS builds "
                "FROM builds WHERE project = %s GROUP BY 1 ORDER BY 1",
                (project,),
            )
        else:
            cur.execute(TAG_VALUES, {"project": project, "tag": tag})

        return [{"value": value, "builds": builds} for value, builds in cur]


def field_counts(conn: psycopg.Connection, project: str) -> list:
    """The same summary as tag_counts, for the columns that behave like dimensions.

    Counted rather than assumed present: a project that never records an author
    should see that, not a row claiming every build has one.
    """
    names = sorted(FIELDS)
    columns = ", ".join(f"count({name}), count(DISTINCT {name})" for name in names)
    with conn.cursor() as cur:
        cur.execute(f"SELECT {columns} FROM builds WHERE project = %s", (project,))
        row = cur.fetchone()

    return [
        {"tag": name, "builds": row[i * 2], "values": row[i * 2 + 1]}
        for i, name in enumerate(names)
    ]


def _write(conn: psycopg.Connection, dry_run: bool) -> None:
    """Commit, or roll back a dry run.

    Running the statement and rolling it back is how --dry-run reports the exact
    number of rows it would touch. Counting them separately would be a second
    query that can disagree with the first.
    """
    if dry_run:
        conn.rollback()
    else:
        conn.commit()


def _refuse_field(name: str, action: str) -> None:
    """A column is not a tag: it cannot be dropped or renamed, only its values can."""
    if name in FIELDS:
        raise RuntimeError(
            f"'{name}' is a field of every build, not a tag, and cannot be {action}. "
            f"Its values can be: fwtrack-tags rename-value --project ... {name} old new"
        )


def drop_tag(conn: psycopg.Connection, project: str, tag: str, dry_run: bool = False) -> int:
    """Remove one dimension from a project's history. Returns rows affected."""
    _refuse_field(tag, "removed")

    with conn.cursor() as cur:
        try:
            cur.execute(DROP_TAG, {"project": project, "tag": tag})
        except psycopg.errors.UniqueViolation as e:
            # builds are unique on (project, commit, built_at, tags): two builds
            # of the same commit that differed only by this tag become the same
            # row once it is gone. Refusing is the honest answer -- picking a
            # winner would silently drop measurements.
            conn.rollback()
            raise RuntimeError(
                f"Cannot drop '{tag}': builds of '{project}' exist that differ only by it, "
                "and removing it would collide them. Delete the redundant builds first"
            ) from e

        affected = cur.rowcount

    _write(conn, dry_run)
    return affected


def rename_tag(conn: psycopg.Connection, project: str, old: str, new: str,
               dry_run: bool = False) -> int:
    """Rename one dimension, keeping its values. Returns rows affected."""
    _refuse_field(old, "renamed")

    with conn.cursor() as cur:
        try:
            cur.execute(RENAME_TAG, {"project": project, "old": old, "new": new})
        except psycopg.errors.UniqueViolation as e:
            conn.rollback()
            raise RuntimeError(
                f"Cannot rename '{old}' to '{new}': the result collides with builds "
                f"that already carry '{new}'"
            ) from e

        affected = cur.rowcount

    _write(conn, dry_run)
    return affected


def rename_value(conn: psycopg.Connection, project: str, tag: str, old: str, new: str,
                 dry_run: bool = False) -> int:
    """Rewrite one value of a dimension. Returns rows affected.

    What history recorded before a project started naming things: a build
    labelled with a hash and the same build labelled with the name it stands for
    are the same variant, and until they share a value they are two series on
    every chart.
    """
    with conn.cursor() as cur:
        if tag in FIELDS:
            # No collision to guard against: none of these columns take part in
            # the identity index that makes two builds the same row.
            cur.execute(
                f"UPDATE builds SET {tag} = %(new)s "
                f"WHERE project = %(project)s AND {tag} = %(old)s",
                {"project": project, "old": old, "new": new},
            )
            affected = cur.rowcount
            _write(conn, dry_run)
            return affected

        try:
            cur.execute(RENAME_VALUE, {"project": project, "tag": tag, "old": old, "new": new})
        except psycopg.errors.UniqueViolation as e:
            conn.rollback()
            raise RuntimeError(
                f"Cannot rename '{tag}={old}' to '{new}': builds of '{project}' already carry "
                f"'{new}' and would collide with it. Delete the redundant builds first"
            ) from e

        affected = cur.rowcount

    _write(conn, dry_run)
    return affected


def delete_build(conn: psycopg.Connection, build_id: int, dry_run: bool = False) -> dict | None:
    """Delete one build and its regions. Returns what was deleted, or None."""
    with conn.cursor() as cur:
        cur.execute(DELETE_BUILD, (build_id,))
        row = cur.fetchone()

    _write(conn, dry_run)
    if row is None:
        return None

    project, commit, built_at = row
    return {"id": build_id, "project": project, "commit": commit, "built_at": built_at}


# Created on use rather than by a migration: deployments predating these
# settings would otherwise need a hand-applied ALTER before the next dashboard
# could be generated, and schema.sql only ever runs on an empty database.
SETTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS project_settings (
    project      TEXT        PRIMARY KEY,
    variant_tags TEXT[],
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
SETTINGS_COLUMNS = """
ALTER TABLE project_settings
    ADD COLUMN IF NOT EXISTS main_branch TEXT,
    ADD COLUMN IF NOT EXISTS overview_pins TEXT[]
"""

SETTINGS = ("variant_tags", "main_branch", "overview_pins")


def project_settings(conn: psycopg.Connection, project: str) -> dict:
    """What this project has chosen about how its dashboards are built."""
    with conn.cursor() as cur:
        cur.execute(SETTINGS_TABLE)
        cur.execute(SETTINGS_COLUMNS)
        cur.execute(
            f"SELECT {', '.join(SETTINGS)} FROM project_settings WHERE project = %s", (project,)
        )
        row = cur.fetchone()

    conn.commit()
    if row is None:
        return {}

    return {name: value for name, value in zip(SETTINGS, row, strict=True) if value}


def save_project_settings(conn: psycopg.Connection, project: str, **values) -> None:
    """Store the settings given, leaving the rest of the row alone."""
    values = {k: v for k, v in values.items() if k in SETTINGS and v}
    if not values:
        return

    columns = ", ".join(values)
    placeholders = ", ".join(f"%({k})s" for k in values)
    updates = ", ".join(f"{k} = EXCLUDED.{k}" for k in values)
    with conn.cursor() as cur:
        cur.execute(SETTINGS_TABLE)
        cur.execute(SETTINGS_COLUMNS)
        cur.execute(
            f"INSERT INTO project_settings (project, {columns}) "
            f"VALUES (%(project)s, {placeholders}) "
            f"ON CONFLICT (project) DO UPDATE SET {updates}, updated_at = now()",
            {"project": project, **values},
        )

    conn.commit()


def list_projects(conn: psycopg.Connection) -> list:
    return fetch_column(conn, "SELECT DISTINCT project FROM builds ORDER BY 1")
