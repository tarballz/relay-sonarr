"""Cheap DB roll-ups shared by the overview endpoint and the Prometheus gauges."""
from __future__ import annotations

from datetime import datetime

from app.obs.metrics import LAST_TICK_TS, PLACEMENT_EPISODES, SERIES_INTENTS
from app.services import status as status_service
from app.store import ticks as tick_store


async def placement_counts(db) -> dict[str, int]:
    rows = await db.query("SELECT state, COUNT(*) AS n FROM placement GROUP BY state ORDER BY state")
    return {r["state"]: r["n"] for r in rows}


async def intent_counts(db) -> dict[str, int]:
    rows = await db.query("SELECT paused, COUNT(*) AS n FROM series_intent GROUP BY paused")
    by_paused = {bool(r["paused"]): r["n"] for r in rows}
    return {"active": by_paused.get(False, 0), "paused": by_paused.get(True, 0)}


async def series_status_counts(db) -> dict[str, int]:
    """Headline status per series (paused series count as 'paused')."""
    out: dict[str, int] = {}
    for s in await status_service.library_status(db):
        key = "paused" if s["paused"] else s["status"]
        out[key] = out.get(key, 0) + 1
    return out


async def refresh_gauges(db) -> None:
    placements = await placement_counts(db)
    intents = await intent_counts(db)
    PLACEMENT_EPISODES.clear()
    for state, n in placements.items():
        PLACEMENT_EPISODES.set(n, state=state)
    SERIES_INTENTS.clear()
    SERIES_INTENTS.set(intents["active"], paused="false")
    SERIES_INTENTS.set(intents["paused"], paused="true")
    last_tick = await tick_store.last_completed(db)
    if last_tick is not None and last_tick["finishedAt"] is not None:
        LAST_TICK_TS.set(datetime.fromisoformat(last_tick["finishedAt"]).timestamp())
