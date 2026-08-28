"""deskcore/tools.py — MCP 工具面。

十六个工具, 按写稿的三个阶段分组(外加一个 create_project 开项目, 和三个
规则台账相关的 my_rules / set_rule_state / reembed_my_rules)。设计原则是【一次调用拿全】—— 治员工反馈里
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

    ⚠️ 【归属校验的拒绝不在兜底范围内】(审计 COR-015)。_safe 兜的是**瞬时**故障
    —— 网络抖、服务没配、库慢, 少点参考不影响写稿。PermissionError 不是这一类:
    它重试一万次也一样, 而包成"只多一个 error 字段的正常结果"会让调用方模型
    继续拿同一个错 project_id 去试下一个工具, 也让 REST 层的 403 变得只对一部分
    工具成立。所以原样上抛。
    """
    try:
        return fn(*args, **kwargs)
    except PermissionError:
        raise
    except Exception as exc:  # noqa: BLE001 — 故意兜底
        logger.exception("%s failed", getattr(fn, "__name__", fn))
        return {"error": f"{type(exc).__name__}: {exc}"[:300],
                "hint": "写稿可以继续, 但这次没拿到这部分数据; 服务端日志有完整堆栈"}


# ══════════════════════════════════════════════════════════════════════
# 写稿前
# ══════════════════════════════════════════════════════════════════════

def create_project(name: str, brand: str = "",
                   _user_id: str | None = None) -> dict:
    """给一个新品牌 / 新方向开一个项目。**建完立刻用返回的 project_id 调
    open_project 接管。**

    什么时候调: 用户要写的那个品在 ``list_projects`` 里没有。

    **调之前先 list_projects 看一眼** —— 已经有的项目不要重建。返回值里
    ``created`` 是 false 就说明撞名了, 库里没新建, 直接用返回的那个
    project_id, 别改名重试。

    返回里带 ``siblings`` 时要停下来问用户: 说明这个品牌名下已经有别的项目
    了。新项目是**独立的一套历史库**, 跟它们不互相查重 —— 如果本意是在已有
    方向下继续写, 用那个已有项目才对。

    参数:
      name  —— 项目名, 建议带上方向, 例如「途鸽-D8薪资谈判」而不是光「途鸽」。
      brand —— 品牌名, 同品牌的项目靠它归堆。同一个品的项目 brand 要写一致。

    ⚠️ 项目归属**恒为你自己**, 不能替别人建。别人要用得他自己那把 key 建。
    """
    # 故意不包 _safe: 这是【写】操作。建失败若降级成带 error 的"成功", 调用方
    # 会拿着一个不存在的 project_id 往下走, 后面每个工具都 404 而根因看不见。
    return core.create_project(core.sb(), name, brand=brand, user_id=_user_id)


def list_projects(_user_id: str | None = None) -> dict:
    """列出【你名下】的项目, 以及每个项目手上有多少积累。

    返回 project_id / 名称 / 品牌 / 已沉淀的硬规则与软偏好条数 / 历史成稿指纹数。
    不知道要写哪个项目时先调这个。

    这里看不到的项目就是不归你 —— 不要去猜别人的 project_id 试, 其它工具会拒绝。
    """
    return _safe(lambda: {"projects": core.list_projects(core.sb(),
                                                         user_id=_user_id)})


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

    ⚠️ **counts.hard_rules 是 0 属于正常, 不要因此怀疑 project_id 传错。**
    现存库里三百多条规则的 severity 全是 soft, 一条 hard 都没有(老工作台收
    反馈时不分"这一次"和"以后都这样", 一律存成 soft)。传错 project_id 的真实
    表现是 403, 或者 soft_rules 和 soft_rules_pool 【同时】为 0。
    只有后者才值得问一句。

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
                   tone: str = "", _user_id: str | None = None) -> dict:
    """向帆谷飞轮图书馆借几张【真实爆款】的经验卡。

    这些卡来自公司自己投放过、数据验证过的笔记, 由策展员提炼成「钩子类型 /
    结构骨架 / 为什么有效 / 可迁移手法」。写稿时可以借它的钩子或结构。

    ⚠️ 严禁照抄卡里的标题主干或具体句子 —— 只借手法。
    ⚠️ 标了 synthetic=true 的卡表示【指标未经验证】(疑似人工刷量), 只能凭内容
       判断借鉴, 不要把它的数据当依据。

    库里没有合适的卡时返回空列表, 这不是错误, 照常写就行。
    """
    return _safe(core.borrow_lessons, core.sb(), project_id, user_id=_user_id,
                 tactic=tactic, draft_topic=draft_topic,
                 key_messages=key_messages, target_audience=target_audience,
                 tone=tone)


