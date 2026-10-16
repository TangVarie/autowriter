"""飞轮分臂与已验证规律透传 (TV docs/28 §7.4, D-116)。

写作台 15% 的批次随机不注入闸二已验证规律, 作对照组。随机在【批次】级: 借卡发生在入库之前, 中间没地方存状态,
所以按 (项目, 写手, 北京日期) 哈希 —— 同一天同一写手同一项目的借卡和入库落进同一个臂, 入库时记进
batches.params.flywheel_arm。

这里钉的【被禁止的形态】:
  · 借卡不带 explore —— TV 那边会当老消费方, 一条规律都不给, 规律臂也就没有了;
  · 借卡和入库算出来的臂不一样 —— 对照组圈不准;
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


def test_borrow_and_commit_use_the_same_arm(monkeypatch):
    seen = {}
    monkeypatch.setattr(core, "flywheel_arm", lambda pid, uid, **kw: seen.setdefault((pid, uid), "explore"))
    fake = _Librarian(PATTERNS)
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", fake)
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    core.borrow_lessons(_client(), PID, user_id=ME)

    captured = {}
    real = store.mint_draft_identity

    def spy(sb, project_id, user_id, tactic, entries, *, batch_params=None):
        captured["params"] = batch_params
        return real(sb, project_id, user_id, tactic, entries, batch_params=batch_params)
    monkeypatch.setattr(store, "mint_draft_identity", spy)

    c = FakeClient(rows={"projects": [{"id": PID, "name": "项目", "brand": "A", "owner_id": ME,
                                       "calibration_notes": "", "tactics": "[]", "custom_roles": []}]})
    c.rpc_impl["deskcore_commit_fingerprints"] = lambda args: [
        {"idx": i, "status": core.COMMIT_STATUS_INSERTED, "collided_with": None, "detail": None}
        for i in range(len(args.get("_rows") or []))]
    out = core.commit_drafts(c, PID, [{"title": "标题一", "body": "正文一二三四五六七八", "keywords": ["a"]}], user_id=ME)
    assert out["batch_id"]
    assert captured["params"] == {"source": "deskcore", "flywheel_arm": "explore"}
    assert c.rows["batches"][0]["params"]["flywheel_arm"] == "explore"
    assert list(seen) == [(PID, ME)], "借卡和入库按同一个 (项目, 写手) 算臂"


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
