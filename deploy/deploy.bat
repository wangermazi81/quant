@echo off
chcp 65001 >nul
echo ========================================
echo   见微 Alpha — 一键部署到服务器
echo ========================================

set SERVER=root@YOUR_SERVER_IP
set REMOTE=/opt/quant
set LOCAL=D:\同花顺量化交易\ths_auto_trader

echo [1/4] 打包代码文件...
cd /d %LOCAL%
powershell -Command "Compress-Archive -Path *.py,*.html,*.txt,*.cfg -DestinationPath deploy_temp.zip -Force"

echo [2/4] 上传代码...
scp deploy_temp.zip %SERVER%:%REMOTE%/
scp data\ai_config.json %SERVER%:%REMOTE%/data/ai_config.json
scp data\search_config.json %SERVER%:%REMOTE%/data/search_config.json

echo [3/4] 解压并重启服务器...
ssh %SERVER% "cd %REMOTE% && unzip -o deploy_temp.zip -d . && rm deploy_temp.zip && pkill -f gunicorn; sleep 1; source venv/bin/activate && nohup gunicorn -c gunicorn.conf.py main:app > app.log 2>&1 &"

echo [4/4] 清理临时文件...
del deploy_temp.zip

echo ========================================
echo   部署完成！访问 https://jwalpha.top
echo ========================================
pause