# ══════════════════════════════════════════════════════════════════════
# 写稿后
# ══════════════════════════════════════════════════════════════════════

def check_drafts(project_id: str, drafts: list[dict],
                 _user_id: str | None = None) -> dict:
    """查重硬闸。成稿后【必须】调这个才能交付。

    drafts 传 [{"title": "...", "body": "...", "angle_key": "..."}, ...]
    (angle_key 是 draw_angles 给的, 有就带上)。

    如果返回的 summary 里有 empty_history_warning, 说明这个项目【还没回填】历史
    指纹 —— 本次实际只在本批内部比对, 跟老稿子的重复不会被发现。要告诉用户去跑
    backfill, 别当作"比过了没撞车"。

    比对本项目【全部】历史成稿 + 本批内互比, 四个信号:
      · 标题语义相似度
      · 正文开头是否精确撞车(标题换了也能抓)
      · 正文四字串重合度(抓换皮的模板化写法)
      · 正文四字串【包含度】(抓短稿整段照搬长稿 —— 那种情况下上一条会被
        长度差稀释, 看起来像不重复)

    summary.containment_checked 为 false 时说明最后那一路本次没生效(草稿太短、
    与历史稿长度差太大, 或服务端没跑 migrations/005), summary 里会写清楚原因 ——
    【要告诉用户】, 整段照搬一篇长稿的重复这次可能漏掉。

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
    return core.check_drafts(core.sb(), project_id, drafts, user_id=_user_id)


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

    返回值里的 ``batch_id`` / ``version_ids`` 是这批稿子在库里的身份, 直接拿去
    喂 export_drafts。带 ``identity_warning`` 时说明身份没建成 —— 稿子入库了、
    查重不受影响, 但这批导不出可归因的 lineage。
    """
    # 故意不包 _safe: 这是【写】操作。_safe 会把异常变成一个看起来成功、
    # 只带 error 字段的结果, 而写作台协议对 commit 没有强制重试 —— 于是定稿
    # 静默缺席指纹库, 查重的历史出现空洞, 同样的稿子以后能再过一次闸。
    return core.commit_drafts(core.sb(), project_id, drafts, user_id=_user_id)


def export_drafts(project_id: str, batch_id: str | None = None,
                  version_ids: list[str] | None = None,
                  _user_id: str | None = None) -> dict:
    """把已经 commit 的稿子导成 Excel, 用来粘进飞书表。

    什么时候调: 用户说「导出」「给我个表」「要发了」。**必须先 commit_drafts** ——
    只有入了库的稿子才有身份, 没身份就没有可归因的 lineage。

    ``batch_id`` 用 commit_drafts 刚回的那个(最常用); 只导其中几篇时给
    ``version_ids``。两个都不给会直接报错, 不会去猜。

    返回值里:
      · ``xlsx_base64`` —— 解码写成 ``filename`` 那个文件交给用户;
      · ``columns``     —— 飞书表里要有的列名, **逐字相同**;
      · ``preview``     —— 标题 + version_id, 用来核对导的是不是那一批。

    ⚠️ 表里除了内容列还有六个 ``_source_autowriter_*`` / ``_ai_engine`` /
       ``_exported_at`` 列。**别让用户删掉它们**, 也别只复制内容列 —— 那六列是
       "这条笔记是谁写的哪一版"的唯一载体, 丢了就等于这篇稿子发出去之后的数据
       再也回不到写作台。飞书表还没建这几列的话, 让用户先按 columns 建好。
    """
    # 同 commit_drafts: 不包 _safe。导出失败必须当场知道 —— 静默回个空表, 用户
    # 会以为"这批没稿子"而不是"导出坏了"。
    return core.export_drafts(core.sb(), project_id, batch_id=batch_id,
                              version_ids=version_ids, user_id=_user_id)


