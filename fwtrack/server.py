"""HTTP ingest endpoint, so CI never needs database credentials.

Exposing Postgres to build runners means handing every pipeline a database
password and opening a port that speaks a protocol with far more surface than
this needs. A single authenticated POST is enough: a leaked token can write
build metrics and nothing else, and the database stays on the loopback
interface.

Runs behind the same reverse proxy as everything else, under /ingest/ rather
than /api/ so it does not collide with Grafana's own routes. It also owns the
schema: every start brings the database up to the version that is running.
"""

import json
import os
import secrets
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, db
from .config import MAX_TAG_VALUE, TAG_NAME
from .grafana import gen_dashboard
from .log import get_logger, setup_logging

logger = get_logger(__name__)

TOKEN_ENV = "FWTRACK_INGEST_TOKEN"
# Where Grafana provisions dashboards from. Set in the bundled stack; without it
# dashboards are left to fwtrack-dash.
DASHBOARDS_ENV = "FWTRACK_DASHBOARDS_DIR"
MAX_BODY = 1 << 20  # a build record is a few kilobytes; anything larger is a mistake
MAX_TAGS = 32
MAX_REGIONS = 256

REQUIRED_BUILD_FIELDS = {"project", "built_at", "commit", "branch", "origin", "dirty"}
REQUIRED_REGION_FIELDS = {"region", "area", "used"}


def validate(payload) -> tuple[dict, list]:
    """The build and its regions, or ValueError saying what is wrong with them.

    Strict about what would otherwise surface as a 500 from deep inside the
    database driver, or worse be stored and break a dashboard later: a tag the
    dashboards cannot name, a size that is not a number.
    """
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object with build and regions")
    build, regions = payload.get("build"), payload.get("regions")
    if not isinstance(build, dict) or not isinstance(regions, list):
        raise ValueError("expected a JSON object with build and regions")

    missing = REQUIRED_BUILD_FIELDS - set(build)
    if missing:
        raise ValueError(f"build is missing {', '.join(sorted(missing))}")
    build = dict(build)
    if not isinstance(build["dirty"], bool):
        raise ValueError("build.dirty must be true or false")
    build["built_at"] = datetime.fromisoformat(str(build["built_at"]))

    tags = build.get("tags") or {}
    if not isinstance(tags, dict) or len(tags) > MAX_TAGS:
        raise ValueError(f"build.tags must be an object of at most {MAX_TAGS} entries")
    # Stored as text, the way every client sends them: {"cfg": 2} and
    # {"cfg": "2"} would otherwise be two different builds of the same variant.
    build["tags"] = {str(k): str(v) for k, v in tags.items()}
    for key, value in build["tags"].items():
        if not TAG_NAME.fullmatch(key):
            raise ValueError(f"tag '{key}': names are letters, digits and underscores")
        if not 0 < len(value) <= MAX_TAG_VALUE:
            raise ValueError(f"tag '{key}': values are 1 to {MAX_TAG_VALUE} characters")

    if len(regions) > MAX_REGIONS:
        raise ValueError(f"at most {MAX_REGIONS} regions")
    for region in regions:
        if not isinstance(region, dict):
            raise ValueError("each region must be an object")
        missing = REQUIRED_REGION_FIELDS - set(region)
        if missing:
            raise ValueError(f"region is missing {', '.join(sorted(missing))}")
        for field in ("used", "total"):
            value = region.get(field)
            if value is None and field == "total":
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"region {region['region']}: {field} must be a whole number")
        thresholds = region.get("thresholds")
        if thresholds is not None and not (
            isinstance(thresholds, list)
            and all(isinstance(t, int | float) and 0 <= t <= 1000 for t in thresholds)
        ):
            raise ValueError(f"region {region['region']}: thresholds must be a list of percentages")

    return build, regions


def validate_dashboard(section) -> dict | None:
    """The [dashboard] settings a build carries, or ValueError."""
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ValueError("dashboard must be an object")

    branch = section.get("main_branch")
    if branch is not None and not (isinstance(branch, str) and 0 < len(branch) <= MAX_TAG_VALUE):
        raise ValueError("dashboard.main_branch must be a branch name")
    for field in ("variant_tags", "overview_pins"):
        value = section.get(field)
        if value is not None and not (
            isinstance(value, list)
            and len(value) <= MAX_TAGS
            and all(isinstance(v, str) for v in value)
        ):
            raise ValueError(f"dashboard.{field} must be a list of strings")

    if any(not TAG_NAME.fullmatch(t) for t in section.get("variant_tags") or []):
        raise ValueError("dashboard.variant_tags: names are letters, digits and underscores")
    for pin in section.get("overview_pins") or []:
        key, sep, value = pin.partition("=")
        if not sep or not TAG_NAME.fullmatch(key) or not 0 < len(value) <= MAX_TAG_VALUE:
            raise ValueError(f"dashboard.overview_pins: '{pin}' is not KEY=VALUE")

    return {f: section.get(f) for f in ("main_branch", "variant_tags", "overview_pins")}


