"""
更新日志管理 — 管理员发布更新日志 / 用户查看
数据库: data/users.db  (SQLite，WAL 模式，与 user_manager 同库)

设计说明：
- 版本号语义化（vX.Y.Z 或日期型）
- type 分类：feature(新功能) / fix(修复) / optimize(优化) / security(安全) / notice(公告)
- 用户端只看 is_published=1 的，管理员看全部
"""
import os
import sqlite3
import threading
import logging
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "users.db")

TYPES = ("feature", "fix", "optimize", "security", "notice")
TYPE_LABELS = {
    "feature": "🆕 新功能",
    "fix": "🐛 修复",
    "optimize": "⚡ 优化",
    "security": "🔒 安全",
    "notice": "📢 公告",
}

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db() -> None:
    """建表（幂等）。在 main.py 启动时调用。"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS changelog (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            version      TEXT    NOT NULL DEFAULT '',
            title        TEXT    NOT NULL,
            content      TEXT    NOT NULL DEFAULT '',
            change_type  TEXT    NOT NULL DEFAULT 'notice',
            is_published INTEGER DEFAULT 1,
            created_by   INTEGER,
            created_at   TEXT    DEFAULT (datetime('now','localtime')),
            updated_at   TEXT    DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_cl_published ON changelog(is_published);
        CREATE INDEX IF NOT EXISTS idx_cl_created   ON changelog(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_cl_type      ON changelog(change_type);
        """)
        c.commit()


def add_changelog(title: str, content: str = "", version: str = "",
                  change_type: str = "notice", created_by: int = 0,
                  is_published: int = 1) -> int:
    """创建一条更新日志，返回新 id。"""
    if change_type not in TYPES:
        change_type = "notice"
    with _lock, _conn() as c:
        cur = c.execute(
            """INSERT INTO changelog (version, title, content, change_type, is_published, created_by)
               VALUES (?,?,?,?,?,?)""",
            (version, title, content, change_type, is_published, created_by),
        )
        c.commit()
        return cur.lastrowid


def list_changelog(limit: int = 100, published_only: bool = True) -> List[Dict]:
    """列出更新日志（倒序）。published_only=True 只看已发布。"""
    with _conn() as c:
        if published_only:
            rows = c.execute(
                "SELECT * FROM changelog WHERE is_published=1 ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM changelog ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def get_changelog(cid: int) -> Optional[Dict]:
    """获取单条。"""
    with _conn() as c:
        row = c.execute("SELECT * FROM changelog WHERE id=?", (cid,)).fetchone()
        return dict(row) if row else None


def update_changelog(cid: int, **kwargs) -> bool:
    """更新字段（version/title/content/change_type/is_published）。"""
    allowed = {"version", "title", "content", "change_type", "is_published"}
    sets, params = [], []
    for k, v in kwargs.items():
        if k in allowed:
            sets.append(f"{k}=?"); params.append(v)
    if not sets:
        return False
    sets.append("updated_at=datetime('now','localtime')")
    params.append(cid)
    with _lock, _conn() as c:
        cur = c.execute(f"UPDATE changelog SET {', '.join(sets)} WHERE id=?", params)
        c.commit()
        return cur.rowcount > 0


def delete_changelog(cid: int) -> bool:
    """删除一条。"""
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM changelog WHERE id=?", (cid,))
        c.commit()
        return cur.rowcount > 0


def count() -> int:
    """总数。"""
    with _conn() as c:
        r = c.execute("SELECT COUNT(*) n FROM changelog WHERE is_published=1").fetchone()
        return r["n"] if r else 0
