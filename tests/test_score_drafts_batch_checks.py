"""score_drafts 的写法检查(2026-11-10, docs/deskcore.md §3.21) —— 从 commit_drafts 走一遍。

钉的是:
  · 同一批里的重复说法、正文里混进来的版式残留进 ``draft_checks``(有才带), 下标是写手传的位置, 被拒的那篇不算;
    next_step 接在规则检查那几句后面: 先改再交, 和合规检查、参考分分开算;
  · 项目规则原文里的说法不算重复; 规则原文只在头一遍查出重复时才读, 读失败这一次不报重复说法、存档照常;
  · 有交付模板时【标题】【正文】只在 delivery.problems 里报一次; 没模板时 draft_checks 报;
  · 项目规则「必须带的话术」检查里列的那一句也不算重复(规则原文没写出那一句时也一样);
  · 交付模板不拼评论 / 不管话题时, 评论段、【话题】的说法不许诺写作台会加(照做的话评论、话题就从交付稿里没了);
  · 这次改过重存还查出来的(重复说法或版式残留): 改一轮就够, 只照实说一句; 重复说法要改的落到没改过的新稿上;
  · 开头一样的只进 ``batch_hints``(提示), 不进 draft_checks;
  · 这一块出什么错都不抛、不带半截的键; 花的时间算进 phase_ms.delivery_rules, phase_ms 的键不变。

所有示例文本都是合成的(示例牌 / 示例品牌)。
"""

from __future__ import annotations

import time

import pytest

import config
from deskcore import core, poststyle, store
from tests.test_delivery import BLOCKS, ME, PROJ, _params_with, desk_client, distinct_drafts
from tests.test_delivery_revision import _embed
from tests.test_rulecheck import _rule
from tests.test_writer_facing_text import _assert_writer_safe

P = "周末带娃去公园晒太阳心情很好"              # 14 个字: 7 个 8 字串
PHASES = {"lock_wait", "commit", "delivery_rules", "judge", "project_isolation", "total"}


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    monkeypatch.setattr(config, "JUDGE_URL", "")


def _drafts(n: int, carriers=(), seed: int = 0, phrase: str = P) -> list[dict]:
    """n 篇开头各不相同的合成稿(不触发开头一样的提示), ``carriers`` 那几篇末尾加同一个说法。"""
    out = distinct_drafts(n, seed=seed)
    for i, d in enumerate(out):
        d["body"] = f"第{i}篇先说点别的，" + d["body"] + (phrase + "。" if i in carriers else "")
    return out


def _meta(c) -> dict:
    return c.rows["batch_metrics"][-1]["meta"]


def _repeat_what(first: str, allowed: int, n: int, quote: str = P) -> str:
    return (f"「{quote}」index 是 {first} 的 {allowed} 篇已经用过, 同一批 {n} 篇里同一个说法最多 {allowed} 篇用: "
            "这篇换个说法或换件事说(项目规则要求照原样写的话术不用改)")


def _spy_memories(monkeypatch, *, fail_plain: bool = False) -> list[dict]:
    """记下每次读规则用的参数; ``fail_plain``: 只让写法检查那一次(vectors=False、不带 rule_kinds)失败。"""
    calls: list[dict] = []
    real = store.shared_memories

    def spy(*a, **kw):
        calls.append(kw)
        if fail_plain and kw.get("vectors") is False and not kw.get("rule_kinds"):
            raise RuntimeError("示例: 规则表读不到")
        return real(*a, **kw)
    monkeypatch.setattr(store, "shared_memories", spy)
    return calls


def _plain_reads(calls: list[dict]) -> int:
    return sum(1 for kw in calls if kw.get("vectors") is False and not kw.get("rule_kinds"))


# ══════════════════════════════════════════════════════════════════════
# 1 · 重复说法
# ══════════════════════════════════════════════════════════════════════

def test_a_phrase_in_three_of_five_flags_the_third_after_the_rule_tips():
    c = desk_client(memories=[_rule(1, "forbidden_word", {"words": ["示例禁词"]})])
    drafts = _drafts(5, carriers={0, 1, 2})
    drafts[4]["body"] += "示例禁词"
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert out["written"] == 5
    assert out["draft_checks"] == [{"index": 2, "kind": "repeat", "what": _repeat_what("0、1", 2, 5)}]
    assert "batch_hints" not in out
    step = core.DRAFT_CHECKS_NEXT_STEP.format(idx="2", n=1)
    assert out["next_step"].endswith(step)
    assert out["next_step"].index(core.RULE_CHECKS_NEXT_STEP) < out["next_step"].index(step), "接在规则检查那几句后面"
    assert "改一轮就够" not in out["next_step"]
    meta = _meta(c)["draft_checks"]
    assert meta["repeat"] == {"need": 3, "grams": 7, "exempted": 0, "carriers": 3, "flagged": 1, "once": 0}
    assert meta["drafts"] == 5 and meta["format"] == {} and meta["opener"] == {"need": 5, "top": 1, "hinted": False}
    assert set(out["phase_ms"]) == PHASES
    _assert_writer_safe(out, "score_drafts draft_checks")


