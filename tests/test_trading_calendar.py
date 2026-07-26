# -*- coding: utf-8 -*-
"""交易日历与交易时段判断测试。

守两件事：
1. 落在工作日的法定休市日不得被当成交易日（原实现只看「周中 + 时间段」）；
2. 没有日历数据时必须**退回原行为**而不是把交易全停掉 —— 拿不到日历
   不能成为停机理由。
"""
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from market import market_data, trading_calendar


def _write_calendar(tmp_path, monkeypatch, year, closed):
    f = tmp_path / "trading_calendar.json"
    f.write_text(json.dumps({"updated": "2026-01-01",
                             "closed_days": {str(year): closed}}),
                 encoding="utf-8")
    monkeypatch.setattr(trading_calendar, "CALENDAR_FILE", f)
    monkeypatch.setattr(trading_calendar, "_cache", None, raising=False)
    monkeypatch.setattr(trading_calendar, "_cache_sig", None, raising=False)
    return f


def test_weekend_is_never_trading_day(tmp_path, monkeypatch):
    _write_calendar(tmp_path, monkeypatch, 2026, [])
    # 不写死具体日期：从某天往后找到真正的周六再断言，避免测试本身依赖日期常识
    saturday = date(2026, 7, 26)
    while saturday.weekday() != 5:
        saturday = date.fromordinal(saturday.toordinal() + 1)
    assert trading_calendar.is_trading_day(saturday) is False


def test_weekday_holiday_is_not_trading_day(tmp_path, monkeypatch):
    """★核心：周中的法定休市日必须被排除"""
    holiday = date(2026, 10, 1)
    while holiday.weekday() >= 5:          # 确保取一个周中的日子
        holiday = date.fromordinal(holiday.toordinal() + 1)
    _write_calendar(tmp_path, monkeypatch, holiday.year, [holiday.isoformat()])

    assert trading_calendar.is_trading_day(holiday) is False


def test_weekday_not_in_holiday_list_is_trading_day(tmp_path, monkeypatch):
    workday = date(2026, 7, 27)
    while workday.weekday() >= 5:
        workday = date.fromordinal(workday.toordinal() + 1)
    _write_calendar(tmp_path, monkeypatch, workday.year, ["2026-10-01"])

    assert trading_calendar.is_trading_day(workday) is True


def test_missing_calendar_falls_back_to_weekday_only(tmp_path, monkeypatch):
    """没有日历数据时退回原行为——不能因为拿不到日历就把交易停掉"""
    missing = tmp_path / "no_such_calendar.json"
    monkeypatch.setattr(trading_calendar, "CALENDAR_FILE", missing)
    monkeypatch.setattr(trading_calendar, "_cache", None, raising=False)
    monkeypatch.setattr(trading_calendar, "_cache_sig", None, raising=False)

    workday = date(2026, 7, 27)
    while workday.weekday() >= 5:
        workday = date.fromordinal(workday.toordinal() + 1)
    assert trading_calendar.is_trading_day(workday) is True
    assert trading_calendar.has_calendar(workday.year) is False


def test_corrupted_calendar_falls_back(tmp_path, monkeypatch):
    f = tmp_path / "trading_calendar.json"
    f.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(trading_calendar, "CALENDAR_FILE", f)
    monkeypatch.setattr(trading_calendar, "_cache", None, raising=False)
    monkeypatch.setattr(trading_calendar, "_cache_sig", None, raising=False)

    workday = date(2026, 7, 27)
    while workday.weekday() >= 5:
        workday = date.fromordinal(workday.toordinal() + 1)
    assert trading_calendar.is_trading_day(workday) is True


def test_is_trading_time_rejects_holiday(tmp_path, monkeypatch):
    """交易时段判断必须接上日历：休市日的 10:30 也不是交易时间"""
    holiday = date(2026, 10, 1)
    while holiday.weekday() >= 5:
        holiday = date.fromordinal(holiday.toordinal() + 1)
    _write_calendar(tmp_path, monkeypatch, holiday.year, [holiday.isoformat()])

    monkeypatch.setattr(market_data, "_now",
                        lambda: datetime(holiday.year, holiday.month, holiday.day, 10, 30))
    assert market_data.is_trading_time() is False

    # 同一时刻、非休市日 → 是交易时间
    workday = date(2026, 7, 27)
    while workday.weekday() >= 5:
        workday = date.fromordinal(workday.toordinal() + 1)
    monkeypatch.setattr(market_data, "_now",
                        lambda: datetime(workday.year, workday.month, workday.day, 10, 30))
    assert market_data.is_trading_time() is True


def test_now_uses_shanghai_wall_clock():
    """_now() 必须返回上海挂钟时间且为 naive（下游全是裸 datetime 比较）"""
    now = market_data._now()
    assert now.tzinfo is None
    from datetime import timezone, timedelta
    expected = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
    assert abs((now - expected).total_seconds()) < 5
