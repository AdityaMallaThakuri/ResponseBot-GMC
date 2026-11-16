"""Sign-offs on a draft. Gamic's past replies end with "Best, / Aryan Aryal / CEO | Gamic Media";
a draft never carries that unless the DRAFT_SIGNATURE setting says so."""

from __future__ import annotations

import re

_CLOSING = re.compile(
    r"^(?:best|best regards|kind regards|warm regards|regards|thanks|thank you|thanks again|"
    r"many thanks|cheers|sincerely|talk soon|speak soon|all the best)\s*[,.!]?\s*$",
    re.IGNORECASE,
)
SIGNATURE_LINES = 4  # a name, a title and a company at most, after the closing word
SIGNATURE_LINE_CHARS = 60


def strip_signature(text: str) -> str:
    """The draft without its closing word ("Best,") and the name and title lines after it.

    Only a closing line near the end counts, and only when what follows is short name or title
    lines (no sentence, no question), so a real sentence is never cut."""
    lines = text.rstrip().split("\n")
    filled = [i for i, line in enumerate(lines) if line.strip()]
    for i in reversed(filled[-(SIGNATURE_LINES + 1) :]):
        if not _CLOSING.match(lines[i].strip()):
            continue
        tail = [line.strip() for line in lines[i + 1 :] if line.strip()]
        if all(len(t) <= SIGNATURE_LINE_CHARS and not t.endswith((".", "?", "!")) for t in tail):
            return "\n".join(lines[:i]).rstrip()
    return text.rstrip()


def with_signature(text: str, signature: str) -> str:
    """The draft ending after its last sentence, plus the configured signature if there is one."""
    body = strip_signature(text)
    sign = signature.strip().replace("\\n", "\n")  # .env holds one line: a backslash and n
    return f"{body}\n\n{sign}" if sign and body else body
