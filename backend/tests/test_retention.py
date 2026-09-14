"""Retention: bounded tables, pruned in chunks, at most once a day."""
from datetime import datetime, timedelta, timezone

from app.services import retention

NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


def _ago(days):
    return (NOW - timedelta(days=days)).isoformat()


async def _event(db, level, ts):
    await db.execute(
        "INSERT INTO event(ts, kind, level, message) VALUES(?, 'instance.up', ?, 'x')", (ts, level)
    )


async def test_prune_applies_per_table_and_per_level_ages(db):
    for _ in range(5):
        await _event(db, "debug", _ago(4))   # debug keeps 3d → gone (exercises chunking)
    await _event(db, "debug", _ago(1))       # kept
    await _event(db, "info", _ago(31))       # info keeps 30d → gone
    await _event(db, "info", _ago(29))       # kept
    await _event(db, "error", _ago(89))      # errors keep 90d → kept
    await _event(db, "warn", _ago(91))       # gone
    await db.execute("INSERT INTO tick(trigger, started_at, status) VALUES('schedule', ?, 'ok')", (_ago(91),))
    await db.execute("INSERT INTO tick(trigger, started_at, status) VALUES('schedule', ?, 'ok')", (_ago(1),))
    old_op = await db.execute(
        "INSERT INTO operation(kind, title, started_at) VALUES('k', 'old', ?)", (_ago(61),))
    await db.execute(
        "INSERT INTO operation_step(operation_id, seq, event_json) VALUES(?, 1, '{}')", (old_op,))
    await db.execute("INSERT INTO operation(kind, title, started_at) VALUES('k', 'new', ?)", (_ago(1),))
    await db.execute(
        "INSERT INTO download_progress(download_id, sizeleft, unchanged_since) VALUES('old', 1, ?)",
        (_ago(31),))
    await db.execute(
        "INSERT INTO download_progress(download_id, sizeleft, unchanged_since) VALUES('new', 1, ?)",
        (_ago(1),))

    counts = await retention.prune(db, NOW, chunk=2)

    assert counts == {"events": 7, "ticks": 1, "operations": 1, "downloadProgress": 1}
    assert [r["level"] for r in await db.query("SELECT level FROM event ORDER BY id")] == [
        "debug", "info", "error"]
    assert [r["title"] for r in await db.query("SELECT title FROM operation")] == ["new"]
    assert (await db.query_one("SELECT COUNT(*) AS n FROM operation_step"))["n"] == 0
    assert [r["download_id"] for r in await db.query("SELECT download_id FROM download_progress")] == ["new"]


async def test_prune_never_deletes_a_running_tick(db):
    await db.execute("INSERT INTO tick(trigger, started_at, status) VALUES('schedule', ?, 'running')", (_ago(200),))
    assert (await retention.prune(db, NOW))["ticks"] == 0


async def test_maybe_prune_runs_at_most_daily(db):
    assert await retention.maybe_prune(db, NOW) is not None
    assert await retention.maybe_prune(db, NOW + timedelta(hours=23)) is None
    assert await retention.maybe_prune(db, NOW + timedelta(hours=25)) is not None