def test_indices_are_the_callers_and_a_rejected_draft_does_not_count():
    c = desk_client()
    old = _drafts(1, carriers={0}, seed=9)[0]
    core.commit_drafts(c, PROJ, [old], user_id=ME)
    fresh = _drafts(4, carriers={0, 1, 2}, seed=3)
    out = core.commit_drafts(c, PROJ, [{**old, "title": "换了个标题"}] + fresh, user_id=ME)
    assert [r["index"] for r in out["rejected"]] == [0]
    assert out["draft_checks"] == [{"index": 3, "kind": "repeat", "what": _repeat_what("1、2", 2, 4)}]
    assert core.DRAFT_CHECKS_NEXT_STEP.format(idx="3", n=1) in out["next_step"]


def test_a_phrase_from_the_projects_rules_is_not_a_repeat_and_rules_are_read_only_when_needed(monkeypatch):
    calls = _spy_memories(monkeypatch)
    rule = _rule(1, content=f"售后统一这么说: {P}", severity="soft")
    c = desk_client(memories=[rule])
    out = core.commit_drafts(c, PROJ, _drafts(5, carriers={0, 1, 2}), user_id=ME)
    assert "draft_checks" not in out and "draft_checks" not in out["next_step"]
    assert _plain_reads(calls) == 1
    assert _meta(c)["draft_checks"]["repeat"] == {"need": 3, "grams": 0, "exempted": 7, "carriers": 0, "flagged": 0,
                                                  "once": 0}
    calls.clear()
    clean = core.commit_drafts(c, PROJ, _drafts(5, seed=4), user_id=ME)
    assert clean["written"] == 5 and "draft_checks" not in clean
    assert _plain_reads(calls) == 0, "一句都不重复的批次不为它多读一次规则"


@pytest.mark.parametrize("payload", [{"any": ["有问题随时来私信客服一直都在线"], "where": ["body"]},
                                     {"target": "有问题随时来私信客服一直都在线"}])
def test_a_phrase_a_rules_check_requires_is_not_a_repeat(payload):
    """规则原文只说「结尾带上售后那一句」, 那一句只在 required_phrase 检查里: 照样不算重复 —— 不然这边叫第 3 篇起换个说法,
    照改了 rule_checks 又报「必须改: 没有那一句」。"""
    phrase = "有问题随时来私信客服一直都在线"
    c = desk_client(memories=[_rule(1, "required_phrase", payload, content="每篇正文结尾都带上售后那一句")])
    out = core.commit_drafts(c, PROJ, _drafts(5, carriers=set(range(5)), phrase=phrase), user_id=ME)
    assert out["written"] == 5
    assert "rule_checks" not in out and "draft_checks" not in out
    rep = _meta(c)["draft_checks"]["repeat"]
    assert rep["grams"] == 0 and rep["exempted"] > 0


def test_if_the_rules_cannot_be_read_repeats_are_not_reported_and_the_batch_is_stored(monkeypatch):
    _spy_memories(monkeypatch, fail_plain=True)
    c = desk_client()
    drafts = _drafts(5, carriers={0, 1, 2})
    drafts[4]["body"] += "<br>"
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert out["written"] == 5
    assert [r["kind"] for r in out["draft_checks"]] == ["br"], "版式残留照报, 重复说法这一次不报"
    rep = _meta(c)["draft_checks"]["repeat"]
    assert rep["grams"] == 7 and "RuntimeError" in rep["error"] and "flagged" not in rep


# ══════════════════════════════════════════════════════════════════════
# 2 · 版式残留
# ══════════════════════════════════════════════════════════════════════

