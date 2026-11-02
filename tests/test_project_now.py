"""简报的 project_now(angle cards v2 §3.4)和发牌回包的那两句话(project_status / batch_note, §3.3)。

数据全是合成的: 一个合成 TV 项目(FX_phase1)对着写作台项目, 笔记只有打标签的列、评论数和一行合成标题; 写作台的台账、
对照表、差异表各几行。

钉住:
  · open_project 只读缓存, 一秒都不等: 缓存是冷的 → counts.project_now = warming(同时投递了后台预热), TV 慢到 5 秒也
    不拖慢简报; 上一次建失败了 → unavailable; 建好了 → ok, 带 project_now;
  · working 一栏: 发出三天以上的够 20 篇才给, 每一格至少 8 篇; 只有篇数和起量数(不给比率、不下结论);
  · desk: 近两周发了几张牌、写成几篇、发出去几篇、几篇标题被改(只数, 不带标题);
  · project_status 三种说法(跑起来了 / 冷启动 / 读不到), ≤120 字; batch_note 只数牌面, 从不提试新。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from deskcore import core, priors, project_now, store, tools as T
from tests.fakes import FakeClient
from tests.test_writer_facing_text import _assert_writer_safe

ME = "11111111-1111-1111-1111-111111111111"
PID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
NOW = datetime.now(timezone.utc)
FMTS = ("提问求助", "提问求助", "提问求助", "提问求助", "场景植入")
LEVS = ("愤怒撬动", "焦虑撬动", "共鸣释放")


def _ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


def _project():
    return {"id": PID, "name": "示例项目", "brand": "示例品", "owner_id": ME, "system_prompt": "你是文案",
            "calibration_notes": "", "tactics": "[]", "default_params": "{}"}


def _notes(n=40):
    out = []
    for i in range(n):
        out.append({"note_id": f"fx{i:04d}", "project_id": "FX_phase1", "publish_time": _ago(days=4 + i),
                    "content_format": FMTS[i % 5], "emotional_lever": LEVS[i % 3],
                    "human_truth_archetype": ["健康焦虑"] if i % 2 else ["消费愉悦"],
                    "emotional_intensity": "medium", "trend_dependencies": ["通用"], "tier": None,
                    "comments_count": 30 if i % 7 == 0 else 3, "data_quality_flags": {},
                    "title": None, "raw_content": f"【标题】合成标题{i}？\n正文略"})
    return out


def _client(notes=None, *, ledger=(), links=(), diffs=()):
    return FakeClient(rows={
        "projects": [_project()], "memories": [], "user_calibration_notes": [], "items": [], "versions": [],
        "batches": [], "draft_fingerprints": [], "style_edits": [], "angle_ledger": list(ledger),
        "tv_project_map": [{"tv_project_id": "FX_phase1", "project_id": PID, "ingest_target": True}],
        "truth_vault.projects": [{"project_id": "FX_phase1", "category": "示例品类"}],
        "truth_vault.notes": list(_notes() if notes is None else notes),
        "truth_vault.note_feature_answers": [], "tv_note_links": list(links), "tv_edit_diffs": list(diffs),
    })


def _brief(c):
    return core.build_writing_brief(c, PID, user_id=ME)


# ══════════════════════════════════════════════════════════════════════
# 简报: ok / warming / unavailable
# ══════════════════════════════════════════════════════════════════════

def test_cold_cache_says_warming_then_ok_once_built():
    c = _client()
    first = _brief(c)
    assert first["counts"]["project_now"] == "warming" and "project_now" not in first
    second = _brief(c)                                       # 用例里的池子就地跑完预热(见 conftest)
    assert second["counts"]["project_now"] == "ok"
    pn = second["project_now"]
    assert pn["stage"] == "warm" and pn["window_days"] == 60 and pn["prior"].startswith("本项目自己的数据和") and pn["task"] == project_now.PROJECT_NOW_TASK
    assert pn["mix"]["posts"] == 40 and pn["mix"]["content_format"] == {"提问求助": 0.8, "场景植入": 0.2}
    assert pn["mix"]["question_title_share"] == 1.0
    assert pn["message"].startswith("示例项目近两个月发了40篇")
    assert pn["external"] == {"category": None, "note": project_now.NO_EXTERNAL_NOTE}
    _assert_writer_safe(second, "open_project")


def test_a_raising_store_gives_unavailable(monkeypatch):
    def down(*a, **k):
        raise RuntimeError("TV 读不到")
    monkeypatch.setattr(store, "tv_projects_of", down)
    c = _client()
    assert _brief(c)["counts"]["project_now"] == "warming"
    out = _brief(c)
    assert out["counts"]["project_now"] == "unavailable" and "project_now" not in out
    assert out["p0"] is not None, "简报照给"

    def boom(*a, **k):
        raise RuntimeError("缓存坏了")
    monkeypatch.setattr(priors, "cached_profile", boom)
    assert _brief(c)["counts"]["project_now"] == "unavailable"


def test_open_project_does_not_wait_for_a_slow_tv(monkeypatch):
    """真的线程池 + 一个 5 秒才回的 TV: 简报照样马上回, 说 warming; 后台那次构建不拖着简报。"""
    priors.reset_cache()                                     # 换回真的线程池
    gate = threading.Event()

    def slow(*a, **k):
        gate.wait(5)
        return []
    for fn in ("tv_notes_outcomes", "tv_projects_of"):
        monkeypatch.setattr(store, fn, slow)
    try:
        c = _client()
        t0 = time.perf_counter()
        out = _brief(c)
        took = time.perf_counter() - t0
        assert out["counts"]["project_now"] == "warming"
        assert took < 1.5, f"open_project 等了先验 {took:.2f}s"
        assert priors.cache_state(PID) == "warming"
    finally:
        gate.set()
        assert priors._POOL[0].wait_idle(10)


def test_brief_survives_project_now_blowing_up(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("坏了")
    monkeypatch.setattr(project_now, "build", boom)
    out = _brief(_client())
    assert out["counts"]["project_now"] == "unavailable" and out["project_id"] == PID


def test_open_project_doc_mentions_project_now():
    doc = T.open_project.__doc__ or ""
    for must in ("project_now", "message", "warming", "unavailable", "不放进开场"):
        assert must in doc, must


# ══════════════════════════════════════════════════════════════════════
# working: 有门槛, 只给计数
# ══════════════════════════════════════════════════════════════════════

def _profile(working, **kw):
    p = priors.uniform_profile(PID)
    p.update(stage="run", n_res=working.get("resolved"), ps="live", pp="live", working=working)
    p["mix"].update(posts=224, per_day=3.7)
    p["evidence"]["content_format"] = {"提问求助": 0.88, "场景植入": 0.12}
    p.update(kw)
    return p


def test_working_needs_twenty_resolved_posts_and_eight_per_cell():
    thin = {"resolved": 19, "successes": 5, "by_title": {"Q": [12, 3], "NQ": [7, 2]}, "by_lever": {}}
    assert "working" not in project_now.build(_profile(thin))
    rich = {"resolved": 203, "successes": 11, "by_title": {"Q": [137, 3], "NQ": [66, 8]},
            "by_lever": {"愤怒撬动": [36, 7], "共鸣释放": [53, 0], "好奇驱动": [7, 1]}}
    wk = project_now.build(_profile(rich))["working"]
    assert wk == {"resolved": 203, "note": project_now.WORKING_NOTE,
                  "by_title": [{"title": "问句", "posts": 137, "起量": 3}, {"title": "非问句", "posts": 66, "起量": 8}],
                  "by_lever": [{"lever": "共鸣释放", "posts": 53, "起量": 0}, {"lever": "愤怒撬动", "posts": 36, "起量": 7}]}
    flat = repr(wk)
    assert "rate" not in flat and "lift" not in flat and "0." not in flat, "只有篇数和起量数"
    cells = {"resolved": 25, "successes": 2, "by_title": {"Q": [7, 1], "NQ": [18, 1]}, "by_lever": {"愤怒撬动": [5, 1]}}
    assert project_now.build(_profile(cells))["working"]["by_title"] == [{"title": "非问句", "posts": 18, "起量": 1}]


# ══════════════════════════════════════════════════════════════════════
# desk: 写作台近两周
# ══════════════════════════════════════════════════════════════════════

def test_desk_counts_written_published_and_title_changes():
    led = [{"id": f"l{i}", "project_id": PID, "angle_key": f"{i:020x}", "dims": {}, "drawn_by": ME,
            "drawn_at": _ago(days=i), "consumed_version_id": f"v{i}" if i < 4 else None,
            "consumed_at": _ago(days=max(i - 1, 0)) if i < 4 else None} for i in range(6)]
    links = [{"note_id": "fx1", "version_id": "v1", "match_kind": "body_exact"},
             {"note_id": "fx2", "version_id": "v2", "match_kind": "ingested"}]          # 补录的不算发出去
    diffs = [{"note_id": "fx1", "title_changed": True, "draft_chars": 500, "published_chars": 480}]
    got = project_now.desk_counts(_client(ledger=led, links=links, diffs=diffs), PID)
    assert got == {"days": 14, "cards_drawn": 6, "written": 4, "published": 1, "title_changed": 1}
    block = project_now.build(_profile({"resolved": 0}), got, name="示例项目")
    assert block["desk"] == got
    assert block["message"].endswith("写作台近两周写的4篇发出去了1篇。")
    nothing = project_now.build(_profile({"resolved": 0}), {**got, "published": 0})
    assert "还没有一篇发出去" in nothing["message"]


def test_desk_counts_fail_soft():
    assert project_now.desk_counts(FakeClient(rows={}), PID) == {"days": 14, "cards_drawn": 0, "written": 0,
                                                                 "published": 0, "title_changed": 0}


# ══════════════════════════════════════════════════════════════════════
# project_status / batch_note
# ══════════════════════════════════════════════════════════════════════

def test_status_line_variants():
    run = _profile({"resolved": 203, "by_lever": {"愤怒撬动": [36, 7]}})
    run["evidence"].update(title_structure={"疑问句": 0.66, "场景直述": 0.34},
                           human_truth_archetype={"健康焦虑": 0.27, "消费愉悦": 0.13})
    s = project_now.status_line(run)
    assert s == "近60天发了224篇，九成是提问求助、一成是场景植入；标题约三分之二是问句，最常写健康焦虑。这批照这个比例发。"
    run["lift"]["emotional_lever"] = {**run["lift"]["emotional_lever"], "愤怒撬动": 1.3}
    assert "情绪偏向这个项目里评论多的几类" in project_now.status_line(run)
    cold = _profile({"resolved": 9}, stage="cold", n_res=9, ref={"source": "brand", "ids": [], "by_dim": {}})
    assert project_now.status_line(cold) == ("本项目发出三天以上的还少（9篇），这批先按同品牌上一期的比例发；"
                                             "发出去三天后开始用本项目自己的数据。")
    assert project_now.status_line(priors.uniform_profile(PID)) == project_now.STATUS_DEGRADED
    assert project_now.status_line(None) == project_now.STATUS_DEGRADED
    ext = _profile({"resolved": 203}, ext={"category": "示例品类", "state": "unavailable", "ok": False})
    assert project_now.status_line(ext, {"ext_ok": False}).endswith("同品类的外部参考这次没读到。")
    for p in (run, cold, ext):
        assert len(project_now.status_line(p)) <= project_now.STATUS_MAX_CHARS


def test_batch_note_counts_the_cards_and_never_mentions_explore():
    def card(fmt, ttl, ref=None):
        a = {"dims": {"content_format": fmt, "title_structure": ttl}}
        if ref:
            a["reference"] = ref
        return a
    picked = ([card("提问求助", "疑问句")] * 6 + [card("提问求助", "场景直述")] * 3
              + [card("场景植入", "感叹共鸣", {"kind": "hit", "own": True})])
    assert project_now.batch_note(picked) == ("新抽的这10篇：9篇提问求助、1篇场景植入，6篇问句标题；"
                                              "1篇参照了本项目近期评论多的帖的结构。")
    picked.append(card("提问求助", "疑问句", {"kind": "external"}))
    note = project_now.batch_note(picked)
    assert "同品类别家" in note and "试新" not in note and "explore" not in note
    assert project_now.batch_note([]) == ""


@pytest.mark.parametrize("s,want", [(0.88, "约九成"), (0.66, "约三分之二"), (0.5, "约一半"), (0.97, "几乎都"),
                                    (0.21, "约两成"), (0.33, "约三分之一")])
def test_share_phrase(s, want):
    assert project_now.share_phrase(s) == want


# ══════════════════════════════════════════════════════════════════════
# 复审回合
# ══════════════════════════════════════════════════════════════════════

def test_message_does_not_say_nothing_was_published_when_the_count_is_unknown():
    """tv_note_links 读不到时 published 是 None(不知道), 不是 0: message 不下「一篇都没发出去」的结论。"""
    desk = {"days": 14, "cards_drawn": 12, "written": 9, "published": None, "title_changed": None}
    msg = project_now.build(_profile({"resolved": 0}), desk, name="示例项目")["message"]
    assert "发出去" not in msg and msg.endswith("写作台近两周写了9篇。")
    assert project_now.build(_profile({"resolved": 0}), {**desk, "published": 0})["message"].endswith(
        "写作台近两周写的9篇还没有一篇发出去。")


def test_batch_note_speaks_only_for_the_newly_drawn_cards():
    """交付里可能有一部分是 angle_debt 补写的(协议: 先写欠着的, 再发牌补齐), batch_note 只数这次新抽的牌, 话也只这么说;
    draw_angles 的说明里交代清楚。"""
    note = project_now.batch_note([{"dims": {"content_format": "提问求助", "title_structure": "疑问句"}}] * 3)
    assert note.startswith("新抽的这3篇：3篇提问求助") and not note.startswith("这批")
    doc = T.draw_angles.__doc__ or ""
    i = doc.index("batch_note")
    assert "angle_debt" in doc[i:i + 400]


def test_status_line_when_only_the_mapping_is_known():
    table = _profile({"resolved": 0}, pp="table", stage="run")
    assert project_now.status_line(table) == project_now.STATUS_TABLE
    assert "通用配比" not in project_now.STATUS_TABLE and len(project_now.STATUS_TABLE) <= project_now.STATUS_MAX_CHARS
    unmapped = _profile({"resolved": 0}, pp="table", stage="none")
    assert project_now.status_line(unmapped).startswith("这个项目还没对上已发的笔记")


def test_external_lesson_wording_waits_for_the_tv_shelf():
    """TV 的书架还是按总评论数排、卡上没有发帖时间和日均评论(配套的 TV 改动还没上): 工具说明照旧说外部卡没经过我们的
    数据验证, 不许诺卡上有它没有的字段。"""
    for fn in (T.open_project, T.borrow_lessons):
        doc = fn.__doc__ or ""
        i = doc.index("external=true 的是")
        part = doc[i:i + 160]
        assert "数据验证" in part and ("没有经过" in part or "没经过" in part), fn.__name__
        assert "日均评论" not in doc and "涨得快" not in doc, fn.__name__
