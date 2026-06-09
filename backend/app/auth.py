"""Optional defense-in-depth verification of the Cloudflare Access JWT.

When the app is exposed via the tunnel, Cloudflare Access authenticates users at
the edge and forwards a signed ``Cf-Access-Jwt-Assertion`` header. Enabling this
(CF_ACCESS_ENABLED=true) makes the app reject any request lacking a valid token —
closing the gap where someone reaching the container directly on the LAN would
otherwise bypass Access. Disabled by default so local/dev runs need no setup.
"""
from __future__ import annotations

import logging
import os
import time

import httpx
from fastapi import Header, HTTPException, Request
from jose import jwt

logger = logging.getLogger(__name__)

# Cloudflare rotates the Access signing keys periodically; cache the JWKS but
# expire it so a rotation can't lock everyone out (or, worse, let a withdrawn key
# keep validating) until a restart.
_JWKS_TTL = 3600.0          # refetch at least hourly
_JWKS_MIN_REFETCH = 60.0    # on a verify failure, refetch at most this often
_jwks_cache: tuple[dict, float] | None = None  # (keys, fetched_at)


def _now() -> float:
    return time.monotonic()


def _settings() -> dict:
    return {
        "enabled": os.environ.get("CF_ACCESS_ENABLED", "false").lower() == "true",
        "team_domain": os.environ.get("CF_ACCESS_TEAM_DOMAIN", ""),
        "aud": os.environ.get("CF_ACCESS_AUD", ""),
    }


async def _get_jwks(team_domain: str, *, retry: bool = False) -> dict:
    """Return the cached JWKS, refetching when stale. ``retry`` allows a fresh
    fetch on a verification failure (key rotation) but is rate-limited so a flood
    of bad tokens can't hammer the certs endpoint."""
    global _jwks_cache
    now = _now()
    if _jwks_cache is None:
        stale = True
    else:
        age = now - _jwks_cache[1]
        stale = age > _JWKS_TTL or (retry and age > _JWKS_MIN_REFETCH)
    if stale:
        url = f"https://{team_domain}/cdn-cgi/access/certs"
        async with httpx.AsyncClient(timeout=10) as http:
            keys = (await http.get(url)).json()
        _jwks_cache = (keys, now)
    return _jwks_cache[0]


async def verify_access(
    request: Request = None,
    cf_assertion: str | None = Header(default=None, alias="Cf-Access-Jwt-Assertion"),
) -> str | None:
    """FastAPI dependency. Returns the authenticated email, or None when disabled."""
    # The liveness probe must stay reachable even with Access enabled (the
    # in-container Docker healthcheck has no JWT).
    if request is not None and request.url.path == "/healthz":
        return None
    cfg = _settings()
    if not cfg["enabled"]:
        return None
    if not cf_assertion:
        raise HTTPException(status_code=403, detail="Missing Cloudflare Access token")

    def _decode(keys: dict) -> str | None:
        claims = jwt.decode(
            cf_assertion,
            keys,
            algorithms=["RS256"],
            audience=cfg["aud"],
            issuer=f"https://{cfg['team_domain']}",
        )
        return claims.get("email")

    try:
        return _decode(await _get_jwks(cfg["team_domain"]))
    except Exception:  # noqa: BLE001 - maybe rotated keys; refetch once and retry
        try:
            return _decode(await _get_jwks(cfg["team_domain"], retry=True))
        except Exception as exc:  # noqa: BLE001
            # Log the real reason server-side; never leak it to the client.
            logger.warning("Cf-Access token rejected: %s", exc)
            raise HTTPException(status_code=403, detail="Invalid Access token")
