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
