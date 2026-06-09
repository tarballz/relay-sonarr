"""Fallback-chain walk: 4K -> 1080p(HD) -> 1080p(SD)."""
import httpx
import pytest
import respx

from app.config import Config, FallbackStep, InstanceConfig
from app.services.add import advance_fallback, resolve_step, smart_add
from app.sonarr.registry import Registry

A = "http://10.0.0.1:8989"  # 1080p
B = "http://10.0.0.2:8990"  # 4k


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


async def _nosleep(_):
    return None


def mock_1080p_profiles():
    respx.get(f"{A}/api/v3/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}, {"id": 7, "name": "SD"}])
    )
    respx.get(f"{A}/api/v3/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "path": "/tv"}])
    )


# --- resolve_step ------------------------------------------------------------

@respx.mock
async def test_resolve_step_uses_first_profile_when_unspecified():
    reg = make_registry()
    mock_1080p_profiles()
    resolved = await resolve_step(reg, FallbackStep(instanceId="1080p"))
    assert resolved["qualityProfileId"] == 4
    assert resolved["qualityProfileName"] == "HD-1080p"
    assert resolved["rootFolderPath"] == "/tv"


@respx.mock
async def test_resolve_step_resolves_named_profile():
    reg = make_registry()
    mock_1080p_profiles()
    resolved = await resolve_step(reg, FallbackStep(instanceId="1080p", profile="SD"))
    assert resolved["qualityProfileId"] == 7


@respx.mock
async def test_resolve_step_raises_for_missing_profile():
    reg = make_registry()
    respx.get(f"{A}/api/v3/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}])
    )
    respx.get(f"{A}/api/v3/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "path": "/tv"}])
    )
    with pytest.raises(ValueError, match="SD"):
        await resolve_step(reg, FallbackStep(instanceId="1080p", profile="SD"))


# --- smart_add: first step of chain on a miss --------------------------------

@respx.mock
async def test_smart_add_suggests_first_chain_step():
    reg = make_registry()
    # Add to 4K, no qualifying 4K release.
    respx.get(f"{B}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": 75710, "title": "BB"}])
    )
    respx.post(f"{B}/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 10}))
    respx.post(f"{B}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))
    respx.get(f"{B}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=[{"id": 2, "monitored": True, "title": "E"}])
    )
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[{"rejected": True}]))
    mock_1080p_profiles()  # for resolving the suggested step

    result = await smart_add(
        reg, tvdb_id=75710, target_id="4k",
        target_opts={"quality_profile_id": 1, "root_folder_path": "/tv4k"}, sleep=_nosleep,
    )

    assert result["status"] == "fallback_suggested"
    assert result["next"]["instanceId"] == "1080p"
    assert result["next"]["qualityProfileName"] == "HD-1080p"
    assert result["next"]["sameInstance"] is False
    assert result["advance"] == {
        "tvdbId": 75710, "fromInstanceId": "4k", "fromSeriesId": 10,
        "chainKey": "4k", "nextIndex": 0,
    }


@respx.mock
async def test_smart_add_added_and_triggers_download_when_target_available():
    import json as _json

    reg = make_registry()
    respx.get(f"{B}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": 75710, "title": "BB"}])
    )
    respx.post(f"{B}/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 10}))
    cmd = respx.post(f"{B}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))
    respx.get(f"{B}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=[{"id": 2, "monitored": True, "title": "E"}])
    )
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[{"rejected": False}]))

    result = await smart_add(
        reg, tvdb_id=75710, target_id="4k",
        target_opts={"quality_profile_id": 1, "root_folder_path": "/tv4k"}, sleep=_nosleep,
    )
    assert result["status"] == "added"
    assert result["instanceId"] == "4k"

    # A release exists, so the download must actually be kicked off.
    commands = [_json.loads(c.request.content)["name"] for c in cmd.calls]
    assert "SeriesSearch" in commands


