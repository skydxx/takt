import asyncio
import csv
import io
import logging
import os
import re
import secrets
from datetime import datetime, timedelta
from functools import wraps
from hmac import compare_digest

from flask import (
    Flask,
    abort,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from config import (
    APP_ENV,
    DATABASE_PATH,
    FLASK_SECRET_KEY,
    PLATFORM_DATABASE_PATH,
    PLATFORM_DATABASE_URL,
    SESSION_COOKIE_SECURE,
    VPN_ENABLED,
)
from core import register_error_handlers
from database import MSK, Database, _get_work_window
from legal_i18n import LEGAL_CONTENT
from platform_store import AUTOMATION_PRESETS, PlatformStore, money_to_minor
from public_i18n import PUBLIC_COPY
from utils import format_hours, format_money

# Настройка приложения
app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=SESSION_COOKIE_SECURE,
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,
)
db = Database(DATABASE_PATH)
platform_store = PlatformStore(PLATFORM_DATABASE_URL or PLATFORM_DATABASE_PATH)
platform_store.init_schema()

SUPPORTED_PUBLIC_LANGS = tuple(PUBLIC_COPY.keys())
REGISTRATION_USERNAME_RE = re.compile(r'^[\w][\w.-]{2,63}$', re.UNICODE)
REGISTRATION_PASSWORD_MIN_LENGTH = 12
REGISTRATION_PASSWORD_MAX_LENGTH = 256


def public_language() -> str:
    """Choose a supported public language without trusting arbitrary input."""
    candidate = request.cookies.get('site_lang') or request.args.get('lang') or 'ru'
    return candidate if candidate in SUPPORTED_PUBLIC_LANGS else 'ru'


def public_csrf_cookie() -> str:
    return request.cookies.get('public_csrf_token') or secrets.token_urlsafe(32)


def render_public(**context):
    lang = context.pop('current_lang', public_language())
    token = context.pop('csrf_token', public_csrf_cookie())
    response = make_response(render_template(
        'landing.html',
        copy=PUBLIC_COPY[lang],
        current_lang=lang,
        csrf_token=token,
        **context,
    ))
    response.set_cookie(
        'public_csrf_token', token, httponly=False, secure=SESSION_COOKIE_SECURE,
        samesite='Lax', max_age=60 * 60 * 24 * 30, path='/',
    )
    return response


def workspace_csrf_token() -> str:
    token = session.get('workspace_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['workspace_csrf_token'] = token
    return token


def valid_workspace_csrf() -> bool:
    submitted = request.form.get('csrf_token', '')
    expected = session.get('workspace_csrf_token', '')
    return bool(submitted and expected and compare_digest(submitted, expected))


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


def auth_rate_limit_allowed(
    action: str, identifier: str = '', limit: int = 8, window_seconds: int = 900
) -> bool:
    """Rate-limit browser auth actions independently by IP and identifier."""
    address = request.remote_addr or 'unknown'
    clean_identifier = ' '.join(identifier.split()).casefold()[:160]
    ip_allowed = platform_store.allow_rate_limit(
        f'{action}|ip|{address}', limit, window_seconds
    )
    if not clean_identifier:
        return ip_allowed
    identity_allowed = platform_store.allow_rate_limit(
        f'{action}|identity|{clean_identifier}', limit, window_seconds
    )
    return ip_allowed and identity_allowed

# Логгер с записью в файл
logger = logging.getLogger('web_app')
logger.setLevel(logging.DEBUG)

# Добавляем handler для записи в файл
log_file = os.path.join(os.path.dirname(DATABASE_PATH), 'web_app.log')
file_handler = logging.FileHandler(log_file, encoding='utf-8')
file_handler.setLevel(logging.DEBUG)
file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(file_formatter)
logger.addHandler(file_handler)

# Также логируем в консоль
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
logger.addHandler(console_handler)

logger.info('Web app logger initialized')

# Глобальная функция для отправки Telegram уведомлений
async def notify_owner(message: str):
    """Отправка уведомления владельцу в Telegram"""
    import aiohttp

    from config import BOT_TOKEN, OWNER_TELEGRAM_ID
    
    if not OWNER_TELEGRAM_ID or not BOT_TOKEN:
        logger.warning("Cannot send Telegram notification: OWNER_TELEGRAM_ID or BOT_TOKEN not set")
        return
    
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": OWNER_TELEGRAM_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data) as response:
                if response.status == 200:
                    logger.info(f"Telegram notification sent to {OWNER_TELEGRAM_ID}")
                else:
                    logger.error(f"Failed to send Telegram notification: {response.status}")
    except Exception as e:
        logger.error(f"Error sending Telegram notification: {e}")

# Контекстные процессоры
@app.context_processor
def utility_processor():
    return dict(format_money=format_money, format_hours=format_hours, auth_csrf_token=auth_csrf_token())

# Polished 404/403/405/500 pages, localized via the public RU/EN switch.
# Never leaks stack traces, paths, or secrets — see core.render_error_page.
register_error_handlers(app, lang_getter=public_language, logger=logger)


# Baseline CSP for the whole app. No third-party origins are needed anywhere
# in the app any more: Google Fonts is self-hosted under static/fonts (see
# static/css/fonts.css) and Lucide is a pinned, self-hosted bundle under
# static/js/vendor/lucide.min.js (was unpkg.com/lucide@latest). The one
# remaining, documented exception:
#   - 'unsafe-inline' on script-src/style-src: the legacy admin/VPN templates
#     (owner_vpn_orders.html, owner_dashboard.html, workspace.html, etc.) rely
#     on inline onclick/oninput handlers and inline style="" attributes that
#     CSP cannot allow via nonces/hashes. Removing this needs a template pass
#     across the legacy surface, out of scope for this bounded security block.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'"
)


@app.after_request
def add_security_headers(response):
    """Apply safe defaults to every browser response."""
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    response.headers.setdefault('Content-Security-Policy', CONTENT_SECURITY_POLICY)
    if APP_ENV == 'production' and SESSION_COOKIE_SECURE:
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    return response

