import asyncio
from datetime import datetime, time

import aiosqlite
import pytest

from database import MSK, Database, _get_work_window


def test_overnight_window_belongs_to_previous_date_after_midnight():
    now = datetime(2026, 9, 9, 2, 0, tzinfo=MSK)

    start, end = _get_work_window(now, time(20), time(8))

    assert start == datetime(2026, 9, 8, 20, 0, tzinfo=MSK)
    assert end == datetime(2026, 9, 9, 8, 0, tzinfo=MSK)


def test_salary_and_time_updates_keep_hourly_rate_in_sync(tmp_path):
    async def scenario():
        database = Database(str(tmp_path / "shifts.sqlite3"))
        await database.init_db()
        chat_number = await database.add_chat(100, "Test point", "work")
        await database.set_chat_settings(chat_number, 100, "09:00-17:00", 800)

        await database.update_chat_salary(chat_number, 1600)
        updated_salary = await database.get_chat_settings(100)
        assert updated_salary["salary_amount"] == 1600
        assert updated_salary["hourly_rate"] == 200

        await database.update_chat_time(chat_number, "10:00-18:00")
        updated_time = await database.get_chat_settings(100)
        assert updated_time["work_time_start"] == "10:00"
        assert updated_time["work_time_end"] == "18:00"
        assert updated_time["hourly_rate"] == 200

        await database.update_chat_time(chat_number, "20:00-08:00")
        overnight = await database.get_chat_settings(100)
        assert overnight["hourly_rate"] == pytest.approx(133.33333333333334)

    asyncio.run(scenario())


def test_stats_recalculate_by_chat_rate_without_legacy_daily_rate_column(tmp_path):
    async def scenario():
        database = Database(str(tmp_path / "stats.sqlite3"))
        await database.init_db()
        chat_number = await database.add_chat(100, "Test point", "work")
        await database.set_chat_settings(chat_number, 100, "09:00-17:00", 800)
        await database.add_or_update_worker(42, "worker", "Worker")
        period_start, period_end = database.get_current_period()
        now = datetime.now()
        async with aiosqlite.connect(database.db_path) as connection:
            await connection.execute(
                """
                INSERT INTO shifts
                    (user_id, chat_id, shift_type, start_time, end_time,
                     actual_start_time, actual_end_time, earned_amount, hours_worked,
                     period_start, period_end, is_active)
                VALUES (?, ?, 'regular', ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (42, 100, now, now, now, now, 999, 4, period_start, period_end),
            )
            await connection.commit()

        daily = await database.get_daily_stats(100, now, use_chat_rate=True)
        by_dates = await database.get_chat_stats_by_dates(
            100, period_start, period_end, include_active=False, use_chat_rate=True
        )
        by_period = await database.get_chat_stats_for_period(
            100, period_start, include_active=False, use_chat_rate=True
        )
        history = await database.get_shift_history_for_period(100, period_start)

        assert daily[0]["total_earned"] == pytest.approx(400)
        assert by_dates[0]["total_earned"] == pytest.approx(400)
        assert by_period[0]["total_earned"] == pytest.approx(400)
        assert history[0]["user_id"] == 42
        assert history[0]["hours_worked"] == pytest.approx(4)

    asyncio.run(scenario())
