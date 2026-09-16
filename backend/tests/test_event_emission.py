"""Poller, sweeps and availability checks journal what they change."""
from datetime import datetime, timedelta, timezone

import httpx
import respx

from app.obs import context, kinds
from app.obs.stats import TickStats
from app.services import placement, poller
from app.store import events as events_store
from app.store.operations import OperationStore
from tests import test_dangerous as td
from tests import test_placement as tpl
from tests import test_poller as tp
from tests import test_search_stall as tss
from tests import test_stalled as ts
from tests.test_chain import A, B, make_registry


async def _events(journal, db, kind):
    await journal.flush()
    return list(reversed(await events_store.query(db, kind=kind)))


def test_tick_stats_counts_and_zero_spikes():
    s = TickStats()
    for _ in range(9):
        s.availability("4k", "zero")
    s.availability("4k", "qualifies")
    for _ in range(3):
        s.availability("1080p", "zero")
    s.availability("4k", "cached")
    assert s.zero_spikes() == {"4k": (9, 10)}   # 1080p has too few live checks
    assert s.to_phases()["availability"] == {
        "live": 13, "cached": 1, "liveBy": {"4k": 10, "1080p": 3}, "zero": {"4k": 9, "1080p": 3},
        # Checks discarded because the instance's indexers were down — kept out of
        # live/zero so an outage we handled can't skew the zero-spike ratio.
        "degraded": {},
    }


@respx.mock
async def test_poll_transition_is_journaled(db, journal):
    reg = make_registry()
    await tp._seed(db, (2, 1))
    tp._mock_4k_series()
    respx.get(f"{B}/api/v3/queue").mock(return_value=httpx.Response(200, json={"records": []}))
    respx.get(f"{B}/api/v3/history").mock(return_value=httpx.Response(200, json={"records": [
        {"eventType": "downloadFailed", "seriesId": 10, "downloadId": "DX",
         "episode": {"seasonNumber": 2, "episodeNumber": 1}},
    ]}))

    await poller.poll_instance(reg, db, reg.get("4k"))

    [ev] = await _events(journal, db, kinds.POLL_TRANSITION)
    assert (ev["tvdbId"], ev["season"], ev["episode"], ev["instanceId"]) == (99, 2, 1, "4k")
    assert ev["level"] == "warn" and ev["source"] == "poller"
    assert ev["data"] == {"from": "wanted", "to": "failed", "downloadId": None}


@respx.mock
async def test_stalled_removal_is_journaled(db, journal):
    ops = OperationStore(db)
    respx.get(f"{A}/api/v3/queue").mock(return_value=ts._queue([ts._rec(id=11, seriesId=5)]))
    respx.get(f"{B}/api/v3/queue").mock(return_value=ts._queue([]))
    respx.get(f"{A}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "tvdbId": 99}]))
    respx.delete(f"{A}/api/v3/queue/11").mock(return_value=httpx.Response(200))

    await poller.sweep_stalled(make_registry(), db, ops, stalled_days=3, cap=25, now=ts.NOW)

    [ev] = await _events(journal, db, kinds.SWEEP_STALLED_REMOVED)
    op = (await ops.recent())[0]
    assert (ev["tvdbId"], ev["instanceId"], ev["operationId"]) == (99, "1080p", op["id"])
    assert ev["data"]["ageDays"] == 10 and ev["data"]["queueId"] == 11


@respx.mock
async def test_dangerous_removal_is_journaled_as_warning(db, journal):
    ops = OperationStore(db)
    respx.get(f"{A}/api/v3/queue").mock(return_value=td._queue(
        [td._rec(id=31, seriesId=5, title="Fake.S01E01.1080p.WEB-GRP.exe")]))
    respx.get(f"{B}/api/v3/queue").mock(return_value=td._queue([]))
    respx.get(f"{A}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "tvdbId": 99}]))
    respx.delete(f"{A}/api/v3/queue/31").mock(return_value=httpx.Response(200))
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))

    await poller.sweep_dangerous(make_registry(), db, ops, cap=25, now=td.NOW)

    [ev] = await _events(journal, db, kinds.SWEEP_DANGEROUS_REMOVED)
    assert ev["level"] == "warn" and ev["tvdbId"] == 99 and ev["instanceId"] == "1080p"


async def test_search_stall_revert_is_journaled(db, journal):
    await tss._seed(db, 777, 1, 2, state="searching",
                    last_search_at=(tss.NOW - timedelta(hours=12)).isoformat())

    await poller.sweep_search_stalls(db, stall_hours=6, cap=25, now=tss.NOW)

    [ev] = await _events(journal, db, kinds.SWEEP_SEARCH_STALL_REVERTED)
    assert (ev["tvdbId"], ev["season"], ev["episode"]) == (777, 1, 2)


@respx.mock
async def test_availability_change_is_journaled_only_on_flip(db, journal):
    now = datetime(2026, 9, 13, tzinfo=timezone.utc)
    tpl._mock_gap_with_airdate("2026-07-15T04:00:00Z")
    respx.get(f"{B}/api/v3/release").mock(
        return_value=httpx.Response(200, json=[{"rejected": False}]))
    # A 7h-old "0 releases" verdict is stale: the check runs live and flips it.
    await tpl._seed_empty_cache(db, checked_at=(now - timedelta(hours=7)).isoformat())

    stats = TickStats()
    with context.bind(tick_stats=stats):
        await placement.refresh_availability(
            make_registry(), db, tvdb_id=tpl.TVDB, instance_id="4k", now=now)

    [ev] = await _events(journal, db, kinds.AVAILABILITY_CHANGED)
    assert (ev["tvdbId"], ev["season"], ev["episode"], ev["instanceId"]) == (tpl.TVDB, 1, 2, "4k")
    assert ev["data"]["before"] == {"qualifies": False, "totalReleases": 0}
    assert ev["data"]["after"]["qualifies"] is True
    assert stats.live == {"4k": 1} and stats.zero == {}

    # Re-checked later with the same verdict: nothing new to journal.
    await placement.refresh_availability(
        make_registry(), db, tvdb_id=tpl.TVDB, instance_id="4k", now=now + timedelta(hours=7))
    assert len(await _events(journal, db, kinds.AVAILABILITY_CHANGED)) == 1
