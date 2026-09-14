"""Buffered, process-wide event journal.

``emit`` is synchronous and cheap (append to a buffer) so it can be called from
anywhere — poller, placement, sweeps — without threading a journal through their
signatures. A background flusher (and the reconciler, at the end of each tick
phase) batches the buffer into SQLite and then publishes the persisted rows, ids
included, to live subscribers (the SSE stream).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from app.obs import context, kinds
from app.obs.metrics import EVENTS_TOTAL
from app.store import events as events_store

logger = logging.getLogger(__name__)

RESYNC = object()  # queued to a subscriber that fell behind: it must re-read from the DB


class Subscription:
    def __init__(self, maxsize: int = 500):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize)
        self.overflowed = False

    def offer(self, item) -> None:
        if self.overflowed:
            return
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.overflowed = True
            try:
                self.queue.get_nowait()  # make room for the marker
            except asyncio.QueueEmpty:
                pass
            self.queue.put_nowait(RESYNC)


def _validate(kind: str, level: str) -> None:
    if kind not in kinds.ALL:
        raise ValueError(f"unknown event kind: {kind!r}")
    if level not in kinds.LEVELS:
        raise ValueError(f"unknown event level: {level!r}")


class NullJournal:
    """Default journal: validates like the real one, records nothing."""

    def emit(self, kind: str, message: str, *, level: str = "info", **_fields) -> None:
        _validate(kind, level)

    def pending(self) -> list[dict]:
        return []

    async def flush(self) -> list[dict]:
        return []

    def subscribe(self) -> Subscription:
        return Subscription()

    def unsubscribe(self, sub: Subscription) -> None:
        return None

    async def run(self, interval: float = 1.0) -> None:
        return None

    async def aclose(self) -> None:
        return None


class Journal:
    def __init__(self, db, *, clock=None, max_buffer: int = 1000):
        self.db = db
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_buffer = max_buffer
        self._buffer: list[tuple] = []
        self._subs: set[Subscription] = set()
        self._flush_lock = asyncio.Lock()
        self._stop = asyncio.Event()

    def emit(self, kind: str, message: str, *, level: str = "info", source: str = "system",
             tvdb_id: int | None = None, season: int | None = None,
             episode: int | None = None, instance_id: str | None = None,
             operation_id: int | None = None, data: dict | None = None) -> None:
        """Buffer one event. Raises only on an unknown kind/level (a programming error)."""
        _validate(kind, level)
        ctx = context.current()
        row = (
            self._clock().isoformat(), kind, level, source, ctx["tick_id"],
            operation_id if operation_id is not None else ctx["operation_id"],
            tvdb_id if tvdb_id is not None else ctx["tvdb_id"],
            season, episode, instance_id, message,
            json.dumps(data, default=str) if data is not None else None,
        )
        if len(self._buffer) >= self._max_buffer:
            self._drop_one()
        self._buffer.append(row)
        EVENTS_TOTAL.inc(group=kinds.group(kind), level=level)

    def _drop_one(self) -> None:
        for i, row in enumerate(self._buffer):
            if row[2] in ("debug", "info"):
                del self._buffer[i]
                break
        else:
            del self._buffer[0]
        logger.warning("journal buffer full (%d); dropped an event", self._max_buffer)

    def pending(self) -> list[dict]:
        """Buffered, not-yet-persisted events (id is None)."""
        return [events_store.from_row_tuple(None, r) for r in self._buffer]

    async def flush(self) -> list[dict]:
        async with self._flush_lock:
            if not self._buffer:
                return []
            rows, self._buffer = self._buffer, []
            try:
                ids = await self.db.execute_batch(events_store.INSERT_SQL, rows)
            except Exception:  # noqa: BLE001 - keep the events and retry next flush
                logger.exception("journal flush failed; will retry")
                self._buffer = (rows + self._buffer)[-self._max_buffer:]
                return []
            persisted = [events_store.from_row_tuple(i, r) for i, r in zip(ids, rows)]
            for sub in list(self._subs):
                for event in persisted:
                    sub.offer(event)
            return persisted

    def subscribe(self) -> Subscription:
        sub = Subscription()
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs.discard(sub)

    async def run(self, interval: float = 1.0) -> None:
        """Periodic flusher; exits (after a final flush) once ``aclose`` is called."""
        while not self._stop.is_set():
            await self.flush()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        await self.flush()

    async def aclose(self) -> None:
        self._stop.set()
        await self.flush()


_current: Journal | NullJournal = NullJournal()


def set_journal(journal: Journal | NullJournal | None) -> None:
    global _current
    _current = journal if journal is not None else NullJournal()


def get_journal() -> Journal | NullJournal:
    return _current
