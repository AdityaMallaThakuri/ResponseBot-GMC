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
    ExampleDrafter,
    build_user_message,
    clean_draft,
    flag_figures,
)
from app.examples.history import history_examples, label_map
from app.examples.learn import learn_once, sent_after_first_reply
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


def ex(n: int, label: str = "info_request", campaign: str = "GMC - M&A May", days: int = 0, **kw):
    return Example(
        source=kw.get("source", "history"),
        source_thread_id=str(n),
        campaign_name=campaign,
        label=label,
        prospect_reply=f"question {n}",
        gamic_reply=f"answer {n}",
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
        gamic_reply="a",
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


def drafter(model: FakeModel, pool: list[Example] | None = None) -> ExampleDrafter:
    return ExampleDrafter(lambda label: pool if pool is not None else [ex(1), ex(2)], model, "m-x")


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


def test_figures_left_in_the_draft_are_flagged() -> None:
    out = draft_with(drafter(reply_with("We charge 5% plus $10k.")))
    assert out.flagged == ("5%", "$10k")


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
            lambda lab: load_pool(db, lab),
            model or reply_with("Hey Pat, here is how it works. 3 steps."),
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
    assert draft["validator_result"]["flagged"] == ["3"]
    (post,) = deps.slack.posts
    text = json.dumps(post["blocks"])
    assert "Here is how it works" not in text and "here is how it works. 3 steps." in text
    assert "```" in text and "Check these figures before you send" in text and "`3`" in text
    assert actions(post) == ["draft_good", "draft_bad"]
    assert rows(deps.db, "slack_cards")[0]["lane"] == "draft"


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
    deps: IngestDeps, body: str = "<p>Hi Pat, here is how it works, 3 steps.</p>"
) -> None:
    history = copy.deepcopy(HISTORY)
    history["history"][-1]["email_body"] = body
    deps.smartlead.history = history


def test_sent_after_first_reply_finds_the_next_sent_message() -> None:
    from app.smartlead.thread import parse_thread

    found = sent_after_first_reply(parse_thread(HISTORY))
    assert found is not None and found[0].direction.value == "REPLY"
    assert found[1].direction.value == "SENT" and found[1].timestamp > found[0].timestamp
    only_first = copy.deepcopy(HISTORY)
    only_first["history"] = only_first["history"][:2]
    assert sent_after_first_reply(parse_thread(only_first)) is None


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
        get_thread_raw=deps.smartlead.get_thread_raw, thread_calls=deps.smartlead.thread_calls
    )  # nothing but a read method exists, so any other call would raise
    assert learn_once(deps, now=NOW).learned == 1
