"""Episode-level availability + placement engine."""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import respx
from starlette.testclient import TestClient

from app.db import Database
from app.main import app
from app.services import placement
from app.services.placement import _infer_desired, tier_priority
from app.state import get_db, get_registry
from app.store import availability as avail_cache
from app.store import placements as place_store
from app.store import settings as settings_store
from tests.test_chain import A, B, make_registry

TVDB = 99


def _db(tmp_path):
    return Database(str(tmp_path / "relay.db"))


# 4k (B) has S1E1 on disk, S1E2 missing. 1080p (A) has neither file; S1E2 monitored.
def _mock_library():
    respx.get(f"{B}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 10, "tvdbId": TVDB, "title": "X"}])
    )
    respx.get(f"{B}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 1001, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": True},
        {"id": 1002, "seasonNumber": 1, "episodeNumber": 2, "monitored": True, "hasFile": False},
    ]))
    respx.get(f"{A}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 20, "tvdbId": TVDB, "title": "X"}])
    )
    respx.get(f"{A}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 2001, "seasonNumber": 1, "episodeNumber": 1, "monitored": False, "hasFile": False},
        {"id": 2002, "seasonNumber": 1, "episodeNumber": 2, "monitored": True, "hasFile": False},
    ]))


# --- pure helpers ------------------------------------------------------------

def test_infer_desired_prefers_instance_with_chain():
    reg = make_registry()
    assert _infer_desired(reg, ["1080p", "4k"]) == "4k"   # 4k has a fallback chain
    assert _infer_desired(reg, ["1080p"]) == "1080p"
    assert _infer_desired(reg, []) is None


def test_tier_priority_is_desired_then_chain_then_present():
    reg = make_registry()
    assert tier_priority(reg, "4k", ["1080p", "4k"]) == ["4k", "1080p"]


# --- compute_plan ------------------------------------------------------------

@respx.mock
async def test_compute_plan_classifies_obtained_and_wanted(tmp_path):
    reg = make_registry()
    _mock_library()
    plan = await placement.compute_plan(reg, _db(tmp_path), tvdb_id=TVDB)

    assert plan["desiredTier"] == "4k"
    assert plan["tierPriority"] == ["4k", "1080p"]
    by = {(e["season"], e["episode"]): e for e in plan["episodes"]}
    # S1E1 is on disk in 4K → imported there.
    assert by[(1, 1)]["state"] == "imported"
    assert by[(1, 1)]["obtainedTier"] == "4k"
    # S1E2 missing everywhere, monitored, not yet checked → wanted, no obtainable tier.
    assert by[(1, 2)]["state"] == "wanted"
    assert by[(1, 2)]["obtainedTier"] is None
    assert by[(1, 2)]["obtainableTier"] is None


@respx.mock
async def test_refresh_then_plan_marks_unavailable_with_reason(tmp_path):
    reg = make_registry()
    _mock_library()
    # 4K interactive search for the gap: only a 1080p release, rejected by the 4K profile.
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[
        {"rejected": True, "rejections": ["Bluray-1080p is not wanted in profile"]},
    ]))
    db = _db(tmp_path)
    await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="4k")
    plan = await placement.compute_plan(reg, db, tvdb_id=TVDB)

    e = next(x for x in plan["episodes"] if (x["season"], x["episode"]) == (1, 2))
    assert e["state"] == "unavailable"
    assert "quality not allowed" in e["reason"]


@respx.mock
async def test_refresh_finds_obtainable_on_fallback_tier(tmp_path):
    reg = make_registry()
    _mock_library()
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[
        {"rejected": True, "rejections": ["Bluray-1080p is not wanted in profile"]},
    ]))
    respx.get(f"{A}/api/v3/release").mock(return_value=httpx.Response(200, json=[
        {"rejected": False},
    ]))
    db = _db(tmp_path)
    await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="4k")
    await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="1080p")
    plan = await placement.compute_plan(reg, db, tvdb_id=TVDB)

    e = next(x for x in plan["episodes"] if (x["season"], x["episode"]) == (1, 2))
    # 4K can't, 1080p can → obtainable on the fallback tier, still "wanted".
    assert e["obtainableTier"] == "1080p"
    assert e["state"] == "wanted"


