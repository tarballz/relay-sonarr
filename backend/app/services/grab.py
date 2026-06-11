"""Seeder-first release selection: grab the healthiest torrent directly.

Sonarr's own decision engine ranks by quality/format first and uses seeders
only as a tiebreaker, so a SeriesSearch can happily grab a 1-seeder release.
``grab_then_search`` takes the releases an interactive search already returned,
grabs the best-seeded qualifying torrent via POST /release, and then still
fires the normal search command — so every failure mode (grab rejected, no
torrent candidate, feature disabled) degrades to exactly the old behavior.
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


def pick_best_torrent(releases: list[dict], min_seeders: int = 0) -> dict | None:
    """The non-rejected torrent with the most seeders, or None.

    Stable sort on seeders only: Sonarr returns releases pre-sorted by its own
    preference (quality profile, format score), so ties keep that ranking —
    no need to reimplement quality weighting here.
    """
    candidates = [
        r for r in releases
        if not r.get("rejected", False)
        and r.get("protocol") == "torrent"
        and (r.get("seeders") or 0) >= max(min_seeders, 1)
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda r: -(r.get("seeders") or 0))[0]


async def grab_then_search(
    client,
    *,
    releases: list[dict],
    series_id: int | None = None,
    episode_ids: list[int] | None = None,
    min_seeders: int = 0,
    enabled: bool = True,
) -> dict:
    """Grab the best-seeded torrent, then fire the search command regardless.

    The command (SeriesSearch if ``series_id``, EpisodeSearch if ``episode_ids``,
    none if neither — for callers that already fired their own search) is the
    safety net: it always runs, so a failed or skipped grab costs nothing.
    Sonarr's queue-aware decision engine skips episodes the grab already covers.
    """
    grabbed = False
    best = pick_best_torrent(releases, min_seeders) if enabled else None
    if best is not None:
        try:
            await client.grab_release(best["guid"], best["indexerId"])
            grabbed = True
        except httpx.HTTPError as exc:
            logger.warning(
                "direct grab of %r failed (%s); falling back to search",
                best.get("title"), exc,
            )

    searched = False
    if series_id is not None:
        await client.command("SeriesSearch", seriesId=series_id)
        searched = True
    elif episode_ids:
        await client.command("EpisodeSearch", episodeIds=episode_ids)
        searched = True

    return {"grabbed": grabbed, "release": best if grabbed else None, "searched": searched}
