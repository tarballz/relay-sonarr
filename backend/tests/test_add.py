import json as _json

import httpx
import respx

from app.config import Config, InstanceConfig
from app.services.add import add_to_instance, build_add_payload
from app.sonarr.registry import Registry

# smart_add / advance_fallback (the chain walk) are covered in test_chain.py.

A = "http://10.0.0.1:8989"
B = "http://10.0.0.2:8990"


def make_registry():
    cfg = Config(
        instances=[
            InstanceConfig(id="1080p", name="Sonarr 1080p", url=A, api_key="ka"),
            InstanceConfig(id="4k", name="Sonarr 4K", url=B, api_key="kb"),
        ],
    )
    return Registry(cfg)


# --- pure payload ------------------------------------------------------------

def test_build_add_payload_sets_required_fields_and_keeps_lookup_data():
    series = {"tvdbId": 81189, "title": "Breaking Bad", "titleSlug": "breaking-bad"}
    payload = build_add_payload(
        series, quality_profile_id=1, root_folder_path="/tv", monitored=True, search_now=True
    )
    assert payload["tvdbId"] == 81189
    assert payload["title"] == "Breaking Bad"
    assert payload["titleSlug"] == "breaking-bad"
    assert payload["qualityProfileId"] == 1
    assert payload["rootFolderPath"] == "/tv"
    assert payload["monitored"] is True
    assert payload["addOptions"]["searchForMissingEpisodes"] is True


def test_build_add_payload_search_now_false():
    payload = build_add_payload({"tvdbId": 1}, 1, "/tv", monitored=True, search_now=False)
    assert payload["addOptions"]["searchForMissingEpisodes"] is False


# --- add over HTTP -----------------------------------------------------------

@respx.mock
async def test_add_to_instance_looks_up_then_posts():
    reg = make_registry()
    respx.get(f"{A}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": 81189, "title": "Breaking Bad"}])
    )
    post = respx.post(f"{A}/api/v3/series").mock(
        return_value=httpx.Response(201, json={"id": 7, "title": "Breaking Bad"})
    )

    result = await add_to_instance(
        reg, "1080p", tvdb_id=81189, quality_profile_id=1, root_folder_path="/tv"
    )

    body = _json.loads(post.calls.last.request.content)
    assert body["tvdbId"] == 81189
    assert body["qualityProfileId"] == 1
    assert result["id"] == 7
