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

import config


# 一次 LLM 选卡(读 ≤50 张候选卡)的量级下限。低于这个数, 借阅基本注定超时 ——
# 而它是 fail-open 的, 超时不报错、只是静默地什么都没借到, 最难发现。
MIN_SANE_TIMEOUT_SEC = 20


def test_default_timeout_is_long_enough_for_an_llm_call():
    assert config.LIBRARIAN_TIMEOUT_SEC >= MIN_SANE_TIMEOUT_SEC, (
        f"借阅超时默认 {config.LIBRARIAN_TIMEOUT_SEC}s —— 馆员选卡要跑一次 LLM, "
        "这个值下借阅会每次都超时。而它 fail-open, 超时不报错, 只是静默地什么都没借到。"
    )


def test_env_var_still_wins(monkeypatch):
    """默认值变大不能把"部署时能调"这件事弄丢 —— 出事时改 env 比改代码快。"""
    monkeypatch.setenv("LIBRARIAN_TIMEOUT_SEC", "45")
    assert float(importlib.reload(config).LIBRARIAN_TIMEOUT_SEC) == 45.0
    monkeypatch.delenv("LIBRARIAN_TIMEOUT_SEC")
    importlib.reload(config)      # 还原, 免得污染同批其它用例


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
