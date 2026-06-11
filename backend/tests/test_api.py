import httpx
import respx
from starlette.testclient import TestClient

from app.config import Config, FallbackStep, InstanceConfig
from app.db import Database
from app.main import app
from app.sonarr.registry import Registry
from app.state import get_db, get_operations, get_registry
from app.store.operations import OperationStore

A = "http://10.0.0.1:8989"
B = "http://10.0.0.2:8990"


def make_registry():
    cfg = Config(
        instances=[
            InstanceConfig(id="1080p", name="Sonarr 1080p", url=A, api_key="ka"),
            InstanceConfig(id="4k", name="Sonarr 4K", url=B, api_key="kb"),
        ],
        fallback_chains={
            "4k": [
                FallbackStep(instanceId="1080p"),
                FallbackStep(instanceId="1080p", profile="SD"),
            ]
        },
    )
    return Registry(cfg)


def client():
    db = Database(":memory:")
    app.dependency_overrides[get_registry] = make_registry
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def make_store(tmp_path):
    """A durable OperationStore on a throwaway SQLite file for stream/op tests."""
    return OperationStore(Database(str(tmp_path / "relay.db")))


@respx.mock
def test_instances_endpoint_reports_health():
    respx.get(f"{A}/api/v3/system/status").mock(
        return_value=httpx.Response(200, json={"version": "4.0.1"})
    )
    respx.get(f"{B}/api/v3/system/status").mock(return_value=httpx.Response(500))

    resp = client().get("/api/instances")
    assert resp.status_code == 200
    by_id = {h["id"]: h for h in resp.json()}
    assert by_id["1080p"]["online"] is True
    assert by_id["4k"]["online"] is False


@respx.mock
def test_queue_endpoint_merges_instances():
    respx.get(f"{A}/api/v3/queue").mock(
        return_value=httpx.Response(200, json={"records": [{"id": 1}]})
    )
    respx.get(f"{B}/api/v3/queue").mock(
        return_value=httpx.Response(200, json={"records": [{"id": 2}]})
    )

    resp = client().get("/api/queue")
    assert resp.status_code == 200
    assert {r["instanceId"] for r in resp.json()} == {"1080p", "4k"}


@respx.mock
def test_profiles_unknown_instance_404():
    resp = client().get("/api/instances/nope/profiles")
    assert resp.status_code == 404


@respx.mock
def test_smart_add_suggests_first_chain_step_via_api():
    # Target 4K add succeeds but no qualifying release exists.
    respx.get(f"{B}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": 81189, "title": "BB"}])
    )
    respx.post(f"{B}/api/v3/series").mock(
        return_value=httpx.Response(201, json={"id": 7, "title": "BB"})
    )
    respx.post(f"{B}/api/v3/command").mock(
        return_value=httpx.Response(201, json={"id": 1, "name": "RefreshSeries"})
    )
    respx.get(f"{B}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=[{"id": 2, "monitored": True, "title": "E"}])
    )
    respx.get(f"{B}/api/v3/release").mock(
        return_value=httpx.Response(200, json=[{"rejected": True}])
    )
    # resolve_step for the suggested 1080p step.
    respx.get(f"{A}/api/v3/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}, {"id": 7, "name": "SD"}])
    )
    respx.get(f"{A}/api/v3/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "path": "/tv"}])
    )

    resp = client().post(
        "/api/smart-add",
        json={
            "tvdbId": 81189,
            "targetInstanceId": "4k",
            "targetQualityProfileId": 1,
            "targetRootFolderPath": "/tv4k",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "fallback_suggested"
    assert body["next"]["instanceId"] == "1080p"
    assert body["advance"]["chainKey"] == "4k"
    assert body["advance"]["nextIndex"] == 0


@respx.mock
def test_advance_fallback_via_api_moves_and_removes():
    respx.get(f"{A}/api/v3/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}, {"id": 7, "name": "SD"}])
    )
    respx.get(f"{A}/api/v3/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "path": "/tv"}])
    )
    respx.get(f"{A}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": 75710, "title": "Frasier"}])
    )
    respx.post(f"{A}/api/v3/series").mock(
        return_value=httpx.Response(201, json={"id": 22, "title": "Frasier"})
    )
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))
    respx.get(f"{A}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "monitored": True, "title": "E"}])
    )
    respx.get(f"{A}/api/v3/release").mock(return_value=httpx.Response(200, json=[{"rejected": False}]))
    delete = respx.delete(f"{B}/api/v3/series/10").mock(return_value=httpx.Response(200, json={}))

    resp = client().post(
        "/api/advance-fallback",
        json={
            "tvdbId": 75710,
            "fromInstanceId": "4k",
            "fromSeriesId": 10,
            "chainKey": "4k",
            "nextIndex": 0,
        },
    )
    assert resp.status_code == 200
    assert delete.called
    body = resp.json()
    assert body["status"] == "placed"
    assert body["instanceId"] == "1080p"
    assert body["seriesId"] == 22


