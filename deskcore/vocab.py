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
import re

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


# 判据里的「标志词: 万一、以后…」这一截会被模型原样抄进标题和正文(卡上给的词, 模型当成要用的词)。
# 只在【给出去的那一份】里去掉, vendor 的 JSON 和它的 sha 校验一个字不动 —— 词表的权威源在 TV。
# 前面那个句号 / 逗号一起吃掉, 不留一个孤零零的「。」; 后面没有句号的(现在两条都是)到行尾为止。
_MARKER_CLAUSE = re.compile(r"[，,。；;\s]*标志词[:：][^。\n]*。?")


def boundary_rule_text(lever: str) -> str:
    """``boundary_rules_for`` 去掉「标志词」那一截, 发牌回包和渲染出来的那一行都用这一份(angle cards v2 §2.8)。

    没有标志词的规则原样返回; 不认识的 lever 回空串。"""
    return _MARKER_CLAUSE.sub("", boundary_rules_for(lever))


# ── 卡面上的大白话(angle cards v2 §3.2) ──────────────────────────────────
# 四个维度名照协议留着, 值后面跟一句人话: 光给「认知重构」「信息差利用」这种分类名, 模型要么把名字抄进正文,
# 要么各自理解。措辞要老板过目; 改这里不碰协议哈希(这些只在 draw_angles 的回包里)。
# 键必须和 vendor 词表 / 标题句式池完全对上 —— tests/test_vocab_plain.py 钉着, 词表一漂就红。
PLAIN_FORMAT: dict[str, str] = {
    "情感叙事": "讲一段自己的经历和感受，情绪带着走",
    "认知重构": "把一个常见的看法翻过来讲清楚",
    "横评对比": "几样东西放在一起比，给出结论",
    "教程攻略": "一步一步教怎么做，读完能照着做",
    "直给推荐": "直接说推荐什么、为什么",
    "场景植入": "在一个具体生活场景里自然带出产品",
    "提问求助": "拿自己遇到的一件具体的事向大家讨主意",
    "反差破圈": "用一个出乎意料的反差把人拉进来",
}

PLAIN_TITLE: dict[str, str] = {
    "疑问句": "标题就是一个问句",
    "数字清单": "标题里带数字或“几个/几条”",
    "对比反转": "标题里有前后对比或意外转折",
    "场景直述": "标题直接讲一个场景或一件事，不用问号",
    "第一人称自白": "标题用“我”开头说自己的事",
    "比喻起手": "标题用一个比喻开头",
    "感叹共鸣": "标题是一句感叹，让人点头",
    "对白引语": "标题引用一句别人说的话",
}

PLAIN_LEVER: dict[str, str] = {
    "焦虑撬动": "担心以后可能发生、还没发生的事",
    "羞耻撬动": "怕被人看低、觉得丢脸",
    "恐惧撬动": "已经发生的、具体的威胁让人怕",
    "愤怒撬动": "被冒犯、觉得不公平、想吐槽",
    "罪恶感撬动": "觉得自己现在做得不够、对不起谁",
    "造梦投射": "描绘读者想成为的样子",
    "认同感建立": "让读者觉得“说的就是我这类人”",
    "归属感建立": "让读者觉得自己是这个圈子里的人",
    "共鸣释放": "说出大家憋着没说的感受",
    "虚荣撬动": "让读者觉得自己比别人懂、比别人好",
    "好奇驱动": "先抛一个让人想知道答案的点",
    "信息差利用": "告诉读者一件大多数人不知道的事",
}

# 情绪浓度在卡上写成 弱/中/强(§3.2)。
INTENSITY_PLAIN: dict[str, str] = {"low": "弱", "medium": "中", "high": "强"}

# 参考结构那一行里 opening_type(TV 特征题)的说法。「其他」不说 —— 没有键, 调用方 .get 到 None 就略过。
OPENING_PLAIN: dict[str, str] = {
    "具体事件": "开头先讲一件具体的事",
    "身份自述": "开头先讲自己的身份",
    "观点断言": "开头先下一个判断",
    "提问": "开头先抛一个问题",
    "数据事实": "开头先摆一个数字或事实",
    "感叹情绪": "开头先是一句感叹",
}

# 写手 pin 标题句式时的别名(§2.9): 写手嘴里说的是「问句」。
TITLE_ALIASES: dict[str, str] = {"问句": "疑问句"}

