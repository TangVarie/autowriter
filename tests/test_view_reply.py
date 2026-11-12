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
  8. 复审之后(2026-11-12): 认那一行链接是线性时间、两头带几个字的装饰也认; 正文里带着那一行的存进去之前拿掉; 一次交付
     分几组存的, 后一组的链接带上前面几组; 没有模板的项目改稿只传标题正文, 话题 / 评论 / 方向沿用上一版; 没记顺序的 batch
     按每篇建的先后排; 「最近记的那一份」按版本号找。
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


def _decorated(note: str) -> dict[str, str]:
    """没有模板的项目那一行是模型自己打的: 两头可能多几个字的装饰。"""
    url = note.rsplit(" ", 1)[1]
    return {
        "句号": note + "。", "英文句点": note + ".", "引用": "> " + note, "列表项": "- " + note,
        "整行加粗": "**" + note + "**", "只把开头加粗": "**排好版的页面**" + note[len("排好版的页面"):],
        "markdown 链接": f"[排好版的页面(逐篇可复制, 改稿后刷新就是最新)]({url})",
        "链接写了两遍": note.replace(url, f"[{url}]({url})"), "尖括号": note.replace(url, f"<{url}>"),
        "反引号": note.replace(url, f"`{url}`"), "前面一个表情": "🔗 " + note, "外面一层引号": "「" + note + "」",
        "全角括号和冒号": note.replace("(", "（").replace(")", "）").replace(": ", "："), "两头的空格": "  " + note + "   ",
    }


def test_tv_sync_drops_the_note_line_with_a_few_characters_of_decoration(view_on):
    """WF-6: 多一个句号、一个「> 」、一对 ** 就认不出来的话, 链接又进了正文、指纹和改稿差异。"""
    out = core.commit_drafts(desk_client(), PROJ, distinct_drafts(1, seed=7), user_id=ME)
    post = "【标题】一个合成的标题\n【正文】第一段。\n第二段。\n#话题甲"
    want = tvlink.parse_raw(post)
    for name, line in _decorated(out["view"]["note"]).items():
        assert tvlink.parse_raw(post + "\n\n" + line) == want, name
        assert tvlink.parse_raw(post + "\n" + line + "\n") == want, name
    # 带四个 batch 的令牌是最长的: 一样认
    long_url = f"{BASE}/v/" + viewpage.make_token([f"aaaaaaaa-0000-0000-0000-0000000000b{k}" for k in range(1, 5)],
                                                 1_900_000_000, viewpage.derive_key(SECRET))
    for name, line in _decorated(viewpage.NOTE.format(url=long_url)).items():
        assert tvlink.parse_raw(post + "\n\n" + line) == want, name
    # 不是那一行的不动: 上一行只有一个「- 」的不被一起吃掉; 链接在下一行的、令牌后面还有一长串字的, 都不算那一行
    assert tvlink.parse_raw(post + "\n- \n\n" + out["view"]["note"]) == (want[0], want[1] + "\n-")
    for kept in ("排好版的页面在这里:\n" + long_url, out["view"]["note"] + " 后面还有一长串别的话不算那一行"):
        assert tvlink.parse_raw(post + "\n" + kept)[1].endswith(kept)


