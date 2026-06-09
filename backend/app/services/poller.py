"""Outcome tracking by polling Sonarr (no webhooks — Relay can't be reached inbound).

Reads each instance's queue + recent history and advances ``placement`` rows
through the real download lifecycle: grabbed → importing → imported, or → failed
(with a backoff ``next_retry_at`` the reconciler later acts on). Correlation keys
on ``(tvdb_id, season, episode)`` via a seriesId→tvdbId map, never per-instance
episode ids. Only episodes already in a plan are touched; everything else is
ignored. Idempotent: re-applying the same history is a no-op.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.fanout import gather_instances
from app.sonarr.registry import Instance, Registry
from app.store import placements as place_store

EVENT_IMPORTED = "downloadFolderImported"
EVENT_FAILED = "downloadFailed"
EVENT_GRABBED = "grabbed"

DEFAULT_RETRY_BASE = 900       # 15 min
DEFAULT_RETRY_MAX = 24 * 3600  # 1 day


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _queue_state(record: dict) -> str:
    tds = (record.get("trackedDownloadState") or "").lower()
    if tds in ("importpending", "importing"):
        return "importing"
    if tds == "imported":
        return "imported"
    return "grabbed"  # queued / downloading / delay


def _ep_key(record: dict):
    """(tvdb_seriesId-less) season/episode from a queue or history record."""
    ep = record.get("episode") or {}
    return ep.get("seasonNumber"), ep.get("episodeNumber")


async def poll_instance(
    registry: Registry,
    db,
    instance: Instance,
    *,
    history_limit: int = 200,
    now: datetime | None = None,
    retry_base: float = DEFAULT_RETRY_BASE,
    retry_max: float = DEFAULT_RETRY_MAX,
) -> list[dict]:
    """Advance placement state for one instance from its queue + history.

    Returns the list of transitions applied (for the caller to summarize/log).
    """
    now = _now(now)
    client = instance.client
    id_to_tvdb = {s["id"]: s.get("tvdbId") for s in await client.list_series()}

    # In-flight downloads (current truth).
    queue_state: dict[tuple, tuple] = {}
    q = await client.queue()
    for rec in q.get("records", []):
        tvdb = id_to_tvdb.get(rec.get("seriesId"))
        season, epnum = _ep_key(rec)
        if tvdb is None or season is None or epnum is None:
            continue
        queue_state[(tvdb, season, epnum)] = (_queue_state(rec), rec.get("downloadId"))

    # Most recent terminal event per episode (history is newest-first).
    latest_event: dict[tuple, tuple] = {}
    hist = await client.history(page_size=history_limit)
    for rec in hist.get("records", []):
        tvdb = id_to_tvdb.get(rec.get("seriesId"))
        season, epnum = _ep_key(rec)
        if tvdb is None or season is None or epnum is None:
            continue
        latest_event.setdefault((tvdb, season, epnum), (rec.get("eventType"), rec.get("downloadId")))

    transitions: list[dict] = []
    for key in set(queue_state) | set(latest_event):
        tvdb, season, epnum = key
        row = await place_store.get(db, tvdb, season, epnum)
        if row is None:
            continue  # only track episodes already in a plan

        event = latest_event.get(key, (None, None))[0]
        new_state = None
        fields: dict = {}
        if event == EVENT_IMPORTED:
            new_state = "imported"
            fields["obtained_tier"] = instance.id
        elif key in queue_state:
            new_state, dl = queue_state[key]
            if dl:
                fields["download_id"] = dl
        elif event == EVENT_FAILED:
            new_state = "failed"
        elif event == EVENT_GRABBED:
            new_state = "grabbed"
            dl = latest_event[key][1]
            if dl:
                fields["download_id"] = dl
        if new_state is None:
            continue

        if new_state == "failed":
            if row["state"] == "failed":
                continue  # already recorded — don't inflate attempts/backoff
            attempts = (row["attempts"] or 0) + 1
            delay = min(retry_max, retry_base * (2 ** (attempts - 1)))
            fields["attempts"] = attempts
            fields["next_retry_at"] = (now + timedelta(seconds=delay)).isoformat()
        elif row["state"] == new_state:
            continue  # no change

        fields["state"] = new_state
        await place_store.update_tracking(db, tvdb, season, epnum, updated_at=now.isoformat(), **fields)
        transitions.append({
            "tvdbId": tvdb, "season": season, "episode": epnum,
            "from": row["state"], "to": new_state,
        })

    return transitions


async def poll_all(registry: Registry, db, *, now: datetime | None = None, **kw) -> dict:
    """Poll every instance concurrently; one instance being down won't sink the rest."""
    out: list[dict] = []
    total = 0
    for inst, res in await gather_instances(
        registry, lambda i: poll_instance(registry, db, i, now=now, **kw)
    ):
        if isinstance(res, Exception):
            out.append({"instanceId": inst.id, "error": str(res)})
            continue
        total += len(res)
        out.append({"instanceId": inst.id, "transitions": res})
    return {"instances": out, "transitionCount": total}
