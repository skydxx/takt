import asyncio
import os
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

import backup_manager
from database import Database
from platform_store import PlatformStore
from utils import safe_path_within


def _make_client(tmp_path, monkeypatch):
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


def _login_as(database, client, username, role, *, admin=False):
    async def prepare():
        user_id = await database.create_web_user(
            username, generate_password_hash('secret-password'), role=role
        )
        token = f'{username}-session'
        await database.create_web_session(user_id, token, datetime.now() + timedelta(days=1))
        return user_id, token

    user_id, token = asyncio.run(prepare())
    client.set_cookie('admin_session_token' if admin else 'session_token', token)
    return user_id


def _csrf(client):
    # Render any authenticated page first so the context processor primes
    # session['auth_csrf_token'] before we read it back.
    client.get('/dashboard')
    with client.session_transaction() as flask_session:
        return flask_session['auth_csrf_token']


def _admin_csrf(client):
    # Render any authenticated admin page first so the context processor
    # primes session['auth_csrf_token'] before we read it back.
    client.get('/admin/info')
    with client.session_transaction() as flask_session:
        return flask_session['auth_csrf_token']


def test_baseline_csp_has_no_third_party_origins(tmp_path, monkeypatch):
    web_app, database, client = _make_client(tmp_path, monkeypatch)

    response = client.get('/')
    csp = response.headers['Content-Security-Policy']

    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "font-src 'self'" in csp
    # Google Fonts and Lucide are now self-hosted (static/fonts,
    # static/js/vendor/lucide.min.js) — no third-party origin should appear
    # anywhere in the policy any more.
    assert 'fonts.googleapis.com' not in csp
    assert 'fonts.gstatic.com' not in csp
    assert 'unpkg.com' not in csp
    # 'unsafe-inline' remains the one documented exception, for legacy
    # inline onclick/oninput handlers and style="" attributes.
    assert "script-src 'self' 'unsafe-inline'" in csp
    assert "style-src 'self' 'unsafe-inline'" in csp


def test_landing_self_hosts_fonts_and_omits_third_party_assets(tmp_path, monkeypatch):
    web_app, database, client = _make_client(tmp_path, monkeypatch)

    response = client.get('/')
    html = response.text

    assert 'fonts.googleapis.com' not in html
    assert 'fonts.gstatic.com' not in html
    assert 'unpkg.com' not in html
    assert '/static/css/fonts.css' in html

    fonts_css = client.get('/static/css/fonts.css')
    assert fonts_css.status_code == 200
    assert b'fonts.gstatic.com' not in fonts_css.data
    assert b'@font-face' in fonts_css.data


def test_base_template_self_hosts_inter_and_lucide(tmp_path, monkeypatch):
    web_app, database, client = _make_client(tmp_path, monkeypatch)

    response = client.get('/login')
    html = response.text

    assert 'fonts.googleapis.com' not in html
    assert 'unpkg.com' not in html
    assert '/static/css/fonts-inter.css' in html
    assert '/static/js/vendor/lucide.min.js' in html

    inter_css = client.get('/static/css/fonts-inter.css')
    assert inter_css.status_code == 200
    lucide_js = client.get('/static/js/vendor/lucide.min.js')
    assert lucide_js.status_code == 200


def test_legal_routes_exist_in_both_languages_with_draft_notice(tmp_path, monkeypatch):
    web_app, database, client = _make_client(tmp_path, monkeypatch)

    for path, ru_title in (
        ('/terms', 'Условия использования'),
        ('/privacy', 'Политика конфиденциальности'),
        ('/cookies', 'Уведомление о cookie и localStorage'),
    ):
        ru = client.get(path)
        assert ru.status_code == 200
        assert ru_title in ru.text
        assert 'Черновик' in ru.text
        assert 'fonts.googleapis.com' not in ru.text

        client.set_cookie('site_lang', 'en')
        en = client.get(path)
        assert en.status_code == 200
        assert 'Draft' in en.text
        client.delete_cookie('site_lang')


def test_landing_footer_links_to_real_legal_routes_not_placeholders(tmp_path, monkeypatch):
    web_app, database, client = _make_client(tmp_path, monkeypatch)

    html = client.get('/').text
    assert 'href="#"' not in html
    assert '/terms' in html
    assert '/privacy' in html
    assert '/cookies' in html


