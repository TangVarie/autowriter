"""
硬约束（severity=hard）生成后自动校验
─────────────────────────────────────────────────────────────────────────

之前 P0 硬规则只在 system_prompt 里以"必须 100% 满足"的措辞要求模型，
但没有任何确定性兜底——模型偶尔失忆就会漏掉。本模块从 P0 规则文本里
抽取 deterministic 检查（regex / 字符串包含 / 字符数限制），生成后
对每个版本逐条比对。

这是 generator._apply_compliance_recheck 的轻量级兄弟：
  - LLM 复检：覆盖面广（语义层判断），但每批多花一次 Claude 调用 + token
  - 本模块：覆盖面窄（只抓出能模式化的规则），但是 0 成本、确定性

设计上两者互补：本模块抓得到的违规直接触发重生 / needs_revision，
抓不到的（如"语气太正式"）继续靠 LLM 复检兜底。

支持的规则模式（按优先级匹配）：
  1. **结构化字段**（2026-05 Day 3）：``rule_kind`` + ``rule_payload`` 直接
     给定可执行的 spec，比正则抽取更稳定。支持的 kind：
       - ``forbidden_word`` ：``{"target": "X"}``
       - ``required_phrase``：``{"target": "X"}``
       - ``max_len``        ：``{"scope": "标题"|"正文"|"开头", "n": N}``
       - ``forbidden_regex``：``{"pattern": "..."}`` ─ re 的写法，
         可用内联标志 ``(?i)`` 不区分大小写；先过安全闸, 再限时执行(search_user_regex)
     不传 ``rule_kind`` 或传 ``"free_text"`` 时退回到下面的正则抽取; ``free_text`` 带 ``{"code_check": "off"}``
       (写作台撤掉了代码检查)时跳过、不抽。
  2. **禁用词 / 片段**："禁止 X" / "不要 X" / "不得 X" / "别用 X" /
     "避免 X" → 检查 title+body 是否含 X
  3. **字符上限**："标题不超过 N 字" / "标题最多 N 字" → 检查长度
  4. **必须包含**："必须包含 X" / "必须出现 X" → 检查 title+body 含 X
  5. 其它没法机械化的规则被跳过（不视为违规，留给 LLM 复检）

调用方式：
::

    hits = validator.check_hard_rules(
        hard_rules=[{"content": "禁止出现'最'字"}, ...],
        title="...", body="...",
    )
    # hits: [{"rule": "...", "kind": "forbidden_word", "match": "最"}]

返回为空列表说明全部通过；非空则按 hit 一一对应。
"""

from __future__ import annotations

import functools
import logging
import re
import threading
import time
from typing import Optional


# ── 用户正则的安全闸（跨库审计 2026-08-24 SUP-011）────────────────────────
#
# ``forbidden_regex`` 的 pattern 是**用户写的**, 直接喂 ``re.search`` 就等于
# 把 CPU 交给用户。实测 ``(a+)+$`` 配 26 个字符的正文要 6 秒, 每多 2 个字符
# 翻一倍 —— 正文上百字就是永不返回。
#
# ⚠️ **靠截断正文兜不住**: 回溯是输入长度的**指数**, 截到 100 字仍然是 2^100。
#    Python 的 ``re`` 又**没有超时**, 而且它在 C 里跑, 线程超时也打不断。
#
# 两层(codex review on aw#115 第四轮之后改成这样):
#   1. **写入时拒绝危险形状**(下面这道静态闸)。它是**尽力而为的过滤**, 不是判定: 四轮 review 每轮都找到新的漏网形状。
#      闸的放行 / 拒绝不再改 —— Jev 的 /compile_rules 一字不差地镜像它, 改了两边就对不上。
#   2. **执行期限时**(``search_user_regex``, 见 MAX_REGEX_TARGET_CHARS 后面那一节): 用第三方 ``regex`` 包的 search(timeout=…)。
#      漏过闸的正则最多花一份时限(config.REGEX_SEARCH_TIMEOUT_SEC), 报成"这次没查", 不算命中、也不算守住了。

_MAXREPEAT = getattr(re, "MAXREPEAT", 4294967295)


def _parse_pattern(pattern: str):
    """拿到 ``re`` 的解析树。私有 API, 拿不到就返回 None。

    3.11 起叫 ``re._parser``, 之前是顶层 ``sre_parse``。两个都试, 都没有就
    诚实地返回 None —— 让调用方走保守分支, 而不是假装检查过了。
    """
    for mod_name, attr in (("re._parser", "parse"), ("sre_parse", "parse")):
        try:
            mod = __import__(mod_name, fromlist=[attr])
            return getattr(mod, attr)(pattern)
        except Exception:      # noqa: BLE001 — 解析不了/API 变了都算"拿不到"
            continue
    return None


def _has_conditional(seq) -> bool:
    """树里有没有条件组 (?(1)是|否)(codex review on aw#115)。它解析出来是 (组号, 是, 否), 下面两道闸都不往里走, 里面放什么都
    看不见: ()(?(1)(a+)+b) 22 个 a 0.4 秒、每多 2 个翻一倍。写作台(rulecheck._assert_regex_shape)本来就不收; memory.py
    存规则、check_hard_rules 执行期只靠这一道, 也不收 —— 写禁用词的规则用不到它。"""
    for op, av in seq or ():
        name = getattr(op, "name", str(op))
        if name == "GROUPREF_EXISTS":
            return True
        if name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT", "SUBPATTERN", "ASSERT", "ASSERT_NOT"):
            subs = [av[-1]]
        elif name == "ATOMIC_GROUP":
            subs = [av]
        elif name == "BRANCH":
            subs = list(av[1] or ())
        else:
            continue
        if any(_has_conditional(sub) for sub in subs):
            return True
    return False


def _has_nested_unbounded_repeat(node) -> bool:
    """树里有没有"无上界重复套无上界重复" —— 灾难性回溯的经典形状。

    ``(a+)+`` / ``(a*)*`` / ``(a+)*`` / ``(.*)*`` 都是这一类: 外层每多匹配
    一次, 内层的切分方式就翻一倍。
    """
    def _walk(seq, inside_unbounded: bool) -> bool:
        for op, av in seq or ():
            name = getattr(op, "name", str(op))
            if name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
                _lo, hi, sub = av
                unbounded = hi >= _MAXREPEAT
                if unbounded and inside_unbounded:
                    return True
                if _walk(sub, inside_unbounded or unbounded):
                    return True
            elif name in ("SUBPATTERN", "ATOMIC_GROUP"):
                sub = av[-1] if isinstance(av, tuple) else av
                if _walk(sub, inside_unbounded):
                    return True
            elif name == "BRANCH":
                for alt in (av[1] or ()):
                    if _walk(alt, inside_unbounded):
                        return True
            elif name in ("ASSERT", "ASSERT_NOT"):
                if _walk(av[1], inside_unbounded):
                    return True
        return False

    return _walk(node, False)


# ── 挨在一起、能匹配同样字符的量词(codex review on aw#115) ──────────────────────────────
# 不嵌套也能回溯到卡死: a*a*a*a*a*a*a*a*b 配 30 个 a 要 1 秒, 200 个 a 超过 3 秒; 两个就够: .*.*x 配 2000 字的
# 一行 1.5 秒。k 个这样的量词挨着, 后面的东西匹配失败时要把那一段在它们之间的 n^k 种切法全试一遍。
# 判法: 顺着每一层往下走, 记着"还开着"的量词(可变长、能回吐的: * + ? {m,n} 且 m≠n; 占有量词不回吐, 不算 —— 但也不是隔断, 见下)。
# 后面来一个量词, 字集和某个开着的有交集, 就算接在它后面, 切法数相乘(无上界按 _OPEN_SPAN 算); 乘过
# _AMBIGUITY_BUDGET 就拒。隔断: 一个必须吃掉的字、开着的那个量词吃不了(\d+\.\d+ 的 \.); 或者连着两个以上的
# 固定字(品牌[\s\S]*品牌: 多字的词在正文里出现的次数少, 切法数被它的出现次数封住 —— Jev 的规则编译器就教模型
# 这么写「最多出现 N 次」, 写作台得收)。分支和组都往里走, 分支后面开着的是各条分支开着的并集。
# 这不是完备的代价分析(几个各自出现很多次的多字词夹着 .* 仍然能慢), 挡的是实际会写出来、又必然慢的那一类。
# 再补三条(codex review on aw#115):
# · 整条正则最后一个量词(后面什么都没有, 连 $ \b 都没有)只当 {下限}: 吃够下限就匹配成功, 不会再回头切。\d+\s*\d+、
#   原价.*\d+、.*[!！]{2,} 放行; \w*\w*\d+ 照拒 —— 结尾的 \d+ 也得吃够一个(3000 个 a 84 秒)。量词底下那一段还是照查。
# · 「X 最多 N 次」展开写: 单个字 X 后面一直到整条正则结尾都是 X 和同一个量词 R 一个隔一个(R 下限 ≤ 1、无上界、底下一个字、
#   X 是 R 的一部分)、并且以 X 结尾时, X 也算隔断([!！][\s\S]*[!！][\s\S]*[!！]、\n\s*\n\s*\n)。这种只会因为 X 不够数而失败,
#   回溯被 X 的个数封住 —— 但不是常数: 正好 N 个 X 的稿子(守住了)要试 2^(N-1) 遍, 实测 2 万字 N=10 0.36 秒、N=14 5.8 秒,
#   所以最多认 _MAX_COUNT_CHAIN 份。结尾接了别的(哪怕一个 $)就不是只数个数了: !.*!.*!$ 1000 个「!」加一个字 1.4 秒。
# · 外层量词的每一次之间也是挨着的: 定次 {n} 展开 n 份(最多 _UNROLL 份; 2^14 已经过预算, 再多也查不出新的); 次数可变、
#   底下又有可变长量词的, 也展开 min(上限, _UNROLL) 份自己查 —— (?:.{0,20}){5}x 配 120 字 4.8 秒、(?:\w{1,5})+x 配 26 字
#   3.2 秒, 两道老闸都放行。展开套几层就是几次方, 闸自己走的步数也封顶(_WALK_STEPS), 超了按太复杂拒。
# · 前瞻 (?=…) (?!…)、占有量词、原子组 (?>…) 都不是隔断(codex review on aw#115)。前瞻不吃字, 可前面开着的量词每换一种切法
#   它就在那个位置重查一遍, 里面的量词和前面的照样挨着(\d+(?=\d*元) 2000 位 4~6 秒); 查完的结果丢掉, 后面接的还是前瞻之前
#   开着的那些。正向前瞻是整条最后一项时它成了整条就成了, 里面结尾的量词照样只当下限; 负向的里面成了反倒是失败、要回头, 不算
#   (\w*(?!\w*) 2000 字 6 秒)。后顾只能定长, 可它里面还能套前瞻(\d+(?<=\d(?=\d*x)) 2000 位 4 秒), 所以也接着开着的查、查完
#   丢掉。占有量词 / 原子组不回吐, 自己不算开着的; 可能吃空的话前后两个照样
#   挨着(.*a*+.*x 2000 字 1.7 秒、\d+(?>x?)\d+y 2000 位 4~5 秒), 吃的是前面开着的那个也能吃的字, 前面每换一种切法它就从头吃一遍
#   (.*a++.*x 2000 个 a 1 秒) —— 所以照普通量词算切法数、照普通量词断开它吃不了的那些开着的, 只是不加进开着的里面。
# · 次数可变的组 (?:…)? / (?:…){m,n} 不是一整块(codex review on aw#115)。原来组里那一段只自己查, 整组的字集当「什么字都能吃」:
#   组里的量词和组前面开着的从来不比, 组里结尾还开着的也不往后传 —— \d+(?:[,，]?\d+)?元 2000 位 19 秒、(?:\d+\s*)?\d+元 5 秒、
#   一段没有「！」的中文配 \S+(?:\s*\S+)?！ 20 秒以上, 前面几条(前瞻、占有量词)套一层可选组也就绕过去了。现在组里有可变长量词的,
#   底下那一段(展开 min(下限, _UNROLL) 份, 至少一份)接着前面开着的查, 组里新开的、走完还开着的接在整组后面往后传。整组的字集照实
#   算(组、分支、量词底下都往里看; 前瞻后顾不吃字, 不算), 不然 \d+(?:\.\d+)?(?=\s*元) 这种快的也拒。下限 ≥ 1 的组能吃空的话
#   ((?:a?b?){1,3})也不断开前面开着的: 吃空也算一次, 组前后的两个照样挨着(\d*(?:a?b?){1,3}\d*x 2000 位 10 秒)。
_OPEN_SPAN = 1000
_AMBIGUITY_BUDGET = 10_000
_UNROLL = 16
_MAX_COUNT_CHAIN = 11
_WALK_STEPS = 20_000
# 探针: ASCII 和 Latin-1 补充全部(× ÷ 夹在 À-Þ / à-þ 中间, 只看两头会漏: \W*[À-Þ]*x 配 1000 个 × 0.6 秒) + 几个常见字和大小写
# 怪字。别的非 ASCII 范围只探两头和这里有的字 —— 公共字只在范围中间的照样看不见, 已知不完备(codex review on aw#115)。
_PROBE_CHARS = ("".join(chr(c) for c in range(256))
                + "\u3000\u2028٣０Ａａ中一哈的ıİſ\u212aẞ。，！？、：；“”（）【】\U0001F600")
