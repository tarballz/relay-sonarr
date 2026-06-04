"""Adding series to instances, plus the smart-add fallback orchestration."""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from app.config import FallbackStep
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
) -> dict:
    """Build the POST /series body from a lookup result.

    Sonarr wants the full looked-up series object plus the destination fields, so
    we spread the lookup result and layer the instance-specific choices on top.
    """
    return {
        **series,
        "qualityProfileId": quality_profile_id,
        "rootFolderPath": root_folder_path,
        "monitored": monitored,
        "addOptions": {"searchForMissingEpisodes": search_now},
    }


async def add_to_instance(
    registry: Registry,
    instance_id: str,
    tvdb_id: int,
    quality_profile_id: int,
    root_folder_path: str,
    monitored: bool = True,
    search_now: bool = True,
) -> dict:
    """Look the series up on the instance by tvdbId, then add it."""
    client = registry.get(instance_id).client
    matches = await client.lookup(f"tvdb:{tvdb_id}")
    if not matches:
        raise ValueError(f"No series found for tvdbId={tvdb_id} on '{instance_id}'")
    payload = build_add_payload(
        matches[0], quality_profile_id, root_folder_path, monitored, search_now
    )
    return await client.add_series(payload)


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
    }