@respx.mock
async def test_refresh_uses_cache_within_ttl(tmp_path):
    reg = make_registry()
    _mock_library()
    route = respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[
        {"rejected": True, "rejections": ["x"]},
    ]))
    db = _db(tmp_path)
    await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="4k")
    await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="4k")
    # Only one gap episode (S1E2); the second sweep is served from cache.
    assert route.call_count == 1


@respx.mock
async def test_refresh_availability_min_seeders_gate(tmp_path):
    reg = make_registry()
    _mock_library()
    # The gap's only release is a non-rejected but 1-seeder torrent.
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[
        {"rejected": False, "protocol": "torrent", "seeders": 1},
    ]))
    db = _db(tmp_path)
    await settings_store.set_defaults(db, {"minSeeders": 3})

    rows = await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="4k")

    assert rows[0]["qualifies"] is False
    cached = await avail_cache.get_cached(db, "4k", TVDB, 1, 2)
    assert cached["qualifies"] == 0
    assert cached["min_seeders"] == 3


@respx.mock
async def test_refresh_availability_recheck_when_min_seeders_changes(tmp_path):
    reg = make_registry()
    _mock_library()
    route = respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[
        {"rejected": False, "protocol": "torrent", "seeders": 1},
    ]))
    db = _db(tmp_path)
    await settings_store.set_defaults(db, {"minSeeders": 0})
    r1 = await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="4k")
    assert r1[0]["qualifies"] is True

    # Flipping the knob must invalidate the cached verdict (no waiting out the TTL).
    await settings_store.set_defaults(db, {"minSeeders": 3})
    r2 = await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="4k")

    assert route.call_count == 2
    assert r2[0]["qualifies"] is False


@respx.mock
async def test_refresh_availability_caches_best_release(tmp_path):
    import json as _json

    reg = make_registry()
    _mock_library()
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[
        {"guid": "lo", "indexerId": 1, "title": "lo-rel", "rejected": False,
         "protocol": "torrent", "seeders": 4},
        {"guid": "hi", "indexerId": 2, "title": "hi-rel", "rejected": False,
         "protocol": "torrent", "seeders": 44},
    ]))
    db = _db(tmp_path)

    await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="4k")

    cached = await avail_cache.get_cached(db, "4k", TVDB, 1, 2)
    best = _json.loads(cached["best_release_json"])
    assert best["guid"] == "hi"
    assert best["indexerId"] == 2
    assert best["seeders"] == 44


@respx.mock
async def test_refresh_availability_no_torrent_candidate_caches_null(tmp_path):
    reg = make_registry()
    _mock_library()
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[
        {"guid": "n", "indexerId": 1, "protocol": "usenet", "rejected": False},
    ]))
    db = _db(tmp_path)

    await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="4k")

    cached = await avail_cache.get_cached(db, "4k", TVDB, 1, 2)
    assert cached["best_release_json"] is None


# --- short-TTL zero-release verdicts (aired gaps with no releases yet) -------

def _mock_gap_with_airdate(air_date_iso: str | None):
    """4k (B) has one gap: S1E2, monitored, no file, with the given airDateUtc."""
    respx.get(f"{B}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 10, "tvdbId": TVDB, "title": "X"}])
    )
    ep = {"id": 1002, "seasonNumber": 1, "episodeNumber": 2, "monitored": True, "hasFile": False}
    if air_date_iso is not None:
        ep["airDateUtc"] = air_date_iso
    respx.get(f"{B}/api/v3/episode").mock(return_value=httpx.Response(200, json=[ep]))


async def _seed_empty_cache(db, *, checked_at: str):
    await settings_store.set_defaults(db, {"minSeeders": 0})
    await avail_cache.put(
        db, instance_id="4k", tvdb_id=TVDB, season=1, episode=2,
        qualifies=False, total_releases=0, qualifying_count=0,
        rejection_json="[]", checked_at=checked_at, min_seeders=0,
    )


def test_has_aired_past_true_future_false_missing_false():
    now = datetime(2026, 9, 13, tzinfo=timezone.utc)
    assert placement._has_aired({"airDateUtc": "2026-08-13T04:00:00Z"}, now) is True
    assert placement._has_aired({"airDateUtc": "2026-09-20T04:00:00Z"}, now) is False
    assert placement._has_aired({}, now) is False
    assert placement._has_aired({"airDateUtc": "not-a-date"}, now) is False


