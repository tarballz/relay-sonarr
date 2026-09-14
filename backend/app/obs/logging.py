"""Logging with correlation context: ``[tick=12 op=7 tvdb=81189]`` or JSON lines."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.obs import context

TEXT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s%(ctx)s"

_FIELDS = (("tick_id", "tick"), ("operation_id", "op"), ("tvdb_id", "tvdb"))


class ContextFilter(logging.Filter):
    """Copy the current correlation context onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = context.current()
        parts = []
        for key, short in _FIELDS:
            setattr(record, key, ctx[key])
            if ctx[key] is not None:
                parts.append(f"{short}={ctx[key]}")
        record.ctx = f" [{' '.join(parts)}]" if parts else ""
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, _short in _FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    """Install (or replace) Relay's root handler. Other handlers are left alone."""
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "_relay", False)]:
        root.removeHandler(handler)
    handler = logging.StreamHandler()
    handler._relay = True
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter() if fmt.lower() == "json" else logging.Formatter(TEXT_FORMAT))
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
