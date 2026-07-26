# -*- coding: utf-8 -*-
"""A-12：资金开关不得绕过 config 直读 env；env 访问器的取值语义。

`.env` 只在 **import config** 时被加载（config._load_dotenv）。任何模块若直接读
os.environ 而调用链上没人先 import config，.env 里的值就静默读不到，落到调用点
自己的默认值。对 QUANT_LIVE_TRADING 这类开关来说，这意味着「以为关着其实开着」
或反过来 —— 直接对应真实资金动作。

所以这里不做「把 25 个模块的 env 读取全改一遍」那种大扫除（多数是缓存 TTL
之类的调优旋钮，读不到就用默认值，无害），而是把**危险面**锁死：
资金开关只能有 config.py 一个读取者。
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config

ROOT = Path(__file__).resolve().parent.parent

# 直接决定真实资金动作的开关，只允许 config.py 读
MONEY_ENV_VARS = [
    "QUANT_LIVE_TRADING",
    "QUANT_AUTO_CONFIRM_ORDER",
    "QUANT_AUTO_CANCEL_UNFILLED",
]

# 只扫生产代码；测试/工具/示例可以为了构造场景直接设读环境变量
_SKIP_DIRS = {"tests", "tools", "examples", "backups", ".worktrees", "docs",
              "__pycache__", "strategies", "factors", "deploy"}


def _production_py_files():
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        if rel.name in {"config.py"}:          # 唯一允许的读取者
            continue
        yield path


@pytest.mark.parametrize("var", MONEY_ENV_VARS)
def test_money_switches_are_only_read_by_config(var):
    """★资金开关只能由 config.py 读取。

    别的模块要用请 `from config import DRY_RUN, AUTO_CONFIRM_ORDER, ...`，
    那些常量是 config._resolve_trading_flags 的双闸产物；自己去读 env
    等于绕过双闸，而且在没 import config 的进程里根本读不到 .env。
    """
    pattern = re.compile(
        r"""(?:os\.environ|environ)(?:\.get)?\s*[\(\[]\s*["']""" + re.escape(var))
    offenders = []
    for path in _production_py_files():
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        if pattern.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], (
        f"{var} 只能由 config.py 读取，以下模块绕过了双闸：{offenders}"
    )


class TestEnvAccessors:
    @pytest.mark.parametrize("raw,expected", [
        ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
        ("0", False), ("false", False), ("no", False), ("", False), ("随便", False),
    ])
    def test_env_bool(self, monkeypatch, raw, expected):
        monkeypatch.setenv("JW_TEST_FLAG", raw)
        assert config.env_bool("JW_TEST_FLAG") is expected

    def test_env_bool_missing_uses_default(self, monkeypatch):
        monkeypatch.delenv("JW_TEST_FLAG", raising=False)
        assert config.env_bool("JW_TEST_FLAG") is False
        assert config.env_bool("JW_TEST_FLAG", True) is True

    def test_env_bool_empty_uses_default(self, monkeypatch):
        """空串要当「没设」处理，否则 `FOO=` 会把默认 True 变成 False。"""
        monkeypatch.setenv("JW_TEST_FLAG", "")
        assert config.env_bool("JW_TEST_FLAG", True) is True

    def test_env_int_and_float(self, monkeypatch):
        monkeypatch.setenv("JW_TEST_N", " 42 ")
        assert config.env_int("JW_TEST_N", 7) == 42
        monkeypatch.setenv("JW_TEST_N", "abc")
        assert config.env_int("JW_TEST_N", 7) == 7, "非法值必须回落默认而不是抛错"
        monkeypatch.setenv("JW_TEST_F", "1.5")
        assert config.env_float("JW_TEST_F", 0.0) == 1.5
        monkeypatch.setenv("JW_TEST_F", "")
        assert config.env_float("JW_TEST_F", 2.5) == 2.5

    def test_env_str_strips(self, monkeypatch):
        monkeypatch.setenv("JW_TEST_S", "  hello  ")
        assert config.env_str("JW_TEST_S") == "hello"
        monkeypatch.delenv("JW_TEST_S", raising=False)
        assert config.env_str("JW_TEST_S", "fallback") == "fallback"


def test_trading_flags_double_gate_still_holds():
    """双闸：没开实盘总开关时，自动确认/自动撤单一律为 False。"""
    live, confirm, cancel = config._resolve_trading_flags({
        "QUANT_AUTO_CONFIRM_ORDER": "1",
        "QUANT_AUTO_CANCEL_UNFILLED": "1",
    })
    assert (live, confirm, cancel) == (False, False, False)

    live, confirm, cancel = config._resolve_trading_flags({
        "QUANT_LIVE_TRADING": "1",
        "QUANT_AUTO_CONFIRM_ORDER": "1",
    })
    assert (live, confirm, cancel) == (True, True, False)