_CATEGORY_SRC = {"CATEGORY_DIGIT": r"\d", "CATEGORY_NOT_DIGIT": r"\D", "CATEGORY_SPACE": r"\s",
                 "CATEGORY_NOT_SPACE": r"\S", "CATEGORY_WORD": r"\w", "CATEGORY_NOT_WORD": r"\W"}


class _Chars:
    """一个字集: test(c) 判断某个字在不在里面, probes 是它自己的探针字。mask 是它在 _PROBE_CHARS 上的样子, 第一次比的时候才算
    —— 一长串互不相交的 ? 每来一个都要和前面开着的每个比一遍, 每次都把两百多个探针字重跑一遍, 闸自己就要一两秒。"""
    __slots__ = ("test", "probes", "_mask")

    def __init__(self, test, probes: str):
        self.test, self.probes, self._mask = test, probes, None

    @property
    def mask(self) -> int:
        if self._mask is None:
            self._mask = sum(1 << k for k, c in enumerate(_PROBE_CHARS) if self.test(c))
        return self._mask


_ANY_CHAR = _Chars(lambda c: True, "")


@functools.lru_cache(maxsize=1024)
def _case_variants(c: str) -> str:
    """(?i) 下可能和 c 互认的字, 只拿来当探针(多几个无妨): lower/upper/casefold(单个字的)、re._casefix 那几组(ı/i、ſ/s、
    K(开尔文)/k…), 再折一轮(K → k → K)。"""
    vs = {c} | {v for v in (c.lower(), c.upper(), c.casefold()) if len(v) == 1}
    try:
        from re import _casefix
        vs |= {chr(k) for v in list(vs) for k in _casefix._EXTRA_CASES.get(ord(v), ())}
    except Exception:                                    # noqa: BLE001 — 私有表拿不到就少几个探针
        pass
    vs |= {v for x in list(vs) for v in (x.lower(), x.upper()) if len(v) == 1}
    return "".join(sorted(vs))


@functools.lru_cache(maxsize=4096)
def _one_char_rx(src: str, flags: int):
    return re.compile(src, flags)


def _char_src(op_name: str, av):
    """单个字符的节点 → (能单独编译的正则原文, 节点里写出来的字(范围取两头)); 认不出返回 None。
    RANGE / CATEGORY 只会出现在 [...] 里, 给的是方括号里那一截。"""
    if op_name in ("LITERAL", "NOT_LITERAL"):
        return ("[^%s]" if op_name == "NOT_LITERAL" else "%s") % re.escape(chr(av)), chr(av)
    if op_name == "ANY":
        return ".", "\n"
    if op_name == "RANGE":
        return re.escape(chr(av[0])) + "-" + re.escape(chr(av[1])), chr(av[0]) + chr(av[1])
    if op_name == "CATEGORY":
        src = _CATEGORY_SRC.get(getattr(av, "name", str(av)))
        return (src, "") if src else None
    if op_name == "IN":
        srcs, chars = [], ""
        for k, (sub_op, sub_av) in enumerate(av):
            sub_name = getattr(sub_op, "name", str(sub_op))
            if sub_name == "NEGATE" and k == 0:
                srcs.append("^")
                continue
            got = _char_src(sub_name, sub_av) if sub_name in ("LITERAL", "RANGE", "CATEGORY") else None
            if got is None:
                return None
            srcs.append(got[0])
            chars += got[1]
        return "[" + "".join(srcs) + "]", chars
    return None


def _char_test(op_name: str, av, flags: int) -> _Chars:
    """单个字符的节点 → 它的字集。拿不准的一律当"什么字都可能"。

    哪些字算在里面交给 re 自己判: 把这个节点单独编译(带上这一段生效的 i / s / a 旗)再 fullmatch(codex review on aw#115)。
    手写的折叠对不齐 sre: 只往小写折, (?i)[A-Z] 就不认 a —— 有了 (?-i:…) 以后 (?i)[A-Z]*(?-i:a*)x 被放行, 1000 个 a 1 秒多;
    还有 ẞ/ß、İ/i、K(开尔文)/k 这几对 casefold 对不上的; (?a) 下 \\W \\D \\S 反而更宽(é ٣ 都算进去)。
    探针是节点里写出来的字, (?i) 下再加它们的大小写变体 —— 交集只在大小写另一边的(é 对 [À-Þ] 里的 É)也探得到。"""
    if op_name == "ANY" and flags & re.DOTALL:
        return _ANY_CHAR
    got = _char_src(op_name, av)
    if got is None:
        return _ANY_CHAR
    src, chars = got
    if op_name in ("RANGE", "CATEGORY"):                 # 方括号里那一截, 单独编译得套上 []
        src = "[" + src + "]"
    rx_flags = flags & (re.IGNORECASE | re.DOTALL | re.ASCII | re.UNICODE)
    if rx_flags & re.ASCII:
        rx_flags &= ~re.UNICODE
    try:
        rx = _one_char_rx(src, rx_flags)
    except re.error:                                     # 拼出来的原文编译不了: 当什么字都行
        return _ANY_CHAR
    probes = "".join(_case_variants(c) for c in chars) if flags & re.IGNORECASE else chars
    return _Chars(lambda c: rx.fullmatch(c) is not None, probes)


def _overlap(a: _Chars, b: _Chars) -> bool:
    return bool(a.mask & b.mask) or any(a.test(c) and b.test(c) for c in a.probes + b.probes)


def _union(sets) -> _Chars:
    tests = [s.test for s in sets]
    return _Chars(lambda c: any(t(c) for t in tests), "".join(s.probes for s in sets))


def _subset(a: _Chars, b: _Chars) -> bool:
    return not a.mask & ~b.mask and all(b.test(c) for c in a.probes + b.probes if a.test(c))


def _scoped(flags: int, av) -> int:
    """进 SUBPATTERN 时换上它自己的旗: (?i:…) / (?-i:…) / (?s:…) / (?a:…) 只管组里那一段(codex review on aw#115)。
    一直拿整条的旗比, (?i:a*)A*B 就看成 {a} 和 {A} 不相交 —— 实测 2000 个 A 2 秒。开头的 (?i) 已经在 state.flags 里。"""
    add, dele = (av[1] or 0, av[2] or 0) if isinstance(av, tuple) and len(av) == 4 else (0, 0)
    if add & (re.ASCII | re.UNICODE):                    # (?a)…(?u:\w) 里 \w 回到 Unicode: 字符类旗只留组里给的那个
        flags &= ~(re.ASCII | re.UNICODE)
    return (flags | add) & ~dele


