"""Smart availability check: does a qualifying release actually exist on a tier?

Used by the smart-add flow. A series can always be *added*, but that doesn't
mean any release matching the instance's quality profile exists (e.g. nothing in
4K). Sonarr's interactive search returns candidate releases and flags the ones
that don't meet the profile as ``rejected``. So "available" = at least one
non-rejected release for a representative episode.
"""
from __future__ import annotations

from app.sonarr.registry import Registry


# Default seeder threshold for the gate; 0 disables it (legacy behavior).
DEFAULT_MIN_SEEDERS = 3


def _meets_seeders(release: dict, min_seeders: int) -> bool:
    """The gate only applies to torrents — usenet has no seeders and always
    passes, as do protocol-less releases (older Sonarr payloads, test mocks)."""
    if min_seeders <= 0 or release.get("protocol") != "torrent":
        return True
    return (release.get("seeders") or 0) >= min_seeders


def count_qualifying(releases: list[dict], min_seeders: int = 0) -> int:
    """Number of releases that satisfy the profile (i.e. not rejected) and,
    for torrents, have at least ``min_seeders`` seeders."""
    return sum(
        1 for r in releases
        if not r.get("rejected", False) and _meets_seeders(r, min_seeders)
    )


def seeder_filtered_count(releases: list[dict], min_seeders: int) -> int:
    """Non-rejected releases excluded *purely* by the seeder gate — i.e. they
    would have qualified at min_seeders=0. Used to explain the gate in the UI."""
    return sum(
        1 for r in releases
        if not r.get("rejected", False) and not _meets_seeders(r, min_seeders)
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


def _pick_sample_episode(episodes: list[dict]) -> dict | None:
    """Pick the episode whose availability actually matters.

    Prefer one we still *need* — monitored but with no file — since that's the
    real question ("can a release be had for something missing?"). For a freshly
    added series nothing has a file yet, so this is simply the first monitored
    episode. For an already-populated series (e.g. 4K with only part of S1 on
    disk) it skips the episodes already grabbed and samples a genuine gap, so a
    partially-available series isn't reported "available" off an episode you
    already have. Falls back to any monitored episode, then to anything.
    """
    if not episodes:
        return None
    for ep in episodes:
        if ep.get("monitored") and not ep.get("hasFile"):
            return ep
    for ep in episodes:
        if ep.get("monitored"):
            return ep
    return episodes[0]


async def check_availability(
    registry: Registry, instance_id: str, series_id: int, *, min_seeders: int = 0
) -> dict:
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
            "seederFiltered": 0,
            "rejectionSummary": [],
            "sampledEpisode": None,
        }

    releases = await client.releases(sample["id"])
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
