# Observability Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Relay record what it did and why — a persisted event journal and tick history, a Prometheus `/metrics` endpoint, instrumented Sonarr calls, an always-on instance monitor, and context-enriched logs — without changing orchestration behavior.

**Architecture:** New `app/obs/` package (context vars, event kinds, journal, metrics, logging, SSE, tick stats). A process-wide journal (like the metrics registry) lets poller/placement/sweeps emit events without signature changes. Reconciler ticks persist to a `tick` table; health is derived from it. A `Monitor` background task probes Sonarr and runs retention independent of the reconciler.

**Tech Stack:** Python 3.11, FastAPI, httpx, stdlib sqlite3, pytest + pytest-asyncio (`asyncio_mode=auto`) + respx.

**Spec:** `docs/superpowers/specs/2026-09-14-observability-foundation-design.md`

## Global Constraints

- No new runtime dependencies (runtime deps stay: fastapi, uvicorn, httpx, pydantic, pyyaml, python-jose).
- No orchestration behavior change: same Sonarr calls, same placement transitions, same sweep decisions.
- Migrations additive only (`CREATE TABLE IF NOT EXISTS`, `_ensure_column`, `_run_once`).
- Existing API response fields are preserved; only fields are added.
- Never use a tvdb id as a metric label.
- TDD: every task writes its failing test first. Run tests from `backend/`: `uv run pytest ...`.
- Commits: conventional prefix, **no AI attribution / Co-Authored-By lines**.
- Timestamps are ISO-8601 strings from `datetime.isoformat()` on tz-aware UTC datetimes.

## File map

| File | Responsibility |
|---|---|
| `app/db.py` (modify) | `tick`/`event` tables, `operation.tick_id`, `execute_batch`, `execute_count`, interrupted-tick repair |
| `app/store/ticks.py` (new) | tick rows: start/finish/get/recent/last_completed/consecutive_failures/count |
| `app/store/events.py` (new) | event row insert SQL, dict mapping, filtered keyset queries |
| `app/store/summary.py` (new) | placement/intent counts + DB-backed gauges |
| `app/obs/metrics.py` (new) | hand-rolled Counter/Gauge/Histogram + text exposition + standard metrics |
| `app/obs/context.py` (new) | contextvars + `bind()` |
| `app/obs/kinds.py` (new) | closed event-kind vocabulary |
| `app/obs/journal.py` (new) | buffered Journal, NullJournal, subscriptions, global accessor |
| `app/obs/stats.py` (new) | `TickStats` collector |
| `app/obs/logging.py` (new) | `ContextFilter`, `JsonFormatter`, `configure_logging` |
| `app/obs/sse.py` (new) | replay-then-live SSE generator |
| `app/sonarr/client.py` (modify) | single `_request`, shared per-loop AsyncClient, `ClientStats`, metrics, `health()`, `aclose()` |
| `app/sonarr/registry.py` (modify) | client `name=id`, `aclose()` |
| `app/store/operations.py` (modify) | N+1 fix, `get()`, `tick_id` stamping |
| `app/services/poller.py`, `app/services/placement.py` (modify) | event emission + stats |
| `app/services/retention.py` (new) | chunked pruning, daily gate |
| `app/services/monitor.py` (new) | probe loop, Sonarr health diff, retention |
| `app/reconciler.py` (modify) | persisted ticks, phases, lock, `run_manual`, async `status()` |
| `app/api/observability.py` (new) | `/metrics`, `/api/events`, `/api/events/stream`, `/api/ticks`, `/api/operations/{id}`, `/api/summary` |
| `app/api/{catalog,policy,settings}.py`, `app/auth.py`, `app/state.py`, `app/main.py` (modify) | 409 tick, async status, audit, exemptions, deps, lifespan |
| `tests/conftest.py` (new) | autouse journal reset + `db`/`journal` fixtures |

---

### Task 1: Tick & event schema, batch writes, tick store

**Files:**
- Modify: `backend/app/db.py`
- Create: `backend/app/store/ticks.py`
- Test: `backend/tests/test_ticks_store.py`, `backend/tests/test_migrations.py` (append)

**Interfaces:**
- Produces:
  - `Database.execute_batch(sql: str, rows: list[tuple]) -> list[int]`
  - `Database.execute_count(sql: str, params: tuple = ()) -> int` (rowcount)
  - `ticks.start(db, *, trigger: str, started_at: str) -> int`
  - `ticks.finish(db, tick_id: int, *, finished_at: str, duration_ms: int, status: str, series_count: int, actions: int, transitions: int, swept: int, errors: int, error: str | None, phases: dict) -> None`
  - `ticks.get(db, tick_id) -> dict | None`, `ticks.recent(db, limit=96) -> list[dict]`, `ticks.last_completed(db) -> dict | None`, `ticks.consecutive_failures(db) -> int`, `ticks.count(db) -> int`
  - Tick dict keys: `id, trigger, startedAt, finishedAt, durationMs, status, seriesCount, actions, transitions, swept, errors, error, phases`

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_ticks_store.py`:

```python
"""Persisted reconciler tick history."""
from app.db import Database
from app.store import ticks


def _db(tmp_path):
    return Database(str(tmp_path / "relay.db"))


async def _finish(db, tick_id, status, *, finished_at="2026-09-14T00:01:00+00:00", error=None):
    await ticks.finish(
        db, tick_id, finished_at=finished_at, duration_ms=60000, status=status,
        series_count=3, actions=2, transitions=1, swept=0, errors=0 if status == "ok" else 1,
        error=error, phases={"poll": {"ms": 12}},
    )


async def test_start_finish_round_trip(tmp_path):
    db = _db(tmp_path)
    tid = await ticks.start(db, trigger="schedule", started_at="2026-09-14T00:00:00+00:00")
    running = await ticks.get(db, tid)
    assert running["status"] == "running" and running["finishedAt"] is None

    await _finish(db, tid, "ok")
    t = await ticks.get(db, tid)
    assert t == {
        "id": tid, "trigger": "schedule",
        "startedAt": "2026-09-14T00:00:00+00:00", "finishedAt": "2026-09-14T00:01:00+00:00",
        "durationMs": 60000, "status": "ok", "seriesCount": 3, "actions": 2,
        "transitions": 1, "swept": 0, "errors": 0, "error": None,
        "phases": {"poll": {"ms": 12}},
    }
    assert await ticks.get(db, 999) is None


async def test_recent_newest_first_and_last_completed_skips_running(tmp_path):
    db = _db(tmp_path)
    a = await ticks.start(db, trigger="schedule", started_at="t1")
    await _finish(db, a, "ok")
    b = await ticks.start(db, trigger="manual", started_at="t2")  # still running
    assert [t["id"] for t in await ticks.recent(db, limit=10)] == [b, a]
    assert (await ticks.last_completed(db))["id"] == a
    assert await ticks.count(db) == 2


async def test_consecutive_failures_counts_leading_failed(tmp_path):
    db = _db(tmp_path)
    assert await ticks.consecutive_failures(db) == 0
    for status in ("failed", "ok", "failed", "degraded", "failed", "failed"):
        await _finish(db, await ticks.start(db, trigger="schedule", started_at="t"), status)
    await ticks.start(db, trigger="schedule", started_at="t")  # running: ignored
    assert await ticks.consecutive_failures(db) == 2


async def test_execute_batch_and_count(tmp_path):
    db = _db(tmp_path)
    ids = await db.execute_batch(
        "INSERT INTO meta(key, value) VALUES(?, ?)", [("a", "1"), ("b", "2")]
    )
    assert len(ids) == 2 and ids[1] == ids[0] + 1
    assert await db.execute_count("DELETE FROM meta WHERE key IN ('a', 'b')") == 2
```

Append to `backend/tests/test_migrations.py`:

```python
def test_ticks_left_running_are_marked_interrupted_on_open(tmp_path):
    path = str(tmp_path / "relay.db")
    db = Database(path)
    db._conn.execute(
        "INSERT INTO tick(trigger, started_at, status) VALUES('schedule', '2026-09-14T00:00:00+00:00', 'running')"
    )
    db._conn.commit()
    db.close()

    db = Database(path)
    row = db._conn.execute("SELECT status, error, finished_at FROM tick").fetchone()
    assert row["status"] == "failed"
    assert row["error"] == "interrupted (restart)"
    assert row["finished_at"] == "2026-09-14T00:00:00+00:00"
    db.close()


def test_operation_has_tick_id_column(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    cols = {r["name"] for r in db._conn.execute("PRAGMA table_info(operation)")}
    assert "tick_id" in cols
    db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ticks_store.py tests/test_migrations.py -v`
Expected: FAIL — `ModuleNotFoundError: app.store.ticks` / `no such table: tick`.

- [ ] **Step 3: Implement schema + helpers in `app/db.py`**

Append to the `SCHEMA` string, before the `CREATE INDEX` lines:

```sql
-- One row per reconciler tick: persisted health history (survives restarts).
CREATE TABLE IF NOT EXISTS tick (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  trigger      TEXT NOT NULL,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  duration_ms  INTEGER,
  status       TEXT NOT NULL DEFAULT 'running',
  series_count INTEGER NOT NULL DEFAULT 0,
  actions      INTEGER NOT NULL DEFAULT 0,
  transitions  INTEGER NOT NULL DEFAULT 0,
  swept        INTEGER NOT NULL DEFAULT 0,
  errors       INTEGER NOT NULL DEFAULT 0,
  error        TEXT,
  phases_json  TEXT
);

-- Append-only journal of what Relay observed and did (see app/obs/kinds.py).
CREATE TABLE IF NOT EXISTS event (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT NOT NULL,
  kind         TEXT NOT NULL,
  level        TEXT NOT NULL DEFAULT 'info',
  source       TEXT NOT NULL DEFAULT 'system',
  tick_id      INTEGER,
  operation_id INTEGER,
  tvdb_id      INTEGER,
  season       INTEGER,
  episode      INTEGER,
  instance_id  TEXT,
  message      TEXT NOT NULL,
  data_json    TEXT
);
```

and after the existing indexes:

```sql
CREATE INDEX IF NOT EXISTS idx_tick_started ON tick(started_at);
CREATE INDEX IF NOT EXISTS idx_event_ts    ON event(ts);
CREATE INDEX IF NOT EXISTS idx_event_tvdb  ON event(tvdb_id, id);
CREATE INDEX IF NOT EXISTS idx_event_tick  ON event(tick_id);
CREATE INDEX IF NOT EXISTS idx_event_kind  ON event(kind, id);
CREATE INDEX IF NOT EXISTS idx_event_level ON event(level, id);
```

In `_migrate()`, after the `best_release_json` column line, add:

```python
        # Correlates an operation with the reconciler tick that started it.
        self._ensure_column("operation", "tick_id", "INTEGER")
```

In `__init__`, right after `self._migrate()`, add:

```python
            # A tick still 'running' at open means the process died mid-tick.
            self._conn.execute(
                "UPDATE tick SET status='failed', error='interrupted (restart)', "
                "finished_at=COALESCE(finished_at, started_at) WHERE status='running'"
            )
```

Add methods after `executemany`:

```python
    async def execute_batch(self, sql: str, rows: list[tuple]) -> list[int]:
        """One INSERT per row inside a single transaction; returns each row id."""
        def fn(conn):
            ids = [conn.execute(sql, row).lastrowid for row in rows]
            conn.commit()
            return ids
        return await self._run(fn)

    async def execute_count(self, sql: str, params: tuple = ()) -> int:
        """Run a write; returns the number of rows it changed."""
        def fn(conn):
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount
        return await self._run(fn)
```

- [ ] **Step 4: Create `app/store/ticks.py`**

```python
"""Tick repo: one row per reconciler pass — the persisted health history.

Health is derived from these rows (not process memory), so it survives restarts
and a partially failing tick (``degraded``) is distinguishable from a wedged loop.
"""
from __future__ import annotations

import json

from app.db import Database


def to_dict(row) -> dict:
    return {
        "id": row["id"],
        "trigger": row["trigger"],
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"],
        "durationMs": row["duration_ms"],
        "status": row["status"],
        "seriesCount": row["series_count"],
        "actions": row["actions"],
        "transitions": row["transitions"],
        "swept": row["swept"],
        "errors": row["errors"],
        "error": row["error"],
        "phases": json.loads(row["phases_json"]) if row["phases_json"] else {},
    }


async def start(db: Database, *, trigger: str, started_at: str) -> int:
    return await db.execute(
        "INSERT INTO tick(trigger, started_at, status) VALUES(?, ?, 'running')",
        (trigger, started_at),
    )


async def finish(db: Database, tick_id: int, *, finished_at: str, duration_ms: int,
                 status: str, series_count: int, actions: int, transitions: int,
                 swept: int, errors: int, error: str | None, phases: dict) -> None:
    await db.execute(
        "UPDATE tick SET finished_at=?, duration_ms=?, status=?, series_count=?, "
        "actions=?, transitions=?, swept=?, errors=?, error=?, phases_json=? WHERE id=?",
        (finished_at, duration_ms, status, series_count, actions, transitions, swept,
         errors, error, json.dumps(phases), tick_id),
    )


async def get(db: Database, tick_id: int) -> dict | None:
    row = await db.query_one("SELECT * FROM tick WHERE id=?", (tick_id,))
    return to_dict(row) if row else None


async def recent(db: Database, limit: int = 96) -> list[dict]:
    rows = await db.query("SELECT * FROM tick ORDER BY id DESC LIMIT ?", (limit,))
    return [to_dict(r) for r in rows]


async def last_completed(db: Database) -> dict | None:
    row = await db.query_one(
        "SELECT * FROM tick WHERE status != 'running' ORDER BY id DESC LIMIT 1"
    )
    return to_dict(row) if row else None


async def consecutive_failures(db: Database) -> int:
    """How many of the most recent completed ticks failed outright, in a row."""
    rows = await db.query(
        "SELECT status FROM tick WHERE status != 'running' ORDER BY id DESC LIMIT 100"
    )
    n = 0
    for r in rows:
        if r["status"] != "failed":
            break
        n += 1
    return n


async def count(db: Database) -> int:
    row = await db.query_one("SELECT COUNT(*) AS n FROM tick")
    return row["n"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_ticks_store.py tests/test_migrations.py tests/test_store.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/db.py backend/app/store/ticks.py backend/tests/test_ticks_store.py backend/tests/test_migrations.py
git commit -m "feat(db): tick and event tables with a persisted tick store"
```

---

### Task 2: Hand-rolled Prometheus metrics

**Files:**
- Create: `backend/app/obs/__init__.py` (empty), `backend/app/obs/metrics.py`
- Test: `backend/tests/test_metrics.py`

**Interfaces:**
- Produces: `MetricsRegistry` with `.counter(name, help, labelnames=())`, `.gauge(...)`, `.histogram(name, help, labelnames=(), buckets=DEFAULT_BUCKETS)`, `.render() -> str`; `Counter.inc(amount=1.0, **labels)`, `Counter.value(**labels)`; `Gauge.set(value, **labels)`, `Gauge.value(**labels)`, `Gauge.clear()`; `Histogram.observe(value, **labels)`.
- Module-level `METRICS` and standard metrics: `TICK_TOTAL(status)`, `TICK_DURATION`, `LAST_TICK_TS`, `RECONCILER_HEALTHY`, `PLACEMENT_EPISODES(state)`, `SERIES_INTENTS(paused)`, `SONARR_REQUESTS(instance, method, endpoint, outcome)`, `SONARR_DURATION(instance)`, `SONARR_UP(instance)`, `AVAILABILITY_CHECKS(instance, result)`, `SWEEP_REMOVED(sweep)`, `EVENTS_TOTAL(group, level)`.

- [ ] **Step 1: Write the failing test**

`backend/tests/test_metrics.py`:

```python
"""Prometheus text exposition without a client library."""
import pytest

from app.obs import metrics as m


def test_counter_and_gauge_render_with_escaped_labels():
    reg = m.MetricsRegistry()
    c = reg.counter("relay_things_total", "Things seen.", ("kind",))
    g = reg.gauge("relay_temp", "A gauge.")
    c.inc(kind='say "hi"\n')
    c.inc(2, kind="plain")
    g.set(1.5)

    assert reg.render() == (
        "# HELP relay_things_total Things seen.\n"
        "# TYPE relay_things_total counter\n"
        'relay_things_total{kind="plain"} 2\n'
        'relay_things_total{kind="say \\"hi\\"\\n"} 1\n'
        "# HELP relay_temp A gauge.\n"
        "# TYPE relay_temp gauge\n"
        "relay_temp 1.5\n"
    )
    assert c.value(kind="plain") == 2


def test_histogram_buckets_are_cumulative():
    reg = m.MetricsRegistry()
    h = reg.histogram("relay_lat_seconds", "Latency.", ("instance",), buckets=(0.1, 1))
    h.observe(0.05, instance="4k")
    h.observe(0.5, instance="4k")
    h.observe(5, instance="4k")
    assert reg.render() == (
        "# HELP relay_lat_seconds Latency.\n"
        "# TYPE relay_lat_seconds histogram\n"
        'relay_lat_seconds_bucket{instance="4k",le="0.1"} 1\n'
        'relay_lat_seconds_bucket{instance="4k",le="1"} 2\n'
        'relay_lat_seconds_bucket{instance="4k",le="+Inf"} 3\n'
        'relay_lat_seconds_sum{instance="4k"} 5.55\n'
        'relay_lat_seconds_count{instance="4k"} 3\n'
    )


def test_wrong_labels_and_duplicate_names_raise():
    reg = m.MetricsRegistry()
    c = reg.counter("relay_x_total", "x", ("a",))
    with pytest.raises(ValueError):
        c.inc(b="1")
    with pytest.raises(ValueError):
        reg.counter("relay_x_total", "again")


def test_gauge_clear_drops_stale_series():
    reg = m.MetricsRegistry()
    g = reg.gauge("relay_eps", "eps", ("state",))
    g.set(3, state="wanted")
    g.clear()
    g.set(1, state="imported")
    assert 'state="wanted"' not in reg.render()


def test_standard_metrics_registered():
    text = m.METRICS.render()
    for name in ("relay_tick_total", "relay_sonarr_requests_total", "relay_events_total",
                 "relay_placement_episodes", "relay_sonarr_up"):
        assert f"# TYPE {name} " in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_metrics.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.obs'`

- [ ] **Step 3: Implement `app/obs/metrics.py`** (and create empty `app/obs/__init__.py`)

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_metrics.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/obs/__init__.py backend/app/obs/metrics.py backend/tests/test_metrics.py
git commit -m "feat(obs): hand-rolled Prometheus metrics registry"
```

---

### Task 3: Correlation context, event kinds, journal, event store

**Files:**
- Create: `backend/app/obs/context.py`, `backend/app/obs/kinds.py`, `backend/app/obs/journal.py`, `backend/app/store/events.py`, `backend/tests/conftest.py`
- Test: `backend/tests/test_journal.py`

**Interfaces:**
- Consumes: `Database.execute_batch` (Task 1), `EVENTS_TOTAL` (Task 2).
- Produces:
  - `context.bind(**values)` context manager over names `tick_id`, `operation_id`, `tvdb_id`, `tick_stats`; `context.current() -> {"tick_id", "operation_id", "tvdb_id"}`; ContextVars `context.tick_id`, `context.operation_id`, `context.tvdb_id`, `context.tick_stats`.
  - `kinds.*` constants (see file), `kinds.ALL`, `kinds.LEVELS`, `kinds.group(kind) -> str`.
  - `Journal(db, *, clock=None, max_buffer=1000)` with `emit(kind, message, *, level="info", source="system", tvdb_id=None, season=None, episode=None, instance_id=None, operation_id=None, data=None) -> None`, `pending() -> list[dict]`, `async flush() -> list[dict]`, `subscribe() -> Subscription`, `unsubscribe(sub)`, `async run(interval=1.0)`, `async aclose()`.
  - `NullJournal` (same surface, validates kinds, stores nothing), `Subscription` (`.queue`, `.offer(item)`), sentinel `RESYNC`, `set_journal(j | None)`, `get_journal()`.
  - `events.INSERT_SQL`, `events.from_row_tuple(event_id, row) -> dict`, `events.to_dict(row) -> dict`, `async events.query(db, *, kind=None, level=None, tvdb_id=None, tick_id=None, source=None, before=None, limit=100) -> list[dict]` (raises `ValueError` on unknown level), `async events.since(db, after_id, limit) -> list[dict]` (ascending), `async events.for_tick(db, tick_id) -> list[dict]` (ascending).
  - Event dict keys: `id, ts, kind, level, source, tickId, operationId, tvdbId, season, episode, instanceId, message, data`.
  - Fixtures: `db` (tmp Database), `journal` (real Journal installed globally); autouse reset of the global journal after every test.

- [ ] **Step 1: Write the failing tests**

`backend/tests/conftest.py`:

```python
"""Shared fixtures. Sonarr-mocking helpers still live in tests/test_chain.py."""
import pytest

from app.db import Database
from app.obs.journal import Journal, set_journal


@pytest.fixture(autouse=True)
def _reset_journal():
    """The journal is process-wide; never let one test's journal leak into another."""
    yield
    set_journal(None)


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "relay.db"))
    yield database
    database.close()


