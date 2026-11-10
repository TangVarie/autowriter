"""一批笔记的写法检查(2026-11-10, docs/deskcore.md §3.21): 同一批里的重复说法、开头一样的提示、正文里混进来的版式残留。

#149 之后 owner 问评论那一侧的整组检查要不要也管笔记。只搬了三样, 都在 score_drafts 存完档之后查存进去的那几篇:
  · 重复说法(``batch_repeats``, 评论规则 B 的笔记版): 同一小句里连着 ``POST_REPEAT_RUN``(8)个字一样, 出现在
    max(3, 两成)篇以上 —— 第 ``need`` 篇起要改, 项目规则里的原话不算;
  · 开头一样(``shared_openers``, 评论规则 C 的笔记版, 只提示): 第一小句的前 4 个字一样的有 max(5, 一半)篇以上;
  · 版式残留(``format_debris``): 正文里混进了评论段、【正文】这类标记、字面的 <br> 或 \\n, 或者去掉这些之后没有正文。

纯函数, 不调模型、不查库。分词和评论那一侧同一套(``commentstyle._clauses``): 汉字一个字一个、英文 / 数字连着的一段一个、
品牌叫法整个算一个 ＠; 一段重复不跨小句。量之前先去掉(换成同样长的空格, 位置不变, 引原话照样从原文里取): 话题标签
(``validator.topic_tag_matches``, 哪个 # 算话题只在那里认)、正文后面接着的评论段(从第一个评论标记起整段不量)、
【正文】这类标记、字面的 <br> 和 \\n —— 去掉的地方都算断句。
"""
from __future__ import annotations

import re

import validator

from . import commentstyle as cs

POST_REPEAT_RUN = 8            # B: 连着 8 个字(英文一个词、品牌各算一个), 同一小句里
POST_REPEAT_MIN = 3            #    出现在至少 3 篇
POST_REPEAT_SHARE = 0.20       #    且至少两成(整数百分比, cs.share_ceil): 15 篇以内是 3 篇, 16~20 篇是 4 篇
POST_OPENER_TOKENS = 4         # C2: 第一小句的前 4 个字(不够 4 个就整句)
POST_OPENER_MIN_TOKENS = 2     #     第一小句至少 2 个字才算(「嗯，」不算)
POST_OPENER_MIN = 5            #     至少 5 篇
POST_OPENER_SHARE = 0.50       #     且至少一半
POST_QUOTE_MAX = 30            # 引原话最多这么多字, 多了加「…」
POST_FIX_PHRASES = 2           # 一篇最多点名两处重复

COMMENT_BLOCKS = ("【素人评论】", "【贴主回复】", "【作者回复】")
STRAY_LABELS = ("【正文】", "【标题】", "【话题】", "【tag】")      # 【tag】 不分大小写
DEBRIS_KINDS = ("empty", "comment_block", "label", "br", "literal_newline")

_BLOCK = re.compile("|".join(map(re.escape, COMMENT_BLOCKS)))
_LABEL = re.compile(r"【(?:正文|标题|话题|tag)】", re.I)
_TOPIC_LABELS = ("【话题】", "【tag】")                              # 交付模板不加这两个: 话题行是 tags 拼的(或正文自己带的)
_BR = re.compile(r"<br\s*/?>", re.I)
# 字面的 \n(反斜杠加 n)。「C:\new」「folder\new」这种路径不算: 只在反斜杠前面是英文 / 数字 / 标点(可见的 ASCII)时才看后面
# 是不是字母。前面是汉字、emoji、全角标点、空白或者开头时一律算 —— 不然「写完了\nVlog」「#话题\nOOTD」里后面接英文的 \n
# 认不出来: 漏报不说, 没去掉的 \ 还会被当成话题里的字, 把后面整行吞进话题(只剩话题 → 误报「没有正文」)
_LITERAL_NL = re.compile(r"(?<![!-~])(?:\\r)?\\n|(?:\\r)?\\n(?![A-Za-z])")
_ANY_NL = re.compile(r"(?:\\r)?\\n")                  # 话题里夹着的: 话题里不会有反斜杠加 n, 有就是换行写坏了


def _blank(text: str, spans) -> str:
    """把这几段换成同样长的空格: 位置不变(引原话照样从原文里取), 空格在 commentstyle 里是断句。"""
    chars = list(text)
    for s, e in spans:
        for k in range(s, e):
            chars[k] = " "
    return "".join(chars)


