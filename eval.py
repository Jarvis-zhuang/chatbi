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
import json
import os
import sys

import text2sql
from db import execute_sql

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EVAL_SET = os.path.join(BASE_DIR, "eval_set.json")


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


def score_sql(expected_sql: str, pred_rows) -> bool:
    ok, err = text2sql.validate_sql(expected_sql)
    if not ok:
        print(f"  [warn] expected_sql 未通过安全校验：{err}")
        return False
    try:
        exp_cols, exp_rows, _ = execute_sql(expected_sql)
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] expected_sql 本身执行失败：{e}")
        return False
    return norm_rows(exp_cols, exp_rows) == norm_rows(None, pred_rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    with open(EVAL_SET, encoding="utf-8") as f:
        cases = json.load(f)["cases"]

    print(f"mode: {'LLM' if text2sql.USE_LLM else 'MOCK'} | cases: {len(cases)}")
    print("-" * 72)

    passed, failed = [], []
    for c in cases:
        q, typ = c["q"], c["type"]
        out = text2sql.ask(q)

        if typ == "sql":
            ok = out["ok"] and score_sql(c["sql"], out["rows"])
        elif typ == "glossary":
            hits = text2sql.retrieve_metrics(q, limit=5)
            ok = any(h["metric_name"] == c["metric"] for h in hits)
        else:  # refuse
            ok = out.get("refused") is True

        (passed if ok else failed).append(c)
        flag = "PASS" if ok else "FAIL"
        line = f"[{flag}] #{c['id']:02d} {q}"
        print(line)
        if args.verbose or not ok:
            print("       predict:", (out.get("sql") or "").replace("\n", " ")[:220])
            if out.get("error"):
                print("       error  :", out["error"][:160])
            if not ok and typ == "sql" and out.get("rows") is not None:
                print("       rows   :", str(out["rows"])[:160])

    total = len(cases)
    acc = len(passed) / total * 100
    print("-" * 72)
    print(f"准确率：{len(passed)}/{total} = {acc:.1f}%")
    if failed:
        print("错题 id：", ", ".join(str(c["id"]) for c in failed))
    sys.exit(0)


if __name__ == "__main__":
    main()