@pytest.fixture
def journal(db):
    j = Journal(db)
    set_journal(j)
    return j
```

`backend/tests/test_journal.py`:

```python
"""Buffered event journal: emit, flush, subscribe, query."""
from datetime import datetime, timezone

import pytest

from app.obs import context, kinds
from app.obs.journal import RESYNC, Journal, NullJournal, Subscription, get_journal, set_journal
from app.store import events as events_store

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


async def test_emit_fills_context_and_flush_persists(db):
    journal = Journal(db, clock=lambda: NOW)
    with context.bind(tick_id=7, tvdb_id=99):
        journal.emit(kinds.POLL_TRANSITION, "S01E02 wanted → grabbed", source="poller",
                     season=1, episode=2, instance_id="4k", data={"to": "grabbed"})
    assert context.current() == {"tick_id": None, "operation_id": None, "tvdb_id": None}
    assert journal.pending()[0]["tickId"] == 7

    persisted = await journal.flush()

    expected = {
        "id": 1, "ts": NOW.isoformat(), "kind": "poll.transition", "level": "info",
        "source": "poller", "tickId": 7, "operationId": None, "tvdbId": 99,
        "season": 1, "episode": 2, "instanceId": "4k",
        "message": "S01E02 wanted → grabbed", "data": {"to": "grabbed"},
    }
    assert persisted == [expected]
    assert await events_store.query(db) == [expected]
    assert journal.pending() == []
    assert await journal.flush() == []


async def test_explicit_ids_override_context(db):
    journal = Journal(db)
    with context.bind(tvdb_id=1, operation_id=2):
        journal.emit(kinds.SERIES_PAUSED, "paused", tvdb_id=5, operation_id=6)
    ev = journal.pending()[0]
    assert (ev["tvdbId"], ev["operationId"]) == (5, 6)


def test_unknown_kind_or_level_raises_even_for_null_journal(db):
    for j in (Journal(db), NullJournal()):
        with pytest.raises(ValueError):
            j.emit("poll.typo", "x")
        with pytest.raises(ValueError):
            j.emit(kinds.POLL_TRANSITION, "x", level="loud")


def test_global_accessor_defaults_to_null(db):
    assert isinstance(get_journal(), NullJournal)
    j = Journal(db)
    set_journal(j)
    assert get_journal() is j
    set_journal(None)
    assert isinstance(get_journal(), NullJournal)


async def test_subscribers_receive_persisted_events_with_ids(db):
    journal = Journal(db)
    sub = journal.subscribe()
    journal.emit(kinds.INSTANCE_UP, "4K is back", instance_id="4k")
    await journal.flush()
    got = sub.queue.get_nowait()
    assert got["id"] == 1 and got["kind"] == "instance.up"

    journal.unsubscribe(sub)
    journal.emit(kinds.INSTANCE_UP, "again")
    await journal.flush()
    assert sub.queue.empty()


def test_subscription_overflow_leaves_a_resync_marker():
    sub = Subscription(maxsize=2)
    sub.offer({"id": 1})
    sub.offer({"id": 2})
    sub.offer({"id": 3})   # overflow
    sub.offer({"id": 4})   # ignored once overflowed
    assert sub.queue.get_nowait() == {"id": 2}
    assert sub.queue.get_nowait() is RESYNC
    assert sub.queue.empty()


def test_full_buffer_drops_oldest_low_level_event_first(db):
    journal = Journal(db, max_buffer=2)
    journal.emit(kinds.TICK_FAILED, "bad", level="error")
    journal.emit(kinds.INSTANCE_UP, "first info")
    journal.emit(kinds.INSTANCE_UP, "second info")
    assert [e["message"] for e in journal.pending()] == ["bad", "second info"]


async def test_failed_flush_keeps_events_for_retry(db, monkeypatch):
    journal = Journal(db)
    journal.emit(kinds.INSTANCE_UP, "keep me")

    async def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "execute_batch", boom)
    assert await journal.flush() == []
    assert [e["message"] for e in journal.pending()] == ["keep me"]


async def test_query_filters_and_keyset_paging(db):
    journal = Journal(db)
    journal.emit(kinds.POLL_TRANSITION, "a", tvdb_id=1)                    # id 1
    journal.emit(kinds.POLL_INSTANCE_ERROR, "b", level="warn", source="poller")  # id 2
    journal.emit(kinds.SWEEP_SEARCH_STALL_REVERTED, "c", tvdb_id=1)         # id 3
    journal.emit(kinds.TICK_FAILED, "d", level="error")                    # id 4
    with context.bind(tick_id=9):
        journal.emit(kinds.TICK_FINISHED, "e")                             # id 5
    await journal.flush()

    ids = lambda rows: [r["id"] for r in rows]
    assert ids(await events_store.query(db)) == [5, 4, 3, 2, 1]
    assert ids(await events_store.query(db, kind="poll.")) == [2, 1]
    assert ids(await events_store.query(db, kind="sweep.search_stall")) == [3]
    assert ids(await events_store.query(db, level="warn")) == [4, 2]
    assert ids(await events_store.query(db, tvdb_id=1)) == [3, 1]
    assert ids(await events_store.query(db, source="poller")) == [2]
    assert ids(await events_store.query(db, tick_id=9)) == [5]
    assert ids(await events_store.query(db, before=3, limit=1)) == [2]
    assert ids(await events_store.since(db, 3, limit=10)) == [4, 5]
    assert ids(await events_store.for_tick(db, 9)) == [5]
    with pytest.raises(ValueError):
        await events_store.query(db, level="loud")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_journal.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.obs.journal'` (conftest import error).

- [ ] **Step 3: Create `app/obs/context.py`**

```python
"""Correlation context carried implicitly through async code.

asyncio tasks (and ``gather`` children) copy the current context, so anything
logged or journaled inside a tick automatically carries that tick's id.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

tick_id: ContextVar[int | None] = ContextVar("tick_id", default=None)
operation_id: ContextVar[int | None] = ContextVar("operation_id", default=None)
tvdb_id: ContextVar[int | None] = ContextVar("tvdb_id", default=None)
tick_stats: ContextVar[object | None] = ContextVar("tick_stats", default=None)

_VARS = {
    "tick_id": tick_id,
    "operation_id": operation_id,
    "tvdb_id": tvdb_id,
    "tick_stats": tick_stats,
}


@contextmanager
def bind(**values):
    """Set context values for the duration of the block, restoring them after."""
    tokens = []
    try:
        for name, value in values.items():
            var = _VARS[name]
            tokens.append((var, var.set(value)))
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def current() -> dict:
    return {"tick_id": tick_id.get(), "operation_id": operation_id.get(), "tvdb_id": tvdb_id.get()}
```

- [ ] **Step 4: Create `app/obs/kinds.py`**

```python
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
```

- [ ] **Step 5: Create `app/store/events.py`**

```python
"""Event repo: the persisted journal, queried newest-first with keyset paging."""
from __future__ import annotations

import json

from app.db import Database

_COLUMNS = ("ts", "kind", "level", "source", "tick_id", "operation_id", "tvdb_id",
            "season", "episode", "instance_id", "message", "data_json")

INSERT_SQL = (
    f"INSERT INTO event({', '.join(_COLUMNS)}) "
    f"VALUES({', '.join('?' * len(_COLUMNS))})"
)

LEVEL_ORDER = ("debug", "info", "warn", "error")


def _shape(event_id, v: dict) -> dict:
    return {
        "id": event_id,
        "ts": v["ts"],
        "kind": v["kind"],
        "level": v["level"],
        "source": v["source"],
        "tickId": v["tick_id"],
        "operationId": v["operation_id"],
        "tvdbId": v["tvdb_id"],
        "season": v["season"],
        "episode": v["episode"],
        "instanceId": v["instance_id"],
        "message": v["message"],
        "data": json.loads(v["data_json"]) if v["data_json"] else None,
    }


def from_row_tuple(event_id, row: tuple) -> dict:
    """Shape an INSERT_SQL parameter tuple (plus its assigned id) as an event dict."""
    return _shape(event_id, dict(zip(_COLUMNS, row)))


def to_dict(row) -> dict:
    return _shape(row["id"], {c: row[c] for c in _COLUMNS})


def _like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


async def query(db: Database, *, kind: str | None = None, level: str | None = None,
                tvdb_id: int | None = None, tick_id: int | None = None,
                source: str | None = None, before: int | None = None,
                limit: int = 100) -> list[dict]:
    """Newest-first events. ``kind`` is a prefix; ``level`` is a minimum."""
    where: list[str] = []
    params: list = []
    if kind:
        where.append("kind LIKE ? ESCAPE '\\'")
        params.append(_like_prefix(kind))
    if level:
        if level not in LEVEL_ORDER:
            raise ValueError(f"unknown level: {level}")
        allowed = LEVEL_ORDER[LEVEL_ORDER.index(level):]
        where.append(f"level IN ({', '.join('?' * len(allowed))})")
        params.extend(allowed)
    for column, value in (("tvdb_id", tvdb_id), ("tick_id", tick_id), ("source", source)):
        if value is not None:
            where.append(f"{column}=?")
            params.append(value)
    if before is not None:
        where.append("id < ?")
        params.append(before)
    sql = "SELECT * FROM event"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [to_dict(r) for r in await db.query(sql, tuple(params))]


async def since(db: Database, after_id: int, limit: int) -> list[dict]:
    rows = await db.query(
        "SELECT * FROM event WHERE id > ? ORDER BY id ASC LIMIT ?", (after_id, limit)
    )
    return [to_dict(r) for r in rows]


async def for_tick(db: Database, tick_id: int) -> list[dict]:
    rows = await db.query("SELECT * FROM event WHERE tick_id=? ORDER BY id ASC", (tick_id,))
    return [to_dict(r) for r in rows]
```

- [ ] **Step 6: Create `app/obs/journal.py`**

```python
"""Buffered, process-wide event journal.

``emit`` is synchronous and cheap (append to a buffer) so it can be called from
anywhere — poller, placement, sweeps — without threading a journal through their
signatures. A background flusher (and the reconciler, at the end of each tick
phase) batches the buffer into SQLite and then publishes the persisted rows, ids
included, to live subscribers (the SSE stream).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from app.obs import context, kinds
from app.obs.metrics import EVENTS_TOTAL
from app.store import events as events_store

logger = logging.getLogger(__name__)

RESYNC = object()  # queued to a subscriber that fell behind: it must re-read from the DB


class Subscription:
    def __init__(self, maxsize: int = 500):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize)
        self.overflowed = False

    def offer(self, item) -> None:
        if self.overflowed:
            return
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.overflowed = True
            try:
                self.queue.get_nowait()  # make room for the marker
            except asyncio.QueueEmpty:
                pass
            self.queue.put_nowait(RESYNC)


def _validate(kind: str, level: str) -> None:
    if kind not in kinds.ALL:
        raise ValueError(f"unknown event kind: {kind!r}")
    if level not in kinds.LEVELS:
        raise ValueError(f"unknown event level: {level!r}")


class NullJournal:
    """Default journal: validates like the real one, records nothing."""

    def emit(self, kind: str, message: str, *, level: str = "info", **_fields) -> None:
        _validate(kind, level)

    def pending(self) -> list[dict]:
        return []

    async def flush(self) -> list[dict]:
        return []

    def subscribe(self) -> Subscription:
        return Subscription()

    def unsubscribe(self, sub: Subscription) -> None:
        return None

    async def run(self, interval: float = 1.0) -> None:
        return None

    async def aclose(self) -> None:
        return None


class Journal:
    def __init__(self, db, *, clock=None, max_buffer: int = 1000):
        self.db = db
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_buffer = max_buffer
        self._buffer: list[tuple] = []
        self._subs: set[Subscription] = set()
        self._flush_lock = asyncio.Lock()
        self._stop = asyncio.Event()

    def emit(self, kind: str, message: str, *, level: str = "info", source: str = "system",
             tvdb_id: int | None = None, season: int | None = None,
             episode: int | None = None, instance_id: str | None = None,
             operation_id: int | None = None, data: dict | None = None) -> None:
        """Buffer one event. Raises only on an unknown kind/level (a programming error)."""
        _validate(kind, level)
        ctx = context.current()
        row = (
            self._clock().isoformat(), kind, level, source, ctx["tick_id"],
            operation_id if operation_id is not None else ctx["operation_id"],
            tvdb_id if tvdb_id is not None else ctx["tvdb_id"],
            season, episode, instance_id, message,
            json.dumps(data, default=str) if data is not None else None,
        )
        if len(self._buffer) >= self._max_buffer:
            self._drop_one()
        self._buffer.append(row)
        EVENTS_TOTAL.inc(group=kinds.group(kind), level=level)

    def _drop_one(self) -> None:
        for i, row in enumerate(self._buffer):
            if row[2] in ("debug", "info"):
                del self._buffer[i]
                break
        else:
            del self._buffer[0]
        logger.warning("journal buffer full (%d); dropped an event", self._max_buffer)

    def pending(self) -> list[dict]:
        """Buffered, not-yet-persisted events (id is None)."""
        return [events_store.from_row_tuple(None, r) for r in self._buffer]

    async def flush(self) -> list[dict]:
        async with self._flush_lock:
            if not self._buffer:
                return []
            rows, self._buffer = self._buffer, []
            try:
                ids = await self.db.execute_batch(events_store.INSERT_SQL, rows)
            except Exception:  # noqa: BLE001 - keep the events and retry next flush
                logger.exception("journal flush failed; will retry")
                self._buffer = (rows + self._buffer)[-self._max_buffer:]
                return []
            persisted = [events_store.from_row_tuple(i, r) for i, r in zip(ids, rows)]
            for sub in list(self._subs):
                for event in persisted:
                    sub.offer(event)
            return persisted

    def subscribe(self) -> Subscription:
        sub = Subscription()
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs.discard(sub)

    async def run(self, interval: float = 1.0) -> None:
        """Periodic flusher; exits (after a final flush) once ``aclose`` is called."""
        while not self._stop.is_set():
            await self.flush()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        await self.flush()

    async def aclose(self) -> None:
        self._stop.set()
        await self.flush()


_current: Journal | NullJournal = NullJournal()


def set_journal(journal: Journal | NullJournal | None) -> None:
    global _current
    _current = journal if journal is not None else NullJournal()


def get_journal() -> Journal | NullJournal:
    return _current
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_journal.py -v && uv run pytest -q`
Expected: `test_journal.py` PASS; full suite still green (conftest must not break existing tests).

