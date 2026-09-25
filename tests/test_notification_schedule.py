from datetime import datetime

from notification_schedule import active_period, reminder_due, subscription_reminder_due


def test_active_period_handles_both_halves_and_february():
    assert active_period(datetime(2026, 2, 10).date()) == (
        datetime(2026, 2, 1).date(),
        datetime(2026, 2, 15).date(),
    )
    assert active_period(datetime(2026, 2, 20).date())[1].day == 28


def test_reminder_due_respects_custom_day_and_time():
    settings = {
        "employee_payroll_days_before": 2,
        "employee_payroll_time": "18:30",
    }
    assert reminder_due(datetime(2026, 2, 13, 18, 29), "employee", settings) is None
    due = reminder_due(datetime(2026, 2, 13, 18, 30), "employee", settings)
    assert due[0].isoformat() == "2026-02-01"
    assert due[1].isoformat() == "2026-02-15"


def test_subscription_reminder_keys_are_stable():
    period_end = datetime(2026, 2, 15, 23, 59)
    assert subscription_reminder_due(datetime(2026, 2, 10, 10), period_end) == "5_days"
    assert subscription_reminder_due(datetime(2026, 2, 13, 9, 59), period_end) is None
    assert subscription_reminder_due(datetime(2026, 2, 16, 10), period_end) == "expired"
