"""scripts/check_reply_target.py: the read-only look at which stats_id a reply would use."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import check_reply_target as crt  # noqa: E402

from app.smartlead.models import Lead, Thread  # noqa: E402
from app.smartlead.thread import latest_reply_stats_id, parse_thread  # noqa: E402
from tests.fakedb import FakeDb  # noqa: E402
from tests.test_webhook import HISTORY  # noqa: E402


def fetched_thread() -> Thread:
    messages = parse_thread(HISTORY)
    return Thread(
        Lead(101, 555, "jane@example.com"),
        messages,
        latest_reply_stats_id(messages),
        "jane@example.com",
    )


def test_it_names_the_stats_id_and_marks_the_message_we_would_reply_to() -> None:
    lines = crt.describe(fetched_thread())
    text = "\n".join(lines)
    assert "reply_to_stats_id = 11111111-aaaa-4aaa-8aaa-000000000001" in text
    assert "campaign 101, lead 555" in text and "3 messages" in text
    marked = [line for line in lines if "we would reply to this" in line]
    assert len(marked) == 1 and " REPLY " in marked[0]
    assert "a SENT message shares this stats_id" in text  # stats_id is not unique per message


def test_the_lead_is_masked_and_no_email_text_is_shown() -> None:
    text = "\n".join(crt.describe(fetched_thread()))
    assert "j***@e***.com" in text and "jane@example.com" not in text
    for message in parse_thread(HISTORY):
        assert message.text not in text if message.text else True


def test_it_compares_with_our_stored_reply() -> None:
    thread = fetched_thread()
    same = "\n".join(crt.describe(thread, "11111111-aaaa-4aaa-8aaa-000000000001"))
    assert "same stats_id" in same
    different = "\n".join(crt.describe(thread, "other-id"))
    assert "DIFFERENT stats_id other-id" in different


def test_a_thread_with_no_reply_says_so() -> None:
    sent_only = [m for m in parse_thread(HISTORY) if m.direction.value == "SENT"]
    thread = Thread(Lead(101, 555), sent_only, latest_reply_stats_id(sent_only), "jane@example.com")
    assert "There is no REPLY in this thread" in "\n".join(crt.describe(thread))


def test_the_stored_latest_reply_is_the_newest_one() -> None:
    db = FakeDb()
    db.tables["messages"] = [
        {
            "thread_id": "t",
            "direction": "REPLY",
            "sent_at": "2026-10-20T10:00:00+00:00",
            "stats_id": "old",
        },
        {
            "thread_id": "t",
            "direction": "SENT",
            "sent_at": "2026-10-21T10:00:00+00:00",
            "stats_id": "sent",
        },
        {
            "thread_id": "t",
            "direction": "REPLY",
            "sent_at": "2026-10-22T10:00:00+00:00",
            "stats_id": "new",
        },
    ]
    assert crt.stored_latest_reply_stats_id(db, {"id": "t"}) == "new"
    assert crt.stored_latest_reply_stats_id(db, {"id": "none"}) is None


def test_the_script_only_reads() -> None:
    source = Path(crt.__file__).read_text(encoding="utf-8")
    for word in ("send_reply", "unsubscribe_lead", "pause_lead", "update_lead_category", ".post("):
        assert word not in source
    assert "get_thread(" in source
