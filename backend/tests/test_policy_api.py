"""Policy / pause / defaults HTTP endpoints."""
import httpx
import respx
from starlette.testclient import TestClient

from app.db import Database
from app.main import app
from app.state import get_db, get_registry
from tests.test_chain import A, B, make_registry

TVDB = 99


def _client(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    app.dependency_overrides[get_registry] = make_registry
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _teardown():
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_registry, None)


def _mock_on_4k():
    respx.get(f"{A}/api/v3/series").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{B}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 10, "tvdbId": TVDB, "title": "Mad Men"}])
    )


@respx.mock
def test_put_then_get_policy(tmp_path):
    c = _client(tmp_path)
    try:
        body = {"preferredTier": "4k",
                "fallbacks": [{"instanceId": "1080p", "afterDays": 14}],
                "allowSplit": False}
        assert c.put(f"/api/series/{TVDB}/policy", json=body).status_code == 200
        got = c.get(f"/api/series/{TVDB}/policy").json()
        assert got["source"] == "stored"
        assert got["policy"]["allowSplit"] is False
        assert got["policy"]["fallbacks"][0]["afterDays"] == 14
    finally:
        _teardown()


@respx.mock
def test_get_policy_derives_from_chain_when_no_intent(tmp_path):
    c = _client(tmp_path)
    try:
        _mock_on_4k()
        got = c.get(f"/api/series/{TVDB}/policy").json()
        assert got["source"] == "derived"
        assert got["policy"]["preferredTier"] == "4k"
        assert len(got["policy"]["fallbacks"]) == 2
        assert got["paused"] is False
    finally:
        _teardown()


@respx.mock
def test_pause_and_resume(tmp_path):
    c = _client(tmp_path)
    try:
        _mock_on_4k()
        assert c.post(f"/api/series/{TVDB}/pause").status_code == 200
        assert c.get(f"/api/series/{TVDB}/policy").json()["paused"] is True
        assert c.post(f"/api/series/{TVDB}/resume").status_code == 200
        assert c.get(f"/api/series/{TVDB}/policy").json()["paused"] is False
    finally:
        _teardown()


@respx.mock
def test_defaults_put_get(tmp_path):
    c = _client(tmp_path)
    try:
        assert c.get("/api/settings/defaults").json() == {}
        c.put("/api/settings/defaults", json={"allowSplit": False, "escalateAfterDays": 14})
        assert c.get("/api/settings/defaults").json()["escalateAfterDays"] == 14
    finally:
        _teardown()
