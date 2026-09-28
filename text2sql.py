# -*- coding: utf-8 -*-
"""
text2sql.py —— ChatBI 全链路核心文件（D1 步骤 4 要精读的就是这个文件）

链路：选表(RAG) → 业务口径检索(RAG) → 生成 SQL → 安全校验 → 只读执行
      → 失败自修正（上限 2 次）→ 结果 → 图表

每条链路节点上都标了对应的面试题（格式：# 考点 N：xxx），对照总纲第六节看。

--------------------------------------------------------------------
D1 精读路线（623 行里真正要读的只有约 277 行，别从头啃）
--------------------------------------------------------------------
【精读】按这个顺序，约 300 行
  1. TABLE_SCHEMA        L54-110   表注释写的是业务口径不是字段名 —— D4 提准确率的主战场
  2. select_tables()     L117-138  选表 RAG：为什么不全塞 prompt（考点 1）
  3. render_schema()     L139-152  怎么把选中的表渲染进 prompt
  4. retrieve_metrics()  L153-186  业务口径检索 RAG —— 本项目的隐藏王牌（考点 10）
  5. 护栏三件套           L220-266  unknown_region / is_out_of_scope / is_meta_question
                                    防模型瞎编：元问题门 + 域外门 + 地域实体校验
  6. llm_generate_sql()  L531-552  真模型生成路径（22 行，很短）
  7. validate_sql()      L558-578  四层安全校验（考点 3，面试必考）
  8. run_sql()           L580-590  只读执行 + 行数/超时封顶（11 行）
  9. ask_stream()        L592-651  ★ 最核心。全链路编排 + 自修正循环（考点 2/4/8/9）

【跳读 / 不看】
  - mock_generate_sql() L336-530  195 行！MOCK 规则引擎，只为免 API Key 跑通链路。
                                    D4 接真模型后被 llm_generate_sql 取代，读了不值。
  - 辅助函数            L291-335  日期/同比环比/省份解析，用到再看
  - ask()               L652-686  ask_stream 的同步包装版，同一套逻辑
  - 常量                L44-49    扫一眼知道 MAX_RETRY/ROW_LIMIT/TIMEOUT 三个上限即可

读的时候每个函数问自己一句：「面试官问 XX，我能指着这段讲吗？」讲不出就重读。
"""
from __future__ import annotations

import json
import os
import re
from typing import Iterator

from db import execute_sql, connect_readonly

# ============================================================ 配置
MAX_RETRY = 2           # 考点 4：自修正硬上限，防 Agent 死循环
ROW_LIMIT = 200         # 考点 4：结果行数封顶
TIMEOUT_S = 5           # 考点 4：执行超时

USE_LLM = bool(os.getenv("OPENAI_API_KEY"))  # 没配 key 就跑 MOCK 模式，链路照跑

# ============================================================ 元数据层
# 考点 1：为什么不把所有表结构塞进 prompt？
#   真实业务库几百张表，全塞既超上下文预算，又会显著拉低准确率（噪声增多）。
#   所以先 select_tables() 做检索，只把和问题相关的 2-3 张表喂给模型。
TABLE_SCHEMA = {
    "dim_plant": {
        "desc": "电站主数据表（维度表）。每个电站一行，记录装机规模、地域、类型与逆变器品牌",
        "columns": {
            "plant_id": "电站唯一 ID",
            "plant_name": "电站名称",
            "province": "所在省份（如青海、新疆、江苏）",
            "region": "所属区域（西北/华北/华东/华中/南方）",
            "station_type": "电站类型：集中式=大基地地面电站；分布式=屋顶/地面小规模电站",
            "capacity_mw": "装机容量（MW），代表这座电站的最大出力能力",
            "inverter_brand": "逆变器品牌（阳光电源、华为等）",
            "inverter_efficiency": "逆变器加权转换效率（0-1 之间的小数，越高越好）",
            "grid_connect_date": "并网日期",
        },
    },
    "fact_daily_generation": {
        "desc": "日发电明细事实表。每个电站每天一行，是查询发电量/利用小时/弃光率的唯一来源",
        "columns": {
            "plant_id": "电站 ID，关联 dim_plant.plant_id",
            "stat_date": "统计日期，格式 YYYY-MM-DD",
            "generation_kwh": "当日发电量（kWh），即当天发了多少度电",
            "irradiation": "当日水平面辐照量（kWh/m2），驱动发电量的核心因子",
            "equivalent_hours": "当日等效利用小时（h）= 当日发电量 / 装机千瓦",
            "curtailment_kwh": "当日弃光电量（kWh），因电网限发而没发出来的电",
            "curtailment_rate": "当日弃光率（0-1 小数）= 弃光电量 / 理论发电量",
        },
    },
    "dim_device": {
        "desc": "设备台账表。记录每个电站下挂了哪些设备",
        "columns": {
            "device_id": "设备唯一 ID",
            "plant_id": "所属电站 ID，关联 dim_plant.plant_id",
            "device_type": "设备类型（逆变器/汇流箱/变压器等）",
            "device_model": "设备型号",
        },
    },
    "fact_device_fault": {
        "desc": "设备故障事实表。每次故障一行，用于统计故障次数与停机时长",
        "columns": {
            "id": "记录 ID",
            "device_id": "设备 ID，关联 dim_device.device_id",
            "plant_id": "电站 ID，关联 dim_plant.plant_id",
            "fault_date": "故障发生日期 YYYY-MM-DD",
            "fault_type": "故障类型（逆变器过温、通信中断等）",
            "downtime_minutes": "停机时长（分钟）",
        },
    },
}

