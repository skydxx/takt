"""Database connection compatibility layer for the platform store.

The domain store is intentionally written against a very small DB-API-like
surface.  SQLite remains the default for local development, while production
can use PostgreSQL by setting ``PLATFORM_DATABASE_URL``.  Keeping the adapter
here makes the backend choice explicit without duplicating authorization and
billing logic in a second repository.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

POSTGRES_PREFIXES = ("postgresql://", "postgres://")
_INSERT_ID_TABLES = {"platform_organizations", "platform_points"}


def is_postgres_target(target: str) -> bool:
    return target.strip().lower().startswith(POSTGRES_PREFIXES)


class _PostgresRow:
    """A row that supports both sqlite-style indexes and named fields."""

    def __init__(self, values: tuple[Any, ...], columns: tuple[str, ...]):
        self._values = values
        self._mapping = dict(zip(columns, values, strict=False))

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._mapping[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


class _PostgresResult:
    def __init__(self, cursor: Any):
        description = cursor.description or ()
        columns = tuple(column.name for column in description)
        raw_rows = cursor.fetchall() if description else []
        self._rows = [_PostgresRow(tuple(row), columns) for row in raw_rows]
        self._index = 0
        self.rowcount = cursor.rowcount
        self.lastrowid = self._rows[0][0] if self._rows else None

    def fetchone(self) -> _PostgresRow | None:
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchall(self) -> list[_PostgresRow]:
        rows = self._rows[self._index :]
        self._index = len(self._rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class PostgresConnection:
    """Small wrapper translating the transition store's SQLite SQL surface."""

    def __init__(self, url: str):
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - exercised in deployment
            raise RuntimeError(
                "PostgreSQL configured but psycopg is not installed; "
                "install requirements.txt"
            ) from exc
        self._connection = psycopg.connect(url)

    def __enter__(self) -> "PostgresConnection":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        try:
            if exc_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        finally:
            self._connection.close()

    @staticmethod
    def _translate(sql: str, params: Iterable[Any]) -> tuple[str, tuple[Any, ...]]:
        values = tuple(params)
        translated = sql.replace("BEGIN IMMEDIATE", "BEGIN")
        translated = re.sub(
            r"\bINSERT\s+OR\s+IGNORE\s+INTO\b",
            "INSERT INTO",
            translated,
            flags=re.IGNORECASE,
        )
        if "INSERT OR REPLACE INTO PLATFORM_RATE_LIMITS" in translated.upper():
            translated = re.sub(
                r"\bINSERT\s+OR\s+REPLACE\s+INTO\b",
                "INSERT INTO",
                translated,
                count=1,
                flags=re.IGNORECASE,
            )
            translated = (
                translated.rstrip()
                + " ON CONFLICT (bucket_key) DO UPDATE SET "
                "window_started = EXCLUDED.window_started, "
                "attempts = EXCLUDED.attempts"
            )
        elif "INSERT OR IGNORE INTO" in sql.upper():
            translated = translated.rstrip() + " ON CONFLICT DO NOTHING"
        translated = translated.replace("?", "%s")
        return translated, values

    @staticmethod
    def _needs_id_returning(sql: str) -> bool:
        normalized = re.sub(r"\s+", " ", sql.strip()).lower()
        if " returning " in f" {normalized} ":
            return False
        return normalized.startswith("insert into ") and any(
            normalized.startswith(f"insert into {table} ") for table in _INSERT_ID_TABLES
        )

    def execute(self, sql: str, params: Iterable[Any] = ()) -> _PostgresResult:
        values = tuple(params)
        translated, values = self._translate(sql, values)
        if self._needs_id_returning(translated):
            translated = translated.rstrip() + " RETURNING id"
        if translated.strip().upper().startswith("PRAGMA"):
            return _PostgresResult(_EmptyCursor())
        with self._connection.cursor() as cursor:
            cursor.execute(translated, values)
            return _PostgresResult(cursor)

    def executescript(self, script: str) -> None:
        for statement in script.split(";"):
            clean = statement.strip()
            if clean:
                self.execute(clean)


class _EmptyCursor:
    description = ()
    rowcount = -1

    def fetchall(self) -> list[Any]:
        return []


def connect_platform_database(target: str) -> sqlite3.Connection | PostgresConnection:
    """Open the configured platform database without exposing driver details."""
    if is_postgres_target(target):
        return PostgresConnection(target)
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
