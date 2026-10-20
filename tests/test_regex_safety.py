"""用户写的 ``forbidden_regex`` 不许把 CPU 交出去（跨库审计 2026-08-24 SUP-011）。

实测数据（``(a+)+$`` 对全 a 的字符串）：

    18 字符 → 0.024s
    20 字符 → 0.092s
    22 字符 → 0.373s
    24 字符 → 1.491s
    26 字符 → 5.993s

每多 2 个字符翻一倍。正文上百字就是"永不返回"。

⚠️ **靠截断正文兜不住**，这是这组用例存在的理由：回溯代价是输入长度的**指数**，
截到 100 字仍然是 2^100。而 Python 的 ``re`` 没有超时，它又在 C 里跑，线程超时
也打不断。所以吃重的那一层只能是"写入时拒绝"，执行期的长度上限只是兜底。
"""

from __future__ import annotations

import time

import pytest

import validator as V


# ══════════════════════════════════════════════════════════════════════
# 1 · 危险形状要被拒
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("pattern", [
    "(a+)+$",
    "(a*)*",
    "(a+)*b",
    "(.*)*x",
    "((ab)+)+",
    "(?:\\d+)+$",
    "(a+)+" + "c",
])
def test_catastrophic_shapes_are_rejected(pattern):
    with pytest.raises(V.UnsafeRegex):
        V.assert_regex_is_safe(pattern)


@pytest.mark.parametrize("pattern", [
    "最",                       # 最常见的用法: 一个词
    "(?i)best|top",
    "第[一二三]名",
    "\\d{1,20}元",              # 有上界的重复 —— 安全
    "(ab){1,10}",
    "^标题[:：]",
    "[a-z]+@[a-z]+\\.com",      # 单层无上界, 不嵌套 —— 安全
])
def test_ordinary_patterns_still_pass(pattern):
    """别把闸修成"什么都不让过" —— 单层 + / * 是完全正常的写法。"""
    V.assert_regex_is_safe(pattern)


# ── 1b · 挨在一起、能匹配同样字符的量词(codex review on aw#115) ─────────────────────────
# 没有嵌套, 但回溯是多项式级: k 个这样的量词挨着, 匹配失败时要试 n^k 种切法。实测 a*a*a*a*a*a*a*a*b 配 30 个 a
# 1.05 秒、200 个 a 超过 3 秒; 两个就够: .*.*x 配 2000 字的一行 1.5 秒(正文执行上限是 2 万字)。

@pytest.mark.parametrize("pattern", [
    "a*a*a*a*a*a*a*a*b",        # codex 给的例子
    ".*.*x",
    "\\w+\\w+x",
    "[哈呵]*哈*[呵哈]*。",
    "a*b*a*c",                  # 中间的 b* 可以是空的, 隔不开
    "a.*a.*a.*b",               # 中间的 a 本身就能被 .* 吃掉, 也隔不开
    "(a*)a*b",                  # 组不挡
    "(?:x|a*)a*b",              # 分支里的也算
    "a{0,30}a{0,30}a{0,30}b",   # 有上界也一样, 只是乘积大
    "a?" * 14 + "a" * 14,       # 经典的 a?ⁿaⁿ
    "(?i)A*a*b",                # 不分大小写时 A 和 a 是同一个字
    # 外层量词的每一次之间也是挨着的(codex review on aw#115): 实测 (?:.{0,20}){5}x 配 120 个 y 4.8 秒,
    # {0,5} 3.4 秒; 外层无上界的 (?:\w{1,5})+x 配 26 个 a 3.2 秒、(?:.{0,20})*x 6.2 秒
    "(?:.{0,20}){5}x",
    "(?:.{0,20}){0,5}x",
    "(?:哈{0,9}){6}!",
    "(?:\\w{1,5})+x",
    "(?:.{0,20})*x",
    # 放宽(数个数 / 结尾的量词)之后仍然要拒的
    "(?:x.*x.*x)y",             # 数完了后面还接着别的字: 失败时每种选法都要试
    "!.*!.*!$",                 # 结尾的 $ 也能失败: 1000 个「!」加一个字 1.4 秒
    "\\w*\\w*\\d+",             # 结尾的量词照样要吃够一个: 3000 个 a 84 秒
    "!.{100,}!.{100,}!.{100,}!",  # 间隔有下限就不是只数个数了: 300 个「!」加 2000 字 0.5 秒
    "[!！]" + "[\\s\\S]*[!！]" * 15,   # 展开超过 11 份: 正好 15 个「!」的稿子(守住了)每多一份翻一倍, 2 万字 20 多秒
    "a.*?[!！].*[!！][!！]",    # 结尾两个 X 挨着, 要的是「连着两个」而不只是个数: 1500 个「a!」16 秒
])
def test_adjacent_overlapping_repeats_are_rejected(pattern):
    with pytest.raises(V.UnsafeRegex) as ei:
        V.assert_regex_is_safe(pattern)
    assert "挨在一起" in str(ei.value)


@pytest.mark.parametrize("pattern", [
    "a*b*c",                            # 两个量词管的字不一样
    "\\d+\\.\\d+",                        # 中间有固定的字隔开
    "[a-z]+@[a-z]+\\.com",
    "加.{0,3}微.{0,3}信",                # 有上界、乘积小
    "\\d{1,3}\\d{1,3}",
    ".*私信.*",                          # 两个字以上的固定词隔开
    "a*+a*b",                           # 占有量词不回吐
    # Jev 的规则编译器教模型写的两种(judge/rules.py 的 PROMPT_HEAD), 写作台得收
    "^\\S+\\s+\\S+\\s+\\S+\\s+\\S+\\s+\\S+\\s+\\S",
    "品牌[\\s\\S]*品牌[\\s\\S]*品牌",
    "最(好|佳|强|便宜|有效)|第一(名|品牌)",
    # 「X 最多 N 次」展开写, X 是单个字(codex review on aw#115): 只能因为 X 不够数而失败, 回溯被 X 的个数封住
    "[!！][\\s\\S]*[!！][\\s\\S]*[!！][\\s\\S]*[!！]",
    "最[\\s\\S]*最[\\s\\S]*最",
    "哦[\\s\\S]*哦[\\s\\S]*哦",
    "#[\\s\\S]*#[\\s\\S]*#[\\s\\S]*#[\\s\\S]*#[\\s\\S]*#",
    "\\n\\s*\\n\\s*\\n",                      # 最多空一行
    "[!！]" + "[\\s\\S]*[!！]" * 10,          # 最多 10 个: 展开 11 份是上限
    # 整条正则最后一个量词: 吃够下限就匹配成功, 不会再回头切(codex review on aw#115)
    "\\d+\\s*\\d+",
    "[!！]+\\s*[!！]+",
    "原价.*\\d+",
    ".*[!！]{2,}",
    # 外层量词展开之后仍然放行的
    "(?:\\S+\\s+){5}\\S+",
    "(?:哈){5}",
    "(?:哈哈)+",
    "哈{5,}",
    "(?:\\d{1,3},)+\\d{3}",
])
def test_separated_or_disjoint_repeats_still_pass(pattern):
    V.assert_regex_is_safe(pattern)


