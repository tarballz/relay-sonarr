# Short-TTL zero-release availability verdicts for aired episodes

**Date:** 2026-07-28 (revised 2026-09-14 to match the implementation)
**Component:** `backend/app/services/placement.py` (`refresh_availability`), wired via
`backend/app/reconciler.py` and `backend/app/api/catalog.py` (`GET /series/{tvdb}/plan?refresh=`)
**Status:** Implemented

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

`refresh_availability` caches every interactive-search verdict and reuses it while
`is_fresh(checked_at, now, ttl)` holds, with a single flat `ttl` (6h). It does not distinguish:

- **"0 total releases"** — often a transient artifact: the indexers were down/flapping, or
  the release simply hasn't been posted yet. Cheap to be wrong about; worth re-checking soon.
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

A zero-release verdict for an aired episode is re-checked within ~45 minutes, so a release
that appears after a transient outage is grabbed within the hour — without increasing
indexer load on unaired episodes or stable "rejected-only" verdicts.

Non-goals (YAGNI): per-episode adaptive backoff; short-TTL for non-empty verdicts;
touching RSS/Sonarr behavior.

## Design

Choose the TTL per cached verdict inside `refresh_availability`'s cache-hit branch, instead
of using one flat `ttl`:

```
effective_ttl = empty_ttl  if (cached["total_releases"] == 0 and _has_aired(ep, now))
                else ttl
if cached is not None and is_fresh(cached["checked_at"], now, effective_ttl)
   and cached["min_seeders"] == min_seeders:
       # use cache
```

- **`empty_ttl`** — parameter on `refresh_availability`, default `EMPTY_RELEASE_TTL`
  = **2700s (45 min)**. Applied only to `total_releases == 0` verdicts on aired episodes.
- **`_has_aired(ep, now) -> bool`** — parses `ep["airDateUtc"]` (ISO-8601, already on each
  Sonarr episode). True only if the airdate parses and is `<= now`. **Future airdates →
  False** (unaired episodes legitimately have 0 releases; keep the 6h TTL). **Missing /
  unparseable airdate → False** (conservative: unknown → keep the long TTL).

### Revision: no recency window

The original design limited the short TTL to episodes aired within the last 14 days
(`_recently_aired`, `RECENT_AIR_DAYS = 14`). The implementation deliberately drops the
window: **any** aired episode with a zero-release verdict gets the short TTL. An old gap
that returns zero releases is just as likely to be an outage artifact as a new one (the
Prowlarr 429 flap does not care about air dates), and an old gap that genuinely has no
releases is re-searched at most ~once per 45 minutes, bounded by the existing
concurrency-4 semaphore. The window is a knob to reintroduce if indexer load becomes a
problem — observability work (per-tick availability counts) will show that.

Everything else keeps the existing 6h TTL: "releases exist but none qualify" (total > 0)
and unaired episodes. The `min_seeders` staleness guard is unchanged.

### Config

`emptyReleaseTtlMinutes` (default 45) is read from `settings_store.get_defaults` and passed as
`empty_ttl = minutes * 60` into `refresh_availability` by **both** callers: the reconciler
tick and the manual `GET /series/{tvdb}/plan?refresh=` path, so the knob means the same thing
everywhere.

### No schema change

`total_releases` is already a column on `availability_cache`; `airDateUtc` comes from the
live Sonarr episode dict already in hand inside `refresh_availability`. Existing cache rows
are honored as-is — the shorter TTL simply makes aired zero-release rows expire sooner on the
next read.

## Why correct & safe

The change only *shortens* trust on exactly one verdict class — an aired episode with zero
results, the signature of a transient outage. It never lengthens any TTL, never short-TTLs a
verdict backed by real releases, and never touches unaired episodes.

## Interaction with the reconciler cadence

The reconciler ticks every ~30 min. A 45-min `empty_ttl` means an aired zero-release episode
is re-checked roughly every other tick (~1 hr worst case), versus every 12th tick (6 h)
before.

## Testing

`backend/tests/test_placement.py` (respx-mocked), driving `refresh_availability` with a
pre-seeded `availability_cache` row and a mocked episode list:

1. **aired + 0 releases**, cache age between `empty_ttl` and `ttl` → **stale**: `/release`
   is re-hit and `checked_at` advances.
2. **aired + releases-exist-but-rejected** (`total_releases > 0`), same age → **fresh**.
3. **aired long ago + 0 releases**, same age → **stale** (no recency window).
4. **future airdate + 0 releases** → **fresh**.
5. **aired + 0 releases**, cache age < `empty_ttl` → **fresh**.
6. `_has_aired` unit cases: past True; future False; missing / unparseable False.

`backend/tests/test_reconciler.py`: the tick threads `emptyReleaseTtlMinutes` through.
`backend/tests/test_api.py`: the `?refresh=` path honors `emptyReleaseTtlMinutes`.

## Rollout

No migration, no backfill. On deploy, the next availability read applies the shorter TTL to
any aired zero-release cache row. `emptyReleaseTtlMinutes` defaults in code, so no settings
write is required.
