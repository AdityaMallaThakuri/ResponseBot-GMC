"""Classifying a thread that is already stored: no Smartlead call, additive only."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import reclassify_stored as rs  # noqa: E402

from app.ingest.classify_step import StepError, classify_stored_thread  # noqa: E402
from app.ingest.deps import IngestConfig, IngestDeps  # noqa: E402
from app.ingest.process import process_reply_event  # noqa: E402
from tests.fakedb import FakeDb  # noqa: E402
from tests.test_c2 import FakeClassifier, FakeSlack, llm  # noqa: E402
from tests.test_webhook import EVENT, NOW, FakeSmartlead, rows  # noqa: E402


def stored_thread() -> tuple[FakeDb, str]:
    """A thread stored by C1 alone: no classification, no card (the situation to repair)."""
    db = FakeDb()
    db.tables.setdefault("raw_events", []).append({"id": "e1", "processed": False})
    c1 = IngestDeps(db=db, smartlead=FakeSmartlead(), clock=lambda: NOW)  # no classifier, no Slack
    outcome = process_reply_event(c1, "e1", EVENT)
    assert outcome.thread_id and not rows(db, "classifications")
    return db, outcome.thread_id


def c2_deps(db: FakeDb, classifier: FakeClassifier | None = None) -> IngestDeps:
    return IngestDeps(
        db=db,
        smartlead=rs.NoSmartlead(),
        clock=lambda: NOW,
        classifier=classifier or FakeClassifier(llm("meeting_request", 0.9)),
        slack=FakeSlack(),
        config=IngestConfig(draft_channel="reply-bot-test"),
    )


def test_a_stored_thread_is_classified_and_posted_without_touching_smartlead() -> None:
    db, thread_id = stored_thread()
    deps = c2_deps(db)
    before = {t: len(db.tables[t]) for t in ("raw_events", "threads", "messages")}
    assert classify_stored_thread(deps, thread_id) is True
    assert len(rows(db, "classifications")) == 1 and len(rows(db, "slack_cards")) == 1
    assert len(deps.slack.posts) == 1
    assert {t: len(db.tables[t]) for t in before} == before  # nothing else gained or lost
    event = rows(db, "raw_events")[0]
    assert event["processed"] is True and "process_error" not in event  # the event is untouched


def test_the_classifier_gets_the_stored_reply_campaign_and_outreach() -> None:
    db, thread_id = stored_thread()
    classifier = FakeClassifier()
    classify_stored_thread(c2_deps(db, classifier), thread_id)
    reply, campaign, outreach = classifier.calls[0]
    assert "send times" in reply and campaign == "GMC - Example Campaign" and outreach


def test_running_it_twice_posts_once() -> None:
    db, thread_id = stored_thread()
    deps = c2_deps(db)
    assert classify_stored_thread(deps, thread_id) is True
    assert classify_stored_thread(deps, thread_id) is False
    assert len(deps.slack.posts) == 1 and len(rows(db, "classifications")) == 1


def test_unknown_thread_and_thread_without_a_reply_are_refused() -> None:
    db, thread_id = stored_thread()
    with pytest.raises(StepError, match="not found"):
        classify_stored_thread(c2_deps(db), "no-such-thread")
    db.tables["messages"] = [m for m in db.tables["messages"] if m["direction"] == "SENT"]
    with pytest.raises(StepError, match="no stored lead reply"):
        classify_stored_thread(c2_deps(db), thread_id)


def test_the_smartlead_guard_raises_on_any_use() -> None:
    with pytest.raises(RuntimeError, match="must not call Smartlead"):
        rs.NoSmartlead().get_thread_raw(None)


def test_dry_run_changes_nothing_and_prints_no_email_text() -> None:
    db, thread_id = stored_thread()
    deps = c2_deps(db)
    lines = rs.run(deps, thread_id, dry_run=True)
    text = "\n".join(lines)
    assert "dry run" in text and "1 reply" in text and "slack cards: 0" in text
    assert "@" not in text and deps.slack.posts == [] and not rows(db, "classifications")


def test_run_reports_what_it_did() -> None:
    db, thread_id = stored_thread()
    deps = c2_deps(db)
    assert rs.run(deps, thread_id)[-1] == "classified and posted a card"
    assert rs.run(deps, thread_id)[-1] == "already has a Slack card: nothing to do"
    assert rs.run(deps, "missing")[0].endswith("not found")
