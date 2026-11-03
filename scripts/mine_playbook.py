# ruff: noqa: E501  (long lines are report text)
"""Mine Gamic's real replies for the most common wording per template (C3 proposal).

Usage: python scripts/mine_playbook.py

Reads eval/data/gmc_all_replies_dataset.csv, gold_set.csv and draft_labels.csv (never modified,
never committed) and writes docs/playbook_proposal.md: for each draft-lane label, the 2 or 3
most common real versions of what Gamic replied, verbatim, with lead names, emails and phones
redacted and the parts that vary between replies marked as slots. It copies wording; it does not
rewrite or improve it. NOTHING in the proposal is approved: it is not app/config/playbook.yaml.

Method: each reply is cleaned of lead names, then grouped with other replies to the same label
whose word 3-grams overlap (Jaccard 0.5 or more). The biggest groups are the "versions". In a
group, the medoid (the member closest to the others) is shown; words that differ in at least 30%
of the other members are replaced by a slot marker.
"""

from __future__ import annotations

import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATASET = ROOT / "eval" / "data" / "gmc_all_replies_dataset.csv"
GOLD = ROOT / "eval" / "data" / "gold_set.csv"
DRAFTS = ROOT / "eval" / "data" / "draft_labels.csv"
OUT = ROOT / "docs" / "playbook_proposal.md"

LABELS = [
    "meeting_request",
    "info_request",
    "free_list_request",
    "price_question",
    "price_and_info",
    "lead_magnet_request",
    "trial_request",
    "booked_confirmation",
    "objection_info",
    "objection_price",
]
SIMILARITY = 0.5
VARIABLE_SHARE = 0.3
VERSIONS = 3
LONG_REPLY = 1200

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{8,}\d")
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
TOKEN_RE = re.compile(r"\S+|\n")
GREETING_RE = re.compile(r"\b(hey|hi|hello|dear)\s+(?!\{)[A-Z][\w'’-]+", re.IGNORECASE)
TIME_WORDS = re.compile(
    r"^(?:\d{1,2}(?::\d{2})?(?:am|pm)?|am|pm|cst|cdt|est|edt|pst|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday|mon|tue|tues|wed|thu|thur|thurs|fri|jan\w*|feb\w*|mar\w*|apr\w*|may|"
    r"jun\w*|jul\w*|aug\w*|sep\w*|oct\w*|nov\w*|dec\w*|at|or|and|the|\d+(?:st|nd|rd|th))[,.;:]*$",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- data


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def labelled_rows() -> dict[str, list[dict[str, Any]]]:
    """Rows with a Gamic reply, by label. A human (gold) label wins over the model's draft label."""
    gold = {r["thread_id"]: r["human_message_type"] for r in read_csv(GOLD)}
    drafts = {r["thread_id"]: r for r in read_csv(DRAFTS)}
    by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in read_csv(DATASET):
        if not row["gamic_response_to_first_reply"].strip():
            continue
        thread = row["thread_id"]
        if thread in gold:
            label, source = gold[thread], "gold"
        elif drafts.get(thread, {}).get("status") == "ok":
            label, source = drafts[thread]["message_type"], "draft"
        else:
            continue
        if label in LABELS:
            by_label[label].append({**row, "label_source": source})
    return by_label


# --------------------------------------------------------------------------- redaction and tokens


def name_parts(lead_name: str) -> list[str]:
    return [p for p in re.split(r"[\s,]+", lead_name.strip()) if len(p) > 1]


def redact(text: str, lead_name: str) -> str:
    """Lead names become {NAME} (first name) or [LAST NAME]; emails and phones are masked."""
    text = EMAIL_RE.sub("[EMAIL]", text)  # before names, so a name inside an address cannot leak
    parts = name_parts(lead_name)
    for i, part in enumerate(parts):
        marker = "{NAME}" if i == 0 else "[LAST NAME]"
        text = re.sub(r"\b" + re.escape(part) + r"\b", marker, text, flags=re.IGNORECASE)
    # A greeting names the lead even when the dataset spells the name differently.
    text = GREETING_RE.sub(lambda m: f"{m.group(1)} {{NAME}}", text)
    text = EMAIL_RE.sub("[EMAIL]", text)
    # Phone numbers are masked outside links only: a link can contain digits that are not a phone.
    pieces = URL_RE.split(text)
    links = URL_RE.findall(text)
    masked = [PHONE_RE.sub("[PHONE]", piece) for piece in pieces]
    merged = "".join(m + (links[i] if i < len(links) else "") for i, m in enumerate(masked))
    return merged.replace("\r\n", "\n").strip()


def tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text)


