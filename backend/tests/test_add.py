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


def test_build_add_payload_monitored_seasons_sets_flags():
    series = {
        "tvdbId": 1,
        "seasons": [
            {"seasonNumber": 0, "monitored": True},
            {"seasonNumber": 1, "monitored": True},
            {"seasonNumber": 2, "monitored": True},
        ],
    }
    payload = build_add_payload(series, 1, "/tv", monitored_seasons={1})
    by_num = {s["seasonNumber"]: s for s in payload["seasons"]}
    assert by_num[1]["monitored"] is True
    assert by_num[0]["monitored"] is False
    assert by_num[2]["monitored"] is False


def test_build_add_payload_monitored_seasons_none_leaves_seasons_untouched():
    series = {"tvdbId": 1, "seasons": [{"seasonNumber": 1, "monitored": True}]}
    payload = build_add_payload(series, 1, "/tv")
    assert payload["seasons"] == [{"seasonNumber": 1, "monitored": True}]


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


async def _nosleep(_):
    return None


@respx.mock
async def test_add_to_instance_unmonitors_off_seasons():
    reg = make_registry()
    respx.get(f"{A}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": 81189, "title": "BB"}])
    )
    respx.post(f"{A}/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 7}))
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))
    respx.get(f"{A}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 501, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": False},
        {"id": 502, "seasonNumber": 2, "episodeNumber": 1, "monitored": True, "hasFile": False},
    ]))
    monitor = respx.put(f"{A}/api/v3/episode/monitor").mock(
        return_value=httpx.Response(200, json={})
    )

    await add_to_instance(
        reg, "1080p", tvdb_id=81189, quality_profile_id=1, root_folder_path="/tv",
        monitored_seasons={1}, sleep=_nosleep,
    )

    body = _json.loads(monitor.calls.last.request.content)
    assert body["episodeIds"] == [502]  # season 2 unmonitored
    assert body["monitored"] is False


@respx.mock
async def test_add_to_instance_no_season_filter_does_not_wait_or_monitor():
    reg = make_registry()
    respx.get(f"{A}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": 81189, "title": "BB"}])
    )
    respx.post(f"{A}/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 7}))
    monitor = respx.put(f"{A}/api/v3/episode/monitor").mock(
        return_value=httpx.Response(200, json={})
    )

    await add_to_instance(reg, "1080p", tvdb_id=81189, quality_profile_id=1, root_folder_path="/tv")
    assert not monitor.called


# --- /api/availability honors the stored minSeeders default ------------------

@respx.mock
def test_availability_endpoint_honors_stored_min_seeders(tmp_path):
    from starlette.testclient import TestClient

    from app.db import Database
    from app.main import app
    from app.state import get_db, get_registry

    reg = make_registry()
    db = Database(str(tmp_path / "relay.db"))
    app.dependency_overrides[get_registry] = lambda: reg
    app.dependency_overrides[get_db] = lambda: db
    try:
        c = TestClient(app)
        assert c.put("/api/settings/defaults", json={"minSeeders": 3}).status_code == 200

        respx.get(f"{B}/api/v3/episode").mock(
            return_value=httpx.Response(200, json=[{"id": 2, "monitored": True, "title": "E"}])
        )
        respx.get(f"{B}/api/v3/release").mock(
            return_value=httpx.Response(
                200, json=[{"rejected": False, "protocol": "torrent", "seeders": 1}]
            )
        )

        r = c.get("/api/availability", params={"instanceId": "4k", "seriesId": 42})
        assert r.status_code == 200
        assert r.json()["available"] is False
        assert r.json()["seederFiltered"] == 1
    finally:
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_db, None)
