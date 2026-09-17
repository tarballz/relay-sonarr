"""Per-instance health for the dashboard's instance list."""
from __future__ import annotations

from app.services.fanout import gather_instances
from app.sonarr.registry import Registry


# Sonarr names the failing indexers in the health message itself; there is no
# structured endpoint for it (/api/v3/indexerstatus is 404 on v4). Both the
# short-term and long-term checks use "...: name, name", so the names are the
# text after the last colon.
_INDEXER_FAILURE_SOURCES = ("IndexerStatusCheck", "IndexerLongTermStatusCheck")
# Sonarr's distinct "there is nothing left to search with" signal.
_NO_INDEXERS = "no indexers available"


def failing_indexers(health: list[dict]) -> set[str]:
    """Names of indexers Sonarr currently considers failed, from its health checks."""
    names: set[str] = set()
    for item in health or []:
        if (item.get("source") or "") not in _INDEXER_FAILURE_SOURCES:
            continue
        message = item.get("message") or ""
        _, _, listed = message.rpartition(":")
        for name in listed.split(","):
            name = name.strip()
            if name:
                names.add(name)
    return names


def indexers_degraded(health: list[dict], *, interactive_count: int | None) -> bool:
    """True only when this instance has *no* working way to search.

    An interactive search against a dead indexer set returns quickly with zero
    releases, indistinguishable from "no release exists". But public-tracker
    indexers cycle in and out of failure constantly, so "any indexer failing"
    describes the normal state and is useless as a gate — used that way it fired
    on every tick, discarded ~9000 searches a day and froze the availability
    cache, because a discarded verdict also never repairs the stale one.

    So this is deliberately strict: every interactive-search indexer must be
    failing, or Sonarr must be reporting none available at all. When capacity
    cannot be established (``interactive_count`` is None) the answer is False —
    under-triggering merely keeps the old caching behavior, while
    over-triggering breaks the repair path.
    """
    for item in health or []:
        if _NO_INDEXERS in (item.get("message") or "").lower():
            return True
    if interactive_count is None:
        return False
    if interactive_count == 0:
        return True
    return len(failing_indexers(health)) >= interactive_count


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