- [ ] **Step 8: Commit**

```bash
git add backend/app/obs/context.py backend/app/obs/kinds.py backend/app/obs/journal.py backend/app/store/events.py backend/tests/conftest.py backend/tests/test_journal.py
git commit -m "feat(obs): buffered event journal with correlation context"
```

---

### Task 4: Context-enriched logging

**Files:**
- Create: `backend/app/obs/logging.py`
- Modify: `backend/app/main.py:42-47` (`_configure_logging`)
- Test: `backend/tests/test_logging.py`

**Interfaces:**
- Consumes: `context.current()` (Task 3).
- Produces: `ContextFilter`, `JsonFormatter`, `TEXT_FORMAT`, `configure_logging(level: str = "INFO", fmt: str = "text") -> None`. Env: `LOG_LEVEL`, `LOG_FORMAT` (`text` | `json`).

- [ ] **Step 1: Write the failing test**

`backend/tests/test_logging.py`:

```python
"""Log records carry tick/operation/series context; optional JSON output."""
import io
import json
import logging

from app.obs import context
from app.obs.logging import ContextFilter, JsonFormatter, configure_logging


def _logger(formatter):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(ContextFilter())
    handler.setFormatter(formatter)
    log = logging.getLogger("test.obs.logging")
    log.handlers = [handler]
    log.propagate = False
    log.setLevel(logging.DEBUG)
    return log, stream


def test_text_format_appends_context_only_when_bound():
    log, stream = _logger(logging.Formatter("%(message)s%(ctx)s"))
    log.info("plain")
    with context.bind(tick_id=12, tvdb_id=81189):
        log.info("inside")
    assert stream.getvalue().splitlines() == ["plain", "inside [tick=12 tvdb=81189]"]


def test_json_formatter_includes_context_and_exception():
    log, stream = _logger(JsonFormatter())
    with context.bind(tick_id=3, operation_id=4):
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("failed")
    rec = json.loads(stream.getvalue())
    assert rec["level"] == "ERROR"
    assert rec["msg"] == "failed"
    assert rec["logger"] == "test.obs.logging"
    assert rec["tick_id"] == 3 and rec["operation_id"] == 4
    assert "tvdb_id" not in rec
    assert "ValueError: boom" in rec["exc"]


def test_configure_logging_replaces_only_its_own_handler():
    root = logging.getLogger()
    before_handlers = list(root.handlers)
    before_level = root.level
    try:
        configure_logging("DEBUG", "json")
        configure_logging("INFO", "text")
        ours = [h for h in root.handlers if getattr(h, "_relay", False)]
        assert len(ours) == 1
        assert not isinstance(ours[0].formatter, JsonFormatter)
        assert root.level == logging.INFO
        assert all(h in root.handlers for h in before_handlers)
    finally:
        for h in [h for h in root.handlers if getattr(h, "_relay", False)]:
            root.removeHandler(h)
        root.setLevel(before_level)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_logging.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.obs.logging'`

- [ ] **Step 3: Create `app/obs/logging.py`**

```python
"""Logging with correlation context: ``[tick=12 op=7 tvdb=81189]`` or JSON lines."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.obs import context

TEXT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s%(ctx)s"

_FIELDS = (("tick_id", "tick"), ("operation_id", "op"), ("tvdb_id", "tvdb"))


class ContextFilter(logging.Filter):
    """Copy the current correlation context onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = context.current()
        parts = []
        for key, short in _FIELDS:
            setattr(record, key, ctx[key])
            if ctx[key] is not None:
                parts.append(f"{short}={ctx[key]}")
        record.ctx = f" [{' '.join(parts)}]" if parts else ""
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, _short in _FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    """Install (or replace) Relay's root handler. Other handlers are left alone."""
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "_relay", False)]:
        root.removeHandler(handler)
    handler = logging.StreamHandler()
    handler._relay = True
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter() if fmt.lower() == "json" else logging.Formatter(TEXT_FORMAT))
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
```

- [ ] **Step 4: Wire it in `app/main.py`**

Replace `_configure_logging` with:

```python
def _configure_logging() -> None:
    configure_logging(
        os.environ.get("LOG_LEVEL", "INFO"),
        os.environ.get("LOG_FORMAT", "text"),
    )
```

and add the import `from app.obs.logging import configure_logging`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_logging.py tests/test_health.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/obs/logging.py backend/app/main.py backend/tests/test_logging.py
git commit -m "feat(obs): context-enriched text and JSON logging"
```

---

### Task 5: Instrumented Sonarr client

**Files:**
- Modify: `backend/app/sonarr/client.py:1-52` (imports, constructor, transport helpers; add `health()`, `aclose()`)
- Modify: `backend/app/sonarr/registry.py` (pass `name=cfg.id`; add `aclose()`)
- Test: `backend/tests/test_client_instrumentation.py`

**Interfaces:**
- Consumes: `SONARR_REQUESTS`, `SONARR_DURATION` (Task 2).
- Produces:
  - `SonarrClient(base_url, api_key, *, timeout=30.0, name=None, perf_counter=time.perf_counter, clock=time.time)`; attributes `name`, `stats: ClientStats`; `async health() -> list[dict]`; `async aclose()`.
  - `ClientStats(*, window=200, clock=time.time)` with `record(ms: float, ok: bool, error: str | None = None)` and `snapshot() -> {"calls", "p50Ms", "p95Ms", "errorRate5m", "lastOkAt", "lastError", "consecutiveFailures"}`.
  - `endpoint_template(path: str) -> str`.
  - `Registry.aclose()`.
- Behavior preserved: per-call `timeout` (the 180s `RELEASE_SEARCH_TIMEOUT`) and the 30s default; `raise_for_status`; `_delete` returns `None`. Existing `tests/test_client.py` must pass unchanged.

- [ ] **Step 1: Write the failing test**

`backend/tests/test_client_instrumentation.py`:

```python
"""Instrumented Sonarr client: one shared pool, per-call stats and metrics."""
import httpx
import pytest
import respx

from app.obs.metrics import SONARR_REQUESTS
from app.sonarr.client import ClientStats, SonarrClient, endpoint_template
from tests.test_chain import make_registry

BASE = "http://10.9.9.9:8989"


def _client(**kw):
    return SonarrClient(base_url=BASE, api_key="k", name="t1", **kw)


def test_endpoint_template_replaces_numeric_segments():
    assert endpoint_template("/series/12") == "/series/{id}"
    assert endpoint_template("/queue/7/x/8") == "/queue/{id}/x/{id}"
    assert endpoint_template("/episode") == "/episode"


@respx.mock
async def test_calls_share_one_async_client_until_closed():
    respx.get(f"{BASE}/api/v3/series").mock(return_value=httpx.Response(200, json=[]))
    c = _client()
    await c.list_series()
    first = c._http[1]
    await c.list_series()
    assert c._http[1] is first
    await c.aclose()
    assert first.is_closed and c._http is None


@respx.mock
async def test_success_and_http_error_recorded_in_stats_and_metrics():
    perf = iter([0.0, 0.25, 1.0, 1.5])
    c = _client(perf_counter=lambda: next(perf), clock=lambda: 1000.0)
    respx.get(f"{BASE}/api/v3/series/5").mock(return_value=httpx.Response(200, json={"id": 5}))
    respx.get(f"{BASE}/api/v3/series/6").mock(return_value=httpx.Response(503))
    labels = dict(instance="t1", method="GET", endpoint="/series/{id}")
    ok_before = SONARR_REQUESTS.value(outcome="ok", **labels)
    err_before = SONARR_REQUESTS.value(outcome="http_5xx", **labels)

    assert (await c.get_series(5)) == {"id": 5}
    with pytest.raises(httpx.HTTPStatusError):
        await c.get_series(6)

    assert SONARR_REQUESTS.value(outcome="ok", **labels) == ok_before + 1
    assert SONARR_REQUESTS.value(outcome="http_5xx", **labels) == err_before + 1
    snap = c.stats.snapshot()
    assert snap["calls"] == 2
    assert snap["p50Ms"] == 250.0 and snap["p95Ms"] == 500.0
    assert snap["errorRate5m"] == 0.5
    assert snap["consecutiveFailures"] == 1
    assert "503" in snap["lastError"]
    assert snap["lastOkAt"] == "1970-01-01T00:16:40+00:00"


@respx.mock
async def test_transport_errors_are_classified():
    c = _client()
    respx.get(f"{BASE}/api/v3/system/status").mock(side_effect=httpx.ConnectError("refused"))
    respx.get(f"{BASE}/api/v3/health").mock(side_effect=httpx.ReadTimeout("slow"))
    err = dict(instance="t1", method="GET", endpoint="/system/status", outcome="error")
    slow = dict(instance="t1", method="GET", endpoint="/health", outcome="timeout")
    err_before, slow_before = SONARR_REQUESTS.value(**err), SONARR_REQUESTS.value(**slow)

    with pytest.raises(httpx.ConnectError):
        await c.system_status()
    with pytest.raises(httpx.ReadTimeout):
        await c.health()

    assert SONARR_REQUESTS.value(**err) == err_before + 1
    assert SONARR_REQUESTS.value(**slow) == slow_before + 1
    assert c.stats.snapshot()["consecutiveFailures"] == 2


@respx.mock
async def test_health_returns_sonarr_health_items():
    items = [{"source": "IndexerStatusCheck", "type": "warning", "message": "Indexers unavailable"}]
    route = respx.get(f"{BASE}/api/v3/health").mock(return_value=httpx.Response(200, json=items))
    assert await _client().health() == items
    assert route.calls.last.request.headers["X-Api-Key"] == "k"


def test_stats_error_rate_only_counts_last_five_minutes():
    now = [0.0]
    stats = ClientStats(clock=lambda: now[0])
    assert stats.snapshot()["p50Ms"] is None
    stats.record(10, False, "old failure")
    now[0] = 400.0
    stats.record(20, True)
    snap = stats.snapshot()
    assert snap["errorRate5m"] == 0.0
    assert snap["consecutiveFailures"] == 0
    assert snap["lastError"] == "old failure"


async def test_registry_names_clients_by_instance_id_and_closes_them():
    reg = make_registry()
    assert reg.get("4k").client.name == "4k"
    await reg.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_client_instrumentation.py -v`
Expected: FAIL — `ImportError: cannot import name 'ClientStats'`

- [ ] **Step 3: Rewrite the transport section of `app/sonarr/client.py`**

Replace everything from the top of the file through the end of `_delete` (the `lookup` method and below stay as they are) with:

```python
"""Async wrapper around a single Sonarr v3 API instance."""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections import deque
from datetime import datetime, timezone

import httpx

from app.obs.metrics import SONARR_DURATION, SONARR_REQUESTS

logger = logging.getLogger(__name__)

_NUMERIC_SEGMENT = re.compile(r"/\d+(?=/|$)")


def endpoint_template(path: str) -> str:
    """``/series/12`` → ``/series/{id}``: bounded metric label cardinality."""
    return _NUMERIC_SEGMENT.sub("/{id}", path)


def _outcome(error: BaseException | None) -> str:
    if error is None:
        return "ok"
    if isinstance(error, httpx.HTTPStatusError):
        return f"http_{error.response.status_code // 100}xx"
    if isinstance(error, httpx.TimeoutException):
        return "timeout"
    return "error"


def _percentile(sorted_values: list[float], q: float) -> float | None:
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, max(0, math.ceil(q * len(sorted_values)) - 1))
    return round(sorted_values[index], 1)


class ClientStats:
    """Rolling per-instance call statistics for health views (last ``window`` calls)."""

    def __init__(self, *, window: int = 200, clock=time.time):
        self._calls: deque = deque(maxlen=window)  # (epoch seconds, ms, ok)
        self._clock = clock
        self.last_ok_at: float | None = None
        self.last_error: str | None = None
        self.consecutive_failures = 0

    def record(self, ms: float, ok: bool, error: str | None = None) -> None:
        now = self._clock()
        self._calls.append((now, ms, ok))
        if ok:
            self.last_ok_at = now
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1
            self.last_error = error

    def snapshot(self) -> dict:
        now = self._clock()
        durations = sorted(c[1] for c in self._calls)
        recent = [c for c in self._calls if now - c[0] <= 300]
        return {
            "calls": len(self._calls),
            "p50Ms": _percentile(durations, 0.5),
            "p95Ms": _percentile(durations, 0.95),
            "errorRate5m": (sum(1 for c in recent if not c[2]) / len(recent)) if recent else 0.0,
            "lastOkAt": (datetime.fromtimestamp(self.last_ok_at, timezone.utc).isoformat()
                         if self.last_ok_at is not None else None),
            "lastError": self.last_error,
            "consecutiveFailures": self.consecutive_failures,
        }


class SonarrClient:
    """Talks to ONE Sonarr instance's v3 API.

    Instantiate once per instance (e.g. 1080p, 4K). All methods are async and
    return parsed JSON. Auth is the per-instance ``X-Api-Key`` header. Every call
    goes through ``_request``, which reuses one connection pool and records
    latency/outcome into ``stats`` and the Prometheus metrics.
    """

    # An interactive release search blocks until every indexer answers (some sit
    # behind FlareSolverr), which routinely exceeds the default timeout.
    RELEASE_SEARCH_TIMEOUT = 180.0

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30.0,
                 name: str | None = None, perf_counter=time.perf_counter, clock=time.time):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._timeout = timeout
        self.name = name or self.base_url
        self.stats = ClientStats(clock=clock)
        self._perf = perf_counter
        self._http: tuple[asyncio.AbstractEventLoop, httpx.AsyncClient] | None = None

    def _client(self) -> httpx.AsyncClient:
        """The shared pool for the running event loop.

        An AsyncClient is bound to the loop it first ran on; the app has one loop,
        but tests (and TestClient) use several, so rebuild when the loop changes.
        """
        loop = asyncio.get_running_loop()
        if self._http is None or self._http[0] is not loop or self._http[1].is_closed:
            self._http = (loop, httpx.AsyncClient(
                base_url=f"{self.base_url}/api/v3",
                headers={"X-Api-Key": self.api_key},
                timeout=self._timeout,
            ))
        return self._http[1]

    async def _request(self, method: str, path: str, *, params: dict | None = None,
                       json: dict | None = None, timeout: float | None = None) -> httpx.Response:
        kwargs: dict = {"params": params}
        if json is not None:
            kwargs["json"] = json
        if timeout is not None:  # omit rather than pass None, which disables the timeout
            kwargs["timeout"] = timeout
        http = self._client()
        start = self._perf()
        error: BaseException | None = None
        try:
            resp = await http.request(method, path, **kwargs)
            resp.raise_for_status()
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._observe(method, path, self._perf() - start, error)
        return resp

    def _observe(self, method: str, path: str, seconds: float,
                 error: BaseException | None) -> None:
        if isinstance(error, asyncio.CancelledError):
            return  # our own cancellation says nothing about Sonarr's health
        endpoint = endpoint_template(path)
        outcome = _outcome(error)
        SONARR_REQUESTS.inc(instance=self.name, method=method, endpoint=endpoint, outcome=outcome)
        SONARR_DURATION.observe(seconds, instance=self.name)
        message = None if error is None else (str(error) or type(error).__name__)
        self.stats.record(seconds * 1000, error is None, message)
        logger.debug("%s %s %s %s %.0fms", self.name, method, endpoint, outcome, seconds * 1000)

    async def _get(self, path: str, params: dict | None = None, *, timeout: float | None = None):
        return (await self._request("GET", path, params=params, timeout=timeout)).json()

    async def _post(self, path: str, json: dict):
        return (await self._request("POST", path, json=json)).json()

    async def _put(self, path: str, json: dict):
        return (await self._request("PUT", path, json=json)).json()

    async def _delete(self, path: str, params: dict | None = None):
        await self._request("DELETE", path, params=params)
        return None

    async def aclose(self) -> None:
        if self._http is None:
            return
        loop, http = self._http
        self._http = None
        if loop is asyncio.get_running_loop():
            await http.aclose()

    async def health(self) -> list[dict]:
        """Sonarr's own health checks (GET /health) — e.g. IndexerStatusCheck."""
        return await self._get("/health")
