"""``open_project`` 里的借阅只等 ``OPEN_PROJECT_BORROW_SEC``, 到点先交简报(TV 2026-10-08 审计 A-04)。

借阅在 open_project 的必经同步路径上, 原来直接等满 LIBRARIAN_TIMEOUT_SEC(60 秒); MCP
客户端只容忍 ~22 秒 —— 超过的不是"少几张卡", 是**整份简报连 P0 硬约束一起丢**, 协议
让模型停笔。TV 侧 ``flywheel_librarian_cache.select_ms`` 实测: 09-22 冷借 13 次里 12 次
超过 22 秒、6 次超过 60 秒; 那之后写作台周产量 175 → 16 → 0。

这里钉的【被禁止的形态】:
  · open_project 等馆员超过预算;
  · 预算的代码默认值 / 上限超出客户端容忍;
  · 到点之后把借阅线程取消(TV 那边选完会写缓存, 稍后 borrow_lessons 才拿得到);
  · borrow_lessons 工具也被这个预算截断(写手明确要卡时该等满 LIBRARIAN_TIMEOUT_SEC)。
"""

from __future__ import annotations

import pathlib
import re
import threading
import time

import config
import librarian_client as lib
from deskcore import core
from tests.test_deskcore_open_project_borrows import ME, PID, _card, _client


class _SlowLibrarian:
    def __init__(self, delay: float, cards=None):
        self.delay = delay
        self.cards = list(cards or [_card(1)])
        self.briefs: list[dict] = []
        self.finished = threading.Event()

    def __call__(self, brief, *, status=None):
        self.briefs.append(dict(brief))
        time.sleep(self.delay)
        if status is not None:
            status.update({"state": lib.BORROW_BORROWED, "count": len(self.cards),
                           "elapsed_ms": int(self.delay * 1000), "detail": ""})
        self.finished.set()
        return list(self.cards)


def test_open_project_returns_the_brief_within_budget_when_the_librarian_is_slow(monkeypatch, caplog):
    fake = _SlowLibrarian(delay=1.2)
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", fake)
    monkeypatch.setattr(config, "OPEN_PROJECT_BORROW_SEC", 0.3)

    t0 = time.monotonic()
    b = core.build_writing_brief(_client(), PID, user_id=ME, brief={"draft_topic": "早八通勤"})
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, f"open_project 等了 {elapsed:.2f}s —— 预算 0.3s 没生效"
    assert "p0" in b and "stable" in b, "简报本体必须交出去"
    assert b["lessons"] == [] and b["counts"]["lessons"] == 0
    assert b["lessons_status"]["status"] == lib.BORROW_TIMEOUT
    assert "borrow_lessons" in b["lessons_status"]["detail"], "要告诉模型稍后怎么拿卡"
    assert any("budget" in r.getMessage() for r in caplog.records), "留痕: 日志里 grep 得到"

    # 借阅线程没被取消: 馆员那边会跑完(生产里 = TV 写缓存)
    assert fake.finished.wait(3.0), "到点之后借阅线程应该继续跑完, 不是被杀掉"
    assert len(fake.briefs) == 1


def test_a_fast_borrow_is_unaffected_by_the_budget(monkeypatch):
    fake = _SlowLibrarian(delay=0.0, cards=[_card(1), _card(2)])
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", fake)
    monkeypatch.setattr(config, "OPEN_PROJECT_BORROW_SEC", 2.0)
    b = core.build_writing_brief(_client(), PID, user_id=ME)
    assert b["lessons_status"]["status"] == lib.BORROW_BORROWED
    assert b["counts"]["lessons"] == 2


def test_borrow_lessons_tool_still_waits_the_full_librarian_timeout(monkeypatch):
    """预算只管 open_project 这条必经路径。写手明确调 borrow_lessons 要卡时, 等满
    LIBRARIAN_TIMEOUT_SEC 是对的 —— 那是一次有意的等待, 不会把简报一起丢。"""
    fake = _SlowLibrarian(delay=0.5)
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", fake)
    monkeypatch.setattr(config, "OPEN_PROJECT_BORROW_SEC", 0.1)
    out = core.borrow_lessons(_client(), PID, user_id=ME, draft_topic="早八通勤")
    assert out["status"] == lib.BORROW_BORROWED and out["count"] == 1


def _code_bounds() -> tuple[float, float, float]:
    """从源码里读【代码默认值 / 下限 / 上限】, 不读 env 解析后的值(见 test_librarian_timeout 的理由)。"""
    src = pathlib.Path(config.__file__).read_text(encoding="utf-8")
    m = re.search(r'_bounded_number\("OPEN_PROJECT_BORROW_SEC",\s*([0-9.]+),\s*([0-9.]+),\s*([0-9.]+)\)', src)
    assert m, "config.py 里 OPEN_PROJECT_BORROW_SEC 那行的写法变了 —— 同步改这条守卫"
    return float(m.group(1)), float(m.group(2)), float(m.group(3))


def test_the_budget_default_and_ceiling_stay_under_the_client_tolerance():
    """MCP 客户端实测容忍 ~22 秒(config.py LIBRARIAN_TIMEOUT_SEC 那段)。open_project 在
    借阅之外还有 4–6 次库往返 + 2 次 embedding, 所以预算本身要留足余量。"""
    default, lo, hi = _code_bounds()
    assert hi <= 20, f"上限 {hi}s 已经贴到 ~22s 的客户端容忍 —— 超过它简报整份丢"
    assert default <= 15, f"默认 {default}s 太长: 冷借 p50 就在 23–59s, 等它没意义, 只会让简报丢"
    assert lo >= 1


def test_env_override_is_clamped_not_trusted(monkeypatch):
    """部署时能调, 但调不出客户端容忍之外; 写坏了退回默认, 别让 import 炸掉主服务。"""
    import importlib
    try:
        monkeypatch.setenv("OPEN_PROJECT_BORROW_SEC", "500")
        assert importlib.reload(config).OPEN_PROJECT_BORROW_SEC <= 20
        monkeypatch.setenv("OPEN_PROJECT_BORROW_SEC", "nan")
        assert importlib.reload(config).OPEN_PROJECT_BORROW_SEC == _code_bounds()[0]
        monkeypatch.setenv("OPEN_PROJECT_BORROW_SEC", "6")
        assert importlib.reload(config).OPEN_PROJECT_BORROW_SEC == 6.0
    finally:
        monkeypatch.delenv("OPEN_PROJECT_BORROW_SEC", raising=False)
        importlib.reload(config)
