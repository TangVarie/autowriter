"""deskcore/fingerprint.py — 指纹与判定(确定性, 不依赖任何外部服务)。

⚠️ 本模块【只用标准库】, 这是刻意的: 查重与发牌是 deskcore 的两条命脉,
它们的回归必须在最便宜的环境里就能跑(CI 不装 supabase/anthropic 也能跑
``python -m deskcore.cli selftest``)。所以判定阈值和 angle_key 都放这里,
不放 core.py —— core 顶层 import db, 会把整条依赖链拖进来。

成稿指纹三种, 各挡一类重复:
  opening_hash  正文开头切入方式雷同 —— 最省, 且【标题换了也能抓】
  ngram_hashes  正文四字串重合 —— 抓"换了词但还是同一篇"的换皮改写
  title 向量    同角度换说法的标题(语义, 在 core.py 里用 dedup.py 算)

前两种是纯字符串运算, 没有 GOOGLE_API_KEY 也能跑 —— 这是刻意的:
autowriter 原来的查重只比标题向量(app.py:520), embedding 一挂就整个失效。
"""

from __future__ import annotations

import hashlib
import re

_PUNCT_RE = re.compile(
    r"[\s，。！？、；：""''《》（）()\[\]…—~·,.!?;:'\"\-_/\\|+*#@$%^&]+"
)


def normalize(text: str) -> str:
    """去标点空白后的规范化串。"""
    return _PUNCT_RE.sub("", text or "")


def opening_of(body: str, n: int = 25) -> str:
    """正文首个非空行前 n 字 —— 与 db.get_recent_titles_and_openings 同口径。"""
    for line in (body or "").splitlines():
        s = line.strip()
        if s:
            return s[:n]
    return ""


def sha16(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def opening_hash(body: str) -> str:
    return sha16(normalize(opening_of(body)))


def ngram_hashes(text: str, n: int = 4, cap: int = 200) -> list[str]:
    """正文 n 字 shingle 的 hash 集合(去重、有上限)。

    对齐 human-writing/scripts/check_prose.py 的跨篇四字串检测: 两篇共享大量
    四字串 = 模板化, 哪怕换了词也能抓出来。

    cap 是防长文把行撑爆; 超了【均匀采样】而不是取前 cap 个 —— 取前 N 会让
    所有长文只比开头, 后半篇随便抄都查不出来。
    """
    norm = normalize(text)
    if len(norm) < n:
        return []
    grams = {norm[i:i + n] for i in range(len(norm) - n + 1)}
    hashed = sorted(sha16(g) for g in grams)
    if len(hashed) <= cap:
        return hashed
    step = len(hashed) / cap
    return [hashed[int(i * step)] for i in range(cap)]


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ── 查重判定 ──────────────────────────────────────────────────────────────

# autowriter 原来用 0.92 且【只比标题】(app.py:520 + config.py:140), 实测换个
# 说法的同角度标题普遍落在 0.85-0.90 全部溜过。这里下调阈值, 但标题只是三个
# 信号之一 —— 单信号不判死, 见 verdict()。
TITLE_SIM_HARD = 0.90
TITLE_SIM_WARN = 0.84
NGRAM_JACCARD_HARD = 0.35
NGRAM_JACCARD_WARN = 0.22


def verdict(title_sim: float, opening_exact: bool, ngram_j: float) -> tuple[str, str]:
    """三个信号合议出结论。

    为什么不是单信号判死: 阈值下调后单看标题会误伤合法的角度变体。所以要求
    「一个强信号」或「两个弱信号」才 reject。
    """
    if opening_exact:
        return "reject", "正文开头与历史稿完全一致"
    if title_sim >= TITLE_SIM_HARD:
        return "reject", f"标题语义与历史稿高度重合(cos={title_sim:.3f})"
    if ngram_j >= NGRAM_JACCARD_HARD:
        return "reject", f"正文与历史稿大面积重合(四字串 Jaccard={ngram_j:.3f})"
    weak, why = 0, []
    if title_sim >= TITLE_SIM_WARN:
        weak += 1
        why.append(f"标题接近(cos={title_sim:.3f})")
    if ngram_j >= NGRAM_JACCARD_WARN:
        weak += 1
        why.append(f"正文用词接近(Jaccard={ngram_j:.3f})")
    if weak >= 2:
        return "reject", "；".join(why) + " —— 两项同时接近"
    if weak == 1:
        return "warn", why[0]
    return "pass", ""


# ── 发牌坐标指纹 ──────────────────────────────────────────────────────────

def angle_key(dims: dict) -> str:
    """创作坐标的规范化指纹。只取参与无放回抽样的四个主维度。

    情绪强度 / 时效依赖 / 词感是叠加项【不进 key】—— 否则同一个核心组合换个
    词感就被当成"没用过", 跨批次台账就白记了。
    """
    core = "|".join(str(dims.get(k, "")) for k in
                    ("emotional_lever", "human_truth_archetype",
                     "content_format", "title_structure"))
    return hashlib.sha256(core.encode("utf-8")).hexdigest()[:20]
