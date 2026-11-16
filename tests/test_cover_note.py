"""借卡时多带的那一句封面参考(2026-11-16, docs/deskcore.md §3.36)。

数据是 TV 的 ``truth_vault.v_first_image_features``(契约: TV docs/34 第 6 节)。这里钉的是那份契约里写作台要守的几条,
每一条都是【被禁止的形态】: 用了 usable 不为真的行、把没答的题当成「否」、认了不认识的键、不到三篇就开口、话里没有
「没有人对着图核过」、读不到时回包变了样 / 报错 / 拖慢借卡。全部是合成数据。
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import threading
import time

import pytest

import config
import librarian_client as lib
from deskcore import calllog, core, covernote, store, tools
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
PID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
VIEW = "truth_vault.v_first_image_features"


def _card(i: int, **kw) -> dict:
    c = {"source_note_id": f"note-{i}", "hook_type": "反差", "structure": "三段", "why_it_worked": "w",
         "transferable_tactic": "t", "borrow_what": "钩子", "why_relevant": "同品类", "excerpt": "x"}
    c.update(kw)
    return c


def _row(i: int, answers=None, usable=True, **kw) -> dict:
    r = {"note_id": f"note-{i}", "usable": usable, "answers": {} if answers is None else answers}
    r.update(kw)
    return r


PHOTO = {"img_layout": "一张照片"}


# ══════════════════════════════════════════════════════════════════════
# 纯函数: covernote.build_note / source_ids
# ══════════════════════════════════════════════════════════════════════

def test_three_usable_cards_give_one_line_of_counts():
    note = covernote.build_note([_card(1), _card(2), _card(3)],
                                [_row(1, PHOTO), _row(2, PHOTO), _row(3, {"img_layout": "拼图"})])
    assert note and "\n" not in note
    assert "有 3 篇的封面有记录" in note and "2 篇是一张照片" in note
    assert "拼图" not in note                        # 只有一篇的不说


def test_the_line_always_carries_the_nobody_checked_caveat():
    note = covernote.build_note([_card(1), _card(2), _card(3)], [_row(i, PHOTO) for i in (1, 2, 3)])
    assert "没有人对着图核过" in note and "不是规律" in note and "不用照着做" in note


def test_fewer_than_three_usable_says_nothing():
    cards = [_card(1), _card(2), _card(3)]
    assert covernote.build_note(cards, [_row(1, PHOTO), _row(2, PHOTO)]) is None       # 第三篇没有行
    assert covernote.build_note(cards, []) is None
    assert covernote.build_note(cards, None) is None


@pytest.mark.parametrize("bad", [
    {"usable": False}, {"usable": "true"}, {"usable": 1}, {"usable": None}, {"answers": None}, {"answers": "x"},
])
def test_only_rows_whose_usable_is_true_count(bad):
    third = {**_row(3, PHOTO), **bad}
    assert covernote.build_note([_card(1), _card(2), _card(3)], [_row(1, PHOTO), _row(2, PHOTO), third]) is None


def test_a_row_without_the_usable_column_does_not_count():
    third = _row(3, PHOTO)
    del third["usable"]
    assert covernote.build_note([_card(1), _card(2), _card(3)], [_row(1, PHOTO), _row(2, PHOTO), third]) is None


def test_unknown_keys_and_unknown_values_are_ignored():
    extra = {"img_scene": "室内", "img_colour": "暖色", "img_whatever": "有", "img_person": "外星人"}
    rows = [_row(i, {**PHOTO, **extra}) for i in (1, 2, 3)]
    note = covernote.build_note([_card(1), _card(2), _card(3)], rows)
    assert "3 篇是一张照片" in note
    for word in ("室内", "暖色", "外星人", "img_"):
        assert word not in note
    # 只有不认识的键: 有封面信息, 但没有可数的 → 不出空话
    assert covernote.build_note([_card(1), _card(2), _card(3)], [_row(i, extra) for i in (1, 2, 3)]) is None


def test_a_missing_key_is_no_answer_not_a_no():
    rows = [_row(1, {"img_person": "没有人"}), _row(2, {"img_person": "没有人"}), _row(3, {})]
    note = covernote.build_note([_card(1), _card(2), _card(3)], rows)
    assert "2 篇封面上没有人(这一项 2 篇有答案)" in note
    assert "有 3 篇的封面有记录" in note
    # 三篇都答了就不带那个括号
    full = covernote.build_note([_card(1), _card(2), _card(3)],
                                [_row(1, {"img_person": "没有人"}), _row(2, {"img_person": "没有人"}),
                                 _row(3, {"img_person": "看得到脸"})])
    assert "2 篇封面上没有人" in full and "这一项" not in full


def test_at_most_two_clauses_one_per_key_in_a_fixed_order():
    ans = {"img_layout": "一张照片", "img_person": "没有人", "img_product": "有而且是主体", "img_decoration": "没有"}
    cards = [_card(1), _card(2), _card(3), _card(4)]
    rows = [_row(1, ans), _row(2, ans), _row(3, {**ans, "img_layout": "拼图"}), _row(4, {**ans, "img_layout": "拼图"})]
    note = covernote.build_note(cards, rows)
    body = note.split("其中 ", 1)[1].split("。", 1)[0]
    # 四篇一样的两项排前面(并列按固定次序: 人在产品前); 版式两种各两篇, 排不进前两项
    assert body == "4 篇封面上没有人, 4 篇产品是主体"
    assert note == covernote.build_note(list(reversed(cards)), list(reversed(rows)))
    # 同一个键两种取值都够数: 只出排前面的那一个
    two = covernote.build_note(cards, [_row(1, PHOTO), _row(2, PHOTO), _row(3, {"img_layout": "拼图"}),
                                       _row(4, {"img_layout": "拼图"})])
    assert "2 篇是一张照片" in two and "拼图" not in two


def test_nothing_reaches_two_posts_says_nothing():
    rows = [_row(1, {"img_layout": "一张照片"}), _row(2, {"img_layout": "拼图"}), _row(3, {"img_layout": "截图样式"})]
    assert covernote.build_note([_card(1), _card(2), _card(3)], rows) is None


def _body(note: str) -> str:
    return note.split("其中 ", 1)[1].split("。", 1)[0]


def test_no_text_is_said_once_not_twice():
    cards = [_card(1), _card(2), _card(3)]
    ans = {"img_text_kind": "没有字", "img_text_has_number": "没有字"}
    note = covernote.build_note(cards, [_row(i, ans) for i in (1, 2, 3)])
    assert _body(note) == "3 篇封面上没有字" and "None" not in note
    # 前一道说了没有字, 后一道答成「没有」也不再占一项
    odd = {"img_text_kind": "没有字", "img_text_has_number": "没有"}
    assert _body(covernote.build_note(cards, [_row(i, odd) for i in (1, 2, 3)])) == "3 篇封面上没有字"


def test_no_text_majority_is_not_dropped_for_the_runner_up():
    cards = [_card(i) for i in range(1, 6)]
    # 字是哪一类没答成; 有没有数字: 三篇没有字、两篇有 → 说多数, 不说那两篇
    rows = [_row(i, {"img_text_has_number": "没有字"}) for i in (1, 2, 3)] + \
           [_row(i, {"img_text_has_number": "有"}) for i in (4, 5)]
    note = covernote.build_note(cards, rows)
    assert _body(note) == "3 篇封面上没有字" and "None" not in note and "数字" not in note
    # 只有这一道题、三篇都答没有字: 照说, 不是 None 也不是空话
    only = covernote.build_note(cards[:3], [_row(i, {"img_text_has_number": "没有字"}) for i in (1, 2, 3)])
    assert _body(only) == "3 篇封面上没有字"
    # 说「有数字」时没有字的那几篇不进分母: 三篇有、两篇没有字 → 括号里写明三篇有答案
    rows = [_row(i, {"img_text_has_number": "有"}) for i in (1, 2, 3)] + \
           [_row(i, {"img_text_has_number": "没有字"}) for i in (4, 5)]
    assert _body(covernote.build_note(cards, rows)) == "3 篇封面的字里有数字(这一项 3 篇有答案)"


def test_the_line_opens_as_counts_only_not_advice():
    note = covernote.build_note([_card(1), _card(2), _card(3)], [_row(i, PHOTO) for i in (1, 2, 3)])
    assert note.startswith("封面参考(只是计数): 这次借到的卡里有 3 篇的封面有记录, 其中 ")
    assert "建议" not in note


def test_unverified_cards_are_not_called_hits():
    # synthetic = 指标没核过: 封面照数, 但话里不能把这几篇叫成爆帖 / 爆款
    cards = [_card(i, synthetic=True) for i in (1, 2, 3)]
    note = covernote.build_note(cards, [_row(i, {"img_layout": "拼图"}) for i in (1, 2, 3)])
    assert "3 篇是拼图" in note and "爆" not in note
    assert "爆" not in covernote.NOTE


def test_stray_columns_and_ids_never_reach_the_line():
    rows = [_row(i, {**PHOTO, "img_main_text": "假的封面大字"}, project_id="某项目", model="某个模型名",
                 main_text="假的封面大字", status="answered") for i in (1, 2, 3)]
    note = covernote.build_note([_card(1), _card(2), _card(3)], rows)
    for word in ("假的封面大字", "某项目", "某个模型名", "note-", "answered"):
        assert word not in note


def test_external_cards_cards_without_an_id_and_repeats_are_not_counted():
    rows = [_row(i, PHOTO) for i in (1, 2, 3)]
    assert covernote.source_ids([_card(1), _card(1), _card(2, external=True), {"hook_type": "x"}, "不是卡",
                                 _card(4, source_note_id=""), _card(5, source_note_id=None),
                                 _card(6, source_note_id="a,b"), _card(7, source_note_id="x" * 65)]) == ["note-1"]
    assert covernote.build_note([_card(1), _card(2), _card(3, external=True)], rows) is None
    assert covernote.build_note([_card(1), _card(1), _card(2)], rows) is None
    assert covernote.build_note([_card(1), _card(2)], rows) is None


# ══════════════════════════════════════════════════════════════════════
# 读函数: store.tv_first_image_features
# ══════════════════════════════════════════════════════════════════════

def test_read_takes_three_columns_usable_rows_only_in_chunks_of_200():
    sb = FakeClient(rows={VIEW: [_row(1, PHOTO), _row(2, PHOTO, usable=False), _row(449, PHOTO)]})
    ids = [f"note-{i}" for i in range(450)]
    got = store.tv_first_image_features(sb, ids)
    assert sorted(r["note_id"] for r in got) == ["note-1", "note-449"]          # usable=False 的那行库里就筛掉了
    assert [c["table"] for c in sb.calls] == [VIEW] * 3
    assert {c["cols"] for c in sb.calls} == {"note_id, usable, answers"}
    sizes = []
    for c in sb.calls:
        assert c["op"] == "select" and ("eq", "usable", True) in c["filters"]
        sizes += [len(f[2]) for f in c["filters"] if f[0] == "in" and f[1] == "note_id"]
    assert sizes == [200, 200, 50] and max(sizes) <= 200


def test_read_failure_returns_none_with_one_warning_and_no_ids(caplog):
    class _NoSchema:
        def schema(self, name):
            raise RuntimeError("PGRST106 The schema must be one of the following: public")

    class _ViewMissing(FakeClient):
        def _execute(self, q):
            raise RuntimeError('42P01 relation "truth_vault.v_first_image_features" does not exist')

    for sb in (_NoSchema(), _ViewMissing(), object()):
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            assert store.tv_first_image_features(sb, ["note-1", "note-2", "note-3"]) is None
        warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warns) == 1 and "note-" not in warns[0].getMessage()


def test_read_with_no_ids_sends_nothing():
    sb = FakeClient()
    assert store.tv_first_image_features(sb, []) == []
    assert store.tv_first_image_features(sb, None) == []
    assert sb.calls == []


# ══════════════════════════════════════════════════════════════════════
# 接线: open_project / borrow_lessons
# ══════════════════════════════════════════════════════════════════════

def _project():
    return {"id": PID, "name": "测试项目", "brand": "测试品", "owner_id": ME, "system_prompt": "你是文案",
            "calibration_notes": "", "tactics": "[]", "default_params": "{}"}


def _client(features=None, cls=FakeClient):
    rows = {"projects": [_project()], "memories": [], "user_calibration_notes": [], "items": [], "versions": [],
            "batches": [], "draft_fingerprints": [], "angle_ledger": [], "style_edits": []}
    if features is not None:
        rows[VIEW] = features
    return cls(rows=rows)


class _Librarian:
    def __init__(self, cards, delay: float = 0.0, state=lib.BORROW_BORROWED):
        self.cards, self.delay, self.state = list(cards), delay, state

    def __call__(self, brief, *, status=None):
        if self.delay:
            time.sleep(self.delay)
        if status is not None:
            status.update({"state": self.state, "count": len(self.cards), "elapsed_ms": 7, "detail": ""})
        return list(self.cards)


def _view_calls(client) -> int:
    return sum(1 for c in client.calls if c["table"] == VIEW)


def _strip_clock(d: dict) -> dict:
    """去掉每次调用都不一样的计时, 剩下的拿来逐键比。"""
    # project_now 是后台预热的(第一次没有、之后有), 和这一步无关
    out = {k: v for k, v in d.items() if k not in ("elapsed_ms", "phase_ms", "generated_at", "project_now")}
    if isinstance(out.get("counts"), dict):
        out["counts"] = {k: v for k, v in out["counts"].items() if "project_now" not in k and not k.endswith("_ms")}
    if isinstance(out.get("lessons_status"), dict):
        out["lessons_status"] = {k: v for k, v in out["lessons_status"].items() if k != "elapsed_ms"}
    return out


THREE = [_card(1), _card(2), _card(3)]
THREE_ROWS = [_row(1, PHOTO), _row(2, PHOTO), _row(3, PHOTO)]


def test_both_tools_carry_the_same_line_when_three_cards_have_cover_info(monkeypatch):
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", _Librarian(THREE))
    b = core.build_writing_brief(_client(THREE_ROWS), PID, user_id=ME)
    note = b["lessons_cover_note"]
    assert list(note) == ["text", "task"] and note["task"] == covernote.TASK      # 拿它做什么跟着值走
    assert note["text"] == covernote.build_note(THREE, THREE_ROWS)                # 那一句本身一个字没变
    assert "3 篇是一张照片" in note["text"] and "没有人对着图核过" in note["text"]
    assert [c["source_note_id"] for c in b["lessons"]] == ["note-1", "note-2", "note-3"]      # 卡原样
    assert all("cover" not in k for c in b["lessons"] for k in c)
    out = core.borrow_lessons(_client(THREE_ROWS), PID, user_id=ME, draft_topic="早八通勤")
    assert out["cover_note"] == note
    assert "cover_note" not in b and "lessons_cover_note" not in out


def test_two_usable_cards_no_key_at_all(monkeypatch):
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", _Librarian(THREE))
    rows = [_row(1, PHOTO), _row(2, PHOTO), _row(3, PHOTO, usable=False)]
    b = core.build_writing_brief(_client(rows), PID, user_id=ME)
    assert not [k for k in b if "cover" in k] and len(b["lessons"]) == 3
    out = core.borrow_lessons(_client(rows), PID, user_id=ME, draft_topic="t")
    assert not [k for k in out if "cover" in k]


class _Boom(FakeClient):
    """视图读不了(没建 / 没授权 / 网络): 只有那一张视图抛。"""

    def _execute(self, q):
        if q.table_name == VIEW:
            raise RuntimeError("42P01 relation does not exist")
        return super()._execute(q)


def test_view_missing_or_read_error_reply_is_identical_to_no_feature(monkeypatch, caplog):
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", _Librarian(THREE))
    monkeypatch.setattr(core, "_daily_rotation_seed", lambda *a, **k: 0)

    def run(client):
        sink: dict = {}
        got = calllog.collecting(sink, lambda: (
            _strip_clock(core.build_writing_brief(client, PID, user_id=ME)),
            _strip_clock(core.borrow_lessons(client, PID, user_id=ME, draft_topic="t"))))
        assert "cover_note" not in sink                   # 没发那句话: 调用留痕里也不多一个键
        return got

    def keys(pair):                                       # 逐键相同, 连先后也相同
        return [list(d) for d in pair]

    # 基准: 没有这个功能时的回包(把那一步整个拿掉)
    with monkeypatch.context() as m:
        m.setattr(core, "_cover_note", lambda *a, **k: None)
        base = run(_client(THREE_ROWS))
    with caplog.at_level(logging.WARNING):
        boom = run(_client(THREE_ROWS, cls=_Boom))        # 读抛异常: 不抛给调用方
    assert boom == base and keys(boom) == keys(base)
    assert any("first image" in r.getMessage() for r in caplog.records)      # 留了痕
    none = run(_client())                                  # 视图里一行都没有 / 假库里没有这张表
    assert none == base and keys(none) == keys(base)
    for brief, more in (base, boom):
        assert not [k for k in brief if "cover" in k] and not [k for k in more if "cover" in k]
        assert len(brief["lessons"]) == 3 and brief["lessons_status"]["status"] == lib.BORROW_BORROWED


def test_a_stuck_read_is_cut_off_and_the_cards_still_arrive(monkeypatch):
    release = threading.Event()

    class _Stuck(FakeClient):
        def _execute(self, q):
            if q.table_name == VIEW:
                release.wait(20)
            return super()._execute(q)

    monkeypatch.setattr(lib, "fetch_flywheel_lessons", _Librarian(THREE))
    monkeypatch.setattr(core, "COVER_NOTE_WAIT_SEC", 0.2)
    try:
        client = _client(THREE_ROWS, cls=_Stuck)
        t0 = time.monotonic()
        out = core.borrow_lessons(client, PID, user_id=ME, draft_topic="t")
        took = time.monotonic() - t0
    finally:
        release.set()
    assert took < 5, took
    assert len(out["lessons"]) == 3 and out["status"] == lib.BORROW_BORROWED
    assert "cover_note" not in out


def test_cover_read_only_gets_what_is_left_of_the_borrow_budget(monkeypatch):
    """这一步不能让借卡比原来更晚回: 给它的等待 = 借卡预算里剩下的, 再不超过 COVER_NOTE_WAIT_SEC。"""
    waits = []
    monkeypatch.setattr(core, "_cover_note", lambda client, lessons, wait: waits.append(wait))     # 回 None: 不发
    monkeypatch.setattr(config, "BORROW_LESSONS_WAIT_SEC", 1.0)
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", _Librarian(THREE, delay=0.4))
    core.borrow_lessons(_client(THREE_ROWS), PID, user_id=ME, draft_topic="慢的那次")
    assert len(waits) == 1 and waits[0] <= 0.6 + 0.05, waits
    waits.clear()
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", _Librarian(THREE))
    monkeypatch.setattr(config, "BORROW_LESSONS_WAIT_SEC", 18.0)
    core.borrow_lessons(_client(THREE_ROWS), PID, user_id=ME, draft_topic="快的那次")
    assert waits == [core.COVER_NOTE_WAIT_SEC]


def test_no_time_left_or_no_client_sends_no_query():
    client = _client(THREE_ROWS)
    assert core._cover_note(client, THREE, 0.05) is None
    assert core._cover_note(client, THREE, -1) is None
    assert core._cover_note(None, THREE, 1.0) is None
    assert _view_calls(client) == 0
    assert core._cover_note(client, THREE, 1.0)           # 对照: 有时间就读得到
    assert _view_calls(client) == 1


def test_fewer_than_three_cards_with_a_source_id_sends_no_query(monkeypatch):
    cards = [_card(1), _card(2), _card(3, external=True), {"hook_type": "没有 id 的卡"}]
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", _Librarian(cards))
    client = _client(THREE_ROWS)
    b = core.build_writing_brief(client, PID, user_id=ME)
    out = core.borrow_lessons(client, PID, user_id=ME, draft_topic="t")
    assert _view_calls(client) == 0
    assert "lessons_cover_note" not in b and "cover_note" not in out and len(b["lessons"]) == 4


@pytest.mark.parametrize("state", [lib.BORROW_TIMEOUT, lib.BORROW_ERROR, lib.BORROW_EMPTY, lib.BORROW_NOT_CONFIGURED])
def test_a_borrow_that_brought_no_cards_sends_no_query(monkeypatch, state):
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", _Librarian([], state=state))
    client = _client(THREE_ROWS)
    b = core.build_writing_brief(client, PID, user_id=ME)
    out = core.borrow_lessons(client, PID, user_id=ME, draft_topic="t")
    assert _view_calls(client) == 0
    assert b["lessons"] == [] and "lessons_cover_note" not in b and "cover_note" not in out


def test_cover_note_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(covernote, "build_note", boom)
    assert core._cover_note(_client(THREE_ROWS), THREE, 1.0) is None
    monkeypatch.setattr(covernote, "source_ids", boom)
    assert core._cover_note(_client(THREE_ROWS), THREE, 1.0) is None


# ══════════════════════════════════════════════════════════════════════
# 不加环境变量、不动协议; 公开仓: 新文件里没有模型名、没有完整的 UUID(测试里那两个全 1 / 全 a 的占位除外)
# ══════════════════════════════════════════════════════════════════════

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_no_env_var_and_no_protocol_change():
    src = (ROOT / "deskcore" / "covernote.py").read_text(encoding="utf-8")
    assert "environ" not in src and "getenv" not in src and "import config" not in src
    assert isinstance(core.COVER_NOTE_WAIT_SEC, float) and not hasattr(config, "COVER_NOTE_WAIT_SEC")
    for path in (ROOT / "deskcore" / "protocol.md", ROOT / "skills" / "bywood-writing-desk" / "SKILL.md"):
        assert "cover_note" not in path.read_text(encoding="utf-8")       # 写手那边的 skill 不用重装


def test_tool_descriptions_mention_the_key():
    src = (ROOT / "deskcore" / "tools.py").read_text(encoding="utf-8")
    assert "lessons_cover_note" in src and "带 cover_note 时" in src and src.count("没有人对着图核过") >= 2


def test_tool_descriptions_tell_the_model_to_pass_the_line_on_without_calling_them_hits():
    # 定封面的是人, 模型只写正文: 说明里不叫它转告, 这句话就到不了人那里
    from deskcore import tools
    for fn, key in ((tools.open_project, "带 lessons_cover_note 时"), (tools.borrow_lessons, "带 cover_note 时")):
        doc = getattr(fn, "__doc__", None) or getattr(getattr(fn, "fn", None), "__doc__", None) or ""
        assert key in doc, fn
        para = doc.split(key, 1)[1].split("不要自己编", 1)[0]
        assert "原样告诉用户一次" in para and "爆" not in para
        # 和回包的样子对得上: 说明里说的是 {text, task}、照 task 办、转告的是 text
        assert "{text, task}" in para and "照 task 办" in para and "把 text 原样告诉用户一次" in para
        assert "在回包最后" in para


def test_new_files_carry_no_model_names_and_no_real_ids():
    uuid = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
    model = re.compile(r"(claude|gpt|gemini|qwen|deepseek|doubao|glm|kimi|sonnet|haiku|opus)-[\w.]+", re.I)
    for rel in ("deskcore/covernote.py", "tests/test_cover_note.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        ids = {m for m in uuid.findall(text) if len(set(m.replace("-", ""))) > 1}     # 全 1 / 全 a 的占位不算
        assert not ids, (rel, ids)
        assert not model.search(text), rel


# ══════════════════════════════════════════════════════════════════════
# 第二版(2026-11-16): 该怎么办跟着值走、排在回包最后、调用留痕里记一个布尔
# 桌面插件那条路: 模型照的是系统提示里的协议, 工具说明不一定重读; 大的工具结果会被裁成头 4096 + 尾 1024 个字符。
# ══════════════════════════════════════════════════════════════════════

HEAD, TAIL = 4096, 1024           # 桌面客户端裁大结果时留下的头和尾(clients/dsh-plugin 的 preset)
THRESHOLD = 8192                  # 超过这么多字符才裁(同一份 preset 的 thresholdChars)


def _trimmed(text: str) -> str:
    return text if len(text) <= THRESHOLD else text[:HEAD] + text[-TAIL:]


def test_the_trim_numbers_are_the_ones_in_the_plugin_preset():
    preset = (ROOT / "clients" / "dsh-plugin" / "presets" / "writing-desk.patch.yml").read_text(encoding="utf-8")
    for key, n in (("thresholdChars", THRESHOLD), ("headChars", HEAD), ("tailChars", TAIL)):
        assert re.search(rf"{key}:\s*{n}\b", preset), key


def _wire(obj) -> list[str]:
    """回包在线上的两种样子: REST 的紧凑 JSON、MCP 文本块那种带缩进的。中文都不转义。"""
    return [json.dumps(obj, ensure_ascii=False), json.dumps(obj, ensure_ascii=False, indent=2)]


def test_the_value_carries_its_own_instruction():
    w = covernote.wrap("那一句")
    assert w == {"text": "那一句", "task": covernote.TASK} and list(w) == ["text", "task"]
    task = covernote.TASK
    assert "把 text 原样告诉用户一次" in task and "交稿时" in task and "不改写" in task and "不加自己的建议" in task
    assert "不是写稿要求" in task                     # 只当参考, 不是让模型照着写
    assert "已经说过就不用再主动说" in task            # 压缩之后重开项目会再来一遍: 说过就不重复
    # 「说过就不再说」不能盖住「用户问封面时也是它」: 交稿时说过、用户后来再问, 照样可以说
    assert "已经说过就不用再说" not in task and "用户再问时照样可以说" in task
    assert "爆" not in task and not re.search(r"\d", task) and "note" not in task     # 固定的一句: 没有数、没有 id


def _worst_case():
    """最长的那种句子: 五张卡、两项都带括号。"""
    cards = [_card(i) for i in range(1, 6)]
    long_a = {"img_decoration": "没有", "img_brand_readable": "不能"}
    rows = [_row(i, long_a) for i in (1, 2, 3, 4)] + [_row(5, {})]
    text = covernote.build_note(cards, rows)
    assert text.count("这一项 4 篇有答案") == 2
    return cards, rows, text


def test_the_whole_value_fits_well_inside_the_kept_tail():
    _cards, _rows, text = _worst_case()
    for wire in _wire({"lessons_cover_note": covernote.wrap(text)}):
        assert len(wire) < TAIL - 200, len(wire)


def test_open_project_puts_the_line_last_and_it_survives_head_tail_trimming(monkeypatch):
    cards, rows, text = _worst_case()
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", _Librarian(cards))
    client = _client(rows)
    # core 这一层: 借卡之后还会加十来个键(角度债、标题、交付要求…), 那一栏照样在最后
    b = core.build_writing_brief(client, PID, user_id=ME)
    assert list(b)[-1] == "lessons_cover_note" and list(b).index("lessons") < len(b) - 2
    # 工具这一层: 后面还会加 edit_digest 和 skill_update, 前面加 desk_first
    monkeypatch.setattr(core, "sb", lambda: client)
    real = core.build_writing_brief
    # 一份比裁剪线长得多的简报(头被 9000 字的规则占满), 其余照真的拼
    monkeypatch.setattr(core, "build_writing_brief", lambda c, pid, **kw: {"规则": "规" * 9000, **real(c, pid, **kw)})
    monkeypatch.setattr(core, "edit_digest", lambda *a, **k: {"message": "改动摘要", "task": "归纳"})
    out = tools.open_project(PID, skill_version="0123456789ab", _user_id=ME)
    assert "edit_digest" in out and "skill_update" in out and list(out)[0] == "desk_first"
    assert list(out)[-1] == "lessons_cover_note"
    value = out["lessons_cover_note"]
    assert value == {"text": text, "task": covernote.TASK}
    for wire in _wire(out):
        assert len(wire) > THRESHOLD + 2000                   # 过了插件的裁剪线, 真的会被裁
        kept = _trimmed(wire)
        assert text in kept[-TAIL:] and covernote.TASK in kept[-TAIL:] and '"lessons_cover_note"' in kept[-TAIL:]
        assert wire.rstrip("}\n ").endswith(covernote.TASK + '"')        # 它后面只有收尾的括号
    # 对照: 还放在原来的位置(紧跟在卡后面)的话, 裁完就没了
    mid = dict(out)
    moved = {}
    for k, v in mid.items():
        if k == "lessons_cover_note":
            continue
        moved[k] = v
        if k == "lessons":
            moved["lessons_cover_note"] = value
    assert text not in _trimmed(json.dumps(moved, ensure_ascii=False))


def test_borrow_lessons_puts_the_line_last_even_when_patterns_usage_is_added_after(monkeypatch):
    cards, rows, text = _worst_case()

    class _WithPatterns(_Librarian):
        def __call__(self, brief, *, status=None):
            got = super().__call__(brief, status=status)
            if status is not None:
                status["patterns"] = [{"id": "p1", "label": "结尾问读者", "instruction": "结尾留一个问题。"}]
            return got

    monkeypatch.setattr(core, "flywheel_arm", lambda pid, uid, **kw: "patterns")
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", _WithPatterns(cards))
    client = _client(rows)
    monkeypatch.setattr(core, "sb", lambda: client)
    out = tools.borrow_lessons(PID, draft_topic="t", _user_id=ME)
    assert out.get("validated_patterns") and "validated_patterns_usage" in out     # 借卡之后才加的键
    assert list(out)[-1] == "cover_note" and out["cover_note"] == {"text": text, "task": covernote.TASK}
    for wire in _wire(out):
        assert wire.rstrip("}\n ").endswith(covernote.TASK + '"')


def test_move_last_leaves_a_reply_without_the_key_untouched():
    d = {"a": 1, "b": 2}
    assert covernote.move_last(d, "cover_note") is d and list(d) == ["a", "b"]
    for odd in (None, [1], "x"):
        assert covernote.move_last(odd, "cover_note") is odd
    e = {"a": 1, "cover_note": {"text": "t"}, "b": 2}
    assert covernote.move_last(e, "cover_note") is e and list(e) == ["a", "b", "cover_note"]


def test_the_call_log_row_records_that_the_line_was_sent(monkeypatch):
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", _Librarian(THREE))
    for tool, call in (("open_project", lambda c: core.build_writing_brief(c, PID, user_id=ME)),
                       ("borrow_lessons", lambda c: core.borrow_lessons(c, PID, user_id=ME, draft_topic="t"))):
        sink: dict = {}
        result = calllog.collecting(sink, call, _client(THREE_ROWS))
        assert sink.get("cover_note") is True, tool
        row = calllog.build({"tool": tool, "channel": "mcp", "user_id": ME, "elapsed_ms": 5,
                             "args": {"project_id": PID}, "sink": sink, "result": result})
        assert row["meta"]["cover_note"] is True, tool
        # 只有这一个布尔: 那句话的字、卡的 id 都不进这一行
        flat = json.dumps(row, ensure_ascii=False)
        assert "封面" not in flat and "note-" not in flat
        # 没发的那次: 行里没有这个键(和原来的行一样)
        sink = {}
        result = calllog.collecting(sink, call, _client(THREE_ROWS[:2]))
        assert "cover_note" not in sink
        row = calllog.build({"tool": tool, "channel": "mcp", "user_id": ME, "elapsed_ms": 5,
                             "args": {"project_id": PID}, "sink": sink, "result": result})
        assert "cover_note" not in row["meta"], tool
    # 只认布尔的 True: 别的东西混进来不记
    carried = {"lessons_cover_note": covernote.wrap("那一句")}
    for odd in ("yes", 1, "封面参考", {"text": "x"}):
        row = calllog.build({"tool": "open_project", "channel": "mcp", "user_id": ME, "elapsed_ms": 1, "args": {},
                             "sink": {"cover_note": odd}, "result": carried})
        assert "cover_note" not in row["meta"]


def test_the_flag_is_not_logged_when_the_reply_did_not_carry_the_line(monkeypatch):
    """core 是在借卡那一步记的布尔; 之后调用抛了、或者降级成不带这一栏的错误回包, 客户端没拿到那句话 —— 行里不能有 true。"""
    def row(**job):
        return calllog.build({"tool": "open_project", "channel": "mcp", "user_id": ME, "elapsed_ms": 1,
                              "args": {"project_id": PID}, "sink": {"cover_note": True}, **job})["meta"]

    assert row(result={"lessons_cover_note": covernote.wrap("那一句")})["cover_note"] is True       # 对照: 带了就记
    assert row(result={"cover_note": covernote.wrap("那一句")})["cover_note"] is True
    assert "cover_note" not in row(outcome="failed", error_class="RuntimeError")       # 抛了: 没有回包
    assert "cover_note" not in row(outcome="refused", error_class="PermissionError")
    assert "cover_note" not in row(result={"error": "degraded", "lessons": []})        # 降级的错误回包里没有这一栏
    assert "cover_note" not in row(result={"lessons": []})
    assert "cover_note" not in row(result={"lessons_cover_note": None})                # 占位不算带了
    # 真的走一遍: 借卡记下了, 后面那一步抛了 → _safe 回降级结果, 行里没有这个键
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", _Librarian(THREE))
    monkeypatch.setattr(core, "flywheel_arm", lambda pid, uid, **kw: "patterns")
    client = _client(THREE_ROWS)
    monkeypatch.setattr(core, "sb", lambda: client)

    def boom(*a, **k):
        raise RuntimeError("after the borrow")
    real = core.borrow_lessons

    def then_fail(*a, **k):
        real(*a, **k)
        boom()
    monkeypatch.setattr(core, "borrow_lessons", then_fail)
    sink: dict = {}
    result = calllog.collecting(sink, tools.borrow_lessons, PID, draft_topic="t", _user_id=ME)
    assert sink.get("cover_note") is True and "cover_note" not in result and result.get("error")
    meta = calllog.build({"tool": "borrow_lessons", "channel": "mcp", "user_id": ME, "elapsed_ms": 1,
                          "args": {"project_id": PID}, "sink": sink, "result": result})["meta"]
    assert meta["outcome"] == "error_result" and "cover_note" not in meta
    assert calllog._COVER_KEYS == covernote.KEYS


def test_the_mcp_text_block_does_not_escape_chinese_so_the_value_stays_in_the_tail(monkeypatch):
    """尾巴装得下的前提: 中文在线上不被转义(转义之后最长的那种比尾巴长)。拿装着的 MCP 包走真的 tools/call 看文本块。"""
    pytest.importorskip("mcp")
    pytest.importorskip("fastapi")
    import anyio
    from mcp import types
    import deskcore.app as A
    from deskcore.identity import Caller

    _cards, _rows, text = _worst_case()
    value = covernote.wrap(text)
    assert len(json.dumps({"lessons_cover_note": value})) > TAIL          # 转义的话装不下: 这条用例钉的就是「不转义」
    reply = {"规则": "规" * 9000, "lessons": [], "lessons_cover_note": value}
    monkeypatch.setattr(tools, "TOOLS", {"t": (lambda _user_id=None: dict(reply), True)})
    mcp = A._register_mcp()
    got: dict = {}

    async def run():
        token = A._caller.set(Caller(ME))
        try:
            handler = mcp._mcp_server.request_handlers[types.CallToolRequest]
            req = types.CallToolRequest(method="tools/call",
                                        params=types.CallToolRequestParams(name="t", arguments={}))
            got["result"] = (await handler(req)).root
        finally:
            A._caller.reset(token)
    anyio.run(run)
    res = got["result"]
    assert not res.isError
    blocks = [b.text for b in res.content if getattr(b, "type", "") == "text"]
    assert len(blocks) == 1
    wire = blocks[0]
    assert "\\u" not in wire and "规规规" in wire
    assert len(wire) > THRESHOLD                                          # 过了插件的裁剪线, 真的会被裁
    kept = _trimmed(wire)
    assert text in kept[-TAIL:] and covernote.TASK in kept[-TAIL:] and '"lessons_cover_note"' in kept[-TAIL:]
    # 整条 JSON-RPC 回包(json_response)也不转义
    assert text in res.model_dump_json()


def test_a_second_open_returns_the_line_again(monkeypatch):
    # 重复规则(docs/deskcore.md §3.36): 服务端不记「这一场说过没有」(共用身份, 分不出哪一场), 再开一次照样带;
    # 不重复念靠的是 task 里那半句
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", _Librarian(THREE))
    client = _client(THREE_ROWS)
    first = core.build_writing_brief(client, PID, user_id=ME)
    second = core.build_writing_brief(client, PID, user_id=ME)
    assert first["lessons_cover_note"] == second["lessons_cover_note"]
    assert "已经说过就不用再主动说" in second["lessons_cover_note"]["task"]


def test_the_docs_say_what_is_not_verified_on_the_desktop_path():
    doc = (ROOT / "docs" / "deskcore.md").read_text(encoding="utf-8")
    sec = doc.split("### 3.36", 1)[1].split("\n## 4.", 1)[0]
    assert "{text, task}" in sec and "最后" in sec and "cover_note" in sec
    # 重复规则: 钉标题和结论那半句(「再打开一次」这几个字在那一条的正文里也有, 只钉它的话标题改掉了也过)
    assert "**再打开一次会再来一遍**" in sec and "不为它加状态" in sec
    rule = sec.split("**再打开一次会再来一遍**", 1)[1].split("\n- **", 1)[0]
    assert "不记" in rule and "强制重开" in rule and "不为它加状态" in rule
    assert "已经说过就不用再主动说" in sec and "用户再问" in sec          # 文档里的 task 和代码里的是同一句
    assert covernote.TASK in sec
    assert "模型转告" in sec and "没有在真的桌面上验证过" in sec
