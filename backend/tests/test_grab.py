"""Seeder-first release selection + direct grab with search fallback."""
import json

import httpx
import pytest
import respx

from app.services.grab import grab_then_search, pick_best_torrent, regrab_episode
from app.sonarr.client import SonarrClient

BASE = "http://192.168.1.10:8989"


@pytest.fixture
def client():
    return SonarrClient(base_url=BASE, api_key="k")


def _t(guid, seeders, **over):
    base = {"guid": guid, "indexerId": 1, "title": f"rel-{guid}",
            "protocol": "torrent", "seeders": seeders, "rejected": False}
    base.update(over)
    return base


# --- pick_best_torrent --------------------------------------------------------

def test_pick_best_sorts_by_seeders_desc():
    releases = [_t("a", 5), _t("b", 50), _t("c", 12)]
    assert pick_best_torrent(releases)["guid"] == "b"


def test_pick_best_skips_rejected_and_below_threshold():
    releases = [
        _t("a", 100, rejected=True),
        _t("b", 1),
        _t("c", 4),
    ]
    assert pick_best_torrent(releases, min_seeders=3)["guid"] == "c"


def test_pick_best_none_when_usenet_only():
    releases = [{"guid": "n", "indexerId": 2, "protocol": "usenet", "rejected": False}]
    assert pick_best_torrent(releases) is None


def test_pick_best_none_when_all_below_threshold():
    assert pick_best_torrent([_t("a", 1), _t("b", 2)], min_seeders=3) is None


def test_pick_best_tiebreak_keeps_sonarr_order():
    # Sonarr returns releases pre-sorted by its own preference; a stable sort
    # on seeders must keep that order among ties.
    releases = [_t("first", 10), _t("second", 10)]
    assert pick_best_torrent(releases)["guid"] == "first"


def test_pick_best_null_seeders_treated_as_zero():
    assert pick_best_torrent([_t("a", None)], min_seeders=1) is None


def test_pick_best_never_picks_dangerous_title():
    # Malware fakes routinely advertise high seeder counts — the seeder sort
    # must never make one the winner.
    releases = [_t("evil", 999, title="From.S04E06.1080p.WEB.h264-ETH.scr"),
                _t("good", 5)]
    assert pick_best_torrent(releases)["guid"] == "good"


# --- grab_then_search ----------------------------------------------------------

@respx.mock
async def test_grab_then_search_grabs_top_then_fires_series_search(client):
    grab = respx.post(f"{BASE}/api/v3/release").mock(
        return_value=httpx.Response(200, json={"guid": "b"})
    )
    cmd = respx.post(f"{BASE}/api/v3/command").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    result = await grab_then_search(
        client, releases=[_t("a", 5), _t("b", 50, indexerId=7)], series_id=10
    )

    assert result["grabbed"] is True
    assert result["release"]["guid"] == "b"
    body = json.loads(grab.calls.last.request.content)
    assert body == {"guid": "b", "indexerId": 7}
    commands = [json.loads(c.request.content)["name"] for c in cmd.calls]
    assert "SeriesSearch" in commands


@respx.mock
async def test_grab_then_search_grab_4xx_still_fires_search(client):
    respx.post(f"{BASE}/api/v3/release").mock(return_value=httpx.Response(400))
    cmd = respx.post(f"{BASE}/api/v3/command").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    result = await grab_then_search(client, releases=[_t("a", 9)], series_id=10)

    assert result["grabbed"] is False
    commands = [json.loads(c.request.content)["name"] for c in cmd.calls]
    assert "SeriesSearch" in commands


@respx.mock
async def test_grab_then_search_no_candidate_fires_search_only(client):
    grab = respx.post(f"{BASE}/api/v3/release").mock(
        return_value=httpx.Response(200, json={})
    )
    cmd = respx.post(f"{BASE}/api/v3/command").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    result = await grab_then_search(
        client,
        releases=[{"guid": "n", "indexerId": 2, "protocol": "usenet", "rejected": False}],
        episode_ids=[42],
    )

    assert result["grabbed"] is False
    assert not grab.called
    body = json.loads(cmd.calls.last.request.content)
    assert body["name"] == "EpisodeSearch"
    assert body["episodeIds"] == [42]


