"""Structured logging (PART 36).

Emits one JSON object per log line with a stable field set, and applies
two mandatory filters before anything is written:

  * :func:`redact_secrets` — API keys, bearer tokens and query-string
    credentials become ``***``;
  * :func:`scrub_pii` — direct identifiers (long numeric IDs, e-mail
    addresses, phone numbers, ISO dates) in free-text query fields are
    replaced, and the text is truncated.

Never logged under any circumstance: the ``X-Clinical-Key`` header value,
``Authorization`` headers, upstream API keys, or full request bodies.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from app.settings import get_settings
from app.utils.helpers import redact_secrets, scrub_pii, utc_now_iso

__all__ = ["configure_logging", "get_logger", "log_event", "SecretRedactingFilter"]

# Fields LogRecord always carries; anything else is treated as structured
# context and merged into the JSON payload.
_STANDARD_ATTRS = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message", "asctime",
    }
)

_SENSITIVE_KEYS = frozenset(
    {
        "api_key", "apikey", "x-clinical-key", "clinical_api_key", "authorization",
        "token", "secret", "password", "ncbi_api_key", "key",
    }
)


class SecretRedactingFilter(logging.Filter):
    """Strip credentials from every message and every structured field."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact_secrets(record.msg)
            for key in list(record.__dict__):
                if key in _STANDARD_ATTRS:
                    continue
                if key.lower() in _SENSITIVE_KEYS:
                    record.__dict__[key] = "***"
                elif isinstance(record.__dict__[key], str):
                    record.__dict__[key] = redact_secrets(record.__dict__[key])
        except Exception:  # noqa: BLE001 - logging must never raise
            pass
        return True


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": utc_now_iso(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            payload[key] = value

        if record.exc_info:
            # Type + message only. Stack traces stay out of production logs
            # and never reach the client (PART 56, PART 77).
            exc_type, exc_value = record.exc_info[0], record.exc_info[1]
            payload["exception_type"] = getattr(exc_type, "__name__", str(exc_type))
            payload["exception_message"] = redact_secrets(str(exc_value))[:400]
            if not get_settings().is_production:
                payload["traceback"] = self.formatException(record.exc_info)[-4000:]

        return json.dumps(payload, ensure_ascii=False, default=str)


class PlainFormatter(logging.Formatter):
    """Human-readable formatter for local development."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
            datefmt="%H:%M:%S",
        )


def configure_logging() -> None:
    """Install handlers, formatter and the redaction filter."""
    settings = get_settings()
    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())

    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if settings.log_format.lower() == "json" else PlainFormatter()
    )
    handler.addFilter(SecretRedactingFilter())
    root.addHandler(handler)

    # Uvicorn's own loggers get the same treatment so access logs cannot
    # leak a key that appeared in a URL.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = [handler]
        uvicorn_logger.propagate = False

    # httpx logs full request URLs at INFO; that can include query params.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Emit a structured event with automatic query scrubbing."""
    settings = get_settings()
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        if key.lower() in _SENSITIVE_KEYS:
            continue
        if key in {"query", "search_term", "expanded_query", "text"} and isinstance(value, str):
            if not settings.log_query_text:
                continue
            safe[key] = scrub_pii(value, settings.log_query_max_chars)
        else:
            safe[key] = value
    logger.info(event, extra=safe)
