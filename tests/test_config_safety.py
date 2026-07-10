# -*- coding: utf-8 -*-
"""交易安全默认值回归测试（2026-07-10 安全默认值改造）。

保护的不变量：
1. 什么都不配置 → 模拟盘（DRY_RUN=True），自动确认/自动撤单全关。
2. 双闸：QUANT_AUTO_CONFIRM_ORDER / QUANT_AUTO_CANCEL_UNFILLED 单独设置无效，
   必须与 QUANT_LIVE_TRADING=1 同时设置才生效。
3. QUANT_LIVE_TRADING=1 → 实盘（DRY_RUN=False）。
"""

from config import _resolve_trading_flags


class TestResolveTradingFlags:
    def test_empty_env_is_safe(self):
        live, confirm, cancel = _resolve_trading_flags({})
        assert live is False
        assert confirm is False
        assert cancel is False

    def test_auto_confirm_alone_is_ignored(self):
        """双闸：只设自动确认、不开实盘 → 仍然全关。"""
        live, confirm, cancel = _resolve_trading_flags({"QUANT_AUTO_CONFIRM_ORDER": "1"})
        assert live is False
        assert confirm is False

    def test_auto_cancel_alone_is_ignored(self):
        live, confirm, cancel = _resolve_trading_flags({"QUANT_AUTO_CANCEL_UNFILLED": "true"})
        assert live is False
        assert cancel is False

    def test_live_trading_enables(self):
        live, confirm, cancel = _resolve_trading_flags({"QUANT_LIVE_TRADING": "1"})
        assert live is True
        assert confirm is False  # 未显式开自动确认
        assert cancel is False

    def test_live_plus_confirm(self):
        env = {"QUANT_LIVE_TRADING": "1", "QUANT_AUTO_CONFIRM_ORDER": "1"}
        live, confirm, cancel = _resolve_trading_flags(env)
        assert (live, confirm, cancel) == (True, True, False)

    def test_full_enabled(self):
        env = {
            "QUANT_LIVE_TRADING": "yes",
            "QUANT_AUTO_CONFIRM_ORDER": "true",
            "QUANT_AUTO_CANCEL_UNFILLED": "on",
        }
        assert _resolve_trading_flags(env) == (True, True, True)

    def test_falsy_values_rejected(self):
        for v in ("0", "false", "no", "off", "", "  "):
            live, confirm, cancel = _resolve_trading_flags({"QUANT_LIVE_TRADING": v})
            assert live is False, f"值 {v!r} 不应开启实盘"

    def test_dry_run_is_inverse_of_live(self):
        """config 模块级不变量：DRY_RUN == not LIVE_TRADING。"""
        import config
        assert config.DRY_RUN == (not config.LIVE_TRADING)
