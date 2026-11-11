"""2026-11-11 晚 owner: 「配比也开放吧」—— 写手在对话里改项目一组评论的配比(set_comment_mix, docs/deskcore.md §3.23)。

钉住:
  1. 改的是 ``default_params.comment_set`` 里的四个数(brand_max / neutral_min / short_min / short_chars); 回包说清改之前 →
     改之后(summary 原文)、带改完之后的那几句(和 comments_for 回的同一个函数拼的);
  2. 几成认小数、百分数、带百分号的字, 存的是整数百分比对应的小数; 边界(0、1、100、6、40)和越界的都钉;
  3. 值是 null 去掉那一项; 四个数都去掉、项目也没配列 → comment_set 整个拿掉; 没有 comment_set 的项目能新建(不带列);
  4. 列(column)改不了: mix 里带它就报一句、什么都不写; 库里原来的列一个字不动;
  5. 校验用的是读的时候同一个(``core._validate_comment_set``): 工具收下的数, 下一次 comments_for / score_comments 不会当成
     「配置不对」; 库里原来就有、这里改不了的毛病 → 不写, 报一句;
  6. ``default_params`` 仍是 JSON 字符串, 别的键原样、顺序不变; 解析不了就不写;
  7. 留痕 ``comment_set_log``: 新的在前、最多 20 条、{at, by, before, after}; by 是钥匙的备注名(app 注入, 调用方传不进来),
     不是钥匙; 和现在一样的不写、不留痕;
  8. 注册在 TOOLS 里, 说明里写清只在用户明说时调、不为了让检查通过去改; 写手看得到的话里没有工程指令。
REST / MCP / /tools / /health 那一层在 tests/test_set_comment_mix_http.py(要 fastapi)。
全部是合成项目、合成品牌和假列名。
"""
from __future__ import annotations

import inspect
import json
import re

import pytest

from deskcore import core, store, tools
from tests.test_comment_set_score import BRANDED, CFG, COL, PLAIN, _acts, _client, _judge, _score
from tests.test_deskcore_judge_tools import wired  # noqa: F401 — fixture
from tests.test_deskcore_score_and_comments import LINK, ME
from tests.test_writer_facing_text import FORBIDDEN

AT = "2026-11-11T08:00:00+00:00"
BY = "示例写手"
OTHER_KEYS = {"delivery": {"layout": "blocks"}, "isolation_terms": ["示例词"], "archived": True}


def _pid(c):
    return c.rows["projects"][0]["id"]


def _params(c) -> dict:
    raw = c.rows["projects"][0]["default_params"]
    assert isinstance(raw, str), "default_params 要一直是 JSON 字符串(生产库这一列全是这个形状)"
    return json.loads(raw)


def _set(c, mix, by=BY, at=AT):
    return core.set_comment_mix(c, _pid(c), user_id=ME, mix=mix, by=by, at=at)


def _writes(c):
    return [x for x in c.calls if x["op"] != "select"]


# ── 1. 改之前 → 改之后 ───────────────────────────────────────────────────────────────────────────

def test_changing_one_number_reports_before_after_and_the_new_lines():
    c = _client()
    out = _set(c, {"brand_max": 0.6})
    assert out["changed"] is True and out["project_name"] == "示例项目"
    assert out["before"] == {"brand_max": 0.5, "neutral_min": 0.3, "short_min": 0.2, "short_chars": 15}
    assert out["after"] == {"brand_max": 0.6, "neutral_min": 0.3, "short_min": 0.2, "short_chars": 15}
    assert out["summary"] == "「示例项目」一组评论的配比改了: 提品牌的上限 50% → 60%。"
    cfg, note = core._comment_set(c.rows["projects"][0])
    assert note is None and cfg["brand_max"] == 0.6
    assert out["comment_set"] == {"lines": core._comment_set_lines(cfg), "column": COL}
    assert out["comment_set"]["lines"][1].startswith("一组里提到品牌的最多 60%(20 条最多 12 条")
    assert out["next_step"] == core.MIX_NEXT_STEP and "comment_set_note" not in out and "warning" not in out
    assert _params(c)["comment_set"] == dict(CFG, brand_max=0.6), "没改的几项和列一个字不动"


