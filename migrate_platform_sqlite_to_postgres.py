"""Import the platform SQLite transition database into an empty PostgreSQL DB.

The importer is intentionally one-way and refuses to write into a target that
already contains platform data.  Run it against a staging database first,
compare row counts and application smoke tests, then repeat with a fresh
production database during the cutover window.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from config import PLATFORM_DATABASE_URL
from platform_database import connect_platform_database, is_postgres_target
from platform_store import PlatformStore

IMPORT_ORDER = (
    "platform_organizations",
    "platform_members",
    "platform_points",
    "platform_point_access",
    "platform_point_rates",
    "platform_member_rates",
    "platform_subscriptions",
    "platform_owner_codes",
    "platform_invites",
    "platform_finance_entries",
    "platform_audit_events",
    "platform_automation_settings",
    "platform_notification_log",
    "platform_subscription_notification_log",
    "platform_rate_limits",
)
IDENTITY_TABLES = {
    "platform_organizations",
    "platform_members",
    "platform_points",
    "platform_subscriptions",
    "platform_owner_codes",
    "platform_invites",
    "platform_finance_entries",
    "platform_audit_events",
    "platform_notification_log",
    "platform_subscription_notification_log",
}
BOOLEAN_COLUMNS = {("platform_organizations", "staff_registration_enabled")}
JSON_COLUMNS = {
    ("platform_audit_events", "metadata"),
    ("platform_automation_settings", "settings_json"),
}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_identifier(identifier: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(identifier):
        raise ValueError(f"Недопустимый идентификатор схемы: {identifier!r}")
    return f'"{identifier}"'


def source_rows(source: sqlite3.Connection, table: str) -> tuple[tuple[str, ...], list[sqlite3.Row]]:
    columns = tuple(row[1] for row in source.execute(f"PRAGMA table_info({table})"))
    if not columns:
        return (), []
    column_sql = ", ".join(quote_identifier(column) for column in columns)
    rows = source.execute(
        "SELECT " + column_sql + " FROM " + quote_identifier(table) + " ORDER BY rowid"  # noqa: S608
    ).fetchall()
    return columns, rows


def normalize_value(table: str, column: str, value: Any) -> Any:
    if (table, column) in BOOLEAN_COLUMNS:
        return bool(value)
    if (table, column) in JSON_COLUMNS:
        if value in (None, ""):
            return "{}"
        json.loads(value)
    return value


def import_database(source_path: str, target: str, dry_run: bool = False) -> dict[str, int]:
    if not dry_run and not is_postgres_target(target):
        raise ValueError("Цель импорта должна быть PostgreSQL DSN")
    source = Path(source_path)
    if not source.is_file():
        raise FileNotFoundError(f"SQLite-файл не найден: {source}")

    # Apply the transition migrations before reading so old platform copies
    # receive the columns that the PostgreSQL contract expects.
    PlatformStore(str(source)).init_schema()
    with sqlite3.connect(source) as source_db:
        source_db.row_factory = sqlite3.Row
        rows_by_table: dict[str, tuple[tuple[str, ...], list[sqlite3.Row]]] = {
            table: source_rows(source_db, table) for table in IMPORT_ORDER
        }

    counts = {table: len(rows) for table, (_columns, rows) in rows_by_table.items()}
    if dry_run:
        return counts

    PlatformStore(target).init_schema()
    with connect_platform_database(target) as destination:
        for table in IMPORT_ORDER:
            existing = destination.execute(
                "SELECT COUNT(*) FROM " + quote_identifier(table)  # noqa: S608
            ).fetchone()[0]
            if existing:
                raise RuntimeError(
                    f"Целевая таблица {table} не пуста ({existing}); "
                    "импорт остановлен без изменений"
                )

        for table in IMPORT_ORDER:
            columns, rows = rows_by_table[table]
            if not rows:
                continue
            placeholders = ", ".join("?" for _ in columns)
            statement = (  # noqa: S608
                "INSERT INTO "  # noqa: S608
                + quote_identifier(table)
                + " ("
                + ", ".join(quote_identifier(column) for column in columns)
                + ") VALUES ("
                + placeholders
                + ")"
            )
            for row in rows:
                values = tuple(
                    normalize_value(table, column, row[column]) for column in columns
                )
                destination.execute(statement, values)

        for table in IDENTITY_TABLES:
            destination.execute(
                "SELECT setval(pg_get_serial_sequence(?, 'id'), "  # noqa: S608
                "COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM "
                + quote_identifier(table),
                (table,),
            )
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Перенос platform SQLite в PostgreSQL")
    parser.add_argument("--source", required=True, help="Путь к platform.db")
    parser.add_argument(
        "--target",
        default=PLATFORM_DATABASE_URL,
        help="PostgreSQL DSN; по умолчанию PLATFORM_DATABASE_URL",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Проверить источник и вывести количество строк без записи в PostgreSQL",
    )
    args = parser.parse_args()
    try:
        counts = import_database(args.source, args.target, dry_run=args.dry_run)
    except (FileNotFoundError, RuntimeError, ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(json.dumps({"dry_run": args.dry_run, "tables": counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()
