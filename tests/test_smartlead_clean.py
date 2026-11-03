import json
from pathlib import Path

import pytest

from app.smartlead.clean import clean_body
from app.smartlead.errors import ParseError
from app.smartlead.inbox import inbox_items, lead_from_inbox_item
from app.smartlead.thread import latest_reply_stats_id, parse_thread

FIXTURES = Path(__file__).parent / "fixtures"


def test_strips_html_and_keeps_paragraphs() -> None:
    assert clean_body("<p>Hello&nbsp;there</p><p>Call me.</p>") == "Hello there\n\nCall me."


def test_drops_gmail_quote_block() -> None:
    html = '<div>Yes please.</div><div class="gmail_quote"><div>old</div></div>'
    assert clean_body(html) == "Yes please."


def test_void_tag_inside_quote_does_not_swallow_following_text() -> None:
    html = "<p>Yes.</p><blockquote>old<br>older</blockquote><p>Ok, thanks.</p>"
    assert clean_body(html) == "Yes.\n\nOk, thanks."


def test_cuts_on_wrote_quote() -> None:
    body = "Sounds good.\n\nOn Mon, Oct 5, 2026 at 9:00 AM Ray <r@g.com> wrote:\n> earlier text"
    assert clean_body(body) == "Sounds good."


def test_cuts_on_wrote_wrapped_over_two_lines() -> None:
    body = "Thanks.\n\nOn Mon, Oct 5, 2026 at 9:00 AM Ray\n<r@g.com> wrote:\n> earlier"
    assert clean_body(body) == "Thanks."


def test_cuts_outlook_header_block() -> None:
    body = "Not now.\n\nFrom: Ray <r@g.com>\nSent: Monday\nTo: Jane\nSubject: Re: hi\n\nold"
    assert clean_body(body) == "Not now."


def test_does_not_cut_ordinary_from_line() -> None:
    assert clean_body("From: my experience this works.") == "From: my experience this works."


@pytest.mark.parametrize(
    "tail", ["--\nJane Doe\nCEO", "-- \nJane", "Sent from my iPhone", "Get Outlook for iOS"]
)
def test_strips_signature_residue(tail: str) -> None:
    assert clean_body(f"Let's talk.\n\n{tail}") == "Let's talk."


def test_drops_quoted_lines() -> None:
    assert clean_body("Reply\n> quoted\n> more") == "Reply"


def _entry(**overrides: object) -> dict:
    entry = {
        "stats_id": "s1",
        "type": "SENT",
        "time": "2026-10-01T10:00:00.000Z",
        "email_body": "a",
    }
    return {**entry, **overrides}


def test_parse_thread_sorts_oldest_first_and_keeps_stats_id() -> None:
    payload = {
        "history": [
            _entry(stats_id="s2", type="REPLY", time="2026-10-02T10:00:00.000Z", email_body="b"),
            _entry(),
        ]
    }
    messages = parse_thread(payload)
    assert [m.text for m in messages] == ["a", "b"]
    assert [m.direction.value for m in messages] == ["SENT", "REPLY"]
    assert [m.stats_id for m in messages] == ["s1", "s2"]


def test_parse_thread_accepts_inbox_item_email_history() -> None:
    item = inbox_items(json.loads((FIXTURES / "inbox_replies.json").read_text()))[0]
    messages = parse_thread(item)
    assert [m.direction.value for m in messages] == ["SENT", "REPLY"]
    assert latest_reply_stats_id(messages) == "11111111-aaaa-4aaa-8aaa-000000000001"


def test_latest_reply_stats_id_none_without_reply() -> None:
    assert latest_reply_stats_id(parse_thread({"history": [_entry()]})) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"nope": 1},
        [_entry()],  # bare list is not a Smartlead shape
        {"history": [_entry(type="WEIRD")]},
        {"history": [_entry(type="inbound")]},  # old guessed vocabulary must not pass
        {"history": [{k: v for k, v in _entry().items() if k != "time"}]},
        {"history": [{k: v for k, v in _entry().items() if k != "stats_id"}]},
        {"history": [{k: v for k, v in _entry().items() if k != "email_body"}]},
        {"history": [_entry(time="yesterday")]},
    ],
)
def test_parse_thread_raises_on_unexpected_shape(payload: object) -> None:
    with pytest.raises(ParseError):
        parse_thread(payload)


def test_lead_from_inbox_item_converts_string_ids() -> None:
    item = inbox_items(json.loads((FIXTURES / "inbox_replies.json").read_text()))[0]
    lead = lead_from_inbox_item(item)
    assert (lead.campaign_id, lead.lead_id, lead.email) == (101, 555, "jane@example.com")


@pytest.mark.parametrize("payload", [{"ok": False, "data": []}, {"ok": True}, [], "x"])
def test_inbox_items_rejects_bad_payload(payload: object) -> None:
    with pytest.raises(ParseError):
        inbox_items(payload)


def test_lead_from_inbox_item_raises_when_ids_missing() -> None:
    with pytest.raises(ParseError):
        lead_from_inbox_item({"lead_email": "a@b.com"})
