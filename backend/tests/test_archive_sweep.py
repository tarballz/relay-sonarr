"""Unimportable-archive sweep: archive releases can never import without an unpacker.

Live incident (2026-10-23): "South Park S29E04" and "The Simpsons S38E04" sat in
importPending with "Invalid video file, unsupported extension: '.7z'". The stack
runs no unpacker, so an archive release is a dead slot at best and a
password-protected scam at worst — Relay removes + blocklists it and re-searches.
Deliberately separate from the dangerous sweep: an archive is not evidence of
malware, and other named extensions (e.g. a bonus .iso) stay untouched.
"""
import json
from datetime import timedelta

import httpx
import respx

from app.obs import kinds
from app.services.poller import _is_unimportable_archive, sweep_archives
from app.store import events as events_store
from app.store.operations import OperationStore
from tests.test_chain import A, B, make_registry
from tests.test_dangerous import NOW, _queue, _rec


def _archive(ext, *, age_h=3.0, **over):
    return _rec(added=(NOW - timedelta(hours=age_h)).isoformat(),
                statusMessages=[{"title": "x", "messages": [
                    f"Invalid video file, unsupported extension: '{ext}'"]}],
                **over)


async def _events(journal, db, kind):
    await journal.flush()
    return await events_store.query(db, kind=kind)


# --- detection ----------------------------------------------------------------

def test_archive_matched_after_grace():
    for ext in (".7z", ".rar", ".zip", ".r00"):
        assert _is_unimportable_archive(_archive(ext), now=NOW, grace_hours=2) is True, ext


def test_archive_not_matched_within_grace():
    assert _is_unimportable_archive(_archive(".7z", age_h=1), now=NOW, grace_hours=2) is False


def test_other_unsupported_extension_not_matched():
    assert _is_unimportable_archive(_archive(".iso"), now=NOW, grace_hours=2) is False
    assert _is_unimportable_archive(_archive(""), now=NOW, grace_hours=2) is False


def test_archive_requires_import_pending():
    rec = _archive(".7z", trackedDownloadState="downloading")
    assert _is_unimportable_archive(rec, now=NOW, grace_hours=2) is False


# --- sweep --------------------------------------------------------------------

@respx.mock
async def test_sweep_archives_removes_blocklists_and_researches(db, journal):
    ops = OperationStore(db)
    reg = make_registry()
    records = [
        _archive(".7z", id=51, episode={"id": 2904, "seasonNumber": 29, "episodeNumber": 4}),
        _rec(id=52),  # clean — untouched
    ]
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue(records))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    del_route = respx.delete(f"{A}/api/v3/queue/51").mock(return_value=httpx.Response(200))
    cmd = respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))

    removed = await sweep_archives(reg, db, ops, cap=25, now=NOW)

    assert removed == 1
    params = del_route.calls.last.request.url.params
    assert params["removeFromClient"] == "true" and params["blocklist"] == "true"
    body = json.loads(cmd.calls.last.request.content)
    assert body["name"] == "EpisodeSearch" and body["episodeIds"] == [2904]
    op = (await ops.recent())[0]
    assert op["kind"] == "archive-cleanup"
    assert len(await _events(journal, db, kinds.SWEEP_ARCHIVE_REMOVED)) == 1


@respx.mock
async def test_stuck_import_warns_once(db, journal):
    ops = OperationStore(db)
    reg = make_registry()
    stuck = {"statusMessages": [{"title": "x", "messages": ["Episode file already imported"]}]}
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    queue = respx.get(f"{A}/api/v3/queue")

    # Past the 24h threshold -> one warning.
    queue.mock(return_value=_queue([_rec(id=61, added=(NOW - timedelta(hours=24.2)).isoformat(),
                                         **stuck)]))
    assert await sweep_archives(reg, db, ops, cap=25, now=NOW) == 0
    assert len(await _events(journal, db, kinds.SWEEP_IMPORT_STUCK)) == 1

    # Already warned for this download -> silent (journal changes, not repetitions).
    queue.mock(return_value=_queue([_rec(id=61, added=(NOW - timedelta(hours=26)).isoformat(),
                                         **stuck)]))
    await sweep_archives(reg, db, ops, cap=25, now=NOW)
    assert len(await _events(journal, db, kinds.SWEEP_IMPORT_STUCK)) == 1


