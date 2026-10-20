"""deskcore/structure.py — 简报里的「最近的稿子在哪几处都一样」(recent_structure, 2026-10-20)。纯函数, 不读库。

10-20 生产诊断: 稿子篇幅短、批批长得像, 飞书里大半被人重写。recent_titles 只管标题撞词, 管不到骨架: 模型不知道
自己最近几十篇在哪几处写成了同一个样子。这份数据其实早就有 —— 每篇打分的稿子 Jev 都按 TV 的特征题库(fq-v0.1)
答一遍, 写进 ``truth_vault.note_feature_answers``(subject_type = aw_version, subject_id = versions.id), 只是写作台
把回包里的 profile 扔了。读法在 ``store.recent_structure_answers``(只取答案列, 不取 evidence —— 那是稿子原文片段);
这里只决定问哪几题、怎么算「扎堆」、给模型看什么。

问哪几题(10-20 查生产库: 近 169 篇打过分的稿子 Jev 答案 vs 2,500 多篇已发笔记的 sonnet 答案): 只挑「两种答案都是
正经写法、而已发笔记本身不扎堆」的结构题。已发笔记自己就七八成落在同一边的题, 稿子扎堆是常态不是趋同, 报出来只会
把模型往偏离已发笔记的方向推:
  · 选: opening_type(已发笔记最多的一种也只占 45%, 有个项目的稿子 86% 第一句是具体事件);
    title_is_question(标题句式是发牌坐标, 疑问句只占 8 种之一, 已发笔记 32% 是问句, 有个项目的稿子 82% 是);
    negative_outcome_happened(已发 35%, 有个项目的稿子 95% 都写了已经发生的坏结果);
    turning_point / comparison_group / calls_out_reader_group(已发笔记分别只有 15% / 6% / 13% 用, 稿子用到六成以上
    就是同一个套路反复用)。
  · 不选: product_role(产品在文中的角色, 见下); narrator_identity / own_experience(已发笔记 81% / 86% 都是「是」,
    题库的 aw_instruction 还在鼓励交代身份);
    has_specific_time / place / direct_quote / body_sensation(具体性: 扎堆在「有」是好事, 叫一半稿子换掉就是叫它们
    写空); divisive_claim / judged_by_others(立场和情绪场面, 情绪杠杆是发牌坐标管的); withholds_product_name
    (和 product_role 一样是露出); invites_sharing(和下面那条轮换中的规律同属评论诱导, 一起报会和规律打架);
    efficacy_promise(合规观察项, 永不下发)。body_len_bucket 这类代码特征稿子没有答案行(Jev 只答模型题), 篇幅/段落
    形状这一维这里做不了。
  · 已验证规律用到的题一律不报(PATTERN_QIDS): 第一批让模型按篇轮换着用它们, 那几处扎不扎堆是那条轮换管的, 是有意的。

怎么算扎堆: 只看 bool 题的「是」、选择题的指定取值(FLAG_ANSWERS)。bool 题落在「否」那边是已发笔记的常态(上面那几个
百分比), 不算趋同。

product_role 不问(10-20 复审): 产品在文中当主角还是顺带一提、只暗示还是不出现, 每一种扎堆都是项目的露出要求, 不是写法
习惯 —— 产品向项目九成稿子产品是主角正是要求本身, 流量向项目九成顺带一提或不出现也是。题库自己也写着这题不单独下发、
要和客户的露出要求一起看。报出来等于叫一半稿子换一种露出方式, 和 p0 打架, 那是客户的决定。
"""

from __future__ import annotations

from collections import Counter

BANK_VERSION = "fq-v0.1"

# 每题: (题目版本, 给模型看的短标签, 合法答案)。标签是题库 ask 的缩写, 选择题的答案原样用题库的取值。
# ⚠️ 版本钉死: 题库改题会升 question_version(旧答案保留、不混用, TV notes_v1_13 的约定), 新版本的行这里认不得,
# 那一题就安静地不报 —— 要接新版本得来这里改标签和取值, 不能让两版答案混着算占比。
_BOOL = frozenset({"是", "否"})
QUESTIONS: dict[str, tuple[int, str, frozenset[str]]] = {
    "opening_type": (2, "正文第一句是哪一种",
                     frozenset({"具体事件", "身份自述", "观点断言", "提问", "数据事实", "感叹情绪", "其他"})),
    "title_is_question": (1, "标题是不是问句", _BOOL),
    "turning_point": (2, "有没有「以前→转折→后来」的前后转折", _BOOL),
    "negative_outcome_happened": (1, "有没有写到已经发生的坏结果", _BOOL),
    "comparison_group": (3, "有没有拿别人和作者自己对照", _BOOL),
    "calls_out_reader_group": (1, "有没有点名某一类读者", _BOOL),
}
# 哪些答案扎堆才报(见模块说明)。没列的题 = 全部取值都报。
FLAG_ANSWERS: dict[str, frozenset[str]] = {
    "title_is_question": frozenset({"是"}),
    "turning_point": frozenset({"是"}),
    "negative_outcome_happened": frozenset({"是"}),
    "comparison_group": frozenset({"是"}),
    "calls_out_reader_group": frozenset({"是"}),
}
# 给模型看的答案标签: 默认就是题库的取值; 「其他」光秃秃一个词模型看不懂是什么, 补上题库 means 里的说法。
ANSWER_LABELS = {("opening_type", "其他"): "其他(列清单、贴参数这类)"}

