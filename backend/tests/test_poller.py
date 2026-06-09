"""Outcome poller: queue + history correlate to placement state transitions."""
import httpx
import respx
from starlette.testclient import TestClient

from app.db import Database
from app.main import app
from app.services import poller
from app.state import get_db, get_registry
from app.store import placements as place_store
from tests.test_chain import B, make_registry

TVDB = 99


def _db(tmp_path):
    return Database(str(tmp_path / "relay.db"))


async def _seed(db, *eps, state="wanted"):
    for (s, e) in eps:
        await place_store.upsert(db, tvdb_id=TVDB, season=s, episode=e,
                                 desired_tier="4k", obtained_tier=None,
                                 state=state, reason=None, updated_at="t0")


def _mock_4k_series():
    respx.get(f"{B}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 10, "tvdbId": TVDB, "title": "X"}])
    )


@respx.mock
async def test_import_and_inflight_transitions(tmp_path):
    reg = make_registry()
    db = _db(tmp_path)
    await _seed(db, (1, 1), (1, 2))
    _mock_4k_series()
    # S1E2 is downloading (import pending); S1E1 was imported.
    respx.get(f"{B}/api/v3/queue").mock(return_value=httpx.Response(200, json={"records": [
        {"seriesId": 10, "episodeId": 1002, "downloadId": "D2", "status": "downloading",
         "trackedDownloadState": "importPending",
         "episode": {"seasonNumber": 1, "episodeNumber": 2}},
    ]}))
    respx.get(f"{B}/api/v3/history").mock(return_value=httpx.Response(200, json={"records": [
        {"eventType": "downloadFolderImported", "seriesId": 10, "episodeId": 1001,
         "downloadId": "D1", "episode": {"seasonNumber": 1, "episodeNumber": 1}},
        {"eventType": "grabbed", "seriesId": 10, "episodeId": 1002, "downloadId": "D2",
         "episode": {"seasonNumber": 1, "episodeNumber": 2}},
    ]}))

    transitions = await poller.poll_instance(reg, db, reg.get("4k"))
    assert len(transitions) == 2

    e1 = await place_store.get(db, TVDB, 1, 1)
    assert e1["state"] == "imported" and e1["obtained_tier"] == "4k"
    e2 = await place_store.get(db, TVDB, 1, 2)
    assert e2["state"] == "importing" and e2["download_id"] == "D2"


@respx.mock
async def test_failed_sets_backoff_once(tmp_path):
    reg = make_registry()
    db = _db(tmp_path)
    await _seed(db, (2, 1))
    _mock_4k_series()
    respx.get(f"{B}/api/v3/queue").mock(return_value=httpx.Response(200, json={"records": []}))
    respx.get(f"{B}/api/v3/history").mock(return_value=httpx.Response(200, json={"records": [
        {"eventType": "downloadFailed", "seriesId": 10, "episodeId": 2001, "downloadId": "DX",
         "episode": {"seasonNumber": 2, "episodeNumber": 1}},
    ]}))

    t1 = await poller.poll_instance(reg, db, reg.get("4k"))
    assert len(t1) == 1
    row = await place_store.get(db, TVDB, 2, 1)
    assert row["state"] == "failed"
    assert row["attempts"] == 1
    assert row["next_retry_at"] is not None

    # Re-polling the same failure must not inflate attempts or re-transition.
    t2 = await poller.poll_instance(reg, db, reg.get("4k"))
    assert t2 == []
    assert (await place_store.get(db, TVDB, 2, 1))["attempts"] == 1


@respx.mock
async def test_untracked_episodes_are_ignored(tmp_path):
    reg = make_registry()
    db = _db(tmp_path)
    # No placement rows seeded.
    _mock_4k_series()
    respx.get(f"{B}/api/v3/queue").mock(return_value=httpx.Response(200, json={"records": []}))
    respx.get(f"{B}/api/v3/history").mock(return_value=httpx.Response(200, json={"records": [
        {"eventType": "downloadFolderImported", "seriesId": 10, "episodeId": 1,
         "episode": {"seasonNumber": 1, "episodeNumber": 1}},
    ]}))
    assert await poller.poll_instance(reg, db, reg.get("4k")) == []


@respx.mock
def test_poll_endpoint_summarizes(tmp_path):
    db = _db(tmp_path)
    app.dependency_overrides[get_registry] = make_registry
    app.dependency_overrides[get_db] = lambda: db
    try:
        # Both instances must answer (poll_all fans out across all of them).
        for base in (B, "http://10.0.0.1:8989"):
            respx.get(f"{base}/api/v3/series").mock(return_value=httpx.Response(200, json=[]))
            respx.get(f"{base}/api/v3/queue").mock(return_value=httpx.Response(200, json={"records": []}))
            respx.get(f"{base}/api/v3/history").mock(return_value=httpx.Response(200, json={"records": []}))
        c = TestClient(app)
        resp = c.post("/api/poll")
        assert resp.status_code == 200
        assert resp.json()["transitionCount"] == 0
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_registry, None)
