"""Validate db/migrations without touching any database.

Checks that every file parses as Postgres (using Postgres's own parser), that numbering is
sequential, that every table has a uuid `id` primary key, `created_at` and row level security
with no policies, that every table is named in a revoke block (anon and authenticated lose
access), that the columns later migrations rely on exist, and that the tables match the
`Tables:` line in CLAUDE.md.

Usage: python scripts/check_schema.py
"""

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pglast
from pglast import ast, enums

ROOT = Path(__file__).resolve().parent.parent
FILE_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
TABLES_LINE = re.compile(r"^Tables:\s*(.+?)\.?\s*$", re.MULTILINE)
QUOTED = re.compile(r"'([a-z_][a-z0-9_]*)'")

# Columns the application code reads or writes, including ones added by ALTER TABLE.
REQUIRED_COLUMNS: dict[str, dict[str, str]] = {
    "threads": {"vertical": "text"},
    "messages": {"message_id": "text", "from_address": "text", "to_address": "text"},
    "classifications": {"extracted_facts": "jsonb"},
    "drafts": {"template_version": "text", "model": "text", "validator_result": "jsonb"},
    "decisions": {"draft_id": "uuid", "decided_by": "text", "edit_distance_pct": "numeric"},
    "gold_labels": {"human_message_type": "text", "human_verdict": "text", "labeler": "text"},
}


@dataclass
class Table:
    name: str
    file: str
    columns: dict[str, str] = field(default_factory=dict)
    uuid_pk: bool = False
    rls: bool = False


def _column_type(column: ast.ColumnDef) -> str:
    return column.typeName.names[-1].sval


def _is_pk(column: ast.ColumnDef) -> bool:
    return any(c.contype == enums.ConstrType.CONSTR_PRIMARY for c in column.constraints or ())


def _read_create(stmt: ast.CreateStmt, file: str) -> Table:
    table = Table(name=stmt.relation.relname, file=file)
    for element in stmt.tableElts or ():
        if isinstance(element, ast.ColumnDef):
            table.columns[element.colname] = _column_type(element)
            if element.colname == "id" and _column_type(element) == "uuid" and _is_pk(element):
                table.uuid_pk = True
    return table


def _apply_alter(stmt: ast.AlterTableStmt, tables: dict[str, Table]) -> None:
    table = tables.get(stmt.relation.relname)
    if table is None:
        return
    for cmd in stmt.cmds:
        if cmd.subtype == enums.AlterTableType.AT_EnableRowSecurity:
            table.rls = True
        elif cmd.subtype == enums.AlterTableType.AT_AddColumn:
            table.columns[cmd.def_.colname] = _column_type(cmd.def_)


def _revoked_tables(files: list[Path]) -> set[str]:
    """Names quoted in any migration that revokes table privileges (the 0007 / 0008 blocks)."""
    names: set[str] = set()
    for path in files:
        text = path.read_text(encoding="utf-8")
        if "revoke all on table" in text:
            names |= set(QUOTED.findall(text))
    return names


def _check_numbering(files: list[Path]) -> list[str]:
    errors = []
    for index, path in enumerate(files, start=1):
        match = FILE_NAME.match(path.name)
        if not match:
            errors.append(f"{path.name}: name must look like 0001_description.sql")
        elif int(match.group(1)) != index:
            errors.append(f"{path.name}: expected migration number {index:04d}")
    return errors


def _scan(files: list[Path]) -> tuple[dict[str, Table], list[str]]:
    tables: dict[str, Table] = {}
    errors: list[str] = []
    for path in files:
        try:
            statements = pglast.parse_sql(path.read_text(encoding="utf-8"))
        except pglast.parser.ParseError as exc:
            errors.append(f"{path.name}: does not parse: {exc}")
            continue
        for raw in statements:
            stmt = raw.stmt
            if isinstance(stmt, ast.CreateStmt):
                table = _read_create(stmt, path.name)
                tables[table.name] = table
            elif isinstance(stmt, ast.AlterTableStmt):
                _apply_alter(stmt, tables)
            elif isinstance(stmt, ast.CreatePolicyStmt):
                errors.append(f"{path.name}: policy {stmt.policy_name!r} is not allowed")
    return tables, errors


def _check_tables(tables: dict[str, Table], revoked: set[str]) -> list[str]:
    errors = []
    for table in tables.values():
        if table.name not in revoked:
            errors.append(
                f"{table.file}: {table.name} is never named in a `revoke all on table` block"
            )
        if not table.uuid_pk:
            errors.append(f"{table.file}: {table.name} needs `id uuid primary key`")
        if "created_at" not in table.columns:
            errors.append(f"{table.file}: {table.name} needs created_at")
        if not table.rls:
            errors.append(f"{table.file}: {table.name} must enable row level security")
    return errors


def _check_required_columns(tables: dict[str, Table]) -> list[str]:
    errors = []
    for name, wanted in REQUIRED_COLUMNS.items():
        table = tables.get(name)
        if table is None:
            continue  # a missing table is reported by the CLAUDE.md comparison
        for column, column_type in wanted.items():
            actual = table.columns.get(column)
            if actual is None:
                errors.append(f"{name} is missing column {column!r}")
            elif actual != column_type:
                errors.append(f"{name}.{column} is {actual}, expected {column_type}")
    return errors


def _documented_tables(claude_md: Path) -> set[str] | None:
    match = TABLES_LINE.search(claude_md.read_text(encoding="utf-8"))
    if not match:
        return None
    return {name.strip() for name in match.group(1).split(",") if name.strip()}


def check(root: Path = ROOT) -> tuple[list[str], int]:
    """Return (errors, table_count)."""
    files = sorted((root / "db" / "migrations").glob("*.sql"))
    if not files:
        return ["no migration files in db/migrations"], 0
    errors = _check_numbering(files)
    tables, scan_errors = _scan(files)
    errors += scan_errors + _check_tables(tables, _revoked_tables(files))
    errors += _check_required_columns(tables)

    documented = _documented_tables(root / "CLAUDE.md")
    if documented is None:
        errors.append("CLAUDE.md has no 'Tables:' line")
    else:
        for name in sorted(documented - tables.keys()):
            errors.append(f"CLAUDE.md names table {name!r} but no migration creates it")
        for name in sorted(tables.keys() - documented):
            errors.append(f"migrations create table {name!r} that CLAUDE.md does not list")
    return errors, len(tables)


def main() -> int:
    errors, count = check()
    for error in errors:
        print(f"ERROR {error}")
    if errors:
        return 1
    print(f"OK: {count} tables, every migration parses, RLS on, tables match CLAUDE.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
