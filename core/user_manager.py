"""
用户管理系统 — 注册 / 登录 / 积分管理
数据库: data/users.db  (SQLite，WAL 模式)
"""
import sqlite3, hashlib, hmac, os, secrets, threading, logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "users.db")

# ── 积分消耗配置（默认值；管理后台「积分设置」可改，存 data/point_costs.json）──
POINT_COSTS = {
    "ai_analysis":     10,   # AI 深度分析
    "ai_strategy":      5,   # AI 写策略
    "review_generate": 10,   # AI 复盘生成
    "chain_graph":      5,   # AI 产业链图谱
    "backtest":         5,   # 策略回测
    "scan":             2,   # 全市场选股扫描
    "paper_trade":      1,   # 模拟盘下单（每笔）
}
POINT_COST_LABELS = {
    "ai_analysis":     "AI 深度分析",
    "ai_strategy":     "AI 写策略",
    "review_generate": "AI 复盘生成",
    "chain_graph":     "AI 产业链图谱",
    "backtest":        "策略回测",
    "scan":            "全市场选股扫描",
    "paper_trade":     "模拟盘下单（每笔）",
}
_COSTS_FILE = os.path.join(os.path.dirname(DB_PATH), "point_costs.json")


def _load_point_costs():
    """启动时加载管理员保存的积分配置覆盖默认值。"""
    try:
        import json as _json
        if os.path.exists(_COSTS_FILE):
            with open(_COSTS_FILE, encoding="utf-8") as f:
                saved = _json.load(f)
            for k, v in (saved or {}).items():
                if k in POINT_COSTS:
                    POINT_COSTS[k] = max(0, int(v))
    except Exception:
        pass


def get_point_costs() -> dict:
    return dict(POINT_COSTS)


