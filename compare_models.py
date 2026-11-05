# -*- coding: utf-8 -*-
"""
compare_models.py —— 三模型对照表汇总（D4 / 2026-11-05）
======================================================
输入：eval.py --dump 产出的 JSON + 网关 GATEWAY_STATS_FILE 产出的观测 JSON。
输出：终端对照表 + compare_models_1105.md（可直接贴进 README）。

口径纪律（庄 10/24 立规）：
  - 多次重复回归，逐题看通过率，不看总数——总数正负抵消会骗人
  - 只看开发集；held-out 封印，不用它选型
  - 两轮取中位；同时列出两轮原值与「稳定失败题」，避免单点结论
"""
import json
import glob
import os
from statistics import median

BASE = os.path.dirname(os.path.abspath(__file__))

RUNS = {
    "DeepSeek-V3（主）": {
        "dumps": ["_gw_on_r1.json", "_gw_on_r2.json"],
        "stats": ["_gw_on_r1_stats.json", "_gw_on_r2_stats.json"],
    },
    "glm-5.3-flash（备胎/免费）": {
        "dumps": ["_gw_backup-glm-flash_r1.json", "_gw_backup-glm-flash_r2.json"],
        "stats": ["_gw_backup-glm-flash_r1_stats.json", "_gw_backup-glm-flash_r2_stats.json"],
    },
    "glm-5.3（同厂商正常档）": {
        "dumps": ["_gw_alt-glm-5.3_r1.json", "_gw_alt-glm-5.3_r2.json"],
        "stats": ["_gw_alt-glm-5.3_r1_stats.json", "_gw_alt-glm-5.3_r2_stats.json"],
    },
}


def load_json(name):
    path = os.path.join(BASE, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    rows = []
    verdicts = {}
    for model, cfg in RUNS.items():
        per_round = []
        for f in cfg["dumps"]:
            d = load_json(f)
            if d is None:
                print(f"[warn] missing {f}")
                continue
            cs = d["cases"]
            per_round.append({
                "file": f,
                "strict": sum(1 for c in cs if c.get("strict")),
                "loose": sum(1 for c in cs if c.get("loose")),
                "n": len(cs),
                "verdicts": {c["id"]: c.get("verdict") for c in cs},
            })
        if not per_round:
            continue
        ids = sorted(set().union(*[set(p["verdicts"]) for p in per_round]))
        stable_bad = [i for i in ids
                      if all(p["verdicts"].get(i) != "PASS" for p in per_round)]
        p50s, succ, degrades = [], [], []
        for f in cfg["stats"]:
            s = load_json(f)
            if not s:
                continue
            degrades.append(s.get("degrade_events", 0))
            for slot in s.get("slots", {}).values():
                if slot.get("p50_latency_s") is not None:
                    p50s.append(slot["p50_latency_s"])
                if slot.get("success_rate") is not None:
                    succ.append(slot["success_rate"])
        rows.append({
            "model": model,
            "rounds": per_round,
            "strict_med": median([p["strict"] for p in per_round]),
            "loose_med": median([p["loose"] for p in per_round]),
            "stable_bad": stable_bad,
            "p50_med": median(p50s) if p50s else None,
            "succ": min(succ) if succ else None,
            "degrades": sum(degrades),
        })
        verdicts[model] = per_round

    print("=" * 78)
    print("三模型对照（开发集 eval_set.json 31 题，评测时钟冻结 2026-08-31，temperature=0）")
    print("=" * 78)
    for r in rows:
        vals = "/".join(str(p["strict"]) for p in r["rounds"])
        lv = "/".join(str(p["loose"]) for p in r["rounds"])
        print(f"{r['model']:<26} 严格 {vals} → 中位 {r['strict_med']:<4} | "
              f"宽松 {lv} → {r['loose_med']:<4} | 稳定错题 {r['stable_bad']}")
        print(f"{'':<26} P50={r['p50_med']}s  成功率={r['succ']}  降级次数={r['degrades']}")
    print("=" * 78)

    # 相对主模型：两轮方向一致的变好/变坏题
    base = next((r for r in rows if r["model"].startswith("DeepSeek")), None)
    if base:
        base_sets = [set(p["verdicts"].items()) for p in base["rounds"]]
        print("\n相对主模型（要求两轮方向一致才算数）：")
        for r in rows:
            if r is base:
                continue
            worse, better = [], []
            for i in range(1, 32):
                b = [p["verdicts"].get(i) for p in base["rounds"]]
                m = [p["verdicts"].get(i) for p in r["rounds"]]
                # 主两轮都 PASS 且对照两轮都不 PASS → 稳定变差
                if all(x == "PASS" for x in b) and all(x != "PASS" for x in m):
                    worse.append(i)
                if all(x != "PASS" for x in b) and all(x == "PASS" for x in m):
                    better.append(i)
            print(f"  {r['model']:<26} 稳定变差 {worse} | 稳定变好 {better}")

    md = ["# 三模型对照表（2026-11-05 · D4）", "",
          "> 开发集 `eval_set.json` 31 题；评测时钟冻结 2026-08-31；temperature=0；",
          "> 每模型 2 轮取中位，逐题比对判定。**held-out 未参与选型**。", "",
          "| 模型 | 严格 r1/r2 | 中位 | 宽松 r1/r2 | 中位 | 稳定错题 | P50 | 成功率 | 降级 |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        vals = "/".join(str(p["strict"]) for p in r["rounds"])
        lv = "/".join(str(p["loose"]) for p in r["rounds"])
        bad = ", ".join(f"#{i}" for i in r["stable_bad"]) or "—"
        md.append(f"| {r['model']} | {vals} | {r['strict_med']} | {lv} | "
                  f"{r['loose_med']} | {bad} | {r['p50_med']}s | {r['succ']} | {r['degrades']} |")
    md += ["", "## 读法（避免只看总数）", "",
           "- 严格 = 列集合全等；宽松 = 数值一致、允许多给列。宽松 - 严格 = 纯形状问题。",
           "- 「稳定错题」= 该模型两轮都没过的题；单轮翻车的题不计入，避免把模型随机抖动当能力差异。",
           "- 降级次数为 0 是因为对照实验用 `CHATBI_PIN_SLOT` 钉住单槽位、刻意关掉降级；",
           "  真实降级行为由 `test_llm_gateway.py` 的故障注入 23 个断言覆盖。", ""]
    out = os.path.join(BASE, "compare_models_1105.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"\n对照表 -> {out}")


if __name__ == "__main__":
    main()
