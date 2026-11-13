"""Drafting from examples: the library, the drafter, the card, the learning loop, feedback."""

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.db.client import fetch_examples_for_label, insert_example
from app.db.records import ExampleSource, NewExample
from app.examples.drafter import (
    NO_NUMBERS_RULE,
    ExampleDrafter,
    build_user_message,
    clean_draft,
    ensure_link,
    flag_figures,
    insert_price_sentence,
    latest_price_sentence,
    price_wording,
    replace_days,
)
from app.examples.history import history_examples, label_map
from app.examples.learn import learn_once, matches_step, sent_after_first_reply, step_texts
from app.examples.library import (
    Example,
    campaign_family,
    example_from_row,
    load_pool,
    parse_time,
    select_examples,
)
from app.examples.similarity import similarity
from app.ingest.deps import IngestConfig, IngestDeps
from app.ingest.process import process_reply_event
from app.slack.feedback import handle_feedback_click, record_feedback
from tests.c3_support import FakeModel
from tests.fakedb import FakeDb
from tests.test_c2 import FakeClassifier, FakeSlack, llm
from tests.test_webhook import EVENT, HISTORY, NOW, FakeSmartlead, rows

T0 = datetime(2026, 1, 1, tzinfo=UTC)


FILLER = " ".join(["word"] * 30)  # examples are long enough for the length cap


def ex(n: int, label: str = "info_request", campaign: str = "GMC - M&A May", days: int = 0, **kw):
    return Example(
        source=kw.get("source", "history"),
        source_thread_id=str(n),
        campaign_name=campaign,
        label=label,
        prospect_reply=f"question {n}",
        gamic_reply=f"answer {n} " + FILLER,
        replied_at=T0 + timedelta(days=days),
    )


# ------------------------------------------------------------------ selecting examples


def test_campaign_family_is_the_first_word_after_gmc() -> None:
    assert campaign_family("GMC - M&A June (US/Canada)") == "M&A"
    assert campaign_family("GMC - Marketing (Lead Magnet) UK") == "MARKETING"
    assert campaign_family("Something else") == "SOMETHING"
    assert campaign_family("") == ""


def test_same_label_first_then_same_family_then_most_recent() -> None:
    pool = [
        ex(1, "price_question", "GMC - M&A May", days=50),  # wrong label, ranks after all matches
        ex(2, "info_request", "GMC - PE Aug", days=40),  # right label, other family, newest
        ex(3, "info_request", "GMC - M&A June", days=10),  # right label, same family, old
        ex(4, "info_request", "GMC - M&A July", days=20),  # right label, same family, newer
    ]
    picked = select_examples(pool, "info_request", "GMC - M&A March New")
    assert [e.source_thread_id for e in picked] == ["4", "3", "2", "1"]


def test_only_five_are_picked_and_the_threads_own_answer_is_left_out() -> None:
    pool = [ex(n, days=n) for n in range(10)]
    picked = select_examples(pool, "info_request", "GMC - M&A", exclude_thread_id="9")
    assert len(picked) == 5 and "9" not in {e.source_thread_id for e in picked}
    assert [e.source_thread_id for e in picked] == ["8", "7", "6", "5", "4"]


def test_examples_without_an_answer_are_never_picked() -> None:
    empty = Example("history", "1", "GMC - M&A", "info_request", "q", "  ", T0)
    assert select_examples([empty], "info_request", "GMC - M&A") == []


def test_parse_time_reads_iso_and_email_dates() -> None:
    assert parse_time("2026-10-28T18:11:50.000Z") == datetime(2026, 10, 28, 18, 11, 50, tzinfo=UTC)
    assert parse_time("Wed, 5 Aug 2026 14:27:45 +0000") == datetime(
        2026, 8, 5, 14, 27, 45, tzinfo=UTC
    )
    assert parse_time("") is None and parse_time("not a date") is None


# ------------------------------------------------------------------ history and the table


