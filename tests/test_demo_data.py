import asyncio
import sqlite3

from werkzeug.security import check_password_hash

from database import Database
from platform_store import PlatformStore
from seed_demo_data import DEMO_ACCOUNTS, DEMO_CHAT_IDS, seed_demo_data


def test_demo_seed_creates_role_specific_workspaces_and_is_idempotent(tmp_path):
    legacy_path = str(tmp_path / "legacy.sqlite3")
    platform_path = str(tmp_path / "platform.sqlite3")
    password = "Demo-PVZ-2026!"  # noqa: S105 -- disposable local fixture

    first = asyncio.run(seed_demo_data(legacy_path, platform_path, password))
    second = asyncio.run(seed_demo_data(legacy_path, platform_path, password))

    assert first["organization_id"] == second["organization_id"]
    assert first["point_ids"] == second["point_ids"]

    legacy = Database(legacy_path)
    store = PlatformStore(platform_path)
    users = {
        role: asyncio.run(legacy.get_web_user(username))
        for role, (username, _telegram_id) in DEMO_ACCOUNTS.items()
    }
    assert all(check_password_hash(user["password_hash"], password) for user in users.values())

    owner = store.dashboard(users["owner"]["id"])
    manager = store.dashboard(users["manager"]["id"])
    employee = store.dashboard(users["employee"]["id"])
    assert owner["viewer_role"] == "owner"
    assert manager["viewer_role"] == "manager"
    assert employee["viewer_role"] == "employee"
    assert len(owner["points"]) == 3
    assert len(manager["points"]) == 2
    assert len(employee["points"]) == 1
    assert owner["can_manage_structure"] is True
    assert manager["can_manage_structure"] is False
    assert employee["can_manage_structure"] is False
    assert len(owner["finance"]) == 7

    with sqlite3.connect(legacy_path) as connection:
        shift_count = connection.execute(
            "SELECT COUNT(*) FROM shifts WHERE chat_id IN (?, ?, ?)", DEMO_CHAT_IDS
        ).fetchone()[0]
    assert shift_count == 10
