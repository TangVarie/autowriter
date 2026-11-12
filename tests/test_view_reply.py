"""排好版的页面接进 score_drafts 的回包(2026-11-12, docs/deskcore.md §3.29; 页面和令牌本身在 tests/test_viewpage.py、
tests/test_view_page_route.py)。

钉住:
  1. 存进去了就带 ``view``(url / expires / note); 有交付模板的项目那一句接在 ``delivery.text`` 的最后一行(空一行之后,
     只接一次, 拼回整批的那条路也一样), tv-sync 拆标题正文时不认那一行;
  2. 没有交付模板的项目: 回包照旧没有 delivery, next_step 里多一份默认的排法和「把 view.note 原样放最后一行」;
  3. 改稿重存拿到的链接看的还是原来那一批(另有新稿的加上新的一批); 分属太多批的不发;
  4. 发不出来、记不下来都不连累存档: 没有 ``view``、一行日志、written 照旧; 总开关关了回包和原来一字不差;
  5. 链接和令牌不进日志、不进任何一张表: batch_metrics 和工具调用的留痕里只有「发没发」和 batch id 的前 8 位;
  6. 每个项目的 batch 都记顺序和话题 / 评论 / 方向; 没模板的项目回包里照旧没有 delivery;
  7. 从头走一遍: 过 app 调 score_drafts → 拿回包里的链接 → GET → 页面上是这几篇、顺序对; 改一篇重存 → 同一个链接是新的字。
所有文本都是合成的, 钥匙是测试里现编的。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

import pytest

import config
from deskcore import calllog, core, delivery, store, tvlink, viewpage
from tests.test_delivery import BLOCKS, ME, PROJ, TABLE, _params_with, desk_client, distinct_drafts
from tests.test_delivery_revision import _embed, _full, _revise
from tests.test_writer_facing_text import _assert_writer_safe

SECRET = "xianbian-de-yemian-miyao-0002"                   # 现编的
BASE = "https://desk.example.com"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    monkeypatch.setattr(config, "JUDGE_URL", "")


@pytest.fixture()
def view_on(monkeypatch):
    monkeypatch.setenv(viewpage.SECRET_ENV, SECRET)
    monkeypatch.setenv(viewpage.PUBLIC_URL_ENV, BASE)
    monkeypatch.delenv(viewpage.SWITCH_ENV, raising=False)
    monkeypatch.delenv(viewpage.RAILWAY_DOMAIN_ENV, raising=False)


def _token(view: dict) -> str:
    return view["url"].rsplit("/v/", 1)[1]


def _batches(view: dict) -> list[str]:
    return viewpage.read_token(_token(view), viewpage.derive_key(SECRET), time.time())


def _view_meta(c) -> dict | None:
    return c.rows["batch_metrics"][-1]["meta"].get("view")


def _page_titles(c, view: dict) -> list[str]:
    ids, exp = viewpage.open_token(_token(view), viewpage.derive_key(SECRET), time.time())
    return [p["title"] for p in core.view_page_data(c, ids, exp)["posts"]]


# ══════════════════════════════════════════════════════════════════════
# 1 · 有交付模板的项目
# ══════════════════════════════════════════════════════════════════════

def test_template_project_gets_the_link_and_the_note_as_the_last_line_of_delivery_text(view_on):
    c = desk_client(default_params=_params_with(TABLE))
    drafts = _full(distinct_drafts(3, seed=5))
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert out["written"] == 3
    view = out["view"]
    assert set(view) == {"url", "expires", "note"}
    assert view["url"].startswith(BASE + "/v/") and view["note"] == viewpage.NOTE.format(url=view["url"])
    assert _batches(view) == [out["batch_id"]]
    until = datetime.fromisoformat(view["expires"])
    assert until.utcoffset() == timedelta(hours=8), "到期时间是北京时间"
    left = until - datetime.now(timezone.utc)
    assert timedelta(days=viewpage.VALID_DAYS, minutes=-5) < left <= timedelta(days=viewpage.VALID_DAYS)

    table = delivery.assemble(delivery.validate(TABLE), drafts)["text"]
    assert out["delivery"]["text"] == table + "\n\n" + view["note"], "表格一个字不变, 空一行之后是那一句"
    assert out["delivery"]["text"].count("/v/") == 1
    assert out["delivery"]["problems"] == []
    step = out["next_step"]
    assert core.DELIVERY_NEXT_STEP in step and core.VIEW_IN_DELIVERY_NEXT_STEP in step
    assert core.VIEW_NOTE_NEXT_STEP not in step and core.PLAIN_LAYOUT_NEXT_STEP not in step
    assert _page_titles(c, view) == [d["title"] for d in drafts]
    _assert_writer_safe(out, "带页面链接的回包")


def test_tv_sync_does_not_read_the_note_line_as_part_of_a_post(view_on):
    """一篇一段的模板: 写手把交付稿整段(或者最后一篇连着那一句)贴进飞书的一格, tv-sync 拆出来的正文里不能有那一行。"""
    c = desk_client(default_params=_params_with(BLOCKS))
    drafts = distinct_drafts(2, seed=7)
    for k, d in enumerate(drafts):
        d["tags"] = [f"话题{k}"]
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    text, note = out["delivery"]["text"], out["view"]["note"]
    assert text.endswith("\n\n" + note) and text.count(note) == 1
    blocks = delivery.assemble(delivery.validate(BLOCKS), drafts)["text"].split("\n\n")
    last_cell = blocks[-1] + "\n\n" + note                 # 最后一篇连着那一句进了同一格
    assert tvlink.parse_raw(last_cell) == tvlink.parse_raw(blocks[-1])
    title, body = tvlink.parse_raw(blocks[-1] + "\r\n\r\n" + note + "\r\n")
    assert (title, body) == tvlink.parse_raw(blocks[-1]) and "/v/" not in body and "排好版" not in body
    assert "/v/" not in tvlink.parse_raw(text)[1], "整段贴进一格也一样"


def test_tv_sync_leaves_ordinary_lines_with_links_alone():
    raw = "【标题】一个标题\n【正文】第一段 https://example.com/v/abc 看这里\n排好版的页面我也做过一个\n第三段"
    assert tvlink.parse_raw(raw) == ("一个标题", "第一段 https://example.com/v/abc 看这里\n排好版的页面我也做过一个\n第三段")
    assert tvlink.parse_raw("没有标记的一段 /v/ 字样") == ("", "没有标记的一段 /v/ 字样")


def test_the_note_wording_and_the_tv_sync_pattern_stay_in_step():
    """tvlink 不 import viewpage(只做匹配), 那一句的写法改了这里会红。"""
    for base in (BASE, "http://localhost:8000", "https://desk.example.com/desk"):
        url = f"{base}/v/{viewpage.make_token(['aaaaaaaa-0000-0000-0000-0000000000b1'], 1_900_000_000, viewpage.derive_key(SECRET))}"
        assert tvlink._VIEW_NOTE_RE.fullmatch(viewpage.NOTE.format(url=url))


# ══════════════════════════════════════════════════════════════════════
# 2 · 没有交付模板的项目
# ══════════════════════════════════════════════════════════════════════

def test_project_without_a_template_gets_the_link_a_default_layout_and_no_delivery(view_on):
    c = desk_client()
    drafts = _full(distinct_drafts(3, seed=6))
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert out["written"] == 3 and "delivery" not in out
    assert _batches(out["view"]) == [out["batch_id"]]
    step = out["next_step"]
    assert step.index(core.PLAIN_LAYOUT_NEXT_STEP) < step.index(core.VIEW_NOTE_NEXT_STEP)
    assert core.VIEW_IN_DELIVERY_NEXT_STEP not in step and core.DELIVERY_NEXT_STEP not in step
    assert _page_titles(c, out["view"]) == [d["title"] for d in drafts]
    _assert_writer_safe(out, "没有模板的项目")


def test_the_default_layout_wording():
    tip = core.PLAIN_LAYOUT_NEXT_STEP
    assert "照项目的" in tip, "项目自己有交付格式的, 项目的优先"
    for part in ("「第N篇 · 这篇的场景(12 字以内)」", "【标题】接标题", "【正文】接正文", "话题单独占一行",
                 "篇与篇之间空一行", "不用表格", "不加别的标记"):
        assert part in tip, part
    assert "原样" in core.VIEW_NOTE_NEXT_STEP and "最后一行" in core.VIEW_NOTE_NEXT_STEP
    _assert_writer_safe([tip, core.VIEW_NOTE_NEXT_STEP, core.VIEW_IN_DELIVERY_NEXT_STEP, viewpage.NOTE], "那几句")


def test_the_default_layout_has_its_own_switch(view_on, monkeypatch):
    monkeypatch.setenv(core.PLAIN_LAYOUT_ENV, "0")
    out = core.commit_drafts(desk_client(), PROJ, distinct_drafts(2, seed=6), user_id=ME)
    assert core.PLAIN_LAYOUT_NEXT_STEP not in out["next_step"], "关了就不给那句默认排法"
    assert core.VIEW_NOTE_NEXT_STEP in out["next_step"] and "view" in out, "页面链接照发, 两个开关各管各的"


def test_posts_laid_out_the_default_way_read_back_through_tv_sync():
    """照默认排法交出去的一篇贴进飞书的一格: 【标题】上面那行小标签不认, 标题正文拆得回来。"""
    cell = "第2篇 · 合成的场景乙\n【标题】一个合成的标题\n【正文】第一段。\n\n第二段。\n#话题甲 #话题乙"
    assert tvlink.parse_raw(cell) == ("一个合成的标题", "第一段。\n\n第二段。\n#话题甲 #话题乙")


def test_a_broken_template_gets_the_link_but_not_the_default_layout(view_on):
    """模板写坏了: 没有 delivery(记日志), 页面链接照发(模型把那一句放最后一行); 默认排法只给真的没有模板的项目。"""
    c = desk_client(default_params=json.dumps({"delivery": {"layout": "不认识的版式"}}, ensure_ascii=False))
    out = core.commit_drafts(c, PROJ, distinct_drafts(2, seed=8), user_id=ME)
    assert out["written"] == 2 and "delivery" not in out and "view" in out
    assert core.VIEW_NOTE_NEXT_STEP in out["next_step"] and core.PLAIN_LAYOUT_NEXT_STEP not in out["next_step"]


# ══════════════════════════════════════════════════════════════════════
# 3 · 改稿重存
# ══════════════════════════════════════════════════════════════════════

def test_a_revision_links_to_the_original_batch_and_appends_the_note_once(view_on):
    c = desk_client(default_params=_params_with(TABLE))
    drafts = _full(distinct_drafts(3, seed=5))
    first = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    _embed(c)
    out = core.commit_drafts(c, PROJ, [_revise(drafts[1], first["version_ids"][1], "一")], user_id=ME)
    assert out["replaced"] and out["batch_id"] is None, "改稿不建新 batch"
    assert _batches(out["view"]) == [first["batch_id"]], "看的还是原来那一批"
    d = out["delivery"]
    assert d["scope"] == "whole_batch"
    lines = d["text"].split("\n")
    assert lines[-1] == out["view"]["note"] and lines[-2] == "" and d["text"].count("/v/") == 1
    assert len(lines) == 5 + 2, "一个表头 + 分隔行 + 三行, 空一行, 那一句"
    assert _view_meta(c) == {"issued": True, "batches": 1, "batch8": [first["batch_id"][:8]]}
    # 第一次拿到的链接, 现在看到的是改过的那一版
    ids, exp = viewpage.open_token(_token(first["view"]), viewpage.derive_key(SECRET), time.time())
    posts = core.view_page_data(c, ids, exp)["posts"]
    assert [p["title"] for p in posts] == [d["title"] for d in drafts]
    assert "后来补了一句一。" in posts[1]["paragraphs"][-1] and posts[1]["direction"] == "改过的方向一"


def test_a_revision_that_could_not_rebuild_the_batch_still_ends_with_the_note(view_on):
    c = desk_client(default_params=_params_with(TABLE))
    drafts = _full(distinct_drafts(3, seed=5))
    first = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    c.rows["batches"][0]["params"].pop(core.DELIVERY_PARTS_KEY)          # 这之前存的 batch
    _embed(c)
    out = core.commit_drafts(c, PROJ, [_revise(drafts[1], first["version_ids"][1], "一")], user_id=ME)
    assert out["delivery"]["scope"] == "this_call"
    assert out["delivery"]["text"].endswith("\n\n" + out["view"]["note"]) and out["delivery"]["text"].count("/v/") == 1
    assert _batches(out["view"]) == [first["batch_id"]]
    assert len(_page_titles(c, out["view"])) == 3, "没记顺序的 batch 页面照样出(按 item id 排)"


def test_a_revision_plus_new_drafts_links_to_the_old_batch_and_the_new_one(view_on):
    c = desk_client()
    old = _full(distinct_drafts(2, seed=11), "甲")
    first = core.commit_drafts(c, PROJ, old, user_id=ME)
    c.rows["batches"][0]["created_at"] = "2026-11-10T08:00:00+00:00"
    _embed(c)
    fresh = _full(distinct_drafts(1, seed=13), "丙")[0]
    out = core.commit_drafts(c, PROJ, [fresh, _revise(old[1], first["version_ids"][1], "甲")], user_id=ME)
    assert out["written"] == 2 and out["batch_id"] and out["batch_id"] != first["batch_id"]
    assert _batches(out["view"]) == [first["batch_id"], out["batch_id"]], "原来那一批在前, 这次新建的在后"
    c.rows["batches"][1]["created_at"] = "2026-11-12T08:00:00+00:00"
    assert _page_titles(c, out["view"]) == [old[0]["title"], old[1]["title"], fresh["title"]]
    assert _view_meta(c)["batches"] == 2


def test_view_batches_keeps_the_callers_order_and_drops_what_it_cannot_place():
    got = core._view_batches({3: "item-c", 0: "item-a", 1: "item-b", 2: "item-x"},
                             {"item-a": "batch-1", "item-b": "batch-2", "item-c": "batch-1"}, "batch-new")
    assert got == ["batch-1", "batch-2", "batch-new"]
    assert core._view_batches({}, {}, None) == [] and core._view_batches({}, {}, "b") == ["b"]


def test_revisions_across_more_batches_than_one_link_can_carry_get_no_link(view_on, caplog):
    c = desk_client()
    stored = []
    for k in range(viewpage.MAX_BATCHES + 1):
        d = distinct_drafts(1, seed=30 + k)[0]
        stored.append((d, core.commit_drafts(c, PROJ, [d], user_id=ME)["version_ids"][0]))
    _embed(c)
    with caplog.at_level(logging.WARNING, logger="deskcore.core"):
        out = core.commit_drafts(c, PROJ, [_revise(d, vid, str(k)) for k, (d, vid) in enumerate(stored)], user_id=ME)
    assert out["written"] == viewpage.MAX_BATCHES + 1 and "view" not in out
    assert core.VIEW_NOTE_NEXT_STEP not in out["next_step"]
    meta = _view_meta(c)
    assert (meta["issued"], meta["batches"], meta["why"]) == (False, viewpage.MAX_BATCHES + 1, "too_many_batches")
    assert any("不发链接" in r.getMessage() for r in caplog.records)


# ══════════════════════════════════════════════════════════════════════
# 4 · 发不出来、记不下来都不连累存档
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("env, why", [
    ({viewpage.SWITCH_ENV: "0"}, "switched_off"),
    ({viewpage.SECRET_ENV: "duan"}, "secret_too_short"),
    ({viewpage.PUBLIC_URL_ENV: "http://desk.example.com"}, "bad_public_url"),
    ({viewpage.PUBLIC_URL_ENV: ""}, "no_public_url"),
])
def test_when_the_feature_is_off_the_reply_is_what_it_was(view_on, monkeypatch, env, why):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    c = desk_client(default_params=_params_with(TABLE))
    drafts = _full(distinct_drafts(2, seed=5))
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert out["written"] == 2 and "view" not in out
    assert out["delivery"]["text"] == delivery.assemble(delivery.validate(TABLE), drafts)["text"]
    assert core.VIEW_IN_DELIVERY_NEXT_STEP not in out["next_step"] and "view.note" not in out["next_step"]
    assert _view_meta(c) == {"issued": False, "batches": 1, "batch8": [out["batch_id"][:8]], "why": why}

    plain = desk_client()
    got = core.commit_drafts(plain, PROJ, distinct_drafts(2, seed=6), user_id=ME)
    assert "view" not in got and core.VIEW_NOTE_NEXT_STEP not in got["next_step"]
    assert core.PLAIN_LAYOUT_NEXT_STEP in got["next_step"], "默认排法和页面开没开无关"


def test_without_any_address_nothing_changes_for_a_direct_call(monkeypatch):
    """用例和 CLI 直接调 core: 没配对外地址、也不在请求里 —— 不发, 回包照旧。"""
    for name in (viewpage.SECRET_ENV, viewpage.PUBLIC_URL_ENV, viewpage.SWITCH_ENV, viewpage.RAILWAY_DOMAIN_ENV):
        monkeypatch.delenv(name, raising=False)
    c = desk_client(default_params=_params_with(TABLE))
    drafts = _full(distinct_drafts(2, seed=5))
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert "view" not in out and out["delivery"]["text"] == delivery.assemble(delivery.validate(TABLE), drafts)["text"]
    assert _view_meta(c)["why"] == "no_public_url"


@pytest.mark.parametrize("broken", ["mint_raises", "link_raises", "link_is_garbage"])
def test_a_failing_link_never_fails_the_store(view_on, monkeypatch, caplog, broken):
    leak = f"{BASE}/v/bu-gai-chu-xian-zai-ri-zhi-li"
    if broken == "mint_raises":
        monkeypatch.setattr(viewpage, "mint", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError(leak)))
    elif broken == "link_raises":
        monkeypatch.setattr(core, "view_link", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError(leak)))
    else:
        monkeypatch.setattr(core, "view_link", lambda *_a, **_k: {"url": leak})      # 没有 note: 挂到一半出错
    c = desk_client(default_params=_params_with(TABLE))
    drafts = _full(distinct_drafts(2, seed=5))
    with caplog.at_level(logging.DEBUG):
        out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert out["written"] == 2 and len(out["version_ids"]) == 2 and "view" not in out
    assert out["delivery"]["text"] == delivery.assemble(delivery.validate(TABLE), drafts)["text"]
    assert "view.note" not in out["next_step"]
    meta = _view_meta(c)
    assert meta["issued"] is False and meta["why"] in ("not_issued", "error")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "页面链接没" in logged, "留一行日志"
    assert "/v/" not in logged and "/v/" not in json.dumps(c.rows["batch_metrics"], ensure_ascii=False)


def test_parts_that_cannot_be_recorded_do_not_fail_the_store(view_on, monkeypatch, caplog):
    monkeypatch.setattr(core, "_delivery_fields", lambda _d: (_ for _ in ()).throw(TypeError("记不下来")))
    c = desk_client()
    drafts = distinct_drafts(2, seed=9)
    with caplog.at_level(logging.WARNING, logger="deskcore.core"):
        out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert out["written"] == 2 and out["batch_id"] and len(out["version_ids"]) == 2
    assert core.DELIVERY_PARTS_KEY not in c.rows["batches"][0]["params"]
    assert any("没记下来" in r.getMessage() for r in caplog.records)
    assert sorted(_page_titles(c, out["view"])) == sorted(d["title"] for d in drafts), "页面照样出, 只是顺序按 item id"


def test_recording_a_revision_failing_still_stores_and_only_drops_the_link(view_on, monkeypatch):
    c = desk_client()
    drafts = distinct_drafts(2, seed=9)
    first = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    _embed(c)
    monkeypatch.setattr(store, "batch_ids_of_items", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("库一时读不出来")))
    out = core.commit_drafts(c, PROJ, [_revise(drafts[0], first["version_ids"][0], "零")], user_id=ME)
    assert out["written"] == 1 and out["replaced"] and "view" not in out
    assert "view" not in c.rows["batch_metrics"][-1]["meta"], "不知道在哪个 batch: 没有可发的"


def test_no_link_when_nothing_was_stored_or_no_draft_identity_was_built(view_on, monkeypatch):
    c = desk_client()
    out = core.commit_drafts(c, PROJ, [{"title": "只有标题", "body": ""}], user_id=ME)
    assert out["written"] == 0 and "view" not in out
    monkeypatch.setattr(store, "mint_draft_identity",
                        lambda *_a, **_k: {"batch_id": "aaaaaaaa-0000-0000-0000-0000000000b9", "versions": {},
                                           "error": "RuntimeError: 建不出来"})
    out = core.commit_drafts(c, PROJ, distinct_drafts(1, seed=3), user_id=ME)
    assert out["written"] == 1 and out["version_ids"] == [] and "view" not in out, "底稿没建成: 页面上什么都没有, 不发"


# ══════════════════════════════════════════════════════════════════════
# 5 · 链接和令牌不进日志、不进任何一张表
# ══════════════════════════════════════════════════════════════════════

def test_the_link_reaches_only_the_reply(view_on, caplog, capsys):
    c = desk_client(default_params=_params_with(TABLE))
    drafts = _full(distinct_drafts(3, seed=5))
    sink: dict = {}
    with caplog.at_level(logging.DEBUG):
        first = calllog.collecting(sink, core.commit_drafts, c, PROJ, drafts, user_id=ME)
        _embed(c)
        second = core.commit_drafts(c, PROJ, [_revise(drafts[1], first["version_ids"][1], "一")], user_id=ME)
    tokens = [_token(first["view"]), _token(second["view"])]
    logged = "\n".join(r.getMessage() for r in caplog.records)
    printed = "".join(capsys.readouterr())
    stored = json.dumps(c.rows, ensure_ascii=False, default=str)
    for text, where in ((logged, "日志"), (printed, "标准输出(埋点)"), (stored, "库里的任何一张表")):
        assert "/v/" not in text and "排好版的页面" not in text, where
        assert not any(t in text for t in tokens), where
    metas = [row["meta"]["view"] for row in c.rows["batch_metrics"]]
    assert metas == [{"issued": True, "batches": 1, "batch8": [first["batch_id"][:8]]}] * 2

    # 工具调用的留痕: 只有一个布尔
    row = calllog.build({"tool": "score_drafts", "channel": "rest", "user_id": ME,
                         "args": {"project_id": PROJ, "drafts": drafts}, "sink": sink, "elapsed_ms": 12, "result": first})
    assert sink == {"view": True} and row["meta"]["view"] is True
    assert not any(t in json.dumps(row, ensure_ascii=False) for t in tokens) and "/v/" not in json.dumps(row)


def test_the_tool_call_row_says_when_no_link_was_issued(view_on, monkeypatch):
    monkeypatch.setenv(viewpage.SWITCH_ENV, "0")
    c = desk_client()
    sink: dict = {}
    out = calllog.collecting(sink, core.commit_drafts, c, PROJ, distinct_drafts(1, seed=4), user_id=ME)
    row = calllog.build({"tool": "score_drafts", "channel": "mcp", "user_id": ME, "args": {"project_id": PROJ},
                         "sink": sink, "elapsed_ms": 5, "result": out})
    assert row["meta"]["view"] is False
    assert "view" not in calllog.build({"tool": "check_drafts", "channel": "mcp", "user_id": ME, "args": {},
                                        "sink": {}, "elapsed_ms": 5, "result": {}})["meta"]


# ══════════════════════════════════════════════════════════════════════
# 6 · 从头走一遍: 过 app 存档 → 拿链接 → 打开页面 → 改一篇 → 同一个链接是新的字
# ══════════════════════════════════════════════════════════════════════

KEY = "k-yemian-0123456789abcdefFAKE"                       # 现编的


@pytest.fixture()
def http(monkeypatch, view_on):
    testclient = pytest.importorskip("fastapi.testclient")
    import deskcore.app as A

    monkeypatch.setenv("DESKCORE_KEYS", json.dumps({KEY: {"user_id": ME, "name": "示例写手"}}, ensure_ascii=False))
    monkeypatch.delenv("DESKCORE_API_KEY", raising=False)
    monkeypatch.delenv("DESKCORE_ALLOW_ANONYMOUS", raising=False)
    box: dict = {}
    monkeypatch.setattr(core, "sb", lambda: box["db"])
    monkeypatch.setattr(A, "_view_client", lambda: box["db"])
    client = testclient.TestClient(A.app, raise_server_exceptions=False)

    def score(drafts, **headers):
        r = client.post("/tool/score_drafts", headers={"X-Deskcore-Key": KEY, **headers},
                        json={"project_id": PROJ, "drafts": drafts})
        assert r.status_code == 200, r.text[:300]
        return r.json()["result"]
    return client, box, score


def _path(view: dict, base: str = BASE) -> str:
    assert view["url"].startswith(base + "/v/")
    return view["url"][len(base):]


def _in_order(text: str, parts: list[str]) -> bool:
    at = [text.find(p) for p in parts]
    return -1 not in at and at == sorted(at)


def test_end_to_end_template_project_store_open_revise_refresh(http):
    client, box, score = http
    box["db"] = c = desk_client(default_params=_params_with(TABLE))
    drafts = _full(distinct_drafts(3, seed=5))
    first = score(drafts)
    assert first["written"] == 3 and first["delivery"]["text"].endswith("\n\n" + first["view"]["note"])
    r = client.get(_path(first["view"]))                   # 不带钥匙
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "示例项目" in r.text and "共 3 篇笔记" in r.text
    assert _in_order(r.text, [d["title"] for d in drafts])
    assert "方向0" in r.text and "示例品第1条" in r.text and "#话题2甲 #话题2乙 #固定甲" in r.text
    assert "后来补了一句一" not in r.text

    _embed(c)
    second = score([_revise(drafts[1], first["version_ids"][1], "一")])
    assert second["replaced"] and second["delivery"]["scope"] == "whole_batch"
    again = client.get(_path(first["view"]))               # 第一次拿到的那个链接, 刷新
    assert again.status_code == 200 and "后来补了一句一" in again.text and "改过的方向一" in again.text
    assert _in_order(again.text, [d["title"] for d in drafts]), "顺序不变"
    newer = client.get(_path(second["view"]))              # 改稿那次拿到的链接, 是同一页
    assert newer.status_code == 200 and "后来补了一句一" in newer.text and "共 3 篇笔记" in newer.text
    assert client.post("/tool/list_projects", json={}).status_code == 401, "别的照旧要钥匙"


def test_end_to_end_project_without_a_template_uses_the_request_address(http, monkeypatch):
    """没配对外地址: 链接用这次请求自己的地址(代理说是 https)。没有模板的项目照写手传的顺序排。"""
    client, box, score = http
    monkeypatch.delenv(viewpage.PUBLIC_URL_ENV, raising=False)
    box["db"] = c = desk_client()
    drafts = distinct_drafts(4, seed=21)
    drafts[2]["comments"] = [{"role": "reader", "text": "合成的一条读者评论"}]
    drafts[0]["tags"] = ["单独传的话题"]
    assert "view" not in score(distinct_drafts(1, seed=20)), "http://testserver: 不可信, 不发"
    first = score(drafts, **{"X-Forwarded-Proto": "https"})
    assert "delivery" not in first and core.PLAIN_LAYOUT_NEXT_STEP in first["next_step"]
    assert core.VIEW_NOTE_NEXT_STEP in first["next_step"]
    r = client.get(_path(first["view"], "https://testserver"))
    assert r.status_code == 200 and "共 4 篇笔记" in r.text
    assert _in_order(r.text, [d["title"] for d in drafts]), "照写手传的顺序, 不是 item id 的顺序"
    assert "读者：" in r.text and "合成的一条读者评论" in r.text and "#单独传的话题" in r.text

    _embed(c)
    revised = {**drafts[3], "title": "改过的第四篇标题", "body": drafts[3]["body"] + "改稿加的一句。",
               "replaces_version_id": first["version_ids"][3]}
    second = score([revised], **{"X-Forwarded-Proto": "https"})
    assert second["replaced"] and _batches(second["view"]) == _batches(first["view"])
    again = client.get(_path(first["view"], "https://testserver"))
    assert "改过的第四篇标题" in again.text and "改稿加的一句。" in again.text and drafts[3]["title"] not in again.text
    assert _in_order(again.text, [drafts[0]["title"], drafts[1]["title"], drafts[2]["title"], "改过的第四篇标题"])
