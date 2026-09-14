# Observability foundation (Phase 1)

**Date:** 2026-09-14
**Parent plan:** `~/.claude/plans/i-want-more-features-atomic-sparkle.md` (approved) — Phase 1
**Status:** Approved design, pending implementation
**Constraint:** no orchestration behavior change; no new runtime dependencies.

## Problem

Every serious Relay incident so far was a silent failure found by hand, weeks late:
57 episodes wedged in `searching` for 6 weeks, 80 dead size=0 queue records for 37 days,
frozen torrents, a 6-hour stale "0 releases" verdict during a Prowlarr 429 outage. Each was
diagnosed with ad-hoc SQL and `docker logs | grep`. Today:

- Poller transitions are computed and **discarded** (`reconciler.py:155`); the search-stall
  reaper's reverts reach only stdout.
- Reconciler health is **in memory** and lost on restart; poll/sweep failures never mark a tick
  unhealthy; `POST /reconcile/tick` bypasses health recording entirely.
- No metrics, no per-Sonarr latency/error tracking, no correlation between a log line, a tick,
  and an operation, and no audit of settings/policy changes.
- `operation` rows grow forever; `recent()` issues one query per operation (N+1).

## Goal

Relay records what it did and why, durably and queryably, and exposes it three ways: an event
journal + tick history in SQLite (API + live SSE stream), a Prometheus `/metrics` endpoint, and
context-enriched logs. Health survives restarts and reflects partial failures.

Non-goals (later phases): decision/skip journaling and dry-run (P2), alert rules and webhooks
(P4), any UI (5B). The API shapes here are designed for those consumers.

## Design

### 1. Data model (`app/db.py`, additive)

```sql
CREATE TABLE IF NOT EXISTS tick (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trigger TEXT NOT NULL,                     -- schedule | manual
  started_at TEXT NOT NULL, finished_at TEXT, duration_ms INTEGER,
  status TEXT NOT NULL DEFAULT 'running',    -- running | ok | degraded | failed
  series_count INTEGER NOT NULL DEFAULT 0, actions INTEGER NOT NULL DEFAULT 0,
  transitions INTEGER NOT NULL DEFAULT 0, swept INTEGER NOT NULL DEFAULT 0,
  errors INTEGER NOT NULL DEFAULT 0, error TEXT,
  phases_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_tick_started ON tick(started_at);

CREATE TABLE IF NOT EXISTS event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, kind TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT 'info',        -- debug | info | warn | error
  source TEXT NOT NULL DEFAULT 'system',     -- reconciler | poller | sweep | user | monitor | system
  tick_id INTEGER, operation_id INTEGER,
  tvdb_id INTEGER, season INTEGER, episode INTEGER, instance_id TEXT,
  message TEXT NOT NULL, data_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_ts ON event(ts);
CREATE INDEX IF NOT EXISTS idx_event_tvdb ON event(tvdb_id, id);
CREATE INDEX IF NOT EXISTS idx_event_tick ON event(tick_id);
CREATE INDEX IF NOT EXISTS idx_event_kind ON event(kind, id);
CREATE INDEX IF NOT EXISTS idx_event_level ON event(level, id);
```

- `_ensure_column("operation", "tick_id", "INTEGER")`.
- `Database.execute_batch(sql, rows) -> list[int]`: one transaction, returns row ids.
- On startup, ticks left `running` (process died mid-tick) become `failed` with
  `error='interrupted (restart)'`.

`phases_json` shape: `{"poll": {"ms", "transitions", "errors": {instanceId: msg}},
"sweep_stalled"|"sweep_dangerous"|"sweep_search_stall": {"ms", "count", "error"},
"availability": {"live", "cached", "zero": {instanceId: n}, "liveBy": {instanceId: n}},
"reconcile": {"ms", "series", "actions", "errors"}}`.

### 2. Event vocabulary (`app/obs/kinds.py`)

A closed set of string constants; the UI (5B) filters by dotted prefix.

| Group | Kinds (P1) |
|---|---|
| loop | `tick.finished` (info ok / warn degraded), `tick.failed` (error), `loop.started`, `loop.stopped` |
| poll | `poll.transition` (data: from, to, downloadId), `poll.instance_error` (warn) |
| sweep | `sweep.stalled.removed`, `sweep.dangerous.removed`, `sweep.search_stall.reverted`, `sweep.failed` (error) |
| availability | `availability.changed` (only when `qualifies` flips or releases go 0↔>0), `availability.zero_spike` (warn) |
| instance | `instance.down` (error), `instance.up`, `instance.health_changed` (warn if any Sonarr health item is `error`/`warning`) |
| config/user | `config.defaults_changed`, `config.policy_changed`, `config.chains_changed`, `series.paused`, `series.resumed`, `series.removed` |

