"""Autonomous stalled-torrent sweep: remove torrents stuck at 0% past the threshold."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import respx

from app.db import Database
from app.services.liveness import TorrentLiveness
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
async def test_sweep_records_tvdb_id_not_sonarr_series_id(tmp_path):
    # A queue record's seriesId is the per-instance Sonarr id. Operations are
    # keyed on tvdb so they join to a series across instances.
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue([_rec(id=11, seriesId=5)]))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    respx.get(f"{A}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "tvdbId": 99, "title": "Show"}]))
    respx.delete(f"{A}/api/v3/queue/11").mock(return_value=httpx.Response(200))

    assert await sweep_stalled(reg, db, ops, stalled_days=3, cap=25, now=NOW) == 1
    assert (await ops.recent())[0]["tvdbId"] == 99


@respx.mock
async def test_sweep_still_removes_when_series_lookup_fails(tmp_path):
    # The tvdb id is bookkeeping; failing to resolve it must not block cleanup.
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue([_rec(id=11, seriesId=5)]))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    respx.get(f"{A}/api/v3/series").mock(return_value=httpx.Response(500))
    del_route = respx.delete(f"{A}/api/v3/queue/11").mock(return_value=httpx.Response(200))

    assert await sweep_stalled(reg, db, ops, stalled_days=3, cap=25, now=NOW) == 1
    assert del_route.called
    assert (await ops.recent())[0]["tvdbId"] is None


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


# --- liveness-aware fast kill -------------------------------------------------

def _live(hash_, *, seeders=0, metadata=True, peers=0, rate=0, pct=0.0):
    return TorrentLiveness(hash=hash_.lower(), has_metadata=metadata,
                           max_seeders=seeders, peers_connected=peers,
                           rate_download=rate, percent_done=pct)


@respx.mock
async def test_dead_torrent_is_removed_in_hours_not_days(tmp_path):
    """0 seeders on every tracker: waiting the full stalledDays helps nobody."""
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    rec = _rec(id=11, downloadId="DEAD", added=(NOW - timedelta(hours=7)).isoformat())
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue([rec]))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    del_route = respx.delete(f"{A}/api/v3/queue/11").mock(return_value=httpx.Response(200))

    removed = await sweep_stalled(reg, db, ops, stalled_days=1, cap=25, now=NOW,
                                  dead_hours=6, liveness={"dead": _live("dead")})

    assert removed == 1 and del_route.called


@respx.mock
async def test_dead_torrent_younger_than_dead_hours_is_left(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    rec = _rec(id=11, downloadId="DEAD", added=(NOW - timedelta(hours=3)).isoformat())
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue([rec]))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    del_route = respx.delete(f"{A}/api/v3/queue/11").mock(return_value=httpx.Response(200))

    removed = await sweep_stalled(reg, db, ops, stalled_days=1, cap=25, now=NOW,
                                  dead_hours=6, liveness={"dead": _live("dead")})

    assert removed == 0 and not del_route.called


@respx.mock
async def test_live_but_idle_torrent_still_waits_the_full_threshold(tmp_path):
    """A healthy swarm we simply haven't connected to yet keeps the slow clock."""
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    rec = _rec(id=11, downloadId="LIVE", added=(NOW - timedelta(hours=7)).isoformat())
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue([rec]))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    del_route = respx.delete(f"{A}/api/v3/queue/11").mock(return_value=httpx.Response(200))

    removed = await sweep_stalled(reg, db, ops, stalled_days=1, cap=25, now=NOW,
                                  dead_hours=6, liveness={"live": _live("live", seeders=30)})

    assert removed == 0 and not del_route.called


@respx.mock
async def test_near_complete_download_keeps_the_long_grace(tmp_path):
    """Discarding 98% of a download is the expensive mistake, so it outranks
    both the lowered stalledDays and the dead verdict."""
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    rec = _rec(id=41, size=1000, sizeleft=20, downloadId="ALMOST",
               added=(NOW - timedelta(days=30)).isoformat())
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue([rec]))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    del_route = respx.delete(f"{A}/api/v3/queue/41").mock(return_value=httpx.Response(200))
    live = {"almost": _live("almost", pct=0.98)}

    await sweep_stalled(reg, db, ops, stalled_days=1, cap=25, now=NOW,
                        dead_hours=6, liveness=live)
    # Frozen for two days — past the lowered stalledDays, inside the 3-day grace.
    removed = await sweep_stalled(reg, db, ops, stalled_days=1, cap=25,
                                  now=NOW + timedelta(days=2), dead_hours=6, liveness=live)

    assert removed == 0 and not del_route.called


@respx.mock
async def test_no_liveness_reproduces_the_age_only_behaviour(tmp_path):
    """Transmission unreachable: the sweep degrades, it does not change verdicts."""
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    records = [_rec(id=11, downloadId="A1", added=(NOW - timedelta(hours=7)).isoformat()),
               _rec(id=12, downloadId="A2", added=(NOW - timedelta(days=10)).isoformat())]
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue(records))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    respx.delete(f"{A}/api/v3/queue/11").mock(return_value=httpx.Response(200))
    respx.delete(f"{A}/api/v3/queue/12").mock(return_value=httpx.Response(200))

    removed = await sweep_stalled(reg, db, ops, stalled_days=1, cap=25, now=NOW,
                                  dead_hours=6, liveness={})

    assert removed == 1  # only the 10-day-old one; 7h is inside stalled_days=1


# --- replace, don't just remove ----------------------------------------------

