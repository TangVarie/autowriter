"""deskcore/tools.py — MCP 工具面。

十个工具, 按写稿的三个阶段分组。设计原则是【一次调用拿全】—— 治员工反馈里
那条「一个项目一般有 5 个提示词要重复操作 5 次, 我的工作台至少有几十个提示词」。

每个工具的 docstring 就是模型看到的说明, 所以写给模型看; 维护者要看的原因
写在 core.py 的注释里。

降级口径:
  · 读类工具出错 → 返回带 error 的可用结构, 不抛(不阻塞写稿)
  · check_drafts 是【唯一例外】→ 出错必须抛。静默放行就是重演
    config.py:132 那个 ENABLE_DEDUP_REGEN 默认关着的老问题。
"""

from __future__ import annotations

import logging
from typing import Any

from . import core, vocab

logger = logging.getLogger("deskcore.tools")


def _safe(fn, *args, **kwargs) -> Any:
    """**可降级的读**的统一包装 —— 返回可用结构 + 【必须留痕】。

    留痕这条是硬要求: TV docs/19:180-200 记过一次事故, librarian 的模型 env
    变量名配错, 每次 LLM 调用失败被 except 吞掉降级成 [], 外面看永远 200,
    查了很久。

    ⚠️ 【什么能包、什么绝不能包】—— 加新工具前先读这段。
    本包装会把异常变成一个**看起来成功**、只多一个 error 字段的结果, 而 hint
    里明写着"写稿可以继续"。所以它只适用于:【读】+【拿不到也只是少点参考】。
    目前只有三个: list_projects / borrow_lessons / my_style。

    绝不能包的两类, 各有各的失败模式:
      · **合规读**(open_project) —— 拿不到 P0 硬约束就照常开写, 产出的是违规
        内容, 而调用方看到的是一份 p0 为空的正常简报。store.shared_memories()
        专门为此【故意不吞异常】, 外面再包一层 _safe 等于把那个设计原样抵消掉。
      · **写**(draw_angles / commit_drafts / record_rule / record_edit /
        label_example) —— 写作台协议对这些没有强制重试步骤。一条用户明确说
        「以后都这样」的 hard 合规规则写失败, 会静默缺席之后的每一份简报;
        发牌没落台账, 同一个角度下批还能再抽出来。

    判据: 失败之后【调用方还会不会当作成功继续往下走】。会 → 不能包。
    (codex review round-5 P1 ×2: open_project 与 record_rule 都踩了这一条)
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — 故意兜底
        logger.exception("%s failed", getattr(fn, "__name__", fn))
        return {"error": f"{type(exc).__name__}: {exc}"[:300],
                "hint": "写稿可以继续, 但这次没拿到这部分数据; 服务端日志有完整堆栈"}


# ══════════════════════════════════════════════════════════════════════
# 写稿前
# ══════════════════════════════════════════════════════════════════════

def list_projects() -> dict:
    """列出所有可写作的项目, 以及每个项目手上有多少积累。

    返回 project_id / 名称 / 品牌 / 已沉淀的硬规则与软偏好条数 / 历史成稿指纹数。
    不知道要写哪个项目时先调这个。
    """
    return _safe(lambda: {"projects": core.list_projects(core.sb())})


def open_project(project_id: str, tactic: str = "", draft_topic: str = "",
                 key_messages: str = "", target_audience: str = "",
                 tone: str = "", extra_instructions: str = "",
                 _user_id: str | None = None) -> dict:
    """打开一个项目, 一次拿到写这个项目需要的【全部】上下文。

    动笔前必须先调这个。返回:
      · stable —— 项目人格/定位, 照抄进你的写作上下文
      · p0 —— 【不可违反的硬约束】, 必须 100% 满足, 与任何偏好冲突时以它为准
      · p1 —— 项目调性偏好 + 调校笔记 + 正反案例, 理解意图后按适用性应用
      · tactics —— 这个项目配置好的战术方向清单

    传入本次的 tactic / draft_topic / key_messages 会让正案例按【相关性】挑选
    而不是按时间倒序 —— 后者会让文风越写越窄。所以知道要写什么就传。

    返回的 counts.hard_rules 是 0 而用户以前明明定过规则, 多半是 project_id
    传错了, 问一句。

    这个工具出错会直接报错, 不会返回半份简报。报错就【停下来】, 不要凭记忆
    或常识补一份约束继续写 —— 这个项目的硬约束是什么, 只有库里那份算数。
    """
    brief = {"tactic": tactic, "draft_topic": draft_topic,
             "key_messages": key_messages, "target_audience": target_audience,
             "tone": tone, "extra_instructions": extra_instructions}
    # 故意不包 _safe: 见 _safe 文档「合规读」。P0 拿不到必须停, 不能给一份
    # p0 为空却看起来正常的简报。
    return core.build_writing_brief(core.sb(), project_id,
                                    user_id=_user_id, brief=brief)


def draw_angles(project_id: str, n: int, avoid_days: int = 30,
                perpetual_bias: bool = False, _user_id: str | None = None) -> dict:
    """发牌: 给本批 n 篇稿子各分配一组互不相同的创作坐标。

    批量写稿前必须先调这个, 然后【每一篇严格按分到的那组坐标写, 不得互换】。

    每组坐标含: 情绪杠杆 / 人性原型 / 内容形式 / 标题句式 / 切入角度, 外加
    情绪强度、词感倾向, 以及易混维度的判别指令(比如抽到「焦虑撬动」会告诉你
    它和「恐惧撬动」怎么分 —— 前者是模糊的未来担心, 后者要有具体已发生的威胁)。

    这些组合会避开本项目最近 avoid_days 天已经用过的 —— 这是跨批次不重复的
    根本保证。光靠提示词让模型「注意不要重复」做不到: 模型是无状态的, 它不知道
    自己上个月写过什么。

    perpetual_bias=True 偏向抽「不依赖任何时效元素」的组合, 写出来更能穿越周期
    (但会少掉当下感)。默认 False。

    返回里的 prompt_block 可以直接贴进生成提示词。
    """
    # 故意不包 _safe: 发牌要写台账。写失败却报成功, 同一个角度下批还能再抽,
    # 跨批次唯一性的承诺就破了(而且没人看得见)。
    return core.draw_angles(core.sb(), project_id, n,
                 avoid_days=avoid_days, user_id=_user_id,
                 perpetual_bias=perpetual_bias)


def borrow_lessons(project_id: str, tactic: str = "", draft_topic: str = "",
                   key_messages: str = "", target_audience: str = "",
                   tone: str = "") -> dict:
    """向帆谷飞轮图书馆借几张【真实爆款】的经验卡。

    这些卡来自公司自己投放过、数据验证过的笔记, 由策展员提炼成「钩子类型 /
    结构骨架 / 为什么有效 / 可迁移手法」。写稿时可以借它的钩子或结构。

    ⚠️ 严禁照抄卡里的标题主干或具体句子 —— 只借手法。
    ⚠️ 标了 synthetic=true 的卡表示【指标未经验证】(疑似人工刷量), 只能凭内容
       判断借鉴, 不要把它的数据当依据。

    库里没有合适的卡时返回空列表, 这不是错误, 照常写就行。
    """
    return _safe(core.borrow_lessons, core.sb(), project_id,
                 tactic=tactic, draft_topic=draft_topic,
                 key_messages=key_messages, target_audience=target_audience,
                 tone=tone)


# ══════════════════════════════════════════════════════════════════════
# 写稿后
# ══════════════════════════════════════════════════════════════════════

def check_drafts(project_id: str, drafts: list[dict]) -> dict:
    """查重硬闸。成稿后【必须】调这个才能交付。

    drafts 传 [{"title": "...", "body": "...", "angle_key": "..."}, ...]
    (angle_key 是 draw_angles 给的, 有就带上)。

    如果返回的 summary 里有 empty_history_warning, 说明这个项目【还没回填】历史
    指纹 —— 本次实际只在本批内部比对, 跟老稿子的重复不会被发现。要告诉用户去跑
    backfill, 别当作"比过了没撞车"。

    比对本项目【全部】历史成稿 + 本批内互比, 三个信号:
      · 标题语义相似度
      · 正文开头是否精确撞车(标题换了也能抓)
      · 正文四字串重合度(抓换皮的模板化写法)

    每条返回 pass / warn / reject:
      · reject —— 必须重写。不允许「少出一条」糊弄过去。reason 里说了是哪个
        信号命中: 撞开头就换开场视角, 撞正文就换比喻系统或叙事路径。
        只改标题没用。重写完再跑一次。
      · warn —— 可疑, 建议换个切入再交。
      · pass —— 可以交付, 记得调 commit_drafts 入库。

    summary.semantic_degraded 为 true 时说明 embedding 不可用, 这次只跑了确定性
    查重, 同角度换说法的标题可能漏过 —— 【要告诉用户这件事】, 别默默交付。

    这个工具出错会直接报错而不是放行 —— 查重挂了必须停下来, 不能当作通过。
    """
    # 故意不包 _safe: 查重是硬闸, 出错必须冒泡。
    return core.check_drafts(core.sb(), project_id, drafts)


def commit_drafts(project_id: str, drafts: list[dict],
                  _user_id: str | None = None) -> dict:
    """把【定稿】的稿子入库, 让它们参与以后的查重, 并把用掉的坐标销账。

    只传真正要发的稿子。把废稿也记进去会让以后正常的选题被误杀。

    ⚠️ 入库时【会再查一次重】。你 check 之后、commit 之前, 可能有队友先提交了
    撞车的稿子 —— 那条竞态窗口只能在写入的同一个事务里关掉。被判撞车的条目
    【不会入库】, 在返回值的 rejected 里列出来: 那几条要重写后重新走 check_drafts,
    不能当作已交付。

    这个工具出错会直接报错。入库失败必须让你知道 —— 稿子没进指纹库的话,
    下次 check_drafts 会把同样的内容再放行一次。看到报错就重试, 别当没事。
    """
    # 故意不包 _safe: 这是【写】操作。_safe 会把异常变成一个看起来成功、
    # 只带 error 字段的结果, 而写作台协议对 commit 没有强制重试 —— 于是定稿
    # 静默缺席指纹库, 查重的历史出现空洞, 同样的稿子以后能再过一次闸。
    return core.commit_drafts(core.sb(), project_id, drafts, user_id=_user_id)


# ══════════════════════════════════════════════════════════════════════
# 反馈学习
# ══════════════════════════════════════════════════════════════════════

def record_rule(project_id: str, content: str, severity: str = "soft",
                scope: str = "project", _user_id: str | None = None) -> dict:
    """把一条规则永久记住。团队共享 —— 队友写这个项目时也会守。

    什么时候调: 用户说的是「以后都这样」而不是「这次这样」。分不清就问一句。

    severity:
      · "hard" —— 不可违反的硬约束, 以后每次生成都会顶在最前面要求 100% 满足。
        用于: 禁词、必须包含的合规话术、绝对不能提的内容。
      · "soft" —— 调性偏好, 适用时应用、不适用可以让位。用于: 风格倾向、语气
        偏好、结构习惯。
      拿不准就用 soft 并问用户要不要设成硬约束 —— hard 设多了会把文案写死,
      二十条互相打架时模型只能写出四不像。

    scope: "project" 只对本项目生效; "global" 对所有项目生效(慎用)。
    """
    # 故意不包 _safe: 这是【写】操作, 且写的可能是 hard 合规规则。写失败若
    # 报成功, 这条规则会静默缺席之后的每一份简报 —— 而用户以为已经记住了。
    return core.record_rule(core.sb(), project_id, content,
                            severity=severity, scope=scope, user_id=_user_id)


def record_edit(project_id: str, ai_title: str, ai_body: str,
                my_title: str, my_body: str, note: str = "",
                _user_id: str | None = None) -> dict:
    """用户手动改了稿子时调这个 —— 这是让文风变得像本人的【最强信号】。

    传 AI 原版和用户改成的样子。个人笔记【只对本人生效】, 不影响队友。

    note 可选, 传用户自己说的原因(比如"太夸张了")会让提炼更准。

    ⚠️⚠️ **这是两步里的第一步, 调完还没结束。**
    返回值里的 distillation_task 是【交给你做】的: 按 instruction 的口径, 拿
    existing_notes 和 edits 提炼出【更新后的完整笔记】, 然后调 save_my_style
    写回去。不写回去的话这次精修等于白喂 —— diff 存下来了, 但文风不会变,
    而且没有任何报错。

    为什么让你做而不是服务端做: 蒸馏就是文本提炼, 你本来就在一个有模型的环境
    里; 服务端自己调 LLM 会多一个 key、多一个故障点, 也违反"推理归平台、MCP
    只做数据操作"的分工。

    ⚠️ 只在用户【真的动手改了】的时候调。用户没改就通过的稿子不要传进来 ——
    从「没改」里推不出偏好, 硬推会让系统编造出根本不存在的风格规则。
    """
    if not _user_id:
        return {"error": "无法识别调用者身份, 个人风格功能不可用",
                "hint": "服务端需要配置 DESKCORE_KEYS 或 DESKCORE_DEFAULT_USER_ID"}
    # 故意不包 _safe: 写。这是"裂变"的唯一入口, 静默失败 = 文风永远长不出来。
    return core.record_edit(core.sb(), project_id, user_id=_user_id,
                            ai_title=ai_title, ai_body=ai_body,
                            my_title=my_title, my_body=my_body,
                            note=note or None)


def label_example(item_id: str, label: str, _user_id: str | None = None) -> dict:
    """把某条历史稿标成正面案例或反面案例。

    label 传 "positive" / "negative" / "none"(撤销)。

    只能标【自己的】稿子 —— 正负例是个人风格资产, 改别人的会污染那个人的文风。
    标别人的会报错。

    正例会作为学习样本注入以后的写作; 负例作为「主动规避」的反面教材。

    ⚠️ 负例只标【你看了内容、判定它就是差】的稿子。不要因为某条数据不好就标
    负例 —— 数据不好有太多与内容无关的原因(没进流量池、账号限流、时机不对),
    那样会把被埋没的好内容也标成垃圾。
    """
    if not _user_id:
        return {"error": "无法识别调用者身份, 不能标记正负例(那是个人资产)",
                "hint": "服务端需要配置 DESKCORE_KEYS 或 DESKCORE_DEFAULT_USER_ID"}
    # 故意不包 _safe: 写。标记没落库却报成功, 用户不会再标第二次。
    return core.label_example(core.sb(), item_id,
                              None if label in ("none", "", None) else label,
                              user_id=_user_id)


def my_style(project_id: str, _user_id: str | None = None) -> dict:
    """看我在这个项目上积累的风格资产: 个人调校笔记、喂过多少次精修、正负例数。

    也用于回答「你现在记住了我什么」这类问题。注意区分: 项目规则是团队共享的,
    别把它说成是这个人的个人偏好。

    ⚠️ 看一眼 pending_distillation。大于 0 说明有精修【还没被吸收进笔记】——
    多半是上次调了 record_edit 却没接着调 save_my_style(比如会话中断了)。
    这时返回值里会直接带上 pending_distillation_task, **材料和口径都在里面**,
    照着提炼完调 save_my_style 就能补上, 不需要让用户把稿子再喂一遍。
    """
    if not _user_id:
        return {"error": "无法识别调用者身份",
                "hint": "服务端需要配置 DESKCORE_KEYS 或 DESKCORE_DEFAULT_USER_ID"}
    return _safe(core.my_style, core.sb(), project_id, user_id=_user_id)


def save_my_style(project_id: str, notes: str,
                  edit_ids: list[str] | None = None,
                  _user_id: str | None = None) -> dict:
    """把你提炼好的个人调校笔记写回去 —— record_edit 的【第二步】。

    notes 传【完整的新笔记】, 不是增量。它会整个替换掉旧笔记, 所以要在
    existing_notes 的基础上改写(合并同类项、冲突时以新观察为准), 别只写新增的
    那几条 —— 那样会把以前积累的偏好全丢掉。

    edit_ids **原样传** distillation_task 里给你的那一份。它决定哪几条精修被
    标记为"已吸收"。
    ⚠️ 不传的话只更新笔记、不销任何账 —— 这正是「用户说这条笔记不对, 直接
    改一下」应有的行为: 手动改写笔记不等于吸收了那些待处理的精修, 顺手把它们
    标掉会让它们静默消失。所以两种用法泾渭分明:
      · 吸收精修  → 传 edit_ids
      · 手动改笔记 → 不传

    返回值里看两样: edits_absorbed(真销掉几条) 和 pending_distillation
    (还剩几条)。剩的不为 0 说明没吸收完 —— 待吸收超过一次快照(8 条)时是正常
    的, 接着做下一批。
    """
    if not _user_id:
        return {"error": "无法识别调用者身份, 个人风格功能不可用",
                "hint": "服务端需要配置 DESKCORE_KEYS 或 DESKCORE_DEFAULT_USER_ID"}
    # 故意不包 _safe: 写。这是"裂变"闭环的最后一步, 静默失败 = 前面白做。
    return core.save_my_style(core.sb(), project_id, notes,
                              user_id=_user_id, edit_ids=edit_ids)


# 工具注册表 —— app.py 和 cli.py 共用。
# 值 = (函数, 是否需要服务端注入调用者身份)
TOOLS = {
    "list_projects":  (list_projects,  False),
    "open_project":   (open_project,   True),
    "draw_angles":    (draw_angles,    True),
    "borrow_lessons": (borrow_lessons, False),
    "check_drafts":   (check_drafts,   False),
    "commit_drafts":  (commit_drafts,  True),
    "record_rule":    (record_rule,    True),
    "record_edit":    (record_edit,    True),
    "save_my_style":  (save_my_style,  True),
    "label_example":  (label_example,  True),
    "my_style":       (my_style,       True),
}
