"""§7.1 里【当前会红】的两条(审计 SUP-024)。

这两条不是"顺手发现的新 bug", 而是审计报告里已经立案、但**不在第 4 批
八条范围内**的两项。§7.1 的测试计划点名要它们, 所以照写 —— 但用
``xfail(strict=True)`` 标记, 理由是:

  · 直接让 CI 红 = 把两条计划外的修复硬塞进本批, 而它们各自都不是一行改动;
  · 悄悄不写 = 测试计划里最有价值的两条被静默跳过, 下次没人记得。

``strict=True`` 是关键: 一旦有人把它修好, xfail 会变成 **XPASS 失败**, 逼着
下一个人把标记摘掉、变成真正的回归。这样它既不挡路, 也不会烂在这儿。
"""

from __future__ import annotations

import pytest

import config
import generator
from deskcore import fingerprint as fp


# ══════════════════════════════════════════════════════════════════════
# SUP-006 · max_tokens 没有按 model 上限 clamp
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.xfail(
    strict=True,
    reason="SUP-006 未修(不在第 4 批八条范围内, 单独立项): "
           "max_tokens = max(2048, count*2500) 是**纯算术**, 从不查模型的输出 "
           "上限 —— config 里只有 MODEL_CONTEXT_WINDOWS(那是**输入**窗口), "
           "没有任何一张表记录 per-model 的 max output。所以本条断言的是那个 "
           "缺失的前提: 先有查表能力, clamp 才谈得上。",
)
def test_sup006_needs_a_per_model_output_cap():
    # ⚠️ 第一版把这条写成"断言 max_tokens ≤ 128000", 结果它 **XPASS** 了 ——
    # count=50 算出来是 125,000, 恰好在 128K 以下。也就是说那个阈值根本没在
    # 验 SUP-006, 只是碰巧成立。真正缺的东西是【按模型查输出上限】这件事本身:
    # 换个上限更低的模型(项目的 CLAUDE_MODELS 里就有 haiku), 同样的 count
    # 依旧会超, 而代码里没有任何地方能看出这一点。
    lookup = getattr(config, "get_max_output_tokens", None)
    assert callable(lookup), "config 没有 per-model 输出上限查表, 无从 clamp"

    engine = generator.ClaudeEngine.__new__(generator.ClaudeEngine)
    for model in config.CLAUDE_MODELS:
        params = generator.ClaudeEngine._make_params(
            engine, model, count=config.MAX_GENERATION_COUNT)
        assert params["max_tokens"] <= lookup(model), (model, params)


def test_sup006_small_count_is_sane_today():
    """把**现状**钉住: 常规批量(count≤8)下的预算没有回归空间。

    上面那条是"还没修", 这条是"别在修它的时候把正常路径改坏"。
    """
    engine = generator.ClaudeEngine.__new__(generator.ClaudeEngine)
    for count in (1, 3, 8):
        params = generator.ClaudeEngine._make_params(
            engine, config.CLAUDE_MODEL, count=count)
        assert params["max_tokens"] >= 2048
        assert params["max_tokens"] == max(2048, count * 2500)


# ══════════════════════════════════════════════════════════════════════
# COR-014 · bottom-k 的 Jaccard 在长短稿之间系统性偏低
# ══════════════════════════════════════════════════════════════════════

def _passage(seed: int, n_sentences: int) -> str:
    """造一段可控长度的中文正文。句式固定、词随 seed 变, 保证:
    短稿是长稿的**逐字子串**(真抄), 而不是"话题相近"。"""
    return "".join(
        f"第{seed + i}段讲的是这个产品在日常使用里的具体细节和真实感受。"
        for i in range(n_sentences)
    )


def test_cor014_baseline_short_inside_long_is_detected_today():
    """先把**当前**能力钉住: 长度相当的两篇, 抄了就该被抓到。

    这条现在是绿的 —— 它存在是为了证明下面那条红的不是"整个机制都坏了",
    而是**特定形状**(长短差距大)下的失真。
    """
    body = _passage(0, 12)
    a = set(fp.ngram_hashes(body))
    b = set(fp.ngram_hashes(body))
    assert fp.jaccard(a, b) == 1.0, "同一篇自己跟自己都不是 1.0, 机制坏了"


@pytest.mark.xfail(
    strict=True,
    reason="COR-014 未修(本批第 13 项, 排在测试地基之后): ngram_hashes 对**每篇 "
           "各自**取 bottom-200, 长短稿的第 200 小阈值差着量级 —— 短稿逐字抄自 "
           "长稿时, 两个采样集几乎不相交, Jaccard 远低于 0.35 的硬闸线, 直接放行。"
           "修法是改成标准 bottom-k 估计(对两个 sketch 的并集再取 bottom-k, "
           "算其中同时属于两边的比例), 存量指纹不用重算。",
)
def test_cor014_short_copied_from_long_should_be_caught():
    long_body = _passage(0, 120)          # 长稿
    short_body = _passage(0, 12)          # 短稿 = 长稿开头的逐字一段
    assert short_body in long_body, "构造错了, 短稿不是长稿的子串"

    a = set(fp.ngram_hashes(short_body))
    b = set(fp.ngram_hashes(long_body))
    assert fp.jaccard(a, b) >= fp.NGRAM_JACCARD_HARD, (
        f"逐字抄袭被判为不重复: jaccard={fp.jaccard(a, b):.4f} "
        f"< 硬闸线 {fp.NGRAM_JACCARD_HARD}")
