"""Pure scheduling helpers for payroll reminders."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, time, timedelta

ROLE_KEYS = {
    "employee": "employee_payroll_days_before",
    "manager": "manager_payroll_days_before",
    "owner": "owner_payroll_days_before",
}


def subscription_reminder_due(
    now: datetime, period_end: datetime, send_at: time = time(10, 0)
) -> str | None:
    """Return a stable reminder key for subscription expiry notifications."""
    if now.time().replace(tzinfo=None) < send_at:
        return None
    days_left = (period_end.date() - now.date()).days
    if days_left == 5:
        return "5_days"
    if days_left == 2:
        return "2_days"
    if days_left == 0:
        return "last_day"
    if days_left < 0:
        return "expired"
    return None


def active_period(day: date) -> tuple[date, date]:
    """Return the payroll period containing *day*."""
    if day.day <= 15:
        return day.replace(day=1), day.replace(day=15)
    last = monthrange(day.year, day.month)[1]
    return day.replace(day=16), day.replace(day=last)


def reminder_due(
    now: datetime,
    role: str,
    notifications: dict[str, int | str],
) -> tuple[date, date, time] | None:
    """Return period and local send time when a role reminder is due now."""
    key = ROLE_KEYS.get(role)
    if not key or key not in notifications:
        return None
    try:
        days_before = int(notifications[key])
        send_at = time.fromisoformat(str(notifications.get(f"{role}_payroll_time", "10:00")))
    except (TypeError, ValueError):
        return None
    period_start, period_end = active_period(now.date())
    if now.date() != period_end - timedelta(days=days_before):
        return None
    if now.time().replace(tzinfo=None) < send_at:
        return None
    return period_start, period_end, send_at
