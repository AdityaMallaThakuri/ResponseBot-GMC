"""The two fixed links (app/config/links.yaml): who gets them, and nothing else gets through."""

from typing import Any

import pytest

from app.examples.drafter import ExampleDrafter, clean_draft, flag_figures
from app.examples.library import Example
from app.examples.links import Links, fill_links, load_links
from tests.c3_support import FakeModel
from tests.test_sop_drafting import NOW

CALENDAR = "https://calendly.com/gamicmedia/one-one"
MAGNET = "https://gamma.app/docs/61-Leads-In-20-Days-e6ddtp51jograal"
LINKS = load_links()

MAGNET_SOP = (
    "Hey [NAME],\n\nSure, you can find that resource here. [LINK]\n\n"
    "I'd love to walk you through how we'd do something similar for your firm over a call.\n\n"
    "Would [TIME OPTIONS] CST work?\n\nIf those don't work, just grab a time here. [LINK]"
)


def test_the_config_holds_exactly_the_two_links() -> None:
    assert LINKS.calendar == CALENDAR
    assert LINKS.lead_magnet == MAGNET
    assert LINKS.lead_magnet_labels == {"lead_magnet_request"}
    assert LINKS.lead_magnet_verticals == {"ma"}


def test_allowed_urls_depend_on_label_and_vertical() -> None:
    both = LINKS.allowed_urls("lead_magnet_request", "ma")
    assert both == {CALENDAR.lower(), MAGNET.lower()}
    for label, vertical in (
        ("lead_magnet_request", "marketing"),
        ("lead_magnet_request", ""),
        ("info_request", "ma"),
    ):
        assert LINKS.allowed_urls(label, vertical) == {CALENDAR.lower()}


def test_the_link_filter_lets_through_only_the_allowed_urls() -> None:
    allowed = LINKS.allowed_urls("lead_magnet_request", "ma")
    text = (
        f"Calendar {CALENDAR}. Magnet {MAGNET}/ and a doc https://gamma.app/docs/other "
        "plus www.evil.example and https://calendly.com/someone-else/30min"
    )
    out = clean_draft(text, allowed)
    assert CALENDAR in out and MAGNET in out
    assert out.count("[LINK]") == 3
    assert "other" not in out and "evil" not in out and "someone-else" not in out
    assert MAGNET not in clean_draft(text, LINKS.allowed_urls("info_request", "ma"))
    assert clean_draft(f"see {CALENDAR}") == "see [LINK]"  # nothing allowed: all replaced


@pytest.mark.parametrize("vertical", ["ma", "lenders", "marketing", "other", "unknown", ""])
@pytest.mark.parametrize(
    "line",
    [
        "If not, just grab a time here. [LINK]",
        "Or pick a slot on my calendar here: [LINK]",
        "Feel free to book a call below [LINK]",
    ],
)
def test_the_calendar_link_fills_booking_lines_for_every_vertical(vertical: str, line: str) -> None:
    out = fill_links(f"Hey Pat,\n\n{line}", "info_request", vertical, LINKS)
    assert CALENDAR in out and "[LINK]" not in out


def test_the_lead_magnet_link_only_on_ma_lead_magnet_requests() -> None:
    body = "Sure, you can find that resource here. [LINK]\n\nIf not, just grab a time here. [LINK]"
    ma = fill_links(body, "lead_magnet_request", "ma", LINKS)
    assert ma.splitlines()[0].endswith(MAGNET)
    assert ma.splitlines()[-1].endswith(CALENDAR)
    for label, vertical in (
        ("lead_magnet_request", "marketing"),
        ("lead_magnet_request", "unknown"),
        ("info_request", "ma"),
        ("price_question", "ma"),
    ):
        other = fill_links(body, label, vertical, LINKS)
        assert MAGNET not in other
        assert other.splitlines()[0].endswith("[LINK]")  # a per-lead doc stays [LINK]
        assert other.splitlines()[-1].endswith(CALENDAR)


def test_with_no_links_configured_everything_stays_a_placeholder() -> None:
    assert fill_links("grab a time here. [LINK]", "info_request", "ma", Links()) == (
        "grab a time here. [LINK]"
    )


def test_a_url_does_not_count_as_a_figure() -> None:
    assert flag_figures(f"See {MAGNET} for 15 minutes") == ("15",)


def pool(label: str, body: str, vertical: str) -> list[Example]:
    return [Example("sop", f"{label}:x", "", label, "situation", body, NOW, NOW, 100, vertical)]


def draft(label: str, vertical: str, body: str, model_reply: str) -> Any:
    model = FakeModel(answer={"reply": model_reply})
    return ExampleDrafter(
        lambda lab, vertical="": pool(label, body, vertical_of_pool),
        model,
        "m-x",
        vertical_of=lambda thread: vertical,
    ).draft(
        thread={"campaign_name": "GMC - X", "lead_first_name": "Pat"},
        label=label,
        confidence=0.95,
        reply="can you send the resource?",
        outreach="",
        min_confidence=0.75,
    )


vertical_of_pool = "ma"


def test_an_ma_lead_magnet_draft_gets_both_links_and_the_url_digits_survive() -> None:
    out = draft("lead_magnet_request", "ma", MAGNET_SOP, MAGNET_SOP.replace("[NAME]", "Pat"))
    assert out.kind == "drafted"
    lines = [line for line in out.text.splitlines() if line.strip()]
    assert lines[1].endswith(MAGNET) and lines[-1].endswith(CALENDAR)
    assert "[LINK]" not in out.text
    assert out.removed == 0 and out.flagged == ()  # 61, 20 and 51 in the URL are not figures


def test_a_marketing_lead_magnet_draft_keeps_link_for_the_strategy_doc() -> None:
    global vertical_of_pool
    vertical_of_pool = "marketing"
    try:
        out = draft(
            "lead_magnet_request", "marketing", MAGNET_SOP, MAGNET_SOP.replace("[NAME]", "Pat")
        )
    finally:
        vertical_of_pool = "ma"
    assert out.kind == "drafted"
    assert MAGNET not in out.text
    assert out.text.count("[LINK]") == 1 and out.text.rstrip().endswith(CALENDAR)


def test_a_link_the_model_invents_never_survives_and_the_calendar_is_not_doubled() -> None:
    reply = (
        f"Hey Pat,\n\nHere it is: https://gamma.app/docs/made-up\n\nGrab a time here. {CALENDAR}"
    )
    out = draft("lead_magnet_request", "ma", MAGNET_SOP, reply)
    assert "made-up" not in out.text
    assert out.text.count(CALENDAR) == 1 and out.text.count(MAGNET) == 1
    assert "[LINK]" not in out.text
    other = draft("lead_magnet_request", "ma", MAGNET_SOP, reply.replace("gamma", "evil"))
    assert "evil" not in other.text
