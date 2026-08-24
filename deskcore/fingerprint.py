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
import unicodedata

# ── 规范化 ────────────────────────────────────────────────────────────────
#
# 判据: **去掉所有标点与符号运算符, 保留文字与象形符号(emoji)**。
# 具体是 Unicode 类别 Cc/Cf(控制/格式) · Zs/Zl/Zp(分隔) · Pc/Pd/Ps/Pe/Pi/Pf/Po
# (全部标点) · Sm/Sc/Sk(数学/货币/修饰符号), 外加 str.isspace()。
# **不去 So** —— emoji 属于那一类, "换个 emoji 算不算同一篇"是产品问题, 不该
# 由一个规范化函数顺手决定。
#
# ── 为什么从字符类改成类别判定(审计 COR-014 的后续) ────────────────────
# 原来是一行手写的字符类, 而且写坏了: `r"[...："` 后面跟着两个 ASCII 双引号,
# raw 串在那里就结束了, 剩下半截是**非 raw** 串。后果有三 ——
#   · `\[` / `\-` 是非法转义(Python 3.12+ 起会 SyntaxWarning);
#   · `\\` 在非 raw 串里塌成一个反斜杠, 转义掉了后面的 `|`, 于是**反斜杠本身
#     不在类里**;
#   · 那两个 ASCII 双引号被当成串边界吃掉了。
#
# 但真正的问题比手误更大: **中文弯引号 “ ” ‘ ’ 从来就没写进去过**, 「」『』
# 【】〈〉〔〕 也没有。实测同一篇稿子把 “” 换成 "", Jaccard 掉到 0.333 ——
# 正好在 0.35 硬闸线之下, 开头指纹也对不上。那是一条**能绕过查重的路子**,
# 而 【】 在小红书文案里遍地都是。逐个往字符类里补是补不完的(当时数出 37 个
# 常见中文标点没被覆盖), 所以换成按类别判。
#
# 性能: 带 memo 的逐字判定比原来的正则慢约 8 倍, 但绝对值是 0.32ms / 3000 字,
# 回填几千篇也就多一两秒。查重硬闸的正确性不该为这个让路。
#
# ⚠️⚠️ **改这里 = 让存量指纹作废。** 库里的 ngram_hashes / opening_hash 都是用
# 当时的口径算出来的; 口径一变, 新稿算出来的四字串就和历史对不上, 查重在过渡期
# **反而更弱**, 而且不报错。所以改完必须跑一次全量重算:
#     python -m deskcore.cli recompute-fingerprints --project <id> --user <id>
# 详见 docs/deskcore.md 与那个子命令的说明(它会告诉你有多少行**重算不了**)。
_DROP_CATEGORIES = frozenset((
    "Cc", "Cf",                                    # 控制符 / 格式符(含零宽连接)
    "Zs", "Zl", "Zp",                              # 各种分隔符
    "Pc", "Pd", "Ps", "Pe", "Pi", "Pf", "Po",      # 全部标点
    "Sm", "Sc", "Sk",                              # 数学 / 货币 / 修饰符号
))
_DROP_CACHE: dict[str, bool] = {}


def _is_droppable(ch: str) -> bool:
    hit = _DROP_CACHE.get(ch)
    if hit is None:
        hit = ch.isspace() or unicodedata.category(ch) in _DROP_CATEGORIES
        _DROP_CACHE[ch] = hit
    return hit


def normalize(text: str) -> str:
    """去标点 / 空白 / 符号运算符之后的规范化串。emoji 保留 —— 见上面的说明。"""
    return "".join(c for c in (text or "") if not _is_droppable(c))


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
    """正文开头的指纹。**没有开头就返回空串, 不返回 sha16("") 那个常量。**

    ⚠️ 这个 early return 不是洁癖: 没有它, 所有"只有标题、正文为空"的行都会
    拿到【同一个】非空哈希。而 backfill 特意保留了 title-only 的历史版本、
    check_drafts 也接受没有正文的稿子 —— 于是库里只要存进一条 title-only,
    之后每一条 title-only 新稿都会被判成"正文开头与历史稿完全一致"而 reject,
    跟它标题写什么毫无关系。opening_exact 是【强信号、单独就判死】, 所以这个
    误伤没有任何东西能兜住。空开头就该让位给标题语义去判。(codex review)
    """
    opening = normalize(opening_of(body))
    return sha16(opening) if opening else ""


