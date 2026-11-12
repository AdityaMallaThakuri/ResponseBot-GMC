"""Attach or detach OUR EMAIL_REPLY webhook on the watched campaigns (manual version of the job).

Usage (every mode is a dry run unless --apply is given):
    python scripts/attach_webhooks.py                      # list watched campaigns, who lacks ours
    python scripts/attach_webhooks.py --apply              # attach ours where missing (asks first)
    python scripts/attach_webhooks.py --detach-ours        # list OUR webhooks that would be removed
    python scripts/attach_webhooks.py --detach-ours --apply
    --base-url https://abc.ngrok-free.app   overrides PUBLIC_BASE_URL (which webhook is "ours")
    --campaign-id 123 [--campaign-id 456]     limit --detach-ours to these campaigns

Watched campaigns follow the same rule as the app (app/smartlead/watch.py): name starts with
"GMC" and no client_id, or a child by parent_campaign_id; IGNORE_CAMPAIGN_IDS removed; if
GMC_CAMPAIGN_IDS is set only those. Why per campaign and not the account-level Smartlead webhook:
that one overrides every campaign webhook and could break other integrations.

Safety: attach only calls POST /webhook/create. Detach deletes only a webhook whose URL has our
host and path (the base URL plus /webhooks/smartlead), by its own id, never any other webhook.
The secret is never printed. Nothing here sends email.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.smartlead.watch import (  # noqa: E402
    PATH,
    WEBHOOK_NAME,
    Watched,
    is_ours,
    watched_campaigns,
    webhook_url,
)


@dataclass(frozen=True)
class CampaignRow:
    campaign: Watched
    has_ours: bool

    @property
    def id(self) -> int:
        return self.campaign.id


def gather(
    client: Any, base_url: str, override: frozenset[int], ignore: frozenset[int]
) -> list[CampaignRow]:
    campaigns = client.list_campaigns()
    return [
        CampaignRow(w, any(is_ours(h, base_url) for h in client.list_campaign_webhooks(w.id)))
        for w in watched_campaigns(campaigns, override, ignore)
    ]


def show(rows: list[CampaignRow]) -> None:
    print(f"{len(rows)} watched campaigns (including POS REPLY children):")
    for r in rows:
        w = r.campaign
        tag = f"  [POS REPLY of: {w.parent_name}]" if w.parent_name else ""
        ours = "webhook already attached" if r.has_ours else "NO webhook of ours"
        print(f"  {w.id:>9}  {w.status:<10} {w.name}{tag}  ({ours})")


def apply(client: Any, rows: list[CampaignRow], url: str) -> int:
    done = 0
    for r in rows:
        if r.has_ours:
            continue
        client.create_campaign_webhook(r.id, WEBHOOK_NAME, url)
        done += 1
        print(f"  attached to {r.id} {r.campaign.name}")
    return done


def find_ours(
    client: Any, campaign_ids: list[int], base_url: str
) -> list[tuple[int, dict[str, Any]]]:
    """(campaign id, webhook) for every webhook of ours on these campaigns. Read only."""
    found = []
    for cid in campaign_ids:
        for hook in client.list_campaign_webhooks(cid):
            if is_ours(hook, base_url) and hook.get("id") is not None:
                found.append((cid, hook))
    return found


def detach(client: Any, found: list[tuple[int, dict[str, Any]]], base_url: str) -> int:
    """Delete only webhooks of ours, by id. Re-checks each one right before deleting it."""
    done = 0
    for cid, hook in found:
        if not is_ours(hook, base_url):  # never delete anything else
            continue
        client.delete_webhook(int(hook["id"]))
        done += 1
        print(f"  removed our webhook {hook['id']} from campaign {cid}")
    return done


def confirm(count: int, what: str) -> bool:
    answer = input(f"Type {count} to {what}: ").strip()
    if answer != str(count):
        print("Not confirmed. Nothing changed.")
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="overrides PUBLIC_BASE_URL")
    parser.add_argument("--apply", action="store_true", help="really change things")
    parser.add_argument("--detach-ours", action="store_true", help="remove OUR webhook only")
    parser.add_argument("--campaign-id", type=int, action="append", default=[])
    args = parser.parse_args()

    from app.config.settings import get_settings
    from app.smartlead.client import SmartleadClient

    settings = get_settings()
    base = (args.base_url or settings.PUBLIC_BASE_URL).strip()
    if not base:
        sys.exit("Give --base-url or set PUBLIC_BASE_URL.")
    client = SmartleadClient(settings.SMARTLEAD_API_KEY)
    override, ignore = settings.gmc_campaign_ids, settings.ignore_campaign_ids
    print(f"Our webhook URL: {base.rstrip('/')}{PATH}?secret=***")

    if args.detach_ours:
        if args.campaign_id:
            ids = args.campaign_id
        else:
            ids = [w.id for w in watched_campaigns(client.list_campaigns(), override, ignore)]
        found = find_ours(client, ids, base)
        print(f"Scanned {len(ids)} campaigns: {len(found)} webhooks of ours:")
        for cid, hook in found:
            print(f"  campaign {cid}: webhook {hook['id']} ({hook.get('name') or 'no name'})")
        if not args.apply:
            print("Dry run: nothing removed. Re-run with --apply to remove them.")
            return
        if found and confirm(len(found), "remove them"):
            print(f"Removed {detach(client, found, base)}.")
        return

    rows = gather(client, base, override, ignore)
    show(rows)
    todo = [r for r in rows if not r.has_ours]
    if not args.apply:
        print(f"\nDry run: {len(todo)} would get the webhook. Re-run with --apply to attach.")
        return
    if todo and confirm(len(todo), f"attach to {len(todo)} campaigns"):
        n = apply(client, rows, webhook_url(base, settings.WEBHOOK_SECRET))
        print(f"Attached {n}.")


if __name__ == "__main__":
    main()
