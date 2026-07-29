# -*- coding: utf-8 -*-
"""对比当前 app.url_map 与基线，确认路由路径+方法零变化。"""
import json
import os

os.environ.setdefault("QUANT_TEST_IMPORT", "1")
os.environ.setdefault("THS_NO_SCHEDULER", "1")

import main

base = sorted(tuple(x) for x in json.load(open("_urlmap_baseline.json", encoding="utf-8")))
now = sorted((str(r.rule), ",".join(sorted(r.methods))) for r in main.app.url_map.iter_rules())
miss = set(base) - set(now)
add = set(now) - set(base)
print("baseline=%d current=%d missing=%d added=%d" % (len(base), len(now), len(miss), len(add)))
for m in sorted(miss):
    print("  MISSING", m)
for a in sorted(add):
    print("  ADDED", a)
print("RESULT:", "IDENTICAL" if not miss and not add else "DIFF")
print("blueprints:", sorted(main.app.blueprints.keys()))
