# -*- coding: utf-8 -*-
"""邮件发送（SMTP）。仅用标准库 smtplib，配置全部走环境变量（.env），不入库不入 git。

支持 QQ/163/Gmail 等常见 SMTP。默认 SSL(465)，也支持 STARTTLS(587)。
环境变量：
  SMTP_HOST       smtp.qq.com / smtp.163.com / smtp.gmail.com ...
  SMTP_PORT       465(SSL，默认) 或 587(STARTTLS)
  SMTP_USER       完整发信邮箱地址（如 xxx@qq.com）
  SMTP_PASSWORD   邮箱「授权码」/应用专用密码（不是登录密码）
  SMTP_FROM       发信地址，默认=SMTP_USER
  SMTP_FROM_NAME  发件人显示名，默认「见微 Alpha」
  SMTP_SSL        1=SSL(默认) / 0=STARTTLS
未配置时 send_email 返回 {"ok": False, "error": "SMTP 未配置"}，调用方据此降级。
"""
import os
import ssl
import smtplib
import logging
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr

logger = logging.getLogger(__name__)


def _cfg() -> dict:
    return {
        "host":      os.environ.get("SMTP_HOST", "").strip(),
        "port":      int(os.environ.get("SMTP_PORT", "465") or 465),
        "user":      os.environ.get("SMTP_USER", "").strip(),
        "password":  os.environ.get("SMTP_PASSWORD", "").strip(),
        "from":      (os.environ.get("SMTP_FROM", "") or os.environ.get("SMTP_USER", "")).strip(),
        "from_name": os.environ.get("SMTP_FROM_NAME", "见微 Alpha").strip(),
        "ssl":       str(os.environ.get("SMTP_SSL", "1")).strip().lower() in ("1", "true", "yes", "on"),
    }


def smtp_configured() -> bool:
    c = _cfg()
    return bool(c["host"] and c["user"] and c["password"])


def send_email(to: str, subject: str, html: str) -> dict:
    """发送一封 HTML 邮件。返回 {"ok": bool, "error"?: str}。"""
    c = _cfg()
    if not (c["host"] and c["user"] and c["password"]):
        return {"ok": False, "error": "SMTP 未配置"}
    if not to or "@" not in to:
        return {"ok": False, "error": "收件地址无效"}

    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"]    = formataddr((str(Header(c["from_name"], "utf-8")), c["from"]))
    msg["To"]      = to

    try:
        if c["ssl"]:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(c["host"], c["port"], timeout=15, context=ctx) as s:
                s.login(c["user"], c["password"])
                s.sendmail(c["from"], [to], msg.as_string())
        else:
            with smtplib.SMTP(c["host"], c["port"], timeout=15) as s:
                s.ehlo()
                s.starttls(context=ssl.create_default_context())
                s.login(c["user"], c["password"])
                s.sendmail(c["from"], [to], msg.as_string())
        return {"ok": True}
    except Exception as e:
        logger.warning(f"[email] 发送失败 to={to}: {e}")
        return {"ok": False, "error": str(e)}


def render_reset_email(username: str, link: str, ttl_min: int = 30) -> str:
    """密码重置邮件 HTML 模板。"""
    return f"""\
<div style="font-family:-apple-system,'Microsoft YaHei',sans-serif;max-width:480px;margin:0 auto;
     padding:32px 28px;background:#fff;border:1px solid #eee;border-radius:14px;color:#333">
  <h2 style="margin:0 0 16px;color:#E0536C">见微 Alpha 密码重置</h2>
  <p>你好 <b>{username}</b>，</p>
  <p>我们收到了你的密码重置请求。点击下方按钮设置新密码（链接 <b>{ttl_min} 分钟</b>内有效）：</p>
  <p style="text-align:center;margin:28px 0">
    <a href="{link}" style="display:inline-block;padding:12px 28px;background:#E0536C;color:#fff;
       text-decoration:none;border-radius:8px;font-weight:600">重置密码</a>
  </p>
  <p style="color:#888;font-size:13px">若按钮无法点击，请复制以下链接到浏览器打开：<br>
    <span style="word-break:break-all;color:#E0536C">{link}</span></p>
  <hr style="border:none;border-top:1px solid #eee;margin:24px 0">
  <p style="color:#999;font-size:12px;margin:0">如果这不是你本人操作，请忽略此邮件，你的密码不会被更改。</p>
</div>"""