def test_history_examples_need_a_reply_a_response_and_a_label() -> None:
    dataset = [
        {
            "thread_id": "1",
            "campaign_name": "GMC - M&A",
            "prospect_first_reply": "Tell me more",
            "gamic_response_to_first_reply": "Sure, here is how it works.",
            "first_reply_time": "2026-10-28T18:11:50.000Z",
        },
        {"thread_id": "2", "prospect_first_reply": "x", "gamic_response_to_first_reply": ""},
        {"thread_id": "3", "prospect_first_reply": "x", "gamic_response_to_first_reply": "y"},
    ]
    labels = label_map(
        [{"thread_id": "1", "human_message_type": "info_request"}],
        [
            {"thread_id": "1", "message_type": "meeting_request", "human_message_type": ""},
            {"thread_id": "2", "message_type": "info_request", "human_message_type": ""},
        ],
    )
    assert labels == {"1": "info_request", "2": "info_request"}  # gold beats the model label
    made, skipped = history_examples(dataset, labels)
    assert skipped == 2 and len(made) == 1
    assert made[0].source is ExampleSource.HISTORY and made[0].label == "info_request"
    assert made[0].replied_at == datetime(2026, 10, 28, 18, 11, 50, tzinfo=UTC)


def new_example(thread_id: str = "t1", label: str = "info_request", **kw: Any) -> NewExample:
    return NewExample(
        source=kw.pop("source", ExampleSource.HISTORY),
        source_thread_id=thread_id,
        label=label,
        prospect_reply="q",
        gamic_reply=kw.pop("gamic_reply", "a " + FILLER),
        campaign_name="GMC - M&A",
        replied_at=T0,
        **kw,
    )


def test_an_example_is_stored_once_and_read_back_by_label() -> None:
    db = FakeDb()
    assert insert_example(db, new_example()).is_new is True
    assert insert_example(db, new_example()).is_new is False
    insert_example(db, new_example("t2", "price_question"))
    assert len(db.tables["reply_examples"]) == 2
    assert [r["source_thread_id"] for r in fetch_examples_for_label(db, "price_question")] == ["t2"]
    pool = load_pool(db, "info_request")
    assert {e.source_thread_id for e in pool} == {"t1", "t2"}
    assert example_from_row(db.tables["reply_examples"][0]).replied_at is not None


# ------------------------------------------------------------------ similarity


def test_similarity_is_100_for_the_same_words_and_ignores_times_links_and_placeholders() -> None:
    assert similarity("Hey Jane, thanks!", "hey jane thanks") == 100.0
    draft = "Sounds good. How about [TIME OPTIONS]? Grab one here [LINK]"
    real = (
        "Sounds good. How about tomorrow at 9am or friday at 10:30am? Grab one here https://x.io/a"
    )
    assert similarity(draft, real) > 70
    assert similarity("", "") == 100.0


def test_similarity_drops_for_different_text() -> None:
    low = similarity("Happy to help with the list.", "Our fee is a retainer plus a success fee.")
    assert 0 <= low < 30


# ------------------------------------------------------------------ the drafter


def test_figures_are_found_and_links_are_replaced() -> None:
    assert flag_figures("We did $2M, 15% growth, and 3 deals. See 15%.") == ("$2M", "15%", "3")
    assert flag_figures("No numbers here.") == ()
    cleaned = clean_draft("Look at https://evil.example/x or www.evil.io today ```")
    assert "evil" not in cleaned and cleaned.count("[LINK]") == 2 and "```" not in cleaned


def reply_with(text: str) -> FakeModel:
    return FakeModel(answer={"reply": text})


def drafter(
    model: FakeModel, pool: list[Example] | None = None, price_sentence: str = ""
) -> ExampleDrafter:
    return ExampleDrafter(
        lambda label, vertical="": pool if pool is not None else [ex(1), ex(2)],
        model,
        "m-x",
        price_sentence,
    )


THREAD = {"campaign_name": "GMC - M&A May", "lead_first_name": "Pat"}


def draft_with(d: ExampleDrafter, **over: Any):
    args: dict[str, Any] = {
        "thread": THREAD,
        "label": "info_request",
        "confidence": 0.9,
        "reply": "How does it work?",
        "outreach": "Our email",
        "min_confidence": 0.75,
    }
    return d.draft(**{**args, **over})


def test_the_draft_uses_the_examples_and_never_keeps_a_link() -> None:
    model = reply_with("Hey Pat, happy to explain. Times: [TIME OPTIONS]. See https://x.example/a")
    out = draft_with(drafter(model))
    assert out.kind == "drafted" and out.model == "m-x"
    assert out.text.endswith("See [LINK]") and "x.example" not in out.text
    assert out.example_ids == ("2", "1") or set(out.example_ids) == {"1", "2"}
    (call,) = model.calls
    user = call["messages"][0]["content"]
    assert "answer 1" in user and "answer 2" in user and "How does it work?" in user
    assert "temperature" not in call  # sonnet-5-5 takes no sampling parameters
    system = call["system"][0]["text"]
    assert "[TIME OPTIONS]" in system and "[LINK]" in system and "Add no new facts" in system


