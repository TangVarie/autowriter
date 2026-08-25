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
