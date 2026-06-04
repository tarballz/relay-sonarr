"""Optional defense-in-depth verification of the Cloudflare Access JWT.

When the app is exposed via the tunnel, Cloudflare Access authenticates users at
the edge and forwards a signed ``Cf-Access-Jwt-Assertion`` header. Enabling this
(CF_ACCESS_ENABLED=true) makes the app reject any request lacking a valid token —
closing the gap where someone reaching the container directly on the LAN would
otherwise bypass Access. Disabled by default so local/dev runs need no setup.
"""
from __future__ import annotations

import os

import httpx
from fastapi import Header, HTTPException
from jose import jwt

_jwks_cache: dict | None = None


def _settings() -> dict:
    return {
        "enabled": os.environ.get("CF_ACCESS_ENABLED", "false").lower() == "true",
        "team_domain": os.environ.get("CF_ACCESS_TEAM_DOMAIN", ""),
        "aud": os.environ.get("CF_ACCESS_AUD", ""),
    }


async def _get_jwks(team_domain: str) -> dict:
    global _jwks_cache
    if _jwks_cache is None:
        url = f"https://{team_domain}/cdn-cgi/access/certs"
        async with httpx.AsyncClient(timeout=10) as http:
            _jwks_cache = (await http.get(url)).json()
    return _jwks_cache


async def verify_access(
    cf_assertion: str | None = Header(default=None, alias="Cf-Access-Jwt-Assertion"),
) -> str | None:
    """FastAPI dependency. Returns the authenticated email, or None when disabled."""
    cfg = _settings()
    if not cfg["enabled"]:
        return None
    if not cf_assertion:
        raise HTTPException(status_code=403, detail="Missing Cloudflare Access token")
    try:
        keys = await _get_jwks(cfg["team_domain"])
        claims = jwt.decode(
            cf_assertion,
            keys,
            algorithms=["RS256"],
            audience=cfg["aud"],
            issuer=f"https://{cfg['team_domain']}",
        )
        return claims.get("email")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=403, detail=f"Invalid Access token: {exc}")
