"""A 0 to 100 score for how close two reply texts are (our draft against what Ray sent).

Both texts are reduced to lower-case words first. Links, placeholders such as [TIME OPTIONS] and
clock times are blanked so that a placeholder is not punished for not being a real time, then
the word sequences are compared with difflib (100 = identical).
"""

from __future__ import annotations

import difflib
import re

_LINK = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"\[[A-Z][A-Z0-9 ]*\]")
_CLOCK = re.compile(r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9']+")


def words(text: str) -> list[str]:
    text = _PLACEHOLDER.sub(" ", _LINK.sub(" ", text or ""))
    return _WORD.findall(_CLOCK.sub(" ", text).lower().replace("’", "'"))


def similarity(draft: str, real: str) -> float:
    a, b = words(draft), words(real)
    if not a and not b:
        return 100.0
    return round(100 * difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(), 1)
