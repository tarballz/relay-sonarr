"""Transmission RPC client: the 409 session-id handshake is the whole trick.

Every Transmission RPC call must carry an ``X-Transmission-Session-Id``. The
server hands it out by rejecting the first request with 409 and putting the id
in a response header, so the client has to retry. The id also rotates, which
means a *mid-session* 409 has to be handled the same way — but exactly once, or
a rotating server turns into an infinite loop.
"""
import json

import httpx
import pytest
import respx

from app.download.transmission import TransmissionClient

RPC = "http://10.0.0.3:9091/transmission/rpc"
SID = "session-abc"


@pytest.fixture
def client():
    return TransmissionClient(base_url=RPC)


def _ok(torrents):
    return httpx.Response(200, json={"result": "success", "arguments": {"torrents": torrents}})


def _409(sid=SID):
    return httpx.Response(409, headers={"X-Transmission-Session-Id": sid}, text="conflict")


@respx.mock
async def test_handshake_retries_once_with_the_session_id(client):
    route = respx.post(RPC).mock(side_effect=[_409(), _ok([{"hashString": "AA"}])])

    torrents = await client.torrents()

    assert len(route.calls) == 2
    assert "X-Transmission-Session-Id" not in route.calls[0].request.headers
    assert route.calls[1].request.headers["X-Transmission-Session-Id"] == SID
    assert torrents[0]["hashString"] == "AA"


@respx.mock
async def test_session_id_is_cached_across_calls(client):
    route = respx.post(RPC).mock(side_effect=[_409(), _ok([]), _ok([])])

    await client.torrents()
    await client.torrents()

    # Only the very first request lacked the header; no second handshake.
    assert len(route.calls) == 3
    assert route.calls[2].request.headers["X-Transmission-Session-Id"] == SID


@respx.mock
async def test_rotated_session_id_is_refreshed_once(client):
    route = respx.post(RPC).mock(
        side_effect=[_409("first"), _ok([]), _409("second"), _ok([{"hashString": "BB"}])]
    )

    await client.torrents()
    torrents = await client.torrents()

    assert route.calls[3].request.headers["X-Transmission-Session-Id"] == "second"
    assert torrents[0]["hashString"] == "BB"


@respx.mock
async def test_repeated_409_gives_up_instead_of_looping(client):
    respx.post(RPC).mock(side_effect=[_409("a"), _409("b"), _409("c"), _409("d")])

    with pytest.raises(httpx.HTTPError):
        await client.torrents()


@respx.mock
async def test_torrents_requests_the_liveness_fields(client):
    route = respx.post(RPC).mock(side_effect=[_409(), _ok([])])

    await client.torrents()

    body = json.loads(route.calls[1].request.content)
    assert body["method"] == "torrent-get"
    fields = body["arguments"]["fields"]
    for needed in ("hashString", "metadataPercentComplete", "trackerStats",
                   "peersConnected", "rateDownload", "percentDone"):
        assert needed in fields


@respx.mock
async def test_non_success_result_raises(client):
    respx.post(RPC).mock(return_value=httpx.Response(200, json={"result": "no such method"}))

    with pytest.raises(RuntimeError):
        await client.torrents()