# Хелпер для запуска асинхронных функций
def run_async(coro):
    """Безопасный запуск асинхронной функции в синхронном контексте"""
    try:
        # Пытаемся получить текущий event loop
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Если loop уже запущен, создаем новый
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(coro)
            loop.close()
            return result
        else:
            return loop.run_until_complete(coro)
    except RuntimeError:
        # Если loop не существует, создаем новый
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(coro)
        loop.close()
        return result

# Декоратор для проверки авторизации
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.cookies.get('session_token')
        if not token:
            return redirect(url_for('login'))

        user = run_async(db.get_user_by_session(token))
        if not user:
            return redirect(url_for('login'))

        request.user = user
        return f(*args, **kwargs)
    return decorated_function

# Декоратор для проверки связанного аккаунта (не guest)
def linked_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not getattr(request, 'user', None):
            return redirect(url_for('login'))
        
        if not request.user.get('telegram_linked'):
            flash('Для доступа к этому разделу необходимо связать аккаунт с Telegram', 'info')
            return redirect(url_for('link_account'))
            
        return f(*args, **kwargs)
    return decorated_function

# Context processor для проверки прав
@app.context_processor
def inject_permissions():
    user = getattr(request, 'user', None)
    permissions = {
        'is_super_admin': False,
        'is_admin': False,
        'is_view_admin': False,
        'is_linked': False,
        'is_guest': True
    }
    
    if user:
        role = user.get('role', 'guest')
        permissions['is_super_admin'] = (role == 'super_admin')
        permissions['is_admin'] = (role in ['admin', 'super_admin'])
        permissions['is_view_admin'] = (role in ['view_admin', 'admin', 'super_admin'])
        permissions['is_linked'] = user.get('telegram_linked', False)
        permissions['is_guest'] = (role == 'guest')
    
    return permissions


@app.before_request
def disable_legacy_vpn_by_default():
    """Keep the old VPN store out of the new product surface."""
    if VPN_ENABLED:
        return None
    legacy_endpoints = {
        'vpn', 'create_vpn_order', 'my_purchases', 'vpn_add',
    }
    if request.endpoint in legacy_endpoints or request.path.startswith('/vpn'):
        abort(404)
    return None

@app.route('/')
def index():
    # Keep the public landing page public even for authenticated visitors.
    # The private workspace is intentionally opened through /workspace so a
    # stale or valid session cookie can never hijack the site's home page.
    return render_public()


@app.route('/lang/<code>')
def set_language(code):
    """Persist the public site language and return to the page the visitor saw."""
    if code not in SUPPORTED_PUBLIC_LANGS:
        abort(404)
    referrer = request.referrer or url_for('index')
    if not referrer.startswith(request.host_url):
        referrer = url_for('index')
    response = make_response(redirect(referrer))
    response.set_cookie(
        'site_lang', code, httponly=False, secure=SESSION_COOKIE_SECURE,
        samesite='Lax', max_age=60 * 60 * 24 * 365, path='/',
    )
    return response


def render_legal(page: str):
    """Render one of the draft legal pages, honoring the same language rules as the landing page."""
    lang = public_language()
    return render_template(
        'legal.html',
        copy=PUBLIC_COPY[lang],
        current_lang=lang,
        content=LEGAL_CONTENT[page][lang],
    )


@app.route('/terms')
def terms_of_use():
    return render_legal('terms')


@app.route('/privacy')
def privacy_policy():
    return render_legal('privacy')


@app.route('/cookies')
def cookies_notice():
    return render_legal('cookies')


@app.route('/request-access', methods=['POST'])
def request_access():
    """Create a sales lead from the public bilingual landing page."""
    lang = request.form.get('lang', 'ru')
    if lang not in SUPPORTED_PUBLIC_LANGS:
        lang = 'ru'
    copy = PUBLIC_COPY[lang]
    token = request.form.get('csrf_token', '')
    cookie_token = request.cookies.get('public_csrf_token', '')
    if not cookie_token or not token or not compare_digest(token, cookie_token):
        return render_public(current_lang=lang, csrf_token=cookie_token or secrets.token_urlsafe(32),
                             lead_ok=False, lead_message=copy['connect_error']), 400

    name = request.form.get('name', '').strip()
    contact = request.form.get('contact', '').strip()
    points = request.form.get('points', '').strip()
    message = request.form.get('message', '').strip()
    consent = request.form.get('consent') == '1'
    allowed_points = {'1', '2-4', '5-9', '10+'}
    if not name or not contact or points not in allowed_points or not consent:
        return render_public(current_lang=lang, csrf_token=cookie_token,
                             lead_ok=False, lead_message=copy['connect_error']), 400

    try:
        run_async(db.init_db())
        lead_id = run_async(db.create_lead_request(
            name=name[:120], contact=contact[:160], points=points,
            message=message[:1000], language=lang,
        ))
        logger.info('New public lead %s received for %s points', lead_id, points)
    except Exception:
        logger.exception('Could not store public lead')
        return render_public(current_lang=lang, csrf_token=cookie_token,
                             lead_ok=False, lead_message=copy['connect_error']), 500

    return render_public(current_lang=lang, csrf_token=cookie_token,
                         lead_ok=True, lead_message=copy['connect_success'])


@app.route('/workspace')
@login_required
def workspace():
    """New owner workspace, isolated from the legacy VPN dashboard."""
    data = platform_store.dashboard(request.user['id'])
    if data:
        data['payroll'] = workspace_payroll_snapshot(data, request.args.get('payroll_period', ''))
        data['network_overview'] = workspace_network_overview(data)
        data['audit'] = workspace_audit_feed(data)
    return render_template(
        'workspace.html',
        user=request.user,
        workspace=data,
        csrf_token=workspace_csrf_token(),
        automation_presets=AUTOMATION_PRESETS,
    )


