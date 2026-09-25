#!/bin/bash
set -e

SERVICE_NAME="salary_bot"
INSTALL_DIR="/opt/salarybot"
ARCHIVE="salary_bot_deploy.zip"

echo "🚀 Начало обновления бота..."

# Проверка прав root
if [ "$EUID" -ne 0 ]; then 
  echo "❌ Запустите скрипт от имени root"
  exit 1
fi

# Проверка наличия архива
if [ ! -f "$ARCHIVE" ]; then
    echo "❌ Архив $ARCHIVE не найден!"
    exit 1
fi

echo "🛑 Остановка сервиса..."
systemctl stop $SERVICE_NAME

echo "📦 Распаковка файлов (БЕЗ удаления базы данных)..."
# -o: overwrite existing files without prompting
# unzip не удаляет файлы, которых нет в архиве, так что БД в безопасности
unzip -o "$ARCHIVE" -d "$INSTALL_DIR"

echo "📥 Обновление зависимостей..."
cd $INSTALL_DIR
./venv/bin/pip install -r requirements.txt

echo "🔄 Перезапуск сервиса..."
systemctl start $SERVICE_NAME

echo "✅ Бот успешно обновлен!"
systemctl status $SERVICE_NAME --no-pager
