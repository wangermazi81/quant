# -*- coding: utf-8 -*-
"""
数据库迁移脚本
运行方式: python db_migration.py
幂等设计: 可反复运行，不会重复操作
"""
import sqlite3
import os
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAPER_DB = os.path.join(BASE, "data", "paper_trading.db")
USERS_DB  = os.path.join(BASE, "data", "users.db")


def _conn(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = sqlite3.connect(path, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _safe_add_column(conn, table: str, column: str, definition: str):
    """幂等地给已存在的表补加一列（列已存在时跳过）。

    失败必须复查而不是一律当作「列已存在」吞掉：ALTER 也可能因
    database is locked / 磁盘异常而失败，静默跳过会让幂等性假设破产 ——
    user_settings 缺列后 save_user_settings 持续失败（只 warning），
    today_trades 无法持久化 → 重启当天日内开仓限额失效可超限开仓；
    max_positions / 单笔上限回退 config 默认，护栏被悄悄放松。
    """
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.commit()
        logger.info(f"[migration] {table}.{column} 列已添加")
    except Exception as e:
        if _col_exists(conn, table, column):
            return                      # 真的已存在 → 正常的幂等路径
        logger.error(
            "[migration] %s.%s 补列失败且该列确实不存在：%s —— "
            "依赖此列的功能（用户设置/日内限额等护栏）将失效，请检查数据库",
            table, column, e)
        raise


def _col_exists(c, table, col):
    rows = c.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == col for r in rows)


def _table_exists(c, table):
    return c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


# ── paper_trading.db 迁移 ────────────────────────────────────────────────────
def migrate_paper_trading_db():
    if not os.path.exists(PAPER_DB):
        logger.info("[paper_trading.db] 文件不存在，跳过迁移（首次运行时由 paper_broker 创建）")
        return

    c = _conn(PAPER_DB)
    try:
        tables = ["paper_config", "paper_orders", "paper_trades", "paper_positions", "paper_equity"]
        for tbl in tables:
            if not _table_exists(c, tbl):
                logger.info(f"[paper_trading.db] 表 {tbl} 不存在，跳过")
                continue
            if not _col_exists(c, tbl, "user_id"):
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0")
                logger.info(f"[paper_trading.db] {tbl}: 添加 user_id 列 ✓")

        # 为高频查询列建索引
        idx_map = {
            "paper_config":    "idx_pc_uid",
            "paper_orders":    "idx_po_uid",
            "paper_trades":    "idx_pt_uid",
            "paper_positions": "idx_pp_uid",
            "paper_equity":    "idx_pe_uid",
        }
        for tbl, idx in idx_map.items():
            if _table_exists(c, tbl):
                c.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON {tbl}(user_id)")

        # paper_positions 的 PRIMARY KEY 原来是 code，多用户后需要 (user_id, code)
        # SQLite 不能修改主键，所以用唯一索引代替（若已存在则跳过）
        if _table_exists(c, "paper_positions"):
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_pp_uid_code "
                "ON paper_positions(user_id, code)"
            )

        c.commit()
        logger.info("[paper_trading.db] 迁移完成 ✓")
    finally:
        c.close()


