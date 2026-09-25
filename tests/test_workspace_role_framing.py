import asyncio
from datetime import datetime, timedelta

import aiosqlite
from werkzeug.security import generate_password_hash

from database import Database
from platform_store import PlatformStore


def _setup(tmp_path, monkeypatch):
    import web_app

    legacy = Database(str(tmp_path / "legacy.sqlite3"))
    platform = PlatformStore(str(tmp_path / "platform.sqlite3"))
    monkeypatch.setattr(web_app, "db", legacy)
    monkeypatch.setattr(web_app, "platform_store", platform)
    web_app.app.config.update(TESTING=True)
    asyncio.run(legacy.init_db())
    return web_app, legacy, platform


def _create_session_user(legacy, username):
    async def _create():
        user_id = await legacy.create_web_user(username, generate_password_hash("secret-password-123"))
        token = f"{username}-session"
        await legacy.create_web_session(user_id, token, datetime.now() + timedelta(days=1))
        return user_id, token

    return asyncio.run(_create())


def test_workspace_shows_role_specific_framing_for_owner_manager_employee(tmp_path, monkeypatch):
    web_app, legacy, platform = _setup(tmp_path, monkeypatch)

    owner_id, owner_token = _create_session_user(legacy, "role-owner")
    code = platform.issue_owner_code(point_limit=2)
    platform.activate_owner(owner_id, "role-owner", code)
    point_id = platform.create_point(owner_id, "ПВЗ на Ленина", "Ленина, 14")
    private_point_id = platform.create_point(owner_id, "ПВЗ на Мира", "Мира, 8")

    manager_id, manager_token = _create_session_user(legacy, "role-manager")
    manager_invite = platform.create_invite(owner_id, "@manager", "manager")
    platform.accept_invite(manager_id, manager_invite)
    platform.set_point_access(owner_id, manager_id, point_id, True)
    platform.set_point_access(owner_id, manager_id, private_point_id, False)

    employee_id, employee_token = _create_session_user(legacy, "role-employee")
    employee_invite = platform.create_invite(owner_id, "@employee", "employee")
    platform.accept_invite(employee_id, employee_invite)
    platform.set_point_access(owner_id, employee_id, point_id, True)
    platform.set_point_access(owner_id, employee_id, private_point_id, False)

    client = web_app.app.test_client()

    # Owner: sees the full role badge and owner-only structure controls.
    client.set_cookie("session_token", owner_token)
    owner_page = client.get("/workspace")
    assert owner_page.status_code == 200
    assert "role-badge role-badge-owner" in owner_page.text
    assert "Полный доступ ко всей сети" in owner_page.text
    assert "Добавить ПВЗ" in owner_page.text
    assert "Сохранить сценарии" in owner_page.text
    assert "ПВЗ на Мира" in owner_page.text
    assert "employee-intro" not in owner_page.text

    # Manager: sees the assigned point and finance/payroll context, but no
    # owner-only structure controls (point creation, invites, automation).
    client.set_cookie("session_token", manager_token)
    manager_page = client.get("/workspace")
    assert manager_page.status_code == 200
    assert "role-badge role-badge-manager" in manager_page.text
    assert "Доступ к назначенным точкам" in manager_page.text
    assert "ПВЗ на Ленина" in manager_page.text  # assigned point is visible
    assert "ПВЗ на Мира" not in manager_page.text
    assert "Добавить ПВЗ" not in manager_page.text
    assert "Приглашать новых участников и управлять их доступом может только владелец." in manager_page.text
    assert "Изменять команды и уведомления может только владелец." in manager_page.text
    assert "employee-intro" not in manager_page.text

    # Employee: compact shift-first intro, only permitted data, and an
    # explicit explanation for why payroll/finance/audit are hidden.
    client.set_cookie("session_token", employee_token)
    employee_page = client.get("/workspace")
    assert employee_page.status_code == 200
    assert "role-badge role-badge-employee" in employee_page.text
    assert "Смена — через Telegram-чат точки" in employee_page.text
    assert "ПВЗ на Ленина" in employee_page.text  # assigned point still visible
    assert "ПВЗ на Мира" not in employee_page.text
    assert "Добавить ПВЗ" not in employee_page.text
    assert "Сводка зарплаты по точке видна владельцу и управляющему." in employee_page.text
    assert "Журнал действий видят владелец и управляющий." in employee_page.text
    # No finance panel at all for employees (can_manage_finance is False).
    assert "Управленческий учёт" not in employee_page.text


def test_workspace_network_overview_uses_real_shift_signal_and_audit_labels(tmp_path, monkeypatch):
    web_app, legacy, platform = _setup(tmp_path, monkeypatch)
    owner_id, _ = _create_session_user(legacy, "audit-owner")
    platform.activate_owner(owner_id, "audit-owner", platform.issue_owner_code())
    point_id = platform.create_point(owner_id, "ПВЗ с чатом", "Ленина, 14")
    platform.set_point_schedule(owner_id, point_id, "00:00", "23:59")
    platform.connect_chat(owner_id, point_id, -100987, "Рабочий чат")

    async def seed_open_shift():
        await legacy.add_or_update_worker(77, "worker", "Сотрудник")
        await legacy.add_chat(-100987, "Рабочий чат", "work")
        async with aiosqlite.connect(legacy.db_path) as connection:
            now = datetime.now()
            await connection.execute(
                """
                INSERT INTO shifts
                    (user_id, chat_id, shift_type, start_time, actual_start_time, is_active)
                VALUES (?, ?, 'regular', ?, ?, 1)
                """,
                (77, -100987, now, now),
            )
            await connection.commit()

    asyncio.run(seed_open_shift())
    dashboard = platform.dashboard(owner_id)
    overview = web_app.workspace_network_overview(dashboard)
    assert overview["counts"] == {"total": 1, "operating": 1, "attention": 0}
    assert overview["points"][0]["state"] == "open"

    feed = web_app.workspace_audit_feed(dashboard)
    assert feed[0]["action"] == "chat_connected"
    assert feed[0]["action_label"] == "Подключён Telegram-чат"
    assert feed[0]["entity_label"] == "ПВЗ"
    assert feed[0]["actor_label"] == "audit-owner"
