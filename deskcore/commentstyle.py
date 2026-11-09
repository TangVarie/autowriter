"""评论的写法底线(2026-11-03): 不用句号、不用方言语气词、不写像要联系方式的字。所有项目的评论都管。

owner 11-03:「写作台加一条，把句号去掉」「修正一下方言的语气……最好不需要人工再来改」「这个v是个啥」。代码查、不靠模型自觉,
两条路都过:
  · 单独写评论(comments_for / score_comments): comments_for 把三句放进回包(style), score_comments 判之前先查, 有一样那条
    就算没过(style_fix), 模型按协议自己改一轮;
  · 跟着笔记交付的评论(项目交付模板里有评论栏: 西屋911gt、Hatherine痘痘贴、百健士-藻油): delivery.requirements 的 task
    里写上三句, delivery.assemble 逐条查, 查出来的进 delivery.problems, 照现有的「先改 problems 再重存」那条路改掉。
只管评论, 不管笔记标题和正文。项目规则说要也不行。
途鸽那批的「v」「伐」是项目里一条旧硬规则要「按比例混入」的(网络缩写 v、方言词「伐」), 句号是另一条「所有文案必须含句号」;
那两条规则的改法另给 owner SQL。「v」在小红书评论里多半是「加 v / vx = 微信」, 像在要联系方式(引流), 平台会判违规; 不是网感。
"""
from __future__ import annotations

import re

LINES = (
    "评论里不用句号: 句末不加, 句中要停顿用逗号或空格",
    "口语用普通话口语, 不用方言语气词(伐、侬、阿拉)",
    "不写 v、vx、加微这类像在要联系方式的字(像引流, 平台会判违规)",
)
# 交付模板的 task 里那一句(和 LINES 同义, 压成一句)
SHORT = "评论不用句号、不用方言语气词(伐、侬、阿拉)、不写 v / vx / 加微这类像要联系方式的字"

# 句号: 全角「。」哪里都不要; 半角只管句末那一个点(「...」省略号不算; 3.5 的点不在句末, 本来就不算, 「今年28.」算)
_PERIOD = re.compile(r"。|(?<!\.)\.\s*$")
# 「伐」只认句末 / 标点前的语气词用法(步伐、讨伐、砍伐不算); 「侬」不认迪卡侬、吴侬软语; 「阿拉」不认阿拉伯、阿拉斯加、
# 阿拉丁、阿拉善、阿拉巴马、阿拉比卡
_DIALECT = re.compile(r"(?<![步讨砍征攻杀])伐(?=[\s？?！!，,。.~～…]|$)|(?<![卡吴])侬|阿拉(?![伯斯丁善巴比])")
# 单独一个 v(前后不是字母数字或点, 后面是标点、空格或结尾; 「V计划」这种后面接汉字的、「I.V.」这种缩写不算)、
# 加微信 / 加v / 加vx、vx、wx、薇信、v我。「加微」只认要联系方式的说法: 前面不是添 / 增 / 参 / 附……(添加微量元素),
# 光一个「微」时后面得是标点、结尾或语气字; 「威信」是常用词(老师的威信), 不查。
_CONTACT = re.compile(
    r"(?i)(?<![a-z0-9.])v(?=[\s。.，,！!？?~～…]|$)"
    r"|(?<![添增参附施追叠倍更])加\s*(?:个\s*)?(?:微信|vx|wx|v(?![a-z]))"
    r"|(?<![添增参附施追叠倍更])加\s*(?:个\s*)?微(?=[\s。.，,！!？?~～…吧呀呢哈了聊我吗啊么嘛]|$)"
    r"|(?<![a-z])(?:vx|wx)(?![a-z])|薇信|(?<![a-z])v我")


