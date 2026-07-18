"""Load and validate the unified-dashboard configuration.

Config lives in a YAML file; secrets (API keys) are referenced as ``${ENV_VAR}``
placeholders and substituted from the environment at load time so keys never sit
in the file or the image.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


class InstanceConfig(BaseModel):
    id: str
    name: str
    url: str
    api_key: str
    # Root folder to use when a request doesn't name one. Without it the code
    # falls back to whichever root folder Sonarr happens to list first, which is
    # the oldest one — wrong once storage spans more than one pool.
    default_root_folder: str | None = None


class FallbackStep(BaseModel):
    """One attempt in a fallback chain.

    ``instanceId`` is which Sonarr to try. ``profile``/``root_folder`` are optional
    overrides resolved by name on that instance at runtime; when omitted the
    instance's first profile/root folder is used. Consecutive steps on the *same*
    instance represent a quality-profile swap (e.g. 1080p HD → 1080p SD) rather
    than a move between instances.
    """

    instanceId: str
    profile: str | None = None
    root_folder: str | None = None


class Config(BaseModel):
    instances: list[InstanceConfig]
    # Ordered fallback steps keyed by the *starting* instance id.
    fallback_chains: dict[str, list[FallbackStep]] = {}


def _expand(value: str, env: dict[str, str]) -> str:
    def repl(match: re.Match) -> str:
        name = match.group(1)
        if name not in env:
            raise ValueError(f"Missing environment variable: {name}")
        return env[name]

    return _ENV_PATTERN.sub(repl, value)


def load_config(path: str | Path, env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ) if env is None else env
    raw = yaml.safe_load(Path(path).read_text())

    instances = []
    for item in raw.get("instances", []):
        instances.append(
            InstanceConfig(
                id=item["id"],
                name=item["name"],
                url=_expand(item["url"], env).rstrip("/"),
                api_key=_expand(item["api_key"], env),
                default_root_folder=item.get("default_root_folder"),
            )
        )
    chains: dict[str, list[FallbackStep]] = {}
    for start_id, steps in (raw.get("fallback_chains") or {}).items():
        chains[start_id] = [
            FallbackStep(
                instanceId=step["instance"],
                profile=step.get("profile"),
                root_folder=step.get("rootFolder"),
            )
            for step in steps
        ]
    return Config(instances=instances, fallback_chains=chains)
