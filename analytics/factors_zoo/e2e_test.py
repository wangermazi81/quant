# -*- coding: utf-8 -*-
"""端到端真数据测试 —— factors_zoo + 项目内 market_data.get_kline + gtja191_001

跑法：
    cd D:\同花顺量化交易\ths_auto_trader
    python -m analytics.factors_zoo.e2e_test
"""
from __future__ import annotations

import os
import sys
import time
import traceback

# 先清掉本机的 HTTP_PROXY/HTTPS_PROXY（你 main.py 也是这么做的，
# 避免 v2ray/clash 拦截 akshare 请求）
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)

import numpy as np
import pandas as pd


def _ok(name: str, cond: bool, detail: str = "") -> bool:
    print(f"[{'OK' if cond else 'FAIL'}] {name}: {detail}")
    return bool(cond)


def main() -> int:
    passed = 0
    total = 0
    # 优先用本地缓存有的股票（避开 akshare 网络拉取的不稳定性）
    # data/daily/ 已缓存: 000001 / 000065 / 000066 / 002031 / 002033 / 600519
    CODES = ["600519", "000001", "000065", "000066", "002031"]
    DAYS = 60

    # 1. 项目内的 get_kline 接口
    total += 1
    try:
        from market.market_data import get_kline
        sample = get_kline(CODES[0], 30)
        passed += _ok(
            "import_market_data.get_kline",
            isinstance(sample, pd.DataFrame) and len(sample) > 0,
            f"sample_rows={len(sample) if isinstance(sample, pd.DataFrame) else 'N/A'}",
        )
    except Exception as e:
        _ok("import_market_data.get_kline", False, f"{e}\n{traceback.format_exc()}")
        return 1

    # 2. build_panel 多股拼接
    total += 1
    try:
        from analytics.factors_zoo import build_panel
        t0 = time.time()
        panel = build_panel(CODES, get_kline, days=DAYS)
        dt = time.time() - t0
        keys = set(panel.keys())
        ok = (
            keys >= {"open", "high", "low", "close", "volume"}
            and panel["close"].shape[1] >= 4  # 至少 4 只股票
            and panel["close"].shape[0] >= 30  # 至少 30 天 K 线
        )
        passed += _ok(
            "build_panel_real",
            ok,
            f"keys={sorted(keys)} close.shape={panel['close'].shape} 耗时={dt:.2f}s",
        )
    except Exception as e:
        _ok("build_panel_real", False, f"{e}\n{traceback.format_exc()}")
        return 1

    # 3. 真实数据样本验证（close 范围在合理价格区间）
    total += 1
    try:
        close = panel["close"]
        # 每只股票各自的最后一个非 NaN 收盘（容忍不同股票日期不齐）
        per_stock_last = {c: close[c].dropna().iloc[-1] for c in close.columns if close[c].dropna().size > 0}
        prices_ok = all(0 < v < 5000 for v in per_stock_last.values())
        passed += _ok(
            "data_sanity",
            prices_ok and len(per_stock_last) >= 4,
            f"per_stock_last={ {k: round(v,2) for k,v in per_stock_last.items()} }",
        )
    except Exception as e:
        _ok("data_sanity", False, str(e))

    # 4. registry.compute('gtja191_001') 真实数据
    total += 1
    try:
        from analytics.factors_zoo import registry
        reg = registry()
        ids = reg.list()
        if "gtja191_001" not in ids:
            _ok("compute_gtja191_001_real", False, f"factor not registered, ids={ids}")
            return 2
        t0 = time.time()
        df = reg.compute("gtja191_001", panel)
        dt = time.time() - t0
        arr = df.to_numpy(dtype=np.float64, na_value=np.nan)
        nan_ratio = float(np.isnan(arr).mean())
        has_finite = np.isfinite(arr).any()
        no_inf = not np.isinf(arr).any()
        ok = df.shape == panel["close"].shape and no_inf and has_finite and nan_ratio < 0.95
        passed += _ok(
            "compute_gtja191_001_real",
            ok,
            f"shape={df.shape} nan_ratio={nan_ratio:.2f} 耗时={dt:.3f}s",
        )
    except Exception as e:
        _ok("compute_gtja191_001_real", False, f"{e}\n{traceback.format_exc()}")
        return 2

    # 5. latest_score 输出
    total += 1
    try:
        from analytics.factors_zoo import latest_score
        scores = latest_score(df)
        # 至少要有一只非 None（部分股票 warmup 不够会是 None）
        non_null = {k: v for k, v in scores.items() if v is not None}
        ok = len(non_null) >= 1
        passed += _ok(
            "latest_score_real",
            ok,
            f"scores={scores}",
        )
    except Exception as e:
        _ok("latest_score_real", False, str(e))

    print(f"\n=== e2e_test summary: {passed}/{total} ===")
    return 0 if passed == total else 2


if __name__ == "__main__":
    sys.exit(main())
