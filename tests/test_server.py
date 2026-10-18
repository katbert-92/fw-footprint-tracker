import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from fwtrack import server

BUILD = {
    "project": "blinky",
    "built_at": "2026-08-05T10:00:00+00:00",
    "commit": "deadbee",
    "branch": "dev",
    "origin": "ci",
    "dirty": False,
}
REGIONS = [{"region": "FLASH", "area": "flash", "used": 1000, "total": 4096}]


def payload(**build):
    return {"build": {**BUILD, **build}, "regions": [dict(r) for r in REGIONS]}


def test_minimal_build_is_valid():
    build, regions = server.validate(payload())

    assert build["built_at"].year == 2026
    assert build["tags"] == {}
    assert regions[0]["used"] == 1000


def test_tag_values_are_stored_as_text():
    build, _ = server.validate(payload(tags={"cfg": 2}))

    assert build["tags"] == {"cfg": "2"}


@pytest.mark.parametrize(
    "bad",
    [
        payload(tags={"board-rev": "a"}),
        payload(dirty="no"),
        payload(built_at="yesterday"),
        {"build": BUILD, "regions": [{"region": "FLASH", "area": "flash", "used": "1k"}]},
        {"build": BUILD, "regions": [{"region": "FLASH", "area": "flash"}]},
        {"build": BUILD},
        [],
    ],
)
def test_invalid_payloads_are_refused(bad):
    with pytest.raises(ValueError):
        server.validate(bad)


@pytest.fixture
def endpoint(monkeypatch):
    written = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql):
            pass

    monkeypatch.setattr(server.db, "connect", lambda: Connection())
    monkeypatch.setattr(
        server.db, "write_build", lambda conn, build, regions: written.append(build) or 7
    )

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.token = "s3cret"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], written
    httpd.shutdown()


def call(port, method, path, body=None, token="s3cret"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=data, headers={"Authorization": f"Bearer {token}"})
    response = conn.getresponse()
    return response.status, json.loads(response.read() or b"{}")


def test_health_needs_no_token(endpoint):
    port, _ = endpoint

    status, body = call(port, "GET", "/ingest/health", token="")

    assert status == 200 and body["status"] == "ok"


def test_build_is_recorded(endpoint):
    port, written = endpoint

    status, body = call(port, "POST", "/ingest/builds", payload())

    assert status == 201 and body == {"build_id": 7, "regions": 1}
    assert written[0]["project"] == "blinky"


@pytest.mark.parametrize("token", ["", "wrong", "s3cret-but-longer", "s3crét"])
def test_wrong_token_is_refused(endpoint, token):
    port, written = endpoint

    status, _ = call(port, "POST", "/ingest/builds", payload(), token=token)

    assert status == 401 and not written


def test_invalid_build_is_a_400_not_a_500(endpoint):
    port, written = endpoint

    status, body = call(port, "POST", "/ingest/builds", payload(tags={"a b": "c"}))

    assert status == 400 and "a b" in body["error"]
    assert not written