# ── users.db 新增表 ──────────────────────────────────────────────────────────
def migrate_users_db():
    c = _conn(USERS_DB)
    try:
        c.executescript("""
        -- 用户运行状态持久化（24/7 服务器端保存）
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id        INTEGER PRIMARY KEY,
            running        INTEGER DEFAULT 0,
            auto_execute   INTEGER DEFAULT 0,
            capital        REAL    DEFAULT 500000,
            position_ratio REAL    DEFAULT 0.20,
            portfolio_mode TEXT    DEFAULT 'equal_weight',
            active_strategy TEXT   DEFAULT '',
            stock_pool     TEXT    DEFAULT '[]',
            broker_type    TEXT    DEFAULT 'paper',
            updated_at     TEXT    DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS admin_user_permissions (
            user_id    INTEGER NOT NULL,
            perm_key   TEXT    NOT NULL,
            granted_by INTEGER,
            updated_at TEXT    DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (user_id, perm_key),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_admin_perms_user ON admin_user_permissions(user_id);

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

        -- 用户运行日志（每用户独立）
        CREATE TABLE IF NOT EXISTS user_logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            level      TEXT    DEFAULT 'INFO',
            message    TEXT    NOT NULL,
            created_at TEXT    DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_ul_uid ON user_logs(user_id);
        CREATE INDEX IF NOT EXISTS idx_ul_uid_time ON user_logs(user_id, created_at DESC);

        -- 用户买入队列（持久化，重启可恢复）
        CREATE TABLE IF NOT EXISTS user_queue (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            code       TEXT    NOT NULL,
            name       TEXT    DEFAULT '',
            price      REAL    DEFAULT 0,
            shares     INTEGER DEFAULT 0,
            budget     REAL    DEFAULT 0,
            reason     TEXT    DEFAULT '',
            status     TEXT    DEFAULT 'pending',
            created_at TEXT    DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_uq_uid ON user_queue(user_id);

        -- 用户AI配置（每人可有独立API Key）
        CREATE TABLE IF NOT EXISTS user_ai_config (
            user_id  INTEGER PRIMARY KEY,
            provider TEXT DEFAULT 'deepseek',
            api_key  TEXT DEFAULT '',
            base_url TEXT DEFAULT 'https://api.deepseek.com/v1',
            model    TEXT DEFAULT 'deepseek-chat',
            enabled  INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        -- 用户券商配置
        CREATE TABLE IF NOT EXISTS user_broker_config (
            user_id     INTEGER PRIMARY KEY,
            broker_type TEXT DEFAULT 'paper',
            config_json TEXT DEFAULT '{}',
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        -- 管理员公告
        CREATE TABLE IF NOT EXISTS announcements (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            title      TEXT NOT NULL,
            content    TEXT NOT NULL,
            type       TEXT DEFAULT 'info',
            is_active  INTEGER DEFAULT 1,
            expires_at TEXT DEFAULT NULL,
            created_by INTEGER,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        );

        -- 用户已读公告（去重）
        CREATE TABLE IF NOT EXISTS announcement_reads (
            user_id         INTEGER NOT NULL,
            announcement_id INTEGER NOT NULL,
            read_at         TEXT DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (user_id, announcement_id)
        );

        -- 实盘交易信号同步队列（云端派发 -> 本地桥接拉取与执行）
        CREATE TABLE IF NOT EXISTS trade_signals (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL,
            code           TEXT NOT NULL,
            name           TEXT DEFAULT '',
            action         TEXT NOT NULL,         -- 'BUY' 或 'SELL'
            price          REAL NOT NULL,
            shares         INTEGER NOT NULL,
            status         TEXT DEFAULT 'pending', -- 'pending', 'sent', 'executed', 'failed'
            msg            TEXT DEFAULT '',       -- 执行日志或成交反馈
            created_at     TEXT DEFAULT (datetime('now','localtime')),
            updated_at     TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_ts_uid ON trade_signals(user_id);
        CREATE INDEX IF NOT EXISTS idx_ts_status ON trade_signals(status);

        -- live_positions：实盘持仓台账（2026-05-30）。实盘(xiadan)成本价/买入日 OCR
        -- 读不到，但"我们自己下的单"精确知道，故成交确认时记此表，供打板 Ride 卖出
        -- 规则（吃连板/止损/回落）自动管理。peak=持仓期最高价；strategy=买入时策略。
        CREATE TABLE IF NOT EXISTS live_positions (
            user_id    INTEGER NOT NULL,
            code       TEXT NOT NULL,
            name       TEXT DEFAULT '',
            shares     INTEGER NOT NULL,
            buy_price  REAL NOT NULL,
            buy_date   TEXT NOT NULL,
            peak       REAL,
            strategy   TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (user_id, code)
        );
        CREATE INDEX IF NOT EXISTS idx_lp_uid ON live_positions(user_id);

        -- 更新日志（2026-06-07）。管理员发布，所有用户可见。
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

        -- 邀请码（邀请制注册，2026-06-05）。管理员/老用户可生成；可多次(限名额)；
        -- 邀请人每成功邀请1人得 reward 积分。used_count 计已注册人数。
        CREATE TABLE IF NOT EXISTS invite_codes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            code        TEXT    UNIQUE NOT NULL,
            created_by  INTEGER NOT NULL,
            max_uses    INTEGER DEFAULT 1,
            used_count  INTEGER DEFAULT 0,
            reward      INTEGER DEFAULT 0,
            is_active   INTEGER DEFAULT 1,
            note        TEXT    DEFAULT '',
            expires_at  TEXT    DEFAULT NULL,
            created_at  TEXT    DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_ic_code    ON invite_codes(code);
        CREATE INDEX IF NOT EXISTS idx_ic_creator ON invite_codes(created_by);

        -- 密码重置 token（找回密码，2026-06-13）。库只存 token 的 SHA256，原文仅在邮件链接里。
        CREATE TABLE IF NOT EXISTS password_resets (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            token_hash TEXT    NOT NULL,
            created_at TEXT    DEFAULT (datetime('now','localtime')),
            expires_at TEXT    NOT NULL,
            used       INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_pwreset_token ON password_resets(token_hash);
        CREATE INDEX IF NOT EXISTS idx_pwreset_uid   ON password_resets(user_id);

        -- 找回密码申请队列（后台审批重置，2026-06-13）。无邮件依赖：用户提交→管理员一键重置。
        CREATE TABLE IF NOT EXISTS password_reset_requests (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            username    TEXT,
            email       TEXT,
            status      TEXT    DEFAULT 'pending',   -- pending | resolved | rejected
            created_at  TEXT    DEFAULT (datetime('now','localtime')),
            resolved_at TEXT,
            resolved_by INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_pwreq_status ON password_reset_requests(status);
        CREATE INDEX IF NOT EXISTS idx_pwreq_uid    ON password_reset_requests(user_id);
        """)
        c.commit()
        # 补列迁移（对已存在的旧库）
        _safe_add_column(c, "announcements", "expires_at", "TEXT DEFAULT NULL")
        _safe_add_column(c, "users", "invited_by", "INTEGER DEFAULT NULL")
        # 防薅羊毛：记录注册来源 IP + 可疑账号标记（同 IP 多开 → 不送分并标记待审）
        _safe_add_column(c, "users", "register_ip", "TEXT DEFAULT NULL")
        _safe_add_column(c, "users", "flagged", "INTEGER DEFAULT 0")
        c.execute("CREATE INDEX IF NOT EXISTS idx_users_register_ip ON users(register_ip)")
        # 日内交易计数持久化（阶段二·运行态外置）：重启后恢复当日开仓次数，
        # 使模拟盘日内开仓限额护栏在重启当天依然准确（此前 today_trades 纯内存，
        # 重启归零 → 当天可超限开仓）。today_date 用于跨天判断是否重置。
        _safe_add_column(c, "user_settings", "today_trades", "INTEGER DEFAULT 0")
        _safe_add_column(c, "user_settings", "today_date", "TEXT DEFAULT ''")
        # 修复既存 bug：仓位护栏(最多持仓数/单笔上限%)前端可调且 set_account 有持久化，
        # 但表无此列 + save 白名单漏了 → 一直存不进库，重启回退 config 默认（护栏被放松）。
        _safe_add_column(c, "user_settings", "max_positions", "INTEGER DEFAULT 5")
        _safe_add_column(c, "user_settings", "single_position_max_ratio", "REAL DEFAULT 0.20")
        # 实盘真实成交均价：桥接器从今日委托/成交页读到后回传。此前台账一律按
        # **信号价**记成本，打板/追高场景两者可差 1%-3%，止损止盈就是拿错误基准
        # 算 pnl（可能提前止盈、延迟止损），加权均价还会随每笔偏差持续漂移。
        _safe_add_column(c, "trade_signals", "exec_price", "REAL DEFAULT NULL")
        c.commit()
        logger.info("[users.db] 新表创建/迁移完成 ✓")
    finally:
        c.close()


if __name__ == "__main__":
    migrate_paper_trading_db()
    migrate_users_db()
    logger.info("所有迁移完成")
