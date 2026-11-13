"""Campaign verticals: rules, the parent chain, and keeping M&A examples apart."""

from datetime import UTC, datetime, timedelta

import pytest

from app.db.client import fetch_examples_for_label, fetch_recent_examples, insert_example
from app.db.records import ExampleSource, NewExample
from app.examples.history import history_examples
from app.examples.library import Example, load_pool, select_examples
from app.examples.sop import sop_examples
from app.examples.verticals import (
    VerticalResolver,
    classify_name,
    is_ma,
    load_rules,
    parse_rules,
    root_campaign,
)
from tests.fakedb import FakeDb

RULES = load_rules()
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("name", "vertical"),
    [
        ("GMC - M&A June (US/Canada)  ", "ma"),
        ("GMC - MA BROAD 01/09", "ma"),
        ("GMC - PE (Deal Origination)", "ma"),
        ("GMC - IB NAMED 25/09", "ma"),
        ("GMC - IMPL PE BRIDGE 06/10", "ma"),
        ("GMC - ACQUIRERS owner map 14/10", "ma"),
        ("GMC - Austin Meet (M&A)", "ma"),
        ("GMC - WARM RE-ENGAGE 26/10", "ma"),
        ("GMC - FINANCE GOOGLE 26/10", "ma"),
        ("GMC - FINANCE MS 26/10", "ma"),
        ("GMC - Capital Advisory (Advisory & Placement)", "lenders"),
        ("GMC - CAPITAL RAISE 29/09", "lenders"),
        ("GMC - Commercial Lending (Capital Advisory)", "lenders"),
        ("GMC - LENDERS B2 borrower intros 14/10", "lenders"),
        ("GMC - Marketing Aug (Broad)", "marketing"),
        ("GMC - Austin Meet (Marketing)", "marketing"),
        ("GMC - FRD Lookalike", "marketing"),
        ("GMC - Competitor Engagement Monitor", "marketing"),
        ("GMC Internal II  - Video Agency", "marketing"),
        ("GMC - SOLAR RELOCATION 28/09", "solar"),
        ("GMC - FACILITIES LOOKALIKE 28/09", "facilities"),
        ("GMC - HVAC ELEC LOOKALIKE 28/09", "relocation"),
        ("GMC - PAVING RELOCATION 27/10", "relocation"),
        ("GMC - Accounting (A Control)", "other"),
        ("GMC - Fractional CFO July", "other"),
        ("GMC - FRANCHISORS conversion map 14/10", "other"),
        ("GMC - MEDDEV free list GOOGLE 19/10", "other"),
        ("GMC - INVEN USA TEST ", "unknown"),
        ("something nobody ruled on", "unknown"),
        ("", "unknown"),
    ],
)
def test_names_land_in_the_corrected_group(name: str, vertical: str) -> None:
    assert classify_name(RULES, name) == vertical


def test_campaign_id_beats_override_and_rules() -> None:
    rules = parse_rules({"campaign_ids": {7: "solar"}, "rules": []})
    assert classify_name(rules, "GMC - M&A", 7) == "solar"
    assert classify_name(rules, "GMC - M&A", 8) == "unknown"


def test_unknown_vertical_in_config_is_refused() -> None:
    with pytest.raises(ValueError):
        parse_rules({"campaign_ids": {1: "plumbing"}})


def test_only_ma_is_ma() -> None:
    assert is_ma("ma")
    assert not any(is_ma(v) for v in ("", None, "unknown", "lenders", "other"))


CAMPAIGNS = [
    {"id": 1, "name": "GMC - M&A May"},
    {"id": 2, "name": "GMC - M&A May POS REPLY", "parent_campaign_id": 1},
    {"id": 3, "name": "No-Show follow up", "parent_campaign_id": 2},
    {"id": 4, "name": "GMC - LENDERS 29/09"},
    {"id": 5, "name": "loop a", "parent_campaign_id": 6},
    {"id": 6, "name": "loop b", "parent_campaign_id": 5},
]


def test_child_takes_the_root_parents_vertical() -> None:
    resolver = VerticalResolver(lambda: CAMPAIGNS, RULES)
    assert resolver.for_campaign(2, "POS REPLY") == "ma"
    assert resolver.for_campaign(3, "No-Show follow up") == "ma"  # a child of a child
    assert resolver.for_campaign(4) == "lenders"


def test_parent_loop_stops() -> None:
    assert root_campaign(CAMPAIGNS, 5) is not None
    assert VerticalResolver(lambda: CAMPAIGNS, RULES).for_campaign(5) == "unknown"


