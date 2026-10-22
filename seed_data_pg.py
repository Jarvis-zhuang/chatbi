# -*- coding: utf-8 -*-
"""
seed_data_pg.py —— ChatBI 灌数脚本 · PostgreSQL 版（W1 D1 派单2/3 交付）

设计原则：**保真**。直接 import seed_data 复用全部生成逻辑（random.seed(20260928)
在 seed_data 模块级固定），本文件只提供一层方言适配 + PG 连接。
同一个种子 + 同一套调用序列 => PG 里的数据和 SQLite 版逐字节一致，
这是「迁移没有偷走数据」的证明方式（W1 D1 验收三对照之一）。

方言差异处理：
- 占位符：sqlite3 的 :name / ?  ->  psycopg 的 %(name)s / %s（正则替换）
- executescript（建表 DDL）-> 表已由 pg_schema.sql 建好，这里只 TRUNCATE 清空重灌（幂等）
- REAL(8字节) -> DOUBLE PRECISION(8字节)，Python float 原样插入，无精度换算
"""
import re
import psycopg

import seed_data
from seed_data import gen_plants, gen_daily, gen_devices_and_faults, load_glossary

DSN = "host=127.0.0.1 port=5432 dbname=chatbi user=postgres password=postgres"


class PGCompat:
    """把 psycopg 连接包装成 seed_data 期望的 sqlite3 接口（最小面）"""

    def __init__(self, conn):
        self._conn = conn
        self._cur = conn.cursor()

    def cursor(self):
        return self._cur

    def commit(self):
        self._conn.commit()

    def executescript(self, sql):
        # 不建表（pg_schema.sql 已建）；清空重灌，RESTART IDENTITY 让自增列归位
        self._cur.execute(
            "TRUNCATE fact_daily_generation, fact_device_fault, dim_device, "
            "dim_plant, metric_glossary RESTART IDENTITY"
        )

    def executemany(self, sql, params):
        sql = re.sub(r":(\w+)", r"%(\1)s", sql)   # :plant_id -> %(plant_id)s
        sql = sql.replace("?", "%s")               # 位置占位
        self._cur.executemany(sql, params)


def main():
    with psycopg.connect(DSN) as conn:
        w = PGCompat(conn)
        w.executescript(None)                      # TRUNCATE
        w.commit()
        plants = gen_plants(w)
        gen_daily(w, plants)
        gen_devices_and_faults(w, plants)
        load_glossary(w)
        w.commit()

        cur = w.cursor()
        stats = {
            "plants": cur.execute("SELECT COUNT(*) FROM dim_plant").fetchone()[0],
            "daily": cur.execute("SELECT COUNT(*) FROM fact_daily_generation").fetchone()[0],
            "devices": cur.execute("SELECT COUNT(*) FROM dim_device").fetchone()[0],
            "faults": cur.execute("SELECT COUNT(*) FROM fact_device_fault").fetchone()[0],
            "glossary": cur.execute("SELECT COUNT(*) FROM metric_glossary").fetchone()[0],
            "max_date": cur.execute("SELECT MAX(stat_date) FROM fact_daily_generation").fetchone()[0],
        }
    print("[seed_pg] dsn =", DSN)
    for k, v in stats.items():
        print(f"[seed_pg] {k:<10} = {v}")


if __name__ == "__main__":
    main()
