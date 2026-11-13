"""clean_quote, ooo_until and referral_name: what the Slack card shows of a prospect's reply."""

from app.slack.quote import QUOTE_LIMIT, clean_quote, ooo_until, referral_name

JON = (
    "No thank you.\n\nJan Sample\n\nManaging Partner\n\nSample Capital\n\n"
    "(555) 010-0101\n\njan@sample.example"
)
JOHN = "I wish you the best of luck.\n\nPlease REMOVE.\n\nJANE SMITH\nMANAGING PRINCIPAL"
PAUL = (
    "I am not sure if this is a robo email.\n\nPlease detail your objective.\n\n"
    "Pat Sample Sr.\n\nManaging Partner\n\nExample Financing Group\n\n"
    "Office: 555.010.0102  |  Direct: 555.010.0103\n\nFax: 555.010.0104\n\n"
    "examplefinancing.group\n\nAPPLY NOW | DOC UPLOAD\n\n| EXAMPLE.AI\n\n"
    "Information transmitted via this email is intended only for the person to whom it is "
    "addressed and may contain confidential or privileged material."
)


def test_signatures_titles_and_contact_lines_are_removed() -> None:
    assert clean_quote(JON) == "No thank you."
    assert clean_quote(JOHN) == "I wish you the best of luck. Please REMOVE."
    assert (
        clean_quote(PAUL) == "I am not sure if this is a robo email. Please detail your objective."
    )


def test_a_sign_off_and_everything_after_it_goes() -> None:
    assert clean_quote("Sounds good, let's talk.\n\nThanks\n\nDana Sample") == (
        "Sounds good, let's talk."
    )
    assert clean_quote("Yes please.\n\nBest regards,\nAnn\nCEO") == "Yes please."
    assert clean_quote("Yes please.\n\nSent from my iPhone") == "Yes please."
    assert clean_quote("Yes please.\n--\nAnn") == "Yes please."


def test_quoted_earlier_emails_are_cut() -> None:
    assert clean_quote(
        "Interested.\n\nOn Tue, Nov 10, 2026 at 3:12 PM Aryan <a@x.com> wrote:\n> hi"
    ) == ("Interested.")
    assert (
        clean_quote("Interested.\n\n-----Original Message-----\nFrom: A\nSent: B") == "Interested."
    )
    assert clean_quote(
        "Interested.\n\nFrom: Aryan\nSent: Tuesday\nTo: me\nSubject: Hi\n\nhello"
    ) == ("Interested.")
    assert clean_quote("Interested.\n> quoted line\n> more") == "Interested."


def test_a_phone_number_in_the_body_is_kept() -> None:
    body = "call me tomorrow at 1 pm CT at 555-010-0105"
    assert clean_quote(body) == body
    stuck = "Give me a call, my number is 555-010-0103. I'm Pat."
    assert clean_quote(stuck) == stuck


def test_a_short_body_is_not_mistaken_for_a_signature() -> None:
    for body in ("Please stop", "how much?", "Unsubscribe", "Not interested\n\nPlease stop"):
        assert clean_quote(body) == " ".join(body.split())
    assert clean_quote("I'm a partner here, tell me more.") == "I'm a partner here, tell me more."


def test_a_reply_that_is_only_signature_falls_back_to_the_start_of_the_text() -> None:
    assert (
        clean_quote("> only a quoted line\n> and another") == "> only a quoted line > and another"
    )
    assert clean_quote("(555) 010-0101\n\njan@sample.example") == "(555) 010-0101"


def test_the_quote_is_at_most_400_characters_on_one_line() -> None:
    assert QUOTE_LIMIT == 400
    out = clean_quote("word " * 300)
    assert len(out) <= 400 and out.endswith("…") and "\n" not in out
    assert clean_quote("x" * 400) == "x" * 400


def test_a_signature_that_arrived_on_one_line_is_cut() -> None:
    one = "Please send details. Pat Sample Managing Partner Office: 555.010.0102 Fax: 555.010.0104"
    assert clean_quote(one) == "Please send details."


def test_the_return_date_of_an_out_of_office() -> None:
    assert ooo_until("I will be on vacation and returning to work Monday, November 30.") == "Nov 30"
    assert ooo_until("I'm out of the office until I return on Dec. 4th.") == "Dec 4"
    assert ooo_until("Back in the office Jan 7") == "Jan 7"
    assert ooo_until("I am away from my desk.") is None


def test_the_person_a_wrong_person_reply_points_to() -> None:
    text = "please email Alice Sample at alice@sample.example or call her at (555) 010-0106"
    assert referral_name(text) == "Alice Sample"
    assert referral_name("Please contact John Q Public for that.") == "John Q Public"
    assert referral_name("I am not the right person.") is None