def _check_adjacent_repeats(tree) -> None:
    """顺着解析树走一遍: 挨在一起的切法数超预算抛 _Ambiguous, 闸自己走的步数超了抛 _TooComplex。"""
    root_flags = getattr(getattr(tree, "state", None), "flags", 0) or 0
    single = {"LITERAL", "NOT_LITERAL", "ANY", "IN"}
    repeats = ("MAX_REPEAT", "MIN_REPEAT")
    steps = [0]
    tests: dict = {}

    def name_of(node):
        return getattr(node[0], "name", str(node[0]))

    def char_test(name, av, flags) -> _Chars:
        """_char_test 按 (节点, 旗) 记下来, 这一次检查里同一个节点只算一次字集(codex review on aw#115): 前瞻 / 原子组 / 占有量词
        里那一段要接着前面开着的查, 走到的每个单字都和开着的比; 每次新建一个, 两百多个探针字就各跑一遍 fullmatch,
        .*(?=(?:(?:(?:a\\d){16}){16}){10}) 这么短一条闸自己就要 0.4~0.6 秒。记在这一次调用里, 不跨正则攒。"""
        key = (name, repr(av), flags)
        if key not in tests:
            tests[key] = _char_test(name, av, flags)
        return tests[key]

    def chars_of(seq, flags):
        """一段(量词底下那一段)能吃的所有字的并集: 组、分支、量词底下都往里看, 前瞻后顾和 ^ $ \\b 不吃字、不算; 只有认不出的
        (回引这类)才当什么字都行。组里有量词原来也一律当什么字都行, \\d+(?:\\.\\d+)?(?=\\s*元) 里的 \\s* 就被看成能接着组吃,
        快的也拒(codex review on aw#115)。"""
        sets = []
        for op, av in seq or ():
            name = getattr(op, "name", str(op))
            if name in single:
                sets.append(char_test(name, av, flags))
            elif name == "SUBPATTERN":
                sets.append(chars_of(av[-1], _scoped(flags, av)))
            elif name in repeats + ("POSSESSIVE_REPEAT",):
                sets.append(chars_of(av[2], flags))
            elif name == "ATOMIC_GROUP":
                sets.append(chars_of(av, flags))
            elif name == "BRANCH":
                sets.extend(chars_of(alt, flags) for alt in (av[1] or ()))
            elif name not in ("AT", "ASSERT", "ASSERT_NOT"):
                return _ANY_CHAR
        return _union(sets)

    def may_be_empty(seq) -> bool:
        """这一段能不能一个字都不吃(拿不准算能)。量词下限 ≥ 1 时「至少吃一个字、它吃不了的那些开着的就此断开」只对吃不空的
        那一段成立: (?:a?b?){1,3} 吃空也算一次, \\d*(?:a?b?){1,3}\\d*x 前后两个 \\d* 还是挨着(2000 位 10 秒)。"""
        for op, av in seq or ():
            name = getattr(op, "name", str(op))
            if name in single:
                return False
            if name in repeats + ("POSSESSIVE_REPEAT",):
                if av[0] and not may_be_empty(av[2]):
                    return False
            elif name in ("SUBPATTERN", "ATOMIC_GROUP"):
                if not may_be_empty(av[-1] if isinstance(av, tuple) else av):
                    return False
            elif name == "BRANCH":
                if not any(may_be_empty(alt) for alt in (av[1] or ())):
                    return False
        return True

    def has_variable_repeat(seq) -> bool:
        for op, av in seq or ():
            name = getattr(op, "name", str(op))
            if name in repeats + ("POSSESSIVE_REPEAT",):
                if av[0] != av[1] or has_variable_repeat(av[2]):
                    return True
            elif name in ("SUBPATTERN", "ATOMIC_GROUP"):
                if has_variable_repeat(av[-1] if isinstance(av, tuple) else av):
                    return True
            elif name == "BRANCH" and any(has_variable_repeat(alt) for alt in (av[1] or ())):
                return True
            elif name in ("ASSERT", "ASSERT_NOT") and has_variable_repeat(av[1]):
                return True
        return False

    def counting_tail(items, flags) -> set:
        """这一段结尾那串「X R X R … X」里 X 的下标(都算隔断): 单个字 X 和同一个量词 R 一个隔一个, X 结尾, X 不超过
        _MAX_COUNT_CHAIN 份; R 下限 ≤ 1、无上界、底下一个字、X 是 R 的一部分。只对后面整条正则就结束了的那一段调。
        一定得一个隔一个: 结尾两个 X 挨着([!！].*[!！][!！])要的就是「连着两个」, 不再只是个数, 前面每种选法都会试。"""
        if len(items) < 3 or name_of(items[-1]) not in single or name_of(items[-2]) not in repeats:
            return set()
        keys = [repr(n) for n in items]
        x, r, xs, j = keys[-1], keys[-2], set(), len(items) - 1
        while j >= 0 and keys[j] == x:
            xs.add(j)
            if j < 2 or keys[j - 1] != r:
                break
            j -= 2
        if len(xs) < 2 or len(xs) > _MAX_COUNT_CHAIN:
            return set()
        lo, hi, sub = items[-2][1]
        if lo > 1 or hi < _MAXREPEAT or len(sub) != 1 or name_of(sub[0]) not in single:
            return set()
        cx, cr = char_test(name_of(items[-1]), items[-1][1], flags), char_test(name_of(sub[0]), sub[0][1], flags)
        return xs if _subset(cx, cr) else set()

    def walk(seq, open_, flags, at_end=False):
        """open_: [(字集, 到这里为止的切法数)]。返回走完这一段之后还开着的; 超预算就抛 _Ambiguous。
        flags: 这一段生效的旗(组里的 (?i:…) 这类已经换上)。at_end: 这一段之后整条正则就结束了(没有任何能失败的东西)。"""
        items = list(seq or ())
        counting = counting_tail(items, flags) if at_end else set()
        for i, (op, av) in enumerate(items):
            steps[0] += 1 + len(open_)
            if steps[0] > _WALK_STEPS:
                raise _TooComplex()
            name = getattr(op, "name", str(op))
            last = at_end and i == len(items) - 1
            if name in single:
                nxt = getattr(items[i + 1][0], "name", "") if i + 1 < len(items) else ""
                if (name == "LITERAL" and nxt == "LITERAL") or i in counting:
                    open_ = []                                   # 两个字以上的固定词 / 数个数的那个字: 隔断
                else:
                    cs = char_test(name, av, flags)
                    open_ = [e for e in open_ if _overlap(e[0], cs)]
            elif name in repeats or (name == "POSSESSIVE_REPEAT" and last):   # 结尾的占有量词: 成了整条就成了, 同普通的
                lo, hi, sub = av
                if lo == hi or last:                             # 定长 {n}: 当一串固定的字; 结尾的量词: 只当 {下限}
                    if lo != hi:
                        walk(sub, [], flags)                     # 多吃的那几次还是会试, 底下那一段自己查
                    open_ = walk(list(sub) * min(lo, _UNROLL), open_, flags, last and lo <= _UNROLL) if lo else open_
                    continue
                walk(sub, [], flags)                             # 量词底下那一段自己也查((?:a*a*){1,3})
                if hi > 1 and has_variable_repeat(sub):          # 每一次之间也挨着: (?:.{0,20}){0,5}x / (?:\w{1,5})+x
                    walk(list(sub) * min(hi, _UNROLL), [], flags)
                cs = chars_of(sub, flags)
                span = _OPEN_SPAN if hi >= _MAXREPEAT else hi - lo + 1
                cost = span * max([c for e, c in open_ if _overlap(e, cs)] or [1])
                if cost > _AMBIGUITY_BUDGET:
                    raise _Ambiguous()
                inner = []
                if has_variable_repeat(sub):                     # 组里的量词接着组前面开着的查, 组里新开的往后传(见上)
                    before = {id(e) for e in open_}
                    inner = [e for e in walk(list(sub) * max(1, min(lo, _UNROLL)), list(open_), flags)
                             if id(e) not in before]
                if lo and not may_be_empty(sub):                 # 至少吃一个: 它吃不了的那些开着的就此断开
                    open_ = [e for e in open_ if _overlap(e[0], cs)]
                open_ = open_ + [(cs, cost)] + inner
            elif name == "POSSESSIVE_REPEAT":                    # 不回吐, 自己不算开着的; 可也不是隔断(见上)
                lo, hi, sub = av
                walk(sub, list(open_), flags)                    # 每次从头吃: 底下那一段接着前面开着的查
                cs = chars_of(sub, flags)
                if lo != hi:                                     # 前面每换一种切法它就吃一遍: 和开着的照样相乘(.*a++.*x)
                    span = _OPEN_SPAN if hi >= _MAXREPEAT else hi - lo + 1
                    if span * max([c for e, c in open_ if _overlap(e, cs)] or [1]) > _AMBIGUITY_BUDGET:
                        raise _Ambiguous()
                if lo and not may_be_empty(sub):                 # 至少吃一个: 它吃不了的那些开着的就此断开; 能吃空就都还开着
                    open_ = [e for e in open_ if _overlap(e[0], cs)]
            elif name == "ATOMIC_GROUP":                         # 组里照常回溯到第一次成功, 接着前面开着的查; 出了组,
                before = {id(e) for e in open_}                  # 组里新开的不回吐、不再开着, 前面开着的照常往后传
                open_ = [e for e in walk(av, list(open_), flags, last) if id(e) in before]
            elif name == "SUBPATTERN":
                open_ = walk(av[-1], open_, _scoped(flags, av), last)
            elif name == "BRANCH":
                after = [walk(alt, list(open_), flags, last) for alt in (av[1] or ())]
                open_ = [e for branch in after for e in branch]
            elif name in ("ASSERT", "ASSERT_NOT"):
                if av[0] == 1:                                   # 前瞻: 接着前面开着的查, 查完丢掉(不吃字, 后面接的还是之前开着的)
                    walk(av[1], list(open_), flags, last and name == "ASSERT")
                else:                                            # 后顾只能定长, 可它里面的前瞻照样往后扫: 也接着开着的查、
                    walk(av[1], list(open_), flags)              # 查完丢掉(\d+(?<=\d(?=\d*x)) 2000 位 4 秒)
            elif name == "AT":
                continue
            else:                                                # 回引这类认不出的: 不当隔断
                continue
        return open_

    walk(tree, [], root_flags, True)


class _Ambiguous(Exception):
    pass


class _TooComplex(Exception):
    pass


# 拿不到解析树时的保守兜底。宁可误伤几个长得像危险形状的正则, 也不放行 ——
# 用户看到的是一条明确的"换个写法"提示, 而不是一次几分钟的卡死。
_NESTED_QUANT_TEXT_RE = re.compile(r"\([^()]*[+*][^()]*\)\s*[+*]")


class UnsafeRegex(ValueError):
    """这条正则会引发灾难性回溯, 不许存、也不许跑。"""


def assert_regex_is_safe(pattern: str) -> None:
    """能编译 **且** 不是灾难性回溯形状, 否则抛。

    ⚠️ 这**不是**完备的判定 —— 重叠分支型(``(a|a)+``)和某些前后缀组合它抓不到,
    那类需要真正的回溯代价分析。它挡住的是实际会被人写出来的那一类
    (嵌套无上界量词), 并且**不谎称**自己挡住了全部: 执行期还有限时(``search_user_regex``)
    和长度上限, 漏过去的最多花一份时限、报成没查。
    """
    if not isinstance(pattern, str) or not pattern:
        raise UnsafeRegex("正则为空")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise UnsafeRegex(f"正则无法编译: {exc}") from exc

    tree = _parse_pattern(pattern)
    if (_has_conditional(tree) if tree is not None else "(?(" in pattern):
        raise UnsafeRegex("条件组 (?(1)…|…) 不支持: 闸看不进它里面, 没法确认不会卡死。拆成几条规则, 或者换成不带条件组的写法。")
    dangerous = (_has_nested_unbounded_repeat(tree) if tree is not None
                 else bool(_NESTED_QUANT_TEXT_RE.search(pattern)))
    if dangerous:
        raise UnsafeRegex(
            "这条正则是嵌套的无上界量词(形如 (a+)+ / (a*)* ), 会引发灾难性回溯 —— "
            "实测 26 个字符就要 6 秒, 每多 2 个字符翻一倍, 正文上百字等于卡死。"
            "把内层的 + / * 换成确定的次数(如 {1,20}), 或去掉外层的量词。")
    if tree is None:
        return
    try:
        _check_adjacent_repeats(tree)
    except _TooComplex:
        raise UnsafeRegex(
            "这条正则太复杂(定次量词套了好几层, 展开后太长), 没法确认它不会卡死。换个更直接的写法。") from None
    except _Ambiguous:
        raise UnsafeRegex(
            "这条正则里有能匹配同样字符的量词挨在一起(形如 a*a*b / .*.*x / \\w+\\w+ / a.*a.*b), 中间没有隔开 —— "
            "量词自己也能吃的单个字隔不开(a.*a.*b 里的 a), 前瞻 (?=…)(?!…) 里的量词和它前面的也算挨着(\\d+(?=\\d*元)), "
            "占有量词 a++、原子组 (?>…) 也隔不开, 可选的组 (?:…)? 里的量词和组前后的也算挨着(\\d+(?:[,，]?\\d+)?元)。"
            "这会引发灾难性回溯: 实测 a*a*a*a*a*a*a*a*b 配 30 个 a 要 1 秒, "
            ".*.*x 配 2000 字的一行要 1.5 秒。删掉多余的量词(开头结尾的 .* 不用写, 本来就在全文里找); 或者用两个字以上的"
            "固定词隔开, 或者让量词吃不了那个字(\\n[ \\t]*\\n、[^!]*、\\d+(?:[,，]\\d+)?元)。数「X 最多 N 次」写成 X[\\s\\S]*X…X、最后一个 X "
            f"后面什么都别接(连 $ 也别加), 最多展开 {_MAX_COUNT_CHAIN} 份。") from None


def regex_is_safe(pattern: str) -> bool:
    """``assert_regex_is_safe`` 的布尔版。"""
    try:
        assert_regex_is_safe(pattern)
    except UnsafeRegex:
        return False
    return True


# 执行期喂给正则的正文上限。**这不是主要防线**(见上), 只是让"漏网的
# 危险形状"和"单纯很慢的正则"都有个天花板。正常稿件远在这个数以内。
MAX_REGEX_TARGET_CHARS = 20_000


