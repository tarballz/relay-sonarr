"""Placement repo: per-(tvdb, season, episode) desired-vs-obtained state.

The upsert only writes the planning columns (desired/obtained/state/reason) so it
never clobbers download-tracking columns (download_id, next_retry_at, attempts,
locked_until) that Phases 3-4 own.
"""
from __future__ import annotations

from app.db import Database


async def upsert(db: Database, *, tvdb_id: int, season: int, episode: int,
                 desired_tier: str | None, obtained_tier: str | None,
                 state: str, reason: str | None, updated_at: str,
                 wanted_since: str | None = None) -> None:
    await db.execute(
        "INSERT INTO placement"
        "(tvdb_id, season, episode, desired_tier, obtained_tier, state, reason, "
        " wanted_since, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(tvdb_id, season, episode) DO UPDATE SET "
        "  desired_tier=excluded.desired_tier, obtained_tier=excluded.obtained_tier, "
        "  state=excluded.state, reason=excluded.reason, "
        "  wanted_since=excluded.wanted_since, updated_at=excluded.updated_at",
        (tvdb_id, season, episode, desired_tier, obtained_tier, state, reason,
         wanted_since, updated_at),
    )


async def get_for_series(db: Database, tvdb_id: int) -> list:
    return await db.query(
        "SELECT * FROM placement WHERE tvdb_id=? ORDER BY season, episode",
        (tvdb_id,),
    )


async def all_rows(db: Database) -> list:
    return await db.query("SELECT * FROM placement")


async def stalled_searching(db: Database, *, before_iso: str, limit: int) -> list:
    """Rows wedged in 'searching' with no download, last searched before ``before_iso``.

    Oldest ``last_search_at`` first so the longest-wedged episodes recover first.
    A null ``download_id`` distinguishes 'searched but nothing came back' from an
    in-flight grab (the poller stamps ``download_id`` on grab)."""
    return await db.query(
        "SELECT * FROM placement "
        "WHERE state='searching' "
        "  AND (download_id IS NULL OR download_id='') "
        "  AND last_search_at IS NOT NULL "
        "  AND last_search_at < ? "
        "ORDER BY last_search_at ASC LIMIT ?",
        (before_iso, limit),
    )


async def get(db: Database, tvdb_id: int, season: int, episode: int):
    return await db.query_one(
        "SELECT * FROM placement WHERE tvdb_id=? AND season=? AND episode=?",
        (tvdb_id, season, episode),
    )


# Columns the poller/reconciler may update (download-tracking, not planning).
_TRACKING_COLUMNS = {
    "state", "obtained_tier", "download_id", "next_retry_at", "attempts", "reason",
    "last_search_at", "locked_until",
}


async def update_tracking(db: Database, tvdb_id: int, season: int, episode: int,
                          *, updated_at: str, **fields) -> None:
    """Update only the given tracking columns on an existing placement row.

    No-op if the row doesn't exist (we only track episodes already in a plan).
    Pass only the columns you mean to change; others are left untouched.
    """
    cols = ["updated_at=?"]
    params: list = [updated_at]
    for key, value in fields.items():
        if key not in _TRACKING_COLUMNS:
            raise ValueError(f"not a tracking column: {key}")
        cols.append(f"{key}=?")
        params.append(value)
    params += [tvdb_id, season, episode]
    await db.execute(
        f"UPDATE placement SET {', '.join(cols)} "
        "WHERE tvdb_id=? AND season=? AND episode=?",
        tuple(params),
    )
