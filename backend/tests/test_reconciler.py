"""Reconciliation loop: per-episode autonomous search + fallback split."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import respx

from app.db import Database
from app.policy import SeriesPolicy
from app.reconciler import Reconciler
from app.store import intents as intent_store
from app.store import placements as place_store
from app.store import settings as settings_store
from app.store.operations import OperationStore
from tests.test_chain import A, B, _nosleep, make_registry, mock_1080p_profiles

TVDB = 99
NOW = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)


def _make(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    reg = make_registry()
    ops = OperationStore(db)
    rec = Reconciler(reg, db, ops, clock=lambda: NOW, sleep=_nosleep,
                     wait_attempts=2, wait_delay=0)
    return reg, db, ops, rec


def _mock_4k():
    # Series lives on 4K (B): S1E1 already have; S1E2 has no 4K release; S1E3 does.
    respx.get(f"{B}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 10, "tvdbId": TVDB, "title": "Mad Men"}])
    )
    respx.get(f"{B}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 1001, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": True},
        {"id": 1002, "seasonNumber": 1, "episodeNumber": 2, "monitored": True, "hasFile": False},
        {"id": 1003, "seasonNumber": 1, "episodeNumber": 3, "monitored": True, "hasFile": False},
    ]))
    respx.get(f"{B}/api/v3/release", params={"episodeId": "1002"}).mock(
        return_value=httpx.Response(200, json=[{"rejected": True, "rejections": ["not wanted in profile"]}])
    )
    respx.get(f"{B}/api/v3/release", params={"episodeId": "1003"}).mock(
        return_value=httpx.Response(200, json=[{"rejected": False}])
    )


@respx.mock
async def test_reconcile_searches_desired_and_fills_fallback(tmp_path):
    reg, db, ops, rec = _make(tmp_path)
    _mock_4k()
    b_cmd = respx.post(f"{B}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))
    b_mon = respx.put(f"{B}/api/v3/episode/monitor").mock(return_value=httpx.Response(200, json=[]))
    # Fallback tier 1080p (A): series not present yet → it gets added + searched.
    mock_1080p_profiles()
    respx.get(f"{A}/api/v3/series").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{A}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": TVDB, "title": "Mad Men"}])
    )
    respx.post(f"{A}/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 20}))
    a_cmd = respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 2}))
    respx.get(f"{A}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 2002, "seasonNumber": 1, "episodeNumber": 2, "monitored": True, "hasFile": False},
    ]))
    a_mon = respx.put(f"{A}/api/v3/episode/monitor").mock(return_value=httpx.Response(200, json=[]))

    result = await rec.reconcile_series({"tvdb_id": TVDB, "chain_key": "4k", "title": "Mad Men"})

    assert result["searchedOnDesired"] == 1   # S1E3 (4K has it)
    assert result["filledOn"] == "1080p"
    assert result["filled"] == 1              # S1E2 (no 4K → spill to 1080p)

    # 4K searched exactly the obtainable gap.
    b_cmds = [json.loads(c.request.content) for c in b_cmd.calls]
    assert {"name": "EpisodeSearch", "episodeIds": [1003]} in b_cmds
    # 1080p got the series added and the gap episode searched.
    a_cmds = [json.loads(c.request.content) for c in a_cmd.calls]
    assert {"name": "EpisodeSearch", "episodeIds": [2002]} in a_cmds
    # Origin (4K) unmonitors the spilled episode → disjoint split.
    b_mon_bodies = [json.loads(c.request.content) for c in b_mon.calls]
    assert {"episodeIds": [1002], "monitored": False} in b_mon_bodies

    # Placement reflects the lifecycle.
    e3 = await place_store.get(db, TVDB, 1, 3)
    e2 = await place_store.get(db, TVDB, 1, 2)
    e1 = await place_store.get(db, TVDB, 1, 1)
    assert e3["state"] == "searching"
    assert e2["state"] == "searching"
    assert e1["state"] == "imported"

    # A reconciler operation was recorded.
    op = (await ops.recent())[0]
    assert op["source"] == "reconciler" and op["kind"] == "reconcile"


@respx.mock
async def test_reconcile_min_seeders_spills_weak_episode(tmp_path):
    reg, db, ops, rec = _make(tmp_path)
    # Same shape as _mock_4k, but S1E3's only 4K release is a 1-seeder torrent —
    # with minSeeders=3 it must spill to 1080p alongside S1E2 instead of being
    # searched on the desired tier.
    respx.get(f"{B}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 10, "tvdbId": TVDB, "title": "Mad Men"}])
    )
    respx.get(f"{B}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 1001, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": True},
        {"id": 1002, "seasonNumber": 1, "episodeNumber": 2, "monitored": True, "hasFile": False},
        {"id": 1003, "seasonNumber": 1, "episodeNumber": 3, "monitored": True, "hasFile": False},
    ]))
    respx.get(f"{B}/api/v3/release", params={"episodeId": "1002"}).mock(
        return_value=httpx.Response(200, json=[{"rejected": True, "rejections": ["not wanted in profile"]}])
    )
    respx.get(f"{B}/api/v3/release", params={"episodeId": "1003"}).mock(
        return_value=httpx.Response(
            200, json=[{"rejected": False, "protocol": "torrent", "seeders": 1}]
        )
    )
    b_cmd = respx.post(f"{B}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))
    respx.put(f"{B}/api/v3/episode/monitor").mock(return_value=httpx.Response(200, json=[]))
    mock_1080p_profiles()
    respx.get(f"{A}/api/v3/series").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{A}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": TVDB, "title": "Mad Men"}])
    )
    respx.post(f"{A}/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 20}))
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 2}))
    respx.get(f"{A}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 2002, "seasonNumber": 1, "episodeNumber": 2, "monitored": True, "hasFile": False},
        {"id": 2003, "seasonNumber": 1, "episodeNumber": 3, "monitored": True, "hasFile": False},
    ]))
    respx.put(f"{A}/api/v3/episode/monitor").mock(return_value=httpx.Response(200, json=[]))

    await settings_store.set_defaults(db, {"minSeeders": 3})
    result = await rec.reconcile_series({"tvdb_id": TVDB, "chain_key": "4k", "title": "Mad Men"})

    assert result["searchedOnDesired"] == 0
    assert result["filled"] == 2
    # No EpisodeSearch fired on the 4K tier — its only candidate was too weak.
    b_cmds = [json.loads(c.request.content) for c in b_cmd.calls]
    assert not any(c.get("name") == "EpisodeSearch" for c in b_cmds)


@respx.mock
async def test_reconcile_allow_split_false_searches_desired_only(tmp_path):
    reg, db, ops, rec = _make(tmp_path)
    _mock_4k()
    b_cmd = respx.post(f"{B}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))
    respx.put(f"{B}/api/v3/episode/monitor").mock(return_value=httpx.Response(200, json=[]))
    # No 1080p (A) mocks at all — if a fill were attempted it would error.
    pol = SeriesPolicy(preferredTier="4k", fallbacks=[{"instanceId": "1080p"}], allowSplit=False)

    result = await rec.reconcile_series(
        {"tvdb_id": TVDB, "chain_key": "4k", "title": "Mad Men", "policy_json": pol.to_json()}
    )
    assert result["searchedOnDesired"] == 1  # S1E3 still searched on 4K
    assert result["filled"] == 0             # S1E2 NOT split (allowSplit False)
    b_cmds = [json.loads(c.request.content) for c in b_cmd.calls]
    assert {"name": "EpisodeSearch", "episodeIds": [1003]} in b_cmds


@respx.mock
async def test_reconcile_escalates_to_fallback_after_days(tmp_path):
    reg, db, ops, _ = _make(tmp_path)
    _mock_4k()
    respx.post(f"{B}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))
    respx.put(f"{B}/api/v3/episode/monitor").mock(return_value=httpx.Response(200, json=[]))
    # 1080p fallback mocks, only exercised once escalation kicks in.
    mock_1080p_profiles()
    respx.get(f"{A}/api/v3/series").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{A}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": TVDB, "title": "Mad Men"}])
    )
    respx.post(f"{A}/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 20}))
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 2}))
    respx.get(f"{A}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 2002, "seasonNumber": 1, "episodeNumber": 2, "monitored": True, "hasFile": False},
    ]))
    respx.put(f"{A}/api/v3/episode/monitor").mock(return_value=httpx.Response(200, json=[]))

    pol = SeriesPolicy(
        preferredTier="4k",
        fallbacks=[{"instanceId": "1080p", "afterDays": 14}],
        allowSplit=True,
    )
    intent = {"tvdb_id": TVDB, "chain_key": "4k", "title": "Mad Men", "policy_json": pol.to_json()}

    rec_now = Reconciler(reg, db, ops, clock=lambda: NOW, sleep=_nosleep, wait_attempts=2, wait_delay=0)
    r1 = await rec_now.reconcile_series(intent)
    assert r1["filled"] == 0  # S1E2 wanted 0 days < 14 → not yet escalated

    later = NOW + timedelta(days=15)
    rec_later = Reconciler(reg, db, ops, clock=lambda: later, sleep=_nosleep, wait_attempts=2, wait_delay=0)
    r2 = await rec_later.reconcile_series(intent)
    assert r2["filled"] == 1 and r2["filledOn"] == "1080p"  # 15 days ≥ 14 → spill to 1080p


@respx.mock
async def test_reconcile_is_noop_when_nothing_actionable(tmp_path):
    reg, db, ops, rec = _make(tmp_path)
    # Everything on disk already → no gaps, no actions.
    respx.get(f"{B}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 10, "tvdbId": TVDB, "title": "Mad Men"}])
    )
    respx.get(f"{B}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 1001, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": True},
    ]))
    result = await rec.reconcile_series({"tvdb_id": TVDB, "chain_key": "4k", "title": "Mad Men"})
    assert result == {"tvdbId": TVDB, "actions": 0}
    assert await ops.recent() == []  # no noise


async def test_run_once_records_success(tmp_path):
    """A successful guarded iteration sets timing/action fields and clears failures."""
    reg, db, ops, rec = _make(tmp_path)

    async def fake_tick():
        return {"reconciled": [{"searchedOnDesired": 2, "filled": 1}, {"actions": 0}]}

    rec.tick = fake_tick
    out = await rec._run_once()

    assert out is not None
    s = rec.status()
    assert s["totalTicks"] == 1
    assert s["lastTickActions"] == 3          # 2 searched + 1 filled
    assert s["consecutiveFailures"] == 0
    assert s["lastError"] is None
    assert s["lastTickFinishedAt"] == NOW.isoformat()
    assert s["lastTickDurationS"] == 0.0      # fixed clock


async def test_run_once_records_failure_without_propagating(tmp_path):
    """A tick that raises is swallowed but recorded — never kills the loop."""
    reg, db, ops, rec = _make(tmp_path)

    async def boom():
        raise ValueError("kaboom")

    rec.tick = boom
    out = await rec._run_once()  # must NOT raise

    assert out is None
    s = rec.status()
    assert s["consecutiveFailures"] == 1
    assert s["lastError"].startswith("ValueError")
    assert "kaboom" in s["lastError"]


def test_is_healthy_grace_staleness_and_disabled(tmp_path):
    reg, db, ops, rec = _make(tmp_path)  # clock fixed at NOW; started_at == NOW
    # Fresh (no tick yet) but within startup grace → healthy.
    assert rec.is_healthy(NOW) is True
    # Past interval*2 with no completed tick → stale → unhealthy.
    assert rec.is_healthy(NOW + timedelta(seconds=rec.interval * 2 + 1)) is False
    # Disabled reconcilers are always "healthy" (nothing is supposed to run).
    rec.enabled = False
    assert rec.is_healthy(NOW + timedelta(days=99)) is True


@respx.mock
async def test_tick_skips_paused_series(tmp_path):
    reg, db, ops, rec = _make(tmp_path)
    await intent_store.ensure(db, tvdb_id=TVDB, title="Mad Men", chain_key="4k", now="t")
    await intent_store.set_paused(db, TVDB, True, now="t")
    # tick polls + seeds intents across all instances.
    for base in (A, B):
        respx.get(f"{base}/api/v3/queue").mock(return_value=httpx.Response(200, json={"records": []}))
        respx.get(f"{base}/api/v3/history").mock(return_value=httpx.Response(200, json={"records": []}))
    respx.get(f"{A}/api/v3/series").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{B}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 10, "tvdbId": TVDB, "title": "Mad Men"}])
    )

    out = await rec.tick()
    assert out["reconciled"] == []  # paused → not acted on
    assert (await intent_store.get(db, TVDB))["paused"] == 1  # ensure() preserved pause


async def test_tick_runs_stalled_sweep_when_enabled(tmp_path, monkeypatch):
    reg, db, ops, rec = _make(tmp_path)
    rec.stalled_cleanup = True
    called = {}

    async def fake_poll_all(*a, **k):
        return {"polled": []}

    async def fake_sweep(registry, db_, ops_, *, stalled_days, cap, now):
        called["days"] = stalled_days
        called["cap"] = cap
        return 0

    monkeypatch.setattr("app.reconciler.poller.poll_all", fake_poll_all)
    monkeypatch.setattr("app.reconciler.poller.sweep_stalled", fake_sweep)
    monkeypatch.setattr(rec, "_ensure_intents", lambda: _anoop())

    await rec.tick()
    assert called.get("cap") == rec._stalled_cap


async def test_tick_skips_stalled_sweep_when_disabled(tmp_path, monkeypatch):
    reg, db, ops, rec = _make(tmp_path)
    rec.stalled_cleanup = False
    called = {"ran": False}

    async def fake_poll_all(*a, **k):
        return {"polled": []}

    async def fake_sweep(*a, **k):
        called["ran"] = True
        return 0

    monkeypatch.setattr("app.reconciler.poller.poll_all", fake_poll_all)
    monkeypatch.setattr("app.reconciler.poller.sweep_stalled", fake_sweep)
    monkeypatch.setattr(rec, "_ensure_intents", lambda: _anoop())

    await rec.tick()
    assert called["ran"] is False


async def _anoop():
    return None