# ── 执行期限时(codex review on aw#115 第四轮) ──────────────────────────────────────────────
# 闸放行的 a*aaa*aaa*aaa*aaa*aaa*aaa*aaa*b(两个字的字面量 aa 能落在很多位置, 隔不开前后的 a*, 闸却当它是隔断)50 个 a 就
# 6 秒, 2 万字的上限之内能把共享的服务进程握着 GIL 卡住几分钟。闸再补一个洞还会有下一个, 所以根上的办法是执行期限时:
# 第三方 regex 包(mrab-regex)的 search 收 timeout, 到点抛 TimeoutError。两处执行(这里的 check_hard_rules、写作台的
# rulecheck)都只走 search_user_regex。超时不算命中、也不往外抛: 调用方把这条规则当"这次没查"(rulecheck 报进
# stats["timed_out"], 这里跳过并记日志), 不能当守住了。
#
# ⚠️ 时限是**一条规则在一次检查里共用的一份**(RegexBudget), 不是每搜一次一份: 一批 20 篇 × 几个部位, 每次都差一点到点的话
#    加起来就不止一份了。用完的那条后面的稿子不再搜。所以一次检查最多多花约 (正则规则条数) × REGEX_SEARCH_TIMEOUT_SEC: 到点之后
#    regex 隔一阵才看一次表, 实测最多再拖两成多; 这个进程第一次用到这条正则时的换写法、编译也从这一份里扣(打断不了, 但换出来的
#    写法有长度上限 MAX_TRANSLATED_CHARS、重复展开有上限 MAX_REPEAT_UNROLL, 编一次 0.1 秒以内)。exact 那份码位表一个进程扫一次
#    (零点几秒), 不扣(见 search_user_regex)。正常的规则一批 20 篇 × 1500 字实测几毫秒到十几毫秒(十个词的
#    \b 词表不到 1 毫秒), 一份 0.1 秒够查完 —— 前提是下面换写法时字集用属性写, 别逐个码位列(见下)。
#
# ⚠️ regex 的默认语法(VERSION0)和 re 几乎一样, 但有几处不一样, 小红书正文里还正好常见。喂给 regex 之前先换写法(_for_regex),
#    结论和 re 一样(tests/test_regex_safety.py 逐个码位比字集、拿闸放行的那批正则 × 含 emoji / ①② 的正文比 search 的结果):
#    · \w \W \d \D \s \S \b \B: regex 的 \w 含组合符(emoji 后面的 U+FE0F、连写 emoji 中间的 U+200D), 不含 ①②③ ² ½, re 正相反;
#      \s 在 re 里多 U+001C~001F。直接换引擎的话 ^\W 碰上「①」两边结论相反。换成用 Unicode 属性写的字集: \w = [\p{L}\p{N}_]、
#      \d = \p{Nd}, \s 是 str.isspace() 那 29 个字(写死, 测试逐个码位和 re 比)。⚠️ 别再逐个码位列显式字集(codex review on
#      aw#115 第五轮): \w 一个就七百多段、14 KB, regex 每走一步挨个比, 普通的 \bvx\b 一批 20 篇就超时、真命中丢了, 编译一条
#      \b 词表也要几秒。属性写法在本机 Python 认识的每个码位上都和 re 一样; 只有 regex 的 Unicode 表比 Python 自带的新, 新收的
#      字母数字(3.13 上两万多个: 汉字扩展 J 区、几种新文字、新的拉丁字母 Ɤ 等)re 一律不算 \w \d —— 正文里真有这种字时
#      (_differs_from_re, 一篇算一次)这篇改用 exact 那份写法(把它们去掉, 慢几倍, 第一次碰上时才算、才编译)。新 emoji 两边都不算 \w。
#    · 字集里的 [(regex 认成嵌套 / POSIX 字集 [:alpha:]): 字集照 re 自己的解析树重写成显式的字和区间。
#    · re 不当量词的 {: {e} {d} {e<=1} 在 regex 里是模糊匹配(x{e} 能配上 y), 转义成字面的 \{。
#    · (?x) 下 re 只跳过 ASCII 的空白, regex 还跳过全角空格等 23 个字: 那些转义成字面的。
#    · (?i) 下 re 不按大小写扩 \w 这类字集, regex 会扩((?i)[\W] 多认 Ι ß): 换出来的字集包一层 (?-i:…); 字集里有它们时拆开写,
#      别的字照旧不分大小写。regex 自己还有个毛病要绕(见 _for_regex 里的 folds): 分支里有不分大小写的时候, 分大小写的取反字集
#      开头的分支会漏字。
#    · \b 挨着一定要配上的英文字母数字(\bvx\b)时只看另一边: 少两个环视, regex 还能先找字面量, \b 词表快几百倍。
#    还剩几处 (?i) 下的不一样, 写规则用不到, 不再绕(test_known_gap_*): 土耳其语的 ı / İ; 回引((?i)(\w)\1 —— re 比的是 lower(),
#    regex 按大小写折叠, ſ 和 s 算同一个); 比 Python 新的 Unicode 才有的大小写对(Ɤ 和 ɤ); 组内的 (?a:…) (?u:…) 碰上不分大小写
#    (regex 不分大小写时只看整条正则的旗, 组里的 a / u 管不到: (?a:(?i:k)) 在 regex 里认 K 开尔文符号, re 不认); 整条开了 (?a)
#    时 (?u:…) 里再套的组(换写法加的、自己写的 (?:…) 都算), regex 退回整条的 ASCII(test_known_gap_unicode_scope_inside_global_ascii)。
# regex 装不上就退回 re(仍在闸后面、仍有长度上限, 只是没有时限), 记一次日志。装上了却跑不了的(换过的写法太长、regex 编译不了、
# 拿不到 re 的解析树)不退回没有时限的 re: 报成这次没查(RegexTimeout), 记一次日志 —— 不然一个 regex 不认的写法就能把闸后面
# 那道时限整个绕过去(a*aaa*…b|示例{1,2}{e} 曾经就这样跑进 re, 2000 个 a 永不返回)。
try:
    import regex as _regex
except ImportError:                                  # pragma: no cover — requirements.lock 里有, 缺了只是退回 re
    _regex = None
_logger = logging.getLogger("validator")
_REGEX_MISSING_LOGGED = [False]


class RegexTimeout(Exception):
    """用户正则这次没查成: 没在时限内跑完, 或者限时的引擎跑不了它(见 _compiled; 消息里是哪一种)。不算命中, 也不算没命中 ——
    这条规则这次没查。"""


def regex_search_timeout() -> float:
    """一条正则规则在一次检查里共用的时限(秒), config.REGEX_SEARCH_TIMEOUT_SEC。config 读不了就用 0.1。"""
    try:
        import config
        return float(config.REGEX_SEARCH_TIMEOUT_SEC)
    except Exception:                                # noqa: BLE001 — 增强项的配置坏了退回默认, 不拖垮查规则
        return 0.1


class RegexBudget:
    """一条用户正则在一次检查里共用的那一份时间。search_user_regex 每搜一次从这里扣(搜的时间, 加上这个进程第一次用这条正则时
    换写法、编译的时间); 扣完了再搜就直接抛 RegexTimeout, 不再跑。``timed_out``: 这一份里有没有哪次没查成(调用方据此报
    "这条这次没查完")。"""
    __slots__ = ("left", "timed_out")

    def __init__(self, seconds: Optional[float] = None):
        self.left = regex_search_timeout() if seconds is None else float(seconds)
        self.timed_out = False


# 换出来的写法的长度上限。regex 编译中途打断不了, 编译时间跟着换出来的长度走(一个 \b 换成七十来个字, 挨着英文字母数字的只要
# 三十来个); 闸不管正则多长, 这是执行期唯一卡住编译的地方: 超过就不编译, 报这次没查。实测 3 万字(四百个 \b 连写)编译 0.1 秒,
# 一条 30 个词的 \b 词表换出来一千多字、编译几毫秒。
# exact 那份(见上)的字集是一长串区间, 一个挨着汉字的 \b 就一万二上下, 但它编译得快(每万字约 10 毫秒, 3.11 / 3.13 实测): 另给一个
# 上限, 卡在编一次 0.1 秒左右。挨着汉字的 \b 词表 5 个词以内、挨着英文的 30 个词以内能查; 再长的, 带新收的字母数字的那几篇报没查
# (独立复审 on aw#115 第六轮: 原来和快的那份共用 3 万, 挨着汉字的 \b 3 个词就碰上)。
MAX_TRANSLATED_CHARS = 30_000
MAX_TRANSLATED_CHARS_EXACT = 100_000

# 重复次数展开的上限(独立复审 on aw#115 第六轮)。regex 编译时把 {m,} {m,n} 的最少那 m 遍展开, 嵌套的乘起来: x{10000000}
# 只有十几个字, 换出来远不到 MAX_TRANSLATED_CHARS, 编译却要 5 秒、2.5 GB, 打断不了、握着 GIL(整个进程卡住); 再大的直接
# 撑爆内存。re 不展开, 微秒级。超过就不编译, 报这次没查(闸不改, Jev 镜像它)。实测展开 2 万遍编译几毫秒、10 万遍五六十毫秒;
# 正常的规则([\s\S]{1001,} \d{11} 哈{5,})几遍到一两千遍。
MAX_REPEAT_UNROLL = 50_000


def _repeat_unroll(seq, outer: int = 1) -> int:
    """解析树里每个重复要展开几遍(它自己的最少次数 × 外面每层重复的最少次数, 0 和 1 都算 1 遍), 全加起来。"""
    total = 0
    for op, av in seq or ():
        name = getattr(op, "name", str(op))
        if name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
            times = outer * max(av[0], 1)
            total += times + _repeat_unroll(av[2], times)
        elif name in ("SUBPATTERN", "ASSERT", "ASSERT_NOT"):
            total += _repeat_unroll(av[-1], outer)
        elif name == "ATOMIC_GROUP":
            total += _repeat_unroll(av, outer)
        elif name == "BRANCH":
            total += sum(_repeat_unroll(sub, outer) for sub in av[1] or ())
        elif name == "GROUPREF_EXISTS":
            total += sum(_repeat_unroll(sub, outer) for sub in av[1:] if sub is not None)
    return total

# re 认的字(str.isalnum() 或 _ / str.isdecimal() / str.isspace()), 写成 regex 的字集正文(不带方括号)
_ASCII_CLASS = {"w": "\\x30-\\x39\\x41-\\x5a\\x5f\\x61-\\x7a", "d": "\\x30-\\x39", "s": "\\x09-\\x0d\\x20"}
_SPACE = "\\x09-\\x0d\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000"
_FAST_CLASS = {"w": "\\p{L}\\p{N}_", "d": "\\p{Nd}", "s": _SPACE}
# \w 的反面: 除了字母(L)数字(N)之外的大类, Pc 里除了 _ 的那几个另列(见 _exact_classes)
_NOT_WORD_PROPS = "\\p{M}\\p{Pd}\\p{Ps}\\p{Pe}\\p{Pi}\\p{Pf}\\p{Po}\\p{S}\\p{Z}\\p{C}"
_RE_VERBOSE_SPACE = frozenset(" \t\n\r\x0b\x0c")     # (?x) 下 re 跳过的空白; regex 跳过的是 str.isspace() 的全部
if _regex is not None:
    _FAST_W, _FAST_D = _regex.compile("[" + _FAST_CLASS["w"] + "]"), _regex.compile(_FAST_CLASS["d"])


def _ranges(chars) -> str:
    out: list[list[int]] = []
    for c in sorted(set(map(ord, chars))):
        if out and out[-1][1] == c - 1:
            out[-1][1] = c
        else:
            out.append([c, c])
    return "".join(f"\\U{a:08x}" if a == b else f"\\U{a:08x}-\\U{b:08x}" for a, b in out)


_EXACT_LOCK = threading.Lock()
_EXACT_TABLE: list = []


def _exact_classes() -> Optional[dict]:
    """exact 那份字集: \\w \\d 去掉 regex 认、re 不认的字(Python 的 unicodedata 还不认识的新字母数字)。都写成「反面」(取反用):
    \\w = [^ 非字母数字的大类 + Pc 里除 _ 的 + 那些新字], \\d = [^ \\P{Nd} + 新数字]。整个码位表扫一遍, 零点几秒, 只在正文里真有
    这种字时才算, 一个进程一次(加锁: 几个线程同时碰上也只算一遍, 别的等着; 独立复审 on aw#115 第六轮)。反过来 re 认、regex 不认
    的字现在一个都没有(两个 Python 版本都是); 哪天有了这份就写不出来, 返回 None, 这几篇报没查。"""
    if not _EXACT_TABLE:
        with _EXACT_LOCK:
            if not _EXACT_TABLE:
                _EXACT_TABLE.append(_build_exact_classes())
    return _EXACT_TABLE[0]


