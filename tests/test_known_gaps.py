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
# COR-014 —— 已修, 见 tests/test_dedup_containment.py
# ══════════════════════════════════════════════════════════════════════
#
# 这里原来有一条 xfail(strict=True), 现在摘掉了 —— 但**它的 reason 写错了**,
# 值得留个记录, 因为错的方式很典型。
#
# 那条 xfail 断言的是「短稿逐字抄自长稿时, fp.jaccard 应该 ≥ 0.35」, reason
# 里写的修法是「改成标准 bottom-k 估计」。真去量之后发现:
#
#   · 估计确实有偏(直接对两个 sketch 求交并比, 偏差最大 0.125),
#     改成标准估计式之后降到 0.045 —— 这一半是对的;
#   · **但那条断言本身仍然不成立**。280 字的短稿整篇塞进 2800 字的长稿里,
#     Jaccard 的**真值**就是 0.1。Jaccard = |A∩B|/|A∪B|, 分母被长稿撑大,
#     这是定义决定的, 不是精度问题。再准的估计也够不着 0.35。
#
# 也就是说我当时给出的"修法"根本修不好我自己写的那条断言。真正缺的是
# **另一个指标**: 包含度 |A∩B|/min(|A|,|B|)。合成对照 7/7 全拦下(原来 1/7)。
#
# 教训与 §0.4 那条是同一件事的第三面: **先量再改**。"bottom-k 用错了"这个
# 诊断是对的, 但它只解释了一部分现象, 而我把它当成了全部, 还把它写进了
# xfail 的 reason —— 下一个人照着做会发现改完测试还是红的。

def _passage(seed: int, n_sentences: int) -> str:
    """造一段可控长度的中文正文。句式固定、词随 seed 变, 保证:
    短稿是长稿的**逐字子串**(真抄), 而不是"话题相近"。"""
    return "".join(
        f"第{seed + i}段讲的是这个产品在日常使用里的具体细节和真实感受。"
        for i in range(n_sentences)
    )


def test_cor014_short_copied_from_long_is_caught_now():
    """本条从 xfail(strict) 变成真回归。判据换成包含度 —— 见上面那段说明。"""
    long_body = _passage(0, 120)
    short_body = _passage(0, 12)          # 长稿开头的逐字一段
    assert short_body in long_body, "构造错了, 短稿不是长稿的子串"

    a = set(fp.ngram_hashes(short_body))
    b = set(fp.ngram_hashes(long_body))
    j, contain, sample = fp.sketch_overlap(a, b)

    assert sample >= fp.CONTAIN_MIN_SAMPLE, (
        f"有效样本量只有 {sample}, 这个形状下包含度不该被采信")
    assert contain >= fp.NGRAM_CONTAIN_HARD, (
        f"逐字抄袭没被抓到: 包含度={contain:.4f} < {fp.NGRAM_CONTAIN_HARD}")
    # 同时钉住"为什么不能靠 Jaccard": 它在这个形状下**本来就**够不着硬闸线。
    assert j < fp.NGRAM_JACCARD_HARD, (
        f"Jaccard={j:.4f} 居然够着硬闸线了 —— 那上面那段说明就得重写")
    assert fp.deciding_signals(0.0, False, j, contain, sample)[0] == "reject"
