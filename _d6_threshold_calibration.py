# -*- coding: utf-8 -*-
"""
_d6_threshold_calibration.py (D6, 2026-11-06)

向量路距离阈值校准：回答「阈值该定多少、这条路走不走得通」。

方法论（与评测集完全独立——防「阈值版背题」）：
  HIT 组   ：现编口语化新问法，覆盖不同口径，不抄评测集原题 → 应放行的上界
  MISS 组  ：纯无关题（天气/写代码/闲聊）                        → 应拦截的下界
  EDGE 组  ：带领域词的域外题（「美国的转换效率」）               → 特意留的陷阱：
             它们有领域词、语义近，距离可能落在 HIT 区间——预期它们靠域外护栏
             （unknown_region）处理而不是靠距离阈值。看数据验证这个预期。

判定规则：
  间隔 = MISS组top1最小距离 - HIT组top1最大距离
  间隔宽（>=0.05）→ 阈值可行，取间隔中央
  间隔窄/重叠     → cosine 距离信号不稳，换信号（RRF 联合判定），别精调阈值
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

USE_PG = __import__("os").getenv("USE_PG", "") in ("1", "true", "yes")
if not USE_PG:
    print("run with USE_PG=1")
    raise SystemExit(1)

from embed_client import embed_query_cached  # noqa: E402
from db import connect_readonly  # noqa: E402

HIT_QUERIES = [
    "哪些电站今年发的电最多",
    "每个电站的装机规模有多大",
    "光伏板被浪费掉的比例大概是多少",
    "电站平均每天能满负荷发几个小时电",
    "哪个省的电站总发电量最高",
    "逆变器把光转成电的效率怎么样",
    "上个月设备出故障了多少次",
    "发电效率最差的电站有哪些",
    "限电损失的电量怎么查",
    "每个电站当天的日照强度怎么样",
    "各电站的发电利用率是多少",
    "统计一下每个省总共装机了多少兆瓦",
    "今年累计发了多少度电",
    "设备的可用程度怎么样",
    "等效小时数是怎么算出来的",
]

MISS_QUERIES = [
    "今天天气怎么样",
    "你会写代码吗",
    "帮我写一封辞职信",
    "附近有什么好吃的餐厅",
    "1 加 1 等于几",
    "推荐一部电影",
    "股票现在行情如何",
    "怎么快速学会英语",
    "写一首关于秋天的诗",
    "明天要不要带伞",
]

EDGE_QUERIES = [
    "美国的转换效率",
    "北京的装机容量",
    "风力发电机的发电量",
    "特斯拉的电池效率怎么样",
]

REF = "按设备类型统计各电站故障次数，取前 10"  # #25 原题：看它的 top3


def top_k(conn, qvec, k=3):
    rows = conn.execute(
        "SELECT metric_name, embedding <=> %s AS dist FROM metric_glossary "
        "WHERE embedding IS NOT NULL ORDER BY embedding <=> %s LIMIT %s",
        (qvec, qvec, k),
    ).fetchall()
    return [(r["metric_name"], float(r["dist"])) for r in rows]


def report(tag, queries, conn):
    dists = []
    print(f"\n===== {tag} ({len(queries)} 条) =====")
    for q in queries:
        vec = embed_query_cached(q)
        top = top_k(conn, vec, 1)
        name, dist = top[0]
        dists.append(dist)
        print(f"  {dist:.4f}  {q}  ->  {name}")
    dists.sort()
    n = len(dists)
    print(f"  -- min={dists[0]:.4f}  median={dists[n//2]:.4f}  max={dists[-1]:.4f}")
    return dists


def main():
    conn = connect_readonly()

    hit = report("HIT 该命中组", HIT_QUERIES, conn)
    miss = report("MISS 纯无关组", MISS_QUERIES, conn)
    edge = report("EDGE 带领域词域外组", EDGE_QUERIES, conn)

    hit_max = max(hit)
    miss_min = min(miss)
    gap = miss_min - hit_max
    print("\n===== 校准判定 =====")
    print(f"HIT  上界（最远的好命中）  = {hit_max:.4f}")
    print(f"MISS 下界（最近的垃圾命中）= {miss_min:.4f}")
    print(f"间隔 = {gap:.4f}")
    if gap >= 0.05:
        mid = (hit_max + miss_min) / 2
        print(f"判定：间隔可行，建议阈值 = {mid:.3f}（取间隔中央）")
    elif gap > 0:
        print("判定：间隔偏窄，阈值脆弱——题型漂移容易击穿，建议联合关键词信号")
    else:
        print("判定：分布重叠，cosine 距离单独不可用，必须换信号")

    print(f"\n===== EDGE 组对照（预期：距离偏近但由域外护栏处理，不靠阈值）=====")
    for q in EDGE_QUERIES:
        vec = embed_query_cached(q)
        name, dist = top_k(conn, vec, 1)[0]
        in_hit_range = dist <= hit_max
        print(f"  {dist:.4f}  {q} -> {name}  {'⚠落入HIT区间' if in_hit_range else '在HIT区间外'}")

    print(f"\n===== #25 原题 top3（归因复核）=====")
    vec = embed_query_cached(REF)
    for name, dist in top_k(conn, vec, 3):
        print(f"  {dist:.4f}  {name}")
    conn.close()


if __name__ == "__main__":
    main()
