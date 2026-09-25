"""Owner/network workspace overview: per-point operational state (connection
+ live shift + schedule), aggregate counts, and the enriched audit journal.

State is derived only from data that genuinely exists (telegram_chat_id, the
legacy shifts table, the point's own work window) — these tests pin down
that honesty: a point with no chat connected must say so, not fake a
status, and "attention" must only fire when the backend can actually see a
mismatch (scheduled hours, no open shift).
"""

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


def _csrf(client, key="workspace_csrf_token"):
    # Render /workspace first so the route's own csrf_token=workspace_csrf_token()
    # call primes session[key] before we read it back.
    client.get("/workspace")
    with client.session_transaction() as session:
        return session[key]


def _connect_chat(client, point_id, chat_id, start_time="00:01", end_time="00:00"):
    """Connect a chat with a work window that covers virtually the whole
    day (23:59 minutes), so a real test run is "within hours" regardless of
    wall-clock time — deterministic without needing to freeze time."""
    return client.post(
        "/workspace/chat",
        data={
            "csrf_token": _csrf(client),
            "point_id": point_id,
            "chat_id": str(chat_id),
            "chat_title": f"Чат {chat_id}",
            "start_time": start_time,
            "end_time": end_time,
        },
    )


def _seed_active_shift(legacy, user_id, chat_id):
    """Raw-insert a currently-open shift, bypassing start_shift()'s
    wall-clock business rules (mirrors the pattern already used by
    tests/test_workspace_e2e.py's seed_closed_shift)."""
    async def seed():
        await legacy.add_or_update_worker(user_id, "worker", "Тестовый сотрудник")
        now = datetime.now()
        async with aiosqlite.connect(legacy.db_path) as connection:
            await connection.execute(
                """
                INSERT INTO shifts
                    (user_id, chat_id, shift_type, start_time, actual_start_time, is_active)
                VALUES (?, ?, 'regular', ?, ?, 1)
                """,
                (user_id, chat_id, now.isoformat(sep=" "), now.isoformat(sep=" ")),
            )
            await connection.commit()

    asyncio.run(seed())


def _create_point(client, name, address="Тестовая, 1"):
    response = client.post(
        "/workspace/point",
        data={"csrf_token": _csrf(client), "name": name, "address": address},
    )
    assert response.status_code == 302
    return response


def test_owner_overview_reports_attention_and_not_connected_honestly(tmp_path, monkeypatch):
    web_app, legacy, platform = _setup(tmp_path, monkeypatch)
    owner_id, owner_token = _create_session_user(legacy, "overview-owner")
    code = platform.issue_owner_code(point_limit=5)
    platform.activate_owner(owner_id, "overview-owner", code)

    client = web_app.app.test_client()
    client.set_cookie("session_token", owner_token)

    _create_point(client, "Точка А")
    _create_point(client, "Точка Б")
    points = platform.dashboard(owner_id)["points"]
    point_a = next(p for p in points if p["name"] == "Точка А")
    assert any(p["name"] == "Точка Б" for p in points)

    # Point A: connected, near-24h window, no active shift -> attention.
    connect = _connect_chat(client, point_a["id"], -100111)
    assert connect.status_code == 302
    # Point B: never connected -> not-connected, also counted as attention.

    page = client.get("/workspace")
    assert page.status_code == 200
    text = page.text

    assert "<strong>2</strong> всего" in text
    assert "<strong>0</strong> сейчас работает" in text
    assert "<strong>2</strong> требуют внимания" in text

    assert "Точка А" in text
    assert "Сейчас рабочее время" in text  # attention detail for the connected point
    assert "Точка Б" in text
    assert "Telegram-чат не подключён" in text  # honest not-connected detail
    assert "state-not-connected" in text
    assert "state-attention" in text
    assert "state-open" not in text


def test_owner_overview_shows_open_state_for_a_real_active_shift(tmp_path, monkeypatch):
    web_app, legacy, platform = _setup(tmp_path, monkeypatch)
    owner_id, owner_token = _create_session_user(legacy, "shift-owner")
    code = platform.issue_owner_code()
    platform.activate_owner(owner_id, "shift-owner", code)

    client = web_app.app.test_client()
    client.set_cookie("session_token", owner_token)

    _create_point(client, "Точка В")
    point = platform.dashboard(owner_id)["points"][0]
    _connect_chat(client, point["id"], -100222)

    worker_id, _ = _create_session_user(legacy, "shift-worker")
    _seed_active_shift(legacy, worker_id, -100222)

    page = client.get("/workspace")
    assert page.status_code == 200
    text = page.text

    assert "1</strong> сейчас работает" in text
    assert "0</strong> требуют внимания" in text
    assert "Смена открыта" in text
    assert "state-open" in text
    assert "state-attention" not in text


