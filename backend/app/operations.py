"""In-memory log of recent smart-add / fallback operations.

Each operation record accumulates the same step events that stream to the UI, so
the Operations page can replay what happened behind the scenes. Bounded to the
last N operations; lost on restart (no persistence needed for this).
"""
from __future__ import annotations

from collections import deque


class OperationLog:
    def __init__(self, maxlen: int = 50):
        self._ops: deque[dict] = deque(maxlen=maxlen)
        self._next_id = 1

    def start(self, *, kind: str, title: str, tvdb_id: int, started_at: str) -> dict:
        """Create and store a new operation record, returning it for in-place updates.

        The returned dict is the same object held in the log, so callers append to
        ``steps`` and set ``result``/``error``/``finishedAt`` as the op progresses.
        """
        op = {
            "id": self._next_id,
            "kind": kind,
            "title": title,
            "tvdbId": tvdb_id,
            "startedAt": started_at,
            "steps": [],
            "result": None,
            "error": None,
            "finishedAt": None,
        }
        self._next_id += 1
        self._ops.append(op)
        return op

    def recent(self) -> list[dict]:
        """All retained operations, newest first."""
        return list(reversed(self._ops))