# 表检索用的关键词权重（真实项目里这一层会用向量召回，这里先做可解释的加权检索）
TABLE_KEYWORDS = {
    "dim_plant": ["装机", "容量", "省份", "电站", "集中式", "分布式", "逆变器", "品牌", "效率", "并网"],
    "fact_daily_generation": ["发电", "电量", "利用小时", "弃光", "辐照", "限发", "月度", "同比", "环比", "趋势", "日均"],
    "dim_device": ["设备", "型号", "台账", "台数"],
    "fact_device_fault": ["故障", "告警", "停机", "缺陷", "MTBF", "MTTR", "运维"],
}

REFUSE_TEMPLATE = (
    "这个问题不在我的问数范围内：我只基于光伏电站数据库做查询统计分析。"
    "可以试试：各省份装机容量排名 / 上个月总发电量 / 弃光率高于 5% 的电站 / 等效利用小时是怎么定义的。"
)

# ============================================================ 第 1 站：选表（RAG）
def select_tables(question: str, top_k: int = 3) -> list[str]:
    """
    考点 1（为什么不把所有表塞进 prompt）+ 考点 10（上下文太长怎么办）
      → 三策略：截断 / 总结 / 检索。这里选「检索」，只取相关的 top_k 张表。
    """
    scores = {}
    for table, keywords in TABLE_KEYWORDS.items():
        score = sum(1 for k in keywords if k in question)
        # 事实表是问数主战场，给一点点先验权重
        if table.startswith("fact_"):
            score += 0.5
        scores[table] = score
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    picked = [t for t, s in ranked[:top_k]]
    # 兜底：至少带上两个主表，避免新手段句什么都查不出来
    if "fact_daily_generation" not in picked:
        picked.append("fact_daily_generation")
    if "dim_plant" not in picked:
        picked.append("dim_plant")
    return picked


def render_schema(tables: list[str]) -> str:
    """把表结构渲染成 prompt 片段。注意注释写的是「业务口径」而不是字段名本身，
    这是 D4 把准确率从 61% 拉到 84% 最主要的那一刀。"""
    lines = []
    for t in tables:
        meta = TABLE_SCHEMA[t]
        lines.append(f"-- {t}：{meta['desc']}")
        for col, comment in meta["columns"].items():
            lines.append(f"--   {t}.{col}：{comment}")
        lines.append(f"CREATE TABLE {t} ({', '.join(meta['columns'].keys())});")
    return "\n".join(lines)


# ============================================================ 第 2 站：业务口径检索（RAG）
def retrieve_metrics(question: str, limit: int = 3) -> list[dict]:
    """
    考点 5：RAG 在 ChatBI 里真正的用武之地不是文档问答，而是「业务口径检索」。
    「发电效率」「等效利用小时」这些指标定义必须先检索出来，SQL 才可能算对。
    D5 会把这一层换成 pgvector 真向量召回 + Postgres 全文检索双路 RRF，
    现在先用关键词 + 别名匹配把链路跑通（可替换点，不改上层逻辑）。
    """
    conn = connect_readonly()
    try:
        rows = conn.execute(
            "SELECT metric_name, aliases, definition, formula, unit, source_doc FROM metric_glossary"
        ).fetchall()
    finally:
        conn.close()

    scored = []
    for r in rows:
        score = 0
        if r["metric_name"] in question:
            score += 10
        for alias in (r["aliases"] or "").split(","):
            alias = alias.strip()
            if alias and alias in question:
                score += 6
        # 问法加分只对「已经命中指标名/别名」的条目生效——
        # 否则「天气怎么样」会因为带个「怎么」让所有口径都得分（D1 实测踩过的坑）
        if score and re.search(r"(怎么|如何|什么是|计算公式|定义|口径|怎么算|什么意思)", question):
            score += 2
        if score:
            scored.append((score, dict(r)))
    scored.sort(key=lambda x: -x[0])
    return [s[1] for s in scored[:limit]]


