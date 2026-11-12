from datetime import UTC, datetime, timedelta

import pytest

from app.db import client
from app.db.records import (
    ActionType,
    CardLane,
    Decision,
    DraftStatus,
    NewAction,
    NewClassification,
    NewDecision,
    NewDraft,
    NewEvalResult,
    NewGoldLabel,
    NewMessage,
    NewQualification,
    NewRawEvent,
    NewRetry,
    NewSlackCard,
    NewThread,
    RawSource,
    RetryKind,
    RetryStatus,
    SkipReason,
    ThreadStatus,
    Verdict,
    payload_hash,
    to_row,
)
from app.db.suppressions import SuppressionScope, build_suppression
from app.smartlead.models import Direction
from app.smartlead.models import Message as ParsedMessage
from tests.fakedb import FakeDb

T0 = datetime(2026, 10, 19, 19, 5, 15, tzinfo=UTC)


def make_thread(db: FakeDb, **kw) -> dict:
    return client.upsert_thread(
        db, NewThread(email_lead_id=555, email_campaign_id=101, lead_email="jane@example.com", **kw)
    )


def message(thread_id: str, **overrides) -> NewMessage:
    fields = {
        "thread_id": thread_id,
        "direction": Direction.REPLY,
        "sent_at": T0,
        "stats_id": "stats-1",
        "message_id": "<m1@example.com>",
        "from_address": "jane@example.com",
        "to_address": "ray@gamicmedia.example",
        "body_clean": "Sounds good.",
    }
    return NewMessage(**{**fields, **overrides})


# -- serialization -----------------------------------------------------------------


def test_payload_hash_ignores_key_order() -> None:
    assert payload_hash({"a": 1, "b": {"c": 2}}) == payload_hash({"b": {"c": 2}, "a": 1})
    assert payload_hash({"a": 1}) != payload_hash({"a": 2})
    assert len(payload_hash({})) == 64


def test_to_row_drops_none_and_converts_enums_and_datetimes() -> None:
    row = to_row(message("t1", body_html=None))
    assert row["direction"] == "REPLY"
    assert row["sent_at"] == T0.isoformat()
    assert "body_html" not in row


def test_to_row_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="naive"):
        to_row(message("t1", sent_at=datetime(2026, 1, 1)))


# -- raw_events --------------------------------------------------------------------


def test_raw_event_insert_is_idempotent() -> None:
    db = FakeDb()
    event = NewRawEvent.from_payload(RawSource.SMARTLEAD_WEBHOOK, {"lead": 1})
    first = client.insert_raw_event(db, event)
    again = client.insert_raw_event(db, event)
    assert first.is_new and not again.is_new
    assert again.id == first.id and len(db.tables["raw_events"]) == 1
    other = client.insert_raw_event(
        db, NewRawEvent.from_payload(RawSource.SMARTLEAD_WEBHOOK, {"lead": 2})
    )
    assert other.is_new and len(db.tables["raw_events"]) == 2


def test_same_payload_from_another_source_is_a_different_event() -> None:
    db = FakeDb()
    client.insert_raw_event(db, NewRawEvent.from_payload(RawSource.SMARTLEAD_WEBHOOK, {"a": 1}))
    result = client.insert_raw_event(db, NewRawEvent.from_payload(RawSource.MANUAL, {"a": 1}))
    assert result.is_new


def test_unprocessed_events_and_marking_processed() -> None:
    db = FakeDb()
    a = client.insert_raw_event(db, NewRawEvent.from_payload(RawSource.SLACK, {"n": 1}))
    client.insert_raw_event(db, NewRawEvent.from_payload(RawSource.SLACK, {"n": 2}))
    db.tables["raw_events"][0]["processed"] = False
    db.tables["raw_events"][1]["processed"] = False
    client.mark_event_processed(db, a.id, T0, error="boom")
    pending = client.fetch_unprocessed_events(db)
    assert len(pending) == 1
    done = db.tables["raw_events"][0]
    assert done["processed"] is True and done["process_error"] == "boom"


# -- threads -----------------------------------------------------------------------


def test_upsert_thread_reuses_row_and_never_resets_status() -> None:
    db = FakeDb()
    first = make_thread(db)
    client.set_thread_status(db, first["id"], ThreadStatus.DRAFTED)
    second = make_thread(db, smartlead_category_id=1)
    assert second["id"] == first["id"] and len(db.tables["threads"]) == 1
    assert second["status"] == "drafted"
    assert second["smartlead_category_id"] == 1