# 上面放宽的那几种, 闸放行就得真的快: 各配几种最坏的正文(2 万字, 即执行上限)掐表
_WORST_TEXTS = {
    "count": ["!" * 3 + "a" * 20_000, "！" * 20_000, "最" * 2 + "\n" * 20_000, "#" * 5 + " " * 20_000,
              "\n" + " " * 20_000, "\n \n a" * 3000, "哦" * 2 + "x" * 20_000],
    "tail": ["1" * 20_000, "1 " * 10_000, "1" + " " * 20_000 + "a", "!" * 20_000, "! " * 10_000, "a!" * 10_000],
}


@pytest.mark.parametrize("pattern, texts", [
    ("[!！][\\s\\S]*[!！][\\s\\S]*[!！][\\s\\S]*[!！]", "count"),
    ("最[\\s\\S]*最[\\s\\S]*最", "count"),
    ("哦[\\s\\S]*哦[\\s\\S]*哦", "count"),
    ("#[\\s\\S]*#[\\s\\S]*#[\\s\\S]*#[\\s\\S]*#[\\s\\S]*#", "count"),
    ("\\n\\s*\\n\\s*\\n", "count"),
    ("\\d+\\s*\\d+", "tail"),
    ("[!！]+\\s*[!！]+", "tail"),
])
def test_what_the_relaxed_rules_let_through_is_actually_fast(pattern, texts):
    V.assert_regex_is_safe(pattern)
    for text in _WORST_TEXTS[texts]:
        t0 = time.perf_counter()
        V.re.search(pattern, text[:V.MAX_REGEX_TARGET_CHARS])
        assert time.perf_counter() - t0 < 0.2, (pattern, text[:20])


def test_deeply_nested_fixed_counts_do_not_hang_the_check_itself():
    """外层定次量词要展开好几份才看得出跨份的切法(codex review on aw#115), 套几层就是几次方 —— 闸自己不能被一条短正则拖住。"""
    t0 = time.perf_counter()
    with pytest.raises(V.UnsafeRegex) as ei:
        V.assert_regex_is_safe("(?:(?:(?:(?:(?:a{16}){16}){16}){16}){16})")
    assert time.perf_counter() - t0 < 1
    assert "太复杂" in str(ei.value)


@pytest.mark.parametrize("pattern, ch", [
    ("(?i:a*)A*B", "A"),                    # codex 给的例子: 组里的 (?i) 只管组里, 但 a 和 A 照样是同一个字, 2000 个 A 实测 2 秒
    ("a*(?i:A)*b", "a"),
    ("(?a:\\W*)é*x", "é"),                   # (?a) 下 \W 包括 é, 3000 个 é 实测 7 秒
    ("(?a)\\W*é*x", "é"),                    # 开头的 (?a) 也得认
    ("(?s:.*)\\n*x", "\n"),                  # (?s) 下 . 也吃换行
])
def test_scoped_flags_are_honored(pattern, ch):
    """组里的旗 (?i:…) (?s:…) (?a:…) 要按组里的算, 不能一直拿整条的旗比字集(codex review on aw#115)。"""
    t0 = time.perf_counter()
    V.re.search(pattern, ch * 1000)
    assert time.perf_counter() - t0 > 0.05, "这条的例子本身得是慢的, 否则测不出东西"
    with pytest.raises(V.UnsafeRegex) as ei:
        V.assert_regex_is_safe(pattern)
    assert "挨在一起" in str(ei.value)


@pytest.mark.parametrize("pattern, ch", [
    ("(?i)[A-Z]*(?-i:a*)x", "a"),           # (?i)[A-Z] 也吃 a: 只往小写折的话 [A-Z] 不认 a, 有了 (?-i:…) 反而放行了
    ("(?i)[A-Z]+(?-i:[a-z]+)\\d", "a"),
    ("(?i:[A-Z]+)\\s*[a-z]+\\d", "a"),
    ("[a-z]*(?i:[A-Z]*)x", "a"),
    ("(?i)[À-Þ]*é*x", "é"),                 # É 在 À-Þ 里
    ("(?i)ß*ẞ*x", "ß"),                     # ẞ 的小写是 ß, casefold 却是 ss
    ("(?i)İ*i*x", "i"),                     # sre 的小写表里 İ → i
    ("\\W*[À-Þ]*x", "×"),                  # × 夹在 À-Þ 中间, 两头的 À Þ 都不是 \W
])
def test_case_insensitive_sets_are_compared_the_way_re_matches(pattern, ch):
    """(?i) 下哪些字算在字集里交给 re 自己判(codex review on aw#115): 手写的只往小写折, (?i)[A-Z] 不认 a、ẞ 和 ß 对不上。
    1000 个字实测 0.4~2.5 秒; 这里配 700 个, 省点时间。"""
    t0 = time.perf_counter()
    V.re.search(pattern, ch * 700)
    assert time.perf_counter() - t0 > 0.05, "这条的例子本身得是慢的, 否则测不出东西"
    with pytest.raises(V.UnsafeRegex) as ei:
        V.assert_regex_is_safe(pattern)
    assert "挨在一起" in str(ei.value)


@pytest.mark.parametrize("pattern", ["()(?(1)(a+)+b)", "()(?(1)a*a*a*a*a*b)", "(a)?(?(1)x|.*.*y)"])
def test_conditional_groups_are_rejected(pattern):
    """条件组 (?(1)是|否) 两道闸都不往里走, 里面放什么都看不见(codex review on aw#115): ()(?(1)(a+)+b) 22 个 a 0.4 秒、
    每多 2 个翻一倍。写作台(rulecheck)本来就不收; memory.py 存规则、check_hard_rules 执行期只靠这一道, 一并不收。"""
    with pytest.raises(V.UnsafeRegex) as ei:
        V.assert_regex_is_safe(pattern)
    assert "条件组" in str(ei.value)


@pytest.mark.parametrize("pattern", [
    "(?i)(?-i:a*)(?-i:A*)B",                # 组里关掉了 (?i): {a} 和 {A} 不相交
    "(?a)(?u:\\W*)é*x",                      # 组里换回 Unicode: \W 不含 é
    ".*\\n*x",                               # 没有 (?s): . 不吃换行
])
def test_scoped_flags_that_narrow_the_set_still_pass(pattern):
    V.assert_regex_is_safe(pattern)


# ── 1c · 前瞻、占有量词、原子组都不是隔断(codex review on aw#115) ─────────────────────────────────────────────
# 前瞻 (?=…) (?!…) 不吃字, 但前面开着的量词每换一种切法, 它就在那个位置重查一遍: 里面的量词和前面的照样挨着。占有量词 / 原子组
# 不回吐, 可它们能吃空(.*a*+.*x 前后两个 .* 还是挨着), 吃的又是前面那个也能吃的字的话, 前面每换一种切法它就从头吃一遍。

