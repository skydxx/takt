"""Small, isolated persistence layer for the new owner workspace.

The legacy bot/database remains available while the product is migrated.  The
workspace schema deliberately has its own tables and invariants so that a
future PostgreSQL repository can replace this SQLite adapter without changing
the Flask routes or domain vocabulary.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from platform_database import connect_platform_database, is_postgres_target

UTC = timezone.utc
SUBSCRIPTION_GRACE_DAYS = 3
PLATFORM_SCHEMA_VERSION = 2

AUTOMATION_PRESETS = {
    "standard": {
        "label": "Стандартный",
        "commands": {"start_shift": "смена", "end_shift": "закрыть", "handover": "передача"},
        "notifications": {
            "employee_payroll_days_before": 2,
            "manager_payroll_days_before": 2,
            "owner_payroll_days_before": 5,
            "employee_payroll_time": "10:00",
            "manager_payroll_time": "10:00",
            "owner_payroll_time": "10:00",
            "shift_alert_minutes": 10,
        },
    },
    "compact": {
        "label": "Короткие команды",
        "commands": {"start_shift": "старт", "end_shift": "стоп", "handover": "смена"},
        "notifications": {
            "employee_payroll_days_before": 1,
            "manager_payroll_days_before": 2,
            "owner_payroll_days_before": 3,
            "employee_payroll_time": "10:00",
            "manager_payroll_time": "10:00",
            "owner_payroll_time": "10:00",
            "shift_alert_minutes": 7,
        },
    },
    "formal": {
        "label": "Формальный",
        "commands": {
            "start_shift": "открыть смену",
            "end_shift": "закрыть смену",
            "handover": "передать смену",
        },
        "notifications": {
            "employee_payroll_days_before": 3,
            "manager_payroll_days_before": 3,
            "owner_payroll_days_before": 7,
            "employee_payroll_time": "09:00",
            "manager_payroll_time": "09:00",
            "owner_payroll_time": "09:00",
            "shift_alert_minutes": 15,
        },
    },
}


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_utc(value: str) -> datetime:
    """Parse old naive SQLite timestamps as UTC for safe comparisons."""
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def money_to_minor(value: str) -> int:
    """Parse a non-negative amount in rubles into kopecks."""
    if not isinstance(value, str):
        raise ValueError("amount must be numeric")
    normalized = value.strip().replace(" ", "").replace(",", ".")
    if not normalized:
        raise ValueError("amount is required")
    try:
        amount = (Decimal(normalized) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("amount must be numeric") from exc
    if not amount.is_finite() or amount < 0 or amount > 10_000_000_00:
        raise ValueError("amount is outside the allowed range")
    return int(amount)


class PlatformStore:
    """Repository for organizations, billing, invites and finance.

    SQLite is the local fallback; PostgreSQL is selected by a DSN and uses the
    same domain-facing methods through :mod:`platform_database`.
    """

    def __init__(self, path: str):
        self.database_target = path.strip()
        self.is_postgres = is_postgres_target(self.database_target)
        self.path = None if self.is_postgres else Path(self.database_target)

    def _connect(self) -> Any:
        return connect_platform_database(self.database_target)

    def init_schema(self) -> None:
        if self.is_postgres:
            schema_path = Path(__file__).resolve().parent / "migrations" / "postgres" / "001_platform_schema.sql"
            with self._connect() as db:
                db.executescript(schema_path.read_text(encoding="utf-8"))
            return
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS platform_organizations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_web_user_id INTEGER NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    staff_registration_enabled INTEGER NOT NULL DEFAULT 1 CHECK (staff_registration_enabled IN (0, 1)),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS platform_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    organization_id INTEGER NOT NULL,
                    web_user_id INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('owner', 'manager', 'employee')),
                    created_at TEXT NOT NULL,
                    UNIQUE (organization_id, web_user_id),
                    FOREIGN KEY (organization_id) REFERENCES platform_organizations(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS platform_points (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    organization_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    address TEXT NOT NULL DEFAULT '',
                    telegram_chat_id INTEGER UNIQUE,
                    telegram_chat_title TEXT NOT NULL DEFAULT '',
                    work_time_start TEXT NOT NULL DEFAULT '09:00',
                    work_time_end TEXT NOT NULL DEFAULT '18:00',
                    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused')),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (organization_id) REFERENCES platform_organizations(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS platform_point_access (
                    organization_id INTEGER NOT NULL,
                    web_user_id INTEGER NOT NULL,
                    point_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (web_user_id, point_id),
                    FOREIGN KEY (organization_id) REFERENCES platform_organizations(id) ON DELETE CASCADE,
                    FOREIGN KEY (point_id) REFERENCES platform_points(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS platform_point_rates (
                    organization_id INTEGER NOT NULL,
                    point_id INTEGER PRIMARY KEY,
                    amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0),
                    updated_by_user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (organization_id) REFERENCES platform_organizations(id) ON DELETE CASCADE,
                    FOREIGN KEY (point_id) REFERENCES platform_points(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS platform_member_rates (
                    organization_id INTEGER NOT NULL,
                    point_id INTEGER NOT NULL,
                    member_web_user_id INTEGER NOT NULL,
                    amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0),
                    visibility TEXT NOT NULL CHECK (visibility IN ('personal', 'hidden')),
                    updated_by_user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (point_id, member_web_user_id),
                    FOREIGN KEY (organization_id) REFERENCES platform_organizations(id) ON DELETE CASCADE,
                    FOREIGN KEY (point_id) REFERENCES platform_points(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS platform_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    organization_id INTEGER NOT NULL UNIQUE,
                    plan TEXT NOT NULL DEFAULT 'control',
                    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'active', 'grace', 'expired')),
                    point_limit INTEGER NOT NULL DEFAULT 1,
                    current_period_end TEXT,
                    grace_until TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (organization_id) REFERENCES platform_organizations(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS platform_owner_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code_hash TEXT NOT NULL UNIQUE,
                    expires_at TEXT,
                    used_at TEXT,
                    used_by_user_id INTEGER,
                    code_plan TEXT,
                    code_point_limit INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS platform_invites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    organization_id INTEGER NOT NULL,
                    contact TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('manager', 'employee')),
                    token_hash TEXT NOT NULL UNIQUE,
                    expires_at TEXT NOT NULL,
                    accepted_at TEXT,
                    accepted_by_user_id INTEGER,
                    created_by_user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (organization_id) REFERENCES platform_organizations(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS platform_finance_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    organization_id INTEGER NOT NULL,
                    point_id INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('income', 'expense')),
                    category TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0),
                    period_month TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'manual',
                    created_by_user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (organization_id) REFERENCES platform_organizations(id) ON DELETE CASCADE,
                    FOREIGN KEY (point_id) REFERENCES platform_points(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS platform_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    organization_id INTEGER NOT NULL,
                    actor_user_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (organization_id) REFERENCES platform_organizations(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS platform_automation_settings (
                    organization_id INTEGER PRIMARY KEY,
                    preset TEXT NOT NULL DEFAULT 'standard',
                    settings_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (organization_id) REFERENCES platform_organizations(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS platform_notification_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    organization_id INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('owner', 'manager', 'employee')),
                    recipient_user_id INTEGER NOT NULL,
                    period_start TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    UNIQUE (organization_id, role, recipient_user_id, period_start),
                    FOREIGN KEY (organization_id) REFERENCES platform_organizations(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS platform_subscription_notification_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    organization_id INTEGER NOT NULL,
                    notification_type TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    UNIQUE (organization_id, notification_type, period_end),
                    FOREIGN KEY (organization_id) REFERENCES platform_organizations(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS platform_rate_limits (
                    bucket_key TEXT PRIMARY KEY,
                    window_started INTEGER NOT NULL,
                    attempts INTEGER NOT NULL CHECK (attempts >= 0)
                );
                CREATE TABLE IF NOT EXISTS platform_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_platform_points_org ON platform_points(organization_id);
                CREATE INDEX IF NOT EXISTS idx_platform_point_access_org_user
                    ON platform_point_access(organization_id, web_user_id);
                CREATE INDEX IF NOT EXISTS idx_platform_finance_org_month
                    ON platform_finance_entries(organization_id, period_month);
                CREATE INDEX IF NOT EXISTS idx_platform_member_rates_org
                    ON platform_member_rates(organization_id, member_web_user_id);
                """
            )
            applied_versions = {
                row[0] for row in db.execute(
                    "SELECT version FROM platform_schema_migrations ORDER BY version"
                )
            }
            if not applied_versions:
                db.execute(
                    "INSERT INTO platform_schema_migrations (version, applied_at) VALUES (?, ?)",
                    (1, utc_now().isoformat()),
                )
                applied_versions.add(1)
            if max(applied_versions) < PLATFORM_SCHEMA_VERSION:
                columns = {row[1] for row in db.execute("PRAGMA table_info(platform_points)")}
                organization_columns = {
                    row[1] for row in db.execute("PRAGMA table_info(platform_organizations)")
                }
                if "staff_registration_enabled" not in organization_columns:
                    db.execute(
                        "ALTER TABLE platform_organizations ADD COLUMN staff_registration_enabled INTEGER NOT NULL DEFAULT 1"
                    )
                if "telegram_chat_id" not in columns:
                    db.execute("ALTER TABLE platform_points ADD COLUMN telegram_chat_id INTEGER")
                if "telegram_chat_title" not in columns:
                    db.execute("ALTER TABLE platform_points ADD COLUMN telegram_chat_title TEXT NOT NULL DEFAULT ''")
                if "work_time_start" not in columns:
                    db.execute("ALTER TABLE platform_points ADD COLUMN work_time_start TEXT NOT NULL DEFAULT '09:00'")
                if "work_time_end" not in columns:
                    db.execute("ALTER TABLE platform_points ADD COLUMN work_time_end TEXT NOT NULL DEFAULT '18:00'")
                code_columns = {row[1] for row in db.execute("PRAGMA table_info(platform_owner_codes)")}
                if "code_plan" not in code_columns:
                    db.execute("ALTER TABLE platform_owner_codes ADD COLUMN code_plan TEXT")
                if "code_point_limit" not in code_columns:
                    db.execute("ALTER TABLE platform_owner_codes ADD COLUMN code_point_limit INTEGER")
                db.execute(
                    "INSERT INTO platform_schema_migrations (version, applied_at) VALUES (?, ?)",
                    (PLATFORM_SCHEMA_VERSION, utc_now().isoformat()),
                )

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def allow_rate_limit(
        self,
        bucket_key: str,
        limit: int,
        window_seconds: int,
        now: int | None = None,
    ) -> bool:
        """Atomically consume one attempt from a persistent rate-limit bucket.

        Bucket identifiers are hashed before storage because they can contain a
        username and an IP address.  ``BEGIN IMMEDIATE`` serializes concurrent
        workers using this SQLite transition store, so two requests cannot both
        pass the final slot in the same window.
        """
        if not bucket_key or len(bucket_key) > 500:
            return False
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("rate-limit bounds must be positive")
        current = int(time.time()) if now is None else int(now)
        digest = self._hash_token(bucket_key)
        self.init_schema()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT window_started, attempts FROM platform_rate_limits WHERE bucket_key = ?",
                (digest,),
            ).fetchone()
            if row is None or current - int(row[0]) >= window_seconds:
                db.execute(
                    "INSERT OR REPLACE INTO platform_rate_limits "
                    "(bucket_key, window_started, attempts) VALUES (?, ?, 1)",
                    (digest, current),
                )
                return True
            if int(row[1]) >= limit:
                return False
            db.execute(
                "UPDATE platform_rate_limits SET attempts = attempts + 1 WHERE bucket_key = ?",
                (digest,),
            )
            return True

    def issue_owner_code(
        self,
        expires_days: int = 30,
        plan: str | None = None,
        point_limit: int | None = None,
    ) -> str:
        """Create a one-time activation code for a manually paid owner."""
        if not 1 <= expires_days <= 3650:
            raise ValueError("Срок действия кода должен быть от 1 до 3650 дней")
        clean_plan = self._normalize_plan(plan) if plan else None
        if point_limit is not None and not 1 <= point_limit <= 1000:
            raise ValueError("Лимит ПВЗ должен быть от 1 до 1000")
        self.init_schema()
        raw = f"PVZ-{secrets.token_urlsafe(12).upper()}"
        now = utc_now()
        expires = now + timedelta(days=expires_days) if expires_days else None
        with self._connect() as db:
            db.execute(
                """INSERT INTO platform_owner_codes
                   (code_hash, expires_at, code_plan, code_point_limit, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (self._hash_token(raw), expires.isoformat() if expires else None,
                 clean_plan, point_limit, now.isoformat()),
            )
        return raw

    @staticmethod
    def _normalize_plan(plan: str) -> str:
        clean = plan.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", clean):
            raise ValueError("Некорректное имя тарифа")
        return clean

    def owner_code_is_available(self, code: str) -> bool:
        clean_code = code.strip().upper()
        if not clean_code or len(clean_code) > 80:
            return False
        self.init_schema()
        with self._connect() as db:
            row = db.execute(
                "SELECT expires_at, used_at FROM platform_owner_codes WHERE code_hash = ?",
                (self._hash_token(clean_code),),
            ).fetchone()
            if not row or row[1]:
                return False
            return not row[0] or parse_utc(row[0]) >= utc_now()

    def _org_for_user(self, db: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
        return db.execute(
            """
            SELECT o.* FROM platform_organizations o
            JOIN platform_members m ON m.organization_id = o.id
            WHERE m.web_user_id = ?
            ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'manager' THEN 1 ELSE 2 END
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()

    def activate_owner(self, user_id: int, username: str, code: str) -> dict[str, Any]:
        self.init_schema()
        clean_code = code.strip().upper()
        if not clean_code or len(clean_code) > 80:
            raise ValueError("Введите код подключения")
        now = utc_now()
        period_end = now + timedelta(days=30)
        with self._connect() as db:
            existing = self._org_for_user(db, user_id)
            if existing:
                return dict(existing)
            row = db.execute(
                """SELECT id, expires_at, used_at, code_plan, code_point_limit
                   FROM platform_owner_codes WHERE code_hash = ?""",
                (self._hash_token(clean_code),),
            ).fetchone()
            if not row or row[2]:
                raise ValueError("Код не найден или уже использован")
            if row[1] and parse_utc(row[1]) < now:
                raise ValueError("Срок действия кода истёк")
            claimed = db.execute(
                """UPDATE platform_owner_codes
                   SET used_at = ?, used_by_user_id = ?
                   WHERE id = ? AND used_at IS NULL""",
                (now.isoformat(), user_id, row[0]),
            )
            if claimed.rowcount != 1:
                raise ValueError("Код не найден или уже использован")
            cursor = db.execute(
                "INSERT INTO platform_organizations (owner_web_user_id, name, created_at) VALUES (?, ?, ?)",
                (user_id, f"Сеть {username}", now.isoformat()),
            )
            organization_id = cursor.lastrowid
            db.execute(
                "INSERT INTO platform_members (organization_id, web_user_id, role, created_at) VALUES (?, ?, 'owner', ?)",
                (organization_id, user_id, now.isoformat()),
            )
            db.execute(
                """INSERT INTO platform_subscriptions
                   (organization_id, plan, status, point_limit, current_period_end, created_at)
                   VALUES (?, ?, 'active', ?, ?, ?)""",
                (organization_id, row[3] or 'control', row[4] or 1,
                 period_end.isoformat(), now.isoformat()),
            )
            db.execute(
                """INSERT INTO platform_audit_events
                   (organization_id, actor_user_id, action, entity_type, entity_id, created_at)
                   VALUES (?, ?, 'owner_activated', 'organization', ?, ?)""",
                (organization_id, user_id, organization_id, now.isoformat()),
            )
            db.execute(
                """INSERT INTO platform_automation_settings
                   (organization_id, preset, settings_json, updated_at) VALUES (?, 'standard', ?, ?)""",
                (organization_id, json.dumps(AUTOMATION_PRESETS["standard"], ensure_ascii=False), now.isoformat()),
            )
            return dict(
                db.execute("SELECT * FROM platform_organizations WHERE id = ?", (organization_id,)).fetchone()
            )

    def renew_subscription(self, user_id: int, code: str, days: int = 30) -> None:
        clean_code = code.strip().upper()
        if not clean_code or len(clean_code) > 80 or days not in {30, 90, 365}:
            raise ValueError("Некорректный код продления")
        now = utc_now()
        with self._connect() as db:
            org = self._org_for_user(db, user_id)
            if not org:
                raise ValueError("Рабочее пространство не найдено")
            member = db.execute(
                "SELECT role FROM platform_members WHERE organization_id = ? AND web_user_id = ?",
                (org["id"], user_id),
            ).fetchone()
            if not member or member[0] != "owner":
                raise PermissionError("Продлевать подписку может только владелец")
            row = db.execute(
                """SELECT id, expires_at, used_at, code_plan, code_point_limit
                   FROM platform_owner_codes WHERE code_hash = ?""",
                (self._hash_token(clean_code),),
            ).fetchone()
            if not row or row[2] or (row[1] and parse_utc(row[1]) < now):
                raise ValueError("Код не найден, истёк или уже использован")
            claimed = db.execute(
                """UPDATE platform_owner_codes
                   SET used_at = ?, used_by_user_id = ?
                   WHERE id = ? AND used_at IS NULL""",
                (now.isoformat(), user_id, row[0]),
            )
            if claimed.rowcount != 1:
                raise ValueError("Код не найден, истёк или уже использован")
            current = db.execute(
                "SELECT current_period_end FROM platform_subscriptions WHERE organization_id = ?",
                (org["id"],),
            ).fetchone()
            current_end = parse_utc(current[0]) if current and current[0] else now
            start = max(now, current_end)
            new_end = start + timedelta(days=days)
            if row[3] or row[4]:
                db.execute(
                    """UPDATE platform_subscriptions
                       SET plan = COALESCE(?, plan), point_limit = COALESCE(?, point_limit),
                           status = 'active', current_period_end = ?, grace_until = NULL
                       WHERE organization_id = ?""",
                    (row[3], row[4], new_end.isoformat(), org["id"]),
                )
            else:
                db.execute(
                    """UPDATE platform_subscriptions
                       SET status = 'active', current_period_end = ?, grace_until = NULL
                       WHERE organization_id = ?""",
                    (new_end.isoformat(), org["id"]),
                )
            db.execute(
                """INSERT INTO platform_audit_events
                   (organization_id, actor_user_id, action, entity_type, entity_id, metadata, created_at)
                   VALUES (?, ?, 'subscription_renewed', 'subscription', NULL, ?, ?)""",
                (org["id"], user_id, json.dumps({"days": days}), now.isoformat()),
            )

    def refresh_subscription(self, organization_id: int, now: datetime | None = None) -> dict[str, Any] | None:
        """Reconcile active/grace/expired state from the stored period dates."""
        now = now or utc_now()
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM platform_subscriptions WHERE organization_id = ?",
                (organization_id,),
            ).fetchone()
            if not row:
                return None
            if not row["current_period_end"]:
                return dict(row)
            period_end = parse_utc(row["current_period_end"])
            grace_until = (
                parse_utc(row["grace_until"])
                if row["grace_until"]
                else period_end + timedelta(days=SUBSCRIPTION_GRACE_DAYS)
            )
            if now <= period_end:
                status = "active"
                stored_grace = None
            elif now <= grace_until:
                status = "grace"
                stored_grace = grace_until.isoformat()
            else:
                status = "expired"
                stored_grace = grace_until.isoformat()
            if row["status"] != status or row["grace_until"] != stored_grace:
                db.execute(
                    "UPDATE platform_subscriptions SET status = ?, grace_until = ? WHERE organization_id = ?",
                    (status, stored_grace, organization_id),
                )
                row = db.execute(
                    "SELECT * FROM platform_subscriptions WHERE organization_id = ?",
                    (organization_id,),
                ).fetchone()
            return dict(row)

    def dashboard(self, user_id: int) -> dict[str, Any] | None:
        self.init_schema()
        with self._connect() as db:
            org = self._org_for_user(db, user_id)
            if not org:
                return None
            organization_id = org["id"]
            self.refresh_subscription(organization_id)
            member = db.execute(
                "SELECT role FROM platform_members WHERE organization_id = ? AND web_user_id = ?",
                (organization_id, user_id),
            ).fetchone()
            viewer_role = member["role"] if member else None
            can_manage_finance = viewer_role in {"owner", "manager"}
            visible_point_ids = self._visible_point_ids(db, organization_id, user_id, viewer_role)
            if visible_point_ids:
                visible = set(visible_point_ids)
                point_rows = [
                    row for row in db.execute(
                        "SELECT * FROM platform_points WHERE organization_id = ? ORDER BY id",
                        (organization_id,),
                    ).fetchall()
                    if row["id"] in visible
                ]
                if can_manage_finance:
                    finance_rows = [
                        row for row in db.execute(
                            """
                            SELECT f.*, p.name AS point_name FROM platform_finance_entries f
                            JOIN platform_points p ON p.id = f.point_id
                            WHERE f.organization_id = ? ORDER BY f.created_at DESC LIMIT 20
                            """,
                            (organization_id,),
                        ).fetchall()
                        if row["point_id"] in visible
                    ]
                else:
                    finance_rows = []
            else:
                point_rows = []
                finance_rows = []
            points = [dict(row) for row in point_rows]
            members = [dict(row) for row in db.execute(
                """SELECT m.role, m.web_user_id, m.created_at
                   FROM platform_members m WHERE m.organization_id = ? ORDER BY m.id""",
                (organization_id,),
            )]
            subscription = db.execute(
                "SELECT * FROM platform_subscriptions WHERE organization_id = ?", (organization_id,)
            ).fetchone()
            subscription_data = dict(subscription) if subscription else None
            if subscription_data:
                now = utc_now()
                period_end = parse_utc(subscription_data["current_period_end"]) if subscription_data["current_period_end"] else None
                grace_until = parse_utc(subscription_data["grace_until"]) if subscription_data["grace_until"] else None
                subscription_data["days_remaining"] = max(0, (period_end.date() - now.date()).days) if period_end else None
                subscription_data["grace_days_remaining"] = max(0, (grace_until.date() - now.date()).days) if grace_until else 0
            finance = [dict(row) for row in finance_rows]
            summary_rows = db.execute(
                """
                SELECT p.id, p.name,
                       COALESCE(SUM(CASE WHEN f.kind = 'income' THEN f.amount_minor ELSE 0 END), 0) AS income_minor,
                       COALESCE(SUM(CASE WHEN f.kind = 'expense' THEN f.amount_minor ELSE 0 END), 0) AS expense_minor
                FROM platform_points p
                LEFT JOIN platform_finance_entries f ON f.point_id = p.id
                WHERE p.organization_id = ?
                GROUP BY p.id, p.name
                ORDER BY p.id
                """,
                (organization_id,),
            ).fetchall() if can_manage_finance else []
            visible = set(visible_point_ids)
            finance_summary = [
                {
                    "point_id": row["id"],
                    "point_name": row["name"],
                    "income_minor": row["income_minor"],
                    "expense_minor": row["expense_minor"],
                    "profit_minor": row["income_minor"] - row["expense_minor"],
                }
                for row in summary_rows
                if row["id"] in visible
            ]
            rates = self._rates_for_dashboard(
                db, organization_id, user_id, viewer_role, visible_point_ids
            )
            invites = [dict(row) for row in db.execute(
                """SELECT id, contact, role, expires_at, accepted_at, created_at
                   FROM platform_invites WHERE organization_id = ? ORDER BY id DESC LIMIT 10""",
                (organization_id,),
            )]
            automation = db.execute(
                "SELECT preset, settings_json, updated_at FROM platform_automation_settings WHERE organization_id = ?",
                (organization_id,),
            ).fetchone()
            if automation:
                automation_data = {
                    "preset": automation["preset"],
                    "settings": json.loads(automation["settings_json"]),
                    "updated_at": automation["updated_at"],
                }
            else:
                automation_data = {
                    "preset": "standard",
                    "settings": AUTOMATION_PRESETS["standard"],
                    "updated_at": None,
                }
            audit = []
            if viewer_role in {"owner", "manager"}:
                audit_rows = db.execute(
                    """
                    SELECT action, entity_type, entity_id, metadata, created_at, actor_user_id
                    FROM platform_audit_events
                    WHERE organization_id = ?
                    ORDER BY id DESC LIMIT 30
                    """,
                    (organization_id,),
                ).fetchall()
                for row in audit_rows:
                    try:
                        metadata = json.loads(row[3] or "{}")
                    except (TypeError, ValueError):
                        metadata = {}
                    audit.append({
                        "action": row[0],
                        "entity_type": row[1],
                        "entity_id": row[2],
                        "actor_user_id": row[5],
                        "metadata": metadata if isinstance(metadata, dict) else {},
                        "created_at": row[4],
                    })
            return {
                "organization": dict(org),
                "viewer_role": viewer_role,
                "can_manage_structure": viewer_role == "owner",
                "can_manage_finance": can_manage_finance,
                "visible_point_ids": visible_point_ids,
                "points": points,
                "members": members,
                "subscription": subscription_data,
                "finance": finance,
                "finance_summary": finance_summary,
                "rates": rates,
                "invites": invites,
                "automation": automation_data,
                "audit": audit,
            }

    def finance_export(self, user_id: int) -> list[dict[str, Any]] | None:
        """Return all finance entries visible to a workspace member for export."""
        self.init_schema()
        with self._connect() as db:
            org = self._org_for_user(db, user_id)
            if not org:
                return None
            member = db.execute(
                "SELECT role FROM platform_members WHERE organization_id = ? AND web_user_id = ?",
                (org["id"], user_id),
            ).fetchone()
            viewer_role = member["role"] if member else None
            if viewer_role not in {"owner", "manager"}:
                return None
            visible = set(self._visible_point_ids(db, org["id"], user_id, viewer_role))
            if not visible:
                return []
            rows = db.execute(
                """
                SELECT f.point_id, f.period_month, f.kind, f.category, f.amount_minor, f.note,
                       f.source, f.created_at, p.name AS point_name
                FROM platform_finance_entries f
                JOIN platform_points p ON p.id = f.point_id
                WHERE f.organization_id = ?
                ORDER BY f.period_month DESC, f.created_at DESC, f.id DESC
                """,
                (org["id"],),
            ).fetchall()
            return [dict(row) for row in rows if row["point_id"] in visible]

    @staticmethod
    def _validate_rate_amount(amount_minor: int) -> int:
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
            raise ValueError("Ставка должна быть целым числом копеек")
        if amount_minor < 0 or amount_minor > 10_000_000:
            raise ValueError("Ставка должна быть от 0 до 100 000 ₽ в час")
        return amount_minor

    @staticmethod
    def _validate_money_amount(amount_minor: int) -> int:
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
            raise ValueError("Сумма должна быть целым числом копеек")
        if amount_minor < 0 or amount_minor > 10_000_000_00:
            raise ValueError("Сумма должна быть от 0 до 100 000 000 ₽")
        return amount_minor

    def _point_for_owner(
        self, db: sqlite3.Connection, organization_id: int, point_id: int
    ) -> sqlite3.Row:
        point = db.execute(
            "SELECT id, name FROM platform_points WHERE id = ? AND organization_id = ? AND status = 'active'",
            (point_id, organization_id),
        ).fetchone()
        if not point:
            raise ValueError("ПВЗ не найден")
        return point

    def set_point_rate(self, owner_user_id: int, point_id: int, amount_minor: int) -> None:
        amount_minor = self._validate_rate_amount(amount_minor)
        now = utc_now().isoformat()
        with self._connect() as db:
            org = self._require_owner(db, owner_user_id)
            point = self._point_for_owner(db, org["id"], point_id)
            db.execute(
                """INSERT INTO platform_point_rates
                   (organization_id, point_id, amount_minor, updated_by_user_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(point_id) DO UPDATE SET
                     amount_minor = excluded.amount_minor,
                     updated_by_user_id = excluded.updated_by_user_id,
                     updated_at = excluded.updated_at""",
                (org["id"], point["id"], amount_minor, owner_user_id, now, now),
            )
            db.execute(
                """INSERT INTO platform_audit_events
                   (organization_id, actor_user_id, action, entity_type, entity_id, metadata, created_at)
                   VALUES (?, ?, 'point_rate_updated', 'point', ?, ?, ?)""",
                (org["id"], owner_user_id, point["id"], json.dumps({"amount_minor": amount_minor}), now),
            )

    def set_member_rate(
        self,
        owner_user_id: int,
        member_user_id: int,
        point_id: int,
        amount_minor: int,
        visibility: str = "personal",
    ) -> None:
        amount_minor = self._validate_rate_amount(amount_minor)
        if visibility not in {"personal", "hidden"}:
            raise ValueError("Неизвестный режим видимости ставки")
        now = utc_now().isoformat()
        with self._connect() as db:
            org = self._require_owner(db, owner_user_id)
            point = self._point_for_owner(db, org["id"], point_id)
            member = db.execute(
                """SELECT role FROM platform_members
                   WHERE organization_id = ? AND web_user_id = ?""",
                (org["id"], member_user_id),
            ).fetchone()
            if not member or member["role"] == "owner":
                raise ValueError("Участник не найден")
            db.execute(
                """INSERT INTO platform_member_rates
                   (organization_id, point_id, member_web_user_id, amount_minor, visibility,
                    updated_by_user_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(point_id, member_web_user_id) DO UPDATE SET
                     amount_minor = excluded.amount_minor,
                     visibility = excluded.visibility,
                     updated_by_user_id = excluded.updated_by_user_id,
                     updated_at = excluded.updated_at""",
                (org["id"], point["id"], member_user_id, amount_minor, visibility,
                 owner_user_id, now, now),
            )
            db.execute(
                """INSERT INTO platform_audit_events
                   (organization_id, actor_user_id, action, entity_type, entity_id, metadata, created_at)
                   VALUES (?, ?, 'member_rate_updated', 'point', ?, ?, ?)""",
                (
                    org["id"], owner_user_id, point["id"],
                    json.dumps({"member_user_id": member_user_id, "visibility": visibility}), now,
                ),
            )

    def _rates_for_dashboard(
        self,
        db: sqlite3.Connection,
        organization_id: int,
        user_id: int,
        role: str | None,
        visible_point_ids: list[int],
    ) -> list[dict[str, Any]]:
        if not visible_point_ids:
            return []
        visible = set(visible_point_ids)
        defaults = db.execute(
            """SELECT r.point_id, p.name AS point_name, r.amount_minor
                FROM platform_point_rates r
                JOIN platform_points p ON p.id = r.point_id
                WHERE r.organization_id = ?
                ORDER BY r.point_id""",
            (organization_id,),
        ).fetchall()
        rates = [
            {
                "point_id": row["point_id"],
                "point_name": row["point_name"],
                "scope": "point",
                "member_web_user_id": None,
                "amount_minor": row["amount_minor"],
                "visibility": "shared",
            }
            for row in defaults
            if row["point_id"] in visible
        ]
        member_params: list[Any] = [organization_id]
        query = """SELECT r.point_id, p.name AS point_name, r.member_web_user_id,
                           r.amount_minor, r.visibility
                    FROM platform_member_rates r
                    JOIN platform_points p ON p.id = r.point_id
                    WHERE r.organization_id = ?"""
        if role != "owner":
            query += " AND r.member_web_user_id = ?"
            member_params.append(user_id)
        query += " ORDER BY r.point_id, r.member_web_user_id"
        for row in db.execute(query, member_params).fetchall():
            if row["point_id"] not in visible:
                continue
            rates.append(
                {
                    "point_id": row["point_id"],
                    "point_name": row["point_name"],
                    "scope": "member",
                    "member_web_user_id": row["member_web_user_id"],
                    "amount_minor": row["amount_minor"],
                    "visibility": row["visibility"],
                }
            )
        return rates

    @staticmethod
    def _visible_point_ids(
        db: sqlite3.Connection, organization_id: int, user_id: int, role: str | None
    ) -> list[int]:
        if role == "owner":
            rows = db.execute(
                "SELECT id FROM platform_points WHERE organization_id = ? AND status = 'active' ORDER BY id",
                (organization_id,),
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT p.id FROM platform_points p
                JOIN platform_point_access a ON a.point_id = p.id
                WHERE p.organization_id = ? AND a.organization_id = ?
                  AND a.web_user_id = ? AND p.status = 'active'
                ORDER BY p.id
                """,
                (organization_id, organization_id, user_id),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def update_automation_settings(self, user_id: int, preset: str, commands: dict[str, str], notifications: dict[str, int | str]) -> None:
        if preset not in AUTOMATION_PRESETS and preset != "custom":
            raise ValueError("Неизвестный пресет")
        allowed_commands = {"start_shift", "end_shift", "handover"}
        if set(commands) != allowed_commands:
            raise ValueError("Нужно задать все команды смены")
        normalized_commands = {}
        for key, value in commands.items():
            clean = value.strip()
            if not clean or len(clean) > 40 or "\n" in clean:
                raise ValueError("Команды должны быть от 1 до 40 символов")
            normalized_commands[key] = clean
        allowed_notifications = {
            "employee_payroll_days_before",
            "manager_payroll_days_before",
            "owner_payroll_days_before",
            "employee_payroll_time",
            "manager_payroll_time",
            "owner_payroll_time",
            "shift_alert_minutes",
        }
        legacy_notifications = allowed_notifications - {
            "employee_payroll_time",
            "manager_payroll_time",
            "owner_payroll_time",
        }
        if set(notifications) == legacy_notifications:
            notifications = {
                **notifications,
                "employee_payroll_time": "10:00",
                "manager_payroll_time": "10:00",
                "owner_payroll_time": "10:00",
            }
        if set(notifications) != allowed_notifications:
            raise ValueError("Нужно задать все интервалы уведомлений")
        normalized_notifications = {}
        for key, value in notifications.items():
            if key.endswith("_time"):
                try:
                    parsed = datetime.strptime(str(value), "%H:%M")
                except (TypeError, ValueError) as exc:
                    raise ValueError("Время должно быть в формате ЧЧ:ММ") from exc
                normalized_notifications[key] = parsed.strftime("%H:%M")
                continue
            integer = int(value)
            upper = 60 if key == "shift_alert_minutes" else 30
            if integer < 0 or integer > upper:
                raise ValueError("Интервал уведомлений вне допустимого диапазона")
            normalized_notifications[key] = integer
        now = utc_now().isoformat()
        payload = {"commands": normalized_commands, "notifications": normalized_notifications}
        with self._connect() as db:
            org = self._require_owner(db, user_id)
            db.execute(
                """INSERT INTO platform_automation_settings (organization_id, preset, settings_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(organization_id) DO UPDATE SET
                     preset = excluded.preset, settings_json = excluded.settings_json, updated_at = excluded.updated_at""",
                (org["id"], preset, json.dumps(payload, ensure_ascii=False), now),
            )
            db.execute(
                """INSERT INTO platform_audit_events
                   (organization_id, actor_user_id, action, entity_type, entity_id, metadata, created_at)
                   VALUES (?, ?, 'automation_updated', 'automation', NULL, ?, ?)""",
                (org["id"], user_id, json.dumps({"preset": preset}, ensure_ascii=False), now),
            )

    def notification_targets(self) -> list[dict[str, Any]]:
        """Return active organizations and their workspace members."""
        self.init_schema()
        with self._connect() as db:
            organization_ids = [row[0] for row in db.execute("SELECT id FROM platform_organizations")]
        for organization_id in organization_ids:
            self.refresh_subscription(organization_id)
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT o.id AS organization_id, o.name, m.role, m.web_user_id,
                       a.settings_json
                FROM platform_organizations o
                JOIN platform_members m ON m.organization_id = o.id
                JOIN platform_automation_settings a ON a.organization_id = o.id
                JOIN platform_subscriptions s ON s.organization_id = o.id
                WHERE s.status IN ('active', 'grace')
                ORDER BY o.id, m.id
                """
            ).fetchall()
            return [
                {
                    "organization_id": row["organization_id"],
                    "organization_name": row["name"],
                    "role": row["role"],
                    "web_user_id": row["web_user_id"],
                    "settings": json.loads(row["settings_json"]),
                }
                for row in rows
            ]

    def claim_notification(self, organization_id: int, role: str, recipient_user_id: int, period_start: str) -> bool:
        """Claim one reminder atomically; return False when already claimed."""
        with self._connect() as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO platform_notification_log
                    (organization_id, role, recipient_user_id, period_start, claimed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (organization_id, role, recipient_user_id, period_start, utc_now().isoformat()),
            )
            return cursor.rowcount == 1

    def release_notification(self, organization_id: int, role: str, recipient_user_id: int, period_start: str) -> None:
        with self._connect() as db:
            db.execute(
                """
                DELETE FROM platform_notification_log
                WHERE organization_id = ? AND role = ? AND recipient_user_id = ? AND period_start = ?
                """,
                (organization_id, role, recipient_user_id, period_start),
            )

    def subscription_targets(self) -> list[dict[str, Any]]:
        """Return owners who can receive subscription-expiry reminders."""
        self.init_schema()
        with self._connect() as db:
            organization_ids = [row[0] for row in db.execute("SELECT id FROM platform_organizations")]
        for organization_id in organization_ids:
            self.refresh_subscription(organization_id)
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT o.id AS organization_id, o.name, o.owner_web_user_id,
                       s.current_period_end, s.status
                FROM platform_organizations o
                JOIN platform_subscriptions s ON s.organization_id = o.id
                WHERE s.current_period_end IS NOT NULL
                  AND s.status IN ('active', 'grace', 'expired')
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def claim_subscription_notification(
        self, organization_id: int, notification_type: str, period_end: str
    ) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO platform_subscription_notification_log
                    (organization_id, notification_type, period_end, claimed_at)
                VALUES (?, ?, ?, ?)
                """,
                (organization_id, notification_type, period_end, utc_now().isoformat()),
            )
            return cursor.rowcount == 1

    def release_subscription_notification(
        self, organization_id: int, notification_type: str, period_end: str
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                DELETE FROM platform_subscription_notification_log
                WHERE organization_id = ? AND notification_type = ? AND period_end = ?
                """,
                (organization_id, notification_type, period_end),
            )

    def _require_member(self, db: sqlite3.Connection, user_id: int, roles: set[str]) -> sqlite3.Row:
        org = self._org_for_user(db, user_id)
        if not org:
            raise ValueError("Сначала активируйте рабочее пространство")
        subscription = self.refresh_subscription(org["id"])
        if subscription and subscription["status"] == "expired":
            raise ValueError("Подписка истекла — продлите её для изменения данных")
        member = db.execute(
            "SELECT role FROM platform_members WHERE organization_id = ? AND web_user_id = ?",
            (org["id"], user_id),
        ).fetchone()
        if not member or member[0] not in roles:
            raise PermissionError("Недостаточно прав")
        return org

    def _require_owner(self, db: sqlite3.Connection, user_id: int) -> sqlite3.Row:
        """Require organization-owner privileges for structural mutations."""
        return self._require_member(db, user_id, {"owner"})

    def _require_manager(self, db: sqlite3.Connection, user_id: int) -> sqlite3.Row:
        """Require owner or manager privileges for operational entries."""
        return self._require_member(db, user_id, {"owner", "manager"})

    def create_point(self, user_id: int, name: str, address: str) -> int:
        clean_name, clean_address = name.strip(), address.strip()
        if not clean_name or len(clean_name) > 120 or len(clean_address) > 240:
            raise ValueError("Укажите название точки")
        now = utc_now().isoformat()
        with self._connect() as db:
            org = self._require_owner(db, user_id)
            limit = db.execute(
                "SELECT point_limit, status FROM platform_subscriptions WHERE organization_id = ?",
                (org["id"],),
            ).fetchone()
            count = db.execute(
                "SELECT COUNT(*) FROM platform_points WHERE organization_id = ? AND status = 'active'",
                (org["id"],),
            ).fetchone()[0]
            if not limit or limit[1] not in {"active", "grace"} or count >= limit[0]:
                raise ValueError("Лимит активных ПВЗ по подписке исчерпан")
            cursor = db.execute(
                "INSERT INTO platform_points (organization_id, name, address, created_at) VALUES (?, ?, ?, ?)",
                (org["id"], clean_name, clean_address, now),
            )
            point_id = cursor.lastrowid
            db.execute(
                """
                INSERT OR IGNORE INTO platform_point_access
                    (organization_id, web_user_id, point_id, created_at)
                SELECT organization_id, web_user_id, ?, ?
                FROM platform_members
                WHERE organization_id = ? AND role IN ('manager', 'employee')
                """,
                (point_id, now, org["id"]),
            )
            db.execute(
                """INSERT INTO platform_audit_events
                   (organization_id, actor_user_id, action, entity_type, entity_id, created_at)
                   VALUES (?, ?, 'point_created', 'point', ?, ?)""",
                (org["id"], user_id, point_id, now),
            )
            return int(point_id)

    def set_point_access(
        self, owner_user_id: int, member_user_id: int, point_id: int, granted: bool
    ) -> None:
        """Grant or revoke a member's access to one active point."""
        now = utc_now().isoformat()
        with self._connect() as db:
            org = self._require_owner(db, owner_user_id)
            member = db.execute(
                """
                SELECT role FROM platform_members
                WHERE organization_id = ? AND web_user_id = ?
                """,
                (org["id"], member_user_id),
            ).fetchone()
            point = db.execute(
                """
                SELECT id FROM platform_points
                WHERE organization_id = ? AND id = ? AND status = 'active'
                """,
                (org["id"], point_id),
            ).fetchone()
            if not member or member["role"] == "owner":
                raise ValueError("Участник не найден или не может быть ограничен")
            if not point:
                raise ValueError("ПВЗ не найден")
            if granted:
                db.execute(
                    """
                    INSERT OR IGNORE INTO platform_point_access
                        (organization_id, web_user_id, point_id, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (org["id"], member_user_id, point_id, now),
                )
                action = "point_access_granted"
            else:
                db.execute(
                    """
                    DELETE FROM platform_point_access
                    WHERE organization_id = ? AND web_user_id = ? AND point_id = ?
                    """,
                    (org["id"], member_user_id, point_id),
                )
                action = "point_access_revoked"
            db.execute(
                """
                INSERT INTO platform_audit_events
                    (organization_id, actor_user_id, action, entity_type, entity_id, metadata, created_at)
                VALUES (?, ?, ?, 'point', ?, ?, ?)
                """,
                (org["id"], owner_user_id, action, point_id, json.dumps({"member_user_id": member_user_id}), now),
            )

    def connect_chat(self, user_id: int, point_id: int, chat_id: int, chat_title: str = '') -> None:
        if not chat_id or len(str(abs(chat_id))) > 30:
            raise ValueError("Некорректный Telegram chat id")
        with self._connect() as db:
            org = self._require_owner(db, user_id)
            point = db.execute(
                "SELECT id FROM platform_points WHERE id = ? AND organization_id = ?",
                (point_id, org["id"]),
            ).fetchone()
            if not point:
                raise ValueError("ПВЗ не найден")
            duplicate = db.execute(
                "SELECT id FROM platform_points WHERE telegram_chat_id = ? AND id != ?",
                (chat_id, point_id),
            ).fetchone()
            if duplicate:
                raise ValueError("Этот чат уже подключён к другому ПВЗ")
            db.execute(
                "UPDATE platform_points SET telegram_chat_id = ?, telegram_chat_title = ? WHERE id = ?",
                (chat_id, chat_title.strip()[:160], point_id),
            )
            db.execute(
                """INSERT INTO platform_audit_events
                   (organization_id, actor_user_id, action, entity_type, entity_id, metadata, created_at)
                   VALUES (?, ?, 'chat_connected', 'point', ?, ?, ?)""",
                (org["id"], user_id, point_id, json.dumps({"chat_id": chat_id}), utc_now().isoformat()),
            )

    def set_point_schedule(
        self, owner_user_id: int, point_id: int, work_time_start: str, work_time_end: str
    ) -> None:
        try:
            start = datetime.strptime(work_time_start.strip(), "%H:%M").strftime("%H:%M")
            end = datetime.strptime(work_time_end.strip(), "%H:%M").strftime("%H:%M")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Время работы должно быть в формате ЧЧ:ММ") from exc
        if start == end:
            raise ValueError("Начало и конец работы не должны совпадать")
        now = utc_now().isoformat()
        with self._connect() as db:
            org = self._require_owner(db, owner_user_id)
            point = self._point_for_owner(db, org["id"], point_id)
            db.execute(
                "UPDATE platform_points SET work_time_start = ?, work_time_end = ? WHERE id = ?",
                (start, end, point["id"]),
            )
            db.execute(
                """INSERT INTO platform_audit_events
                   (organization_id, actor_user_id, action, entity_type, entity_id, metadata, created_at)
                   VALUES (?, ?, 'point_schedule_updated', 'point', ?, ?, ?)""",
                (org["id"], owner_user_id, point["id"],
                 json.dumps({"start": start, "end": end}), now),
            )

    def chat_runtime_config(self, owner_user_id: int, point_id: int) -> dict[str, Any]:
        """Return the transitional bot configuration for an owner's point."""
        with self._connect() as db:
            org = self._require_owner(db, owner_user_id)
            row = db.execute(
                """SELECT p.id, p.name, p.telegram_chat_id, p.telegram_chat_title,
                          p.work_time_start, p.work_time_end,
                          COALESCE(r.amount_minor, 0) AS amount_minor
                   FROM platform_points p
                   LEFT JOIN platform_point_rates r ON r.point_id = p.id
                   WHERE p.id = ? AND p.organization_id = ? AND p.status = 'active'""",
                (point_id, org["id"]),
            ).fetchone()
            if not row or not row["telegram_chat_id"]:
                raise ValueError("Сначала подключите Telegram-чат к ПВЗ")
            return dict(row)

    def automation_for_chat(self, chat_id: int) -> dict[str, Any] | None:
        self.init_schema()
        with self._connect() as db:
            row = db.execute(
                """SELECT p.organization_id FROM platform_points p
                   WHERE p.telegram_chat_id = ? AND p.status = 'active'""",
                (chat_id,),
            ).fetchone()
            if not row:
                return None
            settings = db.execute(
                "SELECT settings_json FROM platform_automation_settings WHERE organization_id = ?",
                (row["organization_id"],),
            ).fetchone()
            if not settings:
                return AUTOMATION_PRESETS["standard"]
            return json.loads(settings["settings_json"])

    def create_invite(self, user_id: int, contact: str, role: str) -> str:
        clean_contact = contact.strip()
        if not clean_contact or len(clean_contact) > 160 or role not in {"manager", "employee"}:
            raise ValueError("Укажите контакт и роль приглашённого")
        now = utc_now()
        expires = now + timedelta(days=7)
        raw = f"INV-{secrets.token_urlsafe(18).upper()}"
        with self._connect() as db:
            org = self._require_owner(db, user_id)
            if not org["staff_registration_enabled"]:
                raise ValueError("Регистрация сотрудников по приглашениям отключена")
            db.execute(
                """INSERT INTO platform_invites
                   (organization_id, contact, role, token_hash, expires_at, created_by_user_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (org["id"], clean_contact, role, self._hash_token(raw), expires.isoformat(), user_id, now.isoformat()),
            )
        return raw

    def get_invite(self, token: str) -> dict[str, Any] | None:
        """Return a safe preview of an unexpired invitation without consuming it."""
        clean_token = token.strip()
        if not clean_token or len(clean_token) > 120:
            return None
        self.init_schema()
        with self._connect() as db:
            row = db.execute(
                """
                SELECT i.id, i.organization_id, i.role, i.expires_at, i.accepted_at,
                       o.name AS organization_name, o.staff_registration_enabled
                FROM platform_invites i
                JOIN platform_organizations o ON o.id = i.organization_id
                WHERE i.token_hash = ?
                """,
                (self._hash_token(clean_token),),
            ).fetchone()
            if (not row or not row["staff_registration_enabled"] or row["accepted_at"]
                    or parse_utc(row["expires_at"]) < utc_now()):
                return None
            return dict(row)

    def accept_invite(self, user_id: int, token: str) -> None:
        clean_token = token.strip()
        if not clean_token or len(clean_token) > 120:
            raise ValueError("Приглашение не найдено")
        now = utc_now()
        with self._connect() as db:
            row = db.execute(
                """SELECT i.*, o.staff_registration_enabled
                   FROM platform_invites i
                   JOIN platform_organizations o ON o.id = i.organization_id
                   WHERE i.token_hash = ?""",
                (self._hash_token(clean_token),),
            ).fetchone()
            if not row or not row["staff_registration_enabled"] or parse_utc(row["expires_at"]) < now:
                raise ValueError("Приглашение не найдено или истекло")
            if row["accepted_at"]:
                raise ValueError("Приглашение уже использовано")
            claimed = db.execute(
                """UPDATE platform_invites
                   SET accepted_at = ?, accepted_by_user_id = ?
                   WHERE id = ? AND accepted_at IS NULL""",
                (now.isoformat(), user_id, row["id"]),
            )
            if claimed.rowcount != 1:
                raise ValueError("Приглашение не найдено или уже использовано")
            db.execute(
                """INSERT INTO platform_members (organization_id, web_user_id, role, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(organization_id, web_user_id) DO UPDATE SET role = excluded.role""",
                (row["organization_id"], user_id, row["role"], now.isoformat()),
            )
            db.execute(
                """
                INSERT OR IGNORE INTO platform_point_access
                    (organization_id, web_user_id, point_id, created_at)
                SELECT organization_id, ?, id, ?
                FROM platform_points
                WHERE organization_id = ? AND status = 'active'
                """,
                (user_id, now.isoformat(), row["organization_id"]),
            )
            db.execute(
                """INSERT INTO platform_audit_events
                   (organization_id, actor_user_id, action, entity_type, entity_id, created_at)
                   VALUES (?, ?, 'invite_accepted', 'invite', ?, ?)""",
                (row["organization_id"], user_id, row["id"], now.isoformat()),
            )

    def set_staff_registration_enabled(self, owner_user_id: int, enabled: bool) -> None:
        with self._connect() as db:
            org = self._require_owner(db, owner_user_id)
            value = 1 if enabled else 0
            db.execute(
                "UPDATE platform_organizations SET staff_registration_enabled = ? WHERE id = ?",
                (value, org["id"]),
            )
            db.execute(
                """INSERT INTO platform_audit_events
                   (organization_id, actor_user_id, action, entity_type, entity_id, metadata, created_at)
                   VALUES (?, ?, 'staff_registration_updated', 'organization', ?, ?, ?)""",
                (org["id"], owner_user_id, org["id"], json.dumps({"enabled": bool(value)}), utc_now().isoformat()),
            )

    def add_finance_entry(
        self,
        user_id: int,
        point_id: int,
        kind: str,
        category: str,
        amount_minor: int,
        period_month: str,
        note: str,
    ) -> None:
        amount_minor = self._validate_money_amount(amount_minor)
        if kind not in {"income", "expense"}:
            raise ValueError("Некорректная финансовая запись")
        if not isinstance(category, str) or not category.strip():
            raise ValueError("Укажите категорию финансовой записи")
        if not isinstance(note, str) or len(category.strip()) > 80 or len(note.strip()) > 500:
            raise ValueError("Слишком длинное описание")
        if not isinstance(period_month, str):
            raise ValueError("Период должен быть в формате YYYY-MM")
        try:
            datetime.strptime(period_month, "%Y-%m")
        except ValueError:
            raise ValueError("Период должен быть в формате YYYY-MM") from None
        now = utc_now().isoformat()
        with self._connect() as db:
            org = self._require_manager(db, user_id)
            point = db.execute(
                "SELECT id FROM platform_points WHERE id = ? AND organization_id = ?",
                (point_id, org["id"]),
            ).fetchone()
            if not point:
                raise ValueError("ПВЗ не найден")
            member = db.execute(
                "SELECT role FROM platform_members WHERE organization_id = ? AND web_user_id = ?",
                (org["id"], user_id),
            ).fetchone()
            if member and member["role"] != "owner":
                access = db.execute(
                    """
                    SELECT 1 FROM platform_point_access
                    WHERE organization_id = ? AND web_user_id = ? AND point_id = ?
                    """,
                    (org["id"], user_id, point_id),
                ).fetchone()
                if not access:
                    raise PermissionError("У вас нет доступа к этому ПВЗ")
            db.execute(
                """INSERT INTO platform_finance_entries
                   (organization_id, point_id, kind, category, amount_minor, period_month, note, created_by_user_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (org["id"], point_id, kind, category.strip(), amount_minor, period_month, note.strip(), user_id, now),
            )
