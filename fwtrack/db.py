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
INSERT INTO memory_usage (build_id, region, area, used, total, thresholds)
VALUES (%(build_id)s, %(region)s, %(area)s, %(used)s, %(total)s, %(thresholds)s)
ON CONFLICT (build_id, region) DO UPDATE
    SET area       = EXCLUDED.area,
        used       = EXCLUDED.used,
        total      = EXCLUDED.total,
        thresholds = EXCLUDED.thresholds
"""


def get_dsn() -> str:
    dsn = os.getenv(DSN_ENV)
    if not dsn:
        raise RuntimeError(f"{DSN_ENV} is not set (see .env.example)")

    return dsn


def connect(dsn: str | None = None) -> psycopg.Connection:
    return psycopg.connect(dsn or get_dsn())


def write_build(conn: psycopg.Connection, build: dict, regions: list) -> int:
    """Store one build and its regions, replacing any previous run of the same build.

    Both statements upsert so that re-running CI on the same commit refreshes the
    numbers instead of piling up duplicate rows.
    """
    with conn.cursor() as cur:
        cur.execute(INSERT_BUILD, {**build, "tags": Jsonb(build["tags"])})
        build_id = cur.fetchone()[0]

        cur.executemany(
            INSERT_USAGE,
            [{"build_id": build_id, **region} for region in regions],
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