@pytest.mark.parametrize("pattern, ch", [
    ("\\d+(?=\\d*元)", "1"),          # 2000 位实测 4~6 秒
    (".*(?=.*x)", "a"),
    ("\\w*(?=\\w*x)", "a"),
    ("\\w*(?!\\w*x)y", "a"),          # 负向前瞻一样
    ("\\d+(?=\\d*)x", "1"),           # 前瞻里结尾的量词: 前瞻后面还有东西, 每次都吃到底, 不只当下限
    ("\\w*(?!\\w*)", "a"),            # 负向前瞻里头成了就是失败、要回头: 整条最后一项也不只当下限
    (".*a*+.*x", "b"),                # 占有量词吃空: 前后两个 .* 还是挨着
    (".*(?>a*).*x", "b"),
    (".*a++.*x", "a"),                # 吃的是 .* 也能吃的字: .* 每换一种切法它从头吃一遍
    ("\\d+(?>x?)\\d+y", "1"),         # 2000 位实测 4~5 秒
    ("\\w*(?>a?)\\w*!", "b"),         # 2000 字实测 7~8 秒
    ("\\d+\\d++x", "1"),
    ("\\d+(?<=\\d(?=\\d*x))", "1"),   # 后顾定长, 可它里面的前瞻照样往后扫: 2000 位 4 秒
    ("\\d+(?<=(?=\\d*x)\\d)", "1"),
])
def test_lookaheads_possessives_and_atomic_groups_do_not_separate(pattern, ch):
    """1000 个字实测 0.15~1 秒(2000 个字 1~8 秒)。"""
    t0 = time.perf_counter()
    V.re.search(pattern, ch * 1000)
    assert time.perf_counter() - t0 > 0.05, "这条的例子本身得是慢的, 否则测不出东西"
    with pytest.raises(V.UnsafeRegex) as ei:
        V.assert_regex_is_safe(pattern)
    assert "挨在一起" in str(ei.value)


@pytest.mark.parametrize("pattern, texts", [
    ("\\d+(?=元)", ["1" * 2000]),
    ("\\d+(?=\\s*元)", ["1" * 2000, "1" + " " * 2000]),
    ("(?=.*品牌).*折扣", ["a" * 2000, "品牌" * 1000]),
    ("(?=.*A)(?=.*B).*C", ["A" * 2000, "AB" * 1000]),
    ("\\d+(?!\\d)", ["1" * 2000]),
    ("\\w+(?=\\s)", ["a" * 2000]),
    ("(?<!\\d)\\d{11}(?!\\d)", ["1" * 2000]),
    ("[^\\n]*(?=\\n)", ["a" * 2000]),
    ("\\d+(?<=\\d)x", ["1" * 2000]),            # 后顾只能定长
    ("\\d+(?=\\d*)", ["1" * 2000]),             # 整条最后一项是正向前瞻: 它成了整条就成了, 里面结尾的量词只当下限
    ("^\\S+(?:\\s+\\S+){3}", ["a" * 2000, "a " * 1000]),
    ("\\d++\\.\\d+", ["1" * 2000]),             # 占有量词前面没有开着的: 和单个 \d+ 一样
    ("(?>\\w+)\\s+\\w+", ["a" * 2000]),
    ("a*+a*b", ["a" * 2000]),                   # 2 万个 a 0.16 秒, 比单个 a*b(0.28 秒)还快
    (".*\\d++", ["a" * 2000, "1" * 2000]),     # 整条最后一项: 成了整条就成了, 和不占有的一样
], ids=lambda v: v if isinstance(v, str) else None)
def test_lookaheads_possessives_and_atomic_groups_that_stay_allowed_are_fast(pattern, texts):
    """放行的照样得真的快: 上面拒的那几条 2000 字都要 1~8 秒, 这些和单个量词一个量级(2000 字几毫秒)。"""
    V.assert_regex_is_safe(pattern)
    for text in texts:
        t0 = time.perf_counter()
        V.re.search(pattern, text)
        assert time.perf_counter() - t0 < 0.2, (pattern, text[:20])


def test_the_check_computes_each_char_set_once(monkeypatch):
    """前瞻 / 原子组 / 占有量词里那一段接着前面开着的查, 走到的每个单字节点都要和开着的比字集。每次都新建一个字集、两百多个
    探针字各跑一遍 fullmatch 的话, 33 个字的 .*(?=(?:(?:(?:a\\d){16}){16}){10}) 闸自己就要 0.4~0.6 秒 —— 而
    check_hard_rules 每次生成、每条规则都重跑一遍闸(codex review on aw#115)。同一个节点(连同生效的旗)一次检查里只算一次。"""
    calls, real = [], V._char_test
    monkeypatch.setattr(V, "_char_test", lambda *a: calls.append(repr(a)) or real(*a))
    for pattern in (".*(?=(?:(?:(?:a\\d){16}){16}){10})", ".*(?!(?:(?:(?:a\\d){16}){16}){10})x",
                    ".*(?>(?:(?:(?:a\\d){16}){16}){10})x"):
        calls.clear()
        t0 = time.perf_counter()
        V.assert_regex_is_safe(pattern)
        assert time.perf_counter() - t0 < 0.2, pattern
        assert len(calls) == len(set(calls)), (pattern, len(calls), len(set(calls)))


# ── 1d · 可选 / 次数可变的组里的量词, 和组外面的照样挨着(codex review on aw#115) ─────────────────────────────────
# (?:…)? / (?:…){m,n} 原来当一整块: 字集算「什么字都能吃」, 组里的量词只自己查, 和组前面开着的从来不比, 组里结尾还开着的也
# 不往后传。价格、词数这种写法很容易写出来: \d+(?:[,，]?\d+)?元 2000 位 19 秒; 一段没有「！」的中文配 \S+(?:\s*\S+)?！
# 2000 字 20 秒以上。前瞻里套一层可选组(\d+(?=\s*(?:\d+)?元))、组里放占有量词 / 原子组 / 前瞻, 前面那几道也一样被绕过去。

@pytest.mark.parametrize("pattern, ch, n", [
    ("\\d+(?:[,，]?\\d+)?元", "1", 500),         # 1000 位 2 秒, 2000 位 19 秒
    ("\\S+(?:\\s*\\S+)?！", "中", 500),           # 1000 字 3 秒
    ("\\d+(?:\\s*\\d+)?元", "1", 500),
    ("(?:\\d+\\s*)?\\d+元", "1", 1000),          # 组里结尾还开着的 \d+ 接着往后传, 和后面的 \d+ 挨着
    ("(?:\\w+\\s*)?\\w+！", "中", 1000),
    ("\\d+(?=\\s*(?:\\d+)?元)", "1", 500),       # 前瞻里的可选组
    ("\\d+(?=(?:\\s*\\d+)?元)", "1", 500),
    ("\\d+(?:\\d++)?x", "1", 1000),              # 组里的占有量词、原子组、前瞻也一样
    ("\\d+(?:(?>\\d*))?x", "1", 1000),
    ("\\d+(?:(?=\\d*x))?y", "1", 1000),
    ("\\d*(?:a?b?){1,3}\\d*x", "1", 1000),        # 组能一个字都不吃: 下限 ≥ 1 也断不开组前后的两个 \d*
    ("\\d*(?:a|)+\\d*x", "1", 1000),
    ("\\d*(?:a?b?)++\\d*x", "1", 1000),
])
def test_repeats_inside_optional_or_counted_groups_still_meet_their_neighbours(pattern, ch, n):
    t0 = time.perf_counter()
    V.re.search(pattern, ch * n)
    assert time.perf_counter() - t0 > 0.05, "这条的例子本身得是慢的, 否则测不出东西"
    with pytest.raises(V.UnsafeRegex) as ei:
        V.assert_regex_is_safe(pattern)
    assert "挨在一起" in str(ei.value)


