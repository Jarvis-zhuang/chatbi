# -*- coding: utf-8 -*-
"""
test_llm_gateway.py —— 网关故障注入测试（不需要任何 API key）
跑法：
  .venv/Scripts/python.exe test_llm_gateway.py
覆盖 6 个场景：
  T1 瞬时错误 ×2 后成功      → 重试生效，不降级
  T2 瞬时错误重试耗尽        → 自动降级到备胎，降级计数 +1
  T3 致命错误(401)           → 不重试，直接切备胎
  T4 所有槽位都失败          → 抛 GatewayError
  T5 402 欠费分类            → 必须判为 fatal（今天硅基流动欠费的活教材）
  T6 P50 延迟统计            → 有值且落在合理区间
"""
import sys
import os
import time

from llm_gateway import (Gateway, GatewayError, ModelSlot, classify_error)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  <- {detail}" if detail and not cond else ""))


class FakeHTTPError(Exception):
    """带状态码的假异常，模拟 openai SDK 的报错形态。"""
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status_code = status


def scripted_caller(script: list, ok_text: str = "SELECT 1"):
    """按脚本依次吐错/吐结果；脚本走完后固定成功。"""
    todo = list(script)

    def call(slot, messages):
        if todo:
            item = todo.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return ok_text
    return call


def make_slot(name: str, script: list, ok_text: str = "SELECT 1") -> ModelSlot:
    return ModelSlot(name=name, api_key="fake-key", model="fake",
                     caller_fn=scripted_caller(script, ok_text))


def quiet(msg: str) -> None:
    pass  # 测试时不刷屏


print("=" * 60)
print("T1 瞬时错误 429 两次后成功：重试生效、不降级")
g = Gateway([make_slot("A", [FakeHTTPError(429), FakeHTTPError(429)],
                       ok_text="SELECT 't1'")],
            backoff_base=0.01, logger=quiet)
out = g.chat([{"role": "user", "content": "q"}], tag="T1")
st = g.stats()["slots"]["A"]
check("返回结果正确", out == "SELECT 't1'", f"got {out!r}")
check("重试计数 = 2", st["retries"] == 2, f"got {st['retries']}")
check("成功 1 次 / 无失败", st["ok"] == 1 and st["fail"] == 0, f"got {st}")
check("无降级事件", g.degrade_events == 0, f"got {g.degrade_events}")

print("T2 瞬时错误重试耗尽：自动降级到备胎")
g = Gateway([make_slot("A", [FakeHTTPError(429), FakeHTTPError(429), FakeHTTPError(429)]),
             make_slot("B", [], ok_text="SELECT 'from-B'")],
            backoff_base=0.01, logger=quiet)
out = g.chat([{"role": "user", "content": "q"}], tag="T2")
stA = g.stats()["slots"]["A"]
stB = g.stats()["slots"]["B"]
check("备胎接管，返回 B 的结果", out == "SELECT 'from-B'", f"got {out!r}")
check("A 重试 2 次后耗尽（3 次尝试）", stA["retries"] == 2 and stA["fail"] == 1, f"got {stA}")
check("B 记为降级接管", stB["degraded"] == 1 and stB["ok"] == 1, f"got {stB}")
check("全局降级事件 = 1", g.degrade_events == 1, f"got {g.degrade_events}")

print("T3 致命错误 401：一次都不重试，直接切备胎")
g = Gateway([make_slot("A", [FakeHTTPError(401)]),
             make_slot("B", [], ok_text="SELECT 'from-B'")],
            backoff_base=0.01, logger=quiet)
out = g.chat([{"role": "user", "content": "q"}], tag="T3")
stA = g.stats()["slots"]["A"]
check("返回备胎结果", out == "SELECT 'from-B'", f"got {out!r}")
check("A 零重试（401 换 key 才有用，重试烧钱无意义）",
      stA["retries"] == 0 and stA["fail"] == 1, f"got {stA}")

