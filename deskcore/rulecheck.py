"""deskcore/rulecheck.py — 能用代码查的规则, 用代码查(2026-10-20)。

10-20 生产诊断: 规则越堆越多, 全部写成文字一股脑注入简报, 模型守不守全凭注意力。其中有一类根本不用模型判:
禁用词、某个词能不能出现在哪、必须带的话术、字数范围。这一类在 ``memories`` 上本来就有位置 ——
``rule_kind`` / ``rule_payload``(000_baseline.sql 的 CHECK 只认 forbidden_word / required_phrase / max_len /
forbidden_regex / free_text), 以前只有 AW 生成链路(generation_service → validator.check_hard_rules, 只看 hard)在读,
生产库 292 条规则一条也没填。⚠️ validator 没退役, 同一批行它也读: 写进来的形状要让它要么读对、要么读不出(见
validator._spec_from_desk: 标题正文照这里的口径查, 只管话题 / 评论的跳过, 都不许退回从 content 里抽词), 不能让它把
一条只管评论的规则套到标题正文上。

这里接上: ``record_rule`` 带 ``check`` 时校验后存进那两列; ``score_drafts`` 存完档, 拿和简报**同一个池子**
(``store.shared_memories``: confirmed、没静音、项目 + 调用者自己的 global)里带 check 的那几条, soft 的再过简报那道
方向闸(core.filter_by_applicability, 和 my_rules 的「方向不符」同一个判据), 逐篇查。简报的相关性筛选和每层 12 条
的封顶不做: 那两道是给注入文字限量的, 记了 check 的规则每次都查。不调模型、不出网, 结果只是参考(必须改 / 建议改),
不拦存档。retire / mute 之后 shared_memories 就取不到它, 代码检查随之停 —— 判据只有一处。

payload 形状(写入时 ``normalize_check`` 校验; 读出来时 ``spec_of`` 再校一遍, 坏的跳过并记日志):
  forbidden_word   {"words": [...], "where": [...]}       where 默认 ["title", "body"]
  forbidden_regex  {"pattern": "...", "where": [...]}   量词只能套在单个字符或定长、不含分支的组上(见 _assert_regex_shape);
                   能匹配同样字符的量词不能挨着(见 validator.assert_regex_is_safe)
  required_phrase  {"any": [...], "where": [...]}          一个都没有才算没守
  max_len          {"max": N, "min": M, "where": "body"}   max / min 至少给一个; 字数去掉空白和 #话题 再数
validator.py 的老形状({"target": X} / {"scope": "标题", "n": N} / {"pattern": P})读的时候也认。
"""

from __future__ import annotations

import json
import logging
import re

import validator

from . import delivery, tvlink

logger = logging.getLogger("deskcore.rulecheck")

CHECK_KINDS = ("forbidden_word", "forbidden_regex", "required_phrase", "max_len")
PARTS = ("title", "body", "tags", "comments")
DEFAULT_WHERE = ("title", "body")
PART_CN = {"title": "标题", "body": "正文", "tags": "话题标签", "comments": "评论"}
SEVERITY_LABEL = {"hard": "必须改", "soft": "建议改"}
MAX_TERMS = 50
MAX_TERM_CHARS = 40
MAX_HITS = 40                 # 一批 20 篇 × 几条规则, 建议改最多补到这么多(必须改全列); 返回值太长会把协议挤出模型的注意力
_LEGACY_LEN_SCOPE = {"标题": "title", "正文": "body"}


class CheckError(ValueError):
    """``record_rule`` 的 check 写错了。消息给模型看: 说清哪一项、该怎么改。"""


# ══════════════════════════════════════════════════════════════════════
# 校验
# ══════════════════════════════════════════════════════════════════════

def _terms(raw, key: str) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise CheckError(f"{key} 应该是非空的字符串列表")
    out: list[str] = []
    for t in raw:
        if not isinstance(t, str) or not t.strip():
            raise CheckError(f"{key} 里每一项都要是非空字符串")
        t = t.strip()
        if len(t) > MAX_TERM_CHARS:
            raise CheckError(f"{key} 里「{t[:20]}…」太长(上限 {MAX_TERM_CHARS} 字): 这里放词, 不放句子")
        if t.casefold() not in {x.casefold() for x in out}:
            out.append(t)
    if len(out) > MAX_TERMS:
        raise CheckError(f"{key} 最多 {MAX_TERMS} 个")
    return out


