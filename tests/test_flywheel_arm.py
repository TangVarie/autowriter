"""飞轮分臂与已验证规律透传 (TV docs/28 §7.4, D-116)。

写作台 15% 的批次随机不注入闸二已验证规律, 作对照组。随机在【批次】级: 按 (项目, 写手, 北京日期) 哈希定臂,
借卡时定下, 入库时把借卡那次的臂记进 batches.params.flywheel_arm (没借过卡 / 进程重启过 → 重算, 标 recomputed)。

这里钉的【被禁止的形态】:
  · 借卡不带 explore —— TV 那边会当老消费方, 一条规律都不给, 规律臂也就没有了;
  · 借卡和入库记的臂不一样 (包括跨了北京 0 点: 入库要记借卡那次的臂, codex review on #104) —— 对照组圈不准;
  · 入库的 batch 没记臂;
  · 馆员回的 patterns 被丢掉, 或者探索臂也把规律给了写手。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import config
import librarian_client as lib
from deskcore import core, store
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
PID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PATTERNS = [{"id": "ending_asks_reader", "label": "结尾问读者", "instruction": "结尾留一个读者能直接回答的问题。",
             "evidence": "OR 2.11"}]


def test_arm_is_deterministic_and_about_15_percent():
    now = datetime(2026, 10, 16, 4, tzinfo=timezone.utc)
    assert core.flywheel_arm(PID, ME, now=now) == core.flywheel_arm(PID, ME, now=now)
    arms = [core.flywheel_arm(f"p{i}", f"u{i % 7}", now=now + timedelta(days=i % 30)) for i in range(4000)]
    share = arms.count("explore") / len(arms)
    assert 0.12 < share < 0.18, share
    assert set(arms) == {"explore", "patterns"}


def test_arm_follows_the_beijing_date():
    """北京 0 点 = UTC 16 点: 同一个北京日里的借卡和入库必须同臂, 跨了北京日才可能换。"""
    morning = datetime(2026, 10, 16, 1, tzinfo=timezone.utc)       # 北京 09:00
    evening = datetime(2026, 10, 16, 15, 59, tzinfo=timezone.utc)  # 北京 23:59 同一天
    assert core.flywheel_arm(PID, ME, now=morning) == core.flywheel_arm(PID, ME, now=evening)
    days = {core.flywheel_arm(PID, ME, now=morning + timedelta(days=d)) for d in range(60)}
    assert days == {"explore", "patterns"}, "60 天里一个写手两个臂都该轮到"


class _Librarian:
    def __init__(self, patterns):
        self.patterns = patterns
        self.briefs: list[dict] = []

    def __call__(self, brief, *, status=None):
        self.briefs.append(dict(brief))
        if status is not None:
            status.update({"state": lib.BORROW_BORROWED, "count": 1, "elapsed_ms": 5, "detail": "",
                           "patterns": [] if brief.get("explore") else list(self.patterns)})
        return [{"source_note_id": "n1", "validated_hits": [] if brief.get("explore") else ["结尾问读者"]}]


def _project():
    return {"id": PID, "name": "测试项目", "brand": "测试品", "owner_id": ME, "system_prompt": "你是文案",
            "calibration_notes": "", "tactics": "[]", "default_params": "{}"}


def _client():
    return FakeClient(rows={"projects": [_project()], "memories": [], "user_calibration_notes": [],
                            "items": [], "versions": [], "batches": [], "draft_fingerprints": [],
                            "angle_ledger": [], "style_edits": []})


@pytest.mark.parametrize("arm", ["patterns", "explore"])
def test_borrow_sends_the_arm_and_passes_patterns_through(monkeypatch, arm):
    fake = _Librarian(PATTERNS)
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", fake)
    monkeypatch.setattr(core, "flywheel_arm", lambda pid, uid, **kw: arm)

    out = core.borrow_lessons(_client(), PID, user_id=ME, tactic="t")
    assert fake.briefs[-1]["explore"] is (arm == "explore"), "explore 必须显式带 (TV 不带这个键就当老消费方)"
    assert out["validated_patterns"] == ([] if arm == "explore" else PATTERNS)

    brief = core.build_writing_brief(_client(), PID, user_id=ME, brief={"tactic": "t"})
    assert fake.briefs[-1]["explore"] is (arm == "explore")
    if arm == "explore":
        assert "validated_patterns" not in brief, "探索臂不许把规律给写手"
    else:
        assert brief["validated_patterns"] == PATTERNS
        assert brief["lessons"][0]["validated_hits"] == ["结尾问读者"]


def _commit(monkeypatch):
    captured = {}
    real = store.mint_draft_identity

    def spy(sb, project_id, user_id, tactic, entries, *, batch_params=None):
        captured["params"] = batch_params
        return real(sb, project_id, user_id, tactic, entries, batch_params=batch_params)
    monkeypatch.setattr(store, "mint_draft_identity", spy)
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)

    c = FakeClient(rows={"projects": [{"id": PID, "name": "项目", "brand": "A", "owner_id": ME,
                                       "calibration_notes": "", "tactics": "[]", "custom_roles": []}]})
    c.rpc_impl["deskcore_commit_fingerprints"] = lambda args: [
        {"idx": i, "status": core.COMMIT_STATUS_INSERTED, "collided_with": None, "detail": None}
        for i in range(len(args.get("_rows") or []))]
    out = core.commit_drafts(c, PID, [{"title": "标题一", "body": "正文一二三四五六七八", "keywords": ["a"]}], user_id=ME)
    assert out["batch_id"]
    assert c.rows["batches"][0]["params"] == captured["params"]
    return captured["params"]


@pytest.fixture
def fresh_arms(monkeypatch):
    monkeypatch.setattr(core, "_borrowed_arms", {})


def test_borrow_and_commit_use_the_same_arm(monkeypatch, fresh_arms):
    seen = {}
    monkeypatch.setattr(core, "flywheel_arm", lambda pid, uid, **kw: seen.setdefault((pid, uid), "explore"))
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", _Librarian(PATTERNS))
    core.borrow_lessons(_client(), PID, user_id=ME)

    assert _commit(monkeypatch) == {"source": "deskcore", "flywheel_arm": "explore", "flywheel_arm_source": "borrow"}
    assert list(seen) == [(PID, ME)], "借卡和入库按同一个 (项目, 写手) 算臂"


def _days_with_different_arms():
    """找一对相邻北京日, 两天这个写手在这个项目的臂不一样 (15% 的臂, 60 天里一定有)。"""
    base = datetime(2026, 10, 16, 15, 59, tzinfo=timezone.utc)        # 北京 23:59
    for d in range(60):
        before = base + timedelta(days=d)
        after = before + timedelta(minutes=2)                           # 北京次日 00:01
        if core.flywheel_arm(PID, ME, now=before) != core.flywheel_arm(PID, ME, now=after):
            return before, after
    raise AssertionError("60 天里臂一直没变")


def test_commit_after_beijing_midnight_keeps_the_borrow_arm(monkeypatch, fresh_arms):
    """借卡在北京 23:59、入库在 00:01, 两天的哈希不同: batch 必须记借卡那次的臂 (codex review on #104)。"""
    before, after = _days_with_different_arms()
    borrowed = core.flywheel_arm(PID, ME, now=before)
    fake = _Librarian(PATTERNS)
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", fake)

    monkeypatch.setattr(core, "_utcnow", lambda: before)
    core.borrow_lessons(_client(), PID, user_id=ME)
    assert fake.briefs[-1]["explore"] is (borrowed == "explore")

    monkeypatch.setattr(core, "_utcnow", lambda: after)
    params = _commit(monkeypatch)
    assert params["flywheel_arm"] == borrowed != core.flywheel_arm(PID, ME, now=after)
    assert params["flywheel_arm_source"] == "borrow"


def test_commit_without_a_recent_borrow_recomputes_and_says_so(monkeypatch, fresh_arms):
    now = datetime(2026, 10, 17, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(core, "_utcnow", lambda: now)
    params = _commit(monkeypatch)
    assert params == {"source": "deskcore", "flywheel_arm": core.flywheel_arm(PID, ME, now=now),
                      "flywheel_arm_source": "recomputed"}, "没借过卡 (或进程重启过) 要标出来, 分析时能剔掉"

    monkeypatch.setattr(core, "_utcnow", lambda: now - timedelta(hours=40))
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", _Librarian(PATTERNS))
    core.borrow_lessons(_client(), PID, user_id=ME)
    monkeypatch.setattr(core, "_utcnow", lambda: now)
    assert _commit(monkeypatch)["flywheel_arm_source"] == "recomputed", "36 小时前的借卡不算这一批的"


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.mark.parametrize("payload, want", [
    ({"selected": [], "status": "no_match", "patterns": PATTERNS}, PATTERNS),
    ({"selected": [], "status": "ok"}, []),                                        # 老馆员: 没有这个键
    ({"selected": [], "patterns": "oops"}, []),
    ({"selected": [], "patterns": [PATTERNS[0], "junk", 3]}, PATTERNS),
])
def test_client_reads_patterns_from_the_librarian(monkeypatch, payload, want):
    import requests
    monkeypatch.setattr(config, "LIBRARIAN_URL", "https://example.invalid")
    monkeypatch.setattr(config, "LIBRARIAN_API_KEY", "k")
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(payload))
    st: dict = {}
    lib.fetch_flywheel_lessons({"project_id": "p", "consumer": "deskcore", "explore": False}, status=st)
    assert st["patterns"] == want


def test_tool_docs_tell_the_model_what_validated_patterns_are():
    from deskcore import tools
    for fn in (tools.open_project, tools.borrow_lessons):
        doc = fn.__doc__ or ""
        assert "validated_patterns" in doc and "不是硬约束" in doc and "不要自己编" in doc, fn.__name__


# ── 臂随简报走、入库原样传回 (codex review on #104 第二轮) ─────────────────────────────

def test_the_brief_carries_the_arm_even_when_the_borrow_times_out(monkeypatch, fresh_arms):
    monkeypatch.setattr(core, "flywheel_arm", lambda pid, uid, **kw: "explore")
    monkeypatch.setattr(config, "OPEN_PROJECT_BORROW_SEC", 0.01)
    import time as _t

    def slow(brief, *, status=None):
        _t.sleep(0.2)
        return []
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", slow)
    brief = core.build_writing_brief(_client(), PID, user_id=ME, brief={})
    assert brief["lessons_status"]["status"] == lib.BORROW_TIMEOUT
    assert brief["flywheel_arm"] == "explore", "借阅超时简报也要带臂, 入库时才传得回来"


def _commit_with(monkeypatch, **kw):
    captured = {}
    real = store.mint_draft_identity

    def spy(sb, project_id, user_id, tactic, entries, *, batch_params=None):
        captured["params"] = batch_params
        return real(sb, project_id, user_id, tactic, entries, batch_params=batch_params)
    monkeypatch.setattr(store, "mint_draft_identity", spy)
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    c = FakeClient(rows={"projects": [{"id": PID, "name": "项目", "brand": "A", "owner_id": ME,
                                       "calibration_notes": "", "tactics": "[]", "custom_roles": []}]})
    c.rpc_impl["deskcore_commit_fingerprints"] = lambda args: [
        {"idx": i, "status": core.COMMIT_STATUS_INSERTED, "collided_with": None, "detail": None}
        for i in range(len(args.get("_rows") or []))]
    core.commit_drafts(c, PID, [{"title": "标题一", "body": "正文一二三四五六七八", "keywords": ["a"]}],
                       user_id=ME, **kw)
    return captured["params"]


def test_overlapping_sessions_each_record_their_own_arm(monkeypatch, fresh_arms):
    """同一写手同一项目开着两场: A 在北京 23:59 借卡, B 在 00:01 借卡 (臂不同, 内存里只剩 B 的)。
    A 入库时传回自己简报里的臂 → 记 A 的; 不传才退到「最近一次借卡」。"""
    before, after = _days_with_different_arms()
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", _Librarian(PATTERNS))
    monkeypatch.setattr(core, "_utcnow", lambda: before)
    arm_a = core.borrow_lessons(_client(), PID, user_id=ME)["flywheel_arm"]
    monkeypatch.setattr(core, "_utcnow", lambda: after)
    arm_b = core.borrow_lessons(_client(), PID, user_id=ME)["flywheel_arm"]
    assert arm_a != arm_b

    p = _commit_with(monkeypatch, flywheel_arm=arm_a)
    assert (p["flywheel_arm"], p["flywheel_arm_source"]) == (arm_a, "brief")
    p = _commit_with(monkeypatch)
    assert (p["flywheel_arm"], p["flywheel_arm_source"]) == (arm_b, "borrow")
    p = _commit_with(monkeypatch, flywheel_arm="随便写的")
    assert (p["flywheel_arm"], p["flywheel_arm_source"]) == (arm_b, "borrow"), "认不出的值不当真"


def test_the_commit_tool_passes_the_arm_through(monkeypatch):
    from deskcore import tools
    seen = {}
    monkeypatch.setattr(core, "sb", lambda: object())
    monkeypatch.setattr(core, "commit_drafts", lambda c, pid, drafts, **kw: seen.update(kw) or {})
    for tool in (tools.score_drafts, tools.commit_drafts):      # commit_drafts: D-120 过渡期的旧名
        seen.clear()
        tool(PID, [], flywheel_arm="explore", _user_id=ME)
        assert seen["flywheel_arm"] == "explore"
        tool(PID, [], _user_id=ME)
        assert seen["flywheel_arm"] is None
    for fn in (tools.open_project, tools.borrow_lessons, tools.score_drafts):
        assert "flywheel_arm" in (fn.__doc__ or "") and "原样" in (fn.__doc__ or ""), fn.__name__


# ── 改稿挂在旧 batch 上: 跨臂就标 mixed ─────────────────────────────────────────────

@pytest.mark.parametrize("first_arm, revise_arm, mixed", [
    ("patterns", "explore", True),
    ("patterns", "patterns", False),
])
def test_a_revision_under_the_other_arm_marks_the_batch_mixed(monkeypatch, fresh_arms, first_arm, revise_arm, mixed):
    from tests import test_deskcore_draft_identity as T
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    c = T._client()
    c.rpc_impl["deskcore_commit_fingerprints"] = T._rpc_that_writes(c)
    first = core.commit_drafts(c, T.PROJ, [{"title": "秋招等回音", "body": T.BODY_X}], user_id=T.ME,
                               flywheel_arm=first_arm)
    batch = c.rows["batches"][0]
    assert batch["params"]["flywheel_arm"] == first_arm

    T._embed(c)
    out = core.commit_drafts(c, T.PROJ, [{"title": "秋招等回音", "body": T.BODY_X2,
                                          "replaces_version_id": first["version_ids"][0]}],
                             user_id=T.ME, flywheel_arm=revise_arm)
    assert out["replaced"] and out["batch_id"] is None, "只有替换稿: 不建新 batch"
    assert bool(batch["params"].get("flywheel_arm_mixed")) is mixed
    assert batch["params"]["flywheel_arm"] == first_arm and batch["params"]["source"] == "deskcore", "原有字段不动"


def test_a_revision_of_a_pre_experiment_batch_is_left_alone(monkeypatch, fresh_arms):
    from tests import test_deskcore_draft_identity as T
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    c = T._client()
    c.rpc_impl["deskcore_commit_fingerprints"] = T._rpc_that_writes(c)
    first = core.commit_drafts(c, T.PROJ, [{"title": "秋招等回音", "body": T.BODY_X}], user_id=T.ME)
    batch = c.rows["batches"][0]
    batch["params"] = {"source": "deskcore"}                     # 分臂之前建的 batch
    T._embed(c)
    core.commit_drafts(c, T.PROJ, [{"title": "秋招等回音", "body": T.BODY_X2,
                                    "replaces_version_id": first["version_ids"][0]}],
                       user_id=T.ME, flywheel_arm="explore")
    assert batch["params"] == {"source": "deskcore"}, "不在实验里的 batch 不碰"


def _commit_meta(c) -> dict:
    rows = [r for r in c.rows.get("batch_metrics", []) if (r.get("meta") or {}).get("mode") == "deskcore_commit"]
    return rows[-1]["meta"]


def test_marking_mixed_never_breaks_the_commit_but_leaves_a_machine_readable_trace(monkeypatch, fresh_arms):
    """标 mixed 失败不拖垮入库, 但不能只记日志 (codex review on #104): 返回值和 batch_metrics.meta 里都要有,
    分析对照组时据此剔掉那几批。"""
    from tests import test_deskcore_draft_identity as T
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    c = T._client()
    c.rpc_impl["deskcore_commit_fingerprints"] = T._rpc_that_writes(c)
    first = core.commit_drafts(c, T.PROJ, [{"title": "秋招等回音", "body": T.BODY_X}], user_id=T.ME,
                               flywheel_arm="patterns")
    meta = _commit_meta(c)
    assert (meta["flywheel_arm"], meta["flywheel_arm_source"]) == ("patterns", "brief")
    assert meta["revised_item_ids"] == [] and meta["arm_mixed_unmarked_item_ids"] == []

    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("db down")
    monkeypatch.setattr(store, "batch_params_for_items", boom)
    T._embed(c)
    out = core.commit_drafts(c, T.PROJ, [{"title": "秋招等回音", "body": T.BODY_X2,
                                          "replaces_version_id": first["version_ids"][0]}],
                             user_id=T.ME, flywheel_arm="explore")
    item = out["replaced"][0]["item_id"]
    assert out["written"] == 1 and len(calls) == 2, "入库照常; 标 mixed 重试一次"
    assert out["flywheel_arm_warning"] == {"code": "arm_mixed_unmarked", "arm": "explore", "item_ids": [item]}
    meta = _commit_meta(c)
    assert meta["revised_item_ids"] == [item] and meta["arm_mixed_unmarked_item_ids"] == [item]
    assert meta["flywheel_arm"] == "explore"


def test_a_follow_up_borrow_after_midnight_keeps_the_session_arm(monkeypatch, fresh_arms):
    """open_project 在北京 23:59 分了组, 同一场 00:01 再调 borrow_lessons 并传回那个臂: 沿用, 不重新分
    (codex review on #104 第三轮)。不传才会按当天重新分。"""
    before, after = _days_with_different_arms()
    fake = _Librarian(PATTERNS)
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", fake)
    monkeypatch.setattr(core, "_utcnow", lambda: before)
    arm = core.build_writing_brief(_client(), PID, user_id=ME, brief={})["flywheel_arm"]

    monkeypatch.setattr(core, "_utcnow", lambda: after)
    out = core.borrow_lessons(_client(), PID, user_id=ME, flywheel_arm=arm)
    assert out["flywheel_arm"] == arm and fake.briefs[-1]["explore"] is (arm == "explore")
    assert core.batch_arm(PID, ME) == (arm, "borrow"), "沿用的臂也记进「最近一次借卡」"
    assert core.borrow_lessons(_client(), PID, user_id=ME)["flywheel_arm"] != arm, "不传就按当天重新分"


def test_the_borrow_tool_passes_the_arm_through(monkeypatch):
    from deskcore import tools
    seen = {}
    monkeypatch.setattr(core, "sb", lambda: object())
    monkeypatch.setattr(core, "borrow_lessons", lambda c, pid, **kw: seen.update(kw) or {})
    tools.borrow_lessons(PID, flywheel_arm="patterns", _user_id=ME)
    assert seen["flywheel_arm"] == "patterns"


def test_tool_docs_explain_external_reference_cards():
    """TV D-117: 写作台的借卡会混进最多 2 张外部参考卡 (external=true)。工具说明要讲清它不是公司投的、只借结构、
    不提它的品牌 —— 不然模型会按「公司自己验证过的爆款」去用它。只改工具说明, 协议不动 (不用重导 skill)。"""
    from deskcore import tools
    for fn in (tools.open_project, tools.borrow_lessons):
        doc = fn.__doc__ or ""
        assert "external=true" in doc and "不是公司投的" in doc and "品牌" in doc, fn.__name__
