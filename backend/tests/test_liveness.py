"""Classifying a torrent as dead from Transmission's own view of the swarm.

Sonarr reports every one of these as ``trackedDownloadStatus: "ok"``, so the
only place the truth lives is the download client. The subtle case is a torrent
whose trackers haven't answered yet: ``seederCount`` is -1, which is *unknown*,
not zero, and must never be mistaken for death.
"""
import httpx
import respx

from app.download.transmission import TransmissionClient
from app.services.liveness import classify, is_dead, liveness_map

RPC = "http://10.0.0.3:9091/transmission/rpc"


def _t(**over):
    base = {
        "hashString": "AbCd",
        "name": "Show.S01E01",
        "metadataPercentComplete": 1,
        "percentDone": 0.0,
        "peersConnected": 0,
        "rateDownload": 0,
        "trackerStats": [{"seederCount": 7}, {"seederCount": 3}],
    }
    base.update(over)
    return base


def test_classify_takes_the_best_tracker_and_lowercases_the_hash():
    live = classify(_t())
    assert live.hash == "abcd"
    assert live.max_seeders == 7
    assert live.has_metadata is True


def test_no_metadata_is_dead():
    assert is_dead(classify(_t(metadataPercentComplete=0.0))) is True


def test_zero_seeders_everywhere_and_idle_is_dead():
    t = _t(trackerStats=[{"seederCount": 0}, {"seederCount": 0}])
    assert is_dead(classify(t)) is True


def test_unreported_trackers_are_unknown_not_dead():
    """seederCount -1 means "no tracker has answered yet" — a fresh torrent."""
    t = _t(trackerStats=[{"seederCount": -1}, {"seederCount": -1}])
    live = classify(t)
    assert live.max_seeders == -1
    assert is_dead(live) is False


def test_no_trackers_at_all_is_unknown_not_dead():
    assert is_dead(classify(_t(trackerStats=[]))) is False


def test_connected_peers_beat_a_zero_seeder_count():
    t = _t(trackerStats=[{"seederCount": 0}], peersConnected=4)
    assert is_dead(classify(t)) is False


def test_active_transfer_beats_a_zero_seeder_count():
    t = _t(trackerStats=[{"seederCount": 0}], rateDownload=50_000)
    assert is_dead(classify(t)) is False


def test_missing_metadata_but_actively_transferring_is_not_dead():
    t = _t(metadataPercentComplete=0.4, rateDownload=1000)
    assert is_dead(classify(t)) is False


@respx.mock
async def test_liveness_map_keys_by_lowercase_hash():
    respx.post(RPC).mock(return_value=httpx.Response(
        200, json={"result": "success", "arguments": {"torrents": [_t(hashString="FFEE")]}}))

    result = await liveness_map(TransmissionClient(base_url=RPC))

    assert set(result) == {"ffee"}
    assert result["ffee"].max_seeders == 7


async def test_liveness_map_is_empty_when_no_client_configured():
    assert await liveness_map(None) == {}


@respx.mock
async def test_liveness_map_degrades_to_empty_when_unreachable():
    """One component down degrades the sweep, it never fails the tick."""
    respx.post(RPC).mock(side_effect=httpx.ConnectError("refused"))

    assert await liveness_map(TransmissionClient(base_url=RPC)) == {}