def _where_list(raw) -> list[str]:
    if raw is None:
        return list(DEFAULT_WHERE)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw or any(w not in PARTS for w in raw):
        raise CheckError(f"where 只认 {' / '.join(PARTS)} 组成的列表")
    return [w for w in PARTS if w in raw]


_SINGLE_CHAR_OPS = frozenset({"LITERAL", "NOT_LITERAL", "IN", "ANY", "CATEGORY", "RANGE", "RANGE_UNI_IGNORE"})
_ZERO_WIDTH_OPS = frozenset({"AT"})


def _fixed_simple(seq) -> bool:
    """这一段每次只能用一种方式匹配一段定长的文本: 只有单个字符、锚点, 和由它们组成的组; 没有分支、量词、回引。"""
    for op, av in seq or ():
        name = getattr(op, "name", str(op))
        if name in _SINGLE_CHAR_OPS or name in _ZERO_WIDTH_OPS:
            continue
        if name == "SUBPATTERN":
            if not _fixed_simple(av[-1]):
                return False
            continue
        return False
    return True


def _assert_regex_shape(pattern: str) -> None:
    """validator.assert_regex_is_safe 之外再收一道: 量词(上限 > 1)只能套在单个字符或定长、不含分支的组上。

    validator 只认得嵌套的无上界量词。``(哈|哈哈)+$`` 这种分支能匹配同一段文本的写法它放行, 而 30 个「哈」后面跟一个
    句号就要回溯近半秒、每多一个字乘 1.6 —— re 匹配时握着 GIL, 共享的服务进程整个停住, 而这条规则每次存档都跑。
    Python 的 re 没有超时, 所以只能在写入(和读出)时就不收这种形状。写词表的规则用不到量词套分支, 真要用就拆成几条。"""
    tree = validator._parse_pattern(pattern)
    if tree is None:
        raise CheckError("pattern 这里没法做安全分析(re 的解析器拿不到), 先改用 forbidden_word")

    def walk(seq) -> bool:
        for op, av in seq or ():
            name = getattr(op, "name", str(op))
            if name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
                _lo, hi, sub = av
                if hi > 1 and not _fixed_simple(sub):
                    return False
                if not walk(sub):
                    return False
            elif name in ("SUBPATTERN", "ATOMIC_GROUP"):
                if not walk(av[-1] if isinstance(av, tuple) else av):
                    return False
            elif name == "BRANCH":
                if not all(walk(alt) for alt in (av[1] or ())):
                    return False
            elif name in ("ASSERT", "ASSERT_NOT"):
                if not walk(av[1]):
                    return False
            elif name == "GROUPREF_EXISTS":
                return False
        return True

    if not walk(tree):
        raise CheckError("pattern 里有量词(+ * {n,m})套在带分支或可变长度的组上, 如 (哈|哈哈)+ / (ab?)+, 会引发"
                         "灾难性回溯。量词只套单个字符或定长的组(哈{5,} / [!！]{3,} / (?:哈哈)+), 分支别加量词")


def _validate(kind: str, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise CheckError("check 应该是 JSON 对象")
    allowed = {"forbidden_word": ("words", "where"), "forbidden_regex": ("pattern", "where"),
               "required_phrase": ("any", "where"), "max_len": ("max", "min", "where")}[kind]
    bad = [k for k in payload if k not in allowed]
    if bad:
        raise CheckError(f"{kind} 的 check 里有不认识的键 {bad}; 只认 {' / '.join(allowed)}")
    if kind == "forbidden_word":
        return {"words": _terms(payload.get("words"), "words"), "where": _where_list(payload.get("where"))}
    if kind == "required_phrase":
        return {"any": _terms(payload.get("any"), "any"), "where": _where_list(payload.get("where"))}
    if kind == "forbidden_regex":
        pattern = payload.get("pattern")
        try:
            validator.assert_regex_is_safe(pattern)
        except validator.UnsafeRegex as exc:
            raise CheckError(f"pattern 不能用: {exc}") from exc
        _assert_regex_shape(pattern)
        return {"pattern": pattern, "where": _where_list(payload.get("where"))}
    # max_len
    lo, hi = payload.get("min"), payload.get("max")
    for name, v in (("min", lo), ("max", hi)):
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v <= 0):
            raise CheckError(f"max_len 的 {name} 要是正整数")
    if lo is None and hi is None:
        raise CheckError("max_len 至少给 max 或 min 其中一个")
    if lo is not None and hi is not None and lo > hi:
        raise CheckError("max_len 的 min 不能比 max 大")
    where = payload.get("where", "body")
    if where not in ("title", "body"):
        raise CheckError("max_len 的 where 只认 title 或 body")
    out = {"where": where}
    if hi is not None:
        out["max"] = hi
    if lo is not None:
        out["min"] = lo
    return out