def set_point_costs(costs: dict) -> dict:
    """管理员保存积分配置：内存即时生效 + 落盘持久化。0 = 该功能免费。"""
    import json as _json
    changed = {}
    for k, v in (costs or {}).items():
        if k not in POINT_COSTS:
            continue
        try:
            POINT_COSTS[k] = max(0, int(v))
            changed[k] = POINT_COSTS[k]
        except Exception:
            continue
    try:
        with open(_COSTS_FILE, "w", encoding="utf-8") as f:
            _json.dump(POINT_COSTS, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return {"ok": False, "error": f"保存失败: {e}"}
    return {"ok": True, "costs": dict(POINT_COSTS), "changed": changed}


_load_point_costs()

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
            last_login    TEXT,
            invited_by    INTEGER DEFAULT NULL
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
        # 没有管理员时自动创建默认账号（密码取自环境变量，否则随机生成）
        if not c.execute("SELECT id FROM users WHERE is_admin=1").fetchone():
            admin_email = os.environ.get("ADMIN_EMAIL", "admin@local.com")
            env_pw = os.environ.get("ADMIN_PASSWORD")
            admin_pw = env_pw or secrets.token_urlsafe(12)
            _create_user(c, "admin", admin_email, admin_pw,
                         is_admin=1, points=999999)
            if env_pw:
                logger.warning("已用环境变量 ADMIN_PASSWORD 创建默认管理员 admin")
            else:
                logger.warning("=" * 60)
                logger.warning(f"已创建默认管理员  用户名: admin  初始密码: {admin_pw}")
                logger.warning("请立即登录修改此密码；该密码仅本次启动日志可见。")
                logger.warning("=" * 60)


_PBKDF2_ALGO       = "sha256"
_PBKDF2_ITERATIONS = 240_000


def _hash_password(pw: str) -> str:
    """加盐慢哈希。格式: pbkdf2:sha256$<迭代次数>$<salt_hex>$<hash_hex>"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, pw.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2:{_PBKDF2_ALGO}${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def _is_legacy_hash(stored: str) -> bool:
    """旧格式：无 $ 分隔的纯 sha256 十六进制串。"""
    return bool(stored) and "$" not in stored


def _verify_password(pw: str, stored: str) -> bool:
    """恒定时间校验密码，兼容旧 sha256 与新 pbkdf2 格式。"""
    if not stored:
        return False
    if _is_legacy_hash(stored):
        return hmac.compare_digest(stored, hashlib.sha256(pw.encode()).hexdigest())
    try:
        scheme, iters, salt_hex, hash_hex = stored.split("$", 3)
        algo = scheme.split(":", 1)[1] if ":" in scheme else "sha256"
        dk = hashlib.pbkdf2_hmac(algo, pw.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def _create_user(c, username, email, password,
                 is_admin=0, points=REGISTER_BONUS, invited_by=None):
    c.execute(
        "INSERT INTO users (username,email,password_hash,points,is_admin,invited_by)"
        " VALUES (?,?,?,?,?,?)",
        (username, email, _hash_password(password), points, is_admin, invited_by)
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


# ── 邀请制注册 ────────────────────────────────────────────
INVITE_REWARD         = 100   # 用户码：每成功邀请1人，邀请人得的积分（admin码可自定义）
_INVITE_USER_MAX_USES = 20    # 用户自建邀请码的名额上限（防滥用）
_INVITE_ALPHABET      = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # 去掉易混 I/O/0/1


def _gen_invite_code(n: int = 8) -> str:
    return "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(n))


def create_invite_code(creator_uid: int, max_uses=1, reward=None,
                       note: str = "", is_admin_code: bool = False,
                       expires_at: str = None) -> dict:
    """生成邀请码。
    - 用户码(is_admin_code=False)：reward 强制 INVITE_REWARD、max_uses 夹到 [1,20]、不可设过期，防刷分。
    - admin 码：reward / max_uses / 过期 均可自定义。"""
    try:
        max_uses = int(max_uses or 1)
    except Exception:
        max_uses = 1
    if is_admin_code:
        max_uses = max(1, max_uses)
        try:
            reward = max(0, int(reward)) if reward is not None else INVITE_REWARD
        except Exception:
            reward = INVITE_REWARD
    else:
        max_uses   = max(1, min(max_uses, _INVITE_USER_MAX_USES))
        reward     = INVITE_REWARD
        expires_at = None
    note = (note or "").strip()[:60]
    with _lock, _conn() as c:
        code = None
        for _ in range(6):
            cand = _gen_invite_code()
            try:
                c.execute(
                    "INSERT INTO invite_codes (code,created_by,max_uses,reward,note,expires_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (cand, creator_uid, max_uses, reward, note, expires_at)
                )
                c.commit()
                code = cand
                break
            except sqlite3.IntegrityError:
                continue
        if not code:
            return {"ok": False, "error": "生成失败，请重试"}
    return {"ok": True, "code": code, "max_uses": max_uses, "reward": reward}


def list_invite_codes(creator_uid: int = None) -> list:
    """creator_uid=None → 全部（admin 视角，含生成者用户名）；否则只列该用户生成的。"""
    with _conn() as c:
        if creator_uid is None:
            rows = c.execute(
                "SELECT ic.*, u.username AS creator_name FROM invite_codes ic"
                " LEFT JOIN users u ON u.id=ic.created_by ORDER BY ic.id DESC"
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM invite_codes WHERE created_by=? ORDER BY id DESC",
                (creator_uid,)
            ).fetchall()
        return [dict(r) for r in rows]


def revoke_invite_code(code_id: int, creator_uid: int = None) -> dict:
    """作废邀请码。creator_uid 非空时只能作废自己生成的（用户侧）。"""
    with _lock, _conn() as c:
        if creator_uid is None:
            cur = c.execute("UPDATE invite_codes SET is_active=0 WHERE id=?", (code_id,))
        else:
            cur = c.execute("UPDATE invite_codes SET is_active=0 WHERE id=? AND created_by=?",
                            (code_id, creator_uid))
        c.commit()
        if cur.rowcount == 0:
            return {"ok": False, "error": "邀请码不存在或无权操作"}
    return {"ok": True}


def get_invite_summary(creator_uid: int) -> dict:
    """用户邀请概览：生成码数 / 累计成功邀请人数 / 累计奖励积分。"""
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS codes, COALESCE(SUM(used_count),0) AS invited"
            " FROM invite_codes WHERE created_by=?", (creator_uid,)
        ).fetchone()
        earned = c.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM point_transactions"
            " WHERE user_id=? AND type='invite_reward'", (creator_uid,)
        ).fetchone()["s"]
        return {"codes": row["codes"], "invited": row["invited"], "earned": earned,
                "reward_per_invite": INVITE_REWARD}


# ── 公开接口 ──────────────────────────────────────────────
def register(username: str, email: str, password: str, invite_code: str = "") -> dict:
    username    = username.strip()
    email       = email.strip().lower()
    invite_code = (invite_code or "").strip().upper()
    if len(username) < 2:
        return {"ok": False, "error": "用户名至少 2 个字符"}
    if len(password) < 6:
        return {"ok": False, "error": "密码至少 6 位"}
    if "@" not in email:
        return {"ok": False, "error": "邮箱格式错误"}
    if not invite_code:
        return {"ok": False, "error": "请输入邀请码（本平台仅限邀请注册）"}
    try:
        with _lock, _conn() as c:
            ic = c.execute("SELECT * FROM invite_codes WHERE code=?", (invite_code,)).fetchone()
            if not ic or not ic["is_active"]:
                return {"ok": False, "error": "邀请码无效"}
            if ic["expires_at"]:
                try:
                    from datetime import datetime as _dt
                    if str(ic["expires_at"]) < _dt.now().strftime("%Y-%m-%d %H:%M:%S"):
                        return {"ok": False, "error": "邀请码已过期"}
                except Exception:
                    pass
            if ic["used_count"] >= ic["max_uses"]:
                return {"ok": False, "error": "邀请码名额已用完"}
            inviter = ic["created_by"]
            # 创建用户（记录邀请人）
            _create_user(c, username, email, password, invited_by=inviter)
            # 消费一个名额
            c.execute("UPDATE invite_codes SET used_count=used_count+1 WHERE id=?", (ic["id"],))
            # 奖励邀请人积分
            reward = int(ic["reward"] or 0)
            if reward > 0 and inviter:
                prow = c.execute("SELECT points FROM users WHERE id=?", (inviter,)).fetchone()
                if prow:
                    new_bal = prow["points"] + reward
                    c.execute("UPDATE users SET points=? WHERE id=?", (new_bal, inviter))
                    c.execute(
                        "INSERT INTO point_transactions (user_id,amount,type,description,balance)"
                        " VALUES (?,?,?,?,?)",
                        (inviter, reward, "invite_reward", f"邀请新用户「{username}」奖励", new_bal)
                    )
            c.commit()
        return {"ok": True}
    except sqlite3.IntegrityError as e:
        msg = "用户名已存在" if "username" in str(e) else "邮箱已注册"
        return {"ok": False, "error": msg}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def login(username: str, password: str) -> dict:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM users WHERE username=?", (username.strip(),)
        ).fetchone()
        if not row or not _verify_password(password, row["password_hash"]):
            return {"ok": False, "error": "用户名或密码错误"}
        if not row["is_active"]:
            return {"ok": False, "error": "账号已被禁用，请联系管理员"}
        # 旧 sha256 哈希命中 → 透明升级为加盐慢哈希
        if _is_legacy_hash(row["password_hash"]):
            try:
                c.execute("UPDATE users SET password_hash=? WHERE id=?",
                          (_hash_password(password), row["id"]))
            except Exception as _e:
                logger.warning(f"密码哈希升级失败 uid={row['id']}: {_e}")
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
        if not _verify_password(old_password, row["password_hash"]):
            return {"ok": False, "error": "原密码错误"}
        c.execute("UPDATE users SET password_hash=? WHERE id=?",
                  (_hash_password(new_password), user_id))
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
                  (_hash_password(new_password), user_id))
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


def set_points(user_id: int, new_value, description: str = "管理员修改积分") -> dict:
    """把用户积分直接设为指定值（可增可减），并按差额记一条流水。"""
    try:
        new_value = int(new_value)
    except (TypeError, ValueError):
        return {"ok": False, "error": "积分必须是整数"}
    if new_value < 0:
        return {"ok": False, "error": "积分不能为负"}
    with _lock, _conn() as c:
        row = c.execute("SELECT points,username FROM users WHERE id=?",
                        (user_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "用户不存在"}
        old   = row["points"]
        delta = new_value - old
        c.execute("UPDATE users SET points=? WHERE id=?", (new_value, user_id))
        c.execute(
            "INSERT INTO point_transactions"
            " (user_id,amount,type,description,balance) VALUES (?,?,?,?,?)",
            (user_id, delta, "admin_set", f"{description}（{old}→{new_value}）", new_value)
        )
        c.commit()
        return {"ok": True, "new_balance": new_value, "old_balance": old,
                "username": row["username"]}


def delete_user(user_id: int) -> dict:
    """删除用户及其在 users.db 中的所有关联数据（动态扫描所有含 user_id 列的表）。"""
    with _lock, _conn() as c:
        row = c.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "用户不存在"}
        uname = row["username"]
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tables:
            if t == "users":
                continue
            try:
                cols = [ci[1] for ci in c.execute(f"PRAGMA table_info({t})")]
                if "user_id" in cols:
                    c.execute(f"DELETE FROM {t} WHERE user_id=?", (user_id,))
            except Exception:
                pass
        c.execute("DELETE FROM users WHERE id=?", (user_id,))
        c.commit()
        return {"ok": True, "username": uname}


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


def delete_user_ai_config(user_id: int) -> dict:
    """删除用户 per-user AI 配置行。

    背景：网页『AI设置』只写全局配置，per-user 表是历史遗留且会顶掉全局新 key 导致 401。
    保存全局配置时调用本函数清掉对应用户的残留，确保全局配置成为唯一来源、永不再被旧 key 顶。
    """
    with _lock, _conn() as c:
        c.execute("DELETE FROM user_ai_config WHERE user_id=?", (user_id,))
        c.commit()
    return {"ok": True}


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
            " AND (expires_at IS NULL OR expires_at='' "
            "      OR expires_at > datetime('now','localtime'))"
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


# ── 实盘信号同步队列 ──────────────────────────────────────────────

# ⚠️ 信号时效约定：交易信号只在生成当天有效。
# 隔日未完成（pending/sent/submitted）的信号视为僵尸信号 —— 交易机已不会再执行，
# 但旧版查询不带时间界限，导致：①资金检查把历史信号金额无限累加
# （曾出现"已用仓位9848%"封死开仓）；②同名票永久去重屏蔽；③重连后按过期价格补单。
# 因此：所有未完成态查询一律限定当天 + expire_stale_signals() 把隔日遗留批量置 expired。

def expire_stale_signals(user_id: int = None) -> int:
    """把今天以前仍处于 pending/sent/submitted 的僵尸信号批量置为 expired。
    user_id=None 时清理全部用户。返回清理条数。"""
    with _lock, _conn() as c:
        sql = ("UPDATE trade_signals SET status='expired', "
               "msg='隔日自动过期（未成交）', updated_at=datetime('now','localtime') "
               "WHERE status IN ('pending','sent','submitted') "
               "AND date(created_at) < date('now','localtime')")
        if user_id is not None:
            cur = c.execute(sql + " AND user_id=?", (user_id,))
        else:
            cur = c.execute(sql)
        c.commit()
        return cur.rowcount


def add_trade_signal(user_id: int, code: str, name: str, action: str,
                     price: float, shares: int, status: str = "pending",
                     msg: str = "") -> int:
    """写入一条新的交易信号，返回其自增 ID。
    去重：同一只股当天如有 pending/sent/submitted 状态的同方向信号，跳过不重复写入。"""
    with _lock, _conn() as c:
        # 去重检查：同 code + 同 action + 当天未完成状态 → 跳过（历史僵尸信号不参与去重）
        dup = c.execute(
            "SELECT id FROM trade_signals WHERE user_id=? AND code=? AND action=? "
            "AND status IN ('pending','sent','submitted') "
            "AND date(created_at) = date('now','localtime') LIMIT 1",
            (user_id, code, action.upper()),
        ).fetchone()
        if dup:
            return -1   # 重复信号，返回 -1 表示跳过
        cur = c.execute(
            "INSERT INTO trade_signals(user_id,code,name,action,price,shares,status,msg)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (user_id, code, name, action.upper(), price, shares, status, msg),
        )
        c.commit()
        return cur.lastrowid


def get_pending_signals(user_id: int) -> list:
    """拉取用户当天处于 pending 状态的实盘信号，并原子性地标记为 sent 防止重复拉取。
    只发当天的：隔日遗留信号价格已失效，绝不能补发给交易机执行。"""
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT * FROM trade_signals WHERE user_id=? AND status='pending' "
            "AND date(created_at) = date('now','localtime') ORDER BY id",
            (user_id,),
        ).fetchall()
        res = [dict(r) for r in rows]
        if res:
            ids = [r["id"] for r in res]
            placeholders = ", ".join("?" for _ in ids)
            c.execute(
                f"UPDATE trade_signals SET status='sent', updated_at=datetime('now','localtime') "
                f"WHERE id IN ({placeholders})",
                ids
            )
            c.commit()
        return res


def get_pending_buy_total(user_id: int) -> float:
    """统计该用户当天未完成买单（pending/sent/submitted）的总金额，用于资金检查。
    限定当天：历史僵尸信号曾把比率累加到9848%，把开新仓永久封死。"""
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(price * shares), 0) FROM trade_signals "
            "WHERE user_id=? AND action='BUY' AND status IN ('pending','sent','submitted') "
            "AND date(created_at) = date('now','localtime')",
            (user_id,),
        ).fetchone()
        return float(row[0]) if row else 0.0


def count_today_open_codes(user_id: int) -> int:
    """今日已开仓（或在途开仓）的股票数：当天 BUY 信号里
    pending/sent/submitted/executed 状态按 code 去重计数。
    供策略"每日最多开 N 仓"限额使用——已报未成交的也占名额，防止超开。"""
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT COUNT(DISTINCT code) FROM trade_signals "
            "WHERE user_id=? AND action='BUY' "
            "AND status IN ('pending','sent','submitted','executed') "
            "AND date(created_at) = date('now','localtime')",
            (user_id,),
        ).fetchone()
        return int(row[0]) if row else 0


def has_pending_signal(user_id: int, code: str, action: str) -> bool:
    """检查该股当天是否已有未完成的同方向信号（去重用）"""
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT 1 FROM trade_signals WHERE user_id=? AND code=? AND action=? "
            "AND status IN ('pending','sent','submitted') "
            "AND date(created_at) = date('now','localtime') LIMIT 1",
            (user_id, code, action.upper()),
        ).fetchone()
        return row is not None


def get_trade_signal(signal_id: int) -> dict | None:
    """查询单条交易信号的当前状态（前端轮询用）"""
    with _conn() as c:
        row = c.execute("SELECT * FROM trade_signals WHERE id=?", (signal_id,)).fetchone()
    return dict(row) if row else None


def report_trade_signal(signal_id: int, status: str, msg: str = "") -> dict:
    """本地交易机上报交易执行结果，更新信号状态与反馈消息"""
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM trade_signals WHERE id=?", (signal_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "信号不存在"}
        c.execute(
            "UPDATE trade_signals SET status=?, msg=?, updated_at=datetime('now','localtime') WHERE id=?",
            (status, msg, signal_id),
        )
        c.commit()
        return {
            "ok": True,
            "user_id": row["user_id"],
            "code": row["code"],
            "name": row["name"],
            "action": row["action"],
            "price": row["price"],
            "shares": row["shares"],
        }


# ── 实盘持仓台账（live_positions）──────────────────────────────
# 实盘下单成交确认时记入；打板 Ride 卖出规则读它来判断止损/回落/吃连板。

def record_live_position(user_id: int, code: str, name: str, buy_price: float,
                         shares: int, strategy: str = "") -> dict:
    """记录/累加一笔实盘持仓。同 code 再买入则按加权均价合并、股数累加。"""
    import datetime as _dt
    today = _dt.datetime.now().strftime("%Y-%m-%d")
    with _lock, _conn() as c:
        row = c.execute("SELECT shares, buy_price FROM live_positions "
                        "WHERE user_id=? AND code=?", (user_id, code)).fetchone()
        if row and row["shares"] > 0:
            old_sh, old_px = int(row["shares"]), float(row["buy_price"])
            new_sh = old_sh + int(shares)
            new_px = (old_px * old_sh + float(buy_price) * int(shares)) / new_sh if new_sh else buy_price
            c.execute("UPDATE live_positions SET shares=?, buy_price=?, "
                      "peak=MAX(COALESCE(peak,0),?), updated_at=datetime('now','localtime') "
                      "WHERE user_id=? AND code=?",
                      (new_sh, round(new_px, 3), float(buy_price), user_id, code))
        else:
            c.execute("INSERT OR REPLACE INTO live_positions"
                      "(user_id,code,name,shares,buy_price,buy_date,peak,strategy) "
                      "VALUES(?,?,?,?,?,?,?,?)",
                      (user_id, code, name, int(shares), float(buy_price),
                       today, float(buy_price), strategy))
        c.commit()
    return {"ok": True}


def get_live_positions(user_id: int) -> list:
    with _conn() as c:
        rows = c.execute("SELECT * FROM live_positions WHERE user_id=? AND shares>0",
                         (user_id,)).fetchall()
    return [dict(r) for r in rows]


def update_live_peak(user_id: int, code: str, peak: float) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE live_positions SET peak=?, updated_at=datetime('now','localtime') "
                  "WHERE user_id=? AND code=?", (float(peak), user_id, code))
        c.commit()


def remove_live_position(user_id: int, code: str) -> None:
    with _lock, _conn() as c:
        c.execute("DELETE FROM live_positions WHERE user_id=? AND code=?", (user_id, code))
        c.commit()
