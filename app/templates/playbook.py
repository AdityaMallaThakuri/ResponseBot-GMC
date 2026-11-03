"""The reply templates (app/config/playbook.yaml). Empty until Aryan or Ray approve wording.

A template fixes the structure and the wording. Only its declared slots vary, and a slot is
filled by code (first name, company), by a placeholder a human completes, or, for a few short
phrases, by the model copying words from the lead's own message. See playbook.yaml.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.templates.facts import Facts, load_facts

PLAYBOOK_YAML = Path(__file__).resolve().parent.parent / "config" / "playbook.yaml"
SLOT_RE = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
CLAIM_RE = re.compile(r"\{claim\.([a-z][a-z0-9_]*)\}")
SOURCES = frozenset({"first_name", "company", "model", "placeholder"})


class PlaybookError(ValueError):
    """playbook.yaml is malformed or inconsistent with labels.yaml or facts.yaml."""


@dataclass(frozen=True)
class Slot:
    name: str
    source: str
    max_words: int | None = None
    hint: str | None = None


@dataclass(frozen=True)
class Template:
    id: str
    labels: tuple[str, ...]
    when: str
    text: str
    slots: dict[str, Slot]
    claims: tuple[str, ...]
    max_chars: int

    def fixed_text(self) -> str:
        """The wording that is always the same: the text without its slots and claims."""
        return CLAIM_RE.sub("", SLOT_RE.sub("", self.text))


@dataclass(frozen=True)
class Playbook:
    version: str
    templates: dict[str, Template]

    def for_label(self, label: str) -> list[Template]:
        return [t for t in self.templates.values() if label in t.labels]


def _slot(template_id: str, name: str, raw: Any) -> Slot:
    if not isinstance(raw, dict) or raw.get("source") not in SOURCES:
        raise PlaybookError(f"{template_id}.{name}: source must be one of {sorted(SOURCES)}")
    max_words = raw.get("max_words")
    if raw["source"] == "model":
        if not isinstance(max_words, int) or isinstance(max_words, bool) or max_words < 1:
            raise PlaybookError(f"{template_id}.{name}: a model slot needs max_words (1 or more)")
        if not isinstance(raw.get("hint"), str) or not raw["hint"].strip():
            raise PlaybookError(f"{template_id}.{name}: a model slot needs a hint")
    return Slot(name, raw["source"], max_words, raw.get("hint"))


def _template(template_id: str, raw: Any, labels_config: dict[str, Any], facts: Facts) -> Template:
    if template_id not in labels_config["templates"]:
        raise PlaybookError(f"unknown template id {template_id}")
    if not isinstance(raw, dict):
        raise PlaybookError(f"{template_id}: must be a mapping")
    labels = raw.get("labels")
    if not isinstance(labels, list) or not labels:
        raise PlaybookError(f"{template_id}: labels is required")
    unknown = [label for label in labels if label not in labels_config["labels"]]
    if unknown:
        raise PlaybookError(f"{template_id}: unknown labels {unknown}")
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        raise PlaybookError(f"{template_id}: text is required")
    max_chars = raw.get("max_chars")
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1:
        raise PlaybookError(f"{template_id}: max_chars is required")
    when = raw.get("when")
    if not isinstance(when, str) or not when.strip():
        raise PlaybookError(f"{template_id}: when is required")

    declared = {
        name: _slot(template_id, name, spec) for name, spec in (raw.get("slots") or {}).items()
    }
    used = set(SLOT_RE.findall(text))
    if used - set(declared):
        raise PlaybookError(
            f"{template_id}: slots used but not declared: {sorted(used - set(declared))}"
        )
    if set(declared) - used:
        raise PlaybookError(
            f"{template_id}: slots declared but not used: {sorted(set(declared) - used)}"
        )

    claims = tuple(raw.get("claims") or ())
    in_text = set(CLAIM_RE.findall(text))
    if in_text != set(claims):
        raise PlaybookError(
            f"{template_id}: claims must list exactly the {{claim.id}} used in text"
        )
    missing = [c for c in claims if c not in facts.claims]
    if missing:
        raise PlaybookError(f"{template_id}: claims not in facts.yaml: {missing}")
    return Template(template_id, tuple(labels), when.strip(), text, declared, claims, max_chars)


def parse_playbook(data: Any, labels_config: dict[str, Any], facts: Facts) -> Playbook:
    if not isinstance(data, dict):
        raise PlaybookError("playbook.yaml must be a mapping")
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise PlaybookError("version is required (a string)")
    raw_templates = data.get("templates") or {}
    if not isinstance(raw_templates, dict):
        raise PlaybookError("templates must be a mapping")
    templates = {
        tid: _template(tid, raw, labels_config, facts) for tid, raw in raw_templates.items()
    }
    return Playbook(version, templates)


def load_playbook(
    path: Path = PLAYBOOK_YAML,
    labels_config: dict[str, Any] | None = None,
    facts: Facts | None = None,
) -> Playbook:
    if labels_config is None:
        from app.classify.classifier import load_config

        labels_config = load_config()
    return parse_playbook(
        yaml.safe_load(path.read_text(encoding="utf-8")), labels_config, facts or load_facts()
    )