def normalize_check(check) -> tuple[str, dict | None]:
    """``record_rule`` 的 ``check`` 参数 → (rule_kind, rule_payload)。写错就抛 CheckError。

    ``{"kind": "free_text"}`` 是"这条不用代码查": 记过 check 又发现记错了, 用它撤掉。
    """
    if not isinstance(check, dict):
        raise CheckError("check 应该是 JSON 对象, 形如 {\"kind\": \"forbidden_word\", \"words\": [...]}")
    kind = check.get("kind")
    payload = {k: v for k, v in check.items() if k != "kind"}
    if kind == "free_text":
        if payload:
            raise CheckError("kind=free_text 是撤掉代码检查, 不带别的键")
        return "free_text", None
    if kind not in CHECK_KINDS:
        raise CheckError(f"check.kind 只认 {' / '.join(CHECK_KINDS)}(撤掉检查用 free_text), 现在是 {kind!r}")
    return kind, _validate(kind, payload)


def _from_legacy(kind: str, payload: dict) -> dict:
    """validator.py(Day 3)存的老形状 → 现在的形状。新形状原样返回。"""
    if kind == "forbidden_word" and "words" not in payload and "target" in payload:
        return {"words": [payload["target"]]}
    if kind == "required_phrase" and "any" not in payload and "target" in payload:
        return {"any": [payload["target"]]}
    if kind == "max_len" and "n" in payload and "max" not in payload:
        where = _LEGACY_LEN_SCOPE.get(str(payload.get("scope") or "标题").strip())
        if where is None:
            raise CheckError(f"老形状的 scope={payload.get('scope')!r} 这里不查(只查标题 / 正文)")
        return {"max": payload["n"], "where": where}
    return payload


def spec_of(rule: dict) -> dict | None:
    """库里一条规则 → 可执行的检查; 没有 check 或 check 坏了返回 None(坏的记一行日志, 不影响别的规则)。"""
    kind = (rule.get("rule_kind") or "").strip()
    if kind not in CHECK_KINDS:
        return None
    payload = rule.get("rule_payload")
    try:
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise CheckError("rule_payload 为空或不是对象")
        return {"kind": kind, **_validate(kind, _from_legacy(kind, payload))}
    except (CheckError, ValueError, TypeError) as exc:
        logger.warning("rulecheck: 规则 %s 的 check 用不了, 跳过(%s)", rule.get("id"), exc)
        return None


# ══════════════════════════════════════════════════════════════════════
# 查
# ══════════════════════════════════════════════════════════════════════

# 查词、数字数和 validator 共用一份(codex review on aw#115): 同一行规则 AW 生成链路也读, 两边口径分开写迟早不一样。
# 不分大小写的包含, 纯英文数字的词按词边界认, 免得 "ad" 撞进 "made"(同 isolation.foreign_markers); 字数去掉空白和 #话题。
_has = validator.contains_term
_count = validator.count_chars


def _parts(d: dict, tags: list[str] | None = None) -> dict[str, list[str]]:
    """一篇稿 → 各部位的文本。tags 是写手传的 tags 加正文里的 #话题; comments 一条一项。

    ``tags`` 给了就用它(项目有交付模板且模板管话题时, core 传交付出去的那一份: 含模板加的固定 / 条件话题; 那时 ``d`` 的
    body 也是交付出去的、摘掉了末尾那行话题的正文, 见 delivery.delivered_post)。"""
    body = str(d.get("body") or "")
    if tags is None:
        # 写手传的 tags 和正文末尾那行 #话题 常常是同一批(约六分之一的稿子末尾带一行): 去重, 不然「话题不超过 N 个」
        # 这类按个数数的检查会把同一个话题数两遍
        raw = delivery.split_tags(d.get("tags")) + delivery.split_tags(" ".join(tvlink._TAG_RE.findall(body)))
        seen: set[str] = set()
        tags = [t for t in raw if not (delivery.norm_tag(t) in seen or seen.add(delivery.norm_tag(t)))]
    comments = []
    for c in d.get("comments") if isinstance(d.get("comments"), list) else []:
        text = c.get("text") if isinstance(c, dict) else c
        if isinstance(text, str) and text.strip():
            comments.append(text.strip())
    return {"title": [str(d.get("title") or "")], "body": [body], "tags": [" ".join(tags)] if tags else [],
            "comments": comments}


