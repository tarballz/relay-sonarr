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

    removed = await sweep_archives(reg, db, ops, cap=25, now=NOW, interval_s=3600)

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

    # Crossing the 24h threshold on this tick -> one warning.
    queue.mock(return_value=_queue([_rec(id=61, added=(NOW - timedelta(hours=24.2)).isoformat(),
                                         **stuck)]))
    assert await sweep_archives(reg, db, ops, cap=25, now=NOW, interval_s=3600) == 0
    assert len(await _events(journal, db, kinds.SWEEP_IMPORT_STUCK)) == 1

    # Crossed on an earlier tick -> silent (journal changes, not repetitions).
    queue.mock(return_value=_queue([_rec(id=61, added=(NOW - timedelta(hours=26)).isoformat(),
                                         **stuck)]))
    await sweep_archives(reg, db, ops, cap=25, now=NOW, interval_s=3600)
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
