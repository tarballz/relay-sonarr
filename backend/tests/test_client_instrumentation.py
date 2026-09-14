"""Instrumented Sonarr client: one shared pool, per-call stats and metrics."""
import httpx
import pytest
import respx

from app.obs.metrics import SONARR_REQUESTS
from app.sonarr.client import ClientStats, SonarrClient, endpoint_template
from tests.test_chain import make_registry

BASE = "http://10.9.9.9:8989"


def _client(**kw):
    return SonarrClient(base_url=BASE, api_key="k", name="t1", **kw)


def test_endpoint_template_replaces_numeric_segments():
    assert endpoint_template("/series/12") == "/series/{id}"
    assert endpoint_template("/queue/7/x/8") == "/queue/{id}/x/{id}"
    assert endpoint_template("/episode") == "/episode"


@respx.mock
async def test_calls_share_one_async_client_until_closed():
    respx.get(f"{BASE}/api/v3/series").mock(return_value=httpx.Response(200, json=[]))
    c = _client()
    await c.list_series()
    first = c._http[1]
    await c.list_series()
    assert c._http[1] is first
    await c.aclose()
    assert first.is_closed and c._http is None


@respx.mock
async def test_success_and_http_error_recorded_in_stats_and_metrics():
    perf = iter([0.0, 0.25, 1.0, 1.5])
    c = _client(perf_counter=lambda: next(perf), clock=lambda: 1000.0)
    respx.get(f"{BASE}/api/v3/series/5").mock(return_value=httpx.Response(200, json={"id": 5}))
    respx.get(f"{BASE}/api/v3/series/6").mock(return_value=httpx.Response(503))
    labels = dict(instance="t1", method="GET", endpoint="/series/{id}")
    ok_before = SONARR_REQUESTS.value(outcome="ok", **labels)
    err_before = SONARR_REQUESTS.value(outcome="http_5xx", **labels)

    assert (await c.get_series(5)) == {"id": 5}
    with pytest.raises(httpx.HTTPStatusError):
        await c.get_series(6)

    assert SONARR_REQUESTS.value(outcome="ok", **labels) == ok_before + 1
    assert SONARR_REQUESTS.value(outcome="http_5xx", **labels) == err_before + 1
    snap = c.stats.snapshot()
    assert snap["calls"] == 2
    assert snap["p50Ms"] == 250.0 and snap["p95Ms"] == 500.0
    assert snap["errorRate5m"] == 0.5
    assert snap["consecutiveFailures"] == 1
    assert "503" in snap["lastError"]
    assert snap["lastOkAt"] == "1970-01-01T00:16:40+00:00"


@respx.mock
async def test_transport_errors_are_classified():
    c = _client()
    respx.get(f"{BASE}/api/v3/system/status").mock(side_effect=httpx.ConnectError("refused"))
    respx.get(f"{BASE}/api/v3/health").mock(side_effect=httpx.ReadTimeout("slow"))
    err = dict(instance="t1", method="GET", endpoint="/system/status", outcome="error")
    slow = dict(instance="t1", method="GET", endpoint="/health", outcome="timeout")
    err_before, slow_before = SONARR_REQUESTS.value(**err), SONARR_REQUESTS.value(**slow)

    with pytest.raises(httpx.ConnectError):
        await c.system_status()
    with pytest.raises(httpx.ReadTimeout):
        await c.health()

    assert SONARR_REQUESTS.value(**err) == err_before + 1
    assert SONARR_REQUESTS.value(**slow) == slow_before + 1
    assert c.stats.snapshot()["consecutiveFailures"] == 2


@respx.mock
async def test_health_returns_sonarr_health_items():
    items = [{"source": "IndexerStatusCheck", "type": "warning", "message": "Indexers unavailable"}]
    route = respx.get(f"{BASE}/api/v3/health").mock(return_value=httpx.Response(200, json=items))
    assert await _client().health() == items
    assert route.calls.last.request.headers["X-Api-Key"] == "k"


def test_stats_error_rate_only_counts_last_five_minutes():
    now = [0.0]
    stats = ClientStats(clock=lambda: now[0])
    assert stats.snapshot()["p50Ms"] is None
    stats.record(10, False, "old failure")
    now[0] = 400.0
    stats.record(20, True)
    snap = stats.snapshot()
    assert snap["errorRate5m"] == 0.0
    assert snap["consecutiveFailures"] == 0
    assert snap["lastError"] == "old failure"


async def test_registry_names_clients_by_instance_id_and_closes_them():
    reg = make_registry()
    assert reg.get("4k").client.name == "4k"
    await reg.aclose()
