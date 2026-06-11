"""The autonomy core: a background loop that drives each series toward its goal.

Each tick: (1) poll Sonarr so placement state reflects reality, (2) seed/refresh
the work set of intents, (3) for each non-paused intent, compute the per-episode
plan and act — search obtainable gaps on the desired tier, split the rest onto a
fallback tier, and retry failed episodes once their backoff elapses. Every action
is recorded to the durable operation log (source='reconciler').

Determinism for tests: ``clock`` and ``sleep`` are injected and ``tick()`` is a
public single-shot entrypoint — the real ``run()`` loop is never used in tests.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timezone

from app.policy import effective_policy
from app.services import orchestrate, placement, poller
from app.services.library import combined_series
from app.store import availability as avail_cache
from app.store import intents as intent_store
from app.store import placements as place_store
from app.store import settings as settings_store

logger = logging.getLogger(__name__)

# States the reconciler will act on (vs in-flight states it leaves alone).
ACTIONABLE = {"wanted", "unavailable", "failed"}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


class Reconciler:
    def __init__(self, registry, db, operations, *, interval: float = 1800,
                 jitter: float = 120, availability_ttl: float = placement.DEFAULT_TTL,
                 clock=None, sleep=asyncio.sleep, wait_attempts: int = 10,
                 wait_delay: float = 1.5, enabled: bool = True,
                 stalled_cleanup: bool = True, stalled_cap: int = 25,
                 dangerous_cleanup: bool = True):
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
        # ---- Health/observability (read via status()/is_healthy()) ----
        self.enabled = enabled
        self.stalled_cleanup = stalled_cleanup
        self._stalled_cap = stalled_cap
        self.dangerous_cleanup = dangerous_cleanup
        self._started_at = self.now()
        self._last_tick_started_at: datetime | None = None
        self._last_tick_finished_at: datetime | None = None
        self._last_tick_duration_s: float | None = None
        self._last_tick_actions = 0
        self._last_error: str | None = None
        self._consecutive_failures = 0
        self._total_ticks = 0

    def now(self) -> datetime:
        return self._clock()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """The long-running loop. Never lets an exception kill it; wakes early on stop."""
        logger.info("reconciler loop starting (interval=%ss)", self.interval)
        while not self._stop.is_set():
            await self._run_once()
            delay = self.interval + random.uniform(0, self.jitter)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        logger.info("reconciler loop stopped")

    async def _run_once(self) -> dict | None:
        """One guarded iteration: run a tick, record health, swallow+log failures.

        Returns the tick result, or None if the tick raised. A bad tick must never
        stop the loop — but it must be visible (logged + recorded), unlike before."""
        started = self.now()
        self._last_tick_started_at = started
        self._total_ticks += 1
        try:
            out = await self.tick()
            finished = self.now()
            self._last_tick_finished_at = finished
            self._last_tick_duration_s = (finished - started).total_seconds()
            self._last_tick_actions = sum(
                r.get("searchedOnDesired", 0) + r.get("filled", 0)
                for r in out.get("reconciled", [])
            )
            errs = [r for r in out.get("reconciled", []) if r.get("error")]
            self._consecutive_failures = 0
            self._last_error = None
            logger.info(
                "reconciler tick ok: %d series, %d action(s), %d error(s), %.2fs",
                len(out.get("reconciled", [])), self._last_tick_actions,
                len(errs), self._last_tick_duration_s,
            )
            for r in errs:
                logger.warning("reconcile series %s failed: %s", r.get("tvdbId"), r.get("error"))
            return out
        except Exception as exc:  # noqa: BLE001 - a bad tick must not stop the loop
            self._consecutive_failures += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("reconciler tick failed")
            return None

    def is_healthy(self, now: datetime | None = None) -> bool:
        """Healthy = disabled, or has completed a tick recently enough. A startup
        grace (measured from start until the first tick) avoids a false alarm on a
        freshly-booted app."""
        if not self.enabled:
            return True
        now = now or self.now()
        ref = self._last_tick_finished_at or self._started_at
        return (now - ref).total_seconds() <= self.interval * 2

    def status(self) -> dict:
        now = self.now()
        ref = self._last_tick_finished_at or self._started_at
        return {
            "enabled": self.enabled,
            "healthy": self.is_healthy(now),
            "startedAt": self._started_at.isoformat(),
            "lastTickStartedAt": _iso(self._last_tick_started_at),
            "lastTickFinishedAt": _iso(self._last_tick_finished_at),
            "lastTickDurationS": self._last_tick_duration_s,
            "lastTickActions": self._last_tick_actions,
            "secondsSinceLastTick": (now - ref).total_seconds(),
            "lastError": self._last_error,
            "consecutiveFailures": self._consecutive_failures,
            "totalTicks": self._total_ticks,
            "intervalS": self.interval,
        }

    async def tick(self) -> dict:
        """One reconciliation pass over all active intents."""
        await poller.poll_all(self.registry, self.db, now=self.now())
        if self.stalled_cleanup:
            defaults = await settings_store.get_defaults(self.db)
            try:
                await poller.sweep_stalled(
                    self.registry, self.db, self.ops,
                    stalled_days=defaults.get("stalledDays", 3),
                    cap=self._stalled_cap, now=self.now(),
                )
            except Exception:  # noqa: BLE001 - sweep failure must not stop the tick
                logger.exception("stalled sweep failed")
        if self.dangerous_cleanup:
            try:
                await poller.sweep_dangerous(
                    self.registry, self.db, self.ops,
                    cap=self._stalled_cap, now=self.now(),
                )
            except Exception:  # noqa: BLE001 - sweep failure must not stop the tick
                logger.exception("dangerous sweep failed")
        await self._ensure_intents()
        results = []
        for intent in await intent_store.all_active(self.db):
            tvdb = intent["tvdb_id"]
            if tvdb in self._inflight:
                continue
            self._inflight.add(tvdb)
            try:
                results.append(await self.reconcile_series(dict(intent)))
            except Exception as exc:  # noqa: BLE001
                results.append({"tvdbId": tvdb, "error": str(exc)})
            finally:
                self._inflight.discard(tvdb)
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
                ttl=self._ttl, now=now,
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
