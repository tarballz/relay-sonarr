"""Async wrapper around a single Sonarr v3 API instance."""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections import deque
from datetime import datetime, timezone

import httpx

from app.obs.metrics import SONARR_DURATION, SONARR_REQUESTS

logger = logging.getLogger(__name__)

_NUMERIC_SEGMENT = re.compile(r"/\d+(?=/|$)")


def endpoint_template(path: str) -> str:
    """``/series/12`` → ``/series/{id}``: bounded metric label cardinality."""
    return _NUMERIC_SEGMENT.sub("/{id}", path)


def _outcome(error: BaseException | None) -> str:
    if error is None:
        return "ok"
    if isinstance(error, httpx.HTTPStatusError):
        return f"http_{error.response.status_code // 100}xx"
    if isinstance(error, httpx.TimeoutException):
        return "timeout"
    return "error"


def _percentile(sorted_values: list[float], q: float) -> float | None:
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, max(0, math.ceil(q * len(sorted_values)) - 1))
    return round(sorted_values[index], 1)


class ClientStats:
    """Rolling per-instance call statistics for health views (last ``window`` calls)."""

    def __init__(self, *, window: int = 200, clock=time.time):
        self._calls: deque = deque(maxlen=window)  # (epoch seconds, ms, ok)
        self._clock = clock
        self.last_ok_at: float | None = None
        self.last_error: str | None = None
        self.consecutive_failures = 0

    def record(self, ms: float, ok: bool, error: str | None = None) -> None:
        now = self._clock()
        self._calls.append((now, ms, ok))
        if ok:
            self.last_ok_at = now
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1
            self.last_error = error

    def snapshot(self) -> dict:
        now = self._clock()
        durations = sorted(c[1] for c in self._calls)
        recent = [c for c in self._calls if now - c[0] <= 300]
        return {
            "calls": len(self._calls),
            "p50Ms": _percentile(durations, 0.5),
            "p95Ms": _percentile(durations, 0.95),
            "errorRate5m": (sum(1 for c in recent if not c[2]) / len(recent)) if recent else 0.0,
            "lastOkAt": (datetime.fromtimestamp(self.last_ok_at, timezone.utc).isoformat()
                         if self.last_ok_at is not None else None),
            "lastError": self.last_error,
            "consecutiveFailures": self.consecutive_failures,
        }