def test_unreadable_campaign_list_falls_back_to_the_name() -> None:
    def broken() -> list[dict]:
        raise RuntimeError("down")

    resolver = VerticalResolver(broken, RULES)
    assert resolver.for_campaign(99, "GMC - LENDERS 29/09") == "lenders"
    assert resolver.for_campaign(None, "") == "unknown"


def test_unknown_id_refreshes_the_list_once() -> None:
    calls: list[int] = []
    lists = [[], CAMPAIGNS]

    def source() -> list[dict]:
        calls.append(1)
        return lists[min(len(calls) - 1, 1)]

    resolver = VerticalResolver(source, RULES)
    assert resolver.for_campaign(2, "x") == "ma"
    assert len(calls) == 2


def test_thread_uses_stored_vertical_first() -> None:
    resolver = VerticalResolver(lambda: CAMPAIGNS, RULES)
    assert resolver.for_thread({"vertical": "solar", "email_campaign_id": 1}) == "solar"
    assert resolver.for_thread({"email_campaign_id": 2, "campaign_name": "POS"}) == "ma"
    assert resolver.for_thread({"campaign_name": "GMC - LENDERS 29/09"}) == "lenders"


def ex(n: int, vertical: str, label: str = "info_request", **kw) -> Example:
    return Example(
        source=kw.get("source", "history"),
        source_thread_id=str(n),
        campaign_name="GMC - M&A May",
        label=label,
        prospect_reply=f"question {n}",
        gamic_reply=f"answer {n}",
        replied_at=T0 + timedelta(days=n),
        priority=kw.get("priority", 0),
        vertical=vertical,
    )


def test_ma_examples_never_reach_other_campaigns_and_back() -> None:
    pool = [ex(1, "ma"), ex(2, "ma", source="sop", priority=100), ex(3, "lenders"), ex(4, "")]
    for vertical in ("lenders", "other", "unknown", ""):
        picked = {
            e.source_thread_id
            for e in select_examples(pool, "info_request", "c", vertical=vertical)
        }
        assert picked == {"3", "4"}, vertical
    picked = {e.source_thread_id for e in select_examples(pool, "info_request", "c", vertical="ma")}
    assert picked == {"1", "2"}


def test_same_vertical_ranks_after_label_before_priority_and_recency() -> None:
    pool = [
        ex(1, "other", priority=50),
        ex(2, "lenders"),
        ex(3, "lenders", label="price_question"),  # wrong label: always after
    ]
    order = [
        e.source_thread_id for e in select_examples(pool, "info_request", "c", vertical="lenders")
    ]
    assert order == ["2", "1", "3"]


def test_sop_templates_are_ma_and_a_vertical_can_be_chosen() -> None:
    text = "## meeting_request | Meeting request\n\nHey [NAME],\n\nSure.\n"
    assert {e.vertical for e in sop_examples(text)[0]} == {"ma"}
    assert {e.vertical for e in sop_examples(text, vertical="lenders")[0]} == {"lenders"}


def test_history_rows_take_the_parents_vertical_through_the_campaign_id() -> None:
    resolver = VerticalResolver(lambda: CAMPAIGNS, RULES)
    dataset = [
        {
            "thread_id": "t1",
            "campaign_id": "2",
            "campaign_name": "POS REPLY (no clue)",
            "prospect_first_reply": "tell me more",
            "gamic_response_to_first_reply": "sure thing",
            "first_reply_time": "2026-01-01T00:00:00Z",
        }
    ]
    out, _ = history_examples(dataset, {"t1": "info_request"}, resolver)
    assert out[0].vertical == "ma"
    out, _ = history_examples(dataset, {"t1": "info_request"})
    assert out[0].vertical is None  # no resolver: the database default


def test_load_pool_reads_only_the_same_side() -> None:
    db = FakeDb()
    for n, vertical in enumerate(["ma", "ma", "lenders", "other"]):
        insert_example(
            db,
            NewExample(
                source=ExampleSource.HISTORY,
                source_thread_id=str(n),
                label="info_request",
                prospect_reply="q",
                gamic_reply="a",
                vertical=vertical,
            ),
        )
    assert {e.source_thread_id for e in load_pool(db, "info_request", "ma")} == {"0", "1"}
    assert {e.source_thread_id for e in load_pool(db, "info_request", "lenders")} == {"2", "3"}
    assert {e.source_thread_id for e in load_pool(db, "info_request", "")} == {"2", "3"}
    assert len(fetch_examples_for_label(db, "info_request")) == 4  # no side given: all
    assert len(fetch_recent_examples(db, ma=False)) == 2
