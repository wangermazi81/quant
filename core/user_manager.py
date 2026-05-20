"""
用户管理系统 — 注册 / 登录 / 积分管理
数据库: data/users.db  (SQLite，WAL 模式)
"""
import sqlite3, hashlib, os, threading
from typing import Optional, Dict

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "users.db")

# ── 积分消耗配置 ──────────────────────────────────────────
POINT_COSTS = {
    "ai_analysis": 10,   # AI 深度分析
    "backtest":     5,   # 策略回测
    "scan":         2,   # 全市场选股扫描
    "paper_trade":  1,   # 模拟盘下单（每笔）
}
REGISTER_BONUS = 100    # 新用户注册赠送积分

_lock = threading.Lock()


# ── 数据库初始化 ──────────────────────────────────────────
def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT    UNIQUE NOT NULL,
            email         TEXT    UNIQUE NOT NULL,
            password_hash TEXT    NOT NULL,
            points        INTEGER DEFAULT 100,
            is_admin      INTEGER DEFAULT 0,
            is_active     INTEGER DEFAULT 1,
            created_at    TEXT    DEFAULT (datetime('now','localtime')),
            last_login    TEXT
        );
        CREATE TABLE IF NOT EXISTS point_transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            amount      INTEGER NOT NULL,
            type        TEXT    NOT NULL,
            description TEXT,
            balance     INTEGER NOT NULL,
            created_at  TEXT    DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        """)
        c.commit()
        # 没有管理员时自动创建默认账号
        if not c.execute("SELECT id FROM users WHERE is_admin=1").fetchone():
            _create_user(c, "admin", "admin@local.com", "admin123",
                         is_admin=1, points=999999)


def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def _create_user(c, username, email, password,
                 is_admin=0, points=REGISTER_BONUS):
    c.execute(
        "INSERT INTO users (username,email,password_hash,points,is_admin)"
        " VALUES (?,?,?,?,?)",
        (username, email, _hash(password), points, is_admin)
    )
    c.commit()
    uid = c.execute("SELECT id FROM users WHERE username=?",
                    (username,)).fetchone()["id"]
    c.execute(
        "INSERT INTO point_transactions"
        " (user_id,amount,type,description,balance) VALUES (?,?,?,?,?)",
        (uid, points, "register", "注册赠送", points)
    )
    c.commit()


# ── 公开接口 ──────────────────────────────────────────────
def register(username: str, email: str, password: str) -> dict:
    username = username.strip()
    email    = email.strip().lower()
    if len(username) < 2:
        return {"ok": False, "error": "用户名至少 2 个字符"}
    if len(password) < 6:
        return {"ok": False, "error": "密码至少 6 位"}
    if "@" not in email:
        return {"ok": False, "error": "邮箱格式错误"}
    try:
        with _lock, _conn() as c:
            _create_user(c, username, email, password)
        return {"ok": True}
    except sqlite3.IntegrityError as e:
        msg = "用户名已存在" if "username" in str(e) else "邮箱已注册"
        return {"ok": False, "error": msg}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def login(username: str, password: str) -> dict:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM users WHERE username=? AND password_hash=?",
            (username.strip(), _hash(password))
        ).fetchone()
        if not row:
            return {"ok": False, "error": "用户名或密码错误"}
        if not row["is_active"]:
            return {"ok": False, "error": "账号已被禁用，请联系管理员"}
        c.execute(
            "UPDATE users SET last_login=datetime('now','localtime') WHERE id=?",
            (row["id"],)
        )
        c.commit()
        return {
            "ok":       True,
            "user_id":  row["id"],
            "username": row["username"],
            "points":   row["points"],
            "is_admin": bool(row["is_admin"]),
        }


def get_user(user_id: int) -> Optional[Dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def change_password(user_id: int, old_password: str, new_password: str) -> dict:
    if len(new_password) < 6:
        return {"ok": False, "error": "新密码至少 6 位"}
    with _lock, _conn() as c:
        row = c.execute("SELECT password_hash FROM users WHERE id=?",
                        (user_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "用户不存在"}
        if row["password_hash"] != _hash(old_password):
            return {"ok": False, "error": "原密码错误"}
        c.execute("UPDATE users SET password_hash=? WHERE id=?",
                  (_hash(new_password), user_id))
        c.commit()
        return {"ok": True}


def admin_reset_password(user_id: int, new_password: str) -> dict:
    """管理员直接重置任意用户密码（无需原密码）"""
    if len(new_password) < 6:
        return {"ok": False, "error": "新密码至少 6 位"}
    with _lock, _conn() as c:
        row = c.execute("SELECT username FROM users WHERE id=?",
                        (user_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "用户不存在"}
        c.execute("UPDATE users SET password_hash=? WHERE id=?",
                  (_hash(new_password), user_id))
        c.commit()
        return {"ok": True, "username": row["username"]}


def get_points(user_id: int) -> int:
    with _conn() as c:
        row = c.execute("SELECT points FROM users WHERE id=?",
                        (user_id,)).fetchone()
        return row["points"] if row else 0


def deduct_points(user_id: int, action: str,
                  description: str = "") -> dict:
    cost = POINT_COSTS.get(action, 0)
    if cost <= 0:
        return {"ok": True, "cost": 0, "remaining": get_points(user_id)}
    with _lock, _conn() as c:
        row = c.execute("SELECT points FROM users WHERE id=?",
                        (user_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "用户不存在"}
        if row["points"] < cost:
            return {
                "ok": False,
                "error": f"积分不足（需要 {cost} 分，当前 {row['points']} 分）",
                "cost": cost,
                "remaining": row["points"],
            }
        new_bal = row["points"] - cost
        c.execute("UPDATE users SET points=? WHERE id=?", (new_bal, user_id))
        c.execute(
            "INSERT INTO point_transactions"
            " (user_id,amount,type,description,balance) VALUES (?,?,?,?,?)",
            (user_id, -cost, action, description or action, new_bal)
        )
        c.commit()
        return {"ok": True, "cost": cost, "remaining": new_bal}


def add_points(user_id: int, amount: int,
               description: str = "管理员充值") -> dict:
    if amount <= 0:
        return {"ok": False, "error": "充值积分必须大于 0"}
    with _lock, _conn() as c:
        row = c.execute("SELECT points,username FROM users WHERE id=?",
                        (user_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "用户不存在"}
        new_bal = row["points"] + amount
        c.execute("UPDATE users SET points=? WHERE id=?", (new_bal, user_id))
        c.execute(
            "INSERT INTO point_transactions"
            " (user_id,amount,type,description,balance) VALUES (?,?,?,?,?)",
            (user_id, amount, "admin_add", description, new_bal)
        )
        c.commit()
        return {"ok": True, "new_balance": new_bal,
                "username": row["username"]}


def get_point_history(user_id: int, limit: int = 50) -> list:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM point_transactions"
            " WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def list_users(page: int = 1, per_page: int = 20,
               search: str = "") -> dict:
    with _conn() as c:
        if search:
            where  = "WHERE username LIKE ? OR email LIKE ?"
            params = (f"%{search}%", f"%{search}%")
        else:
            where, params = "", ()
        total = c.execute(
            f"SELECT COUNT(*) FROM users {where}", params
        ).fetchone()[0]
        rows = c.execute(
            f"SELECT id,username,email,points,is_admin,is_active,"
            f"created_at,last_login FROM users {where}"
            f" ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, per_page, (page - 1) * per_page)
        ).fetchall()
        return {"total": total, "page": page,
                "per_page": per_page, "users": [dict(r) for r in rows]}


def all_point_transactions(page: int = 1, per_page: int = 30) -> dict:
    with _conn() as c:
        total = c.execute(
            "SELECT COUNT(*) FROM point_transactions"
        ).fetchone()[0]
        rows = c.execute(
            "SELECT pt.*,u.username FROM point_transactions pt"
            " JOIN users u ON u.id=pt.user_id"
            " ORDER BY pt.id DESC LIMIT ? OFFSET ?",
            (per_page, (page - 1) * per_page)
        ).fetchall()
        return {"total": total, "page": page,
                "per_page": per_page, "records": [dict(r) for r in rows]}


def set_active(user_id: int, active: bool) -> dict:
    with _conn() as c:
        c.execute("UPDATE users SET is_active=? WHERE id=?",
                  (1 if active else 0, user_id))
        c.commit()
        return {"ok": True}


def set_admin(user_id: int, is_admin: bool) -> dict:
    with _conn() as c:
        c.execute("UPDATE users SET is_admin=? WHERE id=?",
                  (1 if is_admin else 0, user_id))
        c.commit()
        return {"ok": True}


def stats() -> dict:
    """管理员概览数据"""
    with _conn() as c:
        total_users   = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active_users  = c.execute(
            "SELECT COUNT(*) FROM users WHERE is_active=1").fetchone()[0]
        total_points  = c.execute(
            "SELECT COALESCE(SUM(points),0) FROM users").fetchone()[0]
        today_calls   = c.execute(
            "SELECT COUNT(*) FROM point_transactions"
            " WHERE amount<0 AND created_at>=date('now','localtime')"
        ).fetchone()[0]
        return {
            "total_users":  total_users,
            "active_users": active_users,
            "total_points": total_points,
            "today_calls":  today_calls,
        }


# ══════════════════════════════════════════════════════════════
# 用户运行状态持久化（24/7 服务器端）
# ══════════════════════════════════════════════════════════════

def save_user_settings(user_id: int, **kwargs) -> dict:
    """保存用户运行设置（running/capital/position_ratio 等）"""
    allowed = {
        "running", "auto_execute", "capital", "position_ratio",
        "portfolio_mode", "active_strategy", "stock_pool", "broker_type",
    }
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return {"ok": True}
    set_clause = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [user_id]
    with _lock, _conn() as c:
        c.execute(
            f"INSERT OR IGNORE INTO user_settings(user_id) VALUES(?)", (user_id,)
        )
        c.execute(
            f"UPDATE user_settings SET {set_clause}, "
            f"updated_at=datetime('now','localtime') WHERE user_id=?",
            vals,
        )
        c.commit()
    return {"ok": True}


def get_user_settings(user_id: int) -> dict:
    """获取用户运行设置，不存在时返回默认值"""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM user_settings WHERE user_id=?", (user_id,)
        ).fetchone()
    if row:
        return dict(row)
    return {
        "user_id":        user_id,
        "running":        0,
        "auto_execute":   0,
        "capital":        500000.0,
        "position_ratio": 0.20,
        "portfolio_mode": "equal_weight",
        "active_strategy": "",
        "stock_pool":     "[]",
        "broker_type":    "paper",
    }


def get_running_users() -> list:
    """返回所有 running=1 的用户 ID（用于服务器重启后恢复）"""
    with _conn() as c:
        rows = c.execute(
            "SELECT user_id FROM user_settings WHERE running=1"
        ).fetchall()
    return [r["user_id"] for r in rows]


# ── 用户日志 ──────────────────────────────────────────────────

def add_user_log(user_id: int, message: str, level: str = "INFO") -> None:
    """写入用户运行日志"""
    with _conn() as c:
        c.execute(
            "INSERT INTO user_logs(user_id,level,message) VALUES(?,?,?)",
            (user_id, level.upper(), message),
        )
        c.commit()


def get_user_logs(user_id: int, limit: int = 100) -> list:
    """获取指定用户的最新日志"""
    with _conn() as c:
        rows = c.execute(
            "SELECT id,level,message,created_at FROM user_logs"
            " WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_logs(page: int = 1, per_page: int = 50,
                 user_id: Optional[int] = None,
                 uid: Optional[int] = None,
                 limit: Optional[int] = None) -> list:
    """管理员查看所有用户日志（支持按用户过滤）。返回 list[dict]。"""
    uid_filter = uid or user_id
    if limit:
        per_page = limit
    with _conn() as c:
        if uid_filter:
            where  = "WHERE ul.user_id=?"
            params = (uid_filter,)
        else:
            where, params = "", ()
        rows = c.execute(
            f"SELECT ul.*,u.username FROM user_logs ul"
            f" JOIN users u ON u.id=ul.user_id"
            f" {where} ORDER BY ul.id DESC LIMIT ? OFFSET ?",
            (*params, per_page, (page - 1) * per_page),
        ).fetchall()
    return [dict(r) for r in rows]


# ── 用户买入队列 ──────────────────────────────────────────────

def add_user_queue_item(user_id: int, code: str, name: str,
                        price: float, shares: int,
                        budget: float, reason: str = "") -> int:
    """向用户队列添加买入项，返回新记录 id"""
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO user_queue(user_id,code,name,price,shares,budget,reason)"
            " VALUES(?,?,?,?,?,?,?)",
            (user_id, code, name, price, shares, budget, reason),
        )
        c.commit()
        return cur.lastrowid


def get_user_queue(user_id: int) -> list:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM user_queue WHERE user_id=? AND status='pending'"
            " ORDER BY id",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def remove_user_queue_item(item_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM user_queue WHERE id=?", (item_id,))
        c.commit()


def clear_user_queue(user_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM user_queue WHERE user_id=?", (user_id,))
        c.commit()


# ── 用户 AI 配置 ──────────────────────────────────────────────

def save_user_ai_config(user_id: int, provider: str = "", api_key: str = "",
                        base_url: str = "", model: str = "",
                        enabled: bool = False) -> dict:
    with _lock, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO user_ai_config"
            "(user_id,provider,api_key,base_url,model,enabled) VALUES(?,?,?,?,?,?)",
            (user_id, provider, api_key, base_url, model, 1 if enabled else 0),
        )
        c.commit()
    return {"ok": True}


def get_user_ai_config(user_id: int) -> Optional[Dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM user_ai_config WHERE user_id=?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


# ── 用户券商配置 ──────────────────────────────────────────────

def save_user_broker_config(user_id: int, broker_type: str,
                             config: dict) -> dict:
    import json
    with _lock, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO user_broker_config(user_id,broker_type,config_json)"
            " VALUES(?,?,?)",
            (user_id, broker_type, json.dumps(config, ensure_ascii=False)),
        )
        c.commit()
    return {"ok": True}


def get_user_broker_config(user_id: int) -> dict:
    import json
    with _conn() as c:
        row = c.execute(
            "SELECT broker_type,config_json FROM user_broker_config WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if not row:
        return {"broker_type": "paper", "config": {}}
    try:
        cfg = json.loads(row["config_json"])
    except Exception:
        cfg = {}
    return {"broker_type": row["broker_type"], "config": cfg}


# ── 公告管理 ──────────────────────────────────────────────────

def create_announcement(title: str, content: str,
                        ann_type: str = "info",
                        created_by: int = 0,
                        expires_at: str = None) -> dict:
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO announcements(title,content,type,created_by,expires_at)"
            " VALUES(?,?,?,?,?)",
            (title, content, ann_type, created_by, expires_at),
        )
        c.commit()
        ann_id = cur.lastrowid
    return {"ok": True, "id": ann_id}


def get_active_announcements(user_id: Optional[int] = None,
                              limit: int = 20) -> list:
    """获取有效公告，若传 user_id 则附带是否已读标记"""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM announcements WHERE is_active=1"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        if user_id:
            read_ids = {
                r["announcement_id"]
                for r in c.execute(
                    "SELECT announcement_id FROM announcement_reads WHERE user_id=?",
                    (user_id,),
                ).fetchall()
            }
        else:
            read_ids = set()
    result = []
    for r in rows:
        d = dict(r)
        d["is_read"] = (d["id"] in read_ids)
        result.append(d)
    return result


def update_announcement(ann_id: int, **kwargs) -> dict:
    allowed = {"title", "content", "type", "is_active", "active", "expires_at"}
    # 前端可能传 "active"，映射到 "is_active"
    if "active" in kwargs and "is_active" not in kwargs:
        kwargs["is_active"] = int(bool(kwargs.pop("active")))
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return {"ok": True}
    set_clause = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [ann_id]
    with _lock, _conn() as c:
        c.execute(
            f"UPDATE announcements SET {set_clause},"
            f" updated_at=datetime('now','localtime') WHERE id=?",
            vals,
        )
        c.commit()
    return {"ok": True}


def is_announcement_read(user_id: int, ann_id: int) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM announcement_reads WHERE user_id=? AND announcement_id=?",
            (user_id, ann_id),
        ).fetchone()
    return row is not None


def mark_announcement_read(user_id: int, ann_id: int) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO announcement_reads(user_id,announcement_id)"
            " VALUES(?,?)",
            (user_id, ann_id),
        )
        c.commit()


def delete_announcement(ann_id: int) -> dict:
    with _lock, _conn() as c:
        c.execute("DELETE FROM announcements WHERE id=?", (ann_id,))
        c.execute("DELETE FROM announcement_reads WHERE announcement_id=?", (ann_id,))
        c.commit()
    return {"ok": True}


def list_all_announcements(page: int = 1, per_page: int = 100) -> list:
    """管理员获取全部公告（含已禁用），返回 list[dict]。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT a.*,u.username as creator FROM announcements a"
            " LEFT JOIN users u ON u.id=a.created_by"
            " ORDER BY a.id DESC LIMIT ? OFFSET ?",
            (per_page, (page - 1) * per_page),
        ).fetchall()
    return [dict(r) for r in rows]
