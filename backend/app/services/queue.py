"""Combined download queue / activity across all instances."""
from __future__ import annotations

from app.services.fanout import gather_instances
from app.sonarr.registry import Registry


async def combined_queue(registry: Registry) -> list[dict]:
    """Flatten every instance's queue records into one list, tagged by instance."""
    out: list[dict] = []
    for inst, res in await gather_instances(registry, lambda i: i.client.queue()):
        if isinstance(res, Exception):
            continue
        for record in res.get("records", []):
            out.append({**record, "instanceId": inst.id, "instanceName": inst.name})
    return out
