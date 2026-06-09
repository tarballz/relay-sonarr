"""Roadblock resolution options: build_options, reattempt_search, fill_gaps."""
import json

import httpx
import pytest
import respx

from app.services.add import build_options, fill_gaps, reattempt_search
from app.services.availability import _pick_sample_episode
from tests.test_chain import A, B, _nosleep, make_registry, mock_1080p_profiles


# --- availability sampling ---------------------------------------------------

def test_pick_sample_prefers_a_missing_monitored_episode():
    # First monitored episode already has a file (e.g. S01E01 in 4K); the sample
    # must skip it and land on a genuine gap so partial series aren't "available".
    episodes = [
        {"id": 1, "monitored": True, "hasFile": True},
        {"id": 2, "monitored": True, "hasFile": False},
    ]
    assert _pick_sample_episode(episodes)["id"] == 2


def test_pick_sample_falls_back_to_monitored_then_anything():
    assert _pick_sample_episode([{"id": 9, "monitored": True, "hasFile": True}])["id"] == 9
    assert _pick_sample_episode([{"id": 5, "monitored": False}])["id"] == 5
    assert _pick_sample_episode([]) is None


# --- build_options (pure) ----------------------------------------------------

def test_build_options_full_menu_when_cross_instance_next():
    resolved = {
        "instanceId": "1080p", "instanceName": "Sonarr 1080p",
        "qualityProfileName": "HD-1080p",
    }
    by = {o["id"]: o for o in build_options(
        tvdb_id=1, from_instance_id="4k", from_series_id=10,
        chain_key="4k", next_index=0, resolved=resolved,
    )}
    assert set(by) == {"reattempt", "walk_chain", "fill_gaps", "leave", "remove"}
    assert by["reattempt"]["enabled"] and by["reattempt"]["payload"]["nextIndex"] == 0
    assert by["walk_chain"]["enabled"]
    assert by["fill_gaps"]["enabled"]
    assert by["remove"]["payload"] == {"instanceId": "4k", "seriesId": 10}


def test_build_options_no_fill_gaps_for_same_instance_step():
    resolved = {
        "instanceId": "1080p", "instanceName": "Sonarr 1080p",
        "qualityProfileName": "SD",
    }
    by = {o["id"]: o for o in build_options(
        tvdb_id=1, from_instance_id="1080p", from_series_id=22,
        chain_key="4k", next_index=1, resolved=resolved,
    )}
    assert by["walk_chain"]["enabled"]  # same-instance profile swap is still valid
    assert not by["fill_gaps"]["enabled"]
    assert by["fill_gaps"]["disabledReason"] == "next tier is the same instance"


def test_build_options_chain_disabled_when_exhausted():
    by = {o["id"]: o for o in build_options(
        tvdb_id=1, from_instance_id="1080p", from_series_id=22,
        chain_key="4k", next_index=2, resolved=None,
    )}
    assert not by["walk_chain"]["enabled"]
    assert not by["fill_gaps"]["enabled"]
    # Re-attempt / leave / remove are always offered, even at the end of the chain.
    assert by["reattempt"]["enabled"]
    assert by["leave"]["enabled"]
    assert by["remove"]["enabled"]


# --- reattempt_search --------------------------------------------------------

@respx.mock
async def test_reattempt_places_when_now_available():
    reg = make_registry()
    respx.get(f"{B}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=[{"id": 2, "monitored": True, "title": "E"}])
    )
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[{"rejected": False}]))
    cmd = respx.post(f"{B}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))

    result = await reattempt_search(
        reg, tvdb_id=1, instance_id="4k", series_id=10,
        chain_key="4k", next_index=0, sleep=_nosleep,
    )
    assert result["status"] == "placed"
    commands = [json.loads(c.request.content)["name"] for c in cmd.calls]
    assert "SeriesSearch" in commands


@respx.mock
async def test_reattempt_resuggests_menu_when_still_missing():
    reg = make_registry()
    respx.get(f"{B}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=[{"id": 2, "monitored": True, "title": "E"}])
    )
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[{"rejected": True}]))
    mock_1080p_profiles()  # _suggest_next resolves the next step

    result = await reattempt_search(
        reg, tvdb_id=1, instance_id="4k", series_id=10,
        chain_key="4k", next_index=0, sleep=_nosleep,
    )
    assert result["status"] == "fallback_suggested"
    assert {o["id"] for o in result["options"]} >= {"reattempt", "fill_gaps", "remove"}