def test_find_and_get_thread() -> None:
    db = FakeDb()
    thread = make_thread(db)
    assert client.find_thread(db, 555, 101)["id"] == thread["id"]
    assert client.find_thread(db, 555, 999) is None
    assert client.get_thread(db, thread["id"])["lead_email"] == "jane@example.com"


# -- messages ----------------------------------------------------------------------


def test_replayed_message_is_not_duplicated() -> None:
    db = FakeDb()
    tid = make_thread(db)["id"]
    first = client.insert_message(db, message(tid))
    replay = client.insert_message(db, message(tid))
    assert first.is_new and not replay.is_new
    assert replay.id == first.id and len(db.tables["messages"]) == 1


def test_shared_stats_id_does_not_drop_a_real_message() -> None:
    db = FakeDb()
    tid = make_thread(db)["id"]
    sent = client.insert_message(
        db, message(tid, direction=Direction.SENT, message_id="<sent@x>", stats_id="shared")
    )
    reply = client.insert_message(db, message(tid, message_id="<reply@x>", stats_id="shared"))
    assert sent.is_new and reply.is_new and sent.id != reply.id


def test_same_direction_and_time_but_different_message_id_is_kept() -> None:
    db = FakeDb()
    tid = make_thread(db)["id"]
    client.insert_message(db, message(tid, message_id="<a@x>"))
    assert client.insert_message(db, message(tid, message_id="<b@x>")).is_new


def test_same_message_in_another_thread_is_kept() -> None:
    db = FakeDb()
    t1 = make_thread(db)["id"]
    t2 = client.upsert_thread(
        db, NewThread(email_lead_id=556, email_campaign_id=101, lead_email="b@example.com")
    )["id"]
    client.insert_message(db, message(t1))
    assert client.insert_message(db, message(t2)).is_new


def test_fetch_messages_ordered_and_scoped_to_thread() -> None:
    db = FakeDb()
    t1 = make_thread(db)["id"]
    t2 = client.upsert_thread(
        db, NewThread(email_lead_id=556, email_campaign_id=101, lead_email="b@example.com")
    )["id"]
    late = (T0 + timedelta(days=1)).isoformat()
    client.insert_message(db, message(t1, message_id="<late@x>", sent_at=T0 + timedelta(days=1)))
    client.insert_message(db, message(t1, message_id="<early@x>"))
    client.insert_message(db, message(t2, message_id="<other@x>"))
    rows = client.fetch_messages(db, t1)
    assert [r["message_id"] for r in rows] == ["<early@x>", "<late@x>"]
    assert rows[1]["sent_at"] == late


# -- the remaining record types ----------------------------------------------------


def test_classification_qualification_round_trip() -> None:
    db = FakeDb()
    tid = make_thread(db)["id"]
    mid = client.insert_message(db, message(tid)).id
    cls = client.insert_classification(
        db,
        NewClassification(
            thread_id=tid,
            reply_message_id=mid,
            label="meeting_request",
            confidence=0.92,
            reason="asks for times",
            model="small",
            prompt_version="v1",
            smartlead_category_id=2,
            agrees_with_smartlead=True,
        ),
    )
    qual = client.insert_qualification(
        db,
        NewQualification(
            thread_id=tid, verdict=Verdict.FLAG, icp_version="v1", reasons=["small firm"]
        ),
    )
    assert client.fetch_classifications(db, tid)[0]["id"] == cls["id"]
    assert cls["agrees_with_smartlead"] is True and "rule_hit" not in cls
    fetched = client.fetch_qualifications(db, tid)[0]
    assert fetched["id"] == qual["id"]
    assert fetched["verdict"] == "flag" and fetched["reasons"] == ["small firm"]


