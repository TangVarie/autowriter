"""借卡时多带的那一句封面参考(2026-11-16): 这次借到的几篇帖, 封面长什么样 —— 只报数。

数据是 TV 的 ``truth_vault.v_first_image_features``(契约在 TV ``docs/34-first-image-pass.md`` 第 6 节), 由
``store.tv_first_image_features`` 读回来, 这里只做纯计算: 不读库、不取图、不碰封面上抄下来的字(那一面本来也不给)。

守的几条(都是那份契约里的):

· 只用 ``usable`` 是布尔 ``True`` 的行; 没有行 = 这张卡没有封面信息。
· ``answers`` 里没有某个键 = 这道题没答成, 不当成「否」, 也不进这一项的分母。
· 不认识的键、不认识的取值一律忽略(题库以后会多出题来)。
· 借到的卡里有封面信息的不到 ``MIN_USABLE`` 篇就不说, 一个字都不出。
· 说出来的只有计数, 而且带着「没有人对着图核过」那句。
· 话里不说「爆帖」: 借到的卡里有 ``synthetic``(指标没核过)的, 这一句只管封面长什么样, 不替它们的数据作保。
"""

from __future__ import annotations

import re

MIN_USABLE = 3          # 有封面信息的卡不到三篇就不说
MAX_CLAUSES = 2         # 一句话最多说两项
MIN_COUNT = 2           # 一项至少两篇一样才说

# 送去查的 note_id 只收这种形状: postgrest 的 in_() 不转义 , : ( ) (同 store 里 W-31 的做法)
_ID_SHAPE = re.compile(r"[0-9A-Za-z_\-]{1,64}")

# 键的次序就是并列时的先后; 取值是 TV 题库里的原文(闭集)。
_TEXT_KIND, _TEXT_NUMBER, _NO_TEXT = "img_text_kind", "img_text_has_number", "没有字"

_PHRASES: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("img_layout", (
        ("一张照片", "是一张照片"),
        ("拼图", "是拼图"),
        ("前后对比", "是前后对比图"),
        ("纯文字卡片", "是纯文字卡片"),
        ("截图样式", "是截图样式"),
    )),
    ("img_person", (
        ("没有人", "封面上没有人"),
        ("只有手或身体一部分", "只拍到手或身体一部分"),
        ("有人但看不到脸", "有人但看不到脸"),
        ("看得到脸", "看得到人脸"),
    )),
    ("img_product", (
        ("没有", "封面上没有产品"),
        ("有但在背景里或很小", "产品在背景里或很小"),
        ("有而且是主体", "产品是主体"),
    )),
    ("img_text_kind", (
        ("提问", "封面上的字是一句提问"),
        ("结果或结论", "封面上的字是结果或结论"),
        ("清单或教程标题", "封面上的字是清单或教程标题"),
        ("只是名称", "封面上的字只是名称"),
        ("没有字", "封面上没有字"),
    )),
    ("img_text_has_number", (
        ("有", "封面的字里有数字"),
        ("没有", "封面的字里没有数字"),
        ("没有字", "封面上没有字"),    # 和 img_text_kind 的「没有字」是同一件事: 那边说了这边就不说(见 build_note)
    )),
    ("img_decoration", (
        ("有", "封面上有后期加的贴纸、箭头一类的记号"),
        ("没有", "封面上没有后期加的贴纸、箭头一类的记号"),
    )),
    ("img_brand_readable", (
        ("能", "封面上认得出品牌名或商标"),
        ("不能", "封面上认不出品牌名或商标"),
    )),
)

CAVEAT = "这些是模型看图答的, 没有人对着图核过; 只说明这几篇帖的封面长这样, 不是规律, 不用照着做。"
NOTE = "封面参考(只是计数): 这次借到的卡里有 {n} 篇的封面有记录, 其中 {clauses}。" + CAVEAT


def source_ids(lessons) -> list[str]:
    """这批卡的源帖 id(去重、保持顺序)。外部参考卡(``external`` 是 True)不是公司的帖, 不算; 没有 id、id 形状不对的不算。"""
    out: dict[str, None] = {}
    for c in lessons or ():
        if not isinstance(c, dict) or c.get("external") is True:
            continue
        raw = c.get("source_note_id")
        if raw is None or isinstance(raw, bool):
            continue
        sid = str(raw).strip()
        if _ID_SHAPE.fullmatch(sid):
            out.setdefault(sid, None)
    return list(out)


def _usable_answers(rows) -> dict[str, dict]:
    """``{note_id: answers}``: 只收 usable 是 True、answers 是对象的行。"""
    out: dict[str, dict] = {}
    for r in rows or ():
        if not isinstance(r, dict) or r.get("usable") is not True:
            continue
        ans, nid = r.get("answers"), r.get("note_id")
        if isinstance(ans, dict) and nid not in (None, ""):
            out[str(nid)] = ans
    return out


def build_note(lessons, rows) -> str | None:
    """这批卡的那一句封面参考; 不该说(有封面信息的不到三篇、没有哪一项凑够两篇)就回 None。纯函数, 不抛给调用方之外的东西。"""
    by_id = _usable_answers(rows)
    covered = [by_id[i] for i in source_ids(lessons) if i in by_id]
    n = len(covered)
    if n < MIN_USABLE:
        return None
    candidates: list[tuple[int, int, int, int, str]] = []      # (-k, 键次序, 取值次序, m, 短语)
    top: dict[str, str] = {}                            # 每道题说的是哪个取值
    for ki, (key, values) in enumerate(_PHRASES):
        if key == _TEXT_NUMBER and top.get(_TEXT_KIND) == _NO_TEXT:
            continue                                    # 已经说了「封面上没有字」: 有没有数字不用再说
        known = {v: (vi, phrase) for vi, (v, phrase) in enumerate(values)}
        counts: dict[str, int] = {}
        for ans in covered:
            v = ans.get(key)
            if isinstance(v, str) and v in known:
                counts[v] = counts.get(v, 0) + 1
        best = sorted((-k, known[v][0], v) for v, k in counts.items() if k >= MIN_COUNT)
        if not best:
            continue
        neg_k, vi, v = best[0]                          # 同一个键只出一项: 最多的那个取值, 不往下找第二多的
        top[key] = v
        m = sum(counts.values())
        if key == _TEXT_NUMBER and v != _NO_TEXT:
            m -= counts.get(_NO_TEXT, 0)                # 没有字的那几篇谈不上有没有数字: 不进分母, 括号里说清
        candidates.append((neg_k, ki, vi, m, known[v][1]))
    if not candidates:
        return None
    clauses = []
    for neg_k, _ki, _vi, m, phrase in sorted(candidates)[:MAX_CLAUSES]:
        clause = f"{-neg_k} 篇{phrase}"
        if m < n:                                       # 有的篇这道题没答成: 说清分母, 免得读成「其余的都不是」
            clause += f"(这一项 {m} 篇有答案)"
        clauses.append(clause)
    return NOTE.format(n=n, clauses=", ".join(clauses))
