# -*- coding: utf-8 -*-
"""
投资组合优化器
支持4种模式: equal_weight / max_sharpe / min_variance / risk_parity
"""
import logging
import numpy as np
import pandas as pd
from pathlib import Path

logger = logging.getLogger(__name__)
DATA_DIR = Path(__file__).parent.parent / "data" / "daily"


def _load_returns(codes: list, lookback: int = 60) -> pd.DataFrame:
    """加载各股票日收益率矩阵，返回 DataFrame(date × code)。"""
    frames = {}
    for code in codes:
        csv = DATA_DIR / f"{code}.csv"
        if not csv.exists():
            continue
        try:
            df = pd.read_csv(csv, parse_dates=["date"])
            df = df.sort_values("date").tail(lookback + 1)
            if len(df) < 10:
                continue
            frames[code] = df.set_index("date")["close"].pct_change().dropna()
        except Exception as e:
            logger.warning(f"[portfolio] 读取 {code} 失败: {e}")
    if not frames:
        return pd.DataFrame()
    ret = pd.DataFrame(frames).dropna(how="all")
    return ret.fillna(0)


def _equal_weight(codes: list) -> dict:
    n = len(codes)
    w = 1.0 / n if n > 0 else 0
    return {c: round(w, 4) for c in codes}


def _max_sharpe(ret: pd.DataFrame, rf: float = 0.025 / 252) -> dict:
    """最大夏普比率（均值-方差前沿）。"""
    try:
        from scipy.optimize import minimize

        mu  = ret.mean().values
        cov = ret.cov().values
        n   = len(mu)

        def neg_sharpe(w):
            port_ret = np.dot(w, mu)
            port_vol = np.sqrt(w @ cov @ w + 1e-10)
            return -(port_ret - rf) / port_vol

        constraints = [{"type": "eq", "fun": lambda w: np.sum(w) - 1}]
        bounds      = [(0.02, 0.40)] * n
        w0          = np.ones(n) / n
        res         = minimize(neg_sharpe, w0, bounds=bounds, constraints=constraints,
                               method="SLSQP", options={"maxiter": 500})
        w = res.x if res.success else w0
    except Exception:
        n = len(ret.columns)
        w = np.ones(n) / n
    w = np.clip(w, 0, 1)
    w = w / w.sum()
    return {c: round(float(w[i]), 4) for i, c in enumerate(ret.columns)}


def _min_variance(ret: pd.DataFrame) -> dict:
    """最小方差组合。"""
    try:
        from scipy.optimize import minimize

        cov = ret.cov().values
        n   = len(cov)

        def port_var(w):
            return w @ cov @ w

        constraints = [{"type": "eq", "fun": lambda w: np.sum(w) - 1}]
        bounds      = [(0.02, 0.40)] * n
        w0          = np.ones(n) / n
        res         = minimize(port_var, w0, bounds=bounds, constraints=constraints,
                               method="SLSQP", options={"maxiter": 500})
        w = res.x if res.success else w0
    except Exception:
        n = len(ret.columns)
        w = np.ones(n) / n
    w = np.clip(w, 0, 1)
    w = w / w.sum()
    return {c: round(float(w[i]), 4) for i, c in enumerate(ret.columns)}


def _risk_parity(ret: pd.DataFrame) -> dict:
    """风险平价：每只股票贡献相等的风险。"""
    try:
        from scipy.optimize import minimize

        cov = ret.cov().values
        n   = len(cov)
        target = np.ones(n) / n

        def risk_budget_obj(w):
            sigma = np.sqrt(w @ cov @ w + 1e-10)
            mrc   = (cov @ w) / sigma
            rc    = w * mrc
            rc    = rc / (rc.sum() + 1e-10)
            return np.sum((rc - target) ** 2)

        constraints = [{"type": "eq", "fun": lambda w: np.sum(w) - 1}]
        bounds      = [(0.02, 0.40)] * n
        w0          = np.ones(n) / n
        res         = minimize(risk_budget_obj, w0, bounds=bounds, constraints=constraints,
                               method="SLSQP", options={"maxiter": 1000})
        w = res.x if res.success else w0
    except Exception:
        n = len(ret.columns)
        w = np.ones(n) / n
    w = np.clip(w, 0, 1)
    w = w / w.sum()
    return {c: round(float(w[i]), 4) for i, c in enumerate(ret.columns)}


def optimize_portfolio(codes: list, mode: str = "equal_weight",
                       capital: float = 500_000, lookback: int = 60) -> dict:
    """
    投资组合优化入口。
    返回: {ok, weights, positions, mode, expected_return, expected_volatility}
    """
    # Run Card 快照：每次调用产出 logs/runs/<时间戳>-portfolio_optimization/
    try:
        from run_card import RunCard
        _card = RunCard.start("portfolio_optimization", inputs={
            "codes": codes, "mode": mode, "capital": capital, "lookback": lookback,
        })
    except Exception:
        _card = None

    def _emit(result: dict) -> dict:
        if _card is not None:
            try:
                _card.finish(outputs=result,
                             status="ok" if result.get("ok") else "error")
            except Exception:
                pass
        return result

    if not codes:
        return _emit({"ok": False, "error": "codes 不能为空"})

    # 不管哪种模式都尝试加载历史收益率，以便统一计算预期收益/波动率
    ret = _load_returns(codes, lookback=lookback)

    if mode == "equal_weight":
        weights = _equal_weight(codes)
    else:
        valid_codes = list(ret.columns) if not ret.empty else []
        if len(valid_codes) < 2:
            # 数据不足，降级为等权
            weights = _equal_weight(codes)
        else:
            ret = ret[valid_codes]
            if mode == "max_sharpe":
                weights = _max_sharpe(ret)
            elif mode == "min_variance":
                weights = _min_variance(ret)
            elif mode == "risk_parity":
                weights = _risk_parity(ret)
            else:
                weights = _equal_weight(valid_codes)

    # 计算各股建议金额和股数
    positions = []
    for code, w in weights.items():
        budget = capital * w
        # 尝试读取最新收盘价
        price = 0.0
        try:
            csv = DATA_DIR / f"{code}.csv"
            if csv.exists():
                df    = pd.read_csv(csv)
                price = float(df["close"].iloc[-1])
        except Exception:
            pass
        shares = int(budget / price / 100) * 100 if price > 0 else 0
        positions.append({
            "code":    code,
            "weight":  w,
            "budget":  round(budget, 2),
            "price":   round(price, 2),
            "shares":  shares,
        })

    # 组合预期收益/波动率
    exp_ret = exp_vol = None
    if not ret.empty and weights:
        w_arr = np.array([weights.get(c, 0) for c in ret.columns])
        mu    = ret.mean().values * 252
        cov   = ret.cov().values * 252
        exp_ret = round(float(w_arr @ mu) * 100, 2)
        exp_vol = round(float(np.sqrt(w_arr @ cov @ w_arr)) * 100, 2)

    return _emit({
        "ok":                 True,
        "mode":               mode,
        "weights":            weights,
        "positions":          positions,
        "expected_return":    exp_ret,
        "expected_volatility": exp_vol,
    })
