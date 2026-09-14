"""Persisted ticks: phases, degraded/failed status, restart-safe health, manual lock."""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.obs import context, kinds
from app.reconciler import Reconciler, TickBusy
from app.store import events as events_store
from app.store import ticks as tick_store
from app.store.operations import OperationStore
from tests.test_chain import A, B, _nosleep, make_registry

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def _rec(db, clock=lambda: NOW, **kw):
    return Reconciler(make_registry(), db, OperationStore(db), clock=clock, sleep=_nosleep, **kw)


def _empty(base):
    respx.get(f"{base}/api/v3/series").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{base}/api/v3/queue").mock(return_value=httpx.Response(200, json={"records": []}))
    respx.get(f"{base}/api/v3/history").mock(return_value=httpx.Response(200, json={"records": []}))


async def _ok_tick():
    return {"reconciled": []}


@respx.mock
async def test_clean_tick_is_persisted_ok_with_phases(db, journal):
    _empty(A)
    _empty(B)

    out = await _rec(db)._run_once()

    assert out == {"tickId": 1, "status": "ok", "error": None, "reconciled": []}
    tick = await tick_store.get(db, 1)
    assert tick["status"] == "ok" and tick["trigger"] == "schedule"
    assert set(tick["phases"]) >= {
        "poll", "sweep_stalled", "sweep_dangerous", "sweep_search_stall", "reconcile", "availability"}
    events = await events_store.query(db, kind="tick.")
    assert [(e["kind"], e["tickId"], e["level"]) for e in events] == [("tick.finished", 1, "info")]


@respx.mock
async def test_sweep_failure_degrades_tick_and_is_journaled(db, journal, monkeypatch):
    _empty(A)
    _empty(B)

    async def boom(*_a, **_k):
        raise RuntimeError("queue exploded")

    monkeypatch.setattr("app.reconciler.poller.sweep_dangerous", boom)

    out = await _rec(db)._run_once()

    assert out["status"] == "degraded"
    tick = await tick_store.get(db, out["tickId"])
    assert tick["errors"] == 1
    assert "queue exploded" in tick["phases"]["sweep_dangerous"]["error"]
    [failed] = await events_store.query(db, kind=kinds.SWEEP_FAILED)
    assert failed["level"] == "error" and failed["tickId"] == out["tickId"]
    [finished] = await events_store.query(db, kind=kinds.TICK_FINISHED)
    assert finished["level"] == "warn"


@respx.mock
async def test_unreachable_instance_degrades_tick(db, journal):
    _empty(A)
    for path in ("series", "queue", "history"):
        respx.get(f"{B}/api/v3/{path}").mock(side_effect=httpx.ConnectError("refused"))

    out = await _rec(db)._run_once()

    assert out["status"] == "degraded"
    tick = await tick_store.get(db, out["tickId"])
    assert "4k" in tick["phases"]["poll"]["errors"]
    [ev] = await events_store.query(db, kind=kinds.POLL_INSTANCE_ERROR)
    assert ev["instanceId"] == "4k" and ev["tickId"] == out["tickId"]


async def test_raising_tick_is_failed_and_reported(db, journal):
    rec = _rec(db)

    async def boom():
        raise ValueError("kaboom")

    rec.tick = boom
    assert await rec._run_once() is None

    tick = (await tick_store.recent(db))[0]
    assert tick["status"] == "failed" and tick["error"] == "ValueError: kaboom"
    [ev] = await events_store.query(db, kind=kinds.TICK_FAILED)
    assert ev["level"] == "error"
    status = await rec.status()
    assert status["consecutiveFailures"] == 1
    assert status["lastError"] == "ValueError: kaboom"


async def test_health_comes_from_persisted_ticks_and_survives_restart(db):
    clock = {"now": NOW}
    rec = _rec(db, clock=lambda: clock["now"])
    rec.tick = _ok_tick
    await rec._run_once()

    # "Restart" 50 minutes later: a brand-new Reconciler on the same database.
    clock["now"] = NOW + timedelta(minutes=50)
    restarted = _rec(db, clock=lambda: clock["now"])
    status = await restarted.status()
    assert status["healthy"] is True
    assert status["totalTicks"] == 1
    assert status["lastTick"]["status"] == "ok"
    assert status["lastTickFinishedAt"] == NOW.isoformat()

    # Grace is measured from process start; with no tick since, it eventually goes stale.
    clock["now"] = NOW + timedelta(minutes=50, seconds=restarted.interval * 2 + 1)
    assert await restarted.is_healthy() is False


async def test_disabled_reconciler_is_always_healthy(db):
    assert await _rec(db, enabled=False).is_healthy(NOW + timedelta(days=99)) is True


async def test_manual_tick_rejected_while_a_tick_runs(db):
    rec = _rec(db)
    release = asyncio.Event()

    async def slow_tick():
        await release.wait()
        return {"reconciled": []}

    rec.tick = slow_tick
    running = asyncio.create_task(rec._run_once())
    await asyncio.sleep(0)  # the scheduled tick takes the lock

    with pytest.raises(TickBusy):
        await rec.run_manual()

    release.set()
    await running
    rec.tick = _ok_tick
    out = await rec.run_manual()
    assert out["tickId"] == 2
    assert (await tick_store.get(db, 2))["trigger"] == "manual"


async def test_loop_survives_tick_bookkeeping_failure(db, monkeypatch):
    """A DB error out of tick_store.finish() must not kill the background loop
    (regression: run() previously let a bookkeeping exception from _run_once
    escape and die the task, silent until lifespan noticed at shutdown)."""
    rec = _rec(db, interval=0, jitter=0)
    rec.tick = _ok_tick

    calls: list = []

    async def flaky_finish(*_a, **_k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")

    monkeypatch.setattr("app.reconciler.tick_store.finish", flaky_finish)

    task = asyncio.create_task(rec.run())
    for _ in range(200):  # bounded poll (~2s worst case)
        if len(calls) >= 2:
            break
        await asyncio.sleep(0.01)

    rec.stop()
    await asyncio.wait_for(task, timeout=2)  # must finish clean, not raise

    assert len(calls) >= 2


async def test_zero_release_spike_is_journaled_at_tick_end(db, journal):
    rec = _rec(db)

    async def tick():
        stats = context.tick_stats.get()
        for _ in range(10):
            stats.availability("4k", "zero")
        return {"reconciled": []}

    rec.tick = tick
    await rec._run_once()

    [ev] = await events_store.query(db, kind=kinds.AVAILABILITY_ZERO_SPIKE)
    assert ev["instanceId"] == "4k" and ev["data"] == {"zero": 10, "live": 10}
