"""评论的写法底线(2026-11-03): 不用句号、不用方言语气词、不写像要联系方式的字。所有项目的评论都管。

owner 11-03:「写作台加一条，把句号去掉」「修正一下方言的语气……最好不需要人工再来改」「这个v是个啥」。代码查、不靠模型自觉,
两条路都过:
  · 单独写评论(comments_for / score_comments): comments_for 把这几句放进回包(style), score_comments 判之前先查, 有一样那条
    就算没过(style_fix), 模型按协议自己改一轮;
  · 跟着笔记交付的评论(项目交付模板里有评论栏: 西屋911gt、Hatherine痘痘贴、百健士-藻油): delivery.requirements 的 task
    里写上三句, delivery.assemble 逐条查, 查出来的进 delivery.problems, 照现有的「先改 problems 再重存」那条路改掉。
只管评论, 不管笔记标题和正文。项目规则说要也不行。
途鸽那批的「v」「伐」是项目里一条旧硬规则要「按比例混入」的(网络缩写 v、方言词「伐」), 句号是另一条「所有文案必须含句号」;
那两条规则的改法另给 owner SQL。「v」在小红书评论里多半是「加 v / vx = 微信」, 像在要联系方式(引流), 平台会判违规; 不是网感。

2026-11-10 每条再加两样(同样所有项目都管、两条路都过): 不写「帖子里」(帖里、帖子中也一样), 不用「X那栏」「X那次」「X那阵」
这类含糊指代(读者不知道指的是哪一栏、哪一次)。一组评论层面的几样(重复的说法和开头、项目定的品牌 / 中性 / 短评条数)在下面
``repeated_clauses`` / ``brand_share`` / ``neutral_share`` / ``short_share``, 只在单独写评论那条路(score_comments)上数。
"""
from __future__ import annotations

import re

LINES = (
    "评论里不用句号: 句末不加, 句中要停顿用逗号或空格",
    "口语用普通话口语, 不用方言语气词(伐、侬、阿拉)",
    "不写 v、vx、加微这类像在要联系方式的字(像引流, 平台会判违规)",
    "不写「帖子里」(开头、句中、结尾都不写; 帖里、帖子中也一样): 接帖子时直接说那件事",
    "不用含糊的指代(「社团那栏」「三小时那次」「卡住那阵」「X那几个字」「X那条路」): 读者不知道指的是哪个, 直接说是什么事",
)
# 交付模板的 task 里那一句(和 LINES 同义, 压成一句)
SHORT = ("评论不用句号、不用方言语气词(伐、侬、阿拉)、不写 v / vx / 加微这类像要联系方式的字、不写「帖子里」、"
         "不用「X那次」「X那栏」这类含糊指代(直接说是哪件事)")
