#!/bin/bash
# ════════════════════════════════════════════════════════════════
# 见微 Alpha — 云服务器首次部署脚本（Git Clone 流程版）
# ════════════════════════════════════════════════════════════════
# 使用前提：
#   1. 服务器系统：Ubuntu 20.04 / 22.04（其它发行版需调整 apt → yum/dnf）
#   2. 已通过 ssh root@<服务器IP> 登录
#   3. Gitee 上已有仓库（默认 https://gitee.com/wangermazi81/quant.git）
#
# 用法：
#   curl -L -o init.sh https://gitee.com/wangermazi81/quant/raw/main/deploy/server_init.sh
#   chmod +x init.sh
#   bash init.sh
# ════════════════════════════════════════════════════════════════
set -euo pipefail

# ───────────────────────────────────────────
# 配置（按需修改）
# ───────────────────────────────────────────
APP_DIR="/opt/quant"
SERVICE="quant-platform"
GITEE_REPO="${GITEE_REPO:-https://gitee.com/wangermazi81/quant.git}"
BRANCH="${BRANCH:-main}"
PORT="${PORT:-5000}"
PY_VER="${PY_VER:-3.11}"   # Ubuntu 22.04 默认 3.10，建议安装 3.11+

echo ""
echo "╔════════════════════════════════════════════════════════════╗"
echo "║  见微 Alpha 云服务器首次部署                                 ║"
echo "║  APP_DIR  = $APP_DIR"
echo "║  REPO     = $GITEE_REPO"
echo "║  BRANCH   = $BRANCH"
echo "║  PORT     = $PORT"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""

# ───────────────────────────────────────────
# 1. 系统依赖
# ───────────────────────────────────────────
echo "▶ [1/8] 安装系统依赖"
apt-get update -y
apt-get install -y \
    python3 python3-pip python3-venv python3-dev \
    git curl unzip build-essential \
    nginx \
    sqlite3
# python3-dev / build-essential 是给 pandas/numpy 编译用

# ───────────────────────────────────────────
# 2. 从 Gitee 克隆代码
# ───────────────────────────────────────────
echo ""
echo "▶ [2/8] 从 Gitee 克隆代码"
if [ -d "$APP_DIR/.git" ]; then
    echo "  $APP_DIR 已是 git 仓库，跳过 clone（如需重置：先 rm -rf $APP_DIR）"
else
    mkdir -p "$(dirname $APP_DIR)"
    git clone --branch "$BRANCH" "$GITEE_REPO" "$APP_DIR"
fi
cd "$APP_DIR"
mkdir -p logs data data/daily

# ───────────────────────────────────────────
# 3. Python 虚拟环境 + 依赖
# ───────────────────────────────────────────
echo ""
echo "▶ [3/8] 配置 Python 虚拟环境"
if [ ! -d venv ]; then
    python3 -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt

# ───────────────────────────────────────────
# 4. 配置 .env
# ───────────────────────────────────────────
echo ""
echo "▶ [4/8] 配置环境变量"
if [ ! -f .env ]; then
    cp .env.example .env
    # 自动生成 SECRET_KEY（32 字节 hex）
    SK=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$SK|" .env
    # 注释掉 AI_* 占位符行，避免覆盖网页 / ai_config.json 里的真实 key
    # （网页设置 + scp 同步 ai_config.json 是更常见的工作流）
    sed -i 's|^AI_API_KEY=|# AI_API_KEY=|; s|^AI_BASE_URL=|# AI_BASE_URL=|; s|^AI_MODEL=|# AI_MODEL=|' .env
    echo "  ✅ 已生成随机 SECRET_KEY"
    echo "  ✅ 已注释 AI_* 占位符（用网页配置 / 同步 ai_config.json 即可）"
else
    echo "  .env 已存在，跳过"
fi

# ───────────────────────────────────────────
# 5. 上传隐私文件提示
# ───────────────────────────────────────────
echo ""
echo "▶ [5/8] 上传隐私文件（手动步骤）"
echo "  在 ★本地★ Windows 跑（不要在服务器跑）："
echo "    cd D:\\同花顺量化交易\\ths_auto_trader"
echo "    powershell -ExecutionPolicy Bypass -File .\\bundle_private.ps1"
echo "  然后 scp 上传到服务器："
echo "    scp _private_bundle_*.zip root@<服务器IP>:$APP_DIR/"
echo "  上传完成后，在服务器跑："
echo "    cd $APP_DIR && unzip -o _private_bundle_*.zip && rm _private_bundle_*.zip"
echo ""
read -r -p "  是否已上传并解压了隐私文件？[y/N] " ANS
if [ "$ANS" != "y" ] && [ "$ANS" != "Y" ]; then
    echo "  ⚠️  请上传隐私 bundle 后重新运行本脚本，或手动继续后续步骤"
    exit 0
fi

# ───────────────────────────────────────────
# 6. 数据库迁移
# ───────────────────────────────────────────
echo ""
echo "▶ [6/8] 运行数据库迁移"
python3 core/db_migration.py || true   # 幂等，失败也不致命

# ───────────────────────────────────────────
# 7. systemd 服务
# ───────────────────────────────────────────
echo ""
echo "▶ [7/8] 配置 systemd 服务"
cat > /etc/systemd/system/$SERVICE.service << EOF
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
systemctl enable $SERVICE
systemctl restart $SERVICE
sleep 3
systemctl --no-pager status $SERVICE || true

# ───────────────────────────────────────────
# 8. Nginx 反向代理
# ───────────────────────────────────────────
echo ""
echo "▶ [8/8] 配置 Nginx 反向代理"
cat > /etc/nginx/sites-available/$SERVICE << EOF
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
ln -sf /etc/nginx/sites-available/$SERVICE /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

# ───────────────────────────────────────────
# 结果汇总
# ───────────────────────────────────────────
PUBLIC_IP=$(curl -s --max-time 5 ifconfig.me || echo "<服务器公网IP>")
echo ""
echo "╔════════════════════════════════════════════════════════════╗"
echo "║  ✅ 部署完成                                                  ║"
echo "╠════════════════════════════════════════════════════════════╣"
echo "║  访问地址  : http://$PUBLIC_IP"
echo "║  服务名    : $SERVICE"
echo "║  日志      : journalctl -u $SERVICE -f"
echo "║  重启      : systemctl restart $SERVICE"
echo "║  停止      : systemctl stop $SERVICE"
echo "║  更新代码  : bash $APP_DIR/deploy/server_update.sh"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""
echo "⚠️  实盘交易（pywinauto/THS）不能在 Linux 上运行"
echo "   云端可用：模拟盘、回测、AI 分析、扫描、策略管理"
echo ""
