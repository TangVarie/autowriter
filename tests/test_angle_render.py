"""卡面(angle cards v2 §3.1 / §3.2): core.render_angles_block 和 draw_angles 的 prompt_block。

钉住:
  · 协议 98–104 行要的几样一字不改: 头一行、「第i篇 [angle_key=<20 位十六进制>]」、四个维度名、「↳ 判据」、
    「写完能说出每篇用的是哪一组」、最后那行 angle_key 原样复制的话; 「以上坐标为硬约束」那一行没了;
  · 判据去掉了「标志词」那一截(回包的 boundary_rule 和卡面都是);
  · 时效: v2 抽到「通用」而没开 perpetual_bias 就不写那一行; 开了才写「通用」和排他说明;
  · 外部参考帖的标题从不出现在回包里(只给结构);
  · 盲法: 渲染函数没有来源参数; 两张值一样、来源不同(w / x)的卡渲出来一字不差; 括号里的依据只看值;
  · 参考行只有外部参考 / 起量帖 / 点名重写的牌有;
  · 反证: 去掉 draw_angles 里那道 _draw 剥离, 回包里就会带出 _draw。
"""
from __future__ import annotations

import inspect
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from deskcore import core, priors, project_now, store, vocab
from tests.test_draw_angles_v2 import PID, ME, _World, _fixed_profile, _run_profile, _hits

HEADER = "【本批次每篇的创作坐标（必须按编号对应，不得互换）】"
KEY_LINE = ("每篇方括号里的 angle_key 原样复制进这一篇查重（check_drafts）和存档（score_drafts）时的 angle_key，"
            "不要自己编号、缩写或改写；它只是给写作台记账用的，不写进标题和正文。")
SLOT = re.compile(r"^第(\d+)篇 \[angle_key=([0-9a-f]{20})\]：$", re.M)
DIM_NAMES = ("内容形式：", "标题句式：", "情绪杠杆：", "人性原型：", "切入角度：")


def _cards(block: str) -> list[str]:
    parts = SLOT.split(block)
    return [parts[i + 2] for i in range(1, len(parts) - 1, 3)]


def _card(**over):
    a = {"slot": 1, "angle_key": "0123456789abcdef0123",
         "dims": {"emotional_lever": "焦虑撬动", "human_truth_archetype": "健康焦虑",
                  "content_format": "提问求助", "title_structure": "疑问句"},
         "emotional_valence": "负向", "emotional_intensity": "medium", "trend_dependencies": [],
         "word_tilt": "口语", "angle_name": "叙事角", "angle_brief": "以一个具体的生活场景切入",
         "boundary_rule": vocab.boundary_rule_text("焦虑撬动")}
    a.update(over)
    return a


# ══════════════════════════════════════════════════════════════════════
# 协议要的几样
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("policy", ["v2", "v1"])
def test_protocol_lines_are_kept_verbatim(monkeypatch, policy):
    monkeypatch.setenv(core.DRAW_POLICY_ENV, policy)
    out = core.draw_angles(_World().client, PID, 6, user_id=ME, seed=8)
    block = out["prompt_block"]
    lines = block.splitlines()
    assert lines[0] == HEADER and lines[-1] == KEY_LINE
    assert lines[1] == "本项目现在：" + out["project_status"]
    slots = SLOT.findall(block)
    assert [(int(s), k) for s, k in slots] == [(a["slot"], a["angle_key"]) for a in out["angles"]]
    for card in _cards(block):
        for name in DIM_NAMES:
            assert f"  · {name}" in card, name
    assert "写完能说出每篇用的是哪一组" in block
    assert "硬约束" not in block and "说不出就是没按坐标写" not in block


def test_dimension_lines_carry_plain_glosses_and_intensity():
    block = core.render_angles_block([_card()])
    assert "  · 内容形式：提问求助——拿自己遇到的一件具体的事向大家讨主意" in block
    assert "  · 标题句式：疑问句——标题就是一个问句" in block
    assert "  · 情绪杠杆：焦虑撬动——担心以后可能发生、还没发生的事（浓度：中）" in block
    assert "    ↳ 判据：" in block
    assert "  · 切入角度：叙事角——以一个具体的生活场景切入" in block
    assert "全批词感（可参考）：口语" in block


def test_unknown_values_render_by_name():
    """词表外的值(老数据 / CI 里的样例)照原样印, 不报错、不带释义。"""
    block = core.render_angles_block([_card(dims={"emotional_lever": "X杠杆", "human_truth_archetype": "Y原型",
                                                  "content_format": "测评", "title_structure": "疑问式"},
                                            emotional_intensity="强")])
    for v in ("X杠杆", "Y原型", "测评", "疑问式", "（浓度：强）"):
        assert v in block


