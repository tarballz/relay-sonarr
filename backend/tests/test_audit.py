"""User-initiated configuration and series changes are journaled with before/after."""
import asyncio

import httpx
import pytest
import respx
from starlette.testclient import TestClient

from app.auth import verify_access
from app.main import app
from app.obs import kinds
from app.state import get_db, get_registry
from app.store import intents as intent_store
from tests.test_chain import A, make_registry

TVDB = 99


@pytest.fixture
def api(db, journal):
    reg = make_registry()
    app.dependency_overrides[get_registry] = lambda: reg
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_db, None)


def _events(journal, kind):
    return [e for e in journal.pending() if e["kind"] == kind]


def test_defaults_change_records_before_after_and_changed_keys(api, journal):
    api.put("/api/settings/defaults", json={"stalledDays": 3, "minSeeders": 3})
    api.put("/api/settings/defaults", json={"stalledDays": 5, "minSeeders": 3})

    first, second = _events(journal, kinds.CONFIG_DEFAULTS_CHANGED)
    assert first["data"]["changed"] == ["minSeeders", "stalledDays"]
    assert second["data"] == {
        "before": {"stalledDays": 3, "minSeeders": 3},
        "after": {"stalledDays": 5, "minSeeders": 3},
        "changed": ["stalledDays"],
    }
    assert second["source"] == "user"


def test_actor_recorded_when_access_identifies_the_user(api, journal):
    app.dependency_overrides[verify_access] = lambda: "user@example.com"
    try:
        api.put("/api/settings/defaults", json={"minSeeders": 1})
    finally:
        app.dependency_overrides.pop(verify_access, None)
    [ev] = _events(journal, kinds.CONFIG_DEFAULTS_CHANGED)
    assert ev["data"]["actor"] == "user@example.com"


def test_policy_change_is_journaled_for_the_series(api, journal):
    body = {"preferredTier": "4k", "fallbacks": [{"instanceId": "1080p", "afterDays": 14}],
            "allowSplit": False}
    assert api.put(f"/api/series/{TVDB}/policy", json=body).status_code == 200

    [ev] = _events(journal, kinds.CONFIG_POLICY_CHANGED)
    assert ev["tvdbId"] == TVDB
    assert ev["data"]["before"] is None
    assert ev["data"]["after"]["allowSplit"] is False


def test_pause_and_resume_are_journaled(api, db, journal):
    asyncio.run(intent_store.ensure(db, tvdb_id=TVDB, title="Mad Men", chain_key="4k", now="t"))
    api.post(f"/api/series/{TVDB}/pause")
    api.post(f"/api/series/{TVDB}/resume")

    events = [e for e in journal.pending() if e["kind"].startswith("series.")]
    assert [(e["kind"], e["tvdbId"]) for e in events] == [
        (kinds.SERIES_PAUSED, TVDB), (kinds.SERIES_RESUMED, TVDB)]
    assert "Mad Men" in events[0]["message"]


def test_chain_change_is_journaled(api, journal):
    payload = {"4k": [{"instanceId": "1080p", "profile": "SD"}]}
    assert api.put("/api/settings/fallback-chains", json=payload).status_code == 200

    [ev] = _events(journal, kinds.CONFIG_CHAINS_CHANGED)
    assert ev["data"]["before"] == {"4k": [
        {"instanceId": "1080p", "profile": None, "rootFolder": None},
        {"instanceId": "1080p", "profile": "SD", "rootFolder": None},
    ]}
    assert ev["data"]["after"] == payload


@respx.mock
def test_series_removal_is_journaled_with_tvdb(api, journal):
    respx.get(f"{A}/api/v3/series/20").mock(
        return_value=httpx.Response(200, json={"id": 20, "tvdbId": TVDB, "title": "Mad Men"}))
    respx.delete(f"{A}/api/v3/series/20").mock(return_value=httpx.Response(200))

    r = api.delete("/api/instances/1080p/series/20", params={"deleteFiles": "true"})

    assert r.status_code == 200
    [ev] = _events(journal, kinds.SERIES_REMOVED)
    assert (ev["tvdbId"], ev["instanceId"], ev["level"]) == (TVDB, "1080p", "warn")
    assert ev["data"] == {"seriesId": 20, "deleteFiles": True, "title": "Mad Men"}
