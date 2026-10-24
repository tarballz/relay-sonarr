"""Dangerous-release sweep: purge executable/malware fakes from the queue.

Modeled on two live incidents:
- Fake "From S04" releases whose payload was a single .scr/.exe, flagged by
  Sonarr ("Caution: Found potentially dangerous file…") but left sitting in
  importPending for weeks with the file on disk.
- A "South Park S29E01" release that was actually a 1.05GB Windows PE32+
  executable with no file extension at all — bulk padding to look like a
  video and to dodge the extension-based checks above. Sonarr's import
  pipeline already does the content inspection here (it can't find a valid
  video container) and reports "Invalid video file, unsupported extension:
  ''"; Relay has no mount onto the downloads tree to re-verify the bytes
  itself, so it trusts Sonarr's verdict for the one case that verdict is
  unambiguous: a *blank* extension. It sat importPending for 7 days before
  anyone noticed, which is the escalation gap this closes.
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


def test_is_dangerous_flags_blank_extension_message():
    # Live incident: "South Park S29E01" was a bare PE32+ executable — no
    # extension at all, so Sonarr's own container-sniffing import check
    # rejects it. That message is the content-based signal Relay has no
    # mount to reproduce itself, so it trusts it.
    rec = _rec(title="South Park S29E01 1080p WEB-DL DDP5 1 x265 FLUX",
               statusMessages=[{
                   "title": "South Park S29E01 1080p WEB-DL DDP5 1 x265 FLUX",
                   "messages": ["Invalid video file, unsupported extension: ''"],
               }])
    assert _is_dangerous(rec) is True


def test_is_dangerous_ignores_nonblank_unsupported_extension():
    # A real (if unusual) container Sonarr won't import — e.g. a bonus .iso
    # — is a format problem, not evidence of a malware fake. Only a *blank*
    # extension is unambiguous: no legitimate release ships with none.
    rec = _rec(statusMessages=[{
        "title": "x", "messages": ["Invalid video file, unsupported extension: '.iso'"],
    }])
    assert _is_dangerous(rec) is False


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
async def test_sweep_removes_blank_extension_release_and_researches(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    records = [
        _rec(id=41, title="South Park S29E01 1080p WEB-DL DDP5 1 x265 FLUX",
             statusMessages=[{"title": "South Park S29E01 1080p WEB-DL DDP5 1 x265 FLUX",
                              "messages": ["Invalid video file, unsupported extension: ''"]}],
             episode={"id": 4123, "seasonNumber": 29, "episodeNumber": 1}),
        _rec(id=42),  # clean — untouched
    ]
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue(records))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    del_route = respx.delete(f"{A}/api/v3/queue/41").mock(return_value=httpx.Response(200))
    cmd = respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))

    removed = await sweep_dangerous(reg, db, ops, cap=25, now=NOW)

    assert removed == 1
    params = del_route.calls.last.request.url.params
    assert params["removeFromClient"] == "true" and params["blocklist"] == "true"
    body = json.loads(cmd.calls.last.request.content)
    assert body["name"] == "EpisodeSearch" and body["episodeIds"] == [4123]


@respx.mock
async def test_sweep_dangerous_records_tvdb_id_not_sonarr_series_id(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue(
        [_rec(id=31, seriesId=5, title="Fake.S01E01.1080p.WEB-GRP.exe")]))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    respx.get(f"{A}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "tvdbId": 99, "title": "Show"}]))
    respx.delete(f"{A}/api/v3/queue/31").mock(return_value=httpx.Response(200))
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))

    assert await sweep_dangerous(reg, db, ops, cap=25, now=NOW) == 1
    assert (await ops.recent())[0]["tvdbId"] == 99


@respx.mock
async def test_sweep_dangerous_respects_cap(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    records = [_rec(id=40 + i, downloadId=f"H{i}", title=f"Fake.S01E0{i}.1080p.WEB-GRP.exe")
               for i in range(4)]  # four separate downloads, not one pack
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue(records))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    for i in range(4):
        respx.delete(f"{A}/api/v3/queue/{40 + i}").mock(return_value=httpx.Response(200))
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))

    removed = await sweep_dangerous(reg, db, ops, cap=2, now=NOW)
    assert removed == 2
