"""Durable OperationStore over a temp SQLite DB."""
from app.db import Database
from app.store.operations import OperationStore


def make_store(tmp_path):
    return OperationStore(Database(str(tmp_path / "relay.db")))


async def test_start_records_newest_first(tmp_path):
    store = make_store(tmp_path)
    a = await store.start(kind="smart-add", title="Show A", tvdb_id=1, started_at="t0")
    b = await store.start(kind="smart-add", title="Show B", tvdb_id=2, started_at="t1")
    assert a == 1 and b == 2
    assert [o["title"] for o in await store.recent()] == ["Show B", "Show A"]


async def test_steps_and_finish_round_trip(tmp_path):
    store = make_store(tmp_path)
    op_id = await store.start(kind="smart-add", title="S", tvdb_id=1, started_at="t")
    await store.add_step(op_id, {"phase": "add", "status": "running", "message": "…"})
    await store.add_step(op_id, {"phase": "search", "status": "done",
                                 "data": {"availability": {"available": False}}})
    await store.finish(op_id, result={"status": "added"}, error=None, finished_at="t2")

    latest = (await store.recent())[0]
    assert [s["phase"] for s in latest["steps"]] == ["add", "search"]
    assert latest["steps"][1]["data"]["availability"]["available"] is False
    assert latest["result"]["status"] == "added"
    assert latest["finishedAt"] == "t2"
    assert latest["error"] is None
    assert latest["source"] == "user"


async def test_persists_across_reopen(tmp_path):
    db_path = str(tmp_path / "relay.db")
    store = OperationStore(Database(db_path))
    op_id = await store.start(kind="advance", title="Persisted", tvdb_id=7, started_at="t")
    await store.finish(op_id, result={"status": "placed"}, finished_at="t1")

    # Reopen a fresh Database/store on the same file — simulates a restart.
    reopened = OperationStore(Database(db_path))
    ops = await reopened.recent()
    assert ops[0]["title"] == "Persisted"
    assert ops[0]["result"]["status"] == "placed"


async def test_error_is_recorded(tmp_path):
    store = make_store(tmp_path)
    op_id = await store.start(kind="fill-gaps", title="Bad", tvdb_id=3, started_at="t")
    await store.finish(op_id, result=None, error="boom", finished_at="t1")
    latest = (await store.recent())[0]
    assert latest["error"] == "boom"
    assert latest["result"] is None


async def test_availability_put_records_the_degraded_flag(db):
    """A verdict reached while the indexers were down is still cached -- not
    caching it at all removed the only mechanism that ever repairs a stale row --
    but it is marked so the reader can give it a much shorter TTL."""
    from app.store import availability as avail_cache
    await avail_cache.put(
        db, instance_id="4k", tvdb_id=1, season=1, episode=1, qualifies=False,
        total_releases=0, qualifying_count=0, rejection_json="[]",
        checked_at="2026-09-17T12:00:00+00:00", min_seeders=5, degraded=True,
    )
    row = await avail_cache.get_cached(db, "4k", 1, 1, 1)
    assert row["degraded"] == 1


async def test_availability_put_defaults_to_not_degraded(db):
    from app.store import availability as avail_cache
    await avail_cache.put(
        db, instance_id="4k", tvdb_id=1, season=1, episode=2, qualifies=True,
        total_releases=3, qualifying_count=1, rejection_json="[]",
        checked_at="2026-09-17T12:00:00+00:00",
    )
    assert (await avail_cache.get_cached(db, "4k", 1, 1, 2))["degraded"] in (0, None)
