"""2026-11-28 owner: 交付模板还是一个项目一份, 话题那一块写手在对话里自己改(set_delivery_tags, docs/deskcore.md §3.41)。

11-27 一个项目的写手停了旧的话题规则、记了一条「前 4 个跟正文、后 5 个跟产品, 不要那 5 个品牌话题」, 交付模板却照旧每篇
拼那 5 个固定话题; 模型只能叫写手找负责人。钉住:
  1. 改的只是 ``delivery.tags`` 的 content / fixed; 回包说清改之前 → 改之后(summary 原文), 带改完之后的 delivery_requirements
     (和 open_project 回的同一个函数), 交付稿真按新的拼;
  2. 入参: 至少给一个; content 是 0~10 的整数(bool、带小数的、不是纯数字的字都拒); fixed 是列表, 每一个照模板的认法规整、
     不分大小写去重留第一个, 空的 / 中间有空格或 # 的拒, 最多 10 个, 新加的一个最多 20 字; 拒了什么都不写;
  3. 归属闸; 没有交付模板、模板或设置读不了 → 那一句, 不写;
  4. 负责人锁住的固定话题(``delivery.tags.locked``, delivery.validate 新认的键): 写手的 fixed 里没写也留着、按它们现在在
     fixed 里的先后接在后面、回包 kept_locked 列出来、summary 说清; 锁的校验(得在 fixed 里, 写法一样); 没配锁时拼出来的字
     一个不变, 规范形状里也没有这个键(上一版的校验不认它); 最多 10 个只数写手自己的;
  5. 和现在一样(规整之后比) → 不写、不留痕; 写的时候 default_params 别的键、delivery 别的键原样、顺序不变, 仍是 JSON 字符串;
  6. 留痕 ``delivery_tags_log``: 顶层、新的在前、最多 20 条、{at, by, before, after}; 写完再读核对, 没落库照实说;
  7. 和 set_comment_mix 同一把锁(两个都整列写 default_params): 交替着改, 两边的改动和留痕都在;
  8. 注册、说明里的那几句、写手看得到的话里没有工程指令; 协议版本没动。
REST / MCP / /tools / /health 那一层在 tests/test_set_delivery_tags_http.py(要 fastapi)。
全部是合成项目、合成品牌和合成话题。
"""
from __future__ import annotations

import inspect
import json
import re
import threading

import pytest

from deskcore import core, delivery, store, tools
from tests.fakes import FakeClient
from tests.test_writer_facing_text import FORBIDDEN

ME = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
PROJ = "aaaaaaaa-0000-0000-0000-0000000000e1"
AT = "2026-11-28T08:00:00+00:00"
BY = "示例写手"

FIXED = ["示例必买", "#示例同款", "示例品牌"]
COND = [{"when_any": ["Alpha", "阿尔法"], "add": "Alpha好物"}]
TPL = {"layout": "blocks",
       "tags": {"content": 4, "fixed": list(FIXED), "conditional": [dict(c) for c in COND]},
       "comments": {"min": 1, "max": 2, "label_reader": "读者：", "label_author": "作者回复：",
                    "must_mention_any": ["示例品牌"]}}
# 11-28 那个项目手改前后的形状(合成的话题): 4 个写手的 + 5 个固定 + 1 个条件 → 9 个写手的、不要固定、不要条件
FIVE = ["示例必买", "示例韩系", "示例同款", "示例品牌贴", "示例品牌"]
BEFORE_1127 = {"layout": "blocks", "tags": {"content": 4, "fixed": list(FIVE),
                                            "conditional": [{"when_any": ["Alpha Shop", "AlphaShop", "AS"],
                                                             "add": "AlphaShop"}]}}
OTHER_KEYS = {"isolation_terms": ["示例词"], "comment_set": {"brand_max": 0.5, "column": "测试列"}, "archived": False}


_DEFAULT = object()


def _client(tpl=TPL, *, params=_DEFAULT, owner=ME) -> FakeClient:
    dp = params if params is not _DEFAULT else json.dumps(
        {"isolation_terms": ["示例词"], "delivery": tpl, "comment_set": {"brand_max": 0.5, "column": "测试列"},
         "archived": False}, ensure_ascii=False)
    return FakeClient(rows={"projects": [{"id": PROJ, "name": "示例项目", "brand": "示例品牌", "owner_id": owner,
                                          "calibration_notes": "", "tactics": "[]", "custom_roles": [],
                                          "default_params": dp}]})


def _row(c) -> dict:
    return c.rows["projects"][0]


def _params(c) -> dict:
    raw = _row(c)["default_params"]
    assert isinstance(raw, str), "default_params 要一直是 JSON 字符串(生产库这一列全是这个形状)"
    return json.loads(raw)


def _tags(c) -> dict:
    return _params(c)["delivery"]["tags"]


def _set(c, *, content=None, fixed=None, by=BY, at=AT, user=ME):
    return core.set_delivery_tags(c, PROJ, user_id=user, content=content, fixed=fixed, by=by, at=at)


def _writes(c):
    return [x for x in c.calls if x["op"] != "select"]


def _draft(n: int, extra_body: str = "") -> dict:
    return {"title": "示例标题", "body": "示例正文。" + extra_body, "tags": [f"写手话题{i}" for i in range(1, n + 1)],
            "comments": [{"role": "reader", "text": "示例品牌这个好用吗"}]}


# ── 1. 改之前 → 改之后, 交付稿真按新的拼 ────────────────────────────────────────────────────────

def test_content_only_changes_the_count_and_leaves_the_fixed_tags_byte_for_byte():
    c = _client()
    out = _set(c, content=9)
    assert out["changed"] is True and out["project_name"] == "示例项目" and out["project_id"] == PROJ
    assert out["before"] == {"content": 4, "fixed": ["示例必买", "示例同款", "示例品牌"]}
    assert out["after"] == {"content": 9, "fixed": ["示例必买", "示例同款", "示例品牌"]}
    assert out["summary"] == "「示例项目」交付稿的话题改了: 每篇写的话题 4 个 → 9 个。"
    assert _tags(c) == {"content": 9, "fixed": FIXED, "conditional": COND}, "fixed 没给: 库里原来的写法(带 #)一个字不动"
    assert out["next_step"] == core.DTAGS_NEXT_STEP and "kept_locked" not in out and "warning" not in out
    assert out["delivery_requirements"] == delivery.requirements(delivery.template_of(_row(c)))
    assert out["delivery_requirements"]["tags"] == 9


def test_fixed_only_replaces_the_whole_list_and_leaves_the_count():
    c = _client()
    out = _set(c, fixed=["新话题甲", "#新话题乙"])
    assert out["after"] == {"content": 4, "fixed": ["新话题甲", "新话题乙"]}
    assert _tags(c) == {"content": 4, "fixed": ["新话题甲", "新话题乙"], "conditional": COND}
    assert out["summary"].startswith("「示例项目」交付稿的话题改了: 固定话题 #示例必买、#示例同款、#示例品牌 → #新话题甲、#新话题乙。")
    assert out["delivery_requirements"]["fixed_tags"] == ["新话题甲", "新话题乙"]


def test_fixed_empty_means_no_fixed_tags_and_the_conditional_ones_are_mentioned():
    c = _client()
    out = _set(c, fixed=[])
    assert _tags(c)["fixed"] == [] and out["after"]["fixed"] == []
    assert out["summary"] == ("「示例项目」交付稿的话题改了: 固定话题 #示例必买、#示例同款、#示例品牌 → 没有。"
                              "按条件加的话题照旧(负责人定的, 这里改不了): 标题或正文提到「Alpha」「阿尔法」时加 #Alpha好物。")
    assert _tags(c)["conditional"] == COND, "条件话题这里一个字不动"


