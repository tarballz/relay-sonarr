import httpx
import respx

from app.config import Config, InstanceConfig
from app.services.availability import (
    check_availability,
    count_qualifying,
    seeder_filtered_count,
    summarize_rejections,
)
from app.sonarr.registry import Registry

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


def test_seeder_filtered_count_counts_only_gate_exclusions():
    releases = [
        {"rejected": False, "protocol": "torrent", "seeders": 1},   # gated
        {"rejected": True, "protocol": "torrent", "seeders": 1},    # rejected, not the gate
        {"rejected": False, "protocol": "torrent", "seeders": 9},   # passes
        {"rejected": False, "protocol": "usenet"},                  # never gated
    ]
    assert seeder_filtered_count(releases, min_seeders=3) == 1


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
