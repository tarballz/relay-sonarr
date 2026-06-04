"""Per-instance health for the dashboard's instance list."""
from __future__ import annotations

from app.services.fanout import gather_instances
from app.sonarr.registry import Registry


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
            }
        )
    return out
