"""Redact Smartlead payloads down to their structure, for printing and fixtures."""

import re
from typing import Any

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_ISO_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_ADDRESS_KEYS = {"from", "to", "cc", "bcc", "reply_to", "sent_from", "sent_to", "email"}
# Short enum-like fields whose values are what we need to see (never free text).
_KEEP_VALUE_KEYS = {"type", "direction", "email_status", "status", "seq_type"}
PREFIX = 25


def _redact_string(value: str, key: str, for_file: bool) -> str:
    if key in _ADDRESS_KEYS or EMAIL.fullmatch(value.strip()):
        return f"str[{len(value)}]: user@example.com"
    if "name" in key.lower():
        return f"str[{len(value)}]: Name"
    if key in _KEEP_VALUE_KEYS or _ISO_TIME.match(value):
        return f"str[{len(value)}]: {value}"
    if for_file:
        return f"str[{len(value)}]"
    prefix = EMAIL.sub("user@example.com", value)[:PREFIX]
    return f"str[{len(value)}]: {prefix}"


def redact(value: Any, key: str = "", *, for_file: bool = False, raw: bool = False) -> Any:
    """Return a structure-only copy of `value`.

    raw=True keeps scalar values as they are; use it only for account configuration such as
    the lead-category list, never for lead data.
    """
    if isinstance(value, dict):
        return {k: redact(v, str(k), for_file=for_file, raw=raw) for k, v in value.items()}
    if isinstance(value, list):
        items = [redact(v, key, for_file=for_file, raw=raw) for v in value[:2]]
        return {"_len": len(value), "_items": items}
    if raw:
        return value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return _redact_string(value, key, for_file)
    return type(value).__name__
