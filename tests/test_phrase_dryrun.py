"""deskcore/phrase_dryrun.py 单元测试（W-62 ① / D-166；全合成样本、零库零模型）。"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from deskcore import phrase_dryrun as pd


def test_stratum_baseline_neutralizes_position_confounder():
    """开头句本身 100% 被改、中间句 0% 被改：候选词只在开头句出现时，分层净抬升应为 0 且不过线。"""
    pairs = []
    # 6 pairs with phrase in opening sentence (always edited, word removed), middle untouched
    for i in range(6):
        pairs.append(
            {
                "category": "CAT_A",
                "draft_body": f"其实说白了这事真挺简单第{i}版。中段讲一个具体的生活细节过程。尾段交代普通收尾。",
                "published_body": f"换一种完全不同的开篇讲法版本{i}。中段讲一个具体的生活细节过程。尾段交代普通收尾。",
            }
        )
    # 6 pairs WITHOUT phrase in opening sentence (also always edited!), middle untouched
    for i in range(6):
        pairs.append(
            {
                "category": "CAT_A",
                "draft_body": f"普通的开场第一句话示例第{i}版。中段讲另一个具体日常画面过程。尾段交代普通收尾。",
                "published_body": f"重新写了一段全新的开场句子{i}。中段讲另一个具体日常画面过程。尾段交代普通收尾。",
            }
        )

    res = pd.evaluate_phrase_edits(pairs, ["其实说白了"], min_hits=5, min_lift=0.10, min_categories_for_go=1)
    cat = res["categories"]["CAT_A"]
    assert cat["phrase_hits"] == 6
    assert cat["phrase_removed_rate"] == 1.0
    # Naive raw baseline sees mostly unchanged middle/closing sentences (~0.20),
    # whereas stratum-matched baseline compares opening-medium vs opening-medium (= 1.0)!
    assert cat["raw_baseline_changed_rate"] < 0.35
    assert cat["stratum_baseline_changed_rate"] == 1.0
    assert cat["word_removal_lift"] == 0.0
    assert cat["passed_category_gate"] is False
    assert res["gate_passed"] is False


def test_word_level_disappearance_separates_kept_phrase_edits():
    """编辑改了句子的别处但保留了候选词：sentence_changed_rate 高，但 phrase_removed_rate 必须为 0。"""
    pairs = []
    for i in range(6):
        pairs.append(
            {
                "category": "CAT_B",
                "draft_body": (
                    f"开场交代一下今天的合成记录第{i}篇。"
                    f"老实讲这个普通记录过程里第{i}次我先测了半小时再放回桌面。"
                    f"结尾写一句合成的日常收束。"
                ),
                "published_body": (
                    f"开场交代一下今天的合成记录第{i}篇。"
                    # Phrase "老实讲" is KEPT; only tail words are tweaked so sentence is 'edited'
                    f"老实讲这个普通记录过程里第{i}次我先测了二十分钟再收好。"
                    f"结尾写一句合成的日常收束。"
                ),
            }
        )
    res = pd.evaluate_phrase_edits(pairs, ["老实讲"], min_hits=5, min_lift=0.10, min_categories_for_go=1)
    cat = res["categories"]["CAT_B"]
    assert cat["phrase_hits"] == 6
    assert cat["phrase_sentence_changed"] == 6
    assert cat["phrase_word_kept_on_edit"] == 6
    assert cat["phrase_word_removed"] == 0
    assert cat["sentence_changed_rate"] == 1.0
    assert cat["phrase_removed_rate"] == 0.0
    assert cat["passed_category_gate"] is False


def test_multi_category_gate_requires_at_least_two_categories():
    """同一分层下候选词真被运营删掉：只有 1 个品类过线时整体仍不放行，≥2 个品类才 gate_passed=True。"""
    def _cat_pairs(cat_name: str, drop_phrase: bool):
        rows = []
        for i in range(6):
            # Middle sentence with phrase: removed/replaced when drop_phrase=True, kept otherwise
            mid_pub = (
                f"这一句完全改成了平实描述版本{i}号。"
                if drop_phrase
                else f"毫无悬念这次记录里第{i}次过程非常平稳。"
            )
            rows.append(
                {
                    "category": cat_name,
                    "draft_body": (
                        f"开场交代普通合成场景记录{i}号。"
                        f"毫无悬念这次记录里第{i}次过程非常平稳。"
                        f"另外补充一句不含候选词的同等长度中段句子{i}。"
                        f"结尾写一句普通收束记录。"
                    ),
                    "published_body": (
                        f"开场交代普通合成场景记录{i}号。"
                        f"{mid_pub}"
                        f"另外补充一句不含候选词的同等长度中段句子{i}。"
                        f"结尾写一句普通收束记录。"
                    ),
                }
            )
        return rows

    one_cat = _cat_pairs("CAT_1", True) + _cat_pairs("CAT_2", False)
    r1 = pd.evaluate_phrase_edits(one_cat, ["毫无悬念"], min_hits=5, min_lift=0.10, min_categories_for_go=2)
    assert r1["passing_categories"] == 1
    assert r1["gate_passed"] is False

    two_cats = _cat_pairs("CAT_1", True) + _cat_pairs("CAT_2", True)
    r2 = pd.evaluate_phrase_edits(two_cats, ["毫无悬念"], min_hits=5, min_lift=0.10, min_categories_for_go=2)
    assert r2["passing_categories"] == 2
    assert r2["gate_passed"] is True
    assert r2["categories"]["CAT_1"]["word_removal_lift"] == 1.0
    assert r2["categories"]["CAT_2"]["word_removal_lift"] == 1.0


def test_summary_contains_no_raw_sentences_or_phrases():
    """公开日志安全边界：摘要 JSON 里只能有计数和比例，绝不泄露原句或候选词原文。"""
    secret_phrase = "绝密合成口头禅"
    secret_sentence = f"开场写一句包含{secret_phrase}的绝密原始测试草稿句子"
    pairs = [
        {
            "category": "SYNTH_CAT",
            "draft_body": f"{secret_sentence}。第二句是普通合成正文内容。",
            "published_body": "完全重写后的发布开场句子。第二句是普通合成正文内容。",
        }
    ]
    res = pd.evaluate_phrase_edits(pairs, [secret_phrase])
    dumped = json.dumps(res, ensure_ascii=False)
    assert secret_phrase not in dumped
    assert "绝密原始测试草稿" not in dumped
    assert res["diff_rev"] == pd.DIFF_REV


def test_extract_candidate_phrases_finds_overused_draft_phrases():
    drafts = [f"说白了今天记录第{i}条合成观察样本内容" for i in range(5)]
    published = [f"今天记录第{i}条合成观察样本内容" for i in range(5)]
    cands = pd.extract_candidate_phrases(drafts, published, min_draft_docs=3, min_len=3, max_len=3, min_ratio=2.0)
    phrases = [c["phrase"] for c in cands]
    assert "说白了" in phrases
    assert pd.extract_candidate_phrases(drafts, [], min_draft_docs=3) == []


def test_negative_lag_days_pairs_are_skipped():
    pairs = [
        {"category": "CAT_X", "lag_days": -2, "draft_body": "说白了第一句。第二句。", "published_body": "改写第一句。第二句。"},
        {"category": "CAT_X", "lag_days": 1, "draft_body": "正常第一句。第二句。", "published_body": "正常第一句。第二句。"},
    ]
    res = pd.evaluate_phrase_edits(pairs, ["说白了"])
    assert res["skipped_negative_lag"] == 1
    assert res["total_pairs"] == 1
    assert res["total_phrase_hits"] == 0


def test_phrase_dryrun_has_no_llm_or_db_imports():
    """deskcore 铁律：纯计算模块不引入 LLM SDK 或数据库依赖。"""
    src = Path(__file__).resolve().parent.parent / "deskcore" / "phrase_dryrun.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    forbidden = {"anthropic", "openai", "google", "supabase", "httpx", "requests", "psycopg2"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in forbidden
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in forbidden
