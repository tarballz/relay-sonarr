import json

import httpx
import pytest
import respx

from app.sonarr.client import SonarrClient

BASE = "http://192.168.1.10:8989"
KEY = "test-api-key"


@pytest.fixture
def client():
    return SonarrClient(base_url=BASE, api_key=KEY)


@respx.mock
async def test_lookup_hits_v3_endpoint_with_term_and_api_key(client):
    route = respx.get(f"{BASE}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"title": "Breaking Bad", "tvdbId": 81189}])
    )

    results = await client.lookup("breaking bad")

    assert route.called
    request = route.calls.last.request
    assert request.url.params["term"] == "breaking bad"
    assert request.headers["X-Api-Key"] == KEY
    assert results[0]["tvdbId"] == 81189


@respx.mock
async def test_list_series(client):
    respx.get(f"{BASE}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "title": "Show"}])
    )
    assert (await client.list_series())[0]["id"] == 1


@respx.mock
async def test_quality_profiles(client):
    respx.get(f"{BASE}/api/v3/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "name": "HD-1080p"}])
    )
    assert (await client.quality_profiles())[0]["name"] == "HD-1080p"


@respx.mock
async def test_root_folders(client):
    respx.get(f"{BASE}/api/v3/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "path": "/tv"}])
    )
    assert (await client.root_folders())[0]["path"] == "/tv"


@respx.mock
async def test_system_status(client):
    respx.get(f"{BASE}/api/v3/system/status").mock(
        return_value=httpx.Response(200, json={"version": "4.0.0"})
    )
    assert (await client.system_status())["version"] == "4.0.0"


@respx.mock
async def test_disk_space(client):
    respx.get(f"{BASE}/api/v3/diskspace").mock(
        return_value=httpx.Response(200, json=[{"path": "/tv", "freeSpace": 100}])
    )
    assert (await client.disk_space())[0]["freeSpace"] == 100


@respx.mock
async def test_queue_includes_episode(client):
    route = respx.get(f"{BASE}/api/v3/queue").mock(
        return_value=httpx.Response(200, json={"records": [{"id": 5}]})
    )
    result = await client.queue()
    assert route.calls.last.request.url.params["includeEpisode"] == "true"
    assert result["records"][0]["id"] == 5


@respx.mock
async def test_episodes_for_series(client):
    route = respx.get(f"{BASE}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=[{"id": 10, "monitored": True}])
    )
    result = await client.episodes(42)
    assert route.calls.last.request.url.params["seriesId"] == "42"
    assert result[0]["id"] == 10


@respx.mock
async def test_releases_for_episode(client):
    route = respx.get(f"{BASE}/api/v3/release").mock(
        return_value=httpx.Response(200, json=[{"guid": "abc", "rejected": False}])
    )
    result = await client.releases(10)
    assert route.calls.last.request.url.params["episodeId"] == "10"
    assert result[0]["guid"] == "abc"


@respx.mock
async def test_add_series_posts_payload(client):
    route = respx.post(f"{BASE}/api/v3/series").mock(
        return_value=httpx.Response(201, json={"id": 7, "title": "Added"})
    )
    payload = {"tvdbId": 81189, "qualityProfileId": 1, "rootFolderPath": "/tv"}
    result = await client.add_series(payload)
    import json as _json

    assert _json.loads(route.calls.last.request.content)["tvdbId"] == 81189
    assert result["id"] == 7


@respx.mock
async def test_get_series_by_id(client):
    respx.get(f"{BASE}/api/v3/series/22").mock(
        return_value=httpx.Response(200, json={"id": 22, "title": "Frasier", "qualityProfileId": 4})
    )
    result = await client.get_series(22)
    assert result["id"] == 22
    assert result["qualityProfileId"] == 4


@respx.mock
async def test_update_series_puts_full_object(client):
    route = respx.put(f"{BASE}/api/v3/series/22").mock(
        return_value=httpx.Response(200, json={"id": 22, "qualityProfileId": 7})
    )
    result = await client.update_series({"id": 22, "qualityProfileId": 7, "title": "Frasier"})
    body = json.loads(route.calls.last.request.content)
    assert body["qualityProfileId"] == 7
    assert result["qualityProfileId"] == 7


@respx.mock
async def test_delete_series(client):
    route = respx.delete(f"{BASE}/api/v3/series/10").mock(return_value=httpx.Response(200, json={}))
    await client.delete_series(10)
    req = route.calls.last.request
    # Default: do not delete files, do not add an import-list exclusion.
    assert req.url.params["deleteFiles"] == "false"
    assert req.url.params["addImportListExclusion"] == "false"


@respx.mock
async def test_history_filters_event_type(client):
    route = respx.get(f"{BASE}/api/v3/history").mock(
        return_value=httpx.Response(200, json={"records": [{"eventType": "grabbed"}]})
    )
    await client.history(event_type="grabbed", page_size=50)
    p = route.calls.last.request.url.params
    assert p["eventType"] == "grabbed"
    assert p["pageSize"] == "50"
    assert p["includeEpisode"] == "true"


@respx.mock
async def test_history_since_passes_date(client):
    route = respx.get(f"{BASE}/api/v3/history/since").mock(
        return_value=httpx.Response(200, json=[{"eventType": "downloadFolderImported"}])
    )
    await client.history_since("2026-06-04T00:00:00Z")
    assert route.calls.last.request.url.params["date"] == "2026-06-04T00:00:00Z"


@respx.mock
async def test_set_episode_monitor_puts_body(client):
    route = respx.put(f"{BASE}/api/v3/episode/monitor").mock(
        return_value=httpx.Response(200, json=[])
    )
    await client.set_episode_monitor([3, 4, 5], True)
    body = json.loads(route.calls.last.request.content)
    assert body == {"episodeIds": [3, 4, 5], "monitored": True}


@respx.mock
async def test_set_episode_monitor_noop_on_empty(client):
    route = respx.put(f"{BASE}/api/v3/episode/monitor").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await client.set_episode_monitor([], False)
    assert result is None
    assert not route.called


@respx.mock
async def test_command_posts_name(client):
    route = respx.post(f"{BASE}/api/v3/command").mock(
        return_value=httpx.Response(201, json={"id": 99, "name": "RefreshSeries"})
    )
    result = await client.command("RefreshSeries", seriesId=42)
    import json as _json

    body = _json.loads(route.calls.last.request.content)
    assert body["name"] == "RefreshSeries"
    assert body["seriesId"] == 42
    assert result["id"] == 99