def test_draft_card_action_lifecycle() -> None:
    db = FakeDb()
    tid = make_thread(db)["id"]
    draft = client.insert_draft(
        db,
        NewDraft(
            thread_id=tid,
            classification_id="c1",
            template_id="T1",
            template_version="2026-11-03",
            text="Hi NAME",
            validator_ok=True,
            slots={"NAME": "Jane"},
        ),
    )
    assert draft["status"] == "proposed" and draft["slots"] == {"NAME": "Jane"}
    assert draft["template_version"] == "2026-11-03"
    edited = client.set_draft_status(
        db, draft["id"], DraftStatus.EDITED, final_text="Hi Jane", human_edited=True
    )
    assert (edited["status"], edited["final_text"], edited["human_edited"]) == (
        "edited",
        "Hi Jane",
        True,
    )

    card = client.insert_slack_card(
        db, NewSlackCard(thread_id=tid, lane=CardLane.DRAFT, channel="C1", draft_id=draft["id"])
    )
    client.update_slack_card(db, card["id"], {"slack_ts": "123.4", "status": "posted"})
    assert client.fetch_slack_cards(db, tid)[0]["slack_ts"] == "123.4"

    action = client.insert_action(
        db,
        NewAction(thread_id=tid, type=ActionType.PAUSE, triggered_by="bot", reversible=True),
    )
    client.mark_action_reversed(db, action["id"], T0)
    assert client.fetch_actions(db, tid)[0]["reversed_at"] == T0.isoformat()
    assert [len(client.fetch_drafts(db, tid))] == [1]


def test_fetchers_are_scoped_to_their_thread() -> None:
    db = FakeDb()
    t1 = make_thread(db)["id"]
    t2 = client.upsert_thread(
        db, NewThread(email_lead_id=1, email_campaign_id=1, lead_email="c@example.com")
    )["id"]
    client.insert_action(
        db, NewAction(thread_id=t2, type=ActionType.IGNORE, triggered_by="bot", reversible=False)
    )
    assert client.fetch_actions(db, t1) == []


def test_suppression_row_carries_scope_as_text() -> None:
    db = FakeDb()
    s = build_suppression("A@B.com", SuppressionScope.CAMPAIGN, "not interested", campaign_id=101)
    row = client.insert_suppression(db, s)
    assert row["scope"] == "campaign" and row["email"] == "a@b.com"
    assert client.fetch_suppressions(db, " A@B.COM ")[0]["id"] == row["id"]
    g = build_suppression("g@b.com", SuppressionScope.GLOBAL, "legal", confirmed_by="human:U1")
    assert client.insert_suppression(db, g)["confirmed_by"] == "human:U1"


def test_retry_queue_due_and_failure_flow() -> None:
    db = FakeDb()
    due = client.enqueue_retry(
        db,
        NewRetry(kind=RetryKind.SLACK_POST, payload={"x": 1}, next_attempt_at=T0),
    )
    client.enqueue_retry(
        db,
        NewRetry(kind=RetryKind.CLASSIFY, payload={}, next_attempt_at=T0 + timedelta(hours=1)),
    )
    for row in db.tables["retry_queue"]:
        row["status"] = "pending"
    assert [r["id"] for r in client.fetch_due_retries(db, T0)] == [due["id"]]

    later = T0 + timedelta(minutes=5)
    client.record_retry_failure(db, due["id"], attempts=1, error="429", next_attempt_at=later)
    assert db.tables["retry_queue"][0]["attempts"] == 1
    assert client.fetch_due_retries(db, T0) == []
    client.record_retry_failure(
        db, due["id"], attempts=5, error="gave up", next_attempt_at=later, dead=True
    )
    assert db.tables["retry_queue"][0]["status"] == RetryStatus.DEAD.value
    client.finish_retry(db, due["id"])
    assert db.tables["retry_queue"][0]["status"] == "done"


def test_eval_results_newest_first() -> None:
    db = FakeDb()
    client.insert_eval_result(db, NewEvalResult("r1", {"acc": 0.9}, "abc1234"))
    client.insert_eval_result(db, NewEvalResult("r2", {"acc": 0.95}, "abc1235"))
    db.tables["eval_results"][0]["created_at"] = "2026-01-01"
    db.tables["eval_results"][1]["created_at"] = "2026-02-01"
    assert [r["run_id"] for r in client.fetch_eval_results(db)] == ["r2", "r1"]


def make_draft(db: FakeDb, thread_id: str) -> dict:
    return client.insert_draft(
        db,
        NewDraft(
            thread_id=thread_id,
            classification_id="c1",
            template_id="T2",
            template_version="v1",
            text="Hi Jane",
            validator_ok=True,
        ),
    )


