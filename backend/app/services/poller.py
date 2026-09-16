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

from app.obs import kinds
from app.obs.journal import get_journal
from app.obs.metrics import SWEEP_REMOVED
from app.services import grab
from app.services.availability import looks_dangerous
from app.services.fanout import gather_instances
from app.services.liveness import is_dead

logger = logging.getLogger(__name__)
from app.sonarr.registry import Instance, Registry
from app.store import placements as place_store
from app.store import progress

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
        get_journal().emit(
            kinds.POLL_TRANSITION,
            f"S{season:02d}E{epnum:02d} {row['state']} → {new_state} on {instance.id}",
            level="warn" if new_state == "failed" else "info",
            source="poller", tvdb_id=tvdb, season=season, episode=epnum,
            instance_id=instance.id,
            data={"from": row["state"], "to": new_state, "downloadId": fields.get("download_id")},
        )

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


async def sweep_search_stalls(db, *, stall_hours: float, cap: int,
                              now: datetime) -> list[dict]:
    """Revert episodes wedged in 'searching' back to 'wanted' so the reconciler
    re-searches them.

    An episode is marked 'searching' the moment a search is issued — before any
    download exists. A search that finds nothing produces no queue item and no
    history event, so the poller never advances it and it stays 'searching'
    forever. This reaper reverts rows that are 'searching', have no download_id,
    and were last searched more than ``stall_hours`` ago. Pure DB state — makes
    no Sonarr calls. Returns the transitions applied (for the caller to log)."""
    before = (now - timedelta(hours=stall_hours)).isoformat()
    rows = await place_store.stalled_searching(db, before_iso=before, limit=cap)
    transitions: list[dict] = []
    for row in rows:
        await place_store.update_tracking(
            db, row["tvdb_id"], row["season"], row["episode"],
            updated_at=now.isoformat(), state="wanted",
        )
        get_journal().emit(
            kinds.SWEEP_SEARCH_STALL_REVERTED,
            f"S{row['season']:02d}E{row['episode']:02d} searched over {stall_hours:g}h ago "
            f"with no download — back to wanted",
            source="sweep", tvdb_id=row["tvdb_id"], season=row["season"], episode=row["episode"],
            data={"lastSearchAt": row["last_search_at"]},
        )
        transitions.append({
            "tvdbId": row["tvdb_id"], "season": row["season"],
            "episode": row["episode"], "from": "searching", "to": "wanted",
        })
    if transitions:
        logger.info("search-stall sweep reverted %d episode(s) to wanted", len(transitions))
    if len(rows) == cap:
        logger.warning("search-stall sweep hit per-tick cap (%d); more may remain "
                       "and will drain next tick", cap)
    return transitions


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


def _tvdb_lookup(client):
    """Resolve a queue record's per-instance Sonarr ``seriesId`` to its tvdb id.

    The series list is fetched lazily (only once something is actually swept)
    and at most once per instance. A failed lookup resolves to None: the tvdb id
    is bookkeeping, and must never block removing a dead or dangerous download.
    """
    by_id: dict | None = None

    async def resolve(series_id):
        nonlocal by_id
        if by_id is None:
            try:
                by_id = {s["id"]: s.get("tvdbId") for s in await client.list_series()}
            except Exception as exc:  # noqa: BLE001 - see docstring
                logger.warning("series lookup failed; recording sweep without tvdb id: %s", exc)
                by_id = {}
        return by_id.get(series_id)

    return resolve


def _episode_id(record: dict):
    """The episode a queue record covers.

    Sonarr returns both a flat ``episodeId`` and, because we fetch the queue with
    ``includeEpisode=true``, a nested ``episode`` object. Different Sonarr
    versions populate them differently, so accept either.
    """
    return record.get("episodeId") or (record.get("episode") or {}).get("id")


async def _regrab_after_removal(inst, ops, op_id, episode_id, *, title,
                                min_seeders, tvdb_id) -> None:
    """Grab the best-seeded replacement for an episode we just freed up.

    Runs *after* the delete on purpose: while the dead item is queued Sonarr
    rejects every alternative with "Release in queue already meets cutoff",
    including live, well-seeded ones, so a search before the delete finds
    nothing to grab. Failure is soft — the placement falls back to ``wanted``
    and the reconciler searches it again on the next tick.
    """
    best = await grab.regrab_episode(
        inst.client, episode_id=episode_id, min_seeders=min_seeders)
    if best is None:
        await ops.add_step(op_id, {
            "phase": "regrab", "status": "warn",
            "message": f"No healthy replacement found for {title} — left for the next tick",
        })
        return
    await ops.add_step(op_id, {
        "phase": "regrab", "status": "done",
        "message": f"Grabbed replacement — {best.get('title')} "
                   f"({best.get('seeders')} seeders)",
    })
    get_journal().emit(
        kinds.SWEEP_STALLED_REGRABBED,
        f"Replaced {title} with {best.get('title')} ({best.get('seeders')} seeders)",
        source="sweep", tvdb_id=tvdb_id, instance_id=inst.id, operation_id=op_id,
        data={"title": title, "replacement": best.get("title"),
              "seeders": best.get("seeders")},
    )