def workspace_payroll_snapshot(workspace: dict, requested_period: str = '') -> list[dict]:
    """Read a selected payroll period from legacy chats visible in the workspace."""
    if workspace.get('viewer_role') == 'employee':
        return []
    try:
        periods = run_async(db.get_historical_periods(6))
    except Exception:
        logger.exception('Could not determine payroll periods')
        return []
    if not periods:
        return []

    period_options = [
        {
            'key': period_start.date().isoformat(),
            'label': f'{period_start:%d.%m.%Y}–{period_end:%d.%m.%Y}',
        }
        for period_start, period_end in periods
    ]
    period_by_key = {
        option['key']: period
        for option, period in zip(period_options, periods)
    }
    selected_key = requested_period if requested_period in period_by_key else period_options[0]['key']
    period_start, period_end = period_by_key[selected_key]
    workspace['payroll_periods'] = period_options
    workspace['payroll_period_key'] = selected_key
    include_active = selected_key == period_options[0]['key']

    snapshots = []
    for point in workspace.get('points', []):
        chat_id = point.get('telegram_chat_id')
        if not chat_id:
            continue
        try:
            workers = run_async(db.get_chat_stats_for_period(
                chat_id, period_start, include_active=include_active, use_chat_rate=False
            ))
            shift_history = run_async(db.get_shift_history_for_period(
                chat_id, period_start, include_active=include_active
            ))
        except Exception:
            logger.exception('Could not load payroll for workspace point %s', point.get('id'))
            continue
        snapshots.append({
            'point_id': point['id'],
            'point_name': point['name'],
            'period_start': period_start.strftime('%d.%m.%Y'),
            'period_end': period_end.strftime('%d.%m.%Y'),
            'workers': workers,
            'shift_history': shift_history,
            'total_hours': sum(float(worker.get('total_hours') or 0) for worker in workers),
            'total_earned': sum(float(worker.get('total_earned') or 0) for worker in workers),
        })
    return snapshots


# Human-readable labels for platform_audit_events.action / .entity_type.
# Deliberately a plain dict, not a model: any action/entity code the store
# starts writing that isn't listed here still renders (as its raw code)
# instead of crashing or silently dropping the event — see
# workspace_audit_feed()'s .get(..., fallback) below.
AUDIT_ACTION_LABELS = {
    'owner_activated': 'Активация рабочего пространства',
    'subscription_renewed': 'Продление подписки',
    'point_rate_updated': 'Обновлена общая ставка ПВЗ',
    'member_rate_updated': 'Обновлена ставка участника',
    'automation_updated': 'Изменены сценарии автоматизации',
    'point_created': 'Добавлен ПВЗ',
    'point_access_granted': 'Открыт доступ к ПВЗ',
    'point_access_revoked': 'Закрыт доступ к ПВЗ',
    'point_schedule_updated': 'Изменено расписание ПВЗ',
    'invite_accepted': 'Принято приглашение',
    'staff_registration_updated': 'Изменена настройка регистрации сотрудников',
    'chat_connected': 'Подключён Telegram-чат',
}
AUDIT_ENTITY_LABELS = {
    'organization': 'сеть',
    'subscription': 'подписка',
    'point': 'ПВЗ',
    'automation': 'автоматизация',
    'invite': 'приглашение',
}


def workspace_audit_feed(workspace: dict) -> list[dict]:
    """Enrich platform_store.dashboard()'s raw audit trail (already scoped
    to owner/manager there — empty for employees) with human-readable
    action/entity labels and the actor's username, resolved from the
    legacy web_users table. Never invents an actor: a row whose
    actor_user_id can't be resolved falls back to a plain '#<id>' rather
    than a fabricated name.
    """
    events = workspace.get('audit') or []
    if not events:
        return []
    actor_ids = {event['actor_user_id'] for event in events if event.get('actor_user_id')}
    actor_names: dict[int, str] = {}
    for actor_id in actor_ids:
        try:
            actor = run_async(db.get_web_user_by_id(actor_id))
        except Exception:
            logger.exception('Could not resolve audit actor %s', actor_id)
            actor = None
        actor_names[actor_id] = actor['username'] if actor else f'#{actor_id}'
    feed = []
    for event in events:
        actor_id = event.get('actor_user_id')
        feed.append({
            **event,
            'action_label': AUDIT_ACTION_LABELS.get(event['action'], event['action']),
            'entity_label': AUDIT_ENTITY_LABELS.get(event['entity_type'], event['entity_type']),
            'actor_label': actor_names.get(actor_id, f'#{actor_id}') if actor_id else '—',
        })
    return feed


def _point_operational_state(point: dict, active_shift_by_chat: dict, now: datetime) -> dict:
    """Derive one point's live operational state from real signals only:
    whether a Telegram chat is connected, whether a shift is actually open
    right now (legacy shifts table), and the point's own configured work
    window (reusing the exact same overnight-aware window calculation the
    legacy shift-open logic uses, so this never disagrees with what
    start_shift() itself would decide). Never fabricates a status the
    backend has no way to know.
    """
    chat_id = point.get('telegram_chat_id')
    if not chat_id:
        return {
            'point_id': point['id'],
            'point_name': point['name'],
            'state': 'not-connected',
            'detail': 'Telegram-чат не подключён — статус смены недоступен.',
        }
    active = active_shift_by_chat.get(chat_id)
    if active:
        started = active.get('actual_start_time') or active.get('start_time') or ''
        since = f' · с {started[11:16]}' if len(started) >= 16 else ''
        return {
            'point_id': point['id'],
            'point_name': point['name'],
            'state': 'open',
            'detail': f'Смена открыта{since}.',
        }
    within_hours = False
    schedule = f"{point.get('work_time_start', '?')}–{point.get('work_time_end', '?')}"
    try:
        start_time = datetime.strptime(point['work_time_start'], '%H:%M').time()
        end_time = datetime.strptime(point['work_time_end'], '%H:%M').time()
        window_start, window_end = _get_work_window(now, start_time, end_time)
        within_hours = window_start <= now < window_end
    except (KeyError, TypeError, ValueError):
        pass
    if within_hours:
        return {
            'point_id': point['id'],
            'point_name': point['name'],
            'state': 'attention',
            'detail': f'Сейчас рабочее время ({schedule}), но смена не открыта.',
        }
    return {
        'point_id': point['id'],
        'point_name': point['name'],
        'state': 'closed',
        'detail': f'Вне расписания ({schedule}).',
    }


