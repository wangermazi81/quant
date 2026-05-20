# -*- coding: utf-8 -*-
"""
小米MiMo API 诊断脚本
运行方式：python test_mimo.py
会打印出服务器返回的完整原始内容，帮助定位 400 错误原因
"""
import urllib.request
import urllib.error
import json
import sys

# ────────────────────────────────────────
# 从 data/ai_config.json 自动读取配置
# ────────────────────────────────────────
import pathlib

cfg_path = pathlib.Path(__file__).parent.parent / "data" / "ai_config.json"
if not cfg_path.exists():
    print("❌ 找不到 data/ai_config.json，请先在网页保存一次AI配置")
    sys.exit(1)

cfg = json.loads(cfg_path.read_text("utf-8"))
API_KEY  = cfg.get("api_key", "")
BASE_URL = cfg.get("base_url", "").rstrip("/")
MODEL    = cfg.get("model", "")

print(f"读取到配置：")
print(f"  Base URL : {BASE_URL}")
print(f"  Model    : {MODEL}")
print(f"  API Key  : {API_KEY[:8]}****{API_KEY[-4:] if len(API_KEY)>12 else ''}")
print()

# ────────────────────────────────────────
# 逐步尝试不同参数组合
# ────────────────────────────────────────
test_cases = [
    {"desc": "标准参数 temperature=0.1 max_tokens=16",
     "body": {"model": MODEL, "messages": [{"role": "user", "content": "hi"}],
              "temperature": 0.1, "max_tokens": 16}},
    {"desc": "只传 model + messages（去掉 temperature/max_tokens）",
     "body": {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}},
    {"desc": "加 stream=false",
     "body": {"model": MODEL, "messages": [{"role": "user", "content": "hi"}],
              "temperature": 0.7, "max_tokens": 50, "stream": False}},
]

for i, tc in enumerate(test_cases, 1):
    print(f"═══ 测试 {i}：{tc['desc']} ═══")
    url  = f"{BASE_URL}/chat/completions"
    body = json.dumps(tc["body"]).encode("utf-8")
    req  = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {API_KEY}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            reply = data["choices"][0]["message"]["content"].strip()
            print(f"✅ 成功！回复：{reply}")
            print(f"   成功参数：{tc['body']}")
            break
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="ignore")
        print(f"❌ HTTP {e.code}  原始响应：{raw}")
    except Exception as ex:
        print(f"❌ 异常：{ex}")
    print()