def _stalled_title(record: dict) -> str:
    base = (record.get("series") or {}).get("title") or record.get("title") or "unknown"
    ep = record.get("episode") or {}
    if ep.get("seasonNumber") is not None and ep.get("episodeNumber") is not None:
        return f"{base} S{ep['seasonNumber']:02d}E{ep['episodeNumber']:02d}"
    return base


# Three days was chosen when a stalled torrent was merely wasteful. It isn't:
# a wedged queue item makes Sonarr reject every alternative for that episode
# ("already meets cutoff"), so the episode is blocked for as long as we wait.
DEFAULT_STALLED_DAYS = 1.0
# No metadata or zero seeders everywhere is not a slow download, it's a dead
# one. More hours cannot change that, so don't spend days finding out.
DEFAULT_DEAD_HOURS = 6.0
DEFAULT_NEAR_COMPLETE_PCT = 95.0
# An interactive search takes 55-95s and bursts earn a Prowlarr 429, so the
# replacement budget is far smaller than the removal cap.
DEFAULT_REGRAB_CAP = 5

# A download this close to done is worth more patience than the ordinary
# threshold: discarding 98% of a transfer is the expensive mistake, and a
# blocklist means the replacement restarts from zero.
NEAR_COMPLETE_GRACE_DAYS = 3.0


