"""deskcore/phrase_dryrun.py 单元测试（W-62 ① / D-166；全合成样本、零库零模型）。

aw#179 review 之后补的几条统计口径各有一条测试钉住:
挑词 / 核验两批不重叠(F1)、每层对照各自够数(F2)、按篇重抽样的区间(F3)、只动标点不算改(漏掉的第 2 条)、
候选说法去重叠碎片和品牌 / 产品叫法(漏掉的第 1 条)、模块不接线(F5)。
"""

from __future__ import annotations

import ast
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deskcore import phrase_dryrun as pd

REPO = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
# 合成句子用的字(随机拼, 不是任何真实文本)
_POOL = (
    "天地人和春夏秋冬山水花草风云日月星辰江河湖海东南西北上下左右大小多少长短高低远近新旧快慢冷热明暗轻重"
    "黑白红黄蓝绿金银铜铁木石土火光影声色香味甜苦酸辣早晚前后里外开关进出来去走跑看听说写读画唱跳"
)


def _manual(*phrases: str) -> pd.CandidateSet:
    return pd.CandidateSet.manual(phrases)


def _random_pairs(seed: int, *, p_base: float, p_phrase: float, n_phrases: int, phrase_share: float,
                  n_per_cat: int = 60, cats=("CAT_A", "CAT_B", "CAT_C")):
    """随机字拼的稿; 带说法的句子以 p_phrase、别的句子以 p_base 的概率被整句重写。返回 (pairs, 真说法)。"""
    rng = random.Random(seed)
    phrases = ["".join(rng.choice(_POOL) for _ in range(4)) for _ in range(n_phrases)]
    pairs = []
    for cat in cats:
        for k in range(n_per_cat):
            draft, pub = [], []
            for _ in range(6):
                sent = "".join(rng.choice(_POOL) for _ in range(rng.randint(12, 20)))
                has = rng.random() < phrase_share
                if has:
                    pos = rng.randint(0, len(sent))
                    sent = sent[:pos] + rng.choice(phrases) + sent[pos:]
                draft.append(sent)
                rewrite = rng.random() < (p_phrase if has else p_base)
                pub.append("".join(rng.choice(_POOL) for _ in range(len(sent))) if rewrite else sent)
            pairs.append({
                "category": cat,
                "draft_body": "。".join(draft) + "。",
                "published_body": "。".join(pub) + "。",
                "publish_time": T0 + timedelta(days=k),
            })
    return pairs, phrases


# ── 分层对照 ────────────────────────────────────────────────────────────────────────────────

def test_stratum_baseline_neutralizes_position_confounder():
    """开头句本身 100% 被改、中间句 0% 被改：候选词只在开头句出现时，分层净抬升应为 0 且不过线。"""
    pairs = []
    for i in range(6):
        pairs.append({
            "category": "CAT_A",
            "draft_body": f"其实说白了这事真挺简单第{i}版。中段讲一个具体的生活细节过程。尾段交代普通收尾。",
            "published_body": f"换一种完全不同的开篇讲法版本{i}。中段讲一个具体的生活细节过程。尾段交代普通收尾。",
        })
    for i in range(6):
        pairs.append({
            "category": "CAT_A",
            "draft_body": f"普通的开场第一句话示例第{i}版。中段讲另一个具体日常画面过程。尾段交代普通收尾。",
            "published_body": f"重新写了一段全新的开场句子{i}。中段讲另一个具体日常画面过程。尾段交代普通收尾。",
        })

    res = pd.evaluate_phrase_edits(pairs, _manual("其实说白了"), min_categories_for_go=1)
    cat = res["categories"]["CAT_A"]
    assert cat["phrase_hits"] == 6
    assert cat["sentence_changed_rate"] == 1.0
    # 不分层的基线只看到大多没改的中段 / 结尾; 同层基线拿开头比开头 = 1.0
    assert cat["raw_baseline_changed_rate"] < 0.35
    assert cat["stratum_baseline_changed_rate"] == 1.0
    assert cat["sentence_changed_lift"] == 0.0
    assert cat["lift_ci_low"] == 0.0
    assert cat["passed_category_gate"] is False
    assert res["gate_passed"] is False


def test_word_level_disappearance_is_diagnostic_only():
    """改了句子别处但保留了说法: 句子算被改; 「说法不见了」那组诊断数必须是 0。"""
    pairs = []
    for i in range(6):
        pairs.append({
            "category": "CAT_B",
            "draft_body": (
                f"开场交代一下今天的合成记录第{i}篇。"
                f"老实讲这个普通记录过程里第{i}次我先测了半小时再放回桌面。"
                f"结尾写一句合成的日常收束。"
            ),
            "published_body": (
                f"开场交代一下今天的合成记录第{i}篇。"
                f"老实讲这个普通记录过程里第{i}次我先测了二十分钟再收好。"
                f"结尾写一句合成的日常收束。"
            ),
        })
    res = pd.evaluate_phrase_edits(pairs, _manual("老实讲"), min_categories_for_go=1)
    cat = res["categories"]["CAT_B"]
    assert cat["phrase_hits"] == 6
    assert cat["phrase_sentence_changed"] == 6
    assert cat["phrase_word_kept_on_edit"] == 6
    assert cat["phrase_word_removed"] == 0
    assert cat["sentence_changed_rate"] == 1.0
    assert cat["phrase_removed_rate"] == 0.0
    assert res["gate_metric"] == "sentence_changed_lift"
    assert cat["passed_category_gate"] is False      # 中段 medium 没有对照: 对不上


def _middle_phrase_pairs(cat_name: str, drop_phrase: bool, n: int = 6):
    rows = []
    for i in range(n):
        mid_pub = f"这一句完全改成了平实描述版本{i}号。" if drop_phrase else f"毫无悬念这次记录里第{i}次过程非常平稳。"
        rows.append({
            "category": cat_name,
            "draft_body": (
                f"开场交代普通合成场景记录{i}号。"
                f"毫无悬念这次记录里第{i}次过程非常平稳。"
                f"另外补充一句不含候选词的同等长度中段句子{i}。"
                "结尾写一句普通收束记录。"
            ),
            "published_body": (
                f"开场交代普通合成场景记录{i}号。"
                f"{mid_pub}"
                f"另外补充一句不含候选词的同等长度中段句子{i}。"
                "结尾写一句普通收束记录。"
            ),
        })
    return rows