def _build_exact_classes() -> Optional[dict]:
    re_w, re_d, re_nw, re_nd = (re.compile(p) for p in ("\\w", "\\d", "\\W+", "\\D+"))
    rx_nw, rx_nd, rx_pc = (_regex.compile(p) for p in ("[^" + _FAST_CLASS["w"] + "]+", "\\P{Nd}+", "\\p{Pc}"))
    extra_w, extra_d, missing, pc = [], [], [], []
    for lo, hi in ((0, 0xD800), (0xE000, 0x110000)):
        s = "".join(map(chr, range(lo, hi)))
        extra_w += (m.group() for run in re_nw.finditer(s) for m in _FAST_W.finditer(run.group()))
        extra_d += (m.group() for run in re_nd.finditer(s) for m in _FAST_D.finditer(run.group()))
        missing += (m.group() for run in rx_nw.finditer(s) for m in re_w.finditer(run.group()))
        missing += (m.group() for run in rx_nd.finditer(s) for m in re_d.finditer(run.group()))
        pc += (c for c in rx_pc.findall(s) if c != "_")
    if missing:
        return None
    return {"w": (_NOT_WORD_PROPS + _ranges(pc) + _ranges(extra_w), True),
            "d": ("\\P{Nd}" + _ranges(extra_d), True),
            "s": (_SPACE, False)}


@functools.lru_cache(maxsize=64)
def _differs_from_re(text: str) -> bool:
    """这段正文里有没有快的那份字集(_FAST_CLASS)和 re 结论不一样的字(re 的 \\w 是 str.isalnum() 或 _, \\d 是 str.isdecimal())。
    只看出现过的那几种字, 一篇两万字也就零点几毫秒; 同一篇被好几条规则搜, 记住结论。"""
    chars = "".join(set(text))
    return (set(_FAST_W.findall(chars)) != {c for c in chars if c.isalnum() or c == "_"}
            or set(_FAST_D.findall(chars)) != {c for c in chars if c.isdecimal()})


_FLAG_GROUP = re.compile(r"\(\?([aiLmsux]*)(?:-([imsx]*))?([:)])")
# re 当量词的 {: 紧跟 ASCII 数字(可带一个逗号), 再接 }。别的 { 在 re 里都是字面的({e} {} {1, 2} {x})
_QUANTIFIER = re.compile(r"\{(?:[0-9]+(?:,[0-9]*)?|,[0-9]*)\}")
_CATEGORY_ESCAPE = {"CATEGORY_WORD": "w", "CATEGORY_NOT_WORD": "W", "CATEGORY_DIGIT": "d", "CATEGORY_NOT_DIGIT": "D",
                    "CATEGORY_SPACE": "s", "CATEGORY_NOT_SPACE": "S"}
_UNIVERSE = "\\x00-\\U0010ffff"
_WORD_ASCII = frozenset("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz")
_DIGITS = frozenset("0123456789")
_CLASS_ESCAPES = frozenset("wWdDsS")
_HEX_DIGITS = frozenset("0123456789ABCDEFabcdef")
_OCT_DIGITS = frozenset("01234567")


class _Untranslatable(Exception):
    """换不出和 re 结论一样的写法(re 的解析树里有这里不认识的东西, 或者 exact 那份写不出来)。"""


def _set_items(text: str) -> tuple[bool, str, list[str]]:
    """一个字集的原文([…])照 re 自己的解析树拆开: (取反, 普通的字和区间写成的字集正文, [类别转义 w W d D s S …])。
    不照原文抄: [ 在 regex 里是嵌套 / POSIX 字集的开头, 拆掉 \\w 之后剩下的原文拼起来还可能多出区间(x-y-\\da → x-y-a)。"""
    tree = _parse_pattern(text)
    if tree is None or len(tree) != 1:
        raise _Untranslatable(text)
    op, av = tree[0]
    if str(op) in ("LITERAL", "NOT_LITERAL"):
        return str(op) == "NOT_LITERAL", f"\\U{av:08x}", []
    if str(op) != "IN":
        raise _Untranslatable(text)
    negate, raw, escapes = False, [], []
    for item_op, item in av:
        name = str(item_op)
        if name == "NEGATE":
            negate = True
        elif name == "LITERAL":
            raw.append(f"\\U{item:08x}")
        elif name == "RANGE":
            raw.append(f"\\U{item[0]:08x}-\\U{item[1]:08x}")
        elif name == "CATEGORY" and str(item) in _CATEGORY_ESCAPE:
            escapes.append(_CATEGORY_ESCAPE[str(item)])
        else:
            raise _Untranslatable(text)
    return negate, "".join(raw), escapes


def _for_regex(pattern: str, exact: bool = False) -> Optional[tuple[str, bool]]:
    """把 re 的写法换成 regex 包里结论一样的写法(为什么要换、换哪几样见上): (换好的, 用没用到 Unicode 下的 \\w \\d \\b)。后一样
    为真时, 正文里有 _differs_from_re 的字就得用 exact=True 那份。拿不到 re 的解析树(不知道全局的 (?a) (?x) (?i))、或者换不出来,
    返回 None。

    加的组全是不捕获的, 回引的组号不变。跟着组里的旗走: (?a:…) 里换成 ASCII 的字集、(?u:…) 里换回 Unicode 的; (?i:…) (?-i:…)
    决定换出来的字集要不要包 (?-i:…); (?x) 下 # 到行尾是注释、原样抄; (?#…) 原样抄; \\N{…} 原样抄。

    ⚠️ re 自己有一处不一致, 这里不跟: 整条正则以组内换了 ASCII / Unicode 的字集开头时((?a:\\W)…、(?a)(?u:\\w)…), re 扫起点用的是
    全局旗算出来的字集, 会跳过组里其实认的字 —— (?a:\\W) 在「物」上不成立, x(?a:\\W) 在「x物」上又成立。这里照 re 文档的意思(组里的旗
    说了算)换, 只有这种开头的写法两边会不一样, 写规则用不到。"""
    tree = _parse_pattern(pattern)
    if tree is None:
        return None
    flags = getattr(getattr(tree, "state", None), "flags", 0) or 0
    ascii_, verbose, ignore = bool(flags & re.ASCII), bool(flags & re.VERBOSE), bool(flags & re.IGNORECASE)
    unicode_word = False

    def cls(esc: str) -> tuple[str, bool]:
        """一个类别转义 → (字集正文, 取不取反): 认的字是 [正文] 或 [^正文]。"""
        nonlocal unicode_word
        base = esc.lower()
        if ascii_:
            content, inverted = _ASCII_CLASS[base], False
        elif base == "s" or not exact:
            content, inverted = _FAST_CLASS[base], False
        else:
            table = _exact_classes()
            if table is None:
                raise _Untranslatable(pattern)
            content, inverted = table[base]
        unicode_word = unicode_word or (base != "s" and not ascii_)
        return content, inverted != esc.isupper()

    # 整条正则里哪儿开了 (?i), 取反的字集前面就都垫一个什么字都认的前瞻 (?=(?s:.))。regex 自己的毛病: 一组分支里有不分大小写的,
    # 别的分支开头是分大小写的取反字集时, 它预先算「这个分支能以哪些字开头」用的是大小写折叠, 把折叠后落进字集的字剔掉了 ——
    # (?i:x)|[^ιq] 碰上 U+0345(折成 ι), re 配得上、regex 配不上; 换出来的 (?i)x|(?-i:[^\p{L}\p{N}_]) 也一样。垫了前瞻它就不预先算了。
    folds = ignore or any("i" in m.group(1) for m in _FLAG_GROUP.finditer(pattern))

    def guarded(s: str, negated: bool) -> str:
        return f"(?:(?=(?s:.)){s})" if negated and folds else s

    def bare(content: str, inverted: bool, keep_case: bool) -> str:
        s = ("[^" if inverted else "[") + content + "]"
        return f"(?-i:{s})" if keep_case else s

    def one(content: str, inverted: bool) -> str:
        # (?i) 下 re 不按大小写扩这类字集((?i)\W 不认 Ι), regex 会扩: 包一层 (?-i:…)
        return guarded(bare(content, inverted, ignore), inverted)

    def char_set(negate: bool, raw: str, escapes: list[str]) -> str:
        if not escapes:
            return guarded(bare(raw, negate, False), negate)
        if {e for e in escapes if e.islower()} & {e.lower() for e in escapes if e.isupper()}:
            return ("[^" if negate else "[") + _UNIVERSE + "]"          # [\s\S] 这种: 什么字都认
        classes = [cls(e) for e in escapes]
        plain = "".join(c for c, inverted in classes if not inverted)
        flipped = [c for c, inverted in classes if inverted]
        if ignore:              # 普通的字照旧不分大小写, 换出来的字集不扩: 拆开写
            parts = ([(raw, False)] if raw else []) + ([(plain, True)] if plain else [])
        else:
            parts = [(raw + plain, False)] if raw or plain else []
        if not negate:          # 并: 普通的字 ∪ 不取反的字集 ∪ 每个取反的字集
            # 原子组: 每个分支都正好吃一个字, 原子不改能配上什么; 不加的话一个字落进好几个分支(！ 既在普通的字里又是 \W),
            # 后面配不上时 regex 挨个分支回溯, 量词一套就是指数级((?i)[！!\W]{3,}vx 碰上一长串 ！ 超时、真命中报成没查;
            # re 当它是一个字集, 微秒级。独立复审 on aw#115 第六轮)
            alts = [bare(c, False, keep) for c, keep in parts] + [one(c, True) for c in flipped]
            return alts[0] if len(alts) == 1 else "(?>" + "|".join(alts) + ")"
        # 取反: 同一个字既不在普通的字、不取反的字集里, 又在每个取反的字集里 —— 前面的用前瞻, 最后一个吃掉这个字
        conds = [bare(c, True, keep) for c, keep in parts] + [bare(c, False, ignore) for c in flipped]
        body = conds[0] if len(conds) == 1 else "".join(f"(?={c})" for c in conds[:-1]) + conds[-1]
        return guarded(body if len(conds) == 1 else f"(?:{body})", True)

    def boundary(esc: str, word_before: bool, word_after: bool) -> str:
        w = one(*cls("w"))
        if (word_before or word_after) and not (ascii_ and ignore):
            # 紧挨着的那一边是一定要配上的英文字母数字 / _(\bvx\b 的 v 和 x), 那个字一定在 \w 里, 只用看另一边: \b 就是另一边
            # 不是 \w。少两个环视之外, regex 还能先找字面量再回头看 —— \b 词表一篇快几百倍(换成四个环视时一批 20 篇要几十毫秒)
            want = "!" if esc == "b" else "="
            return (f"(?{want}{w})" if word_before else "") + (f"(?<{want}{w})" if word_after else "")
        if esc == "b":
            return f"(?:(?<={w})(?!{w})|(?<!{w})(?={w}))"
        # 空字符串上 re 的 \B 不成立(3.14 起改成成立), regex 的成立: 照当前这个 Python 的 re 来
        guard = "" if re.search(r"\B", "") else r"(?!\A\Z)"
        return f"(?:(?<={w})(?={w})|(?<!{w})(?!{w}){guard})"

    def word_literal_at(j: int) -> bool:
        """pattern[j] 是一个英文字母数字 / _ 的字面量, 而且一定要配上(后面没跟着 ? * { 让它可有可无; (?x) 下后面紧跟着空白或注释的
        也不算, 量词可能隔着空白)。"""
        if j >= n or pattern[j] not in _WORD_ASCII or pattern.startswith("(?#", j + 1):
            return False            # 后面隔着 (?#…) 的量词照样管它(a(?#c)*), 不算一定要配上
        nxt = pattern[j + 1:j + 2]
        return nxt not in ("?", "*", "{") and not (verbose and (nxt in _RE_VERBOSE_SPACE or nxt == "#"))

    out: list[str] = []
    stack: list[tuple[bool, bool, bool]] = []
    i, n = 0, len(pattern)
    word_before = False             # 刚抄过去的是不是一个英文字母数字 / _ 的字面量(见 boundary)
    try:
        while i < n:
            c = pattern[i]
            literal = False
            if c == "\\":
                esc = pattern[i + 1:i + 2]
                if esc in _CLASS_ESCAPES:
                    out.append(one(*cls(esc)))
                    i += 2
                elif esc in ("b", "B"):
                    out.append(boundary(esc, word_before, word_literal_at(i + 2)))
                    i += 2
                elif esc == "N" and pattern.startswith("{", i + 2):          # \N{名字}: 名字里的 { 空格原样抄
                    j = pattern.index("}", i) + 1
                    out.append(pattern[i:j])
                    i = j
                elif esc in _DIGITS:
                    # 回引 \1 \12 / 八进制 \0 \012 \123: 照 re 的读法定长度(\0 后最多两个八进制位; \1-\9 后最多一位, 前两位和
                    # 第三位都是八进制时是三位的八进制)。(?x) 下 regex 会隔着空白、注释接着读数字(\1 23 读成 \123), re 不会:
                    # 包一层组把它截住(独立复审 on aw#115 第六轮)
                    j = i + 2
                    if esc == "0":
                        while j < min(n, i + 4) and pattern[j] in _OCT_DIGITS:
                            j += 1
                    elif j < n and pattern[j] in _DIGITS:
                        j += 1
                        if esc in _OCT_DIGITS and pattern[j - 1] in _OCT_DIGITS and j < n and pattern[j] in _OCT_DIGITS:
                            j += 1
                    out.append(f"(?:{pattern[i:j]})" if verbose else pattern[i:j])
                    i = j
                else:
                    # \x41 \u4e00 后面的十六进制位是这个转义的一部分, 不是字面量: 一起抄过去(多吃几个也只是原样抄)
                    j = i + 2
                    if esc in ("x", "u", "U"):
                        while j < n and pattern[j] in _HEX_DIGITS:
                            j += 1
                    out.append(pattern[i:j])
                    i = j
            elif c == "[":                               # 字集: 一直到收尾的 ](紧跟在 [ 或 [^ 后面的 ] 是字面的)
                j = i + 2 if pattern.startswith("[^", i) else i + 1
                first = j
                while j < n and not (pattern[j] == "]" and j != first):
                    j += 2 if pattern[j] == "\\" else 1
                out.append(char_set(*_set_items(pattern[i:j + 1])))
                i = j + 1
            elif c == "#" and verbose:
                j = pattern.find("\n", i)
                j = n if j < 0 else j + 1
                out.append(pattern[i:j])
                i = j
            elif pattern.startswith("(?#", i):
                j = i + 3
                while j < n and pattern[j] != ")":
                    j += 2 if pattern[j] == "\\" else 1
                out.append(pattern[i:j + 1])
                i = j + 1
            elif c == "(":
                m = _FLAG_GROUP.match(pattern, i)
                if m and m.group(3) == ")":              # 开头的全局旗: 已经从解析树里拿了
                    out.append(m.group(0))
                    i = m.end()
                    word_before = False
                    continue
                stack.append((ascii_, verbose, ignore))
                if m:
                    on, off = m.group(1), m.group(2) or ""
                    ascii_ = True if "a" in on else (False if "u" in on else ascii_)
                    verbose = True if "x" in on else (False if "x" in off else verbose)
                    ignore = True if "i" in on else (False if "i" in off else ignore)
                    out.append(m.group(0))
                    i = m.end()
                else:
                    out.append(c)
                    i += 1
            elif c == ")":
                if stack:
                    ascii_, verbose, ignore = stack.pop()
                out.append(c)
                i += 1
            elif c == "{":
                m = _QUANTIFIER.match(pattern, i)
                out.append(m.group(0) if m else "\\{")
                i = m.end() if m else i + 1
            elif verbose and c.isspace() and c not in _RE_VERBOSE_SPACE:
                out.append(f"\\U{ord(c):08x}")
                i += 1
            else:
                literal = c in _WORD_ASCII
                out.append(c)
                i += 1
            word_before = literal
    except (_Untranslatable, ValueError):
        return None
    return "".join(out), unicode_word


