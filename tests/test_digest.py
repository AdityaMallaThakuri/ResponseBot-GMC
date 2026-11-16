"""The daily summary of replies that need no action, and the draft signature setting."""

from datetime import UTC, datetime
from typing import Any

import pytest

from app.classify.classifier import Classification
from app.config.settings import Settings
from app.examples.signature import strip_signature, with_signature
from app.ingest.deps import IngestConfig
from app.slack.digest import (
    DigestEntry,
    build_digest,
    collect,
    entry_text,
    post_digest,
    seconds_until_hour,
    short_campaign,
)
from tests.conftest import REQUIRED
from tests.fakedb import FakeDb
from tests.test_c2 import FakeClassifier, FakeSlack, make_deps, run
from tests.test_webhook import rows

CONFIG = IngestConfig(draft_channel="drafts", alert_channel="alerts")


def waiting(label: str, confidence: float = 0.95) -> FakeDb:
    db = FakeDb()
    run(make_deps(db, FakeClassifier(Classification(label, confidence, "r", "llm")), config=CONFIG))
    return db


# ------------------------------------------------------------------ the message


def entry(label: str, name: str, campaign: str = "GMC - M&A GOOGLE RESEND 04/11", **kw: Any):
    return DigestEntry(label, name, campaign, kw.pop("url", None), kw.pop("detail", ""))


def test_one_group_line_per_type_with_the_count_and_a_linked_name() -> None:
    url = "https://app.smartlead.ai/app/master-inbox?action=INBOX&leadMap=1"
    entries = [
        entry("do_not_contact", "Paxton Smith", url=url),
        entry("do_not_contact", "John"),
        entry("do_not_contact", "Alex", "GMC - PE GOOGLE RESEND 04/11"),
        entry("out_of_office", "Maelis", detail="until Nov 30"),
    ]
    text, blocks = build_digest(entries)
    assert text == "No action needed: 4 replies"
    assert blocks[0] == {"type": "header", "text": {"type": "plain_text", "text": text}}
    first = blocks[1]["text"]["text"]
    assert first.startswith("⛔ *Unsubscribe (3):* ")
    assert f"<{url}|Paxton Smith> (M&A GOOGLE RESEND 04/11)" in first.replace("&amp;", "&")
    assert (
        "John (M&amp;A GOOGLE RESEND 04/11)" in first and "Alex (PE GOOGLE RESEND 04/11)" in first
    )
    assert blocks[2]["text"]["text"].startswith("🔕 *Out of office (1):* Maelis")
    assert "until Nov 30" in blocks[2]["text"]["text"] and len(blocks) == 3


def test_groups_come_in_a_fixed_order_and_empty_groups_are_left_out() -> None:
    entries = [entry("auto_reply_or_bounce", "a"), entry("not_interested", "b")]
    _, blocks = build_digest(entries)
    assert [b["text"]["text"].split()[0] for b in blocks[1:]] == ["👋", "🤖"]
    assert build_digest(entries[:1])[0] == "No action needed: 1 reply"


def test_names_are_escaped_and_the_campaign_loses_its_gmc_prefix() -> None:
    assert short_campaign("GMC - PAVING RELOCATION 27/10") == "PAVING RELOCATION 27/10"
    assert short_campaign("gmc – Other") == "Other" and short_campaign("") == "(unknown)"
    text = entry_text(entry("wrong_person", "A <b> | C", url="https://x.example/y"))
    assert "<https://x.example/y|A &lt;b&gt; / C>" in text


def test_a_long_group_is_split_into_sections_that_fit_slack() -> None:
    entries = [entry("out_of_office", f"Person {n}" * 3) for n in range(300)]
    _, blocks = build_digest(entries)
    sections = blocks[1:]
    assert len(sections) > 1 and all(len(b["text"]["text"]) <= 3000 for b in sections)
    assert sections[0]["text"]["text"].startswith("🔕 *Out of office (300):*")
    assert sections[1]["text"]["text"].startswith("🔕 *Out of office:*")


# ------------------------------------------------------------------ what gets stored and posted


