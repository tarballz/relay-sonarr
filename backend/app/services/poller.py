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
import re
from datetime import datetime, timedelta, timezone

from app.obs import kinds
from app.obs.journal import get_journal
from app.obs.metrics import SWEEP_NUDGED, SWEEP_REMOVED
from app.services import grab
from app.services.availability import looks_dangerous
from app.services.fanout import gather_instances
from app.services.liveness import is_dead

logger = logging.getLogger(__name__)
from app.sonarr.registry import Instance, Registry
from app.store import events as events_store
from app.store import placements as place_store
from app.store import progress

EVENT_IMPORTED = "downloadFolderImported"
EVENT_FAILED = "downloadFailed"
EVENT_GRABBED = "grabbed"

DEFAULT_RETRY_BASE = 900       # 15 min
DEFAULT_RETRY_MAX = 24 * 3600  # 1 day


def _backoff(attempts: int, base: float, max_: float) -> timedelta:
    """Exponential retry delay for the ``attempts``-th failure, capped at ``max_``."""
    return timedelta(seconds=min(max_, base * (2 ** (attempts - 1))))


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
            # Imported clears the failure backoff: it described a gap that's gone.
            fields["attempts"] = 0
            fields["next_retry_at"] = None
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
        # The row is shared across instances, and compute_plan owns "imported"
        # (a file is on disk somewhere). Another tier's upgrade download or stale
        # failure must not move it: Silo S01E01 flipped imported -> failed 649
        # times that way, and upgrades flip imported <-> grabbed every tick.
        if row["state"] == "imported" and new_state != "imported":
            continue

        if new_state == "failed":
            if row["state"] == "failed":
                continue  # already recorded — don't inflate attempts/backoff
            attempts = (row["attempts"] or 0) + 1
            fields["attempts"] = attempts
            fields["next_retry_at"] = (now + _backoff(attempts, retry_base, retry_max)).isoformat()
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


