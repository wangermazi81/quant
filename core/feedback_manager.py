"""
问题反馈管理 —— 用户提交反馈 / 管理员处理
数据库: data/users.db  (SQLite，WAL 模式，与 user_manager 同库)

设计说明：
- 字段与状态机刻意保持稳定、可机读，便于日后 OpenClaw 巡检时直接
  `GET /api/feedback?status=open` 拉取待处理项（本轮不实现消费端）。
- category / status 用稳定英文键，前端负责展示中文标签。
"""
import os
import sqlite3
import threading
import logging
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "users.db")

# 反馈分类（稳定键 → 前端中文标签在 UI 层）
CATEGORIES = ("bug", "suggestion", "data", "other")
# 状态机：open(待处理) → read(已读) → in_progress(处理中) → resolved(已解决)
STATUSES = ("open", "read", "in_progress", "resolved")

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db() -> None:
    """建表（幂等）。在 main.py 启动时与 user_manager.init_db() 一起调用。"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS feedback (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            username    TEXT,
            category    TEXT    NOT NULL DEFAULT 'other',
            page        TEXT,
            content     TEXT    NOT NULL,
            contact     TEXT,
            status      TEXT    NOT NULL DEFAULT 'open',
            admin_note  TEXT,
            ua          TEXT,
            app_version TEXT,
            created_at  TEXT    DEFAULT (datetime('now','localtime')),
            updated_at  TEXT    DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_feedback_status  ON feedback(status);
        CREATE INDEX IF NOT EXISTS idx_feedback_user    ON feedback(user_id);
        CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at);
        """)
        c.commit()


def add_feedback(user_id: Optional[int], username: str, category: str,
                 page: str, content: str, contact: str = "",
                 ua: str = "", app_version: str = "") -> int:
    """写入一条反馈，返回新 id。"""
    if category not in CATEGORIES:
        category = "other"
    with _lock, _conn() as c:
        cur = c.execute(
            """INSERT INTO feedback
               (user_id, username, category, page, content, contact, status, ua, app_version)
               VALUES (?,?,?,?,?,?, 'open', ?,?)""",
            (user_id, username, category, page, content, contact, ua, app_version),
        )
        c.commit()
        return cur.lastrowid


def list_feedback(status: Optional[str] = None, limit: int = 300) -> List[Dict]:
    """管理员列表：按创建时间倒序，可按状态过滤。"""
    with _conn() as c:
        if status in STATUSES:
            rows = c.execute(
                "SELECT * FROM feedback WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def get_user_feedback(user_id: int, limit: int = 50) -> List[Dict]:
    """用户查看自己的提交与处理状态。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, category, page, content, status, admin_note, created_at, updated_at "
            "FROM feedback WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def update_feedback(fid: int, status: Optional[str] = None,
                    admin_note: Optional[str] = None) -> bool:
    """管理员更新状态 / 备注。任一为 None 表示不改该字段。"""
    sets, params = [], []
    if status is not None and status in STATUSES:
        sets.append("status=?"); params.append(status)
    if admin_note is not None:
        sets.append("admin_note=?"); params.append(admin_note)
    if not sets:
        return False
    sets.append("updated_at=datetime('now','localtime')")
    params.append(fid)
    with _lock, _conn() as c:
        cur = c.execute(f"UPDATE feedback SET {', '.join(sets)} WHERE id=?", params)
        c.commit()
        return cur.rowcount > 0


def count_by_status() -> Dict[str, int]:
    """各状态计数（后台徽章用）。"""
    out = {s: 0 for s in STATUSES}
    with _conn() as c:
        for r in c.execute("SELECT status, COUNT(*) n FROM feedback GROUP BY status").fetchall():
            if r["status"] in out:
                out[r["status"]] = r["n"]
    out["total"] = sum(out[s] for s in STATUSES)
    return out
