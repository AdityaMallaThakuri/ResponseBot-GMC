"""Smartlead sync, SOP import, recency ranking and the length cap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from app.classify.classifier import Classification
from app.examples.drafter import (
    ExampleDrafter,
    adapt_example,
    median_words,
    word_count,
)
from app.examples.library import Example, select_examples
from app.examples.sop import label_for, parse_sections, sop_examples, valid_labels
from app.examples.sync import SYNC_CATEGORIES, sync_examples
from app.slack.card import ReplyCard, build_reply_card_blocks
from tests.c3_support import FakeModel

NOW = datetime(2026, 11, 12, tzinfo=UTC)


def example(n: int, days_ago: int, words: int = 20, **kw: Any) -> Example:
    return Example(
        source=kw.get("source", "history"),
        source_thread_id=str(n),
        campaign_name=kw.get("campaign", "GMC - M&A May"),
        label=kw.get("label", "info_request"),
        prospect_reply=kw.get("prospect", f"question {n}"),
        gamic_reply=f"Hi Sam, answer {n} " + " ".join(["w"] * words),
        replied_at=NOW - timedelta(days=days_ago),
        sent_at=NOW - timedelta(days=days_ago),
        priority=kw.get("priority", 0),
    )


# ------------------------------------------------------------------ ranking


def test_a_recent_reply_beats_an_older_one_of_the_same_label() -> None:
    pool = [example(1, 200), example(2, 10), example(3, 90)]
    picked = select_examples(pool, "info_request", "GMC - M&A May", now=NOW)
    assert [e.source_thread_id for e in picked] == ["2", "3", "1"]


def test_the_label_still_comes_before_recency() -> None:
    pool = [example(1, 2, label="price_question"), example(2, 300)]
    picked = select_examples(pool, "info_request", "GMC - M&A May", now=NOW)
    assert picked[0].source_thread_id == "2"


def test_within_the_recent_window_the_more_similar_reply_wins() -> None:
    pool = [
        example(1, 5, prospect="we are a lender in texas"),
        example(2, 20, prospect="how does the pricing work for you"),
    ]
    picked = select_examples(
        pool, "info_request", "GMC - M&A May", reply="how does pricing work", now=NOW
    )
    assert picked[0].source_thread_id == "2"


def test_an_sop_template_wins_its_label() -> None:
    pool = [example(1, 1), example(2, 400, source="sop", priority=100)]
    picked = select_examples(pool, "info_request", "GMC - M&A May", now=NOW)
    assert picked[0].source_thread_id == "2"


# ------------------------------------------------------------------ length cap

THREAD = {"campaign_name": "GMC - M&A May", "lead_first_name": "Pat"}


def run_draft(model: FakeModel, pool: list[Example]):
    drafter = ExampleDrafter(lambda label: pool, model, "m-x")
    return drafter.draft(
        thread=THREAD,
        label="info_request",
        confidence=0.9,
        reply="How does it work?",
        outreach="Our email",
        min_confidence=0.75,
    )


def sequence_model(*replies: str) -> FakeModel:
    answers = iter(replies)
    return FakeModel(answer=lambda schema, user: {"reply": next(answers)})


def test_the_cap_is_the_median_word_count_of_the_examples() -> None:
    pool = [example(1, 1, 10), example(2, 2, 20), example(3, 3, 40)]
    assert median_words(pool) == word_count(pool[1].gamic_reply)


def test_a_draft_within_the_cap_is_kept_with_one_model_call() -> None:
    model = sequence_model("Hi Pat, short answer here.")
    out = run_draft(model, [example(1, 1), example(2, 2)])
    assert out.text == "Hi Pat, short answer here." and out.length_fix == ""
    assert len(model.calls) == 1 and out.word_count == 5 and out.word_limit >= 20


def test_a_long_draft_is_asked_to_shorten_once() -> None:
    long = " ".join(["long"] * 60)
    model = sequence_model(f"Hi Pat, {long}", "Hi Pat, the short one.")
    out = run_draft(model, [example(1, 1), example(2, 2)])
    assert out.text == "Hi Pat, the short one." and out.length_fix == "shortened"
    assert len(model.calls) == 2
    assert "<too_long>" in model.calls[1]["messages"][0]["content"]


def test_still_too_long_falls_back_to_the_closest_example_with_the_name_swapped() -> None:
    long = " ".join(["long"] * 60)
    model = sequence_model(f"Hi Pat, {long}", f"Hi Pat, {long} again")
    pool = [example(1, 1, 5), example(2, 2, 6), example(3, 3, 7)]
    out = run_draft(model, pool)
    assert out.length_fix == "closest_example" and len(model.calls) == 2
    assert out.text.startswith("Hi Pat, answer 1") and word_count(out.text) <= out.word_limit


def test_adapt_example_changes_only_the_greeting_name() -> None:
    assert adapt_example("Hi Sam,\n\nThanks Sam.", "Pat") == "Hi Pat,\n\nThanks Sam."
    assert adapt_example("Hello Sam!\nOk", "") == "Hello!\nOk"
    assert adapt_example("Thanks for replying.", "Pat") == "Thanks for replying."


def test_the_card_shows_the_word_count() -> None:
    card = ReplyCard(
        thread_ref="t",
        lead_name="Jane",
        campaign="GMC - M&A",
        reply="r",
        lane="draft",
        label="info_request",
        confidence=0.9,
        reason="x",
        draft_text="one two three four",
    )
    assert "4 words" in str(build_reply_card_blocks(card))


# ------------------------------------------------------------------ sync


def entry(kind: str, when: str, body: str, n: int) -> dict[str, str]:
    return {
        "type": kind,
        "time": when,
        "email_body": body,
        "stats_id": f"s{n}",
        "message_id": f"<m{n}@x>",
        "from": "a@x.com",
        "to": "b@y.com",
    }


def item(lead: int, category: int, history: list[dict[str, str]], campaign: int = 10):
    return {
        "email_lead_id": str(lead),
        "email_lead_map_id": str(lead + 5000),
        "email_campaign_id": str(campaign),
        "email_campaign_name": "GMC - M&A June",
        "lead_category_id": str(category),
        "email_history": history,
    }


THREAD_OK = [
    entry("SENT", "2026-10-01T10:00:00Z", "<p>Our cold email step one</p>", 1),
    entry("REPLY", "2026-10-02T10:00:00Z", "<p>Sounds interesting, tell me more</p>", 2),
    entry("SENT", "2026-10-03T10:00:00Z", "<p>Our cold email step one</p>", 3),  # a sequence step
    entry("SENT", "2026-10-04T10:00:00Z", "<p>Happy to explain how it works.</p>", 4),
]


class InboxSmartlead:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items
        self.calls: list[tuple[list[int], int, bool]] = []

    def fetch_master_inbox_replies(self, ids, limit=20, offset=0, include_history=False):
        self.calls.append((list(ids), offset, include_history))
        return {"ok": True, "data": self.items[offset : offset + limit], "offset": offset}

    def get_sequence(self, campaign_id: int) -> Any:
        return [{"seq_variants": [{"email_body": "<p>Our cold email step one</p>"}]}]


class FixedClassifier:
    def __init__(self, label: str = "info_request", confidence: float = 0.9) -> None:
        self.result = Classification(label, confidence, "r", "llm")
        self.calls: list[tuple[str, str, str]] = []

    def classify(self, reply: str, campaign: str, outreach: str) -> Classification:
        self.calls.append((reply, campaign, outreach))
        return self.result


CAMPAIGNS = [SimpleNamespace(id=10, name="GMC - M&A June", parent_name="")]


def sync(items, classifier=None, known_ids=frozenset(), known_replies=()):
    smartlead = InboxSmartlead(items)
    result = sync_examples(
        smartlead,
        classifier or FixedClassifier(),
        CAMPAIGNS,
        set(known_ids),
        list(known_replies),
    )
    return result, smartlead


def test_a_positive_thread_becomes_a_pair_dated_by_our_reply() -> None:
    result, smartlead = sync([item(1, 1, THREAD_OK)])
    (pair,) = result.pairs
    assert pair.source.value == "smartlead_sync" and pair.source_thread_id == "1"
    assert "tell me more" in pair.prospect_reply
    assert pair.gamic_reply == "Happy to explain how it works."  # not the sequence step
    assert pair.sent_at == datetime(2026, 10, 4, 10, tzinfo=UTC)
    assert pair.campaign_name == "GMC - M&A June" and pair.label == "info_request"
    assert smartlead.calls == [([10], 0, True)]  # history comes with the inbox, read only


def test_the_outreach_email_is_the_first_sent_message() -> None:
    classifier = FixedClassifier()
    sync([item(1, 1, THREAD_OK)], classifier)
    assert classifier.calls[0][2] == "Our cold email step one"


def test_only_positive_categories_are_used() -> None:
    result, _ = sync([item(1, 3, THREAD_OK), item(2, 1, THREAD_OK)])  # 3 = Not Interested
    assert [p.source_thread_id for p in result.pairs] == ["2"]
    assert result.inbox_threads == 2 and result.in_categories == 1
    assert 3 not in SYNC_CATEGORIES and 1 in SYNC_CATEGORIES


def test_a_thread_already_in_the_library_is_skipped_by_lead_id_or_by_text() -> None:
    result, _ = sync([item(1, 1, THREAD_OK)], known_ids={"1"})
    assert not result.pairs and result.already_in_library == 1
    result, _ = sync([item(1, 1, THREAD_OK)], known_replies=["Happy to explain how it works."])
    assert not result.pairs and result.already_in_library == 1


def test_a_thread_with_only_sequence_steps_after_the_reply_has_no_answer() -> None:
    history = THREAD_OK[:3]
    result, _ = sync([item(1, 1, history)])
    assert not result.pairs and result.no_answer == 1


def test_labels_that_are_no_example_and_low_confidence_are_skipped() -> None:
    result, _ = sync([item(1, 1, THREAD_OK)], FixedClassifier("do_not_contact", 1.0))
    assert not result.pairs and result.skipped_label == 1
    result, _ = sync([item(1, 1, THREAD_OK)], FixedClassifier("info_request", 0.5))
    assert not result.pairs and result.low_confidence == 1


def test_a_bad_thread_is_counted_and_the_rest_carry_on() -> None:
    bad = item(1, 1, [{"type": "WEIRD"}])
    result, _ = sync([bad, item(2, 1, THREAD_OK)])
    assert result.errors == 1 and [p.source_thread_id for p in result.pairs] == ["2"]


def with_answer(i: int) -> list[dict[str, str]]:
    answer = entry("SENT", "2026-10-04T10:00:00Z", f"<p>Unique answer number {i} {i * 7919}</p>", 9)
    return [*THREAD_OK[:3], answer]


def test_pages_are_followed_and_every_thread_is_read_are_followed() -> None:
    items = [item(i, 1, with_answer(i)) for i in range(1, 26)]
    result, smartlead = sync(items)
    assert len(result.pairs) == 25 and [c[1] for c in smartlead.calls] == [0, 20]


def test_date_range_and_counts() -> None:
    result, _ = sync([item(1, 1, THREAD_OK)])
    assert result.by_label == {"info_request": 1}
    assert result.by_campaign == {"GMC - M&A June": 1}
    assert result.date_range() == (result.pairs[0].sent_at, result.pairs[0].sent_at)


# ------------------------------------------------------------------ SOP

SOP = """Intro text that is ignored

