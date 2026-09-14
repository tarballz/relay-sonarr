"""Hand-rolled Prometheus metrics (text exposition format 0.0.4).

Deliberately tiny instead of a prometheus_client dependency: counters, gauges and
fixed-bucket histograms with labels, rendered on demand by ``GET /metrics``.
"""
from __future__ import annotations

import threading

DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)


def _escape(value) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt(value: float) -> str:
    value = float(value)
    return str(int(value)) if value.is_integer() else repr(round(value, 10))


def _labels(names: tuple, values: tuple, extra: list[tuple] | None = None) -> str:
    pairs = list(zip(names, values)) + (extra or [])
    if not pairs:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in pairs) + "}"


class _Metric:
    kind = ""

    def __init__(self, name: str, help: str, labelnames: tuple = ()):
        self.name = name
        self.help = help
        self.labelnames = tuple(labelnames)
        self._lock = threading.Lock()

    def _key(self, labels: dict) -> tuple:
        if set(labels) != set(self.labelnames):
            raise ValueError(
                f"{self.name}: expected labels {self.labelnames}, got {tuple(sorted(labels))}"
            )
        return tuple(str(labels[n]) for n in self.labelnames)

    def lines(self) -> list[str]:
        return [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"] + self.samples()

    def samples(self) -> list[str]:
        raise NotImplementedError


class _Valued(_Metric):
    def __init__(self, name, help, labelnames=()):
        super().__init__(name, help, labelnames)
        self._values: dict[tuple, float] = {}

    def value(self, **labels) -> float:
        return self._values.get(self._key(labels), 0.0)

    def samples(self) -> list[str]:
        with self._lock:
            items = sorted(self._values.items())
        return [f"{self.name}{_labels(self.labelnames, k)} {_fmt(v)}" for k, v in items]


class Counter(_Valued):
    kind = "counter"

    def inc(self, amount: float = 1.0, **labels) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount


class Gauge(_Valued):
    kind = "gauge"

    def set(self, value: float, **labels) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = float(value)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


class Histogram(_Metric):
    kind = "histogram"

    def __init__(self, name, help, labelnames=(), buckets=DEFAULT_BUCKETS):
        super().__init__(name, help, labelnames)
        self.buckets = tuple(sorted(buckets))
        self._series: dict[tuple, list] = {}  # key -> [bucket_counts, sum, count]

    def observe(self, value: float, **labels) -> None:
        key = self._key(labels)
        with self._lock:
            series = self._series.setdefault(key, [[0] * len(self.buckets), 0.0, 0])
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    series[0][i] += 1
            series[1] += value
            series[2] += 1

    def samples(self) -> list[str]:
        with self._lock:
            items = sorted((k, (list(c), s, n)) for k, (c, s, n) in self._series.items())
        out: list[str] = []
        for key, (counts, total, n) in items:
            for bound, count in zip(self.buckets, counts):
                out.append(f"{self.name}_bucket{_labels(self.labelnames, key, [('le', _fmt(bound))])} {count}")
            out.append(f"{self.name}_bucket{_labels(self.labelnames, key, [('le', '+Inf')])} {n}")
            out.append(f"{self.name}_sum{_labels(self.labelnames, key)} {_fmt(total)}")
            out.append(f"{self.name}_count{_labels(self.labelnames, key)} {n}")
        return out


class MetricsRegistry:
    def __init__(self):
        self._metrics: dict[str, _Metric] = {}

    def _add(self, metric):
        if metric.name in self._metrics:
            raise ValueError(f"duplicate metric: {metric.name}")
        self._metrics[metric.name] = metric
        return metric

    def counter(self, name: str, help: str, labelnames: tuple = ()) -> Counter:
        return self._add(Counter(name, help, labelnames))

    def gauge(self, name: str, help: str, labelnames: tuple = ()) -> Gauge:
        return self._add(Gauge(name, help, labelnames))

    def histogram(self, name: str, help: str, labelnames: tuple = (),
                  buckets: tuple = DEFAULT_BUCKETS) -> Histogram:
        return self._add(Histogram(name, help, labelnames, buckets))

    def render(self) -> str:
        lines: list[str] = []
        for metric in self._metrics.values():
            lines.extend(metric.lines())
        return "\n".join(lines) + "\n"


METRICS = MetricsRegistry()

TICK_TOTAL = METRICS.counter("relay_tick_total", "Reconciler ticks by final status.", ("status",))
TICK_DURATION = METRICS.histogram("relay_tick_duration_seconds", "Reconciler tick wall time.")
LAST_TICK_TS = METRICS.gauge("relay_last_tick_timestamp_seconds", "Unix time the last tick finished.")
RECONCILER_HEALTHY = METRICS.gauge(
    "relay_reconciler_healthy", "1 when the reconciler loop is alive or disabled.")
PLACEMENT_EPISODES = METRICS.gauge(
    "relay_placement_episodes", "Tracked episodes by placement state.", ("state",))
SERIES_INTENTS = METRICS.gauge("relay_series_intents", "Orchestrated series.", ("paused",))
SONARR_REQUESTS = METRICS.counter(
    "relay_sonarr_requests_total", "Sonarr API calls by outcome.",
    ("instance", "method", "endpoint", "outcome"))
SONARR_DURATION = METRICS.histogram(
    "relay_sonarr_request_duration_seconds", "Sonarr API call latency.", ("instance",))
SONARR_UP = METRICS.gauge("relay_sonarr_up", "1 when the Sonarr instance answers probes.", ("instance",))
AVAILABILITY_CHECKS = METRICS.counter(
    "relay_availability_checks_total", "Availability verdicts by result.", ("instance", "result"))
SWEEP_REMOVED = METRICS.counter("relay_sweep_removed_total", "Items removed by sweeps.", ("sweep",))
EVENTS_TOTAL = METRICS.counter("relay_events_total", "Journal events emitted.", ("group", "level"))
