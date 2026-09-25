import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from notification_service import NotificationService

MSK = ZoneInfo("Europe/Moscow")


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, telegram_id, text):
        self.messages.append((telegram_id, text))


class FakeDb:
    async def get_web_user_by_id(self, user_id):
        return {"telegram_id": user_id + 1000}


class FakeStore:
    def __init__(self):
        self.payroll_claims = set()
        self.subscription_claims = set()

    def notification_targets(self):
        return [{
            "organization_id": 1,
            "organization_name": "Сеть",
            "role": "employee",
            "web_user_id": 7,
            "settings": {"notifications": {
                "employee_payroll_days_before": 2,
                "employee_payroll_time": "18:30",
            }},
        }]

    def claim_notification(self, organization_id, role, recipient_user_id, period_start):
        key = (organization_id, role, recipient_user_id, period_start)
        if key in self.payroll_claims:
            return False
        self.payroll_claims.add(key)
        return True

    def release_notification(self, *args):
        self.payroll_claims.discard(tuple(args))

    def subscription_targets(self):
        return [{
            "organization_id": 1,
            "name": "Сеть",
            "owner_web_user_id": 7,
            "current_period_end": "2026-02-15T23:59:00+00:00",
        }]

    def claim_subscription_notification(self, organization_id, notification_type, period_end):
        key = (organization_id, notification_type, period_end)
        if key in self.subscription_claims:
            return False
        self.subscription_claims.add(key)
        return True

    def release_subscription_notification(self, *args):
        self.subscription_claims.discard(tuple(args))


def test_notification_service_delivers_idempotent_payroll_and_subscription_reminders():
    bot = FakeBot()
    store = FakeStore()
    service = NotificationService(bot, FakeDb(), store)
    now = datetime(2026, 2, 13, 18, 30, tzinfo=MSK)

    assert asyncio.run(service.send_payroll_notifications(now)) == 1
    assert asyncio.run(service.send_payroll_notifications(now)) == 0
    assert asyncio.run(service.send_subscription_notifications(datetime(2026, 2, 13, 10, tzinfo=MSK))) == 1
    assert asyncio.run(service.send_subscription_notifications(datetime(2026, 2, 13, 10, tzinfo=MSK))) == 0
    assert len(bot.messages) == 2
    assert bot.messages[0][0] == 1007
    assert "зарплатному" in bot.messages[0][1]
    assert "подписке" in bot.messages[1][1]