def test_several_numbers_in_one_call_are_listed_in_a_fixed_order():
    c = _client()
    out = _set(c, {"short_chars": 12, "short_min": None, "neutral_min": 20, "brand_max": "70%"})
    assert out["summary"] == ("「示例项目」一组评论的配比改了: 提品牌的上限 50% → 70%; 中性评论的下限 30% → 20%; "
                              "一句话短评的下限 20% → 没定(不数); 一句话短评的字数 15 字 → 12 字。")
    assert _params(c)["comment_set"] == {"brand_max": 0.7, "neutral_min": 0.2, "short_chars": 12, "column": COL}
    assert not any("一句话短评" in x for x in out["comment_set"]["lines"]), "短评那一项去掉了, 那一句也没了"


def test_comments_for_and_score_comments_use_the_new_numbers(wired):
    """改完之后真的按新的数: 12 / 20 提品牌, 上限 50% 时报超出, 改成 60% 就不报了。"""
    _, reply = wired
    texts = BRANDED + PLAIN
    reply["fn"] = _judge(_acts(texts))
    c = _client()
    assert _score(c, texts)["thread_fix"][0].startswith(core.SET_LINE_HEAD + "提品牌的 12 条, 最多 10 条")
    _set(c, {"brand_max": 60})
    after = _score(c, texts)
    assert not any("提品牌的" in x for x in after.get("thread_fix", [])), after.get("thread_fix")
    lines = core.comments_for(c, user_id=ME, link=LINK)["comment_set"]["lines"]
    assert lines[1].startswith("一组里提到品牌的最多 60%(20 条最多 12 条")


# ── 2. 几成怎么写 ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("given, stored, shown", [
    (0.4, 0.4, "40%"), (40, 0.4, "40%"), ("40%", 0.4, "40%"), ("40", 0.4, "40%"), ("0.4", 0.4, "40%"), ("40％", 0.4, "40%"),
    (0.25, 0.25, "25%"), (33.3, 0.33, "33%"), (0.333, 0.33, "33%"),
    (0, 0.0, "0%"), (0.0, 0.0, "0%"), ("0%", 0.0, "0%"),
    (1, 1.0, "100%(等于不限)"), (1.0, 1.0, "100%(等于不限)"), (100, 1.0, "100%(等于不限)"), ("100%", 1.0, "100%(等于不限)"),
    ("1%", 0.01, "1%"), (0.01, 0.01, "1%"), (2, 0.02, "2%"), (1.5, 0.02, "2%"),
])
def test_shares_accept_fractions_and_percents(given, stored, shown):
    c = _client(cfg={"column": COL})                     # 只有列: brand_max 随便取, 不和中性的下限打架
    out = _set(c, {"brand_max": given})
    assert _params(c)["comment_set"]["brand_max"] == stored and isinstance(_params(c)["comment_set"]["brand_max"], float)
    assert out["summary"] == f"「示例项目」一组评论的配比改了: 提品牌的上限 没定(不数) → {shown}。"
    assert core._comment_set(c.rows["projects"][0])[1] is None, "收下的数读的时候也认"


@pytest.mark.parametrize("bad", [-0.01, -1, 100.5, 101, 1000, "150%", "-5%", "abc", "", "五成", True, False,
                                 float("nan"), float("inf"), [0.5], {"v": 0.5}])
def test_shares_out_of_range_or_not_numbers_are_refused_without_writing(bad):
    c = _client()
    with pytest.raises(core.CommentMixError) as exc:
        _set(c, {"brand_max": bad})
    assert str(exc.value).startswith("提品牌的上限(brand_max)要") and "小数(0.5)或百分数(50、\"50%\")都行" in str(exc.value)
    assert _writes(c) == [] and _params(c) == {"comment_set": CFG}


