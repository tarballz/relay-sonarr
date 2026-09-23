from datetime import datetime, timedelta, timezone

import httpx
import respx

from app.config import Config, InstanceConfig
from app.services.availability import (
    DEFAULT_MIN_SEEDERS,
    _pick_sample_episodes,
    check_availability,
    count_qualifying,
    effective_min_seeders,
    seeder_filtered_count,
    summarize_rejections,
)
from app.sonarr.registry import Registry

NOW_TS = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)

B = "http://10.0.0.2:8990"


def make_registry():
    cfg = Config(
        instances=[InstanceConfig(id="4k", name="Sonarr 4K", url=B, api_key="kb")],
        fallbacks={"4k": "1080p"},
    )
    return Registry(cfg)


# --- pure logic --------------------------------------------------------------

def test_count_qualifying_ignores_rejected():
    releases = [
        {"guid": "a", "rejected": False},
        {"guid": "b", "rejected": True, "rejections": ["quality"]},
        {"guid": "c", "rejected": False},
    ]
    assert count_qualifying(releases) == 2


def test_count_qualifying_empty_is_zero():
    assert count_qualifying([]) == 0


def test_count_qualifying_all_rejected_is_zero():
    assert count_qualifying([{"rejected": True}, {"rejected": True}]) == 0


def test_summarize_rejections_buckets_and_sorts_by_count():
    releases = (
        [{"rejected": True, "rejections": ["Unknown Series"]}] * 29
        + [{"rejected": True, "rejections": ["Not enough seeders: 1. Minimum seeders: 2"]}] * 2
        + [{"rejected": False}]  # a qualifying one is ignored
    )
    summary = summarize_rejections(releases)
    assert summary[0] == {"reason": "wrong series", "count": 29}
    assert summary[1] == {"reason": "too few seeders", "count": 2}


def test_summarize_rejections_quality_and_other():
    releases = [
        {"rejected": True, "rejections": ["Bluray-1080p is not wanted in profile"]},
        {"rejected": True, "rejections": ["Some bespoke reason"]},
    ]
    by_reason = {r["reason"]: r["count"] for r in summarize_rejections(releases)}
    assert by_reason["quality not allowed"] == 1
    assert by_reason["Some bespoke reason"] == 1


# --- minSeeders gate (pure logic) --------------------------------------------

def test_count_qualifying_min_seeders_filters_weak_torrents():
    releases = [
        {"guid": "a", "rejected": False, "protocol": "torrent", "seeders": 1},
        {"guid": "b", "rejected": False, "protocol": "torrent", "seeders": 5},
    ]
    assert count_qualifying(releases, min_seeders=3) == 1


def test_count_qualifying_usenet_always_qualifies():
    releases = [{"guid": "n", "rejected": False, "protocol": "usenet"}]
    assert count_qualifying(releases, min_seeders=3) == 1


def test_count_qualifying_torrent_null_seeders_treated_as_zero():
    releases = [{"guid": "t", "rejected": False, "protocol": "torrent", "seeders": None}]
    assert count_qualifying(releases, min_seeders=1) == 0


def test_count_qualifying_min_seeders_zero_disables_gate():
    releases = [{"guid": "t", "rejected": False, "protocol": "torrent", "seeders": 0}]
    assert count_qualifying(releases, min_seeders=0) == 1


def test_count_qualifying_missing_protocol_not_gated():
    releases = [{"guid": "x", "rejected": False}]
    assert count_qualifying(releases, min_seeders=3) == 1


def test_count_qualifying_excludes_dangerous_titles():
    releases = [
        {"rejected": False, "title": "From.S04E06.1080p.WEB.h264-ETH.scr"},
        {"rejected": False, "title": "From.S04E08.1080p.WEB.h264-ETHEL.exe"},
        {"rejected": False, "title": "From.S04E06.1080p.WEB.h264-NTb"},
    ]
    assert count_qualifying(releases) == 1


def test_seeder_filtered_count_counts_only_gate_exclusions():
    releases = [
        {"rejected": False, "protocol": "torrent", "seeders": 1},   # gated
        {"rejected": True, "protocol": "torrent", "seeders": 1},    # rejected, not the gate
        {"rejected": False, "protocol": "torrent", "seeders": 9},   # passes
        {"rejected": False, "protocol": "usenet"},                  # never gated
    ]
    assert seeder_filtered_count(releases, min_seeders=3) == 1


# --- sampling ----------------------------------------------------------------

def test_pick_sample_episodes_spreads_across_the_series():
    # Consecutive episodes share a release group and a scene-naming fate, so
    # three neighbours are barely more informative than one. Spread the picks.
    episodes = [{"id": i, "monitored": True, "hasFile": False} for i in range(1, 31)]
    picks = [e["id"] for e in _pick_sample_episodes(episodes, limit=3)]
    assert picks == [1, 15, 30]  # first, middle, last — never three neighbours


def test_pick_sample_episodes_returns_pool_when_smaller_than_limit():
    episodes = [{"id": 4, "monitored": True, "hasFile": False}]
    assert [e["id"] for e in _pick_sample_episodes(episodes, limit=3)] == [4]
    assert _pick_sample_episodes([], limit=3) == []


