"""Classify a stored thread and post its Slack card, from rows already in the database (C2).

Usage:
    python scripts/reclassify_stored.py --thread-id <uuid> --dry-run
    python scripts/reclassify_stored.py --thread-id <uuid>

For a thread whose webhook event was stored before classification existed, or whose C2 step
failed. It reads the thread and messages from the database, runs the same step as the webhook
(rules, then the model, then the lane, the draft from past examples and the Slack card) and ADDS
a classification row and a
Slack card row. It deletes and changes nothing, and it never calls Smartlead: the Smartlead
client is replaced by a guard that raises if anything touches it. It does call the Anthropic API
and posts to Slack. Running it again does nothing once the thread has a card. Prints no email text.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db.client import (  # noqa: E402
    fetch_classifications,
    fetch_messages,
    fetch_slack_cards,
    get_thread,
)
from app.ingest.classify_step import StepError, classify_stored_thread  # noqa: E402
from app.ingest.deps import IngestDeps  # noqa: E402


class NoSmartlead:
    """Stands in for the Smartlead client: any use is a bug in this script."""

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"reclassify_stored must not call Smartlead (tried {name})")


def describe(deps: IngestDeps, thread_id: str) -> list[str]:
    """What is stored for the thread. Counts and ids only."""
    thread = get_thread(deps.db, thread_id)
    if thread is None:
        return [f"thread {thread_id}: not found"]
    messages = fetch_messages(deps.db, thread_id)
    replies = sum(1 for m in messages if m["direction"] == "REPLY")
    return [
        f"thread {thread_id[:8]}: {len(messages)} messages ({replies} reply), "
        f"status {thread.get('status')}",
        f"classifications: {len(fetch_classifications(deps.db, thread_id))}, "
        f"slack cards: {len(fetch_slack_cards(deps.db, thread_id))}",
    ]


def run(deps: IngestDeps, thread_id: str, dry_run: bool = False) -> list[str]:
    lines = describe(deps, thread_id)
    if lines[0].endswith("not found"):
        return lines
    if fetch_slack_cards(deps.db, thread_id):
        return [*lines, "already has a Slack card: nothing to do"]
    if dry_run:
        return [*lines, "dry run: would classify the first reply and post a card (no changes made)"]
    posted = classify_stored_thread(deps, thread_id)
    return [*lines, "classified and posted a card" if posted else "nothing posted"]


def build(settings: Any) -> IngestDeps:
    import anthropic
    from slack_sdk import WebClient

    from app.classify.classifier import Classifier
    from app.db.connect import create_db
    from app.examples.drafter import ExampleDrafter
    from app.examples.library import load_pool
    from app.ingest.deps import IngestConfig

    claude = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=5)
    db = create_db(settings)
    return IngestDeps(
        db=db,
        smartlead=NoSmartlead(),
        classifier=Classifier(claude, settings.CLASSIFIER_MODEL),
        slack=WebClient(token=settings.SLACK_BOT_TOKEN),
        example_drafter=ExampleDrafter(
            lambda label: load_pool(db, label),
            claude,
            settings.DRAFT_MODEL,
            settings.PRICE_SENTENCE,
        ),
        config=IngestConfig.from_settings(settings),
    )


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--thread-id", required=True)
    p.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    args = p.parse_args(argv)

    from app.config.settings import get_settings

    try:
        lines = run(build(get_settings()), args.thread_id, args.dry_run)
    except StepError as exc:
        print(f"stopped: {exc}")
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
