"""List the GMC campaigns and attach our EMAIL_REPLY webhook to them (only with --apply).

Usage:
    python scripts/attach_webhooks.py --base-url https://YOUR-APP.up.railway.app
        Read only. Prints the GMC campaigns and their POS REPLY children (found by
        parent_campaign_id, shown with the parent's name), which already have our webhook,
        and the GMC_CAMPAIGN_IDS value to set. Changes nothing.
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
    parent_name: str = ""


def is_gmc(name: str) -> bool:
    return name.strip().lower().startswith("gmc")


def has_client(campaign: dict[str, Any]) -> bool:
    return campaign.get("client_id") not in (None, "", 0)


def parent_id(campaign: dict[str, Any]) -> int | None:
    value = campaign.get("parent_campaign_id")
    return int(value) if value not in (None, "", 0) else None


def webhook_url(base_url: str, secret: str) -> str:
    return f"{base_url.rstrip('/')}{PATH}?secret={secret}"


def is_ours(hook: dict[str, Any], base_url: str) -> bool:
    """Our webhook: same host and path (the secret is ignored in the comparison)."""
    url = str(hook.get("webhook_url") or "")
    return url.split("?")[0].rstrip("/") == f"{base_url.rstrip('/')}{PATH}"


def gather(client: Any, base_url: str) -> list[CampaignRow]:
    """GMC campaigns plus their POS REPLY children.

    A GMC campaign: its name starts with "GMC". A POS REPLY child: its parent_campaign_id is one
    of those GMC campaigns (the name is not used). Any campaign with a client_id is excluded.
    """
    campaigns = [c for c in client.list_campaigns() if not has_client(c)]
    parents = {
        int(c["id"]): str(c.get("name") or "") for c in campaigns if is_gmc(c.get("name") or "")
    }
    rows = []
    for campaign in campaigns:
        cid = int(campaign["id"])
        pid = parent_id(campaign)
        if cid in parents:
            parent_name, pos_reply = "", False
        elif pid in parents:
            parent_name, pos_reply = parents[pid], True
        else:
            continue
        hooks = client.list_campaign_webhooks(cid)
        rows.append(
            CampaignRow(
                cid,
                str(campaign.get("name") or ""),
                str(campaign.get("status") or ""),
                pos_reply,
                any(is_ours(h, base_url) for h in hooks),
                parent_name,
            )
        )
    return sorted(rows, key=lambda r: (r.parent_name or r.name, r.pos_reply, r.id))


def show(rows: list[CampaignRow]) -> None:
    print(f"{len(rows)} GMC campaigns (including POS REPLY children):")
    for r in rows:
        tag = f"  [POS REPLY of: {r.parent_name}]" if r.pos_reply else ""
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
