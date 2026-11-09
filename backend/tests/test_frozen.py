"""Frozen-torrent nudges: re-announce + restart before giving up on a download.

A torrent can hold peers and still transfer nothing for hours — the live
S13E06 case sat at 99.9% (one 16 KiB block short) with two connected peers and
zero bytes moving. `sweep_stalled` deliberately skips any torrent with a peer,
so that class was invisible. This sweep observes sizeleft for every incomplete
torrent and nudges the client once the clock says nothing has moved.
"""
import json
from datetime import datetime, timedelta, timezone

import httpx
import respx

from app.db import Database
from app.download.transmission import TransmissionClient
from app.obs import kinds
from app.services.liveness import TorrentLiveness
from app.services.poller import sweep_frozen, sweep_stalled
from app.store import progress
from app.store.operations import OperationStore
from tests.test_chain import A, B, make_registry

NOW = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)
RPC = "http://10.0.0.3:9091/transmission/rpc"


def _rec(**over):
    base = {"id": 1, "protocol": "torrent", "status": "downloading",
            "size": 1000, "sizeleft": 540,
            "added": (NOW - timedelta(days=1)).isoformat(),
            "seriesId": 5, "title": "Show.S01E01", "downloadId": "ABCD"}
    base.update(over)
    return base


def _queue(records):
    return httpx.Response(200, json={"page": 1, "pageSize": 200,
                                     "totalRecords": len(records), "records": records})


def _live(hash_, **over):
    base = {"hash": hash_.lower(), "has_metadata": True, "max_seeders": 20,
            "peers_connected": 2, "rate_download": 0, "percent_done": 0.46}
    base.update(over)
    return TorrentLiveness(**base)


def _registry_with_transmission():
    reg = make_registry()
    reg._downloads = TransmissionClient(base_url=RPC)
    return reg


def _rpc_ok():
    return httpx.Response(200, json={"result": "success", "arguments": {}})


def _mock_queues(a_records, b_records=None):
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue(a_records))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue(b_records or []))


def _methods(route):
    return [json.loads(c.request.content)["method"] for c in route.calls]


@respx.mock
async def test_nudges_a_torrent_that_has_not_moved_past_the_threshold(db, journal):
    reg = _registry_with_transmission()
    # It has been sitting at 540 bytes left for 40 minutes.
    await progress.observe(db, download_id="abcd", sizeleft=540,
                           now=NOW - timedelta(minutes=40))
    _mock_queues([_rec()])
    rpc = respx.post(RPC).mock(return_value=_rpc_ok())

    nudged = await sweep_frozen(reg, db, now=NOW,
                                liveness={"abcd": _live("abcd")},
                                nudge_after_minutes=30, cooldown_minutes=60,
                                max_nudges=2, cap=25)

    assert nudged == 1
    assert _methods(rpc) == ["torrent-reannounce", "torrent-stop", "torrent-start"]
    assert all(json.loads(c.request.content)["arguments"]["ids"] == ["abcd"]
               for c in rpc.calls)
    state = await progress.nudge_state(db, "abcd")
    assert state["nudge_count"] == 1
    assert state["last_nudge_at"] == NOW.isoformat()
    event = journal.pending()[0]
    assert event["kind"] == kinds.SWEEP_FROZEN_NUDGED
    assert "40 minutes" in event["message"] or "no progress" in event["message"]


@respx.mock
async def test_leaves_a_torrent_alone_until_the_freeze_threshold(db, journal):
    reg = _registry_with_transmission()
    await progress.observe(db, download_id="abcd", sizeleft=540,
                           now=NOW - timedelta(minutes=10))
    _mock_queues([_rec()])
    rpc = respx.post(RPC).mock(return_value=_rpc_ok())

    nudged = await sweep_frozen(reg, db, now=NOW,
                                liveness={"abcd": _live("abcd")},
                                nudge_after_minutes=30, cooldown_minutes=60,
                                max_nudges=2, cap=25)

    assert nudged == 0
    assert not rpc.called
    assert (await progress.nudge_state(db, "abcd"))["nudge_count"] == 0


