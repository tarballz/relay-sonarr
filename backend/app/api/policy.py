"""Per-series policy + pause/resume + global defaults endpoints."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from app.episodes import find_series_by_tvdb
from app.policy import SeriesPolicy, effective_policy, policy_from_chain
from app.sonarr.registry import Registry
from app.state import get_db, get_reconciler, get_registry
from app.store import intents as intent_store
from app.store import settings as settings_store

router = APIRouter(prefix="/api", tags=["policy"])


@router.get("/reconciler/status")
async def reconciler_status(rec=Depends(get_reconciler)):
    """Liveness/observability snapshot of the autonomous loop (last tick, errors)."""
    return rec.status()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _infer_intent_fields(reg: Registry, tvdb_id: int) -> tuple:
    """(chain_key, title) for a series from the live library, preferring a tier
    that has a fallback chain. (None, None) if the series isn't found anywhere."""
    fallback = None
    for inst in reg.all():
        try:
            series = await find_series_by_tvdb(inst.client, tvdb_id)
        except Exception:  # noqa: BLE001 - an offline instance shouldn't 500 us
            continue
        if series:
            if reg.has_fallback(inst.id):
                return inst.id, series.get("title")
            fallback = fallback or (inst.id, series.get("title"))
    return fallback or (None, None)


@router.get("/series/{tvdb_id}/policy")
async def get_policy(tvdb_id: int, reg: Registry = Depends(get_registry), db=Depends(get_db)):
    defaults = await settings_store.get_defaults(db)
    intent = await intent_store.get(db, tvdb_id)
    if intent is not None:
        pol = effective_policy(reg, dict(intent), defaults)
        return {
            "tvdbId": tvdb_id,
            "paused": bool(intent["paused"]),
            "source": "stored" if intent["policy_json"] else "derived",
            "policy": json.loads(pol.to_json()),
        }
    chain_key, _ = await _infer_intent_fields(reg, tvdb_id)
    if chain_key is None:
        raise HTTPException(status_code=404, detail="Series not found on any instance")
    pol = policy_from_chain(
        chain_key, reg.fallback_chain(chain_key),
        allow_split=defaults.get("allowSplit", True),
    )
    return {"tvdbId": tvdb_id, "paused": False, "source": "derived",
            "policy": json.loads(pol.to_json())}


@router.put("/series/{tvdb_id}/policy")
async def put_policy(tvdb_id: int, policy: SeriesPolicy, db=Depends(get_db)):
    now = _now()
    await intent_store.ensure(db, tvdb_id=tvdb_id, title=None,
                              chain_key=policy.preferredTier, now=now)
    await intent_store.set_policy(db, tvdb_id, policy.to_json(), now)
    return {"ok": True, "tvdbId": tvdb_id}


async def _ensure_intent(reg: Registry, db, tvdb_id: int) -> None:
    if await intent_store.get(db, tvdb_id) is not None:
        return
    chain_key, title = await _infer_intent_fields(reg, tvdb_id)
    if chain_key is None:
        raise HTTPException(status_code=404, detail="Series not found on any instance")
    await intent_store.ensure(db, tvdb_id=tvdb_id, title=title, chain_key=chain_key, now=_now())


@router.post("/series/{tvdb_id}/pause")
async def pause(tvdb_id: int, reg: Registry = Depends(get_registry), db=Depends(get_db)):
    await _ensure_intent(reg, db, tvdb_id)
    await intent_store.set_paused(db, tvdb_id, True, _now())
    return {"ok": True, "tvdbId": tvdb_id, "paused": True}


@router.post("/series/{tvdb_id}/resume")
async def resume(tvdb_id: int, reg: Registry = Depends(get_registry), db=Depends(get_db)):
    await _ensure_intent(reg, db, tvdb_id)
    await intent_store.set_paused(db, tvdb_id, False, _now())
    return {"ok": True, "tvdbId": tvdb_id, "paused": False}


@router.get("/settings/defaults")
async def get_defaults(db=Depends(get_db)):
    return await settings_store.get_defaults(db)


@router.put("/settings/defaults")
async def put_defaults(defaults: dict, db=Depends(get_db)):
    await settings_store.set_defaults(db, defaults)
    return {"ok": True}
