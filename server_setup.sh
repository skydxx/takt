#!/bin/bash
set -e  # Остановить скрипт при любой ошибке

# Настройки
INSTALL_DIR="/opt/salarybot"
SERVICE_NAME="salary_bot"
WORKER_SERVICE_NAME="salary_worker"
SERVICE_USER="salarybot"
PYTHON_BIN="python3"
: "${DOMAIN:?Set DOMAIN to the deployment hostname before running this script}"
: "${CERTBOT_EMAIL:?Set CERTBOT_EMAIL for certificate notices before running this script}"

echo "🚀 Начало полной установки Salary Bot на чистый сервер..."

# Проверка прав root
if [ "$EUID" -ne 0 ]; then 
  echo "Пожалуйста, запустите скрипт от имени root"
  exit 1
fi

# Установка системных зависимостей
echo "Установка системных пакетов (Python, Nginx, Certbot)..."
apt-get update
apt-get install -y python3-venv python3-pip unzip nginx certbot python3-certbot-nginx

# Остановка сервиса если есть
if systemctl is-active --quiet $SERVICE_NAME; then
    echo "Остановка текущего сервиса..."
    systemctl stop $SERVICE_NAME
fi
if systemctl is-active --quiet $WORKER_SERVICE_NAME; then
    echo "Остановка текущего worker-сервиса..."
    systemctl stop $WORKER_SERVICE_NAME
fi

# Создание директории если нет
mkdir -p "$INSTALL_DIR"

# Сервисы не должны работать от root. Отдельный системный пользователь
# получает только доступ к каталогу приложения и его runtime-состоянию.
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$INSTALL_DIR" --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# Распаковка архива
ARCHIVE="salary_bot_deploy.zip"
if [ -f "$ARCHIVE" ]; then
    echo "📦 Распаковка $ARCHIVE в $INSTALL_DIR..."
    unzip -o "$ARCHIVE" -d "$INSTALL_DIR"
else
    echo "⚠️ Архив $ARCHIVE не найден в текущей папке!"
    echo "Пожалуйста, загрузите $ARCHIVE и $0 на сервер в одну папку."
    exit 1
fi

# Переход в директорию
cd "$INSTALL_DIR"

# Создание и настройка venv
if [ ! -d "venv" ]; then
    echo "🐍 Создание виртуального окружения..."
    $PYTHON_BIN -m venv venv
fi

echo "📥 Установка Python зависимостей..."
./venv/bin/pip install -r requirements.txt

# Проверка .env
if [ ! -f ".env" ]; then
    echo "📝 Создание .env файла..."
    cat > .env << EOL
APP_ENV=production
BOT_TOKEN=ваш_токен_здесь
FLASK_SECRET_KEY=$(openssl rand -hex 32)
SESSION_COOKIE_SECURE=1
WEB_PORT=5000
DOMAIN=$DOMAIN
PLATFORM_DATABASE_PATH=$INSTALL_DIR/platform.db
# Для production PostgreSQL задайте DSN вместо локального SQLite:
# PLATFORM_DATABASE_URL=postgresql://user:password@127.0.0.1:5432/tochka_smena
# SUPER_ADMIN_1=ваш_id
EOL
    chmod 600 .env
    chown "$SERVICE_USER:$SERVICE_USER" .env
    echo "⚠️ ВАЖНО: Отредактируйте файл .env и вставьте токен бота!"
fi

if grep -q '^BOT_TOKEN=ваш_токен_здесь$' .env; then
    echo "❌ В .env не задан настоящий BOT_TOKEN; сервисы не запускаются."
    exit 1
fi

if ! grep -Eq '^PLATFORM_DATABASE_URL=postgres(ql)?://' .env; then
    echo "❌ В .env не задан PostgreSQL PLATFORM_DATABASE_URL; сервисы не запускаются."
    exit 1
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod 750 "$INSTALL_DIR"
chmod 600 .env

# Создание сервиса systemd
echo "⚙️ Настройка systemd сервиса..."
cat > /etc/systemd/system/$SERVICE_NAME.service << EOL
[Unit]
Description=Salary Bot Service
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/salary_bot.py
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=$INSTALL_DIR
UMask=027
Environment=WEB_PORT=5000
EnvironmentFile=-$INSTALL_DIR/.env

[Install]
WantedBy=multi-user.target
EOL

cat > /etc/systemd/system/$WORKER_SERVICE_NAME.service << EOL
[Unit]
Description=Такт notification worker
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=-$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/worker.py
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=$INSTALL_DIR
UMask=027

[Install]
WantedBy=multi-user.target
EOL

# Настройка Nginx
echo "🌐 Настройка Nginx для $DOMAIN..."
cat > /etc/nginx/sites-available/$DOMAIN << EOL
server {
    listen 80;
    server_name $DOMAIN;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOL

# Активация сайта Nginx
ln -sf /etc/nginx/sites-available/$DOMAIN /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

# Получение SSL сертификата
echo "🔒 Получение SSL сертификата через Certbot..."
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$CERTBOT_EMAIL" --redirect || echo "⚠️ Не удалось получить SSL сертификат (возможно, DNS еще не обновились)"

# Перезагрузка демона и запуск
systemctl daemon-reload
systemctl enable $SERVICE_NAME
systemctl enable $WORKER_SERVICE_NAME
systemctl start $SERVICE_NAME
systemctl start $WORKER_SERVICE_NAME

echo "✅ Установка завершена!"
echo "📊 Статус сервиса:"
systemctl status $SERVICE_NAME --no-pager
systemctl status $WORKER_SERVICE_NAME --no-pager

echo ""
echo "🔐 Теперь:"
echo "1. Отредактируйте .env (вставьте токен): nano $INSTALL_DIR/.env"
echo "2. Перезапустите бота: systemctl restart $SERVICE_NAME"
echo "3. Создайте админа: cd $INSTALL_DIR && ./venv/bin/python create_admin.py"