def test_multi_category_gate_requires_at_least_two_categories():
    """同一分层下带说法的句子真被改掉: 只有 1 个品类过线时整体不放行, ≥2 个品类才 gate_passed=True。"""
    one_cat = _middle_phrase_pairs("CAT_1", True) + _middle_phrase_pairs("CAT_2", False)
    r1 = pd.evaluate_phrase_edits(one_cat, _manual("毫无悬念"))
    assert r1["passing_categories"] == 1
    assert r1["gate_passed"] is False

    two_cats = _middle_phrase_pairs("CAT_1", True) + _middle_phrase_pairs("CAT_2", True)
    r2 = pd.evaluate_phrase_edits(two_cats, _manual("毫无悬念"))
    assert r2["passing_categories"] == 2
    assert r2["gate_passed"] is True
    for cat in ("CAT_1", "CAT_2"):
        c = r2["categories"][cat]
        assert c["sentence_changed_lift"] == 1.0
        assert c["lift_ci_low"] == 1.0
        assert c["matched_hit_drafts"] == 6


def test_too_few_hit_drafts_fail_even_with_a_clean_interval():
    """命中只来自 4 篇稿(< MIN_HIT_DRAFTS)时, 区间再干净也不放行。"""
    pairs = _middle_phrase_pairs("CAT_1", True, n=4) + _middle_phrase_pairs("CAT_2", True, n=4)
    # 对照另外补够篇数, 让层能对上
    for cat in ("CAT_1", "CAT_2"):
        for i in range(3):
            body = f"开场交代普通合成场景补充{i}号。另外补充一句不含候选词的同等长度中段补句{i}。结尾写一句普通收束记录。"
            pairs.append({"category": cat, "draft_body": body, "published_body": body})
    res = pd.evaluate_phrase_edits(pairs, _manual("毫无悬念"))
    for cat in ("CAT_1", "CAT_2"):
        c = res["categories"][cat]
        assert c["matched_hit_drafts"] == 4 < pd.MIN_HIT_DRAFTS
        assert c["lift_ci_low"] == 1.0
        assert c["passed_category_gate"] is False
    assert res["gate_passed"] is False


def test_hit_draft_minimum_counts_drafts_not_sentences():
    """4 篇稿各有 2 句带说法的中段句、全被改(命中句共 8 句 ≥ MIN_HIT_DRAFTS), 区间干净: 按篇只有 4 篇, 照样不放行。"""
    pairs = []
    for cat in ("CAT_1", "CAT_2"):
        for i in range(4):
            pairs.append({
                "category": cat,
                "draft_body": (
                    f"开场交代普通合成场景记录{i}号。"
                    f"毫无悬念这次记录里第{i}次过程非常平稳。"
                    f"毫无悬念另一段记录里第{i}回节奏也很稳。"
                    f"另外补充一句不含候选词的同等长度中段句子{i}。"
                    "结尾写一句普通收束记录。"
                ),
                "published_body": (
                    f"开场交代普通合成场景记录{i}号。"
                    f"这一句完全改成了平实描述版本{i}号。"
                    f"后面那句也整个换了别的讲法{i}号。"
                    f"另外补充一句不含候选词的同等长度中段句子{i}。"
                    "结尾写一句普通收束记录。"
                ),
            })
        for i in range(3):
            body = f"开场交代普通合成场景补充{i}号。另外补充一句不含候选词的同等长度中段补句{i}。结尾写一句普通收束记录。"
            pairs.append({"category": cat, "draft_body": body, "published_body": body})
    res = pd.evaluate_phrase_edits(pairs, _manual("毫无悬念"))
    for cat in ("CAT_1", "CAT_2"):
        c = res["categories"][cat]
        assert c["matched_phrase_hits"] == 8 >= pd.MIN_HIT_DRAFTS
        assert c["matched_hit_drafts"] == 4 < pd.MIN_HIT_DRAFTS
        assert c["matched_coverage"] == 1.0
        assert c["lift_ci_low"] == 1.0
        assert c["passed_category_gate"] is False
    assert res["gate_passed"] is False


def test_gate_uses_sentence_changed_lift_not_word_removal_lift():
    """带说法的句子都是「别处改了、说法留着」, 同层对照一句没改: 放行口径的 sentence_changed_lift = 1,
    诊断用的 word_removal_lift = 0, 闸照样过(不能拿「说法不见了」来放行)。"""
    pairs = []
    for cat in ("CAT_1", "CAT_2"):
        for i in range(6):
            pairs.append({
                "category": cat,
                "draft_body": (
                    f"开场交代一下今天的合成记录第{i}篇。"
                    f"老实讲这个普通记录过程里第{i}次我先测了半小时再放回桌面。"
                    f"另外补充一句不含候选词的同等长度中段句子{i}。"
                    "结尾写一句合成的日常收束。"
                ),
                "published_body": (
                    f"开场交代一下今天的合成记录第{i}篇。"
                    f"老实讲这个普通记录过程里第{i}次我先测了二十分钟再收好。"
                    f"另外补充一句不含候选词的同等长度中段句子{i}。"
                    "结尾写一句合成的日常收束。"
                ),
            })
    res = pd.evaluate_phrase_edits(pairs, _manual("老实讲"))
    for cat in ("CAT_1", "CAT_2"):
        c = res["categories"][cat]
        assert c["matched_phrase_hits"] == 6
        assert c["phrase_word_kept_on_edit"] == 6
        assert c["phrase_word_removed"] == 0
        assert c["stratum_baseline_changed_rate"] == 0.0
        assert c["sentence_changed_lift"] == 1.0
        assert c["word_removal_lift"] == 0.0
        assert c["lift_ci_low"] == 1.0
        assert c["passed_category_gate"] is True
    assert res["gate_passed"] is True


