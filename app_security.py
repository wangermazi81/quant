"""安全加固模块（纯标准库，无新依赖，导入即用、不会因缺包导致应用启动失败）。

与 app_state.py 同放项目根，供 main 与各 web 蓝图统一 `from app_security import ...`。

提供：
  - init_security(app): 会话 Cookie 硬化 + 安全响应头（CSP/X-Frame-Options 等）
  - client_ip(): 取真实客户端 IP（部署在 nginx 后，信任 nginx 设置的 X-Real-IP）
  - rate_limit(...): 进程内按键（默认按 IP）固定滑动窗口限流装饰器，线程安全
  - login_locked / login_fail / login_ok: 登录失败计数与锁定（防爆破 / 撞库 / CPU-DoS）

⚠️ SESSION_COOKIE_SECURE 由环境变量控制，默认关闭。仅当站点已上 HTTPS 后再设
   SESSION_COOKIE_SECURE=1，否则浏览器在 HTTP 下不回传 Secure cookie → 全员登录失效。
"""
import os
import re
import sqlite3
import time
import threading
from collections import deque
from datetime import datetime, timedelta
from functools import wraps

from flask import request, jsonify, session  # noqa: F401  (session 供调用方便利导入)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SECURITY_DB = os.environ.get("SECURITY_DB") or os.path.join(BASE_DIR, "data", "users.db")

_ANTI_CRAWL_ENABLED = os.environ.get("ANTI_CRAWL_ENABLED", "1") != "0"
_ANTI_BLOCK_SECONDS = int(os.environ.get("ANTI_CRAWL_BLOCK_SECONDS", "900"))
_ANTI_SENSITIVE_MAX = int(os.environ.get("ANTI_CRAWL_SENSITIVE_MAX", "8"))
_ANTI_AUTH_FAIL_MAX = int(os.environ.get("ANTI_CRAWL_AUTH_FAIL_MAX", "25"))
_ANTI_API_MAX_PER_MIN = int(os.environ.get("ANTI_CRAWL_API_MAX_PER_MIN", "240"))

_BAD_UA_RE = re.compile(
    r"(sqlmap|nikto|acunetix|nessus|masscan|nmap|zgrab|dirbuster|gobuster|wpscan)",
    re.I,
)
_SENSITIVE_PATH_RE = re.compile(
    r"(^|/)(\.git|\.svn|\.hg|\.env|\.aws|\.ssh|id_rsa|users\.db|paper_trading\.db|"
    r"config\.py|secret|secrets|backup|backups|dump|logs?|data)(/|$)|"
    r"(\.sql|\.sqlite|\.db|\.bak|\.old|\.zip|\.tar|\.gz)$",
    re.I,
)
_HIGH_VALUE_PREFIXES = (
    "/api/admin",
    "/api/ai",
    "/api/stocks/score",
    "/api/strategies",
    "/api/factors",
)

_security_tables_ready = False
_security_tables_path = ""
_security_db_lock = threading.Lock()
_suspicious_hits: "dict[str, deque]" = {}
_suspicious_lock = threading.Lock()
_ip_blocks: "dict[str, tuple[float, str]]" = {}
_ip_blocks_lock = threading.Lock()


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def client_ip() -> str:
    """真实客户端 IP。

    生产部署在 nginx 反代后：nginx 已 `proxy_set_header X-Real-IP $remote_addr`，
    该值由可信代理写入、客户端无法伪造，优先采用。本地直连时回退 remote_addr。
    """
    xri = request.headers.get("X-Real-IP")
    if xri:
        return xri.strip()
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "0.0.0.0"


def user_key() -> str:
    """限流键：优先按登录用户 ID（每用户独立配额），未登录回退 IP。

    用于给 AI 等高成本接口加「按用户」的速率上限——积分是主闸，这是防止
    单个高余额/被盗号账号短时间狂刷、烧光全局 AI key 预算的兜底。
    """
    try:
        uid = session.get("user_id")
    except Exception:
        uid = None
    return f"uid:{uid}" if uid else f"ip:{client_ip()}"


