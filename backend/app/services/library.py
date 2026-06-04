"""Combined library view across all instances."""
from __future__ import annotations

from app.services.fanout import gather_instances
from app.sonarr.registry import Registry


async def combined_series(registry: Registry) -> list[dict]:
    """All series across every instance, each tagged with its origin instance."""
    out: list[dict] = []
    for inst, res in await gather_instances(registry, lambda i: i.client.list_series()):
        if isinstance(res, Exception):
            continue
        for series in res:
            out.append({**series, "instanceId": inst.id, "instanceName": inst.name})
    return out
