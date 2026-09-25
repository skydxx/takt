import asyncio
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

from database import Database
from platform_store import PlatformStore


def test_admin_can_review_and_update_sales_lead(tmp_path, monkeypatch):
    import admin_app
    import admin_routes

    database = Database(str(tmp_path / 'legacy.sqlite3'))
    platform = PlatformStore(str(tmp_path / 'platform.sqlite3'))
    monkeypatch.setattr(admin_routes, 'db', database)
    monkeypatch.setattr(admin_routes, 'platform_store', platform)
    admin_app.app.config.update(TESTING=True)

    async def prepare():
        await database.init_db()
        admin_id = await database.create_web_user(
            'sales-admin', generate_password_hash('secret'), role='admin'
        )
        session_value = 'admin-session'
        await database.create_web_session(admin_id, session_value, datetime.now() + timedelta(days=1))
        lead_id = await database.create_lead_request(
            'Владелец', '@owner', '2-4', 'Нужна демонстрация'
        )
        return session_value, lead_id

    token, lead_id = asyncio.run(prepare())
    client = admin_app.app.test_client()
    client.set_cookie('admin_session_token', token)
    page = client.get('/admin/leads')
    assert page.status_code == 200
    assert '@owner' in page.text

    with client.session_transaction() as flask_session:
        csrf = flask_session['auth_csrf_token']
    response = client.post(
        f'/admin/leads/{lead_id}',
        data={'auth_csrf_token': csrf, 'status': 'contacted'},
    )
    assert response.status_code == 302
    leads = asyncio.run(database.get_lead_requests())
    assert leads[0]['status'] == 'contacted'

    response = client.post(
        '/admin/owner-code',
        data={
            'auth_csrf_token': csrf,
            'expires_days': '90',
            'plan': 'network',
            'point_limit': '5',
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert 'Код подключения создан: PVZ-' in response.text
    with platform._connect() as db:
        row = db.execute(
            'SELECT code_plan, code_point_limit FROM platform_owner_codes'
        ).fetchone()
    assert row['code_plan'] == 'network'
    assert row['code_point_limit'] == 5
