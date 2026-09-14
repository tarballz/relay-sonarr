"""Monitor: instance up/down, Sonarr health changes, retention, snapshot."""
from datetime import datetime, timezone

import httpx
import respx

from app.obs import kinds
from app.obs.metrics import SONARR_UP
from app.services.monitor import Monitor
from app.store import events as events_store
from tests.test_chain import A, B, make_registry

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
OK = httpx.Response(200, json={"version": "4.0.0"})


def _status_ok(base):
    respx.get(f"{base}/api/v3/system/status").mock(return_value=OK)


def _health(base, items=()):
    respx.get(f"{base}/api/v3/health").mock(return_value=httpx.Response(200, json=list(items)))


async def _instance_events(journal, db, kind="instance."):
    await journal.flush()
    return list(reversed(await events_store.query(db, kind=kind)))


@respx.mock
async def test_down_after_two_failures_once_then_up(db, journal):
    _status_ok(A)
    _health(A)
    _health(B)
    refused = httpx.ConnectError("refused")
    respx.get(f"{B}/api/v3/system/status").mock(side_effect=[refused, refused, refused, OK])
    mon = Monitor(make_registry(), db, clock=lambda: NOW, health_every=1000)

    await mon.step()
    assert await _instance_events(journal, db) == []      # one miss is not an outage
    await mon.step()
    await mon.step()                                       # still down: no duplicate event
    events = await _instance_events(journal, db)
    assert [(e["kind"], e["instanceId"], e["level"]) for e in events] == [
        ("instance.down", "4k", "error")]
    assert SONARR_UP.value(instance="4k") == 0
    four_k = {s["id"]: s for s in mon.snapshot()}["4k"]
    assert four_k["up"] is False and four_k["consecutiveFailures"] == 3
    assert "refused" in four_k["lastError"]

    await mon.step()
    events = await _instance_events(journal, db)
    assert [e["kind"] for e in events] == ["instance.down", "instance.up"]
    assert SONARR_UP.value(instance="4k") == 1
    four_k = {s["id"]: s for s in mon.snapshot()}["4k"]
    assert four_k["up"] is True and four_k["since"] == NOW.isoformat()
    assert four_k["client"]["calls"] == 4


@respx.mock
async def test_sonarr_health_changes_are_journaled(db, journal):
    _status_ok(A)
    _status_ok(B)
    _health(A)
    issue = {"source": "IndexerStatusCheck", "type": "warning",
             "message": "All indexers are unavailable due to failures"}
    respx.get(f"{B}/api/v3/health").mock(side_effect=[
        httpx.Response(200, json=[]),
        httpx.Response(200, json=[issue]),
        httpx.Response(200, json=[issue]),   # unchanged: no event
        httpx.Response(200, json=[]),
    ])
    mon = Monitor(make_registry(), db, clock=lambda: NOW, health_every=1)

    for _ in range(4):
        await mon.step()

    events = await _instance_events(journal, db, kind=kinds.INSTANCE_HEALTH_CHANGED)
    assert [(e["instanceId"], e["level"]) for e in events] == [("4k", "warn"), ("4k", "info")]
    assert "All indexers are unavailable" in events[0]["message"]
    assert {s["id"]: s for s in mon.snapshot()}["4k"]["sonarrHealth"] == []


@respx.mock
async def test_step_runs_retention_gate(db, monkeypatch):
    _status_ok(A)
    _status_ok(B)
    _health(A)
    _health(B)
    calls = []

    async def fake_maybe_prune(db_, now):
        calls.append(now)
        return None

    monkeypatch.setattr("app.services.monitor.retention.maybe_prune", fake_maybe_prune)
    await Monitor(make_registry(), db, clock=lambda: NOW).step()
    assert calls == [NOW]
