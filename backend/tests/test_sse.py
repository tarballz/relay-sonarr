"""SSE over the journal: replay then live, no gaps or duplicates, heartbeat, resync."""
import asyncio

import pytest
from starlette.testclient import TestClient

from app.main import app
from app.obs import kinds, sse
from app.obs.journal import RESYNC
from app.state import get_db


async def _emit(journal, *messages):
    for message in messages:
        journal.emit(kinds.INSTANCE_UP, message)
    await journal.flush()


def _id(frame: str) -> int:
    return int(frame.split("\n", 1)[0].removeprefix("id: "))


async def test_replays_since_last_event_id_then_streams_live_without_duplicates(db, journal):
    await _emit(journal, "one", "two", "three")          # ids 1-3
    stream = sse.event_stream(journal, db, last_event_id=1, heartbeat=5)

    first = await stream.__anext__()                     # subscribed, then replayed id 2
    [sub] = journal._subs
    sub.offer({"id": 3, "kind": "instance.up", "message": "dup"})  # already covered by replay
    await _emit(journal, "four")                         # id 4 arrives live
    rest = [await stream.__anext__() for _ in range(2)]

    assert [_id(f) for f in [first, *rest]] == [2, 3, 4]
    assert "event: journal" in first
    await stream.aclose()
    assert journal._subs == set()


async def test_heartbeat_ping_when_idle(db, journal):
    stream = sse.event_stream(journal, db, last_event_id=None, heartbeat=0.01)
    assert await stream.__anext__() == sse.PING
    await stream.aclose()


async def test_resync_and_end_when_replay_exceeds_limit(db, journal):
    await _emit(journal, "a", "b", "c")
    stream = sse.event_stream(journal, db, last_event_id=0, replay_limit=2)
    assert await stream.__anext__() == sse.RESYNC_FRAME
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()
    assert journal._subs == set()


async def test_overflowed_subscription_ends_with_resync(db, journal):
    stream = sse.event_stream(journal, db, last_event_id=None, heartbeat=5)
    pending = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0.01)
    [sub] = journal._subs
    sub.queue.put_nowait(RESYNC)
    assert await pending == sse.RESYNC_FRAME
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()


def test_stream_endpoint_headers_and_last_event_id(db, journal, monkeypatch):
    asyncio.run(_emit(journal, "a", "b", "c"))
    monkeypatch.setattr(sse, "REPLAY_LIMIT", 1)   # 3 events behind → immediate resync, finite stream
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app).stream("GET", "/api/events/stream",
                                    headers={"Last-Event-ID": "0"}) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            assert r.headers["cache-control"] == "no-cache, no-transform"
            assert r.headers["x-accel-buffering"] == "no"
            assert "".join(r.iter_text()) == sse.RESYNC_FRAME
    finally:
        app.dependency_overrides.pop(get_db, None)
