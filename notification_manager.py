# -*- coding: utf-8 -*-
"""
消息推送管理器
支持：企业微信 / 飞书 / 钉钉 / Telegram
"""

import json
import logging
import urllib.request
import urllib.parse
from pathlib import Path

logger = logging.getLogger(__name__)

NOTIFY_CONFIG_FILE = Path(__file__).parent / "data" / "notify_config.json"

DEFAULT_CONFIG = {
    "wecom":    {"enabled": False, "webhook": ""},
    "feishu":   {"enabled": False, "webhook": ""},
    "dingtalk": {"enabled": False, "webhook": "", "secret": ""},
    "telegram": {"enabled": False, "token": "", "chat_id": ""},
}


def load_notify_config(uid: int = 0) -> dict:
    """加载通知配置（全局共享，uid 保留供未来多用户扩展）"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    path = NOTIFY_CONFIG_FILE
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            for channel, vals in saved.items():
                if channel in cfg and isinstance(vals, dict):
                    cfg[channel].update(vals)
        except Exception:
            pass
    return cfg


def save_notify_config(config: dict, uid: int = 0):
    """保存通知配置"""
    NOTIFY_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    NOTIFY_CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def _post_json(url: str, body: dict, headers: dict = None, timeout: int = 10) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _send_wecom(webhook: str, title: str, content: str) -> dict:
    """企业微信 Webhook 推送（markdown 格式）"""
    url = webhook.strip()
    if not url:
        return {"ok": False, "error": "Webhook URL 未配置"}
    body = {
        "msgtype": "markdown",
        "markdown": {
            "content": f"## {title}\n{content}"
        }
    }
    try:
        r = _post_json(url, body)
        if r.get("errcode") == 0:
            return {"ok": True}
        return {"ok": False, "error": r.get("errmsg", str(r))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _send_feishu(webhook: str, title: str, content: str) -> dict:
    """飞书 Webhook 推送"""
    url = webhook.strip()
    if not url:
        return {"ok": False, "error": "Webhook URL 未配置"}
    body = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": [[{"tag": "text", "text": content}]]
                }
            }
        }
    }
    try:
        r = _post_json(url, body)
        if r.get("StatusCode") == 0 or r.get("code") == 0:
            return {"ok": True}
        return {"ok": False, "error": r.get("msg", str(r))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _send_dingtalk(webhook: str, secret: str, title: str, content: str) -> dict:
    """钉钉 Webhook 推送（支持签名）"""
    url = webhook.strip()
    if not url:
        return {"ok": False, "error": "Webhook URL 未配置"}
    # 加签（如果配置了 secret）
    if secret and secret.strip():
        import time, hmac, hashlib, base64
        ts = str(round(time.time() * 1000))
        sign_str = f"{ts}\n{secret.strip()}"
        sign = base64.b64encode(
            hmac.new(secret.strip().encode("utf-8"), sign_str.encode("utf-8"), digestmod=hashlib.sha256).digest()
        ).decode("utf-8")
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}timestamp={ts}&sign={urllib.parse.quote_plus(sign)}"
    body = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": f"## {title}\n{content}"
        }
    }
    try:
        r = _post_json(url, body)
        if r.get("errcode") == 0:
            return {"ok": True}
        return {"ok": False, "error": r.get("errmsg", str(r))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _send_telegram(token: str, chat_id: str, title: str, content: str) -> dict:
    """Telegram Bot 推送"""
    if not token or not chat_id:
        return {"ok": False, "error": "Token 或 Chat ID 未配置"}
    url = f"https://api.telegram.org/bot{token.strip()}/sendMessage"
    text = f"*{title}*\n{content}"
    body = {"chat_id": chat_id.strip(), "text": text, "parse_mode": "Markdown"}
    try:
        r = _post_json(url, body, timeout=15)
        if r.get("ok"):
            return {"ok": True}
        return {"ok": False, "error": r.get("description", str(r))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send_notification(title: str, content: str, config: dict = None) -> dict:
    """
    向所有启用的渠道发送通知。
    config: 通知配置（来自 load_notify_config），为 None 则自动加载。
    返回: {"ok": True/False, "results": {channel: result_dict}}
    """
    if config is None:
        config = load_notify_config()

    results = {}
    any_ok = False

    try:
        wc = config.get("wecom", {})
        if wc.get("enabled") and wc.get("webhook"):
            r = _send_wecom(wc["webhook"], title, content)
            results["wecom"] = r
            if r.get("ok"):
                any_ok = True
            else:
                logger.warning(f"[Notify] 企业微信推送失败: {r.get('error')}")
    except Exception as e:
        results["wecom"] = {"ok": False, "error": str(e)}

    try:
        fs = config.get("feishu", {})
        if fs.get("enabled") and fs.get("webhook"):
            r = _send_feishu(fs["webhook"], title, content)
            results["feishu"] = r
            if r.get("ok"):
                any_ok = True
            else:
                logger.warning(f"[Notify] 飞书推送失败: {r.get('error')}")
    except Exception as e:
        results["feishu"] = {"ok": False, "error": str(e)}

    try:
        dt = config.get("dingtalk", {})
        if dt.get("enabled") and dt.get("webhook"):
            r = _send_dingtalk(dt["webhook"], dt.get("secret", ""), title, content)
            results["dingtalk"] = r
            if r.get("ok"):
                any_ok = True
            else:
                logger.warning(f"[Notify] 钉钉推送失败: {r.get('error')}")
    except Exception as e:
        results["dingtalk"] = {"ok": False, "error": str(e)}

    try:
        tg = config.get("telegram", {})
        if tg.get("enabled") and tg.get("token") and tg.get("chat_id"):
            r = _send_telegram(tg["token"], tg["chat_id"], title, content)
            results["telegram"] = r
            if r.get("ok"):
                any_ok = True
            else:
                logger.warning(f"[Notify] Telegram推送失败: {r.get('error')}")
    except Exception as e:
        results["telegram"] = {"ok": False, "error": str(e)}

    enabled_count = sum(1 for ch in ["wecom", "feishu", "dingtalk", "telegram"]
                        if config.get(ch, {}).get("enabled"))
    if enabled_count == 0:
        return {"ok": False, "error": "未启用任何推送渠道", "results": results}

    return {"ok": any_ok, "results": results}
