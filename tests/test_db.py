"""Against a real Postgres: FWTRACK_TEST_DSN, or skipped."""

from datetime import UTC, datetime

import psycopg
import pytest

from fwtrack import db


@pytest.fixture
def conn(dsn):
    with psycopg.connect(dsn) as conn:
        db.apply_schema(conn)
        yield conn


def build(**fields):
    return {
        "project": "blinky",
        "built_at": datetime(2026, 8, 5, 10, tzinfo=UTC),
        "commit": "deadbee",
        "branch": "dev",
        "origin": "ci",
        "dirty": False,
        "tags": {"board": "nucleo"},
        **fields,
    }


def regions(*names, used=100):
    return [
        {"region": n, "area": "flash", "used": used, "total": 1000, "thresholds": [80, 90]}
        for n in names
    ]


def test_schema_can_be_applied_again(conn):
    db.apply_schema(conn)

    assert db.schema_ready(conn)


def test_a_view_of_an_older_shape_is_replaced(conn):
    conn.execute("DROP VIEW memory_points")
    conn.execute("CREATE VIEW memory_points AS SELECT id AS build_id, 1 AS extra FROM builds")
    conn.commit()

    db.apply_schema(conn)

    columns = conn.execute("SELECT * FROM memory_points LIMIT 0").description
    assert [c.name for c in columns][-1] == "pcnt"


def test_renaming_a_value_cannot_collide_two_builds(conn):
    db.write_build(conn, build(tags={"cfg": "52362d"}), regions("FLASH"))
    db.write_build(conn, build(tags={"cfg": "NB_B100"}), regions("FLASH"))

    with pytest.raises(RuntimeError, match="collide"):
        db.rename_value(conn, "blinky", "cfg", "52362d", "NB_B100")

    assert db.rename_value(conn, "blinky", "cfg", "52362d", "NB", dry_run=True) == 1
    assert {v["value"] for v in db.tag_values(conn, "blinky", "cfg")} == {"52362d", "NB_B100"}
