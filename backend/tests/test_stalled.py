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
async def test_sweep_respects_per_tick_cap(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    records = [_rec(id=100 + i, added=(NOW - timedelta(days=10)).isoformat()) for i in range(5)]
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue(records))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    for i in range(5):
        respx.delete(f"{A}/api/v3/queue/{100 + i}").mock(return_value=httpx.Response(200))

    removed = await sweep_stalled(reg, db, ops, stalled_days=3, cap=2, now=NOW)
    assert removed == 2
