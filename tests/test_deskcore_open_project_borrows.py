"""``open_project`` 必须随简报把飞轮经验卡一起借回来(通道 2 点灯)。

2026-09-19 查生产库(truth-vault docs/27 §2「请查三件事」):

  · 2026-09-01 ~ 09-16 写作台 ``commit_drafts`` 了 **71 批**稿子;
  · 同期 ``truth_vault.flywheel_librarian_cache`` 里只有 **3 次**借阅, 每次都
    借到 4-5 张卡 —— 馆员是通的, 部署也配了;
  · 协议里 ``open_project`` / ``draw_angles`` 是"必做", ``borrow_lessons`` 是
    "想要真实爆款参照时调"。于是通道 2 在每场对话里取决于模型愿不愿意多调
    一个可选工具, 而它 95% 的时候不愿意。

修法: 借阅并进必做的 ``open_project``。这里钉的是【被禁止的形态】——
简报里没有 ``lessons`` / ``lessons_status``, 或者借阅失败把简报拖垮。
"""

from __future__ import annotations

import logging

import pytest

import config
import librarian_client as lib
from deskcore import core
from tests.fakes import FakeClient


ME = "11111111-1111-1111-1111-111111111111"
PID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _project(**kw):
    row = {"id": PID, "name": "测试项目", "brand": "测试品",
           "owner_id": ME, "system_prompt": "你是文案",
           "calibration_notes": "", "tactics": "[]", "default_params": "{}"}
    row.update(kw)
    return row


def _client():
    return FakeClient(rows={
        "projects": [_project()], "memories": [],
        "user_calibration_notes": [],
        "items": [], "versions": [], "batches": [],
        "draft_fingerprints": [], "angle_ledger": [], "style_edits": [],
    })


def _card(i: int, **kw) -> dict:
    c = {"source_note_id": f"note-{i}", "hook_type": "反差", "structure": "三段",
         "why_it_worked": "w", "transferable_tactic": "t",
         "borrow_what": "钩子", "why_relevant": "同品类", "excerpt": "x"}
    c.update(kw)
    return c


class _Librarian:
    """替身馆员: 记下收到的 brief, 按指定结局回卡。"""

    def __init__(self, cards=None, state=lib.BORROW_BORROWED):
        self.cards = list(cards or [])
        self.state = state
        self.briefs: list[dict] = []

    def __call__(self, brief, *, status=None):
        self.briefs.append(dict(brief))
        if status is not None:
            status.update({"state": self.state, "count": len(self.cards),
                           "elapsed_ms": 7, "detail": ""})
        return list(self.cards)


# ══════════════════════════════════════════════════════════════════════
# 主路径: 简报自带卡
# ══════════════════════════════════════════════════════════════════════

def test_open_project_brief_carries_the_borrowed_cards(monkeypatch):
    fake = _Librarian(cards=[_card(1), _card(2, synthetic=True)])
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", fake)

    b = core.build_writing_brief(_client(), PID, user_id=ME,
                                 brief={"draft_topic": "早八通勤",
                                        "tactic": "场景种草"})

    assert [c["source_note_id"] for c in b["lessons"]] == ["note-1", "note-2"]
    # synthetic 标记原样透传 —— 协议靠它告诉模型"这张卡的数据不可采信"
    assert b["lessons"][1]["synthetic"] is True
    assert b["lessons_status"]["status"] == lib.BORROW_BORROWED
    assert b["lessons_status"]["count"] == 2
    assert b["counts"]["lessons"] == 2
    # 简报其它部分不受影响
    assert "p0" in b and "stable" in b


def test_the_brief_sent_to_the_librarian_is_deskcore_with_this_batch_delta(monkeypatch):
    """馆员按 consumer 记缓存 / 按 delta 匹配, 两样都得从简报参数带过去。"""
    fake = _Librarian(cards=[_card(1)])
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", fake)

    core.build_writing_brief(_client(), PID, user_id=ME,
                             brief={"draft_topic": "早八通勤", "tactic": "场景种草",
                                    "key_messages": "省时", "target_audience": "上班族",
                                    "tone": "轻松", "extra_instructions": "别用问句"})

    assert len(fake.briefs) == 1
    sent = fake.briefs[0]
    assert sent["consumer"] == "deskcore"
    assert sent["project_id"] == PID
    assert sent["brand"] == "测试品"
    assert sent["tactic"] == "场景种草"
    assert sent["draft_topic"] == "早八通勤"
    assert sent["key_messages"] == "省时"
    assert sent["target_audience"] == "上班族"
    assert sent["tone"] == "轻松"


def test_borrowing_happens_even_when_the_caller_passes_no_delta(monkeypatch):
    """"知道要写什么就传"是建议不是前提 —— 光按项目定位也要借一次。"""
    fake = _Librarian(cards=[_card(1)])
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", fake)
    b = core.build_writing_brief(_client(), PID, user_id=ME)
    assert len(fake.briefs) == 1
    assert b["counts"]["lessons"] == 1


