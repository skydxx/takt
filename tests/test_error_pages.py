"""Custom 404/500 error pages, per app, per core.register_error_handlers.

Covers the specific route-separation guarantees this matters for:
  - an unknown path on the public app renders the public 404 (same one
    /admin, /owner, /backups get there — see test_admin_host_isolation.py)
  - an unknown path on the admin app renders the admin 404
  - an unhandled exception in production-like config (TESTING off, so Flask
    does not propagate/re-raise into the test client) renders the generic,
    static, safe 500 page on both apps — never a traceback, file path, or
    exception message.
"""

import asyncio

import core
from database import Database
from platform_store import PlatformStore


def _make_public_client(tmp_path, monkeypatch, *, testing=True):
    import web_app

    database = Database(str(tmp_path / 'legacy.sqlite3'))
    platform = PlatformStore(str(tmp_path / 'platform.sqlite3'))
    monkeypatch.setattr(web_app, 'db', database)
    monkeypatch.setattr(web_app, 'platform_store', platform)
    web_app.app.config.update(TESTING=testing)
    asyncio.run(database.init_db())
    client = web_app.app.test_client()
    return web_app, database, client


def _make_admin_client(tmp_path, monkeypatch, *, testing=True):
    import admin_app
    import admin_routes

    database = Database(str(tmp_path / 'legacy.sqlite3'))
    platform = PlatformStore(str(tmp_path / 'platform.sqlite3'))
    monkeypatch.setattr(admin_routes, 'db', database)
    monkeypatch.setattr(admin_routes, 'platform_store', platform)
    admin_app.app.config.update(TESTING=testing)
    asyncio.run(database.init_db())
    client = admin_app.app.test_client()
    return admin_routes, database, client


def test_public_app_unknown_path_renders_public_404(tmp_path, monkeypatch):
    web_app, database, client = _make_public_client(tmp_path, monkeypatch)

    response = client.get('/this-route-does-not-exist')

    assert response.status_code == 404
    title, message = core.ERROR_COPY['ru'][404]
    assert title in response.text
    assert message in response.text
    assert 'Такт' in response.text
    # Self-contained page: no reference to admin-only sidebar/nav state.
    assert 'sidebar' not in response.text.lower()


def test_admin_app_unknown_path_renders_admin_404(tmp_path, monkeypatch):
    admin_routes, database, client = _make_admin_client(tmp_path, monkeypatch)

    response = client.get('/this-route-does-not-exist')

    assert response.status_code == 404
    title, message = core.ERROR_COPY['ru'][404]
    assert title in response.text
    assert message in response.text


def test_public_app_500_is_generic_and_safe_in_production_mode(tmp_path, monkeypatch):
    # TESTING off so Flask actually invokes our error handlers instead of
    # propagating the exception into the test client, matching real
    # production behavior.
    web_app, database, client = _make_public_client(tmp_path, monkeypatch, testing=False)

    class ExplodingDB:
        async def get_user_by_session(self, token):
            raise RuntimeError('super secret internal detail, must never leak')

    monkeypatch.setattr(web_app, 'db', ExplodingDB())
    client.set_cookie('session_token', 'whatever-token')

    response = client.get('/dashboard')

    assert response.status_code == 500
    title, message = core.ERROR_COPY['ru'][500]
    assert title in response.text
    assert message in response.text
    assert 'super secret internal detail' not in response.text
    assert 'RuntimeError' not in response.text
    assert 'Traceback' not in response.text
    assert 'web_app.py' not in response.text


def test_admin_app_500_is_generic_and_safe_in_production_mode(tmp_path, monkeypatch):
    admin_routes, database, client = _make_admin_client(tmp_path, monkeypatch, testing=False)

    class ExplodingDB:
        async def get_user_by_session(self, token):
            raise RuntimeError('super secret internal detail, must never leak')

    monkeypatch.setattr(admin_routes, 'db', ExplodingDB())
    client.set_cookie('admin_session_token', 'whatever-token')

    response = client.get('/admin')

    assert response.status_code == 500
    title, message = core.ERROR_COPY['ru'][500]
    assert title in response.text
    assert message in response.text
    assert 'super secret internal detail' not in response.text
    assert 'RuntimeError' not in response.text
    assert 'Traceback' not in response.text
    assert 'admin_routes.py' not in response.text