```

- [ ] **Step 4: Update `app/sonarr/registry.py`**

In `Registry.__init__`, construct the client with its id as the metrics name:

```python
                client=SonarrClient(base_url=cfg.url, api_key=cfg.api_key, name=cfg.id),
```

and add after `all()`:

```python
    async def aclose(self) -> None:
        """Close every instance's shared HTTP pool (app shutdown)."""
        for inst in self._instances.values():
            await inst.client.aclose()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_client_instrumentation.py tests/test_client.py -v && uv run pytest -q`
Expected: PASS, full suite green.

- [ ] **Step 6: Commit**

```bash
git add backend/app/sonarr/client.py backend/app/sonarr/registry.py backend/tests/test_client_instrumentation.py
git commit -m "feat(client): shared connection pool with per-call stats and metrics"
```

---

### Task 6: Operation store — single trace, batched steps, tick correlation

**Files:**
- Modify: `backend/app/store/operations.py`
- Test: `backend/tests/test_operations_store.py`

**Interfaces:**
- Consumes: `context.tick_id` (Task 3), `operation.tick_id` column (Task 1).
- Produces: `OperationStore.get(op_id: int) -> dict | None`; operation dicts gain `tickId`; `recent()` output otherwise unchanged.

- [ ] **Step 1: Write the failing test**

`backend/tests/test_operations_store.py`:

```python
"""OperationStore: single-trace reads, batched step loading, tick correlation."""
from app.obs import context
from app.store.operations import OperationStore


async def test_get_returns_one_operation_with_steps(db):
    store = OperationStore(db)
    op = await store.start(kind="reconcile", title="Mad Men", tvdb_id=99,
                           started_at="t0", source="reconciler")
    await store.add_step(op, {"phase": "search", "status": "done"})
    await store.finish(op, result={"searchedOnDesired": 1}, finished_at="t1")

    assert await store.get(op) == {
        "id": op, "kind": "reconcile", "title": "Mad Men", "tvdbId": 99,
        "startedAt": "t0", "steps": [{"phase": "search", "status": "done"}],
        "result": {"searchedOnDesired": 1}, "error": None, "finishedAt": "t1",
        "source": "reconciler", "tickId": None,
    }
    assert await store.get(12345) is None


async def test_recent_groups_steps_per_operation_in_order(db):
    store = OperationStore(db)
    a = await store.start(kind="k", title="A", tvdb_id=1, started_at="t")
    b = await store.start(kind="k", title="B", tvdb_id=2, started_at="t")
    await store.add_step(a, {"n": 1})
    await store.add_step(b, {"n": 2})
    await store.add_step(a, {"n": 3})

    ops = await store.recent()

    assert [(o["title"], o["steps"]) for o in ops] == [
        ("B", [{"n": 2}]), ("A", [{"n": 1}, {"n": 3}]),
    ]


async def test_start_stamps_tick_id_from_context(db):
    store = OperationStore(db)
    with context.bind(tick_id=42):
        op = await store.start(kind="reconcile", title="X", tvdb_id=None, started_at="t")
    assert (await store.get(op))["tickId"] == 42
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_operations_store.py -v`
Expected: FAIL — `AttributeError: 'OperationStore' object has no attribute 'get'`

- [ ] **Step 3: Implement**

Replace the body of `app/store/operations.py` from `import json` down with:

```python
import json

from app.db import Database
from app.obs import context


class OperationStore:
    def __init__(self, db: Database):
        self.db = db

    async def start(self, *, kind: str, title: str, tvdb_id: int | None, started_at: str,
                    source: str = "user") -> int:
        """Create an operation row; returns its id for add_step/finish. Stamps the
        current reconciler tick (if any) so the trace joins the tick's events."""
        return await self.db.execute(
            "INSERT INTO operation(kind, title, tvdb_id, source, started_at, tick_id) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (kind, title, tvdb_id, source, started_at, context.tick_id.get()),
        )

    async def add_step(self, op_id: int, event: dict) -> None:
        """Append one step event (the same dict that streams to the UI)."""
        row = await self.db.query_one(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM operation_step WHERE operation_id=?",
            (op_id,),
        )
        await self.db.execute(
            "INSERT INTO operation_step(operation_id, seq, event_json) VALUES(?, ?, ?)",
            (op_id, row["n"], json.dumps(event)),
        )

    async def finish(self, op_id: int, *, result=None, error: str | None = None,
                     finished_at: str) -> None:
        await self.db.execute(
            "UPDATE operation SET result_json=?, error=?, finished_at=? WHERE id=?",
            (json.dumps(result) if result is not None else None, error, finished_at, op_id),
        )

    @staticmethod
    def _shape(op, steps: list) -> dict:
        return {
            "id": op["id"],
            "kind": op["kind"],
            "title": op["title"],
            "tvdbId": op["tvdb_id"],
            "startedAt": op["started_at"],
            "steps": steps,
            "result": json.loads(op["result_json"]) if op["result_json"] else None,
            "error": op["error"],
            "finishedAt": op["finished_at"],
            "source": op["source"],
            "tickId": op["tick_id"],
        }

    async def _steps_for(self, op_ids: list[int]) -> dict[int, list]:
        if not op_ids:
            return {}
        marks = ", ".join("?" * len(op_ids))
        rows = await self.db.query(
            f"SELECT operation_id, event_json FROM operation_step "
            f"WHERE operation_id IN ({marks}) ORDER BY operation_id, seq",
            tuple(op_ids),
        )
        grouped: dict[int, list] = {}
        for r in rows:
            grouped.setdefault(r["operation_id"], []).append(json.loads(r["event_json"]))
        return grouped

    async def recent(self, limit: int = 200) -> list[dict]:
        """All retained operations, newest first, in the UI's expected shape.
        Two queries total, however many operations there are."""
        ops = await self.db.query("SELECT * FROM operation ORDER BY id DESC LIMIT ?", (limit,))
        steps = await self._steps_for([op["id"] for op in ops])
        return [self._shape(op, steps.get(op["id"], [])) for op in ops]

    async def get(self, op_id: int) -> dict | None:
        op = await self.db.query_one("SELECT * FROM operation WHERE id=?", (op_id,))
        if op is None:
            return None
        steps = await self._steps_for([op_id])
        return self._shape(op, steps.get(op_id, []))
```

Keep the module docstring at the top of the file.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_operations_store.py tests/test_store.py tests/test_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/store/operations.py backend/tests/test_operations_store.py
git commit -m "perf(store): load operation steps in one query; add get() and tick_id"
```

---

### Task 7: Journal what the poller, sweeps and availability checks change

**Files:**
- Create: `backend/app/obs/stats.py`
- Modify: `backend/app/services/poller.py` (`poll_instance`, `sweep_stalled`, `sweep_dangerous`, `sweep_search_stalls`)
- Modify: `backend/app/services/placement.py` (`refresh_availability`)
- Test: `backend/tests/test_event_emission.py`

**Interfaces:**
- Consumes: `get_journal()`, `kinds.*`, `context.tick_stats` (Task 3); `SWEEP_REMOVED`, `AVAILABILITY_CHECKS` (Task 2).
- Produces: `TickStats` dataclass with fields `phases: dict`, `errors: int`, `transitions: int`, `swept: int`, `live: dict`, `cached: dict`, `zero: dict`; methods `availability(instance_id, result)` (result ∈ `qualifies|rejected|zero|cached`), `zero_spikes(*, min_live=10, ratio=0.8) -> dict[str, tuple[int, int]]`, `to_phases() -> dict`.
- No function signatures change.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_event_emission.py`:

```python
"""Poller, sweeps and availability checks journal what they change."""
from datetime import datetime, timedelta, timezone

import httpx
import respx

from app.obs import context, kinds
from app.obs.stats import TickStats
from app.services import placement, poller
from app.store import events as events_store
from app.store.operations import OperationStore
from tests import test_dangerous as td
from tests import test_placement as tpl
from tests import test_poller as tp
from tests import test_search_stall as tss
from tests import test_stalled as ts
from tests.test_chain import A, B, make_registry


async def _events(journal, db, kind):
    await journal.flush()
    return list(reversed(await events_store.query(db, kind=kind)))


def test_tick_stats_counts_and_zero_spikes():
    s = TickStats()
    for _ in range(9):
        s.availability("4k", "zero")
    s.availability("4k", "qualifies")
    for _ in range(3):
        s.availability("1080p", "zero")
    s.availability("4k", "cached")
    assert s.zero_spikes() == {"4k": (9, 10)}   # 1080p has too few live checks
    assert s.to_phases()["availability"] == {
        "live": 13, "cached": 1, "liveBy": {"4k": 10, "1080p": 3}, "zero": {"4k": 9, "1080p": 3},
    }


@respx.mock
async def test_poll_transition_is_journaled(db, journal):
    reg = make_registry()
    await tp._seed(db, (2, 1))
    tp._mock_4k_series()
    respx.get(f"{B}/api/v3/queue").mock(return_value=httpx.Response(200, json={"records": []}))
    respx.get(f"{B}/api/v3/history").mock(return_value=httpx.Response(200, json={"records": [
        {"eventType": "downloadFailed", "seriesId": 10, "downloadId": "DX",
         "episode": {"seasonNumber": 2, "episodeNumber": 1}},
    ]}))

    await poller.poll_instance(reg, db, reg.get("4k"))

    [ev] = await _events(journal, db, kinds.POLL_TRANSITION)
    assert (ev["tvdbId"], ev["season"], ev["episode"], ev["instanceId"]) == (99, 2, 1, "4k")
    assert ev["level"] == "warn" and ev["source"] == "poller"
    assert ev["data"] == {"from": "wanted", "to": "failed", "downloadId": None}


@respx.mock
async def test_stalled_removal_is_journaled(db, journal):
    ops = OperationStore(db)
    respx.get(f"{A}/api/v3/queue").mock(return_value=ts._queue([ts._rec(id=11, seriesId=5)]))
    respx.get(f"{B}/api/v3/queue").mock(return_value=ts._queue([]))
    respx.get(f"{A}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "tvdbId": 99}]))
    respx.delete(f"{A}/api/v3/queue/11").mock(return_value=httpx.Response(200))

    await poller.sweep_stalled(make_registry(), db, ops, stalled_days=3, cap=25, now=ts.NOW)

    [ev] = await _events(journal, db, kinds.SWEEP_STALLED_REMOVED)
    op = (await ops.recent())[0]
    assert (ev["tvdbId"], ev["instanceId"], ev["operationId"]) == (99, "1080p", op["id"])
    assert ev["data"]["ageDays"] == 10 and ev["data"]["queueId"] == 11


@respx.mock
async def test_dangerous_removal_is_journaled_as_warning(db, journal):
    ops = OperationStore(db)
    respx.get(f"{A}/api/v3/queue").mock(return_value=td._queue(
        [td._rec(id=31, seriesId=5, title="Fake.S01E01.1080p.WEB-GRP.exe")]))
    respx.get(f"{B}/api/v3/queue").mock(return_value=td._queue([]))
    respx.get(f"{A}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "tvdbId": 99}]))
    respx.delete(f"{A}/api/v3/queue/31").mock(return_value=httpx.Response(200))
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))

    await poller.sweep_dangerous(make_registry(), db, ops, cap=25, now=td.NOW)

    [ev] = await _events(journal, db, kinds.SWEEP_DANGEROUS_REMOVED)
    assert ev["level"] == "warn" and ev["tvdbId"] == 99 and ev["instanceId"] == "1080p"


async def test_search_stall_revert_is_journaled(db, journal):
    await tss._seed(db, 777, 1, 2, state="searching",
                    last_search_at=(tss.NOW - timedelta(hours=12)).isoformat())

    await poller.sweep_search_stalls(db, stall_hours=6, cap=25, now=tss.NOW)

    [ev] = await _events(journal, db, kinds.SWEEP_SEARCH_STALL_REVERTED)
    assert (ev["tvdbId"], ev["season"], ev["episode"]) == (777, 1, 2)


@respx.mock
async def test_availability_change_is_journaled_only_on_flip(db, journal):
    now = datetime(2026, 9, 13, tzinfo=timezone.utc)
    tpl._mock_gap_with_airdate("2026-07-15T04:00:00Z")
    respx.get(f"{B}/api/v3/release").mock(
        return_value=httpx.Response(200, json=[{"rejected": False}]))
    # A 7h-old "0 releases" verdict is stale: the check runs live and flips it.
    await tpl._seed_empty_cache(db, checked_at=(now - timedelta(hours=7)).isoformat())

    stats = TickStats()
    with context.bind(tick_stats=stats):
        await placement.refresh_availability(
            make_registry(), db, tvdb_id=tpl.TVDB, instance_id="4k", now=now)

    [ev] = await _events(journal, db, kinds.AVAILABILITY_CHANGED)
    assert (ev["tvdbId"], ev["season"], ev["episode"], ev["instanceId"]) == (tpl.TVDB, 1, 2, "4k")
    assert ev["data"]["before"] == {"qualifies": False, "totalReleases": 0}
    assert ev["data"]["after"]["qualifies"] is True
    assert stats.live == {"4k": 1} and stats.zero == {}

    # Re-checked later with the same verdict: nothing new to journal.
    await placement.refresh_availability(
        make_registry(), db, tvdb_id=tpl.TVDB, instance_id="4k", now=now + timedelta(hours=7))
    assert len(await _events(journal, db, kinds.AVAILABILITY_CHANGED)) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_event_emission.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.obs.stats'`

- [ ] **Step 3: Create `app/obs/stats.py`**

```python
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

    def availability(self, instance_id: str, result: str) -> None:
        if result == "cached":
            self.cached[instance_id] = self.cached.get(instance_id, 0) + 1
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
        }
        return out
```

- [ ] **Step 4: Emit from `app/services/poller.py`**

Add imports next to the other `app.` imports:

```python
from app.obs import kinds
from app.obs.journal import get_journal
from app.obs.metrics import SWEEP_REMOVED
```

In `poll_instance`, directly after the `transitions.append({...})` block:

```python
        get_journal().emit(
            kinds.POLL_TRANSITION,
            f"S{season:02d}E{epnum:02d} {row['state']} → {new_state} on {instance.id}",
            level="warn" if new_state == "failed" else "info",
            source="poller", tvdb_id=tvdb, season=season, episode=epnum,
            instance_id=instance.id,
            data={"from": row["state"], "to": new_state, "downloadId": fields.get("download_id")},
        )
```

In `sweep_stalled`, replace

```python
            title = _stalled_title(r)
            op_id = await ops.start(
                kind="stalled-cleanup", title=title, tvdb_id=await tvdb_of(r.get("seriesId")),
```

with

```python
            title = _stalled_title(r)
            tvdb_id = await tvdb_of(r.get("seriesId"))
            op_id = await ops.start(
                kind="stalled-cleanup", title=title, tvdb_id=tvdb_id,
```

and directly after `removed += 1` in that function add:

```python
                SWEEP_REMOVED.inc(sweep="stalled")
                get_journal().emit(
                    kinds.SWEEP_STALLED_REMOVED,
                    f"Removed stalled torrent — {title}, stuck at {pct:.0f}% for {age_days}d",
                    source="sweep", tvdb_id=tvdb_id, instance_id=inst.id, operation_id=op_id,
                    data={"title": title, "ageDays": age_days, "progressPct": round(pct, 1),
                          "downloadId": r.get("downloadId"), "queueId": r["id"]},
                )
```