# ══════════════════════════════════════════════════════════════════════
# 反馈学习
# ══════════════════════════════════════════════════════════════════════

def record_rule(project_id: str, content: str, severity: str = "soft",
                scope: str = "project", applicability: str = "",
                _user_id: str | None = None) -> dict:
    """把一条规则永久记住。团队共享 —— 队友写这个项目时也会守。

    什么时候调: 用户说的是「以后都这样」而不是「这次这样」。分不清就问一句。

    severity:
      · "hard" —— 不可违反的硬约束, 以后每次生成都会顶在最前面要求 100% 满足。
        用于: 禁词、必须包含的合规话术、绝对不能提的内容。
      · "soft" —— 调性偏好, 适用时应用、不适用可以让位。用于: 风格倾向、语气
        偏好、结构习惯。
      拿不准就用 soft 并问用户要不要设成硬约束 —— hard 设多了会把文案写死,
      二十条互相打架时模型只能写出四不像。

    scope: "project" 只对本项目生效; "global" 对**我自己**的所有项目生效 ——
    个人技艺库就是这一档。global 规则是私有的, 不会跑到队友那里。

    applicability: 方向档, 只认三个值 —— ""(通用, 默认) / "产品向" / "流量向"。
      同一条技艺在两个方向上经常是相反的要求(结尾要不要收口、正文出不出品牌
      名、罗列还是叙事), 混成一条必然打架。用户说的是"写产品向的时候这样"就
      传 "产品向"; 没提方向就留空。**不确定就留空** —— 通用的多注入一条看得见,
      设错方向会让规则在该生效的项目上静默缺席。
    """
    # 故意不包 _safe: 这是【写】操作, 且写的可能是 hard 合规规则。写失败若
    # 报成功, 这条规则会静默缺席之后的每一份简报 —— 而用户以为已经记住了。
    return core.record_rule(core.sb(), project_id, content,
                            severity=severity, scope=scope, user_id=_user_id,
                            applicability=applicability)


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


def my_rules(project_id: str, _user_id: str | None = None) -> dict:
    """列出这个项目上**我能管的全部规则**, 含试用档和已停用的。

    什么时候调:
      · 用户问"你现在都记着我什么规矩""这个项目守着哪些规则";
      · 用户抱怨某条规则【没生效】 —— 台账里的 state 会直接告诉他为什么
        (试用 / 已停用 / 方向不符), 不用猜;
      · 用户想清理规则库之前, 先给他看一遍。

    每条的 state:
      · 生效中   —— 这次写稿会注入
      · 方向不符 —— 它是另一个方向(产品向/流量向)的规则, 本项目不注入
      · 试用     —— 还在候选档, 不进简报, 等攒够票或用户手动 promote
      · 已停用   —— 用户自己 mute 掉的, muted_until 到期自动恢复

    改档位用 set_rule_state。
    """
    if not _user_id:
        return {"error": "无法识别调用者身份, 规则台账不可用",
                "hint": "服务端需要配置 DESKCORE_KEYS 或 DESKCORE_DEFAULT_USER_ID"}
    # 读, 可降级: 看不到台账不影响写稿。
    return _safe(core.my_rules, core.sb(), project_id, user_id=_user_id)


