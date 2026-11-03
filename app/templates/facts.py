"""The allowed company facts (app/config/facts.yaml): claims a draft reply may contain.

A claim is a number, price range, result or link with its exact wording. A draft may only
contain a claim whose `approved` is true and whose campaign types fit the thread's campaign.
Nothing is approved by default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

FACTS_YAML = Path(__file__).resolve().parent.parent / "config" / "facts.yaml"
KINDS = frozenset({"number", "price", "result", "link"})
ALL = "all"
_ID = re.compile(r"^[a-z][a-z0-9_]*$")


class FactsError(ValueError):
    """facts.yaml is malformed."""


@dataclass(frozen=True)
class Fact:
    id: str
    kind: str
    wording: str
    campaign_types: tuple[str, ...]
    approved: bool


@dataclass(frozen=True)
class Facts:
    claims: dict[str, Fact]
    campaign_types: tuple[tuple[str, str], ...] = ()  # (type id, text the campaign name contains)

    def types_for(self, campaign_name: str) -> frozenset[str]:
        name = (campaign_name or "").lower()
        found = {type_id for type_id, part in self.campaign_types if part.lower() in name}
        return frozenset({ALL, *found})

    def usable(self, claim_id: str, campaign_name: str) -> bool:
        fact = self.claims.get(claim_id)
        if fact is None or not fact.approved:
            return False
        return bool(set(fact.campaign_types) & self.types_for(campaign_name))

    def usable_facts(self, campaign_name: str) -> list[Fact]:
        return [f for f in self.claims.values() if self.usable(f.id, campaign_name)]


def parse_facts(data: Any) -> Facts:
    if not isinstance(data, dict):
        raise FactsError("facts.yaml must be a mapping")
    types: list[tuple[str, str]] = []
    for entry in data.get("campaign_types") or []:
        if not (isinstance(entry, dict) and entry.get("id") and entry.get("name_contains")):
            raise FactsError("each campaign type needs an id and name_contains")
        types.append((str(entry["id"]), str(entry["name_contains"])))
    known_types = {t for t, _ in types} | {ALL}

    claims: dict[str, Fact] = {}
    for raw in data.get("claims") or []:
        if not isinstance(raw, dict):
            raise FactsError("each claim must be a mapping")
        claim_id = raw.get("id")
        if not isinstance(claim_id, str) or not _ID.match(claim_id):
            raise FactsError(f"bad claim id: {claim_id!r}")
        if claim_id in claims:
            raise FactsError(f"duplicate claim id: {claim_id}")
        if raw.get("kind") not in KINDS:
            raise FactsError(f"{claim_id}: kind must be one of {sorted(KINDS)}")
        wording = raw.get("wording")
        if not isinstance(wording, str) or not wording.strip():
            raise FactsError(f"{claim_id}: wording is required")
        if raw["kind"] == "link" and not wording.startswith("http"):
            raise FactsError(f"{claim_id}: a link must start with http")
        applies = raw.get("campaign_types")
        if (
            not isinstance(applies, list)
            or not applies
            or not all(isinstance(a, str) for a in applies)
        ):
            raise FactsError(f"{claim_id}: campaign_types must be a list (use [all] for any)")
        unknown = set(applies) - known_types
        if unknown:
            raise FactsError(f"{claim_id}: unknown campaign types {sorted(unknown)}")
        if not isinstance(raw.get("approved"), bool):
            raise FactsError(f"{claim_id}: approved must be true or false")
        claims[claim_id] = Fact(claim_id, raw["kind"], wording, tuple(applies), raw["approved"])
    return Facts(claims, tuple(types))


def load_facts(path: Path = FACTS_YAML) -> Facts:
    return parse_facts(yaml.safe_load(path.read_text(encoding="utf-8")))
