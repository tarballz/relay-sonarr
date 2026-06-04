from app.operations import OperationLog


def test_start_assigns_incrementing_ids_and_is_newest_first():
    log = OperationLog(maxlen=5)
    a = log.start(kind="smart-add", title="Show A", tvdb_id=1, started_at="t0")
    b = log.start(kind="smart-add", title="Show B", tvdb_id=2, started_at="t1")
    assert a["id"] == 1 and b["id"] == 2
    assert [o["title"] for o in log.recent()] == ["Show B", "Show A"]


def test_maxlen_drops_oldest():
    log = OperationLog(maxlen=2)
    for i in range(3):
        log.start(kind="k", title=f"S{i}", tvdb_id=i, started_at="t")
    assert [o["title"] for o in log.recent()] == ["S2", "S1"]


def test_record_is_mutable_and_reflected():
    log = OperationLog()
    op = log.start(kind="smart-add", title="S", tvdb_id=1, started_at="t")
    op["steps"].append({"phase": "add", "status": "done"})
    op["result"] = {"status": "added"}
    op["finishedAt"] = "t2"
    latest = log.recent()[0]
    assert latest["steps"] == [{"phase": "add", "status": "done"}]
    assert latest["result"]["status"] == "added"
    assert latest["finishedAt"] == "t2"
