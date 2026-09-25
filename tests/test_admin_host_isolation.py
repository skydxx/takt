"""Route separation between the public app (web_app) and the admin app
(admin_app/admin_routes), plus ADMIN_HOST exact-host isolation within the
admin app itself.

The public app never imports ADMIN_HOST and never registers /admin*,
/owner*, /backups* routes at all — a request to one of those paths on the
public app is a plain unknown-path 404, indistinguishable from any other
typo'd URL, never a redirect and never a hint that an admin surface exists.

ADMIN_HOST exact-host comparison is retained as defense-in-depth *inside*
the admin app/process: once a request has already reached that process (in
production, only reachable via a reverse proxy terminating ADMIN_HOST), an
unexpected Host header still gets redirected (HTTPS) or 404'd (plain HTTP)
rather than served.

Uses app config / monkeypatch to simulate hosts — no real DNS. Werkzeug's
test client scopes cookies by domain just like a real browser, so a cookie
set for one host is not sent on a request to a different host.
``_authenticated_get`` explicitly (re)scopes the session cookie to whichever
host a given request targets before sending it.
"""

import asyncio
from datetime import datetime, timedelta
from urllib.parse import urlparse

from werkzeug.security import generate_password_hash

from database import Database
from platform_store import PlatformStore


def _make_public_client(tmp_path, monkeypatch):
    import web_app

    database = Database(str(tmp_path / 'legacy.sqlite3'))
    platform = PlatformStore(str(tmp_path / 'platform.sqlite3'))
    monkeypatch.setattr(web_app, 'db', database)
    monkeypatch.setattr(web_app, 'platform_store', platform)
    web_app.app.config.update(TESTING=True)
    asyncio.run(database.init_db())
    client = web_app.app.test_client()
    return web_app, database, client


def _make_admin_client(tmp_path, monkeypatch):
    import admin_app
    import admin_routes

    database = Database(str(tmp_path / 'legacy.sqlite3'))
    platform = PlatformStore(str(tmp_path / 'platform.sqlite3'))
    monkeypatch.setattr(admin_routes, 'db', database)
    monkeypatch.setattr(admin_routes, 'platform_store', platform)
    admin_app.app.config.update(TESTING=True)
    asyncio.run(database.init_db())
    client = admin_app.app.test_client()
    return admin_routes, database, client


def _create_session(database, username, role):
    async def prepare():
        user_id = await database.create_web_user(
            username, generate_password_hash('secret-password'), role=role
        )
        token = f'{username}-session'
        await database.create_web_session(user_id, token, datetime.now() + timedelta(days=1))
        return user_id, token

    return asyncio.run(prepare())


def _authenticated_get(client, token, path, base_url, **kwargs):
    host = urlparse(base_url).hostname
    cookie_name = 'admin_session_token' if path.startswith(('/admin', '/owner', '/backups')) else 'session_token'
    client.set_cookie(cookie_name, token, domain=host)
    return client.get(path, base_url=base_url, **kwargs)


# --- Public app: /admin, /owner, /backups don't exist there at all ---


def test_public_app_admin_owner_backups_paths_are_plain_404(tmp_path, monkeypatch):
    web_app, database, client = _make_public_client(tmp_path, monkeypatch)
    _, token = _create_session(database, 'plain-admin', 'admin')

    for path in (
        '/admin', '/admin/leads', '/admin/users', '/admin/info',
        '/owner', '/owner/vpn-orders',
        '/backups', '/backups/create',
    ):
        response = _authenticated_get(client, token, path, 'http://localhost')
        assert response.status_code == 404, path
        assert 'Location' not in response.headers, path


def test_public_app_does_not_know_about_admin_host():
    import web_app

    assert not hasattr(web_app, 'ADMIN_HOST')


def test_public_app_does_not_accept_admin_session_cookie(tmp_path, monkeypatch):
    """The public process must not treat the admin container's cookie as a
    public login, even though both processes currently share the session table
    during the transitional DB split."""
    _web_app, _database, client = _make_public_client(tmp_path, monkeypatch)
    client.set_cookie('admin_session_token', 'valid-session-token')
    response = client.get('/workspace')
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/login')