def test_unmatched_strata_cannot_pass_gate_via_raw_baseline():
    """缺失同层对照（所有开头句都含候选词）不得退回总体基线放行。"""
    pairs = []
    for cat in ("CAT_1", "CAT_2"):
        for i in range(6):
            pairs.append({
                "category": cat,
                "draft_body": f"其实说白了这段开场句第{i}版。中段写一句常规日常观察记录。结尾写一句常规日常收束。",
                "published_body": f"完全重写的开场表达版本{i}。中段写一句常规日常观察记录。结尾写一句常规日常收束。",
            })
    res = pd.evaluate_phrase_edits(pairs, _manual("其实说白了"))
    assert res["gate_passed"] is False
    assert res["passing_categories"] == 0
    for cat in ("CAT_1", "CAT_2"):
        c = res["categories"][cat]
        assert c["phrase_hits"] == 6
        assert c["matched_phrase_hits"] == 0
        assert c["unmatched_stratum_hits"] == 6
        assert c["matched_coverage"] == 0.0
        assert c["stratum_baseline_changed_rate"] is None
        assert c["sentence_changed_lift"] is None
        assert c["passed_category_gate"] is False


def test_control_minimum_holds_per_stratum_not_summed():
    """review F2: 开头层 10 句命中全被改、只有 1 句对照; 中段层 1 句命中、对照很多。
    以前把各层对照加起来和门槛比, 开头那层靠 1 句对照定基线也能过; 现在开头层对不上, 覆盖率不够。"""
    pairs = []
    for cat in ("CAT_1", "CAT_2"):
        for i in range(10):
            pairs.append({
                "category": cat,
                "draft_body": f"其实说白了这段开场句第{i}版。中段写一句常规日常观察记录{i}号。结尾写一句常规日常收束。",
                "published_body": f"完全重写的开场表达版本{i}。中段写一句常规日常观察记录{i}号。结尾写一句常规日常收束。",
            })
        pairs.append({
            "category": cat,
            "draft_body": "不含候选词的普通开场句子。其实说白了中段这一句原样保留下来了。结尾写一句常规日常收束。",
            "published_body": "不含候选词的普通开场句子。其实说白了中段这一句原样保留下来了。结尾写一句常规日常收束。",
        })
    res = pd.evaluate_phrase_edits(pairs, _manual("其实说白了"))
    for cat in ("CAT_1", "CAT_2"):
        c = res["categories"][cat]
        assert c["phrase_hits"] == 11
        assert c["matched_phrase_hits"] == 1          # 只有中段那层对上
        assert c["unmatched_stratum_hits"] == 10      # 开头层只有 1 句对照, 不算对上
        assert c["matched_coverage"] < pd.MIN_MATCHED_COVERAGE
        assert c["passed_category_gate"] is False
    assert res["gate_passed"] is False


def test_stratum_controls_must_come_from_enough_drafts():
    """一层的对照句数够了, 但全来自同一篇稿(< MIN_STRATUM_CONTROL_DRAFTS 篇): 这层不算对上。"""
    pairs = []
    for i in range(6):
        pairs.append({
            "category": "CAT_1",
            "draft_body": f"开场交代普通合成场景记录{i}号。毫无悬念这次记录里第{i}次过程非常平稳。结尾写一句普通收束记录。",
            "published_body": f"开场交代普通合成场景记录{i}号。这一句完全改成了平实描述版本{i}号。结尾写一句普通收束记录。",
        })
    body = "开场交代普通合成场景补充号。" + "".join(
        f"另外补充一句不含候选词的同等长度中段句子{k}。" for k in range(6)) + "结尾写一句普通收束记录。"
    pairs.append({"category": "CAT_1", "draft_body": body, "published_body": body})
    res = pd.evaluate_phrase_edits(pairs, _manual("毫无悬念"), min_categories_for_go=1)
    c = res["categories"]["CAT_1"]
    assert c["phrase_hits"] == 6
    assert c["matched_phrase_hits"] == 0
    assert c["passed_category_gate"] is False


# ── 按篇重抽样的区间(review F3) ─────────────────────────────────────────────────────────────

def _clustered_pairs(cat: str, spread: bool):
    """10 句带说法的中段句全被改。spread=False: 10 句挤在同一篇稿里, 另有 4 篇各 1 句没改;
    spread=True: 10 篇稿各 1 句被改, 另有 4 篇各 1 句没改。对照都是没改的中段句。"""
    rows = []

    def _doc(i: int, n_hit: int, changed: bool):
        hits_d = [f"老实讲第{i}篇的第{k}个中段日常记录句子" for k in range(n_hit)]
        hits_p = [f"整句换成了别的写法第{i}篇第{k}句" if changed else s for k, s in enumerate(hits_d)]
        ctrl = [f"不含说法的中段日常记录句子第{i}篇{k}号" for k in range(2)]
        head, tail = f"开场写一句普通的合成开头第{i}篇。", "结尾写一句普通收束。"
        return {
            "category": cat,
            "draft_body": head + "。".join(hits_d + ctrl) + "。" + tail,
            "published_body": head + "。".join(hits_p + ctrl) + "。" + tail,
        }

    if spread:
        rows += [_doc(i, 1, True) for i in range(10)]
    else:
        rows.append(_doc(0, 10, True))
    rows += [_doc(100 + i, 1, False) for i in range(4)]
    return rows


def test_sentences_from_one_draft_are_not_independent_evidence():
    """一篇稿里 10 句全被改、别的 4 篇没改: 按句算净抬升很高, 但按篇重抽样的区间下界到 0, 不放行;
    同样 10 句分散在 10 篇稿里时才放行。"""
    clustered = _clustered_pairs("CAT_1", False) + _clustered_pairs("CAT_2", False)
    r = pd.evaluate_phrase_edits(clustered, _manual("老实讲"))
    for cat in ("CAT_1", "CAT_2"):
        c = r["categories"][cat]
        assert c["matched_phrase_hits"] == 14
        assert c["matched_hit_drafts"] == 5
        assert c["sentence_changed_lift"] > 0.5
        assert c["lift_ci_low"] <= 0.0
        assert c["passed_category_gate"] is False
    assert r["gate_passed"] is False

    spread = _clustered_pairs("CAT_1", True) + _clustered_pairs("CAT_2", True)
    r2 = pd.evaluate_phrase_edits(spread, _manual("老实讲"))
    for cat in ("CAT_1", "CAT_2"):
        c = r2["categories"][cat]
        assert c["matched_hit_drafts"] == 14
        assert c["lift_ci_low"] > 0.0
        assert c["passed_category_gate"] is True
    assert r2["gate_passed"] is True


