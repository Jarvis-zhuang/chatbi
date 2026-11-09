# -*- coding: utf-8 -*-
"""
seed_embeddings_pg.py (D6 #3, 2026-11-06)

把 metric_glossary 26 条口径灌上 embedding-3 向量（PG 专用，SQLite 兜底档不做向量检索）。

设计决策（面试点）：
1. 检索文本 = metric_name + aliases + definition + formula——用户问法命中的是名字/别名，
   语义命中的是定义/公式，四段拼一起让一个向量同时覆盖两种召回。
2. 维度 1024（embedding-3 支持 256/512/1024/2048）：26 条小表够用，别上 2048 白费存储。
3. 不建 HNSW 索引：26 行 seq scan + ORDER BY embedding <=> qvec 是微秒级，
   索引反而引入构建/召回率开销——量级上万才值得。知道什么时候【不】用索引和知道怎么建一样值钱。
4. 幂等：重复跑覆盖旧向量。**改了 definition/formula 之后必须重跑本脚本**，
   否则向量还是旧定义的语义——检索层和内容层不同步，是 RAG 最隐蔽的静默 bug。
5. 走 postgres 管理账号（UPDATE 需要写权限）；检索侧仍走 chatbi_ro 只读账号（SELECT 无碍）。
"""
import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).parent))
from embed_client import DIM, embed_texts  # noqa: E402  单一事实源：灌库与检索共用

# 管理连接：默认本机容器；连 Neon 灌向量时用 CHATBI_ADMIN_DSN 覆盖
DSN = os.environ.get("CHATBI_ADMIN_DSN") or "host=127.0.0.1 port=5432 dbname=chatbi user=postgres password=postgres"

ALTER_SQL = "ALTER TABLE metric_glossary ADD COLUMN IF NOT EXISTS embedding vector(1024)"


def main():
    with psycopg.connect(DSN) as conn:
        conn.execute(ALTER_SQL)
        rows = conn.execute(
            "SELECT metric_id, metric_name, aliases, definition, formula FROM metric_glossary"
        ).fetchall()
        print(f"rows to embed: {len(rows)}")

        texts = [
            f"{name}；别名：{aliases or '无'}；定义：{definition}；公式：{formula}"
            for _, name, aliases, definition, formula in rows
        ]
        vectors = embed_texts(texts)

        for (metric_id, *_), vec in zip(rows, vectors):
            if len(vec) != DIM:
                raise SystemExit(f"dim mismatch: got {len(vec)}, expect {DIM}")
            # pgvector 接受 '[0.1,0.2,...]' 文本字面量——不依赖 pgvector python 包
            conn.execute(
                "UPDATE metric_glossary SET embedding = %s WHERE metric_id = %s",
                ("[" + ",".join(f"{x:.7g}" for x in vec) + "]", metric_id),
            )
        conn.commit()

        filled, total = conn.execute(
            "SELECT count(embedding), count(*) FROM metric_glossary"
        ).fetchone()
        print(f"embedded: {filled}/{total}")
        if filled != total:
            raise SystemExit("some rows not embedded")
    print("OK")


if __name__ == "__main__":
    main()
