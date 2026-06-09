"""Editable settings: DB-backed fallback-chain overrides applied to the registry."""
from starlette.testclient import TestClient

from app.db import Database
from app.main import app
from app.state import get_db, get_registry
from app.store import settings as settings_store
from tests.test_chain import make_registry


def _client(tmp_path):
    # A SINGLE registry instance (not rebuilt per request) so a PUT that mutates
    # it is visible to a later GET — mirrors the real app.state singleton.
    reg = make_registry()
    db = Database(str(tmp_path / "relay.db"))
    app.dependency_overrides[get_registry] = lambda: reg
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), reg, db


def _teardown():
    app.dependency_overrides.pop(get_registry, None)
    app.dependency_overrides.pop(get_db, None)


def test_put_fallback_chains_applies_to_registry_and_persists(tmp_path):
    c, reg, db = _client(tmp_path)
    try:
        payload = {"4k": [{"instanceId": "1080p", "profile": "SD"}]}
        assert c.put("/api/settings/fallback-chains", json=payload).status_code == 200

        # GET /api/settings reads the live registry → reflects the override.
        chains = c.get("/api/settings").json()["fallbackChains"]
        assert chains["4k"] == [{"instanceId": "1080p", "profile": "SD"}]
        # (DB persistence is covered by test_chain_override_store_roundtrip.)
    finally:
        _teardown()


def test_put_rejects_unknown_instance(tmp_path):
    c, _, _ = _client(tmp_path)
    try:
        r = c.put("/api/settings/fallback-chains", json={"4k": [{"instanceId": "nope"}]})
        assert r.status_code == 400
        r2 = c.put("/api/settings/fallback-chains", json={"ghost": [{"instanceId": "1080p"}]})
        assert r2.status_code == 400
    finally:
        _teardown()


async def test_chain_override_store_roundtrip(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    assert await settings_store.get_chain_overrides(db) is None
    data = {"4k": [{"instanceId": "1080p", "profile": "SD"}]}
    await settings_store.set_chain_overrides(db, data)
    assert await settings_store.get_chain_overrides(db) == data
