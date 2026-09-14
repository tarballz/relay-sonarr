"""The autonomy core: a background loop that drives each series toward its goal.

Each tick: (1) poll Sonarr so placement state reflects reality, (2) seed/refresh
the work set of intents, (3) for each non-paused intent, compute the per-episode
plan and act — search obtainable gaps on the desired tier, split the rest onto a
fallback tier, and retry failed episodes once their backoff elapses. Every action
is recorded to the durable operation log (source='reconciler').

Determinism for tests: ``clock`` and ``sleep`` are injected and ``tick()`` is a
public single-shot entrypoint — the real ``run()`` loop is never used in tests.

Observability: each pass is a persisted ``tick`` row with per-phase timings; its
events carry the tick id through ``app.obs.context``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from app.obs import context, kinds
from app.obs.journal import get_journal
from app.obs.metrics import LAST_TICK_TS, RECONCILER_HEALTHY, TICK_DURATION, TICK_TOTAL
from app.obs.stats import TickStats
from app.policy import effective_policy
from app.services import orchestrate, placement, poller
from app.services.library import combined_series
from app.store import availability as avail_cache
from app.store import intents as intent_store
from app.store import placements as place_store
from app.store import settings as settings_store
from app.store import ticks as tick_store

logger = logging.getLogger(__name__)

# States the reconciler will act on (vs in-flight states it leaves alone).
ACTIONABLE = {"wanted", "unavailable", "failed"}


class TickBusy(Exception):
    """A reconciliation tick is already running."""


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


class Reconciler:
    def __init__(self, registry, db, operations, *, interval: float = 1800,
                 jitter: float = 120, availability_ttl: float = placement.DEFAULT_TTL,
                 clock=None, sleep=asyncio.sleep, wait_attempts: int = 10,
                 wait_delay: float = 1.5, enabled: bool = True,
                 stalled_cleanup: bool = True, stalled_cap: int = 25,
                 dangerous_cleanup: bool = True,
                 search_stall_cleanup: bool = True, monitor=None):
        self.registry = registry
        self.db = db
        self.ops = operations
        self.interval = interval
        self.jitter = jitter
        self._ttl = availability_ttl
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._wait_attempts = wait_attempts
        self._wait_delay = wait_delay
        self._stop = asyncio.Event()
        self._inflight: set[int] = set()  # single-flight per series (this process)
        self.enabled = enabled
        self.stalled_cleanup = stalled_cleanup
        self._stalled_cap = stalled_cap
        self.dangerous_cleanup = dangerous_cleanup
        self.search_stall_cleanup = search_stall_cleanup
        self._monitor = monitor
        self._tick_lock = asyncio.Lock()   # one tick at a time: loop or manual
        self._running = False
        self._next_tick_at: datetime | None = None
        self._started_at = self.now()

    def now(self) -> datetime:
        return self._clock()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """The long-running loop. Never lets an exception kill it; wakes early on stop."""
        logger.info("reconciler loop starting (interval=%ss)", self.interval)
        get_journal().emit(
            kinds.LOOP_STARTED, f"Reconciler loop started (every {self.interval / 60:g} min)",
            source="reconciler", data={"intervalS": self.interval},
        )
        while not self._stop.is_set():
            try:
                await self._run_once("schedule")
            except Exception:  # noqa: BLE001 - tick bookkeeping (DB) must not kill the loop
                logger.exception("reconciler iteration failed")
            delay = self.interval + random.uniform(0, self.jitter)
            self._next_tick_at = self.now() + timedelta(seconds=delay)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        self._next_tick_at = None
        get_journal().emit(kinds.LOOP_STOPPED, "Reconciler loop stopped", source="reconciler")
        logger.info("reconciler loop stopped")

    async def _run_once(self, trigger: str = "schedule") -> dict | None:
        """One guarded, serialized iteration. Returns the tick result, or None if
        the tick raised. A bad tick never stops the loop — but it is recorded."""
        async with self._tick_lock:
            result = await self._run_tick(trigger)
        return None if result["status"] == "failed" else result

    async def run_manual(self) -> dict:
        """'Run tick now' — refuses rather than queueing behind a running tick."""
        if self._tick_lock.locked():
            raise TickBusy()
        async with self._tick_lock:
            return await self._run_tick("manual")

    async def _run_tick(self, trigger: str) -> dict:
        started = self.now()
        stats = TickStats()
        tick_id = await tick_store.start(self.db, trigger=trigger, started_at=started.isoformat())
        journal = get_journal()
        out: dict = {"reconciled": []}
        error: str | None = None
        self._running = True
        with context.bind(tick_id=tick_id, tick_stats=stats):
            try:
                out = await self.tick()
            except Exception as exc:  # noqa: BLE001 - a bad tick must not stop the loop
                error = f"{type(exc).__name__}: {exc}"
                logger.exception("reconciler tick failed")
            finally:
                self._running = False

            finished = self.now()
            duration_s = (finished - started).total_seconds()
            reconciled = out.get("reconciled", [])
            series_errors = [r for r in reconciled if r.get("error")]
            actions = sum(r.get("searchedOnDesired", 0) + r.get("filled", 0) for r in reconciled)
            if error:
                status = "failed"
            elif stats.errors or series_errors:
                status = "degraded"
            else:
                status = "ok"

            for instance_id, (zero, live) in stats.zero_spikes().items():
                journal.emit(
                    kinds.AVAILABILITY_ZERO_SPIKE,
                    f"{zero} of {live} availability searches on {instance_id} returned no "
                    f"releases — indexers may be down",
                    level="warn", source="reconciler", instance_id=instance_id,
                    data={"zero": zero, "live": live},
                )

            errors = stats.errors + len(series_errors) + (1 if error else 0)
            await tick_store.finish(
                self.db, tick_id, finished_at=finished.isoformat(),
                duration_ms=int(duration_s * 1000), status=status,
                series_count=len(reconciled), actions=actions,
                transitions=stats.transitions, swept=stats.swept, errors=errors,
                error=error, phases=stats.to_phases(),
            )
            TICK_TOTAL.inc(status=status)
            TICK_DURATION.observe(duration_s)
            LAST_TICK_TS.set(finished.timestamp())

            if status == "failed":
                journal.emit(kinds.TICK_FAILED, f"Tick #{tick_id} failed: {error}",
                             level="error", source="reconciler",
                             data={"trigger": trigger, "error": error})
            else:
                journal.emit(
                    kinds.TICK_FINISHED,
                    f"Tick #{tick_id} {status}: {len(reconciled)} series, {actions} action(s), "
                    f"{stats.transitions} transition(s), {stats.swept} swept in {duration_s:.1f}s",
                    level="info" if status == "ok" else "warn", source="reconciler",
                    data={"trigger": trigger, "status": status, "actions": actions,
                          "errors": errors},
                )
            await journal.flush()

        logger.info("reconciler tick #%d %s: %d series, %d action(s), %d error(s), %.2fs",
                    tick_id, status, len(reconciled), actions, errors, duration_s)
        for r in series_errors:
            logger.warning("reconcile series %s failed: %s", r.get("tvdbId"), r.get("error"))
        return {"tickId": tick_id, "status": status, "error": error, **out}

    async def _liveness_ref(self) -> tuple[datetime, dict | None]:
        """Reference time for staleness: the later of the last completed tick and
        process start. The process-start term is the startup grace, so a long
        first tick after downtime doesn't trip the container healthcheck."""
        last = await tick_store.last_completed(self.db)
        ref = self._started_at
        if last and last["finishedAt"]:
            try:
                ref = max(ref, datetime.fromisoformat(last["finishedAt"]))
            except ValueError:
                pass
        return ref, last

    async def is_healthy(self, now: datetime | None = None) -> bool:
        """Healthy = disabled, or a tick completed (or the process started) recently.
        A degraded tick counts as alive: partial failure is not a wedged loop."""
        if not self.enabled:
            return True
        now = now or self.now()
        ref, _last = await self._liveness_ref()
        return (now - ref).total_seconds() <= self.interval * 2

    async def status(self) -> dict:
        now = self.now()
        ref, last = await self._liveness_ref()
        healthy = (not self.enabled) or (now - ref).total_seconds() <= self.interval * 2
        RECONCILER_HEALTHY.set(1 if healthy else 0)
        return {
            "enabled": self.enabled,
            "healthy": healthy,
            "startedAt": self._started_at.isoformat(),
            "lastTickStartedAt": last["startedAt"] if last else None,
            "lastTickFinishedAt": last["finishedAt"] if last else None,
            "lastTickDurationS": (last["durationMs"] / 1000
                                  if last and last["durationMs"] is not None else None),
            "lastTickActions": last["actions"] if last else 0,
            "secondsSinceLastTick": (now - ref).total_seconds(),
            "lastError": last["error"] if last and last["status"] == "failed" else None,
            "consecutiveFailures": await tick_store.consecutive_failures(self.db),
            "totalTicks": await tick_store.count(self.db),
            "intervalS": self.interval,
            "running": self._running,
            "nextTickAt": _iso(self._next_tick_at),
            "lastTick": last,
            "instances": self._monitor.snapshot() if self._monitor is not None else [],
        }

    @asynccontextmanager
    async def _phase(self, stats: TickStats, name: str, *, swallow: bool = False):
        """Time one tick phase into ``stats``. A ``swallow`` phase (the sweeps)
        records and journals its failure but lets the tick continue."""
        phase: dict = {}
        started = time.perf_counter()
        try:
            yield phase
        except Exception as exc:  # noqa: BLE001
            phase["error"] = f"{type(exc).__name__}: {exc}"
            stats.errors += 1
            if not swallow:
                raise
            logger.exception("%s failed", name)
            get_journal().emit(
                kinds.SWEEP_FAILED, f"{name} failed: {phase['error']}", level="error",
                source="sweep", data={"phase": name, "error": phase["error"]},
            )
        finally:
            phase["ms"] = round((time.perf_counter() - started) * 1000)
            stats.phases[name] = phase
            await get_journal().flush()

    async def tick(self) -> dict:
        """One reconciliation pass over all active intents, phase by phase."""
        stats = context.tick_stats.get() or TickStats()
        async with self._phase(stats, "poll") as phase:
            polled = await poller.poll_all(self.registry, self.db, now=self.now())
            phase["transitions"] = polled.get("transitionCount", 0)
            stats.transitions += phase["transitions"]
            failed = {i["instanceId"]: i["error"]
                      for i in polled.get("instances", []) if i.get("error")}
            if failed:
                phase["errors"] = failed
                stats.errors += len(failed)
                for instance_id, message in failed.items():
                    get_journal().emit(
                        kinds.POLL_INSTANCE_ERROR, f"Polling {instance_id} failed: {message}",
                        level="warn", source="poller", instance_id=instance_id,
                        data={"error": message},
                    )

        defaults = await settings_store.get_defaults(self.db)
        if self.stalled_cleanup:
            async with self._phase(stats, "sweep_stalled", swallow=True) as phase:
                phase["count"] = await poller.sweep_stalled(
                    self.registry, self.db, self.ops,
                    stalled_days=defaults.get("stalledDays", 3),
                    cap=self._stalled_cap, now=self.now(),
                )
                stats.swept += phase["count"]
        if self.dangerous_cleanup:
            async with self._phase(stats, "sweep_dangerous", swallow=True) as phase:
                phase["count"] = await poller.sweep_dangerous(
                    self.registry, self.db, self.ops, cap=self._stalled_cap, now=self.now(),
                )
                stats.swept += phase["count"]
        if self.search_stall_cleanup:
            async with self._phase(stats, "sweep_search_stall", swallow=True) as phase:
                reverted = await poller.sweep_search_stalls(
                    self.db, stall_hours=defaults.get("searchStallHours", 6),
                    cap=self._stalled_cap, now=self.now(),
                )
                phase["count"] = len(reverted)
                stats.swept += phase["count"]

        results = []
        async with self._phase(stats, "reconcile") as phase:
            await self._ensure_intents()
            for intent in await intent_store.all_active(self.db):
                tvdb = intent["tvdb_id"]
                if tvdb in self._inflight:
                    continue
                self._inflight.add(tvdb)
                try:
                    with context.bind(tvdb_id=tvdb):
                        results.append(await self.reconcile_series(dict(intent)))
                except Exception as exc:  # noqa: BLE001
                    results.append({"tvdbId": tvdb, "error": str(exc)})
                finally:
                    self._inflight.discard(tvdb)
            phase["series"] = len(results)
            phase["errors"] = sum(1 for r in results if r.get("error"))
        return {"reconciled": results}

    async def _ensure_intents(self) -> None:
        """Seed an intent for each library series on a tier that has a fallback
        chain (the orchestrate-able set). Idempotent; preserves paused/policy."""
        now = self.now().isoformat()
        seen: set[int] = set()
        for s in await combined_series(self.registry):
            tvdb = s.get("tvdbId")
            iid = s["instanceId"]
            if tvdb is None or tvdb in seen:
                continue
            if self.registry.has_fallback(iid):
                seen.add(tvdb)
                await intent_store.ensure(
                    self.db, tvdb_id=tvdb, title=s.get("title"), chain_key=iid, now=now
                )

    async def reconcile_series(self, intent: dict) -> dict:
        tvdb = intent["tvdb_id"]
        now = self.now()
        defaults = await settings_store.get_defaults(self.db)
        policy = effective_policy(self.registry, intent, defaults)
        desired = policy.preferredTier

        # Only the DESIRED tier needs an interactive availability check (TTL-cached).
        # Falling back is optimistic — add+search on the next tier, like the manual
        # flow — because we can't search a tier the series isn't on yet.
        try:
            await placement.refresh_availability(
                self.registry, self.db, tvdb_id=tvdb, instance_id=desired,
                ttl=self._ttl,
                empty_ttl=defaults.get("emptyReleaseTtlMinutes", 45) * 60,
                now=now,
            )
        except KeyError:
            pass

        plan = await placement.compute_plan(
            self.registry, self.db, tvdb_id=tvdb, desired_tier=desired, now=now
        )
        rows = {(r["season"], r["episode"]): dict(r)
                for r in await place_store.get_for_series(self.db, tvdb)}

        search_desired: list[tuple] = []
        fills: dict[tuple, list[tuple]] = {}  # (instanceId, profile, root) -> keys
        for e in plan["episodes"]:
            if e["state"] not in ACTIONABLE:
                continue
            key = (e["season"], e["episode"])
            row = rows.get(key, {})
            if e["state"] == "failed":  # respect backoff
                nr = row.get("next_retry_at")
                if nr and nr > now.isoformat():
                    continue
            if e["obtainableTier"] == desired:
                search_desired.append(key)
                continue
            if not policy.allowSplit:
                continue
            # Spill to the first cross-instance fallback step whose time-gate has
            # elapsed (afterDays measured from when the episode was first wanted).
            age = self._age_days(row.get("wanted_since"), now)
            step = next(
                (s for s in policy.fallbacks
                 if s.instanceId != desired and s.afterDays <= age),
                None,
            )
            if step is not None:
                fills.setdefault((step.instanceId, step.profile, step.rootFolder), []).append(key)

        if not search_desired and not fills:
            return {"tvdbId": tvdb, "actions": 0}

        op_id = await self.ops.start(
            kind="reconcile", title=intent.get("title") or f"tvdb:{tvdb}",
            tvdb_id=tvdb, started_at=now.isoformat(), source="reconciler",
        )
        try:
            if search_desired:
                # Grab candidates captured during the availability check —
                # direct grabs cost no extra indexer searches.
                candidates: dict[tuple, dict] = {}
                if defaults.get("seederGrab", True):
                    candidates = {
                        (r["season"], r["episode"]): json.loads(r["best_release_json"])
                        for r in await avail_cache.get_for_series(self.db, tvdb)
                        if r["instance_id"] == desired and r["best_release_json"]
                    }
                ids = await orchestrate.search_keys_on_tier(
                    self.registry, desired, tvdb, set(search_desired),
                    candidates=candidates,
                )
                await self._mark_searching(tvdb, search_desired, now)
                grabbed = sum(1 for k in search_desired if k in candidates)
                await self.ops.add_step(op_id, {
                    "phase": "search", "status": "done",
                    "message": (
                        f"Grabbed {grabbed} directly, searched "
                        f"{len(ids) - grabbed} episode(s) on {desired}"
                        if grabbed else
                        f"Searched {len(ids)} episode(s) on {desired}"
                    ),
                })
            filled = 0
            filled_on = None
            for (inst_id, profile, root), keys in fills.items():
                ids = await orchestrate.place_on_fallback(
                    self.registry, tvdb_id=tvdb, fb_instance_id=inst_id,
                    keys=set(keys), origin_instance_id=desired,
                    profile=profile, root=root,
                    sleep=self._sleep, wait_attempts=self._wait_attempts,
                    wait_delay=self._wait_delay,
                )
                await self._mark_searching(tvdb, keys, now)
                filled += len(keys)
                filled_on = inst_id
                await self.ops.add_step(op_id, {
                    "phase": "split", "status": "done",
                    "message": f"Filling {len(ids)} gap(s) on {inst_id}",
                })
            result = {
                "tvdbId": tvdb,
                "searchedOnDesired": len(search_desired),
                "filledOn": filled_on,
                "filled": filled,
            }
            await self.ops.finish(op_id, result=result, finished_at=self.now().isoformat())
            return result
        except Exception as exc:  # noqa: BLE001
            await self.ops.finish(op_id, error=str(exc), finished_at=self.now().isoformat())
            raise

    @staticmethod
    def _age_days(wanted_since: str | None, now: datetime) -> float:
        if not wanted_since:
            return 0.0
        try:
            ts = datetime.fromisoformat(wanted_since)
        except ValueError:
            return 0.0
        return (now - ts).total_seconds() / 86400.0

    async def _mark_searching(self, tvdb: int, keys: list[tuple], now: datetime) -> None:
        for (season, episode) in keys:
            await place_store.update_tracking(
                self.db, tvdb, season, episode,
                state="searching", last_search_at=now.isoformat(),
                updated_at=now.isoformat(),
            )