# --- tick wiring --------------------------------------------------------------

def _tick_routes(records):
    from tests.test_search_stall import _empty
    _empty(B)
    _empty(A)
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue(records))


@respx.mock
async def test_tick_runs_archive_sweep(db, journal):
    from app.reconciler import Reconciler
    from tests.test_chain import _nosleep
    _tick_routes([_archive(".7z", id=71)])
    deleted = respx.delete(f"{A}/api/v3/queue/71").mock(return_value=httpx.Response(200))
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))
    rec = Reconciler(make_registry(), db, OperationStore(db), clock=lambda: NOW, sleep=_nosleep)

    await rec.tick()

    assert deleted.called
    assert (await rec.ops.recent())[0]["kind"] == "archive-cleanup"


@respx.mock
async def test_tick_skips_archive_sweep_when_disabled(db, journal):
    from app.reconciler import Reconciler
    from tests.test_chain import _nosleep
    _tick_routes([_archive(".7z", id=71)])
    deleted = respx.delete(f"{A}/api/v3/queue/71").mock(return_value=httpx.Response(200))
    rec = Reconciler(make_registry(), db, OperationStore(db), clock=lambda: NOW,
                     sleep=_nosleep, archive_cleanup=False)

    await rec.tick()

    assert not deleted.called


@respx.mock
async def test_stuck_import_warns_even_when_first_seen_late(db, journal):
    # Ticks run tick-duration + interval apart (77min + 60min live), so a
    # crossing can fall between sweeps. It must still warn — once per download.
    ops = OperationStore(db)
    reg = make_registry()
    stuck = {"statusMessages": [{"title": "x", "messages": ["Episode file already imported"]}]}
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    queue = respx.get(f"{A}/api/v3/queue")
    queue.mock(return_value=_queue([_rec(id=62, downloadId="LATE",
                                         added=(NOW - timedelta(hours=27)).isoformat(), **stuck)]))

    await sweep_archives(reg, db, ops, cap=25, now=NOW)
    await journal.flush()
    await sweep_archives(reg, db, ops, cap=25, now=NOW + timedelta(hours=1))

    assert len(await _events(journal, db, kinds.SWEEP_IMPORT_STUCK)) == 1


def test_archive_state_match_is_case_insensitive():
    # M-4: _queue_state lowercases trackedDownloadState; detection must agree.
    rec = _archive(".7z", trackedDownloadState="ImportPending")
    assert _is_unimportable_archive(rec, now=NOW, grace_hours=2) is True


@respx.mock
async def test_season_pack_is_removed_once_and_all_its_episodes_researched(db, journal):
    # M-5: a season pack is one download with a queue record per episode. One
    # DELETE removes it; repeating it per record only logs errored operations.
    ops = OperationStore(db)
    reg = make_registry()
    pack = [_archive(".rar", id=80 + e, downloadId="PACK",
                     episode={"id": 900 + e, "seasonNumber": 1, "episodeNumber": e})
            for e in (1, 2, 3)]
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue(pack))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    deletes = respx.route(method="DELETE", url__startswith=f"{A}/api/v3/queue/").mock(
        return_value=httpx.Response(200))
    cmd = respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))

    assert await sweep_archives(reg, db, ops, cap=25, now=NOW) == 1

    assert deletes.call_count == 1
    assert sorted(json.loads(cmd.calls.last.request.content)["episodeIds"]) == [901, 902, 903]
    assert [o["kind"] for o in await ops.recent()] == ["archive-cleanup"]
