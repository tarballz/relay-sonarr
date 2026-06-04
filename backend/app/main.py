"""FastAPI application: API routers + serving the built React SPA (single origin)."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from app.api import adding, catalog, instances, settings
from app.auth import verify_access
from app.operations import OperationLog
from app.state import build_registry

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.registry = build_registry()
    app.state.operations = OperationLog()
    yield


# Cf-Access verification is a no-op unless CF_ACCESS_ENABLED=true (see auth.py).
app = FastAPI(title="Unified Sonarr Dashboard", lifespan=lifespan,
              dependencies=[Depends(verify_access)])

for module in (instances, catalog, adding, settings):
    app.include_router(module.router)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"status": "ok"}


class SpaStaticFiles(StaticFiles):
    """Serve the SPA, falling back to index.html for client-side routes."""

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return FileResponse(Path(self.directory) / "index.html")
            raise


# Mounted last so it only catches paths not handled by the API or /healthz.
if STATIC_DIR.is_dir():
    app.mount("/", SpaStaticFiles(directory=STATIC_DIR, html=True), name="spa")