# ══════════════════════════════════════════════════════════════
# 安全初始化：Cookie 硬化 + 安全响应头
# ══════════════════════════════════════════════════════════════
def _security_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(SECURITY_DB), exist_ok=True)
    conn = sqlite3.connect(SECURITY_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_security_tables() -> None:
    global _security_tables_ready, _security_tables_path
    if _security_tables_ready and _security_tables_path == SECURITY_DB:
        return
    with _security_db_lock:
        if _security_tables_ready and _security_tables_path == SECURITY_DB:
            return
        with _security_conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS security_events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type  TEXT NOT NULL,
                    severity    TEXT DEFAULT 'low',
                    ip          TEXT DEFAULT '',
                    user_id     INTEGER,
                    method      TEXT DEFAULT '',
                    path        TEXT DEFAULT '',
                    status_code INTEGER,
                    user_agent  TEXT DEFAULT '',
                    detail      TEXT DEFAULT '',
                    created_at  TEXT DEFAULT (datetime('now','localtime'))
                );
                CREATE INDEX IF NOT EXISTS idx_sec_events_created ON security_events(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_sec_events_ip ON security_events(ip, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_sec_events_type ON security_events(event_type, created_at DESC);

                CREATE TABLE IF NOT EXISTS ip_blocks (
                    ip            TEXT PRIMARY KEY,
                    reason        TEXT DEFAULT '',
                    blocked_until REAL NOT NULL,
                    created_at    TEXT DEFAULT (datetime('now','localtime')),
                    updated_at    TEXT DEFAULT (datetime('now','localtime'))
                );
                CREATE INDEX IF NOT EXISTS idx_ip_blocks_until ON ip_blocks(blocked_until);
                """
            )
            conn.commit()
        _security_tables_ready = True
        _security_tables_path = SECURITY_DB


def _request_value(getter, default=None):
    try:
        return getter()
    except Exception:
        return default


def _current_request_user_id():
    return _request_value(lambda: session.get("user_id"))


def record_security_event(event_type: str, severity: str = "low", ip: str = "",
                          user_id=None, method: str = "", path: str = "",
                          status_code=None, user_agent: str = "", detail: str = "") -> None:
    try:
        _ensure_security_tables()
        ip = ip or _request_value(client_ip, "")
        user_id = user_id if user_id is not None else _current_request_user_id()
        method = method or _request_value(lambda: request.method, "")
        path = path or _request_value(lambda: request.path, "")
        user_agent = user_agent or _request_value(lambda: request.headers.get("User-Agent", ""), "")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _security_conn() as conn:
            conn.execute(
                """
                INSERT INTO security_events
                (event_type,severity,ip,user_id,method,path,status_code,user_agent,detail,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(event_type)[:64],
                    str(severity or "low")[:16],
                    str(ip or "")[:64],
                    user_id,
                    str(method or "")[:16],
                    str(path or "")[:512],
                    status_code,
                    str(user_agent or "")[:512],
                    str(detail or "")[:1000],
                    now,
                ),
            )
            conn.commit()
    except Exception:
        pass


def list_security_events(limit: int = 200, ip: str = "", event_type: str = "") -> list[dict]:
    _ensure_security_tables()
    limit = max(1, min(int(limit or 200), 1000))
    where = []
    args = []
    if ip:
        where.append("ip=?")
        args.append(ip)
    if event_type:
        where.append("event_type=?")
        args.append(event_type)
    sql = "SELECT * FROM security_events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _security_conn() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def block_ip(ip: str, seconds: int | None = None, reason: str = "") -> None:
    if not ip:
        return
    _ensure_security_tables()
    seconds = int(seconds or _ANTI_BLOCK_SECONDS)
    until = time.time() + max(1, seconds)
    reason = reason or "blocked"
    with _ip_blocks_lock:
        _ip_blocks[ip] = (until, reason)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _security_conn() as conn:
        conn.execute(
            """
            INSERT INTO ip_blocks (ip,reason,blocked_until,created_at,updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(ip) DO UPDATE SET
              reason=excluded.reason,
              blocked_until=excluded.blocked_until,
              updated_at=excluded.updated_at
            """,
            (ip, reason, until, now, now),
        )
        conn.commit()


def release_ip_block(ip: str) -> None:
    if not ip:
        return
    _ensure_security_tables()
    with _ip_blocks_lock:
        _ip_blocks.pop(ip, None)
    with _security_conn() as conn:
        conn.execute("DELETE FROM ip_blocks WHERE ip=?", (ip,))
        conn.commit()


def is_ip_blocked(ip: str) -> tuple[bool, int, str]:
    if not ip:
        return False, 0, ""
    now = time.time()
    with _ip_blocks_lock:
        cached = _ip_blocks.get(ip)
        if cached:
            until, reason = cached
            if until > now:
                return True, int(until - now), reason
            _ip_blocks.pop(ip, None)
    try:
        _ensure_security_tables()
        with _security_conn() as conn:
            row = conn.execute(
                "SELECT reason,blocked_until FROM ip_blocks WHERE ip=?", (ip,)
            ).fetchone()
            if not row:
                return False, 0, ""
            until = float(row["blocked_until"])
            reason = row["reason"] or ""
            if until <= now:
                conn.execute("DELETE FROM ip_blocks WHERE ip=?", (ip,))
                conn.commit()
                return False, 0, ""
            with _ip_blocks_lock:
                _ip_blocks[ip] = (until, reason)
            return True, int(until - now), reason
    except Exception:
        return False, 0, ""


def list_ip_blocks() -> list[dict]:
    _ensure_security_tables()
    now = time.time()
    with _security_conn() as conn:
        conn.execute("DELETE FROM ip_blocks WHERE blocked_until<=?", (now,))
        conn.commit()
        rows = conn.execute(
            "SELECT ip,reason,blocked_until,created_at,updated_at FROM ip_blocks "
            "ORDER BY blocked_until DESC"
        ).fetchall()
    items = []
    for r in rows:
        item = dict(r)
        item["retry_after"] = max(0, int(float(item["blocked_until"]) - now))
        items.append(item)
    return items


def _count_suspicious(key: str, window_seconds: int = 300) -> int:
    now = time.monotonic()
    with _suspicious_lock:
        dq = _suspicious_hits.setdefault(key, deque())
        cutoff = now - window_seconds
        while dq and dq[0] < cutoff:
            dq.popleft()
        dq.append(now)
        return len(dq)


def _is_high_value_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _HIGH_VALUE_PREFIXES)


