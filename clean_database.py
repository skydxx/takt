#!/usr/bin/env python3
"""
Скрипт для полной очистки БД от аккаунтов, заказов и кодов привязки
"""

import aiosqlite
import asyncio
import os

DB_PATH = "salary_bot.db"


async def clean_database():
    """Очистка всех таблиц с пользовательскими данными"""
    
    if not os.path.exists(DB_PATH):
        print(f"❌ База данных {DB_PATH} не найдена!")
        return
    
    print("="*60)
    print("ОЧИСТКА БАЗЫ ДАННЫХ")
    print("="*60)
    print("\n⚠️  ВНИМАНИЕ! Будут удалены:")
    print("  • Все пользователи (main_users)")
    print("  • Все коды привязки (link_codes)")
    print("  • Все VPN заказы (vpn_orders)")
    print("  • Все VPN ключи (vpn_keys)")
    print("  • Файлы контрольных вопросов")
    print("\n⚠️  Данные бота (смены, чаты, работники) НЕ БУДУТ удалены!")
    print("="*60)
    
    response = input("\nПродолжить? (yes/no): ")
    if response.lower() not in ['yes', 'y', 'да']:
        print("Отменено.")
        return
    
    async with aiosqlite.connect(DB_PATH) as db:
        print("\n🗑️  Удаление данных...")
        
        # Удаляем VPN ключи
        cursor = await db.execute('SELECT COUNT(*) FROM vpn_keys')
        count = (await cursor.fetchone())[0]
        await db.execute('DELETE FROM vpn_keys')
        print(f"  ✅ Удалено VPN ключей: {count}")
        
        # Удаляем VPN заказы
        cursor = await db.execute('SELECT COUNT(*) FROM vpn_orders')
        count = (await cursor.fetchone())[0]
        await db.execute('DELETE FROM vpn_orders')
        print(f"  ✅ Удалено VPN заказов: {count}")
        
        # Удаляем коды привязки
        cursor = await db.execute('SELECT COUNT(*) FROM link_codes')
        count = (await cursor.fetchone())[0]
        await db.execute('DELETE FROM link_codes')
        print(f"  ✅ Удалено кодов привязки: {count}")
        
        # Получаем список файлов контрольных вопросов
        cursor = await db.execute('SELECT security_question_file FROM main_users WHERE security_question_file IS NOT NULL')
        files = await cursor.fetchall()
        
        # Удаляем пользователей
        cursor = await db.execute('SELECT COUNT(*) FROM main_users')
        count = (await cursor.fetchone())[0]
        await db.execute('DELETE FROM main_users')
        print(f"  ✅ Удалено пользователей: {count}")
        
        await db.commit()
        
        # Удаляем файлы контрольных вопросов
        security_dir = "data/security_questions"
        if os.path.exists(security_dir):
            deleted_files = 0
            for file_tuple in files:
                if file_tuple[0]:
                    filepath = os.path.join(security_dir, file_tuple[0])
                    if os.path.exists(filepath):
                        os.remove(filepath)
                        deleted_files += 1
            print(f"  ✅ Удалено файлов контрольных вопросов: {deleted_files}")
        
        print("\n✅ База данных успешно очищена!")
        print("\nТеперь можно создавать новые аккаунты с чистого листа.")


if __name__ == '__main__':
    asyncio.run(clean_database())
