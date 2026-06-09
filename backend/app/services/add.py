"""Adding series to instances, plus the smart-add fallback orchestration."""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from app.config import FallbackStep
from app.episodes import episode_key as _episode_key, find_series_by_tvdb as _find_series_by_tvdb
from app.services.availability import check_availability
from app.sonarr.registry import Registry


async def _noop_emit(event: dict) -> None:
    """Default progress sink — used when no streaming consumer is attached."""


def _search_message(availability: dict) -> str:
    total = availability.get("totalReleases", 0)
    qualifying = availability.get("releaseCount", 0)
    if total == 0:
        return "No releases found"
    return f"{total} found, {qualifying} qualify"


def build_add_payload(
    series: dict,
    quality_profile_id: int,
    root_folder_path: str,
    monitored: bool = True,
    search_now: bool = True,
    monitored_seasons: set[int] | list[int] | None = None,
) -> dict:
    """Build the POST /series body from a lookup result.

    Sonarr wants the full looked-up series object plus the destination fields, so
    we spread the lookup result and layer the instance-specific choices on top.

    When ``monitored_seasons`` is given, each season's ``monitored`` flag is set to
    whether its number is in the set — a best-effort hint to Sonarr's add. The
    authoritative per-season enforcement is the post-add unmonitor in
    ``add_to_instance``/``smart_add``, since Sonarr's honoring of these flags varies.
    """
    payload = {
        **series,
        "qualityProfileId": quality_profile_id,
        "rootFolderPath": root_folder_path,
        "monitored": monitored,
        "addOptions": {"searchForMissingEpisodes": search_now},
    }
    if monitored_seasons is not None:
        wanted = set(monitored_seasons)
        payload["seasons"] = [
            {**s, "monitored": s["seasonNumber"] in wanted}
            for s in series.get("seasons", [])
        ]
    return payload


async def add_to_instance(
    registry: Registry,
    instance_id: str,
    tvdb_id: int,
    quality_profile_id: int,
    root_folder_path: str,
    monitored: bool = True,
    search_now: bool = True,
    monitored_seasons: set[int] | list[int] | None = None,
    sleep: Callable[[float], Awaitable] = asyncio.sleep,
    wait_attempts: int = 10,
    wait_delay: float = 1.5,
) -> dict:
    """Look the series up on the instance by tvdbId, then add it.

    When ``monitored_seasons`` is given, episodes outside those seasons are
    unmonitored after the add (the authoritative per-season enforcement): the add
    is refreshed and we wait for episodes to populate before toggling. Without it,
    this stays a fast single POST with no episode wait.
    """
    client = registry.get(instance_id).client
    matches = await client.lookup(f"tvdb:{tvdb_id}")
    if not matches:
        raise ValueError(f"No series found for tvdbId={tvdb_id} on '{instance_id}'")
    payload = build_add_payload(
        matches[0], quality_profile_id, root_folder_path, monitored, search_now,
        monitored_seasons=monitored_seasons,
    )
    series = await client.add_series(payload)
    if monitored_seasons is not None:
        wanted = set(monitored_seasons)
        await client.command("RefreshSeries", seriesIds=[series["id"]])
        episodes = await _wait_for_episodes(
            client, series["id"], wait_attempts, wait_delay, sleep
        )
        off_ids = [e["id"] for e in episodes if _episode_key(e)[0] not in wanted]
        await client.set_episode_monitor(off_ids, False)
    return series


async def _wait_for_episodes(
    client, series_id: int, attempts: int, delay: float, sleep: Callable[[float], Awaitable]
) -> list[dict]:
    """Poll until Sonarr has populated episodes for a freshly added series."""
    episodes: list[dict] = []
    for _ in range(attempts):
        episodes = await client.episodes(series_id)
        if episodes:
            return episodes
        await sleep(delay)
    return episodes


