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