def test_recognising_the_note_line_costs_linear_time():
    """SEC-1: 那段正则跑在写手传来的整段文字上(comments_for / score_comments 的 post_body、tv-sync 读的飞书格子)。量词不封顶时
    下面第一段 60KB 要三秒半、100KB 十秒, 而且期间整个进程都得等。封了顶是线性的: 这里给的上限比实测宽 20 倍以上。"""
    head = "【标题】合成标题\n【正文】合成正文\n"
    hostile = [
        head + "排好版的页面" + "http://a/v/a" * 9000 + " x",                                        # 一行 108KB, 结尾对不上
        head + "\n".join("排好版的页面" + "http://" * 8 + ("/v/" + "a" * 20) * 20 + "!" * 30 for _ in range(200)),
        head + "\n".join("排好版的页面" + "http://" * 8 + "x/v/" + "a" * 159 + "/v/" + "a" * 150 + "!" * 30
                         for _ in range(280)),
    ]
    for text in hostile:
        assert len(text) > 100_000
        started = time.perf_counter()
        title, body = tvlink.parse_raw(text)
        assert time.perf_counter() - started < 0.5, f"{len(text)} 个字花了 {time.perf_counter() - started:.2f} 秒"
        assert title == "合成标题" and len(body) > 100_000, "对不上的那些行原样留着"
    assert viewpage._TOKEN_MAX_CHARS <= 160, "tvlink._VIEW_NOTE_RE 里令牌那一段的上限(160)得装得下最长的令牌"


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
    for part in ("「第N篇 · 这篇的场景(12 字以内)」", "【标题】接标题", "【正文】接正文", "话题接在正文后面单独占一行",
                 "篇与篇之间空一行", "不用表格", "标题和正文不加别的标记"):
        assert part in tip, part
    # WF-1: 话题不只是正文末尾的(工具说明叫模型放进 tags); 评论有它的位置, 标签照项目规则的叫法 —— 排法不能把规则定的
    # 评论标签也算成「别的标记」
    assert "传在 tags 里的和写在正文末尾的都算" in tip
    assert "评论排在这篇的最后" in tip and "一条一行" in tip and "标签照项目规则的叫法" in tip
    assert "; 不用表格, 不加别的标记" not in tip
    assert "comments / tags / direction" in core.VIEW_NOTE_NEXT_STEP
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
    # 默认排法里评论排在这篇的最后、和上面空一行。连评论一起贴进同一格时, 【正文】后面的字 tv-sync 都算正文(一篇一段的
    # 交付模板也是这样): 标题照样拆得出来, 正文开头不变, 评论那一行跟在一个空行后面
    with_comment = cell + "\n\n读者：合成的一条评论"
    title, body = tvlink.parse_raw(with_comment)
    assert title == "一个合成的标题" and body == "第一段。\n\n第二段。\n#话题甲 #话题乙\n\n读者：合成的一条评论"
    with_comments = {**BLOCKS, "comments": {"min": 0, "max": 3, "label_reader": "读者：", "label_author": "作者回复："}}
    blocks = delivery.assemble(delivery.validate(with_comments),
                               [{"title": "一个合成的标题", "body": "第一段。", "tags": ["话题甲"],
                                 "comments": [{"role": "reader", "text": "合成的一条评论"}]}])
    assert tvlink.parse_raw(blocks["text"])[1].endswith("读者：合成的一条评论"), "有模板的那种排法也一样"


def test_the_tool_description_asks_for_comments_tags_and_direction_before_the_call():
    """WF-3: 页面右边那一栏和小标签只来自每篇 draft 的 comments / direction。没有模板的项目简报里没有 delivery_requirements,
    那两句提示又是存完才到的 —— 调用之前只有工具说明能告诉模型把评论放进字段。"""
    from deskcore import tools
    doc = tools.score_drafts.__doc__
    para = doc.split("**排好版的页面**", 1)[1].split("**规则检查**", 1)[0]
    for part in ("``comments`` / ``tags`` / ``direction``", "没有交付模板的项目", "也照样放进这几个字段",
                 "``direction`` 写这篇的场景(12 字以内)"):
        assert part in para, part


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


# ══════════════════════════════════════════════════════════════════════
# 8 · 复审之后(2026-11-12)
# ══════════════════════════════════════════════════════════════════════

def _posts(c, view: dict) -> list[dict]:
    ids, exp = viewpage.open_token(_token(view), viewpage.derive_key(SECRET), time.time())
    return core.view_page_data(c, ids, exp)["posts"]


def _everything(c) -> str:
    return json.dumps(c.rows, ensure_ascii=False, default=str)


# ── SEC-3: 正文里带着那一行的, 存进去之前拿掉 ────────────────────────────────────────────────────

