#!/bin/bash
# Jianwei Alpha first-time server setup script.
# Usage:
#   curl -L -o init.sh https://gitee.com/wangermazi81/quant/raw/main/deploy/server_init.sh
#   chmod +x init.sh
#   bash init.sh

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/quant}"
SERVICE="${SERVICE:-quant-platform}"
GITEE_REPO="${GITEE_REPO:-https://gitee.com/wangermazi81/quant.git}"
BRANCH="${BRANCH:-main}"
PORT="${PORT:-5000}"

secure_runtime_secrets() {
    local owner="$1"
    local files=(
        ".env"
        "data/.secret_key"
        "data/ai_config.json"
        "data/ai_trader_config.json"
        "data/ai_research.db"
        "data/ai_research.db-wal"
        "data/ai_research.db-shm"
        "data/users.db"
        "data/users.db-wal"
        "data/users.db-shm"
        "data/search_config.json"
        "data/notify_config.json"
        "tools/bridge_config.json"
    )

    for file in "${files[@]}"; do
        if [ -e "$file" ]; then
            chown "$owner:$owner" "$file" 2>/dev/null || true
            chmod 600 "$file" 2>/dev/null || true
        fi
    done
}

echo
echo "Jianwei Alpha first-time server setup"
echo "APP_DIR = $APP_DIR"
echo "REPO    = $GITEE_REPO"
echo "BRANCH  = $BRANCH"
echo "PORT    = $PORT"
echo

echo "[1/9] Install system dependencies"
apt-get update -y
apt-get install -y \
    python3 python3-pip python3-venv python3-dev \
    git curl unzip build-essential \
    nginx sqlite3 nodejs npm

echo
echo "[2/9] Clone code from Gitee"
if [ -d "$APP_DIR/.git" ]; then
    echo "$APP_DIR is already a git repository; skip clone."
else
    mkdir -p "$(dirname "$APP_DIR")"
    git clone --branch "$BRANCH" "$GITEE_REPO" "$APP_DIR"
fi
cd "$APP_DIR"
mkdir -p logs data data/daily

echo
echo "[3/9] Configure Python virtualenv"
if [ ! -d venv ]; then
    python3 -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt

echo
echo "[4/9] Frontend build no longer needed"
# 工单7c：/v2 Vite 双轨已移除，前端 = index.html 骨架 + static/app/ 分文件（随 git 同步）
echo "Frontend is served from static/app (no build step since 工单7c)."

echo
echo "[5/9] Configure environment"
if [ ! -f .env ]; then
    cp .env.example .env
    SK=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$SK|" .env
    sed -i 's|^AI_API_KEY=|# AI_API_KEY=|; s|^AI_BASE_URL=|# AI_BASE_URL=|; s|^AI_MODEL=|# AI_MODEL=|' .env
    # 云端一律走 HTTPS：强制开 Secure Cookie + HSTS。
    # .env.example 里默认是 0（照顾本机 http://127.0.0.1 开发），直接 cp 过来会让
    # 线上 session cookie 明文可截、且 HSTS 不下发 —— 这里必须覆盖成 1。
    sed -i "s|^SESSION_COOKIE_SECURE=.*|SESSION_COOKIE_SECURE=1|" .env
    echo "Generated random SECRET_KEY."
    echo "Forced SESSION_COOKIE_SECURE=1 (server is HTTPS-only)."
    echo "Commented AI_* placeholders; use web settings or synced private config."
else
    echo ".env already exists; skip."
fi

echo
echo "[6/9] Private files reminder"
echo "Run on local Windows, not on the server:"
echo "  cd D:\\同花顺量化交易\\ths_auto_trader"
echo "  powershell -ExecutionPolicy Bypass -File .\\bundle_private.ps1"
echo "Then upload to server:"
echo "  scp _private_bundle_*.zip root@<server-ip>:$APP_DIR/"
echo "Then run on server:"
echo "  cd $APP_DIR && unzip -o _private_bundle_*.zip && rm _private_bundle_*.zip"
echo
read -r -p "Have private files been uploaded and extracted? [y/N] " ANS
if [ "$ANS" != "y" ] && [ "$ANS" != "Y" ]; then
    echo "Upload private bundle first, then rerun this script or continue manually."
    exit 0
fi

echo
echo "[7/9] Run database migration"
python3 core/db_migration.py || true
secure_runtime_secrets root

echo
echo "[8/9] Configure systemd service"
cat > "/etc/systemd/system/$SERVICE.service" << EOF
[Unit]
Description=Quant Trading Platform (Jianwei Alpha)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/gunicorn -c deploy/gunicorn.conf.py main:app
Restart=always
RestartSec=5
StandardOutput=append:$APP_DIR/logs/service.log
StandardError=append:$APP_DIR/logs/service-err.log

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"
sleep 3
systemctl --no-pager status "$SERVICE" || true

echo
echo "[9/9] Configure Nginx reverse proxy"
cat > "/etc/nginx/sites-available/$SERVICE" << EOF
server {
    listen 80;
    server_name _;

    client_max_body_size 10M;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
        proxy_connect_timeout 10s;
    }

    location /static/ {
        alias $APP_DIR/static/;
        expires 7d;
        add_header Cache-Control "public, immutable";
    }
}
EOF
ln -sf "/etc/nginx/sites-available/$SERVICE" "/etc/nginx/sites-enabled/"
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

PUBLIC_IP=$(curl -s --max-time 5 ifconfig.me || echo "<server-public-ip>")
echo
echo "Deployment complete"
echo "URL     : http://$PUBLIC_IP"
echo "Service : $SERVICE"
echo "Logs    : journalctl -u $SERVICE -f"
echo "Update  : bash $APP_DIR/deploy/server_update.sh"
echo
echo "Note: live Windows THS trading cannot run on Linux. Cloud supports paper trading, backtests, AI analysis, scans, and strategy management."