def test_with_a_template_the_markers_are_reported_once_in_delivery_problems():
    c = desk_client(default_params=_params_with(BLOCKS))
    drafts = _drafts(2)
    for d in drafts:
        d["tags"] = ["话题甲"]
    drafts[1]["body"] = "【正文】" + drafts[1]["body"].replace("。", "。<br>", 1)
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert [p["index"] for p in out["delivery"]["problems"]] == [1]
    assert "【正文】" in out["delivery"]["problems"][0]["what"]
    assert out["draft_checks"] == [{"index": 1, "kind": "br",
                                    "what": "正文里有字面的「<br>」(多半是从交付表格里复制回来的): 换成真的换行"}]
    ns = out["next_step"]
    assert ns.index(core.DELIVERY_NEXT_STEP) < ns.index(core.DRAFT_CHECKS_NEXT_STEP.format(idx="1", n=1))
    assert _meta(c)["draft_checks"]["format"] == {"br": 1}
    _assert_writer_safe(out, "score_drafts draft_checks (template)")


def test_without_a_template_labels_and_comment_blocks_are_reported_here():
    c = desk_client()
    drafts = _drafts(3)
    drafts[0]["body"] = "【正文】" + drafts[0]["body"].replace("。", "。<br>", 1)
    drafts[2]["body"] += "【素人评论】示例牌用着挺顺手"
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert "delivery" not in out
    assert out["draft_checks"] == [
        {"index": 0, "kind": "label", "what": "正文里有「【正文】」标记: body 只放正文, 标记删掉(交付时再按项目要的样子加)"},
        {"index": 0, "kind": "br", "what": "正文里有字面的「<br>」(多半是从交付表格里复制回来的): 换成真的换行"},
        {"index": 2, "kind": "comment_block",
         "what": "正文里带了「【素人评论】」那一段: body 只放正文, 评论写在这篇的 comments 里, 交付时再按项目要的样子排在正文后面; "
                 "正文里删掉这一段"}]
    assert core.DRAFT_CHECKS_NEXT_STEP.format(idx="0、2", n=2) in out["next_step"]
    assert _meta(c)["draft_checks"]["format"] == {"label": 1, "br": 1, "comment_block": 1}
    _assert_writer_safe(out, "score_drafts draft_checks (no template)")


def test_a_template_without_comments_does_not_say_the_desk_adds_them():
    """BLOCKS 不拼评论: 「写在 comments 里、写作台按模板加」照做的话, 评论从 delivery.text 里没了、problems 也不说。"""
    c = desk_client(default_params=_params_with(BLOCKS))
    drafts = _drafts(2)
    for d in drafts:
        d["tags"] = ["话题甲"]
    drafts[1]["body"] += "\n【素人评论】示例牌用着挺顺手"
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert out["draft_checks"] == [{"index": 1, "kind": "comment_block",
                                    "what": "正文里带了「【素人评论】」那一段: 这个项目的交付模板不拼评论, 评论不放进正文; "
                                            "正文里删掉这一段, 要给的评论放在交付稿后面单独给用户"}]
    _assert_writer_safe(out, "score_drafts draft_checks (template without comments)")


def test_topic_labels_follow_whether_the_template_handles_topics():
    """模板不管话题: 话题行照项目规则写在正文末尾、交付稿原样带上, 【话题】不报。管话题的: 话题在 tags 里, 标记删掉。"""
    body_tail = "\n【话题】#示例话题 #示例牌好物"
    c = desk_client(default_params=_params_with({"layout": "blocks"}))
    drafts = _drafts(2)
    drafts[1]["body"] += body_tail
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert "【话题】" in out["delivery"]["text"] and "draft_checks" not in out
    c = desk_client(default_params=_params_with(BLOCKS))
    drafts = _drafts(2)
    for d in drafts:
        d["tags"] = ["话题甲"]
    drafts[1]["body"] += body_tail
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert out["draft_checks"] == [{"index": 1, "kind": "label",
                                    "what": "正文里有「【话题】」标记: 话题写在这篇的 tags 里, 交付稿里的话题行由写作台按交付模板加; "
                                            "稿子里删掉这个标记"}]


def test_rows_are_in_index_order_whatever_kind_they_are():
    """版式残留和重复说法分两遍查出来, 回包里照 index 排: 前面那篇的重复说法排在后面那篇的版式残留前头。"""
    c = desk_client()
    drafts = _drafts(5, carriers={0, 1, 2})
    drafts[4]["body"] += "<br>"
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert [(r["index"], r["kind"]) for r in out["draft_checks"]] == [(2, "repeat"), (4, "br")]
    assert core.DRAFT_CHECKS_NEXT_STEP.format(idx="2、4", n=2) in out["next_step"]


