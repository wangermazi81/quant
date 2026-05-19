# -*- coding: utf-8 -*-
"""
合并数据库：
- 保留服务器的用户注册数据 (users 表)
- 把本地的 AI 配置、用户设置 同步到服务器数据库
生成 data/users_merged.db，上传替换服务器的 users.db
"""
import sqlite3, shutil, os

LOCAL  = "data/users.db"          # 本地数据库（含 API 配置）
SERVER = "data/users_server.db"   # 从服务器下载的数据库
OUTPUT = "data/users_merged.db"   # 合并结果

def tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return [r[0] for r in rows]

def main():
    if not os.path.exists(SERVER):
        print(f"❌ 找不到 {SERVER}，请先执行第一步下载服务器数据库")
        return

    # 以服务器数据库为基础
    shutil.copy2(SERVER, OUTPUT)
    print(f"✅ 以服务器数据库为基础: {OUTPUT}")

    local_conn  = sqlite3.connect(LOCAL)
    merged_conn = sqlite3.connect(OUTPUT)

    local_tables  = tables(local_conn)
    merged_tables = tables(merged_conn)

    print(f"\n本地数据库表: {local_tables}")
    print(f"服务器数据库表: {merged_tables}")

    # 需要从本地同步的配置表（不包含 users 表，保留服务器注册用户）
    SYNC_TABLES = [t for t in local_tables if t not in ("users", "sqlite_sequence")]

    for tbl in SYNC_TABLES:
        if tbl not in merged_tables:
            # 服务器没有这张表，直接创建并复制
            ddl = local_conn.execute(
                f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{tbl}'"
            ).fetchone()
            if ddl and ddl[0]:
                merged_conn.execute(ddl[0])
                print(f"  📋 创建新表: {tbl}")

        rows = local_conn.execute(f"SELECT * FROM {tbl}").fetchall()
        if not rows:
            print(f"  ⏭  {tbl} 本地无数据，跳过")
            continue

        # 获取列名
        cols = [d[0] for d in local_conn.execute(f"SELECT * FROM {tbl} LIMIT 1").description]
        placeholders = ",".join(["?"] * len(cols))
        col_names = ",".join(cols)

        try:
            merged_conn.executemany(
                f"INSERT OR REPLACE INTO {tbl} ({col_names}) VALUES ({placeholders})",
                rows
            )
            print(f"  ✅ 同步 {tbl}: {len(rows)} 条记录")
        except Exception as e:
            print(f"  ⚠️  同步 {tbl} 失败: {e}")

    merged_conn.commit()
    local_conn.close()
    merged_conn.close()
    print(f"\n🎉 合并完成！结果文件: {OUTPUT}")
    print("下一步：把 users_merged.db 上传到服务器替换 users.db")

if __name__ == "__main__":
    main()
