# -*- coding: utf-8 -*-
"""
main.py —— FastAPI 服务，SSE 流式推送	全链路事件，并托管简易前端

启动：uvicorn main:app --reload --port 8000
"""
import json
import os
import time

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import text2sql

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="ChatBI 智能问数 · 光伏电站场景")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 公网演示护栏：设置 CHATBI_ACCESS_CODE 后，问数接口必须带 X-Access-Code。
# 防的不是黑客，是拿到链接的陌生人刷接口烧 LLM key（真金白银）。
# 未设置时护栏不启用 → 本地开发零影响；meta 接口永远放行（前端要先知道要不要输码）。
ACCESS_CODE = os.environ.get("CHATBI_ACCESS_CODE", "").strip()


def check_access(x_access_code: str = Header(default="")):
    if not ACCESS_CODE:
        return
    if x_access_code != ACCESS_CODE:
        raise HTTPException(status_code=401, detail="访问码不对")


# ---------- 访客记录（2026-11-12） ----------
# 目的：区分「我自己的访问」和「真实访客」——谁打开了页面、谁提了什么问题。
# 存 data/visitors.jsonl（一行一条 JSON），与业务库/评测完全隔离。
# eval.py 是纯函数级评测不走 HTTP，不会混入；/api/ask_sync 给自动化脚本用，也不打点。
VISITOR_LOG = os.path.join(BASE_DIR, "data", "visitors.jsonl")


def client_ip(request: Request) -> str:
    """有反代/内网穿透时取 X-Forwarded-For 的第一跳，否则取直连地址"""
    xff = request.headers.get("x-forwarded-for", "")
    ip = xff.split(",")[0].strip() if xff else ""
    if not ip and request.client:
        ip = request.client.host or ""
    return ip or "unknown"


def log_visitor(request: Request, event: str, question: str = "") -> None:
    """event: open=打开页面 / ask=提问。记录失败绝不影响主链路。"""
    try:
        ua = (request.headers.get("user-agent", "") or "")[:120]
        if "uptime" in ua.lower():
            return  # UptimeRobot 保活 ping 不打点：~288 条/天噪音会把 /api/visitors 的 200 条窗口滚爆
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ip": client_ip(request),
            "ua": ua,
            "event": event,
            "q": (question or "")[:120],
        }
        os.makedirs(os.path.dirname(VISITOR_LOG), exist_ok=True)
        with open(VISITOR_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


class AskRequest(BaseModel):
    question: str


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/meta")
def meta(request: Request):
    """给前端展示当前运行模式，方便一眼看出是 MOCK 还是真模型"""
    log_visitor(request, "open")
    return {
        "mode": "LLM" if text2sql.USE_LLM else "MOCK",
        "model": os.getenv("OPENAI_MODEL", "deepseek-chat"),
        "max_retry": text2sql.MAX_RETRY,
        "row_limit": text2sql.ROW_LIMIT,
        "auth_required": bool(ACCESS_CODE),
    }


@app.get("/api/visitors")
def visitors(_ok: None = Depends(check_access)):
    """访客统计：按 IP 聚合（次数/首末/UA/最近提问）+ 逐条访问明细。护栏与 /api/ask 一致。"""
    agg: dict = {}
    recents: list = []
    try:
        with open(VISITOR_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                recents.append(rec)
                ip = rec.get("ip") or "unknown"
                v = agg.setdefault(ip, {"ip": ip, "hits": 0, "first": rec.get("ts", ""),
                                        "last": rec.get("ts", ""), "uas": [], "questions": []})
                v["hits"] += 1
                v["last"] = rec.get("ts", "") or v["last"]
                ua = rec.get("ua") or ""
                if ua and ua not in v["uas"]:
                    v["uas"].append(ua)
                if rec.get("event") == "ask" and rec.get("q"):
                    v["questions"].append(rec["q"])
    except FileNotFoundError:
        pass
    lst = sorted(agg.values(), key=lambda v: v["last"], reverse=True)
    for v in lst:
        v["uas"] = v["uas"][:3]
        v["questions"] = v["questions"][-10:]
    recents.reverse()  # 最新在前
    return {
        "total_hits": sum(v["hits"] for v in lst),
        "unique_ips": len(lst),
        "visitors": lst,
        "recent": recents[:200],
    }


@app.post("/api/ask")
def ask_stream(req: AskRequest, request: Request, _ok: None = Depends(check_access)):
    """
    考点 8：为什么用 SSE？全链路 5-15 秒，分步推送让用户看到「AI 在思考」。
    事件分类（对齐 AG-UI 的思路）：生命周期(step) / 文本增量(text) / 结果快照(result) / 异常(error)。
    """
    log_visitor(request, "ask", req.question)

    def gen():
        # MOCK 模式下事件是瞬间算完的，不加节奏的话四步会一闪而过，
        # 看起来像"没流式"。真模型模式下这段 sleep 相对于模型延迟可以忽略。
        pace = 0.05 if text2sql.USE_LLM else 0.45
        first = True
        # 兔底：生成器中途抛异常（LLM API 故障 / DB 层未捕获错误）时，
        # 不兜底 = 连接静默断掉，前端只会看到"走到一半就没了"且无错误提示。
        # 2026-11-18 手机端实锤：两次提问都停在生成 SQL 后无结果无报错，就是这类静默死亡。
        try:
            for ev in text2sql.ask_stream(req.question):
                if not first:
                    time.sleep(pace)
                first = False
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            yield "data: " + json.dumps(
                {"event": "error", "step": 4, "title": "服务端内部错误",
                 "text": type(e).__name__ + ": " + str(e)},
                ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/ask_sync")
def ask_sync(req: AskRequest, _ok: None = Depends(check_access)):
    """非流式接口，给 eval.py 和自动化评测用"""
    return text2sql.ask(req.question)
