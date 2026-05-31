# -*- coding: utf-8 -*-
"""factors_zoo 框架自检（空 zoo 也必须通过）

跑法：
    cd D:\同花顺量化交易\ths_auto_trader
    python -m analytics.factors_zoo.smoke_test
"""
from __future__ import annotations

import sys
import traceback
import numpy as np
import pandas as pd


def _ok(name: str, cond: bool, detail: str = "") -> bool:
    print(f"[{'OK' if cond else 'FAIL'}] {name}: {detail}")
    return bool(cond)


def main() -> int:
    passed = 0
    total = 0

    # T1: 包能 import
    total += 1
    try:
        from analytics.factors_zoo import registry, build_panel, latest_score
        from analytics.factors_zoo import base as zoo_base
        from analytics.factors_zoo.registry import Registry, SkipAlpha
        passed += _ok("import_package", True, "OK")
    except Exception as e:
        _ok("import_package", False, f"{e}\n{traceback.format_exc()}")
        return 1

    # T2: registry 单例 + 空 zoo
    total += 1
    try:
        reg = registry()
        h = reg.health()
        passed += _ok(
            "empty_zoo_health",
            h["failed"] == 0 and isinstance(h["loaded"], int),
            f"loaded={h['loaded']} failed={h['failed']} zoos={h['zoos']}",
        )
    except Exception as e:
        _ok("empty_zoo_health", False, str(e))

    # T3: base 算子可调用（不依赖任何因子）
    total += 1
    try:
        idx = pd.date_range("2026-01-01", periods=10, freq="D")
        df = pd.DataFrame(
            np.random.RandomState(42).randn(10, 3),
            index=idx,
            columns=["600611", "000001", "600519"],
        )
        r_rank = zoo_base.rank(df)
        r_tsmean = zoo_base.ts_mean(df, 5)
        r_delta = zoo_base.delta(df, 1)
        ok = (
            r_rank.shape == df.shape
            and r_tsmean.shape == df.shape
            and r_delta.shape == df.shape
            and not np.isinf(r_rank.to_numpy()).any()
        )
        passed += _ok("base_ops_callable", ok, f"rank/ts_mean/delta shape OK")
    except Exception as e:
        _ok("base_ops_callable", False, str(e))

    # T4: panel_adapter.build_panel 从假 get_kline 拼面板
    total += 1
    try:
        def fake_get_kline(code: str, days: int) -> pd.DataFrame:
            idx = pd.date_range("2026-01-01", periods=days, freq="B")
            return pd.DataFrame(
                {
                    "open":   np.random.RandomState(hash(code) % 2**31).rand(days) * 10 + 5,
                    "high":   np.random.RandomState(hash(code) % 2**31).rand(days) * 10 + 6,
                    "low":    np.random.RandomState(hash(code) % 2**31).rand(days) * 10 + 4,
                    "close":  np.random.RandomState(hash(code) % 2**31).rand(days) * 10 + 5,
                    "volume": np.random.RandomState(hash(code) % 2**31).rand(days) * 10000,
                    "date":   idx,
                }
            )
        panel = build_panel(["600611", "000001"], fake_get_kline, days=30)
        ok = (
            set(panel.keys()) >= {"open", "high", "low", "close", "volume"}
            and panel["close"].shape == (30, 2)
            and list(panel["close"].columns) == ["600611", "000001"]
        )
        passed += _ok(
            "panel_adapter",
            ok,
            f"keys={sorted(panel.keys())} close.shape={panel['close'].shape}",
        )
    except Exception as e:
        _ok("panel_adapter", False, f"{e}\n{traceback.format_exc()}")

    # T5: latest_score —— 同时验证 per_column 和 last_row 两种 mode
    total += 1
    try:
        df = pd.DataFrame(
            {"600611": [0.1, 0.2, 0.3], "000001": [np.nan, 0.5, np.nan]},
            index=pd.date_range("2026-01-01", periods=3, freq="D"),
        )
        per_col = latest_score(df)                          # 每列最后一个非 NaN
        last_row = latest_score(df, mode="last_row")        # 严格最后一行
        ok = (
            per_col.get("600611") == 0.3
            and per_col.get("000001") == 0.5           # 跨过中间 NaN 取到第 1 行的 0.5
            and last_row.get("600611") == 0.3
            and last_row.get("000001") is None         # 最后一行确实是 NaN
        )
        passed += _ok(
            "latest_score",
            ok,
            f"per_col={per_col} last_row={last_row}",
        )
    except Exception as e:
        _ok("latest_score", False, str(e))

    # T6: registry.compute 在不存在的 alpha_id 时正确抛 KeyError
    total += 1
    try:
        from analytics.factors_zoo.registry import reset_default_registry
        reset_default_registry()  # 强制重新扫描（前面 T2 可能 cache 了空状态）
        reg2 = registry()
        try:
            reg2.compute("definitely_not_exist", {})
            passed += _ok("compute_unknown_id", False, "应该抛 KeyError 但没抛")
        except KeyError:
            passed += _ok("compute_unknown_id", True, "KeyError 已正确抛出")
    except Exception as e:
        _ok("compute_unknown_id", False, str(e))

    # T7: registry.list 能找到 gtja191_001（如果已搬进来）
    total += 1
    try:
        reg3 = registry()
        ids = reg3.list()
        has_gtja = "gtja191_001" in ids
        # 阶段 1 空 zoo: 列表空；阶段 2 后: 至少有 gtja191_001
        passed += _ok(
            "registry_lists_gtja191_001",
            has_gtja,
            f"loaded={reg3.health()['loaded']} found gtja191_001={has_gtja} all={ids[:5]}...",
        )
    except Exception as e:
        _ok("registry_lists_gtja191_001", False, str(e))

    # T8: 真实算 gtja191_001（用 wide panel 假数据）—— 需要 7+ 天 warmup
    total += 1
    try:
        rng = np.random.RandomState(123)
        ndays = 30
        codes = ["600611", "000001", "600519"]
        idx = pd.date_range("2026-01-01", periods=ndays, freq="B")
        # 注意：gtja_001 公式里 LOG(VOLUME)，volume 必须 > 0
        def _wide(low=5, hi=20):
            return pd.DataFrame(rng.rand(ndays, len(codes)) * (hi - low) + low,
                                index=idx, columns=codes)
        panel = {
            "open":   _wide(8, 12),
            "high":   _wide(10, 15),
            "low":    _wide(5, 8),
            "close":  _wide(8, 12),
            "volume": _wide(10000, 50000),  # 必 > 0
        }
        reg4 = registry()
        df = reg4.compute("gtja191_001", panel)
        # 验证：shape 一致 + 无 inf + 至少有一个非 NaN（前 6 天 warmup NaN 正常）
        arr = df.to_numpy(dtype=float, na_value=np.nan)
        has_finite = np.isfinite(arr).any()
        ok = df.shape == (ndays, len(codes)) and not np.isinf(arr).any() and has_finite
        nan_ratio = float(np.isnan(arr).mean())
        passed += _ok(
            "compute_gtja191_001",
            ok,
            f"shape={df.shape} nan_ratio={nan_ratio:.2f} has_finite={has_finite}",
        )
    except Exception as e:
        _ok("compute_gtja191_001", False, f"{e}\n{traceback.format_exc()}")

    print(f"\n=== smoke_test summary: {passed}/{total} ===")
    return 0 if passed == total else 2


if __name__ == "__main__":
    sys.exit(main())