class SonarrClient:
    """Talks to ONE Sonarr instance's v3 API.

    Instantiate once per instance (e.g. 1080p, 4K). All methods are async and
    return parsed JSON. Auth is the per-instance ``X-Api-Key`` header. Every call
    goes through ``_request``, which reuses one connection pool and records
    latency/outcome into ``stats`` and the Prometheus metrics.
    """

    # An interactive release search blocks until every indexer answers (some sit
    # behind FlareSolverr), which routinely exceeds the default timeout.
    RELEASE_SEARCH_TIMEOUT = 180.0

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30.0,
                 name: str | None = None, perf_counter=time.perf_counter, clock=time.time):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._timeout = timeout
        self.name = name or self.base_url
        self.stats = ClientStats(clock=clock)
        self._perf = perf_counter
        self._http: tuple[asyncio.AbstractEventLoop, httpx.AsyncClient] | None = None

    def _client(self) -> httpx.AsyncClient:
        """The shared pool for the running event loop.

        An AsyncClient is bound to the loop it first ran on; the app has one loop,
        but tests (and TestClient) use several, so rebuild when the loop changes.
        """
        loop = asyncio.get_running_loop()
        if self._http is None or self._http[0] is not loop or self._http[1].is_closed:
            self._http = (loop, httpx.AsyncClient(
                base_url=f"{self.base_url}/api/v3",
                headers={"X-Api-Key": self.api_key},
                timeout=self._timeout,
            ))
        return self._http[1]

    async def _request(self, method: str, path: str, *, params: dict | None = None,
                       json: dict | None = None, timeout: float | None = None) -> httpx.Response:
        kwargs: dict = {"params": params}
        if json is not None:
            kwargs["json"] = json
        if timeout is not None:  # omit rather than pass None, which disables the timeout
            kwargs["timeout"] = timeout
        http = self._client()
        start = self._perf()
        error: BaseException | None = None
        try:
            resp = await http.request(method, path, **kwargs)
            resp.raise_for_status()
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._observe(method, path, self._perf() - start, error)
        return resp

    def _observe(self, method: str, path: str, seconds: float,
                 error: BaseException | None) -> None:
        if isinstance(error, asyncio.CancelledError):
            return  # our own cancellation says nothing about Sonarr's health
        endpoint = endpoint_template(path)
        outcome = _outcome(error)
        SONARR_REQUESTS.inc(instance=self.name, method=method, endpoint=endpoint, outcome=outcome)
        SONARR_DURATION.observe(seconds, instance=self.name)
        message = None if error is None else (str(error) or type(error).__name__)
        self.stats.record(seconds * 1000, error is None, message)
        logger.debug("%s %s %s %s %.0fms", self.name, method, endpoint, outcome, seconds * 1000)

    async def _get(self, path: str, params: dict | None = None, *, timeout: float | None = None):
        return (await self._request("GET", path, params=params, timeout=timeout)).json()

    async def _post(self, path: str, json: dict):
        return (await self._request("POST", path, json=json)).json()

    async def _put(self, path: str, json: dict):
        return (await self._request("PUT", path, json=json)).json()

    async def _delete(self, path: str, params: dict | None = None):
        await self._request("DELETE", path, params=params)
        return None

    async def aclose(self) -> None:
        if self._http is None:
            return
        loop, http = self._http
        self._http = None
        if loop is asyncio.get_running_loop():
            await http.aclose()

    async def health(self) -> list[dict]:
        """Sonarr's own health checks (GET /health) — e.g. IndexerStatusCheck."""
        return await self._get("/health")

    async def lookup(self, term: str) -> list[dict]:
        """Search series by title/tvdb term (GET /series/lookup?term=)."""
        return await self._get("/series/lookup", {"term": term})

    async def list_series(self) -> list[dict]:
        """All series in this instance's library (GET /series)."""
        return await self._get("/series")

    async def get_series(self, series_id: int) -> dict:
        """A single series by id (GET /series/{id})."""
        return await self._get(f"/series/{series_id}")

    async def update_series(self, series: dict) -> dict:
        """Update a series (PUT /series/{id}); send the full object back."""
        return await self._put(f"/series/{series['id']}", series)

    async def quality_profiles(self) -> list[dict]:
        """Configured quality profiles (GET /qualityprofile)."""
        return await self._get("/qualityprofile")

    async def root_folders(self) -> list[dict]:
        """Configured root folders (GET /rootfolder)."""
        return await self._get("/rootfolder")

    async def system_status(self) -> dict:
        """Instance health/version (GET /system/status)."""
        return await self._get("/system/status")

    async def disk_space(self) -> list[dict]:
        """Free/total space per root path (GET /diskspace)."""
        return await self._get("/diskspace")

    async def queue(self) -> dict:
        """Full download queue with episode details (GET /queue?includeEpisode=true).

        Sonarr paginates /queue (default pageSize 10) and sorts completed /
        import-blocked items first, so a single default page can be entirely
        stuck-completed items and hide every active download. Fetch every page
        and return them as one envelope (records aggregated)."""
        page = 1
        first = await self._get(
            "/queue", {"includeEpisode": "true", "pageSize": 200, "page": page}
        )
        records = list(first.get("records", []))
        total = first.get("totalRecords", len(records))
        while len(records) < total:
            page += 1
            nxt = await self._get(
                "/queue", {"includeEpisode": "true", "pageSize": 200, "page": page}
            )
            batch = nxt.get("records", [])
            if not batch:  # guard against a stale totalRecords looping forever
                break
            records.extend(batch)
        return {**first, "records": records}

    async def episodes(self, series_id: int) -> list[dict]:
        """Episodes for a series (GET /episode?seriesId=)."""
        return await self._get("/episode", {"seriesId": series_id})

    async def history(self, *, page: int = 1, page_size: int = 200,
                      event_type: str | None = None, include_episode: bool = True) -> dict:
        """Recent history (GET /history) — grab/import/failed events per episode."""
        params: dict = {
            "page": page,
            "pageSize": page_size,
            "includeEpisode": str(include_episode).lower(),
        }
        if event_type:
            params["eventType"] = event_type
        return await self._get("/history", params)

    async def history_since(self, date: str, *, event_type: str | None = None,
                            include_episode: bool = True) -> list[dict]:
        """History since an ISO date (GET /history/since) — cheaper incremental poll."""
        params: dict = {"date": date, "includeEpisode": str(include_episode).lower()}
        if event_type:
            params["eventType"] = event_type
        return await self._get("/history/since", params)

    async def blocklist(self, *, page: int = 1, page_size: int = 200) -> dict:
        """Blocklisted releases (GET /blocklist)."""
        return await self._get("/blocklist", {"page": page, "pageSize": page_size})

    async def indexers(self) -> list[dict]:
        """Configured indexers (GET /indexer). Used to size "are they all down?"."""
        return await self._get("/indexer")

    async def releases(self, episode_id: int) -> list[dict]:
        """Interactive release search for an episode (GET /release?episodeId=)."""
        return await self._get(
            "/release", {"episodeId": episode_id}, timeout=self.RELEASE_SEARCH_TIMEOUT
        )

    async def grab_release(self, guid: str, indexer_id: int) -> dict:
        """Grab one specific release from an interactive search (POST /release)."""
        return await self._post("/release", {"guid": guid, "indexerId": indexer_id})

    async def set_episode_monitor(self, episode_ids: list[int], monitored: bool):
        """Toggle monitoring for a set of episodes (PUT /episode/monitor).

        Used by the gap-fill split to monitor only the missing episodes on the
        fallback tier (and unmonitor them on the origin). A no-op on an empty
        list so callers don't have to guard it.
        """
        if not episode_ids:
            return None
        return await self._put(
            "/episode/monitor", {"episodeIds": episode_ids, "monitored": monitored}
        )

    async def add_series(self, payload: dict) -> dict:
        """Add a series to this instance (POST /series)."""
        return await self._post("/series", payload)

    async def command(self, name: str, **kwargs) -> dict:
        """Trigger a command, e.g. RefreshSeries/SeriesSearch (POST /command)."""
        return await self._post("/command", {"name": name, **kwargs})

    async def delete_series(
        self, series_id: int, delete_files: bool = False, add_exclusion: bool = False
    ) -> None:
        """Remove a series from this instance (DELETE /series/{id}).

        Defaults are non-destructive: keep any files on disk and don't add an
        import-list exclusion (so it can be re-added later).
        """
        await self._delete(
            f"/series/{series_id}",
            {
                "deleteFiles": str(delete_files).lower(),
                "addImportListExclusion": str(add_exclusion).lower(),
            },
        )

    async def delete_queue_item(self, queue_id: int, *, remove_from_client: bool = True,
                                blocklist: bool = True,
                                skip_redownload: bool = False) -> None:
        """Remove a queue item (DELETE /queue/{id}).

        With blocklist=true Sonarr blocklists the release so it isn't re-grabbed and,
        leaving skipRedownload at its default, searches for a replacement.

        Pass skip_redownload=True when the caller will grab a replacement itself:
        Sonarr's own re-search ranks by quality, not liveness, so letting both run
        races us and often re-queues another dead torrent."""
        await self._delete(
            f"/queue/{queue_id}",
            {
                "removeFromClient": str(remove_from_client).lower(),
                "blocklist": str(blocklist).lower(),
                "skipRedownload": str(skip_redownload).lower(),
            },
        )