# ============================================================ 第 3 站：生成 SQL
METRIC_RULES = [
    # (命中关键词, 聚合表达式, 需要的事实表, 默认聚合维度含义, 输出列名)
    (["装机", "容量", "装机容量"], "SUM(p.capacity_mw)", [], "装机容量(MW)"),
    (["发电量", "发电", "电量", "度电"], "SUM(f.generation_kwh)", ["fact_daily_generation"], "发电量(kWh)"),
    (["等效利用小时", "利用小时", "发电小时"], "SUM(f.generation_kwh)/NULLIF(SUM(p.capacity_mw),0)/1000",
     ["fact_daily_generation"], "等效利用小时(h)"),
    (["弃光率", "限发率"], "SUM(f.curtailment_kwh)/NULLIF(SUM(f.generation_kwh)+SUM(f.curtailment_kwh),0)",
     ["fact_daily_generation"], "弃光率"),
    (["弃光电量", "限发电量"], "SUM(f.curtailment_kwh)", ["fact_daily_generation"], "弃光电量(kWh)"),
    (["辐照", "峰值日照"], "AVG(f.irradiation)", ["fact_daily_generation"], "辐照量(kWh/m2)"),
    (["转换效率", "逆变器效率", "逆变效率"], "AVG(p.inverter_efficiency)", [], "逆变器转换效率"),
    (["故障次数", "故障"], "COUNT(*)", ["fact_device_fault"], "故障次数"),
]

META_PATTERNS = [
    r"你能(查|做|回答)什么", r"你(能|可以)查(哪些|什么)", r"你的知识库", r"上线后.*困难",
    r"你是谁", r"介绍一下你自己",
]

# 领域词表：问题里一个都没命中 = 大概率是闲聊/域外问题（天气、写诗、闲扯）。
# 这是负样本护栏的第二道门：第一道拦「问能力边界」，这道拦「压根不是问数」。
DOMAIN_HINTS = [
    "装机", "容量", "发电", "电量", "度电", "利用小时", "发电小时", "弃光", "限发", "辐照",
    "逆变器", "转换效率", "逆变", "电站", "光伏", "故障", "设备", "告警", "停机", "并网",
    "同比", "环比", "趋势", "走势", "占比", "构成", "比例", "排名", "汇总", "季度", "月度",
    "口径", "定义", "公式", "指标", "区域", "省份",
]
# （省份名单在下方 PROVINCES 定义处追加进 DOMAIN_HINTS）


# 地域实体不在库内：数据只覆盖国内 14 省，问题主体落在别处时问什么都该拒答。
# （含境外国家 + 直辖市/特别行政区 + 泛境外词；「深圳/上海」这类不带省市后缀的地名也得靠它拦）
FOREIGN_HINTS = [
    "美国", "日本", "韩国", "印度", "德国", "法国", "英国", "俄罗斯", "欧洲",
    "澳洲", "澳大利亚", "加拿大", "巴西", "非洲", "越南", "泰国", "新加坡",
    "北京", "上海", "天津", "重庆", "香港", "澳门", "台湾",
    "国外", "海外", "境外", "全球", "世界", "国际",
]

# 「X省 / X县」模式：抽出来的名字必须在本库覆盖的省份里，否则视为域外实体。
# 不含「市」——「各城市/上市/超市」误伤太多，直辖市已由 FOREIGN_HINTS 兜住。
_UNKNOWN_REGION_RE = re.compile(r"([\u4e00-\u9fa5]{2,3})(?:省|县)")
_NUM_WORDS = "一二两三四五六七八九十几多数每"


def unknown_region(question: str) -> bool:
    """实体链接的简版：问题里出现的地域必须在本库覆盖范围内。
    护栏的另一半——不只拦「无关问题」，还要拦「实体不在库里」的问题，
    否则「美国的转换效率」照样硬生成一条全国 SQL（2026-09-27 用户实测撞出的真 bug）。"""
    if any(k in question for k in FOREIGN_HINTS):
        return True
    for m in _UNKNOWN_REGION_RE.findall(question):
        if m in PROVINCES:
            continue
        # 「各省」「两个省」「多数省份」这类泛指不是具体地名，放行
        if m.endswith("各") or any(ch in _NUM_WORDS for ch in m):
            continue
        return True
    return False


def is_out_of_scope(question: str) -> bool:
    """域外问题判定：不是元问题、也摸不到任何领域词 → 礼貌拒答，不硬生成 SQL"""
    if is_meta_question(question):
        return False
    if unknown_region(question):
        return True
    if any(k in question for k in DOMAIN_HINTS):
        return False
    # 兜底：命中了业务口径的（如「等效利用小时」的别名「发电小时数」）不算域外
    return not retrieve_metrics(question, limit=1)


def is_meta_question(question: str) -> bool:
    """负样本护栏：真实用户一半的问题在试探边界，这时不该硬生成 SQL。
    对应总纲 D3「评测集负样本」与 D4「提问五要素」里的拒答路径。"""
    return any(re.search(p, question) for p in META_PATTERNS)


def pick_metric(question: str):
    for keys, expr, tables, label in METRIC_RULES:
        if any(k in question for k in keys):
            return expr, tables, label
    return "SUM(f.generation_kwh)", ["fact_daily_generation"], "发电量(kWh)"