@respx.mock
async def test_grab_then_search_disabled_fires_search_only(client):
    grab = respx.post(f"{BASE}/api/v3/release").mock(
        return_value=httpx.Response(200, json={})
    )
    cmd = respx.post(f"{BASE}/api/v3/command").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    result = await grab_then_search(
        client, releases=[_t("a", 50)], series_id=10, enabled=False
    )

    assert result["grabbed"] is False
    assert not grab.called
    assert json.loads(cmd.calls.last.request.content)["name"] == "SeriesSearch"


@respx.mock
async def test_grab_then_search_grab_only_mode_fires_no_command(client):
    # Both ids None: the caller already fired its own search (advance_fallback's
    # available branch) — grab the candidate but post no command.
    respx.post(f"{BASE}/api/v3/release").mock(
        return_value=httpx.Response(200, json={"guid": "a"})
    )
    cmd = respx.post(f"{BASE}/api/v3/command").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    result = await grab_then_search(client, releases=[_t("a", 50)])

    assert result["grabbed"] is True
    assert result["searched"] is False
    assert not cmd.called


# --- relaxed floor ------------------------------------------------------------

def test_relaxed_floor_accepts_a_release_below_the_configured_one():
    """Thin-swarm back-catalogue: 4 seeders beats waiting forever for 5."""
    releases = [_t("a", 2), _t("b", 4)]
    assert pick_best_torrent(releases, 5) is None
    assert pick_best_torrent(releases, 1)["guid"] == "b"


def test_a_zero_seeder_release_is_never_a_candidate():
    """The floor never drops below 1, at any threshold including 0."""
    assert pick_best_torrent([_t("a", 0)], 0) is None


def test_relaxed_floor_still_refuses_dangerous_and_rejected():
    releases = [_t("a", 9, title="payload.exe"), _t("b", 9, rejected=True)]
    assert pick_best_torrent(releases, 1) is None


# --- regrab_episode -----------------------------------------------------------

@respx.mock
async def test_regrab_searches_then_grabs_the_best_seeded(client):
    search = respx.get(f"{BASE}/api/v3/release").mock(
        return_value=httpx.Response(200, json=[_t("a", 3), _t("b", 31), _t("c", 12)]))
    grab = respx.post(f"{BASE}/api/v3/release").mock(
        return_value=httpx.Response(200, json={}))

    best = await regrab_episode(client, episode_id=123, min_seeders=5)

    assert search.calls.last.request.url.params["episodeId"] == "123"
    assert json.loads(grab.calls.last.request.content)["guid"] == "b"
    assert best["guid"] == "b"


@respx.mock
async def test_regrab_returns_none_and_grabs_nothing_when_all_dead(client):
    respx.get(f"{BASE}/api/v3/release").mock(
        return_value=httpx.Response(200, json=[_t("a", 0), _t("b", 1)]))
    grab = respx.post(f"{BASE}/api/v3/release").mock(return_value=httpx.Response(200, json={}))

    assert await regrab_episode(client, episode_id=7, min_seeders=5) is None
    assert not grab.called


@respx.mock
async def test_regrab_honours_a_relaxed_floor(client):
    respx.get(f"{BASE}/api/v3/release").mock(
        return_value=httpx.Response(200, json=[_t("a", 2)]))
    grab = respx.post(f"{BASE}/api/v3/release").mock(return_value=httpx.Response(200, json={}))

    best = await regrab_episode(client, episode_id=7, min_seeders=1)

    assert best["guid"] == "a"
    assert grab.called


@respx.mock
async def test_regrab_swallows_a_failed_grab(client):
    """A 4xx on the grab must not abort the sweep that called us."""
    respx.get(f"{BASE}/api/v3/release").mock(
        return_value=httpx.Response(200, json=[_t("a", 30)]))
    respx.post(f"{BASE}/api/v3/release").mock(return_value=httpx.Response(400))

    assert await regrab_episode(client, episode_id=7, min_seeders=5) is None


@respx.mock
async def test_regrab_swallows_a_failed_search(client):
    respx.get(f"{BASE}/api/v3/release").mock(return_value=httpx.Response(500))

    assert await regrab_episode(client, episode_id=7, min_seeders=5) is None