def test_a_floor_of_zero_is_shown_as_not_required():
    c = _client()
    out = _set(c, {"neutral_min": 0, "short_min": "0%"})
    assert out["summary"] == ("「示例项目」一组评论的配比改了: 中性评论的下限 30% → 0%(等于不要求); "
                              "一句话短评的下限 20% → 0%(等于不要求)。")
    assert _params(c)["comment_set"]["neutral_min"] == 0.0


@pytest.mark.parametrize("given, stored", [(6, 6), (40, 40), (15.0, 15), ("12", 12), (" 20 ", 20)])
def test_short_chars_bounds_are_accepted(given, stored):
    c = _client()
    _set(c, {"short_chars": given})
    assert _params(c)["comment_set"]["short_chars"] == stored and type(_params(c)["comment_set"]["short_chars"]) is int


@pytest.mark.parametrize("bad", [5, 41, 0, -15, 15.5, "15.0", "十五", True, [15]])
def test_short_chars_out_of_bounds_are_refused(bad):
    c = _client()
    with pytest.raises(core.CommentMixError) as exc:
        _set(c, {"short_chars": bad})
    assert str(exc.value) == f"一句话短评的字数(short_chars)要是 6~40 的整数, 现在是 {bad!r}。"
    assert _writes(c) == []


def test_brand_max_plus_neutral_min_cannot_exceed_the_whole_set():
    c = _client()
    with pytest.raises(core.CommentMixError) as exc:
        _set(c, {"brand_max": 0.71})                     # 库里中性的下限是 30%
    assert str(exc.value) == ("提品牌的上限(71%)加中性评论的下限(30%)超过了 100%, 一组评论没法同时满足(中性评论是不提品牌的): "
                              "把其中一个调低, 或把中性的下限去掉(值写 null), 再调一次。")
    assert _writes(c) == []
    assert _set(c, {"brand_max": 0.7})["changed"] is True, "刚好 100% 可以"
    assert _set(c, {"brand_max": 1, "neutral_min": 0})["changed"] is True, "两项一起改, 按改完的算"
    with pytest.raises(core.CommentMixError) as exc:
        _set(c, {"neutral_min": 1})                      # 现在提品牌的上限是 100%
    assert str(exc.value).startswith("提品牌的上限(100%)加中性评论的下限(100%)超过了 100%")
    with pytest.raises(core.CommentMixError):
        _set(c, {"brand_max": 1, "neutral_min": 0.01})   # 100% + 1%
    out = _set(c, {"brand_max": 1, "neutral_min": None})
    assert out["changed"] is True and "neutral_min" not in _params(c)["comment_set"], "把中性的下限去掉也行"
    assert out["summary"] == "「示例项目」一组评论的配比改了: 中性评论的下限 0%(等于不要求) → 没定(不数)。"


def test_a_value_that_shows_the_same_keeps_what_is_stored():
    """0.5 和 50 是同一个数: 不写、不留痕; 库里原来写的 0.333 和 33% 也是同一个数。"""
    c = _client(cfg=dict(CFG, brand_max=0.333))
    out = _set(c, {"brand_max": 33, "neutral_min": "30%", "short_chars": 15})
    assert out["changed"] is False and out["summary"] == "「示例项目」的配比和现在的一样, 没有改。"
    assert out["next_step"] == core.MIX_UNCHANGED_NEXT_STEP and out["before"] == out["after"]
    assert out["comment_set"]["column"] == COL and out["comment_set"]["lines"][0] == core.COMMENT_SET_HEAD
    assert _writes(c) == [] and core.COMMENT_SET_LOG_KEY not in _params(c)


# ── 3. 去掉一项、新建 ────────────────────────────────────────────────────────────────────────────

def test_null_removes_a_key_and_missing_keys_are_left_alone():
    c = _client()
    out = _set(c, {"short_min": None})
    assert _params(c)["comment_set"] == {"brand_max": 0.5, "neutral_min": 0.3, "short_chars": 15, "column": COL}
    assert out["after"]["short_min"] is None and out["summary"].endswith("一句话短评的下限 20% → 没定(不数)。")
    assert _set(c, {"short_min": None})["changed"] is False, "本来就没有: 不算改"


