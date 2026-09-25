"""Standalone notification worker for production deployments.

Run this process separately from ``salary_bot.py`` so a bot restart cannot
stop subscription and payroll reminders.  Delivery remains idempotent in the
platform database, so a brief overlap during deployment is safe.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import BOT_TOKEN, DATABASE_PATH, PLATFORM_DATABASE_PATH, PLATFORM_DATABASE_URL
from database import Database
from notification_service import NotificationService
from platform_store import PlatformStore

logger = logging.getLogger("pvz.worker")


async def run_worker(once: bool = False) -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN must be set to run the notification worker")
    legacy_db = Database(DATABASE_PATH)
    await legacy_db.init_db()
    platform_store = PlatformStore(PLATFORM_DATABASE_URL or PLATFORM_DATABASE_PATH)
    platform_store.init_schema()
    bot = Bot(token=BOT_TOKEN)
    service = NotificationService(bot, legacy_db, platform_store)
    if once:
        await service.send_payroll_notifications()
        await service.send_subscription_notifications()
        await bot.session.close()
        return

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(
        service.send_payroll_notifications,
        "interval",
        minutes=5,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        service.send_subscription_notifications,
        "interval",
        minutes=5,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("Notification worker started")
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Такт notification worker")
    parser.add_argument("--once", action="store_true", help="выполнить проверку один раз и завершиться")
    args = parser.parse_args()
    asyncio.run(run_worker(once=args.once))


if __name__ == "__main__":
    main()