def test_message_built_from_parsed_message_matches_the_dedupe_key() -> None:
    db = FakeDb()
    tid = make_thread(db)["id"]
    parsed = ParsedMessage(
        direction=Direction.REPLY,
        timestamp=T0,
        text="Yes please.",
        stats_id="s1",
        message_id="<r1@example.com>",
        from_addr="jane@example.com",
        to_addr="ray@gamicmedia.example",
    )
    row = to_row(NewMessage.from_message(tid, parsed))
    assert row["from_address"] == "jane@example.com" and row["body_clean"] == "Yes please."
    assert row["message_id"] == "<r1@example.com>" and row["sent_at"] == T0.isoformat()
    assert client.insert_message(db, NewMessage.from_message(tid, parsed)).is_new
    assert not client.insert_message(db, NewMessage.from_message(tid, parsed)).is_new


def test_extracted_facts_and_vertical_are_stored() -> None:
    db = FakeDb()
    thread = make_thread(db, vertical="M&A advisory")
    assert thread["vertical"] == "M&A advisory"
    mid = client.insert_message(db, message(thread["id"])).id
    cls = client.insert_classification(
        db,
        NewClassification(
            thread_id=thread["id"],
            reply_message_id=mid,
            label="out_of_office",
            confidence=0.99,
            reason="auto reply",
            model="small",
            prompt_version="v1",
            extracted_facts={"return_date": "2026-11-10", "referred": None},
        ),
    )
    assert cls["extracted_facts"] == {"return_date": "2026-11-10", "referred": None}


def test_decision_is_one_per_draft_and_first_wins() -> None:
    db = FakeDb()
    draft = make_draft(db, make_thread(db)["id"])
    first = client.insert_decision(
        db,
        NewDecision(
            draft_id=draft["id"],
            decided_by="U012ABCDEF",
            decision=Decision.EDITED,
            final_text="Hi Jane, edited",
            edit_distance_pct=12.5,
            decided_at=T0,
        ),
    )
    second = client.insert_decision(
        db,
        NewDecision(
            draft_id=draft["id"],
            decided_by="U099ZZZZZZ",
            decision=Decision.SKIPPED,
            skip_reason=SkipReason.OTHER,
        ),
    )
    assert first.is_new and not second.is_new
    assert second.id == first.id and len(db.tables["decisions"]) == 1
    stored = client.fetch_decision(db, draft["id"])
    assert stored["decision"] == "edited" and stored["decided_by"] == "U012ABCDEF"
    assert stored["edit_distance_pct"] == 12.5 and stored["decided_at"] == T0.isoformat()
    assert client.fetch_decision(db, "no-such-draft") is None


def test_skipped_decision_serialises_reason_as_text() -> None:
    row = to_row(
        NewDecision(
            draft_id="d1",
            decided_by="U012ABCDEF",
            decision=Decision.SKIPPED,
            skip_reason=SkipReason.NOT_WORTH_REPLYING,
        )
    )
    assert row == {
        "draft_id": "d1",
        "decided_by": "U012ABCDEF",
        "decision": "skipped",
        "skip_reason": "not_worth_replying",
    }


def test_gold_label_upsert_corrects_the_same_labelers_label() -> None:
    db = FakeDb()
    tid = make_thread(db)["id"]
    first = client.upsert_gold_label(
        db,
        NewGoldLabel(
            thread_id=tid,
            human_message_type="meeting_request",
            human_verdict=Verdict.PASS,
            labeler="ray",
            human_template="T1",
        ),
    )
    fixed = client.upsert_gold_label(
        db,
        NewGoldLabel(
            thread_id=tid,
            human_message_type="info_request",
            human_verdict=Verdict.FLAG,
            labeler="ray",
            notes="re-read it",
        ),
    )
    other = client.upsert_gold_label(
        db,
        NewGoldLabel(
            thread_id=tid,
            human_message_type="info_request",
            human_verdict=Verdict.FLAG,
            labeler="aditya",
        ),
    )
    assert fixed["id"] == first["id"] and other["id"] != first["id"]
    assert fixed["human_message_type"] == "info_request" and fixed["human_verdict"] == "flag"
    assert len(client.fetch_gold_labels(db, tid)) == 2
    assert len(client.fetch_gold_labels(db)) == 2
    assert client.fetch_gold_labels(db, "other-thread") == []


def test_missing_row_errors_are_clear() -> None:
    db = FakeDb()
    with pytest.raises(client.DatabaseError):
        client.set_thread_status(db, "nope", ThreadStatus.CLOSED)
