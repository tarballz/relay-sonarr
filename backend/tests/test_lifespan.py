"""The app boots with journal, monitor and observability routes wired, and shuts down cleanly."""
import logging
import textwrap

import pytest
from starlette.testclient import TestClient

from app.main import app
from app.obs.journal import NullJournal, get_journal


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """This is the only test that drives the real lifespan, so it's the only one that
    triggers `_configure_logging()`. That installs a root-logger handler nothing ever
    tears down (by design — the process just exits in production); left in place it
    corrupts handler-identity assertions in tests/test_logging.py that run after this
    one. Undo it, the same way conftest's `_reset_journal` undoes the process-wide
    journal set by the same lifespan run."""
    root = logging.getLogger()
    before_handlers = list(root.handlers)
    before_level = root.level
    yield
    for handler in [h for h in root.handlers if h not in before_handlers]:
        root.removeHandler(handler)
    root.setLevel(before_level)


def test_app_lifespan_wires_observability(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(textwrap.dedent("""
        instances:
          - id: "1080p"
            name: "Sonarr 1080p"
            url: "http://127.0.0.1:9"
            api_key: "${TEST_SONARR_KEY}"
    """))
    monkeypatch.setenv("CONFIG_PATH", str(config))
    monkeypatch.setenv("DATA_PATH", str(tmp_path / "relay.db"))
    monkeypatch.setenv("TEST_SONARR_KEY", "k")
    monkeypatch.setenv("RECONCILER_ENABLED", "false")

    with TestClient(app) as client:
        assert not isinstance(get_journal(), NullJournal)
        assert client.get("/healthz").status_code == 200
        assert "# TYPE relay_sonarr_up gauge" in client.get("/metrics").text
        summary = client.get("/api/summary").json()
        assert [i["id"] for i in summary["instances"]] == ["1080p"]

    assert isinstance(get_journal(), NullJournal)
    assert app.state.journal is None and app.state.monitor is None
