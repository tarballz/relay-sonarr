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
- **`download_client`** (optional) points at Transmission's RPC for per-torrent liveness.
  Omit it and every liveness path no-ops back to age-only behavior — never crashes.

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
background loop (`run()` → guarded `_run_once()` → `tick()`; `RECONCILER_INTERVAL_S`,
default 30 min).

- **Persistence** — `db.py` (stdlib `sqlite3`, one lock-serialized connection, WAL) with
  repositories in `store/*`. Migrations are additive: `CREATE TABLE IF NOT EXISTS`,
  `_ensure_column`, and `_run_once` for one-time data repairs keyed in `meta`.
- **Tick** — `poller.poll_all` (queue + history → placement state transitions, failure backoff),
  then `liveness.liveness_map` (one Transmission call), then the sweeps (`sweep_stalled`,
  `sweep_dangerous`, `sweep_search_stalls`), then `reconcile_series` per non-paused
  `series_intent`. Ticks are lock-serialized, so one running longer than
  `RECONCILER_INTERVAL_S` just means the loop never idles.
- **Liveness** (`app/download/` + `services/liveness.py`) — Sonarr reports a torrent with no
  metadata and no seeders as `trackedDownloadStatus: "ok"`, so only the client knows. `is_dead`
  = (no metadata **or** 0 seeders on every tracker) **and** nothing moving; `seederCount: -1`
  means *no tracker has answered yet* — unknown, never dead. `sweep_stalled` reaps dead
  torrents in `deadHours` instead of `stalledDays`, never touches one moving bytes, spares
  downloads past `nearCompletePct`, and re-grabs a replacement itself
  (`grab.regrab_episode`, `skipRedownload=true`) because Sonarr's own re-search ranks by
  quality and re-picks another dead release.
- **Placement** (`services/placement.py`) — `refresh_availability` runs interactive release
  searches on gap episodes (TTL-cached in `availability_cache`; zero-release verdicts on aired
  episodes use the short `emptyReleaseTtlMinutes`). Two rules exist because each was learned
  the expensive way:
  **(1) cache reuse is monotonic, not exact** (`_verdict_survives_floor_change`) — a stricter
  seeder floor can only turn a "yes" into a "no" and a looser one only a "no" into a "yes", so
  most threshold changes reuse the cached verdict. Invalidating on any change turned the
  default moving 3→5 into 1540 re-searches (~7.5h of indexer traffic in one tick).
  **(2) an empty result is only trusted when there was something to ask** —
  `health.indexers_degraded` compares the indexers Sonarr reports as failing against the
  interactive-search-enabled count, and only gates when *all* of them are down. It must stay
  that strict: an earlier version gated on *any* failure, which — since public-tracker indexers
  cycle in and out of failure constantly — fired on every tick, threw away ~9k searches a day
  and froze the cache for 36h. A gated verdict is still **cached**, flagged `degraded` with a
  short `degradedTtlMinutes`; discarding it instead removes the only thing that ever repairs a
  stale row, so the episode is re-searched forever and never learns.
  `compute_plan` then joins episodes across instances into per-episode `placement` rows (`wanted/unavailable/searching/grabbed/importing/
  failed/imported/unmonitored`).
- **Identity** — cross-instance episode identity is always `(tvdb_id, season, episode)`; Sonarr
  `seriesId`/episode ids are per-instance and must be mapped to tvdb before persisting.
- **Policy & settings** — `policy.py` (per-series `SeriesPolicy`), runtime defaults in
  `meta.default_policy` via `store/settings.py` — `minSeeders` (5), `stalledDays` (1),
  `deadHours` (6), `regrabCap` (5), `nearCompletePct` (95), `seederRelaxAfterDays` (3),
  `emptyReleaseTtlMinutes`, `degradedTtlMinutes` (60). The seeder floor is **per-episode and expires**
  (`availability.effective_min_seeders`): thin-swarm back-catalogue would otherwise be stranded
  forever by a hard floor. Defaults live as module constants next to the code that reads them;
  **`Settings.jsx` sends `{...data, ...form}` with its own `??` fallbacks, so any default change
  must ship with the frontend or the first "Save defaults" click freezes the old value.**
  Env knobs: `RECONCILER_ENABLED`, `RECONCILER_INTERVAL_S`, `STALLED_CLEANUP_ENABLED`,
  `DANGEROUS_CLEANUP_ENABLED`, `SEARCH_STALL_CLEANUP_ENABLED`.
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

Transmission's 409 session-id handshake is mocked with an ordered `side_effect`:
`respx.post(RPC).mock(side_effect=[httpx.Response(409, headers={"X-Transmission-Session-Id":
"s"}), ok_response])` (see `tests/test_transmission.py`).

Frontend builds need no host Node: `docker run --rm -v "$PWD/frontend":/fe -w /fe node:20-alpine
sh -c "npm install && npm run build"`.

## Gotchas

- **A "silent" tick is almost always slow searches, not a hang.** httpx logs a request only once
  its response lands, and an interactive search takes 55-180s
  (`SonarrClient.RELEASE_SEARCH_TIMEOUT`). Blocked coroutines burn no CPU, and a socket awaiting
  a response shows `Send-Q`/`Recv-Q` of 0 — so idle CPU + quiet logs + "empty" sockets all look
  like a deadlock and are not. `py-spy` cannot show asyncio tasks; reproduce locally and dump
  with `asyncio.wait_for(asyncio.shield(task), n)` then `task.print_stack()`.
- `refresh_availability` accepts a `limit` the reconciler never passes, so a tick's search volume
  is unbounded — worth remembering before anything that invalidates cache in bulk.
- The container owns `data/relay.db` as root; work on a copy via
  `docker cp sonarr-unified:/data/relay.db <tmp>`.
- Updating a *failing* Sonarr indexer needs `PUT /api/v3/indexer/{id}?forceSave=true` — the
  add-time validation rejects it otherwise.