# 只是写法提示, 代码不查(2026-11-10)
SENTENCE_LINE = "长评论也多用短句: 一句话说一件事, 别一口气连好几个从句"

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
# 「帖子里」(2026-11-10, owner: 开头、句中、结尾都不要): 帖子里 / 帖子中 / 帖子内 / 帖里 / 帖中 / 贴子里……。
# 请帖、字帖、碑帖这些不是帖子, 妥帖、熨帖、服帖也不是(「做事妥帖内敛」); 「补贴里」「贴里」不查(贴子要带「子」才算)
_POST_REF = re.compile(r"(?<![请字庚碑名喜妥熨服])帖子?[里中内]|贴子[里中内]")
# 含糊指代(2026-11-10, owner 举的五个都是「<一个词>那<量词>」: X那栏、X那几个字、X那条路、X那次、X那阵, 指帖子里的某处,
# 读者不知道是哪一处)。只认这个结构, 不列禁用词:
#   · 「那」前面得是一个字(汉字 / 字母 / 数字)或者右引号, 不是句首、标点后: 句首的「那阵真慌」「那次以后」是普通说法;
#     引号括起来的词后面接「那几个字」「那栏」(「“摸鱼”那几个字」)就是在指着帖子里的那几个字, 人写的评论里一条都没有;
#   · 前面那个字不是代词、助词或常带宾语的动词 / 介词(我那次、的那次、走那条路、在那栏、记得那次……): 那是在说自己的事
#     或者「动词 + 那个东西」, 不是指着帖子; 同一小句里「那」前四个字以内有「我」的也不算(「我面挂那次」「我X那阵」);
#   · 量词只认五样: 栏 / 格、(几 / 两…个)字、条路、次、阵(数词、「个」可有可无); 那么 / 那样 / 那时 / 那里 / 那儿 / 那会儿 /
#     那天 / 那些 / 那种 / 那边 / 那就 / 那是 / 那一刻 / 那一下 / 那段时间都不在里面, 一概不查;
#   · 量词后面接的是它量的那个东西时不算: 那格局、那字幕、那次元、那阵风、那阵瘾……;
#   · 「这」不查: 人写的「这」几乎都指同一条评论里刚说过的那句(「……, 这句话太对了」「X这点挺好」), 不是指帖子。
# 11-10 拿人写的评论量过(只读): truth_vault.comments 10588 条里 7 条(0.07%), 别的项目的评论区快照 3817 条里 1 条, 都是
# 「X那阵子 / X那次」说自己的事; 运营写的维护评论 4708 行里 30 行(0.6%), 是同一个结构(「X那几次」「X那栏」), 没有一条是
# 那么 / 那样这类普通说法。owner 举的五个(换成假的上下文)全中。
_QUOTE_CLOSE = "”」』\"'"
_VAGUE_REF = re.compile(
    r"(?<=[\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9”」』\"'])"
    r"(?<![我你他她它您们咱俺的了着地得把被在从到对跟和给让往向比像是有就也都还才又再要会能该走选换])"
    r"那[一两俩仨几三四五六七八九十]?个?(?:栏|格(?![局调式外])|字(?![幕体号典迹母])|条路|次(?![元数])"
    r"|阵(?![仗容营势地线瘾雨风痛]))")
_VAGUE_OWN = 4                                       # 「那」前面几个字以内有「我」就是在说自己的事
_VAGUE_CLAUSE = re.compile(r"[，,。.！!？?；;~～…、:：—\s【】\[\]（）()《》]")
_HEAD_CUT = re.compile(r"[，,。.！!？?；;~～…、:：—\s\"“”「」『』【】\[\]（）()《》的]")
_QUOTED_HEAD = re.compile(r"[“「『\"'][^“”「」『』\"'\n]{1,12}[”」』\"']$")


def _vague_refs(t: str) -> list[str]:
    """一条评论里的含糊指代, 带上前面那个词(最多四个字, 不跨标点; 引号括起来的词整个带上): 「社团那栏」「三小时那次」
    「“摸鱼”那几个字」。"""
    out = []
    for m in _VAGUE_REF.finditer(t):
        if t[m.start() - 1] in _QUOTE_CLOSE:
            q = _QUOTED_HEAD.search(t[max(0, m.start() - 14):m.start()])
            out.append((q.group(0) if q else t[m.start() - 1]) + m.group(0))
            continue
        clause = _VAGUE_CLAUSE.split(t[max(0, m.start() - _VAGUE_OWN):m.start()])[-1]
        if "我" in clause:
            continue
        out.append(_HEAD_CUT.split(clause)[-1] + m.group(0))
    return out


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
    post = "".join(f"「{w}」" for w in dict.fromkeys(m.group(0) for m in _POST_REF.finditer(t)))
    if post:
        out.append(f"去掉{post}: 不写「帖子里」, 接帖子就直接说那件事(「帖子里说的 X」→ 直接说 X)")
    vague = "".join(f"「{w}」" for w in dict.fromkeys(_vague_refs(t)))
    if vague:
        out.append(f"{vague}是含糊的指代, 读者不知道指的是哪一栏、哪一次、哪件事: 直接把那件事说出来")
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

# 收尾那一段: 先去掉末尾的空白、表情、[xx] 贴纸, 再看是不是问句(问句收尾不算品牌评价), 再去掉末尾标点、按标点切小句。
# 「[」只给贴纸那一支吃, 两支不重叠: 重叠时一串贴纸后面接正文(「[赞R]」× 20 + 字)会指数回溯, 卡住判评论(codex on aw#147)
_TRAIL_DECOR = re.compile(r"(?:\[[^\[\]]{1,8}\]|[^\w？?。.，,！!；;~～…、:：—\-\[])+$")
_TRAIL_PUNCT = re.compile(r"(?:\[[^\[\]]{1,8}\]|[^\w\[])+$")
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