def _scrub(text, *, cut: bool) -> tuple[str, bool]:
    """→ (量的那一份, 话题里夹了字面的 \\n 没有)。"""
    t = text if isinstance(text, str) else ""
    if cut:
        m = _BLOCK.search(t)
        if m:
            t = t[:m.start()]
    t = _blank(t, [m.span() for rx in (_BLOCK, _LABEL, _BR, _LITERAL_NL) for m in rx.finditer(t)])
    # 话题最后认: 字面的 \n 后面紧跟的 #话题, 不先去掉 \n 的话 # 前面是字母 n, 不算话题。英文话题后面紧跟字面的 \n 再接英文
    # (「#OOTD\nVlog…」)上面那条认不出(反斜杠前后都是字母, 像路径), 话题会一直吞到下一个空白: 话题在 \n 那儿收住, \n 也去掉
    spans, nl = [], False
    for m in validator.topic_tag_matches(t):
        hit = _ANY_NL.search(m.group(0))
        spans.append((m.start(), m.start() + hit.end()) if hit else m.span())
        nl = nl or hit is not None
    return _blank(t, spans), nl


def measured_text(body) -> str:
    """量重复、量开头用的正文: 从第一个评论标记(``COMMENT_BLOCKS``)起截掉, 标记、字面的 <br> / \\n、话题标签换成同样长的
    空格。返回的串是原文的前缀那么长, 位置一一对得上。"""
    return _scrub(body, cut=True)[0]


def _labels(labels, n: int) -> list[str]:
    return [str(x) for x in labels] if labels else [str(i) for i in range(n)]


def _nth(label: str) -> str:
    """调用方的 index → 跟用户说的第几篇(从 1 数)。不是数字的原样。"""
    return str(int(label) + 1) if str(label).isdigit() else str(label)


def _quote(text: str) -> str:
    return text if len(text) <= POST_QUOTE_MAX else text[:POST_QUOTE_MAX] + "…"


def _uniq(found: list[str]) -> list[str]:
    seen, out = set(), []
    for x in found:
        if x.casefold() not in seen:
            seen.add(x.casefold())
            out.append(x)
    return out