async def sweep_search_stalls(db, *, stall_hours: float, cap: int, now: datetime,
                              retry_base: float = DEFAULT_RETRY_BASE,
                              retry_max: float = DEFAULT_RETRY_MAX) -> list[dict]:
    """Revert episodes wedged in 'searching' back to 'wanted' so the reconciler
    re-searches them.

    An episode is marked 'searching' the moment a search is issued — before any
    download exists. A search that finds nothing produces no queue item and no
    history event, so the poller never advances it and it stays 'searching'
    forever. This reaper reverts rows that are 'searching', have no download_id,
    and were last searched more than ``stall_hours`` ago. Pure DB state — makes
    no Sonarr calls. Returns the transitions applied (for the caller to log).

    A revert counts as a failed attempt and sets ``next_retry_at`` with the same
    exponential backoff as a failed download, so an episode nothing can satisfy
    is retried ever less often instead of blindly every ``stall_hours``."""
    before = (now - timedelta(hours=stall_hours)).isoformat()
    rows = await place_store.stalled_searching(db, before_iso=before, limit=cap)
    transitions: list[dict] = []
    for row in rows:
        attempts = (row["attempts"] or 0) + 1
        next_retry_at = (now + _backoff(attempts, retry_base, retry_max)).isoformat()
        await place_store.update_tracking(
            db, row["tvdb_id"], row["season"], row["episode"],
            updated_at=now.isoformat(), state="wanted",
            attempts=attempts, next_retry_at=next_retry_at,
        )
        get_journal().emit(
            kinds.SWEEP_SEARCH_STALL_REVERTED,
            f"S{row['season']:02d}E{row['episode']:02d} searched over {stall_hours:g}h ago "
            f"with no download — back to wanted",
            source="sweep", tvdb_id=row["tvdb_id"], season=row["season"], episode=row["episode"],
            data={"lastSearchAt": row["last_search_at"], "attempts": attempts,
                  "nextRetryAt": next_retry_at},
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

# Frozen-download recovery. A torrent can hold a peer that never sends and sit
# one block short of done indefinitely (live: S13E06 at 99.9% for 40 minutes
# with two peers and zero bytes moved). A re-announce plus a stop/start resets
# its peer state; only if the budget is spent does the removal clock shorten.
DEFAULT_NUDGE_AFTER_MINUTES = 30.0
DEFAULT_NUDGE_COOLDOWN_MINUTES = 60.0
DEFAULT_NUDGE_CAP = 2
DEFAULT_FROZEN_AFTER_NUDGE_HOURS = 6.0


def _minutes_since(stamp: str | None, now: datetime) -> int:
    """Whole minutes between a stored ISO timestamp and ``now`` (0 if unusable)."""
    if not stamp:
        return 0
    try:
        seen = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return 0
    return max(0, int((now - seen).total_seconds() // 60))


async def sweep_frozen(registry, db, *, now: datetime,
                       liveness: dict | None = None,
                       nudge_after_minutes: float = DEFAULT_NUDGE_AFTER_MINUTES,
                       cooldown_minutes: float = DEFAULT_NUDGE_COOLDOWN_MINUTES,
                       max_nudges: int = DEFAULT_NUDGE_CAP,
                       cap: int = 25) -> int:
    """Nudge torrents that are incomplete but have stopped moving.

    A nudge is a tracker re-announce followed by a stop/start: it asks for a
    fresh peer list and drops a peer/choke state that may be wedged. The
    trigger is ``sizeleft`` unchanged for ``nudge_after_minutes`` — not the
    instantaneous rate, which is too noisy to act on.

    Dead swarms are left to ``sweep_stalled`` (no nudge can conjure peers), and
    user-paused torrents are never touched. Each torrent gets ``max_nudges``
    attempts separated by ``cooldown_minutes``; moving bytes resets the budget.
    Failures are soft: one bad torrent never fails the sweep.
    """
    client = registry.downloads()
    if client is None or not liveness or max_nudges <= 0 or cap <= 0:
        return 0

    nudged = 0
    for inst, res in await gather_instances(registry, lambda i: i.client.queue()):
        if isinstance(res, Exception):
            continue
        groups: dict[str, list[dict]] = {}
        for rec in res.get("records", []):
            key = (rec.get("downloadId") or "").strip().lower()
            if key:
                groups.setdefault(key, []).append(rec)
        tvdb_of = _tvdb_lookup(inst.client)

        for key, recs in groups.items():
            r = recs[0]
            if r.get("protocol") != "torrent":
                continue
            if (r.get("status") or "").lower() not in _STALLABLE_STATUSES:
                continue
            total_left = sum((x.get("sizeleft") or 0) for x in recs)
            if total_left <= 0:
                continue
            swarm = liveness.get(key)
            if swarm is None or is_dead(swarm):
                continue

            unchanged_since = await progress.observe(
                db, download_id=key, sizeleft=total_left, now=now,
            )
            if swarm.rate_download > 0:
                continue
            if not progress.stalled_since(
                    unchanged_since, now=now,
                    stalled_days=nudge_after_minutes / 1440.0):
                continue

            state = await progress.nudge_state(db, key)
            if state["nudge_count"] >= max_nudges:
                continue
            last = state["last_nudge_at"]
            if last and not progress.stalled_since(
                    last, now=now, stalled_days=cooldown_minutes / 1440.0):
                continue

            try:
                await client.reannounce(key)
                await client.restart(key)
            except Exception as exc:  # noqa: BLE001 - one torrent must not fail the sweep
                logger.warning("frozen nudge failed for %s: %s", key, exc)
                continue

            await progress.note_nudge(db, key, now)
            title = _stalled_title(r)
            tvdb_id = await tvdb_of(r.get("seriesId"))
            get_journal().emit(
                kinds.SWEEP_FROZEN_NUDGED,
                f"Nudged frozen download — {title}, no progress for "
                f"{_minutes_since(unchanged_since, now)} min with "
                f"{total_left} bytes left",
                source="sweep", tvdb_id=tvdb_id, instance_id=inst.id,
                data={"downloadId": key, "left": total_left,
                      "peers": swarm.peers_connected,
                      "seeders": swarm.max_seeders,
                      "nudgeCount": state["nudge_count"] + 1},
            )
            SWEEP_NUDGED.inc(sweep="frozen")
            nudged += 1
            if nudged >= cap:
                logger.warning("frozen-nudge sweep hit per-tick cap (%d); "
                               "more may remain and will be nudged next tick", cap)
                return nudged

    if nudged:
        logger.info("frozen-nudge sweep nudged %d torrent(s)", nudged)
    return nudged


async def sweep_stalled(registry, db, ops, *, stalled_days: float, cap: int,
                        now: datetime, dead_hours: float = 6.0,
                        liveness: dict | None = None,
                        near_complete_pct: float = 95.0,
                        min_seeders: int = 0, regrab_cap: int = 0,
                        nudge_cap: int = 0,
                        frozen_after_nudge_hours: float = DEFAULT_FROZEN_AFTER_NUDGE_HOURS,
                        nudge_cooldown_minutes: float = DEFAULT_NUDGE_COOLDOWN_MINUTES) -> int:
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

            # Observe every torrent, even one with peers: a connected peer that
            # never sends is exactly the S13E06 failure, and without the
            # observation the frozen clock would never start.
            unchanged_since = await progress.observe(
                db, download_id=key, sizeleft=total_left, now=now,
            )

            # Bytes are moving: the download client is the only ground truth
            # about the swarm, so it overrides every age/progress heuristic.
            if swarm is not None and swarm.rate_download > 0:
                continue

            pct_done = (total_size - total_left) / total_size if total_size else 0.0
            dead = swarm is not None and is_dead(swarm)
            state = await progress.nudge_state(db, key)
            # A zero cap means the frozen-nudge sweep is disabled: keep the old
            # behaviour exactly, including the three-day near-complete grace.
            # The final nudge must also have had the cooldown to work — both
            # sweeps run in one tick, and otherwise a just-sent nudge would be
            # spent and the torrent removed seconds later.
            last_nudge = state["last_nudge_at"]
            nudge_settled = bool(last_nudge) and progress.stalled_since(
                last_nudge, now=now,
                stalled_days=nudge_cooldown_minutes / 1440.0)
            nudge_exhausted = (
                nudge_cap > 0
                and state["nudge_count"] >= nudge_cap
                and nudge_settled
            )
            if pct_done >= near_complete_pct / 100.0:
                if nudge_exhausted:
                    threshold, reason = (frozen_after_nudge_hours / 24.0,
                                         "frozen-after-nudge")
                else:
                    threshold, reason = (max(stalled_days, NEAR_COMPLETE_GRACE_DAYS),
                                         "near-complete")
            elif dead:
                threshold, reason = dead_hours / 24.0, "dead"
            else:
                threshold, reason = stalled_days, "zero-progress"

            # A connected peer usually means life. Once both nudges have been
            # spent and the clock still has not moved, that evidence is stronger
            # than the peer count, so let the frozen threshold decide.
            if swarm is not None and swarm.peers_connected > 0 and not nudge_exhausted:
                continue

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
                               f"for {age_days}d ({reason}; blocklisted; "
                               + ("Relay replacing it)" if do_regrab else "Sonarr re-searching)"),
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
# executable, or has no recognizable video extension at all). Complements the
# title-extension check in `looks_dangerous`.
#
# "unsupported extension: ''" is deliberately the *blank*-extension message
# only, not "unsupported extension" generally — a real if unwanted container
# Sonarr declines to import (e.g. a bonus .iso) still names an extension, and
# is a format problem, not a malware fake. Live incident: "South Park S29E01"
# was a 1.05GB Windows PE32+ executable with no extension at all, padded to
# look like a video; Relay has no mount onto the downloads tree to inspect
# the bytes itself, but Sonarr's import pipeline already did, and a blank
# extension is the one verdict from it that's unambiguous.
_DANGER_MARKERS = (
    "potentially dangerous file",
    "found executable file",
    "unsupported extension: ''",
)


def _status_text(record: dict) -> str:
    """Sonarr's statusMessages flattened to one lowercased string for matching."""
    return " ".join(
        " ".join([sm.get("title") or ""] + list(sm.get("messages") or []))
        for sm in record.get("statusMessages") or []
    ).lower()


def _added_age_hours(record: dict, now: datetime) -> float | None:
    added = record.get("added")
    if not added:
        return None
    try:
        added_dt = datetime.fromisoformat(str(added).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - added_dt).total_seconds() / 3600.0


def _is_dangerous(record: dict) -> bool:
    """Queue item whose release is an executable/malware fake.

    Three signals: the release title itself ends in an executable extension
    (``looks_dangerous``), or Sonarr's import pipeline flagged a contained file
    ("Caution: Found potentially dangerous file…" / "Found executable file…"),
    or Sonarr's import pipeline found no video extension on the file at all
    ("Invalid video file, unsupported extension: ''").
    """
    if looks_dangerous(record):
        return True
    text = _status_text(record)
    return any(marker in text for marker in _DANGER_MARKERS)


def _by_download(records: list[dict]) -> list[list[dict]]:
    """Group queue records by download. A season pack is one download with a
    record per episode: one DELETE removes all of them, so acting per record
    repeats the delete and logs every repeat as an errored operation."""
    groups: dict = {}
    for r in records:
        groups.setdefault(r.get("downloadId") or ("queue", r.get("id")), []).append(r)
    return list(groups.values())


async def _remove_and_research(inst, ops, records: list[dict], *, tvdb_id, now: datetime,
                               op_kind: str, kind: str, reason: str) -> bool:
    """Remove + blocklist one download, re-search all its episodes, and record it.

    ``records`` are the download's queue records (one per episode). Shared by
    the dangerous and archive sweeps. Returns whether the removal succeeded; a
    failure is logged and recorded on the operation, never raised."""
    record = records[0]
    title = _stalled_title(record)
    op_id = await ops.start(
        kind=op_kind, title=title, tvdb_id=tvdb_id,
        started_at=now.isoformat(), source="reconciler",
    )
    try:
        await inst.client.delete_queue_item(record["id"])
        ep_ids = [i for i in ((r.get("episode") or {}).get("id") for r in records) if i]
        if ep_ids:
            await inst.client.command("EpisodeSearch", episodeIds=ep_ids)
        await ops.add_step(op_id, {
            "phase": "remove", "status": "done",
            "message": f"Removed {reason} — {title} (blocklisted, re-searching)",
        })
        await ops.finish(op_id, result={"removed": True, "downloadId": record.get("downloadId")},
                         finished_at=now.isoformat())
        get_journal().emit(
            kind,
            f"Removed {reason} — {title} (re-searching)",
            level="warn", source="sweep", tvdb_id=tvdb_id, instance_id=inst.id,
            operation_id=op_id,
            data={"title": title, "downloadId": record.get("downloadId"),
                  "queueId": record["id"]},
        )
        return True
    except Exception as exc:  # noqa: BLE001 - one failure shouldn't stop the sweep
        await ops.finish(op_id, error=str(exc), finished_at=now.isoformat())
        logger.warning("failed to remove %s %s: %s", reason, record.get("id"), exc)
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
        flagged = [r for r in res.get("records", []) if _is_dangerous(r)]
        for group in _by_download(flagged):
            if removed >= cap:
                break
            if await _remove_and_research(
                inst, ops, group, tvdb_id=await tvdb_of(group[0].get("seriesId")), now=now,
                op_kind="dangerous-cleanup", kind=kinds.SWEEP_DANGEROUS_REMOVED,
                reason="dangerous release (executable payload)",
            ):
                removed += 1
                SWEEP_REMOVED.inc(sweep="dangerous")
    if removed:
        logger.info("dangerous sweep removed %d release(s)", removed)
    return removed


# Archive releases Sonarr refuses as the episode file itself. The stack runs no
# unpacker, so these can never import: at best a dead slot, at worst a
# password-protected scam. Kept apart from _DANGER_MARKERS on purpose — an
# archive is not evidence of malware, and other named extensions (a bonus .iso)
# are format problems this sweep must leave alone.
_ARCHIVE_EXT = re.compile(r"unsupported extension: '\.(7z|rar|zip|r\d\d)'")
ARCHIVE_GRACE_HOURS = 2
STUCK_IMPORT_WARN_HOURS = 24


def _import_pending(record: dict) -> bool:
    return (record.get("trackedDownloadState") or "").lower() == "importpending"


def _is_unimportable_archive(record: dict, *, now: datetime, grace_hours: float) -> bool:
    """importPending archive release, older than ``grace_hours`` since it was added."""
    if not _import_pending(record):
        return False
    if not _ARCHIVE_EXT.search(_status_text(record)):
        return False
    age = _added_age_hours(record, now)
    return age is not None and age >= grace_hours


async def sweep_archives(registry, db, ops, *, cap: int, now: datetime,
                         grace_hours: float = ARCHIVE_GRACE_HOURS,
                         stuck_warn_hours: float = STUCK_IMPORT_WARN_HOURS) -> int:
    """Remove + blocklist unimportable archive releases, then re-search.

    Also warns once per download about any other import stuck past
    ``stuck_warn_hours`` — the gap that let the S29E01 fake sit for 7 days."""
    removed = 0
    # Ticks run duration + interval (+ jitter) apart, so a threshold "window"
    # misses crossings; the journal itself is the once-per-download record.
    warned = {(e.get("data") or {}).get("downloadId")
              for e in await events_store.query(db, kind=kinds.SWEEP_IMPORT_STUCK, limit=1000)}
    for inst, res in await gather_instances(registry, lambda i: i.client.queue()):
        if isinstance(res, Exception):
            continue
        tvdb_of = _tvdb_lookup(inst.client)
        records = res.get("records", [])
        archives = [r for r in records
                    if _is_unimportable_archive(r, now=now, grace_hours=grace_hours)]
        for group in _by_download(archives):
            if removed >= cap:
                break
            if await _remove_and_research(
                inst, ops, group, tvdb_id=await tvdb_of(group[0].get("seriesId")), now=now,
                op_kind="archive-cleanup", kind=kinds.SWEEP_ARCHIVE_REMOVED,
                reason="unimportable archive release",
            ):
                removed += 1
                SWEEP_REMOVED.inc(sweep="archive")
        for r in records:
            if r in archives or not _import_pending(r):
                continue
            age = _added_age_hours(r, now)
            if (age is not None and age >= stuck_warn_hours
                    and r.get("downloadId") not in warned):
                warned.add(r.get("downloadId"))
                title = _stalled_title(r)
                get_journal().emit(
                    kinds.SWEEP_IMPORT_STUCK,
                    f"{title} has waited {age:.0f}h to import on {inst.id} — "
                    f"{_status_text(r) or 'no reason given'}",
                    level="warn", source="sweep", tvdb_id=await tvdb_of(r.get("seriesId")),
                    instance_id=inst.id,
                    data={"title": title, "downloadId": r.get("downloadId"), "queueId": r["id"]},
                )
    if removed:
        logger.info("archive sweep removed %d release(s)", removed)
    return removed
