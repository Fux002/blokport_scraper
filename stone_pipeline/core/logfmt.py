"""Structured JSON logging (section 13A.4).

Every line carries the run id, source, and stage where known, so a large run is
greppable. The module name is logfmt, not logging, to avoid shadowing stdlib.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("run_id", "source", "stage", "surrogate_key"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


_CONFIGURED = False


def configure(level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("stone_pipeline")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure()
    return logging.getLogger(f"stone_pipeline.{name}")


def bind(logger: logging.Logger, **context: Any) -> logging.LoggerAdapter:
    """Attach standing context (run_id, source, stage) to every line."""
    return logging.LoggerAdapter(logger, extra=context)