def test_changing_only_the_count_does_not_talk_about_conditional_tags():
    out = _set(_client(), content=5)
    assert "按条件加的话题" not in out["summary"]


def test_the_case_from_1127_nine_writer_tags_no_fixed_and_the_delivered_line_follows():
    """「以后标签前4个跟正文、后5个跟产品, 不要那5个品牌标签」: content=9, fixed=[]。交付稿的话题行是写手给的 9 个,
    一个固定话题都没有; 原来每篇拼 4 个写手的 + 5 个固定的。"""
    tpl = {"layout": "blocks", "tags": {"content": 4, "fixed": list(FIVE)}}
    c = _client(tpl)
    draft = _draft(9)
    old_line = delivery.assemble(delivery.template_of(_row(c)), [draft])["text"].splitlines()[-1]
    assert old_line == " ".join("#" + t for t in ["写手话题1", "写手话题2", "写手话题3", "写手话题4", *FIVE])
    out = _set(c, content=9, fixed=[])
    assert out["summary"] == ("「示例项目」交付稿的话题改了: 每篇写的话题 4 个 → 9 个; "
                              "固定话题 #示例必买、#示例韩系、#示例同款、#示例品牌贴、#示例品牌 → 没有。")
    new_tpl = delivery.template_of(_row(c))
    got = delivery.assemble(new_tpl, [draft])
    assert got["text"].splitlines()[-1] == " ".join(f"#写手话题{i}" for i in range(1, 10))
    assert got["problems"] == []
    short = delivery.assemble(new_tpl, [_draft(4)])["problems"]
    assert short == [{"index": 0, "what": "话题标签 4 个, 这个项目要 9 个; 在 tags 里补齐"}], "新的个数也照着查"
    assert out["delivery_requirements"] == delivery.requirements(new_tpl)
    assert out["delivery_requirements"]["task"].startswith("这个项目的交付稿由写作台按模板拼。score_drafts 时每篇 draft 里带上: "
                                                           "tags 给 9 个话题。")
    assert out["delivery_requirements"]["fixed_tags"] == [] and "固定话题" not in out["delivery_requirements"]["task"]


def test_the_1128_hand_edit_is_what_the_tool_would_have_written():
    """11-28 负责人批准在库里手改的那一份(content 9, fixed [], conditional []): 工具能写出 content / fixed 两样,
    conditional 只有负责人能动 —— 手改的那一步正是这一部分。"""
    c = _client(BEFORE_1127)
    _set(c, content=9, fixed=[])
    assert _tags(c) == {"content": 9, "fixed": [], "conditional": BEFORE_1127["tags"]["conditional"]}
    hand = dict(_tags(c), conditional=[])
    assert delivery.validate({"layout": "blocks", "tags": hand})["tags"] == {
        "content": 9, "fixed": [], "conditional": []}


def test_the_project_brief_after_the_change_asks_for_the_new_numbers():
    c = _client()
    _set(c, content=7)
    brief_part = delivery.requirements(delivery.template_of(_row(c)))
    assert brief_part["tags"] == 7 and "tags 给 7 个话题" in brief_part["task"]


def test_next_step_does_not_claim_earlier_batches_stay_as_delivered():
    """之前交付过的那几批没记当时的话题设置: 页面和改其中某篇重存时拼的整批都按现在的模板拼, 没改的那几篇用当时写的话题
    (既不是当时交付的那一行, 也不够新的个数)。next_step 不能只说「已经交付的不用重存」, 得说清这一点、和要原来那份去哪拿。"""
    out = _set(_client(BEFORE_1127), content=9, fixed=[])
    step = out["next_step"]
    assert step == core.DTAGS_NEXT_STEP
    for must in ("已经交付的不用重存", "之前交付过的那几批, 打开页面或者以后改其中某篇重存时, 也按新的话题设置拼",
                 "没改的那几篇只有当时写的话题", "用户要之前的样子, 用当时交给他的那一份"):
        assert must in step, must
    # 说的是真的: 改之前交付的一篇(4 个写手的 + 5 个固定), 现在按新模板拼出来只剩那 4 个
    old_tpl = delivery.validate(BEFORE_1127)
    c = _client(BEFORE_1127)
    _set(c, content=9, fixed=[])
    new_tpl = delivery.template_of(_row(c))
    then = delivery.assemble(old_tpl, [_draft(4)])["text"].splitlines()[2]
    now = delivery.assemble(new_tpl, [_draft(4)], indices=[None])
    assert then == " ".join(f"#写手话题{i}" for i in range(1, 5)) + " " + " ".join("#" + t for t in FIVE)
    assert now["text"].splitlines()[2] == " ".join(f"#写手话题{i}" for i in range(1, 5))


# ── 2. 入参 ──────────────────────────────────────────────────────────────────────────────────

def test_at_least_one_of_the_two_is_needed():
    c = _client()
    with pytest.raises(core.DeliveryTagsError) as exc:
        _set(c)
    assert str(exc.value) == core.DTAGS_EMPTY.format(hi=10) == (
        "content 和 fixed 至少给一个, 什么都没改: content 是每篇写几个话题(0~10 的整数), fixed 是固定话题的完整列表"
        "(不要固定话题写 [])。")
    assert _writes(c) == []


@pytest.mark.parametrize("given, stored", [(0, 0), (10, 10), (9.0, 9), ("9", 9), (" 9 ", 9), ("09", 9)])
def test_content_accepts_whole_numbers(given, stored):
    c = _client()
    _set(c, content=given)
    assert _tags(c)["content"] == stored and type(_tags(c)["content"]) is int


@pytest.mark.parametrize("bad", [-1, 11, 100, 4.5, True, False, "abc", "九", "²", "①", "４", "4.0", "-1", "", " ", "1e1",
                                 float("nan"), float("inf"), float("-inf"), 1e300, "1e400", [4], {"n": 4}, (4,),
                                 "9" * 5000, "0" * 7 + "9"])
def test_content_that_is_not_a_whole_number_in_range_is_refused_without_writing(bad):
    c = _client()
    before = _row(c)["default_params"]
    with pytest.raises(core.DeliveryTagsError) as exc:
        _set(c, content=bad, fixed=["新话题"])
    assert str(exc.value) == f"每篇写的话题个数(content)要是 0~10 的整数, 现在是 {bad!r:.40}。什么都没改。"
    assert _writes(c) == [] and _row(c)["default_params"] == before, "一起给的 fixed 也没改"


def test_numbers_too_long_to_print_are_refused_with_the_sentence_not_a_crash():
    """3.11 起超过 4300 位的整数 int() / repr() 都直接抛 ValueError: 不拦的话 REST 上是 500, 模型读到的是「服务坏了, 重试」。"""
    c = _client()
    huge = 10 ** 5000
    for bad, shown in (("9" * 5000, repr("9" * 5000)[:40]), (huge, "一个太长的值")):
        with pytest.raises(core.DeliveryTagsError) as exc:
            _set(c, content=bad)
        assert str(exc.value) == f"每篇写的话题个数(content)要是 0~10 的整数, 现在是 {shown}。什么都没改。"
    for fixed, part in (([huge], "固定话题第 1 个(一个太长的值)不是一个话题"), (huge, "现在是 一个太长的值。")):
        with pytest.raises(core.DeliveryTagsError) as exc:
            _set(c, fixed=fixed)
        assert part in str(exc.value)
    assert _writes(c) == []


