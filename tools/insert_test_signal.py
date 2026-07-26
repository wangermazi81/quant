# -*- coding: utf-8 -*-
"""本地全闭环联调用：手工插一条 pending 买入信号，看桥接器能否拉到并弹出下单页。

⚠️ 这个脚本会往**真实的 data/users.db** 写一条 uid=1 的待执行买入信号，
桥接器 1 秒内就会拉到 → 弹 xiadan 买入页 + 委托确认框。若该机器正处于实盘模式
且开了自动确认，就是一笔真实委托。

因此：
  - 原先它叫 `_insert_local.py` 且躺在仓库根目录，路径写死、无任何确认，
    极易被误执行（tab 补全一下就跑了）——现移入 tools/ 并加显式确认闸；
  - 必须带 --yes 才会真的写库，否则只打印将要做什么然后退出；
  - DB 路径不再硬编码，按脚本位置推导。

用法：
    python tools/insert_test_signal.py            # 只预览，不写库
    python tools/insert_test_signal.py --yes      # 确认写入
    python tools/insert_test_signal.py --yes --code 600519 --price 1680 --shares 100
"""
import argparse
import datetime
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "users.db"


def main() -> int:
    ap = argparse.ArgumentParser(description="插入一条测试用 pending 买入信号")
    ap.add_argument("--yes", action="store_true", help="确认真的写库（不加则只预览）")
    ap.add_argument("--uid", type=int, default=1)
    ap.add_argument("--code", default="000001")
    ap.add_argument("--name", default="平安银行")
    ap.add_argument("--price", type=float, default=10.50)
    ap.add_argument("--shares", type=int, default=100)
    args = ap.parse_args()

    print(f"目标库: {DB_PATH}")
    print(f"将插入 : uid={args.uid} {args.code} {args.name} "
          f"BUY {args.shares}股 @{args.price}")
    if not args.yes:
        print("\n未加 --yes，仅预览，未写入任何数据。")
        print("注意：真写入后桥接器会在 1 秒内拉到并弹出下单确认框；"
              "若本机处于实盘模式且开了自动确认，将产生真实委托。")
        return 0

    if not DB_PATH.exists():
        print(f"❌ 数据库不存在: {DB_PATH}")
        return 1

    con = sqlite3.connect(str(DB_PATH))
    try:
        con.execute(
            "INSERT INTO trade_signals"
            "(user_id, code, name, action, price, shares, status, msg, created_at)"
            " VALUES (?,?,?,'BUY',?,?,'pending','本地全闭环测试',?)",
            (args.uid, args.code, args.name, args.price, args.shares,
             datetime.datetime.now().isoformat()),
        )
        con.commit()
        nid = con.execute(
            "SELECT id FROM trade_signals ORDER BY id DESC LIMIT 1").fetchone()[0]
    finally:
        con.close()
    print(f"✅ 已插入 trade_signal id={nid}，1 秒内桥接器会拉到 → xiadan 弹买入页 + 委托确认框")
    return 0


if __name__ == "__main__":
    sys.exit(main())
