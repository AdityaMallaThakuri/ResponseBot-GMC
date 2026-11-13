"""Readable text for a Slack card: the reply without its signature, and small facts from it.

`clean_quote` cuts a prospect's reply at the first sign of a quoted earlier email, a sign-off, or a
signature block (name and title lines, phone, fax, website, "APPLY NOW", legal footers), keeps at
most 400 characters, and returns plain text on one line. If cutting would leave nothing, the start
of the original is shown instead. `ooo_until` and `referral_name` pull one fact for the one-line
cards. Nothing here is sent anywhere.
"""

from __future__ import annotations

import re

QUOTE_LIMIT = 400

_SIGNOFF = re.compile(
    r"^\s*(?:best(?: regards)?|kind regards|warm regards|regards|many thanks|thanks(?: again)?|"
    r"thank you|sincerely|cheers|talk soon|yours truly|respectfully|sent from my .+|"
    r"get outlook for .+|-{2,}|—+)\s*[,!.]?\s*$",
    re.IGNORECASE,
)
_WROTE = re.compile(r"^\s*on\s.{0,200}wrote:?\s*$", re.IGNORECASE)
_ORIGINAL = re.compile(
    r"^\s*(?:-{2,}\s*(?:original|forwarded) message\s*-{2,}|_{5,}|-{5,})\s*$", re.IGNORECASE
)
_HEADER = re.compile(r"^\s*(?:from|sent|to|date|subject|cc):\s", re.IGNORECASE)
_FOOTER = re.compile(
    r"confidential|intended only|privileged|disclaimer|unsubscribe|registered in|"
    r"all rights reserved|apply now|doc upload|this email and any|this message (?:is|and)|"
    r"please consider the environment|©",
    re.IGNORECASE,
)
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URLISH = re.compile(
    r"(?:https?://|www\.)\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|io|co|ai|group|capital|us|uk|ca)\b\S*",
    re.IGNORECASE,
)
_LABEL = re.compile(
    r"\b(?:office|direct|cell|mobile|fax|tel|telephone|phone|ph|mob|m|t|o|f|w|e|email|web|website)"
    r"\s*[:.]",
    re.IGNORECASE,
)
_TITLE = re.compile(
    r"\b(?:ceo|coo|cfo|cto|cmo|president|vice president|vp|principal|partner|director|founder|"
    r"co-founder|owner|manager|managing|chairman|associate|analyst|advisor|adviser|consultant|"
    r"executive|head of)\b",
    re.IGNORECASE,
)
_SMALL_WORDS = frozenset({"of", "and", "at", "the", "for", "&", "to", "in"})
_INLINE_CONTACT = re.compile(
    r"\b(?:office|direct|cell|mobile|fax|tel|phone)\s*:\s*[+(]?\d", re.IGNORECASE
)


def _words(line: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z'’.-]*", line)


def _caplike(line: str) -> bool:
    """A short name, title or company line: every word starts with a capital, no sentence end."""
    stripped = line.strip()
    words = _words(stripped)
    if not stripped or len(stripped) > 60 or len(words) > 6 or stripped[-1] in "?!":
        return False
    if stripped[-1] == "." and len(words[-1].rstrip(".")) > 3:  # "Sr." is fine, "Thanks." is not
        return False
    return all(w[0].isupper() or w.lower() in _SMALL_WORDS for w in words)


def _is_title(line: str) -> bool:
    return _caplike(line) and bool(_TITLE.search(line))


def _is_contact(line: str) -> bool:
    if _FOOTER.search(line):
        return True
    found = [p.search(line) for p in (_PHONE, _EMAIL, _URLISH)]
    if not any(found) or len(line) > 160:
        return False
    rest = _EMAIL.sub(" ", _URLISH.sub(" ", _PHONE.sub(" ", line)))
    return len(re.findall(r"[A-Za-z]{2,}", _LABEL.sub(" ", rest))) <= 2


def _quoted_start(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        pair = f"{line} {lines[i + 1]}" if i + 1 < len(lines) else line
        if line.lstrip().startswith(">") or _ORIGINAL.match(line):
            return i
        if _WROTE.match(line) or _WROTE.match(pair):
            return i
        if re.match(r"^\s*from:\s", line, re.IGNORECASE) and any(
            _HEADER.match(x) for x in lines[i + 1 : i + 5]
        ):
            return i
    return None


def _signature_start(lines: list[str]) -> int | None:
    for k, line in enumerate(lines):
        if k == 0 or not line.strip():
            continue
        if _SIGNOFF.match(line):
            return k
        if _is_contact(line) or _is_title(line):
            start = k
            while start - 1 >= 1 and k - start < 6 and _caplike(lines[start - 1]):
                start -= 1
            return start
    return None


def _flatten(lines: list[str]) -> str:
    return " ".join(" ".join(x.split()) for x in lines if x.strip()).strip()


def _inline_cut(text: str) -> str:
    """A signature that arrived on one line: cut at "Office: 555..." and drop the name before it."""
    match = _INLINE_CONTACT.search(text)
    if not match:
        return text
    head = text[: match.start()].rstrip()
    tokens = head.split(" ")
    popped = 0
    while tokens and popped < 10 and tokens[-1][:1].isupper():
        tokens.pop()
        popped += 1
    return " ".join(tokens).strip() or head


def clean_quote(text: str, limit: int = QUOTE_LIMIT) -> str:
    """The reply without quoted emails, sign-off and signature; one line, at most `limit` chars."""
    raw_lines = [x for x in (text or "").replace("\r", "").split("\n") if x.strip()]
    cut = _quoted_start(raw_lines)
    lines = raw_lines[:cut] if cut is not None else raw_lines
    sig = _signature_start(lines)
    body = _flatten(lines[:sig] if sig is not None else lines)
    body = _inline_cut(body)
    if not body:
        body = _flatten(raw_lines)
    return body if len(body) <= limit else body[: limit - 1].rstrip() + "…"


_MONTH = (
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_BACK = re.compile(
    rf"(?:until|return(?:ing|s)?|back)\b[^.\n]{{0,60}}?\b{_MONTH}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b",
    re.IGNORECASE,
)


def ooo_until(text: str) -> str | None:
    """ "Nov 30" from an out-of-office reply ("returning to work Monday, November 30"), or None."""
    match = _BACK.search(text or "")
    return f"{match.group(1)[:3].title()} {match.group(2)}" if match else None


_REFERRAL = re.compile(
    r"(?:email|contact|reach out to|speak (?:to|with)|talk to|forward (?:this )?to|ask for|"
    r"call)\s+([A-Z][a-z'’-]*(?:\s+[A-Z][a-z'’-]*){1,2})\b"
)


def referral_name(text: str) -> str | None:
    """The person a "wrong person" reply points to ("please email Alice Sample"), or None."""
    match = _REFERRAL.search(text or "")
    return match.group(1) if match else None
