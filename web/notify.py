"""消息推送配置 / 测试 API 蓝图。"""
import json
from datetime import datetime

from flask import Blueprint, request, jsonify, session

from core import notification_manager as nm
from app_state import require_auth, _add_log

bp = Blueprint("notify", __name__)


@bp.route("/api/user/notify_config", methods=["GET"])
@require_auth
def user_get_notify_config():
    cfg = nm.load_notify_config()
    # 隐藏敏感字段
    safe = json.loads(json.dumps(cfg))
    for ch in ("wecom", "feishu", "dingtalk"):
        wh = safe.get(ch, {}).get("webhook", "")
        if wh and len(wh) > 20:
            safe[ch]["webhook"] = wh[:12] + "****" + wh[-8:]
    tg = safe.get("telegram", {})
    if tg.get("token") and len(tg["token"]) > 10:
        tg["token"] = tg["token"][:8] + "****"
    return jsonify({"ok": True, **safe})


@bp.route("/api/user/notify_config", methods=["POST"])
@require_auth
def user_save_notify_config():
    uid = session["user_id"]
    d   = request.json or {}
    # 加载已有配置，合并更新（避免覆盖 ****）
    existing = nm.load_notify_config()
    for channel in ("wecom", "feishu", "dingtalk", "telegram"):
        if channel in d and isinstance(d[channel], dict):
            if channel not in existing:
                existing[channel] = {}
            for k, v in d[channel].items():
                # 忽略含 **** 的字段（前端回传了脱敏值）
                if "****" not in str(v):
                    existing[channel][k] = v
    nm.save_notify_config(existing)
    _add_log(uid, "消息推送配置已更新")
    return jsonify({"ok": True})


@bp.route("/api/notify/test", methods=["POST"])
@require_auth
def api_notify_test():
    """测试推送（发送测试消息到所有启用渠道）"""
    uid = session["user_id"]
    result = nm.send_notification(
        title="🔔 见微 Alpha 推送测试",
        content=f"测试消息发送成功！\n用户：{session.get('username','')}\n时间：{datetime.now().strftime('%H:%M:%S')}",
    )
    _add_log(uid, f"推送测试: {'成功' if result.get('ok') else '失败'} {result.get('results', {})}")
    return jsonify(result)
