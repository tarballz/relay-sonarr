"""Add, smart-add (with fallback), availability, live progress streams, and log."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from starlette.responses import StreamingResponse

from app.models import AddRequest, AdvanceFallbackRequest, SmartAddRequest
from app.operations import OperationLog
from app.services import add as add_service
from app.services.availability import check_availability
from app.sonarr.registry import Registry
from app.state import get_operations, get_registry

router = APIRouter(prefix="/api", tags=["add"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _event_stream(operation: dict, coro_factory):
    """Run an emit-aware operation, streaming its events as SSE + logging them.

    ``coro_factory(emit)`` returns the awaitable for the operation. Every emitted
    step is appended to ``operation`` (for the persistent log) and pushed to the
    client as an ``event: step`` SSE frame; the operation finishes with a
    ``result`` (or ``error``) frame.
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: dict):
        operation["steps"].append(event)
        await queue.put(("step", event))

    async def runner():
        try:
            result = await coro_factory(emit)
            operation["result"] = result
            await queue.put(("result", result))
        except Exception as exc:  # noqa: BLE001 - surface to the client as an event
            operation["error"] = str(exc)
            await queue.put(("error", {"message": str(exc)}))
        finally:
            operation["finishedAt"] = _now()
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
            )
            results.append({"instanceId": target.instanceId, "ok": True, "series": series})
        except Exception as exc:  # noqa: BLE001 - surface per-target failures to the UI
            results.append({"instanceId": target.instanceId, "ok": False, "error": str(exc)})
    return {"results": results}


@router.post("/smart-add")
async def smart_add(req: SmartAddRequest, reg: Registry = Depends(get_registry)):
    """Add to the target tier and verify a release exists; suggest fallback if not."""
    try:
        return await add_service.smart_add(
            reg,
            tvdb_id=req.tvdbId,
            target_id=req.targetInstanceId,
            target_opts={
                "quality_profile_id": req.targetQualityProfileId,
                "root_folder_path": req.targetRootFolderPath,
                "monitored": req.monitored,
            },
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/advance-fallback")
async def advance_fallback(req: AdvanceFallbackRequest, reg: Registry = Depends(get_registry)):
    """Execute the next fallback-chain step; returns placed / fallback_suggested / exhausted."""
    try:
        return await add_service.advance_fallback(
            reg,
            tvdb_id=req.tvdbId,
            from_instance_id=req.fromInstanceId,
            from_series_id=req.fromSeriesId,
            chain_key=req.chainKey,
            next_index=req.nextIndex,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/availability")
async def availability(instanceId: str, seriesId: int, reg: Registry = Depends(get_registry)):
    """Check release availability for an already-added series on an instance."""
    try:
        return await check_availability(reg, instanceId, seriesId)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/smart-add/stream")
async def smart_add_stream(
    tvdbId: int,
    targetInstanceId: str,
    targetQualityProfileId: int,
    targetRootFolderPath: str,
    monitored: bool = True,
    title: str = "",
    reg: Registry = Depends(get_registry),
    ops: OperationLog = Depends(get_operations),
):
    """Live (SSE) smart-add: streams each step, ends with the normal result dict."""
    op = ops.start(kind="smart-add", title=title or f"tvdb:{tvdbId}", tvdb_id=tvdbId, started_at=_now())

    async def factory(emit):
        return await add_service.smart_add(
            reg,
            tvdb_id=tvdbId,
            target_id=targetInstanceId,
            target_opts={
                "quality_profile_id": targetQualityProfileId,
                "root_folder_path": targetRootFolderPath,
                "monitored": monitored,
            },
            emit=emit,
        )

    return StreamingResponse(
        _event_stream(op, factory), media_type="text/event-stream", headers=_SSE_HEADERS
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
    ops: OperationLog = Depends(get_operations),
):
    """Live (SSE) fallback step: streams move/swap/search, ends with the result dict."""
    op = ops.start(kind="advance", title=title or f"tvdb:{tvdbId}", tvdb_id=tvdbId, started_at=_now())

    async def factory(emit):
        return await add_service.advance_fallback(
            reg,
            tvdb_id=tvdbId,
            from_instance_id=fromInstanceId,
            from_series_id=fromSeriesId,
            chain_key=chainKey,
            next_index=nextIndex,
            emit=emit,
        )

    return StreamingResponse(
        _event_stream(op, factory), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@router.get("/operations")
async def operations(ops: OperationLog = Depends(get_operations)):
    """Recent smart-add / fallback operations with their step traces, newest first."""
    return ops.recent()
