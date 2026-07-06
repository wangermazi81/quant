# META: {"name": "二三板接力情绪打板", "desc": "只做主板二/三板接力，使用T-1日线和实时盘口，过滤一字板、高开、缩量和过热区间。", "author": "Codex", "builtin": true, "position": {"max_daily": 2, "max_hold": 5, "sizing": "equal", "add_on": false}}
from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP


MIN_PREV_AMOUNT = 100_000_000
MIN_GAIN_20D = 0.30
MAX_GAIN_20D = 0.80
FALLBACK_MIN_GAIN_20D = 0.18
FALLBACK_MAX_GAIN_20D = 1.10
MAX_OPEN_PCT = 0.05
MAX_DAILY_BUYS = 2
BASE_POSITION_RATIO = 0.10
WEAK_POSITION_RATIO = 0.05
STRICT_MIN_CHANGE_PCT = 8.5
FALLBACK_MIN_CHANGE_PCT = 7.0

DABAN_STOP_LOSS = -0.05
DABAN_TRAIL = 0.04
DABAN_MAX_HOLD = 7


def _normalize_code(code):
    text = str(code or "").strip()
    if "." in text:
        text = text.split(".")[-1]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def _is_mainboard(code):
    code = _normalize_code(code)
    return len(code) == 6 and code.startswith(("60", "000", "001", "002", "003"))


def _board_rate(code):
    return 0.10


def _round_price(value):
    try:
        return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except Exception:
        return 0.0


def _limit_up_price(prev_close, code):
    prev_close = _to_float(prev_close)
    if prev_close <= 0:
        return 0.0
    return _round_price(prev_close * (1 + _board_rate(code)))


def _to_float(value, default=0.0):
    try:
        if value is None:
            return default
        if isinstance(value, str):
            value = value.strip().replace(",", "")
            if value in ("", "-", "--", "None", "nan"):
                return default
        num = float(value)
        return num if math.isfinite(num) else default
    except Exception:
        return default


def _as_pct(value):
    pct = _to_float(value)
    if abs(pct) <= 1.0 and pct != 0:
        return pct * 100
    return pct


def _quote_value(q, *names, **kwargs):
    default = kwargs.get("default")
    if q is None:
        return default
    for name in names:
        try:
            value = q.get(name, default)
        except Exception:
            value = default
        if value is not default and value is not None:
            return value
    return default


def _is_limit_close(close, prev_close, code):
    close = _to_float(close)
    prev_close = _to_float(prev_close)
    if close <= 0 or prev_close <= 0:
        return False
    limit_px = _limit_up_price(prev_close, code)
    pct = close / prev_close - 1
    return close >= limit_px - 0.03 or pct >= _board_rate(code) * 0.95


def _sort_kline(kline):
    if kline is None or getattr(kline, "empty", False):
        return None
    try:
        df = kline.copy()
        if "date" in df.columns:
            df = df.sort_values("date")
        return df.reset_index(drop=True)
    except Exception:
        return None


def _count_consec_limit_up(kline, code):
    df = _sort_kline(kline)
    if df is None or len(df) < 2 or "close" not in df.columns:
        return 0
    closes = [_to_float(v) for v in df["close"].tolist()]
    count = 0
    for idx in range(len(closes) - 1, 0, -1):
        if _is_limit_close(closes[idx], closes[idx - 1], code):
            count += 1
        else:
            break
    return count


def _row_amount(row, close):
    amount = _quote_value(row, "amount", "成交额", default=None)
    if amount is not None:
        return _to_float(amount)
    volume = _quote_value(row, "volume", "vol", "成交量", default=0)
    return _to_float(close) * _to_float(volume)


def _daily_features(kline, code):
    code = _normalize_code(code)
    df = _sort_kline(kline)
    if df is None or len(df) < 22 or "close" not in df.columns:
        return None
    last = df.iloc[-1]
    base = df.iloc[-21]
    last_close = _to_float(last.get("close"))
    base_close = _to_float(base.get("close"))
    if last_close <= 0 or base_close <= 0:
        return None

    yesterday_consec = _count_consec_limit_up(df, code)
    prev_amount = _row_amount(last, last_close)
    gain_20d = last_close / base_close - 1
    return {
        "code": code,
        "last_close": last_close,
        "yesterday_consec": yesterday_consec,
        "target_consec": yesterday_consec + 1,
        "gain_20d": gain_20d,
        "prev_amount": prev_amount,
        "limit_up": _limit_up_price(last_close, code),
    }


