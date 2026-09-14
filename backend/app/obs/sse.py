"""Server-sent events over the journal: replay from SQLite, then live, with no gaps.

The subscription is taken *before* the replay query, so an event persisted while
replaying is not lost; anything the replay already covered is skipped by id. A
client too far behind (or whose queue overflowed) gets a ``resync`` frame and the
stream ends — it should refetch its views and reconnect.
"""
from __future__ import annotations

import asyncio
import json

from app.obs.journal import RESYNC
from app.store import events as events_store

REPLAY_LIMIT = 500
HEARTBEAT_S = 15.0   # well inside Cloudflare's ~100s idle timeout
PING = ": ping\n\n"
RESYNC_FRAME = "event: resync\ndata: {}\n\n"


def frame(event: dict) -> str:
    return f"id: {event['id']}\nevent: journal\ndata: {json.dumps(event)}\n\n"


async def event_stream(journal, db, *, last_event_id: int | None,
                       heartbeat: float = HEARTBEAT_S, replay_limit: int | None = None):
    limit = REPLAY_LIMIT if replay_limit is None else replay_limit
    sub = journal.subscribe()
    try:
        last = last_event_id
        if last is not None:
            replay = await events_store.since(db, last, limit + 1)
            if len(replay) > limit:
                yield RESYNC_FRAME
                return
            for event in replay:
                yield frame(event)
                last = event["id"]
        while True:
            try:
                item = await asyncio.wait_for(sub.queue.get(), timeout=heartbeat)
            except asyncio.TimeoutError:
                yield PING
                continue
            if item is RESYNC:
                yield RESYNC_FRAME
                return
            if last is not None and item["id"] <= last:
                continue
            yield frame(item)
            last = item["id"]
    finally:
        journal.unsubscribe(sub)