def test_format_counts_drafts_not_rows():
    """一篇标题、正文里都有标记是两条, batch_metrics 里算一篇(和量过的「多少篇」对得上)。"""
    c = desk_client()
    drafts = _drafts(2)
    drafts[0]["title"] = "【标题】" + drafts[0]["title"]
    drafts[0]["body"] = "【正文】" + drafts[0]["body"]
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert [r["kind"] for r in out["draft_checks"]] == ["label", "label"]
    assert _meta(c)["draft_checks"]["format"] == {"label": 1}


def test_a_body_of_only_topic_tags_is_stored_and_reported_empty():
    c = desk_client()
    drafts = _drafts(2)
    drafts[1]["body"] = "#示例话题甲 #示例话题乙"
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert out["written"] == 2 and not out["rejected"]
    assert out["draft_checks"] == [{"index": 1, "kind": "empty",
                                    "what": "去掉话题标签、标记和评论之后这篇没有正文: 把正文补进 body"}]


def test_a_blank_body_is_still_rejected_by_the_gate_and_not_reported_twice():
    c = desk_client()
    drafts = _drafts(2)
    drafts[1]["body"] = "   "
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert [r["gate"] for r in out["rejected"]] == ["empty_body"]
    assert "draft_checks" not in out


# ══════════════════════════════════════════════════════════════════════
# 3 · 改过重存: 改一轮就够
# ══════════════════════════════════════════════════════════════════════

def test_revised_drafts_that_still_repeat_are_not_sent_round_again():
    c = desk_client()
    drafts = _drafts(5)
    first = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert first["written"] == 5 and "draft_checks" not in first
    _embed(c)
    revised = [{**drafts[k], "body": drafts[k]["body"] + P + "。", "replaces_version_id": first["version_ids"][k]}
               for k in range(3)]
    out = core.commit_drafts(c, PROJ, revised, user_id=ME)
    assert len(out["replaced"]) == 3
    assert out["draft_checks"] == [{"index": 2, "kind": "repeat", "what": _repeat_what("0、1", 2, 3)}]
    assert core.DRAFT_CHECKS_ONCE_NEXT_STEP.format(idx="2", n=1) in out["next_step"]
    assert "先改再交" not in out["next_step"]
    assert _meta(c)["draft_checks"]["repeat"]["once"] == 1


def test_debris_on_a_revised_draft_is_said_once_not_sent_round_again():
    """本来就要那样写的字面 <br> / \\n(讲代码的笔记)、每次都转义坏了的换行: 叫改就改不完。改过重存的同样改一轮就够。"""
    c = desk_client()
    drafts = _drafts(2)
    first = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    _embed(c)
    out = core.commit_drafts(c, PROJ, [{**drafts[0], "body": drafts[0]["body"] + "<br>",
                                        "replaces_version_id": first["version_ids"][0]}], user_id=ME)
    assert out["replaced"] and out["written"] == 1
    assert [r["kind"] for r in out["draft_checks"]] == ["br"]
    assert core.DRAFT_CHECKS_ONCE_NEXT_STEP.format(idx="0", n=1) in out["next_step"]
    assert "先改再交" not in out["next_step"]
    assert _meta(c)["draft_checks"]["once"] == 1


def test_a_revised_draft_with_debris_and_a_repeat_is_not_told_both_fix_and_stop():
    c = desk_client()
    drafts = _drafts(5)
    first = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    _embed(c)
    revised = [{**drafts[k], "body": drafts[k]["body"] + P + "。" + ("<br>" if k == 2 else ""),
                "replaces_version_id": first["version_ids"][k]} for k in range(3)]
    out = core.commit_drafts(c, PROJ, revised, user_id=ME)
    assert [(r["index"], r["kind"]) for r in out["draft_checks"]] == [(2, "br"), (2, "repeat")]
    assert core.DRAFT_CHECKS_ONCE_NEXT_STEP.format(idx="2", n=1) in out["next_step"]
    assert "先改再交" not in out["next_step"], "同一篇不会既叫改、又叫别改"


def test_in_a_mixed_call_the_new_draft_is_asked_to_change_not_the_revised_one():
    """两篇新稿 + 一篇改过重存的都带同一个说法: 改过的那篇先算进不算的那两篇, 要改的落到新稿上 —— 不然只剩一句「照实说」,
    三篇照原样交。"""
    c = desk_client()
    old = _drafts(1, seed=7)
    first = core.commit_drafts(c, PROJ, old, user_id=ME)
    _embed(c)
    new = _drafts(2, carriers={0, 1}, seed=3)
    rev = {**old[0], "body": old[0]["body"] + P + "。", "replaces_version_id": first["version_ids"][0]}
    out = core.commit_drafts(c, PROJ, new + [rev], user_id=ME)
    assert out["written"] == 3 and len(out["replaced"]) == 1
    assert out["draft_checks"] == [{"index": 1, "kind": "repeat", "what": _repeat_what("0、2", 2, 3)}]
    assert core.DRAFT_CHECKS_NEXT_STEP.format(idx="1", n=1) in out["next_step"]
    assert "这几篇不再接着改" not in out["next_step"]
    assert _meta(c)["draft_checks"]["repeat"]["once"] == 0


