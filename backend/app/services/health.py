"""Per-instance health for the dashboard's instance list."""
from __future__ import annotations

from app.services.fanout import gather_instances
from app.sonarr.registry import Registry


# Sonarr reports indexer trouble through these checks. Anything whose source
# starts with "Indexer" is about the search path; everything else (root folders,
# updates, download clients) says nothing about whether a search was answerable.
_FAILING = ("warning", "error")


def indexers_degraded(health: list[dict]) -> bool:
    """True when Sonarr is reporting indexer trouble on this instance.

    An interactive search against a degraded indexer set returns quickly with
    zero releases, which is indistinguishable from "no release exists" — and on
    public trackers behind rate limits and Cloudflare, indexers cycle in and out
    of failure constantly. Callers use this to tell the two apart before
    recording an empty result as a fact about the episode.
    """
    for item in health or []:
        source = item.get("source") or ""
        if source.startswith("Indexer") and (item.get("type") or "").lower() in _FAILING:
            return True
    return False


async def instances_health(registry: Registry) -> list[dict]:
    """Report each instance's id/name/url plus live online state and version."""
    out: list[dict] = []
    for inst, res in await gather_instances(registry, lambda i: i.client.system_status()):
        online = not isinstance(res, Exception)
        out.append(
            {
                "id": inst.id,
                "name": inst.name,
                "url": inst.url,
                "online": online,
                "version": res.get("version") if online else None,
                # Lets the Add dialog pre-select the right root folder instead of
                # defaulting to whichever one Sonarr lists first.
                "defaultRootFolder": inst.default_root_folder,
            }
        )
    return out