def format_debris(title, body, *, skip_labels=(), has_template: bool = False, takes_comments: bool = True,
                  takes_tags: bool = True) -> list[dict]:
    """一篇正文里混进来的版式残留 → ``[{"kind", "what"}]``, kind 按 ``DEBRIS_KINDS`` 的顺序:

      · ``empty``: 去掉话题标签、标记、评论段、<br> / \\n 之后一个字都没有(只有话题的正文也算)。有它就只回这一条: 改法
        就是把正文补进去。整个是空白的不报(存档那道闸已经拒了, W-30), 免得说两遍;
      · ``comment_block``: 正文里带了【素人评论】【贴主回复】【作者回复】那一段;
      · ``label``: 标题 / 正文里有【正文】【标题】【话题】【tag】标记。``skip_labels`` 里的不报(有交付模板时那两个标记
        delivery.problems 已经报了);
      · ``br`` / ``literal_newline``: 正文里有字面的 <br>、字面的 \\n。

    标记、<br>、\\n 只看评论段前面那部分: 评论段整段要删, 里面的不另报。``has_template``: 项目有交付模板(措辞不同),
    这时再看模板管什么: ``takes_comments`` 是模板拼不拼评论(delivery.comments 不是 null) —— 不拼的话不能说「写在 comments
    里、写作台按模板加」, 照那样挪过去评论就从交付稿里没了; ``takes_tags`` 是模板管不管话题(delivery.wants_tags) —— 不管
    的话话题行照项目规则写在正文末尾、交付稿原样带上, 【话题】【tag】不报。"""
    title = title if isinstance(title, str) else ""
    body = body if isinstance(body, str) else ""
    if not body.strip():
        return []
    measured, nl_in_tag = _scrub(body, cut=True)
    if cs.visible_len(measured) == 0:
        return [{"kind": "empty", "what": "去掉话题标签、标记和评论之后这篇没有正文: 把正文补进 body"}]
    rows: list[dict] = []
    block = _BLOCK.search(body)
    kept = body[:block.start()] if block else body
    if block:
        label = block.group(0)
        if not has_template:
            what = (f"正文里带了「{label}」那一段: body 只放正文, 评论写在这篇的 comments 里, 交付时再按项目要的样子"
                    "排在正文后面; 正文里删掉这一段")
        elif takes_comments:
            what = (f"正文里带了「{label}」那一段: 评论不放进正文, 写在这篇的 comments 里, 标签和位置由写作台按交付模板加; "
                    "正文里删掉这一段")
        else:
            what = (f"正文里带了「{label}」那一段: 这个项目的交付模板不拼评论, 评论不放进正文; 正文里删掉这一段, "
                    "要给的评论放在交付稿后面单独给用户")
        rows.append({"kind": "comment_block", "what": what})
    skip = {str(x).casefold() for x in skip_labels or ()}
    topic = {x.casefold() for x in _TOPIC_LABELS}
    for where, text in (("标题", title), ("正文", kept)):
        hit = [x for x in _uniq([m.group(0) for m in _LABEL.finditer(text)]) if x.casefold() not in skip]
        if not has_template:
            if hit:
                rows.append({"kind": "label", "what": f"{where}里有「{'」「'.join(hit)}」标记: body 只放正文, 标记删掉"
                                                      "(交付时再按项目要的样子加)"})
            continue
        marks = [x for x in hit if x.casefold() not in topic]
        tags = [x for x in hit if x.casefold() in topic] if takes_tags else []
        if marks:
            rows.append({"kind": "label", "what": f"{where}里有「{'」「'.join(marks)}」标记: 标记由写作台按交付模板加, 稿子里删掉"})
        if tags:
            rows.append({"kind": "label", "what": f"{where}里有「{'」「'.join(tags)}」标记: 话题写在这篇的 tags 里, 交付稿里的"
                                                  "话题行由写作台按交付模板加; 稿子里删掉这个标记"})
    if _BR.search(kept):
        rows.append({"kind": "br", "what": "正文里有字面的「<br>」(多半是从交付表格里复制回来的): 换成真的换行"})
    if _LITERAL_NL.search(kept) or nl_in_tag:
        rows.append({"kind": "literal_newline", "what": "正文里有字面的「\\n」(反斜杠加 n, 不是换行): 换成真的换行"})
    return rows


def _probe(gram: tuple) -> str:
    return "\x1f" + "\x1f".join(gram) + "\x1f"