# ══════════════════════════════════════════════════════════════════════
# 4 · 开头一样: 只提示
# ══════════════════════════════════════════════════════════════════════

def test_five_drafts_with_the_same_opening_get_a_batch_hint_only():
    c = desk_client()
    out = core.commit_drafts(c, PROJ, distinct_drafts(5), user_id=ME)
    assert out["batch_hints"] == [
        "这批 5 篇里, 第 1、2、3、4、5 篇开头都是「这一段说」: 只是提示, 不算要改的地方; "
        "想让这批读起来不那么像一个模子, 挑几篇换个开头"]
    assert "draft_checks" not in out
    assert out["next_step"].endswith(core.BATCH_HINTS_NEXT_STEP)
    assert "draft_checks" not in out["next_step"]
    assert _meta(c)["draft_checks"]["opener"] == {"need": 5, "top": 5, "hinted": True}
    _assert_writer_safe(out, "score_drafts batch_hints")


def test_four_drafts_with_the_same_opening_get_nothing():
    out = core.commit_drafts(desk_client(), PROJ, distinct_drafts(4), user_id=ME)
    assert "batch_hints" not in out and "draft_checks" not in out


def test_a_clean_small_batch_has_the_same_keys_as_before(monkeypatch):
    now = core.commit_drafts(desk_client(), PROJ, distinct_drafts(2), user_id=ME)
    monkeypatch.setattr(core, "_draft_checks", lambda *a, **k: {})
    before = core.commit_drafts(desk_client(), PROJ, distinct_drafts(2), user_id=ME)
    assert set(now) == set(before)
    assert now["next_step"] == before["next_step"]


# ══════════════════════════════════════════════════════════════════════
# 5 · 不抛、计时
# ══════════════════════════════════════════════════════════════════════

def test_a_failing_check_leaves_no_half_written_keys(monkeypatch):
    def boom(*a, **k):
        raise ValueError("示例: 查坏了")
    monkeypatch.setattr(poststyle, "batch_repeats", boom)
    c = desk_client()
    drafts = distinct_drafts(5)                        # 开头一样, 不出错的话会带 batch_hints
    drafts[0]["body"] += "<br>"                        # 不出错的话会带 draft_checks
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert out["written"] == 5
    assert "draft_checks" not in out and "batch_hints" not in out
    assert "draft_checks" not in out["next_step"] and "batch_hints" not in out["next_step"]
    meta = _meta(c)["draft_checks"]
    assert set(meta) == {"error", "elapsed_ms"} and "ValueError" in meta["error"]


@pytest.mark.parametrize("where", ["shared_openers", "steps"])
def test_a_check_failing_after_the_keys_were_written_takes_them_back_out(monkeypatch, where):
    """出错时 draft_checks / batch_hints 已经挂上了(开头一样那一步、或者拼 next_step 那一步才出错): 照样摘掉, 不带半截的键。"""
    def boom(*a, **k):
        raise ValueError("示例: 查坏了")
    if where == "shared_openers":
        monkeypatch.setattr(poststyle, "shared_openers", boom)           # draft_checks 已经挂上
    else:
        monkeypatch.setattr(core, "_draft_checks_steps", boom)           # draft_checks、batch_hints 都已经挂上
    c = desk_client()
    drafts = distinct_drafts(5)
    drafts[0]["body"] += "<br>"
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert out["written"] == 5
    assert "draft_checks" not in out and "batch_hints" not in out
    assert "draft_checks" not in out["next_step"] and "batch_hints" not in out["next_step"]
    meta = _meta(c)["draft_checks"]
    assert set(meta) == {"error", "elapsed_ms"} and "ValueError" in meta["error"]


def test_the_time_is_counted_in_the_delivery_rules_phase(monkeypatch):
    real = poststyle.batch_repeats

    def slow(*a, **k):
        time.sleep(0.06)
        return real(*a, **k)
    monkeypatch.setattr(poststyle, "batch_repeats", slow)
    out = core.commit_drafts(desk_client(), PROJ, _drafts(2), user_id=ME)
    assert set(out["phase_ms"]) == PHASES
    assert out["phase_ms"]["delivery_rules"] >= 60
