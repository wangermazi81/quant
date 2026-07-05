# -*- coding: utf-8 -*-
"""Runtime policy for strategies that should use the daban Ride lifecycle."""

try:
    from config import (DABAN_STOP_LOSS, DABAN_TRAIL,
                        DABAN_MAX_HOLD, DABAN_HOLD_LIMITUP)
except Exception:
    DABAN_STOP_LOSS, DABAN_TRAIL, DABAN_MAX_HOLD, DABAN_HOLD_LIMITUP = -0.05, 0.07, 10, True


DEFAULT_RIDE_PARAMS = {
    "stop_loss": DABAN_STOP_LOSS,
    "trail": DABAN_TRAIL,
    "max_hold": DABAN_MAX_HOLD,
    "hold_limitup": DABAN_HOLD_LIMITUP,
}


QUALITY_2PLUS = "daban_lianban_2plus_quality"

STRATEGY_RIDE_PARAMS = {
    "da_ban": DEFAULT_RIDE_PARAMS,
    QUALITY_2PLUS: {
        "stop_loss": -0.05,
        "trail": 0.04,
        "max_hold": 7,
        "hold_limitup": True,
    },
}


def is_daban_ride_strategy(strategy):
    return (strategy or "") in STRATEGY_RIDE_PARAMS


def ride_params_for_strategy(strategy):
    params = STRATEGY_RIDE_PARAMS.get(strategy or "", DEFAULT_RIDE_PARAMS)
    return dict(params)