async def resolve_step(registry: Registry, step: FallbackStep) -> dict:
    """Resolve a configured step to concrete ids on its instance.

    Looks the profile up by name (or takes the instance's first profile when the
    step omits one) and likewise for the root folder. Raises ValueError with a
    clear message if a named profile/folder isn't present on that instance.
    """
    inst = registry.get(step.instanceId)
    profiles = await inst.client.quality_profiles()
    if step.profile:
        match = next((p for p in profiles if p["name"].lower() == step.profile.lower()), None)
        if match is None:
            raise ValueError(
                f"Quality profile '{step.profile}' not found on instance '{step.instanceId}'"
            )
        profile_id, profile_name = match["id"], match["name"]
    else:
        if not profiles:
            raise ValueError(f"No quality profiles on instance '{step.instanceId}'")
        profile_id, profile_name = profiles[0]["id"], profiles[0]["name"]

    if step.root_folder:
        root = step.root_folder
    else:
        folders = await inst.client.root_folders()
        if not folders:
            raise ValueError(f"No root folders on instance '{step.instanceId}'")
        root = folders[0]["path"]

    return {
        "instanceId": inst.id,
        "instanceName": inst.name,
        "qualityProfileId": profile_id,
        "qualityProfileName": profile_name,
        "rootFolderPath": root,
    }


def build_options(
    *,
    tvdb_id: int,
    from_instance_id: str,
    from_series_id: int,
    chain_key: str,
    next_index: int,
    resolved: dict | None,
) -> list[dict]:
    """The menu of resolution options shown to the user at a roadblock.

    ``resolved`` is the next chain step pre-resolved (or ``None`` when there is no
    next step, e.g. the chain is exhausted). Chain-dependent options are disabled
    with a reason rather than omitted, so the menu has the same shape whether the
    response is ``fallback_suggested`` or ``exhausted``. Each option carries the
    ``payload`` the UI echoes back to drive the matching endpoint.
    """
    has_next = resolved is not None
    same_instance = has_next and resolved["instanceId"] == from_instance_id
    cross_instance = has_next and not same_instance
    here_remove = {"instanceId": from_instance_id, "seriesId": from_series_id}
    walk_payload = {
        "tvdbId": tvdb_id,
        "fromInstanceId": from_instance_id,
        "fromSeriesId": from_series_id,
        "chainKey": chain_key,
        "nextIndex": next_index,
    }

    options: list[dict] = [
        {
            "id": "reattempt",
            "action": "reattempt",
            "label": "Re-attempt search",
            "description": "Search the indexers again on this tier — useful right "
            "after adding indexers in Prowlarr.",
            "enabled": True,
            "disabledReason": None,
            "payload": {
                "tvdbId": tvdb_id,
                "instanceId": from_instance_id,
                "seriesId": from_series_id,
                "chainKey": chain_key,
                "nextIndex": next_index,
            },
        }
    ]

    if has_next:
        options.append({
            "id": "walk_chain",
            "action": "walk_chain",
            "label": (f"Switch to the {resolved['qualityProfileName']} profile"
                      if same_instance else f"Move to {resolved['instanceName']}"),
            "description": (
                f"Re-search {resolved['instanceName']} with the "
                f"{resolved['qualityProfileName']} profile; the series stays put."
                if same_instance else
                f"Move to {resolved['instanceName']} ({resolved['qualityProfileName']}) "
                "and search; the empty entry here is removed once a release is found."
            ),
            "enabled": True,
            "disabledReason": None,
            "instanceId": resolved["instanceId"],
            "payload": walk_payload,
        })
    else:
        options.append({
            "id": "walk_chain",
            "action": "walk_chain",
            "label": "Try the next tier",
            "description": "No further tier is configured in the fallback chain.",
            "enabled": False,
            "disabledReason": "end of chain",
            "payload": {},
        })

    if cross_instance:
        options.append({
            "id": "fill_gaps",
            "action": "fill_gaps",
            "label": f"Fill gaps from {resolved['instanceName']}",
            "description": (
                f"Keep what's already grabbed here; fetch only the still-missing "
                f"episodes from {resolved['instanceName']} "
                f"({resolved['qualityProfileName']})."
            ),
            "enabled": True,
            "disabledReason": None,
            "instanceId": resolved["instanceId"],
            "payload": walk_payload,
        })
    else:
        options.append({
            "id": "fill_gaps",
            "action": "fill_gaps",
            "label": "Fill gaps from the fallback tier",
            "description": "Needs a different instance in the chain to split onto.",
            "enabled": False,
            "disabledReason": ("end of chain" if not has_next
                               else "next tier is the same instance"),
            "payload": {},
        })

    options.append({
        "id": "leave",
        "action": "leave",
        "label": "Leave monitored",
        "description": "Keep the series where it is, monitored — it'll grab "
        "automatically if a release appears later.",
        "enabled": True,
        "disabledReason": None,
        "payload": {},
    })
    options.append({
        "id": "remove",
        "action": "remove",
        "label": "Remove the empty series",
        "description": "Delete the series that has no downloadable release "
        "(files already on disk are kept).",
        "enabled": True,
        "disabledReason": None,
        "payload": here_remove,
    })
    return options


