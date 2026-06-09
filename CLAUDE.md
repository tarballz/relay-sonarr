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

The orchestration coroutines take an optional async `emit(event)`. The `/smart-add/stream` and
`/advance-fallback/stream` GET endpoints wrap them in `_event_stream` (`api/adding.py`), pushing
each step as an SSE frame *and* appending it to an in-memory `OperationLog` (`operations.py`,
bounded deque, lost on restart) that the Operations page replays via `GET /api/operations`.
The non-streaming `POST` variants share the same coroutines with the default no-op emit.

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
`app.dependency_overrides[get_registry] = make_registry` (see `tests/test_api.py`). When
exercising orchestration timing, pass a fake `sleep` and small `wait_attempts` to `smart_add` /
`advance_fallback` instead of real delays.
