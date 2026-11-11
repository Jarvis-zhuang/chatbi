# -*- coding: utf-8 -*-
"""
agent_graph.py —— D8（2026-11-11）LangGraph 最小闭环 · 可选 Agent 模式

【物理隔离 · 这是本日最重要的设计约束】
本文件不被 main.py / text2sql.py 引用。默认问数链路仍是 text2sql.ask()，
只有显式运行本文件（或将来在 main.py 用 CHATBI_USE_AGENT=1 懒加载）才走这张图。
云端 requirements-deploy.txt 不装 langgraph，因此线上不可能误启用实验代码。
依据：15 号文件「D8 不必接全量回归，LangGraph 做成可选 Agent 模式，数字塌了回退手写链路」
      + 18 号文件 D8 B 档「三节点 + 3 工具跑通即止，不加复杂度（不做 MCP / 不做多轮）」。

【state 图 · 徒手画这三条边就够】
    START -> plan -> tools -> reflect -> END
                     ^          |
                     +----------+   (失败/结果可疑时回 tools，最多 2 次)

三节点各干一件事：
  plan    规划：确定性路由，决定调哪几个工具、什么顺序；护栏与定义类直答在这一层短路
  tools   工具：按游标执行一个工具；三个工具全是现有函数的薄包装，不重写任何业务逻辑
  reflect 反思：看执行结果，失败/可疑就带 error_hint 回 tools 重跑 SQL 工具；
                attempts 到 MAX_AGENT_RETRY=2 就停 —— 硬上限防死循环烧 token

三个工具（注册表 TOOLS，名字对齐 11/06 庄拍板的「3 工具版」）：
  get_schema       <- text2sql.select_tables + render_schema
  retrieve_metrics <- text2sql.retrieve_metrics
  run_readonly_sql <- text2sql.run_sql（四层校验 + 行数封顶 + 只读账号，原样复用）

【为什么不跑全量回归】本模式默认关，默认链路一行未改；32 题开发集、25 条 held-out
基线与本文件无关。要接全量回归必须先拍板，并按 15 号文件「数字塌了回退手写 pipeline」执行。

运行（本地 .venv 专用，MOCK 模式免 API Key、零成本）：
    .\\.venv\\Scripts\\python.exe agent_graph.py "各省份装机容量排名"
"""
from __future__ import annotations

import json
import os
import sys
from typing import TypedDict

from langchain_core.tools import tool
from langgraph.graph import StateGraph, START, END

import text2sql as t2s

# 硬上限：反思节点最多让 SQL 工具重跑 2 次。和 text2sql.MAX_RETRY 同名不同物——
# 那是手写链路的自修正预算，这是图上的重试预算，两者互不干扰（本文件不改常量）。
MAX_AGENT_RETRY = 2
SQL_TOOL = "run_readonly_sql"


# ============================================================ 工具层：3 个薄包装
@tool
def get_schema(question: str) -> str:
    """查表结构：只返回与问题相关的表，注释写的是业务口径而不是字段名。"""
    return t2s.render_schema(t2s.select_tables(question))


@tool
def retrieve_metrics(question: str) -> list:
    """查业务口径定义：返回指标名 / 定义 / 公式，生成 SQL 前注入。"""
    return t2s.retrieve_metrics(question)


@tool
def run_readonly_sql(sql: str) -> dict:
    """执行只读 SQL：复用原有四层校验、行数封顶与只读账号，返回列与行。"""
    return t2s.run_sql(sql)


# 「注册」这一步是 11/06 拍板的核心：工具不是散着调的，是注册表里可被规划节点路由的实体。
TOOLS = {t.name: t for t in (get_schema, retrieve_metrics, run_readonly_sql)}


# ============================================================ 状态：一份在节点间传的对象
class AgentState(TypedDict, total=False):
    question: str
    plan: list[str]
    cursor: int
    definition_mode: bool
    schema_text: str
    tables: list[str]
    metrics: list
    sql: str
    result: dict
    error_hint: str
    attempts: int
    trace: list[str]
    done: bool
    refused: bool
    answer: str


def _trace(state: AgentState, line: str) -> list[str]:
    return list(state.get("trace", [])) + [line]


# ============================================================ 节点 1：规划
def plan_node(state: AgentState) -> dict:
    """确定性路由。这里刻意不调 LLM：8 年前端做状态机，路由规则本来就该是显式的，
    让「为什么调这个工具」能被逐行指出来，而不是藏在模型输出里。"""
    q = (state.get("question") or "").strip()
    trace = _trace(state, "plan: 开始")

    # 护栏前置于图：和手写链路同一套判定，避免 Agent 模式成为绕过安全校验的后门。
    if t2s.is_injection_attempt(q):
        return {"done": True, "refused": True, "answer": t2s.INJECTION_REFUSE_TEMPLATE,
                "plan": [], "trace": trace + ["plan: 注入拦截，短路"]}
    if t2s.is_write_intent(q):
        return {"done": True, "refused": True, "answer": t2s.REFUSE_WRITE_TEMPLATE,
                "plan": [], "trace": trace + ["plan: 写意图拦截，短路"]}
    if t2s.is_meta_question(q) or t2s.is_out_of_scope(q):
        return {"done": True, "refused": True, "answer": t2s.REFUSE_TEMPLATE,
                "plan": [], "trace": trace + ["plan: 元问题/域外问题，短路"]}

    tables = t2s.select_tables(q)
    # 问定义只走口径检索这一个工具，不需要选表和 SQL —— 路由本身就是一种规划。
    if t2s.DEFINITION_RE.search(q):
        return {"plan": ["retrieve_metrics"], "cursor": 0, "definition_mode": True,
                "tables": tables, "trace": trace + ["plan: 定义类问题 -> 只调 retrieve_metrics"]}

    return {"plan": ["get_schema", "retrieve_metrics", SQL_TOOL], "cursor": 0,
            "definition_mode": False, "tables": tables,
            "trace": trace + ["plan: 问数 -> get_schema / retrieve_metrics / run_readonly_sql"]}


