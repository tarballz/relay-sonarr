"""Async wrapper around a single Sonarr v3 API instance."""
from __future__ import annotations

import httpx


class SonarrClient:
    """Talks to ONE Sonarr instance's v3 API.

    Instantiate once per instance (e.g. 1080p, 4K). All methods are async and
    return parsed JSON. Auth is the per-instance ``X-Api-Key`` header.
    """

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=f"{self.base_url}/api/v3",
            headers={"X-Api-Key": self.api_key},
            timeout=self._timeout,
        )

    async def _get(self, path: str, params: dict | None = None):
        async with self._client() as http:
            resp = await http.get(path, params=params)
            resp.raise_for_status()
            return resp.json()

    async def _post(self, path: str, json: dict):
        async with self._client() as http:
            resp = await http.post(path, json=json)
            resp.raise_for_status()
            return resp.json()

    async def _put(self, path: str, json: dict):
        async with self._client() as http:
            resp = await http.put(path, json=json)
            resp.raise_for_status()
            return resp.json()

    async def _delete(self, path: str, params: dict | None = None):
        async with self._client() as http:
            resp = await http.delete(path, params=params)
            resp.raise_for_status()
            return None

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

    async def releases(self, episode_id: int) -> list[dict]:
        """Interactive release search for an episode (GET /release?episodeId=)."""
        return await self._get("/release", {"episodeId": episode_id})

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
                                blocklist: bool = True) -> None:
        """Remove a queue item (DELETE /queue/{id}).

        With blocklist=true Sonarr blocklists the release so it isn't re-grabbed and,
        leaving skipRedownload at its default, searches for a replacement."""
        await self._delete(
            f"/queue/{queue_id}",
            {
                "removeFromClient": str(remove_from_client).lower(),
                "blocklist": str(blocklist).lower(),
            },
        )
