"""The draft validator (C3). A draft that fails goes to human_only; nothing here sends anything.

Blocking reasons (the draft is not shown for approval; a person handles the reply):
  label_not_draftable, template_not_for_label, confidence_below_draft_threshold,
  claim_not_usable:<id>, empty_slot:<NAME>, slot_not_allowed:<NAME>, number_not_allowed:<n>,
  link_not_allowed:<url>, too_long:<len>><max>, contradiction:<what>
Approve blockers (the draft is shown, but Approve is refused):
  placeholder:[NAME]   a visible placeholder is still in the text
Warnings come from a human edit: links, prices or numbers that are not in the template or the
approved facts, or a text over the length limit. An edit is never blocked, only flagged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.classify.routing import DRAFT_LABELS
from app.templates.facts import Facts
from app.templates.playbook import Template

LINK_RE = re.compile(r"https?://[^\s<>\"')\]]+|www\.[^\s<>\"')\]]+", re.IGNORECASE)
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
PLACEHOLDER_RE = re.compile(r"\[[A-Z][A-Z0-9 ]*\]")
NAME_EXTRA = set(" &'’.-,")
MAX_NAME_CHARS = 60

# Offering times (or the calendar link) contradicts a lead who asked something else or has
# already picked a time. A starting set: Aryan or Ray to confirm.
TIME_SLOT_NAMES = frozenset({"TIME_OPTIONS"})
CALENDAR_CLAIM_ID = "calendar_link"
NO_TIMES_FOR_LABELS = frozenset({"price_question", "objection_price", "booked_confirmation"})


@dataclass(frozen=True)
class ValidationContext:
    label: str
    confidence: float
    min_confidence: float
    reply: str
    outreach: str
    campaign_name: str
    facts: Facts


@dataclass(frozen=True)
class ValidationResult:
    blocking: list[str] = field(default_factory=list)
    approve_blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocking

    @property
    def can_approve(self) -> bool:
        return self.ok and not self.approve_blockers

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "blocking": self.blocking,
            "approve_blockers": self.approve_blockers,
            "warnings": self.warnings,
        }


def _clean(token: str) -> str:
    return token.strip().rstrip(".,;:!?)").lower()


# Times and dates a person types into a [TIME OPTIONS] placeholder are not claims.
TIME_RE = re.compile(
    r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm|a\.m\.|p\.m\.)(?:\s?(?:cst|cdt|est|edt|pst|pdt|utc|gmt))?"
    r"|\b\d{1,2}:\d{2}\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)\b",
    re.IGNORECASE,
)


def risky_tokens(text: str) -> tuple[set[str], set[str]]:
    """(links, numbers) found in text. Digits inside a link count as part of the link.

    Clock times and dates ("10am", "2:30 PM CST", "Nov 5", "3rd") are not counted as numbers.
    """
    links = {_clean(m) for m in LINK_RE.findall(text)}
    without_links = TIME_RE.sub(" ", LINK_RE.sub(" ", text))
    numbers = {n.strip(".,") for n in NUMBER_RE.findall(without_links)}
    return links, {n for n in numbers if n}


def allowed_tokens(
    template: Template, facts: Facts, campaign_name: str, all_facts: bool = False
) -> tuple[set[str], set[str]]:
    """Links and numbers that may appear: the template's own wording and approved claims."""
    texts = [template.fixed_text()]
    if all_facts:
        texts += [f.wording for f in facts.usable_facts(campaign_name)]
    else:
        texts += [
            facts.claims[c].wording for c in template.claims if facts.usable(c, campaign_name)
        ]
    links: set[str] = set()
    numbers: set[str] = set()
    for text in texts:
        found_links, found_numbers = risky_tokens(text)
        links |= found_links
        numbers |= found_numbers
    return links, numbers


