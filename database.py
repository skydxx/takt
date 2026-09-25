import hashlib
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import aiosqlite

from config import DATABASE_PATH

# Московский часовой пояс
MSK = ZoneInfo("Europe/Moscow")


def _get_work_window(now: datetime, start_time, end_time) -> Tuple[datetime, datetime]:
    """Return the shift window containing or following ``now``.

    A work period such as 20:00-08:00 belongs to the previous calendar day
    after midnight.  Keeping that rule in one place prevents start, end and
    live-earnings calculations from disagreeing about overnight shifts.
    """
    start = datetime.combine(now.date(), start_time, tzinfo=MSK)
    end = datetime.combine(now.date(), end_time, tzinfo=MSK)
    if end_time <= start_time:
        if now.time() < end_time:
            start -= timedelta(days=1)
        else:
            end += timedelta(days=1)
    return start, end


def _hours_between(start_time: str, end_time: str) -> float:
    """Return the duration of an HH:MM-HH:MM work window in hours."""
    start = datetime.strptime(start_time, '%H:%M')
    end = datetime.strptime(end_time, '%H:%M')
    if end <= start:
        end += timedelta(days=1)
    return (end - start).total_seconds() / 3600


class Database:
    def __init__(self, db_path: str = DATABASE_PATH):
        self.db_path = db_path

    async def _migrate_legacy_web_auth(self, db) -> None:
        """Upgrade the original Telegram-linked web auth tables in place.

        Early installs used ``web_users.user_id`` as the Telegram identifier
        and ``web_sessions.user_id`` as the session owner.  ``CREATE TABLE IF
        NOT EXISTS`` cannot add the newer role and workspace columns, so those
        databases otherwise fail as soon as a modern login is attempted.
        """
        cursor = await db.execute("PRAGMA table_info(web_users)")
        user_columns = {row[1] for row in await cursor.fetchall()}
        if user_columns and "role" not in user_columns and "user_id" in user_columns:
            await db.execute(
                """CREATE TABLE web_users_migrated (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT DEFAULT 'guest',
                    telegram_id INTEGER UNIQUE,
                    telegram_linked BOOLEAN DEFAULT 0,
                    managed_chat_ids TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_login TIMESTAMP
                )"""
            )
            await db.execute(
                """INSERT INTO web_users_migrated
                   (id, username, password_hash, role, telegram_id,
                    telegram_linked, managed_chat_ids, created_at, last_login)
                   SELECT id, username, password_hash,
                          CASE
                            WHEN COALESCE(is_admin, 0) = 1 THEN 'admin'
                            WHEN COALESCE(is_view_only_admin, 0) = 1 THEN 'view_admin'
                            ELSE 'user'
                          END,
                          user_id, 1, NULL, created_at, last_login
                   FROM web_users"""
            )
            await db.execute("DROP TABLE web_users")
            await db.execute("ALTER TABLE web_users_migrated RENAME TO web_users")

        cursor = await db.execute("PRAGMA table_info(web_sessions)")
        session_columns = {row[1] for row in await cursor.fetchall()}
        if session_columns and "web_user_id" not in session_columns and "user_id" in session_columns:
            await db.execute(
                """CREATE TABLE web_sessions_migrated (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_token TEXT UNIQUE NOT NULL,
                    web_user_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL,
                    ip_address TEXT,
                    user_agent TEXT,
                    FOREIGN KEY (web_user_id) REFERENCES web_users(id)
                )"""
            )
            await db.execute(
                """INSERT OR IGNORE INTO web_sessions_migrated
                   (id, session_token, web_user_id, created_at, expires_at,
                    ip_address, user_agent)
                   SELECT s.id, s.session_token, u.id, s.created_at, s.expires_at,
                          s.ip_address, s.user_agent
                   FROM web_sessions s
                   JOIN web_users u ON u.telegram_id = s.user_id"""
            )
            await db.execute("DROP TABLE web_sessions")
            await db.execute("ALTER TABLE web_sessions_migrated RENAME TO web_sessions")
    
    async def init_db(self):
        """Инициализация базы данных"""
        async with aiosqlite.connect(self.db_path) as db:
            await self._migrate_legacy_web_auth(db)
            # Таблица чатов
            await db.execute('''
                CREATE TABLE IF NOT EXISTS chats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER UNIQUE NOT NULL,
                    chat_name TEXT NOT NULL,
                    chat_type TEXT NOT NULL,
                    admin_chat_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица настроек рабочих чатов
            await db.execute('''
                CREATE TABLE IF NOT EXISTS chat_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_number INTEGER UNIQUE NOT NULL,
                    chat_id INTEGER NOT NULL,
                    work_time_start TEXT,
                    work_time_end TEXT,
                    salary_amount REAL,
                    hourly_rate REAL,
                    shift_type TEXT DEFAULT 'regular',
                    FOREIGN KEY (chat_id) REFERENCES chats(chat_id)
                )
            ''')
            
            # Таблица настроек вечерних смен
            await db.execute('''
                CREATE TABLE IF NOT EXISTS evening_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_number INTEGER NOT NULL,
                    work_time_start TEXT,
                    work_time_end TEXT,
                    salary_amount REAL,
                    hourly_rate REAL,
                    FOREIGN KEY (chat_number) REFERENCES chat_settings(chat_number)
                )
            ''')
            
            # Таблица работников
            await db.execute('''
                CREATE TABLE IF NOT EXISTS workers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE NOT NULL,
                    username TEXT,
                    full_name TEXT,
                    custom_rate REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица смен
            await db.execute('''
                CREATE TABLE IF NOT EXISTS shifts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    shift_type TEXT DEFAULT 'regular',
                    start_time TIMESTAMP NOT NULL,
                    end_time TIMESTAMP,
                    actual_start_time TIMESTAMP,
                    actual_end_time TIMESTAMP,
                    earned_amount REAL DEFAULT 0,
                    hours_worked REAL DEFAULT 0,
                    period_start DATE,
                    period_end DATE,
                    is_active BOOLEAN DEFAULT 1,
                    FOREIGN KEY (user_id) REFERENCES workers(user_id),
                    FOREIGN KEY (chat_id) REFERENCES chats(chat_id)
                )
            ''')
            
            # Таблица команд
            await db.execute('''
                CREATE TABLE IF NOT EXISTS command_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    command TEXT NOT NULL,
                    last_used TIMESTAMP NOT NULL,
                    UNIQUE(user_id, chat_id, command)
                )
            ''')
            
            # Таблица сообщений для удаления
            await db.execute('''
                CREATE TABLE IF NOT EXISTS messages_to_delete (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    delete_at TIMESTAMP NOT NULL
                )
            ''')
            
            # Таблица неотправленных отчетов
            await db.execute('''
                CREATE TABLE IF NOT EXISTS pending_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_type TEXT NOT NULL,
                    target_id INTEGER NOT NULL,
                    report_text TEXT NOT NULL,
                    period_start DATE NOT NULL,
                    attempts INTEGER DEFAULT 0,
                    last_attempt TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица веб-пользователей
            await db.execute('''
                CREATE TABLE IF NOT EXISTS web_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT DEFAULT 'guest',
                    telegram_id INTEGER UNIQUE,
                    telegram_linked BOOLEAN DEFAULT 0,
                    managed_chat_ids TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_login TIMESTAMP
                )
            ''')
            
            # Таблица сессий
            await db.execute('''
                CREATE TABLE IF NOT EXISTS web_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_token TEXT UNIQUE NOT NULL,
                    web_user_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL,
                    ip_address TEXT,
                    user_agent TEXT,
                    FOREIGN KEY (web_user_id) REFERENCES web_users(id)
                )
            ''')
            # Legacy versions stored raw browser tokens.  Keep the column for
            # compatibility, but normalize every existing value to a SHA-256
            # digest before the connection is committed.
            session_rows = await db.execute(
                'SELECT id, session_token FROM web_sessions'
            )
            for session_id, stored_token in await session_rows.fetchall():
                if stored_token and not re.fullmatch(r'[0-9a-f]{64}', stored_token):
                    await db.execute(
                        'UPDATE web_sessions SET session_token = ? WHERE id = ?',
                        (self._session_digest(stored_token), session_id),
                    )
            
            # Таблица одноразовых ключей для связывания аккаунтов
            await db.execute('''
            CREATE TABLE IF NOT EXISTS link_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_code TEXT UNIQUE NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL,
                    used BOOLEAN DEFAULT 0,
                    used_by_user_id INTEGER,
                FOREIGN KEY (used_by_user_id) REFERENCES web_users(id)
            )
            ''')
            link_key_rows = await db.execute('SELECT id, key_code FROM link_keys')
            for link_key_id, stored_key in await link_key_rows.fetchall():
                if stored_key and not re.fullmatch(r'[0-9a-f]{64}', stored_key):
                    await db.execute(
                        'UPDATE link_keys SET key_code = ? WHERE id = ?',
                        (self._session_digest(stored_key), link_key_id),
                    )
            
            # Таблица безопасных вопросов пользователей
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

            # Public sales leads. This table is deliberately separate from
            # authenticated users: a visitor can request a demo without an
            # account, while the owner can later convert the lead manually.
            await db.execute('''
                CREATE TABLE IF NOT EXISTS lead_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    contact TEXT NOT NULL,
                    points TEXT NOT NULL,
                    message TEXT,
                    language TEXT NOT NULL DEFAULT 'ru',
                    consent_at TIMESTAMP NOT NULL,
                    source TEXT NOT NULL DEFAULT 'landing',
                    status TEXT NOT NULL DEFAULT 'new',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Таблица заказов VPN (новая система)
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
            
            await db.commit()
    
    def get_current_period(self) -> Tuple[datetime, datetime]:
        """Получение текущего периода"""
        now = datetime.now(MSK)
        year = now.year
        month = now.month
        day = now.day
        
        if day <= 15:
            # Период с 1 по 15 число
            period_start = datetime(year, month, 1)
            period_end = datetime(year, month, 15, 23, 59, 59)
        else:
            # Период с 16 по конец месяца
            period_start = datetime(year, month, 16)
            if month == 12:
                next_month = datetime(year + 1, 1, 1)
            else:
                next_month = datetime(year, month + 1, 1)
            period_end = next_month - timedelta(seconds=1)
        
        return period_start, period_end
    
    async def add_chat(self, chat_id: int, chat_name: str, chat_type: str, admin_chat_id: int = None) -> int:
        """Добавление нового чата и возврат его номера"""
        async with aiosqlite.connect(self.db_path) as db:
            # Проверяем, есть ли уже такой чат
            cursor = await db.execute('SELECT chat_id FROM chats WHERE chat_id = ?', (chat_id,))
            existing = await cursor.fetchone()
            
            if existing:
                # Обновляем существующий чат
                await db.execute(
                    'UPDATE chats SET chat_name = ?, chat_type = ?, admin_chat_id = ? WHERE chat_id = ?',
                    (chat_name, chat_type, admin_chat_id, chat_id)
                )
                await db.commit()
                
                # Получаем номер чата
                cursor = await db.execute('SELECT chat_number FROM chat_settings WHERE chat_id = ?', (chat_id,))
                result = await cursor.fetchone()
                return result[0] if result else 1
            
            # Добавляем новый чат
            await db.execute(
                'INSERT INTO chats (chat_id, chat_name, chat_type, admin_chat_id) VALUES (?, ?, ?, ?)',
                (chat_id, chat_name, chat_type, admin_chat_id)
            )
            
            # Получаем следующий номер чата
            cursor = await db.execute('SELECT MAX(chat_number) FROM chat_settings')
            result = await cursor.fetchone()
            next_number = (result[0] or 0) + 1
            
            # Создаем настройки чата
            await db.execute(
                'INSERT INTO chat_settings (chat_number, chat_id) VALUES (?, ?)',
                (next_number, chat_id)
            )
            
            await db.commit()
            return next_number
    
    async def delete_chat(self, chat_number: int) -> Dict:
        """Удаление чата и всех связанных данных"""
        async with aiosqlite.connect(self.db_path) as db:
            # Получаем информацию о чате перед удалением
            cursor = await db.execute('''
                SELECT c.chat_id, c.chat_name, c.chat_type
                FROM chats c
                JOIN chat_settings cs ON c.chat_id = cs.chat_id
                WHERE cs.chat_number = ?
            ''', (chat_number,))
            
            chat_info = await cursor.fetchone()
            if not chat_info:
                raise ValueError(f"Чат #{chat_number} не найден")
            
            chat_id, chat_name, chat_type = chat_info
            
            # Удаляем смены этого чата
            cursor = await db.execute('DELETE FROM shifts WHERE chat_id = ?', (chat_id,))
            shifts_deleted = cursor.rowcount
            
            # Удаляем настройки вечерней смены
            await db.execute('DELETE FROM evening_settings WHERE chat_number = ?', (chat_number,))
            
            # Удаляем настройки чата
            await db.execute('DELETE FROM chat_settings WHERE chat_number = ?', (chat_number,))
            
            # Удаляем сам чат
            await db.execute('DELETE FROM chats WHERE chat_id = ?', (chat_id,))
            
            # Удаляем сообщения для удаления
            await db.execute('DELETE FROM messages_to_delete WHERE chat_id = ?', (chat_id,))
            
            await db.commit()
            
            return {
                'chat_id': chat_id,
                'chat_name': chat_name,
                'chat_type': chat_type,
                'shifts_deleted': shifts_deleted
            }
    
    async def get_next_chat_number(self) -> int:
        """Получить следующий уникальный номер чата"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('SELECT MAX(chat_number) FROM chat_settings')
            result = await cursor.fetchone()
            return (result[0] or 0) + 1
    
    async def set_chat_settings(self, chat_number: int, chat_id: int, work_time: str, salary: float, 
                                shift_type: str = 'regular', is_evening: bool = False):
        """Установка настроек рабочего чата"""
        time_parts = work_time.split('-')
        if len(time_parts) != 2:
            raise ValueError("Неверный формат времени. Используйте формат HH:MM-HH:MM")
        
        start_time = time_parts[0].strip()
        end_time = time_parts[1].strip()
        
        start_h, start_m = map(int, start_time.split(':'))
        end_h, end_m = map(int, end_time.split(':'))
        
        total_minutes = (end_h * 60 + end_m) - (start_h * 60 + start_m)
        if total_minutes < 0:
            total_minutes += 24 * 60
        
        total_hours = total_minutes / 60
        hourly_rate = salary / total_hours if total_hours > 0 else 0
        
        async with aiosqlite.connect(self.db_path) as db:
            if is_evening:
                await db.execute('''
                    INSERT OR REPLACE INTO evening_settings 
                    (chat_number, work_time_start, work_time_end, salary_amount, hourly_rate)
                    VALUES (?, ?, ?, ?, ?)
                ''', (chat_number, start_time, end_time, salary, hourly_rate))
            else:
                await db.execute('''
                    INSERT OR REPLACE INTO chat_settings 
                    (chat_number, chat_id, work_time_start, work_time_end, salary_amount, hourly_rate, shift_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (chat_number, chat_id, start_time, end_time, salary, hourly_rate, shift_type))
            
            await db.commit()
    
    async def update_chat_salary(self, chat_number: int, salary: float, is_evening: bool = False):
        """Обновление только ставки чата"""
        async with aiosqlite.connect(self.db_path) as db:
            if is_evening:
                cursor = await db.execute(
                    'SELECT work_time_start, work_time_end FROM evening_settings WHERE chat_number = ?',
                    (chat_number,)
                )
            else:
                cursor = await db.execute(
                    'SELECT work_time_start, work_time_end FROM chat_settings WHERE chat_number = ?',
                    (chat_number,)
                )
            
            result = await cursor.fetchone()
            if not result:
                raise ValueError(f"Чат с номером {chat_number} не найден")
            
            work_start, work_end = result
            hourly_rate = salary / _hours_between(work_start, work_end)

            if is_evening:
                await db.execute(
                    'UPDATE evening_settings SET salary_amount = ?, hourly_rate = ? WHERE chat_number = ?',
                    (salary, hourly_rate, chat_number)
                )
            else:
                await db.execute(
                    'UPDATE chat_settings SET salary_amount = ?, hourly_rate = ? WHERE chat_number = ?',
                    (salary, hourly_rate, chat_number)
                )
            
            await db.commit()
    
    async def update_chat_time(self, chat_number: int, work_time: str, is_evening: bool = False):
        """Обновление только времени работы"""
        time_parts = work_time.split('-')
        if len(time_parts) != 2:
            raise ValueError("Неверный формат времени")
        
        start_time = time_parts[0].strip()
        end_time = time_parts[1].strip()
        try:
            datetime.strptime(start_time, '%H:%M')
            datetime.strptime(end_time, '%H:%M')
        except ValueError as exc:
            raise ValueError("Неверный формат времени") from exc

        async with aiosqlite.connect(self.db_path) as db:
            if is_evening:
                cursor = await db.execute(
                    'SELECT salary_amount FROM evening_settings WHERE chat_number = ?',
                    (chat_number,)
                )
                result = await cursor.fetchone()
                if not result:
                    raise ValueError(f"Чат с номером {chat_number} не найден")
                hourly_rate = result[0] / _hours_between(start_time, end_time)
                await db.execute(
                    '''UPDATE evening_settings
                       SET work_time_start = ?, work_time_end = ?, hourly_rate = ?
                       WHERE chat_number = ?''',
                    (start_time, end_time, hourly_rate, chat_number)
                )
            else:
                cursor = await db.execute(
                    'SELECT salary_amount FROM chat_settings WHERE chat_number = ?',
                    (chat_number,)
                )
                result = await cursor.fetchone()
                if not result:
                    raise ValueError(f"Чат с номером {chat_number} не найден")
                hourly_rate = result[0] / _hours_between(start_time, end_time)
                await db.execute(
                    '''UPDATE chat_settings
                       SET work_time_start = ?, work_time_end = ?, hourly_rate = ?
                       WHERE chat_number = ?''',
                    (start_time, end_time, hourly_rate, chat_number)
                )
            
            await db.commit()
    
    async def get_chat_settings(self, chat_id: int, is_evening: bool = False) -> Optional[Dict]:
        """Получение настроек чата"""
        async with aiosqlite.connect(self.db_path) as db:
            if is_evening:
                cursor = await db.execute('''
                    SELECT cs.chat_number, es.work_time_start, es.work_time_end, 
                           es.salary_amount, es.hourly_rate
                    FROM chat_settings cs
                    JOIN evening_settings es ON cs.chat_number = es.chat_number
                    WHERE cs.chat_id = ?
                ''', (chat_id,))
            else:
                cursor = await db.execute('''
                    SELECT chat_number, work_time_start, work_time_end, 
                           salary_amount, hourly_rate
                    FROM chat_settings
                    WHERE chat_id = ?
                ''', (chat_id,))
            
            result = await cursor.fetchone()
            if result:
                return {
                    'chat_number': result[0],
                    'work_time_start': result[1],
                    'work_time_end': result[2],
                    'salary_amount': result[3],
                    'hourly_rate': result[4]
                }
            return None
    
    async def get_chat_id_by_number(self, chat_number: int) -> Optional[int]:
        """Получение chat_id по номеру чата"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                'SELECT chat_id FROM chat_settings WHERE chat_number = ?',
                (chat_number,)
            )
            result = await cursor.fetchone()
            return result[0] if result else None
    
    async def add_or_update_worker(self, user_id: int, username: str = None, full_name: str = None):
        """Добавление или обновление работника"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT INTO workers (user_id, username, full_name)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = COALESCE(?, username),
                    full_name = COALESCE(?, full_name)
            ''', (user_id, username, full_name, username, full_name))
            await db.commit()
    
    async def set_custom_rate(self, user_id: int, rate: float):
        """Установка персональной ставки для работника"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'INSERT OR IGNORE INTO workers (user_id) VALUES (?)',
                (user_id,)
            )
            await db.execute(
                'UPDATE workers SET custom_rate = ? WHERE user_id = ?',
                (rate, user_id)
            )
            await db.commit()
    
    async def get_worker_rate(self, user_id: int, chat_id: int, is_evening: bool = False) -> float:
        """Получение ставки работника"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                'SELECT custom_rate FROM workers WHERE user_id = ?',
                (user_id,)
            )
            result = await cursor.fetchone()
            
            if result and result[0] is not None:
                custom_rate = result[0]
                settings = await self.get_chat_settings(chat_id, is_evening)
                if settings:
                    base_hourly = settings['hourly_rate']
                    base_salary = settings['salary_amount']
                    if base_salary > 0:
                        multiplier = custom_rate / base_salary
                        return base_hourly * multiplier
                return custom_rate
            
            settings = await self.get_chat_settings(chat_id, is_evening)
            if settings:
                return settings['hourly_rate']
            
            return 0.0
    
    async def get_worker_info(self, user_id: int) -> Optional[Dict]:
        """Получение информации о работнике"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                'SELECT user_id, username, full_name, custom_rate FROM workers WHERE user_id = ?',
                (user_id,)
            )
            result = await cursor.fetchone()
            if result:
                return {
                    'user_id': result[0],
                    'username': result[1],
                    'full_name': result[2],
                    'custom_rate': result[3]
                }
            return None
    
    async def get_all_active_shifts(self) -> List[Dict]:
        """Получение всех активных смен"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT s.id, s.user_id, s.chat_id, s.shift_type, s.start_time, s.actual_start_time,
                       c.chat_name
                FROM shifts s
                JOIN chats c ON s.chat_id = c.chat_id
                WHERE s.is_active = 1
            ''')
            
            results = await cursor.fetchall()
            shifts = []
            for row in results:
                shifts.append({
                    'id': row[0],
                    'user_id': row[1],
                    'chat_id': row[2],
                    'shift_type': row[3],
                    'start_time': row[4],
                    'actual_start_time': row[5],
                    'chat_name': row[6]
                })
            return shifts
    
    async def get_daily_stats(self, chat_id: int, date: datetime = None, use_chat_rate: bool = False) -> List[Dict]:
        """Получение статистики по работникам за день
        
        Args:
            chat_id: ID чата
            date: Дата (по умолчанию сегодня)
            use_chat_rate: Если True, пересчитывает зарплату по общей ставке чата (для показа в рабочем чате)
        """
        if date is None:
            date = datetime.now()
        
        day_start = datetime.combine(date.date(), datetime.min.time())
        day_end = datetime.combine(date.date(), datetime.max.time())
        
        async with aiosqlite.connect(self.db_path) as db:
            # Получаем настройки чата для пересчета
            chat_settings = None
            if use_chat_rate:
                settings_cursor = await db.execute('''
                    SELECT work_time_start, work_time_end, salary_amount, hourly_rate
                    FROM chat_settings
                    WHERE chat_id = ?
                ''', (chat_id,))
                chat_settings = await settings_cursor.fetchone()
            
            # Получаем закрытые смены за день
            cursor = await db.execute('''
                SELECT 
                    w.user_id,
                    w.username,
                    w.full_name,
                    SUM(s.hours_worked) as total_hours,
                    SUM(s.earned_amount) as total_earned,
                    COUNT(s.id) as shifts_count
                FROM workers w
                JOIN shifts s ON w.user_id = s.user_id
                WHERE s.chat_id = ? 
                    AND s.is_active = 0
                    AND s.end_time >= ?
                    AND s.end_time <= ?
                GROUP BY w.user_id
            ''', (chat_id, day_start, day_end))
            
            results = await cursor.fetchall()
            
            # Преобразуем в словарь для удобства
            stats_dict = {}
            for row in results:
                total_hours = row[3] or 0
                total_earned = row[4] or 0
                
                # Пересчитываем по общей ставке если нужно
                if use_chat_rate and chat_settings:
                    _, _, daily_rate, hourly_rate = chat_settings
                    if hourly_rate and hourly_rate > 0:
                        total_earned = total_hours * hourly_rate
                
                stats_dict[row[0]] = {
                    'user_id': row[0],
                    'username': row[1],
                    'full_name': row[2],
                    'total_hours': total_hours,
                    'total_earned': total_earned,
                    'shifts_count': row[5] or 0
                }
            
            # Добавляем активные смены
            cursor = await db.execute('''
                SELECT s.id, s.user_id, s.chat_id, w.username, w.full_name
                FROM shifts s
                JOIN workers w ON s.user_id = w.user_id
                WHERE s.chat_id = ? AND s.is_active = 1
            ''', (chat_id,))
            
            active_shifts = await cursor.fetchall()
            
            for shift in active_shifts:
                shift_id, user_id, shift_chat_id, username, full_name = shift
                
                if use_chat_rate and chat_settings:
                    # Для рабочего чата - считаем по общей ставке
                    _, _, daily_rate, hourly_rate = chat_settings
                    current = await self.get_current_earnings(user_id, shift_chat_id, shift_id)
                    
                    # Пересчитываем по общей ставке
                    if hourly_rate and hourly_rate > 0:
                        current['earned_amount'] = current['hours_worked'] * hourly_rate
                else:
                    # Для админов и личных сообщений - реальная зарплата
                    current = await self.get_current_earnings(user_id, shift_chat_id, shift_id)
                
                if user_id in stats_dict:
                    stats_dict[user_id]['total_hours'] += current['hours_worked']
                    stats_dict[user_id]['total_earned'] += current['earned_amount']
                else:
                    stats_dict[user_id] = {
                        'user_id': user_id,
                        'username': username,
                        'full_name': full_name,
                        'total_hours': current['hours_worked'],
                        'total_earned': current['earned_amount'],
                        'shifts_count': 1
                    }
            
            # Преобразуем обратно в список и сортируем по заработку
            stats_list = list(stats_dict.values())
            stats_list.sort(key=lambda x: x['total_earned'], reverse=True)
            
            return stats_list
    
    async def start_shift(self, user_id: int, chat_id: int, shift_type: str = 'regular') -> int:
        """Начало смены"""
        period_start, period_end = self.get_current_period()
        now = datetime.now(MSK)
        
        is_evening = (shift_type == 'evening')
        settings = await self.get_chat_settings(chat_id, is_evening)
        
        if not settings:
            raise ValueError("Настройки чата не найдены")
        
        work_start_time = datetime.strptime(settings['work_time_start'], '%H:%M').time()
        work_end_time = datetime.strptime(settings['work_time_end'], '%H:%M').time()
        work_start_datetime, work_end_datetime = _get_work_window(
            now, work_start_time, work_end_time
        )
        
        # ПРОВЕРКА: Если сейчас после окончания рабочего дня - НЕ открываем смену!
        if now >= work_end_datetime:
            raise ValueError("Рабочий день закончен, смена не открывается")
        
        # Проверяем есть ли недавно закрытая смена (в течение последних 5 минут)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT end_time, actual_end_time
                FROM shifts
                WHERE chat_id = ? AND shift_type = ? AND is_active = 0
                AND end_time > datetime('now', '-5 minutes')
                ORDER BY end_time DESC
                LIMIT 1
            ''', (chat_id, shift_type))
            recent_shift = await cursor.fetchone()
        
        # Если открываем смену раньше чем за час до начала - отметка времени пока None
        one_hour_before = work_start_datetime.timestamp() - 3600
        if now.timestamp() < one_hour_before:
            actual_start = None
        # Если в пределах часа до начала - считаем с момента начала смены
        elif now.timestamp() < work_start_datetime.timestamp():
            actual_start = work_start_datetime
        # Если уже после начала смены - считаем с НАЧАЛА СМЕНЫ (опоздание учитывается!)
        # ИЛИ если была недавно закрытая смена - с момента ее закрытия (простой учитывается!)
        else:
            if recent_shift and recent_shift[1]:  # Если есть недавняя смена с actual_end_time
                last_end_time = datetime.fromisoformat(recent_shift[1])
                # Если последняя смена закончилась позже начала рабочего дня - начинаем с момента закрытия
                if last_end_time > work_start_datetime:
                    actual_start = last_end_time
                else:
                    actual_start = work_start_datetime
            else:
                # Если нет недавних смен - начинаем с начала рабочего дня (опоздание учитывается)
                actual_start = work_start_datetime
        
        await self.add_or_update_worker(user_id)
        
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                INSERT INTO shifts (user_id, chat_id, shift_type, start_time, actual_start_time, 
                                   period_start, period_end, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            ''', (user_id, chat_id, shift_type, now, actual_start, period_start, period_end))
            await db.commit()
            return cursor.lastrowid
    
    async def end_shift(self, shift_id: int) -> Dict:
        """Окончание смены"""
        now = datetime.now(MSK)
        
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT user_id, chat_id, shift_type, start_time, actual_start_time
                FROM shifts WHERE id = ? AND is_active = 1
            ''', (shift_id,))
            
            result = await cursor.fetchone()
            if not result:
                raise ValueError("Активная смена не найдена")
            
            user_id, chat_id, shift_type, start_time, actual_start_time = result
            
            # Получаем настройки чата для расчета зарплаты
            is_evening = (shift_type == 'evening')
            settings = await self.get_chat_settings(chat_id, is_evening)
            
            if not settings:
                # Если настроек нет - просто закрываем смену без расчетов
                await db.execute(
                    'UPDATE shifts SET is_active = 0, end_time = ? WHERE id = ?',
                    (now, shift_id)
                )
                await db.commit()
                return {
                    'user_id': user_id,
                    'hours_worked': 0,
                    'earned_amount': 0
                }
            
            work_start_time = datetime.strptime(settings['work_time_start'], '%H:%M').time()
            work_end_time = datetime.strptime(settings['work_time_end'], '%H:%M').time()
            work_start_datetime, work_end_datetime = _get_work_window(
                now, work_start_time, work_end_time
            )
            
            # Если actual_start_time еще нет (смена началась слишком рано), ставим сейчас
            # Но если сейчас все еще слишком рано - значит смена нулевая
            if actual_start_time is None:
                if now < work_start_datetime:
                    # Смена закрыта ДО начала рабочего дня
                    actual_end = now
                    actual_start = now # Нулевая длительность
                else:
                    # Смена началась
                    actual_start = work_start_datetime
            else:
                actual_start = datetime.fromisoformat(actual_start_time) if isinstance(actual_start_time, str) else actual_start_time
                work_end_datetime = datetime.combine(
                    actual_start.date(), work_end_time, tzinfo=MSK
                )
                if work_end_datetime <= actual_start:
                    work_end_datetime += timedelta(days=1)

            # Определяем фактическое время окончания
            # Если закрываем ПОСЛЕ конца рабочего дня - считаем до конца рабочего дня
            if now > work_end_datetime:
                actual_end = work_end_datetime
            else:
                actual_end = now
            
            # Расчет времени и денег
            if actual_end > actual_start:
                duration = actual_end - actual_start
                hours_worked = duration.total_seconds() / 3600
            else:
                hours_worked = 0
                
            # Получаем ставку работника
            rate = await self.get_worker_rate(user_id, chat_id, is_evening)
            earned = hours_worked * rate
            
            # Стандартный заработок (для отображения в чате)
            standard_earned = 0
            if settings['hourly_rate'] > 0:
                 standard_earned = hours_worked * settings['hourly_rate']
            
            await db.execute('''
                UPDATE shifts 
                SET is_active = 0, end_time = ?, actual_end_time = ?,
                    hours_worked = ?, earned_amount = ?
                WHERE id = ?
            ''', (now, actual_end, hours_worked, earned, shift_id))
            
            await db.commit()
            
            return {
                'user_id': user_id,
                'hours_worked': hours_worked,
                'earned_amount': earned,
                'standard_earned': standard_earned
            }

    async def get_active_shift(self, chat_id: int, shift_type: str = 'regular') -> Optional[Dict]:
        """Получение активной смены в чате (любого пользователя)"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT id, user_id, start_time, actual_start_time
                FROM shifts 
                WHERE chat_id = ? AND shift_type = ? AND is_active = 1
                ORDER BY start_time DESC
                LIMIT 1
            ''', (chat_id, shift_type))
            
            result = await cursor.fetchone()
            if result:
                return {
                    'id': result[0],
                    'user_id': result[1],
                    'start_time': result[2],
                    'actual_start_time': result[3]
                }
            return None

    async def get_current_earnings(self, user_id: int, chat_id: int, shift_id: int) -> Dict:
        """Расчет текущего заработка для активной смены"""
        now = datetime.now(MSK)
        
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT shift_type, actual_start_time
                FROM shifts WHERE id = ?
            ''', (shift_id,))
            result = await cursor.fetchone()
            
            if not result:
                return {'hours_worked': 0, 'earned_amount': 0}
                
            shift_type, actual_start_time = result
            is_evening = (shift_type == 'evening')
            
            settings = await self.get_chat_settings(chat_id, is_evening)
            if not settings:
                return {'hours_worked': 0, 'earned_amount': 0}

            if actual_start_time is None:
                # Смена еще не началась фактически
                return {'hours_worked': 0, 'earned_amount': 0}
            
            actual_start = datetime.fromisoformat(actual_start_time) if isinstance(actual_start_time, str) else actual_start_time
            
            # Ограничиваем концом рабочего дня
            work_end_time = datetime.strptime(settings['work_time_end'], '%H:%M').time()
            work_end_datetime = datetime.combine(actual_start.date(), work_end_time, tzinfo=MSK)
            if work_end_datetime <= actual_start:
                work_end_datetime += timedelta(days=1)
            
            if now > work_end_datetime:
                calc_end = work_end_datetime
            else:
                calc_end = now
                
            if calc_end > actual_start:
                duration = calc_end - actual_start
                hours_worked = duration.total_seconds() / 3600
            else:
                hours_worked = 0
                
            rate = await self.get_worker_rate(user_id, chat_id, is_evening)
            earned = hours_worked * rate
            
            return {
                'hours_worked': hours_worked,
                'earned_amount': earned
            }

    async def get_period_stats(self, user_id: int, chat_id: int) -> Dict:
        """Статистика за текущий период"""
        period_start, period_end = self.get_current_period()
        
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT SUM(hours_worked), SUM(earned_amount)
                FROM shifts
                WHERE user_id = ? AND chat_id = ? 
                AND is_active = 0
                AND end_time >= ? AND end_time <= ?
            ''', (user_id, chat_id, period_start, period_end))
            
            result = await cursor.fetchone()
            
            return {
                'hours_worked': result[0] or 0,
                'earned_amount': result[1] or 0
            }

    async def get_all_chats(self, chat_type: str = None) -> List[Dict]:
        """Получение всех чатов"""
        async with aiosqlite.connect(self.db_path) as db:
            if chat_type:
                cursor = await db.execute('SELECT chat_id, chat_name FROM chats WHERE chat_type = ?', (chat_type,))
            else:
                cursor = await db.execute('SELECT chat_id, chat_name FROM chats')
            
            results = await cursor.fetchall()
            return [{'chat_id': r[0], 'chat_name': r[1]} for r in results]

    async def schedule_message_deletion(self, chat_id: int, message_id: int, delay_seconds: int = 60):
        """Запланировать удаление сообщения"""
        delete_at = datetime.now() + timedelta(seconds=delay_seconds)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'INSERT INTO messages_to_delete (chat_id, message_id, delete_at) VALUES (?, ?, ?)',
                (chat_id, message_id, delete_at)
            )
            await db.commit()

    async def get_messages_to_delete(self) -> List[Tuple[int, int]]:
        """Получить сообщения, которые пора удалить"""
        now = datetime.now()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                'SELECT id, chat_id, message_id FROM messages_to_delete WHERE delete_at <= ?',
                (now,)
            )
            rows = await cursor.fetchall()
            
            messages = []
            ids_to_remove = []
            for row in rows:
                messages.append((row[1], row[2]))
                ids_to_remove.append(row[0])
            
            if ids_to_remove:
                placeholders = ','.join('?' * len(ids_to_remove))
                await db.execute(f'DELETE FROM messages_to_delete WHERE id IN ({placeholders})', ids_to_remove)
                await db.commit()
                
            return messages

    async def delete_expired_messages(self, bot):
        """Удаление просроченных сообщений"""
        messages = await self.get_messages_to_delete()
        for chat_id, message_id in messages:
            try:
                await bot.delete_message(chat_id, message_id)
            except Exception:
                pass  # Игнорируем ошибки при удалении (например, если сообщение уже удалено)

    @staticmethod
    def _session_digest(token: str) -> str:
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    async def delete_web_session(self, token: str):
        """Удаление сессии (логаут)"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'DELETE FROM web_sessions WHERE session_token IN (?, ?)',
                (self._session_digest(token), token),
            )
            await db.commit()

    async def get_all_web_users(self) -> List[Dict]:
        """Получение всех веб-пользователей с информацией о Telegram"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT wu.id, wu.username, wu.role, wu.telegram_id, wu.telegram_linked, 
                       wu.managed_chat_ids, wu.created_at, wu.last_login, w.full_name
                FROM web_users wu
                LEFT JOIN workers w ON wu.telegram_id = w.user_id
                ORDER BY wu.created_at DESC
            ''')
            results = await cursor.fetchall()
            return [{
                'id': r[0],
                'username': r[1],
                'role': r[2],
                'telegram_id': r[3],
                'telegram_linked': bool(r[4]),
                'managed_chat_ids': r[5],
                'created_at': r[6],
                'last_login': r[7],
                'full_name': r[8]
            } for r in results]
    
    async def get_all_telegram_users(self) -> List[Dict]:
        """Получение всех пользователей Telegram (из workers)"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT w.user_id, w.username, w.full_name, w.custom_rate,
                       (SELECT COUNT(*) FROM shifts WHERE user_id = w.user_id) as shift_count,
                       (SELECT GROUP_CONCAT(DISTINCT chat_id) FROM shifts WHERE user_id = w.user_id) as chat_ids
                FROM workers w
                ORDER BY w.user_id
            ''')
            results = await cursor.fetchall()
            return [{
                'telegram_id': r[0],
                'username': r[1],
                'full_name': r[2],
                'custom_rate': r[3],
                'shift_count': r[4] or 0,
                'chat_ids': r[5]
            } for r in results]

    async def get_all_chats_full_info(self) -> List[Dict]:
        """Полная информация о всех чатах для админки"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT c.chat_id, c.chat_name, c.chat_type, 
                       cs.work_time_start, cs.work_time_end, cs.salary_amount, cs.hourly_rate,
                       c.admin_chat_id
                FROM chats c
                LEFT JOIN chat_settings cs ON c.chat_id = cs.chat_id
            ''')
            results = await cursor.fetchall()
            return [{
                'chat_id': r[0],
                'chat_name': r[1],
                'chat_type': r[2],
                'work_time': f"{r[3]}-{r[4]}" if r[3] else "Не настроено",
                'salary': r[5],
                'rate': r[6],
                'admin_chat_id': r[7]
            } for r in results]

    async def get_all_workers_stats(self, chat_ids: List[int] = None) -> List[Dict]:
        """Статистика всех работников для админки (опционально фильтр по чатам)"""
        async with aiosqlite.connect(self.db_path) as db:
            query = '''
                SELECT w.user_id, w.username, w.full_name, w.custom_rate,
                       COUNT(s.id) as shifts_count,
                       SUM(s.hours_worked) as total_hours,
                       SUM(s.earned_amount) as total_earned
                FROM workers w
                LEFT JOIN shifts s ON w.user_id = s.user_id
            '''
            
            params = []
            if chat_ids is not None:
                if not chat_ids:
                    # Если передан пустой список чатов - возвращаем пусто
                    return []
                placeholders = ','.join('?' * len(chat_ids))
                # Фильтруем смены только по разрешенным чатам
                query += f' WHERE s.chat_id IN ({placeholders})'
                params.extend(chat_ids)
            
            query += '''
                GROUP BY w.user_id
                HAVING shifts_count > 0
                ORDER BY total_earned DESC
            '''
            
            cursor = await db.execute(query, params)
            results = await cursor.fetchall()
            return [{
                'user_id': r[0],
                'username': r[1],
                'full_name': r[2],
                'custom_rate': r[3],
                'shifts_count': r[4],
                'total_hours': r[5] or 0,
                'total_earned': r[6] or 0
            } for r in results]

    async def get_stats_for_period_by_dates(self, user_id: int, period_start: datetime, period_end: datetime, chat_id: int = None) -> Dict:
        """Получение статистики за период по датам (новый метод)"""
        async with aiosqlite.connect(self.db_path) as db:
            if chat_id:
                # Закрытые смены в диапазоне дат
                cursor = await db.execute('''
                    SELECT SUM(hours_worked), SUM(earned_amount)
                    FROM shifts
                    WHERE user_id = ? AND chat_id = ? AND is_active = 0
                        AND end_time >= ? AND end_time <= ?
                ''', (user_id, chat_id, period_start, period_end))
            else:
                # Закрытые смены в диапазоне дат (все чаты)
                cursor = await db.execute('''
                    SELECT SUM(hours_worked), SUM(earned_amount)
                    FROM shifts
                    WHERE user_id = ? AND is_active = 0
                        AND end_time >= ? AND end_time <= ?
                ''', (user_id, period_start, period_end))
            
            result = await cursor.fetchone()
            
            hours = result[0] or 0
            amount = result[1] or 0
            
            # Ищем активные смены (только для текущего периода)
            now = datetime.now()
            if period_start <= now <= period_end:
                if chat_id:
                    active_cursor = await db.execute('''
                        SELECT id FROM shifts
                        WHERE user_id = ? AND chat_id = ? AND is_active = 1
                            AND start_time >= ? AND start_time <= ?
                    ''', (user_id, chat_id, period_start, period_end))
                    
                    active_shifts = await active_cursor.fetchall()
                    
                    for shift in active_shifts:
                        shift_id = shift[0]
                        current = await self.get_current_earnings(user_id, chat_id, shift_id)
                        hours += current['hours_worked']
                        amount += current['earned_amount']
                else:
                    active_cursor = await db.execute('''
                        SELECT id, chat_id FROM shifts
                        WHERE user_id = ? AND is_active = 1
                            AND start_time >= ? AND start_time <= ?
                    ''', (user_id, period_start, period_end))
                    
                    active_shifts = await active_cursor.fetchall()
                    
                    for shift in active_shifts:
                        shift_id = shift[0]
                        shift_chat_id = shift[1]
                        current = await self.get_current_earnings(user_id, shift_chat_id, shift_id)
                        hours += current['hours_worked']
                        amount += current['earned_amount']
            
            return {
                'hours_worked': hours,
                'earned_amount': amount,
                'period_start': period_start,
                'period_end': period_end
            }
    
    async def can_use_command(self, user_id: int, chat_id: int, command: str, cooldown_seconds: int = 180) -> bool:
        """Проверка возможности использования команды"""
        now = datetime.now()
        
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT last_used FROM command_usage
                WHERE user_id = ? AND chat_id = ? AND command = ?
            ''', (user_id, chat_id, command))
            
            result = await cursor.fetchone()
            
            if result:
                last_used = datetime.fromisoformat(result[0])
                if (now - last_used).total_seconds() < cooldown_seconds:
                    return False
            
            await db.execute('''
                INSERT INTO command_usage (user_id, chat_id, command, last_used)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, chat_id, command) DO UPDATE SET last_used = ?
            ''', (user_id, chat_id, command, now, now))
            
            await db.commit()
            return True
    
    async def schedule_message_deletion(self, chat_id: int, message_id: int, delay_seconds: int = 300):
        """Запланировать удаление сообщения"""
        delete_at = datetime.now() + timedelta(seconds=delay_seconds)
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT INTO messages_to_delete (chat_id, message_id, delete_at)
                VALUES (?, ?, ?)
            ''', (chat_id, message_id, delete_at))
            await db.commit()
    
    async def get_messages_to_delete(self) -> List[Tuple[int, int]]:
        """Получить сообщения, которые нужно удалить"""
        now = datetime.now()
        
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT id, chat_id, message_id FROM messages_to_delete
                WHERE delete_at <= ?
            ''', (now,))
            
            messages = await cursor.fetchall()
            
            if messages:
                ids = [msg[0] for msg in messages]
                placeholders = ','.join('?' * len(ids))
                await db.execute(f'DELETE FROM messages_to_delete WHERE id IN ({placeholders})', ids)
                await db.commit()
            
            return [(msg[1], msg[2]) for msg in messages]
    
    async def delete_expired_messages(self, bot):
        """Удаление просроченных сообщений"""
        messages = await self.get_messages_to_delete()
        for chat_id, message_id in messages:
            try:
                await bot.delete_message(chat_id, message_id)
            except Exception:
                pass  # Игнорируем ошибки при удалении (например, если сообщение уже удалено)

    async def create_web_user(self, username: str, password_hash: str, role: str = 'guest') -> int:
        """Создание веб-пользователя"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                INSERT INTO web_users (username, password_hash, role)
                VALUES (?, ?, ?)
            ''', (username, password_hash, role))
            await db.commit()
            return cursor.lastrowid

    async def get_web_user(self, username: str) -> Optional[Dict]:
        """Получение веб-пользователя по логину"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT id, username, password_hash, role, telegram_id, telegram_linked, managed_chat_ids, created_at, last_login
                FROM web_users 
                WHERE username = ?
            ''', (username,))
            row = await cursor.fetchone()
            if row:
                return {
                    'id': row[0],
                    'username': row[1],
                    'password_hash': row[2],
                    'role': row[3],
                    'telegram_id': row[4],
                    'telegram_linked': bool(row[5]),
                    'managed_chat_ids': row[6],
                    'created_at': row[7],
                    'last_login': row[8]
                }
            return None

    async def get_web_user_by_id(self, user_id: int) -> Optional[Dict]:
        """Получение веб-пользователя по ID"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT id, username, password_hash, role, telegram_id, telegram_linked, managed_chat_ids, created_at, last_login
                FROM web_users 
                WHERE id = ?
            ''', (user_id,))
            row = await cursor.fetchone()
            if row:
                return {
                    'id': row[0],
                    'username': row[1],
                    'password_hash': row[2],
                    'role': row[3],
                    'telegram_id': row[4],
                    'telegram_linked': bool(row[5]),
                    'managed_chat_ids': row[6],
                    'created_at': row[7],
                    'last_login': row[8]
                }
            return None

    async def create_web_session(self, web_user_id: int, token: str, expires_at: datetime, ip: str = None, user_agent: str = None):
        """Создание веб-сессии without storing the browser token in plaintext."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT INTO web_sessions (session_token, web_user_id, expires_at, ip_address, user_agent)
                VALUES (?, ?, ?, ?, ?)
            ''', (self._session_digest(token), web_user_id, expires_at, ip, user_agent))
            await db.commit()

    async def get_user_by_session(self, token: str) -> Optional[Dict]:
        """Получение пользователя by raw cookie token or legacy value."""
        now = datetime.now()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT u.id, u.username, u.role, u.telegram_id, u.telegram_linked, u.managed_chat_ids, s.expires_at
                FROM web_sessions s
                JOIN web_users u ON s.web_user_id = u.id
                WHERE s.session_token IN (?, ?)
            ''', (self._session_digest(token), token))
            row = await cursor.fetchone()
            
            if row:
                expires_at = datetime.fromisoformat(row[6]) if isinstance(row[6], str) else row[6]
                if expires_at > now:
                    # A legacy raw value can still be accepted during a
                    # rolling migration; normalize it immediately.
                    await db.execute(
                        'UPDATE web_sessions SET session_token = ? WHERE session_token = ?',
                        (self._session_digest(token), token),
                    )
                    await db.commit()
                    return {
                        'id': row[0],
                        'username': row[1],
                        'role': row[2],
                        'telegram_id': row[3],
                        'telegram_linked': bool(row[4]),
                        'managed_chat_ids': row[5]
                    }
                else:
                    # Удаляем просроченную сессию
                    await db.execute(
                        'DELETE FROM web_sessions WHERE session_token IN (?, ?)',
                        (self._session_digest(token), token),
                    )
                    await db.commit()
            return None
    
    # ========== LINK KEYS METHODS ==========
    
    async def create_link_key(self, telegram_id: int) -> str:
        """Создание одноразового ключа для связывания аккаунта"""
        import secrets
        key_code = secrets.token_hex(8)  # 16 символов
        expires_at = datetime.now() + timedelta(hours=1)  # Действует 1 час
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT INTO link_keys (key_code, telegram_id, expires_at)
                VALUES (?, ?, ?)
            ''', (self._session_digest(key_code), telegram_id, expires_at))
            await db.commit()
        
        return key_code
    
    async def verify_link_key(self, key_code: str) -> Optional[int]:
        """Проверка и использование ключа связывания, возвращает telegram_id"""
        now = datetime.now()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT telegram_id, expires_at, used
                FROM link_keys
                WHERE key_code = ?
            ''', (self._session_digest(key_code),))
            row = await cursor.fetchone()
            
            if not row:
                return None
            
            telegram_id = row[0]
            expires_at = datetime.fromisoformat(row[1]) if isinstance(row[1], str) else row[1]
            used = row[2]
            
            if used or expires_at < now:
                return None
            
            return telegram_id
    
    async def mark_key_used(self, key_code: str, web_user_id: int):
        """Отметить ключ как использованный"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                UPDATE link_keys
                SET used = 1, used_by_user_id = ?
                WHERE key_code = ? AND used = 0
            ''', (web_user_id, self._session_digest(key_code)))
            await db.commit()
    
    async def link_telegram_account(self, web_user_id: int, telegram_id: int):
        """Связать веб-аккаунт с Telegram аккаунтом"""
        async with aiosqlite.connect(self.db_path) as db:
            # Получаем информацию о работнике из Telegram
            cursor = await db.execute('''
                SELECT user_id, username, full_name
                FROM workers
                WHERE user_id = ?
            ''', (telegram_id,))
            worker = await cursor.fetchone()
            
            # Определяем роль на основе существующих прав
            role = 'user'  # По умолчанию обычный пользователь
            
            # Проверяем, является ли супер-админом
            from config import SUPER_ADMINS, VIEW_ONLY_ADMINS
            if telegram_id in SUPER_ADMINS:
                role = 'super_admin'
            elif telegram_id in VIEW_ONLY_ADMINS:
                role = 'view_admin'
            
            await db.execute('''
                UPDATE web_users
                SET telegram_id = ?, telegram_linked = 1, role = ?
                WHERE id = ?
            ''', (telegram_id, role, web_user_id))
            await db.commit()
    
    async def update_user_role(self, web_user_id: int, role: str, managed_chat_ids: str = None):
        """Обновить роль пользователя и управляемые чаты"""
        async with aiosqlite.connect(self.db_path) as db:
            if managed_chat_ids is not None:
                await db.execute('''
                    UPDATE web_users
                    SET role = ?, managed_chat_ids = ?
                    WHERE id = ?
                ''', (role, managed_chat_ids, web_user_id))
            else:
                await db.execute('''
                    UPDATE web_users
                    SET role = ?
                    WHERE id = ?
                ''', (role, web_user_id))
            await db.commit()
    
    async def add_pending_report(self, report_type: str, target_id: int, report_text: str, period_start: datetime):
        """Добавить отчет в очередь на отправку"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT INTO pending_reports (report_type, target_id, report_text, period_start)
                VALUES (?, ?, ?, ?)
            ''', (report_type, target_id, report_text, period_start))
            await db.commit()

    # ========== VPN STORE METHODS ==========

    async def get_vpn_products(self, only_active: bool = True) -> List[Dict]:
        """Получить список товаров VPN"""
        async with aiosqlite.connect(self.db_path) as db:
            query = 'SELECT id, name, description, price, country, period_days, is_active FROM vpn_products'
            if only_active:
                query += ' WHERE is_active = 1'
            
            cursor = await db.execute(query)
            results = await cursor.fetchall()
            
            products = []
            for row in results:
                # Считаем количество доступных ключей
                count_cursor = await db.execute(
                    'SELECT COUNT(*) FROM vpn_keys WHERE product_id = ? AND is_sold = 0',
                    (row[0],)
                )
                count = (await count_cursor.fetchone())[0]
                
                products.append({
                    'id': row[0],
                    'name': row[1],
                    'description': row[2],
                    'price': row[3],
                    'country': row[4],
                    'period_days': row[5],
                    'is_active': row[6],
                    'stock': count
                })
            return products

    async def add_vpn_product(self, name: str, description: str, price: float, country: str, period_days: int = 30) -> int:
        """Добавить новый товар VPN"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                INSERT INTO vpn_products (name, description, price, country, period_days)
                VALUES (?, ?, ?, ?, ?)
            ''', (name, description, price, country, period_days))
            await db.commit()
            return cursor.lastrowid

    async def add_vpn_key(self, product_id: int, key_data: str):
        """Добавить ключ для товара"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT INTO vpn_keys (product_id, key_data)
                VALUES (?, ?)
            ''', (product_id, key_data))
            await db.commit()

    async def buy_vpn_product(self, user_id: int, product_id: int) -> Dict:
        """Покупка VPN (получение ключа)"""
        async with aiosqlite.connect(self.db_path) as db:
            # Проверяем наличие товара и цены
            cursor = await db.execute('SELECT price, name FROM vpn_products WHERE id = ?', (product_id,))
            product = await cursor.fetchone()
            if not product:
                raise ValueError("Товар не найден")
            
            price = product[0]
            
            # Ищем свободный ключ
            cursor = await db.execute('''
                SELECT id, key_data FROM vpn_keys 
                WHERE product_id = ? AND is_sold = 0 
                LIMIT 1
            ''', (product_id,))
            key = await cursor.fetchone()
            
            if not key:
                raise ValueError("Нет свободных ключей")
            
            key_id = key[0]
            key_data = key[1]
            
            # Отмечаем ключ как проданный
            await db.execute('''
                UPDATE vpn_keys 
                SET is_sold = 1, sold_at = CURRENT_TIMESTAMP, sold_to_user_id = ? 
                WHERE id = ?
            ''', (user_id, key_id))
            
            # Создаем заказ
            await db.execute('''
                INSERT INTO vpn_orders (user_id, product_id, key_id, amount, status)
                VALUES (?, ?, ?, ?, 'completed')
            ''', (user_id, product_id, key_id, price))
            
            await db.commit()
            
            return {
                'key_data': key_data,
                'product_name': product[1],
                'price': price
            }

    async def get_user_vpn_orders(self, user_id: int) -> List[Dict]:
        """Получить покупки пользователя"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT o.id, p.name, k.key_data, o.amount, o.created_at
                FROM vpn_orders o
                JOIN vpn_products p ON o.product_id = p.id
                JOIN vpn_keys k ON o.key_id = k.id
                WHERE o.user_id = ?
                ORDER BY o.created_at DESC
            ''', (user_id,))
            
            results = await cursor.fetchall()
            orders = []
            for row in results:
                orders.append({
                    'id': row[0],
                    'product_name': row[1],
                    'key_data': row[2],
                    'amount': row[3],
                    'date': row[4]
                })
            return orders
    
    async def get_pending_reports(self, max_attempts: int = 5) -> List[Dict]:
        """Получить отчеты ожидающие отправки"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT id, report_type, target_id, report_text, period_start, attempts
                FROM pending_reports
                WHERE attempts < ?
                ORDER BY created_at ASC
            ''', (max_attempts,))
            
            results = await cursor.fetchall()
            reports = []
            for row in results:
                reports.append({
                    'id': row[0],
                    'report_type': row[1],
                    'target_id': row[2],
                    'report_text': row[3],
                    'period_start': row[4],
                    'attempts': row[5]
                })
            return reports
    
    async def update_report_attempt(self, report_id: int, success: bool):
        """Обновить попытку отправки отчета"""
        async with aiosqlite.connect(self.db_path) as db:
            if success:
                # Удаляем отчет если отправлен успешно
                await db.execute('DELETE FROM pending_reports WHERE id = ?', (report_id,))
            else:
                # Увеличиваем счетчик попыток
                await db.execute('''
                    UPDATE pending_reports 
                    SET attempts = attempts + 1, last_attempt = ?
                    WHERE id = ?
                ''', (datetime.now(), report_id))
            await db.commit()
    
    async def clear_old_pending_reports(self, days: int = 7):
        """Очистить старые неотправленные отчеты"""
        cutoff_date = datetime.now() - timedelta(days=days)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('DELETE FROM pending_reports WHERE created_at < ?', (cutoff_date,))
            await db.commit()
    
    async def get_all_chats(self) -> List[Dict]:
        """Получить список всех чатов"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT chat_id, chat_name
                FROM chats
                ORDER BY chat_name
            ''')
            rows = await cursor.fetchall()
            return [{'chat_id': row[0], 'chat_name': row[1]} for row in rows]
    
    async def get_chat_full_stats(self, chat_id: int) -> Dict:
        """Получить полную статистику чата для бэкапа"""
        async with aiosqlite.connect(self.db_path) as db:
            # Получаем всех работников этого чата
            cursor = await db.execute('''
                SELECT DISTINCT 
                    u.user_id,
                    u.username,
                    u.full_name
                FROM shifts s
                JOIN users u ON s.user_id = u.user_id
                WHERE s.chat_id = ?
                ORDER BY u.full_name
            ''', (chat_id,))
            
            workers = await cursor.fetchall()
            workers_data = []
            
            for worker in workers:
                user_id = worker[0]
                
                # Статистика работника
                cursor = await db.execute('''
                    SELECT 
                        COUNT(*) as shifts_count,
                        SUM(hours_worked) as total_hours,
                        SUM(earned_amount) as total_earned
                    FROM shifts
                    WHERE user_id = ? AND chat_id = ?
                ''', (user_id, chat_id))
                
                stats = await cursor.fetchone()
                
                workers_data.append({
                    'user_id': user_id,
                    'username': worker[1],
                    'full_name': worker[2],
                    'shifts_count': stats[0] or 0,
                    'total_hours': stats[1] or 0,
                    'total_earned': stats[2] or 0
                })
            
            # Общая статистика чата
            cursor = await db.execute('''
                SELECT 
                    COUNT(*) as total_shifts,
                    SUM(hours_worked) as total_hours,
                    SUM(earned_amount) as total_earned
                FROM shifts
                WHERE chat_id = ?
            ''', (chat_id,))
            
            total_stats = await cursor.fetchone()
            
            return {
                'workers': workers_data,
                'total_shifts': total_stats[0] or 0,
                'total_hours': total_stats[1] or 0,
                'total_earned': total_stats[2] or 0
            }
    
    async def get_historical_periods(self, count: int = 6) -> List[Tuple[datetime, datetime]]:
        """Получить последние N периодов"""
        periods = []
        now = datetime.now()
        current_year = now.year
        current_month = now.month
        current_day = now.day
        
        # Определяем с какого периода начинаем
        if current_day <= 15:
            # Сейчас первая половина - начинаем с нее
            start_month = current_month
            start_year = current_year
            is_second_half = False
        else:
            # Сейчас вторая половина - начинаем с нее
            start_month = current_month
            start_year = current_year
            is_second_half = True
        
        for i in range(count):
            if is_second_half:
                # Период 16-конец месяца
                period_start = datetime(start_year, start_month, 16)
                if start_month == 12:
                    next_month = datetime(start_year + 1, 1, 1)
                else:
                    next_month = datetime(start_year, start_month + 1, 1)
                period_end = next_month - timedelta(seconds=1)
                
                # Следующий период - первая половина этого же месяца
                is_second_half = False
            else:
                # Период 1-15 число
                period_start = datetime(start_year, start_month, 1)
                period_end = datetime(start_year, start_month, 15, 23, 59, 59)
                
                # Следующий период - вторая половина предыдущего месяца
                is_second_half = True
                if start_month == 1:
                    start_month = 12
                    start_year -= 1
                else:
                    start_month -= 1
            
            periods.append((period_start, period_end))
        
        return periods
    
    async def get_all_workers_for_period(self, period_start: datetime) -> List[Dict]:
        """Получить всех работников с их статистикой за период"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT 
                    w.user_id, 
                    w.username, 
                    w.full_name, 
                    w.custom_rate,
                    c.chat_id,
                    c.chat_name,
                    SUM(s.hours_worked) as total_hours,
                    SUM(s.earned_amount) as total_earned,
                    COUNT(s.id) as shifts_count
                FROM workers w
                LEFT JOIN shifts s ON w.user_id = s.user_id AND s.period_start = ? AND s.is_active = 0
                LEFT JOIN chats c ON s.chat_id = c.chat_id
                WHERE s.id IS NOT NULL
                GROUP BY w.user_id, c.chat_id
                ORDER BY c.chat_id, w.user_id
            ''', (period_start,))
            
            results = await cursor.fetchall()
            
            workers_stats = []
            for row in results:
                workers_stats.append({
                    'user_id': row[0],
                    'username': row[1],
                    'full_name': row[2],
                    'custom_rate': row[3],
                    'chat_id': row[4],
                    'chat_name': row[5],
                    'total_hours': row[6] or 0,
                    'total_earned': row[7] or 0,
                    'shifts_count': row[8] or 0
                })
            
            return workers_stats
    
    async def get_all_chats(self, chat_type: str = 'work') -> List[Dict]:
        """Получить все чаты определенного типа"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT chat_id, chat_name, chat_type, admin_chat_id
                FROM chats
                WHERE chat_type = ?
            ''', (chat_type,))
            
            results = await cursor.fetchall()
            
            chats = []
            for row in results:
                chats.append({
                    'chat_id': row[0],
                    'chat_name': row[1],
                    'chat_type': row[2],
                    'admin_chat_id': row[3]
                })
            
            return chats
    
    async def get_chat_stats_by_dates(self, chat_id: int, period_start: datetime, period_end: datetime, include_active: bool = True, use_chat_rate: bool = False) -> List[Dict]:
        """Получить статистику по чату за период по датам (новый метод)
        
        Args:
            chat_id: ID чата
            period_start: Начало периода
            period_end: Конец периода  
            include_active: Учитывать ли активные смены
            use_chat_rate: Если True, пересчитывает по общей ставке чата (для показа в рабочем чате)
        """
        async with aiosqlite.connect(self.db_path) as db:
            # Получаем настройки чата для пересчета
            chat_settings = None
            if use_chat_rate:
                settings_cursor = await db.execute('''
                    SELECT work_time_start, work_time_end, salary_amount, hourly_rate
                    FROM chat_settings
                    WHERE chat_id = ?
                ''', (chat_id,))
                chat_settings = await settings_cursor.fetchone()
            # Закрытые смены в диапазоне дат
            cursor = await db.execute('''
                SELECT 
                    w.user_id,
                    w.username,
                    w.full_name,
                    SUM(s.hours_worked) as total_hours,
                    SUM(s.earned_amount) as total_earned
                FROM workers w
                JOIN shifts s ON w.user_id = s.user_id
                WHERE s.chat_id = ? AND s.is_active = 0
                    AND s.end_time >= ? AND s.end_time <= ?
                GROUP BY w.user_id
            ''', (chat_id, period_start, period_end))
            
            results = await cursor.fetchall()
            
            # Словарь для хранения статистики
            stats_dict = {}
            for row in results:
                total_hours = row[3] or 0
                total_earned = row[4] or 0
                
                # Пересчитываем по общей ставке если нужно
                if use_chat_rate and chat_settings:
                    _, _, daily_rate, hourly_rate = chat_settings
                    if hourly_rate and hourly_rate > 0:
                        total_earned = total_hours * hourly_rate
                
                stats_dict[row[0]] = {
                    'user_id': row[0],
                    'username': row[1],
                    'full_name': row[2],
                    'total_hours': total_hours,
                    'total_earned': total_earned
                }
            
            # Добавляем активные смены если нужно
            if include_active:
                now = datetime.now()
                if period_start <= now <= period_end:
                    active_cursor = await db.execute('''
                        SELECT s.id, s.user_id, w.username, w.full_name
                        FROM shifts s
                        JOIN workers w ON s.user_id = w.user_id
                        WHERE s.chat_id = ? AND s.is_active = 1
                            AND s.start_time >= ? AND s.start_time <= ?
                    ''', (chat_id, period_start, period_end))
                    
                    active_shifts = await active_cursor.fetchall()
                    
                    for shift in active_shifts:
                        shift_id, user_id, username, full_name = shift
                        current = await self.get_current_earnings(user_id, chat_id, shift_id)
                        
                        # Пересчитываем по общей ставке если нужно
                        if use_chat_rate and chat_settings:
                            _, _, daily_rate, hourly_rate = chat_settings
                            if hourly_rate and hourly_rate > 0:
                                current['earned_amount'] = current['hours_worked'] * hourly_rate
                        
                        if user_id in stats_dict:
                            stats_dict[user_id]['total_hours'] += current['hours_worked']
                            stats_dict[user_id]['total_earned'] += current['earned_amount']
                        else:
                            stats_dict[user_id] = {
                                'user_id': user_id,
                                'username': username,
                                'full_name': full_name,
                                'total_hours': current['hours_worked'],
                                'total_earned': current['earned_amount']
                            }
            
            # Преобразуем в список и сортируем
            stats_list = list(stats_dict.values())
            stats_list.sort(key=lambda x: x['total_earned'], reverse=True)
            
            return stats_list
    
    async def get_chat_stats_for_period(self, chat_id: int, period_start: datetime, include_active: bool = True, use_chat_rate: bool = False) -> List[Dict]:
        """Получить статистику по всем работникам в чате за период
        
        Args:
            chat_id: ID чата
            period_start: Начало периода
            include_active: Учитывать ли активные смены (по умолчанию True для текущего периода)
            use_chat_rate: Если True, пересчитывает по общей ставке чата (для показа в рабочем чате)
        """
        async with aiosqlite.connect(self.db_path) as db:
            # Получаем настройки чата для пересчета
            chat_settings = None
            if use_chat_rate:
                settings_cursor = await db.execute('''
                    SELECT work_time_start, work_time_end, salary_amount, hourly_rate
                    FROM chat_settings
                    WHERE chat_id = ?
                ''', (chat_id,))
                chat_settings = await settings_cursor.fetchone()
            # Сначала получаем закрытые смены
            cursor = await db.execute('''
                SELECT 
                    w.user_id,
                    w.username,
                    w.full_name,
                    w.custom_rate,
                    SUM(s.hours_worked) as total_hours,
                    SUM(s.earned_amount) as total_earned,
                    COUNT(s.id) as shifts_count
                FROM workers w
                JOIN shifts s ON w.user_id = s.user_id
                WHERE s.chat_id = ? AND s.period_start = ? AND s.is_active = 0
                GROUP BY w.user_id
            ''', (chat_id, period_start))
            
            results = await cursor.fetchall()
            
            # Создаем словарь со статистикой
            stats_dict = {}
            for row in results:
                total_hours = row[4] or 0
                total_earned = row[5] or 0
                
                # Пересчитываем по общей ставке если нужно
                if use_chat_rate and chat_settings:
                    _, _, daily_rate, hourly_rate = chat_settings
                    if hourly_rate and hourly_rate > 0:
                        total_earned = total_hours * hourly_rate
                
                stats_dict[row[0]] = {
                    'user_id': row[0],
                    'username': row[1],
                    'full_name': row[2],
                    'custom_rate': row[3],
                    'total_hours': total_hours,
                    'total_earned': total_earned,
                    'shifts_count': row[6] or 0
                }
            
            # Если нужно учесть активные смены
            if include_active:
                active_cursor = await db.execute('''
                    SELECT user_id, id
                    FROM shifts
                    WHERE chat_id = ? AND period_start = ? AND is_active = 1
                ''', (chat_id, period_start))
                
                active_shifts = await active_cursor.fetchall()
                
                for user_id, shift_id in active_shifts:
                    # Получаем текущие заработки по активной смене
                    current = await self.get_current_earnings(user_id, chat_id, shift_id)
                    
                    # Пересчитываем по общей ставке если нужно
                    if use_chat_rate and chat_settings:
                        _, _, daily_rate, hourly_rate = chat_settings
                        if hourly_rate and hourly_rate > 0:
                            current['earned_amount'] = current['hours_worked'] * hourly_rate
                    
                    if user_id in stats_dict:
                        stats_dict[user_id]['total_hours'] += current['hours_worked']
                        stats_dict[user_id]['total_earned'] += current['earned_amount']
                        stats_dict[user_id]['shifts_count'] += 1
                    else:
                        # Получаем инфо о работнике
                        worker_cursor = await db.execute(
                            'SELECT username, full_name, custom_rate FROM workers WHERE user_id = ?',
                            (user_id,)
                        )
                        worker = await worker_cursor.fetchone()
                        
                        stats_dict[user_id] = {
                            'user_id': user_id,
                            'username': worker[0] if worker else None,
                            'full_name': worker[1] if worker else None,
                            'custom_rate': worker[2] if worker else None,
                            'total_hours': current['hours_worked'],
                            'total_earned': current['earned_amount'],
                            'shifts_count': 1
                        }
            
            # Преобразуем в список и сортируем
            stats = list(stats_dict.values())
            stats.sort(key=lambda x: x['total_earned'], reverse=True)
            
            return stats

    async def get_shift_history_for_period(
        self, chat_id: int, period_start: datetime, include_active: bool = False
    ) -> List[Dict]:
        """Return individual shifts for a chat and payroll period."""
        async with aiosqlite.connect(self.db_path) as db:
            active_clause = '' if include_active else ' AND s.is_active = 0'
            cursor = await db.execute(
                f'''
                SELECT s.id, s.user_id, w.username, w.full_name, s.shift_type,
                       s.start_time, s.end_time, s.actual_start_time, s.actual_end_time,
                       s.hours_worked, s.earned_amount, s.is_active
                FROM shifts s
                JOIN workers w ON w.user_id = s.user_id
                WHERE s.chat_id = ? AND s.period_start = ?{active_clause}
                ORDER BY s.start_time DESC, s.id DESC
                ''',
                (chat_id, period_start),
            )
            rows = await cursor.fetchall()
            return [
                {
                    'id': row[0],
                    'user_id': row[1],
                    'username': row[2],
                    'full_name': row[3],
                    'shift_type': row[4],
                    'start_time': row[5],
                    'end_time': row[6],
                    'actual_start_time': row[7],
                    'actual_end_time': row[8],
                    'hours_worked': row[9] or 0,
                    'earned_amount': row[10] or 0,
                    'is_active': bool(row[11]),
                }
                for row in rows
            ]
    
    async def get_worker_stats_by_chats_by_dates(self, user_id: int, period_start: datetime, period_end: datetime) -> List[Dict]:
        """Получить статистику работника по чатам за период по датам (новый метод)"""
        async with aiosqlite.connect(self.db_path) as db:
            # Закрытые смены в диапазоне
            cursor = await db.execute('''
                SELECT 
                    c.chat_id,
                    c.chat_name,
                    cs.hourly_rate,
                    SUM(s.hours_worked) as total_hours,
                    SUM(s.earned_amount) as total_earned
                FROM shifts s
                JOIN chats c ON s.chat_id = c.chat_id
                LEFT JOIN chat_settings cs ON c.chat_id = cs.chat_id
                WHERE s.user_id = ? AND s.is_active = 0
                    AND s.end_time >= ? AND s.end_time <= ?
                GROUP BY c.chat_id
            ''', (user_id, period_start, period_end))
            
            results = await cursor.fetchall()
            
            # Словарь для хранения
            stats_dict = {}
            for row in results:
                stats_dict[row[0]] = {
                    'chat_id': row[0],
                    'chat_name': row[1],
                    'hourly_rate': row[2] or 0,
                    'total_hours': row[3] or 0,
                    'total_earned': row[4] or 0
                }
            
            # Добавляем активные смены
            now = datetime.now()
            if period_start <= now <= period_end:
                active_cursor = await db.execute('''
                    SELECT s.id, s.chat_id, c.chat_name, cs.hourly_rate
                    FROM shifts s
                    JOIN chats c ON s.chat_id = c.chat_id
                    LEFT JOIN chat_settings cs ON c.chat_id = cs.chat_id
                    WHERE s.user_id = ? AND s.is_active = 1
                        AND s.start_time >= ? AND s.start_time <= ?
                ''', (user_id, period_start, period_end))
                
                active_shifts = await active_cursor.fetchall()
                
                for shift in active_shifts:
                    shift_id, chat_id, chat_name, hourly_rate = shift
                    current = await self.get_current_earnings(user_id, chat_id, shift_id)
                    
                    if chat_id in stats_dict:
                        stats_dict[chat_id]['total_hours'] += current['hours_worked']
                        stats_dict[chat_id]['total_earned'] += current['earned_amount']
                    else:
                        stats_dict[chat_id] = {
                            'chat_id': chat_id,
                            'chat_name': chat_name,
                            'hourly_rate': hourly_rate or 0,
                            'total_hours': current['hours_worked'],
                            'total_earned': current['earned_amount']
                        }
            
            # Преобразуем в список
            stats = list(stats_dict.values())
            stats.sort(key=lambda x: x['chat_name'])
            
            return stats
    
    async def get_worker_stats_by_chats(self, user_id: int, period_start: datetime) -> List[Dict]:
        """Получить статистику работника по всем чатам за период (включая активные смены)"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT 
                    c.chat_id,
                    c.chat_name,
                    cs.hourly_rate,
                    SUM(s.hours_worked) as total_hours,
                    SUM(s.earned_amount) as total_earned,
                    COUNT(s.id) as shifts_count
                FROM shifts s
                JOIN chats c ON s.chat_id = c.chat_id
                LEFT JOIN chat_settings cs ON c.chat_id = cs.chat_id
                WHERE s.user_id = ? AND s.period_start = ?
                GROUP BY c.chat_id
                ORDER BY c.chat_name
            ''', (user_id, period_start))
            
            results = await cursor.fetchall()
            
            stats = []
            for row in results:
                stats.append({
                    'chat_id': row[0],
                    'chat_name': row[1],
                    'hourly_rate': row[2] or 0,
                    'total_hours': row[3] or 0,
                    'total_earned': row[4] or 0,
                    'shifts_count': row[5] or 0
                })
            
            return stats
    
    # ==================== VPN ORDERS ====================
    
    async def create_vpn_order(self, web_user_id: int, device_count: int, period_days: int, 
                               total_price: float, telegram_contact: str = None) -> int:
        """Создание нового заказа VPN"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                INSERT INTO vpn_orders (web_user_id, device_count, period_days, total_price, telegram_contact, status)
                VALUES (?, ?, ?, ?, ?, 'pending')
            ''', (web_user_id, device_count, period_days, total_price, telegram_contact))
            await db.commit()
            return cursor.lastrowid
    
    async def get_user_orders(self, web_user_id: int) -> List[Dict]:
        """Получение всех заказов пользователя"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT id, device_count, period_days, total_price, status, created_at, confirmed_at
                FROM vpn_orders
                WHERE web_user_id = ?
                ORDER BY created_at DESC
            ''', (web_user_id,))
            rows = await cursor.fetchall()
            return [{
                'id': r[0],
                'device_count': r[1],
                'period_days': r[2],
                'total_price': r[3],
                'status': r[4],
                'created_at': r[5],
                'confirmed_at': r[6]
            } for r in rows]
    
    async def get_all_vpn_orders(self, status: str = None) -> List[Dict]:
        """Получение всех заказов (для админа)"""
        async with aiosqlite.connect(self.db_path) as db:
            if status:
                cursor = await db.execute('''
                    SELECT o.id, o.web_user_id, u.username, o.device_count, o.period_days, 
                           o.total_price, o.status, o.telegram_contact, o.created_at, o.confirmed_at
                    FROM vpn_orders o
                    JOIN web_users u ON o.web_user_id = u.id
                    WHERE o.status = ?
                    ORDER BY o.created_at DESC
                ''', (status,))
            else:
                cursor = await db.execute('''
                    SELECT o.id, o.web_user_id, u.username, o.device_count, o.period_days, 
                           o.total_price, o.status, o.telegram_contact, o.created_at, o.confirmed_at
                    FROM vpn_orders o
                    JOIN web_users u ON o.web_user_id = u.id
                    ORDER BY o.created_at DESC
                ''')
            rows = await cursor.fetchall()
            return [{
                'id': r[0],
                'web_user_id': r[1],
                'username': r[2],
                'device_count': r[3],
                'period_days': r[4],
                'total_price': r[5],
                'status': r[6],
                'telegram_contact': r[7],
                'created_at': r[8],
                'confirmed_at': r[9]
            } for r in rows]
    
    async def confirm_vpn_order(self, order_id: int) -> bool:
        """Подтверждение заказа VPN"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                UPDATE vpn_orders 
                SET status = 'confirmed', confirmed_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (order_id,))
            await db.commit()
            
            cursor = await db.execute('SELECT device_count FROM vpn_orders WHERE id = ?', (order_id,))
            device_count = (await cursor.fetchone())[0]
            
            for i in range(1, device_count + 1):
                await db.execute('''
                    INSERT INTO vpn_order_keys (order_id, device_number)
                    VALUES (?, ?)
                ''', (order_id, i))
            await db.commit()
            return True
    
    async def add_vpn_key_to_order(self, order_id: int, device_number: int, vpn_key: str):
        """Добавление VPN ключа к устройству заказа"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                UPDATE vpn_order_keys 
                SET vpn_key = ?, added_at = CURRENT_TIMESTAMP
                WHERE order_id = ? AND device_number = ?
            ''', (vpn_key, order_id, device_number))
            await db.commit()
    
    async def get_order_keys(self, order_id: int) -> List[Dict]:
        """Получение всех ключей заказа"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT device_number, vpn_key, added_at
                FROM vpn_order_keys
                WHERE order_id = ?
                ORDER BY device_number
            ''', (order_id,))
            rows = await cursor.fetchall()
            return [{
                'device_number': r[0],
                'vpn_key': r[1],
                'added_at': r[2]
            } for r in rows]
    
    async def check_user_last_order_time(self, web_user_id: int) -> bool:
        """Проверка можно ли пользователю создать заказ"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT created_at FROM vpn_orders 
                WHERE web_user_id = ?
                ORDER BY created_at DESC LIMIT 1
            ''', (web_user_id,))
            row = await cursor.fetchone()
            
            if not row:
                return True
            
            last_order_time = datetime.fromisoformat(row[0])
            time_diff = (datetime.now() - last_order_time).total_seconds()
            return time_diff >= 120
    
    async def get_user_active_order(self, web_user_id: int) -> Optional[Dict]:
        """Получение активного заказа пользователя"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT id, device_count, period_days, total_price, status, created_at
                FROM vpn_orders
                WHERE web_user_id = ? AND status IN ('pending', 'confirmed')
                ORDER BY created_at DESC LIMIT 1
            ''', (web_user_id,))
            row = await cursor.fetchone()
            
            if row:
                return {
                    'id': row[0],
                    'device_count': row[1],
                    'period_days': row[2],
                    'total_price': row[3],
                    'status': row[4],
                    'created_at': row[5]
                }
            return None
    
    # ==================== SECURITY QUESTIONS ====================
    
    async def save_security_questions(self, web_user_id: int, questions_answers: List[tuple]):
        """Сохранение секретных вопросов пользователя"""
        from werkzeug.security import generate_password_hash
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT INTO security_questions 
                (web_user_id, question1, answer1_hash, question2, answer2_hash, question3, answer3_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                web_user_id,
                questions_answers[0][0], generate_password_hash(questions_answers[0][1].lower().strip()),
                questions_answers[1][0], generate_password_hash(questions_answers[1][1].lower().strip()),
                questions_answers[2][0], generate_password_hash(questions_answers[2][1].lower().strip())
            ))
            await db.commit()
    
    async def verify_security_answers(self, username: str, answers: List[str]) -> bool:
        """Проверка ответов на секретные вопросы"""
        from werkzeug.security import check_password_hash
        
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT sq.answer1_hash, sq.answer2_hash, sq.answer3_hash
                FROM security_questions sq
                JOIN web_users u ON sq.web_user_id = u.id
                WHERE u.username = ?
            ''', (username,))
            row = await cursor.fetchone()
            
            if not row:
                return False
            
            return (check_password_hash(row[0], answers[0].lower().strip()) and
                    check_password_hash(row[1], answers[1].lower().strip()) and
                    check_password_hash(row[2], answers[2].lower().strip()))
    
    async def get_user_security_questions(self, username: str) -> Optional[List[str]]:
        """Получение секретных вопросов пользователя"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT sq.question1, sq.question2, sq.question3
                FROM security_questions sq
                JOIN web_users u ON sq.web_user_id = u.id
                WHERE u.username = ?
            ''', (username,))
            row = await cursor.fetchone()
            
            if row:
                return [row[0], row[1], row[2]]
            return None
    
    # ==================== PASSWORD RESET ====================
    
    async def create_password_reset_request(self, username: str, telegram_contact: str, answers: List[str]) -> int:
        """Create a reset request without persisting verified answers."""
        if len(answers) != 3:
            raise ValueError('Ожидались три подтверждённых ответа')
        redacted_answers = ('[verified]', '[verified]', '[verified]')
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                INSERT INTO password_reset_requests 
                (username, telegram_contact, answer1, answer2, answer3, status)
                VALUES (?, ?, ?, ?, ?, 'pending')
            ''', (username, telegram_contact, *redacted_answers))
            await db.commit()
            return cursor.lastrowid
    
    async def get_pending_reset_requests(self) -> List[Dict]:
        """Получение всех ожидающих запросов на сброс пароля"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT id, username, telegram_contact, answer1, answer2, answer3, created_at
                FROM password_reset_requests
                WHERE status = 'pending'
                ORDER BY created_at DESC
            ''')
            rows = await cursor.fetchall()
            return [{
                'id': r[0],
                'username': r[1],
                'telegram_contact': r[2],
                'answer1': r[3],
                'answer2': r[4],
                'answer3': r[5],
                'created_at': r[6]
            } for r in rows]
    
    async def update_user_password(self, username: str, new_password_hash: str):
        """Обновление пароля пользователя"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                UPDATE web_users SET password_hash = ? WHERE username = ?
            ''', (new_password_hash, username))
            await db.commit()
    
    async def mark_reset_request_processed(self, request_id: int, admin_id: int):
        """Отметить запрос на сброс как обработанный"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                UPDATE password_reset_requests 
                SET status = 'processed', processed_at = CURRENT_TIMESTAMP, processed_by_admin_id = ?
                WHERE id = ?
            ''', (admin_id, request_id))
            await db.commit()
    
    # ==================== USER BANS ====================
    
    async def check_user_banned(self, web_user_id: int = None, telegram_username: str = None) -> Optional[Dict]:
        """Проверка блокировки пользователя"""
        async with aiosqlite.connect(self.db_path) as db:
            if web_user_id:
                cursor = await db.execute('''
                    SELECT id, reason, banned_at, expires_at, is_permanent
                    FROM user_bans
                    WHERE web_user_id = ? AND (is_permanent = 1 OR expires_at > CURRENT_TIMESTAMP)
                    ORDER BY banned_at DESC LIMIT 1
                ''', (web_user_id,))
            elif telegram_username:
                cursor = await db.execute('''
                    SELECT id, reason, banned_at, expires_at, is_permanent
                    FROM user_bans
                    WHERE telegram_username = ? AND (is_permanent = 1 OR expires_at > CURRENT_TIMESTAMP)
                    ORDER BY banned_at DESC LIMIT 1
                ''', (telegram_username,))
            else:
                return None
            
            row = await cursor.fetchone()
            if row:
                return {
                    'id': row[0],
                    'reason': row[1],
                    'banned_at': row[2],
                    'expires_at': row[3],
                    'is_permanent': bool(row[4])
                }
            return None
    
    async def ban_user(self, web_user_id: int, telegram_username: str, reason: str, 
                       banned_by_admin_id: int, expires_at: datetime = None, is_permanent: bool = False):
        """Блокировка пользователя"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT INTO user_bans 
                (web_user_id, telegram_username, reason, banned_by_admin_id, expires_at, is_permanent)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (web_user_id, telegram_username, reason, banned_by_admin_id, 
                  expires_at, 1 if is_permanent else 0))
            await db.commit()
    
    async def unban_user(self, ban_id: int):
        """Разблокировка пользователя"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('DELETE FROM user_bans WHERE id = ?', (ban_id,))
            await db.commit()
    
    async def get_all_bans(self, active_only: bool = True) -> List[Dict]:
        """Получение всех блокировок"""
        async with aiosqlite.connect(self.db_path) as db:
            if active_only:
                cursor = await db.execute('''
                    SELECT b.id, b.web_user_id, u.username, b.telegram_username, b.reason, 
                           b.banned_at, b.expires_at, b.is_permanent
                    FROM user_bans b
                    LEFT JOIN web_users u ON b.web_user_id = u.id
                    WHERE b.is_permanent = 1 OR b.expires_at > CURRENT_TIMESTAMP
                    ORDER BY b.banned_at DESC
                ''')
            else:
                cursor = await db.execute('''
                    SELECT b.id, b.web_user_id, u.username, b.telegram_username, b.reason, 
                           b.banned_at, b.expires_at, b.is_permanent
                    FROM user_bans b
                    LEFT JOIN web_users u ON b.web_user_id = u.id
                    ORDER BY b.banned_at DESC
                ''')
            rows = await cursor.fetchall()
            return [{
                'id': r[0],
                'web_user_id': r[1],
                'username': r[2],
                'telegram_username': r[3],
                'reason': r[4],
                'banned_at': r[5],
                'expires_at': r[6],
                'is_permanent': bool(r[7])
            } for r in rows]
    
    async def cancel_vpn_order(self, order_id: int):
        """Отмена заказа VPN"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                UPDATE vpn_orders 
                SET status = 'cancelled'
                WHERE id = ?
            ''', (order_id,))
            await db.commit()
    
    # ==================== TELEGRAM UNLINKING ====================
    
    async def unlink_telegram(self, web_user_id: int):
        """Отвязка Telegram от аккаунта"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                UPDATE web_users 
                SET telegram_id = NULL, telegram_linked = 0
                WHERE id = ?
            ''', (web_user_id,))
            await db.commit()
    
    async def check_telegram_already_linked(self, telegram_id: int) -> Optional[Dict]:
        """Проверка что Telegram уже привязан к другому аккаунту"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT id, username FROM web_users 
                WHERE telegram_id = ? AND telegram_linked = 1
            ''', (telegram_id,))
            row = await cursor.fetchone()
            if row:
                return {'id': row[0], 'username': row[1]}
            return None
    
    async def delete_old_link_keys(self, telegram_id: int):
        """Удаление старых ключей привязки пользователя"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('DELETE FROM link_keys WHERE telegram_id = ?', (telegram_id,))
            await db.commit()
    
    async def check_active_link_key_exists(self, telegram_id: int) -> bool:
        """Проверка существует ли активный ключ привязки"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT id FROM link_keys 
                WHERE telegram_id = ? AND used = 0 AND expires_at > CURRENT_TIMESTAMP
            ''', (telegram_id,))
            row = await cursor.fetchone()
            return row is not None

    async def create_lead_request(
        self,
        name: str,
        contact: str,
        points: str,
        message: str = '',
        language: str = 'ru',
        source: str = 'landing',
    ) -> int:
        """Store a public connection request without creating an account."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                '''
                INSERT INTO lead_requests
                    (name, contact, points, message, language, consent_at, source)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                ''',
                (name.strip(), contact.strip(), points.strip(), message.strip(), language, source),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def get_lead_requests(self, status: str = None) -> List[Dict]:
        """Return lead requests for a future owner/admin sales inbox."""
        async with aiosqlite.connect(self.db_path) as db:
            if status:
                cursor = await db.execute(
                    '''SELECT id, name, contact, points, message, language, consent_at,
                              source, status, created_at
                       FROM lead_requests WHERE status = ? ORDER BY created_at DESC''',
                    (status,),
                )
            else:
                cursor = await db.execute(
                    '''SELECT id, name, contact, points, message, language, consent_at,
                              source, status, created_at
                       FROM lead_requests ORDER BY created_at DESC'''
                )
            rows = await cursor.fetchall()
            keys = ('id', 'name', 'contact', 'points', 'message', 'language',
                    'consent_at', 'source', 'status', 'created_at')
            return [dict(zip(keys, row, strict=True)) for row in rows]

    async def update_lead_status(self, lead_id: int, status: str) -> None:
        """Move a sales lead through the small operator workflow."""
        if status not in {'new', 'contacted', 'converted', 'closed'}:
            raise ValueError('Unknown lead status')
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                'UPDATE lead_requests SET status = ? WHERE id = ?',
                (status, lead_id),
            )
            if cursor.rowcount != 1:
                raise ValueError('Lead not found')
            await db.commit()