def test_bootstrap_is_deterministic_for_same_input():
    pairs = _clustered_pairs("CAT_1", True) + _clustered_pairs("CAT_2", True)
    a = pd.evaluate_phrase_edits(pairs, _manual("老实讲"))
    b = pd.evaluate_phrase_edits(pairs, _manual("老实讲"))
    assert a == b


# ── 只动标点不算改(review 漏掉的第 2 条) ───────────────────────────────────────────────────

def test_punctuation_only_merges_do_not_inflate_the_baseline():
    """10 篇里 4 篇真删了带说法的那句里的说法; 对照句一个字没改, 只在 6 篇里用逗号和下一句合成一句。
    以前基线把合句算成删改(0.6), 净抬升成了负数; 现在两边都不算只动标点的改动。"""
    pairs = []
    for cat in ("CAT_1", "CAT_2"):
        for i in range(10):
            phrase_pub = f"这一句今天的中段记录是第{i}篇。" if i < 4 else f"说句实话这一句今天的中段记录是第{i}篇。"
            control = f"普通的中段对照句子第{i}篇已经写完"
            tail = "结尾写一句普通收束。"
            pairs.append({
                "category": cat,
                "draft_body": f"开场写一句普通的合成开头第{i}篇。说句实话这一句今天的中段记录是第{i}篇。{control}。{tail}",
                "published_body": (
                    f"开场写一句普通的合成开头第{i}篇。{phrase_pub}"
                    + (f"{control}，{tail}" if i >= 4 else f"{control}。{tail}")
                ),
            })
    res = pd.evaluate_phrase_edits(pairs, _manual("说句实话"))
    for cat in ("CAT_1", "CAT_2"):
        c = res["categories"][cat]
        assert c["non_phrase_punct_only"] >= 6
        assert c["non_phrase_changed"] == 0
        assert c["stratum_baseline_changed_rate"] == 0.0
        assert c["sentence_changed_rate"] == 0.4
        assert c["sentence_changed_lift"] == 0.4
        assert c["passed_category_gate"] is True


_A = "今天下班我先去菜市场买水果"
_B = "回家做一顿普通晚饭"
# (说明, 稿, 帖, 每一句的判法)。标「修」的两组是 Codex 第四轮那次修的(修之前判错), 其余几行把各种交界情况钉住。
_BOUNDARY_CASES = [
    # 修: replace 换进来的那一侧也要归属。稿那边只换了前一句的字, 帖那边多出来的字贴在后一句开头 / 前一句末尾。
    ("替换前句末尾+后句开头加字", f"{_A}。{_B}。", "今天下班我先去菜市场买香蕉。接着回家做一顿普通晚饭。",
     ["edited", "edited"]),
    ("替换后句开头+前句末尾加字", f"{_A}。{_B}。", f"{_A}啦。到家做一顿普通晚饭。", ["edited", "edited"]),
    ("短片段换了字贴到前句", f"{_A}。哈哈。{_B}。", f"{_A}嘿嘿。{_B}。", ["edited", "unchanged"]),
    ("短片段换了字贴到后句", f"{_A}。哈哈。{_B}。", f"{_A}。嘿嘿{_B}。", ["unchanged", "edited"]),
    ("合句处替换, 没标点, 两边都算", f"{_A}。{_B}。", "今天下班我先去菜市场买香蕉接着回家做一顿普通晚饭。",
     ["edited", "edited"]),
    ("合句处替换后句开头, 没标点, 两边都算", f"{_A}。{_B}。", f"{_A}到家做一顿普通晚饭。", ["edited", "edited"]),
    # 修: 纯插 / 纯删两头的字一样时对齐可以左右挪, 要挑合标点和句界的摆法。
    ("单独加的一句末字和前句末字一样", "今天下班去买了。回家做一顿普通的家常晚饭吧。",
     "今天下班去买了，真是累坏了。回家做一顿普通的家常晚饭吧。", ["punct_only", "unchanged"]),
    ("后句开头加的字末字和前句末字一样", "今天下班去买了。回家做一顿普通的家常晚饭。",
     "今天下班去买了。累了回家做一顿普通的家常晚饭。", ["unchanged", "edited"]),
    ("删掉和前句结尾一样的短片段再合句", "今天真开心哈哈。哈哈。明天继续努力工作吧。", "今天真开心哈哈明天继续努力工作吧。",
     ["punct_only", "punct_only"]),
    ("合句处同一个说法只留一份, 看逗号算前句删的", "今天下班我先去菜市场买水果说句实话。说句实话回家做一顿普通晚饭。",
     "今天下班我先去菜市场买水果，说句实话回家做一顿普通晚饭。", ["edited", "punct_only"]),
    # 其余交界情况(之前就对)
    ("前句末尾加字", f"{_A}。{_B}。", f"{_A}呢。{_B}。", ["edited", "unchanged"]),
    ("后句开头加字", f"{_A}。{_B}。", f"{_A}。接着{_B}。", ["unchanged", "edited"]),
    ("两句之间单独加一句", f"{_A}。{_B}。", f"{_A}。真开心。{_B}。", ["unchanged", "unchanged"]),
    ("一句中间插进单独一句", f"{_A}。{_B}。", f"今天下班我先去。真开心。菜市场买水果。{_B}。", ["edited", "unchanged"]),
    ("合句处加字, 没标点, 两边都算", f"{_A}。{_B}。", f"{_A}真开心{_B}。", ["edited", "edited"]),
    ("只合句", f"{_A}。{_B}。", f"{_A}{_B}。", ["punct_only", "punct_only"]),
    ("只拆句", f"{_A}。{_B}。", "今天下班我先去。菜市场买水果。回家做一顿。普通晚饭。", ["punct_only", "punct_only"]),
    ("删掉整个短片段再合句", f"{_A}。哈哈。{_B}。", f"{_A}，{_B}。", ["punct_only", "punct_only"]),
    ("删掉整个短片段, 后句改了", f"{_A}。哈哈。{_B}。", f"{_A}。回家做一顿普通的晚饭。", ["unchanged", "edited"]),
    ("一处替换跨两句", f"{_A}。{_B}。", "今天下班我先去菜市场买香蕉。到家做一顿普通晚饭。", ["edited", "edited"]),
]