def issues(text: str) -> list[str]:
    """一条评论 → 写法底线上要改的地方(空表 = 没问题)。只看文字, 不调模型。"""
    t = text or ""
    out = []
    if _PERIOD.search(t):
        out.append("去掉句号: 句末不加, 句中要停顿用逗号或空格")
    dialect = "".join(f"「{w}」" for w in dict.fromkeys(m.group(0) for m in _DIALECT.finditer(t)))
    if dialect:
        out.append(f"{dialect}是方言语气词, 换成普通话口语(比如「吗」「呀」), 或者不要")
    contact = "".join(f"「{w}」" for w in dict.fromkeys(m.group(0).strip() for m in _CONTACT.finditer(t)))
    if contact:
        out.append(f"去掉{contact}: 像在要联系方式, 平台会判引流")
    return out


# ── 一组评论里以品牌评价收尾的条数(2026-11-09) ──────────────────────────────────────────────────────────────
# owner 11-09: 一组维护评论几乎每条都在最后一句给品牌下个结论(「……, <品牌>这波挺懂行」「……, <品牌>确实让人安心」),
# 太重复;「20 条里有个 3-5 条够多了」。代码数, 不靠模型自觉: 一组里收尾落在品牌上的最多 ceil(25%·n) 条(20 → 5,
# 7 → 2, 4 → 1, 1~4 → 1), 按给的顺序留前面几条, 后面的那几条要改成以事实、感受、细节或提问收尾。品牌放在前面的小句里
# 不算, 照 aw#144 可以自然提、可以写有素材的亲历和推荐; 只管收尾。笔记标题和正文不管(11-09 量过, 桌面稿最后一句落在品牌上的
# 0~6.9%、最后一小句 ≤1.7%, 和发出去的笔记一个水平)。
#
# 收尾怎么数(11-09 复核后收窄): 按标点和空格切小句, 品牌在最后一小句里才算; 最后一小句很短(≤6 字, 「真的稳」)时,
# 品牌在前一小句的末尾(后面最多两个字: 「最后选了<品牌>，挺好」「<品牌>这波，真的稳」)才连上它一起看。品牌在开头、后面
# 接一段事实(「<品牌>帮我改了简历，三天就过了」「<品牌>贴了两晚，红肿退了点」)不算。整条只有一小句时那一句就是收尾,
# 提到品牌就算: 光看字分不出「<品牌>的课我上过」和「<品牌>这波挺懂行」, 四分之一的余地留给它们。
# 已知的小毛病: 两个字的中文品牌按字面找, 会跨词撞上(品牌「天好」撞「今天好累」); 少见, 不另做排除。
BRAND_ENDING_SHARE = 0.25
BRAND_ENDING_LINE = ("一组评论里以品牌评价收尾(最后一两小句落在品牌上)的最多四分之一(20 条最多 5 条, 7 条最多 2 条, 4 条最多 1 条), "
                     "其余以事实、感受、细节或提问收尾, 品牌放在前面的句子里自然提不算; 收尾别都用同一个句式")
# 交付模板 task 里那一句(和上面同义, 按一篇的评论算; 只是写法提示, 交付那条路不数)
BRAND_ENDING_SHORT = "同一篇的评论最多四分之一以品牌评价收尾(4 条以内最多 1 条), 其余以事实、感受、细节或提问收尾"
TEMPLATE_MIN = 3                                   # 一组里几条品牌收尾同一个句式才提示(只提示, 不算没过)

# 收尾那一段: 先去掉末尾的空白、表情、[xx] 贴纸, 再看是不是问句(问句收尾不算品牌评价), 再去掉末尾标点、按标点切小句
_TRAIL_DECOR = re.compile(r"(?:\[[^\[\]]{1,8}\]|[^\w？?。.，,！!；;~～…、:：—\-])+$")
_TRAIL_PUNCT = re.compile(r"(?:\[[^\[\]]{1,8}\]|[^\w])+$")
_CLAUSE_SPLIT = re.compile(r"[，,。！!？?；;~～…、:：—\s]+")
# 没打问号的问句: 句末「吗 / 么」、疑问词收尾、「A不A」(靠不靠谱、贵不贵、有没有)、「…过没有 / …了没有」。
# 「呢」「嘛」两头都有: 「挺好的呢」「挺靠谱的嘛」是陈述, 不算问句; 见 _is_question
_QUESTION_TAIL = ("吗", "么", "怎么样", "咋样", "如何", "啥", "什么", "多少", "几个", "哪个", "哪家", "哪里",
                  "过没有", "了没有")
