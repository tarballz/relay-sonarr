"""Event repo: the persisted journal, queried newest-first with keyset paging."""
from __future__ import annotations

import json

from app.db import Database

_COLUMNS = ("ts", "kind", "level", "source", "tick_id", "operation_id", "tvdb_id",
            "season", "episode", "instance_id", "message", "data_json")

INSERT_SQL = (
    f"INSERT INTO event({', '.join(_COLUMNS)}) "
    f"VALUES({', '.join('?' * len(_COLUMNS))})"
)

LEVEL_ORDER = ("debug", "info", "warn", "error")


def _shape(event_id, v: dict) -> dict:
    return {
        "id": event_id,
        "ts": v["ts"],
        "kind": v["kind"],
        "level": v["level"],
        "source": v["source"],
        "tickId": v["tick_id"],
        "operationId": v["operation_id"],
        "tvdbId": v["tvdb_id"],
        "season": v["season"],
        "episode": v["episode"],
        "instanceId": v["instance_id"],
        "message": v["message"],
        "data": json.loads(v["data_json"]) if v["data_json"] else None,
    }


def from_row_tuple(event_id, row: tuple) -> dict:
    """Shape an INSERT_SQL parameter tuple (plus its assigned id) as an event dict."""
    return _shape(event_id, dict(zip(_COLUMNS, row)))


def to_dict(row) -> dict:
    return _shape(row["id"], {c: row[c] for c in _COLUMNS})


def _like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


async def query(db: Database, *, kind: str | None = None, level: str | None = None,
                tvdb_id: int | None = None, tick_id: int | None = None,
                source: str | None = None, before: int | None = None,
                limit: int = 100) -> list[dict]:
    """Newest-first events. ``kind`` is a prefix; ``level`` is a minimum."""
    where: list[str] = []
    params: list = []
    if kind:
        where.append("kind LIKE ? ESCAPE '\\'")
        params.append(_like_prefix(kind))
    if level:
        if level not in LEVEL_ORDER:
            raise ValueError(f"unknown level: {level}")
        allowed = LEVEL_ORDER[LEVEL_ORDER.index(level):]
        where.append(f"level IN ({', '.join('?' * len(allowed))})")
        params.extend(allowed)
    for column, value in (("tvdb_id", tvdb_id), ("tick_id", tick_id), ("source", source)):
        if value is not None:
            where.append(f"{column}=?")
            params.append(value)
    if before is not None:
        where.append("id < ?")
        params.append(before)
    sql = "SELECT * FROM event"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [to_dict(r) for r in await db.query(sql, tuple(params))]


async def since(db: Database, after_id: int, limit: int) -> list[dict]:
    rows = await db.query(
        "SELECT * FROM event WHERE id > ? ORDER BY id ASC LIMIT ?", (after_id, limit)
    )
    return [to_dict(r) for r in rows]


async def for_tick(db: Database, tick_id: int) -> list[dict]:
    rows = await db.query("SELECT * FROM event WHERE tick_id=? ORDER BY id ASC", (tick_id,))
    return [to_dict(r) for r in rows]


async def latest_id(db: Database) -> int | None:
    """The highest event id, or ``None`` if the journal is empty."""
    row = await db.query_one("SELECT MAX(id) AS id FROM event")
    return row["id"] if row and row["id"] is not None else None
