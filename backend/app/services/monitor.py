"""Always-on observer: probes each Sonarr, watches its health checks, prunes history.

Runs independently of RECONCILER_ENABLED — visibility must not depend on
automation being switched on. Sonarr's own /health is the signal that catches an
indexer outage Sonarr otherwise reports as "HTTP 200, no releases".
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.obs import kinds
from app.obs.journal import get_journal
from app.obs.metrics import SONARR_UP
from app.services import retention

logger = logging.getLogger(__name__)

_PROBLEM_TYPES = {"warning", "error"}


@dataclass
class InstanceState:
    up: bool | None = None          # None until the first successful probe or outage
    since: str | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    sonarr_health: list[dict] = field(default_factory=list)
    health_checked_at: str | None = None


class Monitor:
    def __init__(self, registry, db, *, clock=None, interval: float = 60.0,
                 health_every: int = 5, down_after: int = 2):
        self.registry = registry
        self.db = db
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._interval = interval
        self._health_every = health_every
        self._down_after = down_after
        self._states: dict[str, InstanceState] = {}
        self._iteration = 0
        self._stop = asyncio.Event()

    def _state(self, instance_id: str) -> InstanceState:
        return self._states.setdefault(instance_id, InstanceState())

    async def step(self) -> None:
        """One observation pass (the test entrypoint)."""
        now = self._clock()
        instances = self.registry.all()
        await asyncio.gather(*(self._probe(inst, now) for inst in instances))
        if self._iteration % self._health_every == 0:
            await asyncio.gather(*(
                self._check_health(inst, now) for inst in instances if self._state(inst.id).up
            ))
        self._iteration += 1
        try:
            counts = await retention.maybe_prune(self.db, now)
            if counts:
                logger.info("retention pruned %s", counts)
        except Exception:  # noqa: BLE001 - retried next pass
            logger.exception("retention prune failed")

    async def _probe(self, inst, now: datetime) -> None:
        st = self._state(inst.id)
        try:
            await inst.client.system_status()
        except Exception as exc:  # noqa: BLE001 - any failure counts as unreachable
            st.consecutive_failures += 1
            st.last_error = str(exc) or type(exc).__name__
            if st.consecutive_failures >= self._down_after and st.up is not False:
                st.up = False
                st.since = now.isoformat()
                SONARR_UP.set(0, instance=inst.id)
                get_journal().emit(
                    kinds.INSTANCE_DOWN, f"{inst.name} is unreachable: {st.last_error}",
                    level="error", source="monitor", instance_id=inst.id,
                    data={"error": st.last_error, "failures": st.consecutive_failures},
                )
            return
        was_down = st.up is False
        if st.up is not True:
            st.up = True
            st.since = now.isoformat()
        st.consecutive_failures = 0
        st.last_error = None
        SONARR_UP.set(1, instance=inst.id)
        if was_down:
            get_journal().emit(kinds.INSTANCE_UP, f"{inst.name} is reachable again",
                               source="monitor", instance_id=inst.id)

    async def _check_health(self, inst, now: datetime) -> None:
        st = self._state(inst.id)
        try:
            items = await inst.client.health()
        except Exception as exc:  # noqa: BLE001 - the probe already tracks reachability
            logger.warning("health check failed for %s: %s", inst.id, exc)
            return
        current = sorted({
            (i.get("source") or "", i.get("type") or "", i.get("message") or "") for i in items
        })
        previous = sorted((i["source"], i["type"], i["message"]) for i in st.sonarr_health)
        st.sonarr_health = [{"source": s, "type": t, "message": m} for s, t, m in current]
        st.health_checked_at = now.isoformat()
        if current == previous:
            return
        problems = [m for _s, t, m in current if t in _PROBLEM_TYPES]
        if problems:
            message = f"{inst.name} health: {len(problems)} issue(s) — {problems[0]}"
        else:
            message = f"{inst.name} health checks are clear"
        get_journal().emit(
            kinds.INSTANCE_HEALTH_CHANGED, message,
            level="warn" if problems else "info", source="monitor", instance_id=inst.id,
            data={"items": st.sonarr_health},
        )

    def snapshot(self) -> list[dict]:
        out = []
        for inst in self.registry.all():
            st = self._state(inst.id)
            out.append({
                "id": inst.id,
                "name": inst.name,
                "up": st.up,
                "since": st.since,
                "consecutiveFailures": st.consecutive_failures,
                "lastError": st.last_error,
                "sonarrHealth": st.sonarr_health,
                "healthCheckedAt": st.health_checked_at,
                "client": inst.client.stats.snapshot(),
            })
        return out

    async def run(self) -> None:
        logger.info("monitor loop starting (interval=%ss)", self._interval)
        while not self._stop.is_set():
            try:
                await self.step()
            except Exception:  # noqa: BLE001 - never let observation die
                logger.exception("monitor step failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
