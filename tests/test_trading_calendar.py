# -*- coding: utf-8 -*-
"""交易日历与交易时段判断测试。

守两件事：
1. 落在工作日的法定休市日不得被当成交易日（原实现只看「周中 + 时间段」）；
2. 没有可信日历时必须停止自动交易，不能拿旧行情生成模拟盘或实盘信号。
"""
import hashlib
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


def test_missing_calendar_fails_closed(tmp_path, monkeypatch):
    """缺少权威日历时宁可不交易，也不能把普通工作日当成交易日。"""
    missing = tmp_path / "no_such_calendar.json"
    monkeypatch.setattr(trading_calendar, "CALENDAR_FILE", missing)
    monkeypatch.setattr(trading_calendar, "_cache", None, raising=False)
    monkeypatch.setattr(trading_calendar, "_cache_sig", None, raising=False)

    workday = date(2026, 7, 27)
    while workday.weekday() >= 5:
        workday = date.fromordinal(workday.toordinal() + 1)
    assert trading_calendar.is_trading_day(workday) is False
    assert trading_calendar.has_calendar(workday.year) is False


def test_corrupted_calendar_fails_closed(tmp_path, monkeypatch):
    f = tmp_path / "trading_calendar.json"
    f.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(trading_calendar, "CALENDAR_FILE", f)
    monkeypatch.setattr(trading_calendar, "_cache", None, raising=False)
    monkeypatch.setattr(trading_calendar, "_cache_sig", None, raising=False)

    workday = date(2026, 7, 27)
    while workday.weekday() >= 5:
        workday = date.fromordinal(workday.toordinal() + 1)
    assert trading_calendar.is_trading_day(workday) is False


def test_is_trading_time_fails_closed_when_calendar_check_raises(
    monkeypatch,
):
    workday = datetime(2026, 7, 27, 10, 30)
    monkeypatch.setattr(market_data, "_now", lambda: workday)
    monkeypatch.setattr(
        trading_calendar,
        "is_trading_day",
        lambda _value: (_ for _ in ()).throw(
            RuntimeError("calendar unavailable")
        ),
    )

    assert market_data.is_trading_time() is False


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


def test_is_trading_time_closes_at_exact_session_boundary(tmp_path, monkeypatch):
    workday = date(2026, 7, 27)
    _write_calendar(tmp_path, monkeypatch, workday.year, [])

    monkeypatch.setattr(
        market_data,
        "_now",
        lambda: datetime(workday.year, workday.month, workday.day, 11, 29, 59),
    )
    assert market_data.is_trading_time() is True

    monkeypatch.setattr(
        market_data,
        "_now",
        lambda: datetime(workday.year, workday.month, workday.day, 11, 30, 0),
    )
    assert market_data.is_trading_time() is False

    monkeypatch.setattr(
        market_data,
        "_now",
        lambda: datetime(workday.year, workday.month, workday.day, 15, 0, 0),
    )
    assert market_data.is_trading_time() is False


