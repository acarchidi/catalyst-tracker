"""Structured logging: one JSON object per line on standard error."""

import json
import logging
import sys
from datetime import UTC, datetime

_FIELDS = "catalyst_fields"


class JsonFormatter(logging.Formatter):
    """Format a log record as a single JSON line with its structured fields."""

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as JSON: time, level, logger, event, then any fields."""
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        fields: dict[str, object] = getattr(record, _FIELDS, {})
        payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Send all `catalyst` logs to standard error as JSON lines."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("catalyst")
    root.handlers = [handler]
    root.setLevel(level)
    root.propagate = False


def log_event(logger: logging.Logger, event: str, **fields: object) -> None:
    """Log an info-level event with structured fields. Never pass a secret as a field."""
    logger.info(event, extra={_FIELDS: fields})


def log_alert(logger: logging.Logger, event: str, **fields: object) -> None:
    """Log an error-level event that needs the owner's attention. Never pass a secret as a field."""
    logger.error(event, extra={_FIELDS: fields})
