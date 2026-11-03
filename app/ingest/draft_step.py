"""C3: make (or reuse) the draft for a reply in the draft lane.

Called from the classify step. With no approved template for the label (playbook.yaml is empty
until Aryan or Ray approve wording), or when the validator blocks the draft, the reply goes to
human_only and no draft text is shown. A draft that passes is stored in `drafts` with its
template id, slot values, validator result and model, and shown on the card. Nothing is sent.

Reusing the stored draft on a retry keeps the step idempotent: one draft per thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.classify.classifier import Classification
from app.classify.routing import LANE_HUMAN_ONLY, Route
from app.db.client import Row, fetch_drafts, insert_draft
from app.db.records import DraftStatus, NewDraft
from app.ingest.deps import IngestDeps
from app.smartlead.models import Message
from app.validate.validator import explain


@dataclass(frozen=True)
class DraftView:
    """What the card shows about the draft."""

    text: str | None  # None: no draft is shown (blocked, or no template)
    template_id: str | None
    edited: bool
    notes: tuple[str, ...]
    approve_allowed: bool


def notes_for(approve_blockers: list[str], warnings: list[str]) -> tuple[str, ...]:
    return (
        *(explain(code) for code in approve_blockers),
        *(f"Warning: {explain(code)}" for code in warnings),
    )


def view_from_row(row: Row) -> tuple[Route | None, DraftView]:
    """The card view for a stored draft. A failed draft returns a human_only route."""
    result = row.get("validator_result") or {}
    if not row.get("validator_ok", True):
        reasons = list(row.get("validator_reasons") or result.get("blocking") or [])
        notes = tuple(f"Draft blocked: {explain(code)}" for code in reasons)
        return Route(LANE_HUMAN_ONLY, "draft blocked"), DraftView(
            None, row["template_id"], False, notes, False
        )
    blockers = list(result.get("approve_blockers") or [])
    warnings = list(result.get("warnings") or [])
    edited = bool(row.get("human_edited"))
    return None, DraftView(
        row.get("final_text") or row["text"],
        row["template_id"],
        edited,
        notes_for(blockers, warnings),
        approve_allowed=not blockers,
    )


def prepare_draft(
    deps: IngestDeps,
    thread: Row,
    classification_id: str,
    result: Classification,
    reply: Message,
    outreach: str,
    plan: Route,
) -> tuple[Route, DraftView | None, str | None]:
    """(route to use, draft view, draft row id). The route changes to human_only if no draft."""
    existing = fetch_drafts(deps.db, thread["id"])
    if existing:
        row = existing[-1]
        blocked_route, view = view_from_row(row)
        return (blocked_route or plan), view, row["id"]

    outcome = deps.drafter.draft(
        thread=thread,
        label=result.label,
        confidence=result.confidence,
        reply=reply.text,
        outreach=outreach,
        min_confidence=deps.config.draft_min_confidence,
    )
    if outcome.draft is None or outcome.validation is None:
        note = f"No draft: {explain(outcome.reason) if ':' in outcome.reason else outcome.reason}"
        return (
            Route(LANE_HUMAN_ONLY, outcome.reason),
            DraftView(None, None, False, (note,), False),
            None,
        )

    draft, validation = outcome.draft, outcome.validation
    row = insert_draft(
        deps.db,
        NewDraft(
            thread_id=thread["id"],
            classification_id=classification_id,
            template_id=draft.template_id,
            template_version=draft.template_version,
            text=draft.text,
            validator_ok=validation.ok,
            slots=draft.slots,
            validator_reasons=list(validation.blocking),
            status=DraftStatus.PROPOSED if validation.ok else DraftStatus.FAILED,
            model=draft.model,
            validator_result=validation.to_json(),
        ),
    )
    blocked_route, view = view_from_row(row)
    return (blocked_route or plan), view, row["id"]


def draft_enabled(deps: IngestDeps) -> bool:
    drafter: Any = deps.drafter
    return drafter is not None