@pytest.mark.parametrize(
    "label",
    ["do_not_contact", "not_interested", "out_of_office", "wrong_person", "auto_reply_or_bounce"],
)
def test_the_summary_lists_each_waiting_reply_and_resolves_it(label: str) -> None:
    db = waiting(label)
    slack = FakeSlack()
    assert post_digest(db, slack, "alerts", CONFIG) == 1
    (post_,) = slack.posts
    assert post_["channel"] == "alerts" and post_["unfurl_links"] is False
    assert "Jane" in str(post_["blocks"]) and "Example Campaign" in str(post_["blocks"])
    assert rows(db, "slack_cards")[0]["status"] == "resolved"
    assert post_digest(db, slack, "alerts", CONFIG) == 0 and len(slack.posts) == 1  # never twice


def test_without_an_alert_channel_nothing_is_posted_and_the_reply_stays_stored() -> None:
    db = waiting("do_not_contact")
    slack = FakeSlack()
    assert post_digest(db, slack, None, CONFIG) == 0 and slack.posts == []
    assert rows(db, "slack_cards")[0]["status"] == "pending"
    assert post_digest(db, slack, "alerts", CONFIG) == 1  # set the channel later: it is listed


def test_nothing_waiting_posts_nothing() -> None:
    slack = FakeSlack()
    assert post_digest(FakeDb(), slack, "alerts", CONFIG) == 0 and slack.posts == []


def test_a_reply_that_got_a_card_is_never_listed() -> None:
    db = FakeDb()
    run(
        make_deps(db, FakeClassifier(Classification("off_script", 0.95, "r", "llm")), config=CONFIG)
    )
    assert collect(db, CONFIG) == []


def test_details_come_from_the_reply_and_the_confidence() -> None:
    ((_, ni),) = collect(waiting("not_interested", 0.95), CONFIG)
    assert ni.detail == "suggest: close" and "Jane" in ni.name
    ((_, ni_low),) = collect(waiting("not_interested", 0.85), CONFIG)
    assert ni_low.detail == ""
    ((_, dnc),) = collect(waiting("do_not_contact", 0.6), CONFIG)
    assert dnc.detail == "low confidence: check the label"


def test_each_name_links_to_the_thread_in_smartlead() -> None:
    ((_, found),) = collect(waiting("out_of_office"), CONFIG)
    assert found.url and found.url.startswith("https://app.smartlead.ai/app/master-inbox")


def test_the_summary_hour() -> None:
    at = lambda h, m=0: datetime(2026, 11, 16, h, m, tzinfo=UTC)  # noqa: E731
    assert seconds_until_hour(14, at(13, 0)) == 3600
    assert seconds_until_hour(14, at(14, 0)) == 24 * 3600  # strictly the next one
    assert seconds_until_hour(14, at(15, 30)) == 22.5 * 3600
    assert seconds_until_hour(0, at(23, 59)) == 60


def test_digest_hour_setting() -> None:
    assert Settings(**REQUIRED).DIGEST_HOUR == 14
    assert Settings(**REQUIRED, DIGEST_HOUR="").DIGEST_HOUR == 14
    assert Settings(**REQUIRED, DIGEST_HOUR="9").DIGEST_HOUR == 9
    for bad in ("24", "-1"):
        with pytest.raises(ValueError):
            Settings(**REQUIRED, DIGEST_HOUR=bad)


# ------------------------------------------------------------------ the signature


SIGNED = "Hey Jane,\n\nHappy to explain.\n\nBest,\n\nAryan Aryal\n\nCEO | Gamic Media"


def test_a_sign_off_is_removed_so_the_draft_ends_after_its_last_sentence() -> None:
    assert strip_signature(SIGNED) == "Hey Jane,\n\nHappy to explain."
    assert strip_signature("Hi,\n\nSounds good.\n\nThanks,\nRay") == "Hi,\n\nSounds good."
    assert strip_signature("Hi,\n\nSounds good.\n\nBest regards,") == "Hi,\n\nSounds good."
    assert "Best" not in with_signature(SIGNED, "")


def test_real_sentences_are_never_cut() -> None:
    for text in (
        "Hi,\n\nThanks,\nbut would [TIME OPTIONS] work?",
        "Hi,\n\nThanks for reaching out. Would Tuesday work?",
        "Hey Jane,\n\nBest of luck with the move.",
        "Hi,\n\nThanks,\n\nI will send the list over once we have spoken for a few minutes.",
    ):
        assert strip_signature(text) == text


def test_a_configured_signature_is_added_once() -> None:
    assert with_signature(SIGNED, "Best,\\nRay") == "Hey Jane,\n\nHappy to explain.\n\nBest,\nRay"
    assert with_signature("Hello.", "  ") == "Hello."
    assert Settings(**REQUIRED).DRAFT_SIGNATURE == ""
