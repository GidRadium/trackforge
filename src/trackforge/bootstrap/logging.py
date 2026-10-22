"""Structured logging with a redaction hook."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

_SECRET_VALUES: list[str] = []

_STANDARD_RECORD_ATTRIBUTES = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def register_secret(value: str) -> None:
    """Register a value that must be redacted from log output."""

    if value and value not in _SECRET_VALUES:
        _SECRET_VALUES.append(value)


def redact(value: str) -> str:
    """Replace every registered secret in ``value``."""

    redacted = value
    for secret in _SECRET_VALUES:
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    return value


class JsonlFormatter(logging.Formatter):
    """Render log records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": redact(record.getMessage()),
        }
        for key, value in vars(record).items():
            if key in _STANDARD_RECORD_ATTRIBUTES or key.startswith("_"):
                continue
            payload[key] = _redact_value(value)

        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = redact(self.formatStack(record.stack_info))

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    level: str = "INFO",
    *,
    stream: TextIO | None = None,
) -> None:
    """Configure process-wide structured logging without creating files."""

    normalized_level = level.upper()
    levels = logging.getLevelNamesMapping()
    if normalized_level not in levels:
        raise ValueError(f"unknown log level: {level}")

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(JsonlFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(levels[normalized_level])


__all__ = [
    "JsonlFormatter",
    "configure_logging",
    "redact",
    "register_secret",
]