def _limit_of(question: str, default=None):
    """问题里写明了「前 N / top N / N 个」才加 LIMIT，否则默认返回全部——
    既接近真实 BI 行为，也避免和评测集里的期望 SQL 对不上行。"""
    m = re.search(r"(?:top|前)\s*(\d+)", question, flags=re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*个", question)
    if m:
        return int(m.group(1))
    return default


PROVINCES = ["青海", "新疆", "甘肃", "宁夏", "内蒙古", "山西", "河北",
             "山东", "河南", "江苏", "浙江", "安徽", "广东", "福建"]
DOMAIN_HINTS += PROVINCES  # 省份名也是领域词：「山东省下所有电站的发电量汇总」要能进闸


def _province_of(q: str):
    for p in PROVINCES:
        if p in q:
            return p
    return None


def _ratio_sql(from_clause: str, cond_cur: str, cond_prev: str, alias: str, where: str = "") -> str:
    """同比/环比：把两期数据用 CASE WHEN 摊到一行再算比率，与手写口径一致"""
    return (
        f"SELECT ROUND((cur-prev)/prev,4) AS {alias} FROM ("
        f"SELECT SUM(CASE WHEN {cond_cur} THEN f.generation_kwh ELSE 0 END) AS cur, "
        f"SUM(CASE WHEN {cond_prev} THEN f.generation_kwh ELSE 0 END) AS prev "
        f"FROM {from_clause} {where})"
    )


def _month_range(y: str, m1: int, m2: int) -> str:
    return (f"strftime('%Y', f.stat_date) = '{y}' AND "
            f"CAST(strftime('%m', f.stat_date) AS INTEGER) BETWEEN {m1} AND {m2}")


def _anchor_date() -> str:
    """数据里最新的日期，作为「本月/上月/同比」的锚点（合成数据的时间轴不是今天）"""
    conn = connect_readonly()
    try:
        return conn.execute("SELECT MAX(stat_date) FROM fact_daily_generation").fetchone()[0]
    finally:
        conn.close()


def _prev_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    m -= 1
    if m == 0:
        y, m = y - 1, 12
    return f"{y:04d}-{m:02d}"


# ============================================================ ↓↓↓ D1 不用读 ↓↓↓
# 这 195 行是 MOCK 规则引擎（一堆 if-else 硬匹配问句），存在的唯一目的是：
# 没有 API Key 时也能把「选表 → 检索 → 生成 → 校验 → 执行」整条链路跑通，方便你先理解流程。
# D4 接上真模型之后，这条路径就被 llm_generate_sql() 取代，这里的规则一条都不会留。
# 读它不产生面试价值 —— 面试官不会问「你的 if-else 怎么写」，只会问「模型生成错了怎么自愈」。
# ============================================================ ↓↓↓ D1 不用读 ↓↓↓
def mock_generate_sql(question: str) -> str:
    """
    MOCK 模式：不花钱也能把整条链路跑通。
    规则引擎覆盖评测量级里最常见的几类意图，命中不了就退回一个安全可用的查询。
    """
    q = question.strip()

    # —— 意图 1：业务口径问答（走检索路径，不生成 SQL）
    if is_meta_question(q) or re.search(r"(怎么|如何)?(定义|计算|口径|公式|什么意思|怎么算)", q):
        hits = retrieve_metrics(q, limit=1)
        if hits:
            h = hits[0]
            return f"-- GLOSSARY\n{h['metric_name']}：{h['definition']}\n口径：{h['formula']}（单位 {h['unit']}，来源：{h['source_doc']}）"

    expr, need_tables, label = pick_metric(q)
    anchor = _anchor_date()
    anchor_ym = anchor[:7]
    prev_ym = _prev_month(anchor_ym)
    last_year_ym = f"{int(anchor_ym[:4]) - 1:04d}-{anchor_ym[5:7]}"

    base_from = "dim_plant p"
    if "fact_daily_generation" in need_tables:
        base_from += " JOIN fact_daily_generation f ON f.plant_id = p.plant_id"

    province = _province_of(q)
    metric_alias = re.sub(r"[^\w]", "", label) or "metric"

    def _period_conds(allow_recent=True, include_period=True):
        """把「本月/上月/近一年/某省」这些限定词翻成 SQL 条件。
        include_period=False 给同比环比用——那里的时间范围已经写在 CASE WHEN 里了，
        再叠一层 WHERE 会把上一期的数据过滤掉。"""
        conds = []
        if province:
            conds.append(f"p.province = '{province}'")
        if include_period and "fact_daily_generation" in need_tables:
            if any(k in q for k in ["上个月", "上月"]):
                conds.append(f"strftime('%Y-%m', f.stat_date) = '{prev_ym}'")
            elif any(k in q for k in ["本月", "这个月"]):
                conds.append(f"strftime('%Y-%m', f.stat_date) = '{anchor_ym}'")
            if allow_recent and any(k in q for k in ["近一年", "过去 12 个月", "过去12个月", "近半年"]):
                months = 6 if "半年" in q else 12
                conds.append(f"f.stat_date >= date('{anchor}', '-{months} months')")
        return conds

    def _where(conds):
        return f"WHERE {' AND '.join(conds)}" if conds else ""

    # —— 意图 2：同比（把两期摊到一行算比率，而不是丢两行给用户自己比）
    if "同比" in q or "较去年同期" in q:
        y0 = anchor_ym[:4]
        if "前三季度" in q:
            cur, prev = _month_range(y0, 1, 9), _month_range(str(int(y0) - 1), 1, 9)
        else:
            cur = f"strftime('%Y-%m', f.stat_date) = '{anchor_ym}'"
            prev = f"strftime('%Y-%m', f.stat_date) = '{last_year_ym}'"
        return _ratio_sql(base_from, cur, prev, "yoy", _where(_period_conds(include_period=False)))

    # —— 意图 3：环比（含 Q2 环比 Q1 这类季度对比）
    if "环比" in q or "较上月" in q or ("上月" in q and "变化" in q):
        y0 = anchor_ym[:4]
        qs = re.findall(r"Q([1-4])", q, flags=re.I)
        if len(qs) >= 2:
            qc, qp = int(qs[0]), int(qs[1])
            cur = _month_range(y0, qc * 3 - 2, qc * 3)
            prev = _month_range(y0, qp * 3 - 2, qp * 3)
        else:
            cur = f"strftime('%Y-%m', f.stat_date) = '{anchor_ym}'"
            prev = f"strftime('%Y-%m', f.stat_date) = '{prev_ym}'"
        return _ratio_sql(base_from, cur, prev, "mom", _where(_period_conds(include_period=False)))

    # —— 意图 4：季度序列
    if "季度" in q and "同比" not in q:
        return (
            "SELECT strftime('%Y', f.stat_date) || '-Q' || "
            "((CAST(strftime('%m', f.stat_date) AS INTEGER)+2)/3) AS quarter, "
            f"ROUND(SUM(f.generation_kwh),0) AS gen FROM {base_from} {_where(_period_conds())} "
            "GROUP BY 1 ORDER BY 1"
        )

    # —— 意图 5：趋势（按月序列）
    if any(k in q for k in ["趋势", "走势", "逐月", "月度", "过去 12 个月", "过去12个月", "近半年"]):
        months = 6 if "半年" in q else 12
        return (
            f"SELECT strftime('%Y-%m', f.stat_date) AS month, ROUND(SUM(f.generation_kwh),0) AS gen "
            f"FROM {base_from} WHERE f.stat_date >= date('{anchor}', '-{months} months') "
            f"GROUP BY 1 ORDER BY 1"
        )

    # —— 意图 6：占比 / 构成
    if any(k in q for k in ["占比", "构成", "占多少", "各占", "比例"]):
        if "西北" in q:  # 西北大基地占全国装机比例 → 单行标量
            return (
                "SELECT ROUND(100.0*SUM(CASE WHEN region='西北' AND station_type='集中式' "
                "THEN capacity_mw ELSE 0 END)/SUM(capacity_mw),2) AS pct FROM dim_plant"
            )
        group_col = "p.station_type" if any(k in q for k in ["集中式", "分布式"]) else "p.province"
        group_name = group_col.split(".")[-1]
        return (
            f"SELECT {group_col} AS {group_name}, ROUND(SUM(p.capacity_mw),2) AS cap_mw, "
            f"ROUND(100.0*SUM(p.capacity_mw)/(SELECT SUM(capacity_mw) FROM dim_plant),2) AS pct "
            f"FROM {base_from} GROUP BY 1 ORDER BY cap_mw DESC"
        )

    # —— 意图 7：筛选 / 阈值
    m_th = re.search(r"(低于|小于|少于|高于|大于|超过|高于|超|高过)\s*(\d+(?:\.\d+)?)", q)
    if m_th:
        op = "<" if m_th.group(1) in ("低于", "小于", "少于") else ">"
        val = m_th.group(2)
        extra = _where(_period_conds())
        if any(k in q for k in ["弃光", "限发"]):
            return (
                f"SELECT p.plant_name, ROUND(SUM(f.curtailment_kwh)/NULLIF(SUM(f.generation_kwh)+SUM(f.curtailment_kwh),0),4) AS curt_rate "
                f"FROM {base_from} {extra} GROUP BY p.plant_id HAVING curt_rate {op} {float(val)/100} ORDER BY curt_rate DESC LIMIT {ROW_LIMIT}"
            )
        if "利用小时" in q or "等效" in q:
            return (
                f"SELECT p.plant_name, ROUND(SUM(f.generation_kwh)/NULLIF(SUM(p.capacity_mw),0)/1000,1) AS eq_hours "
                f"FROM {base_from} {extra} GROUP BY p.plant_id HAVING eq_hours {op} {val} ORDER BY eq_hours DESC LIMIT {ROW_LIMIT}"
            )
        if "装机" in q or "容量" in q:
            type_cond = " AND p.station_type = '集中式'" if "集中式" in q else ""
            return (
                f"SELECT plant_name, province, capacity_mw FROM dim_plant p "
                f"WHERE p.capacity_mw {op} {val}{type_cond} ORDER BY p.capacity_mw DESC LIMIT {ROW_LIMIT}"
            )
        return (
            f"SELECT p.plant_name, ROUND(SUM(f.generation_kwh),0) AS gen FROM {base_from} {extra} "
            f"GROUP BY p.plant_id HAVING gen {op} {float(val)*10000} ORDER BY gen DESC LIMIT {ROW_LIMIT}"
        )

    # —— 意图 8：设备故障（多表关联）
    if "故障" in q:
        return (
            f"SELECT p.plant_name, d.device_type, COUNT(*) AS fault_cnt "
            f"FROM fact_device_fault fl JOIN dim_device d ON d.device_id = fl.device_id "
            f"JOIN dim_plant p ON p.plant_id = fl.plant_id "
            f"GROUP BY 1, 2 ORDER BY fault_cnt DESC, p.plant_name, d.device_type "
            f"LIMIT {_limit_of(q, ROW_LIMIT)}"
        )

    # —— 意图 9：按品牌看效率（多表关联 + 维度过滤）
    m_brand = re.search(r"(阳光电源|华为|上能电气|特变电工|固德威|锦浪科技)", q)
    if m_brand or "品牌" in q:
        brand = m_brand.group(1) if m_brand else ""
        where = f"WHERE p.inverter_brand = '{brand}'" if brand else ""
        return (
            f"SELECT p.inverter_brand, ROUND(AVG(p.inverter_efficiency),4) AS avg_eff, COUNT(*) AS plant_cnt "
            f"FROM dim_plant p {where} GROUP BY 1 ORDER BY avg_eff DESC"
        )

    # —— 意图 10：某省份下所有电站 / 某电站明细（必须排在标量汇总之前，否则会被「汇总」这个词吞掉）
    if province and any(k in q for k in ["所有电站", "各电站", "电站"]):
        return (
            f"SELECT p.plant_name AS plant_name, ROUND(SUM(f.generation_kwh),2) AS gen "
            f"FROM {base_from} WHERE p.province = '{province}' GROUP BY p.plant_id "
            f"ORDER BY gen DESC LIMIT {ROW_LIMIT}"
        )

    # —— 意图 11：标量汇总（全国总装机 / 上个月总发电量 / 平均利用小时）
    list_keywords = ["排名", "前", "低于", "高于", "超", "最低", "最高", "哪些", "各电站"]
    if any(k in q for k in ["总", "平均", "全国", "合计"]) and not any(k in q for k in list_keywords):
        return f"SELECT ROUND({expr},4) AS {metric_alias} FROM {base_from} {_where(_period_conds())}"

    # —— 意图 12：排名（兜底形态）
    desc = not any(k in q for k in ["最低", "最小", "最少", "最差"])
    order_dir = "DESC" if desc else "ASC"
    if any(k in q for k in ["省份", "各省", "按省"]) or ("装机" in q and "电站" not in q):
        select_cols, group_by = "p.province AS province", "p.province"
        tiebreak = "p.province"
    else:
        select_cols, group_by = "p.plant_name AS plant_name", "p.plant_id"
        tiebreak = "p.plant_name"
        # 单电站层级的「效率」这类非聚合属性，直接取列值，不要 AVG 包一层
        if expr == "AVG(p.inverter_efficiency)":
            expr = "p.inverter_efficiency"

    where_time = _where(_period_conds())
    limit_clause = f" LIMIT {_limit_of(q)}" if _limit_of(q) else ""
    # 加次级排序：并列值（比如多个电站同为 0.96）必须有确定性，否则评测集比不稳定
    return (
        f"SELECT {select_cols}, ROUND({expr},2) AS {metric_alias} "
        f"FROM {base_from} {where_time} GROUP BY {group_by} "
        f"ORDER BY {metric_alias} {order_dir}, {tiebreak}{limit_clause}"
    )


# ============================================================ 时间锚点（D1 第 1 次提分）
# 考点：2026-09-28 实测，真模型 31 题只跑出 35.5%，其中约 8 题栽在这里——
#   模型不认识「本月/上月/近一年」，只能拿真实时钟 now() 换算，
#   而数据停在 2026-08-31：问「本月」会查一个没有数据的月份，问「上月」整体错位一个月。
# 修法：不给模型猜，由代码把「数据截止日」喂进去，并明令禁用 now()。
# 面试可讲：这是 Text-to-SQL 里最常见的坑之一，属于**元数据没喂全**，不是模型能力问题。
_ANCHOR_DATE: str = ""


def get_anchor_date() -> str:
    """返回数据最新日期（相对时间的唯一基准）。查一次后缓存，避免每次问数都扫表。"""
    global _ANCHOR_DATE
    if not _ANCHOR_DATE:
        try:
            rows = execute_sql("SELECT MAX(stat_date) FROM fact_daily_generation")[1]
            _ANCHOR_DATE = str(rows[0][0]) if rows and rows[0] and rows[0][0] else "2026-08-31"
        except Exception:  # noqa: BLE001  查库失败也不能拖垮问数链路，退回兜底值
            _ANCHOR_DATE = "2026-08-31"
    return _ANCHOR_DATE


def _anchor_hint() -> str:
    """拼进 prompt 的时间锚点说明 + 几个常用相对时间的对照表。"""
    d = get_anchor_date()
    year, month = int(d[:4]), int(d[5:7])
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    ym = d[:7]
    ym_prev = f"{prev_year:04d}-{prev_month:02d}"
    return (
        "\n\n【时间锚点·必须遵守】"
        f"数据截止日期 = {d}。问题里的「本月/上月/同比/环比/近N月/近N年」一律以该日期为基准换算。\n"
        "禁止使用 date('now') 或 strftime('now')——真实时钟会指到没有数据的月份，导致查空或错位。\n"
        f"本月 = strftime('%Y-%m', stat_date) = '{ym}'；上月 = '{ym_prev}'；"
        f"近半年 = stat_date >= date('{d}', '-6 months')；近一年 = stat_date >= date('{d}', '-12 months')。\n"
        "年份也同理：今年指该日期所在年，去年同期指上一年同月/同期。"
    )


OUTPUT_CONTRACT = """
【输出契约·必须遵守】
1. 列数：只 SELECT 问题真正要的列，**不要附赠主键（plant_id/device_id）或整表字段**。
   问"哪几个电站"就给电站名 + 指标，别多给 id。
2. 数值精度：**发电量 (generation_kwh) 用 ROUND(x, 0)**；**装机容量 (capacity_mw) 用 ROUND(x, 2)**；
   "占比/占多少/比例"用**百分比** ROUND(100.0*ratio, 2)；"率"（弃光率、转换效率等）用**小数** ROUND(x, 4)；
   其余数值 ROUND(x, 2)。
3. 同比/环比/变化量：**只输出一个比值列**（内部可用子查询分别算 cur/prev，最终 SELECT 只留比值）。
4. Top N / 排名：**只有问题明确给了条数上限（"前 5 / 前 10 / Top N / 最低的 N 个"）才加 LIMIT N**；
   只说"排名/最高/最低"但没给数字时**不要加 LIMIT**（加了等于偷偷丢数据）。
   需要 LIMIT 时同时加一列次级排序（plant_name / province），避免并列值导致结果不稳定。
5. 分组粒度：问"各 X"就按 X 分组**返回多行**（如"各电站"→每行一个电站），不要聚合成一个总计值。
6. 趋势/走势：输出「时间标签 + 数值」两列，时间用 strftime 拼成 'YYYY-MM' 或 'YYYY-Qn'。
"""

LLM_SYSTEM = """你是一名资深数据分析师，负责把中文业务问题翻译成 SQLite SQL。
规则：
1. 只能基于给定的表结构写 SQL，禁止捏造字段；
2. 只写 SELECT，禁止任何写操作与多语句；
3. 用 strftime('%Y-%m', stat_date) 取月份，禁止用 SQLite 不支持的函数；
4. 输出纯 SQL，不要 markdown 代码围栏，不要解释。
"""


def llm_generate_sql(question: str, tables: list[str], metrics: list[dict], error_hint: str = "") -> str:
    """真模型模式：DeepSeek / OpenAI 兼容接口"""
    from openai import OpenAI  # 延迟导入：MOCK 模式下不需要装 SDK

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), base_url=os.getenv("OPENAI_BASE_URL"))
    glossary = "\n".join(
        f"- {m['metric_name']}：{m['definition']} 口径={m['formula']}" for m in metrics
    ) or "- 未命中业务口径，按表结构推断"
    user = (
        f"问题：{question}\n\n可用表结构：\n{render_schema(tables)}\n\n业务口径：\n{glossary}\n"
        f"{'上次生成的 SQL 执行失败，报错：' + error_hint + '，请修正后重写。' if error_hint else ''}"
        f"\n{OUTPUT_CONTRACT}"
        f"\n请输出 SQL：{_anchor_hint()}"
    )
    resp = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "deepseek-chat"),
        messages=[{"role": "system", "content": LLM_SYSTEM}, {"role": "user", "content": user}],
        temperature=0,
    )
    sql = resp.choices[0].message.content.strip()
    sql = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", sql)
    return sql.strip().rstrip(";")


