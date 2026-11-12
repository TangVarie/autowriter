"""``borrow_lessons`` 自己也限时, 而且等的是 ``open_project`` 起的那次选卡(2026-11-11, 审计 A3; HANDOFF F-04 剩下的那一半)。

原来: ``open_project`` 等 15 秒先交简报, 叫模型动笔之前调一次 ``borrow_lessons``; 而 ``borrow_lessons`` 直接调借阅体,
等满 ``LIBRARIAN_TIMEOUT_SEC``(60 秒), 客户端约 22 秒就断 —— 冷选卡慢的那几场, 补借等来的是一次被掐断的调用。它也不知道
``open_project`` 那次选卡还在跑, 又向馆员发一次(馆员那头第一次还没写缓存, 第二次就再跑一遍模型选卡)。

这里钉的【被禁止的形态】:
  · ``borrow_lessons`` 等馆员超过 ``BORROW_LESSONS_WAIT_SEC``; 这个数的代码默认值 / 上限超出客户端容忍;
  · 到点那句话叫模型再调 ``borrow_lessons``(简报超时那句不能原样用);
  · 同一个 (项目, brief, 臂) 的选卡还在跑, 又向馆员发第二次;
  · 臂不同 / brief 不同 / 项目不同的并到一起(探索臂那次选卡不带规律);
  · 等的人到点就把选卡取消, 或者把表里那一条摘掉(选完要照样进馆员的缓存, 后来的人还要并得上);
  · 选卡跑完(成了、抛了、起不来)之后表里还留着它;
  · 限时这一层动了分臂和「给出去几条规律」那套账: 重新发一次臂、补借不和前面取大的、到点没等到却记成看到了规律、
    并到别人那次选卡上时改了别人的记录。

全部是合成项目、现编的文字。
"""
from __future__ import annotations

import importlib
import pathlib
import re
import threading
import time
from datetime import datetime, timezone

import pytest

import config
import librarian_client as lib
from deskcore import core, tools
from tests.test_flywheel_arm import ME, PATTERNS, PID, _client, _commit_out, _project
from tests.test_open_project_borrow_budget import _DeadPool

OTHER = "22222222-2222-2222-2222-222222222222"
CARDS = [{"source_note_id": "n1", "hook_type": "反差"}, {"source_note_id": "n2", "hook_type": "提问"}]
TOPIC = {"draft_topic": "周末收纳"}