def test_removing_every_number_keeps_the_column_and_says_nothing_is_counted():
    c = _client()
    out = _set(c, {k: None for k in core.MIX_KEYS})
    assert _params(c)["comment_set"] == {"column": COL}
    assert out["comment_set"]["column"] == COL and out["comment_set_note"] == core.MIX_CLEARED_NOTE
    assert core.MIX_CLEARED_NOTE == ("这个项目现在没有定配比: 一组评论里提品牌、中性、短评的条数不再数, "
                                     "所有项目都管的写法底线照旧。")
    assert core._comment_set(c.rows["projects"][0]) == ({"brand_max": None, "neutral_min": None, "short_min": None,
                                                         "short_chars": 15, "column": COL}, None)


def test_removing_every_number_without_a_column_drops_the_whole_comment_set():
    c = _client(cfg={"brand_max": 0.5, "short_min": 0.2})
    out = _set(c, {"brand_max": None, "short_min": None})
    params = _params(c)
    assert core.COMMENT_SET_KEY not in params, "空对象过不了校验, 不留空壳"
    assert out["comment_set"] is None and out["comment_set_note"] == core.MIX_CLEARED_NOTE
    assert params[core.COMMENT_SET_LOG_KEY][0]["after"] is None
    assert core._comment_set(c.rows["projects"][0]) == (None, None)


@pytest.mark.parametrize("default_params", ["{}", "", None, {}])
def test_a_project_without_a_comment_set_gets_one_with_only_the_given_numbers(default_params):
    c = _client(cfg=None)
    c.rows["projects"][0]["default_params"] = default_params
    out = _set(c, {"brand_max": 0.5, "short_min": 20})
    assert _params(c)["comment_set"] == {"brand_max": 0.5, "short_min": 0.2}, "不带列, 也不替用户补别的数"
    assert out["before"] == dict.fromkeys(core.MIX_KEYS) and out["comment_set"]["column"] is None
    assert out["summary"] == ("「示例项目」一组评论的配比改了: 提品牌的上限 没定(不数) → 50%; "
                              "一句话短评的下限 没定(不数) → 20%; 一句话短评的字数 没定 → 15 字(默认)。")
    assert _params(c)[core.COMMENT_SET_LOG_KEY][0]["before"] is None


# ── 4. 列改不了 ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mix", [{"column": "另一列"}, {"column": COL}, {"column": None},
                                 {"brand_max": 0.6, "column": "另一列"}])
def test_the_column_cannot_be_changed_here(mix):
    c = _client()
    with pytest.raises(core.CommentMixError) as exc:
        _set(c, mix)
    assert str(exc.value) == core.MIX_COLUMN_REFUSED == (
        "评论贴进飞书哪一列在这里改不了: 它和发布记录里数评论的那一列是绑着的, 换了那边就数不到了。"
        "这次什么都没改; 用户要换列, 请用户找负责人。配比的四个数可以改, 去掉 column 再调一次。")
    assert _writes(c) == [] and _params(c) == {"comment_set": CFG}, "连一起带的 brand_max 也没改"


@pytest.mark.parametrize("mix, starts", [
    ({}, "mix 里没有要改的"), (None, "mix 里没有要改的"), ([], "mix 里没有要改的"), ("brand_max=0.6", "mix 里没有要改的"),
    ({"brand": 0.6}, "mix 里不认识 brand, 什么都没改。"), ({"brand_max": 0.6, "max_named": 3}, "mix 里不认识 max_named"),
    ({"comment_set_log": []}, "mix 里不认识 comment_set_log"), ({"archived": True}, "mix 里不认识 archived"),
])
def test_empty_or_unknown_keys_are_refused(mix, starts):
    c = _client()
    with pytest.raises(core.CommentMixError) as exc:
        _set(c, mix)
    assert str(exc.value).startswith(starts) and str(exc.value).endswith(core.MIX_KEYS_HELP)
    assert _writes(c) == []


# ── 5. 同一个校验 ────────────────────────────────────────────────────────────────────────────────

