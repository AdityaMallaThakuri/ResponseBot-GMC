"""In-memory stand-in for the supabase-py query builder, enough to exercise app.db.client."""

import copy
import uuid
from dataclasses import dataclass
from typing import Any

Row = dict[str, Any]


@dataclass
class FakeResponse:
    data: list[Row]


class FakeQuery:
    def __init__(self, db: "FakeDb", table: str) -> None:
        self._db = db
        self._table = table
        self._op = "select"
        self._row: Row = {}
        self._on_conflict: list[str] = []
        self._ignore = False
        self._eq: list[tuple[str, Any]] = []
        self._lte: list[tuple[str, Any]] = []
        self._order: tuple[str, bool] | None = None
        self._limit: int | None = None

    def insert(self, json: Row) -> "FakeQuery":
        self._op, self._row = "insert", json
        return self

    def upsert(
        self, json: Row, *, on_conflict: str = "", ignore_duplicates: bool = False
    ) -> "FakeQuery":
        self._op, self._row = "upsert", json
        self._on_conflict = [c for c in on_conflict.split(",") if c]
        self._ignore = ignore_duplicates
        return self

    def update(self, json: Row) -> "FakeQuery":
        self._op, self._row = "update", json
        return self

    def select(self, *columns: str) -> "FakeQuery":
        self._op = "select"
        return self

    def eq(self, column: str, value: Any) -> "FakeQuery":
        self._eq.append((column, value))
        return self

    def lte(self, column: str, value: Any) -> "FakeQuery":
        self._lte.append((column, value))
        return self

    def order(self, column: str, *, desc: bool = False) -> "FakeQuery":
        self._order = (column, desc)
        return self

    def limit(self, size: int) -> "FakeQuery":
        self._limit = size
        return self

    def _new_row(self) -> Row:
        row = {"id": str(uuid.uuid4()), "created_at": "now", **copy.deepcopy(self._row)}
        self._db.tables.setdefault(self._table, []).append(row)
        return row

    def _matches(self, row: Row) -> bool:
        return all(row.get(c) == v for c, v in self._eq) and all(
            row.get(c) is not None and row[c] <= v for c, v in self._lte
        )

    def execute(self) -> FakeResponse:
        rows = self._db.tables.setdefault(self._table, [])
        self._db.calls.append((self._table, self._op))
        if self._op == "insert":
            return FakeResponse([copy.deepcopy(self._new_row())])
        if self._op == "upsert":
            existing = next(
                (r for r in rows if all(r.get(c) == self._row.get(c) for c in self._on_conflict)),
                None,
            )
            if existing is None:
                return FakeResponse([copy.deepcopy(self._new_row())])
            if self._ignore:
                return FakeResponse([])
            existing.update(copy.deepcopy(self._row))
            return FakeResponse([copy.deepcopy(existing)])
        matched = [r for r in rows if self._matches(r)]
        if self._op == "update":
            for row in matched:
                row.update(copy.deepcopy(self._row))
            return FakeResponse(copy.deepcopy(matched))
        if self._order:
            column, desc = self._order
            matched.sort(key=lambda r: str(r.get(column)), reverse=desc)
        if self._limit is not None:
            matched = matched[: self._limit]
        return FakeResponse(copy.deepcopy(matched))


class FakeDb:
    def __init__(self) -> None:
        self.tables: dict[str, list[Row]] = {}
        self.calls: list[tuple[str, str]] = []

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, name)
