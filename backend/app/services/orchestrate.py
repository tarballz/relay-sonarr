"""Emit-agnostic orchestration primitives the reconciler composes.

These act on *specific* episodes (keyed by (season, episode)) on a given tier,
unlike the user-facing ``smart_add``/``fill_gaps`` which narrate over SSE. They
reuse the same low-level helpers (``add_to_instance``, ``resolve_step``,
``_wait_for_episodes``) so behavior matches the manual flow.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import httpx

from app.config import FallbackStep
from app.episodes import episode_key, find_series_by_tvdb
from app.services.add import _noop_emit, _wait_for_episodes, add_to_instance, resolve_step
from app.sonarr.registry import Registry

logger = logging.getLogger(__name__)


async def _series_and_episodes(client, tvdb_id: int):
    series = await find_series_by_tvdb(client, tvdb_id)
    if series is None:
        return None, []
    return series["id"], await client.episodes(series["id"])


async def search_keys_on_tier(
    registry: Registry, instance_id: str, tvdb_id: int, keys: set,
    *, ensure_monitored: bool = True, candidates: dict[tuple, dict] | None = None,
) -> list[int]:
    """Monitor (optionally) + acquire the episodes matching ``keys`` on a tier
    the series already lives on. Returns the episode ids acted on.

    ``candidates`` maps ``(season, episode)`` to a cached grab candidate
    ``{guid, indexerId, ...}`` (captured by ``refresh_availability``). Episodes
    with a candidate are grabbed directly — no indexer search; the rest (and any
    failed grabs, e.g. a guid that went stale within the cache TTL) land in one
    batched EpisodeSearch, so an episode is never left without an action.
    """
    client = registry.get(instance_id).client
    series_id, episodes = await _series_and_episodes(client, tvdb_id)
    if series_id is None:
        return []
    matched = [e for e in episodes if episode_key(e) in keys]
    ids = [e["id"] for e in matched]
    if not ids:
        return []
    if ensure_monitored:
        await client.set_episode_monitor(ids, True)

    to_search: list[int] = []
    for ep in matched:
        cand = (candidates or {}).get(episode_key(ep))
        if cand:
            try:
                await client.grab_release(cand["guid"], cand["indexerId"])
                continue
            except httpx.HTTPError as exc:
                logger.warning(
                    "cached grab of %r failed (%s); falling back to EpisodeSearch",
                    cand.get("title"), exc,
                )
        to_search.append(ep["id"])
    if to_search:
        await client.command("EpisodeSearch", episodeIds=to_search)
    return ids


async def place_on_fallback(
    registry: Registry, *, tvdb_id: int, fb_instance_id: str, keys: set,
    origin_instance_id: str, profile: str | None = None, root: str | None = None,
    sleep: Callable[[float], Awaitable] = asyncio.sleep,
    wait_attempts: int = 10, wait_delay: float = 1.5,
) -> list[int]:
    """Split specific episodes onto a fallback tier: ensure the series exists
    there, monitor+search exactly ``keys``, then unmonitor them on the origin so
    the two tiers hold a disjoint set. Returns the fallback episode ids searched.

    Fallback placement stays a plain EpisodeSearch (no direct grab): it is
    optimistic — the series may not even exist on this tier yet, so no
    availability check ran here and there is no cached candidate to grab."""
    resolved = await resolve_step(
        registry, FallbackStep(instanceId=fb_instance_id, profile=profile, root_folder=root)
    )
    fb = registry.get(fb_instance_id).client
    series_id, episodes = await _series_and_episodes(fb, tvdb_id)
    if series_id is None:
        added = await add_to_instance(
            registry, fb_instance_id, tvdb_id=tvdb_id,
            quality_profile_id=resolved["qualityProfileId"],
            root_folder_path=resolved["rootFolderPath"],
            monitored=True, search_now=False,
        )
        series_id = added["id"]
        await fb.command("RefreshSeries", seriesIds=[series_id])
        episodes = await _wait_for_episodes(fb, series_id, wait_attempts, wait_delay, sleep)

    ids = [e["id"] for e in episodes if episode_key(e) in keys]
    if ids:
        await fb.set_episode_monitor(ids, True)
        await fb.command("EpisodeSearch", episodeIds=ids)

    # Unmonitor the same episodes on the origin tier (disjoint split, no deletes).
    origin = registry.get(origin_instance_id).client
    o_series_id, o_episodes = await _series_and_episodes(origin, tvdb_id)
    if o_series_id is not None:
        o_ids = [e["id"] for e in o_episodes if episode_key(e) in keys]
        await origin.set_episode_monitor(o_ids, False)
    return ids


async def spill_season(
    registry: Registry, *, tvdb_id: int, season: int,
    origin_instance_id: str, fb_instance_id: str,
    profile: str | None = None, root: str | None = None,
    sleep: Callable[[float], Awaitable] = asyncio.sleep,
    wait_attempts: int = 10, wait_delay: float = 1.5,
    emit: Callable[[dict], Awaitable] | None = None,
) -> dict:
    """Fetch one whole season from a lower tier (emit-aware, narratable over SSE).

    "Lower the resolution for season N" in a 1080p+4K setup means getting that
    season from the lower-res instance. Resolves the season's ``(season, episode)``
    keys on the origin and hands them to ``place_on_fallback``, which monitors +
    searches them on the fallback tier and unmonitors them on the origin (disjoint
    split, no files deleted). The rest of the show stays on the origin tier.
    """
    emit = emit or _noop_emit
    fb_name = registry.get(fb_instance_id).name
    origin = registry.get(origin_instance_id).client

    await emit({"phase": "gaps", "status": "running",
                "message": f"Finding season {season} episodes…"})
    _, origin_eps = await _series_and_episodes(origin, tvdb_id)
    keys = {episode_key(e) for e in origin_eps if e.get("seasonNumber") == season}
    await emit({"phase": "gaps", "status": "done",
                "message": f"{len(keys)} episode(s) in season {season}"})

    if not keys:
        return {
            "status": "spilled", "season": season,
            "from": {"instanceId": origin_instance_id},
            "to": {"instanceId": fb_instance_id, "seriesId": None},
            "episodeCount": 0,
        }

    await emit({"phase": "split", "status": "running",
                "message": f"Moving season {season} to {fb_name}…"})
    ids = await place_on_fallback(
        registry, tvdb_id=tvdb_id, fb_instance_id=fb_instance_id, keys=keys,
        origin_instance_id=origin_instance_id, profile=profile, root=root,
        sleep=sleep, wait_attempts=wait_attempts, wait_delay=wait_delay,
    )
    fb_series = await find_series_by_tvdb(registry.get(fb_instance_id).client, tvdb_id)
    await emit({"phase": "split", "status": "done",
                "message": f"Season {season}: {len(ids)} episode(s) now searching on {fb_name}; "
                           f"the rest of the show stays put"})
    return {
        "status": "spilled", "season": season,
        "from": {"instanceId": origin_instance_id},
        "to": {"instanceId": fb_instance_id,
               "seriesId": fb_series["id"] if fb_series else None},
        "episodeCount": len(ids),
    }