@pytest.mark.parametrize("tpl", [None, BLOCKS])
def test_a_body_that_carries_the_note_line_is_stored_without_it(view_on, tpl):
    """模型把交付稿最后一篇连着那一句一起摘了回来(或者从飞书格子里读回来)再存: 链接不进 versions、不进指纹, 页面上也不会
    多出一段。"""
    c = desk_client(default_params=_params_with(tpl)) if tpl else desk_client()
    first = core.commit_drafts(c, PROJ, distinct_drafts(2, seed=51), user_id=ME)
    note, token = first["view"]["note"], _token(first["view"])
    plain = distinct_drafts(1, seed=52)[0]
    out = core.commit_drafts(c, PROJ, [{**plain, "body": plain["body"] + "\n\n" + note + "\n"}], user_id=ME)
    assert out["written"] == 1 and not out.get("rejected")
    stored = _everything(c)
    assert token not in stored and "/v/" not in stored and "排好版的页面" not in stored
    assert c.rows["versions"][-1]["body"] == plain["body"], "存下的就是没有那一行的正文"
    # 指纹和不带那一行时一样
    other = desk_client()
    core.commit_drafts(other, PROJ, [plain], user_id=ME)
    fp_of = lambda rows: (rows[-1]["opening_hash"], rows[-1]["ngram_hashes"])          # noqa: E731
    assert fp_of(c.rows["draft_fingerprints"]) == fp_of(other.rows["draft_fingerprints"])
    post = _posts(c, out["view"])[-1]
    assert not any("排好版" in x or "/v/" in x for x in post["paragraphs"]) and "/v/" not in post["copy_post"]
    if tpl:
        assert out["delivery"]["text"].count("/v/") == 1, "交付稿里只有服务端接的那一句"


def test_a_body_without_the_note_line_is_stored_byte_for_byte(view_on):
    c = desk_client()
    d = distinct_drafts(1, seed=53)[0]
    body = d["body"] + "\n排好版的页面我也做过一个\n看这里 https://example.com/v/abc 还有别的\n  "
    drafts = [{**d, "body": body}]
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert out["written"] == 1 and c.rows["versions"][-1]["body"] == body
    assert core._drafts_without_view_note(drafts) is drafts, "一篇都没带时传进来的列表原样往下走"


def test_a_body_that_is_only_the_note_line_is_rejected_as_empty(view_on):
    c = desk_client()
    first = core.commit_drafts(c, PROJ, distinct_drafts(1, seed=54), user_id=ME)
    out = core.commit_drafts(c, PROJ, [{"title": "只带了那一行", "body": "\n" + first["view"]["note"] + "\n"}], user_id=ME)
    assert out["written"] == 0 and len(out["rejected"]) == 1 and "正文是空的" in out["rejected"][0]["reason"]
    assert "/v/" not in _everything(c)


def test_ingest_and_the_check_snapshot_drop_the_note_line_too(view_on):
    """补录(写手把交付稿整段贴进飞书, 那一行跟着最后一篇进了同一格)和查重时记的底稿, 同一个认法。"""
    c = desk_client()
    first = core.commit_drafts(c, PROJ, distinct_drafts(1, seed=55), user_id=ME)
    note, token = first["view"]["note"], _token(first["view"])
    a, b = distinct_drafts(2, seed=56)
    got = core.ingest_published(c, PROJ, [{"title": a["title"], "body": a["body"] + "\n\n" + note + "。"}],
                                user_id=ME, source="合成的表")
    assert got["minted"] == 1 and got["fingerprinted"] == 1
    assert c.rows["versions"][-1]["body"] == a["body"]
    core.check_drafts(c, PROJ, [{**b, "body": b["body"] + "\n" + note}], user_id=ME, record_snapshot=True)
    assert c.rows["draft_snapshots"][-1]["body"] == b["body"]
    stored = _everything(c)
    assert token not in stored and "/v/" not in stored


def test_when_dropping_the_note_line_fails_the_store_goes_on_and_logs_no_text(view_on, monkeypatch, caplog):
    c = desk_client()
    d = distinct_drafts(1, seed=57)[0]
    body = d["body"] + "\n\n排好版的页面(逐篇可复制, 改稿后刷新就是最新): https://desk.example.com/v/bu-gai-chu-xian-zai-ri-zhi-li"
    monkeypatch.setattr(tvlink, "strip_view_note", lambda _t: (_ for _ in ()).throw(RuntimeError(body)))
    with caplog.at_level(logging.DEBUG):
        out = core.commit_drafts(c, PROJ, [{**d, "body": body}], user_id=ME)
    assert out["written"] == 1 and c.rows["versions"][-1]["body"] == body, "摘不成就原样存, 不连累存档"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "没摘成(RuntimeError)" in logged and "/v/" not in logged and d["body"][:20] not in logged


# ── WF-2: 一次交付分几组存, 后一组的链接带上前面几组 ─────────────────────────────────────────────