def workspace_network_overview(workspace: dict) -> dict:
    """A truthful live snapshot of every point visible to this viewer —
    already role-scoped upstream by platform_store (_visible_point_ids), so
    a manager/employee only ever sees their own assigned points here too.
    State is derived only from data that actually exists: a Telegram chat
    link, the legacy shifts table, and the point's own schedule — never a
    simulated "online" status.
    """
    points = workspace.get('points') or []
    if not points:
        return {'points': [], 'counts': {'total': 0, 'operating': 0, 'attention': 0}}
    try:
        active_shifts = run_async(db.get_all_active_shifts())
    except Exception:
        logger.exception('Could not load active shifts for network overview')
        active_shifts = []
    active_by_chat = {shift['chat_id']: shift for shift in active_shifts}
    now = datetime.now(MSK)
    cards = [_point_operational_state(point, active_by_chat, now) for point in points]
    counts = {
        'total': len(cards),
        'operating': sum(1 for card in cards if card['state'] == 'open'),
        'attention': sum(1 for card in cards if card['state'] in ('attention', 'not-connected')),
    }
    return {'points': cards, 'counts': counts}


def workspace_redirect(message: str, category: str = 'error'):
    flash(message, category)
    return redirect(url_for('workspace'))


def sync_workspace_chat_to_legacy(owner_user_id: int, point_id: int) -> None:
    """Keep the legacy bot tables usable while the platform schema is migrated."""
    runtime = platform_store.chat_runtime_config(owner_user_id, point_id)
    chat_id = runtime['telegram_chat_id']
    chat_name = runtime['telegram_chat_title'] or runtime['name']
    chat_number = run_async(db.add_chat(chat_id, chat_name, 'work'))
    settings = run_async(db.get_chat_settings(chat_id))
    # The legacy bot needs a work window to accept a shift.  The platform point
    # owns the schedule; this sync is transitional until the bot uses platform DB.
    start_minutes = int(runtime['work_time_start'][:2]) * 60 + int(runtime['work_time_start'][3:])
    end_minutes = int(runtime['work_time_end'][:2]) * 60 + int(runtime['work_time_end'][3:])
    duration_minutes = (end_minutes - start_minutes) % (24 * 60)
    daily_salary = runtime['amount_minor'] / 100 * (duration_minutes / 60)
    work_time = f"{runtime['work_time_start']}-{runtime['work_time_end']}"
    if (not settings or not settings.get('work_time_start') or
            settings.get('work_time_start') != runtime['work_time_start'] or
            settings.get('work_time_end') != runtime['work_time_end'] or
            runtime['amount_minor']):
        run_async(db.set_chat_settings(chat_number, chat_id, work_time, daily_salary))


@app.route('/workspace/activate', methods=['POST'])
@login_required
def workspace_activate():
    if not valid_workspace_csrf():
        return workspace_redirect('Сессия формы истекла, обновите страницу')
    if not auth_rate_limit_allowed('workspace-activate', str(request.user['id']), 8, 3600):
        return workspace_redirect('Слишком много попыток активации. Попробуйте позже.'), 429
    try:
        platform_store.activate_owner(
            request.user['id'], request.user['username'], request.form.get('code', '')
        )
        return workspace_redirect('Рабочее пространство активировано', 'success')
    except ValueError as exc:
        return workspace_redirect(str(exc))


@app.route('/workspace/renew', methods=['POST'])
@login_required
def workspace_renew():
    if not valid_workspace_csrf():
        return workspace_redirect('Сессия формы истекла, обновите страницу')
    if not auth_rate_limit_allowed('workspace-renew', str(request.user['id']), 8, 3600):
        return workspace_redirect('Слишком много попыток продления. Попробуйте позже.'), 429
    try:
        platform_store.renew_subscription(
            request.user['id'], request.form.get('code', ''), int(request.form.get('days', '30'))
        )
        return workspace_redirect('Подписка продлена', 'success')
    except (ValueError, PermissionError) as exc:
        return workspace_redirect(str(exc))


@app.route('/workspace/point', methods=['POST'])
@login_required
def workspace_point():
    if not valid_workspace_csrf():
        return workspace_redirect('Сессия формы истекла, обновите страницу')
    try:
        platform_store.create_point(
            request.user['id'], request.form.get('name', ''), request.form.get('address', '')
        )
        return workspace_redirect('ПВЗ добавлен', 'success')
    except (ValueError, PermissionError) as exc:
        return workspace_redirect(str(exc))


@app.route('/workspace/chat', methods=['POST'])
@login_required
def workspace_chat():
    if not valid_workspace_csrf():
        return workspace_redirect('Сессия формы истекла, обновите страницу')
    try:
        point_id = int(request.form.get('point_id', '0'))
        platform_store.set_point_schedule(
            request.user['id'], point_id,
            request.form.get('start_time', '09:00'),
            request.form.get('end_time', '18:00'),
        )
        platform_store.connect_chat(
            request.user['id'],
            point_id,
            int(request.form.get('chat_id', '0')),
            request.form.get('chat_title', ''),
        )
        sync_workspace_chat_to_legacy(request.user['id'], point_id)
        return workspace_redirect('Telegram-чат подключён к ПВЗ', 'success')
    except (ValueError, PermissionError) as exc:
        return workspace_redirect(str(exc))


@app.route('/workspace/invite', methods=['POST'])
@login_required
def workspace_invite():
    if not valid_workspace_csrf():
        return workspace_redirect('Сессия формы истекла, обновите страницу')
    try:
        token = platform_store.create_invite(
            request.user['id'], request.form.get('contact', ''), request.form.get('role', 'employee')
        )
        flash(f'Одноразовая ссылка-приглашение: /invite/{token}', 'success')
        return redirect(url_for('workspace'))
    except (ValueError, PermissionError) as exc:
        return workspace_redirect(str(exc))


@app.route('/workspace/staff-registration', methods=['POST'])
@login_required
def workspace_staff_registration():
    if not valid_workspace_csrf():
        return workspace_redirect('Сессия формы истекла, обновите страницу')
    try:
        platform_store.set_staff_registration_enabled(
            request.user['id'], request.form.get('enabled') == '1'
        )
        return workspace_redirect('Настройка регистрации сотрудников сохранена', 'success')
    except (ValueError, PermissionError) as exc:
        return workspace_redirect(str(exc))


