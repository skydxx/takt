"""Operator/admin routes: /admin*, /owner*, /backups*, plus the admin app's
own /login, /logout, and /.

This module owns its own ``Database``/``PlatformStore`` connections rather
than importing the public app's — the two apps are meant to run as separate
processes sharing only the underlying database file/DSN (see
docs/security-baseline.md). Everything a route here needs — auth, CSRF,
role checks, ADMIN_HOST enforcement, VPN legacy gating — is self-contained
in this module so the public app never has to import anything from it.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta
from functools import wraps
from hmac import compare_digest

from flask import (
    abort,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash

from config import (
    ADMIN_HOST,
    ADMIN_SESSION_COOKIE_NAME,
    DATABASE_PATH,
    PLATFORM_DATABASE_PATH,
    PLATFORM_DATABASE_URL,
    SESSION_COOKIE_SECURE,
    VPN_ENABLED,
)
from core import run_async
from database import Database
from platform_store import PlatformStore
from utils import format_hours, format_money, safe_path_within

logger = logging.getLogger('admin_app')

db = Database(DATABASE_PATH)
platform_store = PlatformStore(PLATFORM_DATABASE_URL or PLATFORM_DATABASE_PATH)
platform_store.init_schema()

global_backup_mgr = None


def set_backup_manager(mgr) -> None:
    global global_backup_mgr
    global_backup_mgr = mgr


# ---------------------------------------------------------------------------
# Auth / CSRF / host-isolation helpers. Deliberately not shared with the
# public app: these close over *this module's* ``db``, which is what lets
# tests monkeypatch ``admin_routes.db`` independently of ``web_app.db``, and
# is exactly how two genuinely separate processes would behave anyway.
# ---------------------------------------------------------------------------

def auth_csrf_token() -> str:
    token = session.get('auth_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['auth_csrf_token'] = token
    return token


def valid_auth_csrf() -> bool:
    submitted = request.form.get('auth_csrf_token', '')
    expected = session.get('auth_csrf_token', '')
    return bool(submitted and expected and compare_digest(submitted, expected))


def _split_host(value: str) -> tuple[str, str | None]:
    """Lowercase-normalize a Host header / config value into (hostname, port)."""
    value = (value or '').strip().lower()
    if not value:
        return '', None
    if value.startswith('['):
        host, _, rest = value.partition(']')
        host = f'{host}]'
        port = rest[1:] if rest.startswith(':') else None
        return host, port
    host, sep, port = value.rpartition(':')
    if sep and port.isdigit():
        return host, port
    return value, None


def _admin_host_matches(request_host: str) -> bool:
    """Exact, case/port-normalized host comparison — never substring/suffix."""
    req_name, req_port = _split_host(request_host)
    cfg_name, cfg_port = _split_host(ADMIN_HOST)
    if not cfg_name or req_name != cfg_name:
        return False
    if cfg_port is not None and req_port != cfg_port:
        return False
    return True


def _admin_host_response():
    """Return the host-gate response, or ``None`` when the host is allowed."""
    if not ADMIN_HOST or _admin_host_matches(request.host):
        return None
    if not request.is_secure:
        abort(404)
    query = f'?{request.query_string.decode()}' if request.query_string else ''
    return redirect(f'https://{ADMIN_HOST}{request.path}{query}', code=302)


def admin_host_required(f):
    """Require the configured ADMIN_HOST. No-op when ADMIN_HOST is unset.

    See web_app.py's former copy of this docstring / docs/security-baseline.md
    for the full redirect-vs-404 rationale — unchanged here.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        response = _admin_host_response()
        if response is not None:
            return response
        return f(*args, **kwargs)
    return decorated_function


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        response = _admin_host_response()
        if response is not None:
            return response
        token = request.cookies.get(ADMIN_SESSION_COOKIE_NAME)
        if not token:
            return redirect(url_for('login'))
        user = run_async(db.get_user_by_session(token))
        if not user:
            return redirect(url_for('login'))
        request.user = user
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not getattr(request, 'user', None):
            return redirect(url_for('login'))
        user_role = request.user.get('role', 'guest')
        if user_role not in ['admin', 'super_admin']:
            flash('Доступ запрещен: требуются права администратора', 'error')
            # Never redirect to another admin_required/super_admin_required
            # page here — for a non-operator role every such page would
            # deny again, looping. 'login' is the only page in this app a
            # denied-but-authenticated session can land on; it recognizes
            # the non-operator role (see _is_operator_role) and renders the
            # form with this flash instead of bouncing back in.
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return admin_host_required(decorated_function)


