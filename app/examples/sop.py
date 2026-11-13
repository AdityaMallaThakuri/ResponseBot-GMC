"""Reply templates from Ray's SOP file, as examples with the highest priority for their label.

The file is text or markdown: one template per section, the situation as the heading (a line that
starts with #, or a line ending with a colon followed by the template). The heading becomes the
"prospect reply" the library matches on, the section body is Gamic's reply word for word.

Which label a section belongs to: the heading `## label | situation` (for example
`## price_question | Price`), or the label in square brackets at the end of the heading, for
example `## They ask what it costs [price_question]`. Without either the heading is matched
on keywords (`KEYWORDS`); a heading that matches nothing is reported and not imported, never
guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from app.db.records import ExampleSource, NewExample

SOP_PRIORITY = 100
LABELS_YAML = Path(__file__).resolve().parent.parent / "config" / "labels.yaml"
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_TAG = re.compile(r"\[([a-z_]+)\]\s*$")

# Ordered: the first keyword group found in the heading wins.
KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("price_and_info", ("price and info", "pricing and info", "cost and how")),
    ("price_question", ("price", "pricing", "cost", "how much", "fee", "rates")),
    ("free_list_request", ("free list", "sample list", "free sample")),
    ("lead_magnet_request", ("lead magnet", "report", "case study")),
    ("trial_request", ("trial", "pilot", "test run")),
    ("meeting_request", ("call", "meeting", "book", "calendar", "time to talk")),
    ("booked_confirmation", ("confirm", "booked", "scheduled")),
    ("info_request", ("info", "how it works", "more details", "interested", "tell me more")),
    ("objection_price", ("too expensive", "budget", "objection price")),
    ("objection_info", ("objection", "already have", "in-house", "not sure")),
]


@dataclass(frozen=True)
class Section:
    heading: str
    body: str


def valid_labels() -> frozenset[str]:
    data = yaml.safe_load(LABELS_YAML.read_text(encoding="utf-8"))
    return frozenset(data["labels"])


def parse_sections(text: str) -> list[Section]:
    """Sections split on heading lines (# ...). Text before the first heading is ignored."""
    found: list[tuple[int, Section]] = []
    heading: str | None = None
    level = 0
    body: list[str] = []

    def close() -> None:
        if heading is not None and "".join(body).strip():
            found.append((level, Section(heading, "\n".join(body).strip())))

    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            close()
            level, heading, body = len(match.group(1)), match.group(2).strip(), []
        else:
            body.append(line)
    close()
    deepest = max((lvl for lvl, _ in found), default=0)
    # A file title (# ...) above the ## templates is not a template.
    return [s for lvl, s in found if lvl > 1 or deepest == 1]


def label_for(heading: str, labels: frozenset[str]) -> tuple[str | None, str]:
    """(label or None, the situation text without the [label] tag)."""
    head, bar, rest = heading.partition("|")
    if bar and head.strip() in labels:
        return head.strip(), rest.strip()
    tag = _TAG.search(heading)
    if tag:
        situation = heading[: tag.start()].strip()
        return (tag.group(1) if tag.group(1) in labels else None), situation
    lowered = heading.lower()
    for label, words in KEYWORDS:
        if label in labels and any(w in lowered for w in words):
            return label, heading
    return None, heading


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80]


def sop_examples(
    text: str, now: datetime | None = None, vertical: str = "ma"
) -> tuple[list[NewExample], list[str]]:
    """(examples, headings that matched no label). Each example has priority SOP_PRIORITY."""
    now = now or datetime.now(UTC)
    labels = valid_labels()
    examples: list[NewExample] = []
    unmatched: list[str] = []
    for section in parse_sections(text):
        label, situation = label_for(section.heading, labels)
        if label is None or not situation:
            unmatched.append(section.heading)
            continue
        examples.append(
            NewExample(
                source=ExampleSource.SOP,
                source_thread_id=f"{label}:{slug(situation)}",
                label=label,
                prospect_reply=situation,
                gamic_reply=section.body,
                campaign_name="",
                replied_at=now,
                sent_at=now,
                priority=SOP_PRIORITY,
                vertical=vertical,
            )
        )
    return examples, unmatched
