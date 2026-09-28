# ChatBI 智能问数系统 · 光伏电站场景

自然语言问数：选表 → 业务口径检索 → Text-to-SQL → 安全校验 → 只读执行 → 失败自修正 → 结果 + 图表。
全流程 SSE 分步推送，前端浏览器里能一步步看到 AI 在干什么。

> 数据说明：库内全部为本地合成数据（`seed_data.py`），分布按真实格局设计——西北集中式大基地 100-500MW、
> 等效利用小时 1400-1800h；中部/沿海分布式 5-50MW、900-1400h。面试时大方承认 synthetic，
> 重点是「分布合理」证明你懂业务。

## 准确率迭代记录（D1 · 2026-09-28 实测）

同一份 31 题评测集、同一个 `deepseek-chat`（temperature=0），**只改 prompt 与喂给模型的元数据**：

| 版本 | 改了什么 | 执行准确率 |
|---|---|---|
| v1 | 原始 prompt | 11/31 = 35.5% |
| v2 | 注入**时间锚点**：把数据截止日喂给模型，明令禁用 `date('now')` | 14/31 = 45.2% |
| v3 | 加**输出契约**：列数 / 数值精度 / 分组粒度 / 同比只留比值 | 17/31 = 54.8% |
| v4 | 修掉 v3 自己写错的约定（容量保留 2 位、题目没给 N 就不加 LIMIT） | **23/31 = 74.2%** |

MOCK 规则引擎同集对照：**30/31 = 96.8%**（说明规则引擎考自己出的题很高，但那不代表智能）。

**代码落点**（都在 `text2sql.py`）：
- `get_anchor_date()` + `_anchor_hint()`：时间锚点。日期从库里查 `MAX(stat_date)` 得到，**不硬编码**。
- `OUTPUT_CONTRACT`：输出契约。当代码资产管理，就是 prompt engineering 的日常。

**为什么停在 74.2% 不再往上调**：剩余错题大多源于**评测集自身的精度口径不一致**
（有的期望 ROUND 0、有的 2 位、有的不 ROUND），继续改 prompt 就是**拟合评测集**——
数字会涨，真实能力不涨。可复现核对：`_eval_llm_v*.txt`，或 `python eval.py --verbose`。

**下一步的正确杠杆（不是继续调 prompt）**：补**自检反馈循环**
（查出空结果 / 可疑结果时带上下文重生成）+ 场景规则（如多表 JOIN 去重）。
公开同行轨迹显示这条路径能走到 86–91%。

## 30 秒跑起来（不需要 API Key）

```powershell
cd D:\code\ai\chatbi
.\.venv\Scripts\python.exe seed_data.py                      # 1. 生成合成业务库 data/chatbi.db（约 4.4 万行日发电明细）
.\.venv\Scripts\uvicorn.exe main:app --reload --port 8000    # 2. 起服务（依赖已在 .venv 里，免激活直接跑）
```

浏览器打开 <http://localhost:8000>，输入「各省份装机容量排名」，会看到 4 个步骤实时推出来。
右上角显示 `MOCK 模式` 时不用花一分钱，规则引擎兜底，链路完整。

## 接真模型（明早第 5 步）

```powershell
$env:OPENAI_API_KEY="sk-你的key"
$env:OPENAI_BASE_URL="https://api.deepseek.com"
$env:OPENAI_MODEL="deepseek-chat"
.\.venv\Scripts\uvicorn.exe main:app --reload --port 8000
```

同一个问题再问一次，这次 SQL 是模型真生成的。把 MOCK 和真模型的结果对比看一眼，
你就明白 Text-to-SQL 到底在做什么。

## 跑评测集（第 3 天的主力活）

```powershell
.\.venv\Scripts\python.exe eval.py             # 输出准确率：xx/30 = xx.x%
.\.venv\Scripts\python.exe eval.py --verbose   # 逐条看预测 SQL 和失败原因
```

评分用「执行结果比对」而不是字符串比对 SQL——同一语义有无数种写法，跑出来的数据集一致才算对。
`eval_set.json` 里 31 条含 4 条负样本（元问题期望礼貌拒答，不硬生成 SQL）。

## 目录结构

| 文件 | 作用 |
|---|---|
| `text2sql.py` | **核心。全链路都在这里**，每个函数上标了对应哪道面试题（`# 考点 N：`） |
| `main.py` | FastAPI：SSE 接口 `/api/ask` + 同步接口 `/api/ask_sync` + 托管前端 |
| `db.py` | 只读连接（SQLite `mode=ro`；D2 换 Postgres 时只动这个文件） |
| `seed_data.py` | 合成数据生成器 + 业务口径表 26 条 |
| `eval.py` / `eval_set.json` | 评测脚本与 31 条评测集 |
| `static/index.html` | 简易前端：步骤时间线 + SQL 展示 + 表格 + SVG 柱状图 |

## 面试考点 → 代码位置速查

> 总纲第六节共 17 条考点。上面这张表只放了**当前代码里能指到的**，下面分「已实现」和「未实现、排在哪天」两组，避免读代码时找不到。

### A. 现在就能指到代码（读完 text2sql.py 即可答）

