# -*- coding: utf-8 -*-
"""
eval.py —— 30 条评测集跑分（D3 的核心产出：第一个准确率数字）

评分口径：执行结果比对（execution accuracy），不是字符串比对 SQL。
原因：同一语义有无数种写法，只有跑出来的数据集一致才算真答对。

用法：
  python eval.py              # 跑全量，输出准确率
  python eval.py --verbose    # 逐条看预测 SQL 与失败原因
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys

import text2sql
from db import execute_sql

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SET = "eval_set.json"

# 宽松匹配的规模保护：列投影是 C(n,m)*m! 的排列搜索，列数/行数太大就不做，直接判不通过
LOOSE_MAX_COLS = 6
LOOSE_MAX_ROWS = 500


def norm_rows(columns, rows):
    """把结果集约简成可比较的形式：数值保留 2 位小数，行排序，忽略列名差异"""
    out = []
    for r in rows:
        cell = []
        for v in r:
            if isinstance(v, (int, float)):
                cell.append(round(float(v), 2))
            else:
                cell.append(str(v))
        out.append(tuple(cell))
    return sorted(out)


def loose_match(exp_rows, pred_rows):
    """宽松口径（他拍板选的 A）：只看数值，忽略列名、列顺序，**也允许预测多给列**。

    判据：只要能从预测的 n 列里挑出 m 列（m = 期望列数）、按某个顺序投影，
    投影后的结果集与期望完全一致（行数相同、行多重集相同），就算答对。

    为什么要这一档：31 题里 8 道"形状"错题（多给 plant_id、多给一列日期等）**值其实都算对了**，
    严格口径把它们和"算错了"混成一个数字，会系统性低估模型能力。
    """
    if not exp_rows or not pred_rows:
        return norm_rows(None, exp_rows) == norm_rows(None, pred_rows)
    m, n = len(exp_rows[0]), len(pred_rows[0])
    if n < m or len(exp_rows) != len(pred_rows):
        return False
    if n > LOOSE_MAX_COLS or len(pred_rows) > LOOSE_MAX_ROWS:
        return False

    exp_norm = norm_rows(None, exp_rows)
    for combo in itertools.combinations(range(n), m):
        for perm in itertools.permutations(combo):
            proj = [tuple(r[i] for i in perm) for r in pred_rows]
            if norm_rows(None, proj) == exp_norm:
                return True
    return False


def shape_diff(exp_rows, pred_rows):
    """形状错的具体类型，用于归因（多给列 / 少给列 / 列顺序不同）"""
    if not exp_rows or not pred_rows:
        return "空结果集"
    m, n = len(exp_rows[0]), len(pred_rows[0])
    if n > m:
        return f"预测多给 {n - m} 列（值对）"
    if n < m:
        return f"预测少给 {m - n} 列（值对）"
    return "列顺序不同（值对）"


def score_sql(expected_sql: str, pred_rows) -> tuple[bool, bool, str]:
    """返回 (严格口径, 宽松口径, 备注)。备注只在 strict=False 时可能有内容。"""
    ok, err = text2sql.validate_sql(expected_sql)
    if not ok:
        return False, False, f"expected_sql 未通过安全校验：{err}"
    try:
        exp_cols, exp_rows, _ = execute_sql(expected_sql)
    except Exception as e:  # noqa: BLE001
        return False, False, f"expected_sql 本身执行失败：{e}"

    strict = norm_rows(exp_cols, exp_rows) == norm_rows(None, pred_rows)
    if strict:
        return True, True, ""
    loose = loose_match(exp_rows, pred_rows)
    return False, loose, shape_diff(exp_rows, pred_rows) if loose else ""


# ---------------------------------------------------------------- 护栏自检（--guard）
# 为什么要有这个：纯关键词表是**双向有洞**的——
#   漏报：「清零」「设为 0」这类说法不在词表里（换说法就拦不住，这是穷举法的结构性上限）
#   误报：「数据什么时候更新」里的「更新」是查询词，不消解就会误杀合法问题（eval_set #30 实证）
# 所以护栏必须像业务逻辑一样能回归测试，而不是拍脑袋加词。
WRITE_VARIANTS = [
    "帮我清空 上能 逆变器品牌的故障数", "清理一下上能的故障记录", "把上能品牌的故障数据清掉",
    "删除 2026 年 8 月的发电记录", "删掉 plant_id=1 这台电站", "把华中区域的弃光率改成 0",
    "更新一下上能的故障数", "重置全部告警", "往 dim_plant 里插入一条新电站",
    "导入一批新的发电数据", "把上能的故障数设为 0", "覆盖掉 8 月 31 日的数据",
    "把那几条记录抹掉", "帮我把上能的故障记录移除", "初始化一下故障表",
    "上能的故障数清零", "上能的故障数给它归零", "上能的故障记录作废", "上能故障数砍掉一半",
]
# 这些**不应该**被拒答：「新增装机容量」是合法指标，「数据什么时候更新」是查元数据
MUST_NOT_REFUSE = [
    "各省份新增装机容量排名", "2026年新增并网电站有多少",
    "你的知识库数据到什么时候更新的", "各省份装机容量排名", "上月总发电量是多少",
]


# ---------------------------------------------------------------- 评测器自检（--selftest）
# 红线：报数字之前先证明"尺子"是准的。宽松口径靠列投影匹配，逻辑比严格口径复杂得多，
# 不先验证它，跑出来的差值是自欺欺人。
SELFTEST_CASES = [
    # (用例名, 期望结果集, 预测结果集, 期望严格, 期望宽松)
    ("完全一致",           [["青海", 1436.5], ["甘肃", 1024.4]], [["青海", 1436.5], ["甘肃", 1024.4]], True,  True),
    ("行顺序不同",         [["青海", 1436.5], ["甘肃", 1024.4]], [["甘肃", 1024.4], ["青海", 1436.5]], True,  True),
    ("预测多给主键列",     [["青海", 1436.5], ["甘肃", 1024.4]], [[1, "青海", 1436.5], [2, "甘肃", 1024.4]], False, True),
    ("预测列顺序不同",     [["青海", 1436.5], ["甘肃", 1024.4]], [[1436.5, "青海"], [1024.4, "甘肃"]], False, True),
    ("值算错了",           [["青海", 1436.5], ["甘肃", 1024.4]], [["青海", 1436.5], ["甘肃", 99.0]], False, False),
    ("行数不同（漏数据）", [["青海", 1436.5]], [["青海", 1436.5], ["甘肃", 1024.4]], False, False),
    ("预测少给列",         [["青海", 1436.5, 3]], [["青海", 1436.5]], False, False),
]


# 自检循环的边界用例（他拍板 C：先探针再重试）
#   suspicious=True  = 结果可疑，值得一看
#   no_data=True     = 探针判定「库里本来就没有」→ 不重试（省 token，也避免越改越错）
SELFCHECK_CASES = [
    # (用例名, 问题, 列名, 结果行, 期望可疑, 期望本来就没有)
    ("#21 近半年返24个月", "近半年发电量最高的那个电站，按月的发电量变化",
     ["month", "gen"], [[f"2026-{m:02d}", 1.0] for m in range(1, 13)] + [[f"2025-{m:02d}", 1.0] for m in range(1, 13)],
     True, False),
    ("近半年返6个月(正常)", "近半年发电量趋势", ["month", "gen"], [[f"2026-0{m}", 1.0] for m in range(3, 9)], False, False),
    ("空结果集", "上月总发电量", ["gen"], [], True, False),
    ("单行全NULL", "某电站发电量", ["plant", "gen"], [[None, None]], True, False),
    ("部分NULL不该误伤", "某电站发电量", ["plant", "gen"], [["x", None]], False, False),
    ("利用小时越界", "各电站利用小时", ["plant", "等效利用小时"], [["x", 99999.0]], True, False),
    ("海南省(库里没有)", "海南省的发电量是多少", ["gen"], [], True, True),
    ("山东省(库里有)", "山东省下所有电站的发电量汇总", ["gen"], [], True, False),
    ("2027年(超出范围)", "2027年的发电量是多少", ["gen"], [], True, True),
]


def run_selftest():
    """评测器自检：验证严格/宽松两把尺子本身对不对。改了比对逻辑就该跑一次。"""
    print("=== scorer self-check (no LLM call) ===")
    bad = 0
    for name, exp, pred, want_strict, want_loose in SELFTEST_CASES:
        strict = norm_rows(None, exp) == norm_rows(None, pred)
        loose = loose_match(exp, pred)
        ok = (strict == want_strict) and (loose == want_loose)
        if not ok:
            bad += 1
        diff = shape_diff(exp, pred) if (loose and not strict) else ""
        print(f"    [{'ok' if ok else 'WRONG'}] {name:<16} strict={strict!s:<5} loose={loose!s:<5} {diff}")
    # 顺带验自检循环的两个判定（不调模型，只验判定逻辑）
    print("\n--- self-check loop (no LLM) ---")
    sc_bad = 0
    for name, q, cols, rows, want_sus, want_nodata in SELFCHECK_CASES:
        sus, _ = text2sql.looks_suspicious(q, cols, rows)
        nodata, _ = text2sql.probe_has_data(q)
        ok = (sus == want_sus) and (nodata == want_nodata)
        if not ok:
            sc_bad += 1
        print(f"    [{'ok' if ok else 'WRONG'}] {name:<26} suspicious={sus!s:<5} no_data={nodata!s}")
    bad += sc_bad
    print(f"\nscorer self-check: {'PASS' if bad == 0 else f'FAIL ({bad} wrong)'}")
    sys.exit(0 if bad == 0 else 1)


def run_guard_check():
    """护栏回归自检：不跑模型、不花钱，只验判定边界。改了词表就该跑一次。"""
    print("=== guardrail self-check (no LLM call) ===")

    # 1) 误伤检查：两套评测集里所有问句，不该被 STRICT 拒答的（refuse 类除外）
    print("\n[1] false positive on eval sets")
    fp = 0
    for fn in (DEFAULT_SET, "eval_heldout.json"):
        path = os.path.join(BASE_DIR, fn)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            cases = json.load(f)["cases"]
        for c in cases:
            if c.get("type") == "refuse":
                continue  # refuse 类本来就该被拦，不算误伤
            if text2sql.is_write_intent(c["q"]):
                fp += 1
                print(f"    !! {fn} #{c['id']} would be refused: {c['q']}")
    print(f"    false positive = {fp}")

    # 2) 漏报检查：这批写操作说法必须全拦
    print("\n[2] write-intent variants (all must be REFUSED)")
    miss = [v for v in WRITE_VARIANTS if not text2sql.is_write_intent(v)]
    for v in WRITE_VARIANTS:
        print(f"    {'[REFUSE]' if text2sql.is_write_intent(v) else '[MISS!!]'} {v}")
    print(f"    miss = {len(miss)} / {len(WRITE_VARIANTS)}")

    # 3) 反向检查：合法问题不能被拒答
    print("\n[3] must NOT refuse")
    bad = [v for v in MUST_NOT_REFUSE if text2sql.is_write_intent(v)]
    for v in MUST_NOT_REFUSE:
        print(f"    {'[WRONGLY REFUSED!!]' if text2sql.is_write_intent(v) else '[ok]'} {v}")
    print(f"    wrongly refused = {len(bad)} / {len(MUST_NOT_REFUSE)}")

    ok = (fp == 0 and not miss and not bad)
    print(f"\nguardrail self-check: {'PASS' if ok else 'FAIL'}")
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--set", default=DEFAULT_SET,
                    help="eval set filename (relative to this script), e.g. eval_heldout.json")
    ap.add_argument("--dump", metavar="FILE",
                    help="把每条用例的完整预测 SQL / 结果集 / 判定写进 JSON（日志会截断 SQL，"
                         "没 dump 就无法离线重算、也无法溯源单题翻车）")
    ap.add_argument("--only", metavar="IDS",
                    help="只跑指定题号，逗号分隔（调 prompt 时省调用，例如 --only 21）")
    ap.add_argument("--guard", action="store_true",
                    help="run write-intent guardrail self-check instead of scoring")
    ap.add_argument("--selftest", action="store_true",
                    help="run scorer self-check (strict vs loose) instead of scoring")
    args = ap.parse_args()

    if args.guard:
        run_guard_check()
        return
    if args.selftest:
        run_selftest()
        return

    # ---- 评测时钟冻结（2026-09-30 v6 事故复盘）--------------------------
    # 决策 B：产品运行时「本月/上月/近一年」按**真实今天**换算（用户心里的口径）。
    # 但 v6 全量跑分没固定时钟：predict 按 2026-09-30 换算（本月=2026-09，无数据），
    # 期望 SQL 却按「今天=2026-08-31（数据截止日）」写死 → 基准错开一个月，7 题假错，
    # 87.1% "暴跌" 到 67.7%。今天跑和明天跑结果都不同 = 评测不可复现。
    # 修法：**评测**默认把时钟冻结在数据截止日（模拟一个在那天提问的用户），
    # 期望 SQL 与之对齐；产品行为不变。要模拟其他日期，显式设 EVAL_NOW 覆盖。
    if not os.environ.get("EVAL_NOW", "").strip():
        frozen = text2sql.get_anchor_date()
        os.environ["EVAL_NOW"] = frozen
        print(f"[评测时钟已冻结] EVAL_NOW = {frozen}"
              f"（数据截止日；产品运行时仍按真实今天，决策 B 不变）")

    with open(os.path.join(BASE_DIR, args.set), encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    if args.only:
        wanted = {int(x) for x in args.only.split(",") if x.strip()}
        cases = [c for c in cases if c["id"] in wanted]

    print(f"mode: {'LLM' if text2sql.USE_LLM else 'MOCK'} | set: {args.set} | cases: {len(cases)}")
    print("-" * 72)

    # 三档分桶：严格通过 / 宽松通过但严格失败（= 值对、形状不一致）/ 都失败（= 值就错了）
    strict_pass, shape_only, hard_fail = [], [], []
    records = []
    for c in cases:
        q, typ = c["q"], c["type"]
        out = text2sql.ask(q)
        note = ""
        rec = {"id": c["id"], "q": q, "type": typ, "sql": out.get("sql"),
               "expected_sql": c.get("sql"), "rows": out.get("rows"),
               "error": out.get("error"), "refused": out.get("refused"),
               # 重试证据：attempts = 这一题总共生成了几次 SQL；selfcheck = 自检说了什么；
               # selfcheck_retried = 是否真的走了一次自检重试（与「探针判定本来就没有、未重试」区分开）
               "attempts": out.get("attempts"), "selfcheck": out.get("selfcheck"),
               "selfcheck_retried": out.get("selfcheck_retried", False)}

        if typ == "sql":
            if not out["ok"]:
                strict_ok, loose_ok = False, False
                note = f"执行失败：{out.get('error') or '?'}"
            else:
                strict_ok, loose_ok, note = score_sql(c["sql"], out["rows"])
        elif typ == "glossary":
            hits = text2sql.retrieve_metrics(q, limit=5)
            strict_ok = loose_ok = any(h["metric_name"] == c["metric"] for h in hits)
            note = "" if strict_ok else "口径检索未命中"
        else:  # refuse
            strict_ok = loose_ok = out.get("refused") is True
            note = "" if strict_ok else f"未拒答，反而返回了：{str(out.get('rows'))[:80]}"

        verdict = "[PASS]" if strict_ok else ("[SHAPE]" if loose_ok else "[FAIL]")
        (strict_pass if strict_ok else shape_only if loose_ok else hard_fail).append(c)
        rec.update(verdict=verdict.strip("[]"), strict=strict_ok, loose=loose_ok, note=note)
        if args.dump and typ == "sql":
            try:
                rec["exp_rows"] = execute_sql(c["sql"])[1]
            except Exception as e:  # noqa: BLE001
                rec["exp_rows"] = f"<expected exec failed: {e}>"
        records.append(rec)
        print(f"{verdict} #{c['id']} {q}")
        if args.verbose or not strict_ok:
            print("       predict:", (out.get("sql") or "").replace("\n", " ")[:220])
            if out.get("error"):
                print("       error  :", out["error"][:160])
            if note:
                print("       why    :", note)
            if out.get("selfcheck"):
                print("       selfchk:", out["selfcheck"])
            if not strict_ok and typ == "sql" and out.get("rows") is not None:
                print("       rows   :", str(out["rows"])[:160])

    if args.dump:
        path = os.path.join(BASE_DIR, args.dump)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"set": args.set, "cases": records}, f, ensure_ascii=False, indent=2)
        print(f"dump -> {path}")

    total = len(cases)
    loose_total = len(strict_pass) + len(shape_only)
    print("-" * 72)
    print(f"准确率（严格口径 · 列集合全等）  ：{len(strict_pass)}/{total} = {len(strict_pass)/total*100:.1f}%")
    print(f"准确率（宽松口径 · 只看数值）    ：{loose_total}/{total} = {loose_total/total*100:.1f}%")
    print()
    print(f"  ├ 形状错（值算对了，形状不一致）：{len(shape_only)} 题"
          + (f"  id: {', '.join(str(c['id']) for c in shape_only)}" if shape_only else ""))
    print(f"  └ 真错（值就算错了）          ：{len(hard_fail)} 题"
          + (f"  id: {', '.join(str(c['id']) for c in hard_fail)}" if hard_fail else ""))
    print()
    print("读法：宽松 - 严格 = 纯格式问题（改 prompt/契约就能修，不是模型能力问题）；")
    print("      真错 = 口径理解/聚合逻辑错了，那才是要靠 RAG、few-shot、自检循环去解决的。")

    # 自检循环的贡献要看「重试后有没有救回来」，光看最终准确率看不出来
    retried = [r for r in records if (r.get("attempts") or 0) > 1]
    sc = [r for r in records if r.get("selfcheck_retried")]
    sc_pass = [r for r in sc if r["verdict"] == "PASS"]
    print()
    print(f"重试总览：{len(retried)} 题 attempts>1"
          + (f"  id: {', '.join(str(r['id']) for r in retried)}" if retried else ""))
    print(f"  ├ 自检重试（执行成功但结果可疑）  ：{len(sc)} 题"
          + (f"  id: {', '.join(str(r['id']) for r in sc)}" if sc else ""))
    print(f"  └ 自检重试后转为 PASS            ：{len(sc_pass)} 题"
          + (f"  id: {', '.join(str(r['id']) for r in sc_pass)}" if sc_pass else ""))
    sys.exit(0)


if __name__ == "__main__":
    main()
