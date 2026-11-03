from datetime import datetime
from typing import Any

from app.smartlead.clean import clean_body
from app.smartlead.errors import ParseError
from app.smartlead.models import Direction, Message

# Smartlead wraps the same message entries under a different key per route:
# GET .../message-history -> "history"; master-inbox item -> "email_history".
_HISTORY_KEYS = ("history", "email_history")


def _entries(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in _HISTORY_KEYS:
            if isinstance(payload.get(key), list):
                return payload[key]
    raise ParseError(f"message history payload has no {' or '.join(_HISTORY_KEYS)} list")


def _field(entry: dict[str, Any], key: str) -> Any:
    if key not in entry or entry[key] is None:
        raise ParseError(f"message entry is missing {key!r}")
    return entry[key]


def _text_field(entry: dict[str, Any], key: str) -> str:
    """A required identifier: present, a string, and not blank."""
    value = _field(entry, key)
    if not isinstance(value, str) or not value.strip():
        raise ParseError(f"message entry has a blank or non-text {key!r}")
    return value


def _direction(entry: dict[str, Any]) -> Direction:
    raw = _field(entry, "type")
    try:
        return Direction(raw)
    except ValueError:
        raise ParseError(f"unknown message type: {raw!r}") from None


def _timestamp(entry: dict[str, Any]) -> datetime:
    raw = _field(entry, "time")
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ParseError(f"unparseable message time: {raw!r}") from exc


def parse_thread(payload: Any) -> list[Message]:
    """Turn a Smartlead history payload into cleaned plain-text messages, oldest first.

    Accepts the message-history route response or a master-inbox item. Any entry that is
    not exactly the recorded shape raises ParseError, so the thread goes to a human.
    """
    entries = _entries(payload)
    messages = [
        Message(
            direction=_direction(entry),
            timestamp=_timestamp(entry),
            text=clean_body(str(_field(entry, "email_body"))),
            stats_id=str(_field(entry, "stats_id")),
            message_id=_text_field(entry, "message_id"),
            from_addr=_text_field(entry, "from"),
            to_addr=_text_field(entry, "to"),
        )
        for entry in entries
    ]
    return sorted(messages, key=lambda m: m.timestamp)


def latest_reply_stats_id(messages: list[Message]) -> str | None:
    """stats_id of the most recent lead reply: the message a reply should answer."""
    replies = [m for m in messages if m.direction is Direction.REPLY]
    return replies[-1].stats_id if replies else None
