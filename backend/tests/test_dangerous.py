"""Dangerous-release sweep: purge executable/malware fakes from the queue.

Modeled on the live incident: fake "From S04" releases whose payload was a
single .scr/.exe, flagged by Sonarr ("Caution: Found potentially dangerous
file…") but left sitting in importPending for weeks with the file on disk.
"""
import json
from datetime import datetime, timedelta, timezone

import httpx
import respx

from app.db import Database
from app.services.poller import _is_dangerous, sweep_dangerous
from app.store.operations import OperationStore
from tests.test_chain import A, B, make_registry

NOW = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)


def _rec(**over):
    base = {"id": 1, "protocol": "torrent", "status": "completed",
            "trackedDownloadState": "importPending",
            "size": 900, "sizeleft": 0, "seriesId": 5,
            "title": "Show.S01E01.1080p.WEB.h264-GRP",
            "episode": {"id": 77, "seasonNumber": 1, "episodeNumber": 1},
            "downloadId": "HASH"}
    base.update(over)
    return base


def _queue(records):
    return httpx.Response(200, json={"page": 1, "pageSize": 200,
                                     "totalRecords": len(records), "records": records})


# --- detection ----------------------------------------------------------------

def test_is_dangerous_flags_sonarr_caution_message():
    rec = _rec(statusMessages=[{
        "title": "From.S04E06.1080p.WEB.h264-ETH.scr",
        "messages": ["Caution: Found potentially dangerous file with extension: .scr"],
    }])
    assert _is_dangerous(rec) is True


def test_is_dangerous_flags_executable_extension_in_title():
    assert _is_dangerous(_rec(title="From.S04E08.1080p.WEB.h264-ETHEL.exe")) is True
    assert _is_dangerous(_rec(title="From.S04E02.1080p.WEB.h264-ETHEL.scr")) is True


def test_is_dangerous_ignores_normal_releases():
    assert _is_dangerous(_rec()) is False
    # Indexer-prefixed titles contain domains but don't end in an executable ext.
    assert _is_dangerous(
        _rec(title="www.Torrenting.com - The Americans S03E01 1080p WEB-DL")
    ) is False
    assert _is_dangerous(_rec(statusMessages=[
        {"title": "x", "messages": ["Episode file already imported"]}
    ])) is False


# --- sweep --------------------------------------------------------------------

@respx.mock
async def test_sweep_removes_flagged_release_and_researches(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    records = [
        _rec(id=31, title="From.S04E06.1080p.WEB.h264-ETH.scr",
             statusMessages=[{"title": "f.scr", "messages": [
                 "Caution: Found potentially dangerous file with extension: .scr"]}],
             episode={"id": 406, "seasonNumber": 4, "episodeNumber": 6}),
        _rec(id=32),  # clean — untouched
    ]
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue(records))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    del_route = respx.delete(f"{A}/api/v3/queue/31").mock(return_value=httpx.Response(200))
    cmd = respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))

    removed = await sweep_dangerous(reg, db, ops, cap=25, now=NOW)

    assert removed == 1
    params = del_route.calls.last.request.url.params
    assert params["removeFromClient"] == "true" and params["blocklist"] == "true"
    body = json.loads(cmd.calls.last.request.content)
    assert body["name"] == "EpisodeSearch" and body["episodeIds"] == [406]
    op = (await ops.recent())[0]
    assert op["kind"] == "dangerous-cleanup" and op["source"] == "reconciler"


@respx.mock
async def test_sweep_dangerous_respects_cap(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    records = [_rec(id=40 + i, title=f"Fake.S01E0{i}.1080p.WEB-GRP.exe") for i in range(4)]
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue(records))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    for i in range(4):
        respx.delete(f"{A}/api/v3/queue/{40 + i}").mock(return_value=httpx.Response(200))
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))

    removed = await sweep_dangerous(reg, db, ops, cap=2, now=NOW)
    assert removed == 2