# sketch 的大小。**从 200 提到 400**(审计 COR-014)。
#
# 为什么要提: 包含度这一路的有效样本量 m 由长稿 sketch 的第 cap 小值决定 ——
# cap 越小, 两个 sketch 共同覆盖的 hash 区间越窄。实测真阳性(短稿逐字抄长稿)
# 的 m 分布:
#
#   形状            cap=200 的 m(p05/中位)   cap=400 的 m(p05/中位)
#   8 句 ⊂ 80 句          14 / 19                 33 / 40
#   12 句 ⊂ 120 句        12 / 21                 31 / 41
#   20 句 ⊂ 200 句        13 / 22                 33 / 42
#   30 句 ⊂ 300 句        14 / 22                 33 / 43
#
# CONTAIN_MIN_SAMPLE 是 15。也就是说 cap=200 时, **最常见的那几个形状有 5% 以上
# 的概率样本量不够、这一路直接不发言** —— 抓不抓得到全看运气。cap=400 之后
# p05 也在 30 以上。
#
# ── 兼容性: 不需要重算存量指纹 ────────────────────────────────────────
# 新旧 sketch 混着比是**正确**的: sketch_overlap 取 t = min(两边最大值), 拿
# cap=400 的新稿去比 cap=200 的老指纹时, t 由老的那边决定, 精度退回老水平 ——
# 与今天完全一样, 不会更差。新稿之间的比对才享受到双倍分辨率, 随着库里新行
# 增多自然变好。想让老行也升上来就重跑一次回填, 但那是可选的。
#
# 代价: 每行指纹的 ngram_hashes 从约 3.4KB 涨到约 6.8KB, GIN 索引同步变大。
# 按现有体量(约 4600 条成稿)是几十 MB 级, 而这是查重硬闸的分辨率, 值这个价。
NGRAM_CAP = 400


def ngram_hashes(text: str, n: int = 4, cap: int = NGRAM_CAP) -> list[str]:
    """正文 n 字 shingle 的 hash 集合(去重、有上限)。

    对齐 human-writing/scripts/check_prose.py 的跨篇四字串检测: 两篇共享大量
    四字串 = 模板化, 哪怕换了词也能抓出来。

    cap 是防长文把行撑爆。超了取【全局最小的 cap 个 hash】(bottom-k / MinHash
    的标准做法), 不是按名次均匀采样。

    ⚠️ 这一点很容易写错, 而且错了不报错: 按名次采样(step = len/cap 取第
    i*step 个)【不是内容稳定的】—— 一个字的增删会改变整张排序表里所有元素的
    名次, 两篇几乎相同的长文可能采出完全不同的子集, 算出来的 Jaccard 低到
    连 0.22 的 warn 线都够不上, 直接从硬闸溜过去。

    bottom-k 稳定是因为: 某个 4-gram 在不在结果里, 只取决于【它自己的 hash 值】
    与第 k 小值的大小关系, 与文本长度和其它 gram 的名次无关。两篇共享的
    gram 会一起进、一起出, Jaccard 近似无偏。

    ⚠️ 返回值是 **sketch 不是完整集合**。拿两个 sketch 比对时必须走
    ``sketch_overlap``, 直接 ``jaccard(set(a), set(b))`` 会系统性偏低 ——
    审计 COR-014 就是这一条, 完整推导在 ``sketch_overlap`` 的文档里。

    这也保证覆盖全文而不是只比开头 —— hash 值与 gram 在文中的位置无关。
    """
    norm = normalize(text)
    if len(norm) < n:
        return []
    grams = {norm[i:i + n] for i in range(len(norm) - n + 1)}
    hashed = sorted(sha16(g) for g in grams)
    return hashed[:cap]


