# -*- coding: utf-8 -*-
import requests, time, json

s = requests.Session()
r = s.post('http://127.0.0.1:5001/api/auth/login',
           json={'username': 'admin', 'password': 'admin123'})
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