@respx.mock
def test_add_endpoint_passes_monitored_seasons():
    import json as _json

    respx.get(f"{A}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": 81189, "title": "BB"}])
    )
    respx.post(f"{A}/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 7}))
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))
    respx.get(f"{A}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 501, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": False},
        {"id": 502, "seasonNumber": 2, "episodeNumber": 1, "monitored": True, "hasFile": False},
    ]))
    monitor = respx.put(f"{A}/api/v3/episode/monitor").mock(return_value=httpx.Response(200, json={}))

    resp = client().post("/api/add", json={
        "tvdbId": 81189,
        "targets": [{
            "instanceId": "1080p", "qualityProfileId": 1, "rootFolderPath": "/tv",
            "monitoredSeasons": [1],
        }],
    })
    assert resp.status_code == 200
    body = _json.loads(monitor.calls.last.request.content)
    assert body["episodeIds"] == [502]  # season 2 unmonitored
    assert body["monitored"] is False


@respx.mock
def test_smart_add_stream_monitored_seasons(tmp_path):
    import json as _json

    store = make_store(tmp_path)
    db = Database(":memory:")
    app.dependency_overrides[get_registry] = make_registry
    app.dependency_overrides[get_operations] = lambda: store
    app.dependency_overrides[get_db] = lambda: db
    c = TestClient(app)

    respx.get(f"{B}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": 81189, "title": "BB"}])
    )
    respx.post(f"{B}/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 7}))
    respx.post(f"{B}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))
    respx.get(f"{B}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 201, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": False},
        {"id": 202, "seasonNumber": 2, "episodeNumber": 1, "monitored": True, "hasFile": False},
    ]))
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[{"rejected": False}]))
    monitor = respx.put(f"{B}/api/v3/episode/monitor").mock(return_value=httpx.Response(200, json={}))

    resp = c.get("/api/smart-add/stream", params=[
        ("tvdbId", 81189), ("targetInstanceId", "4k"), ("targetQualityProfileId", 1),
        ("targetRootFolderPath", "/tv4k"), ("title", "BB"), ("monitoredSeasons", 1),
    ])
    assert resp.status_code == 200
    assert "event: result" in resp.text
    body = _json.loads(monitor.calls.last.request.content)
    assert body["episodeIds"] == [202]  # season 2 unmonitored
    app.dependency_overrides.pop(get_operations, None)


@respx.mock
def test_remove_series_deletes_and_defaults_no_files():
    delete = respx.delete(f"{A}/api/v3/series/5").mock(return_value=httpx.Response(200, json={}))
    resp = client().delete("/api/instances/1080p/series/5")
    assert resp.status_code == 200
    assert delete.called
    assert delete.calls.last.request.url.params["deleteFiles"] == "false"
    assert resp.json()["ok"] is True


@respx.mock
def test_remove_series_can_delete_files():
    delete = respx.delete(f"{A}/api/v3/series/5").mock(return_value=httpx.Response(200, json={}))
    resp = client().delete("/api/instances/1080p/series/5?deleteFiles=true")
    assert resp.status_code == 200
    assert delete.calls.last.request.url.params["deleteFiles"] == "true"


def test_remove_series_unknown_instance_404():
    resp = client().delete("/api/instances/nope/series/5")
    assert resp.status_code == 404


