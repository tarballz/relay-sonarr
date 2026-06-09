# Stalled-torrent cleanup — design

## Problem

Torrents grabbed by Sonarr can sit in the download client at 0% indefinitely when a
release is dead (no seeders). Sonarr keeps them in its queue, and Relay's Activity view
surfaces them, but nothing removes them — so a dead grab blocks the episode from ever
downloading. Observed live: **14 torrents at literal 0%**, one stuck since 2026-04-28
(~6 weeks).

Goal: autonomously detect torrents stuck at 0% past a threshold and have Sonarr remove +
blocklist them (which triggers a search for a replacement), logged to the Operations feed.

## Approach

A **stalled-sweep step inside the existing reconciler tick** — not a separate loop. A new
`sweep_stalled(registry, db, ops, *, stalled_days, cap, now)` (in `services/poller.py`, next to
the existing queue polling) is called from `Reconciler.tick()` right after `poll_all`. It
fetches the queue once per instance via the existing `client.queue()` and runs a detection +
action pass. A standalone periodic task was rejected (second scheduler for no benefit); folding
into `tick()` reuses the loop's scheduling, health tracking, and Operation logging.

Everything goes **through Sonarr**, never Transmission directly: Sonarr manages the client, so
its `DELETE /api/v3/queue/{id}` keeps both sides consistent and auto-re-searches. Talking to
Transmission directly would need separate credentials and cause state drift.

## Detection

A queue record is **stalled** when all hold:
- `protocol == "torrent"` (usenet does not stall at 0% the same way),
- `status == "downloading"`,
- `sizeleft == size` (literally 0% — removal can never discard real progress),
- `now − added > stalledDays` (Sonarr's `added` timestamp is on every queue record; no extra
  state tracking needed).

## Action

New `SonarrClient.delete_queue_item(queue_id, *, remove_from_client=True, blocklist=True)`,
mirroring `delete_series` over the existing `_delete` helper. It calls
`DELETE /api/v3/queue/{id}?removeFromClient=true&blocklist=true`. Effect in Sonarr:
- removes the torrent + its data from Transmission,
- blocklists that release so it is not re-grabbed,
- with `skipRedownload` left at its default (false), Sonarr **searches for a replacement**
  automatically.

## Observability

Each removal is recorded to the Operation log (`OperationStore`) with `source="reconciler"`
and a step describing the series/episode + age (e.g. "Removed stalled torrent — The Wire
S01E03, 0% for 42d"). It therefore appears in the Operations feed and counts toward the
reconciler health card's per-tick action total.

## Config & safety

- **`stalledDays`** — default **3**. Stored in reconciler defaults (`store/settings.py`
  `default_policy` JSON) and editable from the Settings page's defaults editor.
- **`STALLED_CLEANUP_ENABLED`** env — default **on** (the user opted into autonomous handling);
  a kill-switch read in `main.py` and passed to the `Reconciler`.
- **Per-tick cap** — default ~25 removals; prevents a large backlog from firing a re-search
  storm at indexers in one tick. When capped, log how many were skipped (no silent truncation);
  the remainder is handled next tick.
- **Torrent-only** scope (above) is itself a safety bound.
- Idempotent: only acts on Sonarr-managed queue items; 0%-only guarantees no data loss; the
  blocklist prevents re-grabbing the same dead release.

## Testing (TDD)

- `tests/test_client.py`: `delete_queue_item` issues the DELETE with `removeFromClient=true` and
  `blocklist=true`.
- Reconciler/poller test (`respx`-mocked): a queue containing (a) an old 0% torrent, (b) a
  recent 0% torrent within the threshold, (c) a healthy partial download, and (d) a 0% usenet
  item → **only (a)** is removed; assert the DELETE call + that an Operation step was recorded.
- A per-tick-cap test: more stalled items than the cap → exactly cap removals, remainder logged.

## Files

| File | Change |
|------|--------|
| `backend/app/sonarr/client.py` | New `delete_queue_item()` |
| `backend/app/reconciler.py` (or `services/poller.py`) | Stalled-sweep step in the tick, using the already-fetched queue; cap + logging; writes Operations |
| `backend/app/store/settings.py` | `stalledDays` default (read via existing defaults) |
| `backend/app/main.py` | `STALLED_CLEANUP_ENABLED` env → `Reconciler` |
| `frontend/src/pages/Settings.jsx` | `stalledDays` input in the defaults editor |
| `backend/tests/test_client.py`, `tests/test_reconciler.py` | Tests above |

## Out of scope

- Partial-progress / generic "no-progress for N days" detection (rejected in favor of strict 0%).
- Direct Transmission RPC integration.
- Manual per-row "remove" button in Activity (autonomous-only for now; could be added later).