@pytest.fixture()
def db_clock(monkeypatch):
    """真库的 batches.created_at 是 DEFAULT now(); 假库没有列默认值。这里补上: 每建一个 batch 盖上 ``clock["now"]``。"""
    clock = {"now": datetime.now(timezone.utc)}
    real = store.mint_draft_identity

    def minted(sb, *args, **kwargs):
        got = real(sb, *args, **kwargs)
        for row in sb.rows.get("batches", []):
            row.setdefault("created_at", clock["now"].isoformat())
        return got
    monkeypatch.setattr(store, "mint_draft_identity", minted)
    return clock


def _store_groups(c, clock, minutes_ago: list[float], *, size: int = 3, seed: int = 60) -> tuple[list[dict], list[list[dict]]]:
    """一组一组存(协议一次最多存 10 篇), 每组各建一个 batch; ``minutes_ago`` 是每组离现在多久。"""
    now = datetime.now(timezone.utc)
    outs, groups = [], []
    for k, ago in enumerate(minutes_ago):
        clock["now"] = now - timedelta(minutes=ago)
        drafts = _full(distinct_drafts(size, seed=seed + k), f"组{k}")
        outs.append(core.commit_drafts(c, PROJ, drafts, user_id=ME))
        groups.append(drafts)
        assert outs[-1]["written"] == size
    return outs, groups


@pytest.mark.parametrize("tpl", [None, TABLE])
def test_the_second_group_of_one_delivery_links_to_both_groups_numbered_through(view_on, db_clock, tpl):
    c = desk_client(default_params=_params_with(tpl)) if tpl else desk_client()
    (one, two), (g1, g2) = _store_groups(c, db_clock, [12, 0])
    assert _batches(one["view"]) == [one["batch_id"]], "第一组存的时候还没有第二组"
    assert _batches(two["view"]) == [one["batch_id"], two["batch_id"]], "第二组的链接带上第一组, 按建的先后"
    posts = _posts(c, two["view"])
    assert [p["n"] for p in posts] == [1, 2, 3, 4, 5, 6], "一页上连着编号, 不是各自从 1 数起"
    assert [p["title"] for p in posts] == [d["title"] for d in g1 + g2]
    assert _view_meta(c) == {"issued": True, "batches": 2, "batch8": [one["batch_id"][:8], two["batch_id"][:8]]}
    if tpl:
        text = two["delivery"]["text"]
        assert text.endswith("\n\n" + two["view"]["note"]) and text.count("/v/") == 1
        assert g1[0]["title"] not in text, "交付稿照旧只有这一组; 多带的只是页面"


def test_groups_chain_while_each_gap_is_short_and_an_older_batch_is_left_out(view_on, db_clock):
    c = desk_client()
    outs, groups = _store_groups(c, db_clock, [120, 50, 25, 0], size=2)
    old, a, b, last = outs
    assert _batches(a["view"]) == [a["batch_id"]], "70 分钟前的那一批不算同一次交付"
    assert _batches(b["view"]) == [a["batch_id"], b["batch_id"]]
    assert _batches(last["view"]) == [a["batch_id"], b["batch_id"], last["batch_id"]], "一个接一个, 每段不到半小时"
    assert [p["title"] for p in _posts(c, last["view"])] == [d["title"] for g in groups[1:] for d in g]
    assert old["batch_id"] not in _batches(last["view"])


def test_a_long_delivery_keeps_this_calls_batch_and_the_nearest_ones(view_on, db_clock):
    c = desk_client()
    outs, _groups = _store_groups(c, db_clock, [40, 30, 20, 10, 0], size=1)
    got = _batches(outs[-1]["view"])
    assert got == [o["batch_id"] for o in outs[1:]], "一个链接最多 4 个 batch: 这次的一定在, 剩下的给最近的"
    assert _view_meta(c)["batches"] == viewpage.MAX_BATCHES


def test_only_this_projects_desk_batches_count_as_the_same_delivery(view_on, db_clock):
    c = desk_client()
    now = datetime.now(timezone.utc)
    stamp = (now - timedelta(minutes=5)).isoformat()
    c.rows["batches"] = [
        {"id": "aaaaaaaa-0000-0000-0000-0000000000c1", "project_id": PROJ, "created_at": stamp, "params": {"source": "ingest"}},
        {"id": "aaaaaaaa-0000-0000-0000-0000000000c2", "project_id": PROJ, "created_at": stamp, "params": {}},
        {"id": "aaaaaaaa-0000-0000-0000-0000000000c3", "project_id": "bbbbbbbb-0000-0000-0000-0000000000d1",
         "created_at": stamp, "params": {"source": "deskcore"}},
    ]
    out = core.commit_drafts(c, PROJ, distinct_drafts(2, seed=70), user_id=ME)
    assert _batches(out["view"]) == [out["batch_id"]], "补录的、界面生成的、别的项目的都不带"
    asked = [q for q in c.calls if q["table"] == "batches" and any(f[0] == "gte" for f in q["filters"])]
    assert len(asked) == 1 and asked[0]["cols"] == "id, created_at" and asked[0]["range"] == (0, core.VIEW_SESSION_SCAN - 1)
    assert ("eq", "project_id", PROJ) in asked[0]["filters"] and ("eq", "params->>source", "deskcore") in asked[0]["filters"]