def reembed_my_rules(batch: int = 50, _user_id: str | None = None) -> dict:
    """给我自己缺向量的规则补算向量, 一次一批(最多 50 条)。

    什么时候调: `my_rules` 的 counts 里出现「缺向量」时。用户也可能直接说
    「补一下向量」「规则的相关性筛选没生效」。

    **没向量的规则不会报错、照常注入**, 只是不参与相关性筛选 —— 也就是跟
    这次要写的东西毫不相干时也会挤进简报。补完就正常了。

    返回 `remaining` 大于 0 就**再调一次**, 直到它变成 0。
    ⚠️ 但返回里带 `warning`(查到了却一条没补上)时**不要再调** —— 再调还是
    同一批, 只会白花钱。把 warning 原样告诉用户。

    只补**调用者自己**名下的规则, 补不到别人的。
    """
    if not _user_id:
        return {"error": "无法识别调用者身份, 补算功能不可用",
                "hint": "服务端需要配置 DESKCORE_KEYS 或 DESKCORE_DEFAULT_USER_ID"}
    # 故意不包 _safe: 写。而且它的失败模式全是"看起来跑完了其实没干活",
    # 包成带 error 的"成功"正好把这些状态码埋掉。
    return core.reembed_my_rules(core.sb(), user_id=_user_id, batch=batch)


def set_rule_state(memory_id: str, action: str, days: int = 90,
                   direction: str = "", _user_id: str | None = None) -> dict:
    """改一条规则的档位 —— 停用 / 恢复 / 升降档 / 设方向。**不改内容**。

    memory_id 从 my_rules 的返回里拿。

    action:
      · "mute"          —— 停用 days 天(默认 90)。用于"这条最近别用了"。
        到期自动恢复, 所以不确定要不要永久去掉时用这个。
      · "unmute"        —— 立刻恢复。
      · "retire"        —— 降回试用档, 不再进简报。用于"这条不对/过时了"。
        **不删行**, 以后还能 promote 回来。
      · "promote"       —— 试用 → 生效。用于用户明确说"这条留着, 以后都这样"。
      · "set_direction" —— 配合 direction 参数设成 "产品向" / "流量向" / "通用"。

    ⚠️ 用户说"别用这条了"时先问一句是**暂时**还是**以后都不要**: 前者 mute,
    后者 retire。分不清就用 mute —— 它会自己到期, 猜错的代价小。
    """
    if not _user_id:
        return {"error": "无法识别调用者身份, 拒绝改动规则",
                "hint": "服务端需要配置 DESKCORE_KEYS 或 DESKCORE_DEFAULT_USER_ID"}
    # 故意不包 _safe: 写。静默失败 = 用户以为关掉了的规则还在每一份简报里,
    # 或者以为留下的规则其实没生效 —— 两个方向都是无声的。
    return core.set_rule_state(core.sb(), memory_id, action,
                               user_id=_user_id, days=days, direction=direction)


# 工具注册表 —— app.py 和 cli.py 共用。
# 值 = (函数, 是否需要服务端注入调用者身份)
#
# ⚠️ 审计 COR-015 之后【全部为 True】, 而且应该一直是 True。原来
# list_projects / borrow_lessons / check_drafts 三个是 False —— 那不是省事,
# 那是"这三个工具连调用者是谁都不问"的直接写照, 也正是越权读的入口:
#   · list_projects  → 全库项目台账 + 一批可以拿去喂别的工具的 project_id
#   · check_drafts   → 返回值回显撞车对象的标题, 等于一个历史标题读取接口
#   · borrow_lessons → 发给馆员的 brief 是拿项目行拼的(品牌/定位/战术)
# 新增工具时默认写 True; 想写 False 就得先说明它凭什么不需要知道是谁在调。
TOOLS = {
    "create_project": (create_project, True),
    "list_projects":  (list_projects,  True),
    "open_project":   (open_project,   True),
    "draw_angles":    (draw_angles,    True),
    "borrow_lessons": (borrow_lessons, True),
    "check_drafts":   (check_drafts,   True),
    "commit_drafts":  (commit_drafts,  True),
    "export_drafts":  (export_drafts,  True),
    "record_rule":    (record_rule,    True),
    "record_edit":    (record_edit,    True),
    "save_my_style":  (save_my_style,  True),
    "label_example":  (label_example,  True),
    "my_style":       (my_style,       True),
    "my_rules":       (my_rules,       True),
    "set_rule_state": (set_rule_state, True),
    "reembed_my_rules": (reembed_my_rules, True),
}
