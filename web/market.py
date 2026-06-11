"""市场环境 / 大盘复盘 API 蓝图。"""
from flask import Blueprint, request, jsonify

import market_context as mctx
from app_state import require_auth

bp = Blueprint("market", __name__)


@bp.route("/api/market/context")
def api_market_context():
    try:
        force = request.args.get("refresh", "0") == "1"
        ctx   = mctx.get_market_regime(force_refresh=force)
        try:
            import ths_quote as tq
            client = tq.get_client()
            if client and client._ok:
                index_pairs = [(1, "000001"), (0, "399001"), (0, "399006")]
                name_map    = {"000001": "上证指数", "399001": "深证成指", "399006": "创业板指"}
                qs = client.get_index_quotes(index_pairs)
                for q in qs:
                    nm = name_map.get(q.get("code", ""), "")
                    if nm:
                        ctx.setdefault("indices", {}).setdefault(nm, {}).update({
                            "price":  round(float(q.get("price") or 0), 2),
                            "change": round(float(q.get("change_pct") or 0), 2),
                            "code":   q.get("code", ""),
                        })
        except Exception:
            pass
        return jsonify({"ok": True, **ctx})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@bp.route("/api/market/regime", methods=["POST"])
@require_auth
def api_market_regime():
    """
    市场环境检测（牛市/熊市/震荡/急跌）+ 建议仓位系数。
    ---
    responses:
      200:
        description: regime, score, position_multiplier
    """
    try:
        from market_regime import MarketRegimeDetector
        detector = MarketRegimeDetector()
        result   = detector.detect()
        return jsonify({"ok": True, **result})
    except ImportError:
        # 降级：使用 market_context
        ctx = mctx.get_market_regime(force_refresh=True)
        return jsonify({"ok": True, **ctx})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


def _get_market_review() -> dict:
    """大盘复盘：指数行情 + 市场宽度 + 板块轮动（含缓存）"""
    import pandas as _pd
    from datetime import datetime as _dt
    review = {
        "date":    _dt.now().strftime("%Y-%m-%d"),
        "time":    _dt.now().strftime("%H:%M"),
        "indices": {},
        "breadth": {},
        "sectors": {"top_gainers": [], "top_losers": []},
    }

    # 1. 指数实时价格 —— 优先 TDX，降级 akshare
    index_meta = [(1, "000001", "上证指数"), (0, "399001", "深证成指"), (0, "399006", "创业板指")]
    try:
        import ths_quote as tq
        client = tq.get_client()
        if client and client._ok:
            pairs    = [(mkt, code) for mkt, code, _ in index_meta]
            qs       = client.get_index_quotes(pairs)
            name_map = {code: name for _, code, name in index_meta}
            for q in qs:
                nm = name_map.get(q.get("code", ""), "")
                if nm:
                    review["indices"][nm] = {
                        "code":   q.get("code", ""),
                        "price":  round(float(q.get("price") or 0), 2),
                        "change": round(float(q.get("change_pct") or 0), 2),
                    }
    except Exception:
        pass

    if not review["indices"]:
        try:
            import akshare as ak
            code_map = {"000001": "上证指数", "399001": "深证成指", "399006": "创业板指"}
            df_all = ak.stock_zh_index_spot_em()
            if df_all is not None and not df_all.empty:
                for _, row in df_all.iterrows():
                    code = str(row.get("代码", ""))
                    nm   = code_map.get(code, "")
                    if nm:
                        review["indices"][nm] = {
                            "code":   code,
                            "price":  round(float(row.get("最新价", 0) or 0), 2),
                            "change": round(float(row.get("涨跌幅", 0) or 0), 2),
                        }
        except Exception:
            pass

    # 2. 市场宽度（全市场快照）—— 优先 TDX 全市场缓存（akshare 在云端常被东财
    #    掐连接，单靠它会让涨跌家数恒空）；TDX 缓存拿不到时再降级 akshare。
    try:
        import strategy_manager as _sm
        _quotes = _sm._get_market_quotes_cached()
        if _quotes:
            def _cp(q):
                try:
                    return float(q.get("change_pct") or 0)
                except Exception:
                    return 0.0
            review["breadth"] = {
                "up":       sum(1 for q in _quotes if _cp(q) > 0),
                "down":     sum(1 for q in _quotes if _cp(q) < 0),
                "flat":     sum(1 for q in _quotes if _cp(q) == 0),
                "limit_up": sum(1 for q in _quotes if _cp(q) >= 9.9),
                "limit_dn": sum(1 for q in _quotes if _cp(q) <= -9.9),
                "total":    len(_quotes),
            }
    except Exception:
        pass
    if not review["breadth"]:
        try:
            import akshare as ak
            df_spot = ak.stock_zh_a_spot_em()
            if df_spot is not None and not df_spot.empty and "涨跌幅" in df_spot.columns:
                chg = _pd.to_numeric(df_spot["涨跌幅"], errors="coerce").dropna()
                review["breadth"] = {
                    "up":       int((chg > 0).sum()),
                    "down":     int((chg < 0).sum()),
                    "flat":     int((chg == 0).sum()),
                    "limit_up": int((chg >= 9.9).sum()),
                    "limit_dn": int((chg <= -9.9).sum()),
                    "total":    len(chg),
                }
        except Exception:
            pass

    # 3. 板块轮动（行业板块）
    # stock_board_industry_rank_em 在 akshare 1.18.x 不存在（此处曾静默失败导致板块恒空），
    # 正确接口为 stock_board_industry_name_em；名称列需模糊匹配（首列是"排名"）。
    try:
        import akshare as ak
        df_sec = ak.stock_board_industry_name_em()
        if df_sec is not None and not df_sec.empty:
            chg_col  = next((c for c in df_sec.columns if "涨跌幅" in c), None)
            name_col = next((c for c in df_sec.columns if "名称" in str(c)), df_sec.columns[0])
            if chg_col:
                df_sec[chg_col] = _pd.to_numeric(df_sec[chg_col], errors="coerce")
                df_s = df_sec.dropna(subset=[chg_col]).sort_values(chg_col, ascending=False)
                review["sectors"]["top_gainers"] = df_s.head(3)[name_col].tolist()
                review["sectors"]["top_losers"]  = df_s.tail(3)[name_col].tolist()
    except Exception:
        pass

    return review


@bp.route("/api/market/review")
def api_market_review():
    """大盘复盘（公开接口）"""
    try:
        return jsonify({"ok": True, **_get_market_review()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
