"""deskcore/vocab.py — 发牌用的闭集。

分两层, 来源不同, 【故意】不混:

  essence 层  ← vendor/controlled_vocab_v0_2.json (从 truth-vault 原样复制)
                情绪杠杆 / 人性原型 / 时效依赖 / 内容形式 / 目标受众 …
                穿越周期(半衰期 5 年+), 权威源是 TV 的 docs/05-controlled-vocab.md。
                **不要在本文件里手写这些值** —— 改词表走 vendor/README.md 的流程。

  surface 层  ← 本仓 generator.py
                标题句式 / 词感 / 切入角度。半衰期 6-12 个月, 本来就该独立演进,
                是 autowriter 自己的东西, 不归 TV 管。

这个分法直接对应 TV README 原则 2 的 Surface/Essence 分层 —— 两层衰减速度不同,
混进一个字段就锁死了跨时间跨产品的迁移性。
"""

from __future__ import annotations

import hashlib
import json
import os

_VENDOR_DIR = os.path.join(os.path.dirname(__file__), "vendor")
_VOCAB_PATH = os.path.join(_VENDOR_DIR, "controlled_vocab_v0_2.json")
_SHA_PATH = _VOCAB_PATH + ".sha256"


def _load() -> dict:
    with open(_VOCAB_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def vendor_checksum_ok() -> tuple[bool, str]:
    """vendor 的词表副本有没有被手改过。CI 与 /health 都查这个。

    漂移的表现是发牌抽到闭集外的值 —— 不会立刻报错, 会安静产出脏数据。
    所以宁可在启动/CI 阶段吵一声。
    """
    try:
        raw = open(_VOCAB_PATH, "rb").read()
        actual = hashlib.sha256(raw).hexdigest()
        expected = open(_SHA_PATH, encoding="utf-8").read().split()[0].strip()
    except OSError as exc:
        return False, f"cannot read vendored vocab: {exc}"
    if actual != expected:
        return False, (f"vendored vocab was modified by hand "
                       f"(sha256 {actual[:12]}… != recorded {expected[:12]}…). "
                       f"改词表要走 TV 上游 + 重新 vendor, 见 deskcore/vendor/README.md")
    return True, actual[:12]


_DATA = _load()
_SETS = _DATA["sets"]

VOCAB_VERSION: str = _DATA.get("version", "?")
VOCAB_AUTHORITY: str = _DATA.get("authority", "?")


def values(name: str) -> tuple[str, ...]:
    return tuple(_SETS[name]["values"])


# ── essence 层 (来自 vendor, 不要手写) ────────────────────────────────────
EMOTIONAL_LEVERS = values("emotional_lever")
EMOTIONAL_VALENCES = values("emotional_valence")
EMOTIONAL_INTENSITIES = values("emotional_intensity")
HUMAN_TRUTH_ARCHETYPES = values("human_truth_archetype")
TREND_DEPENDENCIES = values("trend_dependencies")
CONTENT_FORMATS = values("content_format")
TARGET_AUDIENCES = values("target_audience")
INTENTS = values("intent")

TREND_EXCLUSIVE: str = _SETS["trend_dependencies"]["exclusive_value"]

# surface 半衰期【不是】逐值属性 —— 真实规则按 trend_dependencies 的【组合】求值
# (「时代语言范式」只有在不含短期集元素时才拿 30 月; 「行业事件」「平台话术」不在
# 短期集、落 12 月默认档)。此前 vendor 的 JSON 里导出成逐值 tier 映射是错的,
# 已换成规则编码。deskcore 目前不消费衰减(发牌只用闭集), 保留入口备用。
SURFACE_DECAY: dict = dict(_DATA.get("surface_decay") or {})
SHORT_TERM_TRENDS: tuple[str, ...] = tuple(SURFACE_DECAY.get("short_term_set") or ())
LEVER_TO_VALENCE: dict[str, str] = dict(_DATA["derivations"]["lever_to_valence"])
LEVER_BOUNDARY_RULES: dict[str, str] = dict(_DATA["boundary_rules"]["rules"])


# ── surface 层 (本仓自有, 与 generator.py 同源) ───────────────────────────
# 这里【引用】generator 的常量而不是复制 —— 那边改了句式池, 发牌自动跟上。
def _surface_pools() -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        import generator
        return (tuple(generator.TITLE_STRUCTURE_MATRIX), tuple(generator.WORD_TILTS))
    except Exception:
        # generator 拉 anthropic/genai SDK; CLI selftest 要能在裸环境跑。
        return (
            ("疑问句", "数字清单", "对比反转", "场景直述",
             "第一人称自白", "比喻起手", "感叹共鸣", "对白引语"),
            ("克制", "口语", "反差", "感性", "理性", "文艺", "冷静", "自嘲"),
        )


TITLE_STRUCTURES, WORD_TILTS = _surface_pools()


def default_angles() -> list[dict]:
    """默认切入角度池 = generator.CREATIVE_ROLES_POOL。

    项目自己配了 projects.custom_roles 时【优先用项目的】—— 当年
    _assign_slot_coordinates(generator.py:1301) 被移除(generator.py:1576-1584)
    的理由就是通用角度池跟项目自己的 role 设定打架, LLM 会锁定更具体的平台标签、
    把项目的 role 降级成风格提示。按项目 opt-in 就不打架, 这正是那段注释里
    留的那条路。
    """
    try:
        import generator
        return [
            {"id": r.get("id", ""), "name": r.get("name", ""),
             "brief": (r.get("prompt_suffix") or "").strip()}
            for r in generator.CREATIVE_ROLES_POOL
        ]
    except Exception:
        return [
            {"id": "narrative", "name": "叙事角",
             "brief": "以一个具体的生活场景或真实故事切入, 产品自然融入叙事, 不要开篇讲卖点。"},
            {"id": "insight", "name": "洞察角",
             "brief": "从反直觉或被忽视的角度切入, 制造认知惊喜。避免常规切入方式。"},
            {"id": "empathy", "name": "共情角",
             "brief": "从目标用户当下最真实的情绪出发, 情绪共鸣先于产品信息。"},
            {"id": "contrast", "name": "对比角",
             "brief": "以「之前 vs 之后」「以为 vs 实际」等对比结构切入, 差异要具体可感。"},
            {"id": "tips", "name": "干货角",
             "brief": "以实用信息或方法论为主轴, 读者要能带走具体可操作的内容。"},
            {"id": "occasion", "name": "场合角",
             "brief": "锁定一个具体的使用时刻或生活节点, 场合越具体代入感越强。"},
        ]


HOOK_TYPES = ("痛点共鸣", "反差", "福利", "悬念", "身份认同", "场景代入", "信息差")


# ── 工具函数 ──────────────────────────────────────────────────────────────

def valence_of(lever: str) -> str:
    """由 lever 派生情绪极性。docs/05 §4: valence 由 lever 唯一决定, 不独立标。"""
    return LEVER_TO_VALENCE.get(lever, "neutral")


def normalize_trends(trends: list[str]) -> list[str]:
    """应用 docs/05 §7 的排他规则: 含「通用」则只保留「通用」。"""
    cleaned = [t for t in trends if t in TREND_DEPENDENCIES]
    if TREND_EXCLUSIVE in cleaned:
        return [TREND_EXCLUSIVE]
    seen: set[str] = set()
    out: list[str] = []
    for t in cleaned:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def boundary_rules_for(lever: str) -> str:
    """抽到某 lever 时要一并给模型的判别指令。

    docs/05 §3 花了 40 行讲焦虑 vs 恐惧、虚荣 vs 造梦、罪恶感 vs 焦虑怎么分 ——
    光给一个标签名模型多半会混。
    """
    return LEVER_BOUNDARY_RULES.get(lever, "")


def combination_space() -> int:
    """参与无放回抽样的四个主维度的笛卡尔积大小。"""
    return (len(EMOTIONAL_LEVERS) * len(HUMAN_TRUTH_ARCHETYPES)
            * len(CONTENT_FORMATS) * len(TITLE_STRUCTURES))


def vocab_reference() -> str:
    """人类可读闭集清单, 给工具描述 / system prompt 用。"""
    return (
        f"emotional_lever({len(EMOTIONAL_LEVERS)}): " + " / ".join(EMOTIONAL_LEVERS) + "\n"
        f"human_truth_archetype({len(HUMAN_TRUTH_ARCHETYPES)}): " + " / ".join(HUMAN_TRUTH_ARCHETYPES) + "\n"
        f"content_format({len(CONTENT_FORMATS)}): " + " / ".join(CONTENT_FORMATS) + "\n"
        f"target_audience({len(TARGET_AUDIENCES)}): " + " / ".join(TARGET_AUDIENCES) + "\n"
        f"trend_dependencies({len(TREND_DEPENDENCIES)}, 多选,「{TREND_EXCLUSIVE}」排他): "
        + " / ".join(TREND_DEPENDENCIES) + "\n"
        f"emotional_intensity({len(EMOTIONAL_INTENSITIES)}): " + " / ".join(EMOTIONAL_INTENSITIES) + "\n"
        f"title_structure({len(TITLE_STRUCTURES)}): " + " / ".join(TITLE_STRUCTURES) + "\n"
        f"hook_type({len(HOOK_TYPES)}): " + " / ".join(HOOK_TYPES) + "\n"
        "(emotional_valence 由 emotional_lever 唯一决定, 不独立标)"
    )
