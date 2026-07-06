import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import _pathsetup  # noqa: F401

import daban_strategy_runtime as runtime


def test_new_quality_strategy_is_managed_by_daban_ride():
    assert runtime.is_daban_ride_strategy("daban_lianban_2plus_quality") is True
    assert runtime.is_daban_ride_strategy("lianban_2plus") is True
    assert runtime.is_daban_ride_strategy("da_ban") is True
    assert runtime.is_daban_ride_strategy("ma_cross") is False


def test_quality_strategy_uses_tighter_sell_parameters():
    params = runtime.ride_params_for_strategy("daban_lianban_2plus_quality")

    assert params == {
        "stop_loss": -0.05,
        "trail": 0.04,
        "max_hold": 7,
        "hold_limitup": True,
    }


def test_legacy_lianban_uses_same_two_three_board_sell_parameters():
    assert runtime.ride_params_for_strategy("lianban_2plus") == runtime.ride_params_for_strategy(
        "daban_lianban_2plus_quality"
    )


def test_default_daban_strategy_keeps_existing_global_parameters():
    params = runtime.ride_params_for_strategy("da_ban")

    assert params["stop_loss"] == runtime.DEFAULT_RIDE_PARAMS["stop_loss"]
    assert params["trail"] == runtime.DEFAULT_RIDE_PARAMS["trail"]
    assert params["max_hold"] == runtime.DEFAULT_RIDE_PARAMS["max_hold"]


def test_main_routes_daban_ride_checks_through_runtime_helper():
    source = (Path(__file__).parent.parent / "main.py").read_text(encoding="utf-8")

    assert "import daban_strategy_runtime as daban_runtime" in source
    assert "daban_runtime.is_daban_ride_strategy" in source
    assert "daban_runtime.ride_params_for_strategy" in source

    hardcoded_checks = [
        '(p.get("strategy") or "") == "da_ban"',
        's.get("active_strategy") == "da_ban"',
        '(active == "da_ban")',
    ]
    for check in hardcoded_checks:
        assert check not in source