@app.route('/workspace/finance', methods=['POST'])
@login_required
def workspace_finance():
    if not valid_workspace_csrf():
        return workspace_redirect('Сессия формы истекла, обновите страницу')
    try:
        platform_store.add_finance_entry(
            request.user['id'],
            int(request.form.get('point_id', '0')),
            request.form.get('kind', 'expense'),
            request.form.get('category', ''),
            money_to_minor(request.form.get('amount', '')),
            request.form.get('period_month', ''),
            request.form.get('note', ''),
        )
        return workspace_redirect('Финансовая запись сохранена', 'success')
    except (ValueError, PermissionError) as exc:
        return workspace_redirect(str(exc))


@app.route('/workspace/finance.csv')
@login_required
def workspace_finance_export():
    rows = platform_store.finance_export(request.user['id'])
    if rows is None:
        abort(404)
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['ПВЗ', 'Месяц', 'Тип', 'Категория', 'Сумма, ₽', 'Комментарий', 'Источник', 'Создано'])
    for row in rows:
        writer.writerow([
            row['point_name'],
            row['period_month'],
            'Доход' if row['kind'] == 'income' else 'Расход',
            row['category'],
            f"{row['amount_minor'] / 100:.2f}",
            row['note'],
            row['source'],
            row['created_at'],
        ])
    response = make_response('\ufeff' + output.getvalue())
    response.headers['Content-Type'] = 'text/csv; charset=utf-8'
    response.headers['Content-Disposition'] = 'attachment; filename="takt-finance.csv"'
    return response


@app.route('/workspace/payroll.csv')
@login_required
def workspace_payroll_export():
    workspace_data = platform_store.dashboard(request.user['id'])
    if workspace_data is None:
        abort(404)
    if workspace_data.get('viewer_role') == 'employee':
        abort(403)
    snapshots = workspace_payroll_snapshot(
        workspace_data, request.args.get('payroll_period', '')
    )
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['ПВЗ', 'Период', 'Сотрудник', 'Часы', 'Сумма, ₽', 'Смен'])
    for snapshot in snapshots:
        period = f"{snapshot['period_start']}–{snapshot['period_end']}"
        for worker in snapshot['workers']:
            writer.writerow([
                snapshot['point_name'],
                period,
                worker.get('full_name') or worker.get('username') or f"Telegram #{worker['user_id']}",
                f"{float(worker.get('total_hours') or 0):.2f}",
                f"{float(worker.get('total_earned') or 0):.2f}",
                worker.get('shifts_count', ''),
            ])
    response = make_response('\ufeff' + output.getvalue())
    response.headers['Content-Type'] = 'text/csv; charset=utf-8'
    response.headers['Content-Disposition'] = 'attachment; filename="takt-payroll.csv"'
    return response


@app.route('/workspace/rate', methods=['POST'])
@login_required
def workspace_rate():
    if not valid_workspace_csrf():
        return workspace_redirect('Сессия формы истекла, обновите страницу')
    try:
        point_id = int(request.form.get('point_id', '0'))
        amount_minor = money_to_minor(request.form.get('amount', ''))
        member_value = request.form.get('member_user_id', '').strip()
        if member_value:
            platform_store.set_member_rate(
                request.user['id'], int(member_value), point_id, amount_minor,
                request.form.get('visibility', 'personal'),
            )
        else:
            platform_store.set_point_rate(request.user['id'], point_id, amount_minor)
            # Keep the transitional bot's public hourly rate aligned with the
            # platform setting when the chat has already been connected.
            try:
                sync_workspace_chat_to_legacy(request.user['id'], point_id)
            except ValueError:
                pass
        return workspace_redirect('Ставка сохранена', 'success')
    except (ValueError, PermissionError) as exc:
        return workspace_redirect(str(exc))


@app.route('/workspace/access', methods=['POST'])
@login_required
def workspace_access():
    if not valid_workspace_csrf():
        return workspace_redirect('Сессия формы истекла, обновите страницу')
    try:
        platform_store.set_point_access(
            request.user['id'],
            int(request.form.get('member_user_id', '0')),
            int(request.form.get('point_id', '0')),
            request.form.get('granted') == '1',
        )
        return workspace_redirect('Доступ к ПВЗ обновлён', 'success')
    except (ValueError, PermissionError) as exc:
        return workspace_redirect(str(exc))


@app.route('/workspace/automation', methods=['POST'])
@login_required
def workspace_automation():
    if not valid_workspace_csrf():
        return workspace_redirect('Сессия формы истекла, обновите страницу')
    try:
        preset = request.form.get('preset', 'custom')
        if preset in AUTOMATION_PRESETS and request.form.get('apply_preset') == '1':
            selected = AUTOMATION_PRESETS[preset]
            commands = selected['commands']
            notifications = selected['notifications']
        else:
            commands = {
                'start_shift': request.form.get('start_shift', ''),
                'end_shift': request.form.get('end_shift', ''),
                'handover': request.form.get('handover', ''),
            }
            notifications = {
                'employee_payroll_days_before': int(request.form.get('employee_payroll_days_before', '0')),
                'manager_payroll_days_before': int(request.form.get('manager_payroll_days_before', '0')),
                'owner_payroll_days_before': int(request.form.get('owner_payroll_days_before', '0')),
                'employee_payroll_time': request.form.get('employee_payroll_time', '10:00'),
                'manager_payroll_time': request.form.get('manager_payroll_time', '10:00'),
                'owner_payroll_time': request.form.get('owner_payroll_time', '10:00'),
                'shift_alert_minutes': int(request.form.get('shift_alert_minutes', '0')),
            }
            preset = 'custom'
        platform_store.update_automation_settings(request.user['id'], preset, commands, notifications)
        return workspace_redirect('Сценарии и уведомления сохранены', 'success')
    except (ValueError, PermissionError) as exc:
        return workspace_redirect(str(exc))


