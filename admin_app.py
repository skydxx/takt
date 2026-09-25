"""Admin/operator WSGI app — the real deployment target for /admin*,
/owner*, /backups*. Runs as its own process/container, bound to its own
port, published only through ADMIN_HOST behind a reverse proxy (see
docker-compose.example.yml). It does not import web_app.py and does not
register any public/workspace route — a request for a public path here
simply 404s via the same custom error page as anything else unknown.
"""

from __future__ import annotations

import logging
import os

from flask import Flask

import admin_routes
from config import ADMIN_FLASK_SECRET_KEY, ADMIN_WEB_PORT, APP_ENV, SESSION_COOKIE_SECURE
from core import register_error_handlers

app = Flask(__name__)
app.secret_key = ADMIN_FLASK_SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=SESSION_COOKIE_SECURE,
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,
)

logger = logging.getLogger('admin_app')

# Same baseline as the public app: no third-party origins, one documented
# 'unsafe-inline' exception for the legacy admin templates' inline
# onclick/oninput handlers and style="" attributes.
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
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    response.headers.setdefault('Content-Security-Policy', CONTENT_SECURITY_POLICY)
    if APP_ENV == 'production' and SESSION_COOKIE_SECURE:
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    return response


admin_routes.register_admin_routes(app)
register_error_handlers(app, logger=logger)  # admin UI is Russian-only, no lang_getter needed


def run_admin_app(backup_mgr=None):
    if backup_mgr:
        admin_routes.set_backup_manager(backup_mgr)

    port = ADMIN_WEB_PORT

    ssl_context = None
    cert_path = os.getenv('ADMIN_SSL_CERT') or os.getenv('SSL_CERT')
    key_path = os.getenv('ADMIN_SSL_KEY') or os.getenv('SSL_KEY')
    if cert_path and key_path and os.path.exists(cert_path) and os.path.exists(key_path):
        ssl_context = (cert_path, key_path)
        logger.info(f'🔒 SSL включен: {cert_path}')
    else:
        logger.info('⚠️ SSL выключен (работаем через HTTP) — рассчитываем на reverse proxy перед админкой')

    # Admin binds to localhost by default — it must only be reachable via a
    # reverse proxy in front of it (see docker-compose.example.yml), never
    # exposed directly. Set ADMIN_BIND_HOST=0.0.0.0 explicitly if the
    # reverse proxy runs in a separate container and needs to reach this
    # one over the network.
    bind_host = os.getenv('ADMIN_BIND_HOST', '127.0.0.1')
    app.run(host=bind_host, port=port, debug=False, use_reloader=False, ssl_context=ssl_context)


if __name__ == '__main__':
    run_admin_app()