def test_cards_are_capped_at_the_same_number_the_prompt_renderer_uses(monkeypatch):
    import memory
    fake = _Librarian(cards=[_card(i) for i in range(memory.FLYWHEEL_CARD_CAP + 4)])
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", fake)
    b = core.build_writing_brief(_client(), PID, user_id=ME)
    assert len(b["lessons"]) == memory.FLYWHEEL_CARD_CAP
    assert b["lessons_status"]["count"] == memory.FLYWHEEL_CARD_CAP


# ══════════════════════════════════════════════════════════════════════
# fail-open: 借不到不能拖垮简报, 但必须留痕
# ══════════════════════════════════════════════════════════════════════

def test_not_configured_still_returns_the_brief_and_says_why(monkeypatch, caplog):
    monkeypatch.setattr(config, "LIBRARIAN_URL", "")
    monkeypatch.setattr(config, "LIBRARIAN_API_KEY", "")
    with caplog.at_level(logging.WARNING, logger="deskcore.core"):
        b = core.build_writing_brief(_client(), PID, user_id=ME)
    assert b["lessons"] == []
    assert b["lessons_status"]["status"] == lib.BORROW_NOT_CONFIGURED
    assert b["counts"]["lessons"] == 0
    assert "p0" in b
    # 留痕: 服务日志里 grep 得到通道 2 为什么黑
    assert any("not_configured" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("state", [lib.BORROW_TIMEOUT, lib.BORROW_ERROR])
def test_librarian_failures_are_reported_not_hidden(monkeypatch, caplog, state):
    fake = _Librarian(cards=[], state=state)
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", fake)
    with caplog.at_level(logging.WARNING, logger="deskcore.core"):
        b = core.build_writing_brief(_client(), PID, user_id=ME)
    assert b["lessons"] == []
    assert b["lessons_status"]["status"] == state
    assert any(state in r.getMessage() for r in caplog.records)


def test_empty_is_a_normal_outcome_with_no_warning(monkeypatch, caplog):
    fake = _Librarian(cards=[], state=lib.BORROW_EMPTY)
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", fake)
    with caplog.at_level(logging.WARNING, logger="deskcore.core"):
        b = core.build_writing_brief(_client(), PID, user_id=ME)
    assert b["lessons_status"]["status"] == lib.BORROW_EMPTY
    assert not [r for r in caplog.records if "flywheel" in r.getMessage()]


def test_an_unexpected_crash_in_the_borrow_path_does_not_kill_open_project(monkeypatch):
    """fetch_flywheel_lessons 本身绝不抛, 但 build_brief / 任何意外仍要兜住 ——
    P0 已经拿到了, 卡借不到只是少点参考。兜住之后仍要留 status, 不吞成看似成功。"""
    def boom(*a, **k):
        raise RuntimeError("librarian client exploded")
    monkeypatch.setattr(lib, "build_brief", boom)
    b = core.build_writing_brief(_client(), PID, user_id=ME)
    assert b["lessons"] == []
    assert b["lessons_status"]["status"] == lib.BORROW_ERROR
    assert "exploded" in b["lessons_status"]["detail"]
    assert "p0" in b


# ══════════════════════════════════════════════════════════════════════
# borrow_lessons 仍在, 与 open_project 走同一个借阅体
# ══════════════════════════════════════════════════════════════════════

def test_borrow_lessons_tool_shares_the_same_borrow_body(monkeypatch):
    fake = _Librarian(cards=[_card(1)])
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", fake)
    out = core.borrow_lessons(_client(), PID, user_id=ME, draft_topic="早八通勤")
    assert out["count"] == 1 and out["status"] == lib.BORROW_BORROWED
    assert fake.briefs[0]["consumer"] == "deskcore"
    assert fake.briefs[0]["draft_topic"] == "早八通勤"


# ══════════════════════════════════════════════════════════════════════
# 协议正文必须告诉模型这件事 —— 否则它会照旧再去调 borrow_lessons, 或者
# 拿到 lessons 也不知道能用
# ══════════════════════════════════════════════════════════════════════

def test_protocol_tells_the_model_the_brief_already_carries_lessons():
    from deskcore import tools as T
    body, _ = T.protocol_text()
    sec1 = body.split("**2. `draw_angles`**")[0]
    assert "`lessons`" in sec1, "协议第 1 条(open_project)没提 lessons"
    assert "lessons_status" in sec1
    assert "`open_project` 已经随简报借过一次" in body
    # 技能文件里那份必须是同一份 —— 改了 protocol.md 就跑 sync-skill
    assert T.skill_sync_state()["in_sync"]