@pytest.mark.parametrize("pattern, texts", [
    # 组的字集照实算(组里有量词原来也当「什么字都能吃」): 前瞻里的 \s* 吃不了组里的 . 和数字, 不算挨着。2000 位 0.08 秒、2 万位
    # 8.5 秒, 和放行的 \d+(?=\s*元)(2 万位 4.7 秒)、\d+(?:\.\d+)?(?=元)(6.8 秒)一样, 是每个起点各试一遍的平方级, 不是挨着的量词
    ("\\d+(?:\\.\\d+)?(?=\\s*元)", ["1" * 1000, "1." * 1000, "1" + " " * 2000, "1.1" * 700]),
    ("\\d+(?:\\.\\d+)?(?=\\s*[元块])", ["1" * 1000, "1." * 1000, "1" + " " * 2000]),
    ("\\d+(?:\\.\\d+)?\\s*[元块]", ["1" * 1000, "1." * 1000, "1" + " " * 2000, "1.1" * 700]),
    ("\\d+(?:\\.\\d+)?元", ["1" * 1000, "1." * 1000]),
    ("[\\u4e00-\\u9fff]+(?:[a-z]+)?！", ["中" * 1000, "中a" * 500]),
], ids=lambda v: v if isinstance(v, str) else None)
def test_optional_groups_that_stay_allowed_are_fast(pattern, texts):
    V.assert_regex_is_safe(pattern)
    for text in texts:
        t0 = time.perf_counter()
        V.re.search(pattern, text)
        assert time.perf_counter() - t0 < 0.2, (pattern, text[:20])


def test_the_codex_example_no_longer_runs():
    """存量里要是已经有这一条(手写进库的), 执行期也当场跳过, 不卡生成。"""
    rule = {"content": "x", "rule_kind": "forbidden_regex", "rule_payload": {"pattern": "a*a*a*a*a*a*a*a*b"}}
    # 先空跑一次: 第一次调用要起 logger 等(本机约 0.3 秒), 不该算进"有没有真去跑这条正则"里 —— 真跑 40 个 a 是几十秒
    V.check_hard_rules(hard_rules=[{"content": "x", "rule_kind": "forbidden_regex", "rule_payload": {"pattern": "b"}}],
                       title="标题", body="a")
    t0 = time.perf_counter()
    assert V.check_hard_rules(hard_rules=[rule], title="标题", body="a" * 40) == []
    assert time.perf_counter() - t0 < 0.5


def test_uncompilable_is_also_rejected():
    with pytest.raises(V.UnsafeRegex):
        V.assert_regex_is_safe("(unclosed")


def test_the_error_message_tells_you_what_to_do():
    """报错要能直接指导改法, 否则用户只会把规则删掉了事。"""
    with pytest.raises(V.UnsafeRegex) as ei:
        V.assert_regex_is_safe("(a+)+$")
    msg = str(ei.value)
    assert "回溯" in msg and "{1,20}" in msg, msg


# ══════════════════════════════════════════════════════════════════════
# 2 · 存量规则也得挡住（写入侧只管以后）
# ══════════════════════════════════════════════════════════════════════

def test_a_dangerous_legacy_rule_does_not_hang_generation():
    """库里已经存着的危险规则, 执行期必须**当场跳过**而不是卡住。

    这条是真的掐表: 危险正则 + 一段普通长度的正文, 修之前会跑到天荒地老。
    2 秒的上限已经比"26 个字符 6 秒"宽松得多。
    """
    rule = {"content": "禁止连续 a", "rule_kind": "forbidden_regex",
            "rule_payload": {"pattern": "(a+)+$"}}
    body = "a" * 40 + "!"          # 40 个字符 —— 修之前是分钟级

    t0 = time.perf_counter()
    hits = V.check_hard_rules(hard_rules=[rule], title="标题", body=body)
    dt = time.perf_counter() - t0

    assert dt < 2.0, f"危险的存量规则还是跑进 re.search 了, 花了 {dt:.1f}s"
    assert hits == [], "不能执行的规则不该报成违规 —— 那是另一种误伤"


def test_a_safe_rule_still_matches():
    """把危险的挡掉之后, 正常的规则要照旧命中。"""
    rule = {"content": "禁止出现「最」", "rule_kind": "forbidden_regex",
            "rule_payload": {"pattern": "最"}}
    hits = V.check_hard_rules(hard_rules=[rule], title="最强攻略", body="正文")
    assert len(hits) == 1 and hits[0]["kind"] == "forbidden_regex", hits


# ══════════════════════════════════════════════════════════════════════
# 3 · 检测手段本身也要被检查
# ══════════════════════════════════════════════════════════════════════

def test_falls_back_conservatively_when_the_parser_is_unavailable(monkeypatch):
    """拿不到 ``re`` 的解析树时, 必须走保守兜底而不是直接放行。

    用的是私有 API(3.11 起 ``re._parser``, 更早是 ``sre_parse``)。哪天它没了,
    正确的降级是"宁可误伤", 不是"当作检查过了"。
    """
    monkeypatch.setattr(V, "_parse_pattern", lambda p: None)
    with pytest.raises(V.UnsafeRegex):
        V.assert_regex_is_safe("(a+)+$")
    V.assert_regex_is_safe("最")           # 兜底也不能把正常的一起误伤


def test_the_parser_is_actually_available_on_this_python():
    """兜底存在不等于可以一直靠兜底 —— 主路径断了要有人知道。

    这条不是断言实现细节, 是断言"精确那一路现在还活着"。它红了就说明检测
    退化成了文本启发式(会漏 ``(?:\\d+)+`` 这种不带括号量词形态的写法)。
    """
    assert V._parse_pattern("(a+)+$") is not None, (
        "re 的解析器拿不到了 —— 检测已降级为文本启发式, 该找个正经方案了")


# ══════════════════════════════════════════════════════════════════════
# 4 · 执行期限时: 闸是尽力而为的, 漏过去的最多花一份时限(codex review on aw#115 第四轮)
# ══════════════════════════════════════════════════════════════════════
# 闸放行、执行又慢的几种(闸的放行 / 拒绝不改, Jev 的 /compile_rules 镜像它): codex 给的 a*aaa*…b(两个字的字面量隔不开前后的
# a*) 50 个 a 就 6 秒, 2000 个 a 是永不返回; \d+(?=\s*元) 这类每个起点各试一遍的平方级, 2 万位(执行上限)5~10 秒。