_A_NOT_A = re.compile(r"(\w)[不没]\1")
_ASK_WORD = re.compile(r"什么|怎么|咋|哪|谁|多少|几[个家次天]|啥|如何|是不是|能不能|可不可以|要不要|有没有")
_NE_ASK = re.compile(r"(?:^(?:那|还有)\S{0,8}|觉得|你们|大家|你|其他人|别人)呢$")
_SHORT_TAIL = 6                                    # 最后一小句这么短(「真的稳」)才考虑连上前一小句
_NEAR_END = 2                                      # 而且品牌在前一小句末尾(后面最多两个字: 「选了<品牌>」「<品牌>这波」)才连
_PLACEHOLDER = "＠"
_SEP = re.compile(r"[\s\-_·/|（）()【】\[\]+&、，,]+")


def brand_aliases(brand: str | None, name: str | None = "", extra=()) -> list[str]:
    """项目的品牌叫法: projects.brand、交付模板的 must_mention_any, 品牌里的英文那一段(「ABC轻享」→ ABC)。都没有时退回
    项目名(按分隔符切开, 两个字以上的段)。不分大小写比, 长的排前面。"""
    out: list[str] = []
    for raw in [brand, *(extra or ())]:
        s = (raw or "").strip() if isinstance(raw, str) else ""
        if s:
            out.append(s)
            out += re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", s)
    if not out and isinstance(name, str):
        out = [p for p in _SEP.split(name.strip()) if len(p) >= 2]
    seen, uniq = set(), []
    for a in out:
        if a.casefold() not in seen:
            seen.add(a.casefold())
            uniq.append(a)
    return sorted(uniq, key=len, reverse=True)