def _records_from_quotes(quotes):
    if quotes is None:
        return []
    if isinstance(quotes, dict):
        return list(quotes.values())
    try:
        if getattr(quotes, "empty", False):
            return []
        return quotes.to_dict("records")
    except Exception:
        pass
    try:
        return list(quotes)
    except Exception:
        return []


def _quote_map(quotes):
    mapped = {}
    for q in _records_from_quotes(quotes):
        code = _normalize_code(_quote_value(q, "code", "代码", "证券代码", default=""))
        if code:
            mapped[code] = q
    return mapped


def _is_bad_name_or_news(name, quote):
    name_text = str(name or "").upper()
    if "ST" in name_text or "退" in name_text:
        return True
    for key in ("negative_news", "bad_news", "重大利空", "风险提示", "risk_flags"):
        value = _quote_value(quote, key, default=None)
        if value in (True, 1, "1"):
            return True
        text = str(value or "")
        if any(word in text for word in ("立案", "退市", "减持", "处罚", "监管函", "业绩预亏")):
            return True
    return False


def _fallback_seal_status(quote, price, limit_up):
    bids = _quote_value(quote, "bids", "买盘", default=[]) or []
    asks = _quote_value(quote, "asks", "卖盘", default=[]) or []
    bid1_vol = 0
    try:
        bid1 = bids[0] if bids else {}
        bid1_price = _to_float(_quote_value(bid1, "price", "价格", default=0))
        if bid1_price >= limit_up - 0.02:
            bid1_vol = _to_float(_quote_value(bid1, "vol", "volume", "数量", default=0))
    except Exception:
        bid1_vol = 0
    current_sealed = price >= limit_up - 0.02 and not asks
    return {
        "current_sealed": current_sealed,
        "open_count": int(_to_float(_quote_value(quote, "open_count", "开板次数", default=0))),
        "score": 55 if current_sealed and bid1_vol > 0 else 40,
        "current_seal": bid1_vol * 100 * limit_up,
    }


def _tracker_status(code):
    try:
        from analytics.seal_tracker import get_tracker
        return get_tracker().status(code) or {}
    except Exception:
        return {}


def _score_candidate(features, quote, seal_status, emotion=None):
    score = 42 if features["target_consec"] == 3 else 38
    gain_pct = features["gain_20d"] * 100
    score += max(0.0, 18.0 - abs(gain_pct - 45.0) * 0.35)

    amount_ratio = max(features["prev_amount"] / MIN_PREV_AMOUNT, 1.0)
    score += min(12.0, 8.0 + math.log10(amount_ratio) * 6.0)

    open_pct = quote.get("_open_pct")
    if open_pct is None:
        score -= 2.0
    elif open_pct <= 0.03:
        score += 8.0
    else:
        score += 3.0

    open_count = int(_to_float(seal_status.get("open_count", 0)))
    if open_count == 1:
        score -= 8.0
    if not seal_status.get("current_sealed", False):
        score -= 4.0

    seal_score = _to_float(seal_status.get("score", 50), 50)
    if seal_score >= 85:
        score -= 8.0
    elif seal_score <= 45:
        score += 3.0

    if _emotion_is_weak(emotion):
        score -= 6.0
    return round(score, 2)


def _emotion_is_weak(emotion):
    text = str(emotion or "").lower()
    return any(word in text for word in ("weak", "cold", "冰点", "退潮", "弱"))


