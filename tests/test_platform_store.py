import asyncio
import sqlite3
from datetime import datetime, timedelta

import pytest
from werkzeug.security import generate_password_hash

from database import Database
from platform_store import PlatformStore, money_to_minor, utc_now


def test_owner_activation_points_finance_invite_and_custom_automation(tmp_path):
    store = PlatformStore(str(tmp_path / "platform.sqlite3"))
    code = store.issue_owner_code()
    store.activate_owner(7, "owner", code)

    point_id = store.create_point(7, "ПВЗ на Ленина", "Ленина, 14")
    store.connect_chat(7, point_id, -100123, "Рабочий чат")
    store.set_staff_registration_enabled(7, False)
    assert store.dashboard(7)["organization"]["staff_registration_enabled"] == 0
    with pytest.raises(ValueError, match="отключена"):
        store.create_invite(7, "@blocked", "employee")
    store.set_staff_registration_enabled(7, True)
    invite = store.create_invite(7, "@manager", "manager")
    store.accept_invite(8, invite)
    with pytest.raises(ValueError, match="уже использовано"):
        store.accept_invite(9, invite)
    with pytest.raises(PermissionError):
        store.create_point(8, "Чужой ПВЗ", "Адрес")
    with pytest.raises(PermissionError):
        store.connect_chat(8, point_id, -100124, "Чужой чат")
    with pytest.raises(PermissionError):
        store.create_invite(8, "@employee", "employee")
    with pytest.raises(PermissionError):
        store.update_automation_settings(
            8,
            "custom",
            {"start_shift": "x", "end_shift": "y", "handover": "z"},
            {
                "employee_payroll_days_before": 1,
                "manager_payroll_days_before": 1,
                "owner_payroll_days_before": 1,
                "employee_payroll_time": "10:00",
                "manager_payroll_time": "10:00",
                "owner_payroll_time": "10:00",
                "shift_alert_minutes": 10,
            },
        )
    store.add_finance_entry(8, point_id, "income", "Поощрение", money_to_minor("1000"), "2026-09", "")
    store.set_point_access(7, 8, point_id, False)
    assert store.dashboard(8)["points"] == []
    with pytest.raises(PermissionError, match="нет доступа"):
        store.add_finance_entry(8, point_id, "income", "После отзыва", money_to_minor("1"), "2026-09", "")
    store.set_point_access(7, 8, point_id, True)
    store.add_finance_entry(7, point_id, "expense", "Аренда", money_to_minor("50 000"), "2026-09", "")
    assert len(store.finance_export(8)) == 2
    store.set_point_access(7, 8, point_id, False)
    assert store.finance_export(8) == []
    store.set_point_access(7, 8, point_id, True)
    store.update_automation_settings(
        7,
        "custom",
        {"start_shift": "открылись", "end_shift": "закрылись", "handover": "передали"},
        {
            "employee_payroll_days_before": 1,
            "manager_payroll_days_before": 2,
            "owner_payroll_days_before": 4,
            "shift_alert_minutes": 12,
        },
    )

    data = store.dashboard(7)
    assert data["points"][0]["name"] == "ПВЗ на Ленина"
    assert data["members"][-1]["role"] == "manager"
    assert any(item["amount_minor"] == 5_000_000 for item in data["finance"])
    summary = data["finance_summary"][0]
    assert summary["income_minor"] == 100_000
    assert summary["expense_minor"] == 5_000_000
    assert summary["profit_minor"] == -4_900_000
    assert data["automation"]["settings"]["commands"]["start_shift"] == "открылись"
    assert data["automation"]["settings"]["notifications"]["employee_payroll_time"] == "10:00"
    assert store.automation_for_chat(-100123)["commands"]["start_shift"] == "открылись"
    assert any(event["action"] == "point_created" for event in data["audit"])
    assert any(event["action"] == "automation_updated" for event in data["audit"])

    renewal_code = store.issue_owner_code()
    before = data["subscription"]["current_period_end"]
    store.renew_subscription(7, renewal_code, 30)
    assert store.dashboard(7)["subscription"]["current_period_end"] > before
    assert store.dashboard(7)["subscription"]["days_remaining"] >= 29
    with pytest.raises(ValueError, match="уже использован"):
        store.renew_subscription(7, renewal_code, 30)

    with pytest.raises(ValueError, match="уже использован"):
        store.activate_owner(9, "other", code)


