"""Small typed functions to write and read each record type.

`Database` is a structural type that matches supabase-py's query builder, so the real client
(created with the service key) can be passed in and tests can pass an in-memory fake.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from app.db.records import (
    DraftStatus,
    NewAction,
    NewClassification,
    NewDecision,
    NewDraft,
    NewEvalResult,
    NewGoldLabel,
    NewMessage,
    NewQualification,
    NewRawEvent,
    NewRetry,
    NewSlackCard,
    NewThread,
    RetryStatus,
    Row,
    ThreadStatus,
    to_row,
)
from app.db.suppressions import Suppression

# Must equal the unique constraints in db/migrations (a test checks this).
RAW_EVENT_KEY = ("source", "payload_hash")
THREAD_KEY = ("email_lead_id", "email_campaign_id")
MESSAGE_KEY = ("thread_id", "direction", "sent_at", "message_id")
DECISION_KEY = ("draft_id",)
GOLD_LABEL_KEY = ("thread_id", "labeler")


class DatabaseError(RuntimeError):
    """A database call returned nothing where a row was expected."""


class Response(Protocol):
    data: list[Row]


class Query(Protocol):
    def insert(self, json: Row) -> "Query": ...
    def upsert(
        self, json: Row, *, on_conflict: str = "", ignore_duplicates: bool = False
    ) -> "Query": ...
    def update(self, json: Row) -> "Query": ...
    def select(self, *columns: str) -> "Query": ...
    def eq(self, column: str, value: Any) -> "Query": ...
    def lte(self, column: str, value: Any) -> "Query": ...
    def order(self, column: str, *, desc: bool = False) -> "Query": ...
    def limit(self, size: int) -> "Query": ...
    def execute(self) -> Response: ...


class Database(Protocol):
    def table(self, name: str) -> Query: ...


@dataclass(frozen=True)
class InsertResult:
    row: Row
    is_new: bool

    @property
    def id(self) -> str:
        return self.row["id"]


# -- generic helpers ---------------------------------------------------------------


def _insert(db: Database, table: str, row: Row) -> Row:
    data = db.table(table).insert(row).execute().data
    if not data:
        raise DatabaseError(f"insert into {table} returned no row")
    return data[0]


def _select(
    db: Database,
    table: str,
    filters: dict[str, Any] | None = None,
    *,
    order: str | None = None,
    desc: bool = False,
    limit: int | None = None,
) -> list[Row]:
    query = db.table(table).select("*")
    for column, value in (filters or {}).items():
        query = query.eq(column, value)
    if order:
        query = query.order(order, desc=desc)
    if limit is not None:
        query = query.limit(limit)
    return query.execute().data


def _first(db: Database, table: str, filters: dict[str, Any]) -> Row | None:
    rows = _select(db, table, filters, limit=1)
    return rows[0] if rows else None


def _update(db: Database, table: str, row_id: str, values: Row) -> Row:
    data = db.table(table).update(values).eq("id", row_id).execute().data
    if not data:
        raise DatabaseError(f"update of {table} {row_id} matched no row")
    return data[0]


def _insert_once(db: Database, table: str, row: Row, key: tuple[str, ...]) -> InsertResult:
    """Insert unless a row with the same key exists. Reports whether the row was new."""
    data = (
        db.table(table)
        .upsert(row, on_conflict=",".join(key), ignore_duplicates=True)
        .execute()
        .data
    )
    if data:  # with ignore-duplicates the response holds only rows actually inserted
        return InsertResult(data[0], True)
    existing = _first(db, table, {column: row[column] for column in key})
    if existing is None:
        raise DatabaseError(f"{table} reported a conflict but no row matches {key}")
    return InsertResult(existing, False)


# -- raw_events --------------------------------------------------------------------


def insert_raw_event(db: Database, event: NewRawEvent) -> InsertResult:
    return _insert_once(db, "raw_events", to_row(event), RAW_EVENT_KEY)


def fetch_unprocessed_events(db: Database, limit: int = 100) -> list[Row]:
    return _select(db, "raw_events", {"processed": False}, order="received_at", limit=limit)


def mark_event_processed(
    db: Database, event_id: str, now: datetime, error: str | None = None
) -> Row:
    values: Row = {"processed": True, "processed_at": now.isoformat()}
    if error is not None:
        values["process_error"] = error
    return _update(db, "raw_events", event_id, values)


def record_event_error(db: Database, event_id: str, error: str) -> Row:
    """Note why processing failed but leave the event unprocessed so it can be retried."""
    return _update(db, "raw_events", event_id, {"process_error": error})


# -- threads -----------------------------------------------------------------------


def upsert_thread(db: Database, thread: NewThread) -> Row:
    """Create the thread or refresh its Smartlead fields. Never touches pipeline status."""
    data = (
        db.table("threads").upsert(to_row(thread), on_conflict=",".join(THREAD_KEY)).execute().data
    )
    if not data:
        raise DatabaseError("upsert into threads returned no row")
    return data[0]


def get_thread(db: Database, thread_id: str) -> Row | None:
    return _first(db, "threads", {"id": thread_id})


def find_thread(db: Database, email_lead_id: int, email_campaign_id: int) -> Row | None:
    return _first(
        db, "threads", {"email_lead_id": email_lead_id, "email_campaign_id": email_campaign_id}
    )


def set_thread_status(db: Database, thread_id: str, status: ThreadStatus) -> Row:
    return _update(db, "threads", thread_id, {"status": status.value})


# -- messages ----------------------------------------------------------------------


def insert_message(db: Database, message: NewMessage) -> InsertResult:
    return _insert_once(db, "messages", to_row(message), MESSAGE_KEY)


def fetch_messages(db: Database, thread_id: str) -> list[Row]:
    return _select(db, "messages", {"thread_id": thread_id}, order="sent_at")


# -- classifications and qualifications --------------------------------------------


def insert_classification(db: Database, classification: NewClassification) -> Row:
    return _insert(db, "classifications", to_row(classification))


def fetch_classifications(db: Database, thread_id: str) -> list[Row]:
    return _select(db, "classifications", {"thread_id": thread_id}, order="created_at")


def insert_qualification(db: Database, qualification: NewQualification) -> Row:
    return _insert(db, "qualifications", to_row(qualification))


def fetch_qualifications(db: Database, thread_id: str) -> list[Row]:
    return _select(db, "qualifications", {"thread_id": thread_id}, order="created_at")


# -- drafts, cards, actions --------------------------------------------------------


def insert_draft(db: Database, draft: NewDraft) -> Row:
    return _insert(db, "drafts", to_row(draft))


def fetch_drafts(db: Database, thread_id: str) -> list[Row]:
    return _select(db, "drafts", {"thread_id": thread_id}, order="created_at")


def set_draft_status(
    db: Database,
    draft_id: str,
    status: DraftStatus,
    *,
    final_text: str | None = None,
    human_edited: bool | None = None,
) -> Row:
    values: Row = {"status": status.value}
    if final_text is not None:
        values["final_text"] = final_text
    if human_edited is not None:
        values["human_edited"] = human_edited
    return _update(db, "drafts", draft_id, values)


def insert_decision(db: Database, decision: NewDecision) -> InsertResult:
    """Record the human decision on a draft. The first one wins: a repeated click is not new."""
    return _insert_once(db, "decisions", to_row(decision), DECISION_KEY)


def fetch_decision(db: Database, draft_id: str) -> Row | None:
    return _first(db, "decisions", {"draft_id": draft_id})


def insert_slack_card(db: Database, card: NewSlackCard) -> Row:
    return _insert(db, "slack_cards", to_row(card))


def fetch_slack_cards(db: Database, thread_id: str) -> list[Row]:
    return _select(db, "slack_cards", {"thread_id": thread_id}, order="created_at")


def update_slack_card(db: Database, card_id: str, values: Row) -> Row:
    return _update(db, "slack_cards", card_id, values)


def insert_action(db: Database, action: NewAction) -> Row:
    return _insert(db, "actions", to_row(action))


def fetch_actions(db: Database, thread_id: str) -> list[Row]:
    return _select(db, "actions", {"thread_id": thread_id}, order="created_at")


def mark_action_reversed(db: Database, action_id: str, when: datetime) -> Row:
    return _update(db, "actions", action_id, {"reversed_at": when.isoformat()})


# -- suppressions ------------------------------------------------------------------


def insert_suppression(db: Database, suppression: Suppression) -> Row:
    return _insert(db, "suppressions", to_row(suppression))


def fetch_suppressions(db: Database, email: str) -> list[Row]:
    return _select(db, "suppressions", {"email": email.strip().lower()}, order="created_at")


# -- retry_queue and eval_results --------------------------------------------------


def enqueue_retry(db: Database, retry: NewRetry) -> Row:
    return _insert(db, "retry_queue", to_row(retry))


def fetch_due_retries(db: Database, now: datetime, limit: int = 50) -> list[Row]:
    return (
        db.table("retry_queue")
        .select("*")
        .eq("status", RetryStatus.PENDING.value)
        .lte("next_attempt_at", now.isoformat())
        .order("next_attempt_at")
        .limit(limit)
        .execute()
        .data
    )


def finish_retry(db: Database, retry_id: str) -> Row:
    return _update(db, "retry_queue", retry_id, {"status": RetryStatus.DONE.value})


def record_retry_failure(
    db: Database,
    retry_id: str,
    *,
    attempts: int,
    error: str,
    next_attempt_at: datetime,
    dead: bool = False,
) -> Row:
    return _update(
        db,
        "retry_queue",
        retry_id,
        {
            "attempts": attempts,
            "last_error": error,
            "next_attempt_at": next_attempt_at.isoformat(),
            "status": (RetryStatus.DEAD if dead else RetryStatus.PENDING).value,
        },
    )


def upsert_gold_label(db: Database, label: NewGoldLabel) -> Row:
    """Create or correct a labeler's label for a thread (one per thread and labeler)."""
    data = (
        db.table("gold_labels")
        .upsert(to_row(label), on_conflict=",".join(GOLD_LABEL_KEY))
        .execute()
        .data
    )
    if not data:
        raise DatabaseError("upsert into gold_labels returned no row")
    return data[0]


def fetch_gold_labels(db: Database, thread_id: str | None = None) -> list[Row]:
    filters = {"thread_id": thread_id} if thread_id else None
    return _select(db, "gold_labels", filters, order="created_at")


def insert_eval_result(db: Database, result: NewEvalResult) -> Row:
    return _insert(db, "eval_results", to_row(result))


def fetch_eval_results(db: Database, limit: int = 20) -> list[Row]:
    return _select(db, "eval_results", order="created_at", desc=True, limit=limit)


def find_slack_card(db: Database, channel: str, slack_ts: str) -> Row | None:
    """The card a Slack message belongs to: a message is identified by channel id and ts."""
    return _first(db, "slack_cards", {"channel": channel, "slack_ts": slack_ts})
