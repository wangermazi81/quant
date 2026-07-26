#!/bin/bash
# 云服务器一键部署脚本（Ubuntu 22.04）
# 使用方法：ssh 登录服务器后执行 bash deploy.sh

set -e
APP_DIR="/opt/quant"
SERVICE="quant-platform"

echo "=== 1. 安装系统依赖 ==="
apt-get update -y
apt-get install -y python3 python3-pip python3-venv nginx git nodejs npm

echo "=== 2. 创建目录 ==="
mkdir -p $APP_DIR/logs

echo "=== 3. 上传代码（手动步骤，此处跳过）==="
echo "  请将项目文件上传到 $APP_DIR"
echo "  可使用: scp -r ./ths_auto_trader/* root@服务器IP:$APP_DIR/"

echo "=== 4. 安装 Python 依赖 ==="
cd $APP_DIR
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

echo "=== 5. 配置环境变量 ==="
if [ ! -f .env ]; then
    cp .env.example .env
    # 云端 HTTPS：Secure Cookie 必须开（.env.example 默认 0 是给本机 http 开发用的）
    sed -i "s|^SESSION_COOKIE_SECURE=.*|SESSION_COOKIE_SECURE=1|" .env
    echo "  ⚠️  请编辑 $APP_DIR/.env 填入真实的 API Key 和 SECRET_KEY"
    echo "  ✓ 已强制 SESSION_COOKIE_SECURE=1"
fi

echo "=== 6. 配置 systemd 服务 ==="
cat > /etc/systemd/system/$SERVICE.service << EOF
[Unit]
Description=Quant Trading Platform
After=network.target

[Service]
User=root
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/gunicorn -c gunicorn.conf.py main:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable $SERVICE
systemctl restart $SERVICE

echo "=== 7. 配置 Nginx ==="
cat > /etc/nginx/sites-available/$SERVICE << 'EOF'
server {
    listen 80;
    server_name _;

    client_max_body_size 10M;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 300s;
        proxy_connect_timeout 10s;
    }

    location /static/ {
        alias /opt/quant/static/;
        expires 7d;
    }
}
EOF

ln -sf /etc/nginx/sites-available/$SERVICE /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

echo ""
echo "=== ✅ 部署完成 ==="
echo "   访问: http://$(curl -s ifconfig.me)"
echo "   查看日志: journalctl -u $SERVICE -f"
echo "   重启服务: systemctl restart $SERVICE"
