"""Autonomous stalled-torrent sweep: remove torrents stuck at 0% past the threshold."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import respx

from app.db import Database
from app.services.poller import sweep_stalled
from app.store.operations import OperationStore
from tests.test_chain import A, B, make_registry

NOW = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)


def _rec(**over):
    base = {"id": 1, "protocol": "torrent", "status": "downloading",
            "size": 1000, "sizeleft": 1000, "added": (NOW - timedelta(days=10)).isoformat(),
            "seriesId": 5, "title": "Show.S01E01", "downloadId": "HASH"}
    base.update(over)
    return base


def _queue(records):
    return httpx.Response(200, json={"page": 1, "pageSize": 200,
                                     "totalRecords": len(records), "records": records})


@respx.mock
async def test_sweep_removes_only_old_zero_percent_torrents(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    a_records = [
        _rec(id=11, added=(NOW - timedelta(days=10)).isoformat()),          # REMOVE
        _rec(id=12, added=(NOW - timedelta(hours=2)).isoformat()),          # too new
        _rec(id=13, sizeleft=400, added=(NOW - timedelta(days=10)).isoformat()),  # partial, not 0%
        _rec(id=14, protocol="usenet", added=(NOW - timedelta(days=10)).isoformat()),  # not a torrent
    ]
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue(a_records))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    del_route = respx.delete(f"{A}/api/v3/queue/11").mock(return_value=httpx.Response(200))

    removed = await sweep_stalled(reg, db, ops, stalled_days=3, cap=25, now=NOW)

    assert removed == 1
    assert del_route.called
    params = del_route.calls.last.request.url.params
    assert params["removeFromClient"] == "true" and params["blocklist"] == "true"
    op = (await ops.recent())[0]
    assert op["kind"] == "stalled-cleanup" and op["source"] == "reconciler"


@respx.mock
async def test_sweep_catches_download_frozen_at_partial_progress(tmp_path):
    # A torrent that downloaded some bytes and then died is invisible to the
    # "transferred nothing" checks: it has real progress, so sizeleft != size.
    # Sonarr exposes no activity timestamp, so the only tell is sizeleft failing
    # to move between ticks (seen live: 9 torrents frozen at 1-97%, 0 peers).
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    frozen = _rec(id=41, size=1000, sizeleft=540, downloadId="FROZEN",
                  added=(NOW - timedelta(days=30)).isoformat())
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue([frozen]))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    del_route = respx.delete(f"{A}/api/v3/queue/41").mock(return_value=httpx.Response(200))

    # First sighting establishes the baseline — nothing is swept on one observation.
    assert await sweep_stalled(reg, db, ops, stalled_days=3, cap=25, now=NOW) == 0
    assert not del_route.called

    # Still 540 bytes left four days later: it has not moved.
    later = NOW + timedelta(days=4)
    assert await sweep_stalled(reg, db, ops, stalled_days=3, cap=25, now=later) == 1
    assert del_route.called


@respx.mock
async def test_sweep_leaves_a_download_that_is_still_progressing(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    del_route = respx.delete(f"{A}/api/v3/queue/51").mock(return_value=httpx.Response(200))

    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue(
        [_rec(id=51, size=1000, sizeleft=900, downloadId="MOVING",
              added=(NOW - timedelta(days=30)).isoformat())]))
    await sweep_stalled(reg, db, ops, stalled_days=3, cap=25, now=NOW)

    # Four days on it has advanced 900 -> 300, so the clock restarts.
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue(
        [_rec(id=51, size=1000, sizeleft=300, downloadId="MOVING",
              added=(NOW - timedelta(days=30)).isoformat())]))
    removed = await sweep_stalled(reg, db, ops, stalled_days=3, cap=25,
                                  now=NOW + timedelta(days=4))

    assert removed == 0
    assert not del_route.called


@respx.mock
async def test_sweep_deletes_a_season_pack_once(tmp_path):
    # A season pack is one torrent but one queue record per episode. Deleting any
    # record removes the whole torrent, so the siblings 404 (seen live: 19-record
    # packs producing a burst of 404 warnings).
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    pack = [_rec(id=60 + i, size=0, sizeleft=0, downloadId="PACK", status="queued",
                 added=(NOW - timedelta(days=30)).isoformat()) for i in range(19)]
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue(pack))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    routes = [respx.delete(f"{A}/api/v3/queue/{60 + i}").mock(
        return_value=httpx.Response(200)) for i in range(19)]

    removed = await sweep_stalled(reg, db, ops, stalled_days=3, cap=25, now=NOW)

    assert removed == 1, "one torrent, so exactly one delete"
    assert sum(1 for r in routes if r.called) == 1


@respx.mock
async def test_sweep_catches_records_with_unknown_size(tmp_path):
    # Sonarr reports size=0/sizeleft=0 when a torrent never fetched its metadata
    # — the deadest state there is. The old `size <= 0` guard skipped exactly
    # those, so they were never swept (seen live: 80 queue records at size=0,
    # stuck 37 days, blocking re-grabs because the queue "already meets cutoff").
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    records = [
        _rec(id=31, size=0, sizeleft=0, status="queued",
             added=(NOW - timedelta(days=37)).isoformat()),                      # REMOVE
        _rec(id=32, size=0, sizeleft=0, status="queued",
             added=(NOW - timedelta(hours=2)).isoformat()),                      # too new: metadata may still arrive
    ]
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue(records))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    del_route = respx.delete(f"{A}/api/v3/queue/31").mock(return_value=httpx.Response(200))

    removed = await sweep_stalled(reg, db, ops, stalled_days=3, cap=25, now=NOW)

    assert removed == 1
    assert del_route.called


@respx.mock
async def test_sweep_catches_queued_zero_percent(tmp_path):
    # Sonarr reports torrents the client hasn't started as status "queued" —
    # at 0% for days they're just as dead as "downloading" ones (seen live:
    # a 4K season pack sat queued/0% for 6 days, invisible to the old sweep).
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    records = [
        _rec(id=21, status="queued", added=(NOW - timedelta(days=6)).isoformat()),   # REMOVE
        _rec(id=22, status="paused", added=(NOW - timedelta(days=6)).isoformat()),   # user-paused: leave
    ]
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue(records))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    del_route = respx.delete(f"{A}/api/v3/queue/21").mock(return_value=httpx.Response(200))

    removed = await sweep_stalled(reg, db, ops, stalled_days=3, cap=25, now=NOW)

    assert removed == 1
    assert del_route.called


@respx.mock
async def test_sweep_respects_per_tick_cap(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    # Distinct downloadIds: five separate torrents, not one pack. The cap counts
    # torrents, and records sharing a downloadId collapse to a single removal.
    records = [_rec(id=100 + i, downloadId=f"HASH{i}",
                    added=(NOW - timedelta(days=10)).isoformat()) for i in range(5)]
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue(records))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    for i in range(5):
        respx.delete(f"{A}/api/v3/queue/{100 + i}").mock(return_value=httpx.Response(200))

    removed = await sweep_stalled(reg, db, ops, stalled_days=3, cap=2, now=NOW)
    assert removed == 2