print("T4 所有槽位都失败：抛 GatewayError")
g = Gateway([make_slot("A", [FakeHTTPError(429)] * 3),
             make_slot("B", [FakeHTTPError(503)] * 3)],
            backoff_base=0.01, logger=quiet)
try:
    g.chat([{"role": "user", "content": "q"}], tag="T4")
    check("应抛 GatewayError 而没有抛", False, "no exception raised")
except GatewayError as e:
    check("抛出 GatewayError", True)
    check("cause 保留了最后一个原始错误", isinstance(e.__cause__, Exception),
          f"cause = {e.__cause__!r}")

print("T5 402 欠费分类：必须判为 fatal（今天硅基流动的活教材）")
check("402 -> fatal（重试不会自己充值）",
      classify_error(FakeHTTPError(402)) == "fatal")
check("429 -> transient（限流值得等）",
      classify_error(FakeHTTPError(429)) == "transient")
check("500 -> transient", classify_error(FakeHTTPError(500)) == "transient")
check("超时类异常 -> transient",
      classify_error(TimeoutError("read timed out")) == "transient")
check("未知异常 -> fatal（别拿重试烧钱）",
      classify_error(ValueError("bad json")) == "fatal")

print("T6 P50 延迟统计")
g = Gateway([make_slot("A", [], ok_text="ok")], backoff_base=0.01, logger=quiet)
# 直接灌三笔延迟 0.1/0.2/0.3，绕过真实计时
for lat in (0.1, 0.2, 0.3):
    g._record("A", ok=True, latency=lat)
p50 = g.stats()["slots"]["A"]["p50_latency_s"]
check("P50 = 0.2（奇数个取中位）", p50 == 0.2, f"got {p50}")

print("T7 CHATBI_PIN_SLOT 钉住单槽：对照实验不触发降级")
import llm_gateway as gwmod

old_env = {k: os.environ.get(k) for k in ("CHATBI_PIN_SLOT", "GATEWAY_STATS_FILE")}
try:
    os.environ["CHATBI_PIN_SLOT"] = "backup-glm-flash"
    os.environ.pop("GATEWAY_STATS_FILE", None)
    gwmod._default = None  # 重置单例，让 pin 生效
    pinned = gwmod.default_gateway(logger=quiet)
    check("只有一个槽位", len(pinned.slots) == 1, f"got {[s.name for s in pinned.slots]}")
    check("钉住的是 backup-glm-flash", pinned.slots[0].name == "backup-glm-flash")
    os.environ.pop("CHATBI_PIN_SLOT", None)
    gwmod._default = None
    plain = gwmod.default_gateway(logger=quiet)
    check("不 pin 时默认降级链不含 alt-glm-5.3",
          "alt-glm-5.3" not in [s.name for s in plain.slots],
          f"got {[s.name for s in plain.slots]}")
    os.environ["CHATBI_PIN_SLOT"] = "alt-glm-5.3"
    gwmod._default = None
    alt = gwmod.default_gateway(logger=quiet)
    check("钉 alt 时槽位存在且 model=glm-5.3",
          len(alt.slots) == 1 and alt.slots[0].model == "glm-5.3",
          f"got {[(s.name, s.model) for s in alt.slots]}")
    os.environ["CHATBI_PIN_SLOT"] = "no-such-slot"
    gwmod._default = None
    try:
        gwmod.default_gateway(logger=quiet)
        check("钉不存在的槽位应报错", False, "no exception")
    except gwmod.GatewayError:
        check("钉不存在的槽位应报错", True)
finally:
    gwmod._default = None
    for k, v in old_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

print("=" * 60)
print(f"结果：{len(PASS)}/{len(PASS) + len(FAIL)} 通过")
if FAIL:
    print("未通过项：", "；".join(FAIL))
    sys.exit(1)
print("网关骨架就绪：主 DeepSeek + 备胎槽位等 key 到位即自动启用。")
