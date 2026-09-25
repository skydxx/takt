import asyncio
import hashlib
import sqlite3
from datetime import datetime, timedelta

import aiosqlite

from database import Database


def test_original_web_auth_schema_is_upgraded_without_losing_users(tmp_path):
    path = tmp_path / 'original.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.executescript(
            '''
            CREATE TABLE workers (user_id INTEGER PRIMARY KEY);
            INSERT INTO workers (user_id) VALUES (777);
            CREATE TABLE web_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE NOT NULL,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin BOOLEAN DEFAULT 0,
                is_view_only_admin BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP
            );
            INSERT INTO web_users
                (user_id, username, password_hash, is_admin)
            VALUES (777, 'legacy-admin', 'hash', 1);
            CREATE TABLE web_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_token TEXT UNIQUE NOT NULL,
                user_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                ip_address TEXT,
                user_agent TEXT
            );
            INSERT INTO web_sessions (session_token, user_id, expires_at)
            VALUES ('legacy-token', 777, '2099-01-01 00:00:00');
            '''
        )

    database = Database(str(path))
    asyncio.run(database.init_db())
    user = asyncio.run(database.get_web_user('legacy-admin'))
    assert user['role'] == 'admin'
    assert user['telegram_id'] == 777
    assert user['telegram_linked'] is True
    assert asyncio.run(database.get_user_by_session('legacy-token'))['id'] == user['id']

    new_id = asyncio.run(database.create_web_user('new-owner', 'new-hash'))
    assert new_id > user['id']


def test_browser_session_tokens_are_hashed_and_legacy_values_migrate(tmp_path):
    database = Database(str(tmp_path / 'legacy.sqlite3'))

    async def exercise():
        await database.init_db()
        user_id = await database.create_web_user('session-user', 'hash')
        cookie_value = 'raw-session-token'
        await database.create_web_session(
            user_id, cookie_value, datetime.now() + timedelta(days=1)
        )
        async with aiosqlite.connect(database.db_path) as db:
            row = await (await db.execute(
                'SELECT session_token FROM web_sessions WHERE web_user_id = ?',
                (user_id,),
            )).fetchone()
        assert row[0] == hashlib.sha256(cookie_value.encode()).hexdigest()
        assert await database.get_user_by_session(cookie_value)

        old_cookie_value = 'legacy-plaintext-token'
        async with aiosqlite.connect(database.db_path) as db:
            await db.execute(
                '''INSERT INTO web_sessions
                   (session_token, web_user_id, expires_at)
                   VALUES (?, ?, ?)''',
                (old_cookie_value, user_id, datetime.now() + timedelta(days=1)),
            )
            await db.commit()
        await database.init_db()
        async with aiosqlite.connect(database.db_path) as db:
            migrated = await (await db.execute(
                'SELECT session_token FROM web_sessions WHERE session_token = ?',
                (hashlib.sha256(old_cookie_value.encode()).hexdigest(),),
            )).fetchone()
        assert migrated is not None
        assert await database.get_user_by_session(old_cookie_value)
        await database.delete_web_session(cookie_value)
        assert not await database.get_user_by_session(cookie_value)

    asyncio.run(exercise())


def test_password_reset_requests_do_not_store_secret_answers(tmp_path):
    database = Database(str(tmp_path / 'legacy.sqlite3'))

    async def exercise():
        await database.init_db()
        await database.create_password_reset_request(
            'owner', '@owner', ['child name', 'school', 'first car']
        )
        pending = await database.get_pending_reset_requests()
        assert pending[0]['telegram_contact'] == '@owner'
        assert pending[0]['answer1'] == '[verified]'
        assert pending[0]['answer2'] == '[verified]'
        assert pending[0]['answer3'] == '[verified]'
        assert 'child name' not in repr(pending)

    asyncio.run(exercise())


def test_link_keys_are_hashed_and_remain_usable(tmp_path):
    database = Database(str(tmp_path / 'legacy.sqlite3'))

    async def exercise():
        await database.init_db()
        raw_key = await database.create_link_key(777)
        async with aiosqlite.connect(database.db_path) as db:
            stored = await (await db.execute(
                'SELECT key_code FROM link_keys WHERE telegram_id = ?', (777,)
            )).fetchone()
        assert stored[0] == hashlib.sha256(raw_key.encode()).hexdigest()
        assert stored[0] != raw_key
        assert await database.verify_link_key(raw_key) == 777
        await database.mark_key_used(raw_key, 12)
        assert await database.verify_link_key(raw_key) is None

    asyncio.run(exercise())
