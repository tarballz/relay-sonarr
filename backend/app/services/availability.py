"""Smart availability check: does a qualifying release actually exist on a tier?

Used by the smart-add flow. A series can always be *added*, but that doesn't
mean any release matching the instance's quality profile exists (e.g. nothing in
4K). Sonarr's interactive search returns candidate releases and flags the ones
that don't meet the profile as ``rejected``. So "available" = at least one
non-rejected release for a representative episode.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime

from app.sonarr.registry import Registry


# Default seeder threshold for the gate; 0 disables it (legacy behavior).
DEFAULT_MIN_SEEDERS = 5

# How many episodes to sample before calling a series unavailable. One is not
# enough — see ``_pick_sample_episodes``.
DEFAULT_SAMPLE_LIMIT = 3

# How long an episode may sit wanted before the floor gives way to
# "best available". Old back-catalogue legitimately tops out at a handful of
# seeders, so a permanent hard floor would strand it forever.
DEFAULT_RELAX_AFTER_DAYS = 3.0

# Release titles ending in an executable extension are malware fakes (seen
# live: "From.S04E06.1080p.WEB.h264-ETH.scr"). Seeder counts don't catch these
# — attackers fake healthy swarms — but the payload extension does.
DANGEROUS_EXTENSIONS = (".exe", ".scr", ".bat", ".cmd", ".msi", ".pif",
                        ".vbs", ".ps1", ".lnk", ".jar")


# "[SelfExtract Bundle]", "Self-Extracting", "self extract": an executable that
# unpacks itself. Live 2026-11-09: South Park S29E05, grabbed 13 days pre-air.
_SELF_EXTRACT = re.compile(r"self[\s._+-]?extract")


def looks_dangerous(release: dict) -> bool:
    """True when the release/queue-item title is an executable fake."""
    title = (release.get("title") or "").strip().lower()
    return title.endswith(DANGEROUS_EXTENSIONS) or bool(_SELF_EXTRACT.search(title))


def effective_min_seeders(*, min_seeders: int, wanted_since: str | None,
                          now: datetime, relax_after_days: float) -> int:
    """The seeder floor to apply to one episode right now.

    ``min_seeders`` while the episode is fresh, then 1 — "the best that exists" —
    once it has been wanted longer than ``relax_after_days``. Preferring a
    healthy release when one exists, and accepting a thin one when none does,
    is the difference between a library that fills and one that stalls.

    ``relax_after_days <= 0`` disables relaxation entirely.
    """
    if relax_after_days <= 0 or not wanted_since:
        return min_seeders
    try:
        since = datetime.fromisoformat(str(wanted_since).replace("Z", "+00:00"))
    except ValueError:
        return min_seeders
    if (now - since).total_seconds() > relax_after_days * 86400.0:
        return 1
    return min_seeders


def _meets_seeders(release: dict, min_seeders: int) -> bool:
    """The gate only applies to torrents — usenet has no seeders and always
    passes, as do protocol-less releases (older Sonarr payloads, test mocks)."""
    if min_seeders <= 0 or release.get("protocol") != "torrent":
        return True
    return (release.get("seeders") or 0) >= min_seeders


def count_qualifying(releases: list[dict], min_seeders: int = 0) -> int:
    """Number of releases that satisfy the profile (i.e. not rejected), aren't
    executable fakes, and — for torrents — have at least ``min_seeders`` seeders."""
    return sum(
        1 for r in releases
        if not r.get("rejected", False)
        and not looks_dangerous(r)
        and _meets_seeders(r, min_seeders)
    )


def seeder_filtered_count(releases: list[dict], min_seeders: int) -> int:
    """Non-rejected releases excluded *purely* by the seeder gate — i.e. they
    would have qualified at min_seeders=0. Used to explain the gate in the UI."""
    return sum(
        1 for r in releases
        if not r.get("rejected", False)
        and not looks_dangerous(r)
        and not _meets_seeders(r, min_seeders)
    )


def _bucket(reasons: list[str]) -> str:
    """Collapse a release's raw rejection strings into one friendly label."""
    text = " ".join(reasons)
    if "Unknown Series" in text:
        return "wrong series"
    if "seeder" in text.lower():
        return "too few seeders"
    if "not wanted in profile" in text:
        return "quality not allowed"
    return reasons[0] if reasons else "rejected"


def summarize_rejections(releases: list[dict]) -> list[dict]:
    """Bucket why rejected releases were rejected, as ``[{reason, count}]``.

    Turns Sonarr's per-release rejection strings into a short, human summary so
    the UI can explain *why* nothing qualified (e.g. wrong series / seeders)
    rather than just saying "no release".
    """
    counts: dict[str, int] = {}
    for release in releases:
        if not release.get("rejected", False):
            continue
        label = _bucket(release.get("rejections") or [])
        counts[label] = counts.get(label, 0) + 1
    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(counts.items(), key=lambda kv: -kv[1])
    ]