@pytest.mark.parametrize("label,draft,pub,want", _BOUNDARY_CASES, ids=[c[0] for c in _BOUNDARY_CASES])
def test_punct_only_alignment_at_fragment_boundaries(label, draft, pub, want):
    """交界处的每种对齐(删 / 插 / 替换, 跨句、贴着句头句尾、整段短片段): 真改了字的不能判成 punct_only,
    只动了标点的不能判成改过; 合句处没标点分不清的两边都算改过。"""
    assert [x["outcome"] for x in pd.classify_draft_sentences(draft, pub)] == want


def test_tiny_fragment_split_is_punct_only_and_fast():
    """发布版每 3 个字切一刀、字没改(review F6 的慢例): 整篇都判成 punct_only, 不走兜底窗口枚举。"""
    body = "。".join(f"这是第{i}句合成的日常记录内容用来测试切分速度的样本文字" for i in range(80)) + "。"
    norm = body.replace("。", "")
    pub = "～".join(norm[i:i + 3] for i in range(0, len(norm), 3))
    out = pd.classify_draft_sentences(body, pub)
    assert len(out) == 80
    assert {x["outcome"] for x in out} == {"punct_only"}


def test_short_draft_fragments_join_punct_only_alignment():
    """tvlink.sentences 丢掉 <6 字的短片段(「真好啊」): 合句时这几个字不能被当成发布帖多出来的字。
    两句字一个没变 → 都是 punct_only; 短片段本身不进句子统计。旁边真改了字的句子照样算改过。"""
    draft = "今天下班我先去菜市场买水果。真好啊。然后回家做一顿普通晚饭。"
    merged = "今天下班我先去菜市场买水果真好啊然后回家做一顿普通晚饭。"
    assert [x["outcome"] for x in pd.classify_draft_sentences(draft, merged)] == ["punct_only", "punct_only"]

    edited = "今天下班我先去菜市场买水果真好啊然后回家做一顿丰盛晚饭。"
    assert [x["outcome"] for x in pd.classify_draft_sentences(draft, edited)] == ["punct_only", "edited"]

    res = pd.evaluate_phrase_edits(
        [{"category": "CAT_A", "draft_body": draft, "published_body": merged}], _manual("说句实话")
    )
    c = res["categories"]["CAT_A"]
    assert c["total_sentences"] == 2
    assert c["non_phrase_punct_only"] == 2
    assert c["non_phrase_changed"] == 0


# ── 句子分类: 拆句 / 合句 / 共用说法 ────────────────────────────────────────────────────────

def test_split_published_sentences_keep_phrase_without_false_removal():
    """标点拆句保留候选词不记词级删除; 真删了才记。只拆不改字的算 punct_only。"""
    kept_split = [
        {   # 拆成两句, 还加了两个字: 算改过, 但说法还在
            "category": "CAT_SPLIT",
            "draft_body": "老实讲今天下班我先去了菜市场然后买了苹果香蕉西瓜回家做饭。尾句普通收束。",
            "published_body": "老实讲今天下班。记得我先去了菜市场然后买了苹果香蕉西瓜回家做饭。尾句普通收束。",
        },
        {   # 只用感叹号拆开, 字一个没变: punct_only
            "category": "CAT_SPLIT",
            "draft_body": "老实讲今天下班我先去了菜市场然后买了苹果香蕉西瓜回家做饭。尾句普通收束。",
            "published_body": "老实讲！今天下班我先去了菜市场然后买了苹果香蕉西瓜回家做饭。尾句普通收束。",
        },
    ]
    c = pd.evaluate_phrase_edits(kept_split, _manual("老实讲"))["categories"]["CAT_SPLIT"]
    assert c["phrase_hits"] == 2
    assert c["phrase_sentence_changed"] == 1
    assert c["phrase_punct_only"] == 1
    assert c["phrase_word_kept_on_edit"] == 1
    assert c["phrase_word_removed"] == 0

    dropped_split = [{
        "category": "CAT_DROP",
        "draft_body": "老实讲今天下班我先去了菜市场然后买了苹果香蕉西瓜回家做饭。尾句普通收束。",
        "published_body": "今天下班我先去了菜市场。然后买了苹果香蕉西瓜回家做饭。尾句普通收束。",
    }]
    c = pd.evaluate_phrase_edits(dropped_split, _manual("老实讲"))["categories"]["CAT_DROP"]
    assert c["phrase_sentence_changed"] == 1
    assert c["phrase_word_removed"] == 1

    # 说法被标点从中间拆开 + 别处加了字: 相邻片段合起来还认得出说法
    boundary_split = [{
        "category": "CAT_BOUNDARY",
        "draft_body": "老实讲今天下班我先去了菜市场然后买了苹果香蕉西瓜回家做饭。尾句普通收束。",
        "published_body": "老实讲！今天下班我先去了菜市场然后买了苹果香蕉和西瓜回家做饭。尾句普通收束。",
    }]
    c = pd.evaluate_phrase_edits(boundary_split, _manual("老实讲今天"))["categories"]["CAT_BOUNDARY"]
    assert c["phrase_hits"] == 1
    assert c["phrase_word_kept_on_edit"] == 1
    assert c["phrase_word_removed"] == 0

    # 整句切成 ≤4 字的碎片、还加了字: 兜底窗口要把整段都认回来
    tiny_split = [{
        "category": "CAT_TINY",
        "draft_body": "今天下班记得早点回家做饭。",
        "published_body": "今天下班。记得早点。回家做饭吧。",
    }]
    c = pd.evaluate_phrase_edits(tiny_split, _manual("回家做饭"))["categories"]["CAT_TINY"]
    assert c["phrase_hits"] == 1
    assert c["phrase_word_kept_on_edit"] == 1
    assert c["phrase_word_removed"] == 0

    # 相邻两句共用开头说法、都被切碎: 第一句删了说法不能被第二句掩盖
    tiny_shared = [{
        "category": "CAT_TINY_SHARED",
        "draft_body": "说句实话今天下班记得早点回家做饭。说句实话昨天周末去公园放风筝。",
        "published_body": "今天下班。记得早点。回家做饭。说句实话。昨天周末。去公园。放风筝。",
    }]
    c = pd.evaluate_phrase_edits(tiny_shared, _manual("说句实话"))["categories"]["CAT_TINY_SHARED"]
    assert c["phrase_hits"] == 2
    assert c["phrase_word_removed"] == 1
    assert c["phrase_punct_only"] == 1


