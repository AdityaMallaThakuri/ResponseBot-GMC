"""Tests for scripts/replay_webhook.py, end to end against the real route with fakes behind it."""

import hashlib
import hmac
import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import replay_webhook as rw  # noqa: E402

from app.config.settings import get_settings  # noqa: E402
from app.ingest.deps import IngestDeps  # noqa: E402
from app.main import app  # noqa: E402
from tests.conftest import REQUIRED as GOOD_VALUES  # noqa: E402
from tests.fakedb import FakeDb  # noqa: E402
from tests.test_webhook import FakeSmartlead  # noqa: E402

SECRET = GOOD_VALUES["WEBHOOK_SECRET"]
PATH = "/webhooks/smartlead"


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, FakeDb]]:
    for key, value in GOOD_VALUES.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GMC_CAMPAIGN_IDS", "101")
    get_settings.cache_clear()
    db = FakeDb()
    app.state.ingest = IngestDeps(db=db, smartlead=FakeSmartlead())
    yield TestClient(app), db
    del app.state.ingest
    get_settings.cache_clear()


def test_signature_is_an_hmac_of_the_exact_body() -> None:
    body, headers = rw.build_request({"a": 1}, SECRET)
    expected = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert headers["X-Smartlead-Signature"] == f"sha256={expected}"
    assert SECRET.encode() not in body


def test_other_auth_modes_carry_the_secret_where_expected() -> None:
    body, headers = rw.build_request({"a": 1}, SECRET, "header")
    assert headers["X-Webhook-Secret"] == SECRET and b"secret_key" not in body
    body, headers = rw.build_request({"a": 1}, SECRET, "body")
    assert json.loads(body)["secret_key"] == SECRET and "X-Webhook-Secret" not in headers
    with pytest.raises(ValueError):
        rw.build_request({}, SECRET, "nope")


@pytest.mark.parametrize("auth", ["signature", "header", "body"])
def test_replaying_the_fixture_is_accepted_and_processed(
    server: tuple[TestClient, FakeDb], auth: str
) -> None:
    client, db = server
    response = rw.replay(PATH, rw.DEFAULT_FIXTURE, SECRET, auth, client=client)
    assert response.status_code == 200 and response.json() == {"status": "accepted"}
    assert db.tables["raw_events"][0]["processed"] is True
    assert "secret_key" not in db.tables["raw_events"][0]["payload"]


def test_replaying_twice_is_a_duplicate_unless_fresh(server: tuple[TestClient, FakeDb]) -> None:
    client, db = server
    rw.replay(PATH, rw.DEFAULT_FIXTURE, SECRET, client=client)
    assert rw.replay(PATH, rw.DEFAULT_FIXTURE, SECRET, client=client).json() == {
        "status": "duplicate"
    }
    assert rw.replay(PATH, rw.DEFAULT_FIXTURE, SECRET, fresh=True, client=client).json() == {
        "status": "accepted"
    }
    assert len(db.tables["raw_events"]) == 2


def test_a_wrong_secret_gets_a_401(server: tuple[TestClient, FakeDb]) -> None:
    client, db = server
    response = rw.replay(PATH, rw.DEFAULT_FIXTURE, "z" * 40, client=client)
    assert response.status_code == 401 and "raw_events" not in db.tables


def test_secret_comes_from_the_argument_or_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert rw.load_secret("given") == "given"
    (tmp_path / ".env").write_text("WEBHOOK_SECRET=from-env-file\n")
    monkeypatch.setattr(rw, "ROOT", tmp_path)
    assert rw.load_secret(None) == "from-env-file"
    (tmp_path / ".env").write_text("OTHER=1\n")
    with pytest.raises(SystemExit):
        rw.load_secret(None)


# ------------------------------------------------------------------ end to end with --check


def test_a_replayed_fixture_goes_all_the_way_to_a_slack_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_c2 import FakeClassifier, FakeSlack, llm, make_deps

    for key, value in GOOD_VALUES.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GMC_CAMPAIGN_IDS", "101")
    get_settings.cache_clear()
    db = FakeDb()
    deps = make_deps(db, FakeClassifier(llm("meeting_request", 0.9)), FakeSlack())
    app.state.ingest = deps
    try:
        client = TestClient(app)
        response = rw.replay(PATH, rw.DEFAULT_FIXTURE, SECRET, client=client)
        assert response.json() == {"status": "accepted"}
        assert len(db.tables["classifications"]) == 1 and len(deps.slack.posts) == 1
        lines = rw.check(db=db, sleep=lambda s: None)
    finally:
        del app.state.ingest
        get_settings.cache_clear()
    text = "\n".join(lines)
    assert "event: processed=True, process_error=none" in text
    assert "classification: meeting_request at 0.9" in text and "prompt version v-test" in text
    assert "slack card: lane human_only, status posted, channel C0TEST" in text
    assert "@" not in text  # no email text


def test_check_stops_waiting_when_the_event_never_finishes() -> None:
    db = FakeDb()
    db.tables["raw_events"] = [{"id": "e1", "processed": False, "created_at": "1"}]
    sleeps: list[float] = []
    lines = rw.check(wait=3.0, db=db, sleep=sleeps.append)
    assert len(sleeps) == 3 and lines[0] == "event: processed=False, process_error=none"
    assert "classification: none" in lines and "slack card: none" in lines
