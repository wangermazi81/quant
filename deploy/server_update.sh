#!/bin/bash
# Jianwei Alpha server update script.
# Usage on server:
#   bash /opt/quant/deploy/server_update.sh

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/quant}"
SERVICE="${SERVICE:-quant-platform}"
PORT="${PORT:-5000}"
FRONTEND_DIST="$APP_DIR/static/dist/index.html"

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

echo "[1/6] Pull latest code from Gitee"
git fetch origin --prune
LOCAL=$(git rev-parse HEAD)
git pull --ff-only origin "$(git branch --show-current)" || {
    echo "ERROR: git pull failed. Check server-side uncommitted changes with: git status"
    exit 1
}
REMOTE=$(git rev-parse HEAD)

if [ "$LOCAL" = "$REMOTE" ] && [ -f "$FRONTEND_DIST" ]; then
    echo "Code is already up to date and frontend dist exists."
    exit 0
fi

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "Code is up to date, but frontend dist is missing; continue build."
else
    echo "Updated: $LOCAL -> $REMOTE"
fi

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
echo "[3/6] Build frontend for /v2"
if [ -f "$APP_DIR/frontend/package.json" ]; then
    if command -v npm >/dev/null 2>&1; then
        (
            cd "$APP_DIR/frontend"
            if [ -f package-lock.json ]; then
                npm ci --no-audit --no-fund
            else
                npm install --no-audit --no-fund
            fi
            npm run build
        )
        if [ "$RUN_USER" != "root" ]; then
            chown -R "$RUN_USER:$RUN_USER" "$APP_DIR/static" "$APP_DIR/frontend/node_modules" 2>/dev/null || true
        fi
    elif [ ! -f "$FRONTEND_DIST" ]; then
        echo "ERROR: npm is not installed and $FRONTEND_DIST is missing."
        echo "Install nodejs/npm or upload a built static/dist artifact."
        exit 1
    else
        echo "npm is not installed, but frontend dist exists; skip build."
    fi
else
    echo "frontend/package.json not found; skip frontend build."
fi

echo
echo "[4/6] Run database migration"
sudo -u "$RUN_USER" "$APP_DIR/venv/bin/python" core/db_migration.py \
    || echo "WARNING: migration reported an error; continuing because migrations are idempotent."
secure_runtime_secrets "$RUN_USER"

echo
echo "[5/6] Restart service"
systemctl restart "$SERVICE"
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