def brand_ending_allowed(n: int) -> int:
    """一组 n 条里最多几条以品牌评价收尾: ceil(25%·n), 至少 1。"""
    return max(1, -(-n * int(BRAND_ENDING_SHARE * 100) // 100))


def brand_ending_allowed(n: int) -> int:
    """一组 n 条里最多几条以品牌评价收尾: ceil(25%·n), 至少 1。"""
    return max(1, -(-n * int(BRAND_ENDING_SHARE * 100) // 100))


def _alias_re(aliases) -> re.Pattern | None:
    parts = []
    for a in aliases or ():
        a = a.strip()
        if not a:
            continue
        p = re.escape(a)
        if a.isascii():                             # 英文牌子不认别的词里的那几个字母
            p = rf"(?<![A-Za-z]){p}(?![A-Za-z])"
        parts.append(p)
    return re.compile("|".join(parts), re.IGNORECASE) if parts else None


def _is_question(last: str) -> bool:
    """没打问号时, 最后一小句是不是问句。「呢」要有别的疑问的样子(疑问词、「那…呢」「你们呢」「觉得呢」);
    「嘛」前面是「的 / 了 / 啦」时是陈述(「挺靠谱的嘛」), 别的(「能退款嘛」)当问句。"""
    if last.endswith(_QUESTION_TAIL) or _A_NOT_A.search(last):
        return True
    if last.endswith("呢"):
        return bool(_ASK_WORD.search(last) or _NE_ASK.search(last))
    if last.endswith("嘛"):
        return not re.search(r"[的了啦]嘛$", last)
    return False


def _tail(text: str) -> tuple[str, str, bool]:
    """一条评论 → ``(前一小句, 最后一小句, 是不是问句)``; 只有一小句时前一小句是空的。空格也切(「…报的班 简历改了三版」)。"""
    t = _TRAIL_DECOR.sub("", (text or "").strip())
    question = bool(re.search(r"[？?][！!~～…。.]*$", t))
    t = _TRAIL_PUNCT.sub("", t)
    clauses = [c for c in _CLAUSE_SPLIT.split(t) if c]
    if not clauses:
        return "", "", question
    last = clauses[-1]
    return (clauses[-2] if len(clauses) >= 2 else ""), last, question or _is_question(last)


def _joins(prev: str, last: str) -> bool:
    return bool(prev) and len(last) <= _SHORT_TAIL


def _brand_tail(text: str, rx: re.Pattern | None) -> tuple[str, str] | None:
    """这条以品牌收尾时回 ``(落在品牌上的那一两小句, 品牌叫法)``, 否则 None。问句不算。"""
    if rx is None:
        return None
    prev, last, question = _tail(text)
    if question or not last:
        return None
    m = rx.search(last)
    if m:
        return last, m.group(0)
    if _joins(prev, last):
        ms = list(rx.finditer(prev))
        if ms and len(prev) - ms[-1].end() <= _NEAR_END:
            return f"{prev}，{last}", ms[-1].group(0)
    return None


def brand_endings(texts: list[str], aliases, labels: list[str] | None = None) -> dict:
    """一组评论里以品牌评价收尾的那几条。纯函数, 不调模型。

    以品牌评价收尾 = 最后一小句里有品牌叫法(最后一小句很短时, 品牌在前一小句末尾的也算, 见 ``_brand_tail``),
    而且不是问句(问句收尾 owner 允许)。按给的顺序留前 ``allowed`` 条, 后面的是超出的。另外数这些品牌收尾的句式: 末三个字
    一样、或「<品牌>这…」这样以品牌起头的两个字一样, 够 ``TEMPLATE_MIN`` 条就给一句提示(不超上限也提示, 只是提示;
    不落在品牌上的收尾不数)。

    回 ``{"n", "allowed", "hits", "excess", "fix": {下标: 一句改法}, "thread_fix": str | None, "thread_hint": str | None}``。
    ``labels``: 每条在回包里怎么称呼(「位 q1」「第 3 条」), 不给就是「第 k 条」。
    """
    n = len(texts)
    labels = labels or [f"第 {i + 1} 条" for i in range(n)]
    allowed = brand_ending_allowed(n)
    rx = _alias_re(aliases)
    found: dict[int, tuple[str, str]] = {}
    for i, t in enumerate(texts):
        bt = _brand_tail(t, rx)
        if bt:
            found[i] = bt
    hits = sorted(found)
    excess = hits[allowed:]
    fix = {i: (f"这条以品牌评价收尾(收尾「{found[i][0]}」落在「{found[i][1]}」上); 同一组 {n} 条评论最多 {allowed} 条这样收, "
               f"前面已经有 {allowed} 条: 把品牌挪到前面的句子里或删掉这句评价, 改成以事实、感受、细节或提问收尾, "
               "别换个说法再夸一次品牌")
           for i in excess}
    thread_fix = None
    if excess:
        kept = "、".join(labels[i] for i in hits[:allowed])
        thread_fix = (f"整组 {n} 条里 {len(hits)} 条以品牌评价收尾, 最多 {allowed} 条: 按顺序留前 {allowed} 条({kept}), "
                      f"{'、'.join(labels[i] for i in excess)} 改成以事实、感受、细节或提问收尾, 收尾别都用同一个句式; "
                      "品牌放在前面的句子里不算, 不用删")
    groups: dict[str, list[int]] = {}
    for i in hits:
        u = rx.sub(_PLACEHOLDER, found[i][0].replace("，", ""))
        if len(u) >= 3:
            groups.setdefault("…" + u[-3:], []).append(i)
        at = u.find(_PLACEHOLDER)
        if 0 <= at < len(u) - 1:
            groups.setdefault(f"<品牌>{u[at + 1]}…", []).append(i)
    rep = [(k, v) for k, v in groups.items() if len(v) >= TEMPLATE_MIN]
    thread_hint = None
    if rep:
        k, v = max(rep, key=lambda kv: len(kv[1]))
        thread_hint = (f"收尾句式重复: {'、'.join(labels[i] for i in v)} 都以品牌收尾, 句式都是「{k}」, "
                       "挑几条换个收法(事实、感受、细节或提问)")
    return {"n": n, "allowed": allowed, "hits": hits, "excess": excess, "fix": fix,
            "thread_fix": thread_fix, "thread_hint": thread_hint}
