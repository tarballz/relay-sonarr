"""Shared episode helpers used by orchestration, the placement engine, and poller.

Episode identity is ALWAYS ``(seasonNumber, episodeNumber)``, never a per-instance
episode id — ids differ across Sonarr instances, season/episode is stable.
"""
from __future__ import annotations


def episode_key(ep: dict) -> tuple:
    """Cross-instance episode identity: (season, episode)."""
    return (ep.get("seasonNumber"), ep.get("episodeNumber"))


def is_gap(ep: dict) -> bool:
    """An episode we still need: monitored and with no file on disk."""
    return bool(ep.get("monitored")) and not ep.get("hasFile")


async def find_series_by_tvdb(client, tvdb_id: int) -> dict | None:
    """The series on an instance matching ``tvdb_id``, or None if absent."""
    for series in await client.list_series():
        if series.get("tvdbId") == tvdb_id:
            return series
    return None