def _build_signal(code, quote, features, seal_status=None, emotion=None, relaxed=False):
    code = _normalize_code(code)
    name = _quote_value(quote, "name", "名称", "股票名称", default=code)
    if not _is_mainboard(code) or _is_bad_name_or_news(name, quote):
        return None
    if features is None:
        return None
    if features["yesterday_consec"] not in (1, 2):
        return None
    min_gain = FALLBACK_MIN_GAIN_20D if relaxed else MIN_GAIN_20D
    max_gain = FALLBACK_MAX_GAIN_20D if relaxed else MAX_GAIN_20D
    if not (min_gain <= features["gain_20d"] <= max_gain):
        return None
    if features["prev_amount"] < MIN_PREV_AMOUNT:
        return None

    last_close = _to_float(_quote_value(quote, "last_close", "pre_close", "昨收", default=features["last_close"]))
    if last_close <= 0:
        last_close = features["last_close"]
    limit_up = _to_float(_quote_value(quote, "limit_up", "涨停价", default=0)) or _limit_up_price(last_close, code)
    price = _to_float(_quote_value(quote, "price", "最新价", "现价", default=0))
    if price <= 0:
        return None
    if not (3 <= min(price, limit_up) <= 50):
        return None

    open_price = _to_float(_quote_value(quote, "open", "今开", "开盘", default=0))
    open_pct = None
    if open_price > 0 and last_close > 0:
        open_pct = open_price / last_close - 1
        if open_pct > MAX_OPEN_PCT:
            return None

    low = _to_float(_quote_value(quote, "low", "最低", "最低价", default=0))
    if low > 0 and open_price >= limit_up - 0.02 and low >= limit_up - 0.02:
        return None

    change_pct = _as_pct(_quote_value(quote, "change_pct", "涨跌幅", default=0))
    near_limit = price >= limit_up - max(0.02, limit_up * 0.0015)
    min_change_pct = FALLBACK_MIN_CHANGE_PCT if relaxed else STRICT_MIN_CHANGE_PCT
    if change_pct < min_change_pct and not near_limit:
        return None

    local_quote = dict(quote)
    local_quote["_open_pct"] = open_pct
    status = dict(seal_status or {})
    if not status:
        status = _tracker_status(code)
    if not status:
        status = _fallback_seal_status(local_quote, price, limit_up)
    if int(_to_float(status.get("open_count", 0))) >= 2:
        return None

    quote_emotion = _quote_value(quote, "emotion", "market_emotion", "市场情绪", default=emotion)
    score = _score_candidate(features, local_quote, status, quote_emotion)
    position_ratio = WEAK_POSITION_RATIO if relaxed or _emotion_is_weak(quote_emotion) else BASE_POSITION_RATIO
    consec = features["target_consec"]
    label = "%d板接力" % consec
    open_note = "开盘%.1f%%" % (open_pct * 100) if open_pct is not None else "开盘缺失"
    reason = (
        "%s | 20日涨幅%.1f%% | T-1成交额%.1f亿 | %s | 止损-5%%/回撤4%%/最长7天"
        % (label, features["gain_20d"] * 100, features["prev_amount"] / 100_000_000, open_note)
    )
    if relaxed:
        reason = reason + " | fallback conservative"
    return {
        "code": code,
        "name": str(name or code),
        "price": round(price, 2),
        "change": round(change_pct, 2),
        "score": score,
        "signal": True,
        "consec_limit": consec,
        "consec_label": label,
        "position_ratio": position_ratio,
        "reason": reason,
        "sell_plan": {
            "stop_loss": DABAN_STOP_LOSS,
            "trail": DABAN_TRAIL,
            "max_hold_days": DABAN_MAX_HOLD,
            "hold_if_limit_up": True,
        },
    }


def cancel_decision(status):
    status = status or {}
    open_count = int(_to_float(status.get("open_count", 0)))
    if open_count >= 2:
        return {"cancel": True, "reason": "开板次数>=2，撤单避免炸板回封失败"}
    if open_count >= 1 and status.get("current_sealed") is False:
        return {"cancel": True, "reason": "开板后未回封，撤单"}
    if _to_float(status.get("score", 0)) >= 90:
        return {"cancel": True, "reason": "封单过强，成交概率过低"}
    return {"cancel": False, "reason": "盘口未触发撤单"}


def scan(codes, get_kline, get_quotes):
    if not codes:
        return []
    norm_codes = [_normalize_code(code) for code in codes]
    try:
        quotes = get_quotes(norm_codes)
    except Exception:
        quotes = None
    qmap = _quote_map(quotes)

    results = []
    fallback_results = []
    for code in norm_codes:
        if not _is_mainboard(code):
            continue
        quote = qmap.get(code)
        if not quote:
            continue
        try:
            kline = get_kline(code, 60)
            features = _daily_features(kline, code)
            signal = _build_signal(code, quote, features)
            if signal:
                results.append(signal)
            else:
                fallback = _build_signal(code, quote, features, relaxed=True)
                if fallback:
                    fallback_results.append(fallback)
        except Exception:
            continue

    if not results:
        results = fallback_results
    results.sort(key=lambda item: (-item.get("score", 0), -item.get("consec_limit", 0), item.get("code", "")))
    return results[:MAX_DAILY_BUYS]
