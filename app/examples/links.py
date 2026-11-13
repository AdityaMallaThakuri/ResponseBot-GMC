"""The two fixed links a draft may carry (app/config/links.yaml), and where they go.

The model never writes a link: whatever it writes becomes [LINK], except exactly these URLs when
they are allowed for the reply. After the draft is final, each [LINK] line is filled by its
wording: a "grab a time" / "my calendar" line gets the calendar link (all verticals); on a
lead_magnet_request draft for an M&A campaign the other [LINK] line gets the lead magnet link. Every
other [LINK] stays [LINK] for a person to fill in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

LINKS_PATH = Path(__file__).resolve().parent.parent / "config" / "links.yaml"
PLACEHOLDER = "[LINK]"
_CALENDAR_CUE = re.compile(
    r"\b(grab|pick|book|choose|schedule)\b[^.\n?!]*\b(time|slot|call)\b"
    r"[^.\n?!]*\b(here|below|link|calendar)\b|\bmy calendar\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Links:
    calendar: str = ""
    lead_magnet: str = ""
    lead_magnet_labels: frozenset[str] = frozenset()
    lead_magnet_verticals: frozenset[str] = frozenset()

    def lead_magnet_allowed(self, label: str, vertical: str) -> bool:
        return bool(
            self.lead_magnet
            and label in self.lead_magnet_labels
            and vertical in self.lead_magnet_verticals
        )

    def allowed_urls(self, label: str, vertical: str) -> frozenset[str]:
        """The URLs that may stay in a draft for this label and vertical."""
        urls = {self.calendar} if self.calendar else set()
        if self.lead_magnet_allowed(label, vertical):
            urls.add(self.lead_magnet)
        return frozenset(u.rstrip("/").lower() for u in urls)


def load_links(path: Path = LINKS_PATH) -> Links:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    magnet = data.get("lead_magnet") or {}
    return Links(
        calendar=str(data.get("calendar") or "").strip(),
        lead_magnet=str(magnet.get("url") or "").strip(),
        lead_magnet_labels=frozenset(magnet.get("labels") or ()),
        lead_magnet_verticals=frozenset(magnet.get("verticals") or ()),
    )


def fill_links(text: str, label: str, vertical: str, links: Links) -> str:
    """Replace [LINK] on calendar lines, and on the resource line of an M&A lead magnet reply."""
    out: list[str] = []
    for line in text.split("\n"):
        if PLACEHOLDER in line:
            if links.calendar and _CALENDAR_CUE.search(line):
                line = line.replace(PLACEHOLDER, links.calendar)
            elif links.lead_magnet_allowed(label, vertical):
                line = line.replace(PLACEHOLDER, links.lead_magnet)
        out.append(line)
    return "\n".join(out)
