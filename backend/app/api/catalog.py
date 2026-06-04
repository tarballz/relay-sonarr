"""Search, combined library, and combined queue."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.services import library
from app.services import queue as queue_service
from app.sonarr.registry import Registry
from app.state import get_registry

router = APIRouter(prefix="/api", tags=["catalog"])


@router.get("/search")
async def search(term: str, reg: Registry = Depends(get_registry)):
    """Look up a show and annotate which instances already have it.

    Lookup hits TVDB and is instance-agnostic, so we query the first instance,
    then cross-reference the combined library to flag ``existsOn``.
    """
    instances = reg.all()
    if not instances:
        raise HTTPException(status_code=503, detail="No instances configured")

    results = await instances[0].client.lookup(term)

    by_tvdb: dict[int, list[str]] = {}
    for series in await library.combined_series(reg):
        by_tvdb.setdefault(series.get("tvdbId"), []).append(series["instanceId"])

    for result in results:
        result["existsOn"] = by_tvdb.get(result.get("tvdbId"), [])
    return results


@router.get("/series")
async def series(reg: Registry = Depends(get_registry)):
    return await library.combined_series(reg)


@router.get("/queue")
async def queue(reg: Registry = Depends(get_registry)):
    return await queue_service.combined_queue(reg)


@router.delete("/instances/{instance_id}/series/{series_id}")
async def remove_series(
    instance_id: str,
    series_id: int,
    deleteFiles: bool = False,
    reg: Registry = Depends(get_registry),
):
    """Remove a series from an instance. Files are kept unless deleteFiles=true."""
    try:
        inst = reg.get(instance_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown instance: {instance_id}")
    await inst.client.delete_series(series_id, delete_files=deleteFiles)
    return {"ok": True, "instanceId": instance_id, "seriesId": series_id}