def placeholders(text: str) -> list[str]:
    return sorted(set(PLACEHOLDER_RE.findall(text)))


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _slot_problems(template: Template, values: dict[str, str], ctx: ValidationContext) -> list[str]:
    """Empty slots and values outside the allowed set."""
    out: list[str] = []
    source = _norm(ctx.reply) + " " + _norm(ctx.outreach)
    for name, slot in template.slots.items():
        value = values.get(name, "").strip()
        if slot.source == "placeholder":
            continue  # a visible placeholder: an Approve blocker, not a block
        if not value:
            out.append(f"empty_slot:{name}")
            continue
        letters_ok = all(ch.isalpha() or ch in NAME_EXTRA for ch in value)
        if not letters_ok or len(value) > MAX_NAME_CHARS * (3 if slot.source == "model" else 1):
            out.append(f"slot_not_allowed:{name}")
        elif slot.source == "model":
            if len(value.split()) > (slot.max_words or 0) or _norm(value) not in source:
                out.append(f"slot_not_allowed:{name}")
        elif len(value.split()) > 4:
            out.append(f"slot_not_allowed:{name}")
    return out


def offers_times(template: Template) -> bool:
    return bool(TIME_SLOT_NAMES & set(template.slots)) or CALENDAR_CLAIM_ID in template.claims


def validate_draft(
    text: str, template: Template, values: dict[str, str], ctx: ValidationContext
) -> ValidationResult:
    """Every check for a freshly made draft. Any blocking reason sends it to human_only."""
    blocking: list[str] = []
    if ctx.label not in DRAFT_LABELS:
        blocking.append("label_not_draftable")
    if ctx.label not in template.labels:
        blocking.append("template_not_for_label")
    if ctx.confidence < ctx.min_confidence:
        blocking.append(
            f"confidence_below_draft_threshold:{ctx.confidence:.2f}<{ctx.min_confidence:.2f}"
        )
    for claim_id in template.claims:
        if not ctx.facts.usable(claim_id, ctx.campaign_name):
            blocking.append(f"claim_not_usable:{claim_id}")
    blocking += _slot_problems(template, values, ctx)

    links, numbers = risky_tokens(text)
    ok_links, ok_numbers = allowed_tokens(template, ctx.facts, ctx.campaign_name)
    blocking += [f"link_not_allowed:{link}" for link in sorted(links - ok_links)]
    blocking += [f"number_not_allowed:{n}" for n in sorted(numbers - ok_numbers)]
    if len(text) > template.max_chars:
        blocking.append(f"too_long:{len(text)}>{template.max_chars}")
    if ctx.label in NO_TIMES_FOR_LABELS and offers_times(template):
        blocking.append(f"contradiction:offers_times_for_{ctx.label}")
    return ValidationResult(
        blocking=blocking, approve_blockers=[f"placeholder:{p}" for p in placeholders(text)]
    )


def check_edited_text(text: str, template: Template, ctx: ValidationContext) -> ValidationResult:
    """A human edit: never blocked. Unapproved links, prices and numbers are warnings."""
    links, numbers = risky_tokens(text)
    ok_links, ok_numbers = allowed_tokens(template, ctx.facts, ctx.campaign_name, all_facts=True)
    warnings = [f"link_not_allowed:{link}" for link in sorted(links - ok_links)]
    warnings += [f"number_not_allowed:{n}" for n in sorted(numbers - ok_numbers)]
    if len(text) > template.max_chars:
        warnings.append(f"too_long:{len(text)}>{template.max_chars}")
    return ValidationResult(
        warnings=warnings, approve_blockers=[f"placeholder:{p}" for p in placeholders(text)]
    )


_EXPLAIN = {
    "placeholder": "Approve is blocked until {rest} is filled in (use Edit).",
    "label_not_draftable": "this label is not drafted",
    "template_not_for_label": "the template does not answer this label",
    "confidence_below_draft_threshold": "confidence is below the draft threshold ({rest})",
    "claim_not_usable": "the claim {rest} is not approved for this campaign",
    "empty_slot": "slot {rest} could not be filled",
    "slot_not_allowed": "slot {rest} has a value that is not allowed",
    "number_not_allowed": "the number {rest} is not in the template or the approved facts",
    "link_not_allowed": "the link {rest} is not in the template or the approved facts",
    "too_long": "the draft is longer than the template allows ({rest})",
    "contradiction": "the draft contradicts the lead's reply ({rest})",
}


def explain(code: str) -> str:
    """A reason code in plain words, for the Slack card."""
    kind, _, rest = code.partition(":")
    return _EXPLAIN.get(kind, code).format(rest=rest)