async def _suggest_next(
    registry: Registry,
    *,
    tvdb_id: int,
    from_instance_id: str,
    from_series_id: int,
    chain_key: str,
    next_index: int,
    availability: dict,
) -> dict:
    """Build a ``fallback_suggested`` response pointing at ``chain[next_index]``."""
    step = registry.fallback_chain(chain_key)[next_index]
    resolved = await resolve_step(registry, step)
    return {
        "status": "fallback_suggested",
        "from": {"instanceId": from_instance_id, "seriesId": from_series_id},
        "next": {
            "instanceId": resolved["instanceId"],
            "instanceName": resolved["instanceName"],
            "qualityProfileName": resolved["qualityProfileName"],
            "sameInstance": resolved["instanceId"] == from_instance_id,
        },
        # Echoed back verbatim by the UI to drive the next step.
        "advance": {
            "tvdbId": tvdb_id,
            "fromInstanceId": from_instance_id,
            "fromSeriesId": from_series_id,
            "chainKey": chain_key,
            "nextIndex": next_index,
        },
        "availability": availability,
        # The full menu the user picks from — chain step is just one option.
        "options": build_options(
            tvdb_id=tvdb_id,
            from_instance_id=from_instance_id,
            from_series_id=from_series_id,
            chain_key=chain_key,
            next_index=next_index,
            resolved=resolved,
        ),
    }


async def smart_add(
    registry: Registry,
    tvdb_id: int,
    target_id: str,
    target_opts: dict,
    sleep: Callable[[float], Awaitable] = asyncio.sleep,
    wait_attempts: int = 10,
    wait_delay: float = 1.5,
    emit: Callable[[dict], Awaitable] | None = None,
) -> dict:
    """Add to the target tier, then verify a release actually exists there.

    Returns ``status="added"`` when a qualifying release is found. Otherwise, if a
    fallback chain is configured for the target, returns ``fallback_suggested``
    pointing at the first chain step; if no chain is configured, returns
    ``status="added"`` anyway (the series is monitored and will grab if a release
    later appears).

    ``emit`` (async) receives structured ``{phase, status, message, data?}`` events
    for each step, so a streaming consumer can narrate progress live. Defaults to a
    no-op, so callers that just want the result are unaffected.
    """
    emit = emit or _noop_emit
    target = registry.get(target_id)

    await emit({"phase": "add", "status": "running", "message": f"Adding to {target.name}…"})
    series = await add_to_instance(
        registry,
        target_id,
        tvdb_id=tvdb_id,
        quality_profile_id=target_opts["quality_profile_id"],
        root_folder_path=target_opts["root_folder_path"],
        monitored=target_opts.get("monitored", True),
        search_now=target_opts.get("search_now", False),
        monitored_seasons=target_opts.get("monitored_seasons"),
        sleep=sleep,
        wait_attempts=wait_attempts,
        wait_delay=wait_delay,
    )
    await emit({"phase": "add", "status": "done", "message": f"Added to {target.name}"})

    client = target.client
    await emit({"phase": "refresh", "status": "running", "message": "Refreshing & loading episodes…"})
    await client.command("RefreshSeries", seriesIds=[series["id"]])
    episodes = await _wait_for_episodes(client, series["id"], wait_attempts, wait_delay, sleep)
    await emit({"phase": "refresh", "status": "done", "message": f"{len(episodes)} episodes loaded"})

    await emit({"phase": "search", "status": "running", "message": f"Searching {target.name} for releases…"})
    availability = await check_availability(registry, target_id, series["id"])
    await emit({
        "phase": "search", "status": "done",
        "message": _search_message(availability), "data": {"availability": availability},
    })

    if availability["available"]:
        # A release exists here — actually kick off the download now.
        await client.command("SeriesSearch", seriesId=series["id"])
        await emit({"phase": "grab", "status": "done",
                    "message": f"Release found on {target.name} — download started ✓"})
    if availability["available"] or not registry.fallback_chain(target_id):
        return {
            "status": "added",
            "instanceId": target_id,
            "series": series,
            "availability": availability,
        }

    await emit({"phase": "fallback", "status": "done",
                "message": f"No qualifying release on {target.name} — suggesting fallback"})
    return await _suggest_next(
        registry,
        tvdb_id=tvdb_id,
        from_instance_id=target_id,
        from_series_id=series["id"],
        chain_key=target_id,
        next_index=0,
        availability=availability,
    )


