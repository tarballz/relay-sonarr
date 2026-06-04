import httpx
import respx
from starlette.testclient import TestClient

from app.config import Config, FallbackStep, InstanceConfig
from app.main import app
from app.operations import OperationLog
from app.sonarr.registry import Registry
from app.state import get_operations, get_registry

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
    app.dependency_overrides[get_registry] = make_registry
    return TestClient(app)


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
def test_smart_add_stream_emits_sse_and_records_operation():
    log = OperationLog()
    app.dependency_overrides[get_registry] = make_registry
    app.dependency_overrides[get_operations] = lambda: log
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
