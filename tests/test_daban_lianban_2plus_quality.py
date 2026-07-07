import pandas as pd

from strategies import daban_lianban_2plus_quality as strat


def make_kline(yesterday_consec=1, gain_20d=0.45, prev_amount=200_000_000):
    base = 10.0
    closes = [base] * 25
    closes[-21] = base
    last_close = round(base * (1 + gain_20d), 2)

    if yesterday_consec == 1:
        closes[-2] = round(last_close / 1.10, 2)
        closes[-1] = last_close
        closes[-3] = round(closes[-2] / 1.04, 2)
    elif yesterday_consec == 2:
        closes[-3] = round(last_close / 1.21, 2)
        closes[-2] = round(closes[-3] * 1.10, 2)
        closes[-1] = last_close
        closes[-4] = round(closes[-3] / 1.04, 2)
    elif yesterday_consec == 3:
        closes[-4] = round(last_close / 1.331, 2)
        closes[-3] = round(closes[-4] * 1.10, 2)
        closes[-2] = round(closes[-3] * 1.10, 2)
        closes[-1] = last_close
        closes[-5] = round(closes[-4] / 1.04, 2)
    else:
        closes[-2] = round(last_close * 0.98, 2)
        closes[-1] = last_close

    volume = [1_000_000] * 25
    volume[-1] = max(1, int(prev_amount / last_close))
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=25),
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": volume,
            "amount": [close * vol for close, vol in zip(closes, volume)],
        }
    )


def quote(code, price=15.95, last_close=14.5, open_price=15.0, change_pct=10.0, name="test"):
    return {
        "code": code,
        "name": name,
        "price": price,
        "last_close": last_close,
        "open": open_price,
        "change_pct": change_pct,
        "low": 14.8,
        "limit_up": round(last_close * 1.10, 2),
        "bids": [{"price": round(last_close * 1.10, 2), "vol": 12000}],
        "asks": [],
    }


def quote_cn(code, price=15.95, last_close=14.5, open_price=15.0, change_pct=10.0, name="test"):
    return {
        "代码": code,
        "名称": name,
        "最新价": price,
        "昨收": last_close,
        "open": open_price,
        "low": 14.8,
        "limit_up": round(last_close * 1.10, 2),
        "涨跌幅": change_pct,
        "bids": [{"price": round(last_close * 1.10, 2), "vol": 12000}],
        "asks": [],
    }


def quote_real_cn(code, price=15.95, last_close=14.5, open_price=15.0, change_pct=10.0, name="test"):
    return {
        "代码": code,
        "名称": name,
        "最新价": price,
        "昨收": last_close,
        "open": open_price,
        "low": 14.8,
        "limit_up": round(last_close * 1.10, 2),
        "涨跌幅": change_pct,
        "bids": [{"price": round(last_close * 1.10, 2), "vol": 12000}],
        "asks": [],
    }


def run_scan(cases):
    codes = list(cases)

    def get_kline(code, days=60):
        return cases[code]["kline"]

    def get_quotes(requested):
        return pd.DataFrame([cases[code]["quote"] for code in requested])

    return strat.scan(codes, get_kline, get_quotes)


def test_daily_features_promotes_yesterday_first_board_to_target_second_board():
    features = strat._daily_features(make_kline(yesterday_consec=1), "000001")

    assert features["yesterday_consec"] == 1
    assert features["target_consec"] == 2
    assert 0.30 <= features["gain_20d"] <= 0.80
    assert features["prev_amount"] >= strat.MIN_PREV_AMOUNT


def test_scan_filters_future_sensitive_and_risky_candidates():
    cases = {
        "000001": {"kline": make_kline(1), "quote": quote("000001", name="ok")},
        "300001": {"kline": make_kline(1), "quote": quote("300001", name="no-mainboard")},
        "000002": {"kline": make_kline(3), "quote": quote("000002", name="fourth-board")},
        "000003": {"kline": make_kline(1, gain_20d=0.20), "quote": quote("000003", name="cold")},
        "000004": {"kline": make_kline(1, prev_amount=50_000_000), "quote": quote("000004", name="thin")},
        "000005": {"kline": make_kline(1), "quote": quote("000005", open_price=15.4, name="high-open")},
    }

    results = run_scan(cases)

    assert [item["code"] for item in results] == ["000001"]
    assert results[0]["consec_limit"] == 2
    assert results[0]["position_ratio"] == strat.BASE_POSITION_RATIO


def test_scan_scores_and_caps_to_top_two_candidates():
    cases = {
        "000001": {"kline": make_kline(1, gain_20d=0.45), "quote": quote("000001", name="second")},
        "000002": {"kline": make_kline(2, gain_20d=0.45), "quote": quote("000002", name="third")},
        "600001": {"kline": make_kline(1, gain_20d=0.78), "quote": quote("600001", name="edge")},
    }

    results = run_scan(cases)

    assert len(results) == 2
    assert results[0]["code"] == "000002"
    assert results[0]["consec_limit"] == 3


