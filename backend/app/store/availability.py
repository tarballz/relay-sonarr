"""Availability cache repo: per-(instance, episode) interactive-search verdicts.

Each row records whether a qualifying release existed for one episode on one
instance, with ``checked_at`` for TTL. Lets the placement engine and reconciler
avoid re-hammering indexers within the TTL window.
"""
from __future__ import annotations

from datetime import datetime

from app.db import Database


async def get_cached(db: Database, instance_id: str, tvdb_id: int, season: int,
                     episode: int):
    return await db.query_one(
        "SELECT * FROM availability_cache "
        "WHERE instance_id=? AND tvdb_id=? AND season=? AND episode=?",
        (instance_id, tvdb_id, season, episode),
    )


async def get_for_series(db: Database, tvdb_id: int) -> list:
    return await db.query(
        "SELECT * FROM availability_cache WHERE tvdb_id=?", (tvdb_id,)
    )


async def put(db: Database, *, instance_id: str, tvdb_id: int, season: int,
              episode: int, qualifies: bool, total_releases: int,
              qualifying_count: int, rejection_json: str, checked_at: str,
              min_seeders: int = 0, best_release_json: str | None = None) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO availability_cache"
        "(instance_id, tvdb_id, season, episode, qualifies, total_releases,"
        " qualifying_count, rejection_json, checked_at, min_seeders,"
        " best_release_json) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (instance_id, tvdb_id, season, episode, 1 if qualifies else 0,
         total_releases, qualifying_count, rejection_json, checked_at, min_seeders,
         best_release_json),
    )


def is_fresh(checked_at: str | None, now: datetime, ttl_seconds: float) -> bool:
    """Whether a cached verdict timestamped ``checked_at`` is still within TTL."""
    if not checked_at:
        return False
    try:
        ts = datetime.fromisoformat(checked_at)
    except ValueError:
        return False
    return (now - ts).total_seconds() < ttl_seconds