def test_a_very_long_bad_value_is_not_echoed_back_whole():
    c = _client()
    for kw in ({"content": "9" * 500}, {"fixed": "甲" * 500}, {"fixed": ["两个 " + "词" * 500]}):
        with pytest.raises(core.DeliveryTagsError) as exc:
            _set(c, **kw)
        assert len(str(exc.value)) < 120, kw.keys()
    assert _writes(c) == []


@pytest.mark.parametrize("bad", ["示例话题", "#甲 #乙", 5, {"示例话题": 1}, True])
def test_fixed_that_is_not_a_list_is_refused(bad):
    c = _client()
    with pytest.raises(core.DeliveryTagsError) as exc:
        _set(c, fixed=bad)
    assert str(exc.value) == f"固定话题(fixed)要是一组话题, 比如 [\"示例话题\"], 不要固定话题写 []; 现在是 {bad!r}。什么都没改。"
    assert _writes(c) == []


@pytest.mark.parametrize("items, k", [(["两个 词"], 1), (["好", ""], 2), (["  "], 1), (["#"], 1), (["#[话题]#"], 1),
                                      ([None], 1), ([5], 1), (["a#b"], 1), (["[x]"], 1), (["甲", ["乙"]], 2),
                                      (["换\n行"], 1)])
def test_an_item_that_is_not_a_tag_is_refused_and_named(items, k):
    c = _client()
    with pytest.raises(core.DeliveryTagsError) as exc:
        _set(c, fixed=items)
    assert str(exc.value) == (f"固定话题第 {k} 个({items[k - 1]!r})不是一个话题: 要是一个词, 不能是空的, 中间不能有空格或 #。"
                              "什么都没改。")
    assert _writes(c) == []


def test_items_are_normalised_like_the_template_and_deduplicated_keeping_the_first():
    c = _client()
    out = _set(c, fixed=["#甲", "甲", " 乙 ", "#乙[话题]#", "Abc", "abc", "#ABC", "丙"])
    assert out["after"]["fixed"] == ["甲", "乙", "Abc", "丙"]
    assert _tags(c)["fixed"] == ["甲", "乙", "Abc", "丙"]
    for t in out["after"]["fixed"]:
        assert delivery.clean_tag(t) == t == delivery._template_tag(t, "x"), "和模板的固定话题同一种认法"


def test_at_most_ten_after_removing_duplicates():
    c = _client()
    ten = [f"话题{i}" for i in range(10)]
    assert _set(c, fixed=ten + ["#话题0"])["after"]["fixed"] == ten, "重复的不算"
    with pytest.raises(core.DeliveryTagsError) as exc:
        _set(c, fixed=ten + ["话题10"])
    assert str(exc.value) == "固定话题最多 10 个(负责人锁定的另算), 这次去掉重复的还有 11 个。什么都没改。"
    assert _tags(c)["fixed"] == ten


def test_a_new_tag_longer_than_twenty_characters_is_refused_but_one_already_in_the_template_is_not():
    long_one = "长" * 25
    c = _client(dict(TPL, tags=dict(TPL["tags"], fixed=[*FIXED, long_one])))
    with pytest.raises(core.DeliveryTagsError) as exc:
        _set(c, fixed=["短的", "新" * 21])
    assert str(exc.value) == f"固定话题第 2 个「{'新' * 20}…」太长了: 一个话题最多 20 个字。什么都没改。"
    assert _writes(c) == []
    assert _set(c, fixed=["新" * 20])["changed"] is True, "20 个字可以"
    c = _client(dict(TPL, tags=dict(TPL["tags"], fixed=[*FIXED, long_one])))
    assert _set(c, fixed=[long_one])["after"]["fixed"] == [long_one], "负责人收下过的那个, 原样带回来不拒"


def test_the_tool_has_no_parameter_for_conditional_or_locked():
    params = list(inspect.signature(tools.set_delivery_tags).parameters)
    assert params == ["project_id", "content", "fixed", "_user_id", "_caller_name"]
    core_params = set(inspect.signature(core.set_delivery_tags).parameters)
    assert not core_params & {"conditional", "locked", "layout", "comments"}


# ── 3. 拒绝 ──────────────────────────────────────────────────────────────────────────────────

def test_unknown_project_and_someone_elses_project_are_refused_without_writing():
    c = _client()
    with pytest.raises(core.ProjectNotFound):
        core.set_delivery_tags(c, "no-such-project", user_id=ME, content=9, by=BY, at=AT)
    with pytest.raises(PermissionError):
        _set(c, content=9, user=OTHER)
    c = _client(owner=OTHER)
    with pytest.raises(PermissionError):
        _set(c, content=9)
    assert _writes(c) == []


@pytest.mark.parametrize("dp", ["{}", "", None, {}, json.dumps({"isolation_terms": ["示例词"], "comment_set": {"column": "列"}})])
def test_a_project_without_a_delivery_template_is_told_to_write_tags_per_project_rules(dp):
    c = _client(params=dp)
    with pytest.raises(core.DeliveryTagsError) as exc:
        _set(c, content=9, fixed=[])
    assert str(exc.value) == core.DTAGS_NO_TEMPLATE == (
        "这个项目没有交付模板, 话题照项目规则写(记规则用 record_rule); 要模板找负责人。")
    assert _writes(c) == [] and _row(c)["default_params"] == dp


@pytest.mark.parametrize("dp", ["{not json", "[1, 2]", "\"just a string\"", 7,
                                json.dumps({"delivery": {"layout": "bogus"}}),
                                json.dumps({"delivery": {"layout": "blocks", "tags": {"content": 11}}}),
                                json.dumps({"delivery": {"layout": "blocks", "tags": {"fixed": ["甲"], "locked": ["乙"]}}}),
                                json.dumps({"delivery": {"layout": "blocks", "extra": 1}}),
                                json.dumps({"delivery": "不是对象"})])
def test_broken_settings_or_a_broken_template_are_not_written_over(dp):
    c = _client(params=dp)
    with pytest.raises(core.DeliveryTagsError) as exc:
        _set(c, content=9)
    assert str(exc.value) == core.DTAGS_BROKEN == "这个项目的交付设置现在改不了, 请用户找负责人看一下, 不用重试。"
    assert _writes(c) == [] and _row(c)["default_params"] == dp


def test_the_tool_goes_through_the_validator_the_readers_use(monkeypatch):
    """换掉 ``delivery.validate`` 工具就写不进去: 收下的设置和读的时候认的是同一道闸。"""
    c = _client()
    real = delivery.validate
    calls = []

    def _refuse_new(raw):
        calls.append(raw)
        if (raw.get("tags") or {}).get("content") == 9:
            raise delivery.TemplateError("示例: 校验不过")
        return real(raw)
    monkeypatch.setattr(delivery, "validate", _refuse_new)
    with pytest.raises(core.DeliveryTagsError) as exc:
        _set(c, content=9)
    assert str(exc.value) == core.DTAGS_BROKEN and _writes(c) == []
    assert any((r.get("tags") or {}).get("content") == 9 for r in calls), "改完的那一份过了 validate"


def test_whatever_the_tool_stores_is_read_back_as_a_valid_template():
    for kw in ({"content": 0}, {"content": 10}, {"fixed": []}, {"content": 0, "fixed": []}, {"fixed": ["甲", "乙"]},
               {"content": 3, "fixed": [f"话题{i}" for i in range(10)]}):
        c = _client()
        _set(c, **kw)
        tpl = delivery.template_of(_row(c))
        assert tpl is not None and delivery.validate(_params(c)["delivery"]) == tpl
        assert core._has_delivery_template(_row(c)) is True


