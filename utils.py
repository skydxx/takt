from datetime import datetime, timedelta
from pathlib import Path
from typing import Tuple
import re


def safe_path_within(base_dir, *parts: str) -> Path | None:
    """Resolve *parts* under *base_dir*, rejecting traversal and symlink escapes.

    Each part must be a single path segment (no ``/``, ``\\`` or ``..``); the
    final resolved path (symlinks included) must still live under the
    resolved *base_dir*. Returns ``None`` instead of raising so call sites can
    treat it like a plain "not found" lookup.
    """
    try:
        base = Path(base_dir).resolve(strict=False)
    except (OSError, RuntimeError):
        return None

    candidate = base
    for part in parts:
        if not part or part in ('.', '..') or '/' in part or '\\' in part or '\x00' in part:
            return None
        candidate = candidate / part

    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return None

    if resolved != base and base not in resolved.parents:
        return None
    return resolved


def format_time_delta(seconds: float) -> str:
    """Форматирование времени в читаемый вид"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    return f"{hours}ч {minutes}м"


def format_hours(hours: float) -> str:
    """Форматирование часов в читаемый вид"""
    h = int(hours)
    m = int((hours - h) * 60)
    return f"{h}ч {m}м"


def format_money(amount: float) -> str:
    """Форматирование денег"""
    return f"{amount:.2f}₽"


def parse_salary(salary_str: str) -> float:
    """Парсинг зарплаты из строки"""
    numbers = re.findall(r'\d+\.?\d*', salary_str)
    if numbers:
        return float(numbers[0])
    raise ValueError("Не удалось распарсить зарплату")


def get_period_dates() -> Tuple[datetime, datetime]:
    """Получение дат текущего периода"""
    now = datetime.now()
    year = now.year
    month = now.month
    day = now.day
    
    if day < 15:
        period_start = datetime(year, month, 1)
        period_end = datetime(year, month, 14, 23, 59, 59)
    else:
        period_start = datetime(year, month, 15)
        if month == 12:
            next_month = datetime(year + 1, 1, 1)
        else:
            next_month = datetime(year, month + 1, 1)
        period_end = next_month - timedelta(seconds=1)
    
    return period_start, period_end


def format_period(period_start: datetime) -> str:
    """Форматирование периода для отображения"""
    day = period_start.day
    month = period_start.month
    year = period_start.year
    
    if day == 1:
        return f"1-14.{month:02d}.{year}"
    else:
        last_day = get_last_day_of_month(year, month)
        return f"15-{last_day}.{month:02d}.{year}"


def get_last_day_of_month(year: int, month: int) -> int:
    """Получение последнего дня месяца"""
    if month == 12:
        next_month = datetime(year + 1, 1, 1)
    else:
        next_month = datetime(year, month + 1, 1)
    last_day = next_month - timedelta(days=1)
    return last_day.day


def format_worker_info(user_id: int, username: str = None, full_name: str = None) -> str:
    """Форматирование информации о работнике"""
    parts = [str(user_id)]
    if username:
        parts.append(f"@{username}")
    if full_name:
        parts.append(full_name)
    return ", ".join(parts)
