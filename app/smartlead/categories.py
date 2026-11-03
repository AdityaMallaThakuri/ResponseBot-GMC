from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

import yaml

from app.smartlead.errors import SmartleadError

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "smartlead_categories.yaml"


class CategoryKind(StrEnum):
    REPLY_LABEL = "reply_label"
    WORKFLOW_TRIGGER = "workflow_trigger"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Category:
    id: int
    name: str
    kind: CategoryKind


def parse_categories(data: object) -> dict[int, Category]:
    """Validate the YAML structure; any malformed entry is an error, never a silent skip."""
    entries = data.get("categories") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise SmartleadError("smartlead_categories.yaml needs a non-empty 'categories' list")
    categories: dict[int, Category] = {}
    for entry in entries:
        try:
            category = Category(
                id=int(entry["id"]), name=str(entry["name"]), kind=CategoryKind(entry["kind"])
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SmartleadError(f"bad category entry {entry!r}: {exc!r}") from exc
        if category.id in categories:
            raise SmartleadError(f"duplicate category id {category.id}")
        categories[category.id] = category
    return categories


@lru_cache
def load_categories(path: Path = DEFAULT_PATH) -> dict[int, Category]:
    return parse_categories(yaml.safe_load(path.read_text(encoding="utf-8")))
