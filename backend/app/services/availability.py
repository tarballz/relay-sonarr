"""Smart availability check: does a qualifying release actually exist on a tier?

Used by the smart-add flow. A series can always be *added*, but that doesn't
mean any release matching the instance's quality profile exists (e.g. nothing in
4K). Sonarr's interactive search returns candidate releases and flags the ones
that don't meet the profile as ``rejected``. So "available" = at least one
non-rejected release for a representative episode.
"""
from __future__ import annotations

from app.sonarr.registry import Registry


def count_qualifying(releases: list[dict]) -> int:
    """Number of releases that satisfy the profile (i.e. not rejected)."""
    return sum(1 for r in releases if not r.get("rejected", False))


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


def _pick_sample_episode(episodes: list[dict]) -> dict | None:
    """Prefer a monitored episode; fall back to the first episode of any kind."""
    if not episodes:
        return None
    for ep in episodes:
        if ep.get("monitored"):
            return ep
    return episodes[0]


async def check_availability(registry: Registry, instance_id: str, series_id: int) -> dict:
    """Run an interactive release search on a sample episode of the series.

    Assumes the series already exists on the instance and its episodes have been
    populated (the smart-add orchestration handles add + refresh first).
    """
    client = registry.get(instance_id).client
    episodes = await client.episodes(series_id)
    sample = _pick_sample_episode(episodes)
    if sample is None:
        return {
            "instanceId": instance_id,
            "available": False,
            "releaseCount": 0,
            "totalReleases": 0,
            "rejectionSummary": [],
            "sampledEpisode": None,
        }

    releases = await client.releases(sample["id"])
    qualifying = count_qualifying(releases)
    return {
        "instanceId": instance_id,
        "available": qualifying > 0,
        "releaseCount": qualifying,
        "totalReleases": len(releases),
        "rejectionSummary": summarize_rejections(releases),
        "sampledEpisode": {"id": sample["id"], "title": sample.get("title")},
    }