@respx.mock
def test_smart_add_stream_emits_sse_and_records_operation(tmp_path):
    store = make_store(tmp_path)
    db = Database(":memory:")
    app.dependency_overrides[get_registry] = make_registry
    app.dependency_overrides[get_operations] = lambda: store
    app.dependency_overrides[get_db] = lambda: db
    c = TestClient(app)

    # 4K target with a qualifying release → streams steps then a result.
    respx.get(f"{B}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": 81189, "title": "BB"}])
    )
    respx.post(f"{B}/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 7}))
    respx.post(f"{B}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))
    respx.get(f"{B}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=[{"id": 2, "monitored": True, "title": "E"}])
    )
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[{"rejected": False}]))

    resp = c.get(
        "/api/smart-add/stream",
        params={
            "tvdbId": 81189,
            "targetInstanceId": "4k",
            "targetQualityProfileId": 1,
            "targetRootFolderPath": "/tv4k",
            "title": "Breaking Bad",
        },
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    body = resp.text
    assert "event: step" in body
    assert "event: result" in body
    assert '"phase": "add"' in body and '"phase": "search"' in body

    # The operation was recorded with its steps + result.
    ops = c.get("/api/operations").json()
    assert ops[0]["title"] == "Breaking Bad"
    assert ops[0]["result"]["status"] == "added"
    assert len(ops[0]["steps"]) >= 4
    app.dependency_overrides.pop(get_operations, None)


@respx.mock
def test_reattempt_stream_emits_sse_and_records_operation(tmp_path):
    store = make_store(tmp_path)
    db = Database(":memory:")
    app.dependency_overrides[get_registry] = make_registry
    app.dependency_overrides[get_operations] = lambda: store
    app.dependency_overrides[get_db] = lambda: db
    c = TestClient(app)

    respx.get(f"{B}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=[{"id": 2, "monitored": True, "title": "E"}])
    )
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[{"rejected": False}]))
    respx.post(f"{B}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))

    resp = c.get(
        "/api/reattempt/stream",
        params={"tvdbId": 1, "instanceId": "4k", "seriesId": 10,
                "chainKey": "4k", "nextIndex": 0, "title": "X"},
    )
    assert resp.status_code == 200
    assert "event: result" in resp.text

    ops = c.get("/api/operations").json()
    assert ops[0]["kind"] == "reattempt"
    assert ops[0]["result"]["status"] == "placed"
    app.dependency_overrides.pop(get_operations, None)


@respx.mock
def test_fill_gaps_stream_records_split_operation(tmp_path):
    store = make_store(tmp_path)
    db = Database(":memory:")
    app.dependency_overrides[get_registry] = make_registry
    app.dependency_overrides[get_operations] = lambda: store
    app.dependency_overrides[get_db] = lambda: db
    c = TestClient(app)

    respx.get(f"{A}/api/v3/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}, {"id": 7, "name": "SD"}])
    )
    respx.get(f"{A}/api/v3/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "path": "/tv"}])
    )
    respx.get(f"{B}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 101, "seasonNumber": 1, "episodeNumber": 2, "monitored": True, "hasFile": False},
    ]))
    respx.get(f"{A}/api/v3/series").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{A}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": 1, "title": "X"}])
    )
    respx.post(f"{A}/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 55}))
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))
    respx.get(f"{A}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 201, "seasonNumber": 1, "episodeNumber": 2, "monitored": True},
    ]))
    respx.put(f"{A}/api/v3/episode/monitor").mock(return_value=httpx.Response(200, json=[]))
    respx.put(f"{B}/api/v3/episode/monitor").mock(return_value=httpx.Response(200, json=[]))

    resp = c.get(
        "/api/fill-gaps/stream",
        params={"tvdbId": 1, "fromInstanceId": "4k", "fromSeriesId": 10,
                "chainKey": "4k", "nextIndex": 0, "title": "X"},
    )
    assert resp.status_code == 200
    assert "event: result" in resp.text

    ops = c.get("/api/operations").json()
    assert ops[0]["kind"] == "fill-gaps"
    assert ops[0]["result"]["status"] == "split"
    app.dependency_overrides.pop(get_operations, None)


def _mock_spill_season():
    """Origin 4k has S1E1 + season 2; fallback 1080p already has the series."""
    respx.get(f"{A}/api/v3/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}, {"id": 7, "name": "SD"}])
    )
    respx.get(f"{A}/api/v3/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "path": "/tv"}])
    )
    respx.get(f"{B}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 10, "tvdbId": 75710, "title": "X"}])
    )
    respx.get(f"{B}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 1001, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": True},
        {"id": 1021, "seasonNumber": 2, "episodeNumber": 1, "monitored": True, "hasFile": False},
    ]))
    respx.put(f"{B}/api/v3/episode/monitor").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{A}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 55, "tvdbId": 75710, "title": "X"}])
    )
    respx.get(f"{A}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 2021, "seasonNumber": 2, "episodeNumber": 1, "monitored": False, "hasFile": False},
    ]))
    respx.put(f"{A}/api/v3/episode/monitor").mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))


@respx.mock
def test_spill_season_stream_records_operation(tmp_path):
    store = make_store(tmp_path)
    db = Database(":memory:")
    app.dependency_overrides[get_registry] = make_registry
    app.dependency_overrides[get_operations] = lambda: store
    app.dependency_overrides[get_db] = lambda: db
    c = TestClient(app)
    _mock_spill_season()

    resp = c.get("/api/spill-season/stream", params={
        "tvdbId": 75710, "season": 2, "originInstanceId": "4k",
        "fallbackInstanceId": "1080p", "title": "X",
    })
    assert resp.status_code == 200
    assert "event: result" in resp.text

    ops = c.get("/api/operations").json()
    assert ops[0]["kind"] == "spill-season"
    assert ops[0]["result"]["status"] == "spilled"
    assert ops[0]["result"]["season"] == 2
    app.dependency_overrides.pop(get_operations, None)


@respx.mock
def test_spill_season_post_returns_result():
    _mock_spill_season()
    resp = client().post("/api/spill-season", json={
        "tvdbId": 75710, "season": 2, "originInstanceId": "4k", "fallbackInstanceId": "1080p",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "spilled"
    assert body["to"]["instanceId"] == "1080p"
    assert body["episodeCount"] == 1