@respx.mock
async def test_refresh_availability_rechecks_stale_empty_verdict_for_old_aired_gap(tmp_path):
    # Aired 60 days ago — well outside any "recently aired" window, but it HAS
    # aired, so a 0-release verdict should still be treated as cheap-to-recheck.
    now = datetime(2026, 9, 13, tzinfo=timezone.utc)
    checked_at = now - timedelta(minutes=50)
    _mock_gap_with_airdate("2026-07-15T04:00:00Z")
    route = respx.get(f"{B}/api/v3/release").mock(
        return_value=httpx.Response(200, json=[{"rejected": False}])
    )
    db = _db(tmp_path)
    await _seed_empty_cache(db, checked_at=checked_at.isoformat())

    rows = await placement.refresh_availability(make_registry(), db, tvdb_id=TVDB, instance_id="4k", now=now)

    assert route.call_count == 1  # re-checked despite being within the flat 6h TTL
    assert rows[0]["qualifies"] is True


@respx.mock
async def test_refresh_availability_keeps_long_ttl_when_releases_exist_but_rejected(tmp_path):
    now = datetime(2026, 9, 13, tzinfo=timezone.utc)
    checked_at = now - timedelta(minutes=50)
    _mock_gap_with_airdate("2026-07-15T04:00:00Z")
    route = respx.get(f"{B}/api/v3/release").mock(
        return_value=httpx.Response(200, json=[{"rejected": False}])
    )
    db = _db(tmp_path)
    await settings_store.set_defaults(db, {"minSeeders": 0})
    # Non-empty verdict (total_releases > 0) is a stable "no" — long TTL applies.
    await avail_cache.put(
        db, instance_id="4k", tvdb_id=TVDB, season=1, episode=2,
        qualifies=False, total_releases=1, qualifying_count=0,
        rejection_json="[]", checked_at=checked_at.isoformat(), min_seeders=0,
    )

    await placement.refresh_availability(make_registry(), db, tvdb_id=TVDB, instance_id="4k", now=now)

    assert route.call_count == 0  # still fresh under the 6h TTL


@respx.mock
async def test_refresh_availability_keeps_long_ttl_for_unaired_gap(tmp_path):
    now = datetime(2026, 9, 13, tzinfo=timezone.utc)
    checked_at = now - timedelta(minutes=50)
    _mock_gap_with_airdate("2026-09-20T04:00:00Z")  # airs next week
    route = respx.get(f"{B}/api/v3/release").mock(
        return_value=httpx.Response(200, json=[])
    )
    db = _db(tmp_path)
    await _seed_empty_cache(db, checked_at=checked_at.isoformat())

    await placement.refresh_availability(make_registry(), db, tvdb_id=TVDB, instance_id="4k", now=now)

    assert route.call_count == 0  # unaired: 0 releases is expected, don't hammer


@respx.mock
async def test_refresh_availability_keeps_long_ttl_when_airdate_missing(tmp_path):
    now = datetime(2026, 9, 13, tzinfo=timezone.utc)
    checked_at = now - timedelta(minutes=50)
    _mock_gap_with_airdate(None)
    route = respx.get(f"{B}/api/v3/release").mock(
        return_value=httpx.Response(200, json=[])
    )
    db = _db(tmp_path)
    await _seed_empty_cache(db, checked_at=checked_at.isoformat())

    await placement.refresh_availability(make_registry(), db, tvdb_id=TVDB, instance_id="4k", now=now)

    assert route.call_count == 0  # unknown airdate: conservative, keep long TTL


@respx.mock
async def test_refresh_availability_empty_verdict_still_fresh_within_short_ttl(tmp_path):
    now = datetime(2026, 9, 13, tzinfo=timezone.utc)
    checked_at = now - timedelta(minutes=10)  # < 45min empty_ttl
    _mock_gap_with_airdate("2026-07-15T04:00:00Z")
    route = respx.get(f"{B}/api/v3/release").mock(
        return_value=httpx.Response(200, json=[])
    )
    db = _db(tmp_path)
    await _seed_empty_cache(db, checked_at=checked_at.isoformat())

    await placement.refresh_availability(make_registry(), db, tvdb_id=TVDB, instance_id="4k", now=now)

    assert route.call_count == 0  # too recent even for the short TTL