# ============================================================ 第 4 站：安全校验
DENY_WORDS = ["insert", "update", "delete", "drop", "alter", "create", "attach", "pragma",
              "vacuum", "replace", "trigger", "exec ", "union all select"]

# 考点 3：用户诱导模型删库怎么办 → validate_sql() 四层
def validate_sql(sql: str) -> tuple[bool, str]:
    s = sql.strip().rstrip(";")
    low = s.lower()
    # 第 1 层：语句白名单，只允许 SELECT / WITH
    first_word = low.lstrip("(").split()[0] if low.split() else ""
    if first_word not in ("select", "with"):
        return False, f"只允许 SELECT 查询，检测到语句类型：{first_word or '空'}"
    # 第 2 层：危险关键词黑名单
    for w in DENY_WORDS:
        if w in low:
            return False, f"命中危险词：{w.strip()}"
    # 第 3 层：禁止多语句（分号后面还有内容）
    core = re.sub(r"'[^']*'", "''", s)
    if ";" in core.replace(";;", ";"):
        remaining = core.split(";", 1)[1].strip()
        if remaining:
            return False, "检测到多语句执行，已拦截"
    # 第 4 层：物理保险——数据库连接本身 mode=ro（见 db.connect_readonly）
    return True, "ok"


# ============================================================ 第 5 站：执行 + 自修正
def run_sql(sql: str) -> dict:
    ok, reason = validate_sql(sql)
    if not ok:
        return {"ok": False, "error": reason}
    try:
        cols, rows, ms = execute_sql(sql)
        return {"ok": True, "columns": cols, "rows": rows, "elapsed_ms": ms}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