def test_no_tags_at_all_leaves_a_template_that_does_not_manage_tags_and_says_where_tags_go():
    """content 0、fixed []、也没有条件话题: 模板不管话题了 —— 交付稿不拼话题行, requirements 的 task 说话题照项目规则写在
    正文末尾(这是 delivery 本来就有的一种模板, 不是这个工具另造的)。"""
    c = _client({"layout": "blocks", "tags": {"content": 2, "fixed": ["甲"]}})
    out = _set(c, content=0, fixed=[])
    tpl = delivery.template_of(_row(c))
    assert not delivery.wants_tags(tpl)
    assert out["delivery_requirements"] == delivery.requirements(tpl)
    assert "这个模板不管话题" in out["delivery_requirements"]["task"]


# ── 4. 负责人锁住的固定话题 ──────────────────────────────────────────────────────────────────────

LOCKED_TPL = dict(TPL, tags=dict(TPL["tags"], locked=["示例品牌", "#示例必买"]))


def test_validate_reads_locked_and_defaults_to_empty():
    assert delivery.validate(TPL)["tags"].get("locked", []) == []
    assert delivery.validate(LOCKED_TPL)["tags"]["locked"] == ["示例品牌", "示例必买"], "和 fixed 一样规整, 顺序照写的"
    assert delivery.validate(dict(TPL, tags=dict(TPL["tags"], locked=["示例品牌", "示例品牌"])))["tags"]["locked"] == ["示例品牌"]
    norm = delivery.validate(LOCKED_TPL)
    assert delivery.validate(norm) == norm, "再喂一次也照样通过"


@pytest.mark.parametrize("locked, words", [
    (["别的话题"], "delivery.tags.locked 里的「别的话题」不在 delivery.tags.fixed 里"),
    (["示例BRAND"], "不在 delivery.tags.fixed 里"),
    (["示例同款", "两个 词"], "不是一个话题"),
    ("示例品牌", "delivery.tags.locked 应该是列表"),
    ([5], "应该是字符串"),
])
def test_validate_refuses_a_locked_tag_that_is_not_a_fixed_tag(locked, words):
    with pytest.raises(delivery.TemplateError) as exc:
        delivery.validate(dict(TPL, tags=dict(TPL["tags"], locked=locked)))
    assert words in str(exc.value)
    with pytest.raises(delivery.TemplateError):     # 大小写不一样也不算: 锁住的写法只看 fixed 那一处
        delivery.validate({"layout": "blocks", "tags": {"fixed": ["Brand"], "locked": ["brand"]}})


def test_locked_tags_change_nothing_that_is_delivered():
    drafts = [_draft(4, "Alpha 来了"), _draft(2)]
    plain, locked = delivery.validate(TPL), delivery.validate(LOCKED_TPL)
    assert delivery.assemble(plain, drafts) == delivery.assemble(locked, drafts)
    assert delivery.requirements(plain) == delivery.requirements(locked)
    assert delivery.expected_parts(plain) == delivery.expected_parts(locked)
    assert delivery.rows(plain, drafts) == delivery.rows(locked, drafts)
    assert delivery.preview(plain) == delivery.preview(locked)
    for d in drafts:
        assert delivery.delivered_draft(plain, d) == delivery.delivered_draft(locked, d)


def test_an_existing_template_delivers_exactly_the_same_text_as_before():
    """模板多认了一个键, 没配它的模板拼出来一个字不变(11-28 之前的样子, 原文钉死)。"""
    out = delivery.assemble(TPL, [_draft(5, "Alpha 来了 #末尾话题")])
    assert out["text"] == ("【标题】示例标题\n【正文】示例正文。Alpha 来了\n"
                           "#写手话题1 #写手话题2 #写手话题3 #写手话题4 #示例必买 #示例同款 #示例品牌 #Alpha好物\n"
                           "读者：示例品牌这个好用吗")
    assert out["problems"] == []
    assert delivery.requirements(TPL) == {
        "layout": "blocks", "tags": 4, "fixed_tags": ["示例必买", "示例同款", "示例品牌"],
        "comments": {"min": 1, "max": 2, "must_mention_any": ["示例品牌"]}, "direction": False,
        "task": delivery.requirements(TPL)["task"]}


def test_a_writer_cannot_remove_a_locked_tag_it_is_kept_and_reported():
    c = _client(LOCKED_TPL)
    out = _set(c, fixed=["新话题"])
    assert out["after"]["fixed"] == ["新话题", "示例必买", "示例品牌"], "没写的锁住的按它们现在在 fixed 里的先后接在后面"
    assert out["kept_locked"] == ["示例必买", "示例品牌"]
    assert out["summary"] == ("「示例项目」交付稿的话题改了: 固定话题 #示例必买、#示例同款、#示例品牌 → #新话题、#示例必买、#示例品牌。"
                              "#示例必买、#示例品牌 是负责人锁定的固定话题, 留着了(这里去不掉, 要去掉请找负责人)。"
                              "按条件加的话题照旧(负责人定的, 这里改不了): 标题或正文提到「Alpha」「阿尔法」时加 #Alpha好物。")
    assert _tags(c)["fixed"] == ["新话题", "示例必买", "示例品牌"]
    assert _tags(c)["locked"] == ["示例品牌", "#示例必买"], "锁本身一个字不动"
    assert delivery.template_of(_row(c)) is not None


def test_a_locked_tag_the_writer_did_write_keeps_its_place_and_the_template_spelling():
    c = _client(dict(TPL, tags={"content": 4, "fixed": ["Brand甲", "示例同款"], "locked": ["Brand甲"]}))
    out = _set(c, fixed=["新话题", "#BRAND甲"])
    assert out["after"]["fixed"] == ["新话题", "Brand甲"], "写了就在写的位置, 写法照模板的"
    assert "kept_locked" not in out and "锁定" not in out["summary"]


def test_when_every_fixed_tag_is_locked_removing_them_changes_nothing_and_says_why():
    c = _client(dict(TPL, tags={"content": 4, "fixed": ["示例品牌", "示例必买"], "locked": ["示例品牌", "示例必买"]}))
    before = _row(c)["default_params"]
    out = _set(c, fixed=[])
    assert out["changed"] is False and out["kept_locked"] == ["示例品牌", "示例必买"]
    assert out["summary"] == ("「示例项目」交付稿的话题和现在的一样, 没有改。"
                              "#示例品牌、#示例必买 是负责人锁定的固定话题, 留着了(这里去不掉, 要去掉请找负责人)。")
    assert out["next_step"] == core.DTAGS_UNCHANGED_NEXT_STEP and out["delivery_requirements"]["fixed_tags"] == [
        "示例品牌", "示例必买"]
    assert _writes(c) == [] and _row(c)["default_params"] == before


def test_reordering_locked_tags_is_a_change():
    c = _client(dict(TPL, tags={"content": 4, "fixed": ["示例品牌", "示例必买"], "locked": ["示例品牌", "示例必买"]}))
    out = _set(c, fixed=["示例必买", "示例品牌"])
    assert out["changed"] is True and out["after"]["fixed"] == ["示例必买", "示例品牌"] and "kept_locked" not in out