In `sweep_dangerous`, make the same `tvdb_id = await tvdb_of(r.get("seriesId"))` extraction before `ops.start(kind="dangerous-cleanup", ...)`, pass `tvdb_id=tvdb_id`, and directly after `removed += 1` add:

```python
                SWEEP_REMOVED.inc(sweep="dangerous")
                get_journal().emit(
                    kinds.SWEEP_DANGEROUS_REMOVED,
                    f"Removed dangerous release — {title} (executable payload; re-searching)",
                    level="warn", source="sweep", tvdb_id=tvdb_id, instance_id=inst.id,
                    operation_id=op_id,
                    data={"title": title, "downloadId": r.get("downloadId"), "queueId": r["id"]},
                )
```

In `sweep_search_stalls`, directly after the `update_tracking(...)` call inside the loop:

```python
        get_journal().emit(
            kinds.SWEEP_SEARCH_STALL_REVERTED,
            f"S{row['season']:02d}E{row['episode']:02d} searched over {stall_hours:g}h ago "
            f"with no download — back to wanted",
            source="sweep", tvdb_id=row["tvdb_id"], season=row["season"], episode=row["episode"],
            data={"lastSearchAt": row["last_search_at"]},
        )
```

- [ ] **Step 5: Emit from `app/services/placement.py`**

Add imports:

```python
from app.obs import context, kinds
from app.obs.journal import get_journal
from app.obs.metrics import AVAILABILITY_CHECKS
```

Add module-level helpers above `refresh_availability`:

```python
def _count_check(instance_id: str, result: str) -> None:
    AVAILABILITY_CHECKS.inc(instance=instance_id, result=result)
    stats = context.tick_stats.get()
    if stats is not None:
        stats.availability(instance_id, result)


def _journal_verdict_change(*, tvdb_id: int, season: int, episode: int, instance_id: str,
                            previous, qualifies: bool, total: int, qualifying: int) -> None:
    """Journal a verdict only when it meaningfully changed: qualifies flipped, or
    releases went from none to some (or back)."""
    was_qualifying = bool(previous["qualifies"])
    was_total = previous["total_releases"] or 0
    if was_qualifying == qualifies and (was_total == 0) == (total == 0):
        return
    label = f"S{season:02d}E{episode:02d} on {instance_id}"
    if qualifies and not was_qualifying:
        message = f"{label}: now available ({qualifying} qualifying of {total})"
    elif was_qualifying and not qualifies:
        message = f"{label}: no longer available ({total} release(s), none qualify)"
    elif total == 0:
        message = f"{label}: releases vanished (was {was_total})"
    else:
        message = f"{label}: {total} release(s) appeared, none qualify"
    get_journal().emit(
        kinds.AVAILABILITY_CHANGED, message, source="reconciler",
        tvdb_id=tvdb_id, season=season, episode=episode, instance_id=instance_id,
        data={
            "before": {"qualifies": was_qualifying, "totalReleases": was_total},
            "after": {"qualifies": qualifies, "totalReleases": total, "qualifyingCount": qualifying},
        },
    )
```

Inside `check()` in `refresh_availability`: in the cache-hit branch, immediately before its `return`, add `_count_check(instance_id, "cached")`. In the live branch, directly after `best = ...` trimming and before `await avail_cache.put(...)`, add:

```python
        _count_check(instance_id, "qualifies" if qc > 0 else ("zero" if not releases else "rejected"))
        if cached is not None:
            _journal_verdict_change(
                tvdb_id=tvdb_id, season=season, episode=epnum, instance_id=instance_id,
                previous=cached, qualifies=qc > 0, total=len(releases), qualifying=qc,
            )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_event_emission.py -v && uv run pytest -q`
Expected: PASS, full suite green.

- [ ] **Step 7: Commit**

```bash
git add backend/app/obs/stats.py backend/app/services/poller.py backend/app/services/placement.py backend/tests/test_event_emission.py
git commit -m "feat(obs): journal poll transitions, sweep actions and availability flips"
```

---

### Task 8: Retention and the always-on Monitor loop

**Files:**
- Create: `backend/app/services/retention.py`, `backend/app/services/monitor.py`
- Test: `backend/tests/test_retention.py`, `backend/tests/test_monitor.py`

**Interfaces:**
- Consumes: `Database.execute_count` (Task 1); `get_journal`, `kinds` (Task 3); `SONARR_UP` (Task 2); `SonarrClient.health()`, `client.stats.snapshot()` (Task 5).
- Produces:
  - `retention.prune(db, now: datetime, *, chunk: int = 5000) -> {"events", "ticks", "operations", "downloadProgress"}`
  - `retention.maybe_prune(db, now: datetime) -> dict | None` (None when pruned < 24h ago; meta key `last_prune_at`)
  - `Monitor(registry, db, *, clock=None, interval=60.0, health_every=5, down_after=2)` with `async step()`, `async run()`, `stop()`, `snapshot() -> list[dict]`. Snapshot item keys: `id, name, up, since, consecutiveFailures, lastError, sonarrHealth, healthCheckedAt, client`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_retention.py`:

```python
"""Retention: bounded tables, pruned in chunks, at most once a day."""
from datetime import datetime, timedelta, timezone

from app.services import retention

NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


def _ago(days):
    return (NOW - timedelta(days=days)).isoformat()


async def _event(db, level, ts):
    await db.execute(
        "INSERT INTO event(ts, kind, level, message) VALUES(?, 'instance.up', ?, 'x')", (ts, level)
    )


async def test_prune_applies_per_table_and_per_level_ages(db):
    for _ in range(5):
        await _event(db, "debug", _ago(4))   # debug keeps 3d → gone (exercises chunking)
    await _event(db, "debug", _ago(1))       # kept
    await _event(db, "info", _ago(31))       # info keeps 30d → gone
    await _event(db, "info", _ago(29))       # kept
    await _event(db, "error", _ago(89))      # errors keep 90d → kept
    await _event(db, "warn", _ago(91))       # gone
    await db.execute("INSERT INTO tick(trigger, started_at, status) VALUES('schedule', ?, 'ok')", (_ago(91),))
    await db.execute("INSERT INTO tick(trigger, started_at, status) VALUES('schedule', ?, 'ok')", (_ago(1),))
    old_op = await db.execute(
        "INSERT INTO operation(kind, title, started_at) VALUES('k', 'old', ?)", (_ago(61),))
    await db.execute(
        "INSERT INTO operation_step(operation_id, seq, event_json) VALUES(?, 1, '{}')", (old_op,))
    await db.execute("INSERT INTO operation(kind, title, started_at) VALUES('k', 'new', ?)", (_ago(1),))
    await db.execute(
        "INSERT INTO download_progress(download_id, sizeleft, unchanged_since) VALUES('old', 1, ?)",
        (_ago(31),))
    await db.execute(
        "INSERT INTO download_progress(download_id, sizeleft, unchanged_since) VALUES('new', 1, ?)",
        (_ago(1),))

    counts = await retention.prune(db, NOW, chunk=2)

    assert counts == {"events": 7, "ticks": 1, "operations": 1, "downloadProgress": 1}
    assert [r["level"] for r in await db.query("SELECT level FROM event ORDER BY id")] == [
        "debug", "info", "error"]
    assert [r["title"] for r in await db.query("SELECT title FROM operation")] == ["new"]
    assert (await db.query_one("SELECT COUNT(*) AS n FROM operation_step"))["n"] == 0
    assert [r["download_id"] for r in await db.query("SELECT download_id FROM download_progress")] == ["new"]


async def test_prune_never_deletes_a_running_tick(db):
    await db.execute("INSERT INTO tick(trigger, started_at, status) VALUES('schedule', ?, 'running')", (_ago(200),))
    assert (await retention.prune(db, NOW))["ticks"] == 0


async def test_maybe_prune_runs_at_most_daily(db):
    assert await retention.maybe_prune(db, NOW) is not None
    assert await retention.maybe_prune(db, NOW + timedelta(hours=23)) is None
    assert await retention.maybe_prune(db, NOW + timedelta(hours=25)) is not None
```

`backend/tests/test_monitor.py`:

```python
"""Monitor: instance up/down, Sonarr health changes, retention, snapshot."""
from datetime import datetime, timezone

import httpx
import respx

from app.obs import kinds
from app.obs.metrics import SONARR_UP
from app.services.monitor import Monitor
from app.store import events as events_store
from tests.test_chain import A, B, make_registry

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
OK = httpx.Response(200, json={"version": "4.0.0"})


def _status_ok(base):
    respx.get(f"{base}/api/v3/system/status").mock(return_value=OK)


def _health(base, items=()):
    respx.get(f"{base}/api/v3/health").mock(return_value=httpx.Response(200, json=list(items)))


async def _instance_events(journal, db, kind="instance."):
    await journal.flush()
    return list(reversed(await events_store.query(db, kind=kind)))


@respx.mock
async def test_down_after_two_failures_once_then_up(db, journal):
    _status_ok(A)
    _health(A)
    _health(B)
    refused = httpx.ConnectError("refused")
    respx.get(f"{B}/api/v3/system/status").mock(side_effect=[refused, refused, refused, OK])
    mon = Monitor(make_registry(), db, clock=lambda: NOW, health_every=1000)

    await mon.step()
    assert await _instance_events(journal, db) == []      # one miss is not an outage
    await mon.step()
    await mon.step()                                       # still down: no duplicate event
    events = await _instance_events(journal, db)
    assert [(e["kind"], e["instanceId"], e["level"]) for e in events] == [
        ("instance.down", "4k", "error")]
    assert SONARR_UP.value(instance="4k") == 0
    four_k = {s["id"]: s for s in mon.snapshot()}["4k"]
    assert four_k["up"] is False and four_k["consecutiveFailures"] == 3
    assert "refused" in four_k["lastError"]

    await mon.step()
    events = await _instance_events(journal, db)
    assert [e["kind"] for e in events] == ["instance.down", "instance.up"]
    assert SONARR_UP.value(instance="4k") == 1
    four_k = {s["id"]: s for s in mon.snapshot()}["4k"]
    assert four_k["up"] is True and four_k["since"] == NOW.isoformat()
    assert four_k["client"]["calls"] == 4


@respx.mock
async def test_sonarr_health_changes_are_journaled(db, journal):
    _status_ok(A)
    _status_ok(B)
    _health(A)
    issue = {"source": "IndexerStatusCheck", "type": "warning",
             "message": "All indexers are unavailable due to failures"}
    respx.get(f"{B}/api/v3/health").mock(side_effect=[
        httpx.Response(200, json=[]),
        httpx.Response(200, json=[issue]),
        httpx.Response(200, json=[issue]),   # unchanged: no event
        httpx.Response(200, json=[]),
    ])
    mon = Monitor(make_registry(), db, clock=lambda: NOW, health_every=1)

    for _ in range(4):
        await mon.step()

    events = await _instance_events(journal, db, kind=kinds.INSTANCE_HEALTH_CHANGED)
    assert [(e["instanceId"], e["level"]) for e in events] == [("4k", "warn"), ("4k", "info")]
    assert "All indexers are unavailable" in events[0]["message"]
    assert {s["id"]: s for s in mon.snapshot()}["4k"]["sonarrHealth"] == []


@respx.mock
async def test_step_runs_retention_gate(db, monkeypatch):
    _status_ok(A)
    _status_ok(B)
    _health(A)
    _health(B)
    calls = []

    async def fake_maybe_prune(db_, now):
        calls.append(now)
        return None

    monkeypatch.setattr("app.services.monitor.retention.maybe_prune", fake_maybe_prune)
    await Monitor(make_registry(), db, clock=lambda: NOW).step()
    assert calls == [NOW]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_retention.py tests/test_monitor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.retention'`

- [ ] **Step 3: Create `app/services/retention.py`**

```python
"""Bounded history: prune old journal/tick/operation/progress rows.

Deletes run in chunks so the single SQLite lock is never held for long, and the
whole pass runs at most once a day (``meta.last_prune_at``).
"""
from __future__ import annotations

from datetime import datetime, timedelta

EVENT_RETENTION_DAYS = {"debug": 3, "info": 30, "warn": 90, "error": 90}
TICK_RETENTION_DAYS = 90
OPERATION_RETENTION_DAYS = 60
PROGRESS_RETENTION_DAYS = 30
CHUNK = 5000
PRUNE_KEY = "last_prune_at"
PRUNE_EVERY = timedelta(days=1)


async def _delete_chunked(db, table: str, key: str, where: str, params: tuple, chunk: int) -> int:
    sql = (f"DELETE FROM {table} WHERE {key} IN "
           f"(SELECT {key} FROM {table} WHERE {where} LIMIT ?)")
    total = 0
    while True:
        n = await db.execute_count(sql, (*params, chunk))
        total += n
        if n < chunk:
            return total


def _cutoff(now: datetime, days: int) -> str:
    return (now - timedelta(days=days)).isoformat()


async def prune(db, now: datetime, *, chunk: int = CHUNK) -> dict:
    events = 0
    for level, days in EVENT_RETENTION_DAYS.items():
        events += await _delete_chunked(
            db, "event", "id", "level=? AND ts<?", (level, _cutoff(now, days)), chunk)
    return {
        "events": events,
        "ticks": await _delete_chunked(
            db, "tick", "id", "status != 'running' AND started_at<?",
            (_cutoff(now, TICK_RETENTION_DAYS),), chunk),
        # operation_step rows go with their operation (ON DELETE CASCADE).
        "operations": await _delete_chunked(
            db, "operation", "id", "started_at<?", (_cutoff(now, OPERATION_RETENTION_DAYS),), chunk),
        "downloadProgress": await _delete_chunked(
            db, "download_progress", "download_id", "unchanged_since<?",
            (_cutoff(now, PROGRESS_RETENTION_DAYS),), chunk),
    }


async def maybe_prune(db, now: datetime) -> dict | None:
    """Prune if the last pass was over a day ago; returns counts, or None if skipped."""
    row = await db.query_one("SELECT value FROM meta WHERE key=?", (PRUNE_KEY,))
    if row and row["value"]:
        try:
            if now - datetime.fromisoformat(row["value"]) < PRUNE_EVERY:
                return None
        except ValueError:
            pass
    counts = await prune(db, now)
    await db.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (PRUNE_KEY, now.isoformat())
    )
    return counts
```

- [ ] **Step 4: Create `app/services/monitor.py`**

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_retention.py tests/test_monitor.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/retention.py backend/app/services/monitor.py backend/tests/test_retention.py backend/tests/test_monitor.py
git commit -m "feat(monitor): always-on instance probe, Sonarr health watch, retention"
```

---

### Task 9: Reconciler — persisted ticks, phases, degraded status, manual-tick lock

**Files:**
- Modify: `backend/app/reconciler.py` (imports, constructor, `run`, `_run_once`, `is_healthy`, `status`, `tick`; `reconcile_series` and below unchanged)
- Modify: `backend/app/api/catalog.py` (`reconcile_tick`), `backend/app/api/policy.py` (`reconciler_status`), `backend/app/main.py` (`healthz`)
- Test: `backend/tests/test_ticks.py` (new), `backend/tests/test_reconciler.py:342-387` (update), `backend/tests/test_health.py` (update)

**Interfaces:**
- Consumes: `tick_store` (Task 1), metrics (Task 2), `context`, `kinds`, `get_journal` (Task 3), `TickStats` (Task 7), `Monitor.snapshot()` (Task 8).
- Produces:
  - `Reconciler(..., monitor=None)` (all existing kwargs unchanged)
  - `async _run_once(trigger="schedule") -> dict | None` — `None` when the tick raised, else `{"tickId", "status", "error", "reconciled"}`
  - `async run_manual() -> dict` (same shape, never None); raises `TickBusy` if a tick is running
  - `class TickBusy(Exception)`
  - `async is_healthy(now=None) -> bool`, `async status() -> dict` with all previous keys plus `running`, `nextTickAt`, `lastTick` (tick dict or None), `instances` (monitor snapshot or `[]`)
  - Tick phases recorded: `poll`, `sweep_stalled`, `sweep_dangerous`, `sweep_search_stall`, `reconcile` (+ `availability` from `TickStats.to_phases()`)
  - `POST /api/reconcile/tick` → 200 `{"tickId", "status", "error", "reconciled"}` or 409

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_ticks.py`:

