import asyncio
import os
from werkzeug.security import generate_password_hash
from database import Database
from config import DATABASE_PATH

async def create_admin():
    db = Database(DATABASE_PATH)
    
    # Инициализируем БД на случай, если она пустая
    await db.init_db()
    
    username = input("Enter admin username: ")
    password = input("Enter admin password: ")
    
    # Use a dummy user_id for the web admin if not linked to a telegram user yet
    # or ask for telegram user_id
    user_id = input("Enter Telegram User ID (optional, press Enter to skip): ")
    
    if not user_id:
        user_id = 0
    else:
        user_id = int(user_id)
        
    password_hash = generate_password_hash(password)
    
    try:
        # Ensure worker exists first if we have a real user_id
        if user_id != 0:
            await db.add_or_update_worker(user_id, username=username, full_name="Admin")
            
        await db.create_web_user(user_id, username, password_hash, is_admin=True)
        print(f"✅ Admin user '{username}' created successfully!")
    except Exception as e:
        print(f"❌ Error creating admin user: {e}")

if __name__ == '__main__':
    asyncio.run(create_admin())