def test_cookie_consent_banner_is_present_and_sets_no_tracking_cookie(tmp_path, monkeypatch):
    web_app, database, client = _make_client(tmp_path, monkeypatch)

    response = client.get('/')
    assert 'id="cookie-banner"' in response.text
    assert 'cookie-banner-dismiss' in response.text
    # No analytics/tracking cookie should ever be set by a plain page view.
    set_cookie_headers = response.headers.get_all('Set-Cookie')
    assert not any('_ga' in h or 'analytics' in h.lower() for h in set_cookie_headers)


def test_logout_requires_post_and_csrf(tmp_path, monkeypatch):
    web_app, database, client = _make_client(tmp_path, monkeypatch)
    _login_as(database, client, 'logout-user', 'user')

    # GET is no longer a valid way to change session state.
    get_response = client.get('/logout')
    assert get_response.status_code == 405

    # A POST without the CSRF token must not tear down the session.
    no_csrf_response = client.post('/logout')
    assert no_csrf_response.status_code == 302
    assert no_csrf_response.headers['Location'].endswith('/dashboard')
    assert client.get('/dashboard').status_code == 200

    # A POST with the correct CSRF token logs the user out.
    csrf = _csrf(client)
    response = client.post('/logout', data={'auth_csrf_token': csrf})
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/login')
    assert client.get('/dashboard').status_code == 302


def test_admin_update_role_requires_csrf(tmp_path, monkeypatch):
    admin_routes, database, client = _make_admin_client(tmp_path, monkeypatch)
    _login_as(database, client, 'root-owner', 'super_admin', admin=True)
    target_id = asyncio.run(
        database.create_web_user('plain-user', generate_password_hash('secret-password'), role='user')
    )

    no_csrf = client.post(f'/owner/update_role/{target_id}', data={'role': 'admin'})
    assert no_csrf.status_code == 302
    assert asyncio.run(database.get_web_user('plain-user'))['role'] == 'user'

    csrf = _admin_csrf(client)
    ok = client.post(
        f'/owner/update_role/{target_id}', data={'auth_csrf_token': csrf, 'role': 'admin'}
    )
    assert ok.status_code == 302
    assert asyncio.run(database.get_web_user('plain-user'))['role'] == 'admin'


def test_backup_create_requires_csrf(tmp_path, monkeypatch):
    admin_routes, database, client = _make_admin_client(tmp_path, monkeypatch)
    _login_as(database, client, 'backup-admin', 'admin', admin=True)

    calls = []

    class FakeBackupManager:
        backup_dir = str(tmp_path)

        async def create_daily_backup(self):
            calls.append(True)
            return 0, 0

    admin_routes.set_backup_manager(FakeBackupManager())

    no_csrf = client.post('/backups/create')
    assert no_csrf.status_code == 302
    assert calls == []

    csrf = _admin_csrf(client)
    ok = client.post('/backups/create', data={'auth_csrf_token': csrf})
    assert ok.status_code == 302
    assert calls == [True]


def test_vpn_cancel_order_requires_csrf(tmp_path, monkeypatch):
    admin_routes, database, client = _make_admin_client(tmp_path, monkeypatch)
    monkeypatch.setattr(admin_routes, 'VPN_ENABLED', True)
    user_id = _login_as(database, client, 'vpn-owner', 'super_admin', admin=True)
    order_id = asyncio.run(
        database.create_vpn_order(user_id, 1, 30, 500.0, '@owner')
    )

    no_csrf = client.post(f'/owner/vpn-orders/{order_id}/cancel')
    assert no_csrf.status_code == 302
    orders = asyncio.run(database.get_all_vpn_orders())
    assert orders[0]['status'] == 'pending'

    csrf = _admin_csrf(client)
    ok = client.post(
        f'/owner/vpn-orders/{order_id}/cancel', data={'auth_csrf_token': csrf}
    )
    assert ok.status_code == 302
    orders = asyncio.run(database.get_all_vpn_orders())
    assert orders[0]['status'] == 'cancelled'


