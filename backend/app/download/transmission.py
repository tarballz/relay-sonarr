"""Read-only Transmission RPC client.

Relay never removes a torrent here — removal stays on the single existing path,
Sonarr's ``delete_queue_item`` (which blocklists the release *and* removes it
from the client). This client exists purely to answer "is this swarm alive?",
which Sonarr's queue cannot: it reports a torrent with no metadata and no
seeders as ``trackedDownloadStatus: "ok"``.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

SESSION_HEADER = "X-Transmission-Session-Id"

# Everything the liveness classifier needs, and nothing else — torrent-get over
# ~120 torrents is cheap, but only if we don't ask for the file list.
TORRENT_FIELDS = (
    "hashString", "name", "status", "percentDone", "isStalled",
    "metadataPercentComplete", "peersConnected", "rateDownload",
    "trackerStats", "addedDate", "leftUntilDone", "totalSize",
)


class TransmissionClient:
    """One Transmission instance's RPC endpoint.

    ``base_url`` is the full RPC path, e.g.
    ``http://host:9091/transmission/rpc``.
    """

    def __init__(self, base_url: str, *, username: str | None = None,
                 password: str | None = None, timeout: float = 20.0):
        self.base_url = base_url
        self._auth = (username, password) if username else None
        self._timeout = timeout
        self._session_id: str | None = None
        self._http: tuple[asyncio.AbstractEventLoop, httpx.AsyncClient] | None = None

    def _client(self) -> httpx.AsyncClient:
        """The shared pool for the running event loop (see SonarrClient._client)."""
        loop = asyncio.get_running_loop()
        if self._http is None or self._http[0] is not loop or self._http[1].is_closed:
            self._http = (loop, httpx.AsyncClient(timeout=self._timeout, auth=self._auth))
        return self._http[1]

    async def _rpc(self, method: str, **arguments) -> dict:
        """One RPC call, transparently performing the session-id handshake.

        Transmission rejects a request without a valid session id with 409 and
        returns the current id in a header. The id also rotates, so a 409 can
        arrive mid-session — but we retry exactly once, because a server that
        409s every time would otherwise spin forever.
        """
        body: dict = {"method": method}
        if arguments:
            body["arguments"] = arguments

        for attempt in (0, 1):
            headers = {SESSION_HEADER: self._session_id} if self._session_id else {}
            resp = await self._client().post(self.base_url, json=body, headers=headers)
            if resp.status_code == 409:
                fresh = resp.headers.get(SESSION_HEADER)
                if fresh and attempt == 0:
                    self._session_id = fresh
                    continue
                resp.raise_for_status()
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("result") != "success":
                raise RuntimeError(
                    f"Transmission {method} failed: {payload.get('result')!r}")
            return payload.get("arguments") or {}

        raise RuntimeError(f"Transmission {method} failed: session handshake did not settle")

    async def torrents(self) -> list[dict]:
        """Every torrent the client holds, with the liveness fields."""
        args = await self._rpc("torrent-get", fields=list(TORRENT_FIELDS))
        return args.get("torrents") or []

    async def aclose(self) -> None:
        if self._http is None:
            return
        loop, http = self._http
        self._http = None
        if loop is asyncio.get_running_loop():
            await http.aclose()
