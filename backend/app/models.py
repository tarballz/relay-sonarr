"""Pydantic request/response models for the API layer."""
from __future__ import annotations

from pydantic import BaseModel


class AddTarget(BaseModel):
    instanceId: str
    qualityProfileId: int
    rootFolderPath: str
    monitored: bool = True
    searchNow: bool = True


class AddRequest(BaseModel):
    tvdbId: int
    targets: list[AddTarget]


class SmartAddRequest(BaseModel):
    tvdbId: int
    targetInstanceId: str
    targetQualityProfileId: int
    targetRootFolderPath: str
    monitored: bool = True


class AdvanceFallbackRequest(BaseModel):
    """The opaque state the UI echoes back to walk to the next chain step."""

    tvdbId: int
    fromInstanceId: str
    fromSeriesId: int
    chainKey: str
    nextIndex: int
