# -*- coding: utf-8 -*-
"""历史数据前复权迁移：把 data/daily 下所有不复权 CSV 重新下载为前复权。

可重复运行（断点续传）：meta 中 adjust=qfq 的股票自动跳过。
用法：python tools/redownload_qfq.py [--limit N]
本地和云端都可直接运行。
"""
import sys
import time
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "analytics"))

import data_manager as dm  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 只（0=全部）")
    ap.add_argument("--start", default="2023-01-01", help="重新下载的起始日期")
    args = ap.parse_args()

    meta = dm._load_meta()
    csv_codes = sorted(p.stem for p in dm.DATA_DIR.glob("*.csv"))
    todo = [c for c in csv_codes if meta.get(c, {}).get("adjust") != "qfq"]
    if args.limit:
        todo = todo[:args.limit]

    total = len(todo)
    print(f"共 {len(csv_codes)} 只本地数据，待迁移 {total} 只（已是 qfq 的跳过）")
    ok = fail = 0
    t0 = time.time()
    for i, code in enumerate(todo, 1):
        r = dm._download_one(code, args.start)
        if r.get("ok"):
            ok += 1
        else:
            fail += 1
            print(f"  [FAIL] {code}: {r.get('msg', '')[:60]}")
        if i % 50 == 0 or i == total:
            el = time.time() - t0
            eta = el / i * (total - i)
            print(f"进度 {i}/{total}  成功{ok} 失败{fail}  "
                  f"已用{el/60:.1f}min  剩余约{eta/60:.1f}min", flush=True)
        time.sleep(0.15)   # 控速，避免被腾讯限流

    print(f"完成：成功 {ok}，失败 {fail}，耗时 {(time.time()-t0)/60:.1f} 分钟")


if __name__ == "__main__":
    main()