def test_unrelated_sentence_sharing_four_char_phrase_cannot_mask_removal():
    """两句共用同一 ≥4 字说法时，前句删词不得借后句掩盖删除。"""
    pairs = [
        {   # 后句加了个「呢」: 改过, 说法还在
            "category": "CAT_SHARED",
            "draft_body": (
                "说句实话今天下班我先去了菜市场买了苹果然后回家做饭。"
                "说句实话昨天周末我在公园跑步看见小朋友一起开心放风筝。"
            ),
            "published_body": (
                "今天下班我先去了菜市场买了苹果然后回家做饭。"
                "说句实话昨天周末我在公园跑步看见小朋友一起开心放风筝呢。"
            ),
        },
        {   # 后句只把自己开头的说法用「！」拆出来: punct_only, 不能借给前句
            "category": "CAT_SHARED",
            "draft_body": (
                "说句实话今天下班我先去了菜市场买了苹果然后回家做饭。"
                "说句实话昨天周末我在公园跑步看见小朋友一起开心放风筝。"
            ),
            "published_body": (
                "今天下班我先去了菜市场买了苹果然后回家做饭。"
                "说句实话！昨天周末我在公园跑步看见小朋友一起开心放风筝。"
            ),
        },
    ]
    c = pd.evaluate_phrase_edits(pairs, _manual("说句实话"))["categories"]["CAT_SHARED"]
    assert c["phrase_hits"] == 4
    assert c["phrase_word_removed"] == 2
    assert c["phrase_word_kept_on_edit"] == 1
    assert c["phrase_punct_only"] == 1


def test_merged_fragment_checks_each_owner_slice_separately():
    """两句合成一句时，只从第一句删掉说法不得被第二句保留的说法掩盖。"""
    pairs = [
        {   # 前句删说法, 后句原样, 逗号合句
            "category": "CAT_MERGE",
            "draft_body": (
                "说句实话今天下班我先去了菜市场买了苹果然后回家做饭。"
                "说句实话昨天周末我在公园跑步看见小朋友一起开心放风筝。"
            ),
            "published_body": (
                "今天下班我先去了菜市场买了苹果然后回家做饭，"
                "说句实话昨天周末我在公园跑步看见小朋友一起开心放风筝。"
            ),
        },
        {   # 调换顺序再合句
            "category": "CAT_MERGE",
            "draft_body": (
                "说句实话今天下班我先去了菜市场买了苹果然后回家做饭。"
                "说句实话昨天周末我在公园跑步看见小朋友一起开心放风筝。"
            ),
            "published_body": (
                "说句实话昨天周末我在公园跑步看见小朋友一起开心放风筝，"
                "今天下班我先去了菜市场买了苹果然后回家做饭。"
            ),
        },
        {   # 前句结尾和后句开头的说法在合句处只留了一份: 1 句只动标点 + 1 句改过。整篇对齐把删掉的那份记在前句,
            # 前句算改过(放行口径对); 剩下那份说法落在前句的切片里, 诊断数把它记成「改了但说法还在」。
            "category": "CAT_MERGE",
            "draft_body": (
                "今天下班我先去了菜市场买了苹果回家做饭说句实话。"
                "说句实话昨天周末我在公园跑步看见小朋友一起放风筝。"
            ),
            "published_body": (
                "今天下班我先去了菜市场买了苹果回家做饭，"
                "说句实话昨天周末我在公园跑步看见小朋友一起放风筝。"
            ),
        },
    ]
    c = pd.evaluate_phrase_edits(pairs, _manual("说句实话"))["categories"]["CAT_MERGE"]
    assert c["phrase_hits"] == 6
    assert c["phrase_sentence_changed"] == 3
    assert c["phrase_punct_only"] == 3
    assert c["phrase_word_removed"] == 2          # 前两对: 删了说法的那句没被另一句保留的说法掩盖
    assert c["phrase_word_kept_on_edit"] == 1


def test_negative_lag_days_pairs_are_skipped():
    pairs = [
        {"category": "CAT_X", "lag_days": -2, "draft_body": "说白了第一句话写完。第二句话写完了。",
         "published_body": "改写后的第一句话。第二句话写完了。"},
        {"category": "CAT_X", "lag_days": 1, "draft_body": "正常的第一句话写完。第二句话写完了。",
         "published_body": "正常的第一句话写完。第二句话写完了。"},
    ]
    res = pd.evaluate_phrase_edits(pairs, _manual("说白了"))
    assert res["skipped_negative_lag"] == 1
    assert res["total_pairs"] == 1
    assert res["total_phrase_hits"] == 0


# ── 挑词和核验分两批(review F1) ────────────────────────────────────────────────────────────

def test_split_pairs_by_time_partitions_and_counts_missing_times():
    pairs = [{"draft_body": f"d{i}", "published_body": f"p{i}", "publish_time": T0 + timedelta(days=i)}
             for i in range(10)]
    pairs.append({"draft_body": "x", "published_body": "y"})
    sp = pd.split_pairs_by_time(pairs)
    assert len(sp.select) == 5 and len(sp.verify) == 5 and sp.without_time == 1
    assert max(p["publish_time"] for p in sp.select) < min(p["publish_time"] for p in sp.verify)

    sp2 = pd.split_pairs_by_time(pairs, cutoff=(T0 + timedelta(days=3)).isoformat())
    assert len(sp2.select) == 3 and len(sp2.verify) == 7
    with pytest.raises(ValueError):
        pd.split_pairs_by_time(pairs, cutoff="not-a-time")


