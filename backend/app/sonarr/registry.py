"""In-memory registry of configured Sonarr instances.

Turns the parsed Config into ready-to-use SonarrClient objects keyed by id, and
records the smart-add fallback pairing. Adding a third instance (or a Radarr
later) is a config edit, not a code change — everything downstream iterates the
registry rather than naming instances directly.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import Config, FallbackStep
from app.sonarr.client import SonarrClient


@dataclass
class Instance:
    id: str
    name: str
    url: str
    client: SonarrClient


class Registry:
    def __init__(self, config: Config):
        self._instances: dict[str, Instance] = {}
        for cfg in config.instances:
            self._instances[cfg.id] = Instance(
                id=cfg.id,
                name=cfg.name,
                url=cfg.url,
                client=SonarrClient(base_url=cfg.url, api_key=cfg.api_key),
            )
        self._chains = dict(config.fallback_chains)

    def get(self, instance_id: str) -> Instance:
        if instance_id not in self._instances:
            raise KeyError(f"Unknown instance: {instance_id}")
        return self._instances[instance_id]

    def all(self) -> list[Instance]:
        return list(self._instances.values())

    def fallback_chain(self, instance_id: str) -> list[FallbackStep]:
        """Ordered fallback steps for an instance (empty if none configured)."""
        return self._chains.get(instance_id, [])

    def has_fallback(self, instance_id: str) -> bool:
        return bool(self._chains.get(instance_id))