def _sample_pool(episodes: list[dict]) -> list[dict]:
    """The episodes worth asking about, best candidates first.

    Prefer ones we still *need* — monitored but with no file — since that's the
    real question ("can a release be had for something missing?"). For a freshly
    added series nothing has a file yet, so this is simply the monitored
    episodes. For an already-populated series (e.g. 4K with only part of S1 on
    disk) it skips the episodes already grabbed and samples genuine gaps, so a
    partially-available series isn't reported "available" off an episode you
    already have. Falls back to any monitored episode, then to anything.
    """
    gaps = [ep for ep in episodes if ep.get("monitored") and not ep.get("hasFile")]
    if gaps:
        return gaps
    monitored = [ep for ep in episodes if ep.get("monitored")]
    return monitored or list(episodes)


def _pick_sample_episodes(
    episodes: list[dict], limit: int = DEFAULT_SAMPLE_LIMIT,
) -> list[dict]:
    """Pick up to ``limit`` episodes to search, spread across the series.

    One episode's verdict is a terrible proxy for a whole series. Sonarr rejects
    a release it can't map to the series as ``Unknown Series``, and whether that
    happens depends on the *release title* — which varies episode by episode,
    group by group. Live case: Jackass's sampled episode returned 7 releases,
    all ``Unknown Series``, so the add flow declared the series empty; the
    reconciler, which searches every episode separately, grabbed 25 of them an
    hour later.

    The picks are spread rather than consecutive because neighbouring episodes
    come from the same release groups and share the same naming fate — three
    neighbours are barely more informative than one.
    """
    pool = _sample_pool(episodes)
    if not pool or limit <= 0:
        return []
    if len(pool) <= limit or limit == 1:
        return pool[:limit]
    step = (len(pool) - 1) / (limit - 1)
    return [pool[round(i * step)] for i in range(limit)]


def _pick_sample_episode(episodes: list[dict]) -> dict | None:
    """The single best episode to sample (the head of the spread)."""
    picks = _pick_sample_episodes(episodes, limit=1)
    return picks[0] if picks else None


def _verdict(instance_id: str, sample: dict, releases: list[dict],
             min_seeders: int) -> dict:
    """Summarize one episode's interactive search."""
    qualifying = count_qualifying(releases, min_seeders)
    filtered = seeder_filtered_count(releases, min_seeders)
    rejections = summarize_rejections(releases)
    if filtered > 0:
        # These releases aren't Sonarr-rejected, so summarize_rejections can't
        # see them — append our gate's exclusions so the UI can explain them.
        rejections.append(
            {"reason": f"fewer than {min_seeders} seeders", "count": filtered}
        )
    return {
        "instanceId": instance_id,
        "available": qualifying > 0,
        "releaseCount": qualifying,
        "totalReleases": len(releases),
        "seederFiltered": filtered,
        "rejectionSummary": rejections,
        "sampledEpisode": {"id": sample["id"], "title": sample.get("title")},
    }


async def check_availability(
    registry: Registry, instance_id: str, series_id: int, *,
    min_seeders: int = 0, include_releases: bool = False,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
) -> dict:
    """Run interactive release searches on a few sample episodes of the series.

    Assumes the series already exists on the instance and its episodes have been
    populated (the smart-add orchestration handles add + refresh first).

    The first sample is searched alone: when it qualifies — the common "yes"
    — the answer costs exactly one search, as it always did. Only a *miss* buys
    a second opinion, because a miss is the answer this check used to get wrong
    (see ``_pick_sample_episodes``). The remaining samples then run concurrently
    so a false negative costs about two search timeouts, not ``sample_limit``.

    ``include_releases`` adds the raw release list to the payload so a caller
    can reuse this search (e.g. for a direct grab) instead of hitting the
    indexers again — callers must pop it before emitting the dict to the UI.
    It always belongs to the episode that decided the verdict.
    """
    client = registry.get(instance_id).client
    episodes = await client.episodes(series_id)
    samples = _pick_sample_episodes(episodes, sample_limit)
    if not samples:
        return {
            "instanceId": instance_id,
            "available": False,
            "releaseCount": 0,
            "totalReleases": 0,
            "seederFiltered": 0,
            "rejectionSummary": [],
            "releasesExist": False,
            "sampledEpisode": None,
            "sampledEpisodes": [],
        }

    async def probe(sample: dict) -> tuple[dict, list[dict]]:
        releases = await client.releases(sample["id"])
        return _verdict(instance_id, sample, releases, min_seeders), releases

    probed = [await probe(samples[0])]
    if not probed[0][0]["available"] and len(samples) > 1:
        rest = await asyncio.gather(
            *(probe(s) for s in samples[1:]), return_exceptions=True,
        )
        # A probe that times out simply doesn't get a vote — one slow indexer
        # must not fail an add that the first sample already answered.
        probed.extend(p for p in rest if not isinstance(p, BaseException))

    # The verdict comes from the first episode that qualified; failing that,
    # from the most informative miss, so the UI explains the richest rejection
    # set rather than whichever episode happened to be sampled first.
    decided, releases = next(
        ((v, r) for v, r in probed if v["available"]),
        max(probed, key=lambda p: p[0]["totalReleases"]),
    )
    result = dict(decided)
    result["releasesExist"] = any(v["totalReleases"] > 0 for v, _ in probed)
    result["sampledEpisodes"] = [v["sampledEpisode"] for v, _ in probed]
    if include_releases:
        result["releases"] = releases
    return result