**Rule: journal changes, not repetitions.** Per-HTTP-call data goes to metrics and DEBUG logs,
never the event table. An emit with an unknown kind raises in tests (guard against typos).

### 3. Correlation context (`app/obs/context.py`)

`contextvars`: `tick_id`, `operation_id`, `tvdb_id`, `tick_stats`. `bind(**values)` is a context
manager that sets and resets tokens. asyncio tasks and `gather` children inherit context, so
anything emitted or logged inside a tick carries its `tick_id` without plumbing.
`OperationStore.start()` stamps `tick_id` from context.

### 4. Journal (`app/obs/journal.py`)

- `Journal(db, *, clock, max_buffer=1000)`:
  - `emit(kind, message, *, level="info", source=..., tvdb_id=, season=, episode=,
    instance_id=, operation_id=, data=None)` — **synchronous, never raises**: validates kind,
    fills `tick_id`/`operation_id`/`tvdb_id` from context when not given, appends to a buffer,
    increments `relay_events_total{group,level}`. Buffer overflow drops oldest debug/info first
    and logs a warning.
  - `async flush()` — `execute_batch` the buffer, then publish the persisted rows (with ids) to
    subscribers.
  - `subscribe() -> Subscription` (bounded `asyncio.Queue(500)`; on overflow the subscription is
    flagged and receives a single `resync` marker); `unsubscribe()`.
  - `async run(interval=1.0)` periodic flusher; `async aclose()` final flush.
- Process-wide accessor, mirroring the metrics registry: `set_journal(j)`, `get_journal()`;
  default is `NullJournal` (no-ops). Poller, placement and sweeps call `get_journal().emit(...)`
  so no signatures change and existing tests are unaffected. The reconciler flushes at the end
  of each tick phase so a tick's events land promptly.

### 5. Metrics (`app/obs/metrics.py`)

Hand-rolled registry (~120 lines): `Counter`, `Gauge` (settable, or callback evaluated at scrape),
`Histogram` (fixed buckets), labels as kwargs. `render() -> str` emits Prometheus text exposition
0.0.4 (`# HELP`/`# TYPE`, escaped label values, `_bucket{le}`/`_sum`/`_count`, `+Inf`).
Module-level `METRICS` registry.

| Metric | Type | Labels |
|---|---|---|
| `relay_tick_total` | counter | status |
| `relay_tick_duration_seconds` | histogram | — |
| `relay_last_tick_timestamp_seconds` | gauge | — |
| `relay_reconciler_healthy` | gauge | — |
| `relay_placement_episodes` | gauge (callback, cached ≤15s) | state |
| `relay_series_intents` | gauge (callback) | paused |
| `relay_sonarr_requests_total` | counter | instance, method, endpoint, outcome (ok / http_4xx / http_5xx / timeout / error) |
| `relay_sonarr_request_duration_seconds` | histogram | instance |
| `relay_sonarr_up` | gauge | instance |
| `relay_availability_checks_total` | counter | instance, result (qualifies / rejected / zero / cached) |
| `relay_sweep_removed_total` | counter | sweep |
| `relay_events_total` | counter | group, level |

`endpoint` is the path with numeric segments replaced by `{id}`. Never label by tvdb id.
Callback gauges read the DB, so `/metrics` is an async handler that refreshes them before render.

### 6. Instrumented Sonarr client (`app/sonarr/client.py`)

All verbs route through one `_request(method, path, *, params, json, timeout)`:

- **Shared `httpx.AsyncClient`** per client, cached per running event loop (`(loop, client)`;
  recreated if the loop differs — TestClient and pytest-asyncio use different loops).
  `async aclose()`; `Registry.aclose()` closes all, called from lifespan shutdown.
- **Timing/outcome** into `ClientStats`: deque of the last 200 `(ts, ms, ok)`, `last_ok_at`,
  `last_error`, `consecutive_failures`, and `snapshot()` → `{p50Ms, p95Ms, errorRate5m,
  lastOkAt, lastError, consecutiveFailures}`. Clock and perf-counter injectable.
- Metrics + one DEBUG log line per call (`GET /queue 200 143ms`).
- New `health()` → `GET /api/v3/health`.
- Behavior preserved: per-call `timeout` (the 180s release search), `raise_for_status`,
  `_delete` returns None, JSON parsing.

### 7. Reconciler (`app/reconciler.py`)