def test_owner_overview_shows_closed_state_outside_work_hours(tmp_path, monkeypatch):
    web_app, legacy, platform = _setup(tmp_path, monkeypatch)
    owner_id, owner_token = _create_session_user(legacy, "closed-owner")
    code = platform.issue_owner_code()
    platform.activate_owner(owner_id, "closed-owner", code)

    client = web_app.app.test_client()
    client.set_cookie("session_token", owner_token)

    _create_point(client, "Точка Г")
    point = platform.dashboard(owner_id)["points"][0]
    # A one-minute window is "outside hours" for virtually the entire day.
    _connect_chat(client, point["id"], -100333, start_time="00:00", end_time="00:01")

    page = client.get("/workspace")
    assert page.status_code == 200
    text = page.text

    assert "0</strong> сейчас работает" in text
    assert "0</strong> требуют внимания" in text
    assert "Вне расписания" in text
    assert "state-closed" in text


def test_manager_overview_only_shows_assigned_point(tmp_path, monkeypatch):
    web_app, legacy, platform = _setup(tmp_path, monkeypatch)
    owner_id, owner_token = _create_session_user(legacy, "scope-owner")
    code = platform.issue_owner_code(point_limit=5)
    platform.activate_owner(owner_id, "scope-owner", code)

    client = web_app.app.test_client()
    client.set_cookie("session_token", owner_token)

    _create_point(client, "Доступная точка")
    _create_point(client, "Скрытая точка")
    points = platform.dashboard(owner_id)["points"]
    visible_point = next(p for p in points if p["name"] == "Доступная точка")
    hidden_point = next(p for p in points if p["name"] == "Скрытая точка")
    _connect_chat(client, visible_point["id"], -100444)
    _connect_chat(client, hidden_point["id"], -100555)

    manager_id, manager_token = _create_session_user(legacy, "scope-manager")
    invite = platform.create_invite(owner_id, "@scope-manager", "manager")
    platform.accept_invite(manager_id, invite)
    # accept_invite() auto-grants access to every point that exists at
    # invite time (onboarding convenience) — explicitly revoke the one the
    # owner does not want this manager to see, the way the real "Доступ
    # управляющих" form in the workspace UI does.
    platform.set_point_access(owner_id, manager_id, hidden_point["id"], False)

    manager_client = web_app.app.test_client()
    manager_client.set_cookie("session_token", manager_token)
    page = manager_client.get("/workspace")
    assert page.status_code == 200
    text = page.text

    assert "1</strong> всего" in text
    assert "Доступная точка" in text
    # The point the manager was never granted access to must never appear
    # anywhere on the page — not in the overview, not in its detail text.
    assert "Скрытая точка" not in text
    assert "-100555" not in text


def test_cross_organization_points_never_leak_into_each_others_overview(tmp_path, monkeypatch):
    web_app, legacy, platform = _setup(tmp_path, monkeypatch)

    owner_a_id, owner_a_token = _create_session_user(legacy, "org-a-owner")
    code_a = platform.issue_owner_code()
    platform.activate_owner(owner_a_id, "org-a-owner", code_a)

    owner_b_id, owner_b_token = _create_session_user(legacy, "org-b-owner")
    code_b = platform.issue_owner_code()
    platform.activate_owner(owner_b_id, "org-b-owner", code_b)

    client_a = web_app.app.test_client()
    client_a.set_cookie("session_token", owner_a_token)
    _create_point(client_a, "Сеть А точка")
    point_a = platform.dashboard(owner_a_id)["points"][0]
    _connect_chat(client_a, point_a["id"], -100666)

    client_b = web_app.app.test_client()
    client_b.set_cookie("session_token", owner_b_token)
    _create_point(client_b, "Сеть Б точка")
    point_b = platform.dashboard(owner_b_id)["points"][0]
    _connect_chat(client_b, point_b["id"], -100777)

    page_a = client_a.get("/workspace")
    assert "Сеть А точка" in page_a.text
    assert "Сеть Б точка" not in page_a.text
    assert "-100777" not in page_a.text

    page_b = client_b.get("/workspace")
    assert "Сеть Б точка" in page_b.text
    assert "Сеть А точка" not in page_b.text
    assert "-100666" not in page_b.text


