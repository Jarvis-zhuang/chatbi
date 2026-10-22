# -*- coding: utf-8 -*-
"""compare_db.py —— SQLite vs PostgreSQL 迁移对账（W1 D1 派单3 交付）
同种子(20260928)灌的两库，聚合数字必须一致；不一致 = 迁移偷了数据。"""
import sqlite3
import psycopg

SQLITE = "file:D:/code/ai/chatbi/data/chatbi.db?mode=ro"
PG = "host=127.0.0.1 port=5432 dbname=chatbi user=postgres password=postgres"

CHECKS = [
    ("dim_plant 行数", "SELECT COUNT(*) FROM dim_plant", "SELECT COUNT(*) FROM dim_plant"),
    # 方言差异实锤：SQLite ROUND(double,int) 可用；PG 只认 numeric，必须 ::numeric 强转
    # —— 这个坑直接影响评测：模型生成的 SQL 带 ROUND(x,2) 在 PG 上直接报错（派单5 会踩）
    ("dim_plant 容量合计", "SELECT ROUND(SUM(capacity_mw),4) FROM dim_plant",
     "SELECT ROUND(SUM(capacity_mw)::numeric,4) FROM dim_plant"),
    ("dim_plant 平均效率", "SELECT ROUND(AVG(inverter_efficiency),6) FROM dim_plant",
     "SELECT ROUND(AVG(inverter_efficiency)::numeric,6) FROM dim_plant"),
    ("fact_daily 行数", "SELECT COUNT(*) FROM fact_daily_generation",
     "SELECT COUNT(*) FROM fact_daily_generation"),
    ("fact_daily 发电量合计", "SELECT ROUND(SUM(generation_kwh),2) FROM fact_daily_generation",
     "SELECT ROUND(SUM(generation_kwh)::numeric,2) FROM fact_daily_generation"),
    ("fact_daily 弃光合计", "SELECT ROUND(SUM(curtailment_kwh),2) FROM fact_daily_generation",
     "SELECT ROUND(SUM(curtailment_kwh)::numeric,2) FROM fact_daily_generation"),
    ("fact_daily 最新日期", "SELECT MAX(stat_date) FROM fact_daily_generation",
     "SELECT MAX(stat_date) FROM fact_daily_generation"),
    ("fact_daily 最早日期", "SELECT MIN(stat_date) FROM fact_daily_generation",
     "SELECT MIN(stat_date) FROM fact_daily_generation"),
    ("dim_device 行数", "SELECT COUNT(*) FROM dim_device", "SELECT COUNT(*) FROM dim_device"),
    ("fact_fault 行数", "SELECT COUNT(*) FROM fact_device_fault", "SELECT COUNT(*) FROM fact_device_fault"),
    ("fact_fault 停机合计", "SELECT SUM(downtime_minutes) FROM fact_device_fault",
     "SELECT SUM(downtime_minutes) FROM fact_device_fault"),
    ("glossary 行数", "SELECT COUNT(*) FROM metric_glossary", "SELECT COUNT(*) FROM metric_glossary"),
]

GROUP_CHECK = ("SELECT plant_id, ROUND(SUM(generation_kwh),2) FROM fact_daily_generation GROUP BY plant_id ORDER BY plant_id",
               "SELECT plant_id, ROUND(SUM(generation_kwh)::numeric,2) FROM fact_daily_generation GROUP BY plant_id ORDER BY plant_id")

sc = sqlite3.connect(SQLITE, uri=True)
pc = psycopg.connect(PG)

def vals_equal(v1, v2):
    """SQLite ROUND 去尾零、PG numeric 保位数 -> 显示格式可能不同，按数值比"""
    if str(v1) == str(v2):
        return True
    try:
        return abs(float(v1) - float(v2)) < 1e-6
    except (TypeError, ValueError):
        return False

print(f"{'检查项':<24} {'SQLite':>18} {'Postgres':>18}  判定")
all_ok = True
for name, s1, s2 in CHECKS:
    v1 = sc.execute(s1).fetchone()[0]
    v2 = pc.execute(s2).fetchone()[0]
    ok = vals_equal(v1, v2)
    all_ok &= ok
    print(f"{name:<24} {str(v1):>18} {str(v2):>18}  {'OK' if ok else 'DIFF'}")

g1 = sc.execute(GROUP_CHECK[0]).fetchall()
g2 = pc.execute(GROUP_CHECK[1]).fetchall()
same_group = len(g1) == len(g2) and all(
    a[0] == b[0] and vals_equal(a[1], b[1]) for a, b in zip(g1, g2)
)
all_ok &= same_group
print(f"{'按电站分组发电量(60组)':<24} {len(g1):>18} {len(g2):>18}  {'OK' if same_group else 'DIFF'}")
if not same_group:
    for (a, va), (b, vb) in zip(g1, g2):
        if va != vb:
            print(f"  DIFF plant {a}: sqlite={va} pg={vb}")

sc.close()
pc.close()
print()
print("对账结论:", "全部一致 - 迁移零损耗" if all_ok else "存在差异 - 需排查")