PRICE_REPLY = "Pricing is typically $2,000 to $4,000 per month for most firms."


def price_pool() -> list[Example]:
    def p(n: int, label: str, text: str, days: int) -> Example:
        return Example(
            "history", f"p{n}", "GMC - M&A", label, "how much?", text, T0 + timedelta(days=days)
        )

    return [
        p(1, "price_question", "Old: it is $9,000 per month.", 1),
        p(2, "price_question", PRICE_REPLY + " Happy to talk.", 30),
        p(3, "price_and_info", "We charge a 15% success fee. More info soon.", 20),
        p(4, "price_question", "Another one at $3,000 monthly.", 25),
        p(5, "price_question", "No figure here at all.", 40),
    ]


def test_price_wording_is_the_figure_sentences_of_the_3_most_recent_price_replies() -> None:
    wording = price_wording(price_pool())
    assert wording == [
        PRICE_REPLY,
        "Another one at $3,000 monthly.",
        "We charge a 15% success fee.",
    ]
    assert not any("9,000" in w for w in wording)  # the 4th newest is not used
    assert (
        price_wording(price_pool(), exclude_thread_id="p2")[0] == "Another one at $3,000 monthly."
    )


def test_other_labels_are_told_no_numbers_and_a_figure_sentence_is_removed() -> None:
    text = "Hey Pat, happy to help.\nWe get 40% reply rates. Want a call?\nBest,\nAryan"
    model = reply_with(text)
    out = draft_with(drafter(model))
    assert out.kind == "drafted" and out.removed == 1 and out.flagged == ()
    assert "40%" not in out.text and "Want a call?" in out.text and "happy to help" in out.text
    user = model.calls[0]["messages"][0]["content"]
    assert NO_NUMBERS_RULE in user and "price wording" not in user.lower()


def test_a_number_the_lead_wrote_is_allowed_in_any_draft() -> None:
    out = draft_with(
        drafter(reply_with("Yes, 50 targets is fine. Want a call?")),
        reply="Can you send 50 targets?",
    )
    assert out.removed == 0 and out.flagged == ("50",)
    gone = draft_with(drafter(reply_with("Yes, 70 targets is fine.")), reply="Can you send 50?")
    assert gone.kind == "no_draft"  # the only sentence had a number that is not allowed


def test_clock_times_are_never_allowed() -> None:
    out = draft_with(
        drafter(reply_with("Does 10am work? Or [TIME OPTIONS].")), reply="call at 10am?"
    )
    assert "10am" not in out.text and "[TIME OPTIONS]" in out.text and out.removed == 1


PRICE_SENTENCE = "Pricing is a monthly retainer of $3,000 plus a success fee of 5%."


def test_the_price_sentence_setting_is_inserted_word_for_word_and_confirmed() -> None:
    model = reply_with("Hey Pat.\n\n[PRICE SENTENCE]\n\nHappy to chat.")
    out = draft_with(
        drafter(model, pool=price_pool(), price_sentence=PRICE_SENTENCE), label="price_question"
    )
    assert PRICE_SENTENCE in out.text and "[PRICE SENTENCE]" not in out.text
    assert out.price_confirmed is True and out.removed == 0
    assert out.flagged == ("$3,000", "5%")
    user = model.calls[0]["messages"][0]["content"]
    rule = user.split("<number_rule>")[1].split("</number_rule>")[0]
    assert "[PRICE SENTENCE]" in rule and "Never write a price" in rule
    assert "$2,000" not in rule  # the model is not shown any price wording


def test_a_price_the_model_writes_itself_is_removed_and_the_set_sentence_used() -> None:
    model = reply_with("Hey Pat.\nIt costs $500 a month.\nHappy to chat.")
    out = draft_with(
        drafter(model, pool=price_pool(), price_sentence=PRICE_SENTENCE), label="price_and_info"
    )
    assert "$500" not in out.text and out.removed == 1
    # no marker was written, so the sentence goes after the first body paragraph
    assert (
        out.text == "Hey Pat.\n" + PRICE_SENTENCE + "\nHappy to chat." or PRICE_SENTENCE in out.text
    )