def test_gate_refuses_to_verify_on_selection_rows():
    pairs, _ = _random_pairs(1, p_base=0.4, p_phrase=0.4, n_phrases=60, phrase_share=0.5, n_per_cat=20)
    cands = pd.select_candidate_phrases(pairs, top_k=10)
    assert cands.source == "selected" and cands.selection_pairs == len(pairs)
    with pytest.raises(pd.SplitViolation):
        pd.evaluate_phrase_edits(pairs, cands)

    sp = pd.split_pairs_by_time(pairs)
    cands = pd.select_candidate_phrases(sp.select, top_k=10)
    # 核验那批混进一对挑词用过的
    with pytest.raises(pd.SplitViolation):
        pd.evaluate_phrase_edits(list(sp.verify) + [sp.select[0]], cands)
    # 内容没用过、但发布时间不晚于挑词那批
    early = dict(sp.verify[0], publish_time=cands.selected_through)
    with pytest.raises(pd.SplitViolation):
        pd.evaluate_phrase_edits([early], cands)
    no_time = {k: v for k, v in sp.verify[0].items() if k != "publish_time"}
    with pytest.raises(pd.SplitViolation):
        pd.evaluate_phrase_edits([no_time], cands)
    # 只给一串说法(不带来历)不行
    with pytest.raises(TypeError):
        pd.evaluate_phrase_edits(sp.verify, list(cands.phrases))


def test_reuse_check_alone_refuses_selection_rows_without_publish_times():
    """挑词那批一对都没有发布时间(selected_through 是 None, 时间那道检查不会触发): 拿同一批去核验, 光靠
    稿 / 帖指纹也要拒绝。"""
    pairs, _ = _random_pairs(1, p_base=0.4, p_phrase=0.4, n_phrases=60, phrase_share=0.5, n_per_cat=20)
    rows = [{k: v for k, v in p.items() if k != "publish_time"} for p in pairs]
    cands = pd.select_candidate_phrases(rows, top_k=10)
    assert cands.source == "selected" and cands.selection_pairs == len(rows)
    assert cands.selected_through is None
    with pytest.raises(pd.SplitViolation):
        pd.evaluate_phrase_edits(rows, cands)


def test_reuse_check_crosses_document_roles():
    """核验那批的稿用了挑词那批的帖、或者核验那批的帖用了挑词那批的稿, 发布时间再晚也要拒绝。"""
    select = [
        {
            "draft_body": f"挑词批的合成稿子第{i}篇写了一段日常记录。",
            "published_body": f"挑词批的合成帖子第{i}篇发了一段日常记录。",
            "publish_time": T0 + timedelta(days=i),
        }
        for i in range(3)
    ]
    cands = pd.select_candidate_phrases(select, min_draft_docs=1)
    later = T0 + timedelta(days=30)
    fresh = {
        "category": "CAT_A",
        "draft_body": "核验批的合成稿子写了一段全新的日常记录。",
        "published_body": "核验批的合成帖子发了一段全新的日常记录。",
        "publish_time": later,
    }
    pd.evaluate_phrase_edits([fresh], cands)          # 不重叠、时间也晚: 不拦

    draft_reuses_pub = dict(fresh, draft_body=select[1]["published_body"])
    with pytest.raises(pd.SplitViolation):
        pd.evaluate_phrase_edits([draft_reuses_pub], cands)
    pub_reuses_draft = dict(fresh, published_body=select[2]["draft_body"])
    with pytest.raises(pd.SplitViolation):
        pd.evaluate_phrase_edits([pub_reuses_draft], cands)


def test_null_effect_does_not_pass_when_selection_and_verification_are_disjoint():
    """说法和删改毫无关系(每句 40% 被重写, review F1 的模拟设置): 挑词那批上自己核三个品类都「过线」,
    换到晚一段核验就过不了。"""
    for seed in (0, 1, 2):
        pairs, _ = _random_pairs(seed, p_base=0.4, p_phrase=0.4, n_phrases=60, phrase_share=0.5)
        sp = pd.split_pairs_by_time(pairs)
        cands = pd.select_candidate_phrases(sp.select, top_k=10, min_draft_docs=3)
        # 在挑词那批上自己核(只能绕成人工清单, 正常流程会被拒): 三个品类都「过线」
        circular = pd.evaluate_phrase_edits(sp.select, pd.CandidateSet.manual(cands.phrases), resamples=300)
        assert circular["passing_categories"] == 3
        res = pd.evaluate_phrase_edits(sp.verify, cands, resamples=300)
        assert res["gate_passed"] is False


def test_real_effect_passes_through_the_split_pipeline():
    """带说法的句子 70% 被重写、别的句子 30%: 早一段挑出来的就是这些说法, 晚一段核验能过闸。"""
    pairs, truth = _random_pairs(0, p_base=0.3, p_phrase=0.7, n_phrases=8, phrase_share=0.3)
    sp = pd.split_pairs_by_time(pairs)
    cands = pd.select_candidate_phrases(sp.select, top_k=10)
    assert cands.phrases and all(any(t in p or p in t for t in truth) for p in cands.phrases)
    res = pd.evaluate_phrase_edits(sp.verify, cands, resamples=300)
    assert res["gate_passed"] is True
    assert res["candidate_source"] == "selected"


# ── 候选说法 ────────────────────────────────────────────────────────────────────────────────

def test_extract_candidate_phrases_finds_overused_draft_phrases():
    drafts = [f"说白了今天记录第{i}条合成观察样本内容" for i in range(5)]
    published = [f"今天记录第{i}条合成观察样本内容" for i in range(5)]
    cands = pd.extract_candidate_phrases(drafts, published, min_draft_docs=3, min_len=3, max_len=3)
    assert [c["phrase"] for c in cands] == ["说白了"]
    assert pd.extract_candidate_phrases(drafts, [], min_draft_docs=3) == []