@respx.mock
async def test_smart_add_unmonitors_off_seasons():
    import json as _json

    reg = make_registry()
    respx.get(f"{B}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": 75710, "title": "BB"}])
    )
    respx.post(f"{B}/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 10}))
    respx.post(f"{B}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))
    respx.get(f"{B}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        {"id": 201, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": False},
        {"id": 202, "seasonNumber": 2, "episodeNumber": 1, "monitored": True, "hasFile": False},
    ]))
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[{"rejected": False}]))
    monitor = respx.put(f"{B}/api/v3/episode/monitor").mock(return_value=httpx.Response(200, json={}))

    result = await smart_add(
        reg, tvdb_id=75710, target_id="4k",
        target_opts={
            "quality_profile_id": 1, "root_folder_path": "/tv4k", "monitored_seasons": [1],
        },
        sleep=_nosleep,
    )

    assert result["status"] == "added"
    body = _json.loads(monitor.calls.last.request.content)
    assert body["episodeIds"] == [202]  # season 2 unmonitored
    assert body["monitored"] is False


@respx.mock
async def test_smart_add_emits_phase_events():
    events = []

    async def emit(e):
        events.append(e)

    reg = make_registry()
    respx.get(f"{B}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": 75710, "title": "BB"}])
    )
    respx.post(f"{B}/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 10}))
    respx.post(f"{B}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))
    respx.get(f"{B}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=[{"id": 2, "monitored": True, "title": "E"}])
    )
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[{"rejected": False}]))

    await smart_add(
        reg, tvdb_id=75710, target_id="4k",
        target_opts={"quality_profile_id": 1, "root_folder_path": "/tv4k"},
        sleep=_nosleep, emit=emit,
    )

    pairs = [(e["phase"], e["status"]) for e in events]
    assert pairs[0] == ("add", "running")
    seq = [e["phase"] for e in events]
    assert seq.index("add") < seq.index("refresh") < seq.index("search")
    assert ("search", "done") in pairs
    assert ("grab", "done") in pairs  # release found -> download kicked off


@respx.mock
async def test_smart_add_emits_fallback_event_when_no_release():
    events = []

    async def emit(e):
        events.append(e)

    reg = make_registry()
    respx.get(f"{B}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": 75710, "title": "BB"}])
    )
    respx.post(f"{B}/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 10}))
    respx.post(f"{B}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))
    respx.get(f"{B}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=[{"id": 2, "monitored": True, "title": "E"}])
    )
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[{"rejected": True}]))
    mock_1080p_profiles()

    await smart_add(
        reg, tvdb_id=75710, target_id="4k",
        target_opts={"quality_profile_id": 1, "root_folder_path": "/tv4k"},
        sleep=_nosleep, emit=emit,
    )

    pairs = [(e["phase"], e["status"]) for e in events]
    assert ("fallback", "done") in pairs
    assert ("grab", "done") not in pairs


@respx.mock
async def test_smart_add_no_chain_no_release_does_not_search():
    import json as _json

    # Instance with NO fallback chain configured.
    cfg = Config(
        instances=[InstanceConfig(id="4k", name="Sonarr 4K", url=B, api_key="kb")],
        fallback_chains={},
    )
    reg = Registry(cfg)
    respx.get(f"{B}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": 75710, "title": "BB"}])
    )
    respx.post(f"{B}/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 10}))
    cmd = respx.post(f"{B}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))
    respx.get(f"{B}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=[{"id": 2, "monitored": True, "title": "E"}])
    )
    respx.get(f"{B}/api/v3/release").mock(return_value=httpx.Response(200, json=[{"rejected": True}]))

    result = await smart_add(
        reg, tvdb_id=75710, target_id="4k",
        target_opts={"quality_profile_id": 1, "root_folder_path": "/tv4k"}, sleep=_nosleep,
    )
    # Nothing qualifies and there's no chain: added + monitored, but no grab attempted.
    assert result["status"] == "added"
    commands = [_json.loads(c.request.content)["name"] for c in cmd.calls]
    assert "SeriesSearch" not in commands


# --- advance_fallback: cross-instance move -----------------------------------

@respx.mock
async def test_advance_cross_instance_places_when_release_found():
    reg = make_registry()
    mock_1080p_profiles()
    # Add to 1080p.
    respx.get(f"{A}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": 75710, "title": "BB"}])
    )
    respx.post(f"{A}/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 22}))
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))
    respx.get(f"{A}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "monitored": True, "title": "E"}])
    )
    respx.get(f"{A}/api/v3/release").mock(return_value=httpx.Response(200, json=[{"rejected": False}]))
    # Remove abandoned 4K series.
    delete = respx.delete(f"{B}/api/v3/series/10").mock(return_value=httpx.Response(200, json={}))

    result = await advance_fallback(
        reg, tvdb_id=75710, from_instance_id="4k", from_series_id=10,
        chain_key="4k", next_index=0, sleep=_nosleep,
    )

    assert delete.called
    assert result["status"] == "placed"
    assert result["instanceId"] == "1080p"
    assert result["seriesId"] == 22


