"""Log records carry tick/operation/series context; optional JSON output."""
import io
import json
import logging

from app.obs import context
from app.obs.logging import ContextFilter, JsonFormatter, configure_logging


def _logger(formatter):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(ContextFilter())
    handler.setFormatter(formatter)
    log = logging.getLogger("test.obs.logging")
    log.handlers = [handler]
    log.propagate = False
    log.setLevel(logging.DEBUG)
    return log, stream


def test_text_format_appends_context_only_when_bound():
    log, stream = _logger(logging.Formatter("%(message)s%(ctx)s"))
    log.info("plain")
    with context.bind(tick_id=12, tvdb_id=81189):
        log.info("inside")
    assert stream.getvalue().splitlines() == ["plain", "inside [tick=12 tvdb=81189]"]


def test_json_formatter_includes_context_and_exception():
    log, stream = _logger(JsonFormatter())
    with context.bind(tick_id=3, operation_id=4):
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("failed")
    rec = json.loads(stream.getvalue())
    assert rec["level"] == "ERROR"
    assert rec["msg"] == "failed"
    assert rec["logger"] == "test.obs.logging"
    assert rec["tick_id"] == 3 and rec["operation_id"] == 4
    assert "tvdb_id" not in rec
    assert "ValueError: boom" in rec["exc"]


def test_configure_logging_replaces_only_its_own_handler():
    root = logging.getLogger()
    before_handlers = list(root.handlers)
    before_level = root.level
    try:
        configure_logging("DEBUG", "json")
        configure_logging("INFO", "text")
        ours = [h for h in root.handlers if getattr(h, "_relay", False)]
        assert len(ours) == 1
        assert not isinstance(ours[0].formatter, JsonFormatter)
        assert root.level == logging.INFO
        assert all(h in root.handlers for h in before_handlers)
    finally:
        for h in [h for h in root.handlers if getattr(h, "_relay", False)]:
            root.removeHandler(h)
        root.setLevel(before_level)
