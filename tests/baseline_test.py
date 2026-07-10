import requests, sys, json
BASE = "http://127.0.0.1:5001"
s = requests.Session()

results = {}

def check(name, ok, detail=""):
    results[name] = {"ok": bool(ok), "detail": detail}
    flag = "OK" if ok else "FAIL"
    print(f"[{flag}] {name}: {detail}")

try:
    r = s.get(BASE + "/", timeout=5)
    check("flask_alive", r.status_code == 200, f"HTTP {r.status_code}")
except Exception as e:
    check("flask_alive", False, f"exception: {e}")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    sys.exit(1)

try:
    import json as _j
    cfg = _j.load(open(r"D:\同花顺量化交易\ths_auto_trader\tools\bridge_config.json", encoding="utf-8"))
    r = s.post(BASE + "/api/auth/login", json={"username": cfg["username"], "password": cfg["password"]}, timeout=5)
    j = r.json()
    check("login_admin", j.get("ok") is True, f"resp={j}")
except Exception as e:
    check("login_admin", False, f"exception: {e}")

try:
    r = s.get(BASE + "/api/paper/positions", timeout=10)
    j = r.json()
    pos_count = len(j.get("positions", []) if isinstance(j, dict) else j)
    check("paper_positions", "positions" in j or isinstance(j, list), f"positions count={pos_count}")
except Exception as e:
    check("paper_positions", False, f"exception: {e}")

try:
    r = s.post(BASE + "/api/portfolio/optimize",
               json={"codes": ["600611", "000001", "600519"], "mode": "equal_weight", "capital": 500000},
               timeout=30)
    j = r.json()
    check("optimize", j.get("ok") is True, f"expected_return={j.get('expected_return')}, vol={j.get('expected_volatility')}")
except Exception as e:
    check("optimize", False, f"exception: {e}")

try:
    r = s.get(BASE + "/api/market/context?refresh=0", timeout=15)
    j = r.json()
    check("market_context", j.get("ok") is True, f"trend={j.get('trend')}, strength={j.get('strength')}")
except Exception as e:
    check("market_context", False, f"exception: {e}")

try:
    # 用 600519（本地缓存 272KB 最完整，规避 akshare 远程拉取偶发 RemoteDisconnected）
    r = s.get(BASE + "/api/kline/600519", timeout=15)
    j = r.json()
    bars = len(j.get("data", [])) if isinstance(j.get("data"), list) else 0
    check("kline_600519", j.get("ok") is True, f"bars={bars}, source={j.get('source')}")
except Exception as e:
    check("kline_600519", False, f"exception: {e}")

try:
    r = s.get(BASE + "/api/bridge/signals", timeout=5)
    check("bridge_signals_api", r.status_code in (200, 401), f"HTTP {r.status_code}")
except Exception as e:
    check("bridge_signals_api", False, f"exception: {e}")

try:
    r = s.get(BASE + "/api/strategies", timeout=10)
    j = r.json()
    if isinstance(j, list):
        strat_count = len(j)
    elif isinstance(j, dict):
        strat_count = len(j.get("strategies", j.get("data", [])))
    else:
        strat_count = 0
    check("strategies_list", strat_count > 0, f"count={strat_count}")
except Exception as e:
    check("strategies_list", False, f"exception: {e}")

print("\n=== summary ===")
total = len(results); passed = sum(1 for v in results.values() if v["ok"])
print(f"{passed}/{total} passed")
sys.exit(0 if passed == total else 2)