@respx.mock
async def test_compute_plan_preserves_in_progress_state(tmp_path):
    reg = make_registry()
    _mock_library()
    db = _db(tmp_path)
    # Poller already marked S1E2 as "grabbed" — the plan must not reset it to wanted.
    await place_store.upsert(db, tvdb_id=TVDB, season=1, episode=2, desired_tier="4k",
                             obtained_tier=None, state="grabbed", reason=None, updated_at="t")
    plan = await placement.compute_plan(reg, db, tvdb_id=TVDB)
    e = next(x for x in plan["episodes"] if (x["season"], x["episode"]) == (1, 2))
    assert e["state"] == "grabbed"


@respx.mock
async def test_compute_plan_stamps_and_preserves_wanted_since(tmp_path):
    reg = make_registry()
    _mock_library()
    db = _db(tmp_path)
    t1 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 9, tzinfo=timezone.utc)

    await placement.compute_plan(reg, db, tvdb_id=TVDB, now=t1)
    wanted = await place_store.get(db, TVDB, 1, 2)   # gap → wanted
    imported = await place_store.get(db, TVDB, 1, 1)  # on disk → imported
    assert wanted["wanted_since"] == t1.isoformat()
    assert imported["wanted_since"] is None

    # A later recompute keeps the original wanted_since (age accrues from first want).
    await placement.compute_plan(reg, db, tvdb_id=TVDB, now=t2)
    assert (await place_store.get(db, TVDB, 1, 2))["wanted_since"] == t1.isoformat()


# --- season rollup -----------------------------------------------------------

def test_season_rollup_separates_specials_and_counts_states():
    episodes_out = [
        {"season": 0, "episode": 1, "obtainedTier": None, "obtainableTier": None, "state": "unmonitored"},
        {"season": 1, "episode": 1, "obtainedTier": "4k", "obtainableTier": None, "state": "imported"},
        {"season": 1, "episode": 2, "obtainedTier": None, "obtainableTier": None, "state": "wanted"},
    ]
    rollup = placement._season_rollup(episodes_out, ["4k", "1080p"], "4k")
    by = {s["season"]: s for s in rollup}
    assert set(by) == {0, 1}
    assert by[1]["episodeCount"] == 2
    assert by[1]["counts"] == {"imported": 1, "wanted": 1}
    assert by[1]["obtainedTiers"] == ["4k"]


def test_season_rollup_canLowerRes_when_unobtained_ep_obtainable_on_lower_tier():
    episodes_out = [
        {"season": 1, "episode": 1, "obtainedTier": "4k", "obtainableTier": None, "state": "imported"},
        {"season": 1, "episode": 2, "obtainedTier": None, "obtainableTier": "1080p", "state": "wanted"},
    ]
    rollup = placement._season_rollup(episodes_out, ["4k", "1080p"], "4k")
    s1 = rollup[0]
    assert s1["obtainableTier"] == "1080p"
    assert s1["canLowerRes"] is True


def test_season_rollup_no_lower_option_is_not_lowerable():
    episodes_out = [
        {"season": 1, "episode": 1, "obtainedTier": "4k", "obtainableTier": None, "state": "imported"},
        {"season": 1, "episode": 2, "obtainedTier": None, "obtainableTier": None, "state": "wanted"},
    ]
    rollup = placement._season_rollup(episodes_out, ["4k", "1080p"], "4k")
    assert rollup[0]["canLowerRes"] is False


@respx.mock
async def test_compute_plan_includes_season_rollup(tmp_path):
    reg = make_registry()
    _mock_library()
    plan = await placement.compute_plan(reg, _db(tmp_path), tvdb_id=TVDB)

    seasons = {s["season"]: s for s in plan["seasons"]}
    assert seasons[1]["episodeCount"] == 2
    assert seasons[1]["counts"] == {"imported": 1, "wanted": 1}
    assert seasons[1]["obtainedTiers"] == ["4k"]
    assert seasons[1]["canLowerRes"] is False  # nothing checked on lower tier yet