def jaccard(a: set[str], b: set[str]) -> float:
    """两个**完整**集合的 Jaccard。

    ⚠️ 对两个 bottom-k **sketch** 直接调这个是错的 —— 见 ``sketch_overlap``。
    保留它是因为 selftest 里拿全量 gram 集算地面真值时要用。
    """
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def sketch_overlap(sa: set[str], sb: set[str]) -> tuple[float, float, int]:
    """两个 bottom-k sketch 的 (Jaccard, 包含度, 有效样本量)。审计 COR-014。

    ── 为什么不能对两个 sketch 直接算 Jaccard ─────────────────────────────
    ``ngram_hashes`` 给**每篇各自**取最小的 cap 个 hash。两篇长度差得多时,
    它们的第 cap 小值差着量级 —— 短稿的 sketch 覆盖 hash 空间的一大片,
    长稿的只覆盖很窄的一条。直接求交并, 短稿里那些"落在长稿 sketch 之外"的
    hash 会被当成"长稿没有", 于是估计值系统性偏低。

    实测(3000 次合成对照): 直接算的偏差最大 0.125, 换成下面这个做法之后
    最大 0.045 —— 而 0.35 的硬闸线上, 0.125 的偏差足够让该拦的溜过去。

    ── 做法: 只在【两个 sketch 都覆盖到的 hash 区间】上算 ──────────────────
    令 ``t = min(max(S_A), max(S_B))``。对任意 ``v ≤ t``:

        v ∈ A  ⟺  v ∈ S_A     (v ∈ A 且 v ≤ t ≤ max S_A ⇒ v 在 A 最小的 cap 个里)

    也就是说在 ``[0, t]`` 这个子域上, **两边的成员判定都是精确的**。而 sha256
    的输出均匀, 所以这个子域是全域的一个均匀随机样本 —— 在它上面算出来的比值
    就是全域比值的无偏估计。这是 bottom-k / MinHash 的标准估计式。

    **存量指纹不用重算**: 它只用已经存下来的两个 sketch, 不需要原文。

    ── 为什么还要返回包含度 ───────────────────────────────────────────────
    这是量化之后才看清的一件事: **改准了估计, 并不能抓住"短稿逐字抄长稿"**。
    因为那个形状下 Jaccard 的**真值**本来就小 —— 短稿整篇塞进长稿里,
    ``J = |A| / |B|``, 280 字抄进 2800 字的稿子里真值就是 0.1, 再准的估计
    也够不着 0.35。Jaccard 天生对长度差敏感, 这不是精度问题, 是**指标选错了**。

    包含度 ``|A∩B| / min(|A|,|B|)`` 问的是另一个问题:"短的那篇有多少比例出现在
    长的那篇里"。逐字抄袭时它是 1.0, 与长度差无关。

    ── 第三个返回值: 有效样本量 ───────────────────────────────────────────
    ``min(|a|, |b|)`` —— 子域里较小的那一边有几个元素。它决定估计的噪声,
    调用方**必须**拿它当闸: 样本量太小时包含度会剧烈抖动(实测 100 字草稿 vs
    6000 字历史时子域只剩 4 个元素, 完全无关的两篇也能撞出包含度 1.0)。
    见 ``CONTAIN_MIN_SAMPLE``。
    """
    if not sa or not sb:
        return 0.0, 0.0, 0
    # sha16 是定长小写十六进制, 字典序 == 数值序, 直接比字符串即可
    # (SQL 侧的同款实现靠的也是这一点)。
    t = min(max(sa), max(sb))
    a = {h for h in sa if h <= t}
    b = {h for h in sb if h <= t}
    inter = len(a & b)
    union = len(a | b)
    sample = min(len(a), len(b))
    return (inter / union if union else 0.0,
            inter / sample if sample else 0.0,
            sample)