def batch_repeats(texts: list[str], aliases, labels: list[str] | None = None, *, exempt=(), prefer=(),
                  run: int = POST_REPEAT_RUN, min_drafts: int = POST_REPEAT_MIN, share=POST_REPEAT_SHARE) -> dict:
    """同一批里重复的说法(评论规则 B 的笔记版)。纯函数。

    同一小句里连着 ``run`` 个字一样、出现在 ``need = max(min_drafts, ceil(share·n))`` 篇以上的算一个说法(一篇里出现几次
    都只数一篇)。按给的顺序, 前 ``need - 1`` 篇用了不算, 第 ``need`` 篇起要改(改掉它们就不再超): 每篇最多点名
    ``POST_FIX_PHRASES`` 处, 引原话(品牌照原样)。``prefer``: 这几篇(位置)先算进「不算」的那 ``need - 1`` 篇 —— 调用方
    传这次改过重存的那几篇: 改一轮就够, 要改的落到没改过的那几篇上, 不然新稿照原样交、改过的那篇只能照实说一句。
    ``exempt``: 项目规则的原文(和必须带的话术), 规则里连着出现的说法不算(要求照原样写的话术、售后和规格的说法, 改了反而
    可能违规); 规则原文不截评论段(规则常常在讲交付的样子)。

    一篇里点名的几处按出处分开并: 挨着的两段要是前面用过的不是同几篇, 各说各的, 不并成一句只报一处出处。

    回 ``{"n", "need", "allowed", "grams": 算重复的说法个数, "exempted": 因为规则原文不算的个数,
    "carriers": [用了的那几篇, 位置], "fix": {位置: [改法, ...]}, "flagged": [要改的那几篇, 位置]}``。位置是 texts 里的
    下标; 改法里说的是 ``labels``(调用方的 index)。"""
    n = len(texts)
    labels = _labels(labels, n)
    need = max(min_drafts, cs.share_ceil(n, share))
    allowed = need - 1
    out = {"n": n, "need": need, "allowed": allowed, "grams": 0, "exempted": 0, "carriers": [], "fix": {},
           "flagged": []}
    if n < need:
        return out
    rx = cs._alias_re(aliases)
    where: dict[tuple, list[tuple[int, int, int]]] = {}
    for i, t in enumerate(texts):
        for g, s, e in cs._grams(cs._clauses(measured_text(t), rx), run):
            occ = where.setdefault(g, [])
            if not occ or occ[-1][0] != i:
                occ.append((i, s, e))
    trip = {g: occ for g, occ in where.items() if len(occ) >= need}
    if trip and exempt:
        refs = [cs._flat(cs._clauses(_scrub(r, cut=False)[0], rx)) for r in exempt if isinstance(r, str) and r.strip()]
        kept = {g: occ for g, occ in trip.items() if not any(_probe(g) in f for f in refs)}
        out["exempted"] = len(trip) - len(kept)
        trip = kept
    keep = set(prefer or ())
    spans: dict[int, dict[str, list[tuple[int, int, str]]]] = {}
    for occ in trip.values():
        occ = sorted(occ, key=lambda o: (o[0] not in keep, o[0]))
        first = "、".join(labels[i] for i in sorted(i for i, _, _ in occ[:allowed]))
        for i, s, e in occ[allowed:]:
            spans.setdefault(i, {}).setdefault(first, []).append((s, e, first))
    fix: dict[int, list[str]] = {}
    for i in sorted(spans):
        merged = sorted(m for grp in spans[i].values() for m in cs._merge(grp))
        fix[i] = [f"「{_quote(texts[i][s:e])}」index 是 {first} 的 {allowed} 篇已经用过, 同一批 {n} 篇里同一个说法最多 "
                  f"{allowed} 篇用: 这篇换个说法或换件事说(项目规则要求照原样写的话术不用改)"
                  for s, e, first in merged[:POST_FIX_PHRASES]]
    out.update(grams=len(trip), carriers=sorted({i for occ in trip.values() for i, _, _ in occ}), fix=fix,
               flagged=sorted(fix))
    return out


def shared_openers(texts: list[str], aliases, labels: list[str] | None = None, *, tokens: int = POST_OPENER_TOKENS,
                   min_tokens: int = POST_OPENER_MIN_TOKENS, min_drafts: int = POST_OPENER_MIN,
                   share=POST_OPENER_SHARE) -> dict:
    """同一批里开头一样的(评论规则 C 的笔记版, 只提示、不算要改的地方)。纯函数。

    开头 = 第一小句的前 ``tokens`` 个字(不够就整句; 第一小句不到 ``min_tokens`` 个字的那篇不算)。开头一样的那一组最大的
    有 ``need = max(min_drafts, ceil(share·n))`` 篇以上时回一句提示, 引第一篇的原话。
    提示是要跟用户提的那一句: 不写 index, 篇数从 1 数(``labels`` 是调用方的 index, 第 index+1 篇)。
    回 ``{"n", "need", "top": 最大那一组几篇, "indices": [那一组, 位置; 没提示时是空的], "hint": 一句或 None}``。"""
    n = len(texts)
    labels = _labels(labels, n)
    need = max(min_drafts, cs.share_ceil(n, share))
    rx = cs._alias_re(aliases)
    groups: dict[tuple, list[tuple[int, str]]] = {}
    for i, t in enumerate(texts):
        cl = cs._clauses(measured_text(t), rx)
        if cl and len(cl[0]) >= min_tokens:
            head = cl[0][:tokens]
            groups.setdefault(tuple(tok for tok, _, _ in head), []).append((i, t[head[0][1]:head[-1][2]]))
    best = max(groups.values(), key=len, default=[])
    out = {"n": n, "need": need, "top": len(best), "indices": [], "hint": None}
    if best and len(best) >= need:
        out["indices"] = [i for i, _ in best]
        out["hint"] = (f"这批 {n} 篇里, 第 {'、'.join(_nth(labels[i]) for i, _ in best)} 篇开头都是「{_quote(best[0][1])}」: "
                       "只是提示, 不算要改的地方; 想让这批读起来不那么像一个模子, 挑几篇换个开头")
    return out
