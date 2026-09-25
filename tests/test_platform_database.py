import sys
from types import SimpleNamespace

import pytest

from config import validate_platform_database_config
from migrate_platform_sqlite_to_postgres import import_database
from platform_database import PostgresConnection, is_postgres_target
from platform_store import PlatformStore


def test_postgres_target_is_explicit_and_does_not_create_a_path():
    assert is_postgres_target("postgresql://user:secret@db/tochka") is True
    assert is_postgres_target("platform.sqlite3") is False

    store = PlatformStore("postgresql://user:secret@db/tochka")
    assert store.is_postgres is True
    assert store.path is None


def test_production_rejects_silent_sqlite_fallback():
    with pytest.raises(RuntimeError, match="PLATFORM_DATABASE_URL"):
        validate_platform_database_config("production", "")
    validate_platform_database_config("development", "")


def test_transition_sql_is_translated_to_postgres_conflict_syntax():
    sql, params = PostgresConnection._translate(
        "INSERT OR IGNORE INTO platform_notification_log (id) VALUES (?)",
        (7,),
    )
    assert "INSERT INTO" in sql
    assert "ON CONFLICT DO NOTHING" in sql
    assert "%s" in sql
    assert params == (7,)

    sql, _ = PostgresConnection._translate(
        "INSERT OR REPLACE INTO platform_rate_limits (bucket_key) VALUES (?)",
        ("digest",),
    )
    assert "ON CONFLICT (bucket_key) DO UPDATE SET" in sql


def test_postgres_configuration_fails_closed_without_driver(monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(RuntimeError, match="psycopg is not installed"):
        PlatformStore("postgresql://user:secret@db/tochka").init_schema()


def test_postgres_connection_preserves_named_and_indexed_rows(monkeypatch):
    class FakeCursor:
        description = (SimpleNamespace(name="id"), SimpleNamespace(name="name"))
        rowcount = 1

        def __init__(self):
            self.sql = None
            self.params = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params):
            self.sql = sql
            self.params = params

        def fetchall(self):
            return [(7, "Точка")]

    class FakeConnection:
        def __init__(self):
            self.cursor_instance = FakeCursor()

        def cursor(self):
            return self.cursor_instance

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    connection = FakeConnection()
    fake_psycopg = SimpleNamespace(connect=lambda _url: connection)
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    with PostgresConnection("postgresql://user:secret@db/tochka") as db:
        result = db.execute("SELECT id, name FROM platform_points WHERE id = ?", (7,))

    row = result.fetchone()
    assert row[0] == 7
    assert row["name"] == "Точка"
    assert connection.cursor_instance.sql.endswith("id = %s")
    assert connection.cursor_instance.params == (7,)


def test_sqlite_importer_dry_run_reports_all_platform_rows(tmp_path):
    source = tmp_path / "platform.sqlite3"
    store = PlatformStore(str(source))
    store.issue_owner_code()

    counts = import_database(
        str(source),
        "postgresql://user:secret@db/tochka",
        dry_run=True,
    )

    assert counts["platform_owner_codes"] == 1
    assert set(counts) == {
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
    }
