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


RAW_WITH_DEFAULT_ROOT = """
instances:
  - id: "1080p"
    name: "Sonarr 1080p"
    url: "http://192.168.1.10:8989"
    api_key: "${SONARR_1080P_API_KEY}"
    default_root_folder: "/data2/TV/TV-1080p"
  - id: "4k"
    name: "Sonarr 4K"
    url: "http://192.168.1.10:8990"
    api_key: "${SONARR_4K_API_KEY}"
"""


def test_parses_optional_default_root_folder(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(RAW_WITH_DEFAULT_ROOT)
    cfg = load_config(p, env={"SONARR_1080P_API_KEY": "x", "SONARR_4K_API_KEY": "y"})
    by_id = {i.id: i for i in cfg.instances}
    assert by_id["1080p"].default_root_folder == "/data2/TV/TV-1080p"
    # Omitted entirely -> None, so behaviour is unchanged for instances that don't set it.
    assert by_id["4k"].default_root_folder is None


def test_default_root_folder_absent_is_none(tmp_path):
    cfg = load_config(_write(tmp_path), env={"SONARR_1080P_API_KEY": "x", "SONARR_4K_API_KEY": "y"})
    assert all(i.default_root_folder is None for i in cfg.instances)


RAW_WITH_DOWNLOAD_CLIENT = """
instances:
  - id: "1080p"
    name: "Sonarr 1080p"
    url: "http://192.168.1.10:8989"
    api_key: "${SONARR_1080P_API_KEY}"
download_client:
  type: transmission
  url: "http://192.168.1.10:9091/transmission/rpc"
  username: "${TRANSMISSION_USER}"
  password: "${TRANSMISSION_PASS}"
"""


def test_download_client_absent_is_none(tmp_path):
    """The block is optional: without it Relay behaves exactly as before."""
    cfg = load_config(_write(tmp_path), env={"SONARR_1080P_API_KEY": "x", "SONARR_4K_API_KEY": "y"})
    assert cfg.download_client is None


def test_parses_download_client_and_expands_credentials(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(RAW_WITH_DOWNLOAD_CLIENT)
    cfg = load_config(p, env={"SONARR_1080P_API_KEY": "x",
                              "TRANSMISSION_USER": "tu", "TRANSMISSION_PASS": "tp"})
    assert cfg.download_client.type == "transmission"
    assert cfg.download_client.url == "http://192.168.1.10:9091/transmission/rpc"
    assert cfg.download_client.username == "tu"
    assert cfg.download_client.password == "tp"


RAW_DOWNLOAD_CLIENT_NO_CREDS = """
instances:
  - id: "1080p"
    name: "Sonarr 1080p"
    url: "http://192.168.1.10:8989"
    api_key: "${SONARR_1080P_API_KEY}"
download_client:
  type: transmission
  url: "http://192.168.1.10:9091/transmission/rpc"
"""


def test_download_client_credentials_are_optional(tmp_path):
    """The live Transmission has no auth; absent keys must not trip _expand()."""
    p = tmp_path / "config.yaml"
    p.write_text(RAW_DOWNLOAD_CLIENT_NO_CREDS)
    cfg = load_config(p, env={"SONARR_1080P_API_KEY": "x"})
    assert cfg.download_client.username is None
    assert cfg.download_client.password is None
