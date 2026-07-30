# -*- coding: utf-8 -*-
"""见微 Alpha 因子评分器 —— IC / IR / alive-dead 分类

设计目标：
  - 评估当前 zoo/ 里已注册的因子在真实数据上的有效性
  - 不动 base/registry/任何因子文件；纯增量
  - 用 data/daily/*.csv 现成缓存当数据源，0 网络调用
  - 失败安全：单个因子炸不影响其它；归类到 "error"
  - 产出 logs/factor_scores/<时间戳>/scores.{csv,md} + 一份 Run Card

定义：
  - IC (Information Coefficient)：每个截面日 factor_value vs 未来 h 日收益的
    Spearman 秩相关系数。
  - IR (Information Ratio)：IC 均值 / IC 标准差 ×  sqrt(年化因子)，
    度量 IC 稳定性。
  - 分类阈值（业内常用近似）：
      alive    |IC_mean| > 0.02 且 |IR| > 0.3
      weak     0.01 < |IC_mean| <= 0.02
      reversed 前后段 IC 均值符号相反且都 > 0.01（动量/反转切换）
      dead     |IC_mean| <= 0.01 或 |IR| < 0.1
      error    compute() 抛错 / SkipAlpha / 数据不足

跑法：
    cd D:\\同花顺量化交易\\ths_auto_trader
    python -m analytics.factors_zoo.scorer
    # 可选：python -m analytics.factors_zoo.scorer --n-stocks 100 --days 252
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
import pandas as pd

from analytics import st_status_history

logger = logging.getLogger(__name__)

# 项目根 = ths_auto_trader/
_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _ROOT / "data" / "daily"
_OUT_BASE = _ROOT / "logs" / "factor_scores"

# 默认评估前瞻期（天）
_DEFAULT_HORIZONS = (1, 5, 10)
# 截面最少股票数（少于这个数不计 IC，太稀薄）
_MIN_CROSS_SECTION = 5
# 年化常数（A 股交易日数）
_TRADING_DAYS = 252
MAX_ABS_DAILY_RETURN = 0.35

# 分类阈值
_TH_ALIVE_IC = 0.02
_TH_ALIVE_IR = 0.3
_TH_WEAK_IC  = 0.01
_TH_REV_IC   = 0.01
_TH_DEAD_IR  = 0.1


# ============================== 数据加载 ==============================

def _has_discontinuous_price_history(frame: pd.DataFrame) -> bool:
    """Reject cache slices that cannot represent a continuous tradable price series."""
    if "close" not in frame.columns:
        return True
    close = pd.to_numeric(frame["close"], errors="coerce")
    if close.isna().any() or (close <= 0).any():
        return True
    returns = close.pct_change(fill_method=None)
    return bool((returns.abs() > MAX_ABS_DAILY_RETURN).any())


def _price_continuity_mask(frame: pd.DataFrame) -> pd.Series:
    close = pd.to_numeric(frame.get("close"), errors="coerce")
    valid = close.notna() & (close > 0)
    breaks = ~valid
    breaks |= close.pct_change(fill_method=None).abs() > MAX_ABS_DAILY_RETURN
    if bool(breaks.any()):
        valid.loc[breaks[breaks].index[0] :] = False
    return valid.astype(bool)


def _st_status_from_cache(
    frame: pd.DataFrame,
    code: str = "",
    history_cache: dict | None = None,
) -> tuple[pd.Series, pd.Series]:
    if code and isinstance(history_cache, dict):
        digits = "".join(ch for ch in str(code).split(".", 1)[0] if ch.isdigit())
        history_item = (
            (history_cache.get("codes") or {}).get(digits[-6:], {})
            if len(digits) >= 6
            else {}
        )
        records = (
            history_item.get("records")
            if isinstance(history_item, dict)
            else None
        )
        if isinstance(records, list) and records:
            historical_is_st, historical_known = (
                st_status_history.status_series(
                    code,
                    frame.index,
                    cache=history_cache,
                )
            )
            return (
                historical_is_st.astype(np.float64),
                historical_known.astype(np.float64),
            )

    is_st = pd.Series(False, index=frame.index, dtype=bool)
    known = pd.Series(False, index=frame.index, dtype=bool)
    if "is_st" in frame.columns:
        raw = frame["is_st"]
        text = raw.astype("string").str.strip().str.lower()
        numeric = pd.to_numeric(raw, errors="coerce")
        explicit_true = text.isin({"1", "true", "yes", "y", "st", "是"}) | numeric.eq(1)
        explicit_false = text.isin({"0", "false", "no", "n", "normal", "普通", "否"}) | numeric.eq(0)
        parsed = (explicit_true | explicit_false).fillna(False)
        is_st.loc[parsed] = explicit_true.loc[parsed].astype(bool)
        known.loc[parsed] = True
    if "name" in frame.columns:
        names = frame["name"].astype("string").str.strip()
        name_known = (names.notna() & names.str.len().gt(0)).fillna(False)
        use_name = name_known & ~known
        normalized = names.str.replace(r"\s+", "", regex=True).str.upper()
        name_is_st = normalized.str.match(r"^(?:S\*?ST|\*?ST)", na=False)
        is_st.loc[use_name] = name_is_st.loc[use_name].astype(bool)
        known.loc[use_name] = True
    if code and isinstance(history_cache, dict):
        historical_is_st, historical_known = st_status_history.status_series(
            code,
            frame.index,
            cache=history_cache,
        )
        use_history = historical_known
        is_st.loc[use_history] = historical_is_st.loc[use_history].astype(bool)
        known.loc[use_history] = True
    return is_st.astype(np.float64), known.astype(np.float64)


def build_panel_from_cache(
    n_stocks: Optional[int] = None,
    days: int = 252,
    min_history: Optional[int] = None,
    require_latest: bool = False,
    point_in_time: bool = False,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """从 data/daily/*.csv 拼一个 wide OHLCV panel。

    min_history 默认 = days：要求股票有完整窗口的历史。这样长 warmup 因子
    （如 academic_hml/carhart_mom 要 252 天）不会因新股大量 NaN 触发
    registry 的 95% NaN 阈值。如要包含新股，显式传更小的 min_history。

    Returns: (panel, used_codes)
    """
    if not _DATA_DIR.is_dir():
        raise FileNotFoundError(f"数据缓存目录不存在: {_DATA_DIR}")
    if min_history is None:
        min_history = days

    csvs = sorted(_DATA_DIR.glob("*.csv"))
    ranked_csvs: list[tuple[pd.Timestamp, int, str, Path]] = []
    skipped_short = 0
    for csv in csvs:
        try:
            dates = pd.read_csv(csv, usecols=["date"])
        except Exception:
            continue
        if len(dates) < min_history:
            skipped_short += 1
            continue
        parsed = pd.to_datetime(dates["date"], errors="coerce").dropna()
        if parsed.empty:
            continue
        ranked_csvs.append((parsed.max().normalize(), len(dates), csv.stem, csv))

    if n_stocks is not None and ranked_csvs:
        latest_counts: dict[pd.Timestamp, int] = {}
        for latest, _rows, _code, _csv in ranked_csvs:
            latest_counts[latest] = latest_counts.get(latest, 0) + 1
        newest_date = max(latest_counts)
        newest_count = latest_counts[newest_date]
        if require_latest and newest_count < n_stocks:
            raise ValueError(
                f"latest cache date {newest_date.date()} only has {newest_count} eligible stocks; "
                f"need {n_stocks}. Refresh data before generating live signals."
            )
        if not point_in_time:
            eligible_dates = [date for date, count in latest_counts.items() if count >= n_stocks]
            if eligible_dates:
                target_latest = newest_date if require_latest else max(eligible_dates)
                ranked_csvs = [item for item in ranked_csvs if item[0] == target_latest]
                logger.info(f"[scorer] 选用覆盖率满足 {n_stocks} 只的最新截面: {target_latest.date()}")

    if n_stocks is not None:
        ranked_csvs.sort(key=lambda item: hashlib.sha256(item[2].encode("ascii", errors="ignore")).hexdigest())
    else:
        ranked_csvs.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    per: dict[str, dict[str, pd.Series]] = {}
    try:
        st_history_cache = st_status_history.load_cache()
    except Exception:
        st_history_cache = {}
    skipped_discontinuous = 0
    for _latest, _rows, _code, csv in ranked_csvs:
        # 关键：先按 min_history 过滤，再用 n_stocks 限制结果数量；
        # 否则若前 N 个 CSV 都是新股会全被丢，结果空。
        if n_stocks is not None and not point_in_time and len(per) >= n_stocks:
            break
        code = csv.stem
        try:
            df = pd.read_csv(csv, parse_dates=["date"])
        except Exception:
            continue
        if len(df) < min_history:
            skipped_short += 1
            continue
        df = df.sort_values("date").tail(days).set_index("date")
        if not point_in_time and _has_discontinuous_price_history(df):
            skipped_discontinuous += 1
            continue
        is_st, st_status_known = _st_status_from_cache(df, code=code, history_cache=st_history_cache)
        cols = {}
        for c in ("open", "high", "low", "close", "volume"):
            if c in df.columns:
                cols[c] = pd.to_numeric(df[c], errors="coerce").astype(np.float64)
        if "close" in cols:  # close 必有
            cols["is_st"] = is_st
            cols["st_status_known"] = st_status_known
            if "volume" in cols:
                proxy_amount = cols["close"] * cols["volume"]
                if "amount" in df.columns:
                    raw_amount = pd.to_numeric(df["amount"], errors="coerce").astype(np.float64)
                    real_amount = raw_amount.notna() & (raw_amount > 0)
                    cols["amount"] = raw_amount.where(real_amount, proxy_amount)
                    cols["amount_real"] = real_amount.astype(np.float64)
                else:
                    cols["amount"] = proxy_amount
                    cols["amount_real"] = pd.Series(0.0, index=df.index, dtype=np.float64)
            if point_in_time:
                cols["continuity_eligible"] = _price_continuity_mask(df).astype(np.float64)
            per[code] = cols

    if skipped_short:
        logger.info(f"[scorer] 跳过 {skipped_short} 只历史不足 {min_history} 天的股票")
    if skipped_discontinuous:
        logger.warning(
            f"[scorer] 隔离 {skipped_discontinuous} 只存在单日价格断层的股票 "
            f"(abs return > {MAX_ABS_DAILY_RETURN:.0%})"
        )
    if require_latest and n_stocks is not None and not point_in_time and len(per) < n_stocks:
        raise ValueError(
            f"latest cache has only {len(per)} quality-valid stocks after price continuity checks; "
            f"need {n_stocks}. Refresh or repair daily data before generating live signals."
        )
    if not per:
        return {}, []

    panel: dict[str, pd.DataFrame] = {}
    for c in (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "amount_real",
        "is_st",
        "st_status_known",
        "continuity_eligible",
    ):
        series_map = {code: cols[c] for code, cols in per.items() if c in cols}
        if series_map:
            panel[c] = pd.DataFrame(series_map).sort_index()
    if not point_in_time:
        return panel, sorted(per.keys())

    close = panel["close"].sort_index().tail(days)
    index = close.index
    columns = close.columns
    panel = {
        key: frame.reindex(index=index, columns=columns)
        for key, frame in panel.items()
    }
    continuity = panel.pop("continuity_eligible").fillna(0.0).astype(bool)
    required_valid = panel["close"].notna() & (panel["close"] > 0)
    for field in ("open", "high", "low", "volume"):
        if field in panel:
            required_valid &= panel[field].notna()
    history_ready = panel["close"].notna().cumsum() >= int(min_history)
    eligible = continuity & required_valid & history_ready

    if n_stocks is not None:
        stable_order = sorted(
            columns,
            key=lambda code: hashlib.sha256(str(code).encode("ascii", errors="ignore")).hexdigest(),
        )
        selected = pd.DataFrame(False, index=index, columns=columns)
        for trade_date in index:
            available = [code for code in stable_order if bool(eligible.at[trade_date, code])]
            selected.loc[trade_date, available[:n_stocks]] = True
        eligible = selected

    latest_count = int(eligible.iloc[-1].sum()) if not eligible.empty else 0
    if require_latest and n_stocks is not None and latest_count < n_stocks:
        raise ValueError(
            f"latest cache has only {latest_count} point-in-time eligible stocks; "
            f"need {n_stocks}. Refresh or repair daily data before generating live signals."
        )

    used_codes = [str(code) for code in columns if bool(eligible[code].any())]
    if not used_codes:
        return {}, []
    for field in tuple(panel):
        panel[field] = panel[field].loc[:, used_codes]
        if field in {"open", "high", "low", "close", "volume", "amount"}:
            panel[field] = panel[field].where(continuity.loc[:, used_codes])
    panel["universe_eligible"] = eligible.loc[:, used_codes]
    return panel, sorted(used_codes)


# ============================== IC 计算 ==============================

def forward_returns(close: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """未来 h 日的对数收益（t → t+h），最后 h 行 NaN。"""
    if horizon < 1:
        raise ValueError("horizon 必须 >= 1")
    return np.log(close.shift(-horizon) / close)


def rank_ic_series(
    factor: pd.DataFrame,
    fwd_ret: pd.DataFrame,
    min_cross_section: int = _MIN_CROSS_SECTION,
) -> pd.Series:
    """按日截面 Spearman 秩相关，返回 IC 时序（index=date）。"""
    # 列对齐
    common = factor.columns.intersection(fwd_ret.columns)
    if len(common) < min_cross_section:
        return pd.Series(dtype=np.float64)
    f = factor[common]
    r = fwd_ret[common]
    # 日期对齐
    idx = f.index.intersection(r.index)
    f = f.loc[idx]
    r = r.loc[idx]
    # 截面 rank
    f_rank = f.rank(axis=1, method="average")
    r_rank = r.rank(axis=1, method="average")

    out = []
    for date in idx:
        fr = f_rank.loc[date]
        rr = r_rank.loc[date]
        mask = fr.notna() & rr.notna()
        if mask.sum() < min_cross_section:
            out.append(np.nan)
            continue
        # Pearson on ranks == Spearman
        a = fr[mask].to_numpy()
        b = rr[mask].to_numpy()
        if a.std() == 0 or b.std() == 0:
            out.append(np.nan)
            continue
        out.append(float(np.corrcoef(a, b)[0, 1]))
    return pd.Series(out, index=idx, dtype=np.float64).dropna()


def summarize_ic(ic: pd.Series, horizon: int) -> dict[str, Any]:
    """从 IC 时序汇总指标。"""
    if ic.empty:
        return {"n_days": 0, "ic_mean": np.nan, "ic_std": np.nan,
                "ir": np.nan, "hit_rate": np.nan,
                "ic_first_half_mean": np.nan, "ic_second_half_mean": np.nan}
    mean = float(ic.mean())
    std  = float(ic.std(ddof=1)) if len(ic) > 1 else np.nan
    # 年化 IR：sqrt(年内可独立预测次数) = sqrt(252 / horizon)
    ir = mean / std * np.sqrt(_TRADING_DAYS / horizon) if std and not np.isnan(std) and std > 0 else np.nan
    # hit rate：IC 与均值同向的比例（衡量稳定性，不是赚钱概率）
    hit = float((np.sign(ic) == np.sign(mean)).mean()) if mean != 0 else 0.5
    # 前后段 IC（用于 reversed 判定）
    mid = len(ic) // 2
    first_half  = float(ic.iloc[:mid].mean()) if mid > 0 else np.nan
    second_half = float(ic.iloc[mid:].mean()) if mid < len(ic) else np.nan
    return {
        "n_days":              int(len(ic)),
        "ic_mean":             mean,
        "ic_std":              std,
        "ir":                  ir,
        "hit_rate":            hit,
        "ic_first_half_mean":  first_half,
        "ic_second_half_mean": second_half,
    }


def categorize(s: dict[str, Any]) -> str:
    """alive / weak / reversed / dead / error / insufficient"""
    if s.get("error"):
        return "error"
    if s.get("n_days", 0) < 20:
        return "insufficient"
    ic_abs = abs(s["ic_mean"])
    ir_abs = abs(s["ir"]) if not pd.isna(s["ir"]) else 0.0
    # reversed
    fh = s.get("ic_first_half_mean", np.nan)
    sh = s.get("ic_second_half_mean", np.nan)
    if (not pd.isna(fh) and not pd.isna(sh)
            and np.sign(fh) != np.sign(sh)
            and abs(fh) > _TH_REV_IC and abs(sh) > _TH_REV_IC):
        return "reversed"
    if ic_abs > _TH_ALIVE_IC and ir_abs > _TH_ALIVE_IR:
        return "alive"
    if ic_abs > _TH_WEAK_IC:
        return "weak"
    return "dead"


# ============================== 打分主流程 ==============================

def score_one(
    registry: Any,
    alpha_id: str,
    panel: dict[str, pd.DataFrame],
    horizons: Sequence[int] = _DEFAULT_HORIZONS,
) -> list[dict[str, Any]]:
    """对一个因子在多个 horizon 下打分。返回每个 horizon 一条记录的 list。"""
    try:
        factor = registry.compute(alpha_id, panel)
    except Exception as exc:
        return [{
            "alpha_id": alpha_id, "horizon": h, "category": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "n_days": 0, "ic_mean": np.nan, "ic_std": np.nan, "ir": np.nan,
            "hit_rate": np.nan,
        } for h in horizons]

    close = panel.get("close")
    if close is None:
        return [{"alpha_id": alpha_id, "horizon": h, "category": "error",
                 "error": "panel 缺 close", "n_days": 0,
                 "ic_mean": np.nan, "ic_std": np.nan, "ir": np.nan,
                 "hit_rate": np.nan} for h in horizons]

    rows = []
    for h in horizons:
        fwd = forward_returns(close, h)
        ic  = rank_ic_series(factor, fwd)
        summary = summarize_ic(ic, h)
        summary["alpha_id"] = alpha_id
        summary["horizon"]  = h
        summary["category"] = categorize(summary)
        summary["error"]    = None
        rows.append(summary)
    return rows


def score_all(
    panel: dict[str, pd.DataFrame],
    horizons: Sequence[int] = _DEFAULT_HORIZONS,
) -> pd.DataFrame:
    """跑所有已注册因子。返回 tidy DataFrame（每行：一个 alpha × 一个 horizon）。"""
    from analytics.factors_zoo.registry import (
        get_default_registry, reset_default_registry,
    )
    reset_default_registry()
    reg = get_default_registry()

    alpha_ids = reg.list()
    if not alpha_ids:
        return pd.DataFrame()

    all_rows: list[dict[str, Any]] = []
    for aid in alpha_ids:
        try:
            all_rows.extend(score_one(reg, aid, panel, horizons))
        except Exception as exc:
            logger.warning(f"[scorer] {aid} 意外异常: {exc}\n{traceback.format_exc()}")
            for h in horizons:
                all_rows.append({
                    "alpha_id": aid, "horizon": h, "category": "error",
                    "error": f"unexpected: {exc}",
                    "n_days": 0, "ic_mean": np.nan, "ic_std": np.nan,
                    "ir": np.nan, "hit_rate": np.nan,
                })
    cols = ["alpha_id", "horizon", "category", "n_days",
            "ic_mean", "ic_std", "ir", "hit_rate",
            "ic_first_half_mean", "ic_second_half_mean", "error"]
    df = pd.DataFrame(all_rows)
    return df[[c for c in cols if c in df.columns]]


# ============================== 报告渲染 ==============================

def render_report(scores: pd.DataFrame, out_dir: Path) -> tuple[Path, Path]:
    """写 scores.csv + scores.md。返回 (csv_path, md_path)。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "scores.csv"
    md_path  = out_dir / "scores.md"
    scores.to_csv(csv_path, index=False, encoding="utf-8-sig")  # Excel 友好

    lines = ["# 因子评分报告", "", f"_生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}_", ""]
    cat_count = scores.groupby("category")["alpha_id"].nunique().to_dict() if not scores.empty else {}
    lines += ["## 分类汇总（按 alpha_id 去重）", ""]
    for cat in ("alive", "weak", "reversed", "dead", "insufficient", "error"):
        lines.append(f"- **{cat}**: {cat_count.get(cat, 0)}")
    lines += ["", "## 明细", "",
              "| alpha_id | horizon | category | n_days | ic_mean | ir | hit_rate |",
              "|---|---:|---|---:|---:|---:|---:|"]
    for _, r in scores.iterrows():
        lines.append(
            f"| {r['alpha_id']} | {int(r['horizon'])} | {r['category']} | "
            f"{int(r['n_days'])} | "
            f"{r['ic_mean']:.4f} | "
            f"{(r['ir'] if not pd.isna(r['ir']) else float('nan')):.3f} | "
            f"{(r['hit_rate'] if not pd.isna(r['hit_rate']) else float('nan')):.2%} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return csv_path, md_path


# ============================== CLI ==============================

def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="见微 Alpha 因子评分器")
    p.add_argument("--n-stocks", type=int, default=200,
                   help="评估股票数（默认 200，按 data/daily 字典序前 N）")
    p.add_argument("--days", type=int, default=500,
                   help="历史窗口（默认 500 交易日；academic_hml/carhart_mom 需 252 天 warmup，"
                        "所以默认要给足 500+ 才能算出有效 IC）")
    p.add_argument("--horizons", type=str, default="1,5,10",
                   help="前瞻日数，逗号分隔（默认 1,5,10）")
    args = p.parse_args(argv)

    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())

    # Run Card（在自己身上用 Run Card —— 完美的可复现实验场景）
    try:
        from core.run_card import RunCard
        card = RunCard.start("factor_scoring", inputs={
            "n_stocks": args.n_stocks, "days": args.days,
            "horizons": list(horizons),
        })
    except Exception:
        card = None

    print(f"[scorer] 构建 panel: n_stocks<={args.n_stocks}, days={args.days}")
    panel, codes = build_panel_from_cache(n_stocks=args.n_stocks, days=args.days)
    if not panel:
        print("[scorer] 错误：未能从 data/daily 加载到任何股票", file=sys.stderr)
        if card: card.finish(outputs={"ok": False, "reason": "empty panel"}, status="error")
        return 1
    print(f"[scorer] 实际股票数={len(codes)}, close.shape={panel['close'].shape}")

    print(f"[scorer] 评分中（horizons={horizons}）...")
    scores = score_all(panel, horizons=horizons)
    if scores.empty:
        print("[scorer] 注册表为空（zoo/ 里无可加载因子）", file=sys.stderr)
        if card: card.finish(outputs={"ok": False, "reason": "no alphas registered"}, status="error")
        return 2

    stamp   = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = _OUT_BASE / stamp
    csv_p, md_p = render_report(scores, out_dir)
    print(f"[scorer] 完成。{len(scores)} 行 ×（{scores['alpha_id'].nunique()} 个 alpha）")
    print(f"  CSV: {csv_p}")
    print(f"  MD : {md_p}")

    if card:
        summary = scores.groupby("category")["alpha_id"].nunique().to_dict()
        card.finish(outputs={
            "ok": True, "n_alphas": int(scores["alpha_id"].nunique()),
            "n_rows": int(len(scores)),
            "category_counts": {k: int(v) for k, v in summary.items()},
            "report_dir": str(out_dir),
        })
    return 0


if __name__ == "__main__":
    sys.exit(main())
