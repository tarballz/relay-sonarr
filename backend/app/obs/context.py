"""Correlation context carried implicitly through async code.

asyncio tasks (and ``gather`` children) copy the current context, so anything
logged or journaled inside a tick automatically carries that tick's id.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

tick_id: ContextVar[int | None] = ContextVar("tick_id", default=None)
operation_id: ContextVar[int | None] = ContextVar("operation_id", default=None)
tvdb_id: ContextVar[int | None] = ContextVar("tvdb_id", default=None)
tick_stats: ContextVar[object | None] = ContextVar("tick_stats", default=None)

_VARS = {
    "tick_id": tick_id,
    "operation_id": operation_id,
    "tvdb_id": tvdb_id,
    "tick_stats": tick_stats,
}


@contextmanager
def bind(**values):
    """Set context values for the duration of the block, restoring them after."""
    tokens = []
    try:
        for name, value in values.items():
            var = _VARS[name]
            tokens.append((var, var.set(value)))
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def current() -> dict:
    return {"tick_id": tick_id.get(), "operation_id": operation_id.get(), "tvdb_id": tvdb_id.get()}