```python
"""Persisted ticks: phases, degraded/failed status, restart-safe health, manual lock."""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.obs import context, kinds
from app.reconciler import Reconciler, TickBusy
from app.store import events as events_store
from app.store import ticks as tick_store
from app.store.operations import OperationStore
from tests.test_chain import A, B, _nosleep, make_registry

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def _rec(db, clock=lambda: NOW, **kw):
    return Reconciler(make_registry(), db, OperationStore(db), clock=clock, sleep=_nosleep, **kw)


def _empty(base):
    respx.get(f"{base}/api/v3/series").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{base}/api/v3/queue").mock(return_value=httpx.Response(200, json={"records": []}))
    respx.get(f"{base}/api/v3/history").mock(return_value=httpx.Response(200, json={"records": []}))


async def _ok_tick():
    return {"reconciled": []}


@respx.mock
async def test_clean_tick_is_persisted_ok_with_phases(db, journal):
    _empty(A)
    _empty(B)

    out = await _rec(db)._run_once()

    assert out == {"tickId": 1, "status": "ok", "error": None, "reconciled": []}
    tick = await tick_store.get(db, 1)
    assert tick["status"] == "ok" and tick["trigger"] == "schedule"
    assert set(tick["phases"]) >= {
        "poll", "sweep_stalled", "sweep_dangerous", "sweep_search_stall", "reconcile", "availability"}
    events = await events_store.query(db, kind="tick.")
    assert [(e["kind"], e["tickId"], e["level"]) for e in events] == [("tick.finished", 1, "info")]


@respx.mock
async def test_sweep_failure_degrades_tick_and_is_journaled(db, journal, monkeypatch):
    _empty(A)
    _empty(B)

    async def boom(*_a, **_k):
        raise RuntimeError("queue exploded")

    monkeypatch.setattr("app.reconciler.poller.sweep_dangerous", boom)

    out = await _rec(db)._run_once()

    assert out["status"] == "degraded"
    tick = await tick_store.get(db, out["tickId"])
    assert tick["errors"] == 1
    assert "queue exploded" in tick["phases"]["sweep_dangerous"]["error"]
    [failed] = await events_store.query(db, kind=kinds.SWEEP_FAILED)
    assert failed["level"] == "error" and failed["tickId"] == out["tickId"]
    [finished] = await events_store.query(db, kind=kinds.TICK_FINISHED)
    assert finished["level"] == "warn"


@respx.mock
async def test_unreachable_instance_degrades_tick(db, journal):
    _empty(A)
    for path in ("series", "queue", "history"):
        respx.get(f"{B}/api/v3/{path}").mock(side_effect=httpx.ConnectError("refused"))

    out = await _rec(db)._run_once()

    assert out["status"] == "degraded"
    tick = await tick_store.get(db, out["tickId"])
    assert "4k" in tick["phases"]["poll"]["errors"]
    [ev] = await events_store.query(db, kind=kinds.POLL_INSTANCE_ERROR)
    assert ev["instanceId"] == "4k" and ev["tickId"] == out["tickId"]


async def test_raising_tick_is_failed_and_reported(db, journal):
    rec = _rec(db)

    async def boom():
        raise ValueError("kaboom")

    rec.tick = boom
    assert await rec._run_once() is None

    tick = (await tick_store.recent(db))[0]
    assert tick["status"] == "failed" and tick["error"] == "ValueError: kaboom"
    [ev] = await events_store.query(db, kind=kinds.TICK_FAILED)
    assert ev["level"] == "error"
    status = await rec.status()
    assert status["consecutiveFailures"] == 1
    assert status["lastError"] == "ValueError: kaboom"


async def test_health_comes_from_persisted_ticks_and_survives_restart(db):
    clock = {"now": NOW}
    rec = _rec(db, clock=lambda: clock["now"])
    rec.tick = _ok_tick
    await rec._run_once()

    # "Restart" 50 minutes later: a brand-new Reconciler on the same database.
    clock["now"] = NOW + timedelta(minutes=50)
    restarted = _rec(db, clock=lambda: clock["now"])
    status = await restarted.status()
    assert status["healthy"] is True
    assert status["totalTicks"] == 1
    assert status["lastTick"]["status"] == "ok"
    assert status["lastTickFinishedAt"] == NOW.isoformat()

    # Grace is measured from process start; with no tick since, it eventually goes stale.
    clock["now"] = NOW + timedelta(minutes=50, seconds=restarted.interval * 2 + 1)
    assert await restarted.is_healthy() is False


async def test_disabled_reconciler_is_always_healthy(db):
    assert await _rec(db, enabled=False).is_healthy(NOW + timedelta(days=99)) is True


async def test_manual_tick_rejected_while_a_tick_runs(db):
    rec = _rec(db)
    release = asyncio.Event()

    async def slow_tick():
        await release.wait()
        return {"reconciled": []}

    rec.tick = slow_tick
    running = asyncio.create_task(rec._run_once())
    await asyncio.sleep(0)  # the scheduled tick takes the lock

    with pytest.raises(TickBusy):
        await rec.run_manual()

    release.set()
    await running
    rec.tick = _ok_tick
    out = await rec.run_manual()
    assert out["tickId"] == 2
    assert (await tick_store.get(db, 2))["trigger"] == "manual"


async def test_zero_release_spike_is_journaled_at_tick_end(db, journal):
    rec = _rec(db)

    async def tick():
        stats = context.tick_stats.get()
        for _ in range(10):
            stats.availability("4k", "zero")
        return {"reconciled": []}

    rec.tick = tick
    await rec._run_once()

    [ev] = await events_store.query(db, kind=kinds.AVAILABILITY_ZERO_SPIKE)
    assert ev["instanceId"] == "4k" and ev["data"] == {"zero": 10, "live": 10}
```

In `backend/tests/test_reconciler.py`, replace `test_run_once_records_success`, `test_run_once_records_failure_without_propagating` and `test_is_healthy_grace_staleness_and_disabled` (lines 342-387) with:

```python
async def test_run_once_records_success(tmp_path):
    """A successful guarded iteration sets timing/action fields and clears failures."""
    reg, db, ops, rec = _make(tmp_path)

    async def fake_tick():
        return {"reconciled": [{"searchedOnDesired": 2, "filled": 1}, {"actions": 0}]}

    rec.tick = fake_tick
    out = await rec._run_once()

    assert out is not None
    s = await rec.status()
    assert s["totalTicks"] == 1
    assert s["lastTickActions"] == 3          # 2 searched + 1 filled
    assert s["consecutiveFailures"] == 0
    assert s["lastError"] is None
    assert s["lastTickFinishedAt"] == NOW.isoformat()
    assert s["lastTickDurationS"] == 0.0      # fixed clock


async def test_run_once_records_failure_without_propagating(tmp_path):
    """A tick that raises is swallowed but recorded — never kills the loop."""
    reg, db, ops, rec = _make(tmp_path)

    async def boom():
        raise ValueError("kaboom")

    rec.tick = boom
    out = await rec._run_once()  # must NOT raise

    assert out is None
    s = await rec.status()
    assert s["consecutiveFailures"] == 1
    assert s["lastError"].startswith("ValueError")
    assert "kaboom" in s["lastError"]


async def test_is_healthy_grace_staleness_and_disabled(tmp_path):
    reg, db, ops, rec = _make(tmp_path)  # clock fixed at NOW; started_at == NOW
    # Fresh (no tick yet) but within startup grace → healthy.
    assert await rec.is_healthy(NOW) is True
    # Past interval*2 with no completed tick → stale → unhealthy.
    assert await rec.is_healthy(NOW + timedelta(seconds=rec.interval * 2 + 1)) is False
    # Disabled reconcilers are always "healthy" (nothing is supposed to run).
    rec.enabled = False
    assert await rec.is_healthy(NOW + timedelta(days=99)) is True
```

In `backend/tests/test_health.py`, make `FakeReconciler.status` async (`async def status(self) -> dict:`) and append:

```python
from app.reconciler import TickBusy


class ManualReconciler:
    def __init__(self, busy: bool):
        self.busy = busy

    async def run_manual(self):
        if self.busy:
            raise TickBusy()
        return {"tickId": 5, "status": "ok", "error": None, "reconciled": []}


def test_manual_tick_endpoint_returns_result_or_409():
    try:
        app.dependency_overrides[get_reconciler] = lambda: ManualReconciler(busy=False)
        r = TestClient(app).post("/api/reconcile/tick")
        assert r.status_code == 200 and r.json()["tickId"] == 5
        app.dependency_overrides[get_reconciler] = lambda: ManualReconciler(busy=True)
        assert TestClient(app).post("/api/reconcile/tick").status_code == 409
    finally:
        _teardown()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ticks.py tests/test_reconciler.py tests/test_health.py -v`
Expected: FAIL — `ImportError: cannot import name 'TickBusy'`

- [ ] **Step 3: Rewrite the top of `app/reconciler.py`**

Replace the file from the first import through the end of `tick()` (everything above `async def _ensure_intents`) with the code below. Keep the module docstring, appending the paragraph "Observability: each pass is a persisted ``tick`` row with per-phase timings; its events carry the tick id through ``app.obs.context``." Everything from `_ensure_intents` down stays as is.

```python
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
            await self._run_once("schedule")
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
```

- [ ] **Step 4: Update the callers**

`app/api/catalog.py` — replace `reconcile_tick`:

```python
@router.post("/reconcile/tick")
async def reconcile_tick(reconciler=Depends(get_reconciler)):
    """Run one reconciliation pass now (the background loop runs it on a schedule).
    409 if a tick is already in progress."""
    try:
        return await reconciler.run_manual()
    except TickBusy:
        raise HTTPException(status_code=409, detail="A reconciliation tick is already running")
```

with `from app.reconciler import TickBusy` added to the imports.

`app/api/policy.py` — in `reconciler_status`, `return await rec.status()`.

`app/main.py` — in `healthz`, `status = await rec.status()`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_ticks.py tests/test_reconciler.py tests/test_health.py tests/test_search_stall.py -v && uv run pytest -q`
Expected: PASS, full suite green.

- [ ] **Step 6: Commit**

```bash
git add backend/app/reconciler.py backend/app/api/catalog.py backend/app/api/policy.py backend/app/main.py backend/tests/test_ticks.py backend/tests/test_reconciler.py backend/tests/test_health.py
git commit -m "feat(reconciler): persisted ticks with phases, degraded status, manual-tick lock"
```

---

### Task 10: Observability API — metrics, events, ticks, operation detail, summary

**Files:**
- Create: `backend/app/api/observability.py`, `backend/app/store/summary.py`
- Modify: `backend/app/state.py` (add `get_event_journal`, `get_monitor`), `backend/app/auth.py:66` (exempt `/metrics`), `backend/app/main.py` (include router)
- Test: `backend/tests/test_observability_api.py`

**Interfaces:**
- Consumes: `METRICS`, `PLACEMENT_EPISODES`, `SERIES_INTENTS` (Task 2); `events_store` (Task 3); `OperationStore.get` (Task 6); `tick_store` (Task 1); `status_service.library_status` (existing).
- Produces:
  - `state.get_event_journal(request)` → `app.state.journal` or the process-wide journal; `state.get_monitor(request)` → `app.state.monitor` or `None`
  - `summary.placement_counts(db) -> dict[str, int]`, `summary.intent_counts(db) -> {"active", "paused"}`, `summary.series_status_counts(db) -> dict[str, int]`, `summary.refresh_gauges(db) -> None`
  - Routes: `GET /metrics`, `GET /api/events`, `GET /api/ticks`, `GET /api/ticks/{tick_id}`, `GET /api/operations/{op_id}`, `GET /api/summary`
  - Env: `METRICS_TOKEN` (optional bearer token for `/metrics`)

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_observability_api.py`:

```python
"""Observability HTTP surface: metrics, events, ticks, operation detail, summary."""
import asyncio

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from app.auth import verify_access
from app.main import app
from app.obs import context, kinds
from app.state import get_db, get_monitor, get_operations
from app.store import intents as intent_store
from app.store import placements as place_store
from app.store import ticks as tick_store
from app.store.operations import OperationStore


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def api(db, journal):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_operations] = lambda: OperationStore(db)
    app.dependency_overrides[get_monitor] = lambda: None
    try:
        yield TestClient(app)
    finally:
        for dep in (get_db, get_operations, get_monitor):
            app.dependency_overrides.pop(dep, None)


async def _placement(db, tvdb, episode, state):
    await place_store.upsert(
        db, tvdb_id=tvdb, season=1, episode=episode, desired_tier="4k",
        obtained_tier="4k" if state == "imported" else None, state=state,
        reason=None, updated_at="t",
    )


def test_metrics_endpoint_exposes_text_format_and_db_gauges(api, db):
    async def seed():
        await _placement(db, 1, 1, "wanted")
        await intent_store.ensure(db, tvdb_id=1, title="X", chain_key="4k", now="t")

    _run(seed())
    r = api.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert 'relay_placement_episodes{state="wanted"} 1' in r.text
    assert 'relay_series_intents{paused="false"} 1' in r.text
    assert "# TYPE relay_tick_total counter" in r.text


def test_metrics_token_required_when_configured(api, monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "s3cret")
    assert api.get("/metrics").status_code == 401
    assert api.get("/metrics", headers={"Authorization": "Bearer s3cret"}).status_code == 200


async def test_metrics_path_is_exempt_from_cloudflare_access(monkeypatch):
    monkeypatch.setenv("CF_ACCESS_ENABLED", "true")
    scope = {"type": "http", "method": "GET", "path": "/metrics", "headers": [],
             "query_string": b"", "server": ("relay", 80), "scheme": "http", "root_path": ""}
    assert await verify_access(request=Request(scope), cf_assertion=None) is None


def test_events_endpoint_filters_and_pages(api, journal):
    journal.emit(kinds.POLL_TRANSITION, "a", tvdb_id=1)
    journal.emit(kinds.INSTANCE_DOWN, "b", level="error", instance_id="4k")
    journal.emit(kinds.POLL_TRANSITION, "c", tvdb_id=1)
    _run(journal.flush())

    page1 = api.get("/api/events", params={"kind": "poll.", "limit": 1}).json()
    assert [e["message"] for e in page1["items"]] == ["c"] and page1["nextBefore"] == 3
    page2 = api.get("/api/events", params={"kind": "poll.", "limit": 1, "before": 3}).json()
    assert [e["message"] for e in page2["items"]] == ["a"]
    errors = api.get("/api/events", params={"level": "error"}).json()["items"]
    assert [e["message"] for e in errors] == ["b"]
    assert api.get("/api/events", params={"tvdb": 1}).json()["nextBefore"] is None
    assert api.get("/api/events", params={"level": "loud"}).status_code == 400


def test_tick_list_and_detail(api, db, journal):
    async def seed():
        tid = await tick_store.start(db, trigger="manual", started_at="2026-09-14T00:00:00+00:00")
        await tick_store.finish(
            db, tid, finished_at="2026-09-14T00:00:02+00:00", duration_ms=2000, status="ok",
            series_count=0, actions=0, transitions=0, swept=0, errors=0, error=None, phases={},
        )
        with context.bind(tick_id=tid):
            journal.emit(kinds.TICK_FINISHED, "done")
        await journal.flush()
        return tid

    tid = _run(seed())
    assert [t["id"] for t in api.get("/api/ticks").json()] == [tid]
    detail = api.get(f"/api/ticks/{tid}").json()
    assert detail["status"] == "ok"
    assert [e["message"] for e in detail["events"]] == ["done"]
    assert api.get("/api/ticks/999").status_code == 404


def test_operation_detail(api, db):
    op = _run(OperationStore(db).start(kind="reconcile", title="X", tvdb_id=1, started_at="t"))
    assert api.get(f"/api/operations/{op}").json()["title"] == "X"
    assert api.get("/api/operations/999").status_code == 404


def test_summary_rolls_up_state(api, db):
    async def seed():
        await _placement(db, 1, 1, "imported")
        await _placement(db, 1, 2, "unavailable")
        await _placement(db, 2, 1, "imported")
        await intent_store.ensure(db, tvdb_id=1, title="A", chain_key="4k", now="t")
        await intent_store.ensure(db, tvdb_id=2, title="B", chain_key="4k", now="t")
        await intent_store.set_paused(db, 2, True, "t")

    _run(seed())
    body = api.get("/api/summary").json()
    assert body["placementCounts"] == {"imported": 2, "unavailable": 1}
    assert body["seriesStatusCounts"] == {"stuck": 1, "paused": 1}
    assert body["intents"] == {"active": 1, "paused": 1}
    assert body["lastTick"] is None and body["instances"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_observability_api.py -v`
Expected: FAIL — `ImportError: cannot import name 'get_monitor' from 'app.state'`

- [ ] **Step 3: Add dependencies to `app/state.py`**

```python
from app.obs.journal import get_journal


def get_event_journal(request: Request):
    """The app's journal; falls back to the process-wide one (e.g. in tests)."""
    return getattr(request.app.state, "journal", None) or get_journal()


def get_monitor(request: Request):
    """The always-on Monitor, or None when the app runs without one (tests)."""
    return getattr(request.app.state, "monitor", None)
```

- [ ] **Step 4: Create `app/store/summary.py`**

