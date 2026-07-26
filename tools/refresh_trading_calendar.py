# -*- coding: utf-8 -*-
"""拉取 A 股交易日历并写入 data/trading_calendar.json。

`is_trading_time()` 靠这份日历识别落在工作日的法定休市日（国庆/春节/清明…）。
没有它就退回「周中即交易日」，休市日会被误判为交易日而照常派发扫描。

用法：
    python tools/refresh_trading_calendar.py            # 刷新当年
    python tools/refresh_trading_calendar.py --year 2027
    python tools/refresh_trading_calendar.py --year 2026 --year 2027

依赖已配置的 tushare 数据源（数据管理页里的 token）。
建议每年年底交易所公布次年休市安排后跑一次。
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from market import trading_calendar  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="刷新 A 股交易日历")
    ap.add_argument("--year", type=int, action="append",
                    help="要刷新的年份，可重复；默认当年")
    args = ap.parse_args()

    years = args.year or [date.today().year]
    failed = 0
    for y in years:
        r = trading_calendar.refresh_from_tushare(y)
        if r.get("ok"):
            print(f"✅ {y}: {r['closed']} 个周中休市日 → {trading_calendar.CALENDAR_FILE}")
        else:
            failed += 1
            print(f"❌ {y}: {r.get('msg')}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
