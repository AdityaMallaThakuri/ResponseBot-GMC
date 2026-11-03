"""Profile the GMC replies dataset (read-only).

Usage: python scripts/profile_dataset.py [csv_path]

Prints and saves counts to eval/reports/profile.md. The CSV is never modified.
Only counts and short masked examples are output, never full bodies or emails.
"""

import csv
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = ROOT / "eval" / "data" / "gmc_all_replies_dataset.csv"
REPORT = ROOT / "eval" / "reports" / "profile.md"

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
LONG_REPLY = 1500
RESIDUE_PATTERNS: dict[str, re.Pattern[str]] = {
    "wrote:": re.compile(r"wrote:", re.I),
    "Original Message": re.compile(r"original message", re.I),
    "Sent from my": re.compile(r"sent from my", re.I),
    "-- signature": re.compile(r"(^|\n)-- ?(\n|$)"),
}


def mask_emails(text: str) -> str:
    """Replace each email with a masked form like a***@d***.com."""

    def _mask(m: re.Match[str]) -> str:
        local, _, domain = m.group(0).partition("@")
        head, _, tld = domain.rpartition(".")
        return f"{local[:1]}***@{head[:1]}***.{tld}"

    return EMAIL_RE.sub(_mask, text)


def example(text: str, n: int = 40) -> str:
    """First n characters, single-lined, emails masked."""
    return mask_emails(" ".join(text.split()))[:n]


def residue_hits(text: str) -> list[str]:
    """Names of residue patterns found in text (long replies counted separately)."""
    return [name for name, pat in RESIDUE_PATTERNS.items() if pat.search(text)]


def first_word(text: str) -> str:
    words = re.findall(r"[a-z0-9']+", text.lower())
    return words[0] if words else ""


def is_yes(value: str) -> bool:
    return value.strip().lower() in {"yes", "true", "1", "y"}


def load(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def pct(n: int, total: int) -> str:
    return f"{n / total * 100:.1f}%" if total else "n/a"


def build_report(rows: list[dict[str, str]]) -> str:
    total = len(rows)
    out: list[str] = ["# Dataset profile", ""]
    leads = {r["lead_email"].strip().lower() for r in rows if r["lead_email"].strip()}
    out += [f"- Rows: {total}", f"- Unique leads (lead_email): {len(leads)}", ""]

    for col in ("category_name", "sentiment"):
        out += [f"## Rows per {col}", "", "| value | rows |", "|---|---|"]
        for value, n in Counter(r[col] for r in rows).most_common():
            out.append(f"| {value or '(empty)'} | {n} |")
        out.append("")

    replies = [(r, r["prospect_first_reply"]) for r in rows]
    empty = [r for r, t in replies if not t.strip()]
    out += ["## Empty prospect_first_reply", "", f"- {len(empty)} rows"]
    for cat, n in Counter(r["category_name"] for r in empty).most_common():
        out.append(f"  - {cat}: {n}")
    out.append("")

    out += ["## Signature or quote residue", ""]
    for name in RESIDUE_PATTERNS:
        hits = [t for _, t in replies if name in residue_hits(t)]
        out.append(f"- `{name}`: {len(hits)} rows")
        for t in hits[:3]:
            out.append(f'  - e.g. "{example(t)}"')
    long_ = [t for _, t in replies if len(t) > LONG_REPLY]
    out.append(f"- over {LONG_REPLY} characters: {len(long_)} rows")
    for t in long_[:3]:
        out.append(f'  - e.g. ({len(t)} chars) "{example(t)}"')
    any_residue = sum(1 for _, t in replies if residue_hits(t) or len(t) > LONG_REPLY)
    out += [f"- rows with any residue or over-length: {any_residue}", ""]

    out += [
        "## Gamic replied after first reply, by category",
        "",
        "| category | rows | replied | share |",
        "|---|---|---|---|",
    ]
    by_cat: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        by_cat.setdefault(r["category_name"], []).append(r)
    for cat, group in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        yes = sum(is_yes(r["gamic_replied_after_first_reply"]) for r in group)
        out.append(f"| {cat} | {len(group)} | {yes} | {pct(yes, len(group))} |")
    out.append("")

    words = Counter(first_word(t) for _, t in replies if t.strip())
    out += ["## 20 most common opening words", "", "| word | rows |", "|---|---|"]
    out += [f"| {w or '(none)'} | {n} |" for w, n in words.most_common(20)]
    out.append("")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_CSV
    if not path.exists():
        print(f"error: dataset not found at {path}", file=sys.stderr)
        return 1
    report = build_report(load(path))
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(report, encoding="utf-8")
    print(report)
    print(f"Saved to {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
