"""Per-tick counters, collected implicitly through ``context.tick_stats``."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TickStats:
    phases: dict = field(default_factory=dict)
    errors: int = 0
    transitions: int = 0
    swept: int = 0
    live: dict = field(default_factory=dict)     # instance -> live availability checks
    cached: dict = field(default_factory=dict)   # instance -> cache hits
    zero: dict = field(default_factory=dict)     # instance -> live checks with 0 releases
    degraded: dict = field(default_factory=dict)  # instance -> checks discarded (indexers down)

    def availability(self, instance_id: str, result: str) -> None:
        if result == "cached":
            self.cached[instance_id] = self.cached.get(instance_id, 0) + 1
            return
        if result == "degraded":
            # Not a live verdict: the search reached no working indexer, so it
            # must not count toward the zero-spike ratio it would otherwise skew.
            self.degraded[instance_id] = self.degraded.get(instance_id, 0) + 1
            return
        self.live[instance_id] = self.live.get(instance_id, 0) + 1
        if result == "zero":
            self.zero[instance_id] = self.zero.get(instance_id, 0) + 1

    def zero_spikes(self, *, min_live: int = 10, ratio: float = 0.8) -> dict[str, tuple[int, int]]:
        """Instances where most live searches found nothing — likely an indexer outage."""
        return {
            iid: (self.zero.get(iid, 0), n)
            for iid, n in self.live.items()
            if n >= min_live and self.zero.get(iid, 0) / n >= ratio
        }

    def to_phases(self) -> dict:
        out = dict(self.phases)
        out["availability"] = {
            "live": sum(self.live.values()),
            "cached": sum(self.cached.values()),
            "liveBy": dict(self.live),
            "zero": dict(self.zero),
            "degraded": dict(self.degraded),
        }
        return out
