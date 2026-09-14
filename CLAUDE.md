# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Relay** — one web app over multiple Sonarr v3 instances (here: 1080p `:8989` + 4K `:8990`).
Unified search/add, combined queue & library, and a **smart-add fallback chain**. Backend
(FastAPI) serves the API *and* the built React SPA on a single origin; ships as one Docker container.

## Commands

```bash
# Backend dev (terminal 1) — point CONFIG_PATH at the repo-root config
cd backend && CONFIG_PATH=../config.yaml uv run uvicorn app.main:app --reload

# Frontend dev (terminal 2) — Vite proxies /api -> :8000
cd frontend && npm install && npm run dev

# Tests (respx-mocked, no live Sonarr needed)
cd backend && uv run pytest
cd backend && uv run pytest tests/test_chain.py::test_smart_add_emits_fallback_event_when_no_release   # single test

# Full prod build/run (multi-stage: builds SPA, serves it from app/static)
docker compose up -d --build
```

There is no linter configured. `pytest` uses `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed).

## Configuration model

- **`config.yaml`** (gitignored; copy from `config.example.yaml`) defines `instances` and
  `fallback_chains`. **`.env`** holds the API keys. Keys are never in the YAML — they're
  `${ENV_VAR}` placeholders that `config.py:_expand()` substitutes at load time.
- `CONFIG_PATH` selects the file (defaults to `/config/config.yaml` in the container, where
  compose bind-mounts it read-only). Tests bypass all of this via dependency override (below).
- Adding a third instance is a **config edit, not a code change** — everything downstream
  iterates `registry.all()` rather than naming instances.

## Architecture

Request flow: `api/*` routers → `services/*` (orchestration) → `sonarr/client.py` (one HTTP call).

- **Registry (`sonarr/registry.py`)** — the spine. Built once at startup in `main.py:lifespan`,
  attached to `app.state.registry`, and handed to routers via the `get_registry` FastAPI
  dependency. Holds one `SonarrClient` per instance (keyed by id) plus the fallback chains.
  Routers never touch config directly.
- **Fan-out (`services/fanout.py`)** — every cross-instance aggregation goes through
  `gather_instances()`: runs one coroutine per instance concurrently with
  `return_exceptions=True`, returning `(instance, result_or_exception)` pairs. Callers skip
  the failed ones (`isinstance(res, Exception)`), so **one Sonarr being down degrades the view
  instead of failing the request**. `library.py` and `health.py` both rely on this.
- **`SonarrClient`** — thin async httpx wrapper over one instance's `/api/v3`. Opens a fresh
  `AsyncClient` per call; auth is the per-instance `X-Api-Key` header.
- **SPA serving (`main.py`)** — API routers + `/healthz` mount first; `SpaStaticFiles` is
  mounted at `/` last and falls back to `index.html` on 404 (client-side routing).

### Smart-add fallback chain (the core feature)

`services/add.py`. "Added" ≠ "will download" — a series can be added to the 4K tier with no 4K
release existing. So `smart_add` adds → `RefreshSeries` → polls episodes → runs
`check_availability` (an *interactive release search* on a sample episode; a non-`rejected`
release means available).

- **Available** → fire `SeriesSearch`, return `status="added"`.
- **Not available, chain configured** → return `status="fallback_suggested"` with an `advance`
  block. **State lives in that payload, not the server** — the UI echoes it back to
  `advance_fallback` to walk each step. The chain is keyed by the *starting* instance id.
- `advance_fallback` per step: a **different** instance → move (add there + search, then delete
  the abandoned series — add-before-delete so a failed add never loses data); the **same**
  instance → swap the quality profile and re-search. Then re-check availability →
  `placed` / next `fallback_suggested` / `exhausted`.

Each `(instance, profile)` step resolves profile/root-folder **by name** at runtime
(`resolve_step`); a missing named profile (e.g. the `SD` step needs an `SD` profile on the
1080p box) raises a clear `ValueError` surfaced as HTTP 400.

### Live progress (SSE) + operation log

The orchestration coroutines take an optional async `emit(event)`. The `*/stream` GET endpoints
wrap them in `_event_stream` (`api/adding.py`), pushing each step as an SSE frame *and* persisting
it via `OperationStore` (`store/operations.py`, SQLite `operation` + `operation_step` tables) that
the Operations page reads via `GET /api/operations`. The non-streaming `POST` variants share the
same coroutines with the default no-op emit.

### Autonomous reconciler

The user declares desired state per series; `reconciler.py` drives reality toward it on a
background loop (`run()` → guarded `_run_once()` → `tick()`, default every 30 min).

- **Persistence** — `db.py` (stdlib `sqlite3`, one lock-serialized connection, WAL) with
  repositories in `store/*`. Migrations are additive: `CREATE TABLE IF NOT EXISTS`,
  `_ensure_column`, and `_run_once` for one-time data repairs keyed in `meta`.
- **Tick** — `poller.poll_all` (queue + history → placement state transitions, failure backoff),
  then the sweeps (`sweep_stalled`, `sweep_dangerous`, `sweep_search_stalls`), then
  `reconcile_series` per non-paused `series_intent`.
- **Placement** (`services/placement.py`) — `refresh_availability` runs interactive release
  searches on gap episodes (TTL-cached in `availability_cache`; zero-release verdicts on aired
  episodes use the short `emptyReleaseTtlMinutes`); `compute_plan` joins episodes across
  instances into per-episode `placement` rows (`wanted/unavailable/searching/grabbed/importing/
  failed/imported/unmonitored`).
- **Identity** — cross-instance episode identity is always `(tvdb_id, season, episode)`; Sonarr
  `seriesId`/episode ids are per-instance and must be mapped to tvdb before persisting.
- **Policy & settings** — `policy.py` (per-series `SeriesPolicy`), runtime defaults in
  `meta.default_policy` via `store/settings.py`. Env kill switches: `RECONCILER_ENABLED`,
  `STALLED_CLEANUP_ENABLED`, `DANGEROUS_CLEANUP_ENABLED`, `SEARCH_STALL_CLEANUP_ENABLED`.
- **Health** — `GET /api/reconciler/status`; `/healthz` returns 503 when the loop is stale.

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

## Frontend

React + Vite + TanStack Query + react-router. `src/api.js` is the single same-origin `/api`
wrapper; `openStream` drives the SSE endpoints (closes on first terminal event to defeat
EventSource auto-reconnect). `tierClass()` maps instance id → color (teal=1080p, amber=4k) by
substring match. Pages in `src/pages/`, dialogs/shared in `src/components/`.

## Auth

`auth.py` — optional Cloudflare Access JWT verification, wired as a global FastAPI dependency in
`main.py`. **No-op unless `CF_ACCESS_ENABLED=true`**; when enabled it validates the
`Cf-Access-Jwt-Assertion` header against the team JWKS. Defense-in-depth behind the tunnel — see
`DEPLOY.md`. Local/dev runs need no auth setup.

## Test pattern

Tests mock Sonarr HTTP with `respx` and inject a fixture registry by overriding the dependency:
`app.dependency_overrides[get_registry] = make_registry` (see `tests/test_api.py`; also `get_db`,
`get_reconciler`). `tests/conftest.py` provides `db` (tmp SQLite) and `journal` (a real journal
installed process-wide) fixtures and resets the global journal after every test; Sonarr helpers
(`make_registry`, base URLs `A`=1080p / `B`=4k, `_nosleep`) live in `tests/test_chain.py`.
Persistence tests use a real `Database(str(tmp_path / "relay.db"))`. The reconciler is
driven deterministically with
`Reconciler(..., clock=lambda: NOW, sleep=_nosleep)` and direct `tick()` / `reconcile_series()`
calls. When exercising orchestration timing, pass a fake `sleep` and small `wait_attempts`
instead of real delays.

Frontend builds need no host Node: `docker run --rm -v "$PWD/frontend":/fe -w /fe node:20-alpine
sh -c "npm install && npm run build"`.
