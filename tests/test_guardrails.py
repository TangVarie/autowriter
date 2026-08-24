"""§7.1 的三组「护栏」用例(审计 SUP-024)。

护栏 = 已经修过、但修法很容易被下一次改动无声推翻的地方。它们的共同点是
**错了不报错**: 价档错配只是账单数字变了, 规则解析错只是硬约束不生效,
笔记截断错只是旧观察悄悄消失。这类东西不钉住就是白修。
"""

from __future__ import annotations

import config
import memory
import validator


# ══════════════════════════════════════════════════════════════════════
# 护栏一 · 价档 / 上下文窗口的前缀匹配(R-042)
# ══════════════════════════════════════════════════════════════════════

def test_pricing_prefix_needs_a_boundary(monkeypatch):
    """``claude-opus-4-10`` 不能被 ``claude-opus-4-1`` 抢走。

    R-042 修过一次: 裸 startswith 会让将来出现的 4-10 命中 4-1 那一档
    (最贵的 $27), 而账单是"看起来像真的"的数字, 没人会去核。

    ⚠️ 不能靠"比较两个真实 model 的返回值"来验这件事 —— 现在表里**所有**
    模型的窗口都是 200K, 两边相等并不说明匹配错了。所以往表里塞一个带
    独特数值的哨兵键, 直接验"边界规则"本身。第一版就是这么写错的。
    """
    sentinel_price = {"input": 999.0, "output": 999.0,
                      "cache_write": 999.0, "cache_read": 999.0}
    monkeypatch.setitem(config.MODEL_PRICING, "claude-opus-4-1", sentinel_price)
    monkeypatch.setitem(config.MODEL_CONTEXT_WINDOWS, "claude-opus-4-1", 12345)

    assert config.get_pricing("claude-opus-4-1") == sentinel_price, "精确匹配都不成立"
    assert config.get_pricing("claude-opus-4-10") != sentinel_price, \
        "claude-opus-4-10 配到了 claude-opus-4-1 的价档(缺边界判断)"
    assert config.get_context_window("claude-opus-4-1") == 12345
    assert config.get_context_window("claude-opus-4-10") != 12345


def test_pricing_strips_route_prefix_and_thinking_suffix():
    """``claude/`` 路由前缀和 ``-thinking`` 后缀都不能让它掉进 unknown 兜底。"""
    known = next(k for k in config.MODEL_PRICING if k.startswith("claude-"))
    assert config.get_pricing(f"claude/{known}") == config.get_pricing(known)
    # -thinking 是中转站约定的变体, 价格应跟随基座模型
    assert config.get_pricing(f"{known}-thinking") == config.get_pricing(known)


def test_pricing_unknown_falls_back_high_not_crash():
    """完全未知的 id 要给个数字, 而且宁可高估 —— 不能 KeyError 把页面炸掉。"""
    got = config.get_pricing("totally-made-up-model-9000")
    assert isinstance(got, dict) and got
    assert config.get_context_window("totally-made-up-model-9000") > 0


# ══════════════════════════════════════════════════════════════════════
# 护栏二 · 规则文本 → 可执行 spec(R-041)
# ══════════════════════════════════════════════════════════════════════

def test_parse_rule_positive_prefix_wins_by_position():
    """``必须包含"不要熬夜"`` 是**正向**规则。

    旧实现先扫否定词表, 中间那个"不要"会抢先命中, 整条正向硬规则被静默
    反转成禁用词 —— 而且那个禁用词永远匹配不到, 于是规则等于不存在。
    """
    spec = validator._parse_rule('必须包含"不要熬夜"')
    assert spec and spec["kind"] == "required_phrase", spec
    assert "不要熬夜" in spec["target"], spec


def test_parse_rule_negative_prefix_wins_by_position():
    """``禁止使用"必须包含"话术`` 是**否定**规则 —— 对称的另一半。"""
    spec = validator._parse_rule('禁止使用"必须包含"话术')
    assert spec and spec["kind"] == "forbidden_word", spec
    assert "必须包含" in spec["target"], spec


def test_parse_rule_returns_none_for_unmechanizable():
    """抓不出上限的长度类描述要返回 None, 交给 LLM 复检, 不能瞎猜一个数。

    ``控制在20字以上`` 是**下限**, 按 max_len 处理会把所有长标题判违规。
    """
    assert validator._parse_rule("标题不要太长") is None
    assert validator._parse_rule("控制在20字以上") is None


def test_parse_rule_max_len_with_upper_bound():
    """带上界后缀的才是 max_len, 且要抠对数字与作用域。"""
    spec = validator._parse_rule("标题控制在20字以内")
    assert spec and spec["kind"] == "max_len", spec
    assert spec["n"] == 20, spec


# ══════════════════════════════════════════════════════════════════════
# 护栏三 · 调校笔记的行级去重 + 软上限
# ══════════════════════════════════════════════════════════════════════

def test_dedup_calibration_drops_oldest_and_reports_them():
    """超 4000 字要丢**最旧**的, 且被丢的必须能被调用方拿到。

    这条笔记是模型学"我的风格"的唯一长期载体。悄悄丢行的后果是
    "它以前记得的东西突然不记得了", 而用户完全看不出发生过什么 ——
    所以 dropped_sink 非空是硬要求, 不只是锦上添花。
    """
    lines = [f"- 观察{i}：这是一条足够长的调校观察，用来把总长度顶过软上限。" * 2
             for i in range(200)]
    dropped: list[str] = []
    out = memory._dedup_calibration_lines("\n".join(lines), dropped_sink=dropped)

    assert len(out) <= 4000, len(out)
    assert dropped, "淘汰了行却没有留下任何痕迹"
    # 留下的应该是**新的**那批: 最后一条在, 第一条不在
    assert "观察199" in out, "把最新的观察丢了"
    assert "观察0" not in out, "最旧的观察没被丢"


def test_dedup_calibration_removes_duplicate_lines():
    """同一条观察写三遍只留一条 —— 四个写入入口都会经过这里。"""
    text = "\n".join(["- 标题别用数字开头"] * 3 + ["- 正文首句要给画面"])
    out = memory._dedup_calibration_lines(text)
    assert out.count("标题别用数字开头") == 1, out
    assert "正文首句要给画面" in out


def test_dedup_calibration_empty_is_empty():
    assert memory._dedup_calibration_lines("") == ""
    assert memory._dedup_calibration_lines("   \n\n  ") == ""
