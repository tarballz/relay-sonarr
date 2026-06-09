"""Declarative per-series policy model, resolution, and defaults store."""
from app.db import Database
from app.policy import SeriesPolicy, effective_policy, policy_from_chain
from app.store import settings as settings_store
from tests.test_chain import make_registry


def test_policy_roundtrips_through_json():
    p = SeriesPolicy(
        preferredTier="4k",
        fallbacks=[{"instanceId": "1080p"}, {"instanceId": "1080p", "profile": "SD", "afterDays": 14}],
        allowSplit=False,
    )
    again = SeriesPolicy.from_json(p.to_json())
    assert again == p
    assert again.fallbacks[1].afterDays == 14
    assert again.allowSplit is False


def test_policy_from_chain_maps_fallback_steps():
    reg = make_registry()
    p = policy_from_chain("4k", reg.fallback_chain("4k"))
    assert p.preferredTier == "4k"
    assert [s.instanceId for s in p.fallbacks] == ["1080p", "1080p"]
    assert p.fallbacks[1].profile == "SD"
    assert all(s.afterDays == 0 for s in p.fallbacks)  # config chain = immediate
    assert p.allowSplit is True


def test_effective_policy_prefers_stored_json():
    reg = make_registry()
    stored = SeriesPolicy(preferredTier="4k", fallbacks=[], allowSplit=False)
    intent = {"tvdb_id": 1, "chain_key": "4k", "policy_json": stored.to_json()}
    assert effective_policy(reg, intent) == stored


def test_effective_policy_derives_from_chain_when_absent():
    reg = make_registry()
    intent = {"tvdb_id": 1, "chain_key": "4k", "policy_json": None}
    p = effective_policy(reg, intent)
    assert p.preferredTier == "4k"
    assert len(p.fallbacks) == 2


def test_effective_policy_applies_default_allow_split():
    reg = make_registry()
    intent = {"tvdb_id": 1, "chain_key": "4k", "policy_json": None}
    p = effective_policy(reg, intent, defaults={"allowSplit": False})
    assert p.allowSplit is False


async def test_defaults_store_roundtrip(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    assert await settings_store.get_defaults(db) == {}
    await settings_store.set_defaults(db, {"allowSplit": False, "escalateAfterDays": 14})
    assert await settings_store.get_defaults(db) == {"allowSplit": False, "escalateAfterDays": 14}
