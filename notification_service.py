"""Delivery service shared by the bot and the standalone notification worker."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from notification_schedule import reminder_due, subscription_reminder_due

MSK = ZoneInfo("Europe/Moscow")
UTC = timezone.utc
logger = logging.getLogger(__name__)


class NotificationService:
    """Deliver idempotent reminders using injected bot, identity and storage."""

    def __init__(self, bot: Any, legacy_db: Any, platform_store: Any):
        self.bot = bot
        self.legacy_db = legacy_db
        self.platform_store = platform_store

    @staticmethod
    def _local_now(now: datetime | None) -> datetime:
        current = now or datetime.now(MSK)
        return current.astimezone(MSK) if current.tzinfo else current.replace(tzinfo=MSK)

    @staticmethod
    def _telegram_id(user: dict[str, Any] | None) -> int | None:
        if not user or not user.get("telegram_id"):
            return None
        return int(user["telegram_id"])

    async def send_payroll_notifications(self, now: datetime | None = None) -> int:
        current = self._local_now(now)
        sent = 0
        try:
            targets = self.platform_store.notification_targets()
        except Exception:
            logger.exception("Не удалось загрузить настройки зарплатных уведомлений")
            return 0
        for target in targets:
            role = target["role"]
            due = reminder_due(current, role, target["settings"].get("notifications", {}))
            if not due:
                continue
            period_start, period_end, _ = due
            recipient = await self.legacy_db.get_web_user_by_id(target["web_user_id"])
            telegram_id = self._telegram_id(recipient)
            if not telegram_id:
                continue
            period_key = period_start.isoformat()
            if not self.platform_store.claim_notification(
                target["organization_id"], role, target["web_user_id"], period_key
            ):
                continue
            label = {"employee": "сотруднику", "manager": "управляющему", "owner": "владельцу"}[role]
            message = (
                "🔔 Напоминание по зарплатному периоду\n\n"
                f"Сеть: {target['organization_name']}\n"
                f"Период: {period_start:%d.%m.%Y} — {period_end:%d.%m.%Y}\n"
                f"Уведомление отправлено {label} по сценарию владельца."
            )
            try:
                await self.bot.send_message(telegram_id, message)
            except Exception:
                self.platform_store.release_notification(
                    target["organization_id"], role, target["web_user_id"], period_key
                )
                logger.exception("Не удалось отправить зарплатное уведомление")
            else:
                sent += 1
        return sent

    async def send_subscription_notifications(self, now: datetime | None = None) -> int:
        current = self._local_now(now)
        sent = 0
        try:
            targets = self.platform_store.subscription_targets()
        except Exception:
            logger.exception("Не удалось загрузить подписки для напоминаний")
            return 0
        for target in targets:
            try:
                period_end = datetime.fromisoformat(target["current_period_end"])
            except (TypeError, ValueError):
                continue
            if period_end.tzinfo is None:
                period_end = period_end.replace(tzinfo=UTC)
            reminder_type = subscription_reminder_due(current, period_end)
            if not reminder_type:
                continue
            recipient = await self.legacy_db.get_web_user_by_id(target["owner_web_user_id"])
            telegram_id = self._telegram_id(recipient)
            if not telegram_id:
                continue
            period_key = target["current_period_end"]
            if not self.platform_store.claim_subscription_notification(
                target["organization_id"], reminder_type, period_key
            ):
                continue
            if reminder_type == "expired":
                text = "Подписка истекла. Данные сохранены, но изменения доступны после продления."
            elif reminder_type == "last_day":
                text = "Сегодня заканчивается подписка на Такт. Продлите её, чтобы не перейти в grace-период."
            else:
                days = reminder_type.removesuffix("_days")
                text = f"До окончания подписки Такт осталось {days} дн. Продлите доступ заранее."
            message = (
                "💳 Напоминание о подписке\n\n"
                f"Сеть: {target['name']}\n"
                f"Дата окончания: {period_end:%d.%m.%Y}\n\n{text}"
            )
            try:
                await self.bot.send_message(telegram_id, message)
            except Exception:
                self.platform_store.release_subscription_notification(
                    target["organization_id"], reminder_type, period_key
                )
                logger.exception("Не удалось отправить напоминание о подписке")
            else:
                sent += 1
        return sent