@respx.mock
async def test_respects_the_cooldown_between_nudges(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    reg = _registry_with_transmission()
    await progress.observe(db, download_id="abcd", sizeleft=540,
                           now=NOW - timedelta(minutes=40))
    await progress.note_nudge(db, "abcd", NOW - timedelta(minutes=10))
    _mock_queues([_rec()])
    rpc = respx.post(RPC).mock(return_value=_rpc_ok())

    nudged = await sweep_frozen(reg, db, now=NOW,
                                liveness={"abcd": _live("abcd")},
                                nudge_after_minutes=30, cooldown_minutes=60,
                                max_nudges=2, cap=25)

    assert nudged == 0
    assert not rpc.called


@respx.mock
async def test_stops_after_the_nudge_budget_is_spent(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    reg = _registry_with_transmission()
    await progress.observe(db, download_id="abcd", sizeleft=540,
                           now=NOW - timedelta(hours=4))
    await progress.note_nudge(db, "abcd", NOW - timedelta(hours=3))
    await progress.note_nudge(db, "abcd", NOW - timedelta(hours=1))
    _mock_queues([_rec()])
    rpc = respx.post(RPC).mock(return_value=_rpc_ok())

    nudged = await sweep_frozen(reg, db, now=NOW,
                                liveness={"abcd": _live("abcd")},
                                nudge_after_minutes=30, cooldown_minutes=60,
                                max_nudges=2, cap=25)

    assert nudged == 0
    assert not rpc.called


@respx.mock
async def test_a_moving_torrent_resets_the_freeze_clock_and_is_not_nudged(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    reg = _registry_with_transmission()
    await progress.observe(db, download_id="abcd", sizeleft=540,
                           now=NOW - timedelta(hours=2))
    # It has advanced since the last observation.
    _mock_queues([_rec(sizeleft=500)])
    rpc = respx.post(RPC).mock(return_value=_rpc_ok())

    nudged = await sweep_frozen(reg, db, now=NOW,
                                liveness={"abcd": _live("abcd", rate_download=4096)},
                                nudge_after_minutes=30, cooldown_minutes=60,
                                max_nudges=2, cap=25)

    assert nudged == 0
    assert not rpc.called
    row = await db.query_one(
        "SELECT sizeleft, unchanged_since FROM download_progress WHERE download_id=?",
        ("abcd",))
    assert row["sizeleft"] == 500
    assert row["unchanged_since"] == NOW.isoformat()


@respx.mock
async def test_dead_torrents_are_left_to_the_removal_sweep(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    reg = _registry_with_transmission()
    await progress.observe(db, download_id="abcd", sizeleft=540,
                           now=NOW - timedelta(hours=2))
    _mock_queues([_rec()])
    rpc = respx.post(RPC).mock(return_value=_rpc_ok())

    nudged = await sweep_frozen(reg, db, now=NOW,
                                liveness={"abcd": _live("abcd", max_seeders=0,
                                                        peers_connected=0)},
                                nudge_after_minutes=30, cooldown_minutes=60,
                                max_nudges=2, cap=25)

    assert nudged == 0
    assert not rpc.called


@respx.mock
async def test_paused_user_torrents_are_never_nudged(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    reg = _registry_with_transmission()
    await progress.observe(db, download_id="abcd", sizeleft=540,
                           now=NOW - timedelta(hours=2))
    _mock_queues([_rec(status="paused")])
    rpc = respx.post(RPC).mock(return_value=_rpc_ok())

    assert await sweep_frozen(reg, db, now=NOW,
                              liveness={"abcd": _live("abcd")},
                              nudge_after_minutes=30, cooldown_minutes=60,
                              max_nudges=2, cap=25) == 0
    assert not rpc.called


@respx.mock
async def test_no_download_client_configured_is_a_no_op(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    reg = make_registry()  # no download_client
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue([_rec()]))

    assert await sweep_frozen(reg, db, now=NOW, liveness={"abcd": _live("abcd")},
                              nudge_after_minutes=30, cooldown_minutes=60,
                              max_nudges=2, cap=25) == 0


async def test_progress_moving_resets_the_nudge_budget(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    await progress.observe(db, download_id="h", sizeleft=100, now=NOW)
    await progress.note_nudge(db, "h", NOW)
    await progress.observe(db, download_id="h", sizeleft=90,
                           now=NOW + timedelta(minutes=1))
    state = await progress.nudge_state(db, "h")
    assert state["nudge_count"] == 0
    assert state["last_nudge_at"] is None


# --- the removal clock shortens only after nudges are exhausted ---------------

@respx.mock
async def test_near_complete_torrent_is_removed_after_nudges_are_exhausted(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    reg = make_registry()
    ops = OperationStore(db)
    # Frozen at 98% for 7 hours, with the full nudge budget already spent.
    await progress.observe(db, download_id="almost", sizeleft=20,
                           now=NOW - timedelta(hours=7))
    await progress.note_nudge(db, "almost", NOW - timedelta(hours=5))
    await progress.note_nudge(db, "almost", NOW - timedelta(hours=3))
    rec = _rec(id=41, size=1000, sizeleft=20, downloadId="ALMOST",
               added=(NOW - timedelta(days=30)).isoformat())
    _mock_queues([rec])
    delete = respx.delete(f"{A}/api/v3/queue/41").mock(return_value=httpx.Response(200))

    removed = await sweep_stalled(
        reg, db, ops, stalled_days=1, cap=25, now=NOW, dead_hours=6,
        liveness={"almost": _live("almost", max_seeders=30, peers_connected=2,
                                  rate_download=0, percent_done=0.98)},
        near_complete_pct=95, nudge_cap=2, frozen_after_nudge_hours=6,
    )

    assert removed == 1
    assert delete.called


@respx.mock
async def test_near_complete_torrent_keeps_the_long_grace_until_nudges_are_spent(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    reg = make_registry()
    ops = OperationStore(db)
    await progress.observe(db, download_id="almost", sizeleft=20,
                           now=NOW - timedelta(hours=7))
    rec = _rec(id=41, size=1000, sizeleft=20, downloadId="ALMOST",
               added=(NOW - timedelta(days=30)).isoformat())
    _mock_queues([rec])
    delete = respx.delete(f"{A}/api/v3/queue/41").mock(return_value=httpx.Response(200))

    removed = await sweep_stalled(
        reg, db, ops, stalled_days=1, cap=25, now=NOW, dead_hours=6,
        liveness={"almost": _live("almost", max_seeders=30, peers_connected=2,
                                  rate_download=0, percent_done=0.98)},
        near_complete_pct=95, nudge_cap=2, frozen_after_nudge_hours=6,
    )

    assert removed == 0
    assert not delete.called


@respx.mock
async def test_a_fresh_nudge_gets_time_to_work_before_removal(tmp_path):
    """The second nudge often goes out in the same tick as the removal sweep.

    Removing right then would spend the nudge and give it zero seconds to do
    anything, so the budget only counts once the cooldown has elapsed.
    """
    db = Database(str(tmp_path / "relay.db"))
    reg = make_registry()
    ops = OperationStore(db)
    await progress.observe(db, download_id="almost", sizeleft=20,
                           now=NOW - timedelta(hours=7))
    await progress.note_nudge(db, "almost", NOW - timedelta(hours=5))
    await progress.note_nudge(db, "almost", NOW)  # just sent this tick
    rec = _rec(id=41, size=1000, sizeleft=20, downloadId="ALMOST",
               added=(NOW - timedelta(days=30)).isoformat())
    _mock_queues([rec])
    delete = respx.delete(f"{A}/api/v3/queue/41").mock(return_value=httpx.Response(200))

    removed = await sweep_stalled(
        reg, db, ops, stalled_days=1, cap=25, now=NOW, dead_hours=6,
        liveness={"almost": _live("almost", max_seeders=30, peers_connected=2,
                                  rate_download=0, percent_done=0.98)},
        near_complete_pct=95, nudge_cap=2, frozen_after_nudge_hours=6,
        nudge_cooldown_minutes=60,
    )

    assert removed == 0
    assert not delete.called


@respx.mock
async def test_peer_connected_torrent_with_spent_nudges_can_be_removed(tmp_path):
    """The old peer guard skipped this forever; two failed nudges prove it is stuck."""
    db = Database(str(tmp_path / "relay.db"))
    reg = make_registry()
    ops = OperationStore(db)
    await progress.observe(db, download_id="stuck", sizeleft=500,
                           now=NOW - timedelta(days=2))
    await progress.note_nudge(db, "stuck", NOW - timedelta(days=1, hours=12))
    await progress.note_nudge(db, "stuck", NOW - timedelta(days=1))
    rec = _rec(id=51, size=1000, sizeleft=500, downloadId="STUCK",
               added=(NOW - timedelta(days=30)).isoformat())
    _mock_queues([rec])
    delete = respx.delete(f"{A}/api/v3/queue/51").mock(return_value=httpx.Response(200))

    removed = await sweep_stalled(
        reg, db, ops, stalled_days=1, cap=25, now=NOW, dead_hours=6,
        liveness={"stuck": _live("stuck", max_seeders=30, peers_connected=3,
                                 rate_download=0, percent_done=0.5)},
        near_complete_pct=95, nudge_cap=2, frozen_after_nudge_hours=6,
    )

    assert removed == 1
    assert delete.called


# --- reconciler wiring -------------------------------------------------------

async def _anoop():
    return None


async def test_tick_runs_the_frozen_nudge_sweep_before_removals(tmp_path, monkeypatch):
    from app.reconciler import Reconciler
    from tests.test_chain import _nosleep

    db = Database(str(tmp_path / "relay.db"))
    reg = make_registry()
    rec = Reconciler(reg, db, OperationStore(db), clock=lambda: NOW, sleep=_nosleep,
                     wait_attempts=2, wait_delay=0, archive_cleanup=False,
                     dangerous_cleanup=False, search_stall_cleanup=False)
    seen = {}

    async def fake_poll_all(*a, **k):
        return {"polled": []}

    async def fake_nudge(registry, db_, *, now, liveness, nudge_after_minutes,
                         cooldown_minutes, max_nudges, cap):
        seen.update(now=now, liveness=liveness, nudge_after_minutes=nudge_after_minutes,
                    cooldown_minutes=cooldown_minutes, max_nudges=max_nudges, cap=cap)
        return 0

    async def fake_stalled(*a, **k):
        return 0

    monkeypatch.setattr("app.reconciler.poller.poll_all", fake_poll_all)
    monkeypatch.setattr("app.reconciler.poller.sweep_frozen", fake_nudge)
    monkeypatch.setattr("app.reconciler.poller.sweep_stalled", fake_stalled)
    monkeypatch.setattr(rec, "_ensure_intents", lambda: _anoop())

    await rec.tick()

    assert seen["now"] == NOW
    assert seen["liveness"] == {}
    assert seen["nudge_after_minutes"] == 30
    assert seen["cooldown_minutes"] == 60
    assert seen["max_nudges"] == 2
    assert seen["cap"] == rec._stalled_cap


async def test_tick_skips_the_frozen_nudge_sweep_when_stalled_cleanup_is_off(
        tmp_path, monkeypatch):
    from app.reconciler import Reconciler
    from tests.test_chain import _nosleep

    db = Database(str(tmp_path / "relay.db"))
    reg = make_registry()
    rec = Reconciler(reg, db, OperationStore(db), clock=lambda: NOW, sleep=_nosleep,
                     wait_attempts=2, wait_delay=0, archive_cleanup=False,
                     dangerous_cleanup=False, search_stall_cleanup=False,
                     stalled_cleanup=False)
    called = {"nudge": False}

    async def fake_poll_all(*a, **k):
        return {"polled": []}

    async def fake_nudge(*a, **k):
        called["nudge"] = True
        return 0

    async def fake_stalled(*a, **k):
        return 0

    monkeypatch.setattr("app.reconciler.poller.poll_all", fake_poll_all)
    monkeypatch.setattr("app.reconciler.poller.sweep_frozen", fake_nudge)
    monkeypatch.setattr("app.reconciler.poller.sweep_stalled", fake_stalled)
    monkeypatch.setattr(rec, "_ensure_intents", lambda: _anoop())

    await rec.tick()

    assert called["nudge"] is False
