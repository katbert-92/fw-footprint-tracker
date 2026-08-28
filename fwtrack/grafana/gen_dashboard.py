"""Generate a Grafana dashboard for one project.

One dashboard per project rather than a single one with a project variable: a
variable is a filter, not a boundary, and anyone who can open the dashboard can
switch it. Permissions in Grafana are granted on folders, so each project gets
its own dashboard in its own folder with the project baked in as a constant.

Nothing about the project is hardcoded here. The dimensions builds are split by,
the memory areas and the warning levels all come out of the database, so a
project that calls its dimensions board and build_type works exactly like one
that calls them cfg and bsp.

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

from .. import __version__, db
from ..log import get_logger, setup_logging
from . import panels, queries

logger = get_logger(__name__)

DATASOURCE_UID = "fwtrack-postgres"
DEFAULT_OUT_DIR = Path("deploy/grafana/dashboards")

# A reverse proxy in front of Grafana may reject the default Python-urllib
# user agent outright, which surfaces as an opaque 403.
USER_AGENT = f"fw-footprint-tracker/{__version__}"

# Used when the project has no data yet, so that a dashboard still generates.
FALLBACK_VARIANT_TAGS = ["type", "tag", "cfg"]
FALLBACK_AREAS = ["flash", "ram"]


def parse_args():
    parser = argparse.ArgumentParser(description="Generate a Grafana dashboard for one project")
    parser.add_argument("--project", help="Project name")
    parser.add_argument(
        "--all", action="store_true", help="Every project that has recorded a build"
    )
    parser.add_argument(
        "--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Where to write the dashboard JSON"
    )
    parser.add_argument(
        "--datasource-uid", default=DATASOURCE_UID, help="Grafana datasource uid to point at"
    )
    parser.add_argument("--push", action="store_true", help="Upload into a running Grafana")
    parser.add_argument("--folder-uid", help="Grafana folder to upload into")
    parser.add_argument(
        "--variant-tags",
        help="Comma separated dimensions, in the order their filters should narrow each other. "
        "Remembered for the project, so later regenerations keep it",
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


def annotation_toolchain(variant_tags: list) -> dict:
    """A dashed line on every time panel where the compiler changed."""
    return {
        "name": "Toolchain",
        "datasource": {"type": "grafana-postgresql-datasource", "uid": "${datasource}"},
        "enable": True,
        "hide": False,
        "iconColor": "purple",
        "target": {
            "refId": "annotation-toolchain",
            "rawSql": queries.toolchain_changes(variant_tags),
            "rawQuery": True,
            "format": "table",
            "editorMode": "code",
        },
    }


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
        "annotations": {"list": [annotation_toolchain(variant_tags)]},
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


def build_activity_dashboard(project: str, datasource_uid: str) -> dict:
    """A second dashboard: the flow of builds rather than what they weigh.

    No variant filters on purpose. "What is going on in this project" is a
    question about all of it, and a dashboard that answers it only for one
    combination of dimensions answers a different question.
    """
    return {
        "uid": f"fwtrack-{project}-activity",
        # Without the project: the folder it lives in already says which one.
        "title": "Build activity",
        "description": (
            f"Builds recorded for '{project}': when they happen, who makes them, "
            "what landed. Generated by fwtrack-dash."
        ),
        "tags": ["firmware", "activity", project],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 0,
        "refresh": "",
        "editable": True,
        "graphTooltip": 0,
        # Shorter than the memory dashboard's: this one is about what is
        # happening now, not about a trend.
        "time": {"from": "now-2d", "to": "now"},
        "templating": {
            "list": [
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
        },
        "panels": [
            panels.stat_activity_totals(),
            panels.timeseries_builds_per_day(),
            panels.barchart_by_hour(),
            panels.barchart_by_weekday(),
            panels.table_authors(),
            panels.table_branches(),
            panels.table_origins(),
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


def generate(conn, project: str, args) -> None:
    """Write both dashboards for one project."""
    discovered = db.discover_variant_tags(conn, project)

    # An order given here replaces the stored one; otherwise the stored one
    # stands, so regenerating never silently reshuffles a dashboard.
    chosen = [t.strip() for t in (args.variant_tags or "").split(",") if t.strip()]
    if chosen:
        db.set_variant_tag_order(conn, project, chosen)
        logger.info(f"Filter order stored for '{project}': {', '.join(chosen)}")
    else:
        chosen = db.variant_tag_order(conn, project)

    # The order decides order only. A dimension the project stopped recording
    # drops out on its own, and one it started recording since appears at the
    # end rather than going missing until someone notices.
    variant_tags = [t for t in chosen if t in discovered]
    variant_tags += [t for t in discovered if t not in variant_tags]

    areas = db.discover_areas(conn, project)
    limits = db.region_limits(conn, project)

    excluded = {t.strip() for t in args.exclude_tags.split(",") if t.strip()}
    if excluded:
        dropped = sorted(excluded & set(variant_tags))
        if dropped:
            logger.info(f"Excluded from the filters: {', '.join(dropped)}")
        variant_tags = [t for t in variant_tags if t not in excluded]

    if not variant_tags:
        logger.warning(f"No dimensions recorded for '{project}', using defaults")
        variant_tags = list(FALLBACK_VARIANT_TAGS)
    if not areas:
        logger.warning(f"No data for '{project}' yet, using default areas")
        areas = list(FALLBACK_AREAS)

    logger.info(f"{project}: dimensions {', '.join(variant_tags)}; areas {', '.join(areas)}")

    dashboard = build_dashboard(project, variant_tags, areas, limits, args.datasource_uid)
    activity = build_activity_dashboard(project, args.datasource_uid)

    # One directory per project: the dashboard provider turns directories into
    # Grafana folders, and folders are where permissions are granted.
    out_dir = args.out_dir / project
    out_dir.mkdir(parents=True, exist_ok=True)

    for name, content in (
        (f"fwtrack-{project}.json", dashboard),
        (f"fwtrack-{project}-activity.json", activity),
    ):
        out_path = out_dir / name
        out_path.write_text(
            json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        logger.info(f"Written {out_path}")

    if args.push:
        push(dashboard, args.folder_uid)
        push(activity, args.folder_uid)


def main():
    setup_logging()
    args = parse_args()
    load_dotenv(find_dotenv(usecwd=True))

    if bool(args.project) == bool(args.all):
        logger.error("Pass either --project NAME or --all")
        sys.exit(1)
    if args.all and args.variant_tags:
        # The order is a property of one project; applying one list to all of
        # them would quietly reorder dashboards nobody asked about.
        logger.error("--variant-tags applies to one project, so not with --all")
        sys.exit(1)

    with db.connect() as conn:
        if not db.schema_ready(conn):
            logger.error("Schema is missing; apply deploy/schema.sql first")
            sys.exit(1)

        projects = db.list_projects(conn) if args.all else [args.project]
        if not projects:
            logger.warning("No project has recorded a build yet, nothing to generate")
            return

        for project in projects:
            generate(conn, project, args)


if __name__ == "__main__":
    main()
