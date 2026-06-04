from app.config import load_config

RAW = """
instances:
  - id: "1080p"
    name: "Sonarr 1080p"
    url: "http://192.168.1.10:8989"
    api_key: "${SONARR_1080P_API_KEY}"
  - id: "4k"
    name: "Sonarr 4K"
    url: "http://192.168.1.10:8990/"
    api_key: "${SONARR_4K_API_KEY}"
fallback_chains:
  "4k":
    - instance: "1080p"
    - instance: "1080p"
      profile: "SD"
"""


def _write(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(RAW)
    return p


def test_substitutes_api_keys_from_env(tmp_path):
    cfg = load_config(
        _write(tmp_path),
        env={"SONARR_1080P_API_KEY": "key-1080", "SONARR_4K_API_KEY": "key-4k"},
    )
    by_id = {i.id: i for i in cfg.instances}
    assert by_id["1080p"].api_key == "key-1080"
    assert by_id["4k"].api_key == "key-4k"


def test_parses_instance_fields_and_fallback_chain(tmp_path):
    cfg = load_config(_write(tmp_path), env={"SONARR_1080P_API_KEY": "x", "SONARR_4K_API_KEY": "y"})
    by_id = {i.id: i for i in cfg.instances}
    assert by_id["1080p"].name == "Sonarr 1080p"
    assert by_id["1080p"].url == "http://192.168.1.10:8989"

    chain = cfg.fallback_chains["4k"]
    assert len(chain) == 2
    assert chain[0].instanceId == "1080p"
    assert chain[0].profile is None
    assert chain[1].instanceId == "1080p"
    assert chain[1].profile == "SD"


def test_missing_env_var_raises(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="SONARR_4K_API_KEY"):
        load_config(_write(tmp_path), env={"SONARR_1080P_API_KEY": "x"})