def test_public_app_404_for_admin_path_matches_generic_unknown_path_404(tmp_path, monkeypatch):
    web_app, database, client = _make_public_client(tmp_path, monkeypatch)

    unknown = client.get('/this-route-does-not-exist')
    admin_path = client.get('/admin')

    assert unknown.status_code == admin_path.status_code == 404
    # Same rendered body as any other unknown path — no distinct "you found
    # the admin gate" response, and no redirect to login either.
    assert unknown.text == admin_path.text


def test_public_and_workspace_routes_still_work(tmp_path, monkeypatch):
    web_app, database, client = _make_public_client(tmp_path, monkeypatch)

    landing = client.get('/', base_url='https://public.example.test')
    assert landing.status_code == 200

    login_page = client.get('/login', base_url='https://public.example.test')
    assert login_page.status_code == 200

    _, token = _create_session(database, 'regular-user', 'user')
    dashboard = _authenticated_get(
        client, token, '/dashboard', 'https://public.example.test'
    )
    assert dashboard.status_code == 200


# --- Admin app: ADMIN_HOST exact-host gate (defense in depth) ---


def test_admin_host_disabled_by_default_keeps_local_dev_working(tmp_path, monkeypatch):
    admin_routes, database, client = _make_admin_client(tmp_path, monkeypatch)
    assert admin_routes.ADMIN_HOST == ''
    _, token = _create_session(database, 'plain-admin', 'admin')

    response = _authenticated_get(client, token, '/admin', 'http://localhost')
    assert response.status_code == 200


def test_admin_host_redirects_https_request_to_admin_host(tmp_path, monkeypatch):
    admin_routes, database, client = _make_admin_client(tmp_path, monkeypatch)
    monkeypatch.setattr(admin_routes, 'ADMIN_HOST', 'admin.example.test')
    _, token = _create_session(database, 'https-admin', 'admin')

    response = _authenticated_get(client, token, '/admin', 'https://wrong.example.test')
    assert response.status_code == 302
    # The redirect target always comes from server config, never from the
    # request's own Host header — this is what rules out an open redirect.
    assert response.headers['Location'] == 'https://admin.example.test/admin'


def test_admin_host_gates_unauthenticated_request_before_login_redirect(
    tmp_path, monkeypatch
):
    admin_routes, _, client = _make_admin_client(tmp_path, monkeypatch)
    monkeypatch.setattr(admin_routes, 'ADMIN_HOST', 'admin.example.test')

    response = client.get('/owner?tab=overview', base_url='https://wrong.example.test')

    assert response.status_code == 302
    assert response.headers['Location'] == (
        'https://admin.example.test/owner?tab=overview'
    )


def test_admin_host_returns_404_for_unauthenticated_plain_http_request(
    tmp_path, monkeypatch
):
    admin_routes, _, client = _make_admin_client(tmp_path, monkeypatch)
    monkeypatch.setattr(admin_routes, 'ADMIN_HOST', 'admin.example.test')

    response = client.get('/backups', base_url='http://wrong.example.test')

    assert response.status_code == 404


def test_admin_host_also_gates_admin_login_surface(tmp_path, monkeypatch):
    admin_routes, _, client = _make_admin_client(tmp_path, monkeypatch)
    monkeypatch.setattr(admin_routes, 'ADMIN_HOST', 'admin.example.test')

    response = client.get('/login', base_url='https://wrong.example.test')

    assert response.status_code == 302
    assert response.headers['Location'] == 'https://admin.example.test/login'


def test_admin_host_preserves_query_string_on_redirect(tmp_path, monkeypatch):
    admin_routes, database, client = _make_admin_client(tmp_path, monkeypatch)
    monkeypatch.setattr(admin_routes, 'ADMIN_HOST', 'admin.example.test')
    _, token = _create_session(database, 'query-admin', 'admin')

    response = _authenticated_get(
        client, token, '/admin/leads?status=new', 'https://wrong.example.test'
    )
    assert response.status_code == 302
    assert response.headers['Location'] == 'https://admin.example.test/admin/leads?status=new'


def test_admin_host_404s_plain_http_request_instead_of_redirecting(tmp_path, monkeypatch):
    admin_routes, database, client = _make_admin_client(tmp_path, monkeypatch)
    monkeypatch.setattr(admin_routes, 'ADMIN_HOST', 'admin.example.test')
    _, token = _create_session(database, 'http-admin', 'admin')

    # A plain HTTP request never gets redirected toward the admin host —
    # we cannot vouch for the transport, so it is rejected outright.
    response = _authenticated_get(client, token, '/admin', 'http://wrong.example.test')
    assert response.status_code == 404


