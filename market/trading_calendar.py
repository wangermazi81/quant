# -*- coding: utf-8 -*-
"""A 股交易日历。

为什么需要它：`is_trading_time()` 原来只判「周一到周五 + 时间段」，于是**落在
工作日的法定休市日（国庆、春节、清明…）照常被当成交易日**：调度器继续派发扫描，
策略基于最后一个交易日的陈旧行情产生信号，模拟盘按陈旧价「成交」，
today_trades / hold_days 全按错误口径累计。

设计上刻意保守：
  - 日历数据是**可选增强**。文件缺失或不覆盖当年时，退回「周中即交易日」的原行为，
    并只打一次 warning —— 不能因为拿不到日历就把交易全停了。
  - 绝不内置硬编码的节假日表：A 股休市安排每年由交易所公布，写死一份猜测的日期
    比没有日历更危险（可能在真交易日停机，或在休市日照常下单）。
  - 刷新走 tushare trade_cal，由调度器每日调用一次，**不在 is_trading_time 的
    热路径里发网络请求**（该函数在调度循环里每 30 秒被调用）。

数据文件 data/trading_calendar.json 结构：
    {"updated": "2026-07-26", "closed_days": {"2026": ["2026-10-01", ...]}}
`closed_days` 只记「周中的休市日」，周末不必记（周末永不交易）。
"""
from __future__ import annotations

import json
import hashlib
import hmac
import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

CALENDAR_FILE = Path(__file__).resolve().parent.parent / "data" / "trading_calendar.json"
CALENDAR_COMMITMENTS_DIR = Path(__file__).resolve().parent / "calendar_commitments"

_lock = threading.Lock()
_cache: dict | None = None
_cache_sig: tuple | None = None
_warned_years: set[str] = set()
CALENDAR_PROVENANCE_VERSION = "tushare_trade_cal_provenance_v1"
CALENDAR_COMMITMENT_VERSION = "tushare_trade_calendar_commitment_v1"


def _signature() -> tuple:
    try:
        st = CALENDAR_FILE.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, -1)


