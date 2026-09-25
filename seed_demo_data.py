"""Create a local-only demo network with accounts for every workspace role.

The script deliberately refuses production and PostgreSQL targets.  It only
touches usernames, Telegram IDs and chat IDs reserved by this module so it can
be run repeatedly without duplicating demo shifts or finance entries.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from werkzeug.security import generate_password_hash

from config import (
    APP_ENV,
    DATABASE_PATH,
    PLATFORM_DATABASE_PATH,
    PLATFORM_DATABASE_URL,
)
from database import Database
from platform_store import PlatformStore, money_to_minor, utc_now
from vpn_config import SECURITY_QUESTIONS

DEMO_ACCOUNTS = {
    "owner": ("owner.demo", 9_090_001),
    "manager": ("manager.demo", 9_090_002),
    "employee": ("employee.demo", 9_090_003),
}
DEMO_CHAT_IDS = (-1_007_770_001, -1_007_770_002, -1_007_770_003)
DEMO_WORKERS = (
    (9_090_002, "manager_demo", "Анна · управляющая"),
    (9_090_003, "employee_demo", "Максим · сотрудник"),
    (9_090_004, "daria_demo", "Дарья Соколова"),
    (9_090_005, "ilya_demo", "Илья Волков"),
)


def _assert_local_target(platform_path: str) -> None:
    if APP_ENV == "production":
        raise RuntimeError("Демо-данные запрещено создавать в production")
    if PLATFORM_DATABASE_URL or platform_path.lower().startswith(("postgres://", "postgresql://")):
        raise RuntimeError("Демо-данные разрешены только в локальной SQLite-базе")


async def _ensure_web_user(
    legacy: Database,
    database_path: str,
    username: str,
    password: str,
    telegram_id: int,
) -> int:
    user = await legacy.get_web_user(username)
    password_hash = generate_password_hash(password)
    if user:
        user_id = int(user["id"])
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """UPDATE web_users
                   SET password_hash = ?, role = 'guest', telegram_id = ?, telegram_linked = 1
                   WHERE id = ?""",
                (password_hash, telegram_id, user_id),
            )
    else:
        user_id = await legacy.create_web_user(username, password_hash, role="guest")
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "UPDATE web_users SET telegram_id = ?, telegram_linked = 1 WHERE id = ?",
                (telegram_id, user_id),
            )

    with sqlite3.connect(database_path) as connection:
        has_questions = connection.execute(
            "SELECT 1 FROM security_questions WHERE web_user_id = ?", (user_id,)
        ).fetchone()
    if not has_questions:
        await legacy.save_security_questions(
            user_id,
            [(question, "demo") for question in SECURITY_QUESTIONS[:3]],
        )
    return user_id


def _organization_id(platform_path: str, owner_user_id: int) -> int | None:
    with sqlite3.connect(platform_path) as connection:
        row = connection.execute(
            "SELECT id FROM platform_organizations WHERE owner_web_user_id = ?",
            (owner_user_id,),
        ).fetchone()
    return int(row[0]) if row else None


def _member_role(platform_path: str, organization_id: int, user_id: int) -> str | None:
    with sqlite3.connect(platform_path) as connection:
        row = connection.execute(
            """SELECT role FROM platform_members
               WHERE organization_id = ? AND web_user_id = ?""",
            (organization_id, user_id),
        ).fetchone()
    return str(row[0]) if row else None


def _point_ids(platform_path: str, organization_id: int) -> dict[str, int]:
    with sqlite3.connect(platform_path) as connection:
        rows = connection.execute(
            "SELECT id, name FROM platform_points WHERE organization_id = ?",
            (organization_id,),
        ).fetchall()
    return {str(name): int(point_id) for point_id, name in rows}


def _ensure_finance(
    store: PlatformStore,
    platform_path: str,
    owner_user_id: int,
    point_id: int,
    kind: str,
    category: str,
    amount: str,
    period_month: str,
    note: str,
) -> None:
    with sqlite3.connect(platform_path) as connection:
        exists = connection.execute(
            """SELECT 1 FROM platform_finance_entries
               WHERE point_id = ? AND kind = ? AND category = ?
                 AND period_month = ? AND note = ?""",
            (point_id, kind, category, period_month, note),
        ).fetchone()
    if not exists:
        store.add_finance_entry(
            owner_user_id,
            point_id,
            kind,
            category,
            money_to_minor(amount),
            period_month,
            note,
        )


async def _sync_chat(
    legacy: Database,
    store: PlatformStore,
    owner_user_id: int,
    point_id: int,
    chat_id: int,
    title: str,
    work_time: str,
    hourly_rate: int,
) -> None:
    start_time, end_time = work_time.split("-")
    store.set_point_schedule(owner_user_id, point_id, start_time, end_time)
    store.connect_chat(owner_user_id, point_id, chat_id, title)
    chat_number = await legacy.add_chat(chat_id, title, "work")
    start = datetime.strptime(start_time, "%H:%M")
    end = datetime.strptime(end_time, "%H:%M")
    if end <= start:
        end += timedelta(days=1)
    daily_salary = hourly_rate * ((end - start).total_seconds() / 3600)
    await legacy.set_chat_settings(chat_number, chat_id, work_time, daily_salary)


async def _seed_shifts(legacy: Database, database_path: str) -> None:
    for user_id, username, full_name in DEMO_WORKERS:
        await legacy.add_or_update_worker(user_id, username, full_name)

    periods = await legacy.get_historical_periods(2)
    now = datetime.now().replace(microsecond=0)
    rates = {DEMO_CHAT_IDS[0]: 345.0, DEMO_CHAT_IDS[1]: 320.0, DEMO_CHAT_IDS[2]: 360.0}
    assignments = (
        (9_090_003, DEMO_CHAT_IDS[0], 8.0, 1),
        (9_090_003, DEMO_CHAT_IDS[0], 8.5, 3),
        (9_090_004, DEMO_CHAT_IDS[0], 9.0, 2),
        (9_090_004, DEMO_CHAT_IDS[1], 7.5, 4),
        (9_090_005, DEMO_CHAT_IDS[1], 8.0, 1),
        (9_090_005, DEMO_CHAT_IDS[2], 10.0, 3),
        (9_090_002, DEMO_CHAT_IDS[1], 5.0, 2),
    )
    previous_assignments = (
        (9_090_003, DEMO_CHAT_IDS[0], 8.0, 2),
        (9_090_004, DEMO_CHAT_IDS[0], 8.0, 4),
        (9_090_005, DEMO_CHAT_IDS[2], 9.0, 5),
    )

    with sqlite3.connect(database_path) as connection:
        placeholders_users = ",".join("?" for _ in DEMO_WORKERS)
        placeholders_chats = ",".join("?" for _ in DEMO_CHAT_IDS)
        connection.execute(
            f"DELETE FROM shifts WHERE user_id IN ({placeholders_users}) "  # noqa: S608 -- placeholders only
            f"AND chat_id IN ({placeholders_chats})",
            tuple(item[0] for item in DEMO_WORKERS) + DEMO_CHAT_IDS,
        )

        def insert_shift(
            user_id: int,
            chat_id: int,
            hours: float,
            day_offset: int,
            period: tuple[datetime, datetime],
        ) -> None:
            period_start, period_end = period
            base_day = max(period_start, now - timedelta(days=day_offset))
            if base_day > period_end:
                base_day = period_start + timedelta(days=min(day_offset, 5))
            shift_start = base_day.replace(hour=9, minute=0, second=0)
            shift_end = shift_start + timedelta(hours=hours)
            earned = round(hours * rates[chat_id], 2)
            connection.execute(
                """INSERT INTO shifts
                   (user_id, chat_id, shift_type, start_time, end_time,
                    actual_start_time, actual_end_time, earned_amount, hours_worked,
                    period_start, period_end, is_active)
                   VALUES (?, ?, 'regular', ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    user_id,
                    chat_id,
                    shift_start.isoformat(sep=" "),
                    shift_end.isoformat(sep=" "),
                    shift_start.isoformat(sep=" "),
                    shift_end.isoformat(sep=" "),
                    earned,
                    hours,
                    period_start.isoformat(sep=" "),
                    period_end.isoformat(sep=" "),
                ),
            )

        for assignment in assignments:
            insert_shift(*assignment, periods[0])
        for assignment in previous_assignments:
            insert_shift(*assignment, periods[1])