def test_download_backup_allows_real_file_and_blocks_traversal_and_symlink(tmp_path, monkeypatch):
    admin_routes, database, client = _make_admin_client(tmp_path, monkeypatch)
    _login_as(database, client, 'download-admin', 'admin', admin=True)

    backup_root = tmp_path / 'backups'
    backup_root.mkdir()
    (backup_root / 'report.txt').write_text('safe contents')

    secret_dir = tmp_path / 'outside'
    secret_dir.mkdir()
    secret_file = secret_dir / 'secret.txt'
    secret_file.write_text('top secret')

    class FakeBackupManager:
        backup_dir = str(backup_root)

    admin_routes.set_backup_manager(FakeBackupManager())

    good = client.get('/backups/download/report.txt')
    assert good.status_code == 200
    assert good.data == b'safe contents'

    # A dotdot-only segment (no slash) must not resolve outside backup_dir.
    dotdot = client.get('/backups/download/..')
    assert dotdot.status_code == 404

    if hasattr(os, 'symlink'):
        try:
            (backup_root / 'escape.txt').symlink_to(secret_file)
        except OSError:
            return  # symlinks unsupported in this environment (e.g. sandboxed CI)
        leaked = client.get('/backups/download/escape.txt')
        assert leaked.status_code == 404


def test_safe_path_within_rejects_traversal_and_symlink_escape(tmp_path):
    base = tmp_path / 'base'
    base.mkdir()
    (base / 'ok.json').write_text('{}')
    outside = tmp_path / 'outside.json'
    outside.write_text('{"leak": true}')

    assert safe_path_within(base, 'ok.json') == (base / 'ok.json').resolve()
    assert safe_path_within(base, '..') is None
    assert safe_path_within(base, '..', 'outside.json') is None
    assert safe_path_within(base, 'a/b') is None
    assert safe_path_within(base, 'a\\b') is None
    assert safe_path_within(base, '') is None

    if hasattr(os, 'symlink'):
        try:
            (base / 'link.json').symlink_to(outside)
        except OSError:
            return
        assert safe_path_within(base, 'link.json') is None


def test_backup_manager_get_backup_chat_data_blocks_traversal_and_symlink(tmp_path):
    backup_dir = tmp_path / 'backups'
    date_dir = backup_dir / '2026-09-12'
    date_dir.mkdir(parents=True)
    (date_dir / 'chat.json').write_text(
        '{"chat_id": 1, "chat_name": "Test"}'
    )

    outside = tmp_path / 'secret.json'
    outside.write_text('{"secret": true}')

    mgr = backup_manager.BackupManager(bot=None, database=None, backup_dir=str(backup_dir))

    chats = asyncio.run(mgr.get_backup_chats('2026-09-12'))
    assert len(chats) == 1
    assert chats[0]['file_name'] == 'chat.json'

    assert asyncio.run(mgr.get_backup_chats('..')) == []
    assert asyncio.run(mgr.get_backup_chat_data('2026-09-12', 'chat.json')) == {
        'chat_id': 1,
        'chat_name': 'Test',
    }
    assert asyncio.run(mgr.get_backup_chat_data('..', 'secret.json')) is None
    assert asyncio.run(mgr.get_backup_chat_data('2026-09-12', '../../secret.json')) is None

    if hasattr(os, 'symlink'):
        try:
            (date_dir / 'escape.json').symlink_to(outside)
        except OSError:
            return
        assert asyncio.run(mgr.get_backup_chat_data('2026-09-12', 'escape.json')) is None


def test_backup_manager_get_backup_chats_skips_symlinked_entry(tmp_path):
    """A symlink planted inside an otherwise-valid date folder must not be
    followed when listing chats, even though the folder itself resolves
    safely under backup_dir."""
    if not hasattr(os, 'symlink'):
        return

    backup_dir = tmp_path / 'backups'
    date_dir = backup_dir / '2026-09-12'
    date_dir.mkdir(parents=True)
    (date_dir / 'real.json').write_text('{"chat_id": 1, "chat_name": "Real chat"}')

    outside_dir = tmp_path / 'outside'
    outside_dir.mkdir()
    outside_secret = outside_dir / 'secret.json'
    outside_secret.write_text('{"chat_id": 999, "chat_name": "Leaked"}')

    try:
        (date_dir / 'escape.json').symlink_to(outside_secret)
    except OSError:
        return

    mgr = backup_manager.BackupManager(bot=None, database=None, backup_dir=str(backup_dir))
    chats = asyncio.run(mgr.get_backup_chats('2026-09-12'))

    # The real, in-place entry is still listed ...
    file_names = {chat['file_name'] for chat in chats}
    assert 'real.json' in file_names
    # ... but the symlinked entry pointing outside backup_dir is not, and
    # its content ("Leaked") never surfaces.
    assert 'escape.json' not in file_names
    assert all(chat['chat_name'] != 'Leaked' for chat in chats)
    assert len(chats) == 1