async def advance_fallback(
    registry: Registry,
    tvdb_id: int,
    from_instance_id: str,
    from_series_id: int,
    chain_key: str,
    next_index: int,
    sleep: Callable[[float], Awaitable] = asyncio.sleep,
    wait_attempts: int = 10,
    wait_delay: float = 1.5,
    emit: Callable[[dict], Awaitable] | None = None,
) -> dict:
    """Execute one chain step, then report placed / next-suggestion / exhausted.

    A step targeting a *different* instance moves the series (add there with a
    search, then remove the abandoned one). A step on the *same* instance swaps
    the series' quality profile and re-searches. Either way we then re-check
    availability and either stop (placed/exhausted) or suggest the next step.

    ``emit`` (async, default no-op) receives ``{phase, status, message, data?}``
    events so a streaming consumer can narrate the move/swap/search live.
    """
    emit = emit or _noop_emit
    chain = registry.fallback_chain(chain_key)
    resolved = await resolve_step(registry, chain[next_index])
    target_name = resolved["instanceName"]

    if resolved["instanceId"] != from_instance_id:
        # Cross-instance move: add first so a failed add never deletes anything.
        await emit({"phase": "move", "status": "running",
                    "message": f"Moving to {target_name} ({resolved['qualityProfileName']})…"})
        added = await add_to_instance(
            registry,
            resolved["instanceId"],
            tvdb_id=tvdb_id,
            quality_profile_id=resolved["qualityProfileId"],
            root_folder_path=resolved["rootFolderPath"],
            search_now=True,
        )
        client = registry.get(resolved["instanceId"]).client
        await client.command("RefreshSeries", seriesIds=[added["id"]])
        await _wait_for_episodes(client, added["id"], wait_attempts, wait_delay, sleep)
        await registry.get(from_instance_id).client.delete_series(from_series_id)
        await emit({"phase": "move", "status": "done",
                    "message": f"Moved to {target_name}; removed the empty entry"})
        new_instance_id, new_series_id = resolved["instanceId"], added["id"]
    else:
        # Same instance: swap the quality profile and re-search.
        await emit({"phase": "swap", "status": "running",
                    "message": f"Switching to the {resolved['qualityProfileName']} profile…"})
        client = registry.get(from_instance_id).client
        series = await client.get_series(from_series_id)
        series["qualityProfileId"] = resolved["qualityProfileId"]
        await client.update_series(series)
        await client.command("SeriesSearch", seriesId=from_series_id)
        await emit({"phase": "swap", "status": "done",
                    "message": f"Now using the {resolved['qualityProfileName']} profile"})
        new_instance_id, new_series_id = from_instance_id, from_series_id

    await emit({"phase": "search", "status": "running",
                "message": f"Searching {target_name} for releases…"})
    availability = await check_availability(registry, new_instance_id, new_series_id)
    await emit({"phase": "search", "status": "done",
                "message": _search_message(availability), "data": {"availability": availability}})

    if availability["available"]:
        await emit({"phase": "grab", "status": "done",
                    "message": f"Release found on {target_name} — download started ✓"})
        return {
            "status": "placed",
            "instanceId": new_instance_id,
            "seriesId": new_series_id,
            "qualityProfileName": resolved["qualityProfileName"],
            "availability": availability,
        }

    if next_index + 1 < len(chain):
        await emit({"phase": "fallback", "status": "done",
                    "message": f"No release on {target_name} — suggesting the next tier"})
        return await _suggest_next(
            registry,
            tvdb_id=tvdb_id,
            from_instance_id=new_instance_id,
            from_series_id=new_series_id,
            chain_key=chain_key,
            next_index=next_index + 1,
            availability=availability,
        )

    # End of the chain: leave it monitored on the last tier so it grabs later.
    await emit({"phase": "exhausted", "status": "done",
                "message": f"No release on any tier — left monitored on {target_name}"})
    return {
        "status": "exhausted",
        "instanceId": new_instance_id,
        "seriesId": new_series_id,
        "availability": availability,
        # Even with the chain spent, the user can still re-attempt / leave / remove.
        "options": build_options(
            tvdb_id=tvdb_id,
            from_instance_id=new_instance_id,
            from_series_id=new_series_id,
            chain_key=chain_key,
            next_index=next_index + 1,
            resolved=None,
        ),
    }


