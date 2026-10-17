"""First-run check: are the services up, is the schema there, is there data.

Meant to be the first thing anyone runs after starting the stack, and to be safe
to run again at any time. Every step reports what it found rather than failing
silently, because the usual first-run problems -- a database that is not up yet,
a schema that was never applied, a project name that does not match what the
build actually records -- all look identical from the dashboard: empty panels.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from dotenv import find_dotenv, load_dotenv

from . import db
from .grafana.gen_dashboard import USER_AGENT, auth_header
from .log import get_logger, setup_logging

logger = get_logger(__name__)

OK, FAIL, WARN = "✅", "❌", "⚠️ "


def parse_args():
    parser = argparse.ArgumentParser(description="Check the stack and set up a project")
    parser.add_argument("--project", help="Generate a dashboard for this project")
    parser.add_argument(
        "--apply-schema",
        action="store_true",
        help="Create or update the tables (the bundled ingest service does this itself)",
    )

    return parser.parse_args()


def check_database(apply_schema: bool) -> bool:
    try:
        conn = db.connect()
    except Exception as e:
        print(f"{FAIL} database: {e}")
        print("     Check FWTRACK_DSN and that the container is up: docker compose ps")
        return False

    with conn:
        print(f"{OK} database: connected")

        if apply_schema:
            db.apply_schema(conn)
            print(f"{OK} schema: applied")
        elif not db.schema_ready(conn):
            print(f"{FAIL} schema: tables are missing")
            print("     Apply it with: fwtrack-init --apply-schema")
            return False
        else:
            print(f"{OK} schema: present")

        report_contents(conn)

    return True


def report_contents(conn) -> None:
    projects = db.list_projects(conn)
    if not projects:
        print(f"{WARN}data: no builds recorded yet")
        print("     Run a build with FWTRACK_ENABLE=1 to record the first one")
        return

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT project, count(*), min(built_at)::date, max(built_at)::date
            FROM builds GROUP BY project ORDER BY project
            """
        )
        for project, count, first, last in cur.fetchall():
            span = f"{first}" if first == last else f"{first} .. {last}"
            print(f"{OK} data: {project} — {count} builds ({span})")


def check_grafana() -> None:
    url = os.getenv("GRAFANA_URL")
    if not url:
        print(f"{WARN}grafana: GRAFANA_URL not set, skipping")
        print("     The bundled container provisions itself; this is only for a remote one")
        return

    auth = auth_header()
    headers = {"User-Agent": USER_AGENT}
    if auth:
        headers["Authorization"] = auth

    request = urllib.request.Request(f"{url.rstrip('/')}/api/health", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            health = json.loads(response.read())
        print(f"{OK} grafana: {url} (version {health.get('version', '?')})")
    except (urllib.error.URLError, OSError) as e:
        print(f"{FAIL} grafana: {url} unreachable: {e}")


def main():
    setup_logging()
    args = parse_args()
    load_dotenv(find_dotenv(usecwd=True))

    print()
    if not check_database(args.apply_schema):
        sys.exit(1)

    check_grafana()

    if args.project:
        print()
        # Imported here so a plain health check does not need the generator.
        from .grafana import gen_dashboard

        sys.argv = ["fwtrack-dash", "--project", args.project]
        gen_dashboard.main()

    print()


if __name__ == "__main__":
    main()