def test_pick_sample_episodes_keeps_the_gap_first_priority():
    # Same priority ladder as the single-sample picker: genuine gaps, then
    # monitored, then anything.
    episodes = [
        {"id": 1, "monitored": True, "hasFile": True},
        {"id": 2, "monitored": True, "hasFile": False},
        {"id": 3, "monitored": True, "hasFile": False},
    ]
    assert [e["id"] for e in _pick_sample_episodes(episodes, limit=2)] == [2, 3]


# --- orchestration -----------------------------------------------------------

@respx.mock
async def test_check_availability_available_when_qualifying_release_exists():
    reg = make_registry()
    respx.get(f"{B}/api/v3/episode").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 1, "monitored": False, "title": "E1"},
                {"id": 2, "monitored": True, "title": "E2"},
            ],
        )
    )
    route = respx.get(f"{B}/api/v3/release").mock(
        return_value=httpx.Response(200, json=[{"rejected": False}, {"rejected": True}])
    )

    result = await check_availability(reg, "4k", series_id=42)

    # Sampled the monitored episode.
    assert route.calls.last.request.url.params["episodeId"] == "2"
    assert result["available"] is True
    assert result["releaseCount"] == 1
    assert result["sampledEpisode"]["id"] == 2


@respx.mock
async def test_check_availability_not_available_when_all_rejected():
    reg = make_registry()
    respx.get(f"{B}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=[{"id": 7, "monitored": True, "title": "E"}])
    )
    respx.get(f"{B}/api/v3/release").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"rejected": True, "rejections": ["Unknown Series"]},
                {"rejected": True, "rejections": ["Unknown Series"]},
            ],
        )
    )

    result = await check_availability(reg, "4k", series_id=42)

    assert result["available"] is False
    assert result["releaseCount"] == 0
    assert result["totalReleases"] == 2
    assert result["rejectionSummary"] == [{"reason": "wrong series", "count": 2}]


@respx.mock
async def test_check_availability_falls_back_to_first_episode_when_none_monitored():
    reg = make_registry()
    respx.get(f"{B}/api/v3/episode").mock(
        return_value=httpx.Response(
            200, json=[{"id": 3, "monitored": False, "title": "E3"}]
        )
    )
    route = respx.get(f"{B}/api/v3/release").mock(
        return_value=httpx.Response(200, json=[{"rejected": False}])
    )

    result = await check_availability(reg, "4k", series_id=42)

    assert route.calls.last.request.url.params["episodeId"] == "3"
    assert result["available"] is True


@respx.mock
async def test_check_availability_no_episodes_is_unavailable():
    reg = make_registry()
    respx.get(f"{B}/api/v3/episode").mock(return_value=httpx.Response(200, json=[]))

    result = await check_availability(reg, "4k", series_id=42)

    assert result["available"] is False
    assert result["sampledEpisode"] is None


@respx.mock
async def test_check_availability_min_seeders_makes_weak_tier_unavailable():
    reg = make_registry()
    respx.get(f"{B}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=[{"id": 7, "monitored": True, "title": "E"}])
    )
    respx.get(f"{B}/api/v3/release").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"rejected": False, "protocol": "torrent", "seeders": 1},
                {"rejected": False, "protocol": "torrent", "seeders": 1},
            ],
        )
    )

    result = await check_availability(reg, "4k", series_id=42, min_seeders=3)

    assert result["available"] is False
    assert result["releaseCount"] == 0
    assert result["totalReleases"] == 2
    assert result["seederFiltered"] == 2
    assert {"reason": "fewer than 3 seeders", "count": 2} in result["rejectionSummary"]


@respx.mock
async def test_check_availability_default_min_seeders_zero_unchanged():
    reg = make_registry()
    respx.get(f"{B}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=[{"id": 7, "monitored": True, "title": "E"}])
    )
    respx.get(f"{B}/api/v3/release").mock(
        return_value=httpx.Response(
            200, json=[{"rejected": False, "protocol": "torrent", "seeders": 0}]
        )
    )

    result = await check_availability(reg, "4k", series_id=42)

    assert result["available"] is True
    assert result["seederFiltered"] == 0
    assert result["rejectionSummary"] == []


# --- multi-episode sampling (the Jackass/Ink Master false negative) -----------

def _episodes(n):
    return [{"id": i, "monitored": True, "hasFile": False, "title": f"E{i}"}
            for i in range(1, n + 1)]