def test_audit_journal_orders_newest_first_and_resolves_actor_and_labels(tmp_path, monkeypatch):
    web_app, legacy, platform = _setup(tmp_path, monkeypatch)
    owner_id, owner_token = _create_session_user(legacy, "audit-owner")
    code = platform.issue_owner_code()
    platform.activate_owner(owner_id, "audit-owner", code)

    client = web_app.app.test_client()
    client.set_cookie("session_token", owner_token)

    _create_point(client, "Первая точка")
    point = platform.dashboard(owner_id)["points"][0]
    _connect_chat(client, point["id"], -100888)

    page = client.get("/workspace")
    assert page.status_code == 200
    text = page.text

    # Human labels, not raw action/entity codes.
    assert "Добавлен ПВЗ" in text
    assert "Подключён Telegram-чат" in text
    assert "owner_activated" not in text
    assert "point_created" not in text

    # Actor resolved to a username, not a bare id.
    assert "audit-owner ·" in text

    # Newest-first ordering: "Подключён Telegram-чат" (last action taken)
    # must appear before "Добавлен ПВЗ" (first action taken) in the feed.
    chat_pos = text.index("Подключён Telegram-чат")
    created_pos = text.index("Добавлен ПВЗ")
    assert chat_pos < created_pos


def test_audit_feed_falls_back_to_raw_code_for_unknown_action(tmp_path, monkeypatch):
    """workspace_audit_feed() must never crash or hide an event just
    because platform_store started writing an action/entity code this
    module doesn't have a label for yet — it should render the raw code."""
    import web_app

    legacy = Database(str(tmp_path / "legacy.sqlite3"))
    asyncio.run(legacy.init_db())
    user_id = asyncio.run(legacy.create_web_user("label-fallback", generate_password_hash("secret-password-123")))

    workspace = {
        "audit": [
            {
                "action": "totally_new_action",
                "entity_type": "mystery_entity",
                "entity_id": 1,
                "actor_user_id": user_id,
                "metadata": {},
                "created_at": "2026-01-01T00:00:00",
            }
        ]
    }
    monkeypatch.setattr(web_app, "db", legacy)
    feed = web_app.workspace_audit_feed(workspace)
    assert len(feed) == 1
    assert feed[0]["action_label"] == "totally_new_action"
    assert feed[0]["entity_label"] == "mystery_entity"
    assert feed[0]["actor_label"] == "label-fallback"


def test_network_overview_point_state_helper_covers_all_states(tmp_path):
    """Direct unit coverage of _point_operational_state for the four
    states, independent of Flask/HTTP plumbing."""
    import web_app

    now = datetime(2026, 6, 15, 12, 0, tzinfo=web_app.MSK)

    not_connected = web_app._point_operational_state(
        {"id": 1, "name": "P1", "telegram_chat_id": None, "work_time_start": "09:00", "work_time_end": "18:00"},
        {}, now,
    )
    assert not_connected["state"] == "not-connected"

    attention = web_app._point_operational_state(
        {"id": 2, "name": "P2", "telegram_chat_id": -1, "work_time_start": "09:00", "work_time_end": "18:00"},
        {}, now,
    )
    assert attention["state"] == "attention"

    closed = web_app._point_operational_state(
        {"id": 3, "name": "P3", "telegram_chat_id": -1, "work_time_start": "09:00", "work_time_end": "18:00"},
        {}, now.replace(hour=22),
    )
    assert closed["state"] == "closed"

    open_state = web_app._point_operational_state(
        {"id": 4, "name": "P4", "telegram_chat_id": -1, "work_time_start": "09:00", "work_time_end": "18:00"},
        {-1: {"chat_id": -1, "start_time": "2026-06-15 10:00:00", "actual_start_time": "2026-06-15 10:05:00"}},
        now,
    )
    assert open_state["state"] == "open"
    assert "10:05" in open_state["detail"]