def test_kept_locked_tags_keep_their_order_in_fixed_not_the_order_the_lock_was_written_in():
    """负责人写锁的先后和 fixed 的先后不一样时: 写手只是去掉别的话题, 客户点名的那几个在交付稿里的先后不能跟着变;
    全是锁住的、写手要全去掉的, 就是现在那一份 —— 不写、不留痕(原来按锁的先后接, 会把它们调个个儿写进去)。"""
    tags = {"content": 4, "fixed": ["示例必买", "示例品牌"], "locked": ["示例品牌", "示例必买"]}
    c = _client(dict(TPL, tags=tags))
    before = _row(c)["default_params"]
    out = _set(c, fixed=[])
    assert out["changed"] is False and out["after"]["fixed"] == ["示例必买", "示例品牌"]
    assert out["kept_locked"] == ["示例必买", "示例品牌"]
    assert out["summary"] == ("「示例项目」交付稿的话题和现在的一样, 没有改。"
                              "#示例必买、#示例品牌 是负责人锁定的固定话题, 留着了(这里去不掉, 要去掉请找负责人)。")
    assert _writes(c) == [] and _row(c)["default_params"] == before

    c = _client(dict(TPL, tags={"content": 4, "fixed": ["锁甲", "别的", "锁乙", "锁丙"], "locked": ["锁丙", "锁甲", "锁乙"]}))
    out = _set(c, fixed=["新的", "锁乙"])
    assert out["after"]["fixed"] == ["新的", "锁乙", "锁甲", "锁丙"], "写了的在写的位置, 没写的按 fixed 里的先后"
    assert out["kept_locked"] == ["锁甲", "锁丙"]
    line = delivery.assemble(delivery.template_of(_row(c)), [_draft(4)])["text"].splitlines()[2]
    assert line.endswith("#新的 #锁乙 #锁甲 #锁丙")


def test_the_ten_tag_cap_counts_only_the_writers_own_tags_whether_or_not_the_locked_ones_are_listed():
    """说明叫模型传完整列表(锁住的写不写都行)。锁住的 5 个 + 新的 6 个: 写进列表和没写, 要的是同一个结果, 不能一个收
    一个拒; 上限数的是写手自己的那几个。"""
    five = [f"品牌{i}" for i in "一二三四五"]
    tpl = dict(TPL, tags={"content": 4, "fixed": list(five), "locked": list(five)})
    own6 = [f"新话题{i}" for i in range(6)]
    listed, omitted = _set(_client(tpl), fixed=five + own6), _set(_client(tpl), fixed=own6)
    assert listed["changed"] is omitted["changed"] is True
    assert sorted(listed["after"]["fixed"]) == sorted(omitted["after"]["fixed"]) == sorted(five + own6)
    assert len(omitted["after"]["fixed"]) == 11, "锁住的照留, 不算进那 10 个"
    own11 = [f"新话题{i}" for i in range(11)]
    for fixed in (five + own11, own11, ["#" + t.upper() for t in five] + own11):
        c = _client(tpl)
        with pytest.raises(core.DeliveryTagsError) as exc:
            _set(c, fixed=fixed)
        assert str(exc.value) == "固定话题最多 10 个(负责人锁定的另算), 这次去掉重复的还有 11 个。什么都没改。"
        assert _writes(c) == []
    own10 = [f"新话题{i}" for i in range(10)]
    assert _set(_client(tpl), fixed=five + own10)["after"]["fixed"] == five + own10


def test_a_template_without_locks_is_stored_without_a_locked_key_so_the_previous_release_still_reads_it():
    """上一版(11-28 之前)的校验只认 tags 里的 content / fixed / conditional, 多一个键整个模板就是坏的(不拼交付稿、简报不带
    要求)。没配锁的模板, 不管是负责人 delivery-format --set 的还是写手改过话题的, 存下来都不带 locked: 回滚时它们照常能用。"""
    previous_tag_keys = {"content", "fixed", "conditional"}
    assert "locked" not in delivery.validate(TPL)["tags"]
    assert delivery.validate(dict(TPL, tags=dict(TPL["tags"], locked=[])))["tags"].keys() <= previous_tag_keys
    c = _client(params=json.dumps({"isolation_terms": ["示例词"]}, ensure_ascii=False))
    core.delivery_format(c, PROJ, template=TPL)
    assert set(_params(c)["delivery"]["tags"]) <= previous_tag_keys
    _set(c, content=9, fixed=["新话题"])
    assert set(_params(c)["delivery"]["tags"]) <= previous_tag_keys
    assert "locked" not in core.delivery_format(c, PROJ)["delivery"]["tags"], "--show 看到的也和 11-28 之前一样"
    # 配了锁的照存(只有用锁的项目回滚时要先把锁拿掉, §3.41)
    core.delivery_format(c, PROJ, template=LOCKED_TPL)
    assert _params(c)["delivery"]["tags"]["locked"] == ["示例品牌", "示例必买"]


# ── 5. 一样就不写; 别的键原样 ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kw", [{"content": 4}, {"content": "4"}, {"fixed": ["示例必买", "示例同款", "#示例品牌"]},
                                {"fixed": ["#示例必买[话题]#", "示例同款", "示例品牌", "示例同款"]},
                                {"content": 4.0, "fixed": ["示例必买", "示例同款", "示例品牌"]}])
def test_the_same_settings_after_normalising_are_not_written_and_not_logged(kw):
    c = _client()
    before = _row(c)["default_params"]
    out = _set(c, **kw)
    assert out["changed"] is False and out["summary"] == "「示例项目」交付稿的话题和现在的一样, 没有改。"
    assert out["before"] == out["after"] and out["next_step"] == core.DTAGS_UNCHANGED_NEXT_STEP
    assert out["delivery_requirements"] == delivery.requirements(TPL)
    assert _writes(c) == [] and _row(c)["default_params"] == before
    assert core.DELIVERY_TAGS_LOG_KEY not in _params(c)


@pytest.mark.parametrize("fixed", [["示例同款", "示例必买", "示例品牌"], ["示例必买", "示例同款", "示例品牌X"],
                                   ["示例必买", "示例同款"]])
def test_order_spelling_and_count_of_fixed_tags_are_changes(fixed):
    assert _set(_client(), fixed=fixed)["changed"] is True


def test_a_case_only_change_is_a_change_too():
    """交付稿的话题行照写的样子拼: 大小写变了, 拼出来的字就变了。"""
    c = _client({"layout": "blocks", "tags": {"content": 2, "fixed": ["Alpha好物"]}})
    out = _set(c, fixed=["ALPHA好物"])
    assert out["changed"] is True and _tags(c)["fixed"] == ["ALPHA好物"]


def test_other_keys_keep_their_values_and_their_order():
    order = ["isolation_terms", "delivery", "comment_set", "archived"]
    dlv = {"layout": "table", "columns": [{"name": "笔记", "field": "post"}], "markers": {"title": "【标题】"},
           "tags": {"conditional": [], "fixed": ["#甲"], "content": 2, "locked": ["#甲"]}, "comments": None}
    c = _client(params=json.dumps({"isolation_terms": ["示例词"], "delivery": dlv,
                                   "comment_set": {"brand_max": 0.5, "column": "测试列"}, "archived": False},
                                  ensure_ascii=False))
    _set(c, content=5)
    after = _params(c)
    assert list(after) == [*order, core.DELIVERY_TAGS_LOG_KEY], "新的留痕接在最后, 别的键原来的顺序"
    for k, v in OTHER_KEYS.items():
        assert after[k] == v
    assert list(after["delivery"]) == ["layout", "columns", "markers", "tags", "comments"]
    assert {k: v for k, v in after["delivery"].items() if k != "tags"} == {k: v for k, v in dlv.items() if k != "tags"}
    assert after["delivery"]["tags"] == {"conditional": [], "fixed": ["#甲"], "content": 5, "locked": ["#甲"]}
    assert list(after["delivery"]["tags"]) == ["conditional", "fixed", "content", "locked"], "content 还在原来的位置"
    (call,) = _writes(c)
    assert call["table"] == "projects" and call["op"] == "update" and list(call["payload"]) == ["default_params"]
    assert isinstance(call["payload"]["default_params"], str), "写回去的是 JSON 字符串, 不是 dict"
    assert "示例词" in call["payload"]["default_params"], "中文原样存, 不转成 \\u"


