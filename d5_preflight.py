# -*- coding: utf-8 -*-
"""d5_preflight.py —— D5/D6 环境预检（只读：不装包、不建表、不调模型、不花钱）

用法（必须用项目 venv，别用系统 python）：
    .\\.venv\\Scripts\\python.exe d5_preflight.py

为什么有这个文件：pg_shim.sql 头部写过一条教训——
「环境里的状态不在版本库里 = 不可复现」。
PG 容器当初是手工 docker run 起的，仓库里既没有 compose 文件也没有预检脚本，
容器一删就没人知道该验什么。这个文件把「该验什么」写进版本库，
D6 真 RAG 开工前跑一次，缺什么当场显形，不拖到当天爆雷。

11/05 下午补：Docker 起通后现场取证发现三件原脚本没覆盖的事，已变成检查项——
  1) 口径表 metric_glossary 没有向量列（D6 得加列，列宽由 embedding 模型维度定）
  2) 项目 venv 是 uv 建的，默认不带 pip，不是环境坏了
  3) 容器初始化 SQL 在仓库外的 D:\\DockerData\\init\\pg，补 compose 也只复现一半
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
results: list[tuple[str, str, str]] = []  # (status, item, detail)

BLOCKERS = {
    "解释器", "依赖 pgvector", "依赖 numpy", "依赖 sentence_transformers",
    "Docker daemon", "5432 端口", "PG 连接", "pgvector extension", "口径表向量列",
}


def add(status: str, item: str, detail: str = "") -> None:
    results.append((status, item, detail))


def installed(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def check_python() -> None:
    parent = os.path.basename(os.path.dirname(os.path.dirname(sys.executable)))
    in_venv = parent.lower() == ".venv"
    add("OK" if in_venv else "MISS", "解释器", sys.executable
        + ("" if in_venv else "  ← 不是项目 .venv，依赖会全报 MISSING（假故障）"))
    for m in ("psycopg", "openai", "dotenv", "pgvector", "numpy", "sentence_transformers"):
        add("OK" if installed(m) else "MISS", "依赖 " + m,
            "" if installed(m) else "D6 要用，未安装")
    uv = shutil.which("uv")
    note = "pip 可用" if installed("pip") else "venv 无 pip（uv 建的 venv 默认如此，不是环境坏了）"
    if uv:
        note += " → 装包用 uv pip install --python .\\.venv\\Scripts\\python.exe"
    add("NOTE", "装包工具", note)


def find_docker() -> str | None:
    found = shutil.which("docker")
    if found:
        return found
    for p in (r"D:\Docker\resources\bin\docker.exe",
              r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"):
        if os.path.isfile(p):
            return p
    return None


def check_docker() -> None:
    exe = find_docker()
    add("OK" if exe else "MISS", "Docker CLI", exe or "PATH 与常见安装路径都没找到")
    if not exe:
        return
    try:
        p = subprocess.run([exe, "info", "--format", "{{.ServerVersion}}"],
                           capture_output=True, text=True, timeout=15)
    except Exception as e:
        add("MISS", "Docker daemon", "CLI 超时/异常：" + type(e).__name__)
        return
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip().splitlines()
    if p.returncode == 0 and out:
        add("OK", "Docker daemon", "ServerVersion=" + out)
    else:
        add("MISS", "Docker daemon", (err[0] if err else "无 Server 段")[:150])


def check_pg() -> None:
    alive = False
    try:
        with socket.create_connection(("127.0.0.1", 5432), timeout=1.5):
            alive = True
    except OSError as e:
        add("MISS", "5432 端口", type(e).__name__ + "（PG 容器没起）")
    if not alive:
        for item in ("PG 连接", "pgvector extension", "口径表向量列"):
            add("SKIP", item, "端口不通，跳过")
        return
    add("OK", "5432 端口", "有进程监听")
    if not installed("psycopg"):
        for item in ("PG 连接", "pgvector extension", "口径表向量列"):
            add("SKIP", item, "psycopg 未安装")
        return
    import psycopg
    cfg = dict(host=os.getenv("CHATBI_PG_HOST", "127.0.0.1"),
               port=int(os.getenv("CHATBI_PG_PORT", "5432")),
               dbname=os.getenv("CHATBI_PG_DB", "chatbi"),
               user=os.getenv("CHATBI_PG_USER", "chatbi_ro"),
               password=os.getenv("CHATBI_PG_PASSWORD", "chatbi_ro"),
               connect_timeout=3)
    try:
        with psycopg.connect(**cfg) as conn, conn.cursor() as cur:
            cur.execute("select current_database(), current_user")
            row = cur.fetchone()
            add("OK", "PG 连接", "%s / %s" % (row[0], row[1]))
            cur.execute("select extname, extversion from pg_extension where extname = 'vector'")
            hit = cur.fetchone()
            add("OK" if hit else "MISS", "pgvector extension",
                "已装 v" + str(hit[1]) if hit else "未装（镜像可能不是 pgvector/pgvector）")
            cur.execute("""select column_name, udt_name from information_schema.columns
                           where table_schema = 'public' and table_name = 'metric_glossary'""")
            cols = cur.fetchall()
            vec = [r[0] for r in cols if "vector" in (r[1] or "").lower()]
            add("OK" if vec else "MISS", "口径表向量列",
                ", ".join(vec) if vec else "metric_glossary 无向量列 → D6 要加列，列宽由模型维度定")
    except Exception as e:
        add("MISS", "PG 连接", type(e).__name__ + ": " + str(e).splitlines()[0][:110])
        for item in ("pgvector extension", "口径表向量列"):
            add("SKIP", item, "连接失败，跳过")


def check_repro() -> None:
    schema = os.path.join(ROOT, "pg_schema.sql")
    has_decl = False
    if os.path.isfile(schema):
        with open(schema, encoding="utf-8") as f:
            has_decl = "CREATE EXTENSION IF NOT EXISTS vector" in f.read()
    add("OK" if has_decl else "MISS", "schema 声明 vector",
        "pg_schema.sql 已写" if has_decl else "缺 CREATE EXTENSION vector")
    files = [f for f in os.listdir(ROOT)
             if "compose" in f.lower() or f.lower().startswith("dockerfile")]
    add("OK" if files else "MISS", "容器定义入库",
        ", ".join(files) if files else "无 compose/Dockerfile → 手工 docker run，删了不可复现")
    init_dir = os.path.join(ROOT, "docker", "init")
    in_repo = os.path.isdir(init_dir) and any(f.endswith(".sql") for f in os.listdir(init_dir))
    add("OK" if in_repo else "MISS", "初始化脚本入库",
        "docker/init/*.sql" if in_repo else "初始化 SQL 在 D:\\DockerData\\init\\pg（仓库外）")


def main() -> None:
    check_python()
    check_docker()
    check_pg()
    check_repro()
    print("D5/D6 环境预检（只读）  " + sys.executable)
    print("-" * 72)
    for status, item, detail in results:
        print("[%-4s] %-26s %s" % (status, item, detail))
    print("-" * 72)
    miss = [i for s, i, _ in results if s == "MISS"]
    hard = [i for i in miss if i in BLOCKERS]
    debt = [i for i in miss if i not in BLOCKERS]
    print("阻塞 D6  %d 项：%s" % (len(hard), ", ".join(hard) if hard else "无"))
    print("复现欠账 %d 项：%s" % (len(debt), ", ".join(debt) if debt else "无"))
    print("结论：" + ("D6 真 RAG 还不能直接开工" if hard else "硬环境齐了，可以开工（复现欠账另记）"))


if __name__ == "__main__":
    main()
