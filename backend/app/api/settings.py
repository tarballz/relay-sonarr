"""View + edit of non-secret configuration.

Fallback chains are editable from the UI; edits persist as a DB override (the
config.yaml mount is read-only and its API keys are env placeholders, so we never
rewrite the file). Instances themselves stay config/env-defined.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.auth import verify_access
from app.obs import audit, kinds
from app.sonarr.registry import Registry
from app.state import get_db, get_registry
from app.store import settings as settings_store

router = APIRouter(prefix="/api", tags=["settings"])


def _chains_payload(reg: Registry) -> dict:
    """The live chains in the API/override JSON shape (for audit before/after)."""
    return {
        i.id: [
            {"instanceId": s.instanceId, "profile": s.profile, "rootFolder": s.root_folder}
            for s in reg.fallback_chain(i.id)
        ]
        for i in reg.all()
        if reg.has_fallback(i.id)
    }


@router.get("/settings")
async def settings(reg: Registry = Depends(get_registry)):
    """Instance metadata (no API keys) plus the configured fallback pairings."""
    return {
        "instances": [
            {"id": i.id, "name": i.name, "url": i.url} for i in reg.all()
        ],
        "fallbackChains": {
            i.id: [
                {"instanceId": s.instanceId, "profile": s.profile}
                for s in reg.fallback_chain(i.id)
            ]
            for i in reg.all()
            if reg.has_fallback(i.id)
        },
    }


@router.put("/settings/fallback-chains")
async def put_fallback_chains(
    payload: dict[str, list[dict]],
    reg: Registry = Depends(get_registry),
    db=Depends(get_db),
    actor: str | None = Depends(verify_access),
):
    """Replace the fallback chains. Validates every referenced instance, persists
    the override, and applies it to the live registry (no restart needed)."""
    valid = {i.id for i in reg.all()}
    for start, steps in payload.items():
        if start not in valid:
            raise HTTPException(status_code=400, detail=f"Unknown instance: {start}")
        for s in steps:
            if s.get("instanceId") not in valid:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown instance in chain: {s.get('instanceId')}",
                )
    before = _chains_payload(reg)
    await settings_store.set_chain_overrides(db, payload)
    reg.set_chains(Registry.coerce_chains(payload))
    audit.record(kinds.CONFIG_CHAINS_CHANGED, "Fallback chains updated", actor=actor,
                 before=before, after=payload)
    return {"ok": True}
