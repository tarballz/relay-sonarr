"""Episode-level availability + placement model.

Replaces the single-sample "is this series available?" boolean with two things:

- ``refresh_availability`` — interactive-search each *gap* episode on one tier,
  cached with a TTL and bounded by a semaphore (interactive search is the main
  indexer-hammering risk). Generalizes ``check_availability`` from one episode to all.
- ``compute_plan`` — join cross-instance file state (cheap, no search) with the
  cached availability into a per-episode placement: where each episode currently
  *is* (obtained_tier), where it *could* come from (obtainable_tier), and its state.
  The result is persisted to the ``placement`` table for the reconciler (Phase 4)
  and returned for the UI.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from app.episodes import episode_key, find_series_by_tvdb, is_gap
from app.services.availability import count_qualifying, summarize_rejections
from app.services.fanout import gather_instances
from app.services.status import derive_status
from app.sonarr.registry import Registry
from app.store import availability as avail_cache
from app.store import placements as place_store

DEFAULT_TTL = 6 * 3600  # seconds

# Episode states owned by the download lifecycle (poller/reconciler), which
# compute_plan must preserve rather than recompute from file/availability.
IN_PROGRESS = {"searching", "grabbed", "importing", "failed"}


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def tier_priority(registry: Registry, desired: str, present: list[str]) -> list[str]:
    """Ordered instance ids to consider: desired first, then its fallback chain,
    then any other instance that actually has the series — deduped, order kept."""
    order: list[str] = []
    def add(iid):
        if iid and iid not in order and iid in {i.id for i in registry.all()}:
            order.append(iid)
    add(desired)
    for step in registry.fallback_chain(desired):
        add(step.instanceId)
    for iid in present:
        add(iid)
    return order


def _infer_desired(registry: Registry, present: list[str]) -> str | None:
    """Prefer a present instance that has a fallback chain configured (the tier
    you'd start from), else the first instance the series is on."""
    for iid in present:
        if registry.has_fallback(iid):
            return iid
    return present[0] if present else None


async def _episodes_for_tvdb(client, tvdb_id: int):
    series = await find_series_by_tvdb(client, tvdb_id)
    if series is None:
        return None
    return await client.episodes(series["id"])


async def refresh_availability(
    registry: Registry,
    db,
    *,
    tvdb_id: int,
    instance_id: str,
    episodes: list[dict] | None = None,
    ttl: float = DEFAULT_TTL,
    concurrency: int = 4,
    limit: int | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Interactive-search each gap episode on ``instance_id``; cache the verdicts.

    Uses the cache when a verdict is still within ``ttl`` so a sweep doesn't
    re-hit indexers; bounds concurrent ``/release`` calls with a semaphore.
    """
    now = _now(now)
    client = registry.get(instance_id).client
    if episodes is None:
        episodes = await _episodes_for_tvdb(client, tvdb_id) or []
    gaps = [e for e in episodes if is_gap(e)]
    if limit is not None:
        gaps = gaps[:limit]

    sem = asyncio.Semaphore(concurrency)
    results: list[dict] = []

    async def check(ep: dict):
        season, epnum = episode_key(ep)
        cached = await avail_cache.get_cached(db, instance_id, tvdb_id, season, epnum)
        if cached is not None and avail_cache.is_fresh(cached["checked_at"], now, ttl):
            results.append({
                "season": season, "episode": epnum,
                "qualifies": bool(cached["qualifies"]),
                "qualifyingCount": cached["qualifying_count"],
                "totalReleases": cached["total_releases"],
                "rejectionSummary": json.loads(cached["rejection_json"] or "[]"),
                "cached": True,
            })
            return
        async with sem:
            releases = await client.releases(ep["id"])
        qc = count_qualifying(releases)
        rej = summarize_rejections(releases)
        await avail_cache.put(
            db, instance_id=instance_id, tvdb_id=tvdb_id, season=season, episode=epnum,
            qualifies=qc > 0, total_releases=len(releases), qualifying_count=qc,
            rejection_json=json.dumps(rej), checked_at=now.isoformat(),
        )
        results.append({
            "season": season, "episode": epnum, "qualifies": qc > 0,
            "qualifyingCount": qc, "totalReleases": len(releases),
            "rejectionSummary": rej, "cached": False,
        })

    await asyncio.gather(*(check(e) for e in gaps))
    results.sort(key=lambda r: (r["season"], r["episode"]))
    return results


def _reason_text(rejection_summary: list[dict]) -> str | None:
    if not rejection_summary:
        return None
    return ", ".join(f"{r['count']} {r['reason']}" for r in rejection_summary)


def _season_rollup(episodes_out: list[dict], tier_priority: list[str],
                   desired: str | None) -> list[dict]:
    """Group the per-episode plan into per-season summaries for the UI.

    Each season reports its episode count, per-state counts, the tier(s) episodes
    were obtained from, and whether it can be "lowered" — i.e. an unobtained
    episode is obtainable on a tier below ``desired`` (or some episode is
    unavailable and a lower tier exists to try). ``obtainableTier`` is that lower
    spill target when known.
    """
    lower_tiers: list[str] = []
    if desired and desired in tier_priority:
        lower_tiers = tier_priority[tier_priority.index(desired) + 1:]

    by_season: dict[int, list[dict]] = {}
    for e in episodes_out:
        by_season.setdefault(e["season"], []).append(e)

    out: list[dict] = []
    for season in sorted(by_season):
        eps = by_season[season]
        counts: dict[str, int] = {}
        obtained_tiers: list[str] = []
        for e in eps:
            counts[e["state"]] = counts.get(e["state"], 0) + 1
            if e["state"] == "imported" and e["obtainedTier"] and e["obtainedTier"] not in obtained_tiers:
                obtained_tiers.append(e["obtainedTier"])

        obtainable_tier = next(
            (t for t in lower_tiers
             if any(e["obtainedTier"] is None and e["obtainableTier"] == t for e in eps)),
            None,
        )
        can_lower = bool(obtainable_tier) or (counts.get("unavailable", 0) > 0 and bool(lower_tiers))
        out.append({
            "season": season,
            "episodeCount": len(eps),
            "counts": counts,
            "obtainedTiers": obtained_tiers,
            "obtainableTier": obtainable_tier,
            "status": derive_status(counts, set(obtained_tiers)),
            "canLowerRes": can_lower,
        })
    return out


async def compute_plan(
    registry: Registry,
    db,
    *,
    tvdb_id: int,
    desired_tier: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Build + persist the per-episode placement for a series. Cheap: reads file
    state across instances and the cached availability — does NOT search indexers
    (call ``refresh_availability`` first to populate obtainable tiers)."""
    now = _now(now)

    per_instance: dict[str, dict] = {}
    present: list[str] = []
    for inst, res in await gather_instances(
        registry, lambda i: _episodes_for_tvdb(i.client, tvdb_id)
    ):
        if isinstance(res, Exception) or not res:
            continue
        per_instance[inst.id] = {episode_key(e): e for e in res}
        present.append(inst.id)

    desired = desired_tier or _infer_desired(registry, present)
    priority = tier_priority(registry, desired, present) if desired else present

    # Cached availability indexed by (instance_id, season, episode).
    cache_by: dict[tuple, dict] = {}
    for row in await avail_cache.get_for_series(db, tvdb_id):
        cache_by[(row["instance_id"], row["season"], row["episode"])] = dict(row)

    # Existing rows: poller/reconciler-owned states the plan must not downgrade,
    # plus the wanted_since stamp that accrues from the first time we wanted it.
    prev_rows = {
        (r["season"], r["episode"]): dict(r)
        for r in await place_store.get_for_series(db, tvdb_id)
    }
    prev_state = {k: r["state"] for k, r in prev_rows.items()}

    all_keys = sorted({k for eps in per_instance.values() for k in eps})
    episodes_out: list[dict] = []
    for (season, epnum) in all_keys:
        obtained = next(
            (t for t in priority
             if (per_instance.get(t, {}).get((season, epnum)) or {}).get("hasFile")),
            None,
        )
        monitored = any(
            (per_instance.get(t, {}).get((season, epnum)) or {}).get("monitored")
            for t in priority
        )
        obtainable = next(
            (t for t in priority
             if (cache_by.get((t, season, epnum)) or {}).get("qualifies")),
            None,
        )
        reason = None
        prev = prev_state.get((season, epnum))
        if obtained:
            state = "imported"
        elif not monitored:
            state = "unmonitored"
        elif prev in IN_PROGRESS:
            # Download lifecycle owns this episode right now — defer to the poller.
            state = prev
        elif obtainable:
            state = "wanted"
        else:
            # Monitored gap. If we checked the desired tier and nothing qualified,
            # it's unavailable-with-reason; otherwise just wanted (not yet checked).
            checked = cache_by.get((desired, season, epnum)) if desired else None
            if checked is not None:
                state = "unavailable"
                reason = _reason_text(json.loads(checked["rejection_json"] or "[]"))
            else:
                state = "wanted"

        # wanted_since: stamped when an episode first needs a release, accrues
        # until imported (cleared), preserved across recomputes. Drives escalation.
        prev_ws = (prev_rows.get((season, epnum)) or {}).get("wanted_since")
        if state in ("wanted", "unavailable", "searching", "grabbed", "importing", "failed"):
            wanted_since = prev_ws or now.isoformat()
        elif state == "imported":
            wanted_since = None
        else:
            wanted_since = prev_ws

        await place_store.upsert(
            db, tvdb_id=tvdb_id, season=season, episode=epnum,
            desired_tier=desired, obtained_tier=obtained, state=state,
            reason=reason, wanted_since=wanted_since, updated_at=now.isoformat(),
        )
        episodes_out.append({
            "season": season, "episode": epnum, "desiredTier": desired,
            "obtainedTier": obtained, "obtainableTier": obtainable,
            "state": state, "reason": reason,
        })

    counts: dict[str, int] = {}
    for e in episodes_out:
        counts[e["state"]] = counts.get(e["state"], 0) + 1

    return {
        "tvdbId": tvdb_id,
        "desiredTier": desired,
        "tierPriority": priority,
        "presentOn": present,
        "counts": counts,
        "episodes": episodes_out,
        "seasons": _season_rollup(episodes_out, priority, desired),
    }