_SLIPS_PAST_THE_GATE = [
    ("a*aaa*aaa*aaa*aaa*aaa*aaa*aaa*b", "a" * 2000),
    ("\\d+(?=\\s*元)", "1" * 20_000),
    ("\\d+(?:\\.\\d+)?(?=\\s*元)", "1" * 20_000),
]
_TIMEOUT = 0.1


@pytest.fixture
def short_timeout(monkeypatch):
    import config
    monkeypatch.setattr(config, "REGEX_SEARCH_TIMEOUT_SEC", _TIMEOUT)
    return _TIMEOUT


@pytest.mark.parametrize("pattern, text", _SLIPS_PAST_THE_GATE, ids=lambda v: v[:12] if isinstance(v, str) else None)
def test_a_slow_pattern_that_slips_past_the_gate_is_cut_off_at_execution(short_timeout, pattern, text):
    from deskcore import rulecheck
    assert V.regex_is_safe(pattern), "前提: 闸放行它(闸不改)"
    rulecheck.normalize_check({"kind": "forbidden_regex", "pattern": pattern})
    V.search_user_regex(pattern, "x")              # 先编译好(第一次的编译也从这一份里扣, 这里只量搜的)
    t0 = time.perf_counter()
    with pytest.raises(V.RegexTimeout):
        V.search_user_regex(pattern, text)
    assert time.perf_counter() - t0 < 10 * short_timeout


@pytest.mark.parametrize("pattern, text", _SLIPS_PAST_THE_GATE, ids=lambda v: v[:12] if isinstance(v, str) else None)
def test_check_hard_rules_skips_a_rule_that_runs_out_of_time(short_timeout, pattern, text):
    """生成链路: 标题、正文共用一份时限, 用完了这条跳过(不算违规), 记一行日志; 同一批里正常的规则照旧命中。"""
    slow = {"content": "示例慢规则", "rule_kind": "forbidden_regex", "rule_payload": {"pattern": pattern}}
    fine = {"content": "示例禁词", "rule_kind": "forbidden_regex", "rule_payload": {"pattern": "示例禁词"}}
    V.check_hard_rules(hard_rules=[slow], title="t", body="b")
    t0 = time.perf_counter()
    hits = V.check_hard_rules(hard_rules=[slow, fine], title="示例禁词", body=text)
    assert time.perf_counter() - t0 < 10 * short_timeout
    assert [h["rule"] for h in hits] == ["示例禁词"], "没查完的不能报成违规"


def test_the_budget_is_shared_by_every_search_of_a_rule(short_timeout):
    """一份时限是一条规则在一次检查里共用的: 每次都差一点到点的话, 一批 20 篇加起来就远不止一份了。用完了再搜直接抛, 不再跑。"""
    budget = V.RegexBudget()
    with pytest.raises(V.RegexTimeout):
        V.search_user_regex("a*aaa*aaa*aaa*aaa*aaa*aaa*aaa*b", "a" * 2000, budget)
    assert budget.timed_out and budget.left <= 0
    t0 = time.perf_counter()
    with pytest.raises(V.RegexTimeout):
        V.search_user_regex("最", "最", budget)
    assert time.perf_counter() - t0 < 0.01


@pytest.mark.parametrize("pattern", [
    "a*aaa*aaa*aaa*aaa*aaa*aaa*aaa*b",
    # 后半截 re 当字面的 {e}, regex 认成模糊匹配、编译不了: 原来就退回没有时限的 re, 整条绕过了时限(codex review on aw#115 第五轮)
    "a*aaa*aaa*aaa*aaa*aaa*aaa*aaa*b|示例{1,2}{e}",
], ids=["codex-example", "fuzzy-brace-bypass"])
def test_the_codex_example_returns_through_both_paths_in_a_fresh_process(pattern):
    """真跑一遍(单独的进程, 卡住了也能收尾): 改之前 rulecheck.check 和 check_hard_rules 配 2000 个 a 都是永不返回。"""
    import json
    import os
    import pathlib
    import subprocess
    import sys
    import textwrap
    code = textwrap.dedent("""
        import json, sys, time, validator
        from deskcore import rulecheck
        rule = {"id": "m-1", "content": "示例", "severity": "hard", "rule_kind": "forbidden_regex",
                "rule_payload": {"pattern": sys.argv[1]}}
        validator.check_hard_rules([rule], "t", "b")
        t0 = time.monotonic()
        gen = validator.check_hard_rules([rule], "a" * 2000, "a" * 2000)
        t1 = time.monotonic()
        hits, stats = rulecheck.check([rule], [{"title": "a" * 2000, "body": "a" * 2000}] * 20)
        t2 = time.monotonic()
        print(json.dumps({"gen": gen, "gen_s": t1 - t0, "hits": hits, "stats": stats, "desk_s": t2 - t1}))
    """)
    assert V.regex_is_safe(pattern), "前提: 闸放行它(闸不改)"
    root = pathlib.Path(V.__file__).resolve().parent
    env = {**os.environ, "REGEX_SEARCH_TIMEOUT_SEC": str(_TIMEOUT)}
    try:
        p = subprocess.run([sys.executable, "-B", "-c", code, pattern], cwd=root, env=env, capture_output=True,
                           text=True, timeout=30)
    except subprocess.TimeoutExpired:
        pytest.fail("30 秒没返回: 漏过闸的正则还是直接跑进了没有时限的 re")
    assert p.returncode == 0, p.stderr[-2000:]
    got = json.loads(p.stdout.strip().splitlines()[-1])
    assert got["gen"] == [] and got["gen_s"] < 10 * _TIMEOUT
    assert got["hits"] == [] and got["stats"] == {"rules": 1, "skipped": 0, "timed_out": ["m-1"], "timed_out_from": {"m-1": 0}}
    assert got["desk_s"] < 10 * _TIMEOUT, "一条慢正则一次 check 只该花一份时限, 不是 20 篇 × 2 个部位各一份"


# ── 普通的正则一批照样查得完(codex review on aw#115 第五轮) ──────────────────────────────────────────
# 早先把 \w 逐个码位列成显式字集(七百多段、14 KB), regex 每走一步挨个比, 一个 \b 又换成四个环视: \bvx\b|\bwx\b|\bqq\b 配 20 篇
# 1500 字的稿子 0.1 秒就到点, 第 18 篇里真有的命中报成「没查」; 生成链路一篇 3000 字的稿子、8 个词的 \b 词表也是。原来的用例
# 只拿十来个字的稿子试, 看不出来。

_POST = ("这是一段示例正文，用来试规则检查的速度❤\ufe0f。今天出门散步，路上看到一家新开的小店，进去逛了一会儿，"
         "店里的东西都挺好看①②③。回家路上又买了杯咖啡，价格 18 元，味道还不错！\n\n")
_WORDS = ("ad", "ads", "promo", "dm", "vx", "wx", "qq", "tb", "jd", "pdd")
_ORDINARY = ["\\bvx\\b|\\bwx\\b|\\bqq\\b", "|".join(f"\\b{w}\\b" for w in _WORDS), "\\b(?:vx|wx|qq)\\b",
             "\\b\\d{6}\\b", "\\b\\d{3,}\\b", "\\b[A-Za-z]{2,}\\b", "\\b1[3-9]\\d{9}\\b", "\\w{2,}\\s+\\w{2,}",
             "\\w+\\s*[:：]\\s*\\d+", "[^\\w\\s]{3,}", "\\W{3,}", "(?i)\\bwechat\\b", "(?i)\\b(?:dm|pm)\\s*me\\b"]


