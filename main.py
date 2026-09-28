# -*- coding: utf-8 -*-
"""
main.py —— FastAPI 服务，SSE 流式推送	全链路事件，并托管简易前端

启动：uvicorn main:app --reload --port 8000
"""
import json
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import text2sql

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="ChatBI 智能问数 · 光伏电站场景")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class AskRequest(BaseModel):
    question: str


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/meta")
def meta():
    """给前端展示当前运行模式，方便一眼看出是 MOCK 还是真模型"""
    return {
        "mode": "LLM" if text2sql.USE_LLM else "MOCK",
        "model": os.getenv("OPENAI_MODEL", "deepseek-chat"),
        "max_retry": text2sql.MAX_RETRY,
        "row_limit": text2sql.ROW_LIMIT,
    }


@app.post("/api/ask")
def ask_stream(req: AskRequest):
    """
    考点 8：为什么用 SSE？全链路 5-15 秒，分步推送让用户看到「AI 在思考」。
    事件分类（对齐 AG-UI 的思路）：生命周期(step) / 文本增量(text) / 结果快照(result) / 异常(error)。
    """
    import time

    def gen():
        # MOCK 模式下事件是瞬间算完的，不加节奏的话四步会一闪而过，
        # 看起来像"没流式"。真模型模式下这段 sleep 相对于模型延迟可以忽略。
        pace = 0.05 if text2sql.USE_LLM else 0.45
        first = True
        for ev in text2sql.ask_stream(req.question):
            if not first:
                time.sleep(pace)
            first = False
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/ask_sync")
def ask_sync(req: AskRequest):
    """非流式接口，给 eval.py 和自动化评测用"""
    return text2sql.ask(req.question)
