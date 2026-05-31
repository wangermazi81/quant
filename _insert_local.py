# -*- coding: utf-8 -*-
import sqlite3, datetime
con = sqlite3.connect(r'D:\同花顺量化交易\ths_auto_trader\data\users.db')
con.execute("INSERT INTO trade_signals(user_id, code, name, action, price, shares, status, msg, created_at) VALUES (1, '000001', '平安银行', 'BUY', 10.50, 100, 'pending', '本地全闭环测试', ?)",
    (datetime.datetime.now().isoformat(),))
con.commit()
nid = con.execute("SELECT id FROM trade_signals ORDER BY id DESC LIMIT 1").fetchone()[0]
con.close()
print(f"✅ 本地插入 trade_signal id={nid}, 1 秒内桥接器会拉到 → xiadan 弹买入页 + 委托确认框")