def test_the_sentence_goes_after_the_greeting_and_first_paragraph_when_no_marker() -> None:
    text = "Hey Pat,\n\nThanks for asking.\n\nWould [TIME OPTIONS] work?"
    out = insert_price_sentence(text, "SENTENCE.")
    assert out.split("\n\n") == [
        "Hey Pat,",
        "Thanks for asking.",
        "SENTENCE.",
        "Would [TIME OPTIONS] work?",
    ]
    assert insert_price_sentence("[PRICE SENTENCE] and [PRICE SENTENCE]", "S.") == "S. and "
    assert "[PRICE SENTENCE]" not in insert_price_sentence("Hi\n\n[PRICE SENTENCE]\n\nBye", "")


def test_without_a_setting_the_newest_library_price_sentence_is_used_and_marked() -> None:
    model = reply_with("Hey Pat.\n[PRICE SENTENCE]\nHappy to chat.")
    out = draft_with(drafter(model, pool=price_pool()), label="price_question")
    assert PRICE_REPLY in out.text  # p2 is the newest price reply with a figure
    assert out.price_confirmed is False
    assert (
        latest_price_sentence(price_pool(), exclude_thread_id="p2")
        == "Another one at $3,000 monthly."
    )


def test_with_no_sentence_anywhere_the_model_is_told_to_state_no_price() -> None:
    model = reply_with("Hey Pat, pricing depends on scope. [PRICE SENTENCE]")
    pool = [Example("history", "x", "GMC - M&A", "price_question", "q", "No figure here.", T0)]
    out = draft_with(drafter(model, pool=pool), label="price_question")
    assert "No price sentence is available" in model.calls[0]["messages"][0]["content"]
    assert "[PRICE SENTENCE]" not in out.text and out.price_confirmed is False


def test_other_labels_never_get_a_price_sentence() -> None:
    out = draft_with(drafter(reply_with("Hey Pat. [PRICE SENTENCE] Bye."), price_sentence="X."))
    assert "X." not in out.text and "[PRICE SENTENCE]" not in out.text
    assert out.price_confirmed is None


# ---- numbers that appear in our outreach or in the same campaign's replies are allowed

FREE_PILOT = "GMC - M&A (Free Pilot) V1"
PILOT_REPLY = "Sounds good, we only have 2 weeks so I need 15-20 minutes to get your deal box."


def pilot_pool() -> list[Example]:
    return [
        Example("history", "a", FREE_PILOT, "trial_request", "yes", PILOT_REPLY, T0),
        Example("history", "b", "GMC - PE Aug", "trial_request", "yes", "We need 45 minutes.", T0),
    ]


def pilot(text: str, **over: Any):
    return draft_with(
        drafter(reply_with(text), pool=pilot_pool()),
        thread={"campaign_name": FREE_PILOT, "lead_first_name": "Pat"},
        label="trial_request",
        reply="Yes, I am interested in the pilot.",
        outreach="Free pilot, nothing to lose.",
        **over,
    )


def test_a_number_from_a_reply_in_the_same_campaign_is_allowed() -> None:
    out = pilot("Hey Pat.\nWe only have 2 weeks, so I need 15-20 minutes.")
    assert out.removed == 0 and "2 weeks" in out.text and "15-20 minutes" in out.text
    assert out.flagged == ("2", "15", "20")


def test_a_number_that_only_other_campaigns_use_stays_blocked() -> None:
    out = pilot("Hey Pat.\nI need 45 minutes. Happy to chat.")
    assert out.removed == 1 and "45" not in out.text and "Happy to chat." in out.text


def test_the_threads_own_answer_is_not_a_same_campaign_source() -> None:
    out = pilot("Hey Pat.\nWe only have 2 weeks. Happy to chat.", exclude_thread_id="a")
    assert out.removed == 1 and "2 weeks" not in out.text


def test_a_number_in_the_outreach_email_we_sent_is_allowed() -> None:
    out = draft_with(
        drafter(reply_with("Hey Pat.\nAs I said, we found 50 targets for you.")),
        outreach="I can pull 50 in Texas showing signs. Want them?",
    )
    assert out.removed == 0 and "50 targets" in out.text


# ---- day words used for scheduling


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Does tomorrow at 9am or friday at 10:30am work?", "Does [TIME OPTIONS] work?"),
        ("Would Monday or Tuesday work for a call?", "Would [TIME OPTIONS] work for a call?"),
        ("Does tomorrow or thursday at [TIME OPTIONS] work?", "Does [TIME OPTIONS] work?"),
        ("Free today for a quick chat.", "Free [TIME OPTIONS] for a quick chat."),
        ("How about Friday at 3pm?", "How about [TIME OPTIONS]?"),
    ],
)
def test_scheduling_day_words_become_time_options(text: str, expected: str) -> None:
    assert replace_days(text) == expected


