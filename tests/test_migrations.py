"""Keep the Python layer, CLAUDE.md and the SQL migrations from drifting apart."""

import importlib.util
import re
from enum import StrEnum
from pathlib import Path

import pytest

from app.db import client, records

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "db" / "migrations"


def load_checker():
    spec = importlib.util.spec_from_file_location("check_schema", ROOT / "scripts/check_schema.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sql() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(MIGRATIONS.glob("*.sql")))


def code() -> str:
    """The migrations with `-- comments` removed."""
    return re.sub(r"--[^\n]*", "", sql())


def check_values(constraint: str) -> set[str]:
    # the last definition wins: a later migration may drop and re-add a constraint
    matches = re.findall(
        rf"constraint {constraint}\s+check \(\s*\w+ in \((.*?)\)\s*\)", sql(), re.DOTALL
    )
    assert matches, f"constraint {constraint} not found"
    return set(re.findall(r"'([^']+)'", matches[-1]))


def values_of(enum: type[StrEnum]) -> set[str]:
    return {member.value for member in enum}


@pytest.mark.parametrize(
    ("constraint", "enum"),
    [
        ("raw_events_source_check", records.RawSource),
        ("threads_status_check", records.ThreadStatus),
        ("messages_direction_check", records.Direction),
        ("qualifications_verdict_check", records.Verdict),
        ("drafts_status_check", records.DraftStatus),
        ("slack_cards_lane_check", records.CardLane),
        ("slack_cards_status_check", records.CardStatus),
        ("actions_type_check", records.ActionType),
        ("retry_queue_kind_check", records.RetryKind),
        ("retry_queue_status_check", records.RetryStatus),
        ("decisions_decision_check", records.Decision),
        ("decisions_skip_reason_check", records.SkipReason),
        ("gold_labels_human_verdict_check", records.Verdict),
    ],
)
def test_python_enums_match_sql_checks(constraint: str, enum: type[StrEnum]) -> None:
    assert check_values(constraint) == values_of(enum)


def test_gold_label_message_types_use_the_same_list_as_classification_labels() -> None:
    assert check_values("gold_labels_human_message_type_check") == check_values(
        "classifications_label_check"
    )


def test_gold_label_templates_are_t1_to_t8() -> None:
    assert check_values("gold_labels_human_template_check") == {f"T{n}" for n in range(1, 9)}


def migration_0008() -> str:
    return (MIGRATIONS / "0008_decisions_gold_labels.sql").read_text(encoding="utf-8")


def test_0008_columns_are_nullable_and_template_version_rule_exists() -> None:
    text = re.sub(r"--[^\n]*", "", migration_0008())
    extracted = re.search(r"add column if not exists extracted_facts jsonb(.*?);", text, re.DOTALL)
    assert extracted and "not null" not in extracted.group(1)
    assert "add column if not exists vertical text;" in text
    version = re.search(r"add column if not exists template_version text(.*?);", text, re.DOTALL)
    assert version and "not null" not in version.group(1).split("check")[0]
    assert "check (template_id is null or template_version is not null)" in version.group(1)


def test_decisions_rules() -> None:
    text = migration_0008()
    assert "draft_id          uuid         not null references drafts (id)" in text
    assert "numeric(5,2)" in text
    assert "check (edit_distance_pct between 0 and 100)" in text
    assert "references messages (id) on delete set null" in text
    assert "decisions_skip_reason_only_when_skipped" in text


def test_0008_locks_down_both_new_tables_and_enables_rls() -> None:
    text = migration_0008()
    for table in ("decisions", "gold_labels"):
        assert f"alter table {table} enable row level security;" in text
    assert "array['decisions', 'gold_labels']" in text
    assert "revoke all on table public.%I from %I" in text
    assert "create policy" not in text.lower()


def test_templates_and_events_tables_were_not_added() -> None:
    errors, count = load_checker().check(ROOT)
    created = set(re.findall(r"create table if not exists (\w+)", sql()))
    assert not created & {"templates", "events"} and len(created) == count == 15


def test_label_check_matches_claude_md_label_set() -> None:
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    line = re.search(r"Initial set: (.+?)\.\n", text).group(1)
    assert check_values("classifications_label_check") == {x.strip() for x in line.split(",")}


@pytest.mark.parametrize(
    ("key", "constraint"),
    [
        (client.RAW_EVENT_KEY, "raw_events_source_hash_key"),
        (client.THREAD_KEY, "threads_lead_campaign_key"),
        (client.MESSAGE_KEY, "messages_thread_direction_sent_at_message_id_key"),
        (client.DECISION_KEY, "decisions_draft_id_key"),
        (client.GOLD_LABEL_KEY, "gold_labels_thread_labeler_key"),
        (client.ANNOUNCED_KEY, "announced_campaigns_campaign_kind_key"),
    ],
)
def test_idempotency_keys_match_sql_unique_constraints(
    key: tuple[str, ...], constraint: str
) -> None:
    match = re.search(rf"constraint {constraint}\s+unique \((.*?)\)", sql(), re.DOTALL)
    assert match, f"{constraint} not found"
    assert tuple(c.strip() for c in match.group(1).split(",")) == key


def test_message_key_columns_are_not_null() -> None:
    # NULLs never collide in a unique constraint, so every key column must be NOT NULL.
    block = re.search(r"create table if not exists messages \((.*?)\n\);", sql(), re.DOTALL)
    for column in client.MESSAGE_KEY:
        assert re.search(rf"\b{column}\b[^,\n]*\bnot null\b", block.group(1)), column


def test_stats_id_is_not_unique() -> None:
    assert not re.search(r"unique[^;]*stats_id|stats_id[^,\n]*unique", code(), re.IGNORECASE)


def test_suppression_scope_rules_survived_the_move() -> None:
    text = sql()
    for needle in (
        "campaign_scope_needs_campaign",
        "client_scope_needs_client",
        "global_scope_needs_human",
        "scope <> 'global' or confirmed_by is not null",
        "'campaign', 'client', 'global'",
    ):
        assert needle in text
    assert not (ROOT / "app" / "db" / "schema").exists()


def test_no_secrets_columns() -> None:
    assert not re.search(r"\b(api_key|secret|token|password)\b", code(), re.IGNORECASE)


def test_repo_migrations_pass_the_checker() -> None:
    errors, count = load_checker().check(ROOT)
    assert errors == [] and count == 15


# -- the checker itself ------------------------------------------------------------


def make_root(tmp_path: Path, files: dict[str, str], tables: str = "t") -> Path:
    (tmp_path / "db" / "migrations").mkdir(parents=True)
    for name, body in files.items():
        (tmp_path / "db" / "migrations" / name).write_text(body)
    (tmp_path / "CLAUDE.md").write_text(f"Tables: {tables}.\n")
    return tmp_path


RLS = "alter table t enable row level security;"
REVOKE = "do $$ begin execute format('revoke all on table public.%I from anon', 't'); end $$;"
GOOD = (
    "create table t (id uuid primary key default gen_random_uuid(),"
    " created_at timestamptz not null default now());"
    f" {RLS} {REVOKE}"
)


def threads_sql(extra: str = "") -> str:
    """A minimal `threads` table, fully locked down, for the required-column checks."""
    return (
        GOOD.replace("create table t", "create table threads")
        .replace("alter table t", "alter table threads")
        .replace("'t')", "'threads')")
        + extra
    )


def test_checker_accepts_a_minimal_good_schema(tmp_path: Path) -> None:
    errors, count = load_checker().check(make_root(tmp_path, {"0001_a.sql": GOOD}))
    assert errors == [] and count == 1


@pytest.mark.parametrize(
    ("files", "tables", "expected"),
    [
        ({"0001_a.sql": "create table t (id uuid"}, "t", "does not parse"),
        ({"0001_a.sql": GOOD.replace(RLS, "")}, "t", "row level"),
        ({"0001_a.sql": GOOD.replace(REVOKE, "")}, "t", "revoke all on table"),
        ({"0001_a.sql": GOOD.replace("'t')", "'other')")}, "t", "revoke all on table"),
        ({"0001_a.sql": threads_sql()}, "threads", "missing column 'vertical'"),
        (
            {"0001_a.sql": threads_sql("alter table threads add column vertical integer;")},
            "threads",
            "expected text",
        ),
        ({"0001_a.sql": GOOD.replace("uuid primary key", "bigint primary key")}, "t", "uuid pri"),
        ({"0001_a.sql": GOOD.replace("created_at", "made_at")}, "t", "created_at"),
        ({"0001_a.sql": GOOD + " create policy p on t for select using (true);"}, "t", "policy"),
        ({"0001_a.sql": GOOD}, "t, ghost", "ghost"),
        ({"0001_a.sql": GOOD}, "other", "does not list"),
        ({"0001_a.sql": GOOD, "0003_c.sql": "select 1;"}, "t", "expected migration number 0002"),
        ({"first.sql": GOOD}, "t", "must look like"),
    ],
)
def test_checker_flags_problems(tmp_path: Path, files: dict, tables: str, expected: str) -> None:
    errors, _ = load_checker().check(make_root(tmp_path, files, tables))
    assert any(expected in e for e in errors), errors


def test_checker_follows_alter_table_add_column(tmp_path: Path) -> None:
    sql_text = threads_sql("alter table threads add column if not exists vertical text;")
    errors, _ = load_checker().check(make_root(tmp_path, {"0001_a.sql": sql_text}, "threads"))
    assert errors == []


def test_checker_requires_a_tables_line(tmp_path: Path) -> None:
    root = make_root(tmp_path, {"0001_a.sql": GOOD})
    (root / "CLAUDE.md").write_text("no tables here\n")
    errors, _ = load_checker().check(root)
    assert any("Tables:" in e for e in errors)
