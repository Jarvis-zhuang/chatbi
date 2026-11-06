# -*- coding: utf-8 -*-
"""
db.py —— 只读数据库连接与执行（SQLite / Postgres 双模式）

为什么只读？ see text2sql.validate_sql 的第四层：DB 账号本身只读是「物理保险」，
前面三层（白名单 / 黑名单 / 禁止多语句）都被绕过时也删不掉数据。

双模式（W1 D1 派单4）：环境变量 USE_PG=1 → Postgres；否则 → SQLite（mode=ro 兜底，原样保留）。
切换不动 text2sql.py / eval.py 的调用方式——它们只 import connect_readonly / execute_sql。

L3 只读保险平移：2026-10-21 庄拍板 A（chatbi_ro 只读账号，见 pg_readonly_setup.sql），
不选 B（SET TRANSACTION READ ONLY 是约定不是强制）。B 为上云降级，记入 README。
"""
import os
import sqlite3
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "chatbi.db")

MAX_ROWS = 200          # 返回结果行数上限：防止「查全国所有明细」把内存打爆
QUERY_TIMEOUT = 5.0     # 秒：慢查询直接掐断

# ---- Postgres 连接（USE_PG=1 时生效；默认值对齐本机 pg 容器）----
# statement_timeout=5000：PG 的「执行上限」毫秒数。
# ⚠️ 方言差异（面试点）：SQLite 的 timeout= 是【锁等待】上限，不是执行上限；
#    PG 要掐慢查询必须显式 statement_timeout，否则默认不限时——一个慢 SQL 能挂住连接。
# 注意：options 必须走 kwargs 传——写进 DSN 串时 psycopg 会按空格拆参，
#    "-c statement_timeout=5000" 会被拆成 "-c" 和 "statement_timeout=5000" 两截报错
#    （本文件 2026-10-21 实踩：FATAL: -c ... requires a value）。
#
# 2026-10-21 庄拍板 A（L3 只读保险）：读路径默认走 chatbi_ro 只读账号——
# LLM 是不可信组件，四层护栏全是应用层代码，账号级只读是数据库强制执行的最后一堵墙
# （最小权限原则 + 纵深防御；不信任自己写的校验代码）。落地凭证：pg_readonly_setup.sql。
# 管理/写路径（seed_data_pg.py / compare_db.py）仍用 postgres，不受影响。
# 上 Neon/Supabase 免费档不让建角色时，降级 B（SET TRANSACTION READ ONLY），记入 README。
PG_CONN_KWARGS = dict(
    host=os.environ.get("CHATBI_PG_HOST", "127.0.0.1"),
    port=int(os.environ.get("CHATBI_PG_PORT", "5432")),
    dbname=os.environ.get("CHATBI_PG_DB", "chatbi"),
    user=os.environ.get("CHATBI_PG_USER", "chatbi_ro"),
    password=os.environ.get("CHATBI_PG_PASSWORD", "chatbi_ro"),
    options="-c statement_timeout=5000",
    # connect_timeout：连接【建立】的上限（秒），与 statement_timeout（执行上限）是两回事。
    # 2026-11-06 实踩：Docker daemon 半死时端口 listening 但不响应，psycopg 默认
    # 连接等待极长——statement_timeout 管不到还没建立的连接。没有它，
    # retrieve_metrics 的「挂了就降级」形同虚设：降级靠异常触发，hang 不抛异常。
    connect_timeout=3,
)
PG_DSN = os.environ.get("CHATBI_PG_DSN")  # 整串覆盖口子（连 Neon/Supabase 时用）

USE_PG = os.environ.get("USE_PG", "").strip().lower() in ("1", "true", "yes")

# 2026-10-22（W1 D2）：方言的单一事实源。
# 只有这一层知道「现在连的是哪个库」，text2sql 的 prompt 从它派生，不自己再判断一遍。
# 为什么不让 text2sql 各自写死「SQLite SQL」？因为 prompt 说的是模型该写什么方言，
# 连接决定的是数据库认什么方言——两处分开维护，就会出现「prompt 说 SQLite、实际连 PG」
# 的静默错配：错不在模型（它严格照 prompt 写了），错在系统从第一天只会说一种方言。
DIALECT = "postgres" if USE_PG else "sqlite"


def connect_readonly():
    """只读连接。
    SQLite：URI mode=ro —— 文件级只读，物理保险（原样保留）。
    Postgres：chatbi_ro 只读账号（2026-10-21 拍板 A）——账号级只读，
    写操作在数据库层被拒（已实测 INSERT/CREATE TABLE 均 permission denied）。
    """
    if USE_PG:
        import psycopg  # 延迟导入：SQLite 模式不依赖 psycopg，兜底路径零新依赖
        from psycopg.rows import dict_row
        # psycopg3 位置参数只收 conninfo 字符串；kwargs 必须全部走关键字传
        conn = psycopg.connect(PG_DSN) if PG_DSN else psycopg.connect(**PG_CONN_KWARGS)
        # ⚠️ 方言差异（2026-10-21 回归实踩）：text2sql.retrieve_metrics 用 r["metric_name"]
        # 按列名取值——SQLite 侧靠 row_factory = sqlite3.Row 撑着，psycopg 默认返回纯 tuple
        # 会直接 TypeError。dict_row 让 PG 侧行访问契约与 SQLite 对齐（返回 dict）。
        conn.row_factory = dict_row
        conn.autocommit = True  # 只读逐条执行，不开显式事务，避免 idle-in-transaction
        return conn
    if not os.path.exists(DB_PATH):
        raise RuntimeError(f"database not found: {DB_PATH} (run: python seed_data.py)")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=QUERY_TIMEOUT)
    conn.row_factory = sqlite3.Row
    return conn


def execute_sql(sql):
    """执行只读 SQL，返回 (columns, rows, elapsed_ms) —— 双模式共用，调用方无感。

    对外契约：rows 永远是「值列表的列表」——SQLite 和 PG 两个驱动必须在此归一化，
    差异不外泄给 text2sql/eval。2026-10-21 实踩：psycopg dict_row 下 list(dict)
    拿到的是键名不是值，锚点日期变成字符串 'max'，一路传到 _prev_month 才炸
    （故障点离根因三公里）——适配层不归一化，方言差异就会以最远的事故点爆发。
    """
    conn = connect_readonly()
    try:
        cur = conn.cursor()
        t0 = time.perf_counter()
        cur.execute(sql)
        rows = cur.fetchmany(MAX_ROWS)
        cols = [d[0] for d in cur.description] if cur.description else []
        elapsed = round((time.perf_counter() - t0) * 1000, 1)
        data = [list(r.values()) if isinstance(r, dict) else list(r) for r in rows]
        return cols, data, elapsed
    finally:
        conn.close()
