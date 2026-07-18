"""Outcome tracking by polling Sonarr (no webhooks — Relay can't be reached inbound).

Reads each instance's queue + recent history and advances ``placement`` rows
through the real download lifecycle: grabbed → importing → imported, or → failed
(with a backoff ``next_retry_at`` the reconciler later acts on). Correlation keys
on ``(tvdb_id, season, episode)`` via a seriesId→tvdbId map, never per-instance
episode ids. Only episodes already in a plan are touched; everything else is
ignored. Idempotent: re-applying the same history is a no-op.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.services.availability import looks_dangerous
from app.services.fanout import gather_instances

logger = logging.getLogger(__name__)
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


# "queued" included: the client may report a torrent it never started as queued,
# and at 0% past the threshold that's just as dead as a stalled "downloading" one.
# "paused" is deliberately excluded — that's a user decision.
_STALLABLE_STATUSES = {"downloading", "queued", "stalled", "warning"}


def _is_stalled(record: dict, *, now: datetime, stalled_days: float) -> bool:
    """A torrent that has transferred nothing since longer than the threshold.

    "Nothing" has two shapes, and both count:
      * size > 0 and sizeleft == size — size known, not a byte received.
      * size == 0 — Sonarr never even learned a size, i.e. the torrent never
        fetched its metadata. This is the *deadest* state, but an earlier
        `size <= 0` guard skipped it, so these were never swept.
    """
    if record.get("protocol") != "torrent":
        return False
    if (record.get("status") or "").lower() not in _STALLABLE_STATUSES:
        return False
    size = record.get("size") or 0
    sizeleft = record.get("sizeleft")
    if sizeleft is None:
        return False
    if size > 0 and sizeleft != size:  # some bytes arrived: progressing, not stalled
        return False
    added = record.get("added")
    if not added:
        return False
    try:
        added_dt = datetime.fromisoformat(str(added).replace("Z", "+00:00"))
    except ValueError:
        return False
    return (now - added_dt).total_seconds() > stalled_days * 86400.0


def _stalled_title(record: dict) -> str:
    base = (record.get("series") or {}).get("title") or record.get("title") or "unknown"
    ep = record.get("episode") or {}
    if ep.get("seasonNumber") is not None and ep.get("episodeNumber") is not None:
        return f"{base} S{ep['seasonNumber']:02d}E{ep['episodeNumber']:02d}"
    return base


async def sweep_stalled(registry, db, ops, *, stalled_days: float, cap: int,
                        now: datetime) -> int:
    """Remove torrents stuck at 0% past the threshold. Sonarr removes them from the
    client, blocklists the release, and re-searches. Returns the number removed."""
    removed = 0
    skipped = 0
    for inst, res in await gather_instances(registry, lambda i: i.client.queue()):
        if isinstance(res, Exception):
            continue
        for r in res.get("records", []):
            if not _is_stalled(r, now=now, stalled_days=stalled_days):
                continue
            if removed >= cap:
                skipped += 1
                continue
            added_dt = datetime.fromisoformat(str(r["added"]).replace("Z", "+00:00"))
            age_days = int((now - added_dt).total_seconds() // 86400)
            title = _stalled_title(r)
            op_id = await ops.start(
                kind="stalled-cleanup", title=title, tvdb_id=r.get("seriesId") or 0,
                started_at=now.isoformat(), source="reconciler",
            )
            try:
                await inst.client.delete_queue_item(r["id"])
                await ops.add_step(op_id, {
                    "phase": "remove", "status": "done",
                    "message": f"Removed stalled torrent — {title}, 0% for {age_days}d "
                               f"(blocklisted; Sonarr re-searching)",
                })
                await ops.finish(op_id, result={"removed": True, "downloadId": r.get("downloadId")},
                                 finished_at=now.isoformat())
                removed += 1
            except Exception as exc:  # noqa: BLE001 - one failure shouldn't stop the sweep
                await ops.finish(op_id, error=str(exc), finished_at=now.isoformat())
                logger.warning("failed to remove stalled torrent %s: %s", r.get("id"), exc)
    if skipped:
        logger.warning("stalled sweep hit per-tick cap (%d); %d deferred to next tick", cap, skipped)
    if removed:
        logger.info("stalled sweep removed %d torrent(s)", removed)
    return removed


# Sonarr's own import-caution markers (set when a downloaded file is an
# executable). Complements the title-extension check in `looks_dangerous`.
_DANGER_MARKERS = ("potentially dangerous file", "found executable file")


def _is_dangerous(record: dict) -> bool:
    """Queue item whose release is an executable/malware fake.

    Two signals: the release title itself ends in an executable extension
    (``looks_dangerous``), or Sonarr's import pipeline flagged a contained file
    ("Caution: Found potentially dangerous file…" / "Found executable file…").
    """
    if looks_dangerous(record):
        return True
    for sm in record.get("statusMessages") or []:
        text = " ".join([sm.get("title") or ""] + list(sm.get("messages") or [])).lower()
        if any(marker in text for marker in _DANGER_MARKERS):
            return True
    return False


async def sweep_dangerous(registry, db, ops, *, cap: int, now: datetime) -> int:
    """Remove + blocklist executable/malware fake releases, then re-search.

    Unlike the stalled sweep there is no age threshold — a flagged executable
    should never sit in the queue (or on disk) at all. The delete removes the
    payload from the download client; the EpisodeSearch immediately re-acquires
    a legitimate release (the fake is blocklisted, so it can't come back).
    """
    removed = 0
    for inst, res in await gather_instances(registry, lambda i: i.client.queue()):
        if isinstance(res, Exception):
            continue
        for r in res.get("records", []):
            if not _is_dangerous(r) or removed >= cap:
                continue
            title = _stalled_title(r)
            op_id = await ops.start(
                kind="dangerous-cleanup", title=title, tvdb_id=r.get("seriesId") or 0,
                started_at=now.isoformat(), source="reconciler",
            )
            try:
                await inst.client.delete_queue_item(r["id"])
                ep_id = (r.get("episode") or {}).get("id")
                if ep_id:
                    await inst.client.command("EpisodeSearch", episodeIds=[ep_id])
                await ops.add_step(op_id, {
                    "phase": "remove", "status": "done",
                    "message": f"Removed dangerous release — {title} "
                               f"(executable payload; blocklisted, re-searching)",
                })
                await ops.finish(op_id, result={"removed": True, "downloadId": r.get("downloadId")},
                                 finished_at=now.isoformat())
                removed += 1
            except Exception as exc:  # noqa: BLE001 - one failure shouldn't stop the sweep
                await ops.finish(op_id, error=str(exc), finished_at=now.isoformat())
                logger.warning("failed to remove dangerous release %s: %s", r.get("id"), exc)
    if removed:
        logger.info("dangerous sweep removed %d release(s)", removed)
    return removed