def super_admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not getattr(request, 'user', None):
            return redirect(url_for('login'))
        if request.user.get('role') != 'super_admin':
            flash('Доступ запрещен: требуются права супер-администратора', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return admin_host_required(decorated_function)


def auth_rate_limit_allowed(action: str, identifier: str = '', limit: int = 8, window_seconds: int = 900) -> bool:
    address = request.remote_addr or 'unknown'
    clean_identifier = ' '.join(identifier.split()).casefold()[:160]
    ip_allowed = platform_store.allow_rate_limit(f'{action}|ip|{address}', limit, window_seconds)
    if not clean_identifier:
        return ip_allowed
    identity_allowed = platform_store.allow_rate_limit(f'{action}|identity|{clean_identifier}', limit, window_seconds)
    return ip_allowed and identity_allowed


def _operator_home_endpoint(user: dict) -> str:
    return 'owner_dashboard' if user.get('role') == 'super_admin' else 'admin_info'


def _is_operator_role(user: dict | None) -> bool:
    """Whether ``user`` belongs on this surface at all. Any account without
    at least view_admin must never be auto-redirected toward an
    admin_required/super_admin_required page — that page would deny it
    right back, so the only safe destination for such a session is the
    login form itself."""
    return bool(user) and user.get('role') in ('view_admin', 'admin', 'super_admin')


def register_admin_routes(app) -> None:
    """Register every /admin*, /owner*, /backups* route, plus this app's own
    /, /login, /logout, on the given Flask app instance."""

    @app.context_processor
    def _utility_processor():
        return dict(format_money=format_money, format_hours=format_hours, auth_csrf_token=auth_csrf_token())

    @app.context_processor
    def _inject_permissions():
        user = getattr(request, 'user', None)
        permissions = {
            'is_super_admin': False,
            'is_admin': False,
            'is_view_admin': False,
            'is_linked': True,
            'is_guest': True,
        }
        if user:
            role = user.get('role', 'guest')
            permissions['is_super_admin'] = role == 'super_admin'
            permissions['is_admin'] = role in ['admin', 'super_admin']
            permissions['is_view_admin'] = role in ['view_admin', 'admin', 'super_admin']
            permissions['is_guest'] = role == 'guest'
        return permissions

    @app.before_request
    def _disable_legacy_vpn_orders_by_default():
        """Mirror the public app's VPN feature flag for the VPN-order admin
        surface (/owner/vpn-orders*) that lives in this app now."""
        if VPN_ENABLED:
            return None
        legacy_endpoints = {
            'owner_vpn_orders', 'confirm_vpn_order', 'add_vpn_key',
            'cancel_vpn_order', 'ban_user_from_order',
        }
        if request.endpoint in legacy_endpoints:
            abort(404)
        return None

    @app.route('/')
    def index():
        host_response = _admin_host_response()
        if host_response is not None:
            return host_response
        token = request.cookies.get(ADMIN_SESSION_COOKIE_NAME)
        if token:
            user = run_async(db.get_user_by_session(token))
            if _is_operator_role(user):
                return redirect(url_for(_operator_home_endpoint(user)))
        return redirect(url_for('login'))

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        host_response = _admin_host_response()
        if host_response is not None:
            return host_response
        if request.cookies.get(ADMIN_SESSION_COOKIE_NAME):
            try:
                user = run_async(db.get_user_by_session(request.cookies.get(ADMIN_SESSION_COOKIE_NAME)))
                if _is_operator_role(user):
                    return redirect(url_for(_operator_home_endpoint(user)))
            except Exception as e:
                logger.error(f'Error checking session: {e}', exc_info=True)

        if request.method == 'POST':
            if not valid_auth_csrf():
                flash('Сессия формы истекла, обновите страницу', 'error')
                return render_template('admin/login.html', user=None)
            if not auth_rate_limit_allowed('admin-login', request.form.get('username', '')):
                flash('Слишком много попыток. Попробуйте позже.', 'error')
                return render_template('admin/login.html', user=None), 429
            try:
                username = request.form.get('username')
                password = request.form.get('password')
                if not username or not password:
                    flash('Заполните все поля', 'error')
                    return render_template('admin/login.html', user=None)

                user = run_async(db.get_web_user(username))
                if user and check_password_hash(user['password_hash'], password) and not _is_operator_role(user):
                    flash('У этого аккаунта нет доступа к панели оператора', 'error')
                    return render_template('admin/login.html', user=None)
                if user and check_password_hash(user['password_hash'], password):
                    session.clear()
                    token = secrets.token_hex(32)
                    expires_at = datetime.now() + timedelta(days=7)
                    run_async(db.create_web_session(
                        web_user_id=user['id'], token=token, expires_at=expires_at,
                        ip=request.remote_addr, user_agent=request.user_agent.string,
                    ))
                    resp = make_response(redirect(url_for(_operator_home_endpoint(user))))
                    resp.set_cookie(
                        ADMIN_SESSION_COOKIE_NAME, token, expires=expires_at, httponly=True,
                        secure=SESSION_COOKIE_SECURE, samesite='Lax', path='/',
                    )
                    return resp
                flash('Неверный логин или пароль', 'error')
            except Exception as e:
                logger.error(f'Admin login error: {e}', exc_info=True)
                flash('Не удалось выполнить вход. Проверьте данные и повторите попытку.', 'error')

        return render_template('admin/login.html', user=None)

    @app.route('/logout', methods=['POST'])
    def logout():
        host_response = _admin_host_response()
        if host_response is not None:
            return host_response
        if not valid_auth_csrf():
            flash('Сессия формы истекла, обновите страницу', 'error')
            return redirect(url_for('login'))
        token = request.cookies.get(ADMIN_SESSION_COOKIE_NAME)
        if token:
            run_async(db.delete_web_session(token))
        resp = make_response(redirect(url_for('login')))
        resp.set_cookie(
            ADMIN_SESSION_COOKIE_NAME, '', expires=0, httponly=True,
            secure=SESSION_COOKIE_SECURE, samesite='Lax', path='/',
        )
        return resp

    @app.route('/backups')
    @login_required
    @admin_required
    def backups():
        """Страница управления бэкапами"""
        if not global_backup_mgr:
            flash('Менеджер бэкапов не инициализирован', 'error')
            return redirect(url_for('admin_info'))
        backup_list = run_async(global_backup_mgr.get_backup_list())
        return render_template('admin/backups.html', user=request.user, backups=backup_list)

    @app.route('/backups/create', methods=['POST'])
    @login_required
    @admin_required
    def create_backup():
        """Создание нового бэкапа"""
        if not valid_auth_csrf():
            flash('Сессия формы истекла, обновите страницу', 'error')
            return redirect(url_for('backups'))
        if not global_backup_mgr:
            flash('Менеджер бэкапов не инициализирован', 'error')
            return redirect(url_for('backups'))
        try:
            backed_up, failed = run_async(global_backup_mgr.create_daily_backup())
            flash(f'Бэкап создан: {backed_up} успешно, {failed} ошибок', 'success')
        except Exception as e:
            logger.error(f'Error creating backup: {e}', exc_info=True)
            flash(f'Ошибка создания бэкапа: {e}', 'error')
        return redirect(url_for('backups'))

    @app.route('/backups/download/<filename>')
    @login_required
    @admin_required
    def download_backup(filename):
        """Скачивание бэкапа"""
        if not global_backup_mgr:
            flash('Менеджер бэкапов не инициализирован', 'error')
            return redirect(url_for('backups'))
        backup_path = safe_path_within(global_backup_mgr.backup_dir, filename)
        if backup_path is None or not backup_path.exists():
            abort(404)
        return send_file(backup_path, as_attachment=True)

    @app.route('/backups/view/<backup_date>')
    @login_required
    @admin_host_required
    def view_backup(backup_date):
        """Просмотр бэкапа"""
        user_role = request.user.get('role', 'guest')
        if user_role not in ['super_admin', 'view_admin']:
            flash('Доступ запрещен', 'error')
            return redirect(url_for('admin_info'))
        if not global_backup_mgr:
            flash('Менеджер бэкапов не инициализирован', 'error')
            return redirect(url_for('admin_info'))
        backup_chats = run_async(global_backup_mgr.get_backup_chats(backup_date))
        return render_template(
            'admin/backup_viewer.html', user=request.user, backup_date=backup_date, chats=backup_chats,
        )

    @app.route('/backups/view/<backup_date>/<filename>')
    @login_required
    @admin_host_required
    def view_backup_chat(backup_date, filename):
        """Просмотр данных чата из бэкапа"""
        user_role = request.user.get('role', 'guest')
        if user_role not in ['super_admin', 'view_admin']:
            flash('Доступ запрещен', 'error')
            return redirect(url_for('admin_info'))
        if not global_backup_mgr:
            abort(404)
        chat_data = run_async(global_backup_mgr.get_backup_chat_data(backup_date, filename))
        if not chat_data:
            abort(404)
        return render_template(
            'admin/backup_chat_view.html', user=request.user, backup_date=backup_date, chat_data=chat_data,
        )

    @app.route('/admin')
    @login_required
    @admin_required
    def admin():
        users = run_async(db.get_all_web_users())
        return render_template('admin/admin.html', user=request.user, users=users)

    @app.route('/admin/users')
    @login_required
    @admin_required
    def admin_users():
        users = run_async(db.get_all_web_users())
        return render_template('admin/admin_users.html', user=request.user, users=users)

    @app.route('/admin/users/toggle_admin/<int:user_id>')
    @login_required
    @super_admin_required
    def toggle_admin(user_id):
        flash('Используйте панель владельца для управления правами', 'info')
        return redirect(url_for('owner_dashboard'))

    @app.route('/admin/leads')
    @login_required
    @admin_required
    def admin_leads():
        leads = run_async(db.get_lead_requests())
        return render_template('admin/admin_leads.html', user=request.user, leads=leads)

    @app.route('/admin/leads/<int:lead_id>', methods=['POST'])
    @login_required
    @admin_required
    def update_admin_lead(lead_id: int):
        if not valid_auth_csrf():
            flash('Сессия формы истекла, обновите страницу', 'error')
            return redirect(url_for('admin_leads'))
        try:
            run_async(db.update_lead_status(lead_id, request.form.get('status', '')))
            flash('Статус заявки обновлён', 'success')
        except ValueError as exc:
            flash(str(exc), 'error')
        return redirect(url_for('admin_leads'))

    @app.route('/admin/owner-code', methods=['POST'])
    @login_required
    @admin_required
    def issue_admin_owner_code():
        """Issue a paid-owner code from the operator inbox without shell access."""
        if not valid_auth_csrf():
            flash('Сессия формы истекла, обновите страницу', 'error')
            return redirect(url_for('admin_leads'))
        try:
            expires_days = int(request.form.get('expires_days', '30'))
            point_limit_value = request.form.get('point_limit', '').strip()
            point_limit = int(point_limit_value) if point_limit_value else None
            code = platform_store.issue_owner_code(
                expires_days=expires_days,
                plan=request.form.get('plan', '').strip() or None,
                point_limit=point_limit,
            )
            flash(f'Код подключения создан: {code}', 'success')
        except (TypeError, ValueError) as exc:
            flash(str(exc), 'error')
        return redirect(url_for('admin_leads'))

    @app.route('/admin/info')
    @login_required
    @admin_required
    def admin_info():
        user_role = request.user.get('role', 'guest')
        is_super = user_role == 'super_admin'
        all_chats = run_async(db.get_all_chats_full_info())
        all_workers = run_async(db.get_all_workers_stats())
        visible_chats = []
        visible_workers = []
        if is_super or user_role == 'view_admin':
            visible_chats = all_chats
            visible_workers = all_workers
        else:
            managed_chats = request.user.get('managed_chat_ids', '')
            if managed_chats:
                managed_chat_list = [int(x.strip()) for x in managed_chats.split(',') if x.strip()]
                visible_chats = [c for c in all_chats if c['chat_id'] in managed_chat_list]
                if visible_chats:
                    chat_ids = [c['chat_id'] for c in visible_chats]
                    visible_workers = run_async(db.get_all_workers_stats(chat_ids=chat_ids))
        return render_template(
            'admin/admin_info.html', user=request.user,
            chats=visible_chats, workers=visible_workers, is_super=is_super,
        )

    @app.route('/owner')
    @login_required
    @super_admin_required
    def owner_dashboard():
        """Панель владельца - управление всеми пользователями"""
        web_users = run_async(db.get_all_web_users())
        telegram_users = run_async(db.get_all_telegram_users())
        all_chats = run_async(db.get_all_chats_full_info())
        return render_template(
            'admin/owner_dashboard.html', user=request.user,
            web_users=web_users, telegram_users=telegram_users, chats=all_chats,
        )

    @app.route('/owner/update_role/<int:user_id>', methods=['POST'])
    @login_required
    @super_admin_required
    def update_user_role(user_id):
        """Обновление роли пользователя"""
        if not valid_auth_csrf():
            flash('Сессия формы истекла, обновите страницу', 'error')
            return redirect(url_for('owner_dashboard'))
        try:
            role = request.form.get('role')
            managed_chats = request.form.get('managed_chats', '')
            if role not in ['guest', 'user', 'view_admin', 'admin', 'super_admin']:
                flash('Неверная роль', 'error')
                return redirect(url_for('owner_dashboard'))
            run_async(db.update_user_role(user_id, role, managed_chats if managed_chats else None))
            flash('Роль пользователя обновлена', 'success')
        except Exception as e:
            logger.error(f'Error updating user role: {e}', exc_info=True)
            flash(f'Ошибка обновления роли: {e}', 'error')
        return redirect(url_for('owner_dashboard'))

    @app.route('/owner/vpn-orders')
    @login_required
    @super_admin_required
    def owner_vpn_orders():
        """Панель управления VPN заказами"""
        all_orders = run_async(db.get_all_vpn_orders())
        for order in all_orders:
            order['vpn_keys'] = run_async(db.get_order_keys(order['id']))
        return render_template('admin/owner_vpn_orders.html', user=request.user, orders=all_orders)

    @app.route('/owner/vpn-orders/<int:order_id>/confirm', methods=['POST'])
    @login_required
    @super_admin_required
    def confirm_vpn_order(order_id):
        """Подтверждение VPN заказа"""
        if not valid_auth_csrf():
            flash('Сессия формы истекла, обновите страницу', 'error')
            return redirect(url_for('owner_vpn_orders'))
        try:
            run_async(db.confirm_vpn_order(order_id))
            flash('Заказ подтвержден! Теперь можно добавить ключи.', 'success')
        except Exception as e:
            logger.error(f'Error confirming order: {e}', exc_info=True)
            flash(f'Ошибка подтверждения: {e}', 'error')
        return redirect(url_for('owner_vpn_orders'))

    @app.route('/owner/vpn-orders/<int:order_id>/add-key', methods=['POST'])
    @login_required
    @super_admin_required
    def add_vpn_key(order_id):
        """Добавление VPN ключа к заказу"""
        if not valid_auth_csrf():
            flash('Сессия формы истекла, обновите страницу', 'error')
            return redirect(url_for('owner_vpn_orders'))
        try:
            device_number = int(request.form.get('device_number'))
            vpn_key = request.form.get('vpn_key', '').strip()
            if not vpn_key:
                flash('Введите VPN ключ', 'error')
                return redirect(url_for('owner_vpn_orders'))
            run_async(db.add_vpn_key_to_order(order_id, device_number, vpn_key))
            flash(f'Ключ для устройства {device_number} добавлен!', 'success')
        except Exception as e:
            logger.error(f'Error adding VPN key: {e}', exc_info=True)
            flash(f'Ошибка добавления ключа: {e}', 'error')
        return redirect(url_for('owner_vpn_orders'))

    @app.route('/owner/vpn-orders/<int:order_id>/cancel', methods=['POST'])
    @login_required
    @super_admin_required
    def cancel_vpn_order(order_id):
        """Отмена VPN заказа"""
        if not valid_auth_csrf():
            flash('Сессия формы истекла, обновите страницу', 'error')
            return redirect(url_for('owner_vpn_orders'))
        try:
            run_async(db.cancel_vpn_order(order_id))
            flash('Заказ отменён', 'success')
        except Exception as e:
            logger.error(f'Error cancelling order: {e}', exc_info=True)
            flash(f'Ошибка отмены заказа: {e}', 'error')
        return redirect(url_for('owner_vpn_orders'))

    @app.route('/owner/vpn-orders/<int:order_id>/ban-user', methods=['POST'])
    @login_required
    @super_admin_required
    def ban_user_from_order(order_id):
        """Блокировка пользователя из заказа"""
        if not valid_auth_csrf():
            flash('Сессия формы истекла, обновите страницу', 'error')
            return redirect(url_for('owner_vpn_orders'))
        try:
            all_orders = run_async(db.get_all_vpn_orders())
            order = next((o for o in all_orders if o['id'] == order_id), None)
            if not order:
                flash('Заказ не найден', 'error')
                return redirect(url_for('owner_vpn_orders'))

            ban_hours = request.form.get('ban_hours', '').strip()
            ban_until = request.form.get('ban_until', '').strip()
            reason = request.form.get('reason', 'Не оплатил заказ').strip()
            is_permanent = request.form.get('permanent') == 'on'

            expires_at = None
            if is_permanent:
                pass
            elif ban_until:
                try:
                    expires_at = datetime.fromisoformat(ban_until)
                except ValueError:
                    flash('Неверный формат даты', 'error')
                    return redirect(url_for('owner_vpn_orders'))
            elif ban_hours:
                try:
                    hours = float(ban_hours)
                    expires_at = datetime.now() + timedelta(hours=hours)
                except ValueError:
                    flash('Неверное количество часов', 'error')
                    return redirect(url_for('owner_vpn_orders'))
            else:
                flash('Укажите срок блокировки', 'error')
                return redirect(url_for('owner_vpn_orders'))

            run_async(db.ban_user(
                order['web_user_id'], order['telegram_contact'], reason,
                request.user['id'], expires_at, is_permanent,
            ))
            run_async(db.cancel_vpn_order(order_id))

            if is_permanent:
                flash('Пользователь заблокирован навсегда. Заказ отменён.', 'success')
            else:
                flash(f'Пользователь заблокирован до {expires_at}. Заказ отменён.', 'success')
        except Exception as e:
            logger.error(f'Error banning user: {e}', exc_info=True)
            flash(f'Ошибка блокировки: {e}', 'error')
        return redirect(url_for('owner_vpn_orders'))