PUNCT = ".,;:!?…\"'“”‘’()"


def norm_token(token: str) -> str:
    """Lower case, digits and links generalised, edge punctuation dropped (for aligning replies)."""
    token = URL_RE.sub("<u>", token.lower()).replace("’", "'").replace("‘", "'")
    return re.sub(r"\d+", "<d>", token).strip(PUNCT)


def norm_tokens(text: str) -> list[str]:
    return [norm_token(t) for t in tokens(text) if t != "\n"]


def exact_token(token: str) -> str:
    """Like norm_token but digits are kept: a number that differs between replies is a variable."""
    token = URL_RE.sub("<u>", token.lower()).replace("’", "'").replace("‘", "'")
    return token.strip(PUNCT)


def exact_tokens(text: str) -> list[str]:
    return [exact_token(t) for t in tokens(text) if t != "\n"]


def grams(words: list[str]) -> set[str]:
    return {" ".join(words[i : i + 3]) for i in range(max(1, len(words) - 2))}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / max(1, len(a | b))


# --------------------------------------------------------------------------- clustering


def cluster(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Greedy grouping: an item joins the first group whose first member it resembles."""
    groups: list[list[dict[str, Any]]] = []
    for item in sorted(items, key=lambda i: (len(i["text"]), i["row"]["thread_id"])):
        for group in groups:
            if jaccard(group[0]["grams"], item["grams"]) >= SIMILARITY:
                group.append(item)
                break
        else:
            groups.append([item])
    return sorted(groups, key=lambda g: (-len(g), g[0]["row"]["thread_id"]))


def medoid(group: list[dict[str, Any]]) -> dict[str, Any]:
    if len(group) == 1:
        return group[0]
    return max(
        group,
        key=lambda a: (
            sum(jaccard(a["grams"], b["grams"]) for b in group if b is not a),
            a["row"]["thread_id"],
        ),
    )


def variable_indexes(center: dict[str, Any], others: list[dict[str, Any]]) -> set[int]:
    """Positions of the medoid's words that are missing from at least VARIABLE_SHARE of the others.

    A word counts as fixed if it also appears (anywhere) in most of the other members. Punctuation
    on its own is always fixed.
    """
    if not others:
        return set()
    other_words = [set(exact_tokens(o["text"])) for o in others]
    variable = set()
    for index, word in enumerate(exact_tokens(center["text"])):
        if not word:
            continue
        missing = sum(1 for words in other_words if word not in words)
        if missing / len(others) >= VARIABLE_SHARE:
            variable.add(index)
    return variable


def slot_kind(words: list[str]) -> str:
    clean = [w for w in words if w != "{NAME}"]
    if not clean:
        return "NAME"
    if all(TIME_WORDS.match(w) for w in clean):
        return "TIME?"
    # a price, percentage or count that differs between replies is a risk worth naming
    return "NUMBER?" if any(re.search(r"[0-9$%]", w) for w in clean) else "VARIES?"


def slotted_text(center: dict[str, Any], variable: set[int]) -> tuple[str, list[str]]:
    """The medoid's wording with variable spans replaced by {SLOT?} markers, and the markers used."""
    out: list[str] = []
    markers: list[str] = []
    word_index = 0
    span: list[str] = []

    def flush() -> None:
        if span:
            kind = slot_kind(span)
            out.append("{NAME}" if kind == "NAME" else "{" + kind.replace("?", "") + "?}")
            markers.append("NAME" if kind == "NAME" else kind)
            span.clear()

    for token in tokens(center["text"]):
        if token == "\n":
            flush()
            out.append("\n")
            continue
        if word_index in variable and not token.startswith("{NAME}"):
            span.append(token)
        else:
            flush()
            out.append(token)
        word_index += 1
    flush()
    text = " ".join(out).replace(" \n ", "\n").replace(" \n", "\n").replace("\n ", "\n")
    return re.sub(r"(\{\w+\??\})(?:\s+\1)+", r"\1", text), markers


def claims_in(text: str) -> list[str]:
    """Numbers, prices, percentages and links in the fixed wording, with a little context."""
    found = []
    for pattern in (
        URL_RE,
        re.compile(r"[$£€]\s?\d[\d,.]*"),
        re.compile(r"\d+(?:[.,]\d+)*\s?%"),
        re.compile(r"\b\d+(?:[-–]\d+)?\b"),
    ):
        for match in pattern.finditer(text):
            start, end = max(0, match.start() - 30), min(len(text), match.end() + 30)
            context = " ".join(text[start:end].split())
            if context not in found and match.group(0) not in "".join(found[:0]):
                found.append(context)
    return found[:8]


# --------------------------------------------------------------------------- the report


def prepare(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for row in rows:
        text = redact(row["gamic_response_to_first_reply"], row["lead_name"])
        items.append({"row": row, "text": text, "grams": grams(norm_tokens(text))})
    return items


COMMON_CAPS: set[str] = set()  # capitalised words that appear in many replies: our own vocabulary


def set_common_capitals(replies: list[str]) -> None:
    """Words capitalised in at least 8 different replies are Gamic's own terms, not lead names."""
    seen: Counter[str] = Counter()
    for reply in replies:
        seen.update({w.strip(PUNCT) for w in reply.split() if w.strip(PUNCT)[:1].isupper()})
    COMMON_CAPS.clear()
    COMMON_CAPS.update(word for word, n in seen.items() if n >= 8)


def suspect_names(text: str) -> list[str]:
    """Capitalised words in the middle of a sentence that are rare in our replies: maybe a name."""
    found: list[str] = []
    previous = "."
    for token in text.replace("\n", " \n ").split(" "):
        word = token.strip(PUNCT)
        if token and token != "\n" and word[:1].isupper() and word not in COMMON_CAPS:
            starts_sentence = previous[-1:] in ".!?:\n" or previous == "\n"
            if not starts_sentence and not word.startswith("{") and word not in found:
                found.append(word)
        if token:
            previous = token
    return found[:8]


def version_section(
    letter: str, group: list[dict[str, Any]], total: int, gold_share: float, also_seen: list[str]
) -> list[str]:
    center = medoid(group)
    others = [g for g in group if g is not center]
    text, markers = slotted_text(center, variable_indexes(center, others))
    campaigns = Counter(g["row"]["campaign_name"] for g in group).most_common(4)
    fixed = re.sub(r"\{\w+\??\}", " ", text)
    claims = claims_in(fixed)
    unsure = []
    if len(group) < 3:
        unsure.append(f"only {len(group)} replies share this wording")
    other_links = sorted({u.rstrip(".,)") for u in URL_RE.findall(fixed) if CALENDAR_HINT not in u})
    if other_links:
        unsure.append(
            f"contains {len(other_links)} link(s) other than the calendar link, which the facts "
            "file would have to approve: " + ", ".join(link[:48] for link in other_links[:3])
        )
    suspects = suspect_names(text)
    if suspects:
        unsure.append(
            "capitalised words to check for a person or company name: " + ", ".join(suspects)
        )
    if gold_share < 0.3:
        unsure.append("most rows carry the model's draft label, not a human label")
    if len(campaigns) == 1:
        unsure.append("one campaign only")
    if len(text) > LONG_REPLY:
        unsure.append(f"long (over {LONG_REPLY} characters): may be a hand-written reply")
    lines = [
        f"#### Version {letter}: {len(group)} of {total} replies ({len(group) / total:.0%})",
        "",
        "Campaigns: " + "; ".join(f"{name} ({n})" for name, n in campaigns),
        *(["Also seen under: " + "; ".join(also_seen)] if also_seen else []),
        "",
        "```text",
        text,
        "```",
        "",
        "Slots: "
        + (", ".join(f"`{m}`" for m in dict.fromkeys(markers)) if markers else "none found"),
        "",
        "Claims in the fixed wording: "
        + ("; ".join(f"`…{c}…`" for c in claims) if claims else "none found"),
    ]
    if unsure:
        lines += ["", "UNSURE: " + "; ".join(unsure) + "."]
    return [*lines, ""]


CALENDAR_HINT = "calendly.com/gamicmedia/one-one"


def shared_elsewhere(
    label: str, group: list[dict[str, Any]], all_groups: dict[str, list[list[dict[str, Any]]]]
) -> list[str]:
    """Other labels whose recurring replies use (nearly) the same wording."""
    mine = medoid(group)["grams"]
    found = []
    for other, groups in all_groups.items():
        if other == label:
            continue
        sizes = [
            len(g)
            for g in groups
            if len(g) >= 2 and jaccard(mine, medoid(g)["grams"]) >= SIMILARITY
        ]
        if sizes:
            found.append(f"{other} ({sum(sizes)} replies)")
    return found


def label_section(
    label: str,
    template: str,
    rows: list[dict[str, Any]],
    groups: list[list[dict[str, Any]]],
    all_groups: dict[str, list[list[dict[str, Any]]]],
) -> list[str]:
    gold = sum(1 for r in rows if r["label_source"] == "gold")
    lines = [
        f"### {template}: {label}",
        "",
        f"{len(rows)} real replies ({gold} with a human label, {len(rows) - gold} with the model's draft label).",
        "",
    ]
    if not rows:
        return [*lines, "UNSURE: no real replies found for this label.", ""]
    recurring = [g for g in groups if len(g) >= 2][:VERSIONS]
    if not recurring:
        return [
            *lines,
            "UNSURE: no wording recurs. These replies look individually written, so none is shown: "
            "a one-off reply can carry a lead's personal details. The closest family may be the "
            "wording under another template (see the notes at the top).",
            "",
        ]
    for letter, group in zip("ABC", recurring, strict=False):
        lines += version_section(
            letter, group, len(rows), gold / len(rows), shared_elsewhere(label, group, all_groups)
        )
    return lines


def build_report() -> str:
    from app.classify.classifier import load_config

    config = load_config()
    by_label = labelled_rows()
    set_common_capitals(
        [r["gamic_response_to_first_reply"] for rows in by_label.values() for r in rows]
    )
    all_groups = {
        label: cluster(prepare(by_label.get(label, []))) if by_label.get(label) else []
        for label in LABELS
    }
    summary = [
        "| label | template | real replies | human-labelled | recurring versions found |",
        "|---|---|---|---|---|",
    ]
    body: list[str] = []
    for label in LABELS:
        rows = by_label.get(label, [])
        groups = all_groups[label]
        recurring = sum(1 for g in groups if len(g) >= 2)
        gold = sum(1 for r in rows if r["label_source"] == "gold")
        template = config["labels"][label]["template"]
        summary.append(f"| {label} | {template} | {len(rows)} | {gold} | {recurring} |")
        body += label_section(label, template, rows, groups, all_groups)
    header = [
        "# Playbook proposal (NOT APPROVED)",
        "",
        "Wording mined from Gamic's past replies, for Aryan or Ray to review. Nothing here is in",
        "`app/config/playbook.yaml`, which stays empty until wording is approved. Every claim still has",
        "to be added to `app/config/facts.yaml` and approved there.",
        "",
        "## How to read this",
        "",
        "- Source: Gamic's real reply to each lead's first reply, from the GMC dataset. The label of the",
        "  lead's reply is the human label where one exists (the gold set), otherwise the model's draft",
        "  label. Model labels are noisy, so a version built mostly from them is marked UNSURE.",
        "- A version is a group of replies with at least half of their word triples in common. The biggest",
        "  groups per label are shown. The wording shown is the most typical member, copied exactly.",
        "- Redactions: lead first names are `{NAME}`, last names `[LAST NAME]`, emails `[EMAIL]`, phone",
        "  numbers `[PHONE]`. Words that differ in at least 30% of the group are replaced by a slot",
        "  marker: `{TIME?}` looks like days and times, `{NUMBER?}` is a price, percentage or count",
        "  that differs between replies (so it is NOT a fixed claim), and `{VARIES?}` is anything else",
        "  that varies (often a company, a sector or a personal line). A question mark means a guess.",
        "- A label with no recurring wording shows no version: one-off replies can carry a lead's",
        "  personal details. For price_and_info the closest wording is under price_question (T4).",
        "- `Also seen under` lists other labels whose replies use nearly the same wording. That is",
        "  either label noise (the model's draft labels are imperfect) or one reply used for several",
        "  situations: either way it is a question for Aryan or Ray.",
        "- Company names are not detected automatically. A company or sector may remain inside fixed",
        "  wording: read every version before approving it.",
        "- Sub-templates (T2a, T2b, T2c, T3a, T4a, T4b, T5b) cannot be told apart from the label alone.",
        "  Where a label has several, the versions below are candidates: which is which needs a decision.",
        "- The calendar link and any numbers are listed as claims so they can be approved one by one.",
        "",
        "## Summary",
        "",
        *summary,
        "",
        "## Versions",
        "",
    ]
    return "\n".join([*header, *body]).rstrip() + "\n"


def main() -> int:
    for path in (DATASET, GOLD, DRAFTS):
        if not path.exists():
            print(f"missing {path}")
            return 1
    OUT.write_text(build_report(), encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