@pytest.mark.parametrize("lever", ["焦虑撬动", "恐惧撬动"])
def test_no_marker_words_in_block_or_boundary_rule(lever):
    assert "标志词" in vocab.boundary_rules_for(lever), "原文里有, 才说明去掉是这里做的"
    out = core.draw_angles(_World().client, PID, 4, user_id=ME, seed=3, pin={"情绪杠杆": lever})
    assert out["delivered"] == 4
    assert "标志词" not in out["prompt_block"]
    assert all("标志词" not in a["boundary_rule"] and a["boundary_rule"] for a in out["angles"])


# ══════════════════════════════════════════════════════════════════════
# 时效
# ══════════════════════════════════════════════════════════════════════

def test_no_trend_line_for_a_sampled_tongyong_without_perpetual(monkeypatch):
    prof = _run_profile()
    prof["extras"]["trend_dependencies"] = {v: (1.0 if v == vocab.TREND_EXCLUSIVE else 0.0)
                                            for v in vocab.TREND_DEPENDENCIES}
    _fixed_profile(monkeypatch, prof)
    out = core.draw_angles(_World().client, PID, 5, user_id=ME, seed=1)
    assert "时效依赖" not in out["prompt_block"]
    assert all(a["trend_dependencies"] == [] for a in out["angles"]), "抽到「通用」= 不要求"
    out = core.draw_angles(_World().client, PID, 5, user_id=ME, seed=1, perpetual_bias=True)
    assert out["prompt_block"].count(f"↳ 时效依赖：{vocab.TREND_EXCLUSIVE}（排他值") == 5
    assert all(a["trend_dependencies"] == [vocab.TREND_EXCLUSIVE] for a in out["angles"])


def test_other_trend_values_render_without_the_exclusive_note():
    block = core.render_angles_block([_card(trend_dependencies=["季节性事件"])])
    assert "↳ 时效依赖：季节性事件（跟当下这个季节的事挂钩）" in block and "排他值" not in block


# ══════════════════════════════════════════════════════════════════════
# 外部参考: 只给结构, 标题不出进程
# ══════════════════════════════════════════════════════════════════════

SECRET_TITLE = "合成标题甲乙丙丁戊己庚辛壬癸"


def _external_profile():
    now = datetime.now(timezone.utc)
    rows, answers = [], {}
    for i in range(12):
        posted = now - timedelta(days=20 + i)
        nid = f"{int(posted.timestamp()):08x}" + f"{i:016x}"
        rows.append({"note_id": nid, "category": "运动补给", "title": f"{SECRET_TITLE}{i}？", "comments": 400 - i,
                     "fetched_at": (now - timedelta(days=1)).isoformat(), "ad_like": False})
        answers[nid] = {"opening_type": "身份自述", "turning_point": "是", "ending_asks_reader": "是"}
    ext = priors.external_pool(rows, answers, category="运动补给", now=now, registered=())
    ext.update(state="ok", ok=True)
    assert ext["eligible"] >= 5
    return _run_profile(ext=ext)


def test_an_external_title_never_reaches_the_output(monkeypatch):
    _fixed_profile(monkeypatch, _external_profile())
    found_ext = False
    for seed in range(5):
        out = core.draw_angles(_World().client, PID, 10, user_id=ME, seed=seed)
        blob = json.dumps(out, ensure_ascii=False)
        assert SECRET_TITLE not in blob and SECRET_TITLE not in out["prompt_block"]
        ext_cards = [a for a in out["angles"] if (a.get("reference") or {}).get("kind") == "external"]
        found_ext |= bool(ext_cards)
        for a in ext_cards:
            assert a["dims"]["title_structure"] == "疑问句", "外部卡的标题句式照参考帖的(Jev 没答问句就按正则分)"
        if ext_cards:
            assert "参考结构（同品类别家近期起量的一篇" in out["prompt_block"]
            assert "只借结构，不抄内容，不提那篇帖子里的品牌。" in out["prompt_block"]
    assert found_ext


# ══════════════════════════════════════════════════════════════════════
# 盲法 + 依据 + 参考行
# ══════════════════════════════════════════════════════════════════════

def test_render_has_no_source_parameter():
    assert list(inspect.signature(core.render_angles_block).parameters) == ["angles", "status"]