async def seed_demo_data(
    database_path: str = DATABASE_PATH,
    platform_path: str = PLATFORM_DATABASE_PATH,
    password: str = "",
) -> dict[str, object]:
    """Seed and return login details plus IDs for browser testing."""
    _assert_local_target(platform_path)
    if len(password) < 12:
        raise ValueError("Демо-пароль должен содержать минимум 12 символов")

    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    Path(platform_path).parent.mkdir(parents=True, exist_ok=True)
    legacy = Database(database_path)
    await legacy.init_db()
    store = PlatformStore(platform_path)
    store.init_schema()

    user_ids: dict[str, int] = {}
    for role, (username, telegram_id) in DEMO_ACCOUNTS.items():
        user_ids[role] = await _ensure_web_user(
            legacy, database_path, username, password, telegram_id
        )

    owner_id = user_ids["owner"]
    organization_id = _organization_id(platform_path, owner_id)
    if organization_id is None:
        owner_code = store.issue_owner_code(plan="finance", point_limit=5)
        store.activate_owner(owner_id, DEMO_ACCOUNTS["owner"][0], owner_code)
        organization_id = _organization_id(platform_path, owner_id)
    assert organization_id is not None

    with sqlite3.connect(platform_path) as connection:
        connection.execute(
            """UPDATE platform_organizations
               SET name = 'Северная сеть · демо', staff_registration_enabled = 1
               WHERE id = ?""",
            (organization_id,),
        )
        connection.execute(
            """UPDATE platform_subscriptions
               SET plan = 'finance', status = 'active', point_limit = 5,
                   current_period_end = ?, grace_until = NULL
               WHERE organization_id = ?""",
            ((utc_now() + timedelta(days=45)).isoformat(), organization_id),
        )

    for role in ("manager", "employee"):
        user_id = user_ids[role]
        if _member_role(platform_path, organization_id, user_id) != role:
            invite = store.create_invite(owner_id, f"@{DEMO_ACCOUNTS[role][0]}", role)
            store.accept_invite(user_id, invite)

    point_specs = (
        ("Ozon · Ленина 14", "Москва, ул. Ленина, 14", DEMO_CHAT_IDS[0], "ПВЗ · Ленина 14", "08:30-21:00", 320),
        ("WB · Мира 8", "Москва, пр-т Мира, 8", DEMO_CHAT_IDS[1], "ПВЗ · Мира 8", "09:00-22:00", 300),
        ("Яндекс · Кирова 21", "Москва, ул. Кирова, 21", DEMO_CHAT_IDS[2], "ПВЗ · Кирова 21", "10:00-21:00", 340),
    )
    points = _point_ids(platform_path, organization_id)
    for name, address, *_ in point_specs:
        if name not in points:
            store.create_point(owner_id, name, address)
    points = _point_ids(platform_path, organization_id)

    for name, _, chat_id, title, work_time, hourly_rate in point_specs:
        point_id = points[name]
        store.set_point_rate(owner_id, point_id, money_to_minor(str(hourly_rate)))
        await _sync_chat(
            legacy, store, owner_id, point_id, chat_id, title, work_time, hourly_rate
        )

    manager_id = user_ids["manager"]
    employee_id = user_ids["employee"]
    for index, (name, *_rest) in enumerate(point_specs):
        point_id = points[name]
        store.set_point_access(owner_id, manager_id, point_id, index < 2)
        store.set_point_access(owner_id, employee_id, point_id, index == 0)
    store.set_member_rate(
        owner_id, manager_id, points[point_specs[1][0]], money_to_minor("365"), "personal"
    )
    store.set_member_rate(
        owner_id, employee_id, points[point_specs[0][0]], money_to_minor("345"), "hidden"
    )

    period_month = datetime.now().strftime("%Y-%m")
    finance_rows = (
        (point_specs[0][0], "income", "Поощрение Ozon", "186500", "Начисление за месяц"),
        (point_specs[0][0], "expense", "Аренда", "58000", "Аренда помещения"),
        (point_specs[0][0], "expense", "ФОТ", "74250", "Сотрудники точки"),
        (point_specs[1][0], "income", "Поощрение WB", "164900", "Начисление за месяц"),
        (point_specs[1][0], "expense", "Аренда", "62000", "Аренда помещения"),
        (point_specs[2][0], "income", "Поощрение Яндекс", "131200", "Начисление за месяц"),
        (point_specs[2][0], "expense", "Аренда", "49000", "Аренда помещения"),
    )
    for point_name, kind, category, amount, note in finance_rows:
        _ensure_finance(
            store,
            platform_path,
            owner_id,
            points[point_name],
            kind,
            category,
            amount,
            period_month,
            note,
        )

    store.update_automation_settings(
        owner_id,
        "custom",
        {"start_shift": "открылись", "end_shift": "закрылись", "handover": "передали"},
        {
            "employee_payroll_days_before": 1,
            "manager_payroll_days_before": 2,
            "owner_payroll_days_before": 5,
            "employee_payroll_time": "11:00",
            "manager_payroll_time": "10:00",
            "owner_payroll_time": "09:30",
            "shift_alert_minutes": 12,
        },
    )
    await _seed_shifts(legacy, database_path)

    return {
        "password": password,
        "accounts": {
            role: {"username": username, "user_id": user_ids[role]}
            for role, (username, _telegram_id) in DEMO_ACCOUNTS.items()
        },
        "organization_id": organization_id,
        "point_ids": points,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Создать локальные демо-аккаунты")
    parser.add_argument("--password", required=True, help="Общий пароль, минимум 12 символов")
    args = parser.parse_args()
    result = asyncio.run(seed_demo_data(password=args.password))
    print("Демо-сеть готова:")
    for role, account in result["accounts"].items():
        print(f"  {role:8} {account['username']}")
    print(f"  password {result['password']}")


if __name__ == "__main__":
    main()