def test_admin_host_accepts_the_configured_host(tmp_path, monkeypatch):
    admin_routes, database, client = _make_admin_client(tmp_path, monkeypatch)
    monkeypatch.setattr(admin_routes, 'ADMIN_HOST', 'admin.example.test')
    _, token = _create_session(database, 'ok-admin', 'admin')

    response = _authenticated_get(client, token, '/admin', 'https://admin.example.test')
    assert response.status_code == 200
    assert 'Настройки системы' in response.text


def test_admin_host_matching_is_case_insensitive_and_port_tolerant(tmp_path, monkeypatch):
    admin_routes, database, client = _make_admin_client(tmp_path, monkeypatch)
    monkeypatch.setattr(admin_routes, 'ADMIN_HOST', 'Admin.Example.Test')
    _, token = _create_session(database, 'case-admin', 'admin')

    response = _authenticated_get(
        client, token, '/admin', 'https://admin.example.test:8443'
    )
    assert response.status_code == 200


def test_admin_host_enforces_explicit_configured_port(tmp_path, monkeypatch):
    admin_routes, database, client = _make_admin_client(tmp_path, monkeypatch)
    monkeypatch.setattr(admin_routes, 'ADMIN_HOST', 'admin.example.test:8443')
    _, token = _create_session(database, 'port-admin', 'admin')

    wrong_port = _authenticated_get(
        client, token, '/admin', 'https://admin.example.test:9000'
    )
    assert wrong_port.status_code == 302
    assert wrong_port.headers['Location'] == 'https://admin.example.test:8443/admin'

    right_port = _authenticated_get(
        client, token, '/admin', 'https://admin.example.test:8443'
    )
    assert right_port.status_code == 200


def test_admin_host_rejects_lookalike_hosts(tmp_path, monkeypatch):
    admin_routes, database, client = _make_admin_client(tmp_path, monkeypatch)
    monkeypatch.setattr(admin_routes, 'ADMIN_HOST', 'admin.example.test')
    _, token = _create_session(database, 'lookalike-admin', 'admin')

    # Neither a different subdomain nor a suffix/prefix trick should match
    # via substring comparison — only an exact hostname counts.
    for lookalike in (
        'notadmin.example.test',
        'admin.example.test.evil.test',
        'evil-admin.example.test',
        'xadmin.example.test',
    ):
        response = _authenticated_get(client, token, '/admin', f'https://{lookalike}')
        assert response.status_code == 302, lookalike
        assert response.headers['Location'] == 'https://admin.example.test/admin', lookalike


def test_admin_host_still_enforces_role_on_the_correct_host(tmp_path, monkeypatch):
    admin_routes, database, client = _make_admin_client(tmp_path, monkeypatch)
    monkeypatch.setattr(admin_routes, 'ADMIN_HOST', 'admin.example.test')
    _, token = _create_session(database, 'plain-user', 'user')

    response = _authenticated_get(
        client, token, '/admin', 'https://admin.example.test', follow_redirects=True
    )
    assert response.status_code == 200
    assert 'Доступ запрещен' in response.text
    assert 'Настройки системы' not in response.text


def test_admin_host_covers_manual_role_backup_view_route(tmp_path, monkeypatch):
    admin_routes, database, client = _make_admin_client(tmp_path, monkeypatch)
    monkeypatch.setattr(admin_routes, 'ADMIN_HOST', 'admin.example.test')
    _, token = _create_session(database, 'view-admin', 'super_admin')

    class FakeBackupManager:
        async def get_backup_chats(self, backup_date):
            return []

    admin_routes.set_backup_manager(FakeBackupManager())

    # view_backup() checks role manually (no admin_required/super_admin_required
    # decorator) — it must still be gated centrally via admin_host_required.
    wrong_host = _authenticated_get(
        client, token, '/backups/view/2026-09-12', 'https://wrong.example.test'
    )
    assert wrong_host.status_code == 302
    assert wrong_host.headers['Location'] == (
        'https://admin.example.test/backups/view/2026-09-12'
    )

    # And it still works normally (renders, not redirected) on the correct host.
    right_host = _authenticated_get(
        client, token, '/backups/view/2026-09-12', 'https://admin.example.test'
    )
    assert right_host.status_code == 200