def test_the_tool_goes_through_the_validator_the_readers_use(monkeypatch):
    """换掉 ``_validate_comment_set`` 工具就写不进去: 收下的数和读的时候认的数是同一道闸。"""
    c = _client()

    def _refuse(raw):
        raise core._CommentSetError("示例: 校验不过")
    monkeypatch.setattr(core, "_validate_comment_set", _refuse)
    with pytest.raises(core.CommentMixError) as exc:
        _set(c, {"brand_max": 0.6})
    assert str(exc.value) == core.MIX_EXISTING_BAD and _writes(c) == []


@pytest.mark.parametrize("mix", [{"brand_max": 0.6}, {"brand_max": 1, "neutral_min": 0}, {"neutral_min": 0},
                                 {"brand_max": 100, "neutral_min": None}, {"short_min": "35%"},
                                 {"short_chars": 6}, {"short_chars": 40}, {"brand_max": None, "neutral_min": 1},
                                 {"brand_max": 0.7, "neutral_min": 0.3, "short_min": 1, "short_chars": 40}])
def test_whatever_the_tool_stores_is_never_later_treated_as_bad_config(mix):
    c = _client()
    _set(c, mix)
    cfg, note = core._comment_set(c.rows["projects"][0])
    assert note is None and cfg is not None
    assert core._validate_comment_set(_params(c)["comment_set"]) == cfg


@pytest.mark.parametrize("existing", [dict(CFG, column="甲|乙"), dict(CFG, column=""), dict(CFG, extra=1),
                                      dict(CFG, short_chars=99), dict(CFG, neutral_min="三成"), "不是对象", [1]])
def test_an_existing_problem_this_tool_cannot_fix_is_not_written_over(existing):
    c = _client(cfg=existing)
    before = c.rows["projects"][0]["default_params"]
    with pytest.raises(core.CommentMixError) as exc:
        _set(c, {"brand_max": 0.6})
    assert str(exc.value) == core.MIX_EXISTING_BAD == (
        "这个项目现有的评论设置里有一项不是这里能改的, 配比没有改。请用户找负责人看一下, 不用重试; "
        "这一组评论照 comments_for 这次回的写。")
    assert _writes(c) == [] and c.rows["projects"][0]["default_params"] == before


def test_an_existing_bad_number_can_be_fixed_by_setting_it():
    c = _client(cfg=dict(CFG, brand_max=7))               # 读的时候: 配置不对, 按没配处理
    assert core._comment_set(c.rows["projects"][0])[0] is None
    out = _set(c, {"brand_max": 0.5})
    assert out["changed"] is True and out["summary"].endswith("提品牌的上限 读不出来 → 50%。")
    assert core._comment_set(c.rows["projects"][0])[1] is None


# ── 6. default_params 的形状 ─────────────────────────────────────────────────────────────────────

def test_other_keys_keep_their_values_and_their_order():
    c = _client()
    before = {"isolation_terms": ["示例词"], "comment_set": CFG, "delivery": {"layout": "blocks"}, "archived": True}
    c.rows["projects"][0]["default_params"] = json.dumps(before)
    _set(c, {"brand_max": 0.6})
    after = _params(c)
    assert list(after) == ["isolation_terms", "comment_set", "delivery", "archived", core.COMMENT_SET_LOG_KEY]
    for k in OTHER_KEYS:
        assert after[k] == before[k]
    (call,) = _writes(c)
    assert call["table"] == "projects" and call["op"] == "update" and list(call["payload"]) == ["default_params"]
    assert isinstance(call["payload"]["default_params"], str), "写回去的是 JSON 字符串, 不是 dict"
    assert "测试列" in call["payload"]["default_params"], "中文原样存, 不转成 \\u"


def test_a_dict_shaped_default_params_is_read_and_written_back_as_a_string():
    c = _client()
    c.rows["projects"][0]["default_params"] = {"comment_set": dict(CFG), "archived": True}
    _set(c, {"short_min": 0.1})
    assert _params(c)["comment_set"]["short_min"] == 0.1 and _params(c)["archived"] is True