async def reattempt_search(
    registry: Registry,
    *,
    tvdb_id: int,
    instance_id: str,
    series_id: int,
    chain_key: str,
    next_index: int,
    sleep: Callable[[float], Awaitable] = asyncio.sleep,
    wait_attempts: int = 10,
    wait_delay: float = 1.5,
    emit: Callable[[dict], Awaitable] | None = None,
) -> dict:
    """Re-run the interactive search in place; grab if a release now qualifies.

    Nothing is moved or reconfigured — this just asks the indexers again (useful
    right after adding indexers in Prowlarr). On a hit we fire SeriesSearch and
    return ``placed``; on a miss we re-present the same option menu (the next
    chain step at ``next_index``, or an exhausted-style result if the chain is
    spent), so the user can pick again.
    """
    emit = emit or _noop_emit
    inst = registry.get(instance_id)

    await emit({"phase": "search", "status": "running",
                "message": f"Re-searching {inst.name} for releases…"})
    availability = await check_availability(registry, instance_id, series_id)
    await emit({"phase": "search", "status": "done",
                "message": _search_message(availability), "data": {"availability": availability}})

    if availability["available"]:
        await inst.client.command("SeriesSearch", seriesId=series_id)
        await emit({"phase": "grab", "status": "done",
                    "message": f"Release found on {inst.name} — download started ✓"})
        return {
            "status": "placed",
            "instanceId": instance_id,
            "seriesId": series_id,
            "availability": availability,
        }

    chain = registry.fallback_chain(chain_key)
    if next_index < len(chain):
        await emit({"phase": "fallback", "status": "done",
                    "message": f"Still nothing on {inst.name} — choose how to proceed"})
        return await _suggest_next(
            registry,
            tvdb_id=tvdb_id,
            from_instance_id=instance_id,
            from_series_id=series_id,
            chain_key=chain_key,
            next_index=next_index,
            availability=availability,
        )

    await emit({"phase": "exhausted", "status": "done",
                "message": f"Still no release on {inst.name} — left monitored"})
    return {
        "status": "exhausted",
        "instanceId": instance_id,
        "seriesId": series_id,
        "availability": availability,
        "options": build_options(
            tvdb_id=tvdb_id,
            from_instance_id=instance_id,
            from_series_id=series_id,
            chain_key=chain_key,
            next_index=next_index,
            resolved=None,
        ),
    }


