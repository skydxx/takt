#!/usr/bin/env python3
"""
Миграция для добавления VPN системы и безопасности
"""
import asyncio
import aiosqlite
import sys

DB_PATH = 'salary_bot.db'

async def migrate():
    print("🔄 Начинаем миграцию для VPN и безопасности...")
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Таблица безопасных вопросов
        print("🔐 Создаем таблицу security_questions...")
        await db.execute('''
            CREATE TABLE IF NOT EXISTS security_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                web_user_id INTEGER NOT NULL,
                question1 TEXT NOT NULL,
                answer1_hash TEXT NOT NULL,
                question2 TEXT NOT NULL,
                answer2_hash TEXT NOT NULL,
                question3 TEXT NOT NULL,
                answer3_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (web_user_id) REFERENCES web_users(id) ON DELETE CASCADE
            )
        ''')
        
        # Таблица запросов на сброс пароля
        print("🔑 Создаем таблицу password_reset_requests...")
        await db.execute('''
            CREATE TABLE IF NOT EXISTS password_reset_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                telegram_contact TEXT NOT NULL,
                answer1 TEXT NOT NULL,
                answer2 TEXT NOT NULL,
                answer3 TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP,
                processed_by_admin_id INTEGER
            )
        ''')
        
        # Удаляем старые таблицы VPN если есть
        print("🗑️  Удаляем старые таблицы VPN...")
        await db.execute('DROP TABLE IF EXISTS vpn_orders')
        await db.execute('DROP TABLE IF EXISTS vpn_keys')
        await db.execute('DROP TABLE IF EXISTS vpn_products')
        
        # Новая таблица VPN заказов
        print("🛡️  Создаем новую таблицу vpn_orders...")
        await db.execute('''
            CREATE TABLE IF NOT EXISTS vpn_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                web_user_id INTEGER NOT NULL,
                telegram_username TEXT,
                device_count INTEGER NOT NULL,
                period_days INTEGER NOT NULL,
                total_price REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                telegram_contact TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                confirmed_at TIMESTAMP,
                FOREIGN KEY (web_user_id) REFERENCES web_users(id) ON DELETE CASCADE
            )
        ''')
        
        # Таблица VPN ключей для заказов
        print("🔑 Создаем таблицу vpn_order_keys...")
        await db.execute('''
            CREATE TABLE IF NOT EXISTS vpn_order_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                device_number INTEGER NOT NULL,
                vpn_key TEXT,
                added_at TIMESTAMP,
                FOREIGN KEY (order_id) REFERENCES vpn_orders(id) ON DELETE CASCADE
            )
        ''')
        
        # Таблица блокировок пользователей
        print("🚫 Создаем таблицу user_bans...")
        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_bans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                web_user_id INTEGER,
                telegram_username TEXT,
                reason TEXT NOT NULL,
                banned_by_admin_id INTEGER NOT NULL,
                banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                is_permanent INTEGER DEFAULT 0,
                FOREIGN KEY (web_user_id) REFERENCES web_users(id) ON DELETE CASCADE
            )
        ''')
        
        await db.commit()
        
        print("✅ Миграция успешно завершена!")
        
        # Статистика
        cursor = await db.execute('SELECT COUNT(*) FROM security_questions')
        sq_count = (await cursor.fetchone())[0]
        
        cursor = await db.execute('SELECT COUNT(*) FROM vpn_orders')
        orders_count = (await cursor.fetchone())[0]
        
        print(f"📊 Статистика:")
        print(f"   - Секретных вопросов: {sq_count}")
        print(f"   - VPN заказов: {orders_count}")

if __name__ == '__main__':
    try:
        asyncio.run(migrate())
        print("\n✅ Миграция успешно применена!")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Ошибка миграции: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