def test_explore_and_weighted_cards_with_the_same_values_render_identically():
    prof = _run_profile()
    dims = {"emotional_lever": "愤怒撬动", "human_truth_archetype": "健康焦虑", "content_format": "提问求助",
            "title_structure": "疑问句"}
    ev = project_now.card_evidence(prof, dims)
    w = _card(dims=dims, evidence=ev, src="w", _draw={"src": "w", "p": 0.01})
    x = _card(dims=dims, evidence=project_now.card_evidence(prof, dict(dims)), src="x", _draw={"src": "x", "p": 0.2})
    assert core.render_angles_block([w], status="s") == core.render_angles_block([x], status="s")
    assert ev == {"content_format": "本项目近60天约九成是这一类", "title_structure": "本项目近60天约三分之二是问句",
                  "human_truth_archetype": "本项目最常写的一种"}


def test_evidence_only_from_fifteen_percent_up():
    prof = _run_profile()
    assert project_now.evidence_text(prof, "content_format", "场景植入") is None           # 0.12
    assert project_now.evidence_text(prof, "title_structure", "场景直述") == "本项目近60天约两成用这种"
    assert project_now.evidence_text(prof, "human_truth_archetype", "消费愉悦") == "本项目常写"
    assert project_now.evidence_text(prof, "human_truth_archetype", "经济焦虑") is None     # 0.10
    assert project_now.evidence_text(prof, "emotional_lever", "愤怒撬动") is None, "杠杆的括号是浓度"
    assert project_now.evidence_text(priors.uniform_profile(PID), "content_format", "提问求助") is None


def test_reference_lines_only_for_external_hit_and_repeat():
    plain = core.render_angles_block([_card()])
    assert "参考结构" not in plain and core.REPEAT_LINE not in plain
    hit = core.render_angles_block([_card(reference={
        "kind": "hit", "own": True, "age_days": 5, "title_structure": "疑问句",
        "desc": {"opening_type": "具体事件", "ending_asks_reader": True, "asks_for_help": None, "turning_point": None}})])
    assert ("    ↳ 参考结构（本项目近三周评论数排在同类帖前一成的一篇，发出5天）：开头先讲一件具体的事；结尾问了读者。"
            "只借结构，不抄说法。") in hit
    ext = core.render_angles_block([_card(reference={
        "kind": "external", "title_structure": "数字清单", "posted_days_ago": 21, "comments_per_day": 14.2,
        "desc": {"opening_type": "身份自述", "turning_point": True, "ending_asks_reader": True}})])
    assert ("    ↳ 参考结构（同品类别家近期起量的一篇，约3周前发，每天约14条评论）：标题是数字清单；开头先讲自己的身份；"
            "中间有一次前后转折；结尾问了读者。只借结构，不抄内容，不提那篇帖子里的品牌。") in ext
    rep = core.render_angles_block([_card(reference={"kind": "repeat", "of": "0123456789abcdef0123"})])
    assert "    ↳ " + core.REPEAT_LINE in rep
    # 「其他」开头不说; 什么结构都没有的起量帖不出参考行
    bare = core.render_angles_block([_card(reference={"kind": "hit", "own": False, "age_days": 9,
                                                      "desc": {"opening_type": "其他"}})])
    assert "参考结构" not in bare


def test_hit_cards_from_a_draw_get_their_reference_line(monkeypatch):
    _fixed_profile(monkeypatch, _run_profile(hits=_hits(5)))
    got = 0
    for seed in range(6):
        out = core.draw_angles(_World().client, PID, 10, user_id=ME, seed=seed)
        hits = [a for a in out["angles"] if (a.get("reference") or {}).get("kind") == "hit"]
        got += len(hits)
        assert out["prompt_block"].count("参考结构（本项目近三周") == len(hits)
    assert got


# ══════════════════════════════════════════════════════════════════════
# 反证: 剥 _draw 那一道真的在起作用
# ══════════════════════════════════════════════════════════════════════

def test_removing_the_draw_strip_leaks_the_block(monkeypatch):
    monkeypatch.setattr(store, "public_dims", lambda d: d)
    out = core.draw_angles(_World().client, PID, 3, user_id=ME, seed=1)
    assert any("_draw" in a["dims"] for a in out["angles"]), "不剥就漏 —— 回包只剩四个键的断言因此能红"


def test_external_reference_line_does_not_ban_the_projects_own_brand():
    """外部参考卡也可能分到产品向的形式(场景植入: 要点品牌名), 参考行只说不提「那篇帖子里的」品牌, 不读成连本项目的
    品牌都不许提。"""
    block = core.render_angles_block([_card(
        dims={"emotional_lever": "愤怒撬动", "human_truth_archetype": "健康焦虑", "content_format": "场景植入",
              "title_structure": "数字清单"},
        reference={"kind": "external", "title_structure": "数字清单", "posted_days_ago": 9, "comments_per_day": 3,
                   "desc": {"opening_type": "身份自述"}})])
    assert "不提任何品牌" not in block and "只借结构，不抄内容，不提那篇帖子里的品牌。" in block