@app.route('/invite/<token>', methods=['GET', 'POST'])
def accept_invite(token):
    """Accept a one-time workspace invitation for the signed-in account."""
    if len(token) > 100:
        abort(404)
    invite = platform_store.get_invite(token)
    user = None
    session_token = request.cookies.get('session_token')
    if session_token:
        user = run_async(db.get_user_by_session(session_token))
    if request.method == 'POST':
        if not user:
            return redirect(url_for('login'))
        if not valid_workspace_csrf():
            flash('Сессия формы истекла, обновите страницу', 'error')
        else:
            try:
                platform_store.accept_invite(user['id'], token)
                flash('Вы присоединились к рабочему пространству', 'success')
                return redirect(url_for('workspace'))
            except ValueError as exc:
                flash(str(exc), 'error')
    return render_template(
        'invite.html', token=token, user=user, invite=invite, csrf_token=workspace_csrf_token()
    )

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.cookies.get('session_token'):
        # Если уже есть кука, проверяем ее валидность
        try:
            token = request.cookies.get('session_token')
            user = run_async(db.get_user_by_session(token))
            if user:
                return redirect(url_for('workspace'))
        except Exception as e:
            logger.error(f"Error checking session: {e}", exc_info=True)

    if request.method == 'POST':
        if not valid_auth_csrf():
            flash('Сессия формы истекла, обновите страницу', 'error')
            return render_template('login.html', user=None)
        if not auth_rate_limit_allowed('login', request.form.get('username', '')):
            flash('Слишком много попыток. Попробуйте позже.', 'error')
            return render_template('login.html', user=None), 429
        try:
            username = request.form.get('username')
            password = request.form.get('password')
            
            if not username or not password:
                flash('Заполните все поля', 'error')
                return render_template('login.html', user=None)
            
            user = run_async(db.get_web_user(username))
            
            if user and check_password_hash(user['password_hash'], password):
                # Do not carry pre-authentication CSRF/session state into the
                # authenticated browser session.
                session.clear()
                token = secrets.token_hex(32)
                expires_at = datetime.now() + timedelta(days=7)
                
                run_async(db.create_web_session(
                    web_user_id=user['id'],
                    token=token,
                    expires_at=expires_at,
                    ip=request.remote_addr,
                    user_agent=request.user_agent.string
                ))
                
                resp = make_response(redirect(url_for('workspace')))
                resp.set_cookie(
                    'session_token', token, expires=expires_at, httponly=True,
                    secure=SESSION_COOKIE_SECURE, samesite='Lax', path='/',
                )
                return resp
            
            flash('Неверный логин или пароль', 'error')
        except Exception as e:
            logger.error(f"Login error: {e}", exc_info=True)
            flash('Не удалось выполнить вход. Проверьте данные и повторите попытку.', 'error')
        
    return render_template('login.html', user=None)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.cookies.get('session_token'):
        return redirect(url_for('workspace'))

    invite_token = request.values.get('invite', '').strip()
    invite = platform_store.get_invite(invite_token) if invite_token else None
    if invite_token and not invite:
        flash('Приглашение не найдено или истекло', 'error')
        invite_token = ''

    def render_register():
        return render_template(
            'register.html', user=None, invite_token=invite_token, invite=invite
        )

    request.user = None

    if request.method == 'POST':
        if not valid_auth_csrf():
            flash('Сессия формы истекла, обновите страницу', 'error')
            return render_register()
        if not auth_rate_limit_allowed('register', request.form.get('username', ''), 5, 3600):
            flash('Слишком много попыток регистрации. Попробуйте позже.', 'error')
            return render_register(), 429
        try:
            username = request.form.get('username', '').strip()
            password = request.form.get('password')
            confirm_password = request.form.get('confirm_password')
            owner_code = request.form.get('owner_code', '')
            posted_invite_token = request.form.get('invite_token', '').strip()
            invite = platform_store.get_invite(posted_invite_token) if posted_invite_token else None
            invite_token = posted_invite_token if invite else ''
            answer1 = request.form.get('answer1', '').strip()
            answer2 = request.form.get('answer2', '').strip()
            answer3 = request.form.get('answer3', '').strip()
            
            if not username or not password:
                flash('Заполните все поля', 'error')
                return render_register()

            if not REGISTRATION_USERNAME_RE.fullmatch(username):
                flash('Логин должен содержать 3–64 символа: буквы, цифры, точку, дефис или подчёркивание', 'error')
                return render_register()

            if not REGISTRATION_PASSWORD_MIN_LENGTH <= len(password) <= REGISTRATION_PASSWORD_MAX_LENGTH:
                flash('Пароль должен содержать от 12 до 256 символов', 'error')
                return render_register()
            
            if not answer1 or not answer2 or not answer3:
                flash('Ответьте на все секретные вопросы', 'error')
                return render_register()

            if not invite and not platform_store.owner_code_is_available(owner_code):
                flash('Для регистрации владельца нужен действующий код после оплаты подключения', 'error')
                return render_register()
            
            if password != confirm_password:
                flash('Пароли не совпадают', 'error')
                return render_register()
            
            # Проверяем, не занят ли логин
            existing_user = run_async(db.get_web_user(username))
            if existing_user:
                flash('Этот логин уже занят', 'error')
                return render_register()
            
            password_hash = generate_password_hash(password)
            
            # Создаем пользователя с ролью guest (гость)
            web_user_id = run_async(db.create_web_user(
                username=username,
                password_hash=password_hash,
                role='guest'
            ))
            
            # Сохраняем секретные вопросы
            from vpn_config import SECURITY_QUESTIONS
            questions_answers = [
                (SECURITY_QUESTIONS[0], answer1),
                (SECURITY_QUESTIONS[1], answer2),
                (SECURITY_QUESTIONS[2], answer3)
            ]
            run_async(db.save_security_questions(web_user_id, questions_answers))
            if invite:
                platform_store.accept_invite(web_user_id, invite_token)
                flash('Аккаунт создан. Войдите, чтобы открыть приглашённое рабочее пространство.', 'success')
            else:
                platform_store.activate_owner(web_user_id, username, owner_code)
                flash('Регистрация владельца завершена. Войдите в кабинет.', 'success')
            return redirect(url_for('login'))
            
        except Exception as e:
            logger.error(f"Error registering user: {e}", exc_info=True)
            flash('Не удалось завершить регистрацию. Проверьте данные и попробуйте ещё раз.', 'error')
            
    return render_register()