def test_day_words_outside_scheduling_are_left_alone() -> None:
    plain = "We launched on Monday and it grew. Today the list is live."
    assert replace_days(plain) == plain


def test_a_drafted_reply_never_keeps_a_day_or_clock_time() -> None:
    out = draft_with(
        drafter(reply_with("Hey Pat.\nDoes tomorrow at 9am or Friday at 2pm work for a call?"))
    )
    assert out.text == "Hey Pat.\nDoes [TIME OPTIONS] work for a call?" and out.removed == 0


def test_no_draft_below_confidence_without_examples_on_error_or_empty_answer() -> None:
    model = reply_with("hi")
    low = draft_with(drafter(model), confidence=0.5)
    assert low.kind == "no_draft" and low.reason.startswith("confidence_below") and not model.calls
    none = draft_with(drafter(model, pool=[]))
    assert none.kind == "no_draft" and "no examples" in none.reason and not model.calls
    broken = draft_with(drafter(FakeModel(error=RuntimeError("boom"))))
    assert broken.reason == "could_not_write_a_draft"
    empty = draft_with(drafter(reply_with("   ")))
    assert empty.reason == "model_could_not_answer_without_new_facts"


def test_emails_are_masked_in_what_the_model_sees() -> None:
    user = build_user_message(
        [ex(1)], "info_request", "GMC - M&A", "Pat", "write to pat@example.com", "from a@b.com"
    )
    assert "pat@example.com" not in user and "a@b.com" not in user and "[email]" in user


# ------------------------------------------------------------------ the pipeline and the card


def make_deps(model: FakeModel | None = None, label: str = "info_request", conf: float = 0.9):
    db = FakeDb()
    insert_example(db, new_example("h1", label))
    return IngestDeps(
        db=db,
        smartlead=FakeSmartlead(),
        clock=lambda: NOW,
        classifier=FakeClassifier(llm(label, conf)),
        slack=FakeSlack(),
        example_drafter=ExampleDrafter(
            lambda lab, vertical="": load_pool(db, lab, vertical),
            model or reply_with("Hey Pat, here is how it works in two steps."),
            "m-x",
        ),
        config=IngestConfig(draft_channel="drafts", draft_min_confidence=0.75),
    )


def run(deps: IngestDeps, event_id: str = "e1"):
    deps.db.tables.setdefault("raw_events", []).append({"id": event_id, "processed": False})
    return process_reply_event(deps, event_id, EVENT)


def actions(post: dict[str, Any]) -> list[str]:
    found = [b for b in post["blocks"] if b["type"] == "actions"]
    return [e["action_id"] for e in found[0]["elements"]] if found else []


def test_a_new_reply_gets_an_example_draft_on_a_copy_card() -> None:
    deps = make_deps()
    run(deps)
    (draft,) = rows(deps.db, "drafts")
    assert draft["template_id"] == "examples" and draft["status"] == "proposed"
    assert draft["slots"] == {"example_ids": ["h1"]} and draft["model"] == "m-x"
    assert draft["validator_result"]["flagged"] == []
    (post,) = deps.slack.posts
    text = json.dumps(post["blocks"])
    assert "here is how it works in two steps." in text and "```" in text
    assert actions(post) == ["draft_good", "draft_bad"]
    assert rows(deps.db, "slack_cards")[0]["lane"] == "draft"


def test_a_removed_figure_sentence_is_noted_on_the_card() -> None:
    deps = make_deps(reply_with("Hey Pat, here is how it works.\nWe see 40% reply rates."))
    run(deps)
    (draft,) = rows(deps.db, "drafts")
    assert "40%" not in draft["text"] and draft["validator_result"]["removed_sentences"] == 1
    text = json.dumps(deps.slack.posts[0]["blocks"])
    assert "Removed 1 sentence(s) with a number, price or percentage" in text


def test_a_price_from_the_library_is_stated_and_highlighted_on_the_card() -> None:
    deps = make_deps(reply_with("Hey Pat. Typically $3,000 per month."), label="price_question")
    insert_example(
        deps.db,
        new_example("pq", "price_question", gamic_reply="It is typically $3,000 per month."),
    )
    run(deps)
    (draft,) = rows(deps.db, "drafts")
    assert draft["validator_result"]["flagged"] == ["$3,000"]
    text = json.dumps(deps.slack.posts[0]["blocks"])
    assert "Check these figures before you send" in text and "`$3,000`" in text


