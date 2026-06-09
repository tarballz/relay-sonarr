"""Global settings (default policy) stored as a JSON row in the meta table."""
from __future__ import annotations

import json

from app.db import Database

_KEY = "default_policy"
_CHAINS_KEY = "fallback_chains_override"


async def get_defaults(db: Database) -> dict:
    row = await db.query_one("SELECT value FROM meta WHERE key=?", (_KEY,))
    return json.loads(row["value"]) if row and row["value"] else {}


async def set_defaults(db: Database, data: dict) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
        (_KEY, json.dumps(data)),
    )


async def get_chain_overrides(db: Database) -> dict | None:
    """UI-edited fallback chains, or None if the user hasn't overridden config.yaml."""
    row = await db.query_one("SELECT value FROM meta WHERE key=?", (_CHAINS_KEY,))
    return json.loads(row["value"]) if row and row["value"] else None


async def set_chain_overrides(db: Database, data: dict) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
        (_CHAINS_KEY, json.dumps(data)),
    )
