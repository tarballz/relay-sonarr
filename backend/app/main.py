"""FastAPI application: API routers + serving the built React SPA (single origin)."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from app.api import adding, catalog, instances, policy, settings
from app.auth import verify_access
from app.db import Database
from app.reconciler import Reconciler
from app.state import build_registry, data_path, get_reconciler
from app.store import settings as settings_store
from app.store.operations import OperationStore

STATIC_DIR = Path(__file__).parent / "static"


def _reconciler_enabled() -> bool:
    return os.environ.get("RECONCILER_ENABLED", "true").lower() != "false"


def _configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    enabled = _reconciler_enabled()
    try:
        app.state.registry = build_registry()
    except (ValueError, FileNotFoundError, KeyError) as exc:
        # Fail loudly and actionably rather than with a cryptic traceback when a
        # required API-key env var or config field is missing.
        logging.getLogger("app").error("Configuration error: %s", exc)
        raise RuntimeError(
            f"Relay failed to start — {exc}. Check config.yaml and that the "
            f"referenced API-key env vars (e.g. SONARR_*_API_KEY) are set in .env."
        ) from exc
    app.state.db = Database(data_path())
    app.state.operations = OperationStore(app.state.db)
    # Apply any UI-saved fallback-chain override on top of config.yaml.
    try:
        override = await settings_store.get_chain_overrides(app.state.db)
        if override:
            app.state.registry.set_chains(app.state.registry.coerce_chains(override))
            logging.getLogger("app").info("applied fallback-chain override from DB")
    except Exception:  # noqa: BLE001 - a bad override must not block startup
        logging.getLogger("app").exception("failed to apply fallback-chain override")
    app.state.reconciler = Reconciler(
        app.state.registry, app.state.db, app.state.operations, enabled=enabled
    )
    task = None
    if enabled:
        task = asyncio.create_task(app.state.reconciler.run())
    else:
        logging.getLogger("app").info("reconciler disabled (RECONCILER_ENABLED=false)")
    try:
        yield
    finally:
        if task is not None:
            app.state.reconciler.stop()
            await task
        app.state.db.close()


# Cf-Access verification is a no-op unless CF_ACCESS_ENABLED=true (see auth.py).
app = FastAPI(title="Unified Sonarr Dashboard", lifespan=lifespan,
              dependencies=[Depends(verify_access)])

for module in (instances, catalog, adding, settings, policy):
    app.include_router(module.router)


@app.get("/healthz", include_in_schema=False)
async def healthz(response: Response, rec=Depends(get_reconciler)):
    """Liveness probe: 503 when the autonomous loop is enabled but has gone stale,
    so Docker/orchestrators can detect a wedged reconciler (not just a live port)."""
    status = rec.status()
    if not status["healthy"]:
        response.status_code = 503
    return status


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