def test_finance_amounts_and_periods_are_strictly_validated(tmp_path):
    store = PlatformStore(str(tmp_path / "platform.sqlite3"))
    store.activate_owner(7, "owner", store.issue_owner_code())
    point_id = store.create_point(7, "ПВЗ", "Адрес")

    with pytest.raises(ValueError, match="целым числом копеек"):
        store.add_finance_entry(7, point_id, "income", "Поощрение", 1.5, "2026-09", "")
    with pytest.raises(ValueError, match="формате YYYY-MM"):
        store.add_finance_entry(7, point_id, "income", "Поощрение", 100, "2026-13", "")
    with pytest.raises(ValueError, match="категорию"):
        store.add_finance_entry(7, point_id, "income", "", 100, "2026-09", "")

    assert money_to_minor("1,005") == 101
    for invalid in ("nan", "inf", "-inf", "not-a-number"):
        with pytest.raises(ValueError):
            money_to_minor(invalid)


def test_employee_cannot_read_or_export_management_finance(tmp_path):
    store = PlatformStore(str(tmp_path / "platform.sqlite3"))
    store.activate_owner(7, "owner", store.issue_owner_code())
    point_id = store.create_point(7, "ПВЗ", "Адрес")
    invite = store.create_invite(7, "@employee", "employee")
    store.accept_invite(9, invite)
    store.add_finance_entry(
        7, point_id, "income", "Поощрение", money_to_minor("1000"), "2026-09", ""
    )

    employee_dashboard = store.dashboard(9)
    assert employee_dashboard["can_manage_finance"] is False
    assert employee_dashboard["finance"] == []
    assert employee_dashboard["finance_summary"] == []
    assert store.finance_export(9) is None

    owner_dashboard = store.dashboard(7)
    assert owner_dashboard["can_manage_finance"] is True
    assert len(owner_dashboard["finance"]) == 1


def test_activation_and_renewal_codes_can_change_plan_point_limit(tmp_path):
    store = PlatformStore(str(tmp_path / "platform.sqlite3"))
    code = store.issue_owner_code(plan="network", point_limit=3)
    store.activate_owner(7, "owner", code)
    subscription = store.dashboard(7)["subscription"]
    assert subscription["plan"] == "network"
    assert subscription["point_limit"] == 3

    renewal = store.issue_owner_code(plan="network_plus", point_limit=5)
    store.renew_subscription(7, renewal, 90)
    subscription = store.dashboard(7)["subscription"]
    assert subscription["plan"] == "network_plus"
    assert subscription["point_limit"] == 5


def test_notification_claim_is_idempotent(tmp_path):
    store = PlatformStore(str(tmp_path / "platform.sqlite3"))
    code = store.issue_owner_code()
    store.activate_owner(7, "owner", code)
    assert store.claim_notification(1, "owner", 7, "2026-09-01") is True
    assert store.claim_notification(1, "owner", 7, "2026-09-01") is False
    store.release_notification(1, "owner", 7, "2026-09-01")
    assert store.claim_notification(1, "owner", 7, "2026-09-01") is True