def _run(spec: dict, parts: dict[str, list[str]]) -> list[tuple[str, str]]:
    """一条检查 × 一篇稿 → [(部位, 说明)]。"""
    kind, out = spec["kind"], []
    if kind == "forbidden_word":
        for w in spec["where"]:
            found = [t for t in spec["words"] if any(_has(x, t) for x in parts[w])]
            if found:
                out.append((w, f"{PART_CN[w]}里有禁用词「{'」「'.join(found)}」"))
    elif kind == "forbidden_regex":
        for w in spec["where"]:
            for x in parts[w]:
                try:
                    m = re.search(spec["pattern"], x[:validator.MAX_REGEX_TARGET_CHARS])
                except re.error:
                    return []
                if m:
                    out.append((w, f"{PART_CN[w]}里有不该出现的写法「{m.group(0)[:30]}」"))
                    break
    elif kind == "required_phrase":
        texts = [x for w in spec["where"] for x in parts[w]]
        if any(x.strip() for x in texts) and not any(_has(x, t) for x in texts for t in spec["any"]):
            where = "/".join(spec["where"])
            out.append((where, f"{'、'.join(PART_CN[w] for w in spec['where'])}里没有「{'」「'.join(spec['any'])}」"
                               "中的任何一个"))
    else:  # max_len
        w = spec["where"]
        n = _count(parts[w][0])
        if "max" in spec and n > spec["max"]:
            out.append((w, f"{PART_CN[w]} {n} 字(不算空白和话题), 上限 {spec['max']}"))
        if "min" in spec and n < spec["min"]:
            out.append((w, f"{PART_CN[w]} {n} 字(不算空白和话题), 下限 {spec['min']}"))
    return out


def check(rules: list[dict], drafts: list[dict], *, indices: list[int] | None = None,
          tags: list[list[str] | None] | None = None) -> tuple[list[dict], dict]:
    """(命中, 统计)。命中按篇、再按 hard 在前排好; 统计 = {"rules": 能查的条数, "skipped": check 坏了跳过的条数}。

    ``rules`` 是 store.shared_memories 的 hard + soft(没 check 的自动略过, soft 的方向闸由调用方先过);
    ``indices`` 同 delivery.assemble; ``tags`` 和 drafts 一一对应, 某篇给了就用它当这篇的话题(见 _parts)。
    """
    specs, skipped = [], 0
    for r in rules or []:
        spec = spec_of(r)
        if spec is not None:
            specs.append((r, spec))
        elif (r.get("rule_kind") or "") in CHECK_KINDS:
            skipped += 1
    hits: list[dict] = []
    idx = list(indices) if indices is not None else list(range(len(drafts)))
    for pos, d in enumerate(drafts):
        parts = _parts(d if isinstance(d, dict) else {}, tags[pos] if tags is not None else None)
        for r, spec in specs:
            sev = "hard" if (r.get("severity") or "soft").lower() == "hard" else "soft"
            for where, what in _run(spec, parts):
                hits.append({"index": idx[pos], "rule_id": r.get("id"), "severity": sev,
                             "where": where, "what": f"{SEVERITY_LABEL[sev]}: {what}"})
    hits.sort(key=lambda h: (h["index"], h["severity"] != "hard"))
    return hits, {"rules": len(specs), "skipped": skipped}


def top_hits(hits: list[dict], limit: int = MAX_HITS) -> tuple[list[dict], int]:
    """回包里列哪些: 必须改(hard)全列, 建议改按篇号补到一共 ``limit`` 条为止; 列出来的再按篇、hard 在前排回去。
    第二个返回值是没列出的条数, 只会是建议改。

    直接按篇号截的话, 后面几篇的必须改会被前面几篇的建议改挤掉 —— 写手看不见, 就当那几篇没问题交出去了。必须改本身
    也不截(codex review on aw#115): 一批 20 篇 × 几条硬规则就能过 40 条, 截掉的那些只剩一个个数, 写手不知道是哪篇哪条,
    同样会当没问题交出去。条数多到这个份上, 整批本来就该返工, 回包长一点是对的。"""
    hard = [h for h in hits if h["severity"] == "hard"]
    soft = sorted((h for h in hits if h["severity"] != "hard"), key=lambda h: h["index"])
    keep = hard + soft[:max(0, limit - len(hard))]
    keep.sort(key=lambda h: (h["index"], h["severity"] != "hard"))
    return keep, len(hits) - len(keep)