def _alias_re(aliases) -> re.Pattern | None:
    """品牌叫法 → 一个正则。只有一个字的叫法不用(2026-11-10): 一个汉字的品牌按字面找会撞上一堆普通词(单字「安」撞「安心」
    「平安」), 一个字母更不用说; 那种项目按没有品牌叫法算。"""
    parts = []
    for a in aliases or ():
        a = a.strip()
        if len(a) < 2:
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


# ── 一组评论的配比和重复(2026-11-10) ───────────────────────────────────────────────────────────────────────
# owner 11-10 对一组维护评论定的几样, 都由代码数、不靠模型自觉, 只在单独写评论那条路(score_comments)上数(跟着笔记交付的
# 评论一篇只有 2~3 条, 有的模板还要求每条点名品牌, 不数):
#   · 重复的说法(所有项目): 同一个说法整组只用一次, 开头别几条一样 —— ``repeated_clauses``;
#   · 项目定的配比(只有在 projects.default_params.comment_set 里配了的项目): 提品牌的最多 brand_max(一半), 中性的(不提品牌、
#     也不夸不劝)至少 neutral_min(三成), short_chars 字以内的一句话短评至少 short_min(两成) —— ``brand_share`` /
#     ``neutral_share`` / ``short_share``。数字都从项目配置来, 这里只管怎么数。
# 份额一律按整数百分比算(n × pct // 100): 浮点在个别 n 上差一条(25 × 0.28 = 7.000000000000001, 向上取整成了 8)。
REPEAT_LINE = ("同一组不重复说法: 同一个说法(日期、活动名、合同条款这类)整组只用一次, 帖子下已有的评论里用过的也别再用; "
               "开头别几条一样(「<品牌>把…」这种最多两条)")
NEUTRAL_ACTS = ("提问", "质疑", "补充经验", "回复答疑", "闲聊表情")   # Jev speech_act 里算「不夸也不劝」的几样
REPEAT_RUN = 6                                      # 规则 A: 连着 6 个字(英文一个词算一个字)在前面哪条里出现过
REPEAT_THRICE_RUN = 5                               # 规则 B: 连着 5 个字在 3 条以上里出现
OPENER_TOKENS, OPENER_MAX = 2, 2                    # 规则 C: 开头 2 个字一样的最多 2 条
_FIX_PHRASES = 2                                    # 一条里最多点名几处重复

_STICKER = re.compile(r"\[[^\[\]]{1,8}\]")
_CJK = re.compile(r"[㐀-䶿一-鿿]")
_VISIBLE = re.compile(r"[㐀-䶿一-鿿A-Za-z0-9]")
_TOKEN_BREAK = re.compile(r"[，,。.！!？?；;~～…、:：—\-\s\"'“”‘’「」『』（）()【】《》<>/|·]")


def _pct(share) -> int:
    return int(round(float(share) * 100))


def share_floor(n: int, share) -> int:
    """n 条里按 share 算最多几条: floor, 整数百分比(20 × 50% → 10, 7 → 3)。"""
    return n * _pct(share) // 100