@app.route('/password-reset', methods=['GET', 'POST'])
def password_reset():
    """Запрос на сброс пароля"""
    if request.method == 'POST':
        if not valid_auth_csrf():
            flash('Сессия формы истекла, обновите страницу', 'error')
            return render_template('password_reset.html')
        if not auth_rate_limit_allowed('password-reset', request.form.get('username', ''), 5, 3600):
            flash('Слишком много запросов. Попробуйте позже.', 'error')
            return render_template('password_reset.html'), 429
        try:
            username = request.form.get('username', '').strip()
            telegram_contact = request.form.get('telegram_contact', '').strip()
            answer1 = request.form.get('answer1', '').strip()
            answer2 = request.form.get('answer2', '').strip()
            answer3 = request.form.get('answer3', '').strip()
            
            if not all([username, telegram_contact, answer1, answer2, answer3]):
                flash('Заполните все поля', 'error')
                return render_template('password_reset.html')
            
            # Проверяем ответы на секретные вопросы
            answers = [answer1, answer2, answer3]
            if run_async(db.verify_security_answers(username, answers)):
                # Создаем запрос на сброс
                request_id = run_async(db.create_password_reset_request(username, telegram_contact, answers))
                
                # Отправляем уведомление владельцу
                from config import OWNER_TELEGRAM_ID
                if OWNER_TELEGRAM_ID:
                    logger.info(
                        "Password reset request %s received for %s; sensitive answers omitted",
                        request_id,
                        username,
                    )
                
                flash('Запрос отправлен! Владелец свяжется с вами для подтверждения.', 'success')
                return redirect(url_for('login'))
            else:
                flash('Неверные ответы на секретные вопросы', 'error')
                
        except Exception as e:
            logger.error(f"Error in password reset: {e}", exc_info=True)
            flash('Не удалось создать запрос на восстановление. Попробуйте ещё раз.', 'error')
    
    return render_template('password_reset.html')

@app.route('/logout', methods=['POST'])
def logout():
    if not valid_auth_csrf():
        flash('Сессия формы истекла, обновите страницу', 'error')
        return redirect(url_for('dashboard'))
    token = request.cookies.get('session_token')
    if token:
        run_async(db.delete_web_session(token))

    resp = make_response(redirect(url_for('login')))
    resp.set_cookie(
        'session_token', '', expires=0, httponly=True,
        secure=SESSION_COOKIE_SECURE, samesite='Lax', path='/',
    )
    return resp

@app.route('/link', methods=['GET', 'POST'])
@login_required
def link_account():
    """Страница для связывания аккаунта с Telegram"""
    # Если уже связан, перенаправляем
    if request.user.get('telegram_linked'):
        flash('Ваш аккаунт уже связан с Telegram', 'info')
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        if not valid_auth_csrf():
            flash('Сессия формы истекла, обновите страницу', 'error')
            return render_template('link_account.html', user=request.user)
        if not auth_rate_limit_allowed('telegram-link', str(request.user['id']), 10, 3600):
            flash('Слишком много попыток связывания. Попробуйте позже.', 'error')
            return render_template('link_account.html', user=request.user), 429
        try:
            key_code = request.form.get('key_code', '').strip()
            
            if not key_code:
                flash('Введите ключ связывания', 'error')
                return render_template('link_account.html', user=request.user)
            
            # Проверяем ключ
            telegram_id = run_async(db.verify_link_key(key_code))
            
            if not telegram_id:
                flash('Неверный или истекший ключ. Получите новый ключ у бота командой /link', 'error')
                return render_template('link_account.html', user=request.user)
            
            # Проверяем что этот Telegram не привязан к другому аккаунту
            existing_link = run_async(db.check_telegram_already_linked(telegram_id))
            if existing_link:
                flash(f'Этот Telegram уже привязан к аккаунту "{existing_link["username"]}"', 'error')
                return render_template('link_account.html', user=request.user)
            
            # Связываем аккаунты
            run_async(db.link_telegram_account(request.user['id'], telegram_id))
            run_async(db.mark_key_used(key_code, request.user['id']))
            
            flash('Аккаунт успешно связан с Telegram! Теперь вам доступны дополнительные функции.', 'success')
            return redirect(url_for('dashboard'))
            
        except Exception as e:
            logger.error(f"Error linking account: {e}", exc_info=True)
            flash('Не удалось связать Telegram. Проверьте ключ и попробуйте ещё раз.', 'error')
    
    return render_template('link_account.html', user=request.user)

@app.route('/reset-telegram', methods=['GET', 'POST'])
@login_required
def reset_telegram():
    """Отвязка Telegram от аккаунта"""
    if request.method == 'POST':
        if not valid_auth_csrf():
            flash('Сессия формы истекла, обновите страницу', 'error')
            return render_template('reset_telegram.html', user=request.user)
        if not auth_rate_limit_allowed('telegram-reset', str(request.user['id']), 10, 3600):
            flash('Слишком много попыток отвязки. Попробуйте позже.', 'error')
            return render_template('reset_telegram.html', user=request.user), 429
        try:
            password = request.form.get('password', '').strip()
            answer1 = request.form.get('answer1', '').strip()
            answer2 = request.form.get('answer2', '').strip()
            answer3 = request.form.get('answer3', '').strip()
            
            if not all([password, answer1, answer2, answer3]):
                flash('Заполните все поля', 'error')
                return render_template('reset_telegram.html', user=request.user)
            
            # Проверяем пароль
            if not check_password_hash(request.user['password_hash'], password):
                flash('Неверный пароль', 'error')
                return render_template('reset_telegram.html', user=request.user)
            
            # Проверяем ответы на секретные вопросы
            answers = [answer1, answer2, answer3]
            if not run_async(db.verify_security_answers(request.user['username'], answers)):
                flash('Неверные ответы на секретные вопросы', 'error')
                return render_template('reset_telegram.html', user=request.user)
            
            # Отвязываем Telegram
            run_async(db.unlink_telegram(request.user['id']))
            
            flash('Telegram успешно отвязан! Вы можете привязать новый аккаунт.', 'success')
            return redirect(url_for('dashboard'))
            
        except Exception as e:
            logger.error(f"Error resetting Telegram: {e}", exc_info=True)
            flash('Не удалось отвязать Telegram. Попробуйте ещё раз.', 'error')
    
    return render_template('reset_telegram.html', user=request.user)

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html', user=request.user)