def test_a_template_without_a_tags_key_gets_one_at_the_end():
    dlv = {"layout": "blocks", "comments": None}
    c = _client(params=json.dumps({"delivery": dlv}))
    out = _set(c, content=3)
    assert out["before"] == {"content": 0, "fixed": []} and out["after"] == {"content": 3, "fixed": []}
    assert _params(c)["delivery"] == {"layout": "blocks", "comments": None, "tags": {"content": 3}}
    c = _client(params=json.dumps({"delivery": {"layout": "blocks", "tags": None, "comments": None}}))
    _set(c, fixed=["甲"])
    assert _params(c)["delivery"] == {"layout": "blocks", "tags": {"fixed": ["甲"]}, "comments": None}


def test_a_dict_shaped_default_params_is_read_and_written_back_as_a_string():
    c = _client(params={"delivery": json.loads(json.dumps(TPL)), "archived": True})
    _set(c, content=6)
    assert _tags(c)["content"] == 6 and _params(c)["archived"] is True


# ── 6. 留痕、写完核对 ─────────────────────────────────────────────────────────────────────────

def test_the_log_entry_has_who_when_before_after_and_sits_at_the_top_level():
    c = _client()
    _set(c, content=9, fixed=[])
    assert _params(c)[core.DELIVERY_TAGS_LOG_KEY] == [
        {"at": AT, "by": BY, "before": {"content": 4, "fixed": ["示例必买", "示例同款", "示例品牌"]},
         "after": {"content": 9, "fixed": []}}]
    assert core.DELIVERY_TAGS_LOG_KEY not in _params(c)["delivery"], "delivery 里的键校验是严的, 留痕不放进去"
    assert core.COMMENT_SET_LOG_KEY not in _params(c), "两份留痕各是各的"


def test_the_log_is_newest_first_and_capped_at_twenty():
    c = _client()
    for i in range(23):
        _set(c, content=i % 11, fixed=[f"话题{i}"], at=f"2026-11-28T08:00:{i:02d}+00:00", by=f"示例写手{i}")
    log = _params(c)[core.DELIVERY_TAGS_LOG_KEY]
    assert len(log) == core.DELIVERY_TAGS_LOG_MAX == 20
    assert [e["by"] for e in log] == [f"示例写手{i}" for i in range(22, 2, -1)], "新的在前, 最早的三条掉了"
    assert log[0]["after"] == {"content": 0, "fixed": ["话题22"]} and log[0]["before"] == {"content": 10, "fixed": ["话题21"]}
    assert log[-1]["at"] == "2026-11-28T08:00:03+00:00"


def test_a_log_that_is_not_a_list_is_started_over_and_no_name_falls_back_to_the_id_prefix():
    c = _client(params=json.dumps({"delivery": TPL, core.DELIVERY_TAGS_LOG_KEY: "坏的"}, ensure_ascii=False))
    _set(c, content=9, by="")
    (entry,) = _params(c)[core.DELIVERY_TAGS_LOG_KEY]
    assert entry["by"] == "user 11111111" and set(entry) == {"at", "by", "before", "after"}


def test_a_write_that_did_not_land_is_reported_as_not_changed(monkeypatch):
    c = _client(LOCKED_TPL)
    monkeypatch.setattr(store, "set_project_default_params", lambda *a, **k: None)
    out = _set(c, fixed=[])
    assert out["changed"] is False and out["warning"] == out["summary"] == core.DTAGS_NOT_STORED
    assert out["after"] == out["before"], "回的是库里实际的, 不是要改成的"
    assert "next_step" not in out and "delivery_requirements" not in out and "kept_locked" not in out


def test_a_write_that_was_overwritten_without_this_log_entry_is_not_reported_as_done(monkeypatch):
    c = _client()
    real = store.set_project_default_params

    def overwritten(client, pid, params_json):
        params = json.loads(params_json)
        params[core.DELIVERY_TAGS_LOG_KEY] = [{"at": AT, "by": "另一位", "before": None, "after": None}]
        return real(client, pid, json.dumps(params, ensure_ascii=False))
    monkeypatch.setattr(store, "set_project_default_params", overwritten)
    out = _set(c, content=9)
    assert out["changed"] is False and out["summary"] == out["warning"] == core.DTAGS_NOT_STORED
    assert out["after"] == {"content": 9, "fixed": ["示例必买", "示例同款", "示例品牌"]}, "话题对得上, 留痕不是这次的"


def test_a_write_that_left_a_broken_template_is_not_reported_as_done(monkeypatch):
    c = _client()
    real = store.set_project_default_params
    monkeypatch.setattr(store, "set_project_default_params",
                        lambda client, pid, params_json: real(client, pid, "{not json"))
    out = _set(c, content=9)
    assert out["changed"] is False and out["warning"] == core.DTAGS_NOT_STORED and out["after"] is None


def test_a_later_write_to_another_key_does_not_turn_a_landed_change_into_not_stored(monkeypatch):
    c = _client()
    real = store.set_project_default_params

    def then_archived(client, pid, params_json):
        return real(client, pid, json.dumps(dict(json.loads(params_json), archived=True), ensure_ascii=False))
    monkeypatch.setattr(store, "set_project_default_params", then_archived)
    out = _set(c, content=9)
    assert out["changed"] is True and "warning" not in out


def test_the_row_is_read_again_once_the_lock_is_held(monkeypatch):
    """归属校验读到的那一行可能已经旧了(排队的时候别人写过): 合并用的是拿到锁之后再读的那一行。"""
    c = _client()
    real = core.assert_project_access

    def stale(client, project_id, *, user_id):
        row = dict(real(client, project_id, user_id=user_id))
        _row(c)["default_params"] = json.dumps({"delivery": dict(TPL, tags=dict(TPL["tags"], content=6)),
                                                "comment_set": {"brand_max": 0.6}, "archived": True},
                                               ensure_ascii=False)
        return row
    monkeypatch.setattr(core, "assert_project_access", stale)
    out = _set(c, fixed=["新话题"])
    assert out["before"]["content"] == 6 and out["after"] == {"content": 6, "fixed": ["新话题"]}
    after = _params(c)
    assert after["comment_set"] == {"brand_max": 0.6} and after["archived"] is True, "别人刚写的键没有被旧的一份盖掉"


# ── 7. 和 set_comment_mix 同一把锁 ─────────────────────────────────────────────────────────────

def _race(monkeypatch, first, second):
    """``first`` 写到一半(卡在第一次写库), ``second`` 才开始: 回 (两个回包, second 有没有等)。"""
    real = store.set_project_default_params
    in_write, release = threading.Event(), threading.Event()

    def slow(client, pid, params_json):
        if not in_write.is_set():                        # 只卡住第一次写
            in_write.set()
            assert release.wait(10)
        return real(client, pid, params_json)
    monkeypatch.setattr(store, "set_project_default_params", slow)
    outs = {}
    a = threading.Thread(target=lambda: outs.update(a=first()))
    b = threading.Thread(target=lambda: outs.update(b=second()))
    a.start()
    assert in_write.wait(10)
    b.start()
    b.join(0.5)
    waited = b.is_alive()
    release.set()
    a.join(10)
    b.join(10)
    return outs, waited


