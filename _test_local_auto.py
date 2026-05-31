# -*- coding: utf-8 -*-
import sqlite3, datetime
con = sqlite3.connect(r'D:\同花顺量化交易\ths_auto_trader\data\users.db')
con.execute("INSERT INTO trade_signals(user_id, code, name, action, price, shares, status, msg, created_at) VALUES (1, '000001', '平安银行', 'BUY', 10.50, 100, 'pending', '本地全自动闭环测试', ?)",
    (datetime.datetime.now().isoformat(),))
con.commit()
nid = con.execute("SELECT id FROM trade_signals ORDER BY id DESC LIMIT 1").fetchone()[0]
con.close()
print(f"inserted id={nid}, 桥接器 1 秒内拉到 -> xiadan 自动按 Y -> 真成交")
