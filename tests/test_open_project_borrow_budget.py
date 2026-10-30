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

2026-10-30 发布收尾: 默认 10 → 15 秒(最近冷借 14~18 秒)。放得下是因为借阅实际等的是
min(OPEN_PROJECT_BORROW_SEC, 回包目标 20 秒 − 借阅之前已用 − 借阅之后的预留), 前面慢了借阅就少等;
到点时那句话叫模型动笔之前用同样的参数调一次 borrow_lessons, 不再是"稍后再调"。
"""

from __future__ import annotations

import pathlib
import re
import threading
import time

import pytest

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


class _DeadPool:
    """起不来的线程池: 构造或 submit 抛 (生产形态 RuntimeError: can't start new thread / 解释器关闭中)。"""

    def __init__(self, *a, fail_at: str = "submit", **k):
        if fail_at == "init":
            raise RuntimeError("can't start new thread")

    def submit(self, *a, **k):
        raise RuntimeError("can't start new thread")

    def shutdown(self, wait=False):
        pass


@pytest.mark.parametrize("fail_at", ["init", "submit"])
def test_a_thread_pool_that_cannot_start_degrades_to_borrow_error(monkeypatch, caplog, fail_at):
    """``_borrow_for_brief_bounded`` 的承诺和 ``_borrow_for_brief`` 一样是**绝不抛** —— 它在
    open_project 的必经路径上, 抛出去整份简报连 P0 硬约束一起丢 (codex review on #93)。
    线程池建不起来 / submit 失败时要按 BORROW_ERROR 回, 简报照交。"""
    fake = _SlowLibrarian(delay=0.0)
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", fake)
    monkeypatch.setattr(core.cf, "ThreadPoolExecutor",
                        lambda *a, **k: _DeadPool(*a, fail_at=fail_at, **k))
    b = core.build_writing_brief(_client(), PID, user_id=ME, brief={"draft_topic": "早八通勤"})
    assert "p0" in b and "stable" in b, "简报本体必须交出去"
    assert b["lessons"] == [] and b["counts"]["lessons"] == 0
    assert b["lessons_status"]["status"] == lib.BORROW_ERROR
    assert "起不来" in b["lessons_status"]["detail"] and "RuntimeError" in b["lessons_status"]["detail"]
    assert fake.briefs == [], "线程都没起来, 馆员不该被调到"
    assert any("could not start" in r.getMessage() for r in caplog.records), "留痕: 日志里 grep 得到"


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


# ══════════════════════════════════════════════════════════════════════
# 2026-10-30 发布收尾: 15 秒, 但整次 open_project 不过 20 秒的回包目标
# ══════════════════════════════════════════════════════════════════════

def test_the_default_is_15s_and_the_arithmetic_leaves_room_for_the_other_steps():
    """账(config.py OPEN_PROJECT_BORROW_SEC 那段): 借阅之前约 1~3 秒 + 借阅 + 借阅之后预留 2.5 秒 ≤ 20 秒。
    默认 15 秒时, 借阅之前的几步有 2.5 秒可用才轮得到"等满 15 秒"; 超了借阅自动少等。"""
    default, _, _ = _code_bounds()
    assert default == 15, "冷借 14~18 秒: 10 秒的预算让简报常常不带卡"
    assert config.OPEN_PROJECT_RESPONSE_BUDGET_SEC <= 20, "回包目标要给 ~22 秒的客户端容忍留余量"
    room = config.OPEN_PROJECT_RESPONSE_BUDGET_SEC - default - core.OPEN_PROJECT_TAIL_RESERVE_SEC
    assert room >= 2, f"借阅之前那几步(约 1~3 秒库往返 + 1 次 embedding)只剩 {room:g}s"
    assert 1.5 <= core.OPEN_PROJECT_TAIL_RESERVE_SEC <= 4, "借阅之后的几步(约 1~2 秒)要有预留, 也别预留得把借阅挤没了"


def test_the_borrow_only_gets_what_is_left_of_the_response_budget(monkeypatch):
    monkeypatch.setattr(config, "OPEN_PROJECT_BORROW_SEC", 15.0)
    monkeypatch.setattr(config, "OPEN_PROJECT_RESPONSE_BUDGET_SEC", 20.0)
    now = time.monotonic()
    assert core._brief_borrow_budget(None) == 15.0, "不是从简报来的就只看 OPEN_PROJECT_BORROW_SEC"
    assert core._brief_borrow_budget(now) == pytest.approx(15.0, abs=0.05), "前面很快: 等满 15 秒"
    left = 20.0 - 5.0 - core.OPEN_PROJECT_TAIL_RESERVE_SEC
    assert core._brief_borrow_budget(now - 5.0) == pytest.approx(left, abs=0.05), "前面用了 5 秒: 借阅少等"
    assert core._brief_borrow_budget(now - 30.0) == 0.0, "前面已经用光: 一秒都不等, 不是负数"


def test_slow_steps_before_the_borrow_shrink_the_wait(monkeypatch):
    """借阅之前的库读慢了(这里 0.4 秒), 借阅只拿回包目标里剩下的: 整次仍在目标之内, 借阅线程照样跑完(TV 写缓存)。"""
    fake = _SlowLibrarian(delay=1.5)
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", fake)
    monkeypatch.setattr(config, "OPEN_PROJECT_BORROW_SEC", 15.0)
    monkeypatch.setattr(config, "OPEN_PROJECT_RESPONSE_BUDGET_SEC", core.OPEN_PROJECT_TAIL_RESERVE_SEC + 0.6)
    real = core.store.shared_memories

    def slow_rules(*a, **k):
        time.sleep(0.4)
        return real(*a, **k)
    monkeypatch.setattr(core.store, "shared_memories", slow_rules)

    t0 = time.monotonic()
    b = core.build_writing_brief(_client(), PID, user_id=ME, brief={"draft_topic": "早八通勤"})
    elapsed = time.monotonic() - t0
    assert elapsed < 0.6 + 0.4, f"open_project 用了 {elapsed:.2f}s —— 借阅没按剩下的回包时间收"
    assert b["lessons_status"]["status"] == lib.BORROW_TIMEOUT and "p0" in b
    assert b["lessons_status"]["elapsed_ms"] <= 300, "借阅等的是剩下的那 0.2 秒左右, 不是 15 秒"
    assert fake.finished.wait(3.0), "借阅线程照样跑完"


def test_the_timeout_note_says_call_borrow_lessons_once_before_drafting_with_the_same_arguments(monkeypatch):
    from tests.test_writer_facing_text import FORBIDDEN
    fake = _SlowLibrarian(delay=0.6)
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", fake)
    monkeypatch.setattr(config, "OPEN_PROJECT_BORROW_SEC", 0.1)
    b = core.build_writing_brief(_client(), PID, user_id=ME, brief={"draft_topic": "早八通勤"})
    note = b["lessons_status"]["detail"]
    assert b["lessons_status"]["status"] == lib.BORROW_TIMEOUT and note == core.BORROW_TIMEOUT_NOTE
    assert "动笔之前调一次 borrow_lessons" in note and "参数和这次 open_project 一样" in note
    for arg in ("tactic", "draft_topic", "key_messages", "target_audience", "tone", "extra_instructions",
                "flywheel_arm"):
        assert arg in note, arg
    assert "只调这一次" in note and "照常写" in note
    assert "稍后" not in note, "不再是「稍后再调」—— 模型多半就不调了"
    assert "TV" not in note and "LLM" not in note
    assert not [f for f in FORBIDDEN if f in note]
    fake.finished.wait(3.0)


def test_tool_docs_say_what_to_do_on_a_borrow_timeout():
    from deskcore import tools as T
    op = T.open_project.__doc__ or ""
    assert "是 timeout 时" in op and "动笔之前调一次 borrow_lessons" in op and "只调这一次" in op
    bl = T.borrow_lessons.__doc__ or ""
    assert "lessons_status.status 是 timeout 时" in bl and "动笔之前" in bl and "只调这一次" in bl


def test_borrow_lessons_with_the_same_arguments_sends_the_same_brief_as_open_project(monkeypatch):
    """"同样的参数"要真能对上那次在后台选好的卡: 馆员的缓存键含 extra_instructions, 所以 borrow_lessons 也得能传它。
    open_project 那次和 borrow_lessons 这次发给馆员的 brief 一模一样(同一个臂)。"""
    from deskcore import tools as T
    from tests.test_deskcore_open_project_borrows import _Librarian
    fake = _Librarian(cards=[_card(1)])
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", fake)
    c = _client()
    monkeypatch.setattr(core, "sb", lambda: c)
    args = {"tactic": "场景种草", "draft_topic": "早八通勤", "key_messages": "省时", "target_audience": "上班族",
            "tone": "轻松", "extra_instructions": "别用问句"}
    b = core.build_writing_brief(c, PID, user_id=ME, brief=args)
    out = T.borrow_lessons(PID, flywheel_arm=b["flywheel_arm"], _user_id=ME, **args)
    assert out["status"] == lib.BORROW_BORROWED
    assert len(fake.briefs) == 2 and fake.briefs[0] == fake.briefs[1]
    assert fake.briefs[1]["extra_instructions"] == "别用问句"


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