@respx.mock
async def test_compute_plan_season_canLowerRes_after_refresh(tmp_path):
    reg = make_registry()
    _mock_library()
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[
        {"rejected": True, "rejections": ["Bluray-1080p is not wanted in profile"]},
    ]))
    respx.get(f"{A}/api/v3/release").mock(return_value=httpx.Response(200, json=[{"rejected": False}]))
    db = _db(tmp_path)
    await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="4k")
    await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="1080p")
    plan = await placement.compute_plan(reg, db, tvdb_id=TVDB)

    s1 = next(s for s in plan["seasons"] if s["season"] == 1)
    assert s1["obtainableTier"] == "1080p"
    assert s1["canLowerRes"] is True


@respx.mock
def test_plan_endpoint_returns_plan(tmp_path):
    db = _db(tmp_path)
    app.dependency_overrides[get_registry] = make_registry
    app.dependency_overrides[get_db] = lambda: db
    try:
        _mock_library()
        c = TestClient(app)
        resp = c.get(f"/api/series/{TVDB}/plan")
        assert resp.status_code == 200
        body = resp.json()
        assert body["desiredTier"] == "4k"
        assert body["counts"]["imported"] == 1
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_registry, None)


@respx.mock
def test_plan_refresh_honors_configured_empty_release_ttl(tmp_path):
    """?refresh= applies emptyReleaseTtlMinutes exactly like the reconciler: a
    15-minute-old zero-release verdict is stale under a 10-minute setting, even
    though it would still be fresh under the 45-minute default."""
    db = _db(tmp_path)
    checked_at = datetime.now(timezone.utc) - timedelta(minutes=15)

    async def seed():
        await _seed_empty_cache(db, checked_at=checked_at.isoformat())
        await settings_store.set_defaults(db, {"minSeeders": 0, "emptyReleaseTtlMinutes": 10})

    asyncio.run(seed())
    _mock_gap_with_airdate("2000-01-01T00:00:00Z")
    respx.get(f"{A}/api/v3/series").mock(return_value=httpx.Response(200, json=[]))
    route = respx.get(f"{B}/api/v3/release").mock(
        return_value=httpx.Response(200, json=[{"rejected": False}])
    )
    app.dependency_overrides[get_registry] = make_registry
    app.dependency_overrides[get_db] = lambda: db
    try:
        resp = TestClient(app).get(f"/api/series/{TVDB}/plan?refresh=4k")
        assert resp.status_code == 200
        assert route.call_count == 1
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_registry, None)


# --- the seeder floor relaxes for episodes that have waited ------------------

async def _want_since(db, since):
    """Seed the one gap episode (S1E2) as wanted since a given moment."""
    await place_store.upsert(
        db, tvdb_id=TVDB, season=1, episode=2, desired_tier="4k",
        obtained_tier=None, state="wanted", reason=None,
        updated_at=since.isoformat(), wanted_since=since.isoformat(),
    )


@respx.mock
async def test_thin_swarm_qualifies_once_an_episode_has_waited(tmp_path):
    """Black Sails S04 tops out at 4 seeders — a permanent floor of 5 strands it."""
    reg = make_registry()
    _mock_library()
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[
        {"rejected": False, "protocol": "torrent", "seeders": 4, "guid": "g", "indexerId": 1},
    ]))
    db = _db(tmp_path)
    await settings_store.set_defaults(db, {"minSeeders": 5, "seederRelaxAfterDays": 3})
    await _want_since(db, datetime.now(timezone.utc) - timedelta(days=30))

    rows = await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="4k")

    assert rows[0]["qualifies"] is True
    cached = await avail_cache.get_cached(db, "4k", TVDB, 1, 2)
    # The *effective* floor is cached, so crossing the boundary self-invalidates.
    assert cached["min_seeders"] == 1


@respx.mock
async def test_thin_swarm_is_rejected_while_the_episode_is_still_fresh(tmp_path):
    reg = make_registry()
    _mock_library()
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[
        {"rejected": False, "protocol": "torrent", "seeders": 4, "guid": "g", "indexerId": 1},
    ]))
    db = _db(tmp_path)
    await settings_store.set_defaults(db, {"minSeeders": 5, "seederRelaxAfterDays": 3})
    await _want_since(db, datetime.now(timezone.utc) - timedelta(hours=6))

    rows = await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="4k")

    assert rows[0]["qualifies"] is False
    assert (await avail_cache.get_cached(db, "4k", TVDB, 1, 2))["min_seeders"] == 5


