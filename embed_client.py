# -*- coding: utf-8 -*-
"""
embed_client.py (D6, 2026-11-06)

embedding 客户端【单一事实源】：灌库（seed_embeddings_pg.py）和检索
（text2sql.retrieve_metrics 的语义路）共用这一个模块。
两头各写一份调用代码，就会出现「灌库用的模型 A、查询用的模型 B」这种静默错配——
不同模型的向量空间不可比，cosine 距离全是噪声，而且不报错。

槽位：智谱 embedding-3（2026-11-06 庄拍板 A：复用已有 key，接入速度优先；
B 选项 bge-small-zh 本地方案未选，理由见 D6 验收报告）。换模型只动本文件——
可替换点设计，上层 retrieve_metrics 不感知模型是谁。

维度契约：灌库与查询必须同为 DIM（1024）。pgvector 对维度不一致直接报错，
宁可炸在写入口，也不让检索层静默变差。

超时契约（2026-11-06 下午补，D6 验收后实踩）：必须显式设 timeout（默认 5s）——
OpenAI SDK 不传 timeout 时默认等 10 分钟，而降级逻辑靠「异常触发」：
hang 住不抛异常 = 降级永远不触发 = 整个问数链路被增强层拖死。
当天实踩：Docker daemon 半死时 PG 连接挂住，retrieve_metrics 卡 60s+。
降级设计要覆盖三种失败：报错（except 接住）、超时（timeout 抛异常再被 except 接住）、
慢而不死（超时上限兜住）。只处理第一种不叫降级，叫侥幸。
"""
import functools
import os
from pathlib import Path

from llm_gateway import load_dotenv

load_dotenv()

EMBED_MODEL = os.getenv("ZHIPU_EMBED_MODEL", "embedding-3")
DIM = int(os.getenv("ZHIPU_EMBED_DIM", "1024"))


def is_enabled() -> bool:
    """向量路门卫：ZHIPU_API_KEY 是否可用（先确保 .env 已加载再查）。

    为什么不复用 text2sql.USE_LLM：那是「SQL 生成用什么模式」的判定
    （看 OPENAI_API_KEY，即 DeepSeek 槽位），而 embedding 是另一个厂商的另一个能力——
    DeepSeek 生成 + 智谱向量化是 D4 网关架构下的正常组合。拿生成侧的开关
    去门卫向量化，会出现「真模型回归开着、向量路却静默关闭」的错配。
    """
    load_dotenv()
    return bool(os.getenv("ZHIPU_API_KEY"))


def _client():
    from openai import OpenAI

    key = os.getenv("ZHIPU_API_KEY")
    if not key:
        raise RuntimeError("ZHIPU_API_KEY not set (.env)")
    return OpenAI(
        api_key=key,
        base_url=os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
        timeout=float(os.getenv("EMBED_TIMEOUT_S", "5")),
    )


def embed_texts(texts: list) -> list:
    """批量向量化（灌库用）。返回与输入顺序一致的向量列表，逐条校验维度。"""
    resp = _client().embeddings.create(model=EMBED_MODEL, input=texts, dimensions=DIM)
    out = [None] * len(texts)
    for d in resp.data:
        if len(d.embedding) != DIM:
            raise RuntimeError(f"embed dim mismatch: got {len(d.embedding)}, expect {DIM}")
        out[d.index] = d.embedding
    if any(v is None for v in out):
        raise RuntimeError("embeddings response missing some indexes")
    return out


@functools.lru_cache(maxsize=256)
def embed_query_cached(question: str) -> str:
    """单条问题向量化，返回 pgvector 字面量字符串 '[0.1,0.2,...]'。

    为什么缓存：一次提问在链路里会触发多次 retrieve_metrics（域外判定 + 主链路各一次），
    同一个问题不该向量化两遍——评测 124 次调用里有近半是重复的，缓存后真实 API 调用减半。
    lru_cache 的 key 是问题原文，评测集各题互不干扰。
    """
    vec = embed_texts([question])[0]
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"
