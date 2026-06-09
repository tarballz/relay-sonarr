"""Series-intent repo: which series Relay actively orchestrates.

Phase 4 seeds these from the live library (series on a tier that has a fallback
chain). Phase 5 adds the declarative ``policy_json`` and editing; ``paused`` is
the override the reconciler checks before acting on a series.
"""
from __future__ import annotations

from app.db import Database


async def ensure(db: Database, *, tvdb_id: int, title: str | None,
                 chain_key: str, now: str) -> None:
    """Insert (or refresh title/chain) an intent row; preserves paused/policy."""
    await db.execute(
        "INSERT INTO series_intent(tvdb_id, title, chain_key, paused, created_at, updated_at) "
        "VALUES(?, ?, ?, 0, ?, ?) "
        "ON CONFLICT(tvdb_id) DO UPDATE SET "
        "  title=excluded.title, chain_key=excluded.chain_key, updated_at=excluded.updated_at",
        (tvdb_id, title, chain_key, now, now),
    )


async def get(db: Database, tvdb_id: int):
    return await db.query_one("SELECT * FROM series_intent WHERE tvdb_id=?", (tvdb_id,))


async def all_active(db: Database) -> list:
    return await db.query(
        "SELECT * FROM series_intent WHERE paused=0 ORDER BY tvdb_id"
    )


async def all_intents(db: Database) -> list:
    return await db.query("SELECT * FROM series_intent ORDER BY tvdb_id")


async def set_paused(db: Database, tvdb_id: int, paused: bool, now: str) -> None:
    await db.execute(
        "UPDATE series_intent SET paused=?, updated_at=? WHERE tvdb_id=?",
        (1 if paused else 0, now, tvdb_id),
    )


async def set_policy(db: Database, tvdb_id: int, policy_json: str, now: str) -> None:
    await db.execute(
        "UPDATE series_intent SET policy_json=?, updated_at=? WHERE tvdb_id=?",
        (policy_json, now, tvdb_id),
    )
