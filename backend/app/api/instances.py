"""Instance list + health, and per-instance profile/root-folder lookups."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.services import health
from app.sonarr.registry import Registry
from app.state import get_registry

router = APIRouter(prefix="/api", tags=["instances"])


def _instance_or_404(reg: Registry, instance_id: str):
    try:
        return reg.get(instance_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown instance: {instance_id}")


@router.get("/instances")
async def list_instances(reg: Registry = Depends(get_registry)):
    return await health.instances_health(reg)


@router.get("/instances/{instance_id}/profiles")
async def quality_profiles(instance_id: str, reg: Registry = Depends(get_registry)):
    return await _instance_or_404(reg, instance_id).client.quality_profiles()


@router.get("/instances/{instance_id}/root-folders")
async def root_folders(instance_id: str, reg: Registry = Depends(get_registry)):
    return await _instance_or_404(reg, instance_id).client.root_folders()