# ── 查重判定 ──────────────────────────────────────────────────────────────

# autowriter 原来用 0.92 且【只比标题】(app.py:520 + config.py:140), 实测换个
# 说法的同角度标题普遍落在 0.85-0.90 全部溜过。这里下调阈值, 但标题只是三个
# 信号之一 —— 单信号不判死, 见 verdict()。
TITLE_SIM_HARD = 0.90
TITLE_SIM_WARN = 0.84
NGRAM_JACCARD_HARD = 0.35
NGRAM_JACCARD_WARN = 0.22

# 包含度(审计 COR-014)。阈值不是拍的, 是从**零分布**定的 —— 合成对照 6000 次,
# 按【有效样本量 m】分桶(m 才是决定噪声的量, 句数只是间接因素):
#
#   m 区间   次数   完全无关两篇的包含度 p99 / max   误杀(≥0.60)
#    0-4      265        1.000 / 1.000                 8   ← 完全不可用
#    5-9      554        0.571 / 0.600                 5   ← 仍会误杀
#   10-14     466        0.364 / 0.417                 0
#   15-19    1054        0.278 / 0.316                 0
#   20-24    1113        0.238 / 0.333                 0
#   30-34     291        0.267 / 0.300                 0
#   50-54     175        0.160 / 0.176                 0
#  100-104     73        0.120 / 0.120                 0
#
# m ≥ 15 的 4707 次里, 无关稿子的包含度**最大 0.333** —— 离 0.60 有近一倍余量。
# 真阳性(短稿逐字抄自长稿)在同样设置下**全部是 1.000**。所以两条线两边都很宽,
# 真正的风险不是阈值定得高低, 而是**样本量太小时估计根本没意义**。
NGRAM_CONTAIN_HARD = 0.60
NGRAM_CONTAIN_WARN = 0.40

# 有效样本量低于这个数就【完全不发包含度信号】。15 这个数就是上表里"误杀归零、
# 且尾部离硬闸线还有一倍余量"的那一档。
#
# ⚠️ 这里刻意 fail-open, 与本仓其它地方的 fail-closed 口径相反, 理由是具体的:
# 样本量 4 的时候无关稿子也能撞出 1.0, 按硬闸处理就是**误杀正常稿子**。而误杀
# 比漏检更难被发现 —— 漏检至少还有另外三路信号和人工复核, 误杀只会让用户觉得
# "这系统老让我重写", 然后没人再信这个闸。样本量不够时其余三路照常跑, 而且
# check_drafts 会在 summary 里明说这一路没生效(containment_skipped_warning)。
#
# ⚠️ 定成 30 会把**最常见的那个形状**排除掉: 280 字草稿 vs 2800 字历史稿的
# m 大约是 20。第一版就是 30, 于是"修好了但最该抓的那档抓不到"。
CONTAIN_MIN_SAMPLE = 15


