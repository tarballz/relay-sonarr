"""Download-progress repo: how long each torrent has been sitting at the same size.

Sonarr's queue record carries ``size``/``sizeleft`` but no activity timestamp, so a
torrent frozen at 46% looks identical to one actively downloading. Comparing
``sizeleft`` against the previous tick is the only way to tell them apart, which
makes stall detection stateful — hence this table.

Keyed on ``downloadId`` (the torrent), not the queue-record id: a season pack
produces one queue record per episode, all sharing a single torrent.
"""
from __future__ import annotations

from datetime import datetime

from app.db import Database


async def observe(db: Database, *, download_id: str, sizeleft: int,
                  now: datetime) -> str:
    """Record ``sizeleft`` for a torrent; return when it last changed.

    Returns the ISO timestamp since which ``sizeleft`` has been unchanged. On the
    first sighting that is ``now`` — a torrent is never judged stalled on the
    strength of a single observation.
    """
    row = await db.query_one(
        "SELECT sizeleft, unchanged_since FROM download_progress WHERE download_id=?",
        (download_id,),
    )
    if row is not None and row["sizeleft"] == sizeleft and row["unchanged_since"]:
        return str(row["unchanged_since"])
    stamp = now.isoformat()
    await db.execute(
        "INSERT OR REPLACE INTO download_progress(download_id, sizeleft, unchanged_since) "
        "VALUES(?, ?, ?)",
        (download_id, sizeleft, stamp),
    )
    return stamp


async def forget(db: Database, download_id: str) -> None:
    """Drop a torrent's row once it's been removed or has finished."""
    await db.execute("DELETE FROM download_progress WHERE download_id=?", (download_id,))


def stalled_since(unchanged_since: str | None, *, now: datetime,
                  stalled_days: float) -> bool:
    """True when ``sizeleft`` hasn't moved for longer than the threshold."""
    if not unchanged_since:
        return False
    try:
        seen = datetime.fromisoformat(str(unchanged_since).replace("Z", "+00:00"))
    except ValueError:
        return False
    return (now - seen).total_seconds() > stalled_days * 86400.0