class _Gated:
    """替身馆员: 记下收到的 brief, 等放行(``gate``)才回卡。``started`` / ``finished`` 让用例知道选卡走到哪了。"""

    def __init__(self, *, open_gate: bool = False):
        self.gate, self.started, self.finished = threading.Event(), threading.Event(), threading.Event()
        self.briefs: list[dict] = []
        if open_gate:
            self.gate.set()

    def __call__(self, brief, *, status=None):
        self.briefs.append(dict(brief))
        self.started.set()
        self.gate.wait(5)
        if status is not None:
            status.update({"state": lib.BORROW_BORROWED, "count": len(CARDS), "elapsed_ms": 900, "detail": "",
                           "patterns": [] if brief.get("explore") else list(PATTERNS)})
        self.finished.set()
        return [dict(c) for c in CARDS]


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """进程内存里的几样记录每条用例从空的开始; 默认都分到 patterns 臂, 简报借卡几乎不等, 补借的等待给足。"""
    monkeypatch.setattr(core, "_borrowed_arms", {})
    monkeypatch.setattr(core, "_patterns_shown", {})
    monkeypatch.setattr(core, "_lessons_shown", {})
    monkeypatch.setattr(core, "_ARMS_SINCE", datetime(2000, 1, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(core, "flywheel_arm", lambda pid, uid, **kw: "patterns")
    monkeypatch.setattr(config, "OPEN_PROJECT_BORROW_SEC", 0.05)
    monkeypatch.setattr(config, "BORROW_LESSONS_WAIT_SEC", 5.0)


def _librarian(monkeypatch, **kw) -> _Gated:
    fake = _Gated(**kw)
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", fake)
    return fake


def _until(cond, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return bool(cond())


def _release_when_joined(monkeypatch, gate: threading.Event) -> list:
    """有人并到正在跑的那次选卡上了, 才放行(``gate``)。不用定时器: 机器慢的时候定时器先到, 选卡先跑完, 后来的人并不上,
    用例就时红时绿。回一个列表, 并上去一次记一条。"""
    real, joined = core._borrow_joinable, []

    def spy(running):
        fut = real(running)
        if fut is not None:
            joined.append(fut)
            gate.set()
        return fut
    monkeypatch.setattr(core, "_borrow_joinable", spy)
    return joined


def _open_and_time_out(c=None, delta=TOPIC) -> dict:
    """开项目: 选卡还在跑, 简报到点先交出去(status=timeout)。"""
    brief = core.build_writing_brief(c or _client(), PID, user_id=ME, brief=dict(delta))
    assert brief["lessons_status"]["status"] == lib.BORROW_TIMEOUT and brief["lessons"] == []
    return brief


# ══════════════════════════════════════════════════════════════════════
# 限时: 到点回一句「照常写, 不用再调」
# ══════════════════════════════════════════════════════════════════════

def test_borrow_lessons_replies_at_its_deadline_and_leaves_the_selection_running(monkeypatch, caplog):
    from tests.test_writer_facing_text import FORBIDDEN
    fake = _librarian(monkeypatch)
    monkeypatch.setattr(config, "BORROW_LESSONS_WAIT_SEC", 0.2)

    t0 = time.monotonic()
    out = core.borrow_lessons(_client(), PID, user_id=ME, **TOPIC)
    elapsed = time.monotonic() - t0

    assert elapsed < 1.5, f"borrow_lessons 等了 {elapsed:.2f}s —— 0.2 秒的上限没生效"
    assert out["status"] == lib.BORROW_TIMEOUT, "回包里的结局还是协议列的那五种之一"
    assert out["lessons"] == [] and out["count"] == 0 and out["validated_patterns"] == []
    assert out["flywheel_arm"] == "patterns", "到点也带着臂"
    assert "validated_patterns_usage" not in out
    assert any("borrow_lessons budget" in r.getMessage() for r in caplog.records), "留痕: 日志里 grep 得到"

    note = out["detail"]
    assert note == core.BORROW_STILL_SELECTING_NOTE and note != core.BORROW_TIMEOUT_NOTE
    assert "照常写" in note and "不用再调" in note and "别跟用户说没有可借的经验" in note
    assert "调一次" not in note and "动笔之前" not in note, "这句是 borrow_lessons 自己回的, 不能再叫模型来调它"
    assert "用户" not in note.replace("别跟用户说", ""), "不让模型去问用户、也不把事情推给用户"
    assert "TV" not in note and "LLM" not in note and not [f for f in FORBIDDEN if f in note]

    assert not fake.finished.is_set() and len(core._borrows_running) == 1, "等的人走了, 选卡还在跑、表里还在"
    fake.gate.set()
    assert fake.finished.wait(3.0), "到点之后选卡应该继续跑完(生产里 = 馆员写缓存), 不是被取消"
    assert _until(lambda: not core._borrows_running), "跑完之后表里不留"
    assert len(fake.briefs) == 1


def test_the_tool_returns_the_timeout_reply_as_a_normal_result(monkeypatch):
    fake = _librarian(monkeypatch)
    monkeypatch.setattr(config, "BORROW_LESSONS_WAIT_SEC", 0.1)
    c = _client()
    monkeypatch.setattr(core, "sb", lambda: c)
    out = tools.borrow_lessons(PID, _user_id=ME, **TOPIC)
    fake.gate.set()
    assert out["status"] == lib.BORROW_TIMEOUT and out["detail"] == core.BORROW_STILL_SELECTING_NOTE
    assert "error" not in out, "到点是正常回包, 不是报错"
    doc = tools.borrow_lessons.__doc__ or ""
    assert "最多等十几秒" in doc and "不用再调" in doc


def _wait_bounds() -> tuple[float, float, float]:
    """从源码里读【代码默认值 / 下限 / 上限】, 不读 env 解析后的值(同 test_open_project_borrow_budget)。"""
    src = pathlib.Path(config.__file__).read_text(encoding="utf-8")
    m = re.search(r'_bounded_number\("BORROW_LESSONS_WAIT_SEC",\s*([0-9.]+),\s*([0-9.]+),\s*([0-9.]+)\)', src)
    assert m, "config.py 里 BORROW_LESSONS_WAIT_SEC 那行的写法变了 —— 同步改这条守卫"
    return float(m.group(1)), float(m.group(2)), float(m.group(3))


def test_the_wait_default_and_ceiling_stay_under_the_client_tolerance():
    """客户端实测容忍约 22 秒; 等待之外还有进门的归属校验(1 次库往返)和回包。"""
    default, lo, hi = _wait_bounds()
    assert default == 18 and lo >= 1 and hi <= 20
    assert default > _open_project_default(), "补借比简报那次多等一会儿才有意义"


def _open_project_default() -> float:
    src = pathlib.Path(config.__file__).read_text(encoding="utf-8")
    return float(re.search(r'_bounded_number\("OPEN_PROJECT_BORROW_SEC",\s*([0-9.]+)', src).group(1))


def test_the_wait_env_override_is_clamped_not_trusted(monkeypatch):
    try:
        monkeypatch.setenv("BORROW_LESSONS_WAIT_SEC", "500")
        assert importlib.reload(config).BORROW_LESSONS_WAIT_SEC <= 20
        monkeypatch.setenv("BORROW_LESSONS_WAIT_SEC", "nan")
        assert importlib.reload(config).BORROW_LESSONS_WAIT_SEC == _wait_bounds()[0]
        monkeypatch.setenv("BORROW_LESSONS_WAIT_SEC", "6")
        assert importlib.reload(config).BORROW_LESSONS_WAIT_SEC == 6.0
    finally:
        monkeypatch.delenv("BORROW_LESSONS_WAIT_SEC", raising=False)
        importlib.reload(config)


# ══════════════════════════════════════════════════════════════════════
# 并到正在跑的那次选卡上
# ══════════════════════════════════════════════════════════════════════

def test_borrow_lessons_joins_the_selection_open_project_started(monkeypatch):
    fake = _librarian(monkeypatch)
    c = _client()
    brief = _open_and_time_out(c)
    assert fake.started.wait(2.0) and len(core._borrows_running) == 1

    joined = _release_when_joined(monkeypatch, fake.gate)          # 补借并上去了, 选卡才回来
    out = core.borrow_lessons(c, PID, user_id=ME, flywheel_arm=brief["flywheel_arm"], **TOPIC)

    assert len(joined) == 1
    assert out["status"] == lib.BORROW_BORROWED and [L["source_note_id"] for L in out["lessons"]] == ["n1", "n2"]
    assert out["validated_patterns"] == PATTERNS and "validated_patterns_usage" in out
    assert out["flywheel_arm"] == brief["flywheel_arm"]
    assert len(fake.briefs) == 1, "open_project 那次选卡还在跑: 补借等它, 不向馆员发第二次"
    assert core._borrows_running == {}, "等的人一拿到结果, 表里就已经没有这一条"
    # 这一场: 简报里 0 条, 补借(同一场, 带着臂)拿到了 —— 记补借的条数, 和原来命中缓存那条路一样
    assert core.batch_arm(PID, ME, from_brief=brief["flywheel_arm"]) == ("patterns", "brief", None, len(PATTERNS))
    assert core._lessons_meta(PID, ME) == {"borrows": 2, "shown": 2, "status": "borrowed", "shown_max": 2,
                                           "statuses": {"timeout": 1, "borrowed": 1}}


def test_two_concurrent_waiters_share_one_selection_and_get_separate_copies(monkeypatch):
    fake = _librarian(monkeypatch)
    c = _client()
    outs: dict[str, dict] = {}

    def borrow(name: str) -> None:
        outs[name] = core.borrow_lessons(c, PID, user_id=ME, **TOPIC)
    joined = _release_when_joined(monkeypatch, fake.gate)          # 第二个并上去了, 选卡才回来
    threads = [threading.Thread(target=borrow, args=(n,)) for n in ("a", "b")]
    threads[0].start()
    assert fake.started.wait(2.0) and _until(lambda: len(core._borrows_running) == 1) and not joined
    threads[1].start()
    for t in threads:
        t.join(5)
    assert set(outs) == {"a", "b"} and len(fake.briefs) == 1 and len(joined) == 1, "两个人等的是同一次选卡"
    a, b = outs["a"], outs["b"]
    assert a["status"] == b["status"] == lib.BORROW_BORROWED and a["lessons"] == b["lessons"] == CARDS
    assert a is not b and a["lessons"] is not b["lessons"] and a["lessons"][0] is not b["lessons"][0], "各拿各的拷贝"
    a["lessons"][0]["hook_type"] = "改过"
    a["validated_patterns"].append({"id": "x"})
    assert b["lessons"][0]["hook_type"] == "反差" and b["validated_patterns"] == PATTERNS
    assert core._borrows_running == {}


def test_a_different_arm_brief_or_project_does_not_join(monkeypatch):
    """探索臂那次选卡不带规律, 规律臂的会话并上去就拿不到规律(反过来就把规律给了对照组); 换了选题、换了项目的更不是同一次。"""
    fake = _librarian(monkeypatch)
    other_project = dict(_project(), id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    core._borrow_for_brief_bounded(_project(), TOPIC, 0.05, user_id=ME, arm="patterns")
    assert fake.started.wait(2.0) and len(fake.briefs) == 1
    for project, delta, arm in ((_project(), TOPIC, "explore"),
                                (_project(), {"draft_topic": "换个选题"}, "patterns"),
                                (other_project, TOPIC, "patterns")):
        n = len(fake.briefs)
        out = core._borrow_for_brief_bounded(project, delta, 0.05, user_id=ME, arm=arm)
        assert out["status"] == lib.BORROW_TIMEOUT and out["flywheel_arm"] == arm
        assert _until(lambda: len(fake.briefs) == n + 1), (arm, delta, "不是同一次选卡: 各发各的")
    assert [b["explore"] for b in fake.briefs] == [False, True, False, False]
    assert len(core._borrows_running) == 4
    assert len({k[1] for k in core._borrows_running}) == 4, "臂在发给馆员的 brief 里(explore), 摘要也跟着不同"
    n = len(fake.briefs)
    core._borrow_for_brief_bounded(_project(), dict(TOPIC, junk="不在契约里的键"), 0.05, user_id=OTHER, arm="patterns")
    assert len(fake.briefs) == n, "同一个项目、同一份 brief、同一个臂: 换了身份、多了没用的键也是同一次选卡"
    fake.gate.set()
    assert _until(lambda: not core._borrows_running)


def test_a_waiter_that_leaves_does_not_cancel_and_a_later_caller_still_joins(monkeypatch):
    """选卡比等的人慢: 第一个人到点走了, 选卡照样跑; 这期间再来的人并上去, 选完拿得到。全程只向馆员发了一次。"""
    fake = _librarian(monkeypatch)
    c = _client()
    monkeypatch.setattr(config, "BORROW_LESSONS_WAIT_SEC", 0.05)
    first = core.borrow_lessons(c, PID, user_id=ME, **TOPIC)
    assert first["status"] == lib.BORROW_TIMEOUT and len(core._borrows_running) == 1

    monkeypatch.setattr(config, "BORROW_LESSONS_WAIT_SEC", 5.0)
    _release_when_joined(monkeypatch, fake.gate)
    second = core.borrow_lessons(c, PID, user_id=ME, flywheel_arm=first["flywheel_arm"], **TOPIC)
    assert second["status"] == lib.BORROW_BORROWED and second["count"] == 2
    assert len(fake.briefs) == 1 and core._borrows_running == {}


def test_a_selection_that_finishes_after_the_waiter_left_cleans_up_and_changes_no_record(monkeypatch):
    fake = _librarian(monkeypatch)
    c = _client()
    brief = _open_and_time_out(c)
    monkeypatch.setattr(config, "BORROW_LESSONS_WAIT_SEC", 0.05)
    out = core.borrow_lessons(c, PID, user_id=ME, flywheel_arm=brief["flywheel_arm"], **TOPIC)
    assert out["status"] == lib.BORROW_TIMEOUT and len(fake.briefs) == 1
    before = (core.batch_arm(PID, ME, from_brief="patterns"), core._lessons_meta(PID, ME))
    assert before[0] == ("patterns", "brief", None, 0), "简报和补借都没等到: 这一场一条规律都没看到"

    fake.gate.set()
    assert fake.finished.wait(3.0) and _until(lambda: not core._borrows_running), "选完了, 表里不留"
    assert (core.batch_arm(PID, ME, from_brief="patterns"), core._lessons_meta(PID, ME)) == before, \
        "后台之后选完的不算: 没有人拿到, 臂、规律条数、卡数都不改"

    monkeypatch.setattr(config, "BORROW_LESSONS_WAIT_SEC", 5.0)
    again = core.borrow_lessons(c, PID, user_id=ME, flywheel_arm=brief["flywheel_arm"], **TOPIC)
    assert again["status"] == lib.BORROW_BORROWED and len(fake.briefs) == 2, \
        "表里已经没有了: 再来的照常向馆员借(生产里这一次命中馆员的缓存)"
    assert core._borrows_running == {}


def test_a_selection_that_raises_wakes_every_waiter_with_an_error_and_cleans_up(monkeypatch):
    """借阅体承诺不抛; 真抛了(这里硬换成会抛的), 等它的人各自按借阅出错回, 不往外抛、不等到点, 表里不留。"""
    gate, started = threading.Event(), threading.Event()
    calls: list[str] = []

    def boom(project, delta, *, user_id=None, arm=None):
        calls.append(arm)
        started.set()
        gate.wait(5)
        raise RuntimeError("selection exploded")
    monkeypatch.setattr(core, "_borrow_for_brief", boom)
    c = _client()
    brief = _open_and_time_out(c)
    assert started.wait(2.0) and len(core._borrows_running) == 1

    with monkeypatch.context() as m:
        _release_when_joined(m, gate)
        t0 = time.monotonic()
        out = core.borrow_lessons(c, PID, user_id=ME, flywheel_arm=brief["flywheel_arm"], **TOPIC)
    assert time.monotonic() - t0 < 3.0, "选卡抛了就该醒, 不是等满 5 秒"
    assert out["status"] == lib.BORROW_ERROR and out["lessons"] == [] and "RuntimeError" in out["detail"]
    assert out["flywheel_arm"] == brief["flywheel_arm"]
    assert calls == ["patterns"] and core._borrows_running == {}, "抛了也摘掉"
    assert core._lessons_meta(PID, ME)["statuses"] == {"timeout": 1, "error": 1}

    gate.clear()
    started.clear()
    threading.Timer(0.1, gate.set).start()
    brief2 = core.build_writing_brief(c, PID, user_id=ME, brief=dict(TOPIC))     # 起这次选卡的人自己也一样: 简报照交
    assert "p0" in brief2 and brief2["lessons_status"]["status"] in (lib.BORROW_TIMEOUT, lib.BORROW_ERROR)
    assert _until(lambda: not core._borrows_running) and calls == ["patterns", "patterns"]


@pytest.mark.parametrize("fail_at", ["init", "submit"])
def test_a_selection_that_cannot_start_is_not_registered(monkeypatch, fail_at):
    fake = _librarian(monkeypatch, open_gate=True)
    with monkeypatch.context() as m:
        m.setattr(core.cf, "ThreadPoolExecutor", lambda *a, **k: _DeadPool(*a, fail_at=fail_at, **k))
        out = core.borrow_lessons(_client(), PID, user_id=ME, **TOPIC)
    assert out["status"] == lib.BORROW_ERROR and "起不来" in out["detail"] and fake.briefs == []
    assert core._borrows_running == {}, "没起来的那次不登记 —— 不然后面的人都并到一个永远不回的 future 上"
    assert core.borrow_lessons(_client(), PID, user_id=ME, **TOPIC)["status"] == lib.BORROW_BORROWED


def test_a_brief_that_cannot_be_built_is_not_merged_and_still_fails_open(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("brief exploded")
    monkeypatch.setattr(lib, "build_brief", boom)
    assert core._borrow_running_key(_project(), TOPIC, "patterns") is None
    out = core.borrow_lessons(_client(), PID, user_id=ME, **TOPIC)
    assert out["status"] == lib.BORROW_ERROR and "exploded" in out["detail"] and core._borrows_running == {}


def test_a_finished_selection_only_removes_its_own_entry():
    """上一次选卡的线程晚到的收尾, 不能把同一个键后来登记的那一次摘掉(不然第三个人又向馆员发一次)。"""
    key = core._borrow_running_key(_project(), TOPIC, "patterns")
    assert key == (PID, key[1], "patterns") and re.fullmatch(r"[0-9a-f]{64}", key[1])
    later = (core.cf.Future(), time.monotonic())
    core._borrows_running[key] = later
    mine = core.cf.Future()
    core._run_borrow(mine, key, _project(), TOPIC, ME, "patterns")
    assert mine.done() and core._borrows_running == {key: later}
    core._run_borrow(later[0], key, _project(), TOPIC, ME, "patterns")
    assert core._borrows_running == {}


def test_a_selection_stuck_past_the_librarian_timeout_is_not_joined(monkeypatch):
    """馆员那次请求最多 LIBRARIAN_TIMEOUT_SEC。过了这个数还多十秒都没回来的那一条是卡死的线程: 再往上并, 这个键之后每次
    借卡都只能等到点。不并, 另起一次, 表里换成新的; 卡死的那条哪天回来了, 也不动新的。"""
    fake = _librarian(monkeypatch, open_gate=True)
    monkeypatch.setattr(config, "LIBRARIAN_TIMEOUT_SEC", 60.0)
    key = core._borrow_running_key(_project(), TOPIC, "patterns")
    stuck = core.cf.Future()
    core._borrows_running[key] = (stuck, time.monotonic() - 60.0 - core._BORROW_JOIN_GRACE_SEC - 1.0)
    out = core.borrow_lessons(_client(), PID, user_id=ME, **TOPIC)
    assert out["status"] == lib.BORROW_BORROWED and len(fake.briefs) == 1, "没并到卡死的那条上, 自己借到了"
    assert core._borrows_running == {}

    fake.gate.clear()                                    # 还在时限里的照样并: 只差一秒的那条
    core._borrows_running[key] = (stuck, time.monotonic() - 60.0 - core._BORROW_JOIN_GRACE_SEC + 1.0)
    monkeypatch.setattr(config, "BORROW_LESSONS_WAIT_SEC", 0.05)
    assert core.borrow_lessons(_client(), PID, user_id=ME, **TOPIC)["status"] == lib.BORROW_TIMEOUT
    assert len(fake.briefs) == 1 and core._borrows_running[key][0] is stuck


# ══════════════════════════════════════════════════════════════════════
# 分臂和规律条数那套账: 限时这一层只多了一种「到点没等到 = 0」, 别的不动
# ══════════════════════════════════════════════════════════════════════

def test_an_explicit_arm_is_used_as_is_and_no_new_arm_is_issued(monkeypatch):
    """borrow_lessons 自己定好了臂(沿用模型传回的那个)再进来: 这一层不能再发一次 —— 再发就是按当天重新分, 传回的臂白传。"""
    fake = _librarian(monkeypatch, open_gate=True)
    monkeypatch.setattr(core, "_borrow_arm", lambda *a, **k: pytest.fail("这一层不该再定一次臂"))
    out = core._borrow_for_brief_bounded(_project(), TOPIC, 2.0, user_id=ME, arm="explore", carry=True)
    assert out["flywheel_arm"] == "explore" and fake.briefs[0]["explore"] is True
    assert out["validated_patterns"] == [] and core._borrowed_arms == {}


def test_a_timed_out_follow_up_keeps_what_the_brief_showed(monkeypatch):
    """简报给过 1 条, 同一场换了选题补借(带着臂), 这次到点没等到: 模型手里还是那 1 条, 记 1(同降级那条路)。
    不带臂的(新开的一场)这次 0 条, 和前面那场的 1 条对不上 —— 分不出入库的是哪一场, 记 None。"""
    fake = _librarian(monkeypatch, open_gate=True)
    monkeypatch.setattr(config, "OPEN_PROJECT_BORROW_SEC", 2.0)
    c = _client()
    brief = core.build_writing_brief(c, PID, user_id=ME, brief=dict(TOPIC))
    assert brief["validated_patterns"] == PATTERNS

    fake.gate.clear()
    monkeypatch.setattr(config, "BORROW_LESSONS_WAIT_SEC", 0.05)
    out = core.borrow_lessons(c, PID, user_id=ME, flywheel_arm=brief["flywheel_arm"], draft_topic="换个选题")
    assert out["status"] == lib.BORROW_TIMEOUT and out["validated_patterns"] == []
    assert core.batch_arm(PID, ME, from_brief=brief["flywheel_arm"]) == ("patterns", "brief", None, 1)

    out = core.borrow_lessons(c, PID, user_id=ME, draft_topic="再换一个")
    assert out["status"] == lib.BORROW_TIMEOUT
    assert core.batch_arm(PID, ME, from_brief=brief["flywheel_arm"])[3] is None
    fake.gate.set()


def test_joining_someone_elses_selection_leaves_their_record_alone(monkeypatch):
    """另一个身份并到这次选卡上、拿到了规律: 记在他自己的键下。起这次选卡的那个身份到点走了, 记的还是 0 —— 不因为
    别人后来拿到了, 就把没看到规律的那一场记成看到了。"""
    fake = _librarian(monkeypatch)
    mine = core._borrow_for_brief_bounded(_project(), TOPIC, 0.05, user_id=ME)
    assert mine["status"] == lib.BORROW_TIMEOUT and fake.started.wait(2.0)
    issued = dict(core._borrowed_arms[(PID, ME)])

    _release_when_joined(monkeypatch, fake.gate)
    theirs = core._borrow_for_brief_bounded(_project(), TOPIC, 5.0, user_id=OTHER)
    assert theirs["status"] == lib.BORROW_BORROWED and theirs["validated_patterns"] == PATTERNS
    assert len(fake.briefs) == 1

    assert core._borrowed_arms[(PID, ME)] == issued, "别人并上来, 不动这个身份发臂的时刻"
    assert core.batch_arm(PID, ME, from_brief="patterns") == ("patterns", "brief", None, 0)
    assert core.batch_arm(PID, OTHER, from_brief="patterns") == ("patterns", "brief", None, len(PATTERNS))
    assert core._lessons_meta(PID, ME)["shown_max"] == 0 and core._lessons_meta(PID, OTHER)["shown"] == 2


# ══════════════════════════════════════════════════════════════════════
# 存档那一行指标: 补借也没等到的场数得出来
# ══════════════════════════════════════════════════════════════════════

def test_the_commit_row_shows_a_follow_up_that_also_timed_out(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    fake = _librarian(monkeypatch)
    c = _client()
    brief = _open_and_time_out(c)
    monkeypatch.setattr(config, "BORROW_LESSONS_WAIT_SEC", 0.05)
    out = core.borrow_lessons(c, PID, user_id=ME, flywheel_arm=brief["flywheel_arm"], **TOPIC)
    assert out["status"] == lib.BORROW_TIMEOUT, "回包里还是 timeout"
    fake.gate.set()
    assert fake.finished.wait(3.0)

    _out, params, meta = _commit_out(monkeypatch, flywheel_arm=brief["flywheel_arm"])
    assert meta["flywheel_lessons"] == {"borrows": 2, "shown": 0, "status": core.LESSONS_FOLLOWUP_TIMEOUT,
                                        "shown_max": 0, "statuses": {"timeout": 1, "followup_timeout": 1}}
    assert params["flywheel_patterns_shown"] == 0 and meta["flywheel_patterns_shown"] == 0
    assert params["flywheel_arm"] == "patterns" and params["flywheel_arm_source"] == "brief"