@functools.lru_cache(maxsize=512)
def _compiled(pattern: str, exact: bool = False):
    """(编译好的, 要不要看正文换 exact 那份, 跑不了的理由)。每个 pattern 每个进程编一次(exact 那份第一次用到才编); 退回 re 和
    跑不了的那次各记一行日志。

    - regex 装不上: (re 编译好的, False, None) —— 退回 re, 没有时限。
    - 限时的引擎跑不了(拿不到解析树 / 换不出来 / 换出来超过 MAX_TRANSLATED_CHARS(_EXACT) / 重复展开超过 MAX_REPEAT_UNROLL /
      regex 编译不了): (None, False, 理由), search_user_regex 报这次没查。不退回没有时限的 re。
    pattern 本身 re 都编译不了的, 照 re 抛 re.error(和闸、和原来一样)。"""
    fallback = re.compile(pattern)
    if _regex is None:
        if not _REGEX_MISSING_LOGGED[0]:
            _REGEX_MISSING_LOGGED[0] = True
            _logger.warning("validator: 装不上 regex 包, 用户正则退回 re 执行(没有时限, 只剩静态闸和长度上限)")
        return fallback, False, None
    unroll = _repeat_unroll(_parse_pattern(pattern))
    got = None if unroll > MAX_REPEAT_UNROLL else _for_regex(pattern, exact)
    cap = MAX_TRANSLATED_CHARS_EXACT if exact else MAX_TRANSLATED_CHARS
    if unroll > MAX_REPEAT_UNROLL:
        why = f"重复次数要展开 {unroll} 遍, 超过 {MAX_REPEAT_UNROLL}, 编译太慢"
    elif got is None:
        why = "换不成限时引擎的写法"
    elif len(got[0]) > cap:
        why = f"换成限时引擎的写法有 {len(got[0])} 字, 超过 {cap}, 编译太慢"
    else:
        try:
            return _regex.compile(got[0], _regex.VERSION0), got[1], None
        except Exception as exc:                     # noqa: BLE001 — 编不了就报没查, 不退回没有时限的 re
            why = f"限时引擎编译不了({str(exc)[:80]})"
    _logger.warning("validator: 正则 %r%s %s, 这条报成这次没查", pattern[:80], "(exact)" if exact else "", why)
    return None, False, why


def search_user_regex(pattern: str, text: str, budget: Optional[RegexBudget] = None):
    """用户写的正则(forbidden_regex)在正文里搜一次: 返回匹配(``.group(0)`` 可用)或 None; 这次没查成抛 ``RegexTimeout``
    (时间用完了, 或者限时的引擎跑不了它)。

    调用方先过 ``assert_regex_is_safe``(闸)再调这里; re 都编译不了的照 re 抛 re.error。正文截到 MAX_REGEX_TARGET_CHARS。
    ``budget`` 是这条规则在这次检查里共用的那一份(见 RegexBudget), 不传就是只给这一次的一份。这个进程第一次用这条正则时的
    换写法、编译也从这一份里扣: 编译打断不了, 但不能不算 —— 换出来的长度有上限(MAX_TRANSLATED_CHARS)、重复展开有上限
    (MAX_REPEAT_UNROLL), 编一次最多零点几秒。exact 那份码位表(一个进程扫一次)不扣。"""
    budget = budget if budget is not None else RegexBudget()
    text = (text or "")[:MAX_REGEX_TARGET_CHARS]
    if budget.left <= 0:
        budget.timed_out = True
        raise RegexTimeout("这条的时限已经用完了")
    t0 = time.monotonic()
    try:
        rx, needs_exact, why = _compiled(pattern)
        if isinstance(rx, re.Pattern):               # regex 装不上: 退回 re, 没有时限
            return rx.search(text)
        if rx is not None and needs_exact and _differs_from_re(text):
            # 码位表一个进程扫一次(零点几秒), 不是这条规则花的: 不从这一份里扣, 不然第一篇带新字的稿子上真命中报成没查
            t_table = time.monotonic()
            _exact_classes()
            t0 += time.monotonic() - t_table
            rx, _, why = _compiled(pattern, True)
        if rx is None:
            budget.timed_out = True
            raise RegexTimeout(why)
        left = budget.left - (time.monotonic() - t0)
        if left <= 0:
            budget.timed_out = True
            raise RegexTimeout("编译用完了这条的时限")
        return rx.search(text, timeout=left)
    except TimeoutError:
        budget.timed_out = True
        raise RegexTimeout("没在时限内跑完") from None
    finally:
        budget.left -= time.monotonic() - t0


# ─────────────────────────────────────────────────────────────────────────
# 规则解析：从中文短句里抽出可执行的断言
# ─────────────────────────────────────────────────────────────────────────

# "禁止 X" 类否定词（不抓出来）
_NEG_PREFIXES = (
    "禁止", "严禁", "不得", "不要", "别用", "别出现", "避免",
    "不能用", "不能出现", "不能", "杜绝",
)

# "必须 X" 类肯定词
_POS_PREFIXES = ("必须包含", "必须出现", "必须带", "需要包含", "需要出现")

# 字符上限模式 "X不超过/最多N字" 或 "X≤N字"
# R-041: 放宽常见中文变体 —— "不超过20个字"(量词 个)、"控制在20字以内"。
# review 修正: "控制在"本身不表方向("控制在20字以上"是下界、"左右"是约数),
# 必须带"以内/之内/内"才能当 max_len; 其余动词(不超过/最多/≤)自带上界语义,
# 后缀可选。捕获 verb+bound 交给 _parse_rule 判定。
_LEN_PATTERN = re.compile(
    r"(?P<scope>标题|正文|开头|结尾|关键词)\s*"
    r"(?P<verb>不超过|最多|≤|<=|不能超过|控制在)\s*(?P<n>\d+)\s*个?字(?P<bound>以内|之内|内)?"
)

# R-041: 无引号回退里的"性质描述"拦截 —— "标题不要太长"会被抠成字面禁用词
# "太长"(文案里出现"太长"两个字即误报违规)。
# review 修正: 不能按首字符一刀切 —— 过/偏/太/很 同时是大量实义名词的首字
# ("禁止出现过敏"/"禁止使用偏方"按旧版会被整条静默丢弃, 硬规则失效)。改成
# 闭集判定: (a) 多字程度副词(过于/过分/比较/有点/有些)后接什么都是性质;
# (b) 单字程度副词(太/很/偏/过)仅当后面恰好是常见性质形容词时才算。闭集外
# 的组合(如"太魔性")按字面词处理 —— 宁可个别罕见性质词漏拦(回到 R-041 前
# 行为), 也不丢真实的字面禁用词。
_PROPERTY_TARGET = re.compile(
    r"^(?:过于|过分|比较|有点|有些)"
    r"|^[太很偏过]"
    r"(?:长|短|多|少|大|小|高|低|快|慢|硬|软|干|湿|轻|重|强|弱|贵|土|俗|淡|浓|平|满|碎|密|杂|乱"
    r"|正式|口语|直白|生硬|夸张|啰嗦|油腻|随意|严肃|书面|官方)$"
)


