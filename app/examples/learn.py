"""The learning loop: turn replies Ray sent into new examples.

Every LEARN_INTERVAL_MINUTES (default 30) `learn_once` looks at the drafts that were posted on a
Slack card and have not been answered yet. For each it fetches the thread from Smartlead (READ
ONLY: this module has no way to send, categorise or change anything). If a SENT message now exists
after the prospect's first reply, that message is saved as a new example (source "ray_sent") with a
0 to 100 similarity score between our draft and what was sent. The next draft already uses it.

A SENT message that matches one of the campaign's sequence steps (fetched read only, compared
after cleaning, see `matches_step`) is an automatic follow-up, not Ray's answer, and is skipped.
Only a SENT message that matches no step counts. If the sequence cannot be fetched the draft is
left for the next run rather than guessed at.

Drafts older than MAX_AGE_DAYS are no longer checked. A thread that cannot be fetched or parsed is
skipped and tried again on the next run.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.db.client import (
    fetch_classifications,
    fetch_proposed_drafts,
    find_example_for_draft,
    get_thread,
    insert_example,
)
from app.db.records import ExampleSource, NewExample
from app.examples.library import parse_time
from app.examples.similarity import similarity
from app.smartlead.clean import clean_body
from app.smartlead.errors import ParseError, SmartleadError
from app.smartlead.models import Direction, Lead, Message
from app.smartlead.thread import parse_thread

logger = logging.getLogger(__name__)

MAX_AGE_DAYS = 14
STEP_MATCH = 80.0  # similarity (0 to 100) from which a SENT message counts as a sequence step
_VARIABLE = re.compile(r"\{\{[^{}]*\}\}")
_SPINTAX = re.compile(r"\{([^{}|]+(?:\|[^{}|]+)+)\}")


@dataclass(frozen=True)
class LearnResult:
    checked: int = 0
    learned: int = 0
    waiting: int = 0
    errors: int = 0
    skipped_steps: int = 0  # SENT messages skipped because they matched a sequence step


def step_texts(sequence: Any) -> list[str]:
    """The cleaned text of every sequence step and variant in a campaign sequence response."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            body = node.get("email_body")
            if isinstance(body, str) and body.strip():
                text = _SPINTAX.sub(lambda m: m.group(1).split("|")[0], _VARIABLE.sub(" ", body))
                found.append(clean_body(text))
            for value in node.values():
                if isinstance(value, dict | list):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(sequence)
    return [t for t in found if t.strip()]


def matches_step(text: str, steps: list[str]) -> bool:
    return any(similarity(text, step) >= STEP_MATCH for step in steps)


def sent_after_first_reply(
    messages: list[Message], steps: list[str] | None = None
) -> tuple[Message, Message | None, int] | None:
    """(first reply, Ray's answer or None, number of sequence steps skipped), or None.

    Ray's answer is the first SENT message after the first reply that matches no sequence step.
    """
    replies = sorted((m for m in messages if m.direction is Direction.REPLY), key=_when)
    if not replies:
        return None
    first = replies[0]
    later = sorted(
        (m for m in messages if m.direction is Direction.SENT and m.timestamp > first.timestamp),
        key=_when,
    )
    skipped = 0
    for message in later:
        if steps and matches_step(message.text, steps):
            skipped += 1
            continue
        return first, message, skipped
    return first, None, skipped


def _when(message: Message) -> datetime:
    return message.timestamp


def _vertical(deps: Any, thread: dict[str, Any]) -> str | None:
    resolver = getattr(deps, "verticals", None)
    return resolver.for_thread(thread) if resolver else (thread.get("vertical") or None)


def learn_from_draft(
    deps: Any, draft: dict[str, Any], sequence_for: Callable[[int], list[str]] | None = None
) -> tuple[bool, int]:
    """(a new example was saved, sequence steps skipped). Raises on a fetch or parse problem."""
    thread = get_thread(deps.db, draft["thread_id"])
    if thread is None:
        return False, 0
    lead = Lead(
        campaign_id=int(thread["email_campaign_id"]),
        lead_id=int(thread["email_lead_id"]),
        email=thread.get("lead_email"),
    )
    messages = parse_thread(deps.smartlead.get_thread_raw(lead))
    steps = sequence_for(lead.campaign_id) if sequence_for else []
    found = sent_after_first_reply(messages, steps)
    if found is None:
        return False, 0
    reply, sent, skipped = found
    if sent is None:
        return False, skipped
    classifications = fetch_classifications(deps.db, thread["id"])
    label = (classifications[-1] if classifications else {}).get("label")
    if not label or not sent.text.strip():
        return False, skipped
    shown = draft.get("final_text") or draft["text"]
    insert_example(
        deps.db,
        NewExample(
            source=ExampleSource.RAY_SENT,
            source_thread_id=thread["id"],
            label=label,
            prospect_reply=reply.text,
            gamic_reply=sent.text,
            campaign_name=str(thread.get("campaign_name") or ""),
            replied_at=reply.timestamp,
            thread_id=thread["id"],
            draft_id=draft["id"],
            draft_similarity=similarity(shown, sent.text),
            vertical=_vertical(deps, thread),
        ),
    )
    return True, skipped


def learn_once(deps: Any, now: datetime | None = None) -> LearnResult:
    """One pass over the drafts still waiting for Ray's reply. Never raises for one bad thread."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    checked = learned = waiting = errors = skipped_steps = 0
    sequences: dict[int, list[str]] = {}  # fetched once per campaign per run

    def sequence_for(campaign_id: int) -> list[str]:
        if campaign_id not in sequences:
            sequences[campaign_id] = step_texts(deps.smartlead.get_sequence(campaign_id))
        return sequences[campaign_id]

    for draft in fetch_proposed_drafts(deps.db):
        created = parse_time(draft.get("created_at"))
        if created is not None and created < cutoff:
            continue
        if find_example_for_draft(deps.db, draft["id"]) is not None:
            continue
        checked += 1
        try:
            saved, skipped = learn_from_draft(deps, draft, sequence_for)
            skipped_steps += skipped
            if saved:
                learned += 1
            else:
                waiting += 1
        except (SmartleadError, ParseError) as exc:
            errors += 1
            logger.warning(
                "learning loop: could not read thread for draft %s: %s", draft["id"], exc
            )
        except Exception as exc:  # noqa: BLE001 - one bad draft must not stop the others
            errors += 1
            logger.error("learning loop: draft %s failed: %s", draft["id"], type(exc).__name__)
    logger.info(
        "learning loop: %d checked, %d learned, %d waiting, %d errors, "
        "%d sent messages skipped as sequence steps",
        checked,
        learned,
        waiting,
        errors,
        skipped_steps,
    )
    return LearnResult(checked, learned, waiting, errors, skipped_steps)
