"""Add, smart-add (with fallback), availability, live progress streams, and log."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.responses import StreamingResponse

from app.models import AddRequest, AdvanceFallbackRequest, SmartAddRequest, SpillSeasonRequest
from app.services import add as add_service
from app.services import orchestrate
from app.services.availability import DEFAULT_MIN_SEEDERS, check_availability
from app.sonarr.registry import Registry
from app.state import get_db, get_operations, get_registry
from app.store import settings as settings_store
from app.store.operations import OperationStore

router = APIRouter(prefix="/api", tags=["add"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _seeder_opts(db) -> tuple[int, bool]:
    """(minSeeders gate threshold, seederGrab flag) from the stored defaults."""
    defaults = await settings_store.get_defaults(db)
    return (
        int(defaults.get("minSeeders", DEFAULT_MIN_SEEDERS)),
        bool(defaults.get("seederGrab", True)),
    )


async def _event_stream(ops: OperationStore, op_id: int, coro_factory):
    """Run an emit-aware operation, streaming its events as SSE + persisting them.

    ``coro_factory(emit)`` returns the awaitable for the operation. Every emitted
    step is persisted via the store and pushed to the client as an ``event: step``
    SSE frame; the operation finishes with a ``result`` (or ``error``) frame and a
    durable ``finish`` write.
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: dict):
        await ops.add_step(op_id, event)
        await queue.put(("step", event))

    async def runner():
        result = None
        error = None
        try:
            result = await coro_factory(emit)
            await queue.put(("result", result))
        except Exception as exc:  # noqa: BLE001 - surface to the client as an event
            error = str(exc)
            await queue.put(("error", {"message": error}))
        finally:
            await ops.finish(op_id, result=result, error=error, finished_at=_now())
            await queue.put(("end", None))

    task = asyncio.create_task(runner())
    try:
        while True:
            kind, payload = await queue.get()
            if kind == "end":
                break
            yield f"event: {kind}\ndata: {json.dumps(payload)}\n\n"
    finally:
        await task


_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@router.post("/add")
async def add(req: AddRequest, reg: Registry = Depends(get_registry)):
    """Add a show to each selected instance; report per-target success/error."""
    results = []
    for target in req.targets:
        try:
            series = await add_service.add_to_instance(
                reg,
                target.instanceId,
                tvdb_id=req.tvdbId,
                quality_profile_id=target.qualityProfileId,
                root_folder_path=target.rootFolderPath,
                monitored=target.monitored,
                search_now=target.searchNow,
                monitored_seasons=target.monitoredSeasons,
            )
            results.append({"instanceId": target.instanceId, "ok": True, "series": series})
        except Exception as exc:  # noqa: BLE001 - surface per-target failures to the UI
            results.append({"instanceId": target.instanceId, "ok": False, "error": str(exc)})
    return {"results": results}


