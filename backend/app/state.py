"""Application state: the shared Registry, loaded once from config.

The registry is attached to ``app.state`` at startup and exposed through the
``get_registry`` FastAPI dependency so routers never touch config directly (and
tests can override it).
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import Request

from app.config import load_config
from app.obs.journal import get_journal
from app.sonarr.registry import Registry

DEFAULT_CONFIG_PATH = "/config/config.yaml"
DEFAULT_DATA_PATH = "/data/relay.db"


def build_registry(config_path: str | Path | None = None) -> Registry:
    path = config_path or os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH)
    return Registry(load_config(path))


def data_path() -> str:
    """Where the SQLite DB lives — mirrors the CONFIG_PATH env pattern."""
    return os.environ.get("DATA_PATH", DEFAULT_DATA_PATH)


def get_registry(request: Request) -> Registry:
    return request.app.state.registry


def get_operations(request: Request):
    return request.app.state.operations


def get_db(request: Request):
    return request.app.state.db


def get_reconciler(request: Request):
    return request.app.state.reconciler


def get_event_journal(request: Request):
    """The app's journal; falls back to the process-wide one (e.g. in tests)."""
    return getattr(request.app.state, "journal", None) or get_journal()


def get_monitor(request: Request):
    """The always-on Monitor, or None when the app runs without one (tests)."""
    return getattr(request.app.state, "monitor", None)