def _extract_target(rule: str, prefix: str) -> Optional[str]:
    """从规则文本里抠出 prefix 之后的目标字符串。

    支持三种写法：带中文引号、带英文引号、不带引号（取到逗号/句号止）。
    返回 None 表示抓不出来，调用方应跳过这条规则。
    """
    if prefix not in rule:
        return None
    after = rule.split(prefix, 1)[1].lstrip(" :：")
    if not after:
        return None
    # 去掉常见的连接动词，让 "不要出现 X" / "禁止使用 X" 等也能正确抠出 X
    after = re.sub(r"^(出现|使用|用|带|含有|包含|有|说)\s*", "", after)
    # 优先抓中文引号
    m = re.search(r"['\"‘’“”「」『』]([^'\"‘’“”「」『』]{1,20})['\"‘’“”「」『』]", after)
    if m:
        return m.group(1).strip()
    # 否则取到下一个标点或行末（限 ≤ 12 字，避免抓到一整句话）
    m = re.match(r"([^，。,.\n;；]{1,12})", after)
    if m:
        # R-041: 剥掉边缘残留的引号字符 —— 配对引号在上面已处理, 落到这里
        # 的单边引号(如 "'最'字" 截断后)是噪音, 留着会让字面匹配永不命中。
        target = m.group(1).strip().strip("'\"‘’“”「」『』")
        # 排除明显的副词/介词残留（如"任何"、"过多"）
        if not target or target.startswith(("任何", "过多", "太多", "一些")):
            return None
        # R-041: 程度副词开头的目标是"性质描述"不是字面词("标题不要太长"
        # 抠出"太长"后, 文案里出现这两个字就误报违规)。放弃机械化, 留给
        # LLM 复检。
        if _PROPERTY_TARGET.match(target):
            return None
        return target
    return None


def _spec_from_structured(rule: dict) -> Optional[dict]:
    """从结构化字段读 spec（Day 3）。

    如果规则带了 ``rule_kind`` 且不是 ``free_text``，直接用 ``rule_payload``
    构造可执行 spec，跳过下面的正则抽取——这是用户在 UI 里"填表"录入
    的规则，可判定性最高。

    返回 None 表示这条规则没有结构化数据，调用方应回退到 ``_parse_rule``。
    返回 ``NOT_APPLICABLE`` 表示有结构化数据、但这里查不了(只管话题 / 评论, 或者写作台的 check 坏了):
    跳过, **不许**退回 ``_parse_rule`` —— 见 ``_spec_from_desk``。
    """
    kind = (rule.get("rule_kind") or "").strip()
    if not kind:
        return None
    payload = rule.get("rule_payload") or {}
    if isinstance(payload, str):
        # 老部署可能把 JSONB 当字符串存了，兜底解析一下
        try:
            import json as _json
            payload = _json.loads(payload)
        except Exception:
            payload = {}
    if kind == "free_text":
        # 写作台 record_rule 的 check={"kind": "free_text"} 是"撤掉这条的代码检查", 存成 free_text + {"code_check": "off"}
        # (deskcore.rulecheck.WITHDRAWN): 跳过, 不许从原文再抽回来(codex review on aw#115)。没带这个标记的 free_text
        # 是 AW 录规则界面的"自由文本", 照旧退回 _parse_rule。
        if isinstance(payload, dict) and payload.get("code_check") == "off":
            return NOT_APPLICABLE
        return None
    if isinstance(payload, dict) and _DESK_KEYS & set(payload):
        return _spec_from_desk(kind, payload)
    if kind in ("forbidden_word", "required_phrase"):
        # 老形状 {"target": X}(AW 的 Streamlit 界面还在这么写): 照写作台的口径查, 两边结论一样(codex review on aw#115)。写作台
        # (rulecheck._from_legacy)把它读成 {"words": [X]} / {"any": [X]}: 标题、正文分开看, 不分大小写, 英文字母数字那头按词边界
        # (contains_term)。原来这里是在"标题\n正文"里分大小写找子串, "made" 算出现了 "ad"、"an ad" 又不算出现了 "AD"。
        # 只剩一处不一样: 超过 40 字的 X 写作台不收(rulecheck.MAX_TERM_CHARS, 跳过这条), 这里照查。
        target = str(payload.get("target", "")).strip()
        return {"kind": kind, "terms": [target], "where": list(_FIELDS)} if target else None
    if kind == "max_len":
        scope = str(payload.get("scope", "标题")).strip() or "标题"
        try:
            n = int(payload.get("n", 0))
        except (TypeError, ValueError):
            n = 0
        return {"kind": kind, "scope": scope, "n": n} if n > 0 else None
    if kind == "forbidden_regex":
        pattern = str(payload.get("pattern", "")).strip()
        return {"kind": kind, "pattern": pattern} if pattern else None
    return None


# ── 写作台写的形状(deskcore/rulecheck.py, 2026-10-20) ────────────────────────
# 同一张 memories 表, 写作台 record_rule 带 check 时存的是 {"words": [...], "where": [...]} /
# {"any": [...]} / {"max": N, "min": M, "where": "body"} / {"pattern": P, "where": [...]}。where 可以只管
# 话题(tags)或评论(comments), 这里只看得到标题和正文。
# ⚠️ 读不了也【不许】退回 _parse_rule(codex review on aw#115): content 是写给人看的原话, 「评论不要出现「私信」」
# 会被抽成"标题正文不许出现私信", 生成的稿子全标 needs_revision; 只管标题的禁用词也会被抽成连正文一起查。
# 所以要么照写作台的口径读对(下面的匹配 / 数字数和 rulecheck 共用), 要么返回 NOT_APPLICABLE 跳过。
# 必须带的话术只管评论 / 话题(或者标题正文之外还管它们)的, 这里一律跳过: 写作台那边模板要求每篇都带评论 / 话题时, 空着算没带
# (rulecheck.check 的 expects, codex review on aw#115), 可生成链路生成的东西里本来就没有评论, 话题是另算的关键词, 判不了。
NOT_APPLICABLE = {"kind": "not_applicable"}
_DESK_KEYS = frozenset({"words", "any", "max", "min", "where"})
_FIELDS = ("title", "body")
_ALNUM = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")

# ── 哪个 # 是话题: 数字数(这里和 rulecheck)、交付稿摘话题、查话题的规则(deskcore/delivery.py)共用这一处 ──────────────
# 基本形: "#秋招" / "#秋招[话题]#" / 连写 "#a#b" / "#a[话题]#b[话题]#"(中间那个 # 收前一个也开后一个, 所以 [话题] 后面
# 的 # 只有在后面不是又一个 "x[话题]" 时才当收尾吃掉; 吃掉了就只是收尾, 后面跟着的是正文)。两种 # 不是话题(codex review
# on aw#115):
#   · 网址里的 #片段: 「详情见 https://example.com/#faq」—— 交付稿里它是网址的一部分, 不进话题行, 也不能从字数里扣掉;
#   · 前一个字是英文字母数字: 「学习C#入门」「1#楼」是一个词。两种除外: 紧接着上一个话题的连写("#abc#def" 第二个
#     # 前面是 c, 但它正好从 #abc 收住的地方开始), 和带 [话题] 的("拿到offer#秋招[话题]#": 小红书的话题写法, 不会是一个词
#     的一部分)。连写只看是不是紧挨着上一个话题, 不往回找 #: 中文句子里没有空白, 往回找会一路找到前面 "#秋招[话题]#" 收尾的
#     那个 #, 隔着一整句的 "学C#入门" 也被认成连写。
#   行首、空白、中文 / 全角字、emoji、标点、] 和 # 后面的 # 都可以开一个话题。
# ⚠️ deskcore.tvlink._TAG_RE 还是老的基本形, 和这里两头不一样: 网址里的 #片段、"C#入门" 的 #入门 它也剥(比这里多);
# 连写的 "#a[话题]#b[话题]#" 它只剥前一个, "b[话题]#" 留在文本里(比这里少)。它只在对内容 / 算改稿差异之前剥标签, 这次
# 不动(理由见那边)。
HASHTAG = re.compile(r"#[^\s#\[\]]+(?:\[话题\](?:#(?![^\s#\[\]]+\[话题\]))?)?")
# 网址: # 前面紧挨着、一直是网址字符(不带空白的 ASCII)的那一段带 ://、带 www.、或者 域名/。只往前看到最近的空白不够: 中文
# 句子里没有空白, 句子前面随便有个网址、甚至 Node.js/ 这种技术栈写法, 后面的话题就都不算了("学了Node.js/Express，面试稳了！#秋招")。
_URL_HINT = re.compile(r"://|www\.|[A-Za-z0-9-]\.[A-Za-z]{2,}(?::\d+)?/", re.I)
_URL_CHARS = "".join(chr(c) for c in range(0x21, 0x7F))


def _hashes_in_urls(text: str) -> dict[int, bool]:
    """每个 # 是不是网址的一部分(紧挨在它前面、一直是网址字符的那一段像网址), 一趟扫完。网址特征(://、www.、域名/)里没有 #,
    所以两个 # 之间那一段单独查一次、再接上前一个 # 的结论就够了 —— 每个 # 都往回切一遍的话, 一长串 "#a#a#a…" 是平方级的。"""
    out: dict[int, bool] = {}
    prev, hint = -1, False
    at = text.find("#")
    while at >= 0:
        seg = text[prev + 1:at]
        tail = seg[len(seg.rstrip(_URL_CHARS)):]
        if prev >= 0 and len(tail) == len(seg):        # 中间一直是网址字符: 和前一个 # 是同一段
            hint = hint or _URL_HINT.search(seg) is not None
        else:
            hint = _URL_HINT.search(tail) is not None
        out[at] = hint
        prev, at = at, text.find("#", at + 1)
    return out


def topic_tag_matches(text: str) -> list[re.Match]:
    """文本里真算话题的那几个(``HASHTAG`` 的匹配, 按出现顺序) —— 唯一的那一个判据(怎么认见上面)。数字数、正文里找话题、
    摘末尾话题都从这里拿。一趟扫完: 网址那条先一趟算好, 连写只比上一个认下的话题在哪儿收住。"""
    text = text or ""
    in_url, out, end = _hashes_in_urls(text), [], -1
    for m in HASHTAG.finditer(text):
        at = m.start()
        if in_url.get(at, False):
            continue
        prev = text[at - 1] if at > 0 else ""
        if prev.isascii() and prev.isalnum() and at != end and "[话题]" not in m.group(0):
            continue
        out.append(m)
        end = m.end()
    return out


def topic_tags(text: str) -> list[str]:
    """文本里真算话题的那几个, 原样(带 # 和 [话题])。"""
    return [m.group(0) for m in topic_tag_matches(text)]


def is_topic_tag(text: str, at: int) -> bool:
    """``text[at]`` 那个 # 是不是一个话题的开头(就是 topic_tag_matches 里有没有从这儿开始的)。"""
    return any(m.start() == at for m in topic_tag_matches(text))


def contains_term(text: str, term: str) -> bool:
    """不分大小写的包含。词的开头 / 结尾是英文字母或数字的, 那一头按词边界认, 免得 "ad" 撞进 "made"。写作台(rulecheck)查词也用这一个。

    只看词两头那一个字, 不看整个词是不是纯英文(codex review on aw#115): "www." 的结尾是点, 后面紧跟着 example 是正常写法, 无条件
    加尾边界的话 "www.example.com" 里永远找不到 "www.", 禁用词漏报、必须带的话术误报没带, ".com" 同理只看结尾; 反过来 "V信"
    "v+" "c++" "K歌" 这种开头是字母的, 开头那一头照样要边界 —— 不然「家里TV信号不好」「Apple TV+」都算出现了「V信」「v+」。
    代价是同一个道理的另一面: "Q群" 不算出现在「QQ群」里、"A货" 不算出现在「AA货」里, 两种都要拦就都列上(record_rule 说明里写了)。
    中文、标点那一头不加。"""
    t, s = term.casefold(), (text or "").casefold()
    head = r"(?<![a-z0-9])" if t[:1] in _ALNUM else ""
    tail = r"(?![a-z0-9])" if t[-1:] in _ALNUM else ""
    if not (head or tail):
        return t in s
    return re.search(head + re.escape(t) + tail, s) is not None