```python
"""Cheap DB roll-ups shared by the overview endpoint and the Prometheus gauges."""
from __future__ import annotations

from app.obs.metrics import PLACEMENT_EPISODES, SERIES_INTENTS
from app.services import status as status_service


async def placement_counts(db) -> dict[str, int]:
    rows = await db.query("SELECT state, COUNT(*) AS n FROM placement GROUP BY state ORDER BY state")
    return {r["state"]: r["n"] for r in rows}


async def intent_counts(db) -> dict[str, int]:
    rows = await db.query("SELECT paused, COUNT(*) AS n FROM series_intent GROUP BY paused")
    by_paused = {bool(r["paused"]): r["n"] for r in rows}
    return {"active": by_paused.get(False, 0), "paused": by_paused.get(True, 0)}


async def series_status_counts(db) -> dict[str, int]:
    """Headline status per series (paused series count as 'paused')."""
    out: dict[str, int] = {}
    for s in await status_service.library_status(db):
        key = "paused" if s["paused"] else s["status"]
        out[key] = out.get(key, 0) + 1
    return out


async def refresh_gauges(db) -> None:
    placements = await placement_counts(db)
    intents = await intent_counts(db)
    PLACEMENT_EPISODES.clear()
    for state, n in placements.items():
        PLACEMENT_EPISODES.set(n, state=state)
    SERIES_INTENTS.clear()
    SERIES_INTENTS.set(intents["active"], paused="false")
    SERIES_INTENTS.set(intents["paused"], paused="true")
```

- [ ] **Step 5: Create `app/api/observability.py`**

```python
"""Observability endpoints: Prometheus metrics, the event journal, tick history, summary."""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.responses import Response

from app.obs.metrics import METRICS
from app.state import get_db, get_monitor, get_operations
from app.store import events as events_store
from app.store import summary
from app.store import ticks as tick_store

router = APIRouter(tags=["observability"])


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request, db=Depends(get_db)):
    """Prometheus scrape target. Exempt from Cloudflare Access (see auth.py); set
    METRICS_TOKEN to require ``Authorization: Bearer <token>`` instead."""
    token = os.environ.get("METRICS_TOKEN")
    if token and request.headers.get("authorization") != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="Invalid metrics token")
    await summary.refresh_gauges(db)
    return Response(METRICS.render(), media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/api/events")
async def list_events(
    kind: str | None = None,
    level: str | None = None,
    tvdb: int | None = None,
    tick: int | None = None,
    source: str | None = None,
    before: int | None = None,
    limit: int = Query(100, ge=1, le=500),
    db=Depends(get_db),
):
    """Newest-first journal events. ``kind`` is a prefix (``poll.``), ``level`` a
    minimum; page with ``before=<nextBefore>``."""
    try:
        items = await events_store.query(
            db, kind=kind, level=level, tvdb_id=tvdb, tick_id=tick, source=source,
            before=before, limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"items": items, "nextBefore": items[-1]["id"] if len(items) == limit else None}


@router.get("/api/ticks")
async def list_ticks(limit: int = Query(96, ge=1, le=500), db=Depends(get_db)):
    return await tick_store.recent(db, limit=limit)


@router.get("/api/ticks/{tick_id}")
async def tick_detail(tick_id: int, db=Depends(get_db)):
    tick = await tick_store.get(db, tick_id)
    if tick is None:
        raise HTTPException(status_code=404, detail="Tick not found")
    return {**tick, "events": await events_store.for_tick(db, tick_id)}


@router.get("/api/operations/{op_id}")
async def operation_detail(op_id: int, ops=Depends(get_operations)):
    op = await ops.get(op_id)
    if op is None:
        raise HTTPException(status_code=404, detail="Operation not found")
    return op


@router.get("/api/summary")
async def overview_summary(db=Depends(get_db), monitor=Depends(get_monitor)):
    return {
        "placementCounts": await summary.placement_counts(db),
        "seriesStatusCounts": await summary.series_status_counts(db),
        "intents": await summary.intent_counts(db),
        "lastTick": await tick_store.last_completed(db),
        "instances": monitor.snapshot() if monitor is not None else [],
    }
```

- [ ] **Step 6: Exempt `/metrics` in `app/auth.py` and include the router**

In `verify_access`, replace the healthz check with:

```python
    # Machine endpoints must stay reachable with Access enabled: the in-container
    # Docker healthcheck and a LAN Prometheus scrape carry no Access JWT.
    # (/metrics has its own optional METRICS_TOKEN.)
    if request is not None and request.url.path in ("/healthz", "/metrics"):
        return None
```

In `app/main.py`, import `observability` alongside the other routers and add it to the include loop:

```python
from app.api import adding, catalog, instances, observability, policy, settings
...
for module in (instances, catalog, adding, settings, policy, observability):
    app.include_router(module.router)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_observability_api.py tests/test_auth.py tests/test_api.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add backend/app/api/observability.py backend/app/store/summary.py backend/app/state.py backend/app/auth.py backend/app/main.py backend/tests/test_observability_api.py
git commit -m "feat(api): /metrics, event journal, tick history and summary endpoints"
```

---

### Task 11: Live event stream (SSE)

**Files:**
- Create: `backend/app/obs/sse.py`
- Modify: `backend/app/api/observability.py` (add `/api/events/stream`)
- Test: `backend/tests/test_sse.py`

**Interfaces:**
- Consumes: `Journal.subscribe/unsubscribe`, `RESYNC` (Task 3); `events_store.since` (Task 3); `get_event_journal` (Task 10).
- Produces: `sse.event_stream(journal, db, *, last_event_id: int | None, heartbeat: float = HEARTBEAT_S, replay_limit: int | None = None)` async generator of `str` frames; constants `REPLAY_LIMIT = 500`, `HEARTBEAT_S = 15.0`, `PING`, `RESYNC_FRAME`; `frame(event) -> str`. Route `GET /api/events/stream` (`Last-Event-ID` header or `?since=`).
- Frame format: `id: <id>\nevent: journal\ndata: <json>\n\n`; `event: resync\ndata: {}\n\n` then the stream ends; `: ping\n\n` heartbeat.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_sse.py`:

```python
"""SSE over the journal: replay then live, no gaps or duplicates, heartbeat, resync."""
import asyncio

import pytest
from starlette.testclient import TestClient

from app.main import app
from app.obs import kinds, sse
from app.obs.journal import RESYNC
from app.state import get_db


async def _emit(journal, *messages):
    for message in messages:
        journal.emit(kinds.INSTANCE_UP, message)
    await journal.flush()


def _id(frame: str) -> int:
    return int(frame.split("\n", 1)[0].removeprefix("id: "))


async def test_replays_since_last_event_id_then_streams_live_without_duplicates(db, journal):
    await _emit(journal, "one", "two", "three")          # ids 1-3
    stream = sse.event_stream(journal, db, last_event_id=1, heartbeat=5)

    first = await stream.__anext__()                     # subscribed, then replayed id 2
    [sub] = journal._subs
    sub.offer({"id": 3, "kind": "instance.up", "message": "dup"})  # already covered by replay
    await _emit(journal, "four")                         # id 4 arrives live
    rest = [await stream.__anext__() for _ in range(2)]

    assert [_id(f) for f in [first, *rest]] == [2, 3, 4]
    assert "event: journal" in first
    await stream.aclose()
    assert journal._subs == set()


async def test_heartbeat_ping_when_idle(db, journal):
    stream = sse.event_stream(journal, db, last_event_id=None, heartbeat=0.01)
    assert await stream.__anext__() == sse.PING
    await stream.aclose()


async def test_resync_and_end_when_replay_exceeds_limit(db, journal):
    await _emit(journal, "a", "b", "c")
    stream = sse.event_stream(journal, db, last_event_id=0, replay_limit=2)
    assert await stream.__anext__() == sse.RESYNC_FRAME
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()
    assert journal._subs == set()


async def test_overflowed_subscription_ends_with_resync(db, journal):
    stream = sse.event_stream(journal, db, last_event_id=None, heartbeat=5)
    pending = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0.01)
    [sub] = journal._subs
    sub.queue.put_nowait(RESYNC)
    assert await pending == sse.RESYNC_FRAME
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()


