#!/bin/bash
# Jianwei Alpha server update script.
# Usage on server:
#   bash /opt/quant/deploy/server_update.sh

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/quant}"
SERVICE="${SERVICE:-quant-platform}"
PORT="${PORT:-5000}"
# FRONTEND_DIST 已移除（工单7c：/v2 双轨下线，前端无构建步骤）

cd "$APP_DIR"

git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true
RUN_USER=$(systemctl show "$SERVICE" -p User --value 2>/dev/null)
RUN_USER="${RUN_USER:-root}"

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

sync_nginx_anti_crawl() {
    if [ "$(id -u)" != "0" ]; then
        echo "Skip Nginx anti-crawl sync: root privileges required."
        return 0
    fi
    if ! command -v nginx >/dev/null 2>&1; then
        echo "Skip Nginx anti-crawl sync: nginx not installed."
        return 0
    fi

    local http_conf="$APP_DIR/deploy/nginx/anti_crawl_http.conf"
    local server_snippet="$APP_DIR/deploy/nginx/anti_crawl_server_snippet.conf"
    local changed=0

    if [ -f "$http_conf" ]; then
        mkdir -p /etc/nginx/conf.d
        cp "$http_conf" /etc/nginx/conf.d/00-jwalpha-anti-crawl.conf
        changed=1
    fi
    if [ -f "$server_snippet" ]; then
        mkdir -p /etc/nginx/snippets
        cp "$server_snippet" /etc/nginx/snippets/jwalpha-anti-crawl-server.conf
        changed=1
    fi

    if [ "$changed" = "1" ]; then
        nginx -t
        systemctl reload nginx
        echo "Synced Nginx anti-crawl templates."
    fi
}

data_scheduler_service_exists() {
    systemctl cat quant-scheduler.service >/dev/null 2>&1
}

ensure_data_scheduler_service() {
    if [ "$(id -u)" != "0" ]; then
        echo "Skip quant-scheduler install: root privileges required."
        return 0
    fi

    local unit_src="$APP_DIR/deploy/quant-scheduler.service"
    local unit_dst="/etc/systemd/system/quant-scheduler.service"
    if [ ! -f "$unit_src" ]; then
        echo "Skip quant-scheduler install: $unit_src missing."
        return 0
    fi

    cp "$unit_src" "$unit_dst"
    if [ -n "$RUN_USER" ] && [ "$RUN_USER" != "quant" ]; then
        sed -i "s/^User=.*/User=$RUN_USER/" "$unit_dst"
    fi
    sed -i "s|/opt/quant|$APP_DIR|g" "$unit_dst"
    systemctl daemon-reload
    systemctl enable quant-scheduler >/dev/null 2>&1 || true
}

ensure_platform_scheduler_split() {
    if [ "$(id -u)" != "0" ]; then
        echo "Skip platform scheduler split: root privileges required."
        return 0
    fi

    local override_file="/etc/systemd/system/$SERVICE.service.d/scheduler-split.conf"
    mkdir -p "$(dirname "$override_file")"
    cat > "$override_file" <<'EOF'
[Service]
Environment=THS_EMBED_SCHEDULER=0
EOF
    systemctl daemon-reload
}

verify_scheduler_quiescence() {
    "$APP_DIR/venv/bin/python" \
        "$APP_DIR/deploy/check_scheduler_quiescence.py" \
        --app-dir "$APP_DIR" \
        --web-service "$SERVICE"
}

quiesce_legacy_schedulers() {
    if [ "$(id -u)" != "0" ]; then
        echo "ERROR: scheduler protocol migration requires root privileges."
        exit 1
    fi

    if data_scheduler_service_exists; then
        systemctl stop quant-scheduler
        if systemctl is-active --quiet quant-scheduler; then
            echo "ERROR: quant-scheduler did not stop cleanly."
            exit 1
        fi
    fi

    ensure_platform_scheduler_split
    systemctl restart "$SERVICE"
    verify_scheduler_quiescence
    echo "Stopped legacy data schedulers before protocol migration."
}

echo "[0/6] Quiesce legacy schedulers"
quiesce_legacy_schedulers

echo "[1/6] Pull latest code from Gitee"
git fetch origin --prune
LOCAL=$(git rev-parse HEAD)
git pull --ff-only origin "$(git branch --show-current)" || {
    echo "ERROR: git pull failed. Check server-side uncommitted changes with: git status"
    exit 1
}
REMOTE=$(git rev-parse HEAD)
sync_nginx_anti_crawl

if [ "$LOCAL" = "$REMOTE" ]; then
    secure_runtime_secrets "$RUN_USER"
    ensure_data_scheduler_service
    if data_scheduler_service_exists; then
        verify_scheduler_quiescence
        systemctl start quant-scheduler
        echo "Restarted quant-scheduler (data schedulers)."
    fi
    echo "Code is already up to date."
    exit 0
fi
echo "Updated: $LOCAL -> $REMOTE"

if [ "$RUN_USER" != "root" ]; then
    chown -R "$RUN_USER:$RUN_USER" "$APP_DIR"
    echo "Restored ownership to $RUN_USER:$RUN_USER"
fi

echo
echo "[2/6] Check Python dependencies"
# shellcheck disable=SC1091
source venv/bin/activate
if git diff --name-only "$LOCAL" "$REMOTE" | grep -q "^requirements.txt$"; then
    echo "requirements.txt changed; installing Python dependencies."
    pip install -r requirements.txt
else
    echo "requirements.txt unchanged; skip pip install."
fi

echo
echo "[3/6] Frontend build no longer needed"
# 工单7c：/v2 Vite 双轨已移除，前端 = index.html 骨架 + static/app/ 分文件
# （随 git 同步，无构建步骤）。旧 frontend/ 目录若仍在服务器上可安全删除。
echo "Frontend is served from static/app (no build step since 工单7c)."

echo
echo "[4/6] Run database migration"
sudo -u "$RUN_USER" "$APP_DIR/venv/bin/python" core/db_migration.py \
    || echo "WARNING: migration reported an error; continuing because migrations are idempotent."
secure_runtime_secrets "$RUN_USER"

echo
echo "[5/6] Restart service"
ensure_platform_scheduler_split
ensure_data_scheduler_service
systemctl restart "$SERVICE"
if data_scheduler_service_exists; then
    verify_scheduler_quiescence
    systemctl restart quant-scheduler
    echo "Restarted quant-scheduler (data schedulers)."
fi
sleep 4

echo
echo "[6/6] Health check"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/api/auth/me" --max-time 8 || echo "000")
case "$HTTP_CODE" in
    200|401|403)
        echo "Service healthy (HTTP $HTTP_CODE)"
        ;;
    *)
        echo "ERROR: service unhealthy (HTTP $HTTP_CODE)"
        echo "Check logs: journalctl -u $SERVICE -n 50 --no-pager"
        exit 1
        ;;
esac

echo
echo "Update complete, commit: $REMOTE"
