"""Concurrent fan-out across all registered instances.

Every aggregation in the app follows the same shape: call one coroutine per
instance at the same time, then decide per-instance how to handle success vs.
failure. Centralizing it here means a single instance being down degrades the
view (that instance is skipped/marked offline) instead of failing the request.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from app.sonarr.registry import Instance, Registry


async def gather_instances(
    registry: Registry, fn: Callable[[Instance], Awaitable]
) -> list[tuple[Instance, object]]:
    """Run ``fn`` against every instance concurrently.

    Returns ``(instance, result)`` pairs in registry order. If ``fn`` raised for
    an instance, that pair's result is the Exception (callers inspect with
    ``isinstance(result, Exception)``), so one failure never sinks the rest.
    """
    instances = registry.all()
    results = await asyncio.gather(
        *(fn(inst) for inst in instances), return_exceptions=True
    )
    return list(zip(instances, results))