## They ask what it costs [price_question]
Hi [NAME],

Pricing depends on scope.

## Interested, wants more details
Hi [NAME], here is how it works.

## Something odd
Never imported.

## Bad tag [not_a_label]
Skipped too.
"""


def test_sections_are_split_on_headings() -> None:
    sections = parse_sections(SOP)
    assert [s.heading for s in sections][:2] == [
        "They ask what it costs [price_question]",
        "Interested, wants more details",
    ]
    assert sections[0].body.startswith("Hi [NAME],")


def test_an_sop_template_is_an_example_with_priority_and_the_word_for_word_body() -> None:
    examples, unmatched = sop_examples(SOP, now=NOW)
    by_label = {e.label: e for e in examples}
    assert set(by_label) == {"price_question", "info_request"}
    price = by_label["price_question"]
    assert price.source.value == "sop" and price.priority == 100
    assert price.prospect_reply == "They ask what it costs"
    assert price.gamic_reply == "Hi [NAME],\n\nPricing depends on scope."
    assert price.sent_at == NOW
    assert unmatched == ["Something odd", "Bad tag [not_a_label]"]


def test_label_for_uses_the_tag_first_then_keywords() -> None:
    labels = valid_labels()
    assert label_for("Asks about pricing", labels)[0] == "price_question"
    assert label_for("x [meeting_request]", labels) == ("meeting_request", "x")
    assert label_for("Totally unrelated", labels)[0] is None