def count_chars(text: str) -> int:
    """写作台口径的字数: 去掉空白和 #话题 再数。写作台(rulecheck)数字数也用这一个。网址里的 #片段、C# 这种词不是话题,
    照样算字(codex review on aw#115): 交付出去它们都还在正文里, 扣掉的话字数上限会放过超长的稿, 下限会冤枉够长的稿。"""
    text, kept, pos = text or "", [], 0
    for m in topic_tag_matches(text):
        kept.append(text[pos:m.start()])
        pos = m.end()
    kept.append(text[pos:])
    return len(re.sub(r"\s+", "", "".join(kept)))


def _spec_from_desk(kind: str, payload: dict) -> dict:
    where = payload.get("where", "body" if kind == "max_len" else None)
    listed = list(_FIELDS) if where is None else \
        [where] if isinstance(where, str) else (where if isinstance(where, list) else [])
    fields = [w for w in _FIELDS if w in listed]
    # 必须带的话术写在"正文或评论里有一个就行"时, 这里看不到评论, 正文里没有也判不了没守
    if not fields or (kind == "required_phrase" and any(w not in _FIELDS for w in listed)):
        return NOT_APPLICABLE
    if kind in ("forbidden_word", "required_phrase"):
        raw = payload.get("words" if kind == "forbidden_word" else "any")
        terms = [t.strip() for t in raw if isinstance(t, str) and t.strip()] if isinstance(raw, list) else []
        return {"kind": kind, "terms": terms, "where": fields} if terms else NOT_APPLICABLE
    if kind == "forbidden_regex":
        pattern = payload.get("pattern")
        return {"kind": kind, "pattern": pattern, "where": fields} if isinstance(pattern, str) and pattern \
            else NOT_APPLICABLE
    if kind == "max_len" and len(fields) == 1:
        lo, hi = payload.get("min"), payload.get("max")
        ok = [v for v in (lo, hi) if isinstance(v, int) and not isinstance(v, bool) and v > 0]
        if ok and len(ok) == sum(v is not None for v in (lo, hi)):
            return {"kind": "desk_len", "where": fields[0], "min": lo, "max": hi}
    return NOT_APPLICABLE


def _parse_rule(rule_content: str) -> Optional[dict]:
    """把一条规则文本编译成可执行的 predicate spec。

    返回 None 表示这条规则无法被机械化（例如"语气要轻盈"这种感受性
    描述）；调用方应忽略它，让 LLM 复检兜底。

    Spec dict 形如：
      - {"kind": "forbidden_word",   "target": "最"}
      - {"kind": "required_phrase",  "target": "认证"}
      - {"kind": "max_len",          "scope": "标题", "n": 20}
    """
    if not rule_content or not rule_content.strip():
        return None
    text = rule_content.strip()

    # 长度规则优先（"标题不超过 20 字" 不会和下面的关键词模式冲突）
    m = _LEN_PATTERN.search(text)
    if m:
        # R-041 review: "控制在 N 字"必须带上界后缀(以内/之内/内)才是 max_len;
        # "控制在20字以上/左右"不是上限规则, 不可机械化 → 跳过留给 LLM 复检。
        if m.group("verb") == "控制在" and not m.group("bound"):
            m = None
    if m:
        return {"kind": "max_len", "scope": m.group("scope"), "n": int(m.group("n"))}

    # R-041: 否定/肯定前缀按**在文本中的出现位置**选择, 不再按"否定列表优先"。
    # 规则的领头动词决定意图 —— 旧实现先扫 _NEG_PREFIXES, "必须包含'不要熬夜'"
    # 会被中间的"不要"抢先命中, 整条正向硬规则被静默反转成禁用词(且永不匹配)。
    # 位置法下: "必须包含'不要熬夜'" → 必须包含@0 胜 → required_phrase ✓;
    # "禁止使用'必须包含'话术" → 禁止@0 胜 → forbidden_word ✓;
    # 复合规则("不要X,必须包含Y")仍按先出现者处理, 与旧行为一致。
    # 同位置前缀重叠(如"不能用"含"不能")取更长者, 避免连接动词被劈半。
    candidates: list[tuple[int, int, str, str]] = []  # (pos, -len, kind, prefix)
    for prefix in _NEG_PREFIXES:
        pos = text.find(prefix)
        if pos != -1:
            candidates.append((pos, -len(prefix), "forbidden_word", prefix))
    for prefix in _POS_PREFIXES:
        pos = text.find(prefix)
        if pos != -1:
            candidates.append((pos, -len(prefix), "required_phrase", prefix))
    for pos, _neg_len, kind, prefix in sorted(candidates):
        target = _extract_target(text, prefix)
        if target:
            return {"kind": kind, "target": target}

    return None


# ─────────────────────────────────────────────────────────────────────────
# 实际执行：对单条版本做检查
# ─────────────────────────────────────────────────────────────────────────

def check_hard_rules(
    hard_rules: list[dict],
    title: str,
    body: str,
    keywords: Optional[list] = None,
) -> list[dict]:
    """对一条版本逐条比对硬规则；返回违反清单。

    ``hard_rules`` 是 db.get_confirmed_memories 返回结构的子集，
    需要带 ``content`` 字段（规则文本）。

    ``keywords`` 是该版本的关键词列表（小红书 #tag），用于 max_len 的
    "关键词" scope；不传则该 scope 退化为空字符串（保持向后兼容）。

    返回列表里每个元素 ``{"rule", "kind", "match"}``：
      - ``rule``  ：原始规则文本（用于警告里告诉用户违反了什么）
      - ``kind``  ：违反类型（forbidden_word / required_phrase / max_len）
      - ``match`` ：具体命中内容（违禁词 / 缺失词 / 超长统计）
    """
    if not hard_rules:
        return []
    title = (title or "").strip()
    body  = (body  or "").strip()
    combined = title + "\n" + body
    keywords_text = " ".join(str(k) for k in (keywords or []) if k)

    out: list[dict] = []
    for rule in hard_rules:
        # 审计 COR-020: 解析用 .get("content","") 而上报却用 rule["content"] ——
        # 带 rule_kind 但没有 content 键的规则(结构化 spec 完全够用, content 只是
        # 给人看的原文)会在**命中违规的那一刻**抛 KeyError, 从生成主流程冒出去。
        # 只读一次, 后面统一用它。
        content = rule.get("content", "")
        # Day 3: 优先用结构化字段，回退到正则抽取。NOT_APPLICABLE 不回退(见 _spec_from_desk)
        spec = _spec_from_structured(rule)
        if spec is None:
            spec = _parse_rule(content)
        if not spec or spec is NOT_APPLICABLE:
            continue
        kind = spec["kind"]
        fields = [t for w, t in (("title", title), ("body", body)) if w in spec.get("where", _FIELDS)]
        if "terms" in spec:              # 写作台的词表: 照写作台的口径查(只看 where 选中的部位)
            found = [t for t in spec["terms"] if any(contains_term(x, t) for x in fields)]
            if kind == "forbidden_word" and found:
                out.append({"rule": content, "kind": kind, "match": "、".join(found)})
            elif kind == "required_phrase" and any(x.strip() for x in fields) and not found:
                out.append({"rule": content, "kind": kind, "match": "、".join(spec["terms"])})
        elif kind == "desk_len":
            n = count_chars(title if spec["where"] == "title" else body)
            if spec["max"] is not None and n > spec["max"]:
                out.append({"rule": content, "kind": "max_len", "match": f"实际 {n} 字 > 限定 {spec['max']} 字"})
            if spec["min"] is not None and n < spec["min"]:
                out.append({"rule": content, "kind": "max_len", "match": f"实际 {n} 字 < 下限 {spec['min']} 字"})
        elif kind == "forbidden_word":
            t = spec["target"]
            if t and t in combined:
                out.append({
                    "rule":  content,
                    "kind":  kind,
                    "match": t,
                })
        elif kind == "required_phrase":
            t = spec["target"]
            if t and t not in combined:
                out.append({
                    "rule":  content,
                    "kind":  kind,
                    "match": t,  # 这里 match = 缺失的词
                })
        elif kind == "max_len":
            scope = spec["scope"]
            n     = spec["n"]
            # _LEN_PATTERN 接受 5 种 scope（标题|正文|开头|结尾|关键词），所以
            # 这里映射也要补齐 5 种；之前缺「结尾」/「关键词」会让用户配的规则
            # 走到 .get("","") fallback，永远命不中，硬规则形同虚设。
            # - 结尾：取正文最后一段非空行（与「开头」对称）
            # - 关键词：keywords 列表拼接后计长（"总长度"口径）
            body_lines = [l for l in body.splitlines() if l.strip()]
            target_text = {
                "标题":   title,
                "正文":   body,
                "开头":   body_lines[0]  if body_lines else "",
                "结尾":   body_lines[-1] if body_lines else "",
                "关键词": keywords_text,
            }.get(scope, "")
            if target_text and len(target_text) > n:
                out.append({
                    "rule":  content,
                    "kind":  kind,
                    "match": f"实际 {len(target_text)} 字 > 限定 {n} 字",
                })
        elif kind == "forbidden_regex":
            pattern = spec["pattern"]
            try:
                # ⚠️ 存量规则也要过这道闸。写入侧的校验只管**以后**存进来的,
                # 库里那些是在有校验之前写下的 —— 只挡新的等于没挡。
                # 不安全就当"这条规则没法执行"处理(与编译失败同一条路径:
                # 不算违规、但埋一行日志), 而不是把生成整个卡死。
                assert_regex_is_safe(pattern)
                # 标题、正文分开搜, 同写作台(codex review on aw#115): 拼成"标题\n正文"再搜的话 ^ 碰不到正文开头、
                # $ 碰不到标题结尾, 同一行规则两边结论不一样。生产库里还没有一条结构化正则, 老形状一起改不影响存量。
                # 闸只是尽力而为: 执行限时(search_user_regex), 标题正文共用一份时限, 用完了这条就当这次没查(下面)。
                budget = RegexBudget()
                m = next(filter(None, (search_user_regex(pattern, x, budget) for x in fields)), None)
                if m:
                    out.append({
                        "rule":  content,
                        "kind":  kind,
                        "match": m.group(0)[:60],
                    })
            except RegexTimeout as exc:
                # 漏过闸、又真的慢的正则(或者限时的引擎跑不了它): 不算违规(没查完不能说它命中), 也不能卡住生成 —— 跳过并记一行,
                # 同下面的不安全。
                _logger.warning("check_hard_rules: 正则规则这次没查成(%s, 时限 %.2fs), 跳过(不算违规): %r",
                                exc, regex_search_timeout(), pattern[:80])
                try:
                    import telemetry as _tm
                    _tm.log_event("hard_rule_regex_timeout", rule=content[:80], pattern=pattern[:80], reason=str(exc)[:120])
                except Exception:
                    pass
                continue
            except (re.error, UnsafeRegex) as exc:
                # 正则编译失败**或不安全**都不算违规——但要埋一行日志，否则用户在 UI 里
                # 看到"硬规则未命中"会以为规则生效了，实际 validator 一直
                # silent skip。memory.py 的添加路径已在保存前做 re.compile
                # 校验，这里兜底覆盖"老规则 + 老部署"的情形。
                try:
                    import telemetry as _tm
                    _tm.log_event(
                        "hard_rule_regex_failed",
                        rule=content[:80],
                        pattern=pattern[:80],
                        error=str(exc)[:120],
                    )
                except Exception:
                    pass
                continue
    return out


def filter_hard(memories: list[dict]) -> list[dict]:
    """从混合的 (hard + soft) 记忆里挑出 severity='hard' 的子集。

    调用方便利：worker 一般持有完整 global+project memories，本函数
    省去重复写 list comprehension。
    """
    return [
        m for m in (memories or [])
        if (m.get("severity") or "soft").lower() == "hard"
    ]
