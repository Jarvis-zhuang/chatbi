# -*- coding: utf-8 -*-
"""
_d6_update_glossary.py (D6 #2, 2026-11-06)

把「等效利用小时」的口径定义刷成带聚合语义正例的新版（源：seed_data.py 同步改动）。

为什么单独跑 UPDATE 而不是重灌库：metric_glossary 只是 26 行静态小表，
但 fact_* 业务表是 43,800 行模拟数据，重跑 seed 有回退风险；
UPDATE 只动这一行 definition/formula，其他表零触碰。

两个库都要刷：USE_PG=1 时 retrieve_metrics 读 PG，兜底档读 SQLite——
定义是 prompt 素材，两库不同步 = 方言错配的翻版（prompt 说什么、库给什么要一致）。
"""
import sqlite3
import sys

import psycopg

NEW_DEF = (
    "统计周期内发电量折算到额定容量下的满发小时数，用于横向对比不同规模电站的效率。"
    "单电站＝该站发电量合计÷该站额定容量。多站或省级汇总：先按电站分组算（子查询 GROUP BY 电站，"
    "容量在电站粒度取维表单值），外层再按需聚合或排名"
)
NEW_FORMULA = (
    "单站：SUM(f.generation_kwh)/MAX(p.capacity_mw)/1000；"
    "多站/省级：FROM(按电站 GROUP BY：SUM(f.generation_kwh) AS gen, p.capacity_mw AS cap) "
    "外层 SUM(gen)/SUM(cap)/1000（排名加权）或 AVG(gen/cap/1000)（全省平均）。"
    "禁止对 JOIN 后明细行 SUM(capacity_mw)（容量按天复制会×730），"
    "禁止 SUM(DISTINCT capacity_mw)（同容量的不同电站会被错误合并）"
)

SQL = "UPDATE metric_glossary SET definition = %s, formula = %s WHERE metric_name = %s"
NAME = "等效利用小时"


def update_pg():
    dsn = "host=127.0.0.1 port=5432 dbname=chatbi user=postgres password=postgres"
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(SQL, (NEW_DEF, NEW_FORMULA, NAME))
            print("pg updated rows:", cur.rowcount)
    conn.close()


def update_sqlite():
    path = "data/chatbi.db"
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute(
        "UPDATE metric_glossary SET definition = ?, formula = ? WHERE metric_name = ?",
        (NEW_DEF, NEW_FORMULA, NAME),
    )
    print("sqlite updated rows:", cur.rowcount)
    conn.commit()
    conn.close()


def verify():
    for label, query in (
        ("pg", "SELECT definition, formula FROM metric_glossary WHERE metric_name = '等效利用小时'"),
    ):
        with psycopg.connect(
            "host=127.0.0.1 port=5432 dbname=chatbi user=postgres password=postgres"
        ) as conn:
            row = conn.execute(query).fetchone()
        print(f"[{label}] def[:60] =", row[0][:60])
        print(f"[{label}] formula[:60] =", row[1][:60])
    sconn = sqlite3.connect("file:data/chatbi.db?mode=ro", uri=True)
    row = sconn.execute(
        "SELECT definition, formula FROM metric_glossary WHERE metric_name = '等效利用小时'"
    ).fetchone()
    print("[sqlite] def[:60] =", row[0][:60])
    print("[sqlite] formula[:60] =", row[1][:60])
    sconn.close()


if __name__ == "__main__":
    update_pg()
    update_sqlite()
    verify()
    print("OK" if "--check" not in sys.argv else "check done")