def test_a_revision_links_to_the_whole_delivery_it_belongs_to(view_on, db_clock):
    c = desk_client()
    (one, two), (g1, g2) = _store_groups(c, db_clock, [15, 5])
    _embed(c)
    out = core.commit_drafts(c, PROJ, [_revise(g1[1], one["version_ids"][1], "一")], user_id=ME)
    assert out["replaced"] and out["batch_id"] is None
    assert _batches(out["view"]) == [one["batch_id"], two["batch_id"]], "改第一组的一篇, 链接看的还是那一次交付的两组"
    posts = _posts(c, out["view"])
    assert len(posts) == 6 and "后来补了一句一。" in posts[1]["paragraphs"][-1]


def test_when_the_neighbour_lookup_fails_the_link_carries_this_calls_batch_only(view_on, db_clock, monkeypatch, caplog):
    c = desk_client()
    (one,), _g = _store_groups(c, db_clock, [10])
    leak = f"{BASE}/v/bu-gai-chu-xian-zai-ri-zhi-li"
    monkeypatch.setattr(store, "desk_batches_since", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError(leak)))
    db_clock["now"] = datetime.now(timezone.utc)
    drafts = distinct_drafts(2, seed=75)
    with caplog.at_level(logging.DEBUG):
        out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert out["written"] == 2 and len(out["version_ids"]) == 2
    assert _batches(out["view"]) == [out["batch_id"]], "和原来一样: 只带这次的"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "没查出同一次交付的前几批(RuntimeError" in logged and out["batch_id"][:8] in logged
    assert "/v/" not in logged and one["batch_id"] not in logged


def test_a_slow_neighbour_lookup_is_cut_off_and_the_store_goes_on(view_on, db_clock, monkeypatch, caplog):
    """PostgREST 默认等 120 秒: 库一卡, 这一次读不能让整次存档的回包跟着等。"""
    import threading
    c = desk_client()
    _store_groups(c, db_clock, [10])
    release = threading.Event()
    monkeypatch.setattr(store, "desk_batches_since", lambda *_a, **_k: release.wait(5) and [])
    monkeypatch.setattr(core, "VIEW_SESSION_TIMEOUT_SEC", 0.05)
    db_clock["now"] = datetime.now(timezone.utc)
    try:
        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="deskcore.core"):
            out = core.commit_drafts(c, PROJ, distinct_drafts(2, seed=76), user_id=ME)
            took = time.monotonic() - started
            assert out["written"] == 2 and _batches(out["view"]) == [out["batch_id"]] and took < 2
            assert any("没查出同一次交付的前几批(TimeoutError" in r.getMessage() for r in caplog.records)
            # 没收场的读最多 2 个: 再来两次, 第二次就不读了(占满), 照样存、照样发这次的
            more = [core.commit_drafts(c, PROJ, distinct_drafts(1, seed=77 + k), user_id=ME) for k in range(2)]
            assert all(o["written"] == 1 and _batches(o["view"]) == [o["batch_id"]] for o in more)
            assert any("没查出同一次交付的前几批(RuntimeError" in r.getMessage() for r in caplog.records)
    finally:
        release.set()
    for _ in range(100):                                    # 等那两个读收场, 把位置还回去, 不带到下一条用例
        if core._VIEW_SESSION_SLOTS.acquire(blocking=False):
            core._VIEW_SESSION_SLOTS.release()
            if core._VIEW_SESSION_SLOTS._value == 2:
                break
        time.sleep(0.02)
    assert core._VIEW_SESSION_SLOTS._value == 2


