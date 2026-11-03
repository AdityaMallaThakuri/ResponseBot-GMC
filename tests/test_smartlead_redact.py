import json

from app.smartlead.redact import redact

PAYLOAD = {
    "lead": {"id": 7, "email": "jane@acme.com", "first_name": "Jane"},
    "last_message": {
        "body": "Hi Jane, reach me at jane@acme.com or later please thanks",
        "from": "Jane Doe <jane@acme.com>",
        "received_at": "2026-10-30T14:00:00Z",
    },
    "message_history": [{"direction": "inbound", "body": "secret text here"}],
    "empty": None,
}


def test_terminal_redaction_truncates_and_masks() -> None:
    out = redact(PAYLOAD)
    assert out["lead"]["id"] == "int"
    assert out["lead"]["first_name"] == "str[4]: Name"
    assert out["lead"]["email"] == "str[13]: user@example.com"
    assert out["last_message"]["from"] == "str[24]: user@example.com"
    assert out["last_message"]["body"] == "str[57]: Hi Jane, reach me at user"
    assert out["last_message"]["received_at"].endswith("2026-10-30T14:00:00Z")
    assert out["message_history"]["_len"] == 1
    assert out["message_history"]["_items"][0]["direction"] == "str[7]: inbound"
    assert out["empty"] == "null"


def test_file_redaction_has_no_free_text() -> None:
    text = json.dumps(redact(PAYLOAD, for_file=True))
    for leaked in ("jane", "acme", "Jane", "secret", "reach me"):
        assert leaked not in text
    assert "inbound" in text  # enum-like direction value is kept


def test_raw_keeps_config_values() -> None:
    cats = [{"id": 3, "name": "Interested"}]
    assert redact(cats, raw=True)["_items"] == cats
