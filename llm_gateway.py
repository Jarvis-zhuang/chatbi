# -*- coding: utf-8 -*-
"""
llm_gateway.py —— 多模型网关（2026-11-04 晚，D3 收尾）
=====================================================
职责：把「调 LLM」收口成一个入口，提供三件事：
  1. 重试：指数退避 + 抖动（复用 11-04 手写骨架），只重试瞬时错误
  2. 降级：主模型重试耗尽 → 自动切下一个模型，记录降级次数
  3. 观测：每槽位 调用数/成功率/重试数/P50 延迟（内存统计，进程生命周期内有效）

槽位配置（按优先级，读 .env，没配 key 的槽自动跳过）：
  主：  OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL          （现 DeepSeek）
  备1： ZHIPU_API_KEY / ZHIPU_MODEL（默认 glm-5.3-flash）/ ZHIPU_REASONING_EFFORT
  备2： SILICONFLOW_API_KEY / SILICONFLOW_MODEL（可选占位）

错误分类（classify_error）：
  致命——重试无意义，直接降级/报错：400 / 401 / 402(欠费) / 403 / 404 / 422
  瞬时——退避后重试：429 限流 / 5xx / 超时 / 连接错误

用法：
  from llm_gateway import chat
  sql_text = chat(messages)
故障注入测试：
  .venv/Scripts/python.exe test_llm_gateway.py
"""
import os
import json
import time
import random
import atexit
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional


