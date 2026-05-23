# -*- coding: utf-8 -*-
"""
诊断测试脚本: 校验 trade_signals 数据库表的插入、拉取和上报反馈操作是否完全正常。
"""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import core.user_manager as um

def run_test():
    print("=== 开始测试 trade_signals 数据库操作 ===")
    
    # 模拟用户 ID = 1 (默认为 admin)
    user_id = 1
    
    # 1. 插入测试买入信号
    print("1. 正在插入测试实盘买入信号...")
    sig_id = um.add_trade_signal(
        user_id=user_id,
        code="600519",
        name="贵州茅台",
        action="BUY",
        price=1688.50,
        shares=100,
        status="pending",
        msg="策略触发测试"
    )
    print(f"   [OK] 信号插入成功，ID = {sig_id}")
    
    # 2. 拉取测试信号
    print("2. 正在拉取待执行信号 (应标记为 sent)...")
    pending = um.get_pending_signals(user_id)
    print(f"   拉取到 {len(pending)} 个 pending 信号")
    for s in pending:
        print(f"   - ID={s['id']}, 代码={s['code']}, 动作={s['action']}, 状态={s['status']}")
        assert s["id"] == sig_id, "信号 ID 不匹配"
        assert s["status"] == "pending", "拉取前状态应为 pending"
    
    # 3. 验证状态是否已被原子性地更新为 'sent'
    # 再次拉取应该为空
    print("3. 再次拉取已下发信号 (应为空，表明原子状态保护生效)...")
    pending_again = um.get_pending_signals(user_id)
    print(f"   再次拉取到 {len(pending_again)} 个信号")
    assert len(pending_again) == 0, "原子状态锁失效，拉取到了已被标记为 sent 的信号"
    print("   [OK] 原子状态保护完全生效！")
    
    # 4. 上报测试执行结果
    print("4. 正在上报下单结果 (executed)...")
    res = um.report_trade_signal(sig_id, "executed", "本地已成功买入成交")
    print(f"   [OK] 结果上报成功，返回数据: {res}")
    assert res["ok"] is True, "上报返回不为 True"
    assert res["code"] == "600519", "返回股票代码不匹配"
    
    # 验证最终数据库状态
    import sqlite3
    c = sqlite3.connect(um.DB_PATH)
    row = c.execute("SELECT status, msg FROM trade_signals WHERE id=?", (sig_id,)).fetchone()
    c.close()
    print(f"5. 校验数据库最终落地状态: 状态={row[0]}, 消息={row[1]}")
    assert row[0] == "executed", "数据库落地状态错误"
    assert row[1] == "本地已成功买入成交", "数据库落地消息错误"
    
    print("\n[OK] All trade_signals DB operations passed successfully!")

if __name__ == "__main__":
    run_test()