@respx.mock
async def test_relax_disabled_keeps_the_floor_forever(tmp_path):
    reg = make_registry()
    _mock_library()
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[
        {"rejected": False, "protocol": "torrent", "seeders": 4},
    ]))
    db = _db(tmp_path)
    await settings_store.set_defaults(db, {"minSeeders": 5, "seederRelaxAfterDays": 0})
    await _want_since(db, datetime.now(timezone.utc) - timedelta(days=365))

    rows = await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="4k")

    assert rows[0]["qualifies"] is False


# --- a floor change must not re-search the whole library ----------------------

def _cached_release(seeders=9):
    return httpx.Response(200, json=[
        {"rejected": False, "protocol": "torrent", "seeders": seeders,
         "guid": "g", "indexerId": 1},
    ])


@respx.mock
async def test_stricter_floor_reuses_a_cached_no(tmp_path):
    """qualifying_count only falls as the floor rises, so a cached "no" stands.

    Keying the cache on the floor alone invalidated 1540 of 1545 verdicts the
    moment the default moved 3 -> 5 — hours of interactive searches, four at a
    time, to re-derive answers that could not have changed.
    """
    reg = make_registry()
    _mock_library()
    route = respx.get(f"{B}/api/v3/release").mock(return_value=_cached_release(2))
    db = _db(tmp_path)
    await settings_store.set_defaults(db, {"minSeeders": 3, "seederRelaxAfterDays": 0})
    r1 = await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="4k")
    assert r1[0]["qualifies"] is False

    await settings_store.set_defaults(db, {"minSeeders": 5, "seederRelaxAfterDays": 0})
    r2 = await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="4k")

    assert route.call_count == 1          # no second search
    assert r2[0]["cached"] is True
    assert r2[0]["qualifies"] is False


@respx.mock
async def test_looser_floor_reuses_a_cached_yes(tmp_path):
    reg = make_registry()
    _mock_library()
    route = respx.get(f"{B}/api/v3/release").mock(return_value=_cached_release(9))
    db = _db(tmp_path)
    await settings_store.set_defaults(db, {"minSeeders": 5, "seederRelaxAfterDays": 0})
    assert (await placement.refresh_availability(
        reg, db, tvdb_id=TVDB, instance_id="4k"))[0]["qualifies"] is True

    await settings_store.set_defaults(db, {"minSeeders": 1, "seederRelaxAfterDays": 0})
    r2 = await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="4k")

    assert route.call_count == 1
    assert r2[0]["qualifies"] is True


@respx.mock
async def test_stricter_floor_rechecks_a_cached_yes(tmp_path):
    """This one really can flip, so it must be re-searched."""
    reg = make_registry()
    _mock_library()
    route = respx.get(f"{B}/api/v3/release").mock(return_value=_cached_release(4))
    db = _db(tmp_path)
    await settings_store.set_defaults(db, {"minSeeders": 3, "seederRelaxAfterDays": 0})
    assert (await placement.refresh_availability(
        reg, db, tvdb_id=TVDB, instance_id="4k"))[0]["qualifies"] is True

    await settings_store.set_defaults(db, {"minSeeders": 5, "seederRelaxAfterDays": 0})
    r2 = await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="4k")

    assert route.call_count == 2
    assert r2[0]["qualifies"] is False


@respx.mock
async def test_looser_floor_rechecks_a_cached_no(tmp_path):
    reg = make_registry()
    _mock_library()
    route = respx.get(f"{B}/api/v3/release").mock(return_value=_cached_release(4))
    db = _db(tmp_path)
    await settings_store.set_defaults(db, {"minSeeders": 5, "seederRelaxAfterDays": 0})
    assert (await placement.refresh_availability(
        reg, db, tvdb_id=TVDB, instance_id="4k"))[0]["qualifies"] is False

    await settings_store.set_defaults(db, {"minSeeders": 1, "seederRelaxAfterDays": 0})
    r2 = await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="4k")

    assert route.call_count == 2
    assert r2[0]["qualifies"] is True