def test_the_draft_card_never_has_approve_edit_or_skip() -> None:
    deps = make_deps()
    run(deps)
    text = json.dumps(deps.slack.posts[0]["blocks"])
    for gone in ("reply_approve", "reply_edit", "reply_skip", "Approve", "Skip"):
        assert gone not in text


def test_a_draft_with_placeholders_says_to_fill_them_in() -> None:
    deps = make_deps(reply_with("Hey Pat. How about [TIME OPTIONS]? Details: [LINK]"))
    run(deps)
    text = json.dumps(deps.slack.posts[0]["blocks"])
    assert "Fill in [LINK], [TIME OPTIONS] before you send." in text


def test_no_examples_for_the_label_means_a_human_notice_with_no_draft() -> None:
    deps = make_deps()
    deps.db.tables["reply_examples"] = []
    run(deps)
    assert not rows(deps.db, "drafts")
    (post,) = deps.slack.posts
    assert actions(post) == [] and "human only" in json.dumps(post["blocks"])


def test_do_not_contact_gets_a_do_not_reply_card_and_no_model_call() -> None:
    model = reply_with("must not be used")
    deps = make_deps(model, label="do_not_contact", conf=0.95)
    run(deps)
    assert not model.calls and not rows(deps.db, "drafts")
    text = json.dumps(deps.slack.posts[0]["blocks"])
    assert "DO NOT REPLY" in text and actions(deps.slack.posts[0]) == []


@pytest.mark.parametrize("label", ["out_of_office", "auto_reply_or_bounce"])
def test_out_of_office_and_auto_replies_get_a_short_note_and_no_draft(label: str) -> None:
    model = reply_with("must not be used")
    deps = make_deps(model, label=label, conf=0.95)
    run(deps)
    assert not model.calls and not rows(deps.db, "drafts")
    assert "no reply needed" in json.dumps(deps.slack.posts[0]["blocks"])


def test_nothing_is_sent_and_smartlead_is_only_read() -> None:
    deps = make_deps()
    run(deps)
    assert not [a for a in rows(deps.db, "actions") if a["type"] == "send"]
    assert [n for n in vars(deps.smartlead) if "send" in n] == []


# ------------------------------------------------------------------ feedback


def card_ids(deps: IngestDeps) -> tuple[str, str]:
    card = rows(deps.db, "slack_cards")[0]
    return card["channel"], card["slack_ts"]


def test_feedback_is_recorded_once_and_changes_nothing_else() -> None:
    deps = make_deps()
    run(deps)
    channel, ts = card_ids(deps)
    before = copy.deepcopy(rows(deps.db, "drafts")[0])
    assert record_feedback(deps.db, channel, ts, "draft_bad", "U012ABCDEF") is True
    assert record_feedback(deps.db, channel, ts, "draft_good", "U099") is False  # first wins
    after = rows(deps.db, "drafts")[0]
    assert (after["feedback"], after["feedback_by"]) == ("bad", "U012ABCDEF")
    assert after["feedback_at"]
    for key in ("text", "status", "template_id", "validator_result"):
        assert after[key] == before[key]
    assert not rows(deps.db, "decisions") and not rows(deps.db, "actions")


def test_feedback_on_an_unknown_card_or_a_bad_action_is_ignored() -> None:
    deps = make_deps()
    run(deps)
    channel, ts = card_ids(deps)
    assert record_feedback(deps.db, channel, "9999.9", "draft_good", "U1") is False
    assert record_feedback(deps.db, channel, ts, "reply_approve", "U1") is False
    assert "feedback" not in rows(deps.db, "drafts")[0]


def test_a_feedback_click_replaces_the_buttons_with_who_said_what() -> None:
    deps = make_deps()
    run(deps)
    channel, ts = card_ids(deps)
    blocks = deps.slack.posts[0]["blocks"]
    updates: list[dict[str, Any]] = []

    class Client:
        async def chat_update(self, **kwargs: Any) -> None:
            updates.append(kwargs)

    async def ack() -> None:
        pass

    body = {
        "message": {"ts": ts, "blocks": blocks},
        "channel": {"id": channel},
        "user": {"id": "U012ABCDEF"},
    }

    def record(c: str, t: str, a: str, u: str) -> bool:
        return record_feedback(deps.db, c, t, a, u)

    asyncio.run(handle_feedback_click("draft_good", ack, body, Client(), record))
    (update,) = updates
    assert not [b for b in update["blocks"] if b["type"] == "actions"]
    assert "<@U012ABCDEF> marked this a good draft" in json.dumps(update["blocks"])
    assert rows(deps.db, "drafts")[0]["feedback"] == "good"
    asyncio.run(handle_feedback_click("draft_bad", ack, body, Client(), record))
    assert len(updates) == 1  # a second click changes nothing