def init_security(app):
    secure = _truthy(os.environ.get("SESSION_COOKIE_SECURE", "0"))
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,                 # JS 读不到 session cookie，挡 XSS 窃取
        SESSION_COOKIE_SAMESITE="Lax",                # 跨站 POST 不带 cookie，挡大部分 CSRF
        SESSION_COOKIE_SECURE=secure,                 # 仅 HTTPS 就绪后置 1（见文件头警告）
        PERMANENT_SESSION_LIFETIME=timedelta(days=int(os.environ.get("SESSION_DAYS", "30"))),
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,          # 请求体上限 10MB（与 nginx 一致，双保险）
    )

    # CSP：精确放行前端实际用到的三个外部源（Google Fonts / cdnjs FontAwesome /
    # jsdelivr echarts），其余外部脚本/样式一律拦截。因前端是大量内联的单文件，
    # 保留 'unsafe-inline'/'unsafe-eval'；即便如此仍能挡掉 <script src=evil> 注入。
    csp = (
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com; "
        "font-src 'self' data: https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net; "
        "connect-src 'self'; "
        "frame-ancestors 'self'; "
        "base-uri 'self'; "
        "object-src 'none'"
    )

    @app.before_request
    def _anti_crawl_guard():
        if not _ANTI_CRAWL_ENABLED:
            return None
        ip = client_ip()
        path = request.path or ""
        ua = request.headers.get("User-Agent", "") or ""

        blocked, retry_after, reason = is_ip_blocked(ip)
        if blocked:
            return jsonify({
                "ok": False,
                "error": "access temporarily blocked",
                "code": 403,
                "retry_after": retry_after,
                "reason": reason,
            }), 403

        if _SENSITIVE_PATH_RE.search(path):
            record_security_event(
                "sensitive_path",
                severity="high",
                ip=ip,
                path=path,
                method=request.method,
                user_agent=ua,
                status_code=404,
            )
            if _count_suspicious(f"sensitive:{ip}") >= _ANTI_SENSITIVE_MAX:
                block_ip(ip, _ANTI_BLOCK_SECONDS, "sensitive path probing")
            return jsonify({"ok": False, "error": "not found", "code": 404}), 404

        if ua and _BAD_UA_RE.search(ua):
            record_security_event(
                "bad_user_agent",
                severity="high",
                ip=ip,
                path=path,
                method=request.method,
                user_agent=ua,
                status_code=403,
            )
            block_ip(ip, _ANTI_BLOCK_SECONDS, "offensive user agent")
            return jsonify({"ok": False, "error": "forbidden", "code": 403}), 403

        if path.startswith("/api/") and not _hit(f"anti-api:{ip}", _ANTI_API_MAX_PER_MIN, 60):
            record_security_event(
                "rate_limited",
                severity="medium",
                ip=ip,
                path=path,
                method=request.method,
                user_agent=ua,
                status_code=429,
                detail="api coarse limit",
            )
            return jsonify({"ok": False, "error": "too many requests", "code": 429}), 429

        return None

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Content-Security-Policy", csp)
        if secure:
            resp.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        if _ANTI_CRAWL_ENABLED:
            try:
                ip = client_ip()
                path = request.path or ""
                if resp.status_code in (401, 403) and _is_high_value_path(path):
                    event_type = "unauthorized_probe" if resp.status_code == 401 else "forbidden_probe"
                    record_security_event(
                        event_type,
                        severity="medium",
                        ip=ip,
                        path=path,
                        method=request.method,
                        user_agent=request.headers.get("User-Agent", "") or "",
                        status_code=resp.status_code,
                    )
                    if _count_suspicious(f"auth:{ip}") >= _ANTI_AUTH_FAIL_MAX:
                        block_ip(ip, _ANTI_BLOCK_SECONDS, "repeated unauthorized probing")
            except Exception:
                pass
        return resp

    return app