@respx.mock
async def test_check_availability_second_opinion_when_first_sample_misses():
    """One unlucky episode must not condemn the whole series.

    Live case: Jackass sampled "Poo Cocktail", got 7 releases all rejected as
    Unknown Series, and was declared exhausted — while 25 other episodes were
    grabbable and the reconciler grabbed them an hour later.
    """
    reg = make_registry()
    respx.get(f"{B}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=_episodes(3))
    )

    def by_episode(request):
        ep = request.url.params["episodeId"]
        if ep == "3":
            return httpx.Response(200, json=[{"rejected": False, "title": "good"}])
        return httpx.Response(
            200, json=[{"rejected": True, "rejections": ["Unknown Series"]}]
        )

    respx.get(f"{B}/api/v3/release").mock(side_effect=by_episode)

    result = await check_availability(reg, "4k", series_id=42, sample_limit=3)

    assert result["available"] is True
    assert result["releaseCount"] == 1
    # The reported sample is the one that decided the verdict.
    assert result["sampledEpisode"]["id"] == 3
    assert [e["id"] for e in result["sampledEpisodes"]] == [1, 2, 3]


@respx.mock
async def test_check_availability_stops_at_the_first_qualifying_sample():
    """A hit costs exactly one interactive search, as it always did — extra
    searches are only spent on the answer we got wrong before."""
    reg = make_registry()
    respx.get(f"{B}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=_episodes(10))
    )
    route = respx.get(f"{B}/api/v3/release").mock(
        return_value=httpx.Response(200, json=[{"rejected": False}])
    )

    result = await check_availability(reg, "4k", series_id=42, sample_limit=3)

    assert result["available"] is True
    assert route.call_count == 1


@respx.mock
async def test_check_availability_reports_releases_exist_when_none_qualify():
    """Ink Master: 83 releases, 0 qualifying. 'Empty series' was a lie."""
    reg = make_registry()
    respx.get(f"{B}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=_episodes(3))
    )

    def by_episode(request):
        if request.url.params["episodeId"] == "2":
            return httpx.Response(
                200,
                json=[{"rejected": True, "rejections": ["Unknown Series"]}] * 83,
            )
        return httpx.Response(200, json=[])

    respx.get(f"{B}/api/v3/release").mock(side_effect=by_episode)

    result = await check_availability(reg, "4k", series_id=42, sample_limit=3)

    assert result["available"] is False
    assert result["releasesExist"] is True
    # The most informative miss is reported, not an arbitrary one.
    assert result["totalReleases"] == 83
    assert result["sampledEpisode"]["id"] == 2


@respx.mock
async def test_check_availability_releases_exist_false_when_nothing_returned():
    reg = make_registry()
    respx.get(f"{B}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=_episodes(2))
    )
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[]))

    result = await check_availability(reg, "4k", series_id=42, sample_limit=2)

    assert result["available"] is False
    assert result["releasesExist"] is False
    assert result["totalReleases"] == 0


@respx.mock
async def test_check_availability_survives_a_failing_extra_sample():
    """An extra probe timing out must not fail the add — it just doesn't vote."""
    reg = make_registry()
    respx.get(f"{B}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=_episodes(2))
    )

    def by_episode(request):
        if request.url.params["episodeId"] == "2":
            raise httpx.ReadTimeout("indexer took too long")
        return httpx.Response(200, json=[{"rejected": True, "rejections": ["x"]}])

    respx.get(f"{B}/api/v3/release").mock(side_effect=by_episode)

    result = await check_availability(reg, "4k", series_id=42, sample_limit=2)

    assert result["available"] is False
    assert result["totalReleases"] == 1
    assert [e["id"] for e in result["sampledEpisodes"]] == [1]


@respx.mock
async def test_check_availability_returns_releases_of_the_deciding_sample():
    """``include_releases`` feeds grab_then_search — it must hand back the
    releases of the episode that qualified, not the first one searched."""
    reg = make_registry()
    respx.get(f"{B}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=_episodes(2))
    )

    def by_episode(request):
        if request.url.params["episodeId"] == "2":
            return httpx.Response(200, json=[{"rejected": False, "title": "winner"}])
        return httpx.Response(200, json=[{"rejected": True, "rejections": ["x"]}])

    respx.get(f"{B}/api/v3/release").mock(side_effect=by_episode)

    result = await check_availability(
        reg, "4k", series_id=42, sample_limit=2, include_releases=True
    )

    assert [r["title"] for r in result["releases"]] == ["winner"]


# --- effective_min_seeders ----------------------------------------------------

def test_default_min_seeders_is_five():
    """Documents the decision; fails loudly if someone quietly reverts it."""
    assert DEFAULT_MIN_SEEDERS == 5


def _eff(wanted_since, *, floor=5, days=3.0):
    return effective_min_seeders(min_seeders=floor, wanted_since=wanted_since,
                                 now=NOW_TS, relax_after_days=days)


def test_a_freshly_wanted_episode_keeps_the_floor():
    assert _eff((NOW_TS - timedelta(days=1)).isoformat()) == 5


def test_a_long_wanted_episode_relaxes_to_best_available():
    assert _eff((NOW_TS - timedelta(days=30)).isoformat()) == 1


def test_no_wanted_since_keeps_the_floor():
    assert _eff(None) == 5


def test_relax_after_zero_days_never_relaxes():
    assert _eff((NOW_TS - timedelta(days=99)).isoformat(), days=0) == 5


def test_unparseable_wanted_since_keeps_the_floor():
    assert _eff("not-a-timestamp") == 5