@pytest.mark.parametrize("broken", ["{not json", "[1, 2]", "\"just a string\"", 7])
def test_unreadable_default_params_are_not_written_over(broken):
    c = _client()
    c.rows["projects"][0]["default_params"] = broken
    with pytest.raises(core.CommentMixError) as exc:
        _set(c, {"brand_max": 0.6})
    assert str(exc.value) == core.MIX_UNREADABLE == "这个项目的设置现在读不出来, 配比没有改。请用户找负责人看一下, 不用重试。"
    assert _writes(c) == [] and c.rows["projects"][0]["default_params"] == broken


def test_a_write_that_did_not_land_is_reported_as_not_changed(monkeypatch):
    c = _client()
    monkeypatch.setattr(store, "set_project_default_params", lambda *a, **k: None)
    out = _set(c, {"brand_max": 0.6})
    assert out["changed"] is False and out["warning"] == out["summary"] == core.MIX_NOT_STORED
    assert out["after"] == out["before"], "回的是库里实际的数, 不是要改成的"
    assert "comment_set" not in out and "next_step" not in out


# ── 7. 留痕 ──────────────────────────────────────────────────────────────────────────────────────

def test_the_log_entry_has_who_when_before_after():
    c = _client()
    _set(c, {"brand_max": 0.6})
    assert _params(c)[core.COMMENT_SET_LOG_KEY] == [
        {"at": AT, "by": BY, "before": CFG, "after": dict(CFG, brand_max=0.6)}]


def test_the_log_is_newest_first_and_capped_at_twenty():
    c = _client(cfg={"column": COL})
    for i in range(23):
        _set(c, {"brand_max": (i + 1) / 100}, at=f"2026-11-11T08:00:{i:02d}+00:00", by=f"示例写手{i}")
    log = _params(c)[core.COMMENT_SET_LOG_KEY]
    assert len(log) == core.COMMENT_SET_LOG_MAX == 20
    assert [e["by"] for e in log] == [f"示例写手{i}" for i in range(22, 2, -1)], "新的在前, 最早的三条掉了"
    assert log[0]["after"]["brand_max"] == 0.23 and log[0]["before"]["brand_max"] == 0.22
    assert log[-1]["at"] == "2026-11-11T08:00:03+00:00"


def test_a_log_that_is_not_a_list_is_started_over_and_no_name_falls_back_to_the_id_prefix():
    c = _client()
    c.rows["projects"][0]["default_params"] = json.dumps({"comment_set": CFG, core.COMMENT_SET_LOG_KEY: "坏的"})
    _set(c, {"brand_max": 0.6}, by="")
    (entry,) = _params(c)[core.COMMENT_SET_LOG_KEY]
    assert entry["by"] == "user 11111111" and set(entry) == {"at", "by", "before", "after"}


def test_the_tool_layer_stamps_utc_time_and_the_injected_name(monkeypatch):
    c = _client()
    monkeypatch.setattr(core, "sb", lambda: c)
    out = tools.set_comment_mix(_pid(c), {"brand_max": 0.6}, _user_id=ME, _caller_name="  示例写手  ")
    assert out["changed"] is True
    (entry,) = _params(c)[core.COMMENT_SET_LOG_KEY]
    assert entry["by"] == "示例写手"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", entry["at"]), entry["at"]
    tools.set_comment_mix(_pid(c), {"brand_max": 0.5}, _user_id=ME, _caller_name="长" * 100)
    assert _params(c)[core.COMMENT_SET_LOG_KEY][0]["by"] == "长" * 40


def test_the_tool_layer_turns_refusals_into_invalid_input_and_does_not_swallow(monkeypatch):
    c = _client()
    monkeypatch.setattr(core, "sb", lambda: c)
    with pytest.raises(tools.InvalidInput) as exc:
        tools.set_comment_mix(_pid(c), {"column": "另一列"}, _user_id=ME)
    assert str(exc.value) == core.MIX_COLUMN_REFUSED
    with pytest.raises(PermissionError):
        tools.set_comment_mix(_pid(c), {"brand_max": 0.6}, _user_id="22222222-2222-2222-2222-222222222222")
    with pytest.raises(core.ProjectNotFound):
        tools.set_comment_mix("no-such-project", {"brand_max": 0.6}, _user_id=ME)
    assert "_safe(" not in inspect.getsource(tools.set_comment_mix).split('"""')[2], "写: 失败要冒泡"
    assert _writes(c) == []


