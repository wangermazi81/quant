#!/bin/bash
# ════════════════════════════════════════════════════════════════
# 见微 Alpha — 服务器日常更新脚本
# ════════════════════════════════════════════════════════════════
# 用法（在服务器）：
#   bash /opt/quant/deploy/server_update.sh
#
# 流程：
#   1. git pull 最新代码（gitee）
#   2. 检查 requirements.txt 是否变化，自动重装依赖
#   3. 跑数据库迁移（幂等）
#   4. 重启 systemd 服务
#   5. 验证 5xx 响应
# ════════════════════════════════════════════════════════════════
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/quant}"
SERVICE="${SERVICE:-quant-platform}"
PORT="${PORT:-5000}"

cd "$APP_DIR"

# 仓库已 chown 给非 root 服务用户(降权后)，root 运行 git 需加 safe.directory 白名单。
git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true
# 服务实际运行用户（systemd User=），拉取后据此恢复属主、以其身份跑迁移。
RUN_USER=$(systemctl show "$SERVICE" -p User --value 2>/dev/null); RUN_USER="${RUN_USER:-root}"

echo "▶ [1/5] 拉取最新代码 (gitee)"
# 仅 fetch origin（已指向可用 SSH 远端），避免 --all 触发历史遗留坏远端导致中止
git fetch origin --prune
LOCAL=$(git rev-parse HEAD)
git pull --ff-only origin "$(git branch --show-current)" || {
    echo "❌ git pull 失败。常见原因：服务器侧有未提交改动覆盖了 tracked 文件"
    echo "   排查：git status   如确认丢弃服务器改动：git reset --hard origin/$(git branch --show-current)"
    exit 1
}
REMOTE=$(git rev-parse HEAD)
if [ "$LOCAL" = "$REMOTE" ]; then
    echo "  代码已是最新，无需更新"
    exit 0
fi
echo "  $LOCAL → $REMOTE"
# 拉取的文件以 root 写入，恢复属主给服务用户（app 非 root 需能读新文件）
if [ "$RUN_USER" != "root" ]; then
    chown -R "$RUN_USER:$RUN_USER" "$APP_DIR"
    echo "  已恢复属主 $RUN_USER:$RUN_USER"
fi

echo ""
echo "▶ [2/5] 检查依赖是否变化"
# shellcheck disable=SC1091
source venv/bin/activate
if git diff --name-only "$LOCAL" "$REMOTE" | grep -q "^requirements.txt$"; then
    echo "  requirements.txt 有变化，重装依赖"
    pip install -r requirements.txt
else
    echo "  requirements.txt 未变化，跳过 pip install"
fi

echo ""
echo "▶ [3/5] 运行数据库迁移（幂等）"
# 以服务用户身份跑，避免迁移产生的 -wal/-shm 文件归 root → app(非root) 写不动
sudo -u "$RUN_USER" "$APP_DIR/venv/bin/python" core/db_migration.py \
    || echo "  ⚠️  migration 报错（可能是兼容 try/except，不致命）"

echo ""
echo "▶ [4/5] 重启服务"
systemctl restart "$SERVICE"
sleep 4

echo ""
echo "▶ [5/5] 健康检查"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/api/auth/me" --max-time 8 || echo "000")
case "$HTTP_CODE" in
    200|401|403)
        # 200 已登录，401/403 未登录但服务在跑
        echo "  ✅ 服务健康 (HTTP $HTTP_CODE)"
        ;;
    *)
        echo "  ❌ 服务无响应或异常 (HTTP $HTTP_CODE)"
        echo "  查看日志:  journalctl -u $SERVICE -n 50 --no-pager"
        exit 1
        ;;
esac

echo ""
echo "✅ 更新完成，commit: $REMOTE"