def test_rate_limit_is_atomic_and_resets_after_window(tmp_path):
    store = PlatformStore(str(tmp_path / "platform.sqlite3"))

    assert store.allow_rate_limit("login|127.0.0.1|owner", 2, 60, now=100) is True
    assert store.allow_rate_limit("login|127.0.0.1|owner", 2, 60, now=120) is True
    assert store.allow_rate_limit("login|127.0.0.1|owner", 2, 60, now=130) is False
    assert store.allow_rate_limit("login|127.0.0.1|owner", 2, 60, now=160) is True

    with store._connect() as db:
        row = db.execute("SELECT bucket_key, attempts FROM platform_rate_limits").fetchone()
    assert len(row[0]) == 64
    assert row[1] == 1


def test_platform_schema_migrations_are_versioned_and_idempotent(tmp_path):
    store = PlatformStore(str(tmp_path / "platform.sqlite3"))
    store.init_schema()
    store.init_schema()

    with sqlite3.connect(store.path) as db:
        versions = [row[0] for row in db.execute(
            "SELECT version FROM platform_schema_migrations ORDER BY version"
        )]

    assert versions == [1, 2]


def test_platform_schema_migrates_legacy_columns(tmp_path):
    path = tmp_path / "legacy-platform.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE platform_organizations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_web_user_id INTEGER NOT NULL UNIQUE,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE platform_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                address TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL
            );
            CREATE TABLE platform_owner_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code_hash TEXT NOT NULL UNIQUE,
                expires_at TEXT,
                used_at TEXT,
                used_by_user_id INTEGER,
                created_at TEXT NOT NULL
            );
            """
        )

    PlatformStore(str(path)).init_schema()

    with sqlite3.connect(path) as db:
        organization_columns = {
            row[1] for row in db.execute("PRAGMA table_info(platform_organizations)")
        }
        point_columns = {row[1] for row in db.execute("PRAGMA table_info(platform_points)")}
        code_columns = {row[1] for row in db.execute("PRAGMA table_info(platform_owner_codes)")}

    assert "staff_registration_enabled" in organization_columns
    assert {"telegram_chat_id", "work_time_start", "work_time_end"} <= point_columns
    assert {"code_plan", "code_point_limit"} <= code_columns


def test_rates_support_shared_personal_and_hidden_visibility(tmp_path):
    store = PlatformStore(str(tmp_path / "platform.sqlite3"))
    owner_code = store.issue_owner_code()
    store.activate_owner(7, "owner", owner_code)
    point_id = store.create_point(7, "ПВЗ", "Адрес")
    manager_invite = store.create_invite(7, "manager", "manager")
    employee_invite = store.create_invite(7, "employee", "employee")
    store.accept_invite(8, manager_invite)
    store.accept_invite(9, employee_invite)

    store.set_point_rate(7, point_id, money_to_minor("300"))
    store.set_member_rate(7, 8, point_id, money_to_minor("350"), "personal")
    store.set_member_rate(7, 9, point_id, money_to_minor("400"), "hidden")

    owner_rates = store.dashboard(7)["rates"]
    manager_rates = store.dashboard(8)["rates"]
    employee_rates = store.dashboard(9)["rates"]
    assert {(rate["scope"], rate["member_web_user_id"]) for rate in owner_rates} == {
        ("point", None), ("member", 8), ("member", 9)
    }
    assert {rate["amount_minor"] for rate in manager_rates} == {30_000, 35_000}
    assert {rate["amount_minor"] for rate in employee_rates} == {30_000, 40_000}
    assert all(rate["member_web_user_id"] != 9 for rate in manager_rates)
    assert all(rate["member_web_user_id"] != 8 for rate in employee_rates)
    with pytest.raises(PermissionError):
        store.set_point_rate(8, point_id, money_to_minor("500"))


def test_point_schedule_is_validated_and_persisted(tmp_path):
    store = PlatformStore(str(tmp_path / "platform.sqlite3"))
    code = store.issue_owner_code()
    store.activate_owner(7, "owner", code)
    point_id = store.create_point(7, "ПВЗ", "Адрес")
    store.set_point_schedule(7, point_id, "08:30", "17:30")
    runtime = store.dashboard(7)["points"][0]
    assert runtime["work_time_start"] == "08:30"
    assert runtime["work_time_end"] == "17:30"
    with pytest.raises(ValueError):
        store.set_point_schedule(7, point_id, "09:00", "09:00")


def test_subscription_moves_to_grace_and_blocks_mutations_after_expiry(tmp_path):
    store = PlatformStore(str(tmp_path / "platform.sqlite3"))
    code = store.issue_owner_code()
    store.activate_owner(7, "owner", code)
    past_end = utc_now() - timedelta(days=1)
    with store._connect() as db:
        db.execute(
            "UPDATE platform_subscriptions SET current_period_end = ?, grace_until = NULL WHERE organization_id = 1",
            (past_end.isoformat(),),
        )
    assert store.dashboard(7)["subscription"]["status"] == "grace"
    with store._connect() as db:
        db.execute(
            "UPDATE platform_subscriptions SET grace_until = ? WHERE organization_id = 1",
            ((utc_now() - timedelta(days=1)).isoformat(),),
        )
    assert store.dashboard(7)["subscription"]["status"] == "expired"
    with pytest.raises(ValueError, match="истекла"):
        store.create_point(7, "ПВЗ", "Адрес")


def test_workspace_activation_route_uses_owner_code(tmp_path, monkeypatch):
    import web_app

    legacy_db = Database(str(tmp_path / "legacy.sqlite3"))
    platform = PlatformStore(str(tmp_path / "platform.sqlite3"))
    monkeypatch.setattr(web_app, "db", legacy_db)
    monkeypatch.setattr(web_app, "platform_store", platform)

    async def prepare_session():
        await legacy_db.init_db()
        user_id = await legacy_db.create_web_user("route-owner", generate_password_hash("secret"))
        session_value = "session-token"
        await legacy_db.create_web_session(
            user_id,
            session_value,
            datetime.now() + timedelta(days=1),
        )
        return session_value

    token = asyncio.run(prepare_session())
    code = platform.issue_owner_code()
    client = web_app.app.test_client()
    client.set_cookie("session_token", token)
    page = client.get("/workspace")
    assert page.status_code == 200
    assert "Активируйте рабочее пространство" in page.text

    with client.session_transaction() as flask_session:
        csrf = flask_session["workspace_csrf_token"]
    response = client.post("/workspace/activate", data={"csrf_token": csrf, "code": code})

    assert response.status_code == 302
    assert client.get("/workspace").status_code == 200
    assert "Сеть route-owner" in client.get("/workspace").text

    renewal_payload = {"csrf_token": csrf, "code": "PVZ-invalid", "days": "30"}
    renewal_responses = [client.post("/workspace/renew", data=renewal_payload) for _ in range(9)]
    assert all(response.status_code == 302 for response in renewal_responses[:8])
    assert renewal_responses[-1].status_code == 429


def test_login_route_returns_429_after_repeated_attempts(tmp_path, monkeypatch):
    import web_app

    legacy_db = Database(str(tmp_path / "legacy.sqlite3"))
    platform = PlatformStore(str(tmp_path / "platform.sqlite3"))
    monkeypatch.setattr(web_app, "db", legacy_db)
    monkeypatch.setattr(web_app, "platform_store", platform)
    web_app.app.config.update(TESTING=True)
    asyncio.run(legacy_db.init_db())

    client = web_app.app.test_client()
    client.get("/login")
    with client.session_transaction() as flask_session:
        csrf = flask_session["auth_csrf_token"]

    payload = {"auth_csrf_token": csrf, "username": "rate-user", "password": "wrong"}
    responses = [client.post("/login", data=payload) for _ in range(9)]
    assert all(response.status_code == 200 for response in responses[:8])
    assert responses[-1].status_code == 429

    different_user = {**payload, "username": "another-user"}
    assert client.post("/login", data=different_user).status_code == 429
