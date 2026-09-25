#!/usr/bin/env python3
"""
Скрипт миграции базы данных для обновления структуры web_users и добавления link_keys
"""
import asyncio
import aiosqlite
import sys
from datetime import datetime

DB_PATH = 'salary_bot.db'

async def migrate_database():
    """Применяет миграцию к базе данных"""
    print("🔄 Начинаем миграцию базы данных...")
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем текущую структуру web_users
        cursor = await db.execute("PRAGMA table_info(web_users)")
        columns = await cursor.fetchall()
        column_names = [col[1] for col in columns]
        
        print(f"📋 Текущие колонки web_users: {column_names}")
        
        # Создаем таблицу link_keys если её нет
        print("🔑 Создаем таблицу link_keys...")
        await db.execute('''
            CREATE TABLE IF NOT EXISTS link_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_code TEXT UNIQUE NOT NULL,
                telegram_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                used INTEGER DEFAULT 0,
                used_at TIMESTAMP,
                used_by_web_user_id INTEGER,
                FOREIGN KEY (used_by_web_user_id) REFERENCES web_users(id)
            )
        ''')
        
        # Если в web_users нет колонки role, значит таблица старая - пересоздаем
        if 'role' not in column_names:
            print("⚠️  Обнаружена старая структура web_users. Выполняем миграцию...")
            
            # Сохраняем старые данные
            cursor = await db.execute('SELECT * FROM web_users')
            old_users = await cursor.fetchall()
            print(f"📦 Сохранено {len(old_users)} пользователей")
            
            # Получаем старую структуру
            cursor = await db.execute("PRAGMA table_info(web_users)")
            old_columns = await cursor.fetchall()
            
            # Переименовываем старую таблицу
            await db.execute('ALTER TABLE web_users RENAME TO web_users_old')
            
            # Создаем новую таблицу с правильной структурой
            print("🆕 Создаем новую структуру web_users...")
            await db.execute('''
                CREATE TABLE web_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT DEFAULT 'guest',
                    telegram_id INTEGER UNIQUE,
                    telegram_linked INTEGER DEFAULT 0,
                    managed_chat_ids TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_login TIMESTAMP
                )
            ''')
            
            # Мигрируем данные
            print("📥 Переносим данные...")
            for user in old_users:
                # Старая структура: id, user_id, username, password_hash, is_admin, created_at
                try:
                    old_id = user[0]
                    old_user_id = user[1]  # telegram_id
                    username = user[2]
                    password_hash = user[3]
                    is_admin = user[4] if len(user) > 4 else 0
                    created_at = user[5] if len(user) > 5 else datetime.now()
                    
                    # Определяем роль
                    role = 'admin' if is_admin else 'user'
                    
                    await db.execute('''
                        INSERT INTO web_users 
                        (id, username, password_hash, role, telegram_id, telegram_linked, created_at)
                        VALUES (?, ?, ?, ?, ?, 1, ?)
                    ''', (old_id, username, password_hash, role, old_user_id, created_at))
                    
                except Exception as e:
                    print(f"⚠️  Ошибка миграции пользователя {user}: {e}")
            
            # Удаляем старую таблицу
            await db.execute('DROP TABLE web_users_old')
            print("✅ Старая таблица удалена")
        
        # Проверяем и обновляем web_sessions
        cursor = await db.execute("PRAGMA table_info(web_sessions)")
        session_columns = await cursor.fetchall()
        session_column_names = [col[1] for col in session_columns]
        
        print(f"📋 Текущие колонки web_sessions: {session_column_names}")
        
        if 'session_token' not in session_column_names:
            print("🔄 Обновляем структуру web_sessions...")
            
            # Сохраняем данные сессий
            cursor = await db.execute('SELECT * FROM web_sessions')
            old_sessions = await cursor.fetchall()
            
            # Пересоздаем таблицу с правильными названиями колонок
            await db.execute('DROP TABLE web_sessions')
            await db.execute('''
                CREATE TABLE web_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_token TEXT UNIQUE NOT NULL,
                    web_user_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL,
                    ip_address TEXT,
                    user_agent TEXT,
                    FOREIGN KEY (web_user_id) REFERENCES web_users(id) ON DELETE CASCADE
                )
            ''')
            
            print("🗑️  Старые сессии удалены (пользователи должны войти заново)")
        
        await db.commit()
        
        # Проверяем результат
        cursor = await db.execute("PRAGMA table_info(web_users)")
        new_columns = await cursor.fetchall()
        new_column_names = [col[1] for col in new_columns]
        
        print("✅ Миграция завершена!")
        print(f"📋 Новые колонки web_users: {new_column_names}")
        
        # Показываем статистику
        cursor = await db.execute('SELECT COUNT(*) FROM web_users')
        user_count = (await cursor.fetchone())[0]
        print(f"👥 Всего пользователей: {user_count}")
        
        cursor = await db.execute('SELECT COUNT(*) FROM web_sessions')
        session_count = (await cursor.fetchone())[0]
        print(f"🔐 Всего сессий: {session_count}")

if __name__ == '__main__':
    try:
        asyncio.run(migrate_database())
        print("\n✅ Миграция успешно завершена!")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Ошибка миграции: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
