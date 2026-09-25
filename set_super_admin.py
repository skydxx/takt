#!/usr/bin/env python3
"""
Скрипт для установки роли super_admin пользователю
"""
import asyncio
import aiosqlite
import sys

DB_PATH = 'salary_bot.db'

async def set_super_admin(username: str):
    """Устанавливает роль super_admin для указанного пользователя"""
    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем существование пользователя
        cursor = await db.execute('SELECT id, username, role FROM web_users WHERE username = ?', (username,))
        user = await cursor.fetchone()
        
        if not user:
            print(f"❌ Пользователь '{username}' не найден!")
            return False
        
        user_id, current_username, current_role = user
        print(f"👤 Найден пользователь: {current_username}")
        print(f"📋 Текущая роль: {current_role}")
        
        # Обновляем роль
        await db.execute('UPDATE web_users SET role = ? WHERE id = ?', ('super_admin', user_id))
        await db.commit()
        
        print(f"✅ Роль успешно обновлена на: super_admin")
        return True

async def list_users():
    """Показывает список всех веб-пользователей"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT id, username, role, telegram_id, telegram_linked FROM web_users')
        users = await cursor.fetchall()
        
        if not users:
            print("📋 Нет зарегистрированных пользователей")
            return
        
        print("\n📋 Список пользователей:")
        print("-" * 80)
        print(f"{'ID':<5} {'Username':<20} {'Role':<15} {'TG ID':<15} {'Linked':<10}")
        print("-" * 80)
        
        for user in users:
            user_id, username, role, tg_id, linked = user
            linked_str = "✓" if linked else "✗"
            print(f"{user_id:<5} {username:<20} {role:<15} {str(tg_id or '-'):<15} {linked_str:<10}")
        
        print("-" * 80)

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Использование:")
        print("  python set_super_admin.py <username>  - установить super_admin")
        print("  python set_super_admin.py --list      - показать всех пользователей")
        sys.exit(1)
    
    if sys.argv[1] == '--list':
        asyncio.run(list_users())
    else:
        username = sys.argv[1]
        success = asyncio.run(set_super_admin(username))
        sys.exit(0 if success else 1)
