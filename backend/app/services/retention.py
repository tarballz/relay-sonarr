"""Bounded history: prune old journal/tick/operation/progress rows.

Deletes run in chunks so the single SQLite lock is never held for long, and the
whole pass runs at most once a day (``meta.last_prune_at``).
"""
from __future__ import annotations

from datetime import datetime, timedelta

EVENT_RETENTION_DAYS = {"debug": 3, "info": 30, "warn": 90, "error": 90}
TICK_RETENTION_DAYS = 90
OPERATION_RETENTION_DAYS = 60
PROGRESS_RETENTION_DAYS = 30
CHUNK = 5000
PRUNE_KEY = "last_prune_at"
PRUNE_EVERY = timedelta(days=1)


async def _delete_chunked(db, table: str, key: str, where: str, params: tuple, chunk: int) -> int:
    sql = (f"DELETE FROM {table} WHERE {key} IN "
           f"(SELECT {key} FROM {table} WHERE {where} LIMIT ?)")
    total = 0
    while True:
        n = await db.execute_count(sql, (*params, chunk))
        total += n
        if n < chunk:
            return total


def _cutoff(now: datetime, days: int) -> str:
    return (now - timedelta(days=days)).isoformat()


async def prune(db, now: datetime, *, chunk: int = CHUNK) -> dict:
    events = 0
    for level, days in EVENT_RETENTION_DAYS.items():
        events += await _delete_chunked(
            db, "event", "id", "level=? AND ts<?", (level, _cutoff(now, days)), chunk)
    return {
        "events": events,
        "ticks": await _delete_chunked(
            db, "tick", "id", "status != 'running' AND started_at<?",
            (_cutoff(now, TICK_RETENTION_DAYS),), chunk),
        # operation_step rows go with their operation (ON DELETE CASCADE).
        "operations": await _delete_chunked(
            db, "operation", "id", "started_at<?", (_cutoff(now, OPERATION_RETENTION_DAYS),), chunk),
        "downloadProgress": await _delete_chunked(
            db, "download_progress", "download_id", "unchanged_since<?",
            (_cutoff(now, PROGRESS_RETENTION_DAYS),), chunk),
    }


async def maybe_prune(db, now: datetime) -> dict | None:
    """Prune if the last pass was over a day ago; returns counts, or None if skipped."""
    row = await db.query_one("SELECT value FROM meta WHERE key=?", (PRUNE_KEY,))
    if row and row["value"]:
        try:
            if now - datetime.fromisoformat(row["value"]) < PRUNE_EVERY:
                return None
        except ValueError:
            pass
    counts = await prune(db, now)
    await db.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (PRUNE_KEY, now.isoformat())
    )
    return counts
