"""OperationStore: single-trace reads, batched step loading, tick correlation."""
from app.obs import context
from app.store.operations import OperationStore


async def test_get_returns_one_operation_with_steps(db):
    store = OperationStore(db)
    op = await store.start(kind="reconcile", title="Mad Men", tvdb_id=99,
                           started_at="t0", source="reconciler")
    await store.add_step(op, {"phase": "search", "status": "done"})
    await store.finish(op, result={"searchedOnDesired": 1}, finished_at="t1")

    assert await store.get(op) == {
        "id": op, "kind": "reconcile", "title": "Mad Men", "tvdbId": 99,
        "startedAt": "t0", "steps": [{"phase": "search", "status": "done"}],
        "result": {"searchedOnDesired": 1}, "error": None, "finishedAt": "t1",
        "source": "reconciler", "tickId": None,
    }
    assert await store.get(12345) is None


async def test_recent_groups_steps_per_operation_in_order(db):
    store = OperationStore(db)
    a = await store.start(kind="k", title="A", tvdb_id=1, started_at="t")
    b = await store.start(kind="k", title="B", tvdb_id=2, started_at="t")
    await store.add_step(a, {"n": 1})
    await store.add_step(b, {"n": 2})
    await store.add_step(a, {"n": 3})

    ops = await store.recent()

    assert [(o["title"], o["steps"]) for o in ops] == [
        ("B", [{"n": 2}]), ("A", [{"n": 1}, {"n": 3}]),
    ]


async def test_start_stamps_tick_id_from_context(db):
    store = OperationStore(db)
    with context.bind(tick_id=42):
        op = await store.start(kind="reconcile", title="X", tvdb_id=None, started_at="t")
    assert (await store.get(op))["tickId"] == 42
