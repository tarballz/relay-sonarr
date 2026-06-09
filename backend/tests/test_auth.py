"""Cloudflare Access JWT verification (app/auth.py).

Covers the optional defense-in-depth path: disabled passthrough, missing header,
valid/expired/wrong-audience tokens, that error detail is NOT leaked to clients,
and that the JWKS cache refetches after its TTL (key-rotation safety).
"""
import time

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jose import jwk, jwt

import app.auth as auth

TEAM = "team.cloudflareaccess.com"
AUD = "test-aud"
ISS = f"https://{TEAM}"
CERTS = f"https://{TEAM}/cdn-cgi/access/certs"

# One RSA keypair + its public JWKS for the whole module.
_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIV = _key.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
_PUB = _key.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
).decode()
_jwk = {
    k: (v.decode() if isinstance(v, bytes) else v)
    for k, v in jwk.construct(_PUB, "RS256").to_dict().items()
}
_jwk.update({"kid": "test", "alg": "RS256", "use": "sig"})
JWKS = {"keys": [_jwk]}


def _token(**override):
    claims = {
        "aud": AUD, "iss": ISS, "email": "user@example.com",
        "exp": int(time.time()) + 3600, **override,
    }
    return jwt.encode(claims, _PRIV, algorithm="RS256", headers={"kid": "test"})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    auth._jwks_cache = None
    monkeypatch.setenv("CF_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.setenv("CF_ACCESS_AUD", AUD)
    yield
    auth._jwks_cache = None


async def test_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("CF_ACCESS_ENABLED", "false")
    assert await auth.verify_access(cf_assertion=None) is None


async def test_enabled_missing_header_403():
    with pytest.raises(HTTPException) as ei:
        await auth.verify_access(cf_assertion=None)
    assert ei.value.status_code == 403


@respx.mock
async def test_valid_token_returns_email():
    respx.get(CERTS).mock(return_value=httpx.Response(200, json=JWKS))
    assert await auth.verify_access(cf_assertion=_token()) == "user@example.com"


@respx.mock
async def test_expired_token_rejected_without_leaking_detail():
    respx.get(CERTS).mock(return_value=httpx.Response(200, json=JWKS))
    with pytest.raises(HTTPException) as ei:
        await auth.verify_access(cf_assertion=_token(exp=int(time.time()) - 10))
    assert ei.value.status_code == 403
    # Generic message — must not echo the jose exception internals.
    assert ei.value.detail == "Invalid Access token"


@respx.mock
async def test_wrong_audience_rejected():
    respx.get(CERTS).mock(return_value=httpx.Response(200, json=JWKS))
    with pytest.raises(HTTPException):
        await auth.verify_access(cf_assertion=_token(aud="someone-else"))


@respx.mock
async def test_jwks_cache_refetches_after_ttl(monkeypatch):
    route = respx.get(CERTS).mock(return_value=httpx.Response(200, json=JWKS))
    clock = [1000.0]
    monkeypatch.setattr(auth, "_now", lambda: clock[0])

    await auth._get_jwks(TEAM)
    await auth._get_jwks(TEAM)
    assert route.call_count == 1  # second call served from cache

    clock[0] += auth._JWKS_TTL + 1
    await auth._get_jwks(TEAM)
    assert route.call_count == 2  # stale → refetched (handles key rotation)
