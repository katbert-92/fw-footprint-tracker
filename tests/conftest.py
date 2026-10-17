import os
import uuid
from pathlib import Path

import psycopg
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
CORTEX_M = FIXTURES / "cortex-m"


@pytest.fixture
def v1():
    return CORTEX_M / "v1" / "fw.elf", CORTEX_M / "v1" / "fw.map"


@pytest.fixture
def v2():
    return CORTEX_M / "v2" / "fw.elf", CORTEX_M / "v2" / "fw.map"


@pytest.fixture
def dsn():
    """A throwaway database on the server FWTRACK_TEST_DSN points at.

    Skipped without one. The database is created for the test and dropped
    after it, so the server can be any Postgres the tests may write to.
    """
    base = os.getenv("FWTRACK_TEST_DSN")
    if not base:
        pytest.skip("FWTRACK_TEST_DSN is not set")

    name = f"fwtrack_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    info = psycopg.conninfo.conninfo_to_dict(base)
    yield psycopg.conninfo.make_conninfo(**{**info, "dbname": name})
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
