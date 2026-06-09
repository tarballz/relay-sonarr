"""Per-series resolution status, derived purely from the DB (no Sonarr calls).

Powers the Library status badges: a cheap roll-up of each series' placement rows
into one headline status plus per-state counts.
"""
from __future__ import annotations

from app.store import intents as intent_store
from app.store import placements as place_store


def derive_status(counts: dict, obtained_tiers: set) -> str:
    """One headline status from per-state counts (precedence: action in flight >
    needs attention > settled)."""
    if counts.get("searching") or counts.get("grabbed") or counts.get("importing"):
        return "reconciling"
    if counts.get("failed"):
        return "retrying"
    if counts.get("unavailable"):
        return "stuck"
    if counts.get("wanted"):
        return "waiting"
    real_tiers = {t for t in obtained_tiers if t}
    if len(real_tiers) > 1:
        return "split"
    return "on-target"


async def library_status(db) -> list[dict]:
    """Status summary for every series Relay has placement data for."""
    paused_by = {i["tvdb_id"]: bool(i["paused"]) for i in await intent_store.all_intents(db)}

    grouped: dict[int, dict] = {}
    for row in await place_store.all_rows(db):
        g = grouped.setdefault(row["tvdb_id"], {"counts": {}, "tiers": set()})
        g["counts"][row["state"]] = g["counts"].get(row["state"], 0) + 1
        if row["state"] == "imported" and row["obtained_tier"]:
            g["tiers"].add(row["obtained_tier"])

    out = []
    for tvdb_id, g in grouped.items():
        out.append({
            "tvdbId": tvdb_id,
            "status": derive_status(g["counts"], g["tiers"]),
            "counts": g["counts"],
            "paused": paused_by.get(tvdb_id, False),
        })
    out.sort(key=lambda s: s["tvdbId"])
    return out
