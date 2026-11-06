# -*- coding: utf-8 -*-
"""
_d6_rollback_glossary.py (D6 基线重锚用，2026-11-06)

把「等效利用小时」回滚到 D5 时的旧定义——这是 _d6_update_glossary.py 的逆操作。
用途：模型漂移后换 glm-5.3-flash 重锚基线，基线必须是「新模型 + 旧代码」，
D6 改动（新定义 + 向量路）才能与之公平对比（控单一变量）。

跑完基线后：重跑 _d6_update_glossary.py（新定义）+ seed_embeddings_pg.py（重灌向量）。
⚠️ 定义一变向量必须重灌——检索层与内容层不同步是 RAG 最隐蔽的静默 bug（见灌库脚本注释 4）。
"""
import sqlite3

import psycopg

OLD_DEF = "统计周期内发电量折算到额定容量下的满发小时数，用于横向对比不同规模电站的效率"
OLD_FORMULA = (
    "SUM(generation_kwh) / 电站额定容量(capacity_mw) / 1000"
    "（分母取电站额定容量，禁止对 JOIN 后的明细行重复求和）"
)

SQL = "UPDATE metric_glossary SET definition = %s, formula = %s WHERE metric_name = %s"
NAME = "等效利用小时"


def main():
    dsn = "host=127.0.0.1 port=5432 dbname=chatbi user=postgres password=postgres"
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(SQL, (OLD_DEF, OLD_FORMULA, NAME))
            print("pg rollback rows:", cur.rowcount)
    conn.close()

    sconn = sqlite3.connect("data/chatbi.db")
    cur = sconn.cursor()
    cur.execute(
        "UPDATE metric_glossary SET definition = ?, formula = ? WHERE metric_name = ?",
        (OLD_DEF, OLD_FORMULA, NAME),
    )
    print("sqlite rollback rows:", cur.rowcount)
    sconn.commit()
    sconn.close()


if __name__ == "__main__":
    main()