@respx.mock
async def test_removal_regrabs_the_best_seeded_replacement(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    rec = _rec(id=11, downloadId="DEAD", episodeId=77,
               added=(NOW - timedelta(hours=7)).isoformat())
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue([rec]))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    del_route = respx.delete(f"{A}/api/v3/queue/11").mock(return_value=httpx.Response(200))
    search = respx.get(f"{A}/api/v3/release").mock(return_value=httpx.Response(200, json=[
        {"guid": "g1", "indexerId": 1, "title": "weak", "protocol": "torrent",
         "seeders": 6, "rejected": False},
        {"guid": "g2", "indexerId": 1, "title": "strong", "protocol": "torrent",
         "seeders": 31, "rejected": False},
    ]))
    grab = respx.post(f"{A}/api/v3/release").mock(return_value=httpx.Response(200, json={}))

    removed = await sweep_stalled(reg, db, ops, stalled_days=1, cap=25, now=NOW,
                                  dead_hours=6, liveness={"dead": _live("dead")},
                                  min_seeders=5, regrab_cap=5)

    assert removed == 1
    # Sonarr's own re-search is suppressed, because we are doing it ourselves.
    assert del_route.calls.last.request.url.params["skipRedownload"] == "true"
    assert search.calls.last.request.url.params["episodeId"] == "77"
    assert json.loads(grab.calls.last.request.content)["guid"] == "g2"


@respx.mock
async def test_season_pack_leaves_the_redownload_to_sonarr(tmp_path):
    """One torrent covering many episodes would mean one search per episode."""
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    recs = [_rec(id=11, downloadId="PACK", episodeId=1,
                 added=(NOW - timedelta(hours=7)).isoformat()),
            _rec(id=12, downloadId="PACK", episodeId=2,
                 added=(NOW - timedelta(hours=7)).isoformat())]
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue(recs))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    del_route = respx.delete(f"{A}/api/v3/queue/11").mock(return_value=httpx.Response(200))
    search = respx.get(f"{A}/api/v3/release").mock(return_value=httpx.Response(200, json=[]))

    removed = await sweep_stalled(reg, db, ops, stalled_days=1, cap=25, now=NOW,
                                  dead_hours=6, liveness={"pack": _live("pack")},
                                  min_seeders=5, regrab_cap=5)

    assert removed == 1
    assert del_route.calls.last.request.url.params["skipRedownload"] == "false"
    assert not search.called


@respx.mock
async def test_regrab_cap_bounds_the_interactive_searches(tmp_path):
    """An interactive search takes 55-95s and Prowlarr 429s under bursts, so the
    re-grab budget is far smaller than the removal cap."""
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    recs = [_rec(id=10 + n, downloadId=f"D{n}", episodeId=100 + n,
                 added=(NOW - timedelta(hours=7)).isoformat()) for n in range(4)]
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue(recs))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    for n in range(4):
        respx.delete(f"{A}/api/v3/queue/{10 + n}").mock(return_value=httpx.Response(200))
    search = respx.get(f"{A}/api/v3/release").mock(return_value=httpx.Response(200, json=[]))
    liveness = {f"d{n}": _live(f"d{n}") for n in range(4)}

    removed = await sweep_stalled(reg, db, ops, stalled_days=1, cap=25, now=NOW,
                                  dead_hours=6, liveness=liveness,
                                  min_seeders=5, regrab_cap=2)

    assert removed == 4
    assert len(search.calls) == 2


@respx.mock
async def test_removal_still_succeeds_when_the_regrab_fails(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    rec = _rec(id=11, downloadId="DEAD", episodeId=77,
               added=(NOW - timedelta(hours=7)).isoformat())
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue([rec]))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    respx.delete(f"{A}/api/v3/queue/11").mock(return_value=httpx.Response(200))
    respx.get(f"{A}/api/v3/release").mock(return_value=httpx.Response(500))

    removed = await sweep_stalled(reg, db, ops, stalled_days=1, cap=25, now=NOW,
                                  dead_hours=6, liveness={"dead": _live("dead")},
                                  min_seeders=5, regrab_cap=5)

    assert removed == 1


@respx.mock
async def test_removal_message_says_who_is_replacing_it(tmp_path):
    """skipRedownload=true means Sonarr is NOT re-searching — don't claim it is."""
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    rec = _rec(id=11, downloadId="DEAD", episodeId=77,
               added=(NOW - timedelta(hours=7)).isoformat())
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue([rec]))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    respx.delete(f"{A}/api/v3/queue/11").mock(return_value=httpx.Response(200))
    respx.get(f"{A}/api/v3/release").mock(return_value=httpx.Response(200, json=[]))

    await sweep_stalled(reg, db, ops, stalled_days=1, cap=25, now=NOW,
                        dead_hours=6, liveness={"dead": _live("dead")},
                        min_seeders=5, regrab_cap=5)

    remove_step = (await ops.recent())[0]["steps"][0]
    assert "Sonarr re-searching" not in remove_step["message"]
    assert "replacing" in remove_step["message"]


@respx.mock
async def test_removal_message_credits_sonarr_when_it_does_redownload(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    rec = _rec(id=11, downloadId="DEAD", added=(NOW - timedelta(hours=7)).isoformat())
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue([rec]))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    respx.delete(f"{A}/api/v3/queue/11").mock(return_value=httpx.Response(200))

    await sweep_stalled(reg, db, ops, stalled_days=1, cap=25, now=NOW,
                        dead_hours=6, liveness={"dead": _live("dead")})

    assert "Sonarr re-searching" in (await ops.recent())[0]["steps"][0]["message"]
