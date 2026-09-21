"""借阅超时要够馆员跑完一次 LLM 选卡。

2026-09-20 实测那次: 写作台开「RIO便利店调酒」, WorkBuddy 报「飞轮图书馆超时没借到」。
查下来**不是没调到, 也不是服务挂了**:

  · TV 馆员 `/health` 0.64s 响应, 正常;
  · TV 侧 `flywheel_librarian_cache` 在 12:14:17 给同一个 project 写了一行、
    **选了 5 张卡**;
  · 而写作台的超时是 **8 秒**, 早就挂断了。

也就是说 TV 把活干完了、token 烧了、卡选好了, 写手一张没拿到。**每次借阅都这样 = 纯白烧。**
根因是 8 秒按"一次 HTTP 往返"估的, 而馆员选卡**走 LLM**(TV 侧 claude-sonnet-4-6
读最多 50 张候选卡)。

这里钉的是【被禁止的形态】: 把默认超时调回一个连 LLM 调用都跑不完的值。
"""

from __future__ import annotations

import importlib
import os
import pathlib
import re

import config


# 下限按【已知不够的值】定, 不按感觉定:
#   · 8s  实测冷路径必超时;
#   · 30s 实测仍不够 —— 2026-09-21 有一次冷路径落在 (30, 50] 秒(TV 侧缓存行的落地时刻
#         减去调用最早可能的时刻)。
# 所以 ≤30 的任何值都是【已经被证伪的】。45 = 在已证伪区间之上留一点余量。
# ⚠️ 这不是"最佳值", 只是"不许退回已知不够的区间"。真正的值等 TV 侧
#    flywheel_librarian_cache.select_ms 攒够 ≥20 个冷路径样本后按 p95 定 (D-074)。
MIN_SANE_TIMEOUT_SEC = 45


def _code_default() -> float:
    """从源码里取【代码默认值】, 不是取 config.LIBRARIAN_TIMEOUT_SEC。

    ⚠️ 第一版就是直接断言 config.LIBRARIAN_TIMEOUT_SEC —— 那是 **env 解析之后**的值:
       · 环境里有 LIBRARIAN_TIMEOUT_SEC=45 时, 默认值改回 8 这条也照样绿(反证失效);
       · 环境里是 8 时, 它又会在毫不相干的改动上判红。
       两个方向都错。要钉"代码默认值"就得去源码里读那个字面量。
    """
    src = pathlib.Path(config.__file__).read_text(encoding="utf-8")
    m = re.search(r'_get_secret\("LIBRARIAN_TIMEOUT_SEC"\)\s*or\s*"([0-9.]+)"', src)
    assert m, "config.py 里那行的写法变了 —— 同步改这条守卫"
    return float(m.group(1))


def test_default_timeout_is_long_enough_for_an_llm_call():
    got = _code_default()
    assert got >= MIN_SANE_TIMEOUT_SEC, (
        f"借阅超时的【代码默认值】是 {got}s —— 馆员选卡要跑一次 LLM, "
        "这个值下借阅会每次都超时。而它 fail-open, 超时不报错, 只是静默地什么都没借到。"
    )


def test_env_var_still_wins():
    """默认值变大不能把"部署时能调"这件事弄丢 —— 出事时改 env 比改代码快。

    ⚠️ 用 try/finally 精确还原: 第一版是 delenv 之后 reload, 那还的是**代码默认值**
       而不是跑之前的状态 —— 环境里本来有 LIBRARIAN_TIMEOUT_SEC=10 的话, 这个文件跑完
       之后 config 就变成 30 了, 后面的用例读到的是假值。而且没有 finally, 断言一挂
       模块就永久停在 45。
    """
    saved = os.environ.get("LIBRARIAN_TIMEOUT_SEC")
    try:
        os.environ["LIBRARIAN_TIMEOUT_SEC"] = "45"
        assert float(importlib.reload(config).LIBRARIAN_TIMEOUT_SEC) == 45.0
    finally:
        if saved is None:
            os.environ.pop("LIBRARIAN_TIMEOUT_SEC", None)
        else:
            os.environ["LIBRARIAN_TIMEOUT_SEC"] = saved
        importlib.reload(config)


def test_borrow_stays_fail_open(monkeypatch):
    """调大超时的前提是**借不到不拖垮写稿**。这条一旦破了, 上面那个数就得重新权衡。

    ⚠️ 必须先把 URL/KEY 配上再测。第一版没配, 于是函数在"未配置"那一支就 return 了,
    **根本没走到 HTTP 调用**, 把 fail-open 改成往外抛都照样绿 —— 一条永远不失败的用例。
    所以这里除了断言回 [], 还断言 state 是 error 而不是 not_configured: 那是"真的走到
    了异常路径"的凭据。
    """
    import librarian_client
    import requests

    monkeypatch.setattr(config, "LIBRARIAN_URL", "https://example.invalid")
    monkeypatch.setattr(config, "LIBRARIAN_API_KEY", "k")

    def boom(*a, **k):
        raise RuntimeError("librarian 挂了")
    monkeypatch.setattr(requests, "post", boom)

    st: dict = {}
    got = librarian_client.fetch_flywheel_lessons(
        {"project_id": "p", "consumer": "deskcore"}, status=st)

    assert got == [], "借阅失败必须 fail-open 回空 list, 不能把写稿一起拖垮"
    assert st["state"] == librarian_client.BORROW_ERROR, (
        f"没走到异常路径(state={st.get('state')!r}) —— 这条用例又变成空跑了")


def test_a_real_timeout_is_reported_as_timeout_not_generic_error(monkeypatch):
    """**这条才是本 PR 正对着的那一支。**

    上一版只用 RuntimeError 试了 BORROW_ERROR —— 而 requests.Timeout → BORROW_TIMEOUT
    这条路一次都没走到, 偏偏它就是"超时"这件事本身。protocol.md 让模型对 timeout 和
    error 分别处置, 混成一个就等于告诉它"馆员坏了"而不是"这次慢了"。
    """
    import librarian_client
    import requests

    monkeypatch.setattr(config, "LIBRARIAN_URL", "https://example.invalid")
    monkeypatch.setattr(config, "LIBRARIAN_API_KEY", "k")

    def slow(*a, **k):
        raise requests.Timeout("timed out")
    monkeypatch.setattr(requests, "post", slow)

    st: dict = {}
    got = librarian_client.fetch_flywheel_lessons(
        {"project_id": "p", "consumer": "deskcore"}, status=st)

    assert got == []
    assert st["state"] == librarian_client.BORROW_TIMEOUT, (
        f"超时该报 timeout 不是 {st.get('state')!r} —— 两者在协议里处置不同")