# ------------------------------------------------------------------ the learning loop


def with_later_send(
    deps: IngestDeps, body: str = "<p>Hi Pat, here is how it works in two steps.</p>"
) -> None:
    history = copy.deepcopy(HISTORY)
    history["history"][-1]["email_body"] = body
    deps.smartlead.history = history


def test_sent_after_first_reply_finds_the_next_sent_message() -> None:
    from app.smartlead.thread import parse_thread

    found = sent_after_first_reply(parse_thread(HISTORY))
    assert found is not None and found[0].direction.value == "REPLY"
    assert found[1].direction.value == "SENT" and found[1].timestamp > found[0].timestamp
    assert found[2] == 0
    only_first = copy.deepcopy(HISTORY)
    only_first["history"] = only_first["history"][:2]
    (_, none, skipped) = sent_after_first_reply(parse_thread(only_first))
    assert none is None and skipped == 0


def test_a_reply_ray_sent_becomes_a_new_example_with_a_similarity_score() -> None:
    deps = make_deps()
    run(deps)
    with_later_send(deps)
    assert deps.smartlead.thread_calls  # the pipeline read the thread; learning reads it again
    result = learn_once(deps, now=NOW)
    assert (result.checked, result.learned, result.errors) == (1, 1, 0)
    ray = [r for r in rows(deps.db, "reply_examples") if r["source"] == "ray_sent"]
    assert len(ray) == 1
    example = ray[0]
    draft = rows(deps.db, "drafts")[0]
    assert example["draft_id"] == draft["id"] and example["label"] == "info_request"
    assert "how it works" in example["gamic_reply"] and example["prospect_reply"]
    assert 0 <= example["draft_similarity"] <= 100 and example["draft_similarity"] > 50
    assert example["campaign_name"] and example["replied_at"]


def test_a_draft_is_learned_from_only_once() -> None:
    deps = make_deps()
    run(deps)
    with_later_send(deps)
    learn_once(deps, now=NOW)
    again = learn_once(deps, now=NOW)
    assert (again.checked, again.learned) == (0, 0)
    assert len([r for r in rows(deps.db, "reply_examples") if r["source"] == "ray_sent"]) == 1


def test_new_examples_are_used_for_the_next_draft_immediately() -> None:
    deps = make_deps()
    run(deps)
    with_later_send(deps)
    learn_once(deps, now=NOW)
    pool = load_pool(deps.db, "info_request")
    picked = select_examples(pool, "info_request", "GMC - M&A May")
    assert "ray_sent" in {e.source for e in picked}


def test_nothing_is_learned_until_a_reply_has_been_sent() -> None:
    deps = make_deps()
    run(deps)
    history = copy.deepcopy(HISTORY)
    history["history"] = history["history"][:2]  # the lead's reply is the newest message
    deps.smartlead.history = history
    result = learn_once(deps, now=NOW)
    assert (result.checked, result.learned, result.waiting) == (1, 0, 1)


def test_a_fetch_problem_is_counted_and_retried_next_time() -> None:
    from app.smartlead.errors import SmartleadError

    deps = make_deps()
    run(deps)
    deps.smartlead.error = SmartleadError("down")
    assert learn_once(deps, now=NOW).errors == 1
    deps.smartlead.error = None
    with_later_send(deps)
    assert learn_once(deps, now=NOW).learned == 1


def test_old_drafts_are_no_longer_checked() -> None:
    deps = make_deps()
    run(deps)
    deps.db.tables["drafts"][0]["created_at"] = (NOW - timedelta(days=30)).isoformat()
    assert learn_once(deps, now=NOW).checked == 0


def test_the_learning_loop_only_reads_from_smartlead() -> None:
    deps = make_deps()
    run(deps)
    with_later_send(deps)
    deps.smartlead = SimpleNamespace(
        get_thread_raw=deps.smartlead.get_thread_raw,
        get_sequence=deps.smartlead.get_sequence,
        thread_calls=deps.smartlead.thread_calls,
    )  # nothing but a read method exists, so any other call would raise
    assert learn_once(deps, now=NOW).learned == 1


# ---- sequence steps are not Ray's answer


STEP_HTML = HISTORY["history"][-1]["email_body"]
SEQUENCE = [{"seq_number": 2, "email_body": STEP_HTML.replace("Jane", "{{first_name}}")}]