# --- fill_gaps ---------------------------------------------------------------

@respx.mock
async def test_fill_gaps_splits_episodes():
    reg = make_registry()
    mock_1080p_profiles()  # resolve_step for the 1080p target
    # Origin (4K): S1E1 already grabbed, S1E2 still missing.
    respx.get(f"{B}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 100, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": True},
        {"id": 101, "seasonNumber": 1, "episodeNumber": 2, "monitored": True, "hasFile": False},
    ]))
    # Not yet present on 1080p → add it.
    respx.get(f"{A}/api/v3/series").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{A}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": 1, "title": "X"}])
    )
    post_series = respx.post(f"{A}/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 55}))
    a_cmd = respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))
    # Fallback episodes carry DIFFERENT ids than the origin.
    respx.get(f"{A}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 200, "seasonNumber": 1, "episodeNumber": 1, "monitored": True},
        {"id": 201, "seasonNumber": 1, "episodeNumber": 2, "monitored": True},
    ]))
    mon_fb = respx.put(f"{A}/api/v3/episode/monitor").mock(return_value=httpx.Response(200, json=[]))
    mon_origin = respx.put(f"{B}/api/v3/episode/monitor").mock(return_value=httpx.Response(200, json=[]))

    result = await fill_gaps(
        reg, tvdb_id=1, from_instance_id="4k", from_series_id=10,
        chain_key="4k", next_index=0, sleep=_nosleep,
    )

    assert result["status"] == "split"
    assert result["to"]["seriesId"] == 55
    assert result["gapCount"] == 1
    assert post_series.called

    # On the fallback tier: gap (E2) monitored, non-gap (E1) unmonitored.
    fb_bodies = [json.loads(c.request.content) for c in mon_fb.calls]
    assert {"episodeIds": [201], "monitored": True} in fb_bodies
    assert {"episodeIds": [200], "monitored": False} in fb_bodies
    # The gap episode (by fallback id) is searched.
    a_cmds = [json.loads(c.request.content) for c in a_cmd.calls]
    assert {"name": "EpisodeSearch", "episodeIds": [201]} in a_cmds
    # Origin unmonitors the gap episode (its own id) so the split is disjoint.
    assert json.loads(mon_origin.calls.last.request.content) == {
        "episodeIds": [101], "monitored": False
    }


@respx.mock
async def test_fill_gaps_reuses_existing_series_on_fallback():
    reg = make_registry()
    mock_1080p_profiles()
    respx.get(f"{B}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 101, "seasonNumber": 1, "episodeNumber": 2, "monitored": True, "hasFile": False},
    ]))
    # Series ALREADY on 1080p (id 77) → reuse, do not POST a duplicate.
    respx.get(f"{A}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 77, "tvdbId": 1}])
    )
    post_series = respx.post(f"{A}/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 999}))
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))
    respx.get(f"{A}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 201, "seasonNumber": 1, "episodeNumber": 2, "monitored": True},
    ]))
    respx.put(f"{A}/api/v3/episode/monitor").mock(return_value=httpx.Response(200, json=[]))
    respx.put(f"{B}/api/v3/episode/monitor").mock(return_value=httpx.Response(200, json=[]))

    result = await fill_gaps(
        reg, tvdb_id=1, from_instance_id="4k", from_series_id=10,
        chain_key="4k", next_index=0, sleep=_nosleep,
    )
    assert result["to"]["seriesId"] == 77
    assert not post_series.called


@respx.mock
async def test_fill_gaps_rejects_same_instance_step():
    reg = make_registry()
    mock_1080p_profiles()
    # next_index=1 is the SD profile on 1080p — same instance as the origin.
    with pytest.raises(ValueError, match="different instance"):
        await fill_gaps(
            reg, tvdb_id=1, from_instance_id="1080p", from_series_id=22,
            chain_key="4k", next_index=1, sleep=_nosleep,
        )


