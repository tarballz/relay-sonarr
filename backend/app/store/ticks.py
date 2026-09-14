"""Tick repo: one row per reconciler pass — the persisted health history.

Health is derived from these rows (not process memory), so it survives restarts
and a partially failing tick (``degraded``) is distinguishable from a wedged loop.
"""
from __future__ import annotations

import json

from app.db import Database


def to_dict(row) -> dict:
    return {
        "id": row["id"],
        "trigger": row["trigger"],
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"],
        "durationMs": row["duration_ms"],
        "status": row["status"],
        "seriesCount": row["series_count"],
        "actions": row["actions"],
        "transitions": row["transitions"],
        "swept": row["swept"],
        "errors": row["errors"],
        "error": row["error"],
        "phases": json.loads(row["phases_json"]) if row["phases_json"] else {},
    }


async def start(db: Database, *, trigger: str, started_at: str) -> int:
    return await db.execute(
        "INSERT INTO tick(trigger, started_at, status) VALUES(?, ?, 'running')",
        (trigger, started_at),
    )


async def finish(db: Database, tick_id: int, *, finished_at: str, duration_ms: int,
                 status: str, series_count: int, actions: int, transitions: int,
                 swept: int, errors: int, error: str | None, phases: dict) -> None:
    await db.execute(
        "UPDATE tick SET finished_at=?, duration_ms=?, status=?, series_count=?, "
        "actions=?, transitions=?, swept=?, errors=?, error=?, phases_json=? WHERE id=?",
        (finished_at, duration_ms, status, series_count, actions, transitions, swept,
         errors, error, json.dumps(phases), tick_id),
    )


async def get(db: Database, tick_id: int) -> dict | None:
    row = await db.query_one("SELECT * FROM tick WHERE id=?", (tick_id,))
    return to_dict(row) if row else None


async def recent(db: Database, limit: int = 96) -> list[dict]:
    rows = await db.query("SELECT * FROM tick ORDER BY id DESC LIMIT ?", (limit,))
    return [to_dict(r) for r in rows]


async def last_completed(db: Database) -> dict | None:
    row = await db.query_one(
        "SELECT * FROM tick WHERE status != 'running' ORDER BY id DESC LIMIT 1"
    )
    return to_dict(row) if row else None


async def consecutive_failures(db: Database) -> int:
    """How many of the most recent completed ticks failed outright, in a row."""
    rows = await db.query(
        "SELECT status FROM tick WHERE status != 'running' ORDER BY id DESC LIMIT 100"
    )
    n = 0
    for r in rows:
        if r["status"] != "failed":
            break
        n += 1
    return n


async def count(db: Database) -> int:
    row = await db.query_one("SELECT COUNT(*) AS n FROM tick")
    return row["n"]