def test_scan_does_not_use_same_day_amount_to_pass_liquidity_filter():
    cases = {
        "000001": {
            "kline": make_kline(1, prev_amount=50_000_000),
            "quote": {**quote("000001"), "amount": 5_000_000_000, "volume": 400_000_000},
        }
    }

    assert run_scan(cases) == []


def test_scan_returns_conservative_fallback_when_strict_pool_is_empty():
    cases = {
        "000006": {
            "kline": make_kline(1, gain_20d=0.24, prev_amount=200_000_000),
            "quote": quote("000006", name="fallback"),
        }
    }

    results = run_scan(cases)

    assert len(results) == 1
    assert results[0]["code"] == "000006"
    assert results[0]["consec_limit"] == 2
    assert results[0]["position_ratio"] == strat.WEAK_POSITION_RATIO
    assert "fallback" in results[0]["reason"]


def test_scan_fallback_accepts_near_limit_relay_with_flat_20d_gain():
    cases = {
        "000011": {
            "kline": make_kline(1, gain_20d=-0.02, prev_amount=300_000_000),
            "quote": quote("000011", price=15.94, last_close=14.5, open_price=14.6, change_pct=9.9, name="flat-relay"),
        }
    }

    results = run_scan(cases)

    assert len(results) == 1
    assert results[0]["code"] == "000011"
    assert results[0]["position_ratio"] == strat.WEAK_POSITION_RATIO


def test_scan_fallback_allows_hot_relay_price_up_to_80():
    cases = {
        "000012": {
            "kline": make_kline(1, gain_20d=1.20, prev_amount=300_000_000),
            "quote": quote("000012", price=71.0, last_close=65.86, open_price=64.0, change_pct=7.8, name="hot-relay"),
        }
    }

    results = run_scan(cases)

    assert len(results) == 1
    assert results[0]["code"] == "000012"
    assert results[0]["position_ratio"] == strat.WEAK_POSITION_RATIO


def test_scan_reads_chinese_quote_columns_from_market_dataframe():
    cases = {
        "000007": {
            "kline": make_kline(1, gain_20d=0.45, prev_amount=200_000_000),
            "quote": quote_cn("000007", name="cn-fields"),
        }
    }

    results = run_scan(cases)

    assert len(results) == 1
    assert results[0]["code"] == "000007"
    assert results[0]["price"] == 15.95
    assert results[0]["change"] == 10.0


def test_scan_reads_real_chinese_quote_columns_from_market_dataframe():
    cases = {
        "000008": {
            "kline": make_kline(1, gain_20d=0.45, prev_amount=200_000_000),
            "quote": quote_real_cn("000008", name="real-cn-fields"),
        }
    }

    results = run_scan(cases)

    assert len(results) == 1
    assert results[0]["code"] == "000008"
    assert results[0]["price"] == 15.95
    assert results[0]["change"] == 10.0


def test_daily_features_ignore_current_trading_day_bar():
    today = pd.Timestamp.today().normalize()
    dates = pd.date_range(today - pd.Timedelta(days=25), periods=26)
    closes = [10.0] * 26
    closes[-4] = 13.00
    closes[-3] = 13.18
    closes[-2] = 14.50  # T-1 first board.
    closes[-1] = 14.20  # Current incomplete daily bar must not reset the streak.
    df = pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1_000_000] * 26,
            "amount": [200_000_000] * 26,
        }
    )
    cases = {
        "000009": {
            "kline": df,
            "quote": quote("000009", price=15.95, last_close=14.5, open_price=15.0, change_pct=10.0, name="intraday"),
        }
    }

    results = run_scan(cases)

    assert len(results) == 1
    assert results[0]["code"] == "000009"
    assert results[0]["consec_limit"] == 2


def test_daily_features_treats_kline_volume_as_hands_when_amount_missing():
    row = {"volume": 200_000}

    assert strat._row_amount(row, close=10.0) == 200_000_000


def test_scan_does_not_multiply_sub_one_percent_quote_change():
    cases = {
        "000010": {
            "kline": make_kline(1, gain_20d=0.45, prev_amount=200_000_000),
            "quote": quote("000010", price=15.0, last_close=14.5, open_price=14.6, change_pct=0.45, name="small-rise"),
        }
    }

    assert run_scan(cases) == []


def test_cancel_decision_cancels_after_repeated_board_opening():
    decision = strat.cancel_decision({"open_count": 2, "current_sealed": False})

    assert decision["cancel"] is True
    assert "开板" in decision["reason"]
