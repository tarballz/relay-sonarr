"""Buffered event journal: emit, flush, subscribe, query."""
from datetime import datetime, timezone

import pytest

from app.obs import context, kinds
from app.obs.journal import RESYNC, Journal, NullJournal, Subscription, get_journal, set_journal
from app.store import events as events_store

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


async def test_emit_fills_context_and_flush_persists(db):
    journal = Journal(db, clock=lambda: NOW)
    with context.bind(tick_id=7, tvdb_id=99):
        journal.emit(kinds.POLL_TRANSITION, "S01E02 wanted → grabbed", source="poller",
                     season=1, episode=2, instance_id="4k", data={"to": "grabbed"})
    assert context.current() == {"tick_id": None, "operation_id": None, "tvdb_id": None}
    assert journal.pending()[0]["tickId"] == 7

    persisted = await journal.flush()

    expected = {
        "id": 1, "ts": NOW.isoformat(), "kind": "poll.transition", "level": "info",
        "source": "poller", "tickId": 7, "operationId": None, "tvdbId": 99,
        "season": 1, "episode": 2, "instanceId": "4k",
        "message": "S01E02 wanted → grabbed", "data": {"to": "grabbed"},
    }
    assert persisted == [expected]
    assert await events_store.query(db) == [expected]
    assert journal.pending() == []
    assert await journal.flush() == []


async def test_explicit_ids_override_context(db):
    journal = Journal(db)
    with context.bind(tvdb_id=1, operation_id=2):
        journal.emit(kinds.SERIES_PAUSED, "paused", tvdb_id=5, operation_id=6)
    ev = journal.pending()[0]
    assert (ev["tvdbId"], ev["operationId"]) == (5, 6)


def test_unknown_kind_or_level_raises_even_for_null_journal(db):
    for j in (Journal(db), NullJournal()):
        with pytest.raises(ValueError):
            j.emit("poll.typo", "x")
        with pytest.raises(ValueError):
            j.emit(kinds.POLL_TRANSITION, "x", level="loud")


def test_global_accessor_defaults_to_null(db):
    assert isinstance(get_journal(), NullJournal)
    j = Journal(db)
    set_journal(j)
    assert get_journal() is j
    set_journal(None)
    assert isinstance(get_journal(), NullJournal)


async def test_subscribers_receive_persisted_events_with_ids(db):
    journal = Journal(db)
    sub = journal.subscribe()
    journal.emit(kinds.INSTANCE_UP, "4K is back", instance_id="4k")
    await journal.flush()
    got = sub.queue.get_nowait()
    assert got["id"] == 1 and got["kind"] == "instance.up"

    journal.unsubscribe(sub)
    journal.emit(kinds.INSTANCE_UP, "again")
    await journal.flush()
    assert sub.queue.empty()


def test_subscription_overflow_leaves_a_resync_marker():
    sub = Subscription(maxsize=2)
    sub.offer({"id": 1})
    sub.offer({"id": 2})
    sub.offer({"id": 3})   # overflow
    sub.offer({"id": 4})   # ignored once overflowed
    assert sub.queue.get_nowait() == {"id": 2}
    assert sub.queue.get_nowait() is RESYNC
    assert sub.queue.empty()


def test_full_buffer_drops_oldest_low_level_event_first(db):
    journal = Journal(db, max_buffer=2)
    journal.emit(kinds.TICK_FAILED, "bad", level="error")
    journal.emit(kinds.INSTANCE_UP, "first info")
    journal.emit(kinds.INSTANCE_UP, "second info")
    assert [e["message"] for e in journal.pending()] == ["bad", "second info"]


async def test_failed_flush_keeps_events_for_retry(db, monkeypatch):
    journal = Journal(db)
    journal.emit(kinds.INSTANCE_UP, "keep me")

    async def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "execute_batch", boom)
    assert await journal.flush() == []
    assert [e["message"] for e in journal.pending()] == ["keep me"]


async def test_query_filters_and_keyset_paging(db):
    journal = Journal(db)
    journal.emit(kinds.POLL_TRANSITION, "a", tvdb_id=1)                    # id 1
    journal.emit(kinds.POLL_INSTANCE_ERROR, "b", level="warn", source="poller")  # id 2
    journal.emit(kinds.SWEEP_SEARCH_STALL_REVERTED, "c", tvdb_id=1)         # id 3
    journal.emit(kinds.TICK_FAILED, "d", level="error")                    # id 4
    with context.bind(tick_id=9):
        journal.emit(kinds.TICK_FINISHED, "e")                             # id 5
    await journal.flush()

    ids = lambda rows: [r["id"] for r in rows]
    assert ids(await events_store.query(db)) == [5, 4, 3, 2, 1]
    assert ids(await events_store.query(db, kind="poll.")) == [2, 1]
    assert ids(await events_store.query(db, kind="sweep.search_stall")) == [3]
    assert ids(await events_store.query(db, level="warn")) == [4, 2]
    assert ids(await events_store.query(db, tvdb_id=1)) == [3, 1]
    assert ids(await events_store.query(db, source="poller")) == [2]
    assert ids(await events_store.query(db, tick_id=9)) == [5]
    assert ids(await events_store.query(db, before=3, limit=1)) == [2]
    assert ids(await events_store.since(db, 3, limit=10)) == [4, 5]
    assert ids(await events_store.for_tick(db, 9)) == [5]
    assert await events_store.latest_id(db) == 5
    with pytest.raises(ValueError):
        await events_store.query(db, level="loud")


async def test_latest_id_is_none_on_an_empty_table(db):
    assert await events_store.latest_id(db) is None