@pytest.mark.parametrize("pattern", _ORDINARY)
def test_ordinary_rules_finish_a_normal_batch_with_the_same_hits_as_re(short_timeout, pattern):
    """20 篇 × 1500 字(比小红书正文上限还长), 违规埋在第 18 篇: 一份 0.1 秒查得完(第一次的编译也算在里面), 命中和 re 一样。"""
    import re
    from deskcore import rulecheck
    rulecheck.normalize_check({"kind": "forbidden_regex", "pattern": pattern})
    V._compiled.cache_clear()
    drafts = [{"title": f"示例标题第{i}期", "body": (_POST * 20)[:1500]} for i in range(20)]
    drafts[17]["body"] += " 示例联系方式 vx 123456 13912345678 WeChat：12 dm me"
    rule = {"id": "m-1", "severity": "hard", "rule_kind": "forbidden_regex", "rule_payload": {"pattern": pattern}}
    hits, stats = rulecheck.check([rule], drafts)
    assert "timed_out" not in stats
    want = {i for i, d in enumerate(drafts) if any(re.search(pattern, x) for x in (d["title"], d["body"]))}
    assert {h["index"] for h in hits} == want and 17 in want


def test_check_hard_rules_finds_a_word_list_hit_in_a_long_post(short_timeout):
    rule = {"content": "示例词表", "rule_kind": "forbidden_regex",
            "rule_payload": {"pattern": "|".join(f"\\b{w}\\b" for w in _WORDS[:8])}}
    V._compiled.cache_clear()
    hits = V.check_hard_rules([rule], title="示例标题", body=_POST * 30 + " 有事 dm")
    assert [h["match"] for h in hits] == ["dm"]


@pytest.mark.parametrize("pattern", ["(?>\\w+)\\s+\\w+", "(?=\\w+\\s)", "\\w++\\s", "(?>\\w*)\\s"])
def test_running_out_of_time_on_cjk_text_stops_near_the_limit(short_timeout, pattern):
    """到点之后 regex 隔一阵才看一次表。显式字集每一步都慢, 那一阵就拖成近 1 秒(到点后多跑 8~11 倍), 「(规则条数) × 0.1 秒」的
    账就不成立了; 换成属性写的字集之后实测 0.1~0.12 秒。"""
    from deskcore import rulecheck
    rulecheck.normalize_check({"kind": "forbidden_regex", "pattern": pattern})
    V.search_user_regex(pattern, "x")              # 先编译好(第一次的编译也从这一份里扣, 这里只量搜的)
    t0 = time.perf_counter()
    with pytest.raises(V.RegexTimeout):
        V.search_user_regex(pattern, "示例" * 10_000)
    assert time.perf_counter() - t0 < 2 * short_timeout


def test_compiling_a_long_boundary_pattern_costs_at_most_about_one_time_limit(short_timeout):
    """编译打断不了, 也不能不算: 一百个 \\b 连写闸和写作台都放行, 原来换成四百个环视、几 MB 的写法, 第一次 check 编译 5 秒。"""
    from deskcore import rulecheck
    pattern = "\\b" * 100 + "x"
    rulecheck.normalize_check({"kind": "forbidden_regex", "pattern": pattern})
    V._compiled.cache_clear()
    t0 = time.perf_counter()
    try:
        V.search_user_regex(pattern, _POST * 10)
    except V.RegexTimeout:
        pass
    assert time.perf_counter() - t0 < 2 * short_timeout


def test_a_pattern_too_long_to_compile_in_time_is_reported_unchecked(short_timeout):
    """换出来超过 MAX_TRANSLATED_CHARS 的不编译(编一次就是零点几秒到几秒), 报这次没查; 这个结论记住, 下次不再换。"""
    pattern = "\\b" * 1000 + "x"
    assert V.regex_is_safe(pattern), "前提: 闸放行它(闸不改)"
    V._compiled.cache_clear()
    for _ in range(2):
        budget = V.RegexBudget()
        t0 = time.perf_counter()
        with pytest.raises(V.RegexTimeout, match="超过"):
            V.search_user_regex(pattern, "x", budget)
        assert budget.timed_out and time.perf_counter() - t0 < short_timeout


@pytest.mark.parametrize("pattern", ["a{1000000}", "(?:ab){4294967294}", "私信{10000000}", "x{3000000,3000001}",
                                     "(?:x{300}){200}", "a{30000}b{30000}"])
def test_a_huge_repeat_count_is_unchecked_without_compiling(short_timeout, pattern):
    """regex 编译时把重复的最少次数展开, 打断不了、握着 GIL: x{10000000} 编 5 秒、2.5 GB, (?:ab){4294967294} 撑爆内存。
    re 微秒级, 闸放行(不套组的写作台也放行)。超过 MAX_REPEAT_UNROLL 的不编译, 报这次没查(独立复审 on aw#115 第六轮)。"""
    from deskcore import rulecheck
    assert V.regex_is_safe(pattern), "前提: 闸放行它(闸不改)"
    if "(" not in pattern:
        rulecheck.normalize_check({"kind": "forbidden_regex", "pattern": pattern})
    V._compiled.cache_clear()
    budget = V.RegexBudget()
    t0 = time.perf_counter()
    with pytest.raises(V.RegexTimeout, match="展开"):
        V.search_user_regex(pattern, "私信" * 10, budget)
    assert budget.timed_out and time.perf_counter() - t0 < short_timeout


@pytest.mark.parametrize("pattern, text", [("[\\s\\S]{20000,}", "x" * 20_000), ("x{1,10000000}", "xx"), ("\\d{11}", "13912345678"),
                                           ("(?:x{100}){200}", "x" * 20_000), ("哈{5,}", "哈" * 6)])
def test_ordinary_repeat_counts_still_compile_and_match(short_timeout, pattern, text):
    V._compiled.cache_clear()
    assert V.search_user_regex(pattern, text, V.RegexBudget())