def test_short_published_fragment_counts_toward_published_docs():
    """发布帖用标点把说法单独隔成 <6 字的一段(「说白了！」), 说法其实都还在: 不能当成删了说法挑出来。"""
    drafts = [f"说白了今天记录第{i}条合成观察样本内容。" for i in range(5)]
    published = [f"说白了！今天记录第{i}条合成观察样本内容。" for i in range(5)]
    assert pd.extract_candidate_phrases(drafts, published, min_draft_docs=3) == []
    # 对照: 帖里真删了说法, 照样挑出来
    removed = [f"今天记录第{i}条合成观察样本内容。" for i in range(5)]
    assert [c["phrase"] for c in pd.extract_candidate_phrases(drafts, removed, min_draft_docs=3)] == ["说白了"]


def test_short_draft_fragment_counts_toward_draft_docs():
    """稿里说法单独成 <6 字的一段(「说白了！」)、发布时真删了: 稿那边也要数到它; 只把它并回下一句(只动标点)不算。"""
    drafts = [f"说白了！今天记录第{i}条合成观察样本内容。" for i in range(5)]
    removed = [f"今天记录第{i}条合成观察样本内容。" for i in range(5)]
    cands = pd.extract_candidate_phrases(drafts, removed, min_draft_docs=3)
    assert [(c["phrase"], c["draft_docs"], c["published_docs"]) for c in cands] == [("说白了", 5, 0)]
    merged = [f"说白了今天记录第{i}条合成观察样本内容。" for i in range(5)]
    assert pd.extract_candidate_phrases(drafts, merged, min_draft_docs=3) == []


def test_extract_candidate_phrases_respects_sentence_boundaries():
    """跨句拼接不能造出假候选词：仅仅调换句序时候选清单必须为空。"""
    drafts = ["今天下班去买菜。然后回家做晚饭。"] * 5
    published = ["然后回家做晚饭。今天下班去买菜。"] * 5
    assert pd.extract_candidate_phrases(drafts, published, min_draft_docs=3) == []
    # 短片段也一样不跨段拼接
    assert pd.extract_candidate_phrases(["说白了！今天下班去买菜。"] * 5, ["今天下班去买菜。说白了！"] * 5,
                                        min_draft_docs=3) == []


def test_extract_candidate_phrases_collapses_fragments_of_one_edit():
    """review 漏掉的第 1 条: 删掉「说白了」时, 跨到后面正文上的重叠碎片(带着产品词)不能把清单占满。"""
    drafts = [f"说白了这款面霜我用了第{i}周。周末去公园散步看见风筝第{i}只。" for i in range(10)]
    published = [f"这款面霜我用了第{i}周。周末去公园散步看见风筝第{i}只。" for i in range(10)]
    cands = pd.extract_candidate_phrases(drafts, published, top_k=10)
    assert [c["phrase"] for c in cands] == ["说白了"]


def test_extract_candidate_phrases_excludes_brand_and_product_terms():
    """被删的品牌名本身也是「稿里多、帖里少」: 调用方给了品牌 / 产品叫法, 碰到它的说法和同一处的碎片都不要。"""
    drafts = [f"我用SYNTH轻享第{i}周感觉不错。说白了今天记录第{i}条合成观察样本。" for i in range(8)]
    published = [f"我用第{i}周感觉不错。今天记录第{i}条合成观察样本。" for i in range(8)]
    plain = [c["phrase"] for c in pd.extract_candidate_phrases(drafts, published, top_k=10)]
    assert "说白了" in plain and any("SYNTH" in p for p in plain)

    cands = pd.extract_candidate_phrases(drafts, published, top_k=10, exclude_terms=["SYNTH 轻享"])
    assert [c["phrase"] for c in cands] == ["说白了"]
    runs = pd._exclusion_runs(["synth 轻享"])
    assert not any(pd._touches_excluded(c["phrase"], runs) for c in cands)


# ── 隐私边界 / 不接线 / 不调模型 ───────────────────────────────────────────────────────────

def test_summary_and_reprs_contain_no_raw_sentences_or_phrases():
    """公开日志安全边界：摘要 JSON 和对象 repr 里只能有计数和比例，绝不带原句或说法原文。"""
    secret_phrase = "绝密合成口头禅"
    secret_sentence = f"开场写一句包含{secret_phrase}的绝密原始测试草稿句子"
    pairs = [{
        "category": "SYNTH_CAT",
        "draft_body": f"{secret_sentence}。第二句是普通合成正文内容。",
        "published_body": "完全重写后的发布开场句子。第二句是普通合成正文内容。",
        "publish_time": T0,
    }]
    cands = pd.CandidateSet.manual([secret_phrase])
    res = pd.evaluate_phrase_edits(pairs, cands)
    dumped = json.dumps(res, ensure_ascii=False)
    assert secret_phrase not in dumped
    assert "绝密原始测试草稿" not in dumped
    assert res["diff_rev"] == pd.DIFF_REV == 2
    assert secret_phrase not in repr(cands)
    assert "绝密原始测试草稿" not in repr(pd.split_pairs_by_time(pairs))


def test_phrase_dryrun_is_not_wired_anywhere():
    """W-62 ① 只是干跑模块: 除了它自己和这份测试, 仓里没有任何地方 import 它(没有 CLI、tools、app、定时任务入口)。"""
    me = Path(__file__).resolve()
    module = REPO / "deskcore" / "phrase_dryrun.py"
    files = list(REPO.glob("*.py")) + list((REPO / "deskcore").rglob("*.py")) + list((REPO / "scripts").rglob("*.py"))
    files += list((REPO / "tests").glob("*.py"))
    hits = [f.name for f in files if f.resolve() not in (me, module)
            and "phrase_dryrun" in f.read_text(encoding="utf-8")]
    assert hits == []


def test_phrase_dryrun_has_no_llm_or_db_imports():
    """deskcore 铁律：纯计算模块不引入 LLM SDK 或数据库依赖。"""
    tree = ast.parse((REPO / "deskcore" / "phrase_dryrun.py").read_text(encoding="utf-8"))
    forbidden = {"anthropic", "openai", "google", "supabase", "httpx", "requests", "psycopg2"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in forbidden
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in forbidden