def test_no_neighbour_lookup_when_no_link_can_be_issued(view_on, db_clock, monkeypatch):
    monkeypatch.setenv(viewpage.SWITCH_ENV, "0")
    c = desk_client()
    outs, _g = _store_groups(c, db_clock, [10, 0])
    assert all("view" not in o for o in outs)
    assert not [q for q in c.calls if q["table"] == "batches" and any(f[0] == "gte" for f in q["filters"])], "关着就不多读这一次"


def test_view_session_batches_leaves_a_full_or_empty_list_alone(view_on):
    c = desk_client()
    full = [f"aaaaaaaa-0000-0000-0000-0000000000e{k}" for k in range(viewpage.MAX_BATCHES + 1)]
    assert core._view_session_batches(c, PROJ, full) == full and core._view_session_batches(c, PROJ, []) == []
    assert core._view_session_batches(c, PROJ, full[:viewpage.MAX_BATCHES]) == full[:viewpage.MAX_BATCHES]
    assert not c.calls, "这次的已经占满一个链接: 不读"


# ── DATA-1 / PAGE-1: 没有模板的项目, 改稿只传标题正文 ───────────────────────────────────────────

def test_a_title_and_body_only_revision_keeps_tags_comments_and_direction_without_a_template(view_on):
    """没有模板的项目没有 delivery.problems 提醒模型把三样再传一遍。原来新的那一版记的是一份空的: 页面上这一篇的小标签、
    话题、评论全没了, 「复制笔记」也少了话题那一行。"""
    c = desk_client()
    drafts = _full(distinct_drafts(3, seed=41))
    first = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    before = _posts(c, first["view"])[1]
    assert before["copy_post"].endswith("#话题1甲 #话题1乙") and before["comments"]
    _embed(c)
    out = core.commit_drafts(c, PROJ, [{"title": drafts[1]["title"], "body": drafts[1]["body"] + "改稿加的一句。",
                                        "replaces_version_id": first["version_ids"][1]}], user_id=ME)
    assert out["written"] == 1 and out["replaced"]
    after = _posts(c, out["view"])[1]
    assert "改稿加的一句。" in after["paragraphs"][-1]
    assert (after["direction"], after["tags"], after["comments"]) == (before["direction"], before["tags"], before["comments"])
    assert after["copy_post"].endswith("#话题1甲 #话题1乙") and after["copy_comments"] == before["copy_comments"]

    # 传了的照这次的: 传 tags: [] / comments: [] 是清空; 没传的 direction 接着沿用(隔了一版也一样)
    _embed(c)
    again = core.commit_drafts(c, PROJ, [{"title": drafts[1]["title"], "body": drafts[1]["body"] + "又改了一句。",
                                          "tags": [], "comments": [], "replaces_version_id": out["version_ids"][0]}],
                               user_id=ME)
    cleared = _posts(c, again["view"])[1]
    assert cleared["tags"] == [] and cleared["comments"] == [] and cleared["direction"] == before["direction"]
    assert cleared["copy_post"].endswith("又改了一句。")
    others = _posts(c, again["view"])
    assert others[0]["comments"] and others[2]["tags"], "别的篇不受影响"


def test_a_template_project_still_records_exactly_what_the_revision_passed(view_on):
    """有模板的项目不沿用: 页面要和 delivery.text 一字不差, 少传了的由 delivery.problems 报。"""
    c = desk_client(default_params=_params_with(TABLE))
    drafts = _full(distinct_drafts(2, seed=42))
    first = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    _embed(c)
    out = core.commit_drafts(c, PROJ, [{"title": drafts[0]["title"], "body": drafts[0]["body"] + "改稿加的一句。",
                                        "replaces_version_id": first["version_ids"][0]}], user_id=ME)
    parts = c.rows["batches"][0]["params"][core.DELIVERY_PARTS_KEY]["parts"]
    assert parts[out["version_ids"][0]] == {}
    assert out["delivery"]["problems"], "少传的那几样照旧报出来"
    assert _posts(c, out["view"])[0]["comments"] == []


# ── DATA-2: 没记顺序的 batch ─────────────────────────────────────────────────────────────────────

