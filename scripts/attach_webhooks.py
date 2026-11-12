"""List the GMC campaigns and attach our EMAIL_REPLY webhook to them (only with --apply).

Usage:
    python scripts/attach_webhooks.py --base-url https://YOUR-APP.up.railway.app
        Read only. Prints the GMC campaigns (and their "POS REPLY" children), which already have
        our webhook, and the GMC_CAMPAIGN_IDS value to set. Changes nothing.
    python scripts/attach_webhooks.py --base-url ... --apply
        Creates our webhook on each campaign that lacks it, after you typed the count to confirm.

Why per campaign: Smartlead also has an account-level ("user") webhook, but it overrides every
campaign-level webhook for the same event, which would silently break any other integration.

Safety: it only calls POST /webhook/create (never update or delete), skips a campaign that already
has our URL, never touches another webhook, and never prints the secret. It does not send email.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

WEBHOOK_NAME = "Reply Bot (EMAIL_REPLY)"
PATH = "/webhooks/smartlead"


@dataclass(frozen=True)
class CampaignRow:
    id: int
    name: str
    status: str
    pos_reply: bool
    has_ours: bool


def is_gmc(name: str) -> bool:
    return name.strip().lower().startswith("gmc")


def is_pos_reply(name: str) -> bool:
    return "pos reply" in name.lower()


def webhook_url(base_url: str, secret: str) -> str:
    return f"{base_url.rstrip('/')}{PATH}?secret={secret}"


def is_ours(hook: dict[str, Any], base_url: str) -> bool:
    """Our webhook: same host and path (the secret is ignored in the comparison)."""
    url = str(hook.get("webhook_url") or "")
    return url.split("?")[0].rstrip("/") == f"{base_url.rstrip('/')}{PATH}"


def gather(client: Any, base_url: str) -> list[CampaignRow]:
    rows = []
    for campaign in client.list_campaigns():
        name = str(campaign.get("name") or "")
        if not is_gmc(name):
            continue
        cid = int(campaign["id"])
        hooks = client.list_campaign_webhooks(cid)
        rows.append(
            CampaignRow(
                cid,
                name,
                str(campaign.get("status") or ""),
                is_pos_reply(name),
                any(is_ours(h, base_url) for h in hooks),
            )
        )
    return sorted(rows, key=lambda r: (r.name.lower(), r.id))


def show(rows: list[CampaignRow]) -> None:
    print(f"{len(rows)} GMC campaigns:")
    for r in rows:
        tag = "  [POS REPLY]" if r.pos_reply else ""
        ours = "webhook already attached" if r.has_ours else "NO webhook of ours"
        print(f"  {r.id:>9}  {r.status:<10} {r.name}{tag}  ({ours})")
    ids = ",".join(str(r.id) for r in rows)
    print(f"\nGMC_CAMPAIGN_IDS={ids}")


def apply(client: Any, rows: list[CampaignRow], url: str) -> int:
    done = 0
    for r in rows:
        if r.has_ours:
            continue
        client.create_campaign_webhook(r.id, WEBHOOK_NAME, url)
        done += 1
        print(f"  attached to {r.id} {r.name}")
    return done


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="the deployed app's public URL")
    parser.add_argument("--apply", action="store_true", help="really create the webhooks")
    args = parser.parse_args()

    from app.config.settings import get_settings
    from app.smartlead.client import SmartleadClient

    settings = get_settings()
    client = SmartleadClient(settings.SMARTLEAD_API_KEY)  # read and webhook-create only
    rows = gather(client, args.base_url)
    show(rows)
    todo = [r for r in rows if not r.has_ours]
    print(f"\nTarget URL: {args.base_url.rstrip('/')}{PATH}?secret=*** (EMAIL_REPLY only)")
    if not args.apply:
        print(f"Dry run: {len(todo)} would get the webhook. Re-run with --apply to attach.")
        return
    if not todo:
        print("Nothing to attach.")
        return
    answer = input(f"Type {len(todo)} to attach to {len(todo)} campaigns: ").strip()
    if answer != str(len(todo)):
        print("Not confirmed. Nothing changed.")
        return
    n = apply(client, rows, webhook_url(args.base_url, settings.WEBHOOK_SECRET))
    print(f"Attached {n}. Set GMC_CAMPAIGN_IDS in Railway to the value printed above.")


if __name__ == "__main__":
    main()