async def fill_gaps(
    registry: Registry,
    *,
    tvdb_id: int,
    from_instance_id: str,
    from_series_id: int,
    chain_key: str,
    next_index: int,
    sleep: Callable[[float], Awaitable] = asyncio.sleep,
    wait_attempts: int = 10,
    wait_delay: float = 1.5,
    emit: Callable[[dict], Awaitable] | None = None,
) -> dict:
    """Split a series across tiers: keep what's grabbed here, fetch the gaps elsewhere.

    Computes the episodes still missing on the origin, ensures the series exists on
    the next (different) chain instance, monitors *only* the missing episodes there
    and searches them, then unmonitors those same episodes on the origin so the two
    instances hold a disjoint split. Episode identity is matched on
    ``(season, episode)`` because ids differ per instance. No files are deleted.
    """
    emit = emit or _noop_emit
    resolved = await resolve_step(registry, registry.fallback_chain(chain_key)[next_index])
    if resolved["instanceId"] == from_instance_id:
        raise ValueError("Gap-fill needs a different instance to split onto")
    target_name = resolved["instanceName"]

    # 1. Which episodes are still missing on the origin?
    origin = registry.get(from_instance_id).client
    await emit({"phase": "gaps", "status": "running",
                "message": "Finding episodes still missing here…"})
    origin_eps = await origin.episodes(from_series_id)
    missing = [e for e in origin_eps if e.get("monitored") and not e.get("hasFile")]
    missing_keys = {_episode_key(e) for e in missing}
    await emit({"phase": "gaps", "status": "done",
                "message": f"{len(missing_keys)} episode(s) to fill from {target_name}"})
    if not missing_keys:
        return {
            "status": "split",
            "from": {"instanceId": from_instance_id, "seriesId": from_series_id},
            "to": {"instanceId": resolved["instanceId"], "seriesId": None},
            "gapCount": 0,
        }

    # 2. Ensure the series exists on the fallback instance (reuse if already there).
    fb = registry.get(resolved["instanceId"]).client
    await emit({"phase": "add", "status": "running",
                "message": f"Setting up {target_name} for the missing episodes…"})
    existing = await _find_series_by_tvdb(fb, tvdb_id)
    if existing:
        fb_series_id = existing["id"]
    else:
        added = await add_to_instance(
            registry,
            resolved["instanceId"],
            tvdb_id=tvdb_id,
            quality_profile_id=resolved["qualityProfileId"],
            root_folder_path=resolved["rootFolderPath"],
            monitored=True,
            search_now=False,
        )
        fb_series_id = added["id"]
    await fb.command("RefreshSeries", seriesIds=[fb_series_id])
    fb_eps = await _wait_for_episodes(fb, fb_series_id, wait_attempts, wait_delay, sleep)
    await emit({"phase": "add", "status": "done",
                "message": f"{target_name} ready ({len(fb_eps)} episodes)"})
    if not fb_eps:
        raise ValueError(f"Episodes never populated on {target_name}; try again shortly")

    # 3. Monitor only the gaps on the fallback tier; unmonitor everything else.
    gap_fb_ids = [e["id"] for e in fb_eps if _episode_key(e) in missing_keys]
    non_gap_ids = [e["id"] for e in fb_eps if _episode_key(e) not in missing_keys]
    await emit({"phase": "split", "status": "running",
                "message": f"Monitoring {len(gap_fb_ids)} gap episode(s) on {target_name}…"})
    await fb.set_episode_monitor(non_gap_ids, False)
    await fb.set_episode_monitor(gap_fb_ids, True)

    # 4. Search just those episodes on the fallback tier.
    if gap_fb_ids:
        await fb.command("EpisodeSearch", episodeIds=gap_fb_ids)

    # 5. Unmonitor the same episodes on the origin so the split is disjoint.
    await origin.set_episode_monitor([e["id"] for e in missing], False)
    await emit({"phase": "split", "status": "done",
                "message": f"Split — {len(gap_fb_ids)} episode(s) now searching on {target_name}; "
                           f"existing episodes kept here"})

    return {
        "status": "split",
        "from": {"instanceId": from_instance_id, "seriesId": from_series_id},
        "to": {"instanceId": resolved["instanceId"], "seriesId": fb_series_id},
        "gapCount": len(gap_fb_ids),
    }