# ============================================================ 第 6 站：编排（含流式事件）
def ask_stream(question: str) -> Iterator[dict]:
    """
    考点 8：为什么全流程要 SSE 分步推送？全链路 5-15 秒，让用户看到「AI 在思考」，
    而不是干等转圈——既是体验也是信任。
    """
    q = (question or "").strip()
    if not q:
        yield {"event": "error", "step": 0, "text": "请输入问题"}
        return

    # 负样本护栏（两道门）放在最前面：元问题 / 域外问题根本不进链路，不选表也不检索
    if is_meta_question(q) or is_out_of_scope(q):
        # D4 提问五要素的拒答话术：说明做不到的原因 + 给替代路径
        yield {"event": "refuse", "step": 0, "title": "超出能力范围", "text": REFUSE_TEMPLATE}
        return

    # step 1 选表
    tables = select_tables(q)
    yield {"event": "step", "step": 1, "title": "选表", "text": "、".join(tables)}
    yield {"event": "text", "step": 1, "text": "已定位相关表：" + "、".join(tables)}

    # step 2 业务口径检索
    metrics = retrieve_metrics(q)
    if metrics:
        yield {"event": "step", "step": 2, "title": "口径检索", "text": "、".join(m["metric_name"] for m in metrics)}
        yield {"event": "text", "step": 2, "text": "命中业务口径：" + "、".join(m["metric_name"] for m in metrics)}
    else:
        yield {"event": "step", "step": 2, "title": "口径检索", "text": "未命中，按表结构推断"}

    # step 3 生成 SQL（含自修正循环）
    error_hint = ""
    sql = ""
    for attempt in range(MAX_RETRY + 1):
        if USE_LLM:
            sql = llm_generate_sql(q, tables, metrics, error_hint)
        else:
            sql = mock_generate_sql(q)

        if sql.startswith("-- GLOSSARY"):
            yield {"event": "glossary", "step": 3, "title": "口径回答", "text": sql.replace("-- GLOSSARY\n", "")}
            return

        yield {"event": "sql", "step": 3, "title": "生成 SQL" + (f"（第 {attempt + 1} 次）" if attempt else ""), "text": sql}

        # step 4 执行
        result = run_sql(sql)
        if result["ok"]:
            yield {"event": "result", "step": 4, "title": "执行完成",
                   "text": f"返回 {len(result['rows'])} 行，耗时 {result['elapsed_ms']} ms",
                   "columns": result["columns"], "rows": result["rows"],
                   "attempts": attempt + 1, "sql": sql}
            return
        # 考点 2：生成错了怎么办 → 把原始报错喂回模型重生成，MAX_RETRY 是硬上限
        error_hint = result["error"]
        yield {"event": "retry", "step": 4, "title": f"执行失败，第 {attempt + 1}/{MAX_RETRY} 次修正",
               "text": error_hint}

    yield {"event": "error", "step": 4, "title": "多次修正仍失败", "text": error_hint}