def test_stream_endpoint_headers_and_last_event_id(db, journal, monkeypatch):
    asyncio.run(_emit(journal, "a", "b", "c"))
    monkeypatch.setattr(sse, "REPLAY_LIMIT", 1)   # 3 events behind → immediate resync, finite stream
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app).stream("GET", "/api/events/stream",
                                    headers={"Last-Event-ID": "0"}) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            assert r.headers["cache-control"] == "no-cache, no-transform"
            assert r.headers["x-accel-buffering"] == "no"
            assert "".join(r.iter_text()) == sse.RESYNC_FRAME
    finally:
        app.dependency_overrides.pop(get_db, None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_sse.py -v`
Expected: FAIL — `ImportError: cannot import name 'sse' from 'app.obs'`

- [ ] **Step 3: Create `app/obs/sse.py`**

```python
"""Server-sent events over the journal: replay from SQLite, then live, with no gaps.

The subscription is taken *before* the replay query, so an event persisted while
replaying is not lost; anything the replay already covered is skipped by id. A
client too far behind (or whose queue overflowed) gets a ``resync`` frame and the
stream ends — it should refetch its views and reconnect.
"""
from __future__ import annotations

import asyncio
import json

from app.obs.journal import RESYNC
from app.store import events as events_store

REPLAY_LIMIT = 500
HEARTBEAT_S = 15.0   # well inside Cloudflare's ~100s idle timeout
PING = ": ping\n\n"
RESYNC_FRAME = "event: resync\ndata: {}\n\n"


def frame(event: dict) -> str:
    return f"id: {event['id']}\nevent: journal\ndata: {json.dumps(event)}\n\n"


async def event_stream(journal, db, *, last_event_id: int | None,
                       heartbeat: float = HEARTBEAT_S, replay_limit: int | None = None):
    limit = REPLAY_LIMIT if replay_limit is None else replay_limit
    sub = journal.subscribe()
    try:
        last = last_event_id
        if last is not None:
            replay = await events_store.since(db, last, limit + 1)
            if len(replay) > limit:
                yield RESYNC_FRAME
                return
            for event in replay:
                yield frame(event)
                last = event["id"]
        while True:
            try:
                item = await asyncio.wait_for(sub.queue.get(), timeout=heartbeat)
            except asyncio.TimeoutError:
                yield PING
                continue
            if item is RESYNC:
                yield RESYNC_FRAME
                return
            if last is not None and item["id"] <= last:
                continue
            yield frame(item)
            last = item["id"]
    finally:
        journal.unsubscribe(sub)
```

- [ ] **Step 4: Add the route to `app/api/observability.py`**

Add imports:

```python
from starlette.responses import Response, StreamingResponse

from app.obs import sse
from app.state import get_db, get_event_journal, get_monitor, get_operations
```

and the route (after `list_events`):

```python
@router.get("/api/events/stream")
async def events_stream(
    request: Request,
    since: int | None = None,
    db=Depends(get_db),
    journal=Depends(get_event_journal),
):
    """Live journal as SSE. Resumes from ``Last-Event-ID`` (sent automatically by
    EventSource on reconnect) or ``?since=``; otherwise live events only."""
    header = request.headers.get("last-event-id", "")
    last = int(header) if header.isdigit() else since
    return StreamingResponse(
        sse.event_stream(journal, db, last_event_id=last),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_sse.py tests/test_observability_api.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/obs/sse.py backend/app/api/observability.py backend/tests/test_sse.py
git commit -m "feat(api): live journal stream over SSE with replay and heartbeat"
```

---

### Task 12: Audit trail for configuration and series changes

**Files:**
- Create: `backend/app/obs/audit.py`
- Modify: `backend/app/api/policy.py` (`put_policy`, `pause`, `resume`, `put_defaults`), `backend/app/api/settings.py` (`put_fallback_chains`), `backend/app/api/catalog.py` (`remove_series`)
- Test: `backend/tests/test_audit.py`

**Interfaces:**
- Consumes: `get_journal`, `kinds` (Task 3); `verify_access` (existing; returns the Access email or `None`).
- Produces: `audit.record(kind, message, *, actor=None, level="info", tvdb_id=None, instance_id=None, **data) -> None` — emits with `source="user"`; `actor` is added to `data` only when known.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_audit.py`:

```python
"""User-initiated configuration and series changes are journaled with before/after."""
import asyncio

import httpx
import pytest
import respx
from starlette.testclient import TestClient

from app.auth import verify_access
from app.main import app
from app.obs import kinds
from app.state import get_db, get_registry
from app.store import intents as intent_store
from tests.test_chain import A, make_registry

TVDB = 99


@pytest.fixture
def api(db, journal):
    reg = make_registry()
    app.dependency_overrides[get_registry] = lambda: reg
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_db, None)


def _events(journal, kind):
    return [e for e in journal.pending() if e["kind"] == kind]


def test_defaults_change_records_before_after_and_changed_keys(api, journal):
    api.put("/api/settings/defaults", json={"stalledDays": 3, "minSeeders": 3})
    api.put("/api/settings/defaults", json={"stalledDays": 5, "minSeeders": 3})

    first, second = _events(journal, kinds.CONFIG_DEFAULTS_CHANGED)
    assert first["data"]["changed"] == ["minSeeders", "stalledDays"]
    assert second["data"] == {
        "before": {"stalledDays": 3, "minSeeders": 3},
        "after": {"stalledDays": 5, "minSeeders": 3},
        "changed": ["stalledDays"],
    }
    assert second["source"] == "user"


def test_actor_recorded_when_access_identifies_the_user(api, journal):
    app.dependency_overrides[verify_access] = lambda: "user@example.com"
    try:
        api.put("/api/settings/defaults", json={"minSeeders": 1})
    finally:
        app.dependency_overrides.pop(verify_access, None)
    [ev] = _events(journal, kinds.CONFIG_DEFAULTS_CHANGED)
    assert ev["data"]["actor"] == "user@example.com"


def test_policy_change_is_journaled_for_the_series(api, journal):
    body = {"preferredTier": "4k", "fallbacks": [{"instanceId": "1080p", "afterDays": 14}],
            "allowSplit": False}
    assert api.put(f"/api/series/{TVDB}/policy", json=body).status_code == 200

    [ev] = _events(journal, kinds.CONFIG_POLICY_CHANGED)
    assert ev["tvdbId"] == TVDB
    assert ev["data"]["before"] is None
    assert ev["data"]["after"]["allowSplit"] is False


def test_pause_and_resume_are_journaled(api, db, journal):
    asyncio.run(intent_store.ensure(db, tvdb_id=TVDB, title="Mad Men", chain_key="4k", now="t"))
    api.post(f"/api/series/{TVDB}/pause")
    api.post(f"/api/series/{TVDB}/resume")

    events = [e for e in journal.pending() if e["kind"].startswith("series.")]
    assert [(e["kind"], e["tvdbId"]) for e in events] == [
        (kinds.SERIES_PAUSED, TVDB), (kinds.SERIES_RESUMED, TVDB)]
    assert "Mad Men" in events[0]["message"]


def test_chain_change_is_journaled(api, journal):
    payload = {"4k": [{"instanceId": "1080p", "profile": "SD"}]}
    assert api.put("/api/settings/fallback-chains", json=payload).status_code == 200

    [ev] = _events(journal, kinds.CONFIG_CHAINS_CHANGED)
    assert ev["data"]["before"] == {"4k": [
        {"instanceId": "1080p", "profile": None, "rootFolder": None},
        {"instanceId": "1080p", "profile": "SD", "rootFolder": None},
    ]}
    assert ev["data"]["after"] == payload


@respx.mock
def test_series_removal_is_journaled_with_tvdb(api, journal):
    respx.get(f"{A}/api/v3/series/20").mock(
        return_value=httpx.Response(200, json={"id": 20, "tvdbId": TVDB, "title": "Mad Men"}))
    respx.delete(f"{A}/api/v3/series/20").mock(return_value=httpx.Response(200))

    r = api.delete("/api/instances/1080p/series/20", params={"deleteFiles": "true"})

    assert r.status_code == 200
    [ev] = _events(journal, kinds.SERIES_REMOVED)
    assert (ev["tvdbId"], ev["instanceId"], ev["level"]) == (TVDB, "1080p", "warn")
    assert ev["data"] == {"seriesId": 20, "deleteFiles": True, "title": "Mad Men"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_audit.py -v`
Expected: FAIL — no events recorded (`ValueError: not enough values to unpack`).

- [ ] **Step 3: Create `app/obs/audit.py`**

```python
"""Audit trail: user-initiated changes, journaled with before/after and the actor."""
from __future__ import annotations

from app.obs.journal import get_journal


def record(kind: str, message: str, *, actor: str | None = None, level: str = "info",
           tvdb_id: int | None = None, instance_id: str | None = None, **data) -> None:
    """Journal a user action. ``actor`` is the Cloudflare Access email when Access
    is enabled (``verify_access``), and is omitted otherwise."""
    if actor:
        data["actor"] = actor
    get_journal().emit(kind, message, level=level, source="user",
                       tvdb_id=tvdb_id, instance_id=instance_id, data=data)
```

- [ ] **Step 4: Record changes in `app/api/policy.py`**

Add imports:

```python
from app.auth import verify_access
from app.obs import audit, kinds
```

Replace `put_policy`, `pause`, `resume` and `put_defaults` with:

```python
@router.put("/series/{tvdb_id}/policy")
async def put_policy(tvdb_id: int, policy: SeriesPolicy, db=Depends(get_db),
                     actor: str | None = Depends(verify_access)):
    now = _now()
    existing = await intent_store.get(db, tvdb_id)
    before = json.loads(existing["policy_json"]) if existing and existing["policy_json"] else None
    await intent_store.ensure(db, tvdb_id=tvdb_id, title=None,
                              chain_key=policy.preferredTier, now=now)
    await intent_store.set_policy(db, tvdb_id, policy.to_json(), now)
    title = (existing["title"] if existing else None) or f"tvdb:{tvdb_id}"
    audit.record(kinds.CONFIG_POLICY_CHANGED, f"Policy updated for {title}", actor=actor,
                 tvdb_id=tvdb_id, before=before, after=json.loads(policy.to_json()))
    return {"ok": True, "tvdbId": tvdb_id}
```

```python
@router.post("/series/{tvdb_id}/pause")
async def pause(tvdb_id: int, reg: Registry = Depends(get_registry), db=Depends(get_db),
                actor: str | None = Depends(verify_access)):
    await _ensure_intent(reg, db, tvdb_id)
    await intent_store.set_paused(db, tvdb_id, True, _now())
    intent = await intent_store.get(db, tvdb_id)
    audit.record(kinds.SERIES_PAUSED, f"Paused {intent['title'] or f'tvdb:{tvdb_id}'}",
                 actor=actor, tvdb_id=tvdb_id)
    return {"ok": True, "tvdbId": tvdb_id, "paused": True}


@router.post("/series/{tvdb_id}/resume")
async def resume(tvdb_id: int, reg: Registry = Depends(get_registry), db=Depends(get_db),
                 actor: str | None = Depends(verify_access)):
    await _ensure_intent(reg, db, tvdb_id)
    await intent_store.set_paused(db, tvdb_id, False, _now())
    intent = await intent_store.get(db, tvdb_id)
    audit.record(kinds.SERIES_RESUMED, f"Resumed {intent['title'] or f'tvdb:{tvdb_id}'}",
                 actor=actor, tvdb_id=tvdb_id)
    return {"ok": True, "tvdbId": tvdb_id, "paused": False}
```

```python
@router.put("/settings/defaults")
async def put_defaults(defaults: dict, db=Depends(get_db),
                       actor: str | None = Depends(verify_access)):
    before = await settings_store.get_defaults(db)
    await settings_store.set_defaults(db, defaults)
    changed = sorted(k for k in set(before) | set(defaults) if before.get(k) != defaults.get(k))
    audit.record(kinds.CONFIG_DEFAULTS_CHANGED,
                 f"Default settings updated: {', '.join(changed) or 'no changes'}",
                 actor=actor, before=before, after=defaults, changed=changed)
    return {"ok": True}
```

- [ ] **Step 5: Record chain changes in `app/api/settings.py`**

Add imports `from app.auth import verify_access` and `from app.obs import audit, kinds`, add a helper, and update `put_fallback_chains`:

```python
def _chains_payload(reg: Registry) -> dict:
    """The live chains in the API/override JSON shape (for audit before/after)."""
    return {
        i.id: [
            {"instanceId": s.instanceId, "profile": s.profile, "rootFolder": s.root_folder}
            for s in reg.fallback_chain(i.id)
        ]
        for i in reg.all()
        if reg.has_fallback(i.id)
    }


@router.put("/settings/fallback-chains")
async def put_fallback_chains(
    payload: dict[str, list[dict]],
    reg: Registry = Depends(get_registry),
    db=Depends(get_db),
    actor: str | None = Depends(verify_access),
):
    """Replace the fallback chains. Validates every referenced instance, persists
    the override, and applies it to the live registry (no restart needed)."""
    valid = {i.id for i in reg.all()}
    for start, steps in payload.items():
        if start not in valid:
            raise HTTPException(status_code=400, detail=f"Unknown instance: {start}")
        for s in steps:
            if s.get("instanceId") not in valid:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown instance in chain: {s.get('instanceId')}",
                )
    before = _chains_payload(reg)
    await settings_store.set_chain_overrides(db, payload)
    reg.set_chains(Registry.coerce_chains(payload))
    audit.record(kinds.CONFIG_CHAINS_CHANGED, "Fallback chains updated", actor=actor,
                 before=before, after=payload)
    return {"ok": True}
```

- [ ] **Step 6: Record removals in `app/api/catalog.py`**

Add imports `from app.auth import verify_access` and `from app.obs import audit, kinds`, then replace `remove_series`:

```python
@router.delete("/instances/{instance_id}/series/{series_id}")
async def remove_series(
    instance_id: str,
    series_id: int,
    deleteFiles: bool = False,
    reg: Registry = Depends(get_registry),
    actor: str | None = Depends(verify_access),
):
    """Remove a series from an instance. Files are kept unless deleteFiles=true."""
    try:
        inst = reg.get(instance_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown instance: {instance_id}")
    try:
        series = await inst.client.get_series(series_id)  # for the audit record only
    except Exception:  # noqa: BLE001 - bookkeeping must never block the removal
        series = {}
    await inst.client.delete_series(series_id, delete_files=deleteFiles)
    title = series.get("title") or f"series {series_id}"
    audit.record(
        kinds.SERIES_REMOVED,
        f"Removed {title} from {instance_id}" + (" (files deleted)" if deleteFiles else ""),
        actor=actor, level="warn", tvdb_id=series.get("tvdbId"), instance_id=instance_id,
        seriesId=series_id, deleteFiles=deleteFiles, title=series.get("title"),
    )
    return {"ok": True, "instanceId": instance_id, "seriesId": series_id}
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_audit.py tests/test_api.py tests/test_policy_api.py tests/test_settings_api.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add backend/app/obs/audit.py backend/app/api/policy.py backend/app/api/settings.py backend/app/api/catalog.py backend/tests/test_audit.py
git commit -m "feat(audit): journal settings, policy, pause and removal changes"
```

---

### Task 13: Wire it into the app lifespan, document, verify end to end

**Files:**
- Modify: `backend/app/main.py` (`lifespan`)
- Modify: `.env.example`, `DEPLOY.md` (Operations → Health monitoring), `CLAUDE.md` (test pattern + new Observability section)
- Test: `backend/tests/test_lifespan.py`

**Interfaces:**
- Consumes: `Journal`, `set_journal` (Task 3); `Monitor` (Task 8); `Reconciler(monitor=)` (Task 9); `Registry.aclose()` (Task 5); routes (Tasks 10–11).
- Produces: running app with `app.state.journal`, `app.state.monitor`; both reset to `None` on shutdown so later in-process tests fall back to overrides/the global journal.

- [ ] **Step 1: Write the failing test**

`backend/tests/test_lifespan.py`:

```python
"""The app boots with journal, monitor and observability routes wired, and shuts down cleanly."""
import textwrap

from starlette.testclient import TestClient

from app.main import app
from app.obs.journal import NullJournal, get_journal


def test_app_lifespan_wires_observability(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(textwrap.dedent("""
        instances:
          - id: "1080p"
            name: "Sonarr 1080p"
            url: "http://127.0.0.1:9"
            api_key: "${TEST_SONARR_KEY}"
    """))
    monkeypatch.setenv("CONFIG_PATH", str(config))
    monkeypatch.setenv("DATA_PATH", str(tmp_path / "relay.db"))
    monkeypatch.setenv("TEST_SONARR_KEY", "k")
    monkeypatch.setenv("RECONCILER_ENABLED", "false")

    with TestClient(app) as client:
        assert not isinstance(get_journal(), NullJournal)
        assert client.get("/healthz").status_code == 200
        assert "# TYPE relay_sonarr_up gauge" in client.get("/metrics").text
        summary = client.get("/api/summary").json()
        assert [i["id"] for i in summary["instances"]] == ["1080p"]

    assert isinstance(get_journal(), NullJournal)
    assert app.state.journal is None and app.state.monitor is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_lifespan.py -v`
Expected: FAIL — `AttributeError: 'State' object has no attribute 'monitor'` (or the journal is still a `NullJournal`).

- [ ] **Step 3: Rewrite `lifespan` in `app/main.py`**

Add imports:

```python
import contextlib

from app.obs.journal import Journal, set_journal
from app.services.monitor import Monitor
```

Replace `lifespan` with:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    enabled = _reconciler_enabled()
    try:
        app.state.registry = build_registry()
    except (ValueError, FileNotFoundError, KeyError) as exc:
        # Fail loudly and actionably rather than with a cryptic traceback when a
        # required API-key env var or config field is missing.
        logging.getLogger("app").error("Configuration error: %s", exc)
        raise RuntimeError(
            f"Relay failed to start — {exc}. Check config.yaml and that the "
            f"referenced API-key env vars (e.g. SONARR_*_API_KEY) are set in .env."
        ) from exc
    app.state.db = Database(data_path())  # also marks ticks interrupted by a crash
    app.state.journal = Journal(app.state.db)
    set_journal(app.state.journal)
    app.state.operations = OperationStore(app.state.db)
    # Apply any UI-saved fallback-chain override on top of config.yaml.
    try:
        override = await settings_store.get_chain_overrides(app.state.db)
        if override:
            app.state.registry.set_chains(app.state.registry.coerce_chains(override))
            logging.getLogger("app").info("applied fallback-chain override from DB")
    except Exception:  # noqa: BLE001 - a bad override must not block startup
        logging.getLogger("app").exception("failed to apply fallback-chain override")
    # The monitor observes even when automation is off.
    app.state.monitor = Monitor(app.state.registry, app.state.db)
    app.state.reconciler = Reconciler(
        app.state.registry, app.state.db, app.state.operations,
        enabled=enabled, stalled_cleanup=_stalled_cleanup_enabled(),
        dangerous_cleanup=_dangerous_cleanup_enabled(),
        search_stall_cleanup=_search_stall_cleanup_enabled(),
        monitor=app.state.monitor,
    )
    journal_task = asyncio.create_task(app.state.journal.run())
    monitor_task = asyncio.create_task(app.state.monitor.run())
    reconciler_task = None
    if enabled:
        reconciler_task = asyncio.create_task(app.state.reconciler.run())
    else:
        logging.getLogger("app").info("reconciler disabled (RECONCILER_ENABLED=false)")
    try:
        yield
    finally:
        if reconciler_task is not None:
            app.state.reconciler.stop()
            await reconciler_task
        # A probe may be mid-request (up to the client timeout): cancel, don't wait.
        monitor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor_task
        await app.state.journal.aclose()   # final flush
        await journal_task
        set_journal(None)
        await app.state.registry.aclose()
        app.state.db.close()
        # Don't leave closed resources on the module-level app for later requests.
        app.state.journal = None
        app.state.monitor = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_lifespan.py -v && uv run pytest -q`
Expected: PASS; full suite green.

- [ ] **Step 5: Document**

`.env.example` — after the `LOG_LEVEL=INFO` line add:

```bash
# Optional: LOG_FORMAT=json writes one JSON object per log line (for log shippers).
LOG_FORMAT=text

# Optional: require "Authorization: Bearer <token>" on GET /metrics. /metrics is
# exempt from Cloudflare Access so a LAN Prometheus can scrape it.
# METRICS_TOKEN=
```

`DEPLOY.md` — in "### Health monitoring", replace the bullet that begins `- Logs go to stdout` (two lines) with:

````markdown
- **`GET /metrics`** — Prometheus text format (tick counts/durations, Sonarr call
  latency and outcomes per instance, `relay_sonarr_up`, placement states, sweep
  removals, journal volume). Exempt from Cloudflare Access; set `METRICS_TOKEN` to
  require a bearer token. Example scrape job and a staleness alert:

  ```yaml
  scrape_configs:
    - job_name: relay
      static_configs: [{ targets: ["192.168.1.50:8088"] }]
      # authorization: { credentials: "<METRICS_TOKEN>" }
  # alert: time() - relay_last_tick_timestamp_seconds > 3600
  ```
- **Event journal** — `GET /api/events?kind=&level=&tvdb=&tick=` (newest first, page with
  `before=`), `GET /api/ticks` / `GET /api/ticks/{id}` (per-phase timings and the tick's
  events), `GET /api/summary`, and a live `GET /api/events/stream` (SSE; 15s `: ping`
  heartbeats — `curl -N` through the tunnel should show pings every ~15s, not in bursts).
  Ticks are `ok`, `degraded` (a sweep failed or a Sonarr was unreachable) or `failed`.
- **Instance monitor** — probes each Sonarr every 60s and reads its own `/health` checks
  every ~5 min (indexer outages show up here), even with `RECONCILER_ENABLED=false`.
- **Retention** — events are kept 3d (debug) / 30d (info) / 90d (warn, error), ticks 90d,
  operations 60d; pruned once a day.
- Logs go to stdout — `docker compose logs -f sonarr-unified`. Set `LOG_LEVEL=DEBUG`
  in `.env` for per-request detail, `LOG_FORMAT=json` for structured lines. Lines
  inside a tick carry `[tick=N tvdb=M]`.
````

`CLAUDE.md` — in "## Test pattern", replace this text:

```markdown
There is no conftest — shared helpers (`make_registry`, base URLs `A`=1080p /
`B`=4k, `_nosleep`) live in `tests/test_chain.py`.
```

(the line break may fall differently in the file — match the sentence) with:

```markdown
`tests/conftest.py` provides `db` (tmp SQLite) and `journal` (a real journal installed
process-wide) fixtures and resets the global journal after every test; Sonarr helpers
(`make_registry`, base URLs `A`=1080p / `B`=4k, `_nosleep`) live in `tests/test_chain.py`.
```

and append this section directly after the "### Autonomous reconciler" section:

```markdown
### Observability (`app/obs/`)

- **Journal** — `get_journal().emit(kind, message, ...)` from anywhere (poller, placement,
  sweeps, API audit); kinds are a closed set in `obs/kinds.py` (unknown kind raises).
  Buffered, flushed to the `event` table every second and at the end of each tick phase,
  then fanned out to SSE subscribers. **Journal changes, not repetitions** — per-call data
  belongs in metrics, not events.
- **Context** — `obs/context.bind(tick_id=..., tvdb_id=...)`; events, operations and log
  lines pick the ids up implicitly.
- **Ticks** — every reconciler pass is a `tick` row (`ok`/`degraded`/`failed`, per-phase
  ms/errors). Health (`status()`, `/healthz`) is derived from it, so it survives restarts.
- **Metrics** — hand-rolled registry in `obs/metrics.py` (no prometheus_client); never label
  by tvdb id. `SonarrClient._request` is the single choke point for call metrics/stats.
- **Monitor** (`services/monitor.py`) — always-on probe + Sonarr `/health` watch + daily
  retention (`services/retention.py`).
```

- [ ] **Step 6: Verify end to end**

Run: `cd backend && uv run pytest -q`
Expected: all tests pass (the Phase 0 baseline of 219 plus the new tests).

Run: `cd .. && docker compose build`
Expected: image builds.

Manual smoke (local, no live Sonarr needed):

```bash
cd backend
CONFIG_PATH=../config.yaml DATA_PATH=/tmp/relay-smoke.db RECONCILER_ENABLED=false \
  uv run uvicorn app.main:app --port 8765 &
sleep 3
curl -s localhost:8765/healthz
curl -s localhost:8765/metrics | grep -E "^# TYPE relay_(tick_total|sonarr_up)"
curl -s localhost:8765/api/summary
curl -s -X POST localhost:8765/api/reconcile/tick | head -c 300; echo
curl -s "localhost:8765/api/ticks?limit=1"
timeout 20 curl -sN localhost:8765/api/events/stream   # expect a ": ping" within 15s
kill %1
```

Expected: healthz 200 JSON with `lastTick`; metric TYPE lines present; the manual tick returns `tickId`; `/api/ticks` shows it with `phases`; the stream prints `: ping`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/main.py backend/tests/test_lifespan.py .env.example DEPLOY.md CLAUDE.md
git commit -m "feat(app): wire journal and monitor into the lifespan; document observability"
```

---

## Deployment (after all tasks — the user's call)

Follow the spec's "Rollout & verification": back up `data/relay.db`, deploy with
`RECONCILER_ENABLED=false`, check `/healthz`, `/metrics`, `/api/summary` and the SSE stream
through the Cloudflare tunnel, then re-enable the reconciler and watch `/api/ticks`.
