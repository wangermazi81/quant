# -*- coding: utf-8 -*-
import os, requests, time, json

# 管理员密码已改为启动时随机生成；冒烟测试从环境变量读取
ADMIN_USER = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PW   = os.environ.get('ADMIN_PASSWORD')
if not ADMIN_PW:
    raise SystemExit('请先设置环境变量 ADMIN_PASSWORD（与启动服务时一致）再运行本冒烟测试')

s = requests.Session()
r = s.post('http://127.0.0.1:5001/api/auth/login',
           json={'username': ADMIN_USER, 'password': ADMIN_PW})
print('login:', r.status_code, r.json().get('ok'))

t0 = time.time()
r2 = s.post('http://127.0.0.1:5001/api/ai/analyze',
            json={'code': '000001', 'name': 'test'}, timeout=15)
elapsed = time.time() - t0
d = r2.json()
print(f'HTTP {r2.status_code}, {elapsed:.2f}s')
print(f'ok={d.get("ok")} ai_enabled={d.get("ai_enabled")}')
print(f'message={d.get("message", d.get("error", ""))[:100]}')
score = d.get('score', {})
print(f'score total={score.get("total")} signal={score.get("signal")}')
