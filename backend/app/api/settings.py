"""Read-only view of non-secret configuration."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.sonarr.registry import Registry
from app.state import get_registry

router = APIRouter(prefix="/api", tags=["settings"])


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
