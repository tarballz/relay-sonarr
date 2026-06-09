"""Durable operation log — the SQLite-backed replacement for OperationLog.

Same conceptual surface as the old in-memory deque, but methods are async and
state survives restarts. ``recent()`` returns the exact dict shape the Operations
page already consumes ({id, kind, title, tvdbId, startedAt, steps[], result,
error, finishedAt}), so the frontend is untouched. The old "return a mutable dict
and mutate it" API is replaced by explicit ``add_step``/``finish`` calls.
"""
from __future__ import annotations

import json

from app.db import Database


class OperationStore:
    def __init__(self, db: Database):
        self.db = db

    async def start(self, *, kind: str, title: str, tvdb_id: int, started_at: str,
                    source: str = "user") -> int:
        """Create an operation row; returns its id for add_step/finish."""
        return await self.db.execute(
            "INSERT INTO operation(kind, title, tvdb_id, source, started_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (kind, title, tvdb_id, source, started_at),
        )

    async def add_step(self, op_id: int, event: dict) -> None:
        """Append one step event (the same dict that streams to the UI)."""
        row = await self.db.query_one(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM operation_step WHERE operation_id=?",
            (op_id,),
        )
        await self.db.execute(
            "INSERT INTO operation_step(operation_id, seq, event_json) VALUES(?, ?, ?)",
            (op_id, row["n"], json.dumps(event)),
        )

    async def finish(self, op_id: int, *, result=None, error: str | None = None,
                     finished_at: str) -> None:
        await self.db.execute(
            "UPDATE operation SET result_json=?, error=?, finished_at=? WHERE id=?",
            (json.dumps(result) if result is not None else None, error, finished_at, op_id),
        )

    async def recent(self, limit: int = 200) -> list[dict]:
        """All retained operations, newest first, in the UI's expected shape."""
        ops = await self.db.query(
            "SELECT * FROM operation ORDER BY id DESC LIMIT ?", (limit,)
        )
        out: list[dict] = []
        for op in ops:
            steps = await self.db.query(
                "SELECT event_json FROM operation_step WHERE operation_id=? ORDER BY seq",
                (op["id"],),
            )
            out.append({
                "id": op["id"],
                "kind": op["kind"],
                "title": op["title"],
                "tvdbId": op["tvdb_id"],
                "startedAt": op["started_at"],
                "steps": [json.loads(s["event_json"]) for s in steps],
                "result": json.loads(op["result_json"]) if op["result_json"] else None,
                "error": op["error"],
                "finishedAt": op["finished_at"],
                "source": op["source"],
            })
        return out
