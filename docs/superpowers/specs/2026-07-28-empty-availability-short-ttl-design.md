# Short-TTL zero-release availability verdicts for just-aired episodes

**Date:** 2026-07-28
**Component:** `backend/app/services/placement.py` (`refresh_availability`), wired via `backend/app/reconciler.py`
**Status:** Approved design, pending implementation

## Problem

A just-aired episode can sit un-grabbed for up to 6 hours after a release is actually
available. Observed live: `The Man Will Burn` S01E03 aired 2026-07-23; the reconciler's
availability check at 03:00:47 UTC landed in a brief indexer outage and cached
"0 releases → unavailable". Minutes later the indexers recovered and a qualifying release
(GRACE WEBDL-1080p, 250 seeders) was present — but the cached verdict carries a **6-hour
TTL** (`placement.DEFAULT_TTL`), so the reconciler would not re-check until ~09:00. The
episode was grabbable the whole time; the automation was sitting on a 7-minute-stale
"nothing here" verdict.

### Root cause

`refresh_availability` (`placement.py:107-124`) caches every interactive-search verdict and
reuses it while `is_fresh(checked_at, now, ttl)` holds, with a single flat `ttl` (6h). It
does not distinguish:

- **"0 total releases"** — often a transient artifact: the indexers were down/flapping, or
  the episode is too fresh. Cheap to be wrong about; worth re-checking soon.
- **"releases exist but none qualify"** (total > 0, e.g. wrong quality/seeders) — a real,
  stable verdict. Re-checking soon just re-hammers indexers for the same "no" answer.

Both get the same 6h TTL, so a spurious zero-result from an outage window suppresses
re-checks for 6 hours.

This is distinct from the search-stall reaper (which recovers episodes wedged in
`searching`). Here the episode is correctly `unavailable`; the defect is that the
`unavailable` verdict is trusted too long when it was likely an outage artifact.

### What this is NOT

Not an indexer/Prowlarr fix. The indexers recover on their own; this change makes Relay
*notice* the recovery quickly instead of waiting out a stale cache. Reducing the frequency
of the 429 flap itself is explicitly out of scope (deferred "option B").

## Goal

A zero-release verdict for a recently-aired episode is re-checked within ~45 minutes, so a
release that appears after a transient outage is grabbed within the hour — without
increasing indexer load on permanent gaps, unaired episodes, or stable "rejected-only"
verdicts.

Non-goals (YAGNI): per-episode adaptive backoff; short-TTL for non-empty verdicts;
short-TTL for gaps that are not recently aired; touching RSS/Sonarr behavior.

## Design

Choose the TTL per cached verdict inside `refresh_availability`'s cache-hit branch, instead
of using one flat `ttl`:

```
effective_ttl = empty_ttl  if (cached["total_releases"] == 0 and _recently_aired(ep, now))
                else ttl
if cached is not None and is_fresh(cached["checked_at"], now, effective_ttl)
   and cached["min_seeders"] == min_seeders:
       # use cache
```

- **`empty_ttl`** — new parameter on `refresh_availability`, default **2700s (45 min)**.
  Applied only to `total_releases == 0` verdicts on recently-aired episodes.
- **`_recently_aired(ep, now, days=RECENT_AIR_DAYS) -> bool`** — parses `ep["airDateUtc"]`
  (ISO-8601, the field Sonarr already returns on each episode). Returns True only if the
  airdate parses and falls within `[now - days, now]`. **Future airdates → False**
  (unaired episodes legitimately have 0 releases; keep the 6h TTL). **Missing / unparseable
  airdate → False** (conservative: unknown → keep the long TTL).
- **`RECENT_AIR_DAYS = 14`** — module constant in `placement.py`.

Everything else keeps the existing 6h TTL: "releases exist but none qualify" (total > 0),
gaps older than 14 days, and unaired episodes. The `min_seeders` staleness guard is
unchanged.

### Config

The reconciler reads `emptyReleaseTtlMinutes` (default 45) from `settings_store.get_defaults`
and passes `empty_ttl = minutes * 60` into `refresh_availability`, mirroring how
`stalledDays` / `searchStallHours` are threaded. `RECENT_AIR_DAYS` stays a code constant
(one knob is enough — YAGNI). No settings-schema change (defaults are read inline via
`.get(...)`).

### No schema change

`total_releases` is already a column on `availability_cache`; `airDateUtc` comes from the
live Sonarr episode dict already in hand inside `refresh_availability`. Existing cache rows
are honored as-is — the shorter TTL simply makes recently-aired zero-release rows expire
sooner on the next read.

## Why correct & safe

The change only *shortens* trust on exactly one verdict class — a fresh episode with zero
results, the signature of a transient outage. It never lengthens any TTL, never short-TTLs a
verdict backed by real releases, and never touches permanent/future gaps. Worst case if the
release genuinely isn't out yet: a few extra interactive searches (bounded by the existing
concurrency-4 semaphore) on that one episode until it appears or ages past 14 days.

## Interaction with the reconciler cadence

The reconciler ticks every ~30 min. A 45-min `empty_ttl` means a recently-aired zero-release
episode is re-checked roughly every other tick (~1 hr worst case), versus every 12th tick
(6 h) today. Fast enough to close the observed lag, gentle enough not to search the same
episode every single tick.

## Testing (TDD — failing tests first)

New tests in `backend/tests/` (respx-mocked, `asyncio_mode=auto`), driving
`refresh_availability` with a pre-seeded `availability_cache` row and a mocked episode list:

1. **recently-aired + 0 releases**, cache age between `empty_ttl` and `ttl` → verdict treated
   as **stale**: `refresh_availability` re-hits `/release` (assert the release route is
   called, and the cache row's `checked_at` advances).
2. **recently-aired + releases-exist-but-rejected** (`total_releases > 0`), same cache age →
   **fresh**: no re-hit (assert the release route is NOT called; cached verdict returned).
3. **old-aired (airdate > 14 d ago) + 0 releases**, same cache age → **fresh** (not hammered).
4. **future airdate + 0 releases** → **fresh** (not hammered).
5. **recently-aired + 0 releases**, cache age < `empty_ttl` → **fresh** (not re-checked yet).
6. `_recently_aired` unit cases: within window True; future False; > 14 d False; missing /
   unparseable `airDateUtc` False.

## Rollout

No migration, no restart-time backfill. On deploy, the next reconciler tick applies the
shorter TTL to any recently-aired zero-release cache row on its next read. `emptyReleaseTtlMinutes`
defaults in code, so no settings write is required.
