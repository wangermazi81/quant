"""龙虎榜 API 蓝图。

- GET /api/lhb/data?date=YYYY-MM-DD   当日龙虎榜全量（股票+席位+游资图谱）
- GET /api/lhb/dates                  本地已缓存的历史日期列表
"""
import logging
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)
bp = Blueprint("lhb", __name__)


@bp.route("/api/lhb/data")
def api_lhb_data():
    import lhb_engine as le
    date = (request.args.get("date") or "").strip()
    force = request.args.get("refresh", "0") == "1"
    data = le.get_lhb_data(date or None, force_refresh=force)
    return jsonify(data)


@bp.route("/api/lhb/dates")
def api_lhb_dates():
    import lhb_engine as le
    return jsonify({"dates": le.get_lhb_dates()})
