"""回流闸 v1 的判法(deskcore/postgate.py, 纯函数): 来源、结论、原因、哪个原因挡哪一路。

这里钉的是口径: 名字(TV 那边的馆员和灯按名字读, 改名等于改接口)、检查的先后、两条线、「没有结论 = 照旧」。
接线(tv-sync 的最后一步怎么读、怎么写)在 tests/test_tv_sync_gate.py。文字全是编的。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from deskcore import postgate as G
from deskcore import priors, tvlink as L

NOW = datetime(2026, 11, 12, 14, 0, tzinfo=timezone.utc)


def _note(nid="n1", *, days_ago=5.0, tier="趴", flags=None, chars=300, **extra):
    t = None if days_ago is None else (NOW - timedelta(days=days_ago)).isoformat()
    return {"note_id": nid, "publish_time": t, "tier": tier, "data_quality_flags": flags, "body_chars": chars, **extra}


def _judge(note, *, vanished=(), boosted=(), efficacy=None):
    return G.classify(note, vanished=set(vanished), boosted=frozenset(boosted),
                      efficacy={"n1": "否"} if efficacy is None else efficacy, now=NOW)


# ── 名字: 存进库、别的仓按它读, 不许悄悄改 ───────────────────────────────────────────────

def test_the_names_other_readers_depend_on():
    assert G.ORIGINS == ("desk", "desk_edited", "copied_back", "elsewhere", "unclear")
    assert G.GATES == ("pass", "fail", "unsure", "hold", "withdrawn")
    assert G.REASONS == ("withdrawn", "hold", "not_clean", "risk", "efficacy", "unsure")
    assert G.NOT_LEARNABLE == {"fail", "unsure", "hold", "withdrawn"} and G.PENDING_GATES == {"hold", "unsure"}
    assert (G.ELSEWHERE_MAX, G.DESK_TITLE_MIN, G.MIN_BODY_CHARS) == (0.15, 0.30, 1)
    # 两条线就是匹配器里已经在用的那两条
    assert G.DESK_TITLE_MIN == L.EDITED_FLOOR and G.ELSEWHERE_MAX == pytest.approx(L.EDITED_FLOOR - L.EDITED_MARGIN)
    assert G.MATCHED_KINDS | G.MISS_KINDS | {"ambiguous"} == {
        "body_exact", "title_exact", "fuzzy", "edited", "ingested", "ambiguous", "unmatched", "tv_lineage"}, \
        "match_kind 的 CHECK 里每一种都有去处"


def test_each_consumer_has_one_blocking_set_and_cards_do_not_block_on_not_clean():
    """decisions 第 5 条: 写作台的两处六个原因都挡(not_clean 今天就按 priors.is_clean 挡着); 教训卡不看 not_clean
    (D-133 第 1 条: 书架借不借投过流的帖是老板的事, 还没定) —— 以后定了, 是这一个常数的一行改动。"""
    assert G.BLOCKS_DESK == set(G.REASONS)
    assert G.BLOCKS_CARDS == set(G.REASONS) - {"not_clean"}
    assert G.BLOCKS_DESK - G.BLOCKS_CARDS == {"not_clean"}


# ── 结论: 五步, 第一个成立的定 ────────────────────────────────────────────────────────────

def test_a_plain_post_passes_with_no_reasons():
    assert _judge(_note()) == G.Verdict("pass", (), ())


@pytest.mark.parametrize("flags, boosted", [
    ({"paid_promoted": True}, ()),
    ({"paid_promoted": "true"}, ()),
    ({"comment_maintained_routes": ["铺评工单"]}, ()),
    ({}, ("n1",)),                                      # 投流事件里的帖: 没标 paid_promoted 也不干净
])
def test_not_clean_fails(flags, boosted):
    v = _judge(_note(flags=flags), boosted=boosted)
    assert v == G.Verdict("fail", ("not_clean",), ())


def test_risk_tier_and_an_efficacy_promise_fail():
    assert _judge(_note(tier="风控")) == G.Verdict("fail", ("risk",))
    assert _judge(_note(), efficacy={"n1": "是"}) == G.Verdict("fail", ("efficacy",))
    both = _judge(_note(tier="风控", flags={"paid_promoted": True}), efficacy={"n1": "是"})
    assert both == G.Verdict("fail", ("not_clean", "risk", "efficacy")), "成立的原因全列上, 按登记的顺序"


def test_a_missing_efficacy_answer_is_unsure_and_is_listed_next_to_a_fail_reason():
    assert _judge(_note(), efficacy={}) == G.Verdict("unsure", ("unsure",))
    assert _judge(_note(), efficacy={"n1": ""}) == G.Verdict("unsure", ("unsure",)), "空答案不算答了"
    assert _judge(_note(), efficacy={"n2": "否"}) == G.Verdict("unsure", ("unsure",)), "别的帖的答案不算"
    # 投了流、功效题又还没答: 结论是 fail, 但 unsure 也在原因里 —— 教训卡不看 not_clean, 靠它在答案到之前不借这篇
    v = _judge(_note(flags={"paid_promoted": True}), efficacy={})
    assert v == G.Verdict("fail", ("not_clean", "unsure"))
    assert not G.learnable(v.gate, G.pack_reasons(v), blocking=G.BLOCKS_CARDS)


@pytest.mark.parametrize("note, detail", [
    (_note(days_ago=None), "no_publish_time"),
    (_note(days_ago=-0.5), "scheduled"),                # 发布时间在未来: 排了期还没发
    (_note(chars=0), "short_body"),                     # 正文是空的: 文字还没到
    (_note(flags={"synthetic": True}), "synthetic"),
])
def test_hold(note, detail):
    assert _judge(note) == G.Verdict("hold", ("hold",), (detail,))


def test_hold_edges():
    assert _judge(_note(chars=1)).gate == "pass", "有字就不是「文字还没到」"
    assert _judge(_note(chars=None)).gate == "pass", "没给正文长度(这晚读不到正文)不因此判 hold"
    assert _judge(_note(days_ago=0)).gate == "pass", "发布时间就是现在: 算发了(同 priors.published_at 的上界)"
    v = _judge(_note(days_ago=None, chars=0, flags={"synthetic": True}))
    assert v == G.Verdict("hold", ("hold",), ("no_publish_time", "short_body", "synthetic"))


def test_a_short_finished_post_is_judged_by_the_other_checks():
    """hold 是「还没到齐」: 没有发布时间、排了期、正文是空的。发出去的短帖(一句问话加话题)正文不会再变长 —— 按字数
    放着等于永远放着, 而 hold 每晚重判、两路都挡, 一批起量的短帖和它们的教训卡就这么没了。有字的走后面三步。"""
    old_short = _note(days_ago=90, tier="大爆", chars=40)
    assert _judge(old_short) == G.Verdict("pass", (), ())
    assert G.learnable("pass", G.pack_reasons(_judge(old_short)), blocking=G.BLOCKS_CARDS)
    assert _judge(_note(days_ago=2, chars=1)).gate == "pass", "刚发的也一样: 值不值得学(发出三天以上)各处自己有线"
    # 短不是豁免: 后面三步照查
    assert _judge(_note(chars=40, tier="风控")) == G.Verdict("fail", ("risk",))
    assert _judge(_note(chars=40), efficacy={"n1": "是"}) == G.Verdict("fail", ("efficacy",))
    assert _judge(_note(chars=40, flags={"paid_promoted": True})) == G.Verdict("fail", ("not_clean",))
    assert _judge(_note(chars=40), efficacy={}) == G.Verdict("unsure", ("unsure",))
    assert _judge(_note(days_ago=90, chars=0)) == G.Verdict("hold", ("hold",), ("short_body",)), "空的还是等"


def test_the_order_of_checks():
    """withdrawn 先于 hold 先于 fail 先于 unsure 先于 pass —— 每一步拿一篇后面几步也都成立的帖来问。"""
    everything = _note(days_ago=None, chars=0, tier="风控", flags={"paid_promoted": True, "synthetic": True})
    assert _judge(everything, vanished=("n1",), efficacy={}) == G.Verdict("withdrawn", ("withdrawn",))
    assert _judge(everything, efficacy={}).gate == "hold"
    assert _judge(_note(chars=0, tier="风控"), efficacy={"n1": "是"}) == G.Verdict("hold", ("hold",), ("short_body",)), \
        "hold 的不往下查: 原因里没有 risk / efficacy"
    assert _judge(_note(tier="风控"), efficacy={}).gate == "fail"
    assert _judge(_note(), efficacy={}).gate == "unsure"
    assert _judge(_note(), vanished=("n2",)).gate == "pass", "别的帖删了不关它的事"


@pytest.mark.parametrize("flags", [None, {}, {"paid_promoted": True}, {"paid_promoted": "TRUE"}, {"paid_promoted": "no"},
                                   {"comment_maintained_routes": ["铺评工单", "别的"]},
                                   {"comment_maintained_routes": "铺评工单"}, {"comment_maintained": True},
                                   '{"paid_promoted": true}', "not json", {"tier_cleared": True}])
@pytest.mark.parametrize("boosted", [frozenset(), frozenset({"n1"}), frozenset({"n9"})])
def test_not_clean_is_exactly_what_priors_is_clean_says(flags, boosted):
    """一把尺: 干不干净只问 priors.is_clean(同一行、同一个投流事件集合), 这里不另写一遍。合成的在第 2 步就 hold 了, 不在此列。"""
    row = _note(flags=flags)
    v = G.classify(row, boosted=boosted, efficacy={"n1": "否"}, now=NOW)
    assert ("not_clean" in v.reasons) == (not priors.is_clean(row, boosted))
    assert v.gate == ("pass" if priors.is_clean(row, boosted) else "fail")


def test_origin_is_not_an_input_to_the_class():
    """来源只是标签: classify 的参数里根本没有对照。同一篇帖, 写作台写的和别处写的结论一样。"""
    import inspect
    assert set(inspect.signature(G.classify).parameters) == {"note", "vanished", "boosted", "efficacy", "now"}


# ── 来源 ──────────────────────────────────────────────────────────────────────────────

def _link(kind, *, version="v1", score=None, lag=None):
    return {"match_kind": kind, "version_id": version, "score": score, "lag_days": lag}


@pytest.mark.parametrize("link, origin", [
    (_link("body_exact", score=1.0, lag=2), "desk"),
    (_link("fuzzy", score=0.7, lag=0), "desk"),
    (_link("tv_lineage", lag=None), "desk"),            # lag 没有的按没晚于发布算(同 core._diffable)
    (_link("tv_lineage", version=None), "unclear"),     # TV 带的血缘指到认不出的版本
    (_link("edited", score=0.45, lag=3), "desk_edited"),
    (_link("title_exact", score=0.30, lag=1), "desk"),
    (_link("title_exact", score=0.299, lag=1), "unclear"),   # 只有标题对上、正文不像
    (_link("title_exact", score=None, lag=1), "unclear"),
    (_link("body_exact", score=1.0, lag=-1), "copied_back"),
    (_link("edited", score=0.5, lag=-6), "copied_back"),
    (_link("title_exact", score=0.0, lag=-2), "copied_back"),
    (_link("ingested", score=0.0), "elsewhere"),
    (_link("ingested", score=0.149), "elsewhere"),
    (_link("ingested", score=0.15), "unclear"),
    (_link("ingested", score=0.9), "unclear"),
    (_link("ingested", score=None), "unclear"),         # 还没有分数
    (_link("unmatched", version=None, score=0.02), "elsewhere"),
    (_link("unmatched", version=None, score=None), "unclear"),
    (_link("ambiguous", version=None, score=0.4), "unclear"),
    ({"match_kind": None}, "unclear"),
    ({}, "unclear"),
])
def test_origin_of(link, origin):
    assert G.origin_of(link) == origin
    assert origin in G.ORIGINS


def test_a_copy_back_does_not_read_as_rewritten_from_a_desk_draft():
    """decisions 第 4 条: 存着的 score 把发了之后才抄回写作台的那版也算上(接近 1); 定来源只认发布前就在的文字。"""
    link = _link("ingested", score=0.97)
    assert G.origin_of(link) == "unclear", "只有 score: 看着像拿写作台的稿改的"
    assert G.origin_of(link, 0.0) == "elsewhere", "量过发布前的重合: 写作台里那版是抄回来的"
    assert G.origin_of(link, 0.4) == "unclear"
    # 反过来: 帖排期在后面、先被抄进指纹库(score 0), 发布前又被存进写作台(prior 接近 1) —— 也是抄回来的
    assert G.origin_of(_link("ingested", score=0.0), 0.95) == "elsewhere"
    assert G.origin_of(_link("ingested", score=None), 0.03) == "elsewhere", "没有 score 就只看发布前的重合"
    # 对上了的行不看这个数
    assert G.origin_of(_link("body_exact", score=1.0, lag=1), 0.0) == "desk"


def test_origin_overlap_takes_the_smaller_of_the_two():
    assert G.origin_overlap(None, None) is None
    assert G.origin_overlap(0.4, None) == 0.4 and G.origin_overlap(None, 0.1) == 0.1
    assert G.origin_overlap(0.9, 0.05) == 0.05 and G.origin_overlap(0.0, 0.8) == 0.0
    assert G.origin_overlap("0.25", 0.3) == 0.25 and G.origin_overlap("x", True) is None


# ── 存进库的样子, 和读它 ─────────────────────────────────────────────────────────────────

def test_pack_reasons_holds_names_and_one_number_only():
    assert G.pack_reasons(G.Verdict("pass")) == {"reasons": []}
    assert G.pack_reasons(G.Verdict("fail", ("efficacy", "not_clean"))) == {"reasons": ["not_clean", "efficacy"]}, \
        "按登记的顺序存, 两晚算出来的顺序不同也不算变了"
    assert G.pack_reasons(G.Verdict("hold", ("hold",), ("short_body",)), overlap=0.03149) == {
        "reasons": ["hold"], "hold": ["short_body"], "overlap": 0.031}
    packed = G.pack_reasons(G.Verdict("fail", ("risk", "这不是登记过的名字")), overlap="没有数")
    assert packed == {"reasons": ["risk"]}, "没登记的名字、不是数的重合都存不进去"
    assert json.loads(json.dumps(packed)) == packed


@pytest.mark.parametrize("stored, want", [
    ({"reasons": ["not_clean", "unsure"]}, ("not_clean", "unsure")),
    ('{"reasons": ["efficacy"]}', ("efficacy",)),       # jsonb 有的客户端回字符串
    (["hold"], ("hold",)),
    ({"reasons": []}, ()), (None, ()), ("", ()), ("not json", ()), ({"reasons": "risk"}, ()), (7, ()),
    ({"reasons": ["risk", None, 3, ""]}, ("risk",)),
])
def test_reasons_of_reads_what_is_stored(stored, want):
    assert G.reasons_of(stored) == want


def test_stored_overlap():
    assert G.stored_overlap({"reasons": [], "overlap": 0.031}) == 0.031
    assert G.stored_overlap('{"reasons": [], "overlap": 0}') == 0.0
    assert G.stored_overlap({"reasons": []}) is None and G.stored_overlap(None) is None and G.stored_overlap([]) is None


def test_learnable_no_verdict_is_open_and_a_reviewed_post_is_closed_by_reason():
    # 没有结论: 列还没加 / 没判过 / 不认识的值 —— 照没有闸时的口径
    for gate in (None, "", "something_new"):
        assert G.learnable(gate) and G.learnable(gate, {"reasons": ["efficacy"]}, blocking=G.BLOCKS_CARDS)
    assert G.learnable("pass", {"reasons": []}) and G.learnable("pass", None)
    for reason, gate in (("withdrawn", "withdrawn"), ("hold", "hold"), ("risk", "fail"), ("efficacy", "fail"),
                         ("unsure", "unsure")):
        assert not G.learnable(gate, {"reasons": [reason]})
        assert not G.learnable(gate, {"reasons": [reason]}, blocking=G.BLOCKS_CARDS), reason
    # not_clean: 写作台的两处挡, 教训卡不挡
    assert not G.learnable("fail", {"reasons": ["not_clean"]})
    assert G.learnable("fail", {"reasons": ["not_clean"]}, blocking=G.BLOCKS_CARDS)
    assert not G.learnable("fail", {"reasons": ["not_clean", "efficacy"]}, blocking=G.BLOCKS_CARDS)
    # 判过、没放行, 原因读不出来 / 有不认识的名字: 不放行
    assert not G.learnable("fail", None, blocking=G.BLOCKS_CARDS)
    assert not G.learnable("fail", {"reasons": ["not_clean", "新加的原因"]}, blocking=G.BLOCKS_CARDS)


def test_blocked_reads_a_link_row():
    assert not G.blocked(None) and not G.blocked({}) and not G.blocked({"gate": None, "gate_reasons": None})
    row = {"gate": "fail", "gate_reasons": {"reasons": ["not_clean"]}}
    assert G.blocked(row) and G.blocked(row, G.BLOCKS_DESK) and not G.blocked(row, G.BLOCKS_CARDS)
    assert not G.blocked({"gate": "pass", "gate_reasons": {"reasons": []}})


def test_the_module_is_pure():
    """postgate 不碰库、不发请求: 它 import 的只有标准库和 priors(纯算的那几个函数)。"""
    import ast
    from pathlib import Path
    tree = ast.parse(Path(G.__file__).read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module.split(".")[0] if node.module else "." + ",".join(a.name for a in node.names))
    assert mods == {"__future__", "json", "datetime", "typing", ".priors"}, mods


# ── 发布前写作台里的文字(tvlink.prior_overlap) ─────────────────────────────────────────────

T0 = datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc)      # 北京 12:00
BODY = ("周末第一次去打网球, 教练让我先对着墙打两百个正手, 打到一半手腕就酸得抬不起来。"
        "回家查了才知道握拍太紧, 第二周换了松一点的握法, 墙打到三百个也不累。")
OTHER = ("这一篇讲的是另一件事: 面试前一晚把自我介绍念给室友听, 室友听到第三句就睡着了。"
         "第二天我把前三句删了, 面试官第一次在我说完之前就开始点头。")


def _v(vid, body, days):
    return L.Version(version_id=vid, project_id="p", title="t", body=body,
                     created_at=None if days is None else T0 + timedelta(days=days))


def _n(body, days=10):
    return L.Note(note_id="n", title="x", body=body, publish_time=None if days is None else T0 + timedelta(days=days))


def test_prior_overlap_counts_only_desk_text_from_before_publication():
    note = _n(BODY, days=10)
    before, after = _v("v1", BODY, 8), _v("v2", BODY, 12)
    assert L.prior_overlap(note, L.VersionIndex([before])) == 1.0
    assert L.prior_overlap(note, L.VersionIndex([after])) == 0.0, "发了之后才存的那版不算"
    assert L.match_note(note, L.VersionIndex([after])).score == 1.0, "而对不对得上那一路是算它的: 两个数量的不是一回事"
    assert L.prior_overlap(note, L.VersionIndex([_v("v3", BODY, 10)])) == 1.0, "同一天写、同一天发的算在内(北京日历日)"
    assert L.prior_overlap(note, L.VersionIndex([_v("v4", BODY, -25)])) == 0.0, "早过时间窗(30 天)的不算"
    assert L.prior_overlap(note, L.VersionIndex([_v("v5", BODY, None)])) == 0.0, "没有时间的版本说不清在不在发布之前"
    assert L.prior_overlap(note, L.VersionIndex([_v("v6", OTHER, 8), after])) == pytest.approx(
        L.overlap(note, _v("v6", OTHER, 8)))


def test_prior_overlap_counts_snapshots_and_knows_when_it_cannot_tell():
    note = _n(BODY, days=10)
    snap = _v("s1", BODY, 9)
    assert L.prior_overlap(note, L.VersionIndex([]), [snap]) == 1.0, "只查过重、没存档的底稿也是写作台的文字"
    assert L.prior_overlap(note, L.VersionIndex([]), [_v("s2", BODY, 11)], L.VersionIndex([])) == 0.0
    assert L.prior_overlap(note, L.VersionIndex([])) == 0.0
    assert L.prior_overlap(_n(BODY, days=None), L.VersionIndex([_v("v1", BODY, 8)])) is None, "没有发布时间: 说不出"
    assert L.prior_overlap(_n("太短了"), L.VersionIndex([_v("v1", BODY, 8)])) is None, "太短没得比"