# 已验证规律(TV prompts/desk_delivery_v1.json 的 patterns, D-116)用到的题: ending_asks_reader / asks_for_help 是模型题,
# body_question_marks / brand_in_title / brand_first_position 是代码特征(稿子没有答案行, 列上是防以后有了)。
# 第一批(validated_patterns_usage)让模型按篇轮换着用这几条 —— 那几处扎堆是有意的, 这里报了就是在和那条轮换打架。
PATTERN_QIDS = frozenset({"ending_asks_reader", "asks_for_help", "body_question_marks",
                          "brand_in_title", "brand_first_position"})

# 真正去问的题: 防着以后有人把规律题加进 QUESTIONS(测试也钉着两边不相交)
ASKED: dict[str, tuple[int, frozenset[str]]] = {
    q: (ver, answers) for q, (ver, _label, answers) in QUESTIONS.items() if q not in PATTERN_QIDS}

MAX_DRAFTS = 30          # 最近这么多篇打过分的稿子(一个 item 算一篇, 取它最新那版)
MIN_DRAFTS = 12          # 少于这么多篇不报: 七八篇里六成是同一种, 可能只是一两批的题材碰巧
SHARE_MIN = 0.6

TASK = (
    "concentrated 是这个项目最近 window 篇打过分的稿子里, 大多数篇写成了同一个样子的几处地方: question 是哪一处, "
    "answer 是扎堆的那种写法, share 是占比。这一批新写的稿子, 每一处都至少有一半换成别的写法, 另一半可以照旧; "
    "不用每篇都换, 也不用一篇把几处全换。比如「正文第一句是哪一种 / 具体事件 / 0.86」, 就让至少一半的稿子第一句"
    "不写具体事件, 改用身份自述、观点断言、提问这类开头。换的是写法, 不动 p0 / stable 定下的东西: 人设照旧, "
    "产品要不要露出、怎么露出照项目要求。优先级: p0 / stable、draw_angles 分到的坐标和切入角度、用户点名要的写法, "
    "都在这一栏之上 —— 分到的标题句式就是疑问句的那篇, 标题照样写问句。这一栏只管新写的稿, 改稿不受它约束; "
    "只用来自己调整, 不用念给用户听。"
)


def recent_structure(drafts: list[dict[str, str]]) -> dict | None:
    """``drafts``: 最近打过分的稿子(新的在前), 每篇 ``{题目 id: 答案}``, 只含有效答案。

    回 ``{"window": n, "concentrated": [{question, answer, share}], "task": TASK}``; 篇数不够或哪一处都不扎堆时回
    None(简报就不带这一栏)。占比的分母是这一题有有效答案的篇数, 那一题有效答案也要够 MIN_DRAFTS 篇。"""
    drafts = [d for d in (drafts or [])[:MAX_DRAFTS] if isinstance(d, dict) and d]
    if len(drafts) < MIN_DRAFTS:
        return None
    hits = []
    for order, (qid, (_ver, label, answers)) in enumerate(QUESTIONS.items()):
        if qid not in ASKED:
            continue
        counts = Counter(d[qid] for d in drafts if d.get(qid) in answers)
        valid = sum(counts.values())
        if valid < MIN_DRAFTS:
            continue
        answer, top = counts.most_common(1)[0]
        share = top / valid
        if share < SHARE_MIN or answer not in FLAG_ANSWERS.get(qid, answers):
            continue
        hits.append((-share, order, {"question": label, "answer": ANSWER_LABELS.get((qid, answer), answer),
                                     "share": round(share, 2)}))
    if not hits:
        return None
    return {"window": len(drafts), "concentrated": [h for _s, _o, h in sorted(hits, key=lambda t: t[:2])],
            "task": TASK}
