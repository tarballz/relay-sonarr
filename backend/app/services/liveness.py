"""Is this torrent's swarm alive?

Sonarr's queue is not a usable liveness signal: it reported all 113 wedged items
as ``trackedDownloadStatus: "ok"`` while 52 of them had zero seeders on every
tracker and 41 had never even fetched their metadata. The download client is the
only component that knows, so this module turns its ``torrent-get`` rows into a
verdict the stalled sweep can act on quickly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.obs import kinds
from app.obs.journal import get_journal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TorrentLiveness:
    hash: str
    has_metadata: bool
    # Best seeder count any tracker reported. -1 means *nothing has answered
    # yet* — unknown, not zero. Conflating the two would kill fresh torrents.
    max_seeders: int
    peers_connected: int
    rate_download: int
    percent_done: float


def classify(torrent: dict) -> TorrentLiveness:
    # `or -1` would be wrong here: a genuine 0 is falsy, and 0 is the whole point.
    seeders = [-1 if s.get("seederCount") is None else int(s["seederCount"])
               for s in (torrent.get("trackerStats") or [])]
    return TorrentLiveness(
        hash=(torrent.get("hashString") or "").lower(),
        has_metadata=float(torrent.get("metadataPercentComplete") or 0) >= 1,
        max_seeders=max(seeders) if seeders else -1,
        peers_connected=int(torrent.get("peersConnected") or 0),
        rate_download=int(torrent.get("rateDownload") or 0),
        percent_done=float(torrent.get("percentDone") or 0.0),
    )


def is_dead(live: TorrentLiveness) -> bool:
    """No metadata, or a swarm every tracker agrees is empty — and nothing moving.

    Observed reality always wins: a torrent that has peers or is transferring is
    alive no matter what the trackers claim, because public-tracker counts are
    stale in both directions.
    """
    if live.peers_connected > 0 or live.rate_download > 0:
        return False
    return (not live.has_metadata) or live.max_seeders == 0


async def liveness_map(client) -> dict[str, TorrentLiveness]:
    """``{infohash: TorrentLiveness}``, or ``{}`` if we can't ask.

    Never raises. An unreachable download client degrades the sweep back to its
    age-only behavior rather than failing the tick — the same contract
    ``gather_instances`` gives a downed Sonarr.
    """
    if client is None:
        return {}
    try:
        torrents = await client.torrents()
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.warning("download client unreachable; sweeping on age alone: %s", exc)
        get_journal().emit(
            kinds.DOWNLOAD_CLIENT_ERROR,
            f"Download client unreachable: {exc or type(exc).__name__}",
            level="warn", source="liveness", data={"error": str(exc)},
        )
        return {}
    result = {}
    for row in torrents:
        live = classify(row)
        if live.hash:
            result[live.hash] = live
    return result