class TestRefreshSanity:
    """★线上实测踩到的坑：replay 端点无视 is_open=1，返回区间内全部日历日。

    若照单全收，「开市日」里就包含周末与法定假日 → 反推出的休市日为 0 条，
    落盘一份「零休市日」的日历。那比没有日历更糟：has_calendar() 变 True、
    「缺日历」告警被抑制，而节假日一天都拦不住。
    """

    def _patch_provider(self, monkeypatch, open_days):
        import types
        from analytics import tushare_provider as tp
        monkeypatch.setattr(tp, "fetch_trade_calendar",
                            lambda s, e, **k: list(open_days))
        return types.SimpleNamespace()

    def test_rejects_calendar_with_zero_holidays(self, tmp_path, monkeypatch):
        """上游把每一天都当开市日 → 算出 0 个休市日 → 必须拒绝落盘。"""
        f = tmp_path / "trading_calendar.json"
        monkeypatch.setattr(trading_calendar, "CALENDAR_FILE", f)
        monkeypatch.setattr(trading_calendar, "_cache", None, raising=False)
        monkeypatch.setattr(trading_calendar, "_cache_sig", None, raising=False)

        year = 2026
        every_day = []
        d = date(year, 1, 1)
        while d <= date(year, 12, 31):
            every_day.append(d.isoformat())
            d = date.fromordinal(d.toordinal() + 1)
        self._patch_provider(monkeypatch, every_day)

        r = trading_calendar.refresh_from_tushare(year)
        assert r["ok"] is False
        assert "拒绝写入" in r["msg"]
        assert not f.exists(), "异常数据绝不能落盘"

    def test_accepts_plausible_calendar(self, tmp_path, monkeypatch):
        """正常数据（工作日去掉十几个假日）→ 正常落盘。"""
        f = tmp_path / "trading_calendar.json"
        monkeypatch.setattr(trading_calendar, "CALENDAR_FILE", f)
        monkeypatch.setattr(trading_calendar, "_cache", None, raising=False)
        monkeypatch.setattr(trading_calendar, "_cache_sig", None, raising=False)

        year = 2026
        holidays = set()
        d = date(year, 10, 1)
        while len(holidays) < 12:
            if d.weekday() < 5:
                holidays.add(d.isoformat())
            d = date.fromordinal(d.toordinal() + 1)

        opens = []
        d = date(year, 1, 1)
        while d <= date(year, 12, 31):
            if d.weekday() < 5 and d.isoformat() not in holidays:
                opens.append(d.isoformat())
            d = date.fromordinal(d.toordinal() + 1)
        self._patch_provider(monkeypatch, opens)

        r = trading_calendar.refresh_from_tushare(year)
        assert r["ok"] is True
        assert r["closed"] == 12
        assert f.exists()
        saved = json.loads(f.read_text(encoding="utf-8"))
        provenance = saved["provenance"][str(year)]
        expected_digest = hashlib.sha256(
            json.dumps(
                sorted(opens),
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        assert provenance == {
            "provenance_version": "tushare_trade_cal_provenance_v1",
            "source": "tushare_trade_cal",
            "provider_mode": "replay",
            "fetched_at": provenance["fetched_at"],
            "open_days_count": len(opens),
            "open_days_sha256": expected_digest,
        }
        assert provenance["fetched_at"].endswith(("+08:00", "Z"))
        for h in holidays:
            assert trading_calendar.is_trading_day(date.fromisoformat(h)) is False


def test_fetch_trade_calendar_filters_by_is_open(monkeypatch):
    """★fetch_trade_calendar 必须按行里的 is_open 字段本地过滤。

    replay 端点会把 is_open=0 的行（周末/假日）一起返回，只是带了正确标记；
    信任请求参数就会把假日当成开市日。
    """
    from analytics import tushare_provider as tp

    rows = [
        {"cal_date": "20261001", "is_open": 0},   # 国庆，休市
        {"cal_date": "20261008", "is_open": 1},   # 节后首个交易日
        {"cal_date": "20261009", "is_open": 1},
        {"cal_date": "20261010", "is_open": 0},   # 周末
    ]
    monkeypatch.setattr(tp, "_replay_get", lambda *a, **k: rows)
    monkeypatch.setattr(tp.datasource_config, "resolve_tushare_token", lambda **k: "tok")

    got = tp.fetch_trade_calendar("20261001", "20261010")
    assert got == ["2026-10-08", "2026-10-09"], f"未按 is_open 过滤：{got}"


def test_verified_calendar_rejects_invalid_or_naive_fetch_timestamp(
    monkeypatch,
):
    year = 2026
    closed = {
        "2026-01-01",
        "2026-01-02",
        "2026-02-16",
        "2026-02-17",
        "2026-02-18",
    }
    open_days = trading_calendar._open_days_from_closed(year, closed)
    provenance = {
        "provenance_version": "tushare_trade_cal_provenance_v1",
        "source": "tushare_trade_cal",
        "provider_mode": "replay",
        "fetched_at": "not-a-timestamp",
        "open_days_count": len(open_days),
        "open_days_sha256": trading_calendar._open_days_digest(open_days),
    }
    monkeypatch.setattr(
        trading_calendar,
        "closed_days",
        lambda _year: closed,
    )
    monkeypatch.setattr(
        trading_calendar,
        "calendar_provenance",
        lambda _year: provenance,
    )

    assert trading_calendar.has_verified_calendar(year) is False
    provenance["fetched_at"] = "2026-07-30T16:10:00"
    assert trading_calendar.has_verified_calendar(year) is False


def test_now_uses_shanghai_wall_clock():
    """_now() 必须返回上海挂钟时间且为 naive（下游全是裸 datetime 比较）"""
    now = market_data._now()
    assert now.tzinfo is None
    from datetime import timezone, timedelta
    expected = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
    assert abs((now - expected).total_seconds()) < 5
