#!/bin/bash
# Jianwei Alpha server update script.
# Usage on server:
#   bash /opt/quant/deploy/server_update.sh

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/quant}"
SERVICE="${SERVICE:-quant-platform}"
PORT="${PORT:-5000}"
QUIESCENCE_HELPER="${QUIESCENCE_HELPER:-$APP_DIR/deploy/check_scheduler_quiescence.py}"
HEALTH_HELPER="${HEALTH_HELPER:-$APP_DIR/deploy/check_scheduler_health.py}"
DEPLOY_PYTHON="${DEPLOY_PYTHON:-/usr/bin/python3}"
PERMISSIONS_HELPER="${PERMISSIONS_HELPER:-$APP_DIR/deploy/harden_runtime_permissions.py}"
DEPLOY_REMOTE_URL="${DEPLOY_REMOTE_URL:-}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-}"
DEPLOY_TARGET_SHA="${DEPLOY_TARGET_SHA:-}"
DEPLOY_TOOL_BUNDLE_SHA256="${DEPLOY_TOOL_BUNDLE_SHA256:-}"
DEPLOY_GIT_SSH_COMMAND="${DEPLOY_GIT_SSH_COMMAND:-}"
DEPLOY_LOCK_FILE="${DEPLOY_LOCK_FILE:-/run/lock/jwalpha-deploy.lock}"
DEPLOY_COMPLETED=0
CODE_UPDATED=0
QUIESCE_STARTED=0
SCHEDULER_WAS_ACTIVE=0
SCHEDULER_SHOULD_RUN=0
SCHEDULER_UNIT_PATH="/etc/systemd/system/quant-scheduler.service"
SCHEDULER_UNIT_WAS_PRESENT=0
SCHEDULER_UNIT_BACKUP=""
PLATFORM_SPLIT_OVERRIDE="/etc/systemd/system/$SERVICE.service.d/scheduler-split.conf"
PLATFORM_SPLIT_WAS_PRESENT=0
PLATFORM_SPLIT_BACKUP=""
WEB_VENV_OVERRIDE="/etc/systemd/system/$SERVICE.service.d/release-venv.conf"
RELEASE_VENV=""
# FRONTEND_DIST 已移除（工单7c：/v2 双轨下线，前端无构建步骤）

exec 9>"$DEPLOY_LOCK_FILE"
if ! /usr/bin/flock -n 9; then
    echo "ERROR: another Jianwei Alpha deployment is already running."
    exit 1
fi

cd "$APP_DIR"

RUN_USER=$(systemctl show "$SERVICE" -p User --value 2>/dev/null)
RUN_USER="${RUN_USER:-root}"

trusted_git() {
    local -a trusted_environment=(
        "HOME=/root"
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        "GIT_CONFIG_NOSYSTEM=1"
        "GIT_CONFIG_SYSTEM=/dev/null"
        "GIT_CONFIG_GLOBAL=/dev/null"
        "GIT_TERMINAL_PROMPT=0"
    )
    if [ -n "$DEPLOY_GIT_SSH_COMMAND" ]; then
        trusted_environment+=(
            "GIT_SSH_COMMAND=$DEPLOY_GIT_SSH_COMMAND"
        )
    fi
    env -i \
        "${trusted_environment[@]}" \
        git \
        -c core.hooksPath=/dev/null \
        -c credential.helper= \
        -c protocol.file.allow=never \
        "$@"
}

secure_runtime_secrets() {
    local owner="$1"
    "$DEPLOY_PYTHON" -I "$PERMISSIONS_HELPER" \
        --app-dir "$APP_DIR" \
        --run-user "$owner" \
        --runtime-only
}

secure_application_ownership() {
    local owner="$1"
    if [ "$(id -u)" != "0" ]; then
        echo "ERROR: application ownership hardening requires root privileges."
        return 1
    fi
    if [ "$owner" = "root" ] || ! id "$owner" >/dev/null 2>&1; then
        echo "ERROR: platform service must use an existing non-root account."
        return 1
    fi
    if [ ! -f "$PERMISSIONS_HELPER" ]; then
        echo "ERROR: permission hardening helper is unavailable: $PERMISSIONS_HELPER"
        return 1
    fi
    "$DEPLOY_PYTHON" -I "$PERMISSIONS_HELPER" \
        --app-dir "$APP_DIR" \
        --run-user "$owner"
}

reset_git_configuration() {
    "$DEPLOY_PYTHON" -I "$PERMISSIONS_HELPER" \
        --app-dir "$APP_DIR" \
        --run-user "$RUN_USER" \
        --git-config-only \
        --remote-url "$DEPLOY_REMOTE_URL" \
        --branch "$DEPLOY_BRANCH"
}

