"""Declarative per-series desired-state policy.

A series' policy says where it wants to live (``preferredTier``), the ordered tiers
to spill to (``fallbacks``, each optionally time-gated by ``afterDays`` for
"accept lower quality after N days"), and whether per-episode splitting across
tiers is allowed (``allowSplit``). Stored as JSON on ``series_intent.policy_json``;
when absent it's derived from the config fallback chain so existing setups keep
working with zero migration.
"""
from __future__ import annotations

from pydantic import BaseModel

from app.config import FallbackStep
from app.sonarr.registry import Registry


class PolicyStep(BaseModel):
    instanceId: str
    profile: str | None = None
    rootFolder: str | None = None
    afterDays: float = 0.0  # only spill to this step once an episode has waited this long


class SeriesPolicy(BaseModel):
    preferredTier: str
    fallbacks: list[PolicyStep] = []
    allowSplit: bool = True

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, raw: str) -> "SeriesPolicy":
        return cls.model_validate_json(raw)


def policy_from_chain(preferred_tier: str, chain: list[FallbackStep], *,
                      allow_split: bool = True) -> SeriesPolicy:
    """Build a policy from a config fallback chain — steps are immediate (afterDays=0)."""
    return SeriesPolicy(
        preferredTier=preferred_tier,
        fallbacks=[
            PolicyStep(instanceId=s.instanceId, profile=s.profile, rootFolder=s.root_folder)
            for s in chain
        ],
        allowSplit=allow_split,
    )


def effective_policy(registry: Registry, intent: dict,
                     defaults: dict | None = None) -> SeriesPolicy:
    """The policy in force for an intent: its stored JSON, else derived from the
    config chain with global defaults applied."""
    raw = intent.get("policy_json")
    if raw:
        return SeriesPolicy.from_json(raw)
    allow_split = (defaults or {}).get("allowSplit", True)
    return policy_from_chain(
        intent["chain_key"], registry.fallback_chain(intent["chain_key"]),
        allow_split=allow_split,
    )
