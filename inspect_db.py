# -*- coding: utf-8 -*-
import sqlite3

conn = sqlite3.connect('data/users.db')
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()

for (t,) in tables:
    try:
        rows = conn.execute(f"SELECT * FROM {t}").fetchall()
        if not rows:
            print(f"\n=== {t} (空表) ===")
            continue
        cols = [d[0] for d in conn.execute(f"SELECT * FROM {t} LIMIT 1").description]
        print(f"\n=== {t} ({len(rows)} rows) ===")
        print("columns:", cols)
        for r in rows[:5]:
            print(r)
    except Exception as e:
        print(f"\n=== {t} ERROR: {e} ===")

conn.close()
