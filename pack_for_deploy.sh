#!/usr/bin/env bash
set -euo pipefail

ARCHIVE_NAME="${1:-salary_bot_deploy.zip}"

echo "📦 Упаковка файлов для деплоя..."

# Удаляем старый архив если есть
rm -f -- "$ARCHIVE_NAME"

# Создаем архив
zip -r "$ARCHIVE_NAME" \
    salary_bot.py \
    database.py \
    config.py \
    platform_database.py \
    platform_store.py \
    notification_service.py \
    worker.py \
    backup_manager.py \
    utils.py \
    web_app.py \
    public_i18n.py \
    create_admin.py \
    create_owner_code.py \
    migrate_platform_sqlite_to_postgres.py \
    migrations/ \
    requirements.txt \
    .env.example \
    templates/ \
    static/ \
    -x "*__pycache__*" \
    -x "*.DS_Store" \
    -x "*.git*" \
    -x "venv/*" \
    -x "data/*" \
    -x ".env" \
    -x "*.db" \
    -x "*.sqlite*" \
    -x "*.log" \
    -x "*.zip"

echo "✅ Архив $ARCHIVE_NAME создан!"
echo "📤 Теперь загрузите этот файл и server_setup.sh на ваш сервер."