def test_a_comment_mix_change_and_a_tags_change_on_one_project_queue_and_neither_is_lost(monkeypatch):
    """两个工具都是读、改、写整列 default_params。各用各的锁的话: 配比那次读完还没写, 话题这次写完了, 配比那次拿旧的一份
    盖回去 —— 话题改动和它的留痕都没了, 两边还都报改成了。"""
    c = _client()
    outs, waited = _race(monkeypatch,
                         lambda: core.set_comment_mix(c, PROJ, user_id=ME, mix={"brand_max": 0.6}, by="示例写手甲", at=AT),
                         lambda: _set(c, content=9, fixed=[], by="示例写手乙"))
    assert waited, "话题那次要等配比那次写完(同一个项目、同一把锁)"
    assert outs["a"]["changed"] is True and outs["b"]["changed"] is True
    after = _params(c)
    assert after["comment_set"]["brand_max"] == 0.6 and after["delivery"]["tags"]["content"] == 9
    assert after["delivery"]["tags"]["fixed"] == []
    assert after[core.COMMENT_SET_LOG_KEY][0]["by"] == "示例写手甲"
    assert after[core.DELIVERY_TAGS_LOG_KEY][0]["by"] == "示例写手乙"


def test_the_other_way_round_too(monkeypatch):
    c = _client()
    outs, waited = _race(monkeypatch,
                         lambda: _set(c, content=9, by="示例写手乙"),
                         lambda: core.set_comment_mix(c, PROJ, user_id=ME, mix={"brand_max": 0.4}, by="示例写手甲", at=AT))
    assert waited and outs["a"]["changed"] is True and outs["b"]["changed"] is True
    after = _params(c)
    assert after["comment_set"]["brand_max"] == 0.4 and after["delivery"]["tags"]["content"] == 9
    assert after[core.DELIVERY_TAGS_LOG_KEY][0]["by"] == "示例写手乙"


def test_both_tools_take_the_same_lock_object_and_only_for_real_projects(monkeypatch):
    c = _client()
    taken = []
    real = core._mix_lock
    monkeypatch.setattr(core, "_mix_lock", lambda pid: taken.append(pid) or real(pid))
    core.set_comment_mix(c, PROJ, user_id=ME, mix={"brand_max": 0.6}, by=BY, at=AT)
    _set(c, content=9)
    assert taken == [PROJ, PROJ] and real(PROJ) is real(PROJ)
    before = len(core._MIX_LOCKS)
    with pytest.raises(core.ProjectNotFound):
        core.set_delivery_tags(c, "no-such-project-dt", user_id=ME, content=9, by=BY, at=AT)
    assert len(core._MIX_LOCKS) == before, "没过归属校验的 id 不占一把锁"
    with real("另一个项目"):                             # 别的项目占着锁, 不挡这个项目
        assert _set(c, content=8)["changed"] is True


# ── 8. 工具那一层、注册、说明、写手看得到的话 ────────────────────────────────────────────────────

def test_the_tool_layer_stamps_utc_time_and_the_injected_name(monkeypatch):
    c = _client()
    monkeypatch.setattr(core, "sb", lambda: c)
    out = tools.set_delivery_tags(PROJ, content=9, _user_id=ME, _caller_name="  示例写手  ")
    assert out["changed"] is True
    (entry,) = _params(c)[core.DELIVERY_TAGS_LOG_KEY]
    assert entry["by"] == "示例写手"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", entry["at"]), entry["at"]
    tools.set_delivery_tags(PROJ, fixed=[], _user_id=ME, _caller_name="长" * 100)
    assert _params(c)[core.DELIVERY_TAGS_LOG_KEY][0]["by"] == "长" * 40


def test_the_tool_layer_turns_refusals_into_invalid_input_and_does_not_swallow(monkeypatch):
    c = _client()
    monkeypatch.setattr(core, "sb", lambda: c)
    with pytest.raises(tools.InvalidInput) as exc:
        tools.set_delivery_tags(PROJ, content=11, _user_id=ME)
    assert str(exc.value) == "每篇写的话题个数(content)要是 0~10 的整数, 现在是 11。什么都没改。"
    with pytest.raises(tools.InvalidInput):
        tools.set_delivery_tags(PROJ, _user_id=ME)
    with pytest.raises(PermissionError):
        tools.set_delivery_tags(PROJ, content=9, _user_id=OTHER)
    with pytest.raises(core.ProjectNotFound):
        tools.set_delivery_tags("no-such-project", content=9, _user_id=ME)
    with pytest.raises(PermissionError) as who:
        tools.set_delivery_tags(PROJ, content=9, _user_id=None)
    assert str(who.value) == "无法识别调用者身份, 话题设置改不了。这次没做成; 请用户找负责人看一下, 不用重试。"
    assert "_safe(" not in inspect.getsource(tools.set_delivery_tags).split('"""')[2], "写: 失败要冒泡"
    assert _writes(c) == []
    c2 = _client(params="{}")
    monkeypatch.setattr(core, "sb", lambda: c2)
    with pytest.raises(tools.InvalidInput) as exc:
        tools.set_delivery_tags(PROJ, content=9, _user_id=ME)
    assert str(exc.value) == core.DTAGS_NO_TEMPLATE


def test_registered_with_identity_next_to_set_comment_mix():
    fn, needs_user = tools.TOOLS["set_delivery_tags"]
    assert fn is tools.set_delivery_tags and needs_user is True
    names = list(tools.TOOLS)
    assert names.index("set_delivery_tags") == names.index("set_comment_mix") + 1
    sig = inspect.signature(fn)
    assert sig.parameters["content"].default is None and sig.parameters["fixed"].default is None


def test_the_docstring_says_only_when_the_user_asks_and_never_to_pass_a_check():
    doc = tools.set_delivery_tags.__doc__
    assert doc.startswith("改这个项目交付稿的话题: 每篇写几个话题、固定加哪几个话题")
    assert "**只在用户明说这个项目以后都这样时调。**" in doc
    for must in ("什么时候调:", "什么时候不调:", "「以后标签前4个跟正文、后5个跟痘痘, 不要那5个品牌标签」",
                 "content=9, fixed=[]", "用户只是这一批想要别的话题",
                 "**不为了让检查通过去改话题个数或固定话题**", "也不替用户决定", "先问一句", "不用找负责人",
                 "照旧用 record_rule 记成规则", "记成规则不生效", "0~10 的整数", "不要固定话题写 []", "整个换掉现在的",
                 "负责人锁定的", "kept_locked", "这里去不掉", "**原样告诉用户**", "delivery_requirements",
                 "和 open_project 回的同一个样子", "20 次的记录", "已经交付的不用重存", "照着改参数再调", "不重试"):
        assert must in doc, must
    assert doc.index("什么时候调:") < doc.index("什么时候不调:") < doc.index("返回:")


