"""HTTP ingest endpoint, so CI never needs database credentials.

Exposing Postgres to build runners means handing every pipeline a database
password and opening a port that speaks a protocol with far more surface than
this needs. A single authenticated POST is enough: a leaked token can write
build metrics and nothing else, and the database stays on the loopback
interface.

Runs behind the same reverse proxy as everything else, under /ingest/ rather
than /api/ so it does not collide with Grafana's own routes.
"""

import json
import os
import secrets
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db
from .log import get_logger, setup_logging

logger = get_logger(__name__)

TOKEN_ENV = "FWTRACK_INGEST_TOKEN"
MAX_BODY = 1 << 20  # a build record is a few kilobytes; anything larger is a mistake

REQUIRED_BUILD_FIELDS = {"project", "built_at", "commit", "branch", "origin", "dirty"}
REQUIRED_REGION_FIELDS = {"region", "area", "used"}


class Handler(BaseHTTPRequestHandler):
    server_version = "fwtrack"
    sys_version = ""

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
        expected = self.server.token
        header = self.headers.get("Authorization", "").strip()
        supplied = header[7:].strip() if header.startswith("Bearer ") else ""
        # Constant time: a plain == leaks the token one character at a time to
        # anyone willing to measure.
        return secrets.compare_digest(supplied, expected)

    def do_GET(self):
        if self.path.rstrip("/") == "/ingest/health":
            self._reply(200, {"status": "ok"})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/ingest/builds":
            self._reply(404, {"error": "not found"})
            return

        if not self._authorised():
            self._reply(401, {"error": "unauthorised"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            self._reply(413, {"error": "body missing or too large"})
            return

        try:
            payload = json.loads(self.rfile.read(length))
            build = payload["build"]
            regions = payload["regions"]
            missing = REQUIRED_BUILD_FIELDS - set(build)
            if missing:
                raise ValueError(f"build is missing {', '.join(sorted(missing))}")
            for region in regions:
                missing = REQUIRED_REGION_FIELDS - set(region)
                if missing:
                    raise ValueError(f"region is missing {', '.join(sorted(missing))}")
            build["built_at"] = datetime.fromisoformat(build["built_at"])
        except (ValueError, KeyError, TypeError) as e:
            self._reply(400, {"error": str(e)})
            return

        try:
            with db.connect() as conn:
                build_id = db.write_build(conn, build, regions)
        except Exception as e:
            logger.error(f"Write failed: {e}")
            self._reply(500, {"error": "write failed"})
            return

        self._reply(201, {"build_id": build_id, "regions": len(regions)})


def main():
    setup_logging()

    # Stripped for the same reason the client strips it: a secret that travelled
    # through a settings page may well arrive with a newline attached.
    token = (os.getenv(TOKEN_ENV) or "").strip()
    if not token:
        raise SystemExit(f"{TOKEN_ENV} must be set")

    port = int(os.getenv("FWTRACK_INGEST_PORT", "8099"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.token = token

    logger.info(f"Listening on :{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