# One at a time: a build_all reports variant after variant, and two writers of
# the same files would only race each other.
_refresh = threading.Lock()


def refresh_dashboards(project: str) -> None:
    """Regenerate a project's dashboards in the background, if this stack serves them."""
    out_dir = os.getenv(DASHBOARDS_ENV)
    if not out_dir:
        return

    def work():
        with _refresh:
            try:
                args = gen_dashboard.parse_args(["--project", project, "--out-dir", out_dir])
                with db.connect() as conn:
                    gen_dashboard.generate(conn, project, args)
            except Exception as e:
                logger.error(f"{project}: dashboards not regenerated: {e}")

    threading.Thread(target=work, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = "fwtrack"
    sys_version = ""
    # Per read: a client that connects and goes quiet would otherwise hold its
    # thread for ever, and enough of them hold every thread there is.
    timeout = 30

    def log_message(self, fmt, *args):
        logger.info(f"{self.address_string()} {fmt % args}")

    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        header = self.headers.get("Authorization", "").strip()
        supplied = header[7:].strip() if header.startswith("Bearer ") else ""
        # Constant time: a plain == leaks the token one character at a time to
        # anyone willing to measure. Bytes, because compare_digest refuses a
        # str with anything outside ASCII, and a header can carry anything.
        return secrets.compare_digest(supplied.encode(), self.server.token.encode())

    def do_GET(self):
        if self.path.rstrip("/") != "/ingest/health":
            self._reply(404, {"error": "not found"})
            return

        # The database too: a monitor watching this route should see the
        # outage that matters, not just that the process is alive.
        try:
            with db.connect() as conn:
                conn.execute("SELECT 1")
        except Exception as e:
            logger.error(f"Health check: {e}")
            self._reply(503, {"status": "database unreachable", "version": __version__})
            return
        self._reply(200, {"status": "ok", "version": __version__})

    def do_POST(self):
        if self.path.rstrip("/") != "/ingest/builds":
            self._reply(404, {"error": "not found"})
            return

        if not self._authorised():
            self._reply(401, {"error": "unauthorised"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0:
            self._reply(411, {"error": "Content-Length required"})
            return
        if length > MAX_BODY:
            self._reply(413, {"error": "body too large"})
            return

        try:
            payload = json.loads(self.rfile.read(length))
            build, regions = validate(payload)
            dashboard = validate_dashboard(payload.get("dashboard"))
        except (ValueError, KeyError, TypeError) as e:
            self._reply(400, {"error": str(e)})
            return

        project = build["project"]
        try:
            with db.connect() as conn:
                tags = set(db.discover_variant_tags(conn, project))
                areas = set(db.discover_areas(conn, project))
                build_id = db.write_build(conn, build, regions)
                changed = dashboard is not None and db.sync_project_settings(
                    conn, project, dashboard
                )
        except Exception as e:
            logger.error(f"Write failed: {e}")
            self._reply(500, {"error": "write failed"})
            return

        self._reply(201, {"build_id": build_id, "regions": len(regions)})

        # A new project, a new dimension, a new memory area or new settings is
        # something the dashboards cannot show until they are generated again.
        if (
            changed
            or not areas
            or set(build["tags"]) - tags
            or {r["area"] for r in regions} - areas
        ):
            refresh_dashboards(project)


def prepare_database() -> None:
    """Schema and reader account, before the first request can need them.

    A failure is logged rather than fatal: the database may still hold a
    schema the running code can work with, and refusing to start would turn
    a partial upgrade into a full outage.
    """
    try:
        with db.connect() as conn:
            db.apply_schema(conn, (os.getenv(db.READER_PASSWORD_ENV) or "").strip() or None)
        logger.info("Schema is up to date")
    except Exception as e:
        logger.error(f"Could not update the schema: {e}")


def main():
    setup_logging()

    # Stripped for the same reason the client strips it: a secret that travelled
    # through a settings page may well arrive with a newline attached.
    token = (os.getenv(TOKEN_ENV) or "").strip()
    if not token:
        raise SystemExit(f"{TOKEN_ENV} must be set")

    prepare_database()

    port = int(os.getenv("FWTRACK_INGEST_PORT", "8099"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.token = token

    logger.info(f"fwtrack {__version__} listening on :{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