def test_the_docstring_does_not_promise_a_one_batch_tag_line_the_template_cannot_deliver():
    """交付稿的话题行永远按项目的设置拼(写手的截到 content 个, 固定话题一定接上), 协议又不许模型改 delivery.text: 「这批
    先不带品牌话题」只这一批的, 交付稿里照样有品牌话题。说明里不能叫模型「照现在的写、再说明这批哪里不一样」——
    照现在的写就没有不一样; 得照实告诉用户这批还是照项目的设置拼的, 要的话他交付后自己改。"""
    flat = re.sub(r"\n\s*", "", tools.set_delivery_tags.__doc__)
    bullet = flat[flat.index("用户只是这一批想要别的话题"):flat.index("用户没说要改")]
    for must in ("不调。", "交付稿的话题行是按项目的设置拼的, 一批单独改不了", "这一批照 delivery_requirements 写",
                 "交付时一句话告诉用户这批的话题还是照项目的设置、和他这次要的哪里不一样", "要的话交付后他自己在稿子里改",
                 "用户说以后都这样, 再调这个工具"):
        assert must in bullet, must
    assert "说明这批和项目定的话题哪里不一样" not in flat
    # 那一句说的是真的: 只这一批不要品牌话题, 照 delivery_requirements 写出来的交付稿里品牌话题照样在
    line = delivery.assemble(delivery.validate(TPL), [_draft(4)])["text"].splitlines()[2]
    assert all(f"#{t.lstrip('#')}" in line for t in FIXED)
    assert len(delivery.assemble(delivery.validate(TPL), [_draft(6)])["text"].splitlines()[2].split()) == 4 + len(FIXED)


def test_the_docstring_says_limits_the_user_asked_past_go_back_to_the_user():
    """用户要的个数或话题超出上限(content 12、11 个固定话题、一个太长的): 报错只说上限。只有「照着改参数再调」的话, 模型会
    自己改成 10、删掉两个、截短一个, 整个项目以后都按它替用户定的那一样拼。"""
    flat = re.sub(r"\n\s*", "", tools.set_delivery_tags.__doc__)
    assert ("超出上限的(个数 0~10、固定话题最多 10 个、新加的一个最多 20 个字)是用户要的, 不自己改小、删掉几个或改短了再调: "
            "告诉用户上限, 问他要哪样。") in flat
    assert flat.index("照着改参数再调") < flat.index("告诉用户上限") < flat.index("不重试")
    assert "最多 10 个(负责人锁定的另算)" in flat
    assert f"0~{delivery.MAX_CONTENT_TAGS}" in flat and f"最多 {core.DTAGS_FIXED_MAX} 个" in flat
    assert f"最多 {core.DTAGS_TAG_CHARS} 个字" in flat


def test_record_rule_and_my_rules_send_tag_counts_to_set_delivery_tags():
    """协议正文(冻着)说「以后…」必须 record_rule; 有模板的项目里话题个数和固定话题是例外, 记成规则不生效。"""
    doc = tools.record_rule.__doc__
    for sentence in ("有交付模板的项目, 每篇几个话题、带哪几个固定话题也是模板管的, 记成规则不生效: 用户明说以后要换, 调 "
                     "set_delivery_tags 改;", "话题该写什么(跟正文、跟产品)照样记成规则。"):
        assert sentence in doc, sentence
    assert doc.index("什么时候调:") < doc.index("例外:") < doc.index("set_delivery_tags") < doc.index("severity:")
    assert "用户明说要改时用 set_delivery_tags。" in tools.my_rules.__doc__


DTAGS_TEXTS = {name: getattr(core, name) for name in dir(core) if name.startswith("DTAGS_") and isinstance(getattr(core, name), str)}
DTAGS_TEXTS["tools.set_delivery_tags"] = tools.set_delivery_tags.__doc__


def test_the_text_constants_are_all_collected():
    assert {"DTAGS_EMPTY", "DTAGS_CONTENT_BAD", "DTAGS_FIXED_NOT_LIST", "DTAGS_FIXED_BAD", "DTAGS_FIXED_LONG",
            "DTAGS_FIXED_MANY", "DTAGS_NO_TEMPLATE", "DTAGS_BROKEN", "DTAGS_UNCHANGED", "DTAGS_SUMMARY",
            "DTAGS_LOCKED_KEPT", "DTAGS_CONDITIONAL", "DTAGS_NOT_STORED", "DTAGS_NEXT_STEP",
            "DTAGS_UNCHANGED_NEXT_STEP"} <= set(DTAGS_TEXTS)


@pytest.mark.parametrize("name", sorted(DTAGS_TEXTS))
def test_texts_are_writer_safe(name):
    text = DTAGS_TEXTS[name]
    bad = [f for f in FORBIDDEN + ("归档", "archive", "写错", "你的错", "反馈给", "报告负责人", "default_params", "SQL",
                                   "service_role", "JSON", "delivery_tags_log", "delivery-format", "delivery.tags",
                                   "conditional", "TemplateError") if f in text]
    assert not bad, (name, bad)


def test_every_reply_is_writer_safe():
    from tests.test_writer_facing_text import _assert_writer_safe
    outs = [_set(_client(LOCKED_TPL), content=9, fixed=[]), _set(_client(), content=4),
            _set(_client(dict(TPL, tags={"fixed": ["甲"], "locked": ["甲"]})), fixed=[])]
    for out in outs:
        _assert_writer_safe(out, "set_delivery_tags")
        assert not any(w in json.dumps(out, ensure_ascii=False) for w in ("default_params", "delivery_tags_log", "JSON"))


def test_the_protocol_is_unchanged_so_writers_reimport_nothing():
    assert tools.protocol_text()[1] == "00ff79b30fcc"
    assert "set_delivery_tags" not in tools.PROTOCOL_PATH.read_text(encoding="utf-8")
    state = tools.skill_sync_state()
    assert state.get("in_sync", state.get("ok")) is True, state


# ── 9. 负责人那一侧: delivery-format 设锁、看留痕 ───────────────────────────────────────────────────

def test_delivery_format_sets_locked_tags_shows_them_and_the_last_writer_changes(monkeypatch, tmp_path, capsys):
    from deskcore import cli
    c = _client(params=json.dumps({"isolation_terms": ["示例词"]}, ensure_ascii=False))
    monkeypatch.setattr(core, "sb", lambda: c)
    f = tmp_path / "tpl.json"
    f.write_text(json.dumps(LOCKED_TPL, ensure_ascii=False), encoding="utf-8")
    assert cli.main(["delivery-format", "--project", PROJ, "--set", str(f)]) == 0
    printed = capsys.readouterr().out
    assert "锁定的固定话题(写手在对话里去不掉): #示例品牌、#示例必买" in printed
    assert _params(c)["delivery"]["tags"]["locked"] == ["示例品牌", "示例必买"]
    _set(c, fixed=["新话题"])
    assert cli.main(["delivery-format", "--project", PROJ, "--show"]) == 0
    shown = capsys.readouterr().out
    assert "\"delivery_tags_log\"" in shown and "示例写手" in shown and "#新话题 #示例必买 #示例品牌" in shown
    f.write_text(json.dumps(TPL, ensure_ascii=False), encoding="utf-8")
    assert cli.main(["delivery-format", "--project", PROJ, "--set", str(f)]) == 0
    assert "锁定的固定话题" not in capsys.readouterr().out, "没有锁就不多这一行"
    assert core.DELIVERY_TAGS_LOG_KEY in _params(c), "负责人重设模板, 写手改话题的留痕照留"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"layout": "blocks", "tags": {"fixed": ["甲"], "locked": ["乙"]}}, ensure_ascii=False),
                   encoding="utf-8")
    assert cli.main(["delivery-format", "--project", PROJ, "--set", str(bad)]) == 1
    assert "「乙」不在 delivery.tags.fixed 里" in capsys.readouterr().out


def test_delivery_format_without_a_log_has_no_log_key():
    c = _client()
    assert "delivery_tags_log" not in core.delivery_format(c, PROJ)
