"""Episode-level availability + placement engine."""
from datetime import datetime, timezone

import httpx
import respx
from starlette.testclient import TestClient

from app.db import Database
from app.main import app
from app.services import placement
from app.services.placement import _infer_desired, tier_priority
from app.state import get_db, get_registry
from app.store import placements as place_store
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
