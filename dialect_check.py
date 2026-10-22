# -*- coding: utf-8 -*-
"""dialect_check.py —— 方言一致性自检（W1 D2 配套，防止 prompt 与连接再次错配）

背景：prompt 决定模型写什么方言，连接决定数据库认什么方言。
两者一旦分开维护，就会出现「prompt 说 SQLite、实际连 PG」的静默错配——
错不在模型（它严格照 prompt 写了），错在系统只会说一种方言。

这个脚本把「一致性」变成可执行断言，而不是靠人记得。
用法：
  python dialect_check.py           # 跟随当前 USE_PG 环境变量
  USE_PG=1 python dialect_check.py  # 强制检查 PG 档
退出码：0 一致；1 不一致（可直接当提交前门槛）
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows 控制台 GBK 兜底

import db
import text2sql as t

FAIL = []

# 每个方言「不该出现」的函数名——出现即说明 prompt 说错了话
FORBIDDEN = {
    "postgres": ["strftime", "date('", "date(stat_date", "AUTOINCREMENT", "sqlite"],
    "sqlite": ["to_char", "::numeric", "::date", "INTERVAL", "postgres"],
}

sys_prompt = t.llm_system_prompt()
anchor = t._anchor_hint()
contract = t.output_contract()
prompt_all = "\n".join([sys_prompt, anchor, contract])

print(f"[1] 连接层方言 db.DIALECT      = {db.DIALECT}")
print(f"    USE_PG 环境变量            = {os.getenv('USE_PG', '(unset)')!r}")
print(f"    prompt 声明的方言          = {t.DIALECT_SPEC[db.DIALECT]['name']}")

if db.DIALECT != ("postgres" if db.USE_PG else "sqlite"):
    FAIL.append("db.DIALECT 与 db.USE_PG 不一致（单一事实源被绕过了）")

print("[2] prompt 反向词检查（出现即错配）：")
for bad in FORBIDDEN[db.DIALECT]:
    hit = bad.lower() in prompt_all.lower()
    print(f"    {'HIT ' if hit else 'ok  '} {bad!r}")
    if hit:
        FAIL.append(f"prompt 里残留反方言写法 {bad!r}")

print("[3] prompt 必备词检查（缺了说明注入没生效）：")
MUST = {
    "postgres": ["to_char", "::numeric", "INTERVAL", "PostgreSQL"],
    "sqlite": ["strftime", "SQLite"],
}[db.DIALECT]
for need in MUST:
    hit = need.lower() in prompt_all.lower()
    print(f"    {'ok  ' if hit else 'MISS'} {need!r}")
    if not hit:
        FAIL.append(f"prompt 缺少方言必备写法 {need!r}")

print("\n[4] 实际注入的内容（人工过一眼）：")
for line in anchor.strip().splitlines():
    if "本月" in line or "近半年" in line:
        print("    " + line.strip())
for line in contract.splitlines():
    if "趋势/走势" in line:
        print("    " + line.strip())
for line in sys_prompt.splitlines():
    if db.DIALECT == "postgres" and ("ROUND" in line or "日期列" in line):
        print("    " + line.strip())

print()
if FAIL:
    print("结论：不一致 —— " + "；".join(FAIL))
    sys.exit(1)
print(f"结论：一致（{db.DIALECT} 档），prompt 与连接说的是同一种方言")
