"""Generate a Grafana dashboard for one project.

One dashboard per project rather than a single one with a project variable: a
variable is a filter, not a boundary, and anyone who can open the dashboard can
switch it. Permissions in Grafana are granted on folders, so each project gets
its own dashboard in its own folder with the project baked in as a constant.

Nothing about the project is hardcoded here. The dimensions builds are split by,
the memory areas and the warning levels all come out of the database, so a project that calls its dimensions board and build_type
works exactly like one that calls them cfg and bsp.

    fwtrack-dash --project dmd                 # write JSON for provisioning
    fwtrack-dash --project dmd --push          # upload into a running Grafana
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from .. import db
from ..log import get_logger, setup_logging
from . import panels, queries

logger = get_logger(__name__)

DATASOURCE_UID = "fwtrack-postgres"
DEFAULT_OUT_DIR = Path("deploy/grafana/dashboards")

# A reverse proxy in front of Grafana may reject the default Python-urllib
# user agent outright, which surfaces as an opaque 403.
USER_AGENT = "fw-footprint-tracker/1.0"

# Used when the project has no data yet, so that a dashboard still generates.
FALLBACK_VARIANT_TAGS = ["type", "tag", "cfg"]
FALLBACK_AREAS = ["flash", "ram"]


def parse_args():
    parser = argparse.ArgumentParser(description="Generate a Grafana dashboard for one project")
    parser.add_argument("--project", required=True, help="Project name")
    parser.add_argument(
        "--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Where to write the dashboard JSON"
    )
    parser.add_argument(
        "--datasource-uid", default=DATASOURCE_UID, help="Grafana datasource uid to point at"
    )
    parser.add_argument("--push", action="store_true", help="Upload into a running Grafana")
    parser.add_argument("--folder-uid", help="Grafana folder to upload into")
    parser.add_argument(
        "--variant-tags", help="Comma separated dimensions, overriding what the database reports"
    )
    parser.add_argument(
        "--exclude-tags",
        default="",
        help="Comma separated tags to leave out of the filters, without touching the history. "
        "Use for a dimension a project has stopped recording, or one that turned out to "
        "duplicate another",
    )

    return parser.parse_args()


def query_variable(name: str, label: str, sql: str, multi: bool = False) -> dict:
    return {
        "name": name,
        "label": label,
        "type": "query",
        "datasource": {"type": "grafana-postgresql-datasource", "uid": "${datasource}"},
        # Object form with an explicit table format. Handed to the Postgres
        # datasource as a bare string it may be run as a time series query, and
        # with no time column the variable silently ends up with no options.
        "query": {
            "refId": f"tempvar-{name}",
            "rawSql": sql,
            "rawQuery": True,
            "format": "table",
            "editorMode": "code",
        },
        # On time range change: the queries are filtered by it, so narrowing the
        # range must narrow the choices too.
        "refresh": 2,
        "multi": multi,
        "includeAll": multi,
        # A multi variable with no current value comes back empty, the IN clause
        # gets nothing, and panels show No data for no visible reason.
        "current": {"text": ["All"], "value": ["$__all"]} if multi else {},
        "options": [],
        "sort": 1,
    }


def build_variables(project: str, variant_tags: list, datasource_uid: str) -> list:
    variables = [
        {
            "name": "datasource",
            "label": "Datasource",
            "type": "datasource",
            "query": "grafana-postgresql-datasource",
            "current": {"text": datasource_uid, "value": datasource_uid},
            "refresh": 1,
        },
        {
            "name": "project",
            "type": "constant",
            "query": project,
            "current": {"text": project, "value": project},
            "hide": 2,
        },
    ]

    # Each dimension narrows the next one, so a combination that never existed
    # cannot be assembled from the dropdowns.
    variables += [
        query_variable(tag, tag, queries.variable_values(tag, variant_tags[:i]))
        for i, tag in enumerate(variant_tags)
    ]

    variables += [
        query_variable("branch", "Branch", queries.simple_values("branch"), multi=True),
        query_variable("origin", "Build origin", queries.simple_values("origin"), multi=True),
        # Multiple choice, like the branch: being made to pick one person to see
        # the chart at all would hide everyone else's work.
        query_variable("author", "Author", queries.author_values(), multi=True),
        query_variable("area", "Memory area", queries.simple_values("area"), multi=True),
        query_variable("region", "Region", queries.simple_values("region"), multi=True),
    ]

    return variables


def build_dashboard(project: str, variant_tags: list, areas: list,
                    limits: dict, datasource_uid: str) -> dict:
    # Repeated panels split the top row evenly between the memory areas.
    width = max(24 // max(len(areas), 1), 3)

    return {
        "uid": f"fwtrack-{project}",
        "title": f"Firmware memory — {project}",
        "description": (
            f"Memory footprint of '{project}' across builds. "
            f"Dimensions: {', '.join(variant_tags)}. "
            "Generated by fwtrack-dash; edits made here are overwritten on regeneration."
        ),
        "tags": ["firmware", "memory", project],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 0,
        "refresh": "",
        "editable": True,
        "graphTooltip": 1,
        # Builds are rare enough that the usual six hour default reads as a
        # broken dashboard.
        "time": {"from": "now-90d", "to": "now"},
        "templating": {"list": build_variables(project, variant_tags, datasource_uid)},
        "panels": [
            panels.stat_last_delta(variant_tags, width),
            panels.bargauge_usage(variant_tags, width, limits),
            panels.table_builds(variant_tags),
            panels.row_per_area(),
            panels.timeseries_trend(variant_tags),
            panels.barchart_by_build(variant_tags),
            panels.barchart_delta(variant_tags),
        ],
    }


def auth_header() -> str | None:
    token = os.getenv("GRAFANA_TOKEN")
    if token:
        return f"Bearer {token}"

    user, password = os.getenv("GRAFANA_USER"), os.getenv("GRAFANA_PASSWORD")
    if user and password:
        return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    return None


def push(dashboard: dict, folder_uid: str | None) -> None:
    url, auth = os.getenv("GRAFANA_URL"), auth_header()
    if not url or not auth:
        logger.error("Set GRAFANA_URL and either GRAFANA_TOKEN or GRAFANA_USER/GRAFANA_PASSWORD")
        sys.exit(1)

    payload = {"dashboard": dashboard, "overwrite": True, "message": "generated by fwtrack-dash"}
    if folder_uid:
        payload["folderUid"] = folder_uid

    request = urllib.request.Request(
        f"{url.rstrip('/')}/api/dashboards/db",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": auth,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            logger.info(f"Uploaded: {json.loads(response.read()).get('url')}")
    except urllib.error.HTTPError as e:
        logger.error(f"Grafana returned {e.code}: {e.read().decode()}")
        sys.exit(1)


def main():
    setup_logging()
    args = parse_args()
    load_dotenv(find_dotenv(usecwd=True))

    with db.connect() as conn:
        if not db.schema_ready(conn):
            logger.error("Schema is missing; apply deploy/schema.sql first")
            sys.exit(1)

        variant_tags = (
            [t.strip() for t in args.variant_tags.split(",") if t.strip()]
            if args.variant_tags
            else db.discover_variant_tags(conn, args.project)
        )
        areas = db.discover_areas(conn, args.project)
        limits = db.region_limits(conn, args.project)

    excluded = {t.strip() for t in args.exclude_tags.split(",") if t.strip()}
    if excluded:
        dropped = sorted(excluded & set(variant_tags))
        if dropped:
            logger.info(f"Excluded from the filters: {', '.join(dropped)}")
        variant_tags = [t for t in variant_tags if t not in excluded]

    if not variant_tags:
        logger.warning(f"No dimensions recorded for '{args.project}', using defaults")
        variant_tags = list(FALLBACK_VARIANT_TAGS)
    if not areas:
        logger.warning(f"No data for '{args.project}' yet, using default areas")
        areas = list(FALLBACK_AREAS)

    logger.info(f"Dimensions: {', '.join(variant_tags)}")
    logger.info(f"Memory areas: {', '.join(areas)}")

    dashboard = build_dashboard(
        args.project, variant_tags, areas, limits, args.datasource_uid
    )

    # One directory per project: the dashboard provider turns directories into
    # Grafana folders, and folders are where permissions are granted.
    out_dir = args.out_dir / args.project
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"fwtrack-{args.project}.json"
    out_path.write_text(json.dumps(dashboard, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info(f"Written {out_path}")

    if args.push:
        push(dashboard, args.folder_uid)


if __name__ == "__main__":
    main()