def test_step_texts_reads_bodies_and_variants_and_drops_merge_tags_and_spintax() -> None:
    sequence = [
        {
            "seq_number": 1,
            "email_body": "<p>{{first_name}} - {saw|noticed} your firm</p>",
            "sequence_variants": [{"variant_label": "B", "email_body": "<p>Variant text here</p>"}],
        }
    ]
    steps = step_texts(sequence)
    assert len(steps) == 2 and "saw your firm" in steps[0] and "Variant text here" in steps[1]
    assert step_texts([]) == [] and step_texts(None) == []


def test_a_sent_message_that_matches_a_step_is_not_an_answer() -> None:
    from app.smartlead.thread import parse_thread

    steps = step_texts(SEQUENCE)
    assert matches_step(parse_thread(HISTORY)[-1].text, steps)
    reply, answer, skipped = sent_after_first_reply(parse_thread(HISTORY), steps)
    assert answer is None and skipped == 1 and reply.direction.value == "REPLY"


def test_the_first_sent_message_that_is_not_a_step_is_the_answer() -> None:
    from app.smartlead.thread import parse_thread

    history = copy.deepcopy(HISTORY)
    ray = copy.deepcopy(history["history"][-1])
    ray.update(
        {"email_body": "<p>Hi Pat, here is how it works in two steps.</p>", "message_id": "<ray@x>"}
    )
    ray["time"] = "2026-10-22T09:00:00.000Z"
    ray["stats_id"] = "33333333-cccc-4ccc-8ccc-000000000003"
    history["history"].append(ray)
    _, answer, skipped = sent_after_first_reply(parse_thread(history), step_texts(SEQUENCE))
    assert skipped == 1 and "two steps" in answer.text


def test_the_learning_loop_skips_sequence_steps_logs_the_count_and_waits(caplog) -> None:
    import logging

    deps = make_deps()
    run(deps)
    deps.smartlead.sequence = SEQUENCE
    with caplog.at_level(logging.INFO, logger="app.examples.learn"):
        result = learn_once(deps, now=NOW)
    assert (result.learned, result.waiting, result.skipped_steps) == (0, 1, 1)
    assert not [r for r in rows(deps.db, "reply_examples") if r["source"] == "ray_sent"]
    assert "1 sent messages skipped as sequence steps" in caplog.text
    assert deps.smartlead.sequence_calls == [deps.smartlead.sequence_calls[0]] * len(
        deps.smartlead.sequence_calls
    )


def test_an_answer_after_a_sequence_step_is_learned_and_the_step_is_counted() -> None:
    deps = make_deps()
    run(deps)
    deps.smartlead.sequence = SEQUENCE
    history = copy.deepcopy(HISTORY)
    ray = copy.deepcopy(history["history"][-1])
    ray.update({"email_body": "<p>Hi Pat, here is how it works in two steps.</p>"})
    ray.update({"time": "2026-10-22T09:00:00.000Z", "message_id": "<ray@x>", "stats_id": "s-ray"})
    history["history"].append(ray)
    deps.smartlead.history = history
    result = learn_once(deps, now=NOW)
    assert (result.learned, result.skipped_steps) == (1, 1)
    (example,) = [r for r in rows(deps.db, "reply_examples") if r["source"] == "ray_sent"]
    assert "two steps" in example["gamic_reply"]


def test_if_the_sequence_cannot_be_read_nothing_is_guessed() -> None:
    from app.smartlead.errors import SmartleadError

    deps = make_deps()
    run(deps)

    def broken(campaign_id: int):
        raise SmartleadError("sequence down")

    deps.smartlead.get_sequence = broken
    result = learn_once(deps, now=NOW)
    assert (result.errors, result.learned) == (1, 0)
    assert not [r for r in rows(deps.db, "reply_examples") if r["source"] == "ray_sent"]


def test_a_booking_line_without_a_link_gets_the_placeholder() -> None:
    text = "Hey,\n\nWould [TIME OPTIONS] work?\n\nIf not, just grab a time over here.\n\nBest,"
    fixed = ensure_link(text)
    assert "If not, just grab a time over here [LINK]." in fixed
    assert fixed.count("[LINK]") == 1 and "Would [TIME OPTIONS] work?" in fixed


def test_a_booking_line_that_has_the_link_is_left_alone() -> None:
    text = "If not, just grab a time over here [LINK].\n\nWe launched a call last week."
    assert ensure_link(text) == text
