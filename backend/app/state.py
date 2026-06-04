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
from app.sonarr.registry import Registry

DEFAULT_CONFIG_PATH = "/config/config.yaml"


def build_registry(config_path: str | Path | None = None) -> Registry:
    path = config_path or os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH)
    return Registry(load_config(path))


def get_registry(request: Request) -> Registry:
    return request.app.state.registry


def get_operations(request: Request):
    return request.app.state.operations
