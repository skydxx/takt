import os
import secrets

from dotenv import load_dotenv

load_dotenv()

APP_ENV = os.getenv('APP_ENV', 'development').strip().lower()
FLASK_SECRET_KEY = os.getenv('FLASK_SECRET_KEY', '').strip()
if APP_ENV == 'production' and not FLASK_SECRET_KEY:
    raise RuntimeError('FLASK_SECRET_KEY must be set in production')
if not FLASK_SECRET_KEY:
    # Local-only fallback. Production is intentionally fail-fast above.
    FLASK_SECRET_KEY = secrets.token_urlsafe(32)

ADMIN_FLASK_SECRET_KEY = os.getenv('ADMIN_FLASK_SECRET_KEY', '').strip()
if APP_ENV == 'production' and not ADMIN_FLASK_SECRET_KEY:
    raise RuntimeError('ADMIN_FLASK_SECRET_KEY must be set in production')
if not ADMIN_FLASK_SECRET_KEY:
    # Local development fallback keeps the two apps usable without another
    # secret, while production must provide an independent signing key.
    ADMIN_FLASK_SECRET_KEY = FLASK_SECRET_KEY

ADMIN_SESSION_COOKIE_NAME = os.getenv(
    'ADMIN_SESSION_COOKIE_NAME', 'admin_session_token'
).strip() or 'admin_session_token'

SESSION_COOKIE_SECURE = os.getenv(
    'SESSION_COOKIE_SECURE',
    '1' if APP_ENV == 'production' else '0',
).strip().lower() in {'1', 'true', 'yes'}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Telegram Bot Token
BOT_TOKEN = os.getenv('BOT_TOKEN', '')

# IDs for operational notifications stay in the environment, not in source.
ERROR_NOTIFICATION_USER_ID = int(os.getenv('ERROR_NOTIFICATION_USER_ID', '0'))
OWNER_TELEGRAM_ID = int(os.getenv('OWNER_TELEGRAM_ID', '0'))

# ID пользователей с расширенными правами (5 человек) - СУПЕРАДМИНЫ
SUPER_ADMINS = [
    int(os.getenv('SUPER_ADMIN_1', 0)),
    int(os.getenv('SUPER_ADMIN_2', 0)),
    int(os.getenv('SUPER_ADMIN_3', 0)),
    int(os.getenv('SUPER_ADMIN_4', 0)),
    int(os.getenv('SUPER_ADMIN_5', 0))
]

WEB_PORT = int(os.getenv("WEB_PORT", 5000))
# The admin/operator WSGI process binds here — a distinct internal port from
# the public app. It should never be published directly to the internet;
# only a reverse proxy terminating ADMIN_HOST should reach it. See
# docker-compose.example.yml and docs/security-baseline.md.
ADMIN_WEB_PORT = int(os.getenv("ADMIN_WEB_PORT", 5001))
DOMAIN = os.getenv("DOMAIN", "localhost")

# Optional host isolation for the operator/admin surface (/admin*, /owner*,
# /backups*). Empty by default so local dev and the test suite keep working
# against a single host. Set to a dedicated hostname (optionally with a
# port, e.g. "admin.example.com" or "admin.example.com:8443") in production
# to require that exact host for every admin route; requests on any other
# host are redirected (HTTPS-to-HTTPS only) or rejected. See
# docs/security-baseline.md for the threat model — this is defense in
# depth, not a substitute for real network/reverse-proxy isolation.
ADMIN_HOST = os.getenv('ADMIN_HOST', '').strip()

# ID админов только для просмотра (могут смотреть статистику, но не менять настройки)
VIEW_ONLY_ADMINS = [
    # Добавьте сюда ID админов с правами только на просмотр
]

# ID пользователей для уведомлений об отсутствии смены
SHIFT_ALERT_USERS = [
    # Добавьте сюда ID людей, которым нужно отправлять уведомления
    # если через 15 минут после открытия никто не начал смену
]

# ID кураторского чата
CURATOR_CHAT_ID = int(os.getenv('CURATOR_CHAT_ID', 0)) if os.getenv('CURATOR_CHAT_ID') else None

# База данных (абсолютный путь). Overridable via env so the public and
# admin containers can point at the same file on a shared volume that lives
# outside either image's code directory (see docker-compose.example.yml).
DATABASE_PATH = os.getenv('DATABASE_PATH', '').strip() or os.path.join(BASE_DIR, 'salary_bot.db')
PLATFORM_DATABASE_PATH = os.getenv(
    'PLATFORM_DATABASE_PATH',
    os.path.join(BASE_DIR, 'platform.db'),
)
# Set this in production to a PostgreSQL DSN.  The path remains the local
# development default so existing SQLite test fixtures and installs continue
# to work without a database server.
PLATFORM_DATABASE_URL = os.getenv('PLATFORM_DATABASE_URL', '').strip()


def validate_platform_database_config(app_env: str, database_url: str) -> None:
    if app_env == 'production' and not database_url:
        raise RuntimeError('PLATFORM_DATABASE_URL must be set in production')


validate_platform_database_config(APP_ENV, PLATFORM_DATABASE_URL)
VPN_ENABLED = os.getenv('VPN_ENABLED', '0').strip().lower() in {'1', 'true', 'yes'}