# ============================================================ 节点 2：工具
def tools_node(state: AgentState) -> dict:
    if state.get("done"):
        return {}
    plan = state.get("plan", [])
    cursor = state.get("cursor", 0)
    if cursor >= len(plan):
        return {}
    name = plan[cursor]
    q = state.get("question", "")

    if name == SQL_TOOL:
        # SQL 生成不是工具，是调用工具前的准备：它依赖前两个工具拿到的 schema 和口径。
        metrics = state.get("metrics") or []
        sql = (t2s.llm_generate_sql(q, state.get("tables") or [], metrics,
                                    state.get("error_hint", ""))
               if t2s.USE_LLM else t2s.mock_generate_sql(q))
        args = {"sql": sql}
    else:
        args = {"question": q}

    out = TOOLS[name].invoke(args)
    update = {"cursor": cursor + 1,
              "trace": _trace(state, f"tools: 调用 {name} -> {type(out).__name__}")}
    if name == "get_schema":
        update["schema_text"] = out
    elif name == "retrieve_metrics":
        update["metrics"] = out
    else:
        update["sql"] = args["sql"]
        update["result"] = out
    return update


# ============================================================ 节点 3：反思
def reflect_node(state: AgentState) -> dict:
    trace = list(state.get("trace", []))
    if state.get("done"):
        return {"trace": trace + ["reflect: 规划层已短路，结束"]}

    if state.get("definition_mode"):
        metrics = state.get("metrics") or []
        answer = metrics[0]["definition"] if metrics else "未检索到该指标口径"
        return {"done": True, "answer": answer,
                "trace": trace + ["reflect: 定义类问题，直答口径定义"]}

    # 工具队列还没走完 -> 回 tools 接着调下一个
    if state.get("cursor", 0) < len(state.get("plan", [])):
        return {"trace": trace + ["reflect: 工具队列未完成，继续"]}

    res = state.get("result") or {}
    attempts = state.get("attempts", 0)
    if res.get("ok"):
        suspicious, reason = t2s.looks_suspicious(state.get("question", ""),
                                                  res.get("columns"), res.get("rows"))
        if not suspicious:
            return {"done": True, "trace": trace + ["reflect: 结果正常，结束"]}
    else:
        reason = res.get("error") or "SQL 执行失败"

    # 硬上限：模型抖动 / SQL 一直写错时，烧的是额度和时间，不是无限重试。
    if attempts >= MAX_AGENT_RETRY:
        return {"done": True, "error_hint": reason,
                "trace": trace + [f"reflect: 已达重试硬上限 {MAX_AGENT_RETRY}，停止（{reason}）"]}

    return {
        "attempts": attempts + 1,
        # 只回退到 SQL 工具：选表和口径检索是确定性的，重跑纯属浪费 token
        "cursor": len(state.get("plan", [])) - 1,
        "error_hint": t2s.selfcheck_hint(reason) if res.get("ok") else reason,
        "result": {},
        "trace": trace + [f"reflect: {reason} -> 第 {attempts + 1} 次重试（上限 {MAX_AGENT_RETRY}）"],
    }


def _route(state: AgentState) -> str:
    return "end" if state.get("done") else "tools"


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("plan", plan_node)
    g.add_node("tools", tools_node)
    g.add_node("reflect", reflect_node)
    g.add_edge(START, "plan")
    g.add_edge("plan", "tools")
    g.add_edge("tools", "reflect")
    g.add_conditional_edges("reflect", _route, {"tools": "tools", "end": END})
    return g.compile()


GRAPH = build_graph()


def run_agent(question: str) -> dict:
    """可选 Agent 模式的唯一入口。返回形状对齐 text2sql.ask()，便于将来接 API。"""
    final = GRAPH.invoke(
        {"question": question, "plan": [], "cursor": 0, "definition_mode": False,
         "trace": [], "attempts": 0, "done": False, "refused": False, "result": {}},
        # 框架层第二道保险：图自己有 2 次硬上限，recursion_limit 防的是「改坏了也停得下来」
        config={"recursion_limit": 20},
    )
    res = final.get("result") or {}
    return {
        "question": question,
        "sql": final.get("sql"),
        "ok": bool(res.get("ok")) or bool(final.get("answer")),
        "refused": bool(final.get("refused")),
        "answer": final.get("answer"),
        "columns": res.get("columns"),
        "rows": res.get("rows"),
        "attempts": final.get("attempts", 0),
        "tables": final.get("tables"),
        "trace": final.get("trace", []),
        "mode": "LLM" if t2s.USE_LLM else "MOCK",
        "dialect": t2s.DIALECT,
        "error_hint": final.get("error_hint"),
    }


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    q = " ".join(sys.argv[1:]).strip() or "各省份装机容量排名"
    out = run_agent(q)
    # 控制台只打前 3 行结果，完整数据在 out 里；避免一次刷屏
    preview = dict(out)
    if preview.get("rows"):
        preview["rows"] = preview["rows"][:3]
        preview["rows_note"] = f"共 {len(out['rows'])} 行，仅预览前 3 行"
    print("mode:", out["mode"], "| dialect:", out["dialect"],
          f"| agent_retry_limit: {MAX_AGENT_RETRY} | tools: {list(TOOLS)}")
    print(json.dumps(preview, ensure_ascii=False, indent=2, default=str))
