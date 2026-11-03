import pytest

from app.config.settings import get_settings
from app.smartlead.categories import CategoryKind, load_categories, parse_categories
from app.smartlead.errors import CategoryWriteRefused, SmartleadError
from tests.test_smartlead_client import LEAD, make_client, ok

REPLY_LABELS = {1, 2, 3, 4, 5, 6, 7, 8, 9, 1589, 1995, 96272, 100396, 157189, 157190}
TRIGGERS = {95974, 113794, 126961, 131581, 131680, 134584, 144352, 170257, 185019}
UNKNOWN = {98680, 118216}


def ids_of(kind: CategoryKind) -> set[int]:
    return {c.id for c in load_categories().values() if c.kind is kind}


def enabled_client():
    return make_client(ok(), send_enabled=True, category_writeback_enabled=True)


def test_yaml_matches_the_agreed_allowlist() -> None:
    assert len(load_categories()) == 26
    assert ids_of(CategoryKind.REPLY_LABEL) == REPLY_LABELS
    assert ids_of(CategoryKind.WORKFLOW_TRIGGER) == TRIGGERS
    assert ids_of(CategoryKind.UNKNOWN) == UNKNOWN


@pytest.mark.parametrize("category_id", sorted(TRIGGERS | UNKNOWN))
def test_trigger_and_unknown_categories_are_refused(category_id: int) -> None:
    client, rec, _ = enabled_client()  # both flags on: the kind alone must block it
    with pytest.raises(CategoryWriteRefused, match="not a reply_label"):
        client.update_lead_category(LEAD, category_id)
    assert rec.requests == []


def test_category_missing_from_yaml_is_refused() -> None:
    client, rec, _ = enabled_client()
    with pytest.raises(CategoryWriteRefused, match="not in smartlead_categories.yaml"):
        client.update_lead_category(LEAD, 999999)
    assert rec.requests == []


def test_refused_when_send_disabled() -> None:
    client, rec, _ = make_client(ok(), send_enabled=False, category_writeback_enabled=True)
    with pytest.raises(CategoryWriteRefused, match="SEND_ENABLED"):
        client.update_lead_category(LEAD, 3)
    assert rec.requests == []


def test_refused_when_writeback_disabled() -> None:
    client, rec, _ = make_client(ok(), send_enabled=True, category_writeback_enabled=False)
    with pytest.raises(CategoryWriteRefused, match="CATEGORY_WRITEBACK_ENABLED"):
        client.update_lead_category(LEAD, 3)
    assert rec.requests == []


def test_both_flags_default_off() -> None:
    client, rec, _ = make_client(ok())
    with pytest.raises(CategoryWriteRefused):
        client.update_lead_category(LEAD, 3)
    assert rec.requests == []


@pytest.mark.parametrize("category_id", sorted(REPLY_LABELS))
def test_reply_label_reaches_request_builder_which_is_not_implemented(category_id: int) -> None:
    client, rec, _ = enabled_client()
    with pytest.raises(NotImplementedError):
        client.update_lead_category(LEAD, category_id)
    assert rec.requests == []  # nothing is ever sent until the endpoint is confirmed


def test_writeback_setting_defaults_false(env: None) -> None:
    assert get_settings().CATEGORY_WRITEBACK_ENABLED is False


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"categories": []},
        {"categories": [{"id": 1, "name": "x", "kind": "bogus"}]},
        {"categories": [{"id": 1, "name": "x"}]},
        {"categories": [{"id": 1, "name": "x", "kind": "reply_label"}] * 2},
    ],
)
def test_malformed_category_file_raises(data: object) -> None:
    with pytest.raises(SmartleadError):
        parse_categories(data)