| 面试官问题 | 看这里 |
|---|---|
| 为什么不把所有表结构塞进 prompt？ | `select_tables()` (L93) + `render_schema()` (L115)：上下文预算 + 表多了准确率显著下降 |
| Text-to-SQL 生成错了怎么办？ | `ask_stream()` (L529) 的自修正循环，报错回喂模型，`MAX_RETRY=2` (L20) 硬上限 |
| 用户诱导模型删库怎么办？ | `validate_sql()` (L495) 四层：语句白名单 / 关键词黑名单 / 禁止多语句 / 只读连接 `mode=ro`（物理保险） |
| Agent 死循环怎么防？ | `MAX_RETRY=2` (text2sql L20) + `MAX_ROWS=200` (db.py L15) + `QUERY_TIMEOUT=5.0` (db.py L16)，三层各管各的 |
| RAG 在这里用在哪？ | 两路：表检索 `select_tables()`；**业务口径检索** `retrieve_metrics()` (L129)——不是文档问答，是「等效利用小时怎么定义」必须检索到才算得对 |
| 效果怎么量化？ | `python eval.py`：31 条评测集按**执行结果比对**（不是字符串比 SQL） |
| 流式怎么做的，为什么？ | `main.py` 的 SSE (`/api/ask`) + `ask_stream()` 分步 yield；全链路 5-15 秒，让用户看到在思考而不是干等 |
| Agent 和前端之间用什么协议？ | SSE 之上做事件分类：生命周期 `step` / 文本增量 `text` / 工具调用 `sql` / 状态快照 `result`。对齐 AG-UI 的事件分类思路 |
| 怎么防止模型瞎编（域外/元问题）？ | **三道门**：`is_meta_question()` (L261) 元问题 + `is_out_of_scope()` (L249) 域外 + `unknown_region()` (L233) 地域实体校验。护栏在 `ask_stream()` 最前面，命中直接拒答，不选表不检索不生成 SQL；话术 `REFUSE_TEMPLATE` (L111) = 说明做不到的原因 + 给替代路径 |
| 拒答这件事怎么验证？ | `eval_set.json` 里 4 条 `type=refuse` 负样本，`eval.py` 判 `refused is True` |
| 已知限制（面试能主动讲反而加分） | 地域校验是黑名单 + 省份白名单的简版：「美国的转换效率」「北京市」能拦，「海南的发电量」「纽约的发电量」这种不带省市后缀的未知地名拦不住。生产上该用 NER + 实体链接，或按 D4「提问五要素」缺槽位就反问用户 |
| 上下文越来越长怎么办？ | 截断 / 总结 / 检索三策略，本项目选「检索口径定义」而非全量表结构 |
| 为什么不用微调？ | 无对应代码（标准答案）：先 Prompt → 再 RAG → 数据量够且格式要求极高才考虑 LoRA。微调解决「形式」，RAG 解决「知识」 |
| Agent Skill 是什么、怎么设计？ | `run_sql()` 这个 SQL 工具 + 口径检索就是一个 skill 雏形：工具 + 使用说明打包成可复用能力单元，单一职责 |

### B. 还没做，别在代码里找（排期按总纲）

| 面试官问题 | 什么时候有 |
|---|---|
| 混合检索为什么优于纯向量？ | **D5**（10/14-15）：`retrieve_metrics()` 现在只是关键词打分，要换成 pgvector 向量 + Postgres 全文检索双路 + RRF 合并 |
| 引用溯源 / 证据链怎么做的？ | **D5-6**（10/15-16）：口径定义带来源、前端可点开原文、`ts_headline` 高亮 |
| 流式断了怎么办？刷新页面呢？ | **D6-7**（10/17-18）：事件带 sequence、先落库再推、客户端带 `after_sequence` 重连补发 |
| MCP 和 Function Calling 什么关系？ | **第 2 周 D1-2**：自研 MCP Server 把 SQL 查询封装成工具 |
| LangGraph 核心概念（节点/边/条件路由/checkpoint）？ | **第 2 周 D3**：规划→工具→反思三节点闭环，能手画状态图 |
| checkpoint 和会话历史什么关系？ | **第 2 周**：四种存储各司其职，ChatBI 里会话历史放 Postgres，checkpoint 只为 run 恢复，绝不混用 |

## 待办（按总纲推进）

- [x] **D1**：真模型基线 35.5% → **74.2%**（时间锚点 + 输出契约，见上方迭代记录）
- [ ] D2 ①：**自检反馈循环**——查出空结果 / 可疑结果时带上下文重生成
      （当前只在 SQL **执行报错**时重试，**查空不重试**，这是 #03 这类题当初静默返回空集的原因）
- [ ] D2 ②：业务库从 SQLite 换 Postgres（只改 `db.py`）
- [ ] D4：改 schema 注释 / 加 few-shot / 结构化输出 → 每改跑一次 `eval.py`，记录「改了什么 → 准确率从 X 到 Y」
- [ ] D4：提问五要素（指标对象/时间范围/分析维度/筛选条件/排序对比）拆槽位，缺槽位就反问
- [ ] D5：`retrieve_metrics()` 换成 pgvector 向量召回 + Postgres 全文检索，双路 RRF
- [ ] D6-7：Next.js 重写前端，用更复杂的流式渲染与证据链交互
- [ ] 评测集加固：补**边界 case / 对抗 case + held-out 集**，避免长期只在同一批题上优化（防拟合）