# ---------------------------------------------------------- .env 加载
def load_dotenv(path: Optional[Path] = None) -> None:
    """轻量 .env 加载：只补缺，不覆盖已有的环境变量（shell 已导出的优先）。"""
    p = path or Path(__file__).with_name(".env")
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---------------------------------------------------------- 错误分类
def classify_error(exc: Exception) -> str:
    """返回 'fatal'（重试无意义）或 'transient'（退避后可重试）。

    不绑定 openai SDK 版本的异常类，只看状态码属性 + 异常类名，稳。
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        # 402 欠费：重试一万次它也不会自己充值 —— 今天庄亲眼见的就是这个码
        if status in (400, 401, 402, 403, 404, 422):
            return "fatal"
        return "transient"  # 429 / 500 / 502 / 503 ...
    name = type(exc).__name__.lower()
    if "timeout" in name or "connection" in name:
        return "transient"
    return "fatal"  # 解析错误等未知异常，别拿重试烧钱


def describe(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    return f"HTTP {status}" if isinstance(status, int) else type(exc).__name__


# ---------------------------------------------------------- 槽位定义
@dataclass
class ModelSlot:
    """一个可调用的模型端点。caller_fn 仅供测试注入假调用，真跑时为 None。"""
    name: str
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    max_retries: int = 3
    extra_body: Optional[dict] = None  # 厂商私有参数（如智谱关思考模式）
    caller_fn: Optional[Callable] = None
    _client: object = field(default=None, repr=False, compare=False)

    @property
    def available(self) -> bool:
        return bool(self.caller_fn) or bool(self.api_key)

    def call(self, messages: list) -> str:
        if self.caller_fn is not None:
            return self.caller_fn(self, messages)
        return _openai_call(self, messages)


def _openai_call(slot: ModelSlot, messages: list) -> str:
    """真模型调用：OpenAI 兼容接口（DeepSeek / 智谱 / 硅基流动 都吃这一套）。"""
    from openai import OpenAI  # 延迟导入：MOCK 模式不装 SDK 也能 import 本模块

    if slot._client is None:
        # 客户端必须有超时：否则连接一直挂着，重试/降级永远不会被触发
        timeout_s = float(os.getenv("LLM_TIMEOUT_S", "30"))
        slot._client = OpenAI(api_key=slot.api_key, base_url=slot.base_url or None,
                              timeout=timeout_s)
    kwargs = {"extra_body": slot.extra_body} if slot.extra_body else {}
    resp = slot._client.chat.completions.create(
        model=slot.model, messages=messages, temperature=0, **kwargs
    )
    return resp.choices[0].message.content


def load_slots() -> List[ModelSlot]:
    """按优先级从 .env / 环境变量读槽位。key 缺失的槽直接不出场。"""
    slots: List[ModelSlot] = []
    if os.getenv("OPENAI_API_KEY"):
        slots.append(ModelSlot(
            name="primary-deepseek",
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL", ""),
            model=os.getenv("OPENAI_MODEL", "deepseek-chat"),
        ))
    if os.getenv("ZHIPU_API_KEY"):
        # 2026-11-05 实测定版：账号在架 flash 只有 glm-5.3-flash / -flashx / -flashx-legal，
        # glm-4.5-flash 是已下线别名（会被自动路由）。5.3 系列始终思考，
        # 传 thinking.type 会 400；用 reasoning_effort=low 压掉推理 tokens（实测 0.9s）。
        effort = os.getenv("ZHIPU_REASONING_EFFORT", "low")
        extra = {"reasoning_effort": effort} if effort else None
        slots.append(ModelSlot(
            name="backup-glm-flash",
            api_key=os.getenv("ZHIPU_API_KEY"),
            base_url=os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
            model=os.getenv("ZHIPU_MODEL", "glm-5.3-flash"),
            extra_body=extra,
        ))
        # 对照实验第三个模型：同厂商正常档（非重度思考），与 DeepSeek-V3 可比。
        # 默认不进降级链，只被 CHATBI_PIN_SLOT=alt-glm-5.3 钉住时使用（见 default_gateway）。
        slots.append(ModelSlot(
            name="alt-glm-5.3",
            api_key=os.getenv("ZHIPU_API_KEY"),
            base_url=os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
            model=os.getenv("ZHIPU_MODEL_HEAVY", "glm-5.3"),
            extra_body=extra,
        ))
    if os.getenv("SILICONFLOW_API_KEY"):
        slots.append(ModelSlot(
            name="spare-siliconflow",
            api_key=os.getenv("SILICONFLOW_API_KEY"),
            base_url=os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
            model=os.getenv("SILICONFLOW_MODEL", "Qwen/Qwen3-8B"),
        ))
    return slots


# ---------------------------------------------------------- 网关主体
class GatewayError(RuntimeError):
    """所有可用模型都失败后的最终异常（cause 保留最后一个原始错误）。"""


class Gateway:
    def __init__(self, slots: List[ModelSlot], backoff_base: float = 1.0,
                 jitter_ratio: float = 0.5, logger: Optional[Callable] = None):
        self.slots = [s for s in slots if s.available]
        if not self.slots:
            raise GatewayError("没有任何可用模型槽位（检查 .env 里的 key）")
        self.backoff_base = backoff_base
        self.jitter_ratio = jitter_ratio
        self._log = logger or (lambda msg: print(msg, flush=True))
        # 观测数据：slot -> {calls, ok, fail, retries, degraded, latencies}
        self.stats_raw: dict = {}
        self.degrade_events = 0  # 全局降级次数（主模型撑不住被迫切备胎的次数）

    # -- 内部：单槽重试（手写骨架的完整版） --------------------------
    def _call_with_retry(self, slot: ModelSlot, messages: list) -> str:
        delay = self.backoff_base
        for attempt in range(1, slot.max_retries + 1):
            try:
                return slot.call(messages)
            except Exception as exc:
                kind = classify_error(exc)
                if kind == "fatal" or attempt == slot.max_retries:
                    raise
                sleep_for = delay + random.uniform(0, delay * self.jitter_ratio)
                self._record(slot.name, retries=1)
                self._log(f"[{slot.name}] 第{attempt}次失败 {describe(exc)}（瞬时）"
                          f"→ 退避 {sleep_for:.2f}s 后重试")
                time.sleep(sleep_for)
                delay *= 2

    # -- 内部：观测记录 --------------------------------------------
    def _record(self, name: str, *, ok: bool = False, fail: bool = False,
                retries: int = 0, degraded: bool = False, latency: Optional[float] = None):
        s = self.stats_raw.setdefault(
            name, {"calls": 0, "ok": 0, "fail": 0, "retries": 0,
                   "degraded": 0, "latencies": []})
        if ok:
            s["calls"] += 1
            s["ok"] += 1
            s["latencies"].append(latency)
        if fail:
            s["calls"] += 1
            s["fail"] += 1
        s["retries"] += retries
        if degraded:
            s["degraded"] += 1

    # -- 对外主入口 ------------------------------------------------
    def chat(self, messages: list, tag: str = "") -> str:
        """按槽位优先级依次尝试：重试耗尽或致命错误 → 切下一个模型。"""
        last_exc: Optional[Exception] = None
        for idx, slot in enumerate(self.slots):
            degraded = idx > 0
            try:
                t0 = time.perf_counter()
                text = self._call_with_retry(slot, messages)
                self._record(slot.name, ok=True, degraded=degraded,
                             latency=time.perf_counter() - t0)
                if degraded:
                    self.degrade_events += 1
                    self._log(f"[{tag}] 降级生效：主模型不可用，由 {slot.name} 接管")
                return text
            except Exception as exc:
                self._record(slot.name, fail=True, degraded=degraded)
                self._log(f"[{tag}] [{slot.name}] 放弃（{describe(exc)}），切下一个模型")
                last_exc = exc
        raise GatewayError(f"所有模型槽位均失败：{describe(last_exc)}") from last_exc

    # -- 对外：统计摘要 --------------------------------------------
    def stats(self) -> dict:
        out = {"degrade_events": self.degrade_events, "slots": {}}
        for name, s in self.stats_raw.items():
            lats = sorted(s["latencies"])
            p50 = lats[len(lats) // 2] if lats else None
            out["slots"][name] = {
                "calls": s["calls"], "ok": s["ok"], "fail": s["fail"],
                "retries": s["retries"], "degraded": s["degraded"],
                "success_rate": round(s["ok"] / s["calls"], 3) if s["calls"] else None,
                "p50_latency_s": round(p50, 3) if p50 is not None else None,
            }
        return out

    def summary_line(self) -> str:
        """一行版统计，塞进评测日志尾部用。"""
        parts = [f"降级{self.degrade_events}次"]
        for name, s in self.stats()["slots"].items():
            parts.append(f"{name}:{s['ok']}/{s['calls']} "
                         f"(重试{s['retries']}, P50={s['p50_latency_s']}s)")
        return " | ".join(parts)


# ---------------------------------------------------------- 模块级便捷入口
_default: Optional[Gateway] = None


def default_gateway(**kwargs) -> Gateway:
    """进程内单例：第一次调用时读 .env 建槽位。

    CHATBI_PIN_SLOT=<name>：对照实验用，只保留指定槽位、不触发降级。
    GATEWAY_STATS_FILE=<path>：进程退出时把观测数据落盘，供对照表汇总。
    """
    global _default
    if _default is None:
        load_dotenv()
        slots = load_slots()
        pin = os.getenv("CHATBI_PIN_SLOT", "").strip()
        if pin:
            kept = [s for s in slots if s.name == pin]
            if not kept:
                raise GatewayError(
                    f"CHATBI_PIN_SLOT={pin!r} 不存在；可用槽位：{[s.name for s in slots]}")
            slots = kept
        else:
            # 默认降级链不含对照专用槽，避免多一个备胎改变生产行为
            slots = [s for s in slots if s.name != "alt-glm-5.3"]
        _default = Gateway(slots, **kwargs)
        stats_path = os.getenv("GATEWAY_STATS_FILE", "").strip()
        if stats_path:
            atexit.register(_dump_stats, stats_path, _default)
    return _default


def _dump_stats(path: str, gw: "Gateway") -> None:
    """退出时落盘统计（对照表的数据源）。失败不抛，别把评测主流程带崩。"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"pin": os.getenv("CHATBI_PIN_SLOT", "").strip(),
                       "summary": gw.summary_line(), **gw.stats()},
                      f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        print(f"[gateway] stats dump failed: {exc}", flush=True)


def chat(messages: list, tag: str = "") -> str:
    """最简用法：chat(messages)。text2sql.py 接这个口。"""
    return default_gateway().chat(messages, tag=tag)
