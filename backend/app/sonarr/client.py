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
        """Download queue with episode details (GET /queue?includeEpisode=true)."""
        return await self._get("/queue", {"includeEpisode": "true"})

    async def episodes(self, series_id: int) -> list[dict]:
        """Episodes for a series (GET /episode?seriesId=)."""
        return await self._get("/episode", {"seriesId": series_id})

    async def releases(self, episode_id: int) -> list[dict]:
        """Interactive release search for an episode (GET /release?episodeId=)."""
        return await self._get("/release", {"episodeId": episode_id})

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