def test_the_exact_table_is_built_once_and_not_charged_to_a_rule(monkeypatch):
    """正文里第一次出现 Python 还不认识的字母数字时要扫一遍码位表(零点几秒)。这不是哪条规则花的: 原来从这条的那一份里扣,
    第一篇带这种字的稿子上真命中报成没查; 几个线程同时碰上还各扫一遍(独立复审 on aw#115 第六轮)。"""
    import threading
    real, calls = V._build_exact_classes, []

    def slow():
        calls.append(1)
        time.sleep(0.5)                 # 比这条的那一份长: 扣了就一定报没查
        return real()

    monkeypatch.setattr(V, "_build_exact_classes", slow)
    monkeypatch.setattr(V, "_EXACT_TABLE", [])
    V._compiled.cache_clear()
    results: list = []

    def one():
        try:
            m = V.search_user_regex("\\bvx\\b", "加 vx 聊 \ua7cb", V.RegexBudget(0.25))
            results.append(m and m.group())
        except V.RegexTimeout as exc:
            results.append(exc)

    threads = [threading.Thread(target=one) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    V._compiled.cache_clear()
    assert results == ["vx"] * 4 and len(calls) == 1


def test_overlapping_set_members_do_not_backtrack_exponentially(short_timeout):
    """字集里的字和取反的类别转义重叠((?i)[！!\\W]: ！ 两边都在), 换成分支后一个字落进好几个分支, 后面配不上时挨个回溯, 量词一套
    就是指数级: 24 个 ！ 就超时、真命中报成没查。换出来的是原子组, re 当它一个字集, 两边都是微秒级(独立复审 on aw#115 第六轮)。"""
    from deskcore import rulecheck
    pattern = "(?i)[！!？?\\W]{3,}(?:vx|wx|v信)"
    rulecheck.normalize_check({"kind": "forbidden_regex", "pattern": pattern})
    V._compiled.cache_clear()
    hit = V.search_user_regex(pattern, "太好用了" + "！" * 24 + "快去买～ 想要链接的姐妹私信 ！！！vx", V.RegexBudget())
    assert hit and hit.group() == " ！！！vx"
    assert V.search_user_regex(pattern, "！" * 500 + "x", V.RegexBudget()) is None


def test_when_the_bounded_engine_cannot_take_a_pattern_it_is_unchecked_not_run_without_a_limit(monkeypatch):
    """regex 装上了却编译不了换出来的写法: 报这次没查, 不退回没有时限的 re —— 不然一个 regex 不认的写法就绕过了时限。"""
    monkeypatch.setattr(V, "_for_regex", lambda pattern, exact=False: ("(", False))
    V._compiled.cache_clear()
    try:
        budget = V.RegexBudget(5)
        with pytest.raises(V.RegexTimeout, match="编译不了"):
            V.search_user_regex("示例", "这是示例", budget)
        assert budget.timed_out
    finally:
        V._compiled.cache_clear()


# ── 换了引擎, 结论一字不差(\w \d \s \b 照 re 的字集) ─────────────────────────────────────────────

# 小红书正文里常见、两个引擎原生字集又判得不一样的字: emoji 后的 U+FE0F、连写 emoji 的 U+200D、keycap 的 U+20E3、组合重音、
# ①②③、上标、分数、U+001C~001F、全角空格; 再加上 ASCII、Latin-1、常用汉字和标点
_MIXED = ("示例正文❤\ufe0f好物分享👩\u200d💻，第①步②③ x²+y½ 1\ufe0f\u20e3 cafe\u0301 \x1c\x1d\u3000"
          "ABC abc_123 #话题 www.example.com/#faq ！！ ı İ é É ß")
_TEXTS = [_MIXED, "", " ", "①", "❤\ufe0f", "\x1c", "a", "Ab1_", "１２３", "\n\n", _MIXED * 30,
          # {e} 这类(regex 的模糊匹配)、(?x) 下的全角空格、(?i) 下 regex 会扩的那几个字、\b 挨着字面量
          "y", "x", "示", "加薇信", "加微{e<=1}信", "示例{e}", "x{e}", "\u3000", "a\u3000\u3000b", "！！\u3000",
          "\u0345", "\u0399", "ẞß", "vx", " vx ", "made", "an ad", "ads_x", "x_\u3000", "13912345678",
          "好！", "aa23", "aS", "aa0", "aabb", "太好用了" + "！" * 24 + "私信 ！！！vx"]
_CORPUS = [
    # 第 1 节、1b 节放行的那些
    "最", "(?i)best|top", "第[一二三]名", "\\d{1,20}元", "(ab){1,10}", "^标题[:：]", "[a-z]+@[a-z]+\\.com",
    "a*b*c", "\\d+\\.\\d+", "加.{0,3}微.{0,3}信", "\\d{1,3}\\d{1,3}", ".*私信.*", "a*+a*b",
    "^\\S+\\s+\\S+\\s+\\S+\\s+\\S+\\s+\\S+\\s+\\S", "品牌[\\s\\S]*品牌[\\s\\S]*品牌", "最(好|佳|强|便宜|有效)|第一(名|品牌)",
    "[!！][\\s\\S]*[!！][\\s\\S]*[!！][\\s\\S]*[!！]", "\\n\\s*\\n\\s*\\n", "\\d+\\s*\\d+", "[!！]+\\s*[!！]+",
    "原价.*\\d+", ".*[!！]{2,}", "(?:\\S+\\s+){5}\\S+", "(?:哈){5}", "(?:哈哈)+", "哈{5,}", "(?:\\d{1,3},)+\\d{3}",
    "\\d+(?=元)", "\\d+(?!\\d)", "\\w+(?=\\s)", "(?<!\\d)\\d{11}(?!\\d)", "[^\\n]*(?=\\n)", "\\d+(?:\\.\\d+)?元",
    "(?i)(?-i:a*)(?-i:A*)B", ".*\\n*x",
    # 字集不一样的那几样
    "^\\W", "\\W$", "[^\\w\\s]{2,}", "\\w+", "\\W+", "\\b\\w", "\\B\\w", "\\bwww\\b", "\\B", "\\s+", "\\S+", "\\d", "\\D+",
    "[\\w-]+", "[^\\W\\d]+", "[\\s\\S]{3}", "(?a)\\w+", "(?a:\\W\\w)", "x(?a:\\W)", "(?a)x(?u:\\w)", "(?i)\\w+",
    "[[:alpha:]]", "[[a]", "(?x) \\w+ # [注释\n x?", "(?#[\\w)\\w", "(\\w)\\1", "(?P<n>\\d)(?P=n)",
    # re 当字面的 {: regex 认成模糊匹配(x{e} 配得上 y), 或者编译不了(codex review on aw#115 第五轮)
    "x{e}", "示例{d}", "加微{e<=1}信", "示例{1,2}{e}", "x{e}?", "a{e}{e}", "a{}", "a{,}", "x{1, 2}", "(?x)x{1,\u30002}",
    # (?x) 下 re 只跳过 ASCII 空白, 全角空格是字面的
    "(?x)^\u3000{1,3}", "(?x)(\u3000+)", "(?x)a|\u3000*b", "(?x)！{2,}\u3000*", "(?x)\\b\\w\u3000+", "(?x)\\\u3000+",
    # (?i) 下换出来的字集不按大小写扩; regex 预先算分支开头的字时会照折叠剔字
    "(?i)[\\W]", "(?i)[^\\W\\d_]{2}", "(?i)[\\W\\d]", "(?i)[\\Wa]", "(?i)[^\\Wa]", "(?i)x|\\W", "(?i:x)|[^ιq]",
    "(?i)\\bwechat\\b",
    # \b 挨着英文字母数字的字面量时只看另一边
    "\\bvx\\b", "\\bad\\b|\\bads\\b", "vx\\B", "\\Bd", "_\\b", "\\x41\\b", "\\d\\b", "a?\\b", "\\ba?", "\\b1[3-9]\\d{9}\\b",
    "(?x)\\b a", "a\\bb", "a\\Bb",
    # 独立复审 on aw#115 第六轮: \b 后面的字面量隔着 (?#…) 跟量词; (?x) 下回引 / 八进制后面隔着空白、注释还是数字; 字集里
    # 的字和取反的类别转义重叠
    "\\bv(?#可省)?！", "\\ba(?#c)*", "(?x)(a)\\1 23", "(?x)\\0 12", "(?x)(a)\\1 0", "(?x)(a)\\1b+", "(?ax)\\01#c{e}\n2??",
    "(?x)(a)\\1\t1", "\\101", "(?x)\\101 1", "(?i)[！!？?\\W]{3,}(?:vx|wx|v信)", "[a\\W]+x",
]


@pytest.mark.filterwarnings("ignore::FutureWarning")      # [[:alpha:]] / [[a] 是 re 自己报的「可能是嵌套字集」
@pytest.mark.parametrize("pattern", _CORPUS)
def test_the_bounded_engine_finds_exactly_what_re_finds(pattern):
    import re
    for text in _TEXTS:
        want = re.search(pattern, text)
        got = V.search_user_regex(pattern, text, V.RegexBudget(5))
        assert (got and (got.span(), got.groups())) == (want and (want.span(), want.groups())), (pattern, text[:30])


def _bounded_spans(pattern: str, text: str) -> list:
    """照 search_user_regex 选哪一份写法(正文里有 Python 不认识的字母数字时用 exact 那份), 列出所有匹配的位置。"""
    rx, needs_exact, why = V._compiled(pattern)
    assert rx is not None, why
    if needs_exact and V._differs_from_re(text):
        rx, _, why = V._compiled(pattern, True)
        assert rx is not None, why
    return [m.span() for m in rx.finditer(text)]


_CLASS_PATTERNS = ("\\w", "\\W", "\\d", "\\D", "\\s", "\\S", "[\\w]", "[^\\w]", "[\\W\\d]", "[^\\W\\s]", "[\\w-]",
                   "[^\\w\\s，]", "[\\s\\S]", "[^\\D\\S]", "(?a)\\w", "(?a)[\\W]", "x?(?a:\\s)", "(?i)\\w", "\\b", "\\B",
                   "(?i)\\b", "(?i)[\\W]", "(?i)[^\\W]", "(?i)[^\\W\\d_]", "(?i)[\\W\\d]", "(?i)[\\w]", "(?i)[\\Wa]",
                   "(?i)[^\\Wa]", "(?i)[\\sk]", "(?i)[^\\Dx]", "(?i)x|\\W", "(?i:x)|[^ιq]")


def test_class_escapes_cover_exactly_the_characters_re_does():
    """逐个码位比, 两份写法都比: 本机 Python 认识的码位(全部)走快的那份(属性写的字集); regex 新收、Python 还不认识的字母数字
    (全部), 掺上每 7 个取一个的其余码位, 走 exact 那份。regex 原生的 \\w 含组合符、不含 ①², 直接换引擎 ^\\W 碰上「①」两边结论
    相反; (?i) 下 regex 还会按大小写扩字集((?i)[\\W] 多认 Ι ß)。"""
    import re
    import unicodedata
    every = "".join(chr(c) for c in range(0x110000) if not 0xD800 <= c <= 0xDFFF)
    known = "".join(c for c in every if unicodedata.category(c) != "Cn")
    newer = "".join(V._FAST_W.findall("".join(c for c in every if unicodedata.category(c) == "Cn")))
    mixed = known[::7] + newer + every[::7]
    assert newer and not V._differs_from_re(known) and V._differs_from_re(mixed), "前提: 两段各走一份"
    for pattern in _CLASS_PATTERNS:
        for chars in (known, mixed):
            assert _bounded_spans(pattern, chars) == [m.span() for m in re.finditer(pattern, chars)], pattern


def test_the_exact_tables_can_be_written_on_this_python():
    """exact 那份要求: re 认、regex 不认的字一个都没有(两个 Python 版本上都是), 不然写不出来、带新字的那几篇只能报没查。"""
    assert V._exact_classes() is not None


def test_ordinary_text_takes_the_fast_tables():
    """emoji(连新 emoji)、①②、组合符、全角字两份结论一样, 走快的那份; 只有 Python 还不认识的字母数字才换 exact 那份。"""
    assert not V._differs_from_re(_MIXED * 3 + "\U0001fae9\U0001fa77")
    assert V._differs_from_re("示例\ua7cb")


@pytest.mark.xfail(strict=True, reason="已知差异, 不值得再绕: (?i) 下 re 让 i/I 和土耳其语的 ı/İ 互认, regex 不认")
def test_known_gap_turkish_dotless_i_under_ignorecase():
    import re
    assert bool(re.search("(?i)[a-z]", "ı")) == bool(V.search_user_regex("(?i)[a-z]", "ı"))


@pytest.mark.xfail(strict=True, reason="已知差异, 不值得再绕: (?i) 下的回引, re 逐字比 lower(), regex 按大小写折叠(ſ 折成 s)")
def test_known_gap_backreference_under_ignorecase():
    import re
    assert bool(re.search("(?i)(\\w)\\1", "ſs")) == bool(V.search_user_regex("(?i)(\\w)\\1", "ſs"))


@pytest.mark.xfail(strict=True, reason="已知差异, 不值得再绕: 比 Python 新的 Unicode 才有的大小写对(Ɤ U+A7CB / ɤ), regex 照自己的表互认")
def test_known_gap_case_pairs_newer_than_python_under_ignorecase():
    import re
    assert bool(re.search("(?i)\u0264", "\ua7cb")) == bool(V.search_user_regex("(?i)\u0264", "\ua7cb"))


@pytest.mark.xfail(strict=True, reason="已知差异, 不值得再绕: 组内的 (?a:…) 管不到 regex 的不分大小写(只看整条正则的旗), "
                                       "re 在组里只按 ASCII 互认")
def test_known_gap_scoped_ascii_flag_under_ignorecase():
    import re
    assert bool(re.search("(?a:(?i:k))", "\u212a")) == bool(V.search_user_regex("(?a:(?i:k))", "\u212a"))


@pytest.mark.xfail(strict=True, reason="已知差异, 不值得再绕: 整条开了 (?a) 时, (?u:…) 里面再套的组(换写法加的、自己写的 "
                                       "(?:…) 都算) regex 退回整条的 ASCII; 写规则用不到这种开法(独立复审 on aw#115 第六轮)")
def test_known_gap_unicode_scope_inside_global_ascii():
    import re
    assert bool(re.search("(?a)vx(?u:\\b)", "vx天")) == bool(V.search_user_regex("(?a)vx(?u:\\b)", "vx天"))


def test_falls_back_to_re_when_regex_is_not_installed(monkeypatch):
    """regex 包没装上就退回 re: 规则照查, 只是没有时限(还在闸后面、有长度上限)。装上了却跑不了的不退回(见上一节)。"""
    monkeypatch.setattr(V, "_regex", None)
    V._compiled.cache_clear()
    try:
        assert V.search_user_regex("示例\\w", "这是示例A").group(0) == "示例A"
    finally:
        V._compiled.cache_clear()