def share_ceil(n: int, share) -> int:
    """n 条里按 share 算至少几条: ceil, 整数百分比(20 × 30% → 6, 不是浮点的 7)。"""
    return -(-n * _pct(share) // 100)


def _labels(labels, n: int) -> list[str]:
    return list(labels) if labels else [f"第 {i + 1} 条" for i in range(n)]


def brand_share(texts: list[str], aliases, labels: list[str] | None = None, *, brand_max) -> dict:
    """一组里提到品牌的超过 floor(brand_max·n) 时, 按给的顺序留前面几条, 后面的要去掉品牌。纯函数, 不调模型。

    提到品牌 = 品牌叫法在这条里任何地方出现(只有一个字的叫法不算, 见 ``_alias_re``)。代称(这家、它家、缩写)不数: 改法里说了
    不许换成代称。回 ``{"n", "allowed", "named", "excess", "fix": {下标: 一句改法}, "segment": 整组那一句或 None}``。"""
    n = len(texts)
    labels = _labels(labels, n)
    rx = _alias_re(aliases)
    hit = {i: m.group(0) for i, t in enumerate(texts) if rx is not None and (m := rx.search(t or ""))}
    named = sorted(hit)
    allowed = share_floor(n, brand_max)
    excess = named[allowed:]
    # 一组只有一条时 floor(50%·1) = 0: 那一条也不能提品牌, 别说「前面已经有 0 条」这种话
    cap = (f"同一组 {n} 条最多 {allowed} 条提品牌, 前面已经有 {allowed} 条" if allowed
           else f"同一组 {n} 条时一条都不能提品牌")
    fix = {i: (f"这条提到了品牌(「{hit[i]}」); {cap}: 去掉品牌, "
               "也不换成代称(这家、它家、缩写); 能改成不夸的提问、观望或自己的经历更好")
           for i in excess}
    segment = None
    if excess:
        keep = f"按顺序留前 {allowed} 条, " if allowed else ""
        segment = (f"提品牌的 {len(named)} 条, 最多 {allowed} 条: {keep}"
                   f"{'、'.join(labels[i] for i in excess)} 去掉品牌(不换成代称)")
    return {"n": n, "allowed": allowed, "named": named, "excess": excess, "fix": fix, "segment": segment}


def neutral_share(texts: list[str], aliases, labels: list[str] | None = None, *, neutral_min, speech_acts,
                  prefer=()) -> dict:
    """中性 = 不提品牌, 而且 Jev 判的主要行为在 ``NEUTRAL_ACTS`` 里(提问、质疑、补充经验、答疑、闲聊); 判成说不清、没判出来
    的不算。一组至少 ceil(neutral_min·n) 条, 不够时整组没过, 建议改哪几条: ``prefer``(提品牌超出的那几条)先, 再从后往前挑
    不中性的。``speech_acts`` 是 None(判定没判成)时不数, ``checked=False``。"""
    n = len(texts)
    labels = _labels(labels, n)
    need = share_ceil(n, neutral_min)
    if speech_acts is None or len(speech_acts) != n:
        return {"n": n, "need": need, "checked": False, "neutral": [], "short_by": 0, "candidates": [],
                "segment": f"中性的条数这次没数(判定没判成; 这个项目一组至少 {need} 条不提品牌也不夸)"}
    rx = _alias_re(aliases)
    neutral = [i for i, t in enumerate(texts)
               if not (rx is not None and rx.search(t or "")) and speech_acts[i] in NEUTRAL_ACTS]
    short_by = max(0, need - len(neutral))
    candidates: list[int] = []
    if short_by:
        pool = [i for i in prefer if i not in neutral] + [i for i in reversed(range(n))
                                                          if i not in neutral and i not in prefer]
        candidates = pool[:short_by]
    segment = None
    if short_by:
        segment = (f"中性的(不提品牌、也不夸不劝: 提问、观望、自己的经历) {len(neutral)} 条, 至少 {need} 条: "
                   f"把 {'、'.join(labels[i] for i in candidates)} 改成这样")
    return {"n": n, "need": need, "checked": True, "neutral": neutral, "short_by": short_by,
            "candidates": candidates, "segment": segment}


def visible_len(text: str) -> int:
    """数字数: 去掉空白、标点、表情、[xx] 贴纸, 汉字、字母、数字各算一个(品牌按它字面的长度算)。"""
    return len(_VISIBLE.findall(_STICKER.sub("", text or "")))


def short_share(texts: list[str], labels: list[str] | None = None, *, short_chars: int, short_min) -> dict:
    """一组里 short_chars 字以内的一句话短评至少 ceil(short_min·n) 条。不够时建议改哪几条: 先挑提问(问句好压成一句),
    再从后往前挑。回 ``{"n", "need", "short", "short_by", "candidates", "segment"}``。"""
    n = len(texts)
    labels = _labels(labels, n)
    need = share_ceil(n, short_min)
    short = [i for i, t in enumerate(texts) if visible_len(t) <= short_chars]
    short_by = max(0, need - len(short))
    candidates: list[int] = []
    if short_by:
        rest = [i for i in range(n) if i not in short]
        asks = [i for i in rest if _tail(texts[i])[2]]
        candidates = (asks + [i for i in reversed(rest) if i not in asks])[:short_by]
    segment = None
    if short_by:
        segment = (f"{short_chars} 字以内的一句话短评 {len(short)} 条, 至少 {need} 条: "
                   f"把 {'、'.join(labels[i] for i in candidates)} 改成一句话短评(比如蹲一个、同问)")
    return {"n": n, "need": need, "short": short, "short_by": short_by, "candidates": candidates,
            "segment": segment}


def _clauses(text: str, rx: re.Pattern | None) -> list[list[tuple[str, int, int]]]:
    """一条评论 → 按标点和空格切开的小句, 每句是 ``(字, 起, 止)`` 的序列(起止是在原文里的位置, 拿来引原话)。

    先把 [xx] 贴纸换成断句; 品牌叫法(不管长短)整个算一个字 ＠; 汉字一个字一个; 英文 / 数字连着的一段算一个(小写);
    表情和别的符号直接跳过。"""
    t = _STICKER.sub(lambda m: " " * len(m.group(0)), text or "")
    starts = {m.start(): m.end() for m in rx.finditer(t)} if rx is not None else {}
    out: list[list[tuple[str, int, int]]] = []
    cur: list[tuple[str, int, int]] = []
    i, n = 0, len(t)
    while i < n:
        if i in starts and starts[i] > i:
            cur.append((_PLACEHOLDER, i, starts[i]))
            i = starts[i]
            continue
        ch = t[i]
        if _CJK.match(ch):
            cur.append((ch, i, i + 1))
            i += 1
        elif ch.isascii() and ch.isalnum():
            j = i
            while j < n and t[j].isascii() and t[j].isalnum():
                j += 1
            cur.append((t[i:j].lower(), i, j))
            i = j
        else:
            if _TOKEN_BREAK.match(ch) and cur:
                out.append(cur)
                cur = []
            i += 1
    if cur:
        out.append(cur)
    return out


def _grams(clauses, size: int) -> list[tuple[tuple[str, ...], int, int]]:
    return [(tuple(tok for tok, _, _ in c[k:k + size]), c[k][1], c[k + size - 1][2])
            for c in clauses for k in range(len(c) - size + 1)]


def _merge(spans: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """重叠 / 相连的几段并成一段, 留第一段的出处。"""
    out: list[list] = []
    for s, e, src in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e, src])
    return [tuple(x) for x in out]