- Constructor gains `journal=None` (→ `NullJournal`); existing tests construct unchanged.
- `_run_once(trigger="schedule")`:
  1. `ticks.start()` → bind `tick_id` and a fresh `TickStats`.
  2. Run `tick()`; each phase is wrapped in `async with self._phase(name)` recording ms/error
     into stats. Poll: per-instance errors (currently in `poll_all`'s return value) are recorded
     and emitted `poll.instance_error`; transitions count. A sweep exception records the error
     and emits `sweep.failed` — still never stops the tick.
  3. Status: `failed` if `tick()` raised; `degraded` if any poll instance error, sweep error or
     series error; else `ok`. `ticks.finish()`, emit `tick.finished`/`tick.failed`, update
     metrics, flush journal.
  4. In-memory counters remain as a cache but are no longer the source of truth.
- `_run_once` is serialized by an `asyncio.Lock`. `POST /api/reconcile/tick` calls
  `run_manual()`, which returns **409** if a tick is running, else `_run_once("manual")`;
  response is `{tickId, status, reconciled}` (adds fields to today's `{reconciled}`).
- **Health from persisted ticks.** `async status()` / `async is_healthy()`:
  - reference time = `max(last non-running tick's finished_at, process start)` — the process
    start term is the startup grace, so a long first tick after downtime doesn't trip Docker's
    healthcheck;
  - healthy = disabled, or `now - reference <= 2 × interval`;
  - a `degraded` tick counts as alive (partial failure ≠ wedged loop); `/healthz` stays 503 only
    for a stale loop — a down Sonarr must never make Docker restart Relay.
  - Response keeps every existing field (derived from the DB) and adds
    `lastTick {id, status, trigger, startedAt, durationMs, actions, transitions, swept, errors}`,
    `running`, `nextTickAt`, `instances` (from the Monitor).
- End of tick: if any instance had `≥10` live availability checks and `zero/live ≥ 0.8`, emit
  `availability.zero_spike` (detection only).

### 8. Emission points (no signature changes)

- `poller.poll_instance`: `poll.transition` per applied transition (with `instance_id`).
- `poller.sweep_stalled` / `sweep_dangerous`: `sweep.*.removed` with title, age, progress,
  downloadId; `relay_sweep_removed_total`. `sweep_search_stalls`: `sweep.search_stall.reverted`.
- `placement.refresh_availability`: on a live (non-cached) check, compare with the previous cached
  row and emit `availability.changed` on a flip; count into `tick_stats` (if bound) and
  `relay_availability_checks_total`.
- Audit (`source="user"`, `data={"before", "after"}`, actor email from `verify_access` when
  Access is enabled): `PUT /settings/defaults`, `PUT /series/{tvdb}/policy`,
  `POST /series/{tvdb}/pause|resume`, `PUT /settings/fallback-chains`,
  `DELETE /instances/{id}/series/{sid}`.

### 9. Monitor loop (`app/services/monitor.py`)

An always-on background task, independent of `RECONCILER_ENABLED` (observation must work even
when automation is off). Every 60s:

- **Probe** each instance's `system_status`. Two consecutive failures → `down`
  (`instance.down`, `relay_sonarr_up=0`); first success after down → `instance.up`.
- Every 5th iteration, fetch Sonarr `GET /api/v3/health`; emit `instance.health_changed` when the
  set of `(source, type, message)` items changes. (This is how an indexer outage that Sonarr
  answers with HTTP 200 + empty results becomes visible.)
- **Retention**, at most daily (meta `last_prune_at`): delete events older than 3d (debug),
  30d (info), 90d (warn/error); ticks 90d; operations 60d (steps cascade); `download_progress`
  rows unchanged for 30d. Chunked deletes (≤5000 rows per statement) so the single DB lock is
  never held long.
- `snapshot()` → per-instance `{id, name, up, since, consecutiveFailures, sonarrHealth[],
  client: ClientStats.snapshot()}`; used by reconciler status and `/api/summary`.
- Clock/sleep injectable; `step()` is the single-iteration test entrypoint.

P4's alert engine will hook into this same loop.

### 10. Logging (`app/obs/logging.py`)

`ContextFilter` copies context vars onto records. Text format appends `[tick=12 op=7 tvdb=81189]`
when set. `LOG_FORMAT=json` selects `JsonFormatter` (one object per line: ts, level, logger, msg,
tick_id, operation_id, tvdb_id, exc). `main._configure_logging` wires both.

### 11. API (`app/api/observability.py`)

| Method | Path | Notes |
|---|---|---|
| GET | `/metrics` | text/plain 0.0.4. Exempt from Access (like `/healthz`); if `METRICS_TOKEN` is set, requires `Authorization: Bearer <token>` |
| GET | `/api/events` | `kind` (prefix), `level` (minimum), `tvdb`, `tick`, `source`, `before` (id), `limit` ≤500 → `{items, nextBefore}` |
| GET | `/api/events/stream` | SSE, see below |
| GET | `/api/ticks` | `limit` ≤500, newest first, `phases` parsed |
| GET | `/api/ticks/{id}` | tick + its events |
| GET | `/api/operations/{id}` | one operation with steps (404 if missing) |
| GET | `/api/summary` | `{placementCounts, seriesStatusCounts, intents: {active, paused}, lastTick, instances}` |

**SSE stream:** subscribe to the journal *first*, then replay persisted events with
`id > Last-Event-ID` (header or `?since=`) up to 500 (more → a `resync` frame), then relay live
events skipping ids already replayed. Frames: `id: <eventId>\nevent: journal\ndata: <json>`;
`event: resync` on overflow; `: ping` comment every 15s. Headers
`Cache-Control: no-cache, no-transform`, `X-Accel-Buffering: no`. The generator lives in
`app/obs/sse.py` with injectable heartbeat timeout for tests.

### 12. Operations store

`recent()` becomes two queries (operations, then `operation_step WHERE operation_id IN (...)`),
same output shape. New `get(op_id)`.

### 13. Wiring (`app/main.py`, `app/state.py`)

Lifespan order: logging → registry → DB (marks interrupted ticks) → journal (`set_journal`) →
operations → monitor → reconciler; start journal flusher, monitor, and (if enabled) reconciler
tasks; shut down in reverse, final journal flush, `registry.aclose()`, DB close.
New dependencies `get_journal`, `get_monitor`. `/healthz` awaits `status()`.

## Error handling

- Journal `emit` never raises into callers; a failed `flush` logs, keeps the buffer (bounded), and
  retries next interval.
- Monitor and journal tasks catch-and-log per iteration, like the reconciler loop.
- Metrics rendering failures in one callback gauge skip that gauge and log.
- Retention errors are logged and retried next day window.

## Testing (TDD — tests first)

New: `test_metrics.py` (golden render, escaping, histogram buckets, `/metrics` content type, Access
exemption, `METRICS_TOKEN`), `test_journal.py` (emit→flush persists, context tick_id, subscriber
receives ids, overflow→resync, NullJournal, unknown kind), `test_ticks.py` (`_run_once` ok /
degraded on sweep error / degraded on poll instance error / failed on raise; persisted health
survives a new Reconciler on the same DB; interrupted ticks marked failed; manual tick 409 while
locked; startup grace), `test_client_instrumentation.py` (one AsyncClient reused across calls,
stats on 200/500/ConnectError with fake perf counter, endpoint templating, per-call timeout
preserved), `test_monitor.py` (down after 2 failures emits once, up emits, health diff emits,
retention runs at most daily), `test_retention.py` (age/level matrix, cascade, chunking),
`test_events_api.py` (filters, keyset paging, SSE generator: replay-then-live without duplicates,
heartbeat with fake timeout, resync), `test_audit.py` (config/series events with before/after),
`test_logging.py` (JSON formatter fields, text suffix).
Extended: `test_poller.py`, `test_stalled.py`, `test_search_stall.py` (events emitted),
`test_placement.py` (`availability.changed` only on flip; stats counted), `test_store.py`
(`recent()` shape unchanged, `get`), `test_health.py` (async status).

## Rollout & verification

1. Back up `data/relay.db` (`sqlite3 .backup`). Migrations are additive; the previous image
   still runs against the new DB.
2. Deploy with `RECONCILER_ENABLED=false`: check `/healthz`, `/metrics`, `/api/summary`, and
   `curl -N https://<host>/api/events/stream` through the Cloudflare tunnel — `: ping` should
   arrive every ~15s (bursts mean the tunnel buffers SSE; 5B's watchdog covers that).
3. Enable the reconciler; watch `/api/ticks` for 2–3 ticks; confirm `phases` and statuses.
4. After 24h: `SELECT kind, count(*) FROM event GROUP BY kind` — expect hundreds/day, not
   hundreds of thousands; confirm `meta.last_prune_at` set.

## Implementation notes (decisions refined while planning)

The implementation plan (`docs/superpowers/plans/2026-09-14-observability-foundation.md`)
settles these details; where they differ from the sections above, the plan wins:

- `Journal.emit` raises `ValueError` on an unknown kind or level (a programming error that
  tests catch); runtime failures (full buffer, failed flush) never raise.
- The reconciler uses the process-wide `get_journal()` rather than a `journal=` constructor
  argument; only `monitor=` is added.
- DB-backed gauges (`relay_placement_episodes`, `relay_series_intents`) are refreshed on each
  `/metrics` scrape by `store/summary.refresh_gauges`, not registered as callbacks.
- `Monitor` has no injectable sleep; tests drive `step()` directly.
- The FastAPI dependency is `get_event_journal`; `app.state.journal` / `app.state.monitor`
  are reset to `None` on shutdown.
- `tests/conftest.py` is introduced (`db`, `journal` fixtures; autouse journal reset).
- The release-search timeout is already 180s (commit `a43aecb`) and is left unchanged.
