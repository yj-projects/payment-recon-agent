"""Small JSON logger with an allowlist of non-record metadata fields."""

from datetime import datetime, timezone
import json
import logging

LOGGER = logging.getLogger("payment_recon")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        for field in ("request_id", "method", "path", "status", "duration_ms", "error_type", "summary", "status_counts", "mode", "tool_name", "outcome"):
            if hasattr(record, field):
                event[field] = getattr(record, field)
        # Deliberately omit exception messages/tracebacks, headers, queries and bodies.
        return json.dumps(event, separators=(",", ":"))


def configure_logging() -> None:
    if not LOGGER.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
