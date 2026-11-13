"""SOP templates in drafting: objections, the M&A price sentence, and numbers inside a template."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from app.classify.classifier import Classification
from app.examples.drafter import (
    OBJECTION_NO_DRAFT,
    ExampleDrafter,
    sentence_in,
)
from app.examples.library import Example
from app.ingest.draft_step import _prepare_from_examples
from tests.c3_support import FakeModel
from tests.test_examples import FILLER

NOW = datetime(2026, 11, 13, tzinfo=UTC)
HYBRID = "But to give you an idea we work on a hybrid model of retainer + success fee."
PRICE_SOP = (
    "Hey [NAME],\n\nPricing depends.. I would like to learn more about your markets.\n\n"
    f"{HYBRID} The fastest way to get exact numbers is a quick call.\n\n"
    "Would [TIME OPTIONS] CST work?\n\nIf not, just grab a time here. [LINK]"
)
OBJ_PRICE_SOP = (
    "Hey [NAME],\n\nI get that. Most M&A firms are paying $20k–$50k+ for a full-time hire.\n\n"
    "At 10-20% of the advisory fee, you pay us if we perform.\n\n"
    "Would [TIME OPTIONS] work?\n\nIf those don't work, just grab a time here. [LINK]"
)
OBJ_INFO_SOP = (
    "Hey [NAME],\n\nThat's great, we complement your broker network.\n\nWould [TIME OPTIONS] work?"
)
TRIAL_SOP = (
    "Hey [NAME],\n\nI'd need 15-20 minutes to get your deal box.\n\nWould [TIME OPTIONS] work?"
)


def sop(label: str, situation: str, body: str, vertical: str = "ma") -> Example:
    return Example(
        "sop", f"{label}:{situation[:8]}", "", label, situation, body, NOW, NOW, 100, vertical
    )


def hist(n: int, label: str, body: str, vertical: str = "ma") -> Example:
    return Example(
        "history", f"h{n}", "GMC - M&A May", label, f"q{n}", body, NOW, None, 0, vertical
    )


POOL = [
    sop("objection_price", "That's more than we expected", OBJ_PRICE_SOP),
    sop("objection_info", "We already have a broker network", OBJ_INFO_SOP),
    sop("price_question", "Price", PRICE_SOP),
    sop("trial_request", "2 week free trial", TRIAL_SOP),
    hist(1, "price_question", "Hey, it is a flat fee of $2.5k-$10k. " + FILLER),
    hist(2, "info_request", "Hey, happy to explain. " + FILLER),
]


def model(match: int = 1, reply: str = "Hey Pat, thanks.") -> FakeModel:
    def answer(schema: dict[str, Any], user: str) -> dict[str, Any]:
        return {"template": match} if "template" in schema["properties"] else {"reply": reply}

    return FakeModel(answer=answer)


def make(fake: FakeModel, price_sentence: str = "") -> ExampleDrafter:
    return ExampleDrafter(
        lambda label, vertical="": POOL,
        fake,
        "m-x",
        price_sentence,
        vertical_of=lambda thread: str(thread.get("vertical") or ""),
    )


def draft(d: ExampleDrafter, label: str, reply: str = "too expensive", vertical: str = "ma"):
    return d.draft(
        thread={"campaign_name": "GMC - M&A May", "lead_first_name": "Pat", "vertical": vertical},
        label=label,
        confidence=0.95,
        reply=reply,
        outreach="Hi Pat",
        min_confidence=0.75,
    )


def test_a_matching_sop_objection_template_is_the_first_example_and_its_numbers_stay() -> None:
    fake = model(match=1, reply=OBJ_PRICE_SOP.replace("[NAME]", "Pat"))
    out = draft(make(fake), "objection_price")
    assert out.kind == "drafted"
    assert out.examples[0].gamic_reply == OBJ_PRICE_SOP
    assert "$20k–$50k+" in out.text and "10-20%" in out.text
    assert out.removed == 0
    match_call, draft_call = fake.calls
    assert "That's more than we expected" in match_call["messages"][0]["content"]
    assert "approved Gamic template" in draft_call["messages"][0]["content"]


def test_the_model_names_the_template_and_only_that_one_leads() -> None:
    out = draft(make(model(match=1, reply="Hey Pat, we complement brokers.")), "objection_info")
    assert out.kind == "drafted"
    assert out.examples[0].gamic_reply == OBJ_INFO_SOP


def test_no_clear_match_means_ray_writes_it() -> None:
    for answer in (0, 7, -1):
        out = draft(make(model(match=answer)), "objection_price")
        assert (out.kind, out.reason) == ("no_draft", OBJECTION_NO_DRAFT)


def test_a_failing_match_call_means_ray_writes_it() -> None:
    out = draft(make(FakeModel(error=RuntimeError("down"))), "objection_price")
    assert (out.kind, out.reason) == ("no_draft", OBJECTION_NO_DRAFT)


def test_objections_on_other_verticals_are_never_drafted_and_never_call_the_model() -> None:
    for vertical in ("lenders", "other", "unknown", ""):
        fake = model()
        out = draft(make(fake), "objection_price", vertical=vertical)
        assert (out.kind, out.reason) == ("no_draft", OBJECTION_NO_DRAFT)
        assert fake.calls == []


def test_ma_price_uses_the_setting_never_old_library_figures() -> None:
    reply = f"Hey Pat,\n\n{HYBRID}\n\nWould [TIME OPTIONS] work?"
    out = draft(make(model(reply=reply), HYBRID), "price_question", reply="what does it cost?")
    assert out.kind == "drafted" and out.price_confirmed is True
    assert out.text.count("hybrid model") == 1  # the template carries it: not inserted twice
    assert "$2.5k" not in out.text and out.flagged == ()


def test_ma_price_sentence_is_inserted_when_the_draft_lost_it() -> None:
    out = draft(
        make(model(reply="Hey Pat,\n\nPricing depends on scope."), HYBRID),
        "price_question",
        reply="what does it cost?",
    )
    assert sentence_in(out.text, HYBRID) and out.price_confirmed is True


def test_ma_price_without_a_setting_states_no_price_and_does_not_search_the_library() -> None:
    reply = "Hey Pat,\n\nIt is a flat fee of $2.5k-$10k.\n\nWould [TIME OPTIONS] work?"
    out = draft(make(model(reply=reply)), "price_question", reply="what does it cost?")
    assert out.price_confirmed is False
    assert "$2.5k" not in (out.text or "")  # old library figure is stripped


def test_numbers_inside_the_sop_template_in_use_are_allowed_for_any_label() -> None:
    out = draft(
        make(model(reply="Hey Pat,\n\nI'd need 15-20 minutes to get your deal box.")),
        "trial_request",
        reply="yes let's do the trial",
    )
    assert out.kind == "drafted" and "15-20 minutes" in out.text and out.removed == 0
    other = draft(
        make(model(reply="Hey Pat,\n\nI'd need 30 minutes to get your deal box.")),
        "trial_request",
        reply="yes",
    )
    assert other.removed == 1  # a number that is not in the template is still removed


def test_a_non_ma_campaign_never_sees_an_sop_template_or_the_ma_price_sentence() -> None:
    out = ExampleDrafter(
        lambda label, vertical="": [
            *POOL,
            hist(3, "price_question", "Hey. Fees are custom.", "lenders"),
        ],
        model(reply="Hey Pat, fees are custom."),
        "m-x",
        HYBRID,
        vertical_of=lambda thread: "lenders",
    ).draft(
        thread={"campaign_name": "GMC - LENDERS", "lead_first_name": "Pat"},
        label="price_question",
        confidence=0.95,
        reply="cost?",
        outreach="",
        min_confidence=0.75,
    )
    assert out.kind == "drafted"
    assert {e.source for e in out.examples} == {"history"}
    assert all(e.vertical == "lenders" for e in out.examples)
    assert "hybrid" not in out.text


class FakeObjectionDrafter:
    def __init__(self, kind: str) -> None:
        self.kind = kind

    def draft(self, **_: Any) -> Any:
        return SimpleNamespace(kind=self.kind, reason=OBJECTION_NO_DRAFT, text=None)


def test_an_objection_with_no_draft_keeps_the_note_for_ray() -> None:
    deps = SimpleNamespace(
        example_drafter=FakeObjectionDrafter("no_draft"),
        config=SimpleNamespace(draft_min_confidence=0.75),
        db=None,
    )
    result = Classification("objection_price", 0.9, "r", "llm")
    reply = SimpleNamespace(text="too expensive")
    from app.classify.routing import OBJECTION_NOTE, Route

    plan, view, draft_id = _prepare_from_examples(
        deps, {"id": "t"}, "c", result, reply, "", Route("draft", "x")
    )
    assert plan.lane == "human_only" and plan.note == OBJECTION_NOTE
    assert view.text is None and draft_id is None
