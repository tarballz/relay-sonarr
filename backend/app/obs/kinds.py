"""The closed vocabulary of journal event kinds.

Kinds are dotted: the first segment is the group the UI filters on. Emitting a
kind not listed here raises, so a typo fails a test instead of polluting data.
"""
from __future__ import annotations

TICK_FINISHED = "tick.finished"
TICK_FAILED = "tick.failed"
LOOP_STARTED = "loop.started"
LOOP_STOPPED = "loop.stopped"

POLL_TRANSITION = "poll.transition"
POLL_INSTANCE_ERROR = "poll.instance_error"

SWEEP_STALLED_REMOVED = "sweep.stalled.removed"
SWEEP_DANGEROUS_REMOVED = "sweep.dangerous.removed"
SWEEP_SEARCH_STALL_REVERTED = "sweep.search_stall.reverted"
SWEEP_FAILED = "sweep.failed"

AVAILABILITY_CHANGED = "availability.changed"
AVAILABILITY_ZERO_SPIKE = "availability.zero_spike"

INSTANCE_DOWN = "instance.down"
INSTANCE_UP = "instance.up"
INSTANCE_HEALTH_CHANGED = "instance.health_changed"

CONFIG_DEFAULTS_CHANGED = "config.defaults_changed"
CONFIG_POLICY_CHANGED = "config.policy_changed"
CONFIG_CHAINS_CHANGED = "config.chains_changed"
SERIES_PAUSED = "series.paused"
SERIES_RESUMED = "series.resumed"
SERIES_REMOVED = "series.removed"

ALL = frozenset(v for k, v in dict(globals()).items() if k.isupper() and isinstance(v, str))

LEVELS = ("debug", "info", "warn", "error")


def group(kind: str) -> str:
    return kind.split(".", 1)[0]
