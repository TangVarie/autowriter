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
       - ``forbidden_regex``：``{"pattern": "..."}`` ─ 用 re.search，
         可用内联标志 ``(?i)`` 不区分大小写
     不传 ``rule_kind`` 或传 ``"free_text"`` 时退回到下面的正则抽取。
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

import re
from typing import Optional


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
    """
    kind = (rule.get("rule_kind") or "").strip()
    if not kind or kind == "free_text":
        return None
    payload = rule.get("rule_payload") or {}
    if isinstance(payload, str):
        # 老部署可能把 JSONB 当字符串存了，兜底解析一下
        try:
            import json as _json
            payload = _json.loads(payload)
        except Exception:
            payload = {}
    if kind == "forbidden_word":
        target = str(payload.get("target", "")).strip()
        return {"kind": kind, "target": target} if target else None
    if kind == "required_phrase":
        target = str(payload.get("target", "")).strip()
        return {"kind": kind, "target": target} if target else None
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
        # Day 3: 优先用结构化字段，回退到正则抽取
        spec = _spec_from_structured(rule) or _parse_rule(content)
        if not spec:
            continue
        kind = spec["kind"]
        if kind == "forbidden_word":
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
                m = re.search(pattern, combined)
                if m:
                    out.append({
                        "rule":  content,
                        "kind":  kind,
                        "match": m.group(0)[:60],
                    })
            except re.error as exc:
                # 正则编译失败不算违规——但要埋一行日志，否则用户在 UI 里
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
