import httpx
import respx

from app.config import Config, FallbackStep, InstanceConfig
from app.sonarr.registry import Registry
from app.services import library, queue, health

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


def test_registry_lookup_and_fallback_chain():
    reg = make_registry()
    assert reg.get("4k").name == "Sonarr 4K"
    assert reg.get("4k").client.base_url == B
    chain = reg.fallback_chain("4k")
    assert [s.instanceId for s in chain] == ["1080p", "1080p"]
    assert [s.profile for s in chain] == [None, "SD"]
    assert reg.fallback_chain("1080p") == []
    assert {i.id for i in reg.all()} == {"1080p", "4k"}


@respx.mock
async def test_combined_series_merges_and_tags():
    reg = make_registry()
    respx.get(f"{A}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "title": "Show A"}])
    )
    respx.get(f"{B}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "title": "Show B"}])
    )

    result = await library.combined_series(reg)

    assert len(result) == 2
    tags = {(s["instanceId"], s["title"]) for s in result}
    assert ("1080p", "Show A") in tags
    assert ("4k", "Show B") in tags


@respx.mock
async def test_combined_series_tolerates_one_instance_down():
    reg = make_registry()
    respx.get(f"{A}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "title": "Show A"}])
    )
    respx.get(f"{B}/api/v3/series").mock(return_value=httpx.Response(500))

    result = await library.combined_series(reg)

    # The healthy instance's data still comes through.
    assert [s["title"] for s in result] == ["Show A"]


@respx.mock
async def test_combined_queue_flattens_records_and_tags():
    reg = make_registry()
    respx.get(f"{A}/api/v3/queue").mock(
        return_value=httpx.Response(200, json={"records": [{"id": 11}]})
    )
    respx.get(f"{B}/api/v3/queue").mock(
        return_value=httpx.Response(200, json={"records": [{"id": 22}, {"id": 23}]})
    )

    result = await queue.combined_queue(reg)

    assert len(result) == 3
    assert {r["instanceId"] for r in result} == {"1080p", "4k"}


@respx.mock
async def test_instances_health_reports_online_and_offline():
    reg = make_registry()
    respx.get(f"{A}/api/v3/system/status").mock(
        return_value=httpx.Response(200, json={"version": "4.0.1"})
    )
    respx.get(f"{B}/api/v3/system/status").mock(return_value=httpx.Response(500))

    result = await health.instances_health(reg)
    by_id = {h["id"]: h for h in result}

    assert by_id["1080p"]["online"] is True
    assert by_id["1080p"]["version"] == "4.0.1"
    assert by_id["4k"]["online"] is False
