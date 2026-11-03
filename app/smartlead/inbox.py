from typing import Any

from app.smartlead.errors import ParseError
from app.smartlead.models import Lead


def inbox_items(payload: Any) -> list[dict[str, Any]]:
    """Items of a master-inbox response: {"ok": true, "data": [...], "offset", "limit"}."""
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise ParseError("inbox response is not an ok payload")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ParseError("inbox response has no data list")
    return data


def lead_from_inbox_item(item: dict[str, Any]) -> Lead:
    """Campaign id, lead id and email of an inbox item (ids arrive as strings)."""
    try:
        return Lead(
            campaign_id=int(item["email_campaign_id"]),
            lead_id=int(item["email_lead_id"]),
            email=item["lead_email"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ParseError(f"inbox item is missing a usable id field: {exc!r}") from exc
