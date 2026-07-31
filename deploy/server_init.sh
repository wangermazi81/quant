#!/bin/bash
# Jianwei Alpha first-time server setup script.
# Usage:
#   curl -L -o init.sh https://gitee.com/wangermazi81/quant/raw/main/deploy/server_init.sh
#   chmod +x init.sh
#   bash init.sh

set -euo pipefail
umask 077

APP_DIR="${APP_DIR:-/opt/quant}"
SERVICE="${SERVICE:-quant-platform}"
RUN_USER="${RUN_USER:-quant}"
GITEE_REPO="${GITEE_REPO:-https://gitee.com/wangermazi81/quant.git}"
BRANCH="${BRANCH:-main}"
PORT="${PORT:-5000}"
PUBLIC_HTTPS_READY="${PUBLIC_HTTPS_READY:-0}"
DEPLOY_PYTHON="${DEPLOY_PYTHON:-/usr/bin/python3}"
PERMISSIONS_HELPER="${PERMISSIONS_HELPER:-$APP_DIR/deploy/harden_runtime_permissions.py}"

secure_runtime_secrets() {
    local owner="$1"
    "$DEPLOY_PYTHON" -I "$PERMISSIONS_HELPER" \
        --app-dir "$APP_DIR" \
        --run-user "$owner" \
        --runtime-only
}

secure_application_ownership() {
    local owner="$1"
    if [ ! -f "$PERMISSIONS_HELPER" ]; then
        echo "ERROR: permission hardening helper is unavailable: $PERMISSIONS_HELPER"
        return 1
    fi
    "$DEPLOY_PYTHON" -I "$PERMISSIONS_HELPER" \
        --app-dir "$APP_DIR" \
        --run-user "$owner" \
        --include-venv
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
if ! getent group "$RUN_USER" >/dev/null; then
    groupadd --system "$RUN_USER"
fi
if ! id "$RUN_USER" >/dev/null 2>&1; then
    useradd --system \
        --gid "$RUN_USER" \
        --home-dir "$APP_DIR" \
        --no-create-home \
        --shell /usr/sbin/nologin \
        "$RUN_USER"
fi

echo
echo "[2/9] Clone code from Gitee"
if [ -d "$APP_DIR/.git" ]; then
    echo "ERROR: existing checkout must be updated with the trusted bootstrap."
    exit 1
fi
if [[ "$GITEE_REPO" != https://* ]]; then
    echo "ERROR: GITEE_REPO must use HTTPS."
    exit 1
fi
mkdir -p "$(dirname "$APP_DIR")"
env -i \
    HOME="/root" \
    PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    GIT_CONFIG_NOSYSTEM="1" \
    GIT_CONFIG_SYSTEM="/dev/null" \
    GIT_CONFIG_GLOBAL="/dev/null" \
    GIT_TERMINAL_PROMPT="0" \
    git \
    -c core.hooksPath=/dev/null \
    -c credential.helper= \
    -c protocol.file.allow=never \
    clone --branch "$BRANCH" --single-branch \
    "$GITEE_REPO" "$APP_DIR"
cd "$APP_DIR"
mkdir -p logs data data/daily

echo
echo "[3/9] Configure Python virtualenv"
if [ -e venv ]; then
    echo "ERROR: a first-time install must not contain an existing venv."
    exit 1
fi
install -d -o "$RUN_USER" -g "$RUN_USER" -m 0700 "$APP_DIR/venv"
sudo -u "$RUN_USER" env -i \
    HOME="$APP_DIR/data" \
    PATH="/usr/local/bin:/usr/bin:/bin" \
    PIP_CONFIG_FILE="/dev/null" \
    "$DEPLOY_PYTHON" -I -m venv --copies "$APP_DIR/venv"
sudo -u "$RUN_USER" env -i \
    HOME="$APP_DIR/data" \
    PATH="/usr/local/bin:/usr/bin:/bin" \
    PIP_CONFIG_FILE="/dev/null" \
    "$APP_DIR/venv/bin/python" -I -m pip install --upgrade pip wheel
sudo -u "$RUN_USER" env -i \
    HOME="$APP_DIR/data" \
    PATH="/usr/local/bin:/usr/bin:/bin" \
    PIP_CONFIG_FILE="/dev/null" \
    "$APP_DIR/venv/bin/python" -I -m pip install \
    -r "$APP_DIR/requirements.txt"

echo
echo "[4/9] Frontend build no longer needed"
# 工单7c：/v2 Vite 双轨已移除，前端 = index.html 骨架 + static/app/ 分文件（随 git 同步）
echo "Frontend is served from static/app (no build step since 工单7c)."

echo
echo "[5/9] Configure environment"
if [ "$PUBLIC_HTTPS_READY" != "0" ] \
    && [ "$PUBLIC_HTTPS_READY" != "1" ]; then
    echo "ERROR: PUBLIC_HTTPS_READY must be 0 or 1."
    exit 1
fi
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    SK=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$SK|" .env
    sed -i 's|^AI_API_KEY=|# AI_API_KEY=|; s|^AI_BASE_URL=|# AI_BASE_URL=|; s|^AI_MODEL=|# AI_MODEL=|' .env
    SESSION_COOKIE_SECURE=$PUBLIC_HTTPS_READY
    sed -i \
        "s|^SESSION_COOKIE_SECURE=.*|SESSION_COOKIE_SECURE=$PUBLIC_HTTPS_READY|" \
        .env
    echo "Generated random SECRET_KEY."
    echo "SESSION_COOKIE_SECURE=$PUBLIC_HTTPS_READY"
    echo "Commented AI_* placeholders; use web settings or synced private config."
else
    echo "ERROR: unexpected .env in a first-time checkout."
    exit 1
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
echo "Private files are optional during first boot; upload them through the audited deployment flow."

echo
echo "[7/9] Run database migration"
secure_application_ownership "$RUN_USER"
sudo -u "$RUN_USER" "$APP_DIR/venv/bin/python" \
    core/db_migration.py
secure_runtime_secrets "$RUN_USER"

echo
echo "[8/9] Configure systemd service"
cat > "/etc/systemd/system/$SERVICE.service" << EOF
[Unit]
Description=Quant Trading Platform (Jianwei Alpha)
After=network.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
Environment=THS_EMBED_SCHEDULER=0
UMask=0077
ExecStart=$APP_DIR/venv/bin/gunicorn -c deploy/gunicorn.conf.py main:app
Restart=always
RestartSec=5
StandardOutput=append:$APP_DIR/logs/service.log
StandardError=append:$APP_DIR/logs/service-err.log

[Install]
WantedBy=multi-user.target
EOF
cp "$APP_DIR/deploy/quant-scheduler.service" /etc/systemd/system/quant-scheduler.service
sed -i \
    -e "s|^User=.*|User=$RUN_USER|" \
    -e "s|/opt/quant|$APP_DIR|g" \
    /etc/systemd/system/quant-scheduler.service
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl enable quant-scheduler
systemctl restart "$SERVICE"
systemctl restart quant-scheduler
sleep 3
/usr/bin/python3 -I \
    "$APP_DIR/deploy/check_scheduler_health.py" \
    --app-dir "$APP_DIR" \
    --expected-pid "$(systemctl show quant-scheduler -p MainPID --value)" \
    --wait-seconds 20
systemctl --no-pager status "$SERVICE" || true
systemctl --no-pager status quant-scheduler || true

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
echo "Update  : follow docs/RUNBOOK.md bootstrap deployment flow"
echo
echo "Note: live Windows THS trading cannot run on Linux. Cloud supports paper trading, backtests, AI analysis, scans, and strategy management."