@respx.mock
async def test_advance_cross_instance_suggests_next_when_still_missing():
    reg = make_registry()
    mock_1080p_profiles()
    respx.get(f"{A}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": 75710, "title": "BB"}])
    )
    respx.post(f"{A}/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 22}))
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))
    respx.get(f"{A}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "monitored": True, "title": "E"}])
    )
    respx.get(f"{A}/api/v3/release").mock(return_value=httpx.Response(200, json=[{"rejected": True}]))
    respx.delete(f"{B}/api/v3/series/10").mock(return_value=httpx.Response(200, json={}))

    result = await advance_fallback(
        reg, tvdb_id=75710, from_instance_id="4k", from_series_id=10,
        chain_key="4k", next_index=0, sleep=_nosleep,
    )

    # 1080p HD had nothing -> suggest the SD step on the SAME instance.
    assert result["status"] == "fallback_suggested"
    assert result["next"]["sameInstance"] is True
    assert result["next"]["qualityProfileName"] == "SD"
    assert result["advance"]["fromInstanceId"] == "1080p"
    assert result["advance"]["fromSeriesId"] == 22
    assert result["advance"]["nextIndex"] == 1


# --- advance_fallback: same-instance profile swap ----------------------------

@respx.mock
async def test_advance_same_instance_swaps_profile_and_places():
    reg = make_registry()
    mock_1080p_profiles()
    respx.get(f"{A}/api/v3/series/22").mock(
        return_value=httpx.Response(200, json={"id": 22, "qualityProfileId": 4, "title": "BB"})
    )
    put = respx.put(f"{A}/api/v3/series/22").mock(
        return_value=httpx.Response(200, json={"id": 22, "qualityProfileId": 7})
    )
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 2}))
    respx.get(f"{A}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "monitored": True, "title": "E"}])
    )
    respx.get(f"{A}/api/v3/release").mock(return_value=httpx.Response(200, json=[{"rejected": False}]))

    result = await advance_fallback(
        reg, tvdb_id=75710, from_instance_id="1080p", from_series_id=22,
        chain_key="4k", next_index=1, sleep=_nosleep,
    )

    import json
    assert json.loads(put.calls.last.request.content)["qualityProfileId"] == 7
    assert result["status"] == "placed"
    assert result["seriesId"] == 22


@respx.mock
async def test_advance_last_step_exhausted_when_still_missing():
    reg = make_registry()
    mock_1080p_profiles()
    respx.get(f"{A}/api/v3/series/22").mock(
        return_value=httpx.Response(200, json={"id": 22, "qualityProfileId": 4, "title": "BB"})
    )
    respx.put(f"{A}/api/v3/series/22").mock(
        return_value=httpx.Response(200, json={"id": 22, "qualityProfileId": 7})
    )
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 2}))
    respx.get(f"{A}/api/v3/episode").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "monitored": True, "title": "E"}])
    )
    respx.get(f"{A}/api/v3/release").mock(return_value=httpx.Response(200, json=[{"rejected": True}]))

    result = await advance_fallback(
        reg, tvdb_id=75710, from_instance_id="1080p", from_series_id=22,
        chain_key="4k", next_index=1, sleep=_nosleep,
    )
    assert result["status"] == "exhausted"
    assert result["instanceId"] == "1080p"