def deciding_signals(title_sim: float, opening_exact: bool, ngram_j: float,
                     ngram_contain: float = 0.0,
                     contain_sample: int = 0) -> tuple[str, str, list[str]]:
    """四个信号合议出结论, 并说清【是哪个信号定的】。

    为什么不是单信号判死: 阈值下调后单看标题会误伤合法的角度变体。所以要求
    「一个强信号」或「两个弱信号」才 reject。

    第三个返回值是判定所依据的信号名(``opening`` / ``title`` / ``ngram`` /
    ``contain``)。调用方要靠它把"撞的是哪一条"归到对的来源上 —— 各路信号的最佳
    命中可能来自不同的稿子(甚至分属本批内和历史), 归错了就会让人对着一条根本
    没引发拒绝的稿子去改。(codex review)

    ``ngram_contain`` / ``contain_sample`` 默认值让老调用方(只喂三路的)行为不变
    —— 包含度为 0 就等于这一路没发言。
    """
    contain_ok = contain_sample >= CONTAIN_MIN_SAMPLE
    if opening_exact:
        return "reject", "正文开头与历史稿完全一致", ["opening"]
    if title_sim >= TITLE_SIM_HARD:
        return "reject", f"标题语义与历史稿高度重合(cos={title_sim:.3f})", ["title"]
    if ngram_j >= NGRAM_JACCARD_HARD:
        return "reject", f"正文与历史稿大面积重合(四字串 Jaccard={ngram_j:.3f})", ["ngram"]
    if contain_ok and ngram_contain >= NGRAM_CONTAIN_HARD:
        # 这一路专抓 Jaccard **结构上抓不到**的形状: 短稿整段照搬长稿。
        # 那种情况下 Jaccard 的真值就等于两篇的长度比, 本来就够不着硬闸线。
        return "reject", (
            f"正文有 {ngram_contain:.0%} 的四字串都出现在历史稿里 —— "
            f"短稿照搬长稿的典型形态(Jaccard={ngram_j:.3f} 看不出来, "
            f"因为它会被长度差稀释)"), ["contain"]
    weak, why, which = 0, [], []
    if title_sim >= TITLE_SIM_WARN:
        weak += 1
        why.append(f"标题接近(cos={title_sim:.3f})")
        which.append("title")
    if ngram_j >= NGRAM_JACCARD_WARN:
        weak += 1
        why.append(f"正文用词接近(Jaccard={ngram_j:.3f})")
        which.append("ngram")
    if contain_ok and ngram_contain >= NGRAM_CONTAIN_WARN:
        weak += 1
        why.append(f"正文有 {ngram_contain:.0%} 与历史稿重合")
        which.append("contain")
    if weak >= 2:
        return "reject", "；".join(why) + " —— 两项同时接近", which
    if weak == 1:
        return "warn", why[0], which
    return "pass", "", []


def verdict(title_sim: float, opening_exact: bool, ngram_j: float,
            ngram_contain: float = 0.0,
            contain_sample: int = 0) -> tuple[str, str]:
    """``deciding_signals`` 的两元组形式, 给只要结论不要归因的调用方。"""
    status, reason, _ = deciding_signals(title_sim, opening_exact, ngram_j,
                                         ngram_contain, contain_sample)
    return status, reason


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


# ── 正例多样性限额 ────────────────────────────────────────────────────────

def opening_shape(body: str, n: int = 12) -> str:
    """开头形态指纹 —— 用于给正例分桶。"""
    return sha16(normalize(opening_of(body, n)))


def cap_by_shape(items: list[dict], limit: int,
                 body_key: str = "body") -> list[dict]:
    """按开头形态两趟贪心挑 limit 条: 第一趟每种形态只收一条, 第二趟补满
    【但单一形态最多占 limit//2】。

    为什么补满那趟也要限额: 不限的话, 当某一种开头形态在候选池里占绝对多数时,
    它能占满全部 slot —— 正例池的趋同回路就等于没断, 改了白改。宁可少给几条。

    思路来自 TV sync_truth_vault_baokuan_to_autowriter_items.py:211-269 的两趟
    贪心, 但那边 min_levers 只是 advisory 不拒绝, 这里是真约束。
    """
    if limit <= 0 or not items:
        return []
    per_shape_cap = max(1, limit // 2)
    counts: dict[str, int] = {}
    first: list[dict] = []
    rest: list[dict] = []
    for it in items:
        sh = opening_shape(it.get(body_key, "") or "")
        if sh in counts:
            rest.append(it)
        else:
            counts[sh] = 0
            first.append(it)

    picked: list[dict] = []
    for it in first:
        if len(picked) >= limit:
            break
        picked.append(it)
        counts[opening_shape(it.get(body_key, "") or "")] += 1
    for it in rest:
        if len(picked) >= limit:
            break
        sh = opening_shape(it.get(body_key, "") or "")
        if counts.get(sh, 0) >= per_shape_cap:
            continue
        picked.append(it)
        counts[sh] = counts.get(sh, 0) + 1
    return picked
