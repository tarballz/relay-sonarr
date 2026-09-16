"""One-off: remove the dead torrents already wedged in Sonarr's queue.

Relay's sweep handles this going forward, but it is deliberately capped per tick,
so a backlog that built up before the fix would take days to clear. This drains
it in one supervised pass.

Reuses Relay's own definitions (config loading, the Transmission client, the
liveness classifier, SonarrClient) so "dead" means exactly what the reconciler
means by it -- there is no second copy of the rule to drift.

Run it from the backend so ``app`` is importable:

    cd backend && CONFIG_PATH=../config.yaml uv run python ../scripts/drain_dead.py
    cd backend && CONFIG_PATH=../config.yaml uv run python ../scripts/drain_dead.py --apply

Dry run is the default and prints exactly what it would remove.

Two hard-won constraints are baked in:

* **Batches with pauses.** Removing ~190 torrents at once once wedged the ZFS
  pool's txg sync for 45 minutes, froze Transmission into an unkillable D-state
  and needed a NAS reboot. Ten at a time, then breathe.
* **Abort when a DELETE gets slow.** A single call crossing ``--slow-abort``
  seconds is the observable signature of that same backlog starting. Stopping at
  torrent 23 instead of 190 is the entire point of the guard.

No replacement searches happen here: ~90 back-to-back interactive searches would
hammer the indexers for hours. The reconciler refills, seeder-aware and paced, on
its next ticks.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import httpx  # noqa: E402

from app.config import load_config  # noqa: E402
from app.services.liveness import is_dead, liveness_map  # noqa: E402
from app.sonarr.registry import Registry  # noqa: E402


def _age_hours(added: str | None, now: datetime) -> float:
    if not added:
        return 0.0
    try:
        when = datetime.fromisoformat(str(added).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (now - when).total_seconds() / 3600.0


async def collect(registry, *, min_age_hours: float, now: datetime):
    """Every queue group whose torrent the download client calls dead."""
    swarms = await liveness_map(registry.downloads())
    if not swarms:
        raise SystemExit("Download client returned nothing — refusing to guess. Check config.yaml.")

    doomed = []
    for inst in registry.all():
        queue = await inst.client.queue()
        groups: dict[str, list[dict]] = {}
        for rec in queue.get("records", []):
            key = (rec.get("downloadId") or "").strip().lower()
            if key:
                groups.setdefault(key, []).append(rec)
        for key, recs in groups.items():
            swarm = swarms.get(key)
            if swarm is None or not is_dead(swarm):
                continue
            age = _age_hours(recs[0].get("added"), now)
            if age < min_age_hours:
                continue
            doomed.append({
                "instance": inst, "queue_id": recs[0]["id"], "records": len(recs),
                "title": recs[0].get("title", "?"), "age": age,
                "indexer": (recs[0].get("indexer") or "?").split(" (")[0],
                "seeders": swarm.max_seeders, "metadata": swarm.has_metadata,
            })
    # Oldest first: the most clearly dead go first, so an abort leaves the
    # ambiguous ones untouched.
    doomed.sort(key=lambda d: -d["age"])
    return doomed


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually remove (default: dry run)")
    ap.add_argument("--batch", type=int, default=10)
    ap.add_argument("--pause", type=float, default=30.0, help="seconds between batches")
    ap.add_argument("--slow-abort", type=float, default=20.0,
                    help="abort if any single DELETE takes longer than this")
    ap.add_argument("--min-age-hours", type=float, default=6.0)
    ap.add_argument("--max", type=int, default=None, help="stop after this many removals")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    registry = Registry(load_config(os.environ.get("CONFIG_PATH", "../config.yaml")))
    try:
        doomed = await collect(registry, min_age_hours=args.min_age_hours, now=now)
        if args.max:
            doomed = doomed[: args.max]

        if not doomed:
            print("Nothing dead in the queue. Nothing to do.")
            return 0

        print(f"{'#':>3}  {'inst':6} {'indexer':18} {'age':>6} {'seed':>5} {'meta':>4} "
              f"{'recs':>4}  title")
        by_indexer: dict[str, int] = {}
        for n, d in enumerate(doomed, 1):
            by_indexer[d["indexer"]] = by_indexer.get(d["indexer"], 0) + 1
            print(f"{n:3d}  {d['instance'].id:6} {d['indexer'][:18]:18} {d['age']:5.0f}h "
                  f"{d['seeders']:5d} {'yes' if d['metadata'] else 'NO':>4} "
                  f"{d['records']:4d}  {d['title'][:62]}")
        print("\nby indexer: " + ", ".join(f"{k}={v}" for k, v in
                                           sorted(by_indexer.items(), key=lambda kv: -kv[1])))

        if not args.apply:
            print(f"\nDRY RUN — {len(doomed)} torrent(s) would be removed and blocklisted.")
            print("Re-run with --apply to act.")
            return 0

        if not args.yes:
            if input(f"\nRemove {len(doomed)} torrents? Type DRAIN to continue: ") != "DRAIN":
                print("Aborted.")
                return 1

        removed = 0
        for start in range(0, len(doomed), args.batch):
            batch = doomed[start:start + args.batch]
            for d in batch:
                began = time.perf_counter()
                try:
                    # skip_redownload: the docstring promises no searches here, and
                    # 52 simultaneous re-searches would earn a Prowlarr 429. The
                    # reconciler refills these, seeder-aware and paced, next tick.
                    await d["instance"].client.delete_queue_item(
                        d["queue_id"], skip_redownload=True)
                except httpx.HTTPStatusError as exc:
                    # A season-pack sibling vanishes with the shared torrent.
                    if exc.response.status_code != 404:
                        raise
                elapsed = time.perf_counter() - began
                removed += 1
                if elapsed > args.slow_abort:
                    print(f"\nABORT: a DELETE took {elapsed:.0f}s (limit {args.slow_abort:.0f}s).")
                    print("That is how the pool-wedge incident started. "
                          f"Stopped after {removed}; re-run later to continue.")
                    return 2
            print(f"  removed {removed}/{len(doomed)}")
            if start + args.batch < len(doomed):
                await asyncio.sleep(args.pause)

        print(f"\nDone — {removed} removed and blocklisted.")
        return 0
    finally:
        await registry.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