# 参考项目只有两类(title_is_question 是 / 否)时, 非问句那一份按这个比例摊到其余七种句式上(§2.3)。
# 来自数据地图里有标题文本的那几个项目的非问句分布; 只是没有本项目数据时的先验, 合计为 1。
NONQ_SPLIT: dict[str, float] = {
    "场景直述": 0.62,
    "感叹共鸣": 0.15,
    "数字清单": 0.08,
    "对比反转": 0.0375,
    "第一人称自白": 0.0375,
    "比喻起手": 0.0375,
    "对白引语": 0.0375,
}


# ── 标题句式的粗分类(§2.3 / §6 item 1) ────────────────────────────────────
# 按顺序试, 先中先得。只用来摊「非问句」那一份: 问句与否以 TV 的 title_is_question(Jev 答的)为准, 调用方有答案就
# 传 is_question 覆盖掉正则那一档。这是个粗分类器(§8 风险 6), 宁可落到「场景直述」也不乱归类。
# 在 spec 的正则上只多挡了两处明显的误判: 「求」不认 要求/需求/追求/请求/寻求/求职 这类词里的那个字(求职留学品类的
# 标题会整批被当成问句), 「像」不认「好像」(那是「似乎」, 不是比喻)。
_TQ_QUESTION = re.compile(r"[？?]|吗|呢[\s～~。…]*$|怎么|为什么|是不是|有没有|(?<![要需追请寻诉祈恳谋渴征])求(?!职)")
_TQ_NUMBER = re.compile(r"^\s*[0-9０-９]|[0-9０-９]+\s*[个条种招步款]|[二三四五六七八九十两几]+\s*[个条种招步款]")
_TQ_QUOTE = re.compile(r"[「」“”]")
_TQ_FIRST_PERSON = re.compile(r"^\s*我")
_TQ_CONTRAST = re.compile(r"但|却|竟|没想到|原来|以为|vs", re.IGNORECASE)
_TQ_METAPHOR = re.compile(r"(?<!好)像|仿佛|如同")
_TQ_EXCLAIM = re.compile(r"[！!]")
_TQ_ORDER = (("数字清单", _TQ_NUMBER), ("对白引语", _TQ_QUOTE), ("第一人称自白", _TQ_FIRST_PERSON),
             ("对比反转", _TQ_CONTRAST), ("比喻起手", _TQ_METAPHOR), ("感叹共鸣", _TQ_EXCLAIM))
TITLE_FALLBACK = "场景直述"


def classify_title(title, is_question: bool | None = None) -> str | None:
    """标题 → 八种标题句式之一(TITLE_STRUCTURES)。空标题回 None(不计入任何一类)。

    ``is_question``: TV 的 title_is_question 答案(是 → True, 否 → False, 没答 → None)。True 直接是「疑问句」;
    False 时跳过问句那一档, 在其余七种里分; None 才用正则判问句。"""
    t = re.sub(r"\s+", " ", str(title or "")).strip()
    if not t:
        return None
    if is_question is True:
        return "疑问句"
    if is_question is None and _TQ_QUESTION.search(t):
        return "疑问句"
    for name, rx in _TQ_ORDER:
        if rx.search(t):
            return name
    return TITLE_FALLBACK


# TV 的 raw_content 是「【标题】…\n【正文】…」(近 60 天的笔记 notes.title 全是 NULL, 标题只在这里)。少数是
# 「【标题】\n标题」(标题在下一行)或整段一行「【标题】…【正文】…」; 没有【标题】标记的取第一个非空行。
_TITLE_TAG = "【标题】"
# 只认这几个分段标记: 标题自己也可能以「【干货】」这种括号开头, 那是标题的一部分。
_SECTION_TAG = re.compile(r"【(?:标题|正文|标签|话题)】")


def note_title(title, raw_content) -> str:
    """一条 TV 笔记的标题: ``notes.title`` 有就用它, 否则从 ``raw_content`` 里取【标题】那一行(或第一行)。取不到回空串。
    只在进程内用来分句式, 不往外给。"""
    t = str(title or "").strip()
    if t:
        return t
    raw = str(raw_content or "")
    if not raw.strip():
        return ""
    i = raw.find(_TITLE_TAG)
    if i >= 0:
        rest = raw[i + len(_TITLE_TAG):]
        for line in rest.split("\n"):
            cut = _SECTION_TAG.search(line)
            line = (line[:cut.start()] if cut else line).strip()
            if line:
                return line
            if cut:                       # 「【标题】【正文】…」: 标题是空的, 不往正文里找
                return ""
        return ""
    for line in raw.split("\n"):
        line = _SECTION_TAG.sub("", line).strip()
        if line:
            return line
    return ""


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
