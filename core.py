"""Genuinely shared, database-agnostic helpers used by both the public app
(``web_app.py``) and the admin/operator app (``admin_app.py`` /
``admin_routes.py``).

Deliberately does NOT hold a shared ``db``/``platform_store`` instance: the
public and admin surfaces are meant to run as separate processes, each with
its own database connection (pointed at the same underlying file/DSN for
now — see docs/security-baseline.md for the documented limitation). Sharing
a live Python object here would be a false economy that only works because
today's tests/dev run everything in one interpreter.
"""

from __future__ import annotations

import asyncio


def run_async(coro):
    """Run an async coroutine from sync Flask view code."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(coro)
            loop.close()
            return result
        return loop.run_until_complete(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(coro)
        loop.close()
        return result


# Static copy for the small set of custom error pages both apps use. Titles
# and messages are always static strings from this table — never built from
# request data, exception text, file paths, or anything else that could leak
# internals. The admin app always uses 'ru' (its UI is Russian-only today).
ERROR_COPY = {
    'ru': {
        404: ('Страница не найдена', 'Такой страницы нет или она была перемещена.'),
        403: ('Доступ запрещён', 'У вас нет прав для просмотра этой страницы.'),
        405: ('Метод не поддерживается', 'Этот адрес не принимает такой запрос.'),
        500: ('Что-то пошло не так', 'Мы уже знаем о проблеме. Попробуйте обновить страницу позже.'),
    },
    'en': {
        404: ('Page not found', 'This page does not exist or was moved.'),
        403: ('Access denied', 'You do not have permission to view this page.'),
        405: ('Method not allowed', 'This address does not accept that request.'),
        500: ('Something went wrong', 'We already know about this. Please try again later.'),
    },
}


def render_error_page(status_code: int, title: str, message: str, *, lang: str = 'ru', brand: str = 'Такт') -> str:
    """A minimal, fully self-contained error page: no Jinja, no external CSS
    or JS, no template inheritance. Keeping it standalone means it can never
    accidentally pull in a sidebar, nav links to routes that don't exist on
    this app, or any other app-specific template state — and it renders
    correctly even if the app is in a degraded state (DB down, etc).

    ``title``/``message`` must always be static, caller-supplied copy from
    ERROR_COPY — never interpolate exception text, tracebacks, file paths,
    or request-derived values into this page.
    """
    home_label = 'На главную' if lang == 'ru' else 'Back home'
    return f'''<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{brand} — {status_code}</title>
<style>
  :root{{color-scheme:dark}}
  *{{box-sizing:border-box}}
  body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
    background:#050505;color:#f2f0eb;font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Onest,sans-serif;padding:24px}}
  .card{{max-width:460px;text-align:center}}
  .code{{font:600 .78rem/1 "SFMono-Regular",Consolas,monospace;letter-spacing:.12em;
    color:#8ee6c1;text-transform:uppercase;margin-bottom:18px}}
  h1{{margin:0 0 14px;font-size:2rem;letter-spacing:-.03em;font-weight:650}}
  p{{margin:0 0 28px;color:#a6a6a3}}
  a{{display:inline-flex;padding:12px 20px;border:1px solid rgba(255,255,255,.2);
    border-radius:11px;color:#f2f0eb;text-decoration:none;font-weight:600;font-size:.85rem}}
  a:hover{{border-color:rgba(255,255,255,.4)}}
</style>
</head>
<body>
  <div class="card">
    <div class="code">{brand} · {status_code}</div>
    <h1>{title}</h1>
    <p>{message}</p>
    <a href="/">{home_label}</a>
  </div>
</body>
</html>'''


def register_error_handlers(app, *, lang_getter=None, logger=None) -> None:
    """Register 404/403/405/500 handlers that render ``render_error_page``.

    ``lang_getter`` is an optional zero-arg callable returning 'ru'/'en' for
    the current request (the public app passes its ``public_language``); the
    admin app omits it and always gets 'ru'. Never leaks stack traces, paths,
    or secrets — the 500 page text is always the same static safe message,
    in production and in local dev alike.
    """
    from werkzeug.exceptions import HTTPException

    def _lang() -> str:
        if lang_getter is None:
            return 'ru'
        try:
            value = lang_getter()
        except Exception:
            return 'ru'
        return value if value in ERROR_COPY else 'ru'

    def _page(status_code: int):
        lang = _lang()
        title, message = ERROR_COPY[lang][status_code]
        return render_error_page(status_code, title, message, lang=lang), status_code

    @app.errorhandler(404)
    def _handle_404(_e):
        return _page(404)

    @app.errorhandler(403)
    def _handle_403(_e):
        return _page(403)

    @app.errorhandler(405)
    def _handle_405(_e):
        return _page(405)

    @app.errorhandler(500)
    def _handle_500(_e):
        return _page(500)

    @app.errorhandler(Exception)
    def _handle_exception(e):
        if isinstance(e, HTTPException):
            # Let the specific handlers above (or Werkzeug, for anything we
            # did not explicitly cover) render it.
            return e
        if logger is not None:
            logger.error(f'Unhandled exception: {e}', exc_info=True)
        return _page(500)
