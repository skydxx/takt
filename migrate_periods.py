#!/usr/bin/env python3
"""
Миграция: обновление границ периодов с (1-14, 15-конец) на (1-15, 16-конец)
"""

import aiosqlite
import asyncio
from datetime import datetime, timedelta

DB_PATH = 'salary_bot.db'


async def migrate_periods():
    """Обновить границы периодов в существующих сменах"""
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Получаем все уникальные периоды
        cursor = await db.execute('''
            SELECT DISTINCT period_start, period_end
            FROM shifts
            ORDER BY period_start DESC
        ''')
        
        periods = await cursor.fetchall()
        
        print(f"\n📊 Найдено {len(periods)} уникальных периодов")
        print("\nТекущие периоды в БД:")
        for period_start, period_end in periods:
            print(f"  {period_start} - {period_end}")
        
        updated_count = 0
        
        for period_start_str, period_end_str in periods:
            period_start = datetime.fromisoformat(period_start_str)
            period_end = datetime.fromisoformat(period_end_str)
            
            # Определяем, это первая или вторая половина месяца
            if period_start.day == 1:
                # Первая половина месяца (1-14 -> 1-15)
                # Проверяем, нужно ли обновить
                correct_end = datetime(period_start.year, period_start.month, 15, 23, 59, 59)
                
                if period_end != correct_end:
                    print(f"\n✏️  Обновление первой половины {period_start.strftime('%m.%Y')}:")
                    print(f"   Было: {period_start_str} - {period_end_str}")
                    print(f"   Стало: {period_start_str} - {correct_end}")
                    
                    # Обновляем смены
                    await db.execute('''
                        UPDATE shifts
                        SET period_end = ?
                        WHERE period_start = ? AND period_end = ?
                    ''', (correct_end, period_start, period_end))
                    
                    result = await db.execute(
                        'SELECT changes()'
                    )
                    count = (await result.fetchone())[0]
                    updated_count += count
                    print(f"   Обновлено смен: {count}")
            
            elif period_start.day == 15:
                # Вторая половина месяца (15-конец -> 16-конец)
                # Нужно изменить period_start с 15 на 16
                correct_start = datetime(period_start.year, period_start.month, 16)
                
                print(f"\n✏️  Обновление второй половины {period_start.strftime('%m.%Y')}:")
                print(f"   Было: {period_start_str} - {period_end_str}")
                print(f"   Стало: {correct_start} - {period_end_str}")
                
                # Обновляем смены
                await db.execute('''
                    UPDATE shifts
                    SET period_start = ?
                    WHERE period_start = ? AND period_end = ?
                ''', (correct_start, period_start, period_end))
                
                result = await db.execute(
                    'SELECT changes()'
                )
                count = (await result.fetchone())[0]
                updated_count += count
                print(f"   Обновлено смен: {count}")
        
        await db.commit()
        
        print(f"\n✅ Миграция завершена!")
        print(f"   Всего обновлено смен: {updated_count}")
        
        # Показываем новые периоды
        cursor = await db.execute('''
            SELECT DISTINCT period_start, period_end
            FROM shifts
            ORDER BY period_start DESC
        ''')
        
        new_periods = await cursor.fetchall()
        
        print(f"\nОбновленные периоды в БД:")
        for period_start, period_end in new_periods:
            print(f"  {period_start} - {period_end}")


async def main():
    print("="*60)
    print("МИГРАЦИЯ ГРАНИЦ ПЕРИОДОВ")
    print("="*60)
    print("\nЭтот скрипт обновит границы периодов в существующих сменах:")
    print("  • 1-14 число → 1-15 число")
    print("  • 15-конец месяца → 16-конец месяца")
    print("\n⚠️  ВНИМАНИЕ: Перед запуском сделайте резервную копию БД!")
    print("="*60)
    
    response = input("\nПродолжить? (yes/no): ")
    if response.lower() not in ['yes', 'y', 'да']:
        print("Отменено.")
        return
    
    await migrate_periods()


if __name__ == '__main__':
    asyncio.run(main())