def ask(question: str) -> dict:
    """非流式版本，给 eval.py 用"""
    out = {"question": question, "sql": None, "ok": False, "refused": False, "attempts": 0,
           "error": None, "columns": None, "rows": None, "answer": None}

    # 护栏：负样本拒答优先于任何 SQL 生成（流式、非流式两条都用同一套判定）
    if is_meta_question(question) or is_out_of_scope(question):
        out.update(ok=True, refused=True, answer=REFUSE_TEMPLATE)
        return out

    metrics = retrieve_metrics(question)
    if metrics and re.search(r"(定义|口径|公式|怎么算|什么意思|怎么定义)", question):
        out.update(ok=True, answer=metrics[0]["definition"])
        return out
    error_hint = ""
    for attempt in range(MAX_RETRY + 1):
        sql = llm_generate_sql(question, select_tables(question), metrics, error_hint) if USE_LLM \
            else mock_generate_sql(question)
        out["sql"] = sql
        out["attempts"] = attempt + 1
        res = run_sql(sql)
        if res["ok"]:
            out.update(ok=True, columns=res["columns"], rows=res["rows"])
            return out
        error_hint = res["error"]
    out["error"] = error_hint
    return out


if __name__ == "__main__":
    print("mode:", "LLM" if USE_LLM else "MOCK")
    for qs in ["各省份装机容量排名", "发电量最高的 10 个电站", "上月总发电量",
               "等效利用小时是怎么定义的", "你能查哪些指标"]:
        print("\n>>>", qs)
        print(json.dumps(ask(qs), ensure_ascii=False)[:600])