async def sweep_stalled(registry, db, ops, *, stalled_days: float, cap: int,
                        now: datetime, dead_hours: float = 6.0,
                        liveness: dict | None = None,
                        near_complete_pct: float = 95.0,
                        min_seeders: int = 0, regrab_cap: int = 0) -> int:
    """Remove torrents stuck at 0% past the threshold. Sonarr removes them from the
    client, blocklists the release, and re-searches. Returns the number removed.

    ``liveness`` is the download client's own view of each swarm, keyed by
    infohash (see services/liveness.py). It sharpens the verdict in two ways:

      * a torrent the client says is *dead* — no metadata, or zero seeders on
        every tracker — is removed after ``dead_hours`` instead of waiting out
        ``stalled_days``, because more time cannot help it;
      * a torrent that is actually moving bytes is never removed, whatever the
        age and progress heuristics conclude.

    An empty or absent map means "no opinion", which reproduces the age-only
    behavior exactly — so an unreachable download client degrades this sweep
    rather than changing its verdicts.

    With ``regrab_cap`` above zero the sweep also *replaces* what it removes: it
    suppresses Sonarr's own re-search and grabs the best-seeded alternative
    itself. Interactive searches are slow and rate-limited, so that budget is
    deliberately far smaller than ``cap``.
    """
    removed = 0
    skipped = 0
    regrabs = 0
    live: set[str] = set()
    for inst, res in await gather_instances(registry, lambda i: i.client.queue()):
        if isinstance(res, Exception):
            continue
        # Group by torrent. A season pack is one torrent but one queue record per
        # episode, and deleting any one record removes the whole torrent — so the
        # siblings would 404. Decide and act once per torrent.
        groups: dict[str, list[dict]] = {}
        for rec in res.get("records", []):
            key = (rec.get("downloadId") or "").strip().lower() or f"__rec{rec.get('id')}"
            groups.setdefault(key, []).append(rec)
        live |= set(groups)
        tvdb_of = _tvdb_lookup(inst.client)

        for key, recs in groups.items():
            r = recs[0]
            # Sum across the group: each episode record holds a slice of the torrent.
            total_left = sum((x.get("sizeleft") or 0) for x in recs)
            total_size = sum((x.get("size") or 0) for x in recs)
            swarm = (liveness or {}).get(key)

            # Bytes are moving: the download client is the only ground truth
            # about the swarm, so it overrides every age/progress heuristic.
            # Still record the observation, or the frozen clock would restart
            # from scratch the moment it does stall.
            if swarm is not None and (swarm.peers_connected > 0 or swarm.rate_download > 0):
                await progress.observe(db, download_id=key, sizeleft=total_left, now=now)
                continue

            unchanged_since = await progress.observe(
                db, download_id=key, sizeleft=total_left, now=now,
            )
            pct_done = (total_size - total_left) / total_size if total_size else 0.0
            dead = swarm is not None and is_dead(swarm)
            if pct_done >= near_complete_pct / 100.0:
                threshold, reason = max(stalled_days, NEAR_COMPLETE_GRACE_DAYS), "near-complete"
            elif dead:
                threshold, reason = dead_hours / 24.0, "dead"
            else:
                threshold, reason = stalled_days, "zero-progress"

            frozen = (
                r.get("protocol") == "torrent"
                and (r.get("status") or "").lower() in _STALLABLE_STATUSES
                and progress.stalled_since(
                    unchanged_since, now=now, stalled_days=threshold)
            )
            if not (_is_stalled(r, now=now, stalled_days=threshold) or frozen):
                continue
            if frozen and reason == "zero-progress":
                reason = "frozen"
            if removed >= cap:
                skipped += 1
                continue
            added_dt = datetime.fromisoformat(str(r["added"]).replace("Z", "+00:00"))
            age_days = int((now - added_dt).total_seconds() // 86400)
            title = _stalled_title(r)
            tvdb_id = await tvdb_of(r.get("seriesId"))
            op_id = await ops.start(
                kind="stalled-cleanup", title=title, tvdb_id=tvdb_id,
                started_at=now.isoformat(), source="reconciler",
            )
            pct = pct_done * 100
            # Only replace a single-episode torrent: a season pack would cost one
            # interactive search per episode, and those are slow and rate-limited.
            episode_ids = {eid for eid in (_episode_id(x) for x in recs) if eid}
            do_regrab = len(episode_ids) == 1 and regrabs < regrab_cap
            try:
                await inst.client.delete_queue_item(r["id"], skip_redownload=do_regrab)
                await ops.add_step(op_id, {
                    "phase": "remove", "status": "done",
                    "message": f"Removed stalled torrent — {title}, stuck at {pct:.0f}% "
                               f"for {age_days}d (blocklisted; Sonarr re-searching)",
                })
                await ops.finish(op_id, result={"removed": True, "downloadId": r.get("downloadId")},
                                 finished_at=now.isoformat())
                await progress.forget(db, key)
                removed += 1
                SWEEP_REMOVED.inc(sweep="stalled")
                get_journal().emit(
                    kinds.SWEEP_STALLED_REMOVED,
                    f"Removed stalled torrent — {title}, stuck at {pct:.0f}% for {age_days}d",
                    source="sweep", tvdb_id=tvdb_id, instance_id=inst.id, operation_id=op_id,
                    data={"title": title, "ageDays": age_days, "progressPct": round(pct, 1),
                          "downloadId": r.get("downloadId"), "queueId": r["id"],
                          "reason": reason,
                          "seeders": swarm.max_seeders if swarm else None},
                )
                if do_regrab:
                    regrabs += 1
                    await _regrab_after_removal(
                        inst, ops, op_id, episode_ids.pop(), title=title,
                        min_seeders=min_seeders, tvdb_id=tvdb_id,
                    )
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
        tvdb_of = _tvdb_lookup(inst.client)
        for r in res.get("records", []):
            if not _is_dangerous(r) or removed >= cap:
                continue
            title = _stalled_title(r)
            tvdb_id = await tvdb_of(r.get("seriesId"))
            op_id = await ops.start(
                kind="dangerous-cleanup", title=title, tvdb_id=tvdb_id,
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
                SWEEP_REMOVED.inc(sweep="dangerous")
                get_journal().emit(
                    kinds.SWEEP_DANGEROUS_REMOVED,
                    f"Removed dangerous release — {title} (executable payload; re-searching)",
                    level="warn", source="sweep", tvdb_id=tvdb_id, instance_id=inst.id,
                    operation_id=op_id,
                    data={"title": title, "downloadId": r.get("downloadId"), "queueId": r["id"]},
                )
            except Exception as exc:  # noqa: BLE001 - one failure shouldn't stop the sweep
                await ops.finish(op_id, error=str(exc), finished_at=now.isoformat())
                logger.warning("failed to remove dangerous release %s: %s", r.get("id"), exc)
    if removed:
        logger.info("dangerous sweep removed %d release(s)", removed)
    return removed