@router.post("/smart-add")
async def smart_add(
    req: SmartAddRequest, reg: Registry = Depends(get_registry), db=Depends(get_db)
):
    """Add to the target tier and verify a release exists; suggest fallback if not."""
    min_seeders, seeder_grab = await _seeder_opts(db)
    try:
        return await add_service.smart_add(
            reg,
            tvdb_id=req.tvdbId,
            target_id=req.targetInstanceId,
            target_opts={
                "quality_profile_id": req.targetQualityProfileId,
                "root_folder_path": req.targetRootFolderPath,
                "monitored": req.monitored,
                "monitored_seasons": req.monitoredSeasons,
            },
            min_seeders=min_seeders,
            seeder_grab=seeder_grab,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/advance-fallback")
async def advance_fallback(
    req: AdvanceFallbackRequest, reg: Registry = Depends(get_registry), db=Depends(get_db)
):
    """Execute the next fallback-chain step; returns placed / fallback_suggested / exhausted."""
    min_seeders, seeder_grab = await _seeder_opts(db)
    try:
        return await add_service.advance_fallback(
            reg,
            tvdb_id=req.tvdbId,
            from_instance_id=req.fromInstanceId,
            from_series_id=req.fromSeriesId,
            chain_key=req.chainKey,
            next_index=req.nextIndex,
            min_seeders=min_seeders,
            seeder_grab=seeder_grab,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/spill-season")
async def spill_season(req: SpillSeasonRequest, reg: Registry = Depends(get_registry)):
    """Fetch one whole season from a lower tier (per-season 'lower resolution')."""
    try:
        return await orchestrate.spill_season(
            reg,
            tvdb_id=req.tvdbId,
            season=req.season,
            origin_instance_id=req.originInstanceId,
            fb_instance_id=req.fallbackInstanceId,
            profile=req.profile,
            root=req.root,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/availability")
async def availability(
    instanceId: str, seriesId: int,
    reg: Registry = Depends(get_registry), db=Depends(get_db),
):
    """Check release availability for an already-added series on an instance."""
    try:
        min_seeders, _ = await _seeder_opts(db)
        return await check_availability(reg, instanceId, seriesId, min_seeders=min_seeders)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/smart-add/stream")
async def smart_add_stream(
    tvdbId: int,
    targetInstanceId: str,
    targetQualityProfileId: int,
    targetRootFolderPath: str,
    monitored: bool = True,
    monitoredSeasons: list[int] | None = Query(default=None),
    title: str = "",
    reg: Registry = Depends(get_registry),
    ops: OperationStore = Depends(get_operations),
    db=Depends(get_db),
):
    """Live (SSE) smart-add: streams each step, ends with the normal result dict."""
    op_id = await ops.start(kind="smart-add", title=title or f"tvdb:{tvdbId}", tvdb_id=tvdbId, started_at=_now())
    min_seeders, seeder_grab = await _seeder_opts(db)

    async def factory(emit):
        return await add_service.smart_add(
            reg,
            tvdb_id=tvdbId,
            target_id=targetInstanceId,
            target_opts={
                "quality_profile_id": targetQualityProfileId,
                "root_folder_path": targetRootFolderPath,
                "monitored": monitored,
                "monitored_seasons": monitoredSeasons,
            },
            emit=emit,
            min_seeders=min_seeders,
            seeder_grab=seeder_grab,
        )

    return StreamingResponse(
        _event_stream(ops, op_id, factory), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@router.get("/advance-fallback/stream")
async def advance_fallback_stream(
    tvdbId: int,
    fromInstanceId: str,
    fromSeriesId: int,
    chainKey: str,
    nextIndex: int,
    title: str = "",
    reg: Registry = Depends(get_registry),
    ops: OperationStore = Depends(get_operations),
    db=Depends(get_db),
):
    """Live (SSE) fallback step: streams move/swap/search, ends with the result dict."""
    op_id = await ops.start(kind="advance", title=title or f"tvdb:{tvdbId}", tvdb_id=tvdbId, started_at=_now())
    min_seeders, seeder_grab = await _seeder_opts(db)

    async def factory(emit):
        return await add_service.advance_fallback(
            reg,
            tvdb_id=tvdbId,
            from_instance_id=fromInstanceId,
            from_series_id=fromSeriesId,
            chain_key=chainKey,
            next_index=nextIndex,
            emit=emit,
            min_seeders=min_seeders,
            seeder_grab=seeder_grab,
        )

    return StreamingResponse(
        _event_stream(ops, op_id, factory), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@router.get("/reattempt/stream")
async def reattempt_stream(
    tvdbId: int,
    instanceId: str,
    seriesId: int,
    chainKey: str,
    nextIndex: int,
    title: str = "",
    reg: Registry = Depends(get_registry),
    ops: OperationStore = Depends(get_operations),
    db=Depends(get_db),
):
    """Live (SSE) re-attempt: re-search in place; ends placed / fallback_suggested / exhausted."""
    op_id = await ops.start(kind="reattempt", title=title or f"tvdb:{tvdbId}", tvdb_id=tvdbId, started_at=_now())
    min_seeders, seeder_grab = await _seeder_opts(db)

    async def factory(emit):
        return await add_service.reattempt_search(
            reg,
            tvdb_id=tvdbId,
            instance_id=instanceId,
            series_id=seriesId,
            chain_key=chainKey,
            next_index=nextIndex,
            emit=emit,
            min_seeders=min_seeders,
            seeder_grab=seeder_grab,
        )

    return StreamingResponse(
        _event_stream(ops, op_id, factory), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@router.get("/fill-gaps/stream")
async def fill_gaps_stream(
    tvdbId: int,
    fromInstanceId: str,
    fromSeriesId: int,
    chainKey: str,
    nextIndex: int,
    title: str = "",
    reg: Registry = Depends(get_registry),
    ops: OperationStore = Depends(get_operations),
):
    """Live (SSE) gap-fill split: streams gaps/add/split, ends with a ``split`` result."""
    op_id = await ops.start(kind="fill-gaps", title=title or f"tvdb:{tvdbId}", tvdb_id=tvdbId, started_at=_now())

    async def factory(emit):
        return await add_service.fill_gaps(
            reg,
            tvdb_id=tvdbId,
            from_instance_id=fromInstanceId,
            from_series_id=fromSeriesId,
            chain_key=chainKey,
            next_index=nextIndex,
            emit=emit,
        )

    return StreamingResponse(
        _event_stream(ops, op_id, factory), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@router.get("/spill-season/stream")
async def spill_season_stream(
    tvdbId: int,
    season: int,
    originInstanceId: str,
    fallbackInstanceId: str,
    profile: str = "",
    root: str = "",
    title: str = "",
    reg: Registry = Depends(get_registry),
    ops: OperationStore = Depends(get_operations),
):
    """Live (SSE) season spill: streams gaps/split, ends with a ``spilled`` result."""
    op_id = await ops.start(kind="spill-season", title=title or f"tvdb:{tvdbId}",
                            tvdb_id=tvdbId, started_at=_now())

    async def factory(emit):
        return await orchestrate.spill_season(
            reg,
            tvdb_id=tvdbId,
            season=season,
            origin_instance_id=originInstanceId,
            fb_instance_id=fallbackInstanceId,
            profile=profile or None,
            root=root or None,
            emit=emit,
        )

    return StreamingResponse(
        _event_stream(ops, op_id, factory), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@router.get("/operations")
async def operations(ops: OperationStore = Depends(get_operations)):
    """Recent smart-add / fallback operations with their step traces, newest first."""
    return await ops.recent()
