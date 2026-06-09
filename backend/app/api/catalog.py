"""Search, combined library, and combined queue."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.services import library, placement, poller, status as status_service
from app.services import queue as queue_service
from app.sonarr.registry import Registry
from app.state import get_db, get_reconciler, get_registry
from app.store import intents as intent_store

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


@router.get("/library/status")
async def library_status(db=Depends(get_db)):
    """Per-series resolution status (cheap DB roll-up) for the Library badges."""
    return await status_service.library_status(db)


@router.post("/reconcile/tick")
async def reconcile_tick(reconciler=Depends(get_reconciler)):
    """Run one reconciliation pass now (the background loop runs it on a schedule)."""
    return await reconciler.tick()


@router.post("/series/{tvdb_id}/reconcile")
async def reconcile_series(
    tvdb_id: int,
    reg: Registry = Depends(get_registry),
    db=Depends(get_db),
    reconciler=Depends(get_reconciler),
):
    """Reconcile one series now (manual 'reconcile now'). Seeds an intent if needed."""
    intent = await intent_store.get(db, tvdb_id)
    if intent is None:
        # Derive a chain_key from where the series lives (prefer a tier with a chain).
        chain_key = None
        title = None
        for inst in reg.all():
            try:
                from app.episodes import find_series_by_tvdb
                series = await find_series_by_tvdb(inst.client, tvdb_id)
            except Exception:  # noqa: BLE001
                continue
            if series:
                title = series.get("title")
                if reg.has_fallback(inst.id):
                    chain_key = inst.id
                    break
                chain_key = chain_key or inst.id
        if chain_key is None:
            raise HTTPException(status_code=404, detail="Series not found on any instance")
        from datetime import datetime, timezone
        await intent_store.ensure(db, tvdb_id=tvdb_id, title=title, chain_key=chain_key,
                                  now=datetime.now(timezone.utc).isoformat())
        intent = await intent_store.get(db, tvdb_id)
    return await reconciler.reconcile_series(dict(intent))


@router.post("/poll")
async def poll(reg: Registry = Depends(get_registry), db=Depends(get_db)):
    """Poll every Sonarr's queue + history to advance download-tracking state.

    Manual trigger (and test hook); Phase 4's reconciler calls the same poller on
    a schedule. Returns a per-instance summary of state transitions applied.
    """
    return await poller.poll_all(reg, db)


@router.get("/series/{tvdb_id}/plan")
async def series_plan(
    tvdb_id: int,
    refresh: str | None = None,
    reg: Registry = Depends(get_registry),
    db=Depends(get_db),
):
    """Per-episode placement plan for a series (where each episode is / could be).

    Cheap by default (file state + cached availability). Pass ``?refresh=<instanceId>``
    to interactively re-search that tier's gaps first, or ``?refresh=chain`` to
    sweep the desired tier and its whole fallback chain (bounded + cached).
    """
    if refresh:
        targets: list[str]
        if refresh == "chain":
            plan = await placement.compute_plan(reg, db, tvdb_id=tvdb_id)
            targets = plan["tierPriority"]
        else:
            targets = [refresh]
        for instance_id in targets:
            try:
                await placement.refresh_availability(
                    reg, db, tvdb_id=tvdb_id, instance_id=instance_id
                )
            except KeyError:
                raise HTTPException(status_code=404, detail=f"Unknown instance: {instance_id}")
    return await placement.compute_plan(reg, db, tvdb_id=tvdb_id)


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