def test_a_batch_without_a_recorded_order_is_shown_in_the_order_its_posts_were_made(view_on):
    """这之前存的 batch 没记顺序。原来按 item id 排(id 是随机的 UUID, 等于乱序); 一批里的 item 是照写手传的顺序一篇一篇
    建的, 按 items.created_at 排就是当初的顺序。"""
    c = desk_client()
    drafts = distinct_drafts(6, seed=43)
    first = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    c.rows["batches"][0]["params"].pop(core.DELIVERY_PARTS_KEY)
    item_of = {v["id"]: v["item_id"] for v in c.rows["versions"]}
    made = [item_of[vid] for vid in first["version_ids"]]                  # 当初建的先后
    by_id = sorted(made)
    # 让 id 的顺序正好和建的先后相反: 换 id 太绕, 直接把时间按 id 倒着盖
    stamps = {item: f"2026-11-12T08:00:{59 - k:02d}+00:00" for k, item in enumerate(by_id)}
    for it in c.rows["items"]:
        it["created_at"] = stamps[it["id"]]
    title_of_item = {item_of[vid]: d["title"] for vid, d in zip(first["version_ids"], drafts)}
    want = [title_of_item[item] for item in reversed(by_id)]
    assert want != [title_of_item[item] for item in by_id]
    assert _page_titles(c, first["view"]) == want, "按每篇建的先后, 不是 item id"


def test_a_revision_on_an_unrecorded_batch_does_not_start_a_record(view_on):
    """没记过顺序的旧 batch 改稿时照旧不补记(没有当初的顺序, 整批拼不回来; 也不往原来没有这一份的 batch 里加): 页面照样出,
    改过的那一篇是新的字, 只是没有评论和小标签。"""
    c = desk_client()
    drafts = distinct_drafts(3, seed=44)
    first = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    c.rows["batches"][0]["params"].pop(core.DELIVERY_PARTS_KEY)             # 这之前存的 batch
    _embed(c)
    out = core.commit_drafts(c, PROJ, [{**drafts[1], "body": drafts[1]["body"] + "改稿加的一句。", "direction": "改稿时传的场景",
                                        "replaces_version_id": first["version_ids"][1]}], user_id=ME)
    assert out["written"] == 1 and core.DELIVERY_PARTS_KEY not in c.rows["batches"][0]["params"]
    posts = _posts(c, out["view"])
    assert len(posts) == 3 and any("改稿加的一句。" in p["paragraphs"][-1] for p in posts)
    assert all(p["direction"] == "" for p in posts)


# ── DATA-3: 「最近记的那一份」按版本号找 ─────────────────────────────────────────────────────────

def test_the_fallback_part_is_the_one_of_the_highest_numbered_recorded_version(view_on):
    """现在那一版没记话题 / 评论(别的入口改出来的一版)时用这一篇最近记过的那一份。version 的 id 是随机的 UUID: 按 id 倒着找,
    找到的是随便哪一版。这里把 id 的顺序和版本号的顺序做成相反的。"""
    batch, item = "aaaaaaaa-0000-0000-0000-0000000000f1", "aaaaaaaa-0000-0000-0000-0000000000f2"
    v1, v2, v3, v4 = (f"{h}0000000-0000-0000-0000-00000000000{k}" for k, h in ((1, "f"), (2, "9"), (3, "0"), (4, "5")))
    c = desk_client()
    part = lambda n: {"comments": [{"role": "reader", "text": f"第{n}版记的评论"}], "direction": f"第{n}版的方向"}  # noqa: E731
    c.rows["batches"] = [{"id": batch, "project_id": PROJ, "created_at": "2026-11-12T02:00:00+00:00",
                          "params": {"source": "deskcore",
                                     core.DELIVERY_PARTS_KEY: {"order": [v1], "parts": {v1: part(1), v2: part(2), v3: part(3)}}}}]
    c.rows["items"] = [{"id": item, "batch_id": batch, "best_version_id": None}]
    c.rows["versions"] = [{"id": v, "item_id": item, "version_num": n, "title": f"第{n}版的标题", "body": f"第{n}版的正文"}
                          for n, v in ((1, v1), (2, v2), (3, v3), (4, v4))]
    assert sorted([v1, v2, v3]) == [v3, v2, v1], "id 的顺序和版本号相反"
    info = store.batch_drafts(c, PROJ, [batch])[batch]["items"][0]
    assert info["version_ids"] == [v1, v2, v3, v4], "按版本号从旧到新"
    post = core.view_page_data(c, [batch], int(time.time()) + 3600)["posts"][0]
    assert post["title"] == "第4版的标题" and post["direction"] == "第3版的方向"
    assert [x["text"] for x in post["comments"]] == ["第3版记的评论"]
