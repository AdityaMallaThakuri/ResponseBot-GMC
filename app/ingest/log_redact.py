"""Keep the webhook secret out of logs when it arrives as a query parameter (?secret=...).

uvicorn's access log prints the whole request line, query string included, so a filter on that
logger replaces the value before any handler sees it.
"""

from __future__ import annotations

import logging
import re

QUERY_SECRET_PARAM = "secret"
REDACTED = "[redacted]"
_PATTERN = re.compile(rf"([?&]{QUERY_SECRET_PARAM}=)[^&#\s\"']*", re.IGNORECASE)


def redact(text: str) -> str:
    return _PATTERN.sub(rf"\g<1>{REDACTED}", text)


class RedactQuerySecret(logging.Filter):
    """Rewrite `secret=<value>` in a record's message and arguments. Never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()
            }
        return True


_LOGGERS = ("uvicorn.access", "uvicorn.error", "uvicorn")


def install_redaction() -> None:
    """Attach the filter once to uvicorn's loggers. Safe to call again."""
    for name in _LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactQuerySecret) for f in logger.filters):
            logger.addFilter(RedactQuerySecret())