def load() -> dict:
    """读日历（带 mtime+size 指纹的进程内缓存）。"""
    global _cache, _cache_sig
    sig = _signature()
    with _lock:
        if _cache is not None and _cache_sig == sig:
            return _cache
        data: dict = {}
        if CALENDAR_FILE.exists():
            try:
                loaded = json.loads(CALENDAR_FILE.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception as e:
                logger.warning(f"[calendar] 交易日历读取失败，退回「周中即交易日」: {e}")
        _cache, _cache_sig = data, sig
        return data


def closed_days(year: int | str) -> set[str] | None:
    """某年的周中休市日集合；返回 None 表示该年无日历数据。"""
    raw = (load().get("closed_days") or {}).get(str(year))
    if not isinstance(raw, list):
        return None
    return {str(d).strip() for d in raw if str(d).strip()}


def has_calendar(year: int | str) -> bool:
    return closed_days(year) is not None


def calendar_provenance(year: int | str) -> dict | None:
    raw = (load().get("provenance") or {}).get(str(year))
    return dict(raw) if isinstance(raw, dict) else None


def _open_days_from_closed(year: int, closed: set[str]) -> list[str]:
    current = date(year, 1, 1)
    end = date(year, 12, 31)
    values: list[str] = []
    while current <= end:
        text = current.isoformat()
        if current.weekday() < 5 and text not in closed:
            values.append(text)
        current = date.fromordinal(current.toordinal() + 1)
    return values


def _open_days_digest(open_days: list[str]) -> str:
    payload = json.dumps(
        sorted(open_days),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class AuthoritativeCalendarYear:
    year: int
    closed_days: frozenset[str]
    open_days_count: int
    open_days_sha256: str


def authoritative_year(
    year: int | str,
) -> AuthoritativeCalendarYear | None:
    if isinstance(year, bool):
        return None
    try:
        target = int(year)
    except (TypeError, ValueError):
        return None
    path = (
        CALENDAR_COMMITMENTS_DIR
        / f"tushare_sse_{target}_v1.json"
    )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    expected_keys = {
        "schema_version",
        "source",
        "exchange",
        "year",
        "verified_at",
        "closed_days",
        "open_days_count",
        "open_days_sha256",
    }
    if (
        not isinstance(raw, dict)
        or set(raw) != expected_keys
        or raw.get("schema_version") != CALENDAR_COMMITMENT_VERSION
        or raw.get("source") != "tushare_trade_cal"
        or raw.get("exchange") != "SSE"
        or raw.get("year") != target
    ):
        return None
    verified_at = raw.get("verified_at")
    try:
        parsed_verified_at = datetime.fromisoformat(
            str(verified_at).replace("Z", "+00:00")
        )
    except ValueError:
        return None
    raw_closed_days = raw.get("closed_days")
    if (
        not isinstance(verified_at, str)
        or parsed_verified_at.tzinfo is None
        or parsed_verified_at.utcoffset() is None
        or not isinstance(raw_closed_days, list)
    ):
        return None
    closed_days: set[str] = set()
    for value in raw_closed_days:
        if not isinstance(value, str):
            return None
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            return None
        if parsed.year != target or parsed.weekday() >= 5:
            return None
        closed_days.add(value)
    if (
        raw_closed_days != sorted(closed_days)
        or not 5 <= len(closed_days) <= 40
    ):
        return None
    open_days = _open_days_from_closed(target, closed_days)
    open_days_count = raw.get("open_days_count")
    open_days_sha256 = raw.get("open_days_sha256")
    if (
        isinstance(open_days_count, bool)
        or not isinstance(open_days_count, int)
        or open_days_count != len(open_days)
        or not isinstance(open_days_sha256, str)
        or not hmac.compare_digest(
            open_days_sha256,
            _open_days_digest(open_days),
        )
    ):
        return None
    return AuthoritativeCalendarYear(
        year=target,
        closed_days=frozenset(closed_days),
        open_days_count=open_days_count,
        open_days_sha256=open_days_sha256,
    )


def has_verified_calendar(year: int | str) -> bool:
    try:
        target = int(year)
    except (TypeError, ValueError):
        return False
    closed = closed_days(target)
    provenance = calendar_provenance(target)
    authority = authoritative_year(target)
    if (
        closed is None
        or provenance is None
        or authority is None
        or not 5 <= len(closed) <= 40
        or frozenset(closed) != authority.closed_days
    ):
        return False
    fetched_at = provenance.get("fetched_at")
    if not isinstance(fetched_at, str) or not fetched_at.strip():
        return False
    try:
        parsed_fetched_at = datetime.fromisoformat(
            fetched_at.replace("Z", "+00:00")
        )
    except ValueError:
        return False
    if (
        parsed_fetched_at.tzinfo is None
        or parsed_fetched_at.utcoffset() is None
    ):
        return False
    open_days = _open_days_from_closed(target, closed)
    open_days_sha256 = _open_days_digest(open_days)
    return bool(
        provenance.get("provenance_version")
        == CALENDAR_PROVENANCE_VERSION
        and provenance.get("source") == "tushare_trade_cal"
        and provenance.get("provider_mode") in {"replay", "sdk"}
        and provenance.get("open_days_count") == len(open_days)
        and isinstance(provenance.get("open_days_sha256"), str)
        and hmac.compare_digest(
            provenance["open_days_sha256"],
            open_days_sha256,
        )
        and len(open_days) == authority.open_days_count
        and hmac.compare_digest(
            open_days_sha256,
            authority.open_days_sha256,
        )
    )


def is_trading_day(d: date | datetime | None = None) -> bool:
    """d 是否为 A 股交易日。无日历数据时退回「周中即交易日」。"""
    if d is None:
        d = date.today()
    if isinstance(d, datetime):
        d = d.date()
    if d.weekday() >= 5:            # 周末永不交易，与日历无关
        return False
    holidays = closed_days(d.year)
    if holidays is None:
        year_key = str(d.year)
        if year_key not in _warned_years:
            _warned_years.add(year_key)
            logger.warning(
                "[calendar] 缺少 %s 年交易日历，暂按「周中即交易日」处理"
                "（法定休市日会被误判为交易日）。可运行 tools/refresh_trading_calendar.py 拉取。",
                year_key)
        return True
    return d.isoformat() not in holidays


# ── 刷新（由调度器每日调用一次，不在热路径）──────────────────────
def refresh_from_tushare(year: int | None = None) -> dict:
    """用 tushare trade_cal 重建某年的休市日集合并落盘。

    返回 {"ok": bool, "year": int, "closed": int, "msg": str}
    """
    target = int(year or date.today().year)
    try:
        from analytics import tushare_provider as tp
        open_days = tp.fetch_trade_calendar(f"{target}0101", f"{target}1231")
    except Exception as e:
        return {"ok": False, "year": target, "closed": 0, "msg": f"拉取失败: {e}"}

    if not open_days:
        return {"ok": False, "year": target, "closed": 0, "msg": "trade_cal 返回空"}

    open_set = {str(d) for d in open_days}
    # 反推「周中但不开市」的日期 = 休市日
    closed = []
    cur = date(target, 1, 1)
    end = date(target, 12, 31)
    while cur <= end:
        if cur.weekday() < 5 and cur.isoformat() not in open_set:
            closed.append(cur.isoformat())
        cur = date.fromordinal(cur.toordinal() + 1)

    # ★ 合理性校验：宁可不落盘，也不要写一份「看着有效实则无用」的日历。
    #   A 股一年约 242 个交易日、周中法定休市日约 10-17 天。
    #   若算出 0 天，几乎一定是上游把周末也当成了开市日（实测 replay 端点
    #   会无视 is_open=1 返回全部 365 天）—— 这种日历一旦落盘，has_calendar()
    #   变 True、"缺日历"告警被抑制，而节假日一天都拦不住，比没有日历更危险。
    if not (5 <= len(closed) <= 40):
        msg = (f"休市日数 {len(closed)} 不在合理区间[5,40]，判定为上游数据异常"
               f"（开市日 {len(open_set)} 条，正常应约 242），拒绝写入日历")
        logger.error("[calendar] %s", msg)
        return {"ok": False, "year": target, "closed": len(closed), "msg": msg}

    data = load().copy()
    days = dict(data.get("closed_days") or {})
    days[str(target)] = closed
    data["closed_days"] = days
    provenance = dict(data.get("provenance") or {})
    normalized_open_days = _open_days_from_closed(target, set(closed))
    try:
        provider_mode = str(
            tp.datasource_config.get_internal_config().get(
                "tushare_mode",
                "",
            )
        ).strip().lower()
    except Exception:
        provider_mode = ""
    if provider_mode not in {"replay", "sdk"}:
        return {
            "ok": False,
            "year": target,
            "closed": len(closed),
            "msg": "Tushare provider mode is unavailable",
        }
    provenance[str(target)] = {
        "provenance_version": CALENDAR_PROVENANCE_VERSION,
        "source": "tushare_trade_cal",
        "provider_mode": provider_mode,
        "fetched_at": datetime.now(
            timezone(timedelta(hours=8))
        ).isoformat(timespec="seconds"),
        "open_days_count": len(normalized_open_days),
        "open_days_sha256": _open_days_digest(normalized_open_days),
    }
    data["provenance"] = provenance
    data["updated"] = date.today().isoformat()

    CALENDAR_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CALENDAR_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CALENDAR_FILE)          # 原子替换，避免读到写一半的文件

    global _cache, _cache_sig
    with _lock:
        _cache, _cache_sig = None, None     # 失效缓存
    logger.info(f"[calendar] {target} 年交易日历已更新：{len(closed)} 个周中休市日")
    return {"ok": True, "year": target, "closed": len(closed), "msg": "ok"}


def ensure_fresh() -> None:
    """当年日历缺失时尝试补一次。供调度器启动/每日调用，失败不抛异常。"""
    year = date.today().year
    if has_verified_calendar(year):
        return
    try:
        refresh_from_tushare(year)
    except Exception as e:
        logger.warning(f"[calendar] 自动刷新失败（不影响运行）: {e}")