verify_deploy_python() {
    local resolved owner mode
    resolved=$(readlink -f "$DEPLOY_PYTHON")
    if [ ! -x "$resolved" ]; then
        echo "ERROR: trusted deployment Python is unavailable: $DEPLOY_PYTHON"
        return 1
    fi
    owner=$(stat -c '%u' "$resolved")
    mode=$(stat -c '%a' "$resolved")
    if [ "$owner" != "0" ] || (( (8#$mode & 022) != 0 )); then
        echo "ERROR: deployment Python must be root-owned and not group/world writable."
        return 1
    fi
}

verify_clean_checkout() {
    local untracked
    if ! trusted_git diff --quiet --; then
        echo "ERROR: tracked working-tree changes must be committed or reverted."
        return 1
    fi
    if ! trusted_git diff --cached --quiet --; then
        echo "ERROR: staged changes must be committed or reverted."
        return 1
    fi
    untracked=$(trusted_git ls-files --others --exclude-standard)
    if [ -n "$untracked" ]; then
        echo "ERROR: untracked files are not allowed in the deployment checkout."
        printf '%s\n' "$untracked"
        return 1
    fi
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

restore_services_on_failure() {
    local status=$?
    trap - EXIT
    if [ "$status" -ne 0 ] \
        && [ "$DEPLOY_COMPLETED" != "1" ] \
        && [ "$QUIESCE_STARTED" = "1" ]; then
        if [ "${CODE_UPDATED:-0}" = "1" ]; then
            echo "Deployment failed after code update; leaving services stopped for a safe retry."
            systemctl stop quant-scheduler || true
            systemctl stop "$SERVICE" || true
        else
            echo "Deployment failed before code update; restoring prior service state."
            if [ "$PLATFORM_SPLIT_WAS_PRESENT" = "1" ]; then
                cp "$PLATFORM_SPLIT_BACKUP" "$PLATFORM_SPLIT_OVERRIDE" \
                    || true
            else
                rm -f "$PLATFORM_SPLIT_OVERRIDE" || true
            fi
            if [ "$SCHEDULER_UNIT_WAS_PRESENT" = "1" ]; then
                cp "$SCHEDULER_UNIT_BACKUP" "$SCHEDULER_UNIT_PATH" \
                    || true
            else
                rm -f "$SCHEDULER_UNIT_PATH" || true
            fi
            systemctl daemon-reload || true
            systemctl restart "$SERVICE" || true
            if [ "$SCHEDULER_SHOULD_RUN" = "1" ]; then
                systemctl enable quant-scheduler || true
            else
                systemctl disable quant-scheduler || true
            fi
            if [ "$SCHEDULER_WAS_ACTIVE" = "1" ]; then
                systemctl restart quant-scheduler || true
            elif [ "$SCHEDULER_SHOULD_RUN" = "1" ]; then
                systemctl restart quant-scheduler || true
            else
                systemctl stop quant-scheduler || true
            fi
        fi
    fi
    if [ -n "$PLATFORM_SPLIT_BACKUP" ]; then
        rm -f "$PLATFORM_SPLIT_BACKUP" || true
    fi
    if [ -n "$SCHEDULER_UNIT_BACKUP" ]; then
        rm -f "$SCHEDULER_UNIT_BACKUP" || true
    fi
    exit "$status"
}

trap restore_services_on_failure EXIT

ensure_data_scheduler_service() {
    if [ "$(id -u)" != "0" ]; then
        echo "ERROR: quant-scheduler install requires root privileges."
        return 1
    fi

    local unit_src="$APP_DIR/deploy/quant-scheduler.service"
    local unit_dst="/etc/systemd/system/quant-scheduler.service"
    if [ ! -f "$unit_src" ]; then
        echo "ERROR: scheduler unit source is missing: $unit_src"
        return 1
    fi

    cp "$unit_src" "$unit_dst"
    if [ -n "$RUN_USER" ] && [ "$RUN_USER" != "quant" ]; then
        sed -i "s/^User=.*/User=$RUN_USER/" "$unit_dst"
    fi
    sed -i "s|/opt/quant|$APP_DIR|g" "$unit_dst"
    sed -i "s|$APP_DIR/venv|$RELEASE_VENV|g" "$unit_dst"
    systemctl daemon-reload
    systemctl enable quant-scheduler >/dev/null
}

ensure_release_venv_override() {
    if [ "$(id -u)" != "0" ]; then
        echo "ERROR: release venv override requires root privileges."
        return 1
    fi
    if [ -z "$RELEASE_VENV" ] \
        || [ ! -x "$RELEASE_VENV/bin/gunicorn" ]; then
        echo "ERROR: release venv is incomplete: $RELEASE_VENV"
        return 1
    fi
    mkdir -p "$(dirname "$WEB_VENV_OVERRIDE")"
    cat > "$WEB_VENV_OVERRIDE" <<EOF
[Service]
ExecStart=
ExecStart=$RELEASE_VENV/bin/gunicorn -c $APP_DIR/deploy/gunicorn.conf.py main:app
EOF
    systemctl daemon-reload
}

prepare_release_venv() {
    local releases_dir="$APP_DIR/.venv-releases"
    local runtime_home="$APP_DIR/data"
    install -d -o root -g "$RUN_USER" -m 0750 "$releases_dir"
    RELEASE_VENV=$(mktemp -d "$releases_dir/release-$REMOTE.XXXXXX")
    chown "$RUN_USER:$RUN_USER" "$RELEASE_VENV"
    chmod 0700 "$RELEASE_VENV"
    sudo -u "$RUN_USER" env -i \
        HOME="$runtime_home" \
        PATH="/usr/local/bin:/usr/bin:/bin" \
        PIP_CONFIG_FILE="/dev/null" \
        "$DEPLOY_PYTHON" -I -m venv --copies "$RELEASE_VENV"
    sudo -u "$RUN_USER" env -i \
        HOME="$runtime_home" \
        PATH="/usr/local/bin:/usr/bin:/bin" \
        PIP_CONFIG_FILE="/dev/null" \
        "$RELEASE_VENV/bin/python" -I -m pip install \
        --upgrade pip wheel
    sudo -u "$RUN_USER" env -i \
        HOME="$runtime_home" \
        PATH="/usr/local/bin:/usr/bin:/bin" \
        PIP_CONFIG_FILE="/dev/null" \
        "$RELEASE_VENV/bin/python" -I -m pip install \
        -r "$APP_DIR/requirements.txt"
    secure_application_ownership "$RUN_USER"
}

ensure_platform_scheduler_split() {
    if [ "$(id -u)" != "0" ]; then
        echo "ERROR: platform scheduler split requires root privileges."
        return 1
    fi

    mkdir -p "$(dirname "$PLATFORM_SPLIT_OVERRIDE")"
    cat > "$PLATFORM_SPLIT_OVERRIDE" <<'EOF'
[Service]
Environment=THS_EMBED_SCHEDULER=0
UMask=0077
EOF
    systemctl daemon-reload
}

verify_scheduler_quiescence() {
    if [ ! -f "$QUIESCENCE_HELPER" ]; then
        echo "ERROR: scheduler quiescence helper is unavailable: $QUIESCENCE_HELPER"
        echo "Run deploy/bootstrap_server_update.py from the fetched remote revision."
        exit 1
    fi
    "$DEPLOY_PYTHON" -I \
        "$QUIESCENCE_HELPER" \
        --app-dir "$APP_DIR" \
        --web-service "$SERVICE" \
        --cleanup-lock
}

verify_scheduler_health() {
    if ! systemctl is-enabled --quiet quant-scheduler; then
        echo "ERROR: quant-scheduler is not enabled."
        exit 1
    fi
    if ! systemctl is-active --quiet quant-scheduler; then
        echo "ERROR: quant-scheduler is not active."
        systemctl status quant-scheduler --no-pager || true
        exit 1
    fi
    local scheduler_pid
    scheduler_pid=$(systemctl show quant-scheduler -p MainPID --value)
    if ! [[ "$scheduler_pid" =~ ^[1-9][0-9]*$ ]]; then
        echo "ERROR: quant-scheduler MainPID is invalid: $scheduler_pid"
        exit 1
    fi
    "$DEPLOY_PYTHON" -I \
        "$HEALTH_HELPER" \
        --app-dir "$APP_DIR" \
        --expected-pid "$scheduler_pid" \
        --wait-seconds 20
}

quiesce_legacy_schedulers() {
    if [ "$(id -u)" != "0" ]; then
        echo "ERROR: scheduler protocol migration requires root privileges."
        exit 1
    fi

    if [ -f "$PLATFORM_SPLIT_OVERRIDE" ]; then
        PLATFORM_SPLIT_WAS_PRESENT=1
        PLATFORM_SPLIT_BACKUP=$(mktemp)
        cp "$PLATFORM_SPLIT_OVERRIDE" "$PLATFORM_SPLIT_BACKUP"
    fi
    if [ -f "$SCHEDULER_UNIT_PATH" ]; then
        SCHEDULER_UNIT_WAS_PRESENT=1
        SCHEDULER_UNIT_BACKUP=$(mktemp)
        cp "$SCHEDULER_UNIT_PATH" "$SCHEDULER_UNIT_BACKUP"
    fi
    if systemctl is-enabled --quiet quant-scheduler; then
        SCHEDULER_SHOULD_RUN=1
    fi
    if data_scheduler_service_exists \
        && systemctl is-active --quiet quant-scheduler; then
        SCHEDULER_WAS_ACTIVE=1
    fi
    QUIESCE_STARTED=1

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
    secure_runtime_secrets "$RUN_USER"
    echo "Stopped legacy data schedulers before protocol migration."
}

echo "[0/7] Verify remote revision and local state"
if [ "${SCHEDULER_MIGRATION_BOOTSTRAPPED:-}" != "1" ]; then
    echo "ERROR: deployment must run through deploy/bootstrap_server_update.py."
    exit 1
fi
if [[ "$DEPLOY_REMOTE_URL" != https://* \
        && "$DEPLOY_REMOTE_URL" != ssh://git@* ]] \
    || [ -z "$DEPLOY_BRANCH" ] \
    || ! [[ "$DEPLOY_BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] \
    || [[ "$DEPLOY_BRANCH" == *".."* ]] \
    || [[ "$DEPLOY_BRANCH" == *"//"* ]] \
    || ! [[ "$DEPLOY_TARGET_SHA" =~ ^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$ ]] \
    || ! [[ "$DEPLOY_TOOL_BUNDLE_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
    echo "ERROR: pinned deployment revision is missing or invalid."
    exit 1
fi
verify_deploy_python
secure_application_ownership "$RUN_USER"
reset_git_configuration
REMOTE_REF="refs/remotes/origin/$DEPLOY_BRANCH"
trusted_git fetch --prune --no-tags \
    "$DEPLOY_REMOTE_URL" \
    "+refs/heads/$DEPLOY_BRANCH:$REMOTE_REF"
BRANCH_NAME=$(trusted_git branch --show-current)
if [ "$BRANCH_NAME" != "$DEPLOY_BRANCH" ]; then
    echo "ERROR: checkout branch does not match pinned deployment branch."
    exit 1
fi
REMOTE_TARGET="$DEPLOY_TARGET_SHA"
CURRENT_REMOTE_TARGET=$(trusted_git rev-parse "$REMOTE_REF")
if [ "$CURRENT_REMOTE_TARGET" != "$REMOTE_TARGET" ]; then
    echo "ERROR: $REMOTE_REF moved after deployment tools were staged."
    exit 1
fi
verify_clean_checkout
LOCAL=$(trusted_git rev-parse HEAD)
if ! trusted_git merge-base --is-ancestor "$LOCAL" "$REMOTE_TARGET"; then
    echo "ERROR: local HEAD is ahead of or diverged from $REMOTE_REF."
    exit 1
fi

quiesce_legacy_schedulers
systemctl stop "$SERVICE"

echo "[1/7] Fast-forward to verified remote revision"
trusted_git merge --ff-only "$REMOTE_TARGET"
REMOTE=$(trusted_git rev-parse HEAD)
if [ "$REMOTE" != "$REMOTE_TARGET" ]; then
    echo "ERROR: deployed HEAD does not match $REMOTE_REF."
    exit 1
fi
verify_clean_checkout
CODE_UPDATED=1
secure_application_ownership "$RUN_USER"
sync_nginx_anti_crawl

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "Code is already up to date."
else
    echo "Updated: $LOCAL -> $REMOTE"
fi

echo
echo "[2/7] Check Python dependencies"
prepare_release_venv

echo
echo "[3/7] Frontend build no longer needed"
# 工单7c：/v2 Vite 双轨已移除，前端 = index.html 骨架 + static/app/ 分文件
# （随 git 同步，无构建步骤）。旧 frontend/ 目录若仍在服务器上可安全删除。
echo "Frontend is served from static/app (no build step since 工单7c)."

echo
echo "[4/7] Run database migration"
sudo -u "$RUN_USER" "$RELEASE_VENV/bin/python" core/db_migration.py
secure_runtime_secrets "$RUN_USER"

echo
echo "[5/7] Install scheduler services"
ensure_platform_scheduler_split
ensure_release_venv_override
ensure_data_scheduler_service

echo
echo "[6/7] Restart services"
systemctl restart "$SERVICE"
verify_scheduler_quiescence
systemctl restart quant-scheduler
verify_scheduler_health
echo "Restarted quant-scheduler (data schedulers)."
sleep 4

echo
echo "[7/7] Health check"
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
DEPLOY_COMPLETED=1
