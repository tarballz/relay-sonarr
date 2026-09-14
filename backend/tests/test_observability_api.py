"""Observability HTTP surface: metrics, events, ticks, operation detail, summary."""
import asyncio

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from app.auth import verify_access
from app.main import app
from app.obs import context, kinds
from app.state import get_db, get_monitor, get_operations
from app.store import intents as intent_store
from app.store import placements as place_store
from app.store import ticks as tick_store
from app.store.operations import OperationStore


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def api(db, journal):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_operations] = lambda: OperationStore(db)
    app.dependency_overrides[get_monitor] = lambda: None
    try:
        yield TestClient(app)
    finally:
        for dep in (get_db, get_operations, get_monitor):
            app.dependency_overrides.pop(dep, None)


async def _placement(db, tvdb, episode, state):
    await place_store.upsert(
        db, tvdb_id=tvdb, season=1, episode=episode, desired_tier="4k",
        obtained_tier="4k" if state == "imported" else None, state=state,
        reason=None, updated_at="t",
    )


def test_metrics_endpoint_exposes_text_format_and_db_gauges(api, db):
    async def seed():
        await _placement(db, 1, 1, "wanted")
        await intent_store.ensure(db, tvdb_id=1, title="X", chain_key="4k", now="t")

    _run(seed())
    r = api.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert 'relay_placement_episodes{state="wanted"} 1' in r.text
    assert 'relay_series_intents{paused="false"} 1' in r.text
    assert "# TYPE relay_tick_total counter" in r.text


def test_metrics_token_required_when_configured(api, monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "s3cret")
    assert api.get("/metrics").status_code == 401
    assert api.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert api.get("/metrics", headers={"Authorization": "Bearer s3cret"}).status_code == 200


async def test_metrics_path_is_exempt_from_cloudflare_access(monkeypatch):
    monkeypatch.setenv("CF_ACCESS_ENABLED", "true")
    scope = {"type": "http", "method": "GET", "path": "/metrics", "headers": [],
             "query_string": b"", "server": ("relay", 80), "scheme": "http", "root_path": ""}
    assert await verify_access(request=Request(scope), cf_assertion=None) is None


def test_events_endpoint_filters_and_pages(api, journal):
    journal.emit(kinds.POLL_TRANSITION, "a", tvdb_id=1)
    journal.emit(kinds.INSTANCE_DOWN, "b", level="error", instance_id="4k")
    journal.emit(kinds.POLL_TRANSITION, "c", tvdb_id=1)
    _run(journal.flush())

    page1 = api.get("/api/events", params={"kind": "poll.", "limit": 1}).json()
    assert [e["message"] for e in page1["items"]] == ["c"] and page1["nextBefore"] == 3
    page2 = api.get("/api/events", params={"kind": "poll.", "limit": 1, "before": 3}).json()
    assert [e["message"] for e in page2["items"]] == ["a"]
    errors = api.get("/api/events", params={"level": "error"}).json()["items"]
    assert [e["message"] for e in errors] == ["b"]
    assert api.get("/api/events", params={"tvdb": 1}).json()["nextBefore"] is None
    assert api.get("/api/events", params={"level": "loud"}).status_code == 400


def test_tick_list_and_detail(api, db, journal):
    async def seed():
        tid = await tick_store.start(db, trigger="manual", started_at="2026-09-14T00:00:00+00:00")
        await tick_store.finish(
            db, tid, finished_at="2026-09-14T00:00:02+00:00", duration_ms=2000, status="ok",
            series_count=0, actions=0, transitions=0, swept=0, errors=0, error=None, phases={},
        )
        with context.bind(tick_id=tid):
            journal.emit(kinds.TICK_FINISHED, "done")
        await journal.flush()
        return tid

    tid = _run(seed())
    assert [t["id"] for t in api.get("/api/ticks").json()] == [tid]
    detail = api.get(f"/api/ticks/{tid}").json()
    assert detail["status"] == "ok"
    assert [e["message"] for e in detail["events"]] == ["done"]
    assert api.get("/api/ticks/999").status_code == 404


def test_operation_detail(api, db):
    op = _run(OperationStore(db).start(kind="reconcile", title="X", tvdb_id=1, started_at="t"))
    assert api.get(f"/api/operations/{op}").json()["title"] == "X"
    assert api.get("/api/operations/999").status_code == 404


def test_summary_rolls_up_state(api, db):
    async def seed():
        await _placement(db, 1, 1, "imported")
        await _placement(db, 1, 2, "unavailable")
        await _placement(db, 2, 1, "imported")
        await intent_store.ensure(db, tvdb_id=1, title="A", chain_key="4k", now="t")
        await intent_store.ensure(db, tvdb_id=2, title="B", chain_key="4k", now="t")
        await intent_store.set_paused(db, 2, True, "t")

    _run(seed())
    body = api.get("/api/summary").json()
    assert body["placementCounts"] == {"imported": 2, "unavailable": 1}
    assert body["seriesStatusCounts"] == {"stuck": 1, "paused": 1}
    assert body["intents"] == {"active": 1, "paused": 1}
    assert body["lastTick"] is None and body["instances"] == []
