"""Search-stall reaper: recover episodes wedged in 'searching' with no download."""
from datetime import datetime, timedelta, timezone

from app.db import Database
from app.store import placements as place_store

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


async def _seed(db, tvdb, season, episode, *, state, last_search_at=None,
                download_id=None, updated_at=None):
    """Insert a placement row, then stamp tracking columns the upsert doesn't own."""
    updated_at = updated_at or NOW.isoformat()
    await place_store.upsert(
        db, tvdb_id=tvdb, season=season, episode=episode,
        desired_tier="1080p", obtained_tier=None, state=state, reason=None,
        updated_at=updated_at,
    )
    fields = {"state": state}
    if last_search_at is not None:
        fields["last_search_at"] = last_search_at
    if download_id is not None:
        fields["download_id"] = download_id
    await place_store.update_tracking(db, tvdb, season, episode,
                                      updated_at=updated_at, **fields)


async def test_stalled_searching_selects_only_stuck_rows(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    old = (NOW - timedelta(hours=12)).isoformat()
    recent = (NOW - timedelta(hours=1)).isoformat()
    # stuck: searching, no download, searched long ago  -> SELECTED
    await _seed(db, 1, 1, 1, state="searching", last_search_at=old)
    # has a download in flight                          -> excluded
    await _seed(db, 1, 1, 2, state="searching", last_search_at=old, download_id="HASH")
    # searched recently                                 -> excluded
    await _seed(db, 1, 1, 3, state="searching", last_search_at=recent)
    # not searching                                     -> excluded
    await _seed(db, 1, 1, 4, state="grabbed", last_search_at=old, download_id="H2")

    before = (NOW - timedelta(hours=6)).isoformat()
    rows = await place_store.stalled_searching(db, before_iso=before, limit=25)

    keys = {(r["season"], r["episode"]) for r in rows}
    assert keys == {(1, 1)}


async def test_stalled_searching_respects_limit_and_orders_oldest_first(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    await _seed(db, 1, 1, 1, state="searching",
                last_search_at=(NOW - timedelta(hours=20)).isoformat())
    await _seed(db, 1, 1, 2, state="searching",
                last_search_at=(NOW - timedelta(hours=30)).isoformat())
    await _seed(db, 1, 1, 3, state="searching",
                last_search_at=(NOW - timedelta(hours=10)).isoformat())

    before = (NOW - timedelta(hours=6)).isoformat()
    rows = await place_store.stalled_searching(db, before_iso=before, limit=2)

    assert [(r["season"], r["episode"]) for r in rows] == [(1, 2), (1, 1)]  # oldest first, capped at 2