def repeated_clauses(texts: list[str], aliases, labels: list[str] | None = None, *, earlier=(), existing=()) -> dict:
    """一组评论里重复的说法和开头。纯函数, 不调模型。

    先去掉 [xx] 贴纸、表情、标点, 品牌叫法换成同一个 ＠, 按标点和空格切小句(一段重复不跨小句), 汉字一个字一个、英文一个词一个。
      · 规则 A: 连着 ``REPEAT_RUN``(6)个字, 在这组前面哪条、``earlier``(同一批前几次送判的)或 ``existing``(帖子下已有的评论)
        里出现过 —— 后面这条要改, 说清和哪条重复;
      · 规则 B: 连着 ``REPEAT_THRICE_RUN``(5)个字在 3 条以上里出现(``earlier`` 也算) —— 第 3 条起要改;
      · 规则 C: 开头 ``OPENER_TOKENS``(2)个字一样的超过 ``OPENER_MAX``(2)条(``earlier`` 也算, 「＠把」这种) —— 第 3 条起要改。
    帖子正文里的说法(日期、活动名)不豁免, 第一次用不算重复。只改这组里的(``earlier`` / ``existing`` 只当参照)。

    回 ``{"fix": {下标: [改法, ...]}, "flagged": [下标], "segment": 整组那一句或 None}``。"""
    n = len(texts)
    labels = _labels(labels, n)
    rx = _alias_re(aliases)
    sets = [_clauses(t, rx) for t in texts]
    olds = [_clauses(t, rx) for t in earlier or ()]
    refs = [_clauses(t, rx) for t in existing or ()]
    old_labels = [f"之前送判的第 {k + 1} 条" for k in range(len(olds))]
    fix: dict[int, list[str]] = {}
    # 规则 A
    seen: dict[tuple, str] = {}
    for cl in refs:
        for g, _, _ in _grams(cl, REPEAT_RUN):
            seen.setdefault(g, "帖子下已有的评论")
    for k, cl in enumerate(olds):
        for g, _, _ in _grams(cl, REPEAT_RUN):
            seen.setdefault(g, old_labels[k])
    a_spans: dict[int, list[tuple[int, int, str]]] = {}
    for i, cl in enumerate(sets):
        grams = _grams(cl, REPEAT_RUN)
        hits = [(s, e, seen[g]) for g, s, e in grams if g in seen]
        if hits:
            a_spans[i] = _merge(hits)
        for g, _, _ in grams:
            seen.setdefault(g, labels[i])
    # 规则 B: 每个 5 字串出现在哪几条里(按顺序: 之前的在前)
    where: dict[tuple, list] = {}
    for key, cl in [(("old", k), c) for k, c in enumerate(olds)] + [(("set", i), c) for i, c in enumerate(sets)]:
        for g, s, e in _grams(cl, REPEAT_THRICE_RUN):
            occ = where.setdefault(g, [])
            if not occ or occ[-1][0] != key:
                occ.append((key, s, e))
    b_spans: dict[int, list[tuple[int, int, str]]] = {}
    for g, occ in where.items():
        if len(occ) <= 2:
            continue
        first = "、".join(old_labels[k] if kind == "old" else labels[k] for (kind, k), _, _ in occ[:2])
        for (kind, i), s, e in occ[2:]:
            if kind == "set" and not any(s0 <= s and e <= e0 for s0, e0, _ in a_spans.get(i, [])):
                b_spans.setdefault(i, []).append((s, e, first))
    for i, spans in a_spans.items():
        fix.setdefault(i, []).extend(
            f"「{texts[i][s:e]}」和{src}重复, 整组同一个说法只用一次: 换个说法或换件事说" for s, e, src in spans[:_FIX_PHRASES])
    for i, spans in b_spans.items():
        fix.setdefault(i, []).extend(
            f"「{texts[i][s:e]}」{src}已经用过, 整组同一个说法只用一次: 换个说法或换件事说"
            for s, e, src in _merge(spans)[:_FIX_PHRASES])
    # 规则 C
    openers: dict[tuple, list] = {}
    for key, cl in [(("old", k), c) for k, c in enumerate(olds)] + [(("set", i), c) for i, c in enumerate(sets)]:
        if cl and len(cl[0]) >= OPENER_TOKENS:
            head = cl[0][:OPENER_TOKENS]
            openers.setdefault(tuple(tok for tok, _, _ in head), []).append((key, head[0][1], head[-1][2]))
    c_rows = []
    for g, occ in openers.items():
        if len(occ) <= OPENER_MAX:
            continue
        first = "、".join(old_labels[k] if kind == "old" else labels[k] for (kind, k), _, _ in occ[:OPENER_MAX])
        later = [(i, s, e) for (kind, i), s, e in occ[OPENER_MAX:] if kind == "set"]
        for i, s, e in later:
            fix.setdefault(i, []).append(f"开头「{texts[i][s:e]}」已经有 {OPENER_MAX} 条({first}), 换个开头")
        if later:
            i, s, e = later[0]
            c_rows.append((len(occ), texts[i][s:e]))
    # 整组那一句: 用得最多的一两个说法和开头
    counts: dict[tuple, tuple[int, str]] = {}
    flat = [_flat(cl) for cl in refs + olds + sets]
    for i, spans in list(a_spans.items()) + list(b_spans.items()):
        for s, e, _ in spans:
            key = tuple(tok for c in _clauses(texts[i][s:e], rx) for tok, _, _ in c)
            if key and key not in counts:
                probe = "\x1f" + "\x1f".join(key) + "\x1f"
                counts[key] = (sum(1 for f in flat if probe in f), texts[i][s:e])
    top = sorted(counts.values(), key=lambda x: -x[0])[:2]
    parts = [f"「{p}」×{c}" for c, p in top] + [f"开头「{p}」×{c}" for c, p in sorted(c_rows, reverse=True)[:1]]
    segment = ("重复的说法 " + "、".join(parts) + ": 同一个说法整组只用一次, 开头别几条一样") if parts else None
    return {"fix": fix, "flagged": sorted(fix), "segment": segment}


def _flat(clauses) -> str:
    """小句们 → 一个串, 拿来数一个说法在这条里有没有: 字之间用 \\x1f 隔开, 小句之间用 \\x1e(一段说法不会跨过它)。"""
    return "".join("\x1e\x1f" + "\x1f".join(tok for tok, _, _ in c) + "\x1f\x1e" for c in clauses)
