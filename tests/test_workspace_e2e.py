import asyncio
from datetime import datetime

import aiosqlite

from database import Database
from platform_store import PlatformStore


def _csrf(client, key):
    with client.session_transaction() as session:
        return session[key]


def test_owner_onboarding_to_operational_workspace(tmp_path, monkeypatch):
    import web_app

    legacy = Database(str(tmp_path / "legacy.sqlite3"))
    platform = PlatformStore(str(tmp_path / "platform.sqlite3"))
    monkeypatch.setattr(web_app, "db", legacy)
    monkeypatch.setattr(web_app, "platform_store", platform)
    web_app.app.config.update(TESTING=True)
    asyncio.run(legacy.init_db())

    code = platform.issue_owner_code()
    client = web_app.app.test_client()

    client.get("/register")
    response = client.post(
        "/register",
        data={
            "auth_csrf_token": _csrf(client, "auth_csrf_token"),
            "username": "e2e-owner",
            "owner_code": code,
            "password": "strong-password-123",
            "confirm_password": "strong-password-123",
            "answer1": "school",
            "answer2": "mother",
            "answer3": "pet",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")

    client.get("/login")
    response = client.post(
        "/login",
        data={
            "auth_csrf_token": _csrf(client, "auth_csrf_token"),
            "username": "e2e-owner",
            "password": "strong-password-123",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/workspace")

    workspace = client.get("/workspace")
    assert workspace.status_code == 200
    assert "Сеть e2e-owner" in workspace.text
    csrf = _csrf(client, "workspace_csrf_token")

    response = client.post(
        "/workspace/point",
        data={"csrf_token": csrf, "name": "ПВЗ на Ленина", "address": "Ленина, 14"},
    )
    assert response.status_code == 302
    point_id = platform.dashboard(1)["points"][0]["id"]

    response = client.post(
        "/workspace/chat",
        data={
            "csrf_token": csrf,
            "point_id": point_id,
            "chat_id": "-100123",
            "chat_title": "Рабочий чат",
            "start_time": "08:30",
            "end_time": "17:30",
        },
    )
    assert response.status_code == 302
    legacy_chat_settings = asyncio.run(legacy.get_chat_settings(-100123))
    assert legacy_chat_settings["work_time_start"] == "08:30"
    assert legacy_chat_settings["work_time_end"] == "17:30"

    async def seed_closed_shift():
        await legacy.add_or_update_worker(77, "worker", "Сотрудник")
        periods = await legacy.get_historical_periods(2)
        period_start, period_end = periods[0]
        previous_start, previous_end = periods[1]
        now = datetime.now()
        async with aiosqlite.connect(legacy.db_path) as connection:
            await connection.execute(
                """
                INSERT INTO shifts
                    (user_id, chat_id, shift_type, start_time, end_time,
                     actual_start_time, actual_end_time, earned_amount, hours_worked,
                     period_start, period_end, is_active)
                VALUES (?, ?, 'regular', ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (77, -100123, now, now, now, now, 999, 4.5, period_start, period_end),
            )
            await connection.execute(
                """
                INSERT INTO shifts
                    (user_id, chat_id, shift_type, start_time, end_time,
                     actual_start_time, actual_end_time, earned_amount, hours_worked,
                     period_start, period_end, is_active)
                VALUES (?, ?, 'regular', ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    77, -100123, previous_start, previous_start,
                    previous_start, previous_start, 777, 3.5,
                    previous_start, previous_end,
                ),
            )
            await connection.commit()

    asyncio.run(seed_closed_shift())

    response = client.post(
        "/workspace/invite",
        data={"csrf_token": csrf, "contact": "@manager", "role": "manager"},
    )
    assert response.status_code == 302

    response = client.post(
        "/workspace/finance",
        data={
            "csrf_token": csrf,
            "point_id": point_id,
            "kind": "income",
            "category": "Поощрение Ozon",
            "amount": "1000",
            "period_month": "2026-09",
            "note": "E2E",
        },
    )
    assert response.status_code == 302

    response = client.post(
        "/workspace/rate",
        data={
            "csrf_token": csrf,
            "point_id": point_id,
            "member_user_id": "",
            "amount": "250",
            "visibility": "personal",
        },
    )
    assert response.status_code == 302

    response = client.post(
        "/workspace/automation",
        data={
            "csrf_token": csrf,
            "preset": "custom",
            "start_shift": "открылись",
            "end_shift": "закрылись",
            "handover": "передали",
            "employee_payroll_days_before": "1",
            "employee_payroll_time": "09:30",
            "manager_payroll_days_before": "2",
            "manager_payroll_time": "10:00",
            "owner_payroll_days_before": "3",
            "owner_payroll_time": "11:00",
            "shift_alert_minutes": "12",
        },
    )
    assert response.status_code == 302
    dashboard = platform.dashboard(1)
    assert dashboard["points"][0]["telegram_chat_id"] == -100123
    assert dashboard["finance"][0]["category"] == "Поощрение Ozon"
    export = client.get("/workspace/finance.csv")
    assert export.status_code == 200
    assert "ПВЗ на Ленина" in export.text
    assert "Поощрение Ozon" in export.text
    assert export.headers["Content-Disposition"].startswith("attachment;")
    assert dashboard["automation"]["settings"]["notifications"]["employee_payroll_time"] == "09:30"
    payroll_page = client.get("/workspace")
    assert "История расчётов" in payroll_page.text
    assert "Аудит" in payroll_page.text
    assert "Сотрудник" in payroll_page.text
    assert "999.00 ₽" in payroll_page.text
    assert "Смены · 1" in payroll_page.text
    payroll_export = client.get("/workspace/payroll.csv")
    assert payroll_export.status_code == 200
    assert "Сотрудник" in payroll_export.text
    assert "999.00" in payroll_export.text
    assert payroll_export.headers["Content-Disposition"].startswith("attachment;")
    previous_period = asyncio.run(legacy.get_historical_periods(2))[1][0].date().isoformat()
    previous_payroll_page = client.get(f"/workspace?payroll_period={previous_period}")
    assert "777.00 ₽" in previous_payroll_page.text
    assert "999.00 ₽" not in previous_payroll_page.text
    previous_export = client.get(f"/workspace/payroll.csv?payroll_period={previous_period}")
    assert "777.00" in previous_export.text
    assert "999.00" not in previous_export.text
    assert dashboard["rates"][0]["amount_minor"] == 25_000
    assert asyncio.run(legacy.get_chat_settings(-100123))["salary_amount"] == 2250.0

    renewal_code = platform.issue_owner_code()
    response = client.post(
        "/workspace/renew",
        data={"csrf_token": csrf, "code": renewal_code, "days": "90"},
    )
    assert response.status_code == 302
    assert platform.dashboard(1)["subscription"]["status"] == "active"


def test_invitation_can_create_staff_account_without_owner_code(tmp_path, monkeypatch):
    import web_app

    legacy = Database(str(tmp_path / "legacy.sqlite3"))
    platform = PlatformStore(str(tmp_path / "platform.sqlite3"))
    monkeypatch.setattr(web_app, "db", legacy)
    monkeypatch.setattr(web_app, "platform_store", platform)
    web_app.app.config.update(TESTING=True)

    async def prepare_owner():
        await legacy.init_db()
        return await legacy.create_web_user("owner", "unused-hash")

    owner_id = asyncio.run(prepare_owner())
    owner_code = platform.issue_owner_code()
    platform.activate_owner(owner_id, "owner", owner_code)
    invite = platform.create_invite(owner_id, "@employee", "employee")

    client = web_app.app.test_client()
    page = client.get(f"/invite/{invite}")
    assert page.status_code == 200
    assert f"/register?invite={invite}" in page.text

    client.get(f"/register?invite={invite}")
    response = client.post(
        "/register",
        data={
            "auth_csrf_token": _csrf(client, "auth_csrf_token"),
            "invite_token": invite,
            "username": "employee",
            "password": "staff-password-123",
            "confirm_password": "staff-password-123",
            "answer1": "school",
            "answer2": "mother",
            "answer3": "pet",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")
    assert platform.dashboard(2)["members"][-1]["role"] == "employee"

    client.get("/login")
    response = client.post(
        "/login",
        data={
            "auth_csrf_token": _csrf(client, "auth_csrf_token"),
            "username": "employee",
            "password": "staff-password-123",
        },
    )
    assert response.status_code == 302
    assert client.get("/workspace/payroll.csv").status_code == 403


def test_registration_rejects_invalid_identity_policy(tmp_path, monkeypatch):
    import web_app

    legacy = Database(str(tmp_path / "legacy.sqlite3"))
    platform = PlatformStore(str(tmp_path / "platform.sqlite3"))
    monkeypatch.setattr(web_app, "db", legacy)
    monkeypatch.setattr(web_app, "platform_store", platform)
    web_app.app.config.update(TESTING=True)
    asyncio.run(legacy.init_db())

    code = platform.issue_owner_code()
    client = web_app.app.test_client()
    client.get("/register")

    response = client.post(
        "/register",
        data={
            "auth_csrf_token": _csrf(client, "auth_csrf_token"),
            "username": "bad user",
            "owner_code": code,
            "password": "strong-password-123",
            "confirm_password": "strong-password-123",
            "answer1": "school",
            "answer2": "mother",
            "answer3": "pet",
        },
    )
    assert response.status_code == 200
    assert "Логин должен содержать" in response.text
    assert asyncio.run(legacy.get_web_user("bad user")) is None

    response = client.post(
        "/register",
        data={
            "auth_csrf_token": _csrf(client, "auth_csrf_token"),
            "username": "valid-owner",
            "owner_code": code,
            "password": "too-short",
            "confirm_password": "too-short",
            "answer1": "school",
            "answer2": "mother",
            "answer3": "pet",
        },
    )
    assert response.status_code == 200
    assert "Пароль должен содержать" in response.text
    assert asyncio.run(legacy.get_web_user("valid-owner")) is None


def test_employee_cannot_receive_workspace_payroll_snapshot(monkeypatch):
    import web_app

    class UnexpectedDatabase:
        def get_current_period(self):
            raise AssertionError("employee payroll must be blocked before database access")

    monkeypatch.setattr(web_app, "db", UnexpectedDatabase())
    assert web_app.workspace_payroll_snapshot({"viewer_role": "employee", "points": []}) == []
