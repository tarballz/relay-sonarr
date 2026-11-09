"""Fake swarms: peers that never deliver metadata are not evidence of life.

Live incident (2026-11-09): "South Park S29E05 ... MeGusta [SelfExtract Bundle]"
was grabbed 2026-10-30 — thirteen days before the episode aired (2026-11-12).
Ten days later Transmission still had metadataPercentComplete 0, 50 connected
peers, 9 tracker-reported seeders and 0 B/s. A real swarm with one honest peer
hands over the metadata (a few KiB) in seconds; a "swarm" that never does is a
fake. But ``is_dead`` let ``peers_connected > 0`` win before it ever looked at
metadata, ``sweep_stalled`` skipped any peer-connected torrent, and
``sweep_frozen`` never spent the nudge budget that would lift that skip because
a metadata-less torrent reports sizeleft 0. Nothing could ever remove it.

The title said what it was, too: a self-extracting bundle is an executable.
"""
from datetime import timedelta

import httpx
import respx

from app.db import Database
from app.services.availability import looks_dangerous
from app.services.liveness import classify, is_dead
from app.services.poller import sweep_stalled
from app.store.operations import OperationStore
from tests.test_chain import A, B, make_registry
from tests.test_liveness import _t
from tests.test_stalled import NOW, _live, _queue, _rec

S29E05 = "South+Park+S29E05+1080p+WEB-DL+HEVC+x265-MeGusta+[SelfExtract+Bundle]"


# --- liveness -----------------------------------------------------------------

def test_peers_without_metadata_are_not_life():
    t = _t(metadataPercentComplete=0.0, peersConnected=50,
           trackerStats=[{"seederCount": 9}, {"seederCount": 1}])
    assert is_dead(classify(t)) is True


def test_peers_with_metadata_still_count_as_life():
    t = _t(trackerStats=[{"seederCount": 0}], peersConnected=4)
    assert is_dead(classify(t)) is False


def test_metadata_actually_arriving_is_not_dead():
    t = _t(metadataPercentComplete=0.0, peersConnected=3, rateDownload=2_000)
    assert is_dead(classify(t)) is False


# --- sweep --------------------------------------------------------------------

def _fake_swarm_rec(*, age):
    # What Sonarr's queue showed: no metadata means no size at all.
    return _rec(id=31, downloadId="FAKE", title=S29E05, status="queued",
                size=0, sizeleft=0, added=(NOW - age).isoformat())


@respx.mock
async def test_metadata_less_torrent_with_fake_peers_is_removed(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue([_fake_swarm_rec(age=timedelta(days=10))]))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    delete = respx.delete(f"{A}/api/v3/queue/31").mock(return_value=httpx.Response(200))
    swarm = {"fake": _live("fake", metadata=False, peers=50, seeders=9)}

    removed = await sweep_stalled(make_registry(), db, OperationStore(db), stalled_days=1,
                                  cap=25, now=NOW, dead_hours=6, liveness=swarm,
                                  nudge_cap=2)

    assert removed == 1 and delete.called
    assert delete.calls.last.request.url.params["blocklist"] == "true"


@respx.mock
async def test_fresh_metadata_less_torrent_with_peers_gets_dead_hours_grace(tmp_path):
    """Metadata can take a moment on a brand-new magnet; deadHours still applies."""
    db = Database(str(tmp_path / "relay.db"))
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue([_fake_swarm_rec(age=timedelta(hours=2))]))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    delete = respx.delete(f"{A}/api/v3/queue/31").mock(return_value=httpx.Response(200))
    swarm = {"fake": _live("fake", metadata=False, peers=50, seeders=9)}

    removed = await sweep_stalled(make_registry(), db, OperationStore(db), stalled_days=1,
                                  cap=25, now=NOW, dead_hours=6, liveness=swarm,
                                  nudge_cap=2)

    assert removed == 0 and not delete.called


# --- title --------------------------------------------------------------------

def test_self_extracting_bundle_title_is_dangerous():
    for title in (S29E05,
                  "South Park S29E05 1080p WEB-DL x265 [SelfExtract Bundle]",
                  "Show.S01E01.1080p.Self-Extracting.Archive",
                  "Show S01E01 720p self extract"):
        assert looks_dangerous({"title": title}) is True, title


def test_ordinary_titles_are_not_dangerous():
    for title in ("South Park S29E04 1080p WEB-DL HEVC x265-MeGusta",
                  "Show.S01E01.Extraction.Point.1080p.WEB",   # a word, not a bundle
                  "Self Made S01E01 1080p WEB-DL"):
        assert looks_dangerous({"title": title}) is False, title
