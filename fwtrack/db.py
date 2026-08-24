"""Database access: connection handling and the writes a build produces."""

import os

import psycopg
from psycopg.types.json import Jsonb

from .log import get_logger

logger = get_logger(__name__)

DSN_ENV = "FWTRACK_DSN"

INSERT_BUILD = """
INSERT INTO builds (project, built_at, commit, branch, version, origin, dirty, toolchain, tags)
VALUES (%(project)s, %(built_at)s, %(commit)s, %(branch)s, %(version)s,
        %(origin)s, %(dirty)s, %(toolchain)s, %(tags)s)
ON CONFLICT (project, commit, built_at, tags) DO UPDATE
    SET branch    = EXCLUDED.branch,
        version   = EXCLUDED.version,
        origin    = EXCLUDED.origin,
        dirty     = EXCLUDED.dirty,
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


def get_dsn() -> str:
    dsn = os.getenv(DSN_ENV)
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
    with conn.cursor() as cur:
        cur.execute(INSERT_BUILD, {**build, "tags": Jsonb(build["tags"])})
        build_id = cur.fetchone()[0]

        cur.executemany(
            INSERT_USAGE,
            [
                {k: v for k, v in {"build_id": build_id, **region}.items() if k != "thresholds"}
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
                if region["thresholds"]
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


def list_projects(conn: psycopg.Connection) -> list:
    return fetch_column(conn, "SELECT DISTINCT project FROM builds ORDER BY 1")
