# Search-stall reaper — recover episodes wedged in `searching`

**Date:** 2026-07-19
**Component:** `backend/app` (reconciler + poller)
**Status:** Approved design, pending implementation

## Problem

Monitored episodes silently stop being downloaded and never recover, even after the
condition that blocked them clears. Observed live: `The Man Will Burn` S01E02 aired
2026-07-16 but was never grabbed; **57 episodes** library-wide are stuck the same way,
some since 2026-06-05.

### Root cause (confirmed end-to-end)

The reconciler optimistically marks an episode `searching` the moment it fires a search,
*before* any download exists:

- `reconciler.py:_mark_searching()` sets `state="searching"`, `last_search_at=now` right
  after issuing the search/spill — the only writer of `searching`.
- `poller.py:poll_instance()` only advances episodes that appear in the Sonarr **queue or
  history** (`for key in set(queue_state) | set(latest_event)`). A search that finds
  nothing produces **neither** a queue item nor a history event, so the poller never
  visits that episode.
- `placement.py:compute_plan()` preserves the state for anything in
  `IN_PROGRESS = {"searching","grabbed","importing","failed"}` ("defer to the poller",
  `placement.py:270`).
- The reconciler only acts on `ACTIONABLE = {"wanted","unavailable","failed"}`
  (`reconciler.py:31`). `searching` is excluded.

Net effect: an episode whose search quietly returns nothing (indexer outage, or no
release seeded yet) is wedged in `searching` **forever**. Nothing in the codebase reverts
a stale `searching` back to an actionable state (verified by grep: the only `searching`
writer is `_mark_searching`).

This is a classic optimistic-transition-without-a-reaper bug: work is marked in-progress
before there is proof it started, and there is no timeout for the "it never started" case.

### What this is NOT

Not a Prowlarr or Sonarr defect. Prowlarr correctly proxied the searches and reported
`429 TooManyRequests` when the indexer sites rate-limited it; the outage was external and
transient. The durable defect is entirely in Relay's reconciler state machine — the layer
whose job is to keep driving each episode to "obtained".

## Goal

Any transient search failure self-heals: once indexers recover (or a release finally
seeds), a previously-wedged episode gets re-searched automatically, with bounded retry
frequency and no search storms.

Non-goals (YAGNI): indexer hardening / FlareSolverr tuning / pruning dead indexers
(explicitly out of scope for this change); webhooks; changing how Sonarr itself retries.

## Design

Add a **search-stall reaper**: an isolated sweep that reverts genuinely-stuck `searching`
episodes back to `wanted`, so the existing reconcile pass re-searches them.

### Reap predicate

Revert a `placement` row to `state="wanted"` when **all** hold:

1. `state == "searching"`
2. `download_id IS NULL` or empty — a real grab sets `download_id` (poller/`_queue_state`),
   so this isolates "searched but nothing came back" from "actively downloading".
3. `last_search_at` is older than `stall_hours` (default **6h**, matching
   `placement.DEFAULT_TTL` availability cache).

Revert target is `wanted` (not `unavailable`): `wanted` forces a fresh availability check
+ search on the next reconcile pass, rather than implying "checked, nothing qualified".

`wanted_since` is left untouched (compute_plan already preserves it for `wanted`), so
fallback/escalation timing is unaffected.

### Placement and timing

- New function `sweep_search_stalls(db, *, stall_hours, cap, now) -> list[transitions]`
  in `poller.py`, alongside `sweep_stalled` / `sweep_dangerous`. Pure DB state — **no
  Sonarr HTTP calls**.
- Called from `Reconciler.tick()` immediately **after** `poller.poll_all(...)` and
  **before** `_ensure_intents()` / the reconcile loop. Ordering matters: a row reverted to
  `wanted` in this sweep is seen as `wanted` (∉ `IN_PROGRESS`) by `compute_plan` in the
  same tick and searched in the same tick.
- Wrapped in its own `try/except` that logs and swallows, exactly like the other sweeps —
  a reaper failure must never stop the tick.
- Guarded by a `search_stall_cleanup: bool = True` constructor flag on `Reconciler`,
  toggled by `SEARCH_STALL_CLEANUP_ENABLED` in `main.py` (mirrors
  `STALLED_CLEANUP_ENABLED` / `DANGEROUS_CLEANUP_ENABLED`).

### Bounding (no thrash, no storm)

- **Per-tick cap** (default 25, reusing the `stalled_cap` sizing): the initial 57-row
  backlog drains over ~3 ticks instead of one burst. When the cap is hit, log how many
  were deferred (no silent truncation) — mirrors `sweep_stalled`.
- **Self-healing, bounded retry:** re-search re-stamps `last_search_at`; if indexers are
  still down the episode simply re-wedges and waits another `stall_hours`. At most one
  retry per episode per window. `refresh_availability`'s existing semaphore (concurrency 4)
  further bounds indexer load.

### Config

Add `searchStallHours` (default 6) to settings defaults, mirroring `stalledDays`. The
reconciler reads it from `settings_store.get_defaults(db)` inside `tick()` and passes it as
`stall_hours` (consistent with how `stalledDays` is threaded to `sweep_stalled`).

## Data flow (one tick, after the fix)

```
tick()
  poll_all()                      # queue/history → grabbed/importing/imported/failed
  sweep_stalled()                 # dead torrents in queue
  sweep_dangerous()               # executable fakes in queue
  sweep_search_stalls()  ← NEW    # searching + no download_id + stale → wanted
  _ensure_intents()
  for intent: reconcile_series()  # 'wanted' rows (incl. just-reverted) get searched
```

## Edge cases

- **Grab in flight, magnet still resolving:** briefly `searching` with no `download_id`.
  The 6h threshold is far longer than metadata resolution (seconds–minutes); the poller
  flips it to `grabbed` (with `download_id`) well before the reaper would consider it.
- **Grabbed then download removed:** the poller records `failed` (with backoff) from
  history — never left `searching` — so the reaper does not touch it.
- **Series/episode removed:** an orphaned `searching` row could be reverted to `wanted`
  harmlessly; the next `compute_plan` (which only emits rows for episodes present on an
  instance) supersedes it. No Sonarr call is made on the orphan.
- **Indexers still down at reap time:** re-search finds nothing, re-wedges, retries next
  window. Correct and bounded.

## Testing (TDD — failing tests first)

New `backend/tests/test_search_stall.py`, following the injected-clock / respx patterns in
`test_stalled.py` and `test_poller.py`:

1. `searching` + no `download_id` + `last_search_at` older than `stall_hours` → reverted to
   `wanted`.
2. `searching` + non-null `download_id` → left unchanged (real in-flight download).
3. `searching` + `last_search_at` within `stall_hours` → left unchanged.
4. `grabbed` / `importing` / `failed` rows → never touched.
5. Cap respected: with N > cap stuck rows, exactly `cap` reverted per call; remainder
   deferred and logged.
6. Integration: after a reap reverts a row to `wanted`, the same `tick()` issues a search
   for it (reuse the reconcile test harness in `test_reconciler.py`).

## Rollout

On first deploy the reaper recovers the existing backlog (~57 episodes, incl. this show's
S01E03/S01E04 before they air), draining under the per-tick cap. No migration: uses
existing `placement` columns (`state`, `download_id`, `last_search_at`, `wanted_since`).
`searchStallHours` defaults in code, so no settings backfill is required.
