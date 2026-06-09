"""Pydantic request/response models for the API layer."""
from __future__ import annotations

from pydantic import BaseModel


class AddTarget(BaseModel):
    instanceId: str
    qualityProfileId: int
    rootFolderPath: str
    monitored: bool = True
    searchNow: bool = True
    # None ⇒ monitor all seasons; a list ⇒ monitor only those season numbers.
    monitoredSeasons: list[int] | None = None


class AddRequest(BaseModel):
    tvdbId: int
    targets: list[AddTarget]


class SmartAddRequest(BaseModel):
    tvdbId: int
    targetInstanceId: str
    targetQualityProfileId: int
    targetRootFolderPath: str
    monitored: bool = True
    # None ⇒ monitor all seasons; a list ⇒ monitor only those season numbers.
    monitoredSeasons: list[int] | None = None


class AdvanceFallbackRequest(BaseModel):
    """The opaque state the UI echoes back to walk to the next chain step."""

    tvdbId: int
    fromInstanceId: str
    fromSeriesId: int
    chainKey: str
    nextIndex: int


class SpillSeasonRequest(BaseModel):
    """Fetch one whole season from a lower tier (the per-season 'lower res' action)."""

    tvdbId: int
    season: int
    originInstanceId: str
    fallbackInstanceId: str
    profile: str | None = None
    root: str | None = None
