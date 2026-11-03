"""Synthetic templates and fakes for the C3 tests. No real wording and no customer text.

Every template here is clearly synthetic ("SYNTHETIC T1 ...") and says nothing a person would
send. They exist to exercise the engine, the validator and the Slack flow.
"""

from __future__ import annotations

import json
from typing import Any

from app.classify.classifier import load_config
from app.templates.facts import Facts, parse_facts
from app.templates.playbook import Playbook, parse_playbook

LABELS_CONFIG = load_config()

FACTS_DATA = {
    "campaign_types": [{"id": "lenders", "name_contains": "lender"}],
    "claims": [
        {
            "id": "calendar_link",
            "kind": "link",
            "wording": "https://calendar.example/synthetic",
            "campaign_types": ["all"],
            "approved": True,
        },
        {
            "id": "synthetic_result",
            "kind": "result",
            "wording": "SYNTHETIC RESULT 42%",
            "campaign_types": ["lenders"],
            "approved": True,
        },
        {
            "id": "unapproved_claim",
            "kind": "number",
            "wording": "SYNTHETIC 99",
            "campaign_types": ["all"],
            "approved": False,
        },
    ],
}

# template id -> labels it answers (the mapping in labels.yaml)
SERVES = {
    "T1": ["meeting_request"],
    "T2": ["info_request"],
    "T2a": ["objection_info"],
    "T2b": ["objection_info"],
    "T2c": ["objection_info"],
    "T3": ["free_list_request"],
    "T3a": ["free_list_request"],
    "T4": ["price_question"],
    "T4a": ["objection_price"],
    "T4b": ["objection_price"],
    "T5": ["price_and_info"],
    "T5b": ["price_and_info"],
    "T6": ["lead_magnet_request"],
    "T7": ["trial_request"],
    "T8": ["booked_confirmation"],
}


def template_data(template_id: str) -> dict[str, Any]:
    """A synthetic template. T1 has times; T2 has a model slot; T3 a company slot; T6 a claim."""
    data: dict[str, Any] = {
        "labels": SERVES[template_id],
        "when": f"synthetic: use {template_id}",
        "text": f"SYNTHETIC {template_id}: Hello {{NAME}}. Test wording only.",
        "slots": {"NAME": {"source": "first_name"}},
        "claims": [],
        "max_chars": 400,
    }
    if template_id == "T1":
        data["text"] += " {TIME_OPTIONS} Book here: {claim.calendar_link}"
        data["slots"]["TIME_OPTIONS"] = {"source": "placeholder"}
        data["claims"] = ["calendar_link"]
    if template_id == "T2":
        data["text"] += " Thanks for asking about {TOPIC}."
        data["slots"]["TOPIC"] = {"source": "model", "max_words": 4, "hint": "what they ask about"}
    if template_id == "T3":
        data["text"] += " Is {COMPANY} still focused on this? About 15 minutes."
        data["slots"]["COMPANY"] = {"source": "company"}
    if template_id == "T6":
        data["text"] += " Result: {claim.synthetic_result}"
        data["claims"] = ["synthetic_result"]
    return data


def make_facts(**overrides: Any) -> Facts:
    return parse_facts({**FACTS_DATA, **overrides})


def make_playbook(facts: Facts | None = None, templates: dict[str, Any] | None = None) -> Playbook:
    data = {
        "version": "synthetic-1",
        "templates": templates or {tid: template_data(tid) for tid in SERVES},
    }
    return parse_playbook(data, LABELS_CONFIG, facts or make_facts())


THREAD = {
    "id": "thread-1",
    "lead_first_name": "Pat",
    "lead_last_name": "Sample",
    "company": "Example Capital",
    "campaign_name": "GMC - Lenders Synthetic",
    "lead_email": "pat@example.com",
}
REPLY = "Thanks, tell me more about the origination service."
OUTREACH = "SYNTHETIC outreach: we help firms find deals."


class FakeModel:
    """Stands in for the Anthropic client. `answer(schema, user)` returns the JSON to reply with."""

    def __init__(self, answer: Any = None, error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        self.calls.append(kwargs)
        if self.error:
            raise self.error
        schema = kwargs["output_config"]["format"]["schema"]
        data = (
            self.answer(schema, kwargs["messages"][0]["content"])
            if callable(self.answer)
            else self.answer
        )
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(data))],
        )
