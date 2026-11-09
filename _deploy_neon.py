# -*- coding: utf-8 -*-
r"""
_deploy_neon.py —— Neon 一键部署脚本（2026-11-09 · D7 上云）

本机没有 psql 客户端，用 venv 里的 psycopg 替代执行 SQL 文件：
- 过滤 psql 元命令（\\c / \\set），其余 SQL 原样下发（DO $$ 块直接执行，不做字符串切割）
- pg_readonly_setup.sql 的弱密码 'chatbi_ro' 在执行前替换成 CHATBI_RO_PASSWORD
  （公网可直连 Neon，弱密码不合适——不设该变量脚本直接拒绝跑，硬约束优于口头提醒）

用法（PowerShell）：
  $env:CHATBI_ADMIN_DSN = "postgresql://<user>:<pass>@<ep>.neon.tech/neondb?sslmode=require"
  $env:CHATBI_RO_PASSWORD = "<强密码>"
  .\.venv\Scripts\python.exe _deploy_neon.py

流程：CREATE DATABASE chatbi → 建表(pg_schema) → 灌数(seed_data_pg)
      → 灌向量(seed_embeddings_pg，需 .env 里的智谱 key) → 只读账号(pg_readonly_setup)
      → 自证（表数 / 向量数 / chatbi_ro 只读实测）

幂等：重复跑安全（CREATE DATABASE 捕获已存在、seed TRUNCATE 重灌、CREATE OR REPLACE）。
"""
import os
import sys

import psycopg

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ADMIN_DSN = os.environ.get("CHATBI_ADMIN_DSN", "").strip()
RO_PASSWORD = os.environ.get("CHATBI_RO_PASSWORD", "").strip()


def die(msg: str):
    print(f"[deploy] ❌ {msg}")
    sys.exit(1)


def to_chatbi_dsn(dsn: str) -> str:
    """把 .../neondb?params 的库名段替换成 chatbi（psycopg 只认单库连接）。"""
    head, sep_tail = dsn.rpartition("/")
    db_part = sep_tail.split("?", 1)
    print(f"[deploy] 库名 {db_part[0]!r} → 'chatbi'")
    return head + "/chatbi" + (("?" + db_part[1]) if len(db_part) > 1 else "")


def run_sql_file(conn, path: str):
    """执行 .sql 文件：过滤 psql 元命令行（\\c、\\set），整串下发（支持 DO $$）。"""
    with open(path, encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines()
                 if ln.lstrip() and not ln.lstrip().startswith("\\")]
    sql = "\n".join(lines)
    conn.execute(sql)
    print(f"[deploy] ✅ {os.path.basename(path)}")


def main():
    if not ADMIN_DSN:
        die("请先设置 CHATBI_ADMIN_DSN（Neon 控制台复制的连接串）")
    if not RO_PASSWORD or len(RO_PASSWORD) < 12:
        die("请先设置 CHATBI_RO_PASSWORD（≥12 位强密码——公网可直连 Neon，弱密码不行）")

    # 1. 连默认库，建 chatbi（CREATE DATABASE 不能在事务里 → autocommit）
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        try:
            conn.execute("CREATE DATABASE chatbi")
            print("[deploy] ✅ CREATE DATABASE chatbi")
        except psycopg.errors.DuplicateDatabase:
            print("[deploy] ℹ️ chatbi 已存在，跳过")

    # 2. 后续全部指向 chatbi 库；seed 脚本 import 时读同一 env
    chatbi_dsn = to_chatbi_dsn(ADMIN_DSN)
    os.environ["CHATBI_ADMIN_DSN"] = chatbi_dsn

    with psycopg.connect(chatbi_dsn, autocommit=True) as conn:
        run_sql_file(conn, os.path.join(BASE_DIR, "pg_schema.sql"))

    # 3. 灌数据 + 灌向量（embed_client 内部 load_dotenv，智谱 key 从 .env 来）
    import seed_data_pg
    seed_data_pg.main()
    import seed_embeddings_pg
    seed_embeddings_pg.main()

    # 4. 只读账号 + 方言垫片（弱密码替换）
    ro_sql_path = os.path.join(BASE_DIR, "pg_readonly_setup.sql")
    with open(ro_sql_path, encoding="utf-8") as f:
        ro_sql = f.read().replace("PASSWORD 'chatbi_ro'", f"PASSWORD '{RO_PASSWORD}'")
    ro_lines = [ln for ln in ro_sql.splitlines()
                if ln.lstrip() and not ln.lstrip().startswith("\\")]
    with psycopg.connect(chatbi_dsn, autocommit=True) as conn:
        conn.execute("\n".join(ro_lines))
    print("[deploy] ✅ pg_readonly_setup.sql（密码已替换，未落盘明文）")

    # 5. 自证
    print("\n[deploy] ===== 自证 =====")
    with psycopg.connect(chatbi_dsn) as conn:
        tables = conn.execute(
            "SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY relname").fetchall()
        for name, n in tables:
            print(f"  {name:<28} {n} 行")
        emb = conn.execute(
            "SELECT COUNT(*), COUNT(embedding) FROM metric_glossary").fetchone()
        print(f"  metric_glossary 向量覆盖: {emb[1]}/{emb[0]}")

    ro_kwargs = {"user": "chatbi_ro", "password": RO_PASSWORD}
    with psycopg.connect(chatbi_dsn, **ro_kwargs) as conn:
        n = conn.execute("SELECT COUNT(*) FROM dim_plant").fetchone()[0]
        print(f"  chatbi_ro SELECT dim_plant → {n} 行 ✅")
        try:
            conn.execute("INSERT INTO dim_plant(plant_name) VALUES('x')")
            die("chatbi_ro 竟然能 INSERT——只读护栏失效！")
        except psycopg.errors.InsufficientPrivilege:
            print("  chatbi_ro INSERT → permission denied ✅（L3 只读保险生效）")

    print(f"""
[deploy] 🎉 灌库完成。下一步（Render 环境变量，值抄这里）：
  USE_PG=1
  CHATBI_PG_DSN=postgresql://chatbi_ro:{RO_PASSWORD}@<你的ep>.neon.tech/chatbi?sslmode=require
  DEEPSEEK_API_KEY=<.env 里抄>
  ZHIPU_API_KEY=<.env 里抄>
  CHATBI_ACCESS_CODE=<自定一个演示访问码>""")


if __name__ == "__main__":
    main()
