"""Persisted reconciler tick history."""
from app.db import Database
from app.store import ticks


def _db(tmp_path):
    return Database(str(tmp_path / "relay.db"))


async def _finish(db, tick_id, status, *, finished_at="2026-09-14T00:01:00+00:00", error=None):
    await ticks.finish(
        db, tick_id, finished_at=finished_at, duration_ms=60000, status=status,
        series_count=3, actions=2, transitions=1, swept=0, errors=0 if status == "ok" else 1,
        error=error, phases={"poll": {"ms": 12}},
    )


async def test_start_finish_round_trip(tmp_path):
    db = _db(tmp_path)
    tid = await ticks.start(db, trigger="schedule", started_at="2026-09-14T00:00:00+00:00")
    running = await ticks.get(db, tid)
    assert running["status"] == "running" and running["finishedAt"] is None

    await _finish(db, tid, "ok")
    t = await ticks.get(db, tid)
    assert t == {
        "id": tid, "trigger": "schedule",
        "startedAt": "2026-09-14T00:00:00+00:00", "finishedAt": "2026-09-14T00:01:00+00:00",
        "durationMs": 60000, "status": "ok", "seriesCount": 3, "actions": 2,
        "transitions": 1, "swept": 0, "errors": 0, "error": None,
        "phases": {"poll": {"ms": 12}},
    }
    assert await ticks.get(db, 999) is None


async def test_recent_newest_first_and_last_completed_skips_running(tmp_path):
    db = _db(tmp_path)
    a = await ticks.start(db, trigger="schedule", started_at="t1")
    await _finish(db, a, "ok")
    b = await ticks.start(db, trigger="manual", started_at="t2")  # still running
    assert [t["id"] for t in await ticks.recent(db, limit=10)] == [b, a]
    assert (await ticks.last_completed(db))["id"] == a
    assert await ticks.count(db) == 2


async def test_consecutive_failures_counts_leading_failed(tmp_path):
    db = _db(tmp_path)
    assert await ticks.consecutive_failures(db) == 0
    for status in ("failed", "ok", "failed", "degraded", "failed", "failed"):
        await _finish(db, await ticks.start(db, trigger="schedule", started_at="t"), status)
    await ticks.start(db, trigger="schedule", started_at="t")  # running: ignored
    assert await ticks.consecutive_failures(db) == 2


async def test_execute_batch_and_count(tmp_path):
    db = _db(tmp_path)
    ids = await db.execute_batch(
        "INSERT INTO meta(key, value) VALUES(?, ?)", [("a", "1"), ("b", "2")]
    )
    assert len(ids) == 2 and ids[1] == ids[0] + 1
    assert await db.execute_count("DELETE FROM meta WHERE key IN ('a', 'b')") == 2
