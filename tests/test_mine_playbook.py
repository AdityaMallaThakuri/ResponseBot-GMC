"""scripts/mine_playbook.py: redaction, grouping and slot marking, on synthetic text only."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mine_playbook as mp  # noqa: E402


def row(
    thread: str,
    text: str,
    name: str = "Pat Sample",
    campaign: str = "GMC - Synthetic",
    source: str = "gold",
):
    return {
        "thread_id": thread,
        "gamic_response_to_first_reply": text,
        "lead_name": name,
        "campaign_name": campaign,
        "label_source": source,
    }


def items(*texts: str):
    return mp.prepare([row(f"t{i}", text) for i, text in enumerate(texts)])


# ------------------------------------------------------------------ redaction


def test_names_emails_and_phones_are_redacted() -> None:
    text = "Hi pat, I spoke to Sample at pat@example.com or +1 (555) 123-4567."
    out = mp.redact(text, "Pat Sample")
    assert "{NAME}" in out and "[LAST NAME]" in out
    assert "pat@example.com" not in out and "[EMAIL]" in out and "[PHONE]" in out
    assert "Sample" not in out and "123-4567" not in out


def test_a_greeting_name_is_redacted_even_if_the_dataset_spells_it_differently() -> None:
    assert mp.redact("Hey Patrick,\n\nThanks", "Pat Sample").startswith("Hey {NAME},")
    assert mp.redact("Hello Dr", "Pat Sample") == "Hello {NAME}"


def test_digits_inside_a_link_are_not_masked_as_a_phone() -> None:
    link = "https://calendar.example/slot?month=2026-03&date=2026-03-17"
    assert link in mp.redact(f"Book here ({link}).", "Pat Sample")


def test_tokens_normalise_case_digits_links_and_punctuation() -> None:
    assert mp.norm_tokens("Hello, 12 people at https://x.example/a. Don’t!") == [
        "hello",
        "<d>",
        "people",
        "at",
        "<u>",
        "don't",
    ]


# ------------------------------------------------------------------ grouping and slots


TEMPLATE = "We use signals to build the list. Would {} at {} work? If not, grab a time here."


def test_similar_replies_group_and_different_ones_do_not() -> None:
    a = TEMPLATE.format("tuesday", "10am")
    b = TEMPLATE.format("wednesday", "2pm")
    other = "Completely different wording about pricing and a retainer fee for your firm."
    groups = mp.cluster(items(a, b, other))
    assert [len(g) for g in groups] == [2, 1]


def test_the_medoid_is_the_most_typical_member() -> None:
    group = items(
        TEMPLATE.format("tuesday", "10am"),
        TEMPLATE.format("wednesday", "2pm"),
        TEMPLATE.format("tuesday", "2pm"),
    )
    assert mp.medoid(group) in group


def test_words_that_differ_become_slots_and_fixed_words_stay() -> None:
    group = items(
        TEMPLATE.format("tuesday", "10am"),
        TEMPLATE.format("wednesday", "2pm"),
        TEMPLATE.format("thursday", "4pm"),
    )
    center = mp.medoid(group)
    others = [g for g in group if g is not center]
    text, markers = mp.slotted_text(center, mp.variable_indexes(center, others))
    assert "We use signals to build the list." in text and "If not, grab a time here." in text
    assert "{TIME?}" in text and "TIME?" in markers
    assert "tuesday" not in text and "10am" not in text


def test_a_price_that_differs_is_named_as_a_number_slot() -> None:
    price = "Pricing depends on scope. A retainer of {} plus a success fee. Fastest is a call."
    group = items(price.format("$4,000"), price.format("$6,500"), price.format("$5,000"))
    center = mp.medoid(group)
    text, markers = mp.slotted_text(
        center, mp.variable_indexes(center, [g for g in group if g is not center])
    )
    assert "NUMBER?" in markers and "$" not in text


def test_a_single_reply_has_no_slots() -> None:
    (only,) = items("Just one reply with no siblings.")
    assert mp.variable_indexes(only, []) == set()


def test_claims_list_numbers_prices_percentages_and_links_with_context() -> None:
    claims = mp.claims_in(
        "We charge $2-$4k, which is 10-20% of a fee. See https://example.com/x for 15 targets."
    )
    joined = " | ".join(claims)
    assert (
        "$2" in joined
        and "10-20%" in joined
        and "https://example.com/x" in joined
        and "15 targets" in joined
    )


def test_suspect_names_flag_rare_capitalised_words_mid_sentence() -> None:
    mp.set_common_capitals(["Aryan said hi"] * 10)  # Aryan is our own vocabulary
    assert mp.suspect_names("We also helped Acme Widgets book meetings. Aryan agrees.") == [
        "Acme",
        "Widgets",
    ]
    assert mp.suspect_names("Thanks. Pricing depends.") == []
    mp.COMMON_CAPS.clear()


# ------------------------------------------------------------------ the report


def test_a_label_without_recurring_wording_shows_nothing() -> None:
    rows = [
        row("a", "A one-off reply about something personal."),
        row("b", "Another unrelated reply entirely."),
    ]
    groups = mp.cluster(mp.prepare(rows))
    lines = mp.label_section("price_and_info", "T5", rows, groups, {"price_and_info": groups})
    text = "\n".join(lines)
    assert (
        "no wording recurs" in text
        and "```" not in text
        and "personal" not in text.replace("personal details", "")
    )


def test_a_recurring_version_is_shown_with_its_campaigns_and_flags() -> None:
    rows = [
        row("a", TEMPLATE.format("tuesday", "10am"), campaign="GMC - One"),
        row("b", TEMPLATE.format("wednesday", "2pm"), campaign="GMC - Two"),
        row("c", TEMPLATE.format("friday", "4pm"), campaign="GMC - One", source="draft"),
    ]
    groups = mp.cluster(mp.prepare(rows))
    text = "\n".join(
        mp.label_section("meeting_request", "T1", rows, groups, {"meeting_request": groups})
    )
    assert "#### Version A: 3 of 3 replies (100%)" in text and "```text" in text
    assert "GMC - One (2)" in text and "GMC - Two (1)" in text


def test_wording_shared_with_another_label_is_noted() -> None:
    a = [row(f"a{i}", TEMPLATE.format("tuesday", f"{i}pm")) for i in range(2)]
    b = [row(f"b{i}", TEMPLATE.format("friday", f"{i}am")) for i in range(2)]
    all_groups = {
        "meeting_request": mp.cluster(mp.prepare(a)),
        "info_request": mp.cluster(mp.prepare(b)),
    }
    found = mp.shared_elsewhere("meeting_request", all_groups["meeting_request"][0], all_groups)
    assert found == ["info_request (2 replies)"]


def test_a_link_other_than_the_calendar_link_is_flagged() -> None:
    link_text = "Here is the doc https://drive.example/file/abc for {} and a call at https://calendly.com/gamicmedia/one-one"
    rows = [
        row(f"t{i}", link_text.format(name))
        for i, name in enumerate(["you", "your team", "your firm"])
    ]
    groups = mp.cluster(mp.prepare(rows))
    text = "\n".join(
        mp.label_section("lead_magnet_request", "T6", rows, groups, {"lead_magnet_request": groups})
    )
    assert (
        "link(s) other than the calendar link" in text and "https://drive.example/file/abc" in text
    )