@app.route('/salary')
@login_required
@linked_required
def salary():
    """Страница зарплаты пользователя"""
    telegram_id = request.user.get('telegram_id')
    if not telegram_id:
        flash('Необходимо связать аккаунт с Telegram', 'error')
        return redirect(url_for('link_account'))
    
    # Получаем активную смену (для всех рабочих чатов пользователя)
    active_shift = None
    
    # Получаем статистику за текущий период
    period_start, period_end = db.get_current_period()  # НЕ async метод!
    stats = run_async(db.get_stats_for_period_by_dates(telegram_id, period_start, period_end))
    
    return render_template('salary.html', 
                         user=request.user,
                         active_shift=active_shift,
                         stats=stats,
                         period_start=period_start,
                         period_end=period_end)

@app.route('/vpn')
@login_required
def vpn():
    """Страница VPN магазина с новым калькулятором"""
    # Проверяем активный заказ
    has_active_order = False
    active_order = run_async(db.get_user_active_order(request.user['id']))
    if active_order:
        has_active_order = True
    
    return render_template('vpn_new.html',
                         user=request.user,
                         has_active_order=has_active_order)

@app.route('/vpn/order', methods=['POST'])
@login_required
def create_vpn_order():
    """Создание заказа VPN"""
    if not valid_auth_csrf():
        flash('Сессия формы истекла, обновите страницу', 'error')
        return redirect(url_for('vpn'))
    try:
        from vpn_config import calculate_vpn_price

        device_count = int(request.form.get('device_count', 1))
        period_days = int(request.form.get('period_days', 30))
        telegram_contact = request.form.get('telegram_contact', '').strip()
        
        if not telegram_contact:
            flash('Укажите ваш Telegram для связи', 'error')
            return redirect(url_for('vpn'))
        
        # Проверка бана по аккаунту
        ban = run_async(db.check_user_banned(web_user_id=request.user['id']))
        if ban:
            if ban['is_permanent']:
                flash(f'Вы заблокированы навсегда. Причина: {ban["reason"]}', 'error')
            else:
                flash(f'Вы заблокированы до {ban["expires_at"]}. Причина: {ban["reason"]}', 'error')
            return redirect(url_for('vpn'))
        
        # Проверка бана по Telegram username
        ban = run_async(db.check_user_banned(telegram_username=telegram_contact))
        if ban:
            if ban['is_permanent']:
                flash(f'Этот Telegram заблокирован навсегда. Причина: {ban["reason"]}', 'error')
            else:
                flash(f'Этот Telegram заблокирован до {ban["expires_at"]}. Причина: {ban["reason"]}', 'error')
            return redirect(url_for('vpn'))
        
        # Проверка частоты заказов
        can_order = run_async(db.check_user_last_order_time(request.user['id']))
        if not can_order:
            flash('Подождите 2 минуты перед созданием нового заказа', 'error')
            return redirect(url_for('vpn'))
        
        # Расчет цены
        total_price = calculate_vpn_price(device_count, period_days)
        
        # Создаем заказ
        order_id = run_async(db.create_vpn_order(
            request.user['id'],
            device_count,
            period_days,
            total_price,
            telegram_contact
        ))
        
        # Отправляем уведомление владельцу в Telegram
        from config import OWNER_TELEGRAM_ID
        if OWNER_TELEGRAM_ID:
            notification_text = (
                f"🛒 <b>Новый заказ VPN #{order_id}</b>\n\n"
                f"👤 Пользователь: {request.user['username']}\n"
                f"📱 Telegram: {telegram_contact}\n"
                f"🖥️ Устройств: {device_count}\n"
                f"📅 Период: {period_days} дней\n"
                f"💰 Сумма: {total_price} ₽\n\n"
                f"Свяжитесь с клиентом для подтверждения оплаты."
            )
            # Отправка через глобальную функцию notify_owner
            try:
                run_async(notify_owner(notification_text))
            except Exception as e:
                logger.error(f"Failed to send Telegram notification: {e}")
        
        flash(f'Заказ #{order_id} создан! Владелец свяжется с вами для подтверждения.', 'success')
        return redirect(url_for('my_purchases'))
        
    except ValueError as e:
        flash(f'Ошибка в параметрах заказа: {e}', 'error')
    except Exception as e:
        logger.error(f"Error creating VPN order: {e}", exc_info=True)
        flash(f'Ошибка создания заказа: {e}', 'error')
    
    return redirect(url_for('vpn'))

@app.route('/my-purchases')
@login_required
def my_purchases():
    """Страница с покупками пользователя"""
    # Получаем все заказы пользователя
    orders = run_async(db.get_user_orders(request.user['id']))
    
    # Для каждого заказа получаем ключи
    for order in orders:
        order['vpn_keys'] = run_async(db.get_order_keys(order['id']))
    
    # TODO: Получить инструкции из настроек
    instructions = """
    <p>1. Скачайте приложение VPN на ваше устройство</p>
    <p>2. Откройте приложение и выберите "Добавить конфигурацию"</p>
    <p>3. Вставьте ваш ключ</p>
    <p>4. Нажмите "Подключиться"</p>
    """
    
    return render_template('my_purchases.html',
                         user=request.user,
                         orders=orders,
                         instructions=instructions)

def run_web_app():
    port = int(os.getenv('WEB_PORT', 5000))
    
    # Настройка SSL
    ssl_context = None
    cert_path = os.getenv('SSL_CERT')
    key_path = os.getenv('SSL_KEY')
    
    # SSL включаем только если явно заданы пути в переменных окружения
    if cert_path and key_path and os.path.exists(cert_path) and os.path.exists(key_path):
        ssl_context = (cert_path, key_path)
        logger.info(f"🔒 SSL включен: {cert_path}")
    else:
        logger.info("⚠️ SSL выключен (работаем через HTTP)")

    # Отключаем reloader, так как запускаем в потоке
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False, ssl_context=ssl_context)

if __name__ == '__main__':
    run_web_app()