# ── 8. 注册、说明、写手看得到的话 ────────────────────────────────────────────────────────────────

def test_registered_with_identity_and_a_self_explanatory_signature():
    fn, needs_user = tools.TOOLS["set_comment_mix"]
    assert fn is tools.set_comment_mix and needs_user is True
    assert list(inspect.signature(fn).parameters) == ["project_id", "mix", "_user_id", "_caller_name"]
    assert "column" not in inspect.signature(fn).parameters and "by" not in inspect.signature(fn).parameters


def test_the_docstring_says_only_when_the_user_asks_and_never_to_pass_a_check():
    doc = tools.set_comment_mix.__doc__
    assert doc.startswith("改这个项目一组评论的配比(comments_for 回的 comment_set 里那几个数)。"
                          "**只在用户明说要改这个项目的配比时调。**")
    for must in ("什么时候调:", "什么时候不调:", "用户只是这一组想要别的配比", "这一组照用户说的写, 交付时一句话说明",
                 "**不为了让检查通过去改配比**", "也不替用户决定要改", "先问一句", "不用找负责人",
                 "brand_max", "neutral_min", "short_min", "short_chars", "6~40", "写 1 是全部(100%)",
                 "值写 null 是把这一项去掉", "不能超过 100%", "这里改不了", "**原样告诉用户**", "20 次的记录",
                 "同一次把 neutral_min 写 0 或 null"):
        assert must in doc, must
    for name in core.MIX_KEYS:
        assert f"· {name} " in doc


MIX_TEXTS = {name: getattr(core, name) for name in (
    "MIX_KEYS_HELP", "MIX_EMPTY", "MIX_COLUMN_REFUSED", "MIX_UNREADABLE", "MIX_EXISTING_BAD", "MIX_SUM", "MIX_UNCHANGED",
    "MIX_SUMMARY", "MIX_NOT_STORED", "MIX_CLEARED_NOTE", "MIX_NEXT_STEP", "MIX_UNCHANGED_NEXT_STEP")}
MIX_TEXTS["tools.set_comment_mix"] = tools.set_comment_mix.__doc__


@pytest.mark.parametrize("name", sorted(MIX_TEXTS))
def test_mix_texts_are_writer_safe(name):
    text = MIX_TEXTS[name]
    bad = [f for f in FORBIDDEN + ("归档", "archive", "写错", "你的错", "反馈给", "报告负责人", "default_params", "SQL",
                                   "service_role", "JSON") if f in text]
    assert not bad, (name, bad)


def test_the_earlier_wording_no_longer_says_the_mix_cannot_be_changed():
    texts = [core.USER_MIX_NOTE, core.COMMENT_SET_HEAD, core.SET_NEXT_STEP, core.COMMENT_SET_NEXT_STEP, core.SET_LINE_HEAD,
             tools.comments_for.__doc__, tools.score_comments.__doc__, tools.my_rules.__doc__]
    for t in texts:
        assert "改不了。" not in t and "对话里改不了" not in t, t[:40]
        assert "负责人给这个项目定的" not in t, "数可能是写手照用户说的改过的: 只说是这个项目定的配比"
    for t in (core.USER_MIX_NOTE, core.COMMENT_SET_HEAD, core.COMMENT_SET_NEXT_STEP, tools.comments_for.__doc__,
              tools.score_comments.__doc__, tools.my_rules.__doc__):
        assert "set_comment_mix" in t, t[:40]
    assert "这一组就照用户的写" in core.USER_MIX_NOTE and "以后都按新的配比" in core.USER_MIX_NOTE
    assert core.USER_MIX_NOTE.endswith("只有用户明说才改: 不为了让这一组通过自己去改配比。")
    assert "用户明说要改这个项目的配比时用 set_comment_mix" in tools.my_rules.__doc__
