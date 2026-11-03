"""Read-only Smartlead smoke test: prints redacted response shapes.

Usage: python scripts/smartlead_smoke.py <campaign_id>

Makes only read calls (inbox replies fetch, message history, lead categories).
Writes redacted structures to tests/fixtures/real_shapes.json.
"""

import json
import sys
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from app.smartlead.client import SmartleadClient
from app.smartlead.errors import SmartleadError
from app.smartlead.models import Lead
from app.smartlead.redact import EMAIL, redact

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "real_shapes.json"
LEAD_ID_PATHS = [("email_lead_id",), ("email_lead_map_id",)]


def dig(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    for part in path:
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def find_lead_id(message: dict[str, Any]) -> tuple[tuple[str, ...], int] | None:
    for path in LEAD_ID_PATHS:
        value = dig(message, path)
        if value is not None and str(value).isdigit():
            return path, int(value)
    return None


def show(title: str, shape: Any) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(shape, indent=2))


def attempt(shapes: dict[str, Any], name: str, call: Any, **redact_opts: bool) -> Any:
    try:
        payload = call()
    except SmartleadError as exc:
        shapes[name] = {"error": EMAIL.sub("user@example.com", str(exc))[:200]}
        show(name, shapes[name])
        return None
    shapes[name] = redact(payload, for_file=True, **redact_opts)
    show(name, redact(payload, **redact_opts))
    return payload


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print(__doc__)
        return 2
    campaign_id = int(sys.argv[1])
    api_key = dotenv_values(".env").get("SMARTLEAD_API_KEY")
    if not api_key:
        print("SMARTLEAD_API_KEY missing from .env")
        return 2

    client = SmartleadClient(api_key)  # send_enabled stays False; no write is ever called
    shapes: dict[str, Any] = {}

    inbox = attempt(
        shapes,
        "inbox_replies_with_history",
        lambda: client.fetch_master_inbox_replies([campaign_id], limit=3, include_history=True),
    )
    messages = (inbox or {}).get("data") or []
    if messages:
        found = find_lead_id(messages[0])
        print("\nlead id source:", ".".join(found[0]) if found else "NOT FOUND")
        cid = messages[0].get("email_campaign_id") or campaign_id
        if found:
            lead = Lead(campaign_id=int(cid), lead_id=found[1])
            attempt(shapes, "message_history_route", lambda: client.get_thread_raw(lead))
    else:
        print("\nno replies returned; skipping message-history call")

    categories = attempt(shapes, "lead_categories", client.get_lead_categories, raw=True)
    if isinstance(categories, list):
        # Account configuration (ids and names only), not lead data.
        shapes["lead_categories_all"] = [
            {"id": c.get("id"), "name": c.get("name")} for c in categories
        ]
        print("\n=== all lead categories ===")
        for c in shapes["lead_categories_all"]:
            print(f"{c['id']}\t{c['name']}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(shapes, indent=2) + "\n", encoding="utf-8")
    print(f"\nsaved redacted shapes to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
