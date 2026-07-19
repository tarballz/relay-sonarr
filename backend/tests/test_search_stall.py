"""Search-stall reaper: recover episodes wedged in 'searching' with no download."""
from datetime import datetime, timedelta, timezone

from app.db import Database
from app.services.poller import sweep_search_stalls
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


async def test_sweep_reverts_stuck_searching_to_wanted(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    await _seed(db, 1, 1, 1, state="searching",
                last_search_at=(NOW - timedelta(hours=12)).isoformat())

    transitions = await sweep_search_stalls(db, stall_hours=6, cap=25, now=NOW)

    assert transitions == [
        {"tvdbId": 1, "season": 1, "episode": 1, "from": "searching", "to": "wanted"}
    ]
    row = await place_store.get(db, 1, 1, 1)
    assert row["state"] == "wanted"


async def test_sweep_leaves_in_flight_and_recent_and_other_states(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    old = (NOW - timedelta(hours=12)).isoformat()
    await _seed(db, 1, 1, 2, state="searching", last_search_at=old, download_id="HASH")
    await _seed(db, 1, 1, 3, state="searching",
                last_search_at=(NOW - timedelta(hours=1)).isoformat())
    await _seed(db, 1, 1, 4, state="grabbed", last_search_at=old, download_id="H2")
    await _seed(db, 1, 1, 5, state="importing", last_search_at=old, download_id="H3")
    await _seed(db, 1, 1, 6, state="failed", last_search_at=old)

    transitions = await sweep_search_stalls(db, stall_hours=6, cap=25, now=NOW)

    assert transitions == []
    for ep, expected in [(2, "searching"), (3, "searching"), (4, "grabbed"),
                         (5, "importing"), (6, "failed")]:
        assert (await place_store.get(db, 1, 1, ep))["state"] == expected


async def test_sweep_respects_cap(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    for ep in range(1, 6):
        await _seed(db, 1, 1, ep, state="searching",
                    last_search_at=(NOW - timedelta(hours=12 + ep)).isoformat())

    transitions = await sweep_search_stalls(db, stall_hours=6, cap=2, now=NOW)

    assert len(transitions) == 2
    reverted = 0
    for ep in range(1, 6):
        row = await place_store.get(db, 1, 1, ep)
        if row["state"] == "wanted":
            reverted += 1
    assert reverted == 2