@respx.mock
async def test_fill_gaps_noop_when_nothing_missing():
    reg = make_registry()
    mock_1080p_profiles()
    respx.get(f"{B}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 100, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": True},
    ]))
    result = await fill_gaps(
        reg, tvdb_id=1, from_instance_id="4k", from_series_id=10,
        chain_key="4k", next_index=0, sleep=_nosleep,
    )
    assert result["status"] == "split"
    assert result["gapCount"] == 0


@respx.mock
async def test_fill_gaps_raises_when_fallback_episodes_never_populate():
    reg = make_registry()
    mock_1080p_profiles()
    respx.get(f"{B}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 101, "seasonNumber": 1, "episodeNumber": 2, "monitored": True, "hasFile": False},
    ]))
    respx.get(f"{A}/api/v3/series").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{A}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": 1, "title": "X"}])
    )
    respx.post(f"{A}/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 55}))
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))
    respx.get(f"{A}/api/v3/episode").mock(return_value=httpx.Response(200, json=[]))  # never populates

    with pytest.raises(ValueError, match="never populated"):
        await fill_gaps(
            reg, tvdb_id=1, from_instance_id="4k", from_series_id=10,
            chain_key="4k", next_index=0, sleep=_nosleep, wait_attempts=2, wait_delay=0,
        )


# --- spill_season: move one season to the lower tier -------------------------

@respx.mock
async def test_spill_season_searches_season_keys_on_fallback_and_unmonitors_origin():
    from app.services.orchestrate import spill_season

    reg = make_registry()
    mock_1080p_profiles()  # resolve_step for the 1080p fallback
    # Origin = 4k (B): S1E1 + season 2 (E1, E2).
    respx.get(f"{B}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 10, "tvdbId": 75710, "title": "X"}])
    )
    respx.get(f"{B}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 1001, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": True},
        {"id": 1021, "seasonNumber": 2, "episodeNumber": 1, "monitored": True, "hasFile": False},
        {"id": 1022, "seasonNumber": 2, "episodeNumber": 2, "monitored": True, "hasFile": False},
    ]))
    origin_monitor = respx.put(f"{B}/api/v3/episode/monitor").mock(
        return_value=httpx.Response(200, json={})
    )
    # Fallback = 1080p (A): series already present.
    respx.get(f"{A}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 55, "tvdbId": 75710, "title": "X"}])
    )
    respx.get(f"{A}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 2021, "seasonNumber": 2, "episodeNumber": 1, "monitored": False, "hasFile": False},
        {"id": 2022, "seasonNumber": 2, "episodeNumber": 2, "monitored": False, "hasFile": False},
    ]))
    respx.put(f"{A}/api/v3/episode/monitor").mock(return_value=httpx.Response(200, json={}))
    fb_cmd = respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))

    result = await spill_season(
        reg, tvdb_id=75710, season=2, origin_instance_id="4k", fb_instance_id="1080p",
        sleep=_nosleep,
    )

    assert result["status"] == "spilled"
    assert result["season"] == 2
    assert result["episodeCount"] == 2
    assert result["to"]["instanceId"] == "1080p"
    assert result["to"]["seriesId"] == 55
    # Fallback searched exactly the season-2 episode ids.
    searched = [json.loads(c.request.content) for c in fb_cmd.calls
                if json.loads(c.request.content)["name"] == "EpisodeSearch"]
    assert searched and sorted(searched[-1]["episodeIds"]) == [2021, 2022]
    # Origin unmonitored the same season-2 episodes.
    body = json.loads(origin_monitor.calls.last.request.content)
    assert sorted(body["episodeIds"]) == [1021, 1022]
    assert body["monitored"] is False


@respx.mock
async def test_spill_season_no_matching_episodes_returns_zero():
    from app.services.orchestrate import spill_season

    reg = make_registry()
    mock_1080p_profiles()
    respx.get(f"{B}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 10, "tvdbId": 75710, "title": "X"}])
    )
    respx.get(f"{B}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 1001, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": True},
    ]))

    result = await spill_season(
        reg, tvdb_id=75710, season=5, origin_instance_id="4k", fb_instance_id="1080p",
        sleep=_nosleep,
    )
    assert result["status"] == "spilled"
    assert result["episodeCount"] == 0