def test_verdict_reuse_rules():
    from app.services.placement import _verdict_survives_floor_change as ok
    assert ok(3, False, 5) is True      # stricter, cached no  -> still no
    assert ok(3, True, 5) is False      # stricter, cached yes -> may flip
    assert ok(5, True, 1) is True       # looser,   cached yes -> still yes
    assert ok(5, False, 1) is False     # looser,   cached no  -> may flip
    assert ok(3, True, 3) is True       # unchanged
    # A NULL threshold predates the seeder gate, i.e. it was computed with no
    # filter at all — a floor of 0, so the same monotonicity applies.
    assert ok(None, False, 5) is True   # no filter found nothing -> a filter won't
    assert ok(None, True, 5) is False   # found something unfiltered -> may flip


# --- an outage is not a fact about the episode --------------------------------

def _mock_health(base, entries):
    respx.get(f"{base}/api/v3/health").mock(return_value=httpx.Response(200, json=entries))


INDEXERS_DOWN = [{"source": "IndexerStatusCheck", "type": "warning",
                  "message": "Indexers unavailable due to failures: Uindex"}]


@respx.mock
async def test_empty_search_during_an_indexer_outage_is_not_recorded(tmp_path):
    """Zero releases while indexers are down means "we couldn't ask", not
    "nothing exists" — caching it marks the episode unavailable for hours."""
    reg = make_registry()
    _mock_library()
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[]))
    _mock_health(B, INDEXERS_DOWN)
    db = _db(tmp_path)

    rows = await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="4k")

    assert rows[0]["unknown"] is True
    assert rows[0]["qualifies"] is False
    # Nothing cached, so the next tick asks again instead of waiting out a TTL.
    assert await avail_cache.get_cached(db, "4k", TVDB, 1, 2) is None


@respx.mock
async def test_empty_search_with_healthy_indexers_is_still_recorded(tmp_path):
    """The regression fence: a genuine "nothing exists" must still be cached."""
    reg = make_registry()
    _mock_library()
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[]))
    _mock_health(B, [{"source": "RootFolderCheck", "type": "warning", "message": "x"}])
    db = _db(tmp_path)

    rows = await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="4k")

    assert rows[0].get("unknown") is not True
    cached = await avail_cache.get_cached(db, "4k", TVDB, 1, 2)
    assert cached is not None and cached["total_releases"] == 0


@respx.mock
async def test_a_non_empty_result_is_trusted_even_while_degraded(tmp_path):
    """Releases came back, so the indexers that matter clearly answered."""
    reg = make_registry()
    _mock_library()
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[
        {"rejected": False, "protocol": "torrent", "seeders": 30, "guid": "g", "indexerId": 1},
    ]))
    _mock_health(B, INDEXERS_DOWN)
    db = _db(tmp_path)

    rows = await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="4k")

    assert rows[0]["qualifies"] is True
    assert await avail_cache.get_cached(db, "4k", TVDB, 1, 2) is not None


@respx.mock
async def test_unreachable_health_endpoint_keeps_the_old_behaviour(tmp_path):
    """If we can't tell, don't invent an outage — degrade to what we did before."""
    reg = make_registry()
    _mock_library()
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{B}/api/v3/health").mock(return_value=httpx.Response(500))
    db = _db(tmp_path)

    rows = await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="4k")

    assert rows[0].get("unknown") is not True
    assert await avail_cache.get_cached(db, "4k", TVDB, 1, 2) is not None


@respx.mock
async def test_health_is_checked_once_per_refresh_not_once_per_episode(tmp_path):
    reg = make_registry()
    respx.get(f"{B}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 10, "tvdbId": TVDB, "title": "X"}]))
    respx.get(f"{B}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 1000 + n, "seasonNumber": 1, "episodeNumber": n,
         "monitored": True, "hasFile": False} for n in range(1, 7)
    ]))
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[]))
    route = respx.get(f"{B}/api/v3/health").mock(
        return_value=httpx.Response(200, json=INDEXERS_DOWN))
    db = _db(tmp_path)

    rows = await placement.refresh_availability(reg, db, tvdb_id=TVDB, instance_id="4k")

    assert len(rows) == 6
    assert all(r["unknown"] for r in rows)
    assert route.call_count == 1
