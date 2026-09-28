# -*- coding: utf-8 -*-
"""
db.py —— 只读数据库连接与执行

为什么只读？ see text2sql.validate_sql 的第四层：DB 账号本身只读是「物理保险」，
前面三层（白名单 / 黑名单 / 禁止多语句）都被绕过时也删不掉数据。
"""
import os
import sqlite3
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "chatbi.db")

MAX_ROWS = 200          # 返回结果行数上限：防止「查全国所有明细」把内存打爆
QUERY_TIMEOUT = 5.0     # 秒：慢查询直接掐断


def connect_readonly():
    """只读连接（SQLite 用 URI mode=ro；D2 换成 Postgres 时对应 mode=ro 的账号）"""
    if not os.path.exists(DB_PATH):
        raise RuntimeError(f"database not found: {DB_PATH} (run: python seed_data.py)")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=QUERY_TIMEOUT)
    conn.row_factory = sqlite3.Row
    return conn


def execute_sql(sql):
    """执行只读 SQL，返回 (columns, rows, elapsed_ms)"""
    conn = connect_readonly()
    try:
        cur = conn.cursor()
        t0 = time.perf_counter()
        cur.execute(sql)
        rows = cur.fetchmany(MAX_ROWS)
        cols = [d[0] for d in cur.description] if cur.description else []
        elapsed = round((time.perf_counter() - t0) * 1000, 1)
        data = [list(r) for r in rows]
        return cols, data, elapsed
    finally:
        conn.close()