# ══════════════════════════════════════════════════════════════
# 进程内限流（固定滑动窗口，线程安全）
# 单 worker 部署下足够；将来多 worker 需外置到 Redis。
# ══════════════════════════════════════════════════════════════
_buckets: "dict[str, deque]" = {}
_buckets_lock = threading.Lock()
_last_sweep = [0.0]


def _hit(key: str, max_calls: int, per_seconds: float) -> bool:
    """返回 True=放行 / False=超限。滑动窗口计数。"""
    now = time.monotonic()
    with _buckets_lock:
        # 周期性清理空桶，防止字典无限增长（每 5 分钟一次）
        if now - _last_sweep[0] > 300:
            for k in [k for k, dq in _buckets.items() if not dq or dq[-1] < now - 3600]:
                _buckets.pop(k, None)
            _last_sweep[0] = now
        dq = _buckets.get(key)
        if dq is None:
            dq = deque()
            _buckets[key] = dq
        cutoff = now - per_seconds
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= max_calls:
            return False
        dq.append(now)
        return True


def rate_limit(max_calls: int, per_seconds: float, scope: str = "",
               key_func=None, message: str = "请求过于频繁，请稍后再试"):
    """按 (scope:函数名:key) 限流的装饰器。默认按客户端 IP。

    用法：
        @bp.route(...)
        @rate_limit(20, 300, scope="auth")   # 同键 5 分钟最多 20 次
        @require_auth
        def view(): ...
    """
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            try:
                k = key_func() if key_func else client_ip()
            except Exception:
                k = "unknown"
            if not _hit(f"{scope}:{f.__name__}:{k}", max_calls, per_seconds):
                return jsonify({"ok": False, "error": message, "code": 429}), 429
            return f(*a, **kw)
        return wrapper
    return deco


# ══════════════════════════════════════════════════════════════
# 登录防爆破（按 用户名+IP 计数与锁定）
# ══════════════════════════════════════════════════════════════
_LOGIN_MAX_FAILS = int(os.environ.get("LOGIN_MAX_FAILS", "5"))
_LOGIN_WINDOW    = int(os.environ.get("LOGIN_FAIL_WINDOW", "900"))    # 15 分钟内累计
_LOGIN_LOCK      = int(os.environ.get("LOGIN_LOCK_SECONDS", "900"))   # 触发后锁 15 分钟
_ROOT_LOGIN_MAX_FAILS = int(os.environ.get("ROOT_LOGIN_MAX_FAILS", "3"))
_ROOT_LOGIN_WINDOW    = int(os.environ.get("ROOT_LOGIN_FAIL_WINDOW", "900"))
_ROOT_LOGIN_LOCK      = int(os.environ.get("ROOT_LOGIN_LOCK_SECONDS", "1800"))

_fail_log: "dict[str, deque]" = {}
_lock_until: "dict[str, float]" = {}
_login_lock = threading.Lock()


def login_locked(ident: str) -> int:
    """返回剩余锁定秒数；0=未锁。"""
    now = time.monotonic()
    with _login_lock:
        until = _lock_until.get(ident, 0)
        if until > now:
            return int(until - now)
        if until:
            _lock_until.pop(ident, None)
        return 0


def _login_fail_with_policy(ident: str, max_fails: int, window: int, lock_seconds: int) -> None:
    """记一次登录失败；窗口内累计达阈值则锁定。"""
    now = time.monotonic()
    with _login_lock:
        dq = _fail_log.setdefault(ident, deque())
        cutoff = now - window
        while dq and dq[0] < cutoff:
            dq.popleft()
        dq.append(now)
        if len(dq) >= max_fails:
            _lock_until[ident] = now + lock_seconds
            dq.clear()


def login_fail(ident: str) -> None:
    _login_fail_with_policy(ident, _LOGIN_MAX_FAILS, _LOGIN_WINDOW, _LOGIN_LOCK)


def root_login_fail(ident: str) -> None:
    _login_fail_with_policy(ident, _ROOT_LOGIN_MAX_FAILS, _ROOT_LOGIN_WINDOW, _ROOT_LOGIN_LOCK)


def login_ok(ident: str) -> None:
    """登录成功，清空该标识的失败记录与锁定。"""
    with _login_lock:
        _fail_log.pop(ident, None)
        _lock_until.pop(ident, None)
