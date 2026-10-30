"""deskcore/tools.py — MCP 工具面。

按写稿的流程分组: 取协议 → 开项目 → 写稿前 → 写稿后 → 人审 → 交付 → 收反馈,
外加三个规则台账工具(my_rules / set_rule_state / reembed_my_rules)。
设计原则是【一次调用拿全】—— 治员工反馈里那条「一个项目一般有 5 个提示词
要重复操作 5 次, 我的工作台至少有几十个提示词」。

⚠️ **个数不写在这里** —— 它变过好几次(文档里至今留着 12/13/15/16 三四个版本的
残影), 而每次加工具都得回来改一个数字的注释, 迟早漏。要准确数字就读下面的
``TOOLS``, 或者打一下 ``/health``。

每个工具的 docstring 就是模型看到的说明, 所以写给模型看; 维护者要看的原因
写在 core.py 的注释里。

降级口径:
  · 读类工具出错 → 返回带 error 的可用结构, 不抛(不阻塞写稿)
  · check_drafts 是【唯一例外】→ 出错必须抛。静默放行就是重演
    config.py 那个 ENABLE_DEDUP_REGEN 默认关着的老问题。
"""

from __future__ import annotations

import threading
import hashlib
import re
import logging
from pathlib import Path
from typing import Any

from . import core, skillcheck, vocab

logger = logging.getLogger("deskcore.tools")

# 写作台协议正文的【源文件】。运营机器上的 skill 文件里那份由它生成(见下面
# SKILL_PATH); get_protocol 只在版本对不上时才把它发下去。
PROTOCOL_PATH = Path(__file__).with_name("protocol.md")
# 运营机器上的那份协议: 技能文件。它进的是**系统提示**, 整场对话都在模型眼前 ——
# 这是它和 get_protocol 返回值的本质区别, 见 get_protocol 的说明。
SKILL_PATH = (PROTOCOL_PATH.parent.parent / "skills" / "bywood-writing-desk"
              / "SKILL.md")
# 技能文件里这一行以下是 protocol.md 的原样拷贝, 由 `python -m deskcore.cli
# sync-skill` 生成; 以上是手写的引线头。tests/test_protocol_tool.py 盯着两边一致。
SKILL_VERSION_MARK = "<!-- protocol_version: {version} -->"
_SKILL_MARK_RE = re.compile(r"<!-- protocol_version: ([0-9a-f]{12}) -->")


# ══════════════════════════════════════════════════════════════════════
# 协议
# ══════════════════════════════════════════════════════════════════════

def skill_head() -> str:
    """技能文件里标记行以上的部分(含 frontmatter): 手写的引线头。没有文件就是空串。"""
    try:
        skill = SKILL_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""
    head, sep, _ = skill.partition("<!-- protocol_version:")
    return head.strip() if sep else ""


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5:].strip()
    return text


def protocol_text() -> tuple[str, str]:
    """(协议正文, 12 位版本哈希)。每次从盘上读: 十几 KB, 微秒级, 换来热修不必重启。

    ⚠️ 版本哈希盖的是**引线头 + 正文**, 不只是 protocol.md。引线头里有几条独立
    生效的规矩(失败不编原因 / 不问"要不要跳过"…), 只改它不改正文时, 装着旧
    skill 的机器如果版本号不变, get_protocol 就会一直说 up_to_date, 那次更新
    永远送不到(codex review P2)。
    """
    text = PROTOCOL_PATH.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"协议文件为空: {PROTOCOL_PATH}")
    digest = hashlib.sha256((skill_head() + "\n" + text).encode("utf-8")).hexdigest()[:12]
    return text, digest


def get_protocol(local_version: str | None = None,
                 _user_id: str | None = None) -> dict:
    """核对写作台协议的版本; 本地没有或已过期时下发全文。对话开始时调一次。

    ``local_version`` 传 skill 文件里 ``protocol_version`` 那一行的值。返回:
      · ``version``     —— 服务端协议的版本哈希
      · ``up_to_date``  —— True: 本地 skill 就是最新版, **照 skill 执行**, 本返回值
                          里没有正文; False: 本地是旧版, 正文在 ``protocol`` 里,
                          照它执行, 并告诉用户一句「写作台 skill 有更新, 重新导入
                          一下」; None: 没传 local_version, 全文已返回
      · ``protocol``    —— 协议正文(只在需要时带)
      · ``skill_header`` —— skill 文件引线头里那几条独立规矩(和 protocol 一起带;
                          版本号盖的是两者, 头改了也算过期)
      · ``skill_update`` —— (不是最新版 / 没传版本时才有) 升级提醒。把它的 ``message``
                          **原样**(三行都要)放在给用户回复的最前面, 然后照常继续。

    ⚠️ 协议正文的**主副本在 skill 文件里**, 不在这个工具的返回值里。2026-09-10 曾
    反过来 —— skill 只留一根引线, 正文每次由这个工具下发 —— 结果 09-11 起定稿
    入库率从 110% 掉到 22%: 34 KB 的正文作为对话开头的一次工具返回值, 在模型
    写完十几篇稿子之后已经离得太远或被平台压缩掉, 于是它不记得还要查重和入库,
    或者记得但找不到工具名(「找不到规则操作工具」就是那个状态的原话)。技能文件
    进的是系统提示, 整场对话都在眼前, 没有这个问题。这个工具保留下来只为一件事:
    skill 拷到运营机器上之后改了没人提醒 —— 版本对不上时它把新正文发下来。

    这个工具出错就是服务端的协议文件坏了。那时**按本地 skill 那份继续**(同一份
    正文), 但要知道版本没核上; 不要说"服务掉线"。
    """
    # _user_id 收下但不用: 协议对所有人一样, 不按人裁剪。仍然走 needs_user=True
    # 是为了不给 test_all_tools_now_require_caller_identity 那条"所有工具都绑
    # 身份"的机械保险开口子 —— 一个例外就会变成下一个例外的先例。
    # 故意不包 _safe: 包成带 error 的"成功"会让模型以为版本核过了。
    text, digest = protocol_text()
    out: dict = {"version": digest, "chars": len(text)}
    # 服务端版本探针 (skillcheck): 记下这个写手报的版本, 不是最新就带升级提醒。不拦调用。
    status, reported = skillcheck.report(_user_id, local_version, digest)
    if status != skillcheck.UP_TO_DATE:
        out["skill_update"] = skillcheck.notice(status, reported, digest)
    # 宽松地取版本号: 模型可能把整行 "<!-- protocol_version: xxx -->" 或
    # "protocol_version: xxx" 原样传进来。只要里面有那 12 位十六进制就认。
    # 精确比较的后果是每场对话都判成"旧版", 全文再发一遍、还让运营白重装。
    # 与探针 (skillcheck.normalize) 同一个口径: 空串当没传, 认不出的按旧版。
    if local_version is not None:
        local_version = skillcheck.normalize(local_version)
    header = _strip_frontmatter(skill_head())
    if local_version is None:
        out["protocol"] = text
        out["skill_header"] = header
        out["up_to_date"] = None
        out["note"] = ("没带 local_version, 全文已返回, 照着执行。装了 skill 的话"
                       "下次带上它 protocol_version 那一行的值, 一致就不用再传全文。")
    elif str(local_version).strip() == digest:
        out["up_to_date"] = True
        out["note"] = "本地 skill 就是最新版, 照它执行。"
    else:
        out["protocol"] = text
        out["skill_header"] = header
        out["up_to_date"] = False
        out["warning"] = (
            f"本地 skill 是旧版({str(local_version).strip()[:12]}), 服务端是 "
            f"{digest}。**照本返回值里的正文执行**, 并把 skill_update.message 原样告诉用户"
            f"(写作台 skill 有更新, 要重新导入: {skillcheck.UPDATE_URL} / {skillcheck.UPDATE_COMMAND})。")
    return out


def render_skill(current_skill: str, body: str, version: str) -> str:
    """把 protocol.md 全文接到技能文件的引线头下面。

    标记行以上是手写的引线头(原样保留), 以下整段替换成 ``body``。没有标记行就
    拒绝 —— 那说明有人把 SKILL.md 改回了手工维护的一整份, 生成会把他的改动盖掉。
    """
    head, sep, _ = current_skill.partition("<!-- protocol_version:")
    if not sep:
        raise RuntimeError(
            f"{SKILL_PATH} 里没有 protocol_version 标记行 —— 不知道从哪儿开始替换。")
    # 标记行之外再写一行**可见的**版本号: 有的 skill 加载器会把 HTML 注释剥掉,
    # 模型就找不到该传给 get_protocol 的值了。机器认注释, 人和模型认下面这行。
    # ⚠️ version 必须是按【这个 head + 这个 body】算出来的(见 protocol_text);
    #    调用方(cli sync-skill)先把 head 写好再算, 否则标记里的号和内容对不上。
    return (head.rstrip("\n") + "\n\n"
            + SKILL_VERSION_MARK.format(version=version) + "\n"
            + f"protocol_version: {version}\n\n" + body + "\n")


def skill_sync_state() -> dict:
    """技能文件里那份协议与 protocol.md 是不是同一份。给 sync-skill --check 和测试用。"""
    skill = SKILL_PATH.read_text(encoding="utf-8")
    body, digest = protocol_text()
    m = _SKILL_MARK_RE.search(skill)
    embedded_version = m.group(1) if m else None
    embedded_body = skill[m.end():].strip() if m else ""
    # 标记后面那行可见的 "protocol_version: xxx" 不算正文
    if embedded_body.startswith("protocol_version:"):
        embedded_body = embedded_body.split("\n", 1)[1].strip() if "\n" in embedded_body else ""
    return {
        "in_sync": embedded_version == digest and embedded_body == body,
        "skill_version": embedded_version,
        "protocol_version": digest,
        "body_matches": embedded_body == body,
        "skill_path": str(SKILL_PATH),
    }


def _safe(fn, *args, **kwargs) -> Any:
    """**可降级的读**的统一包装 —— 返回可用结构 + 【必须留痕】。

    留痕这条是硬要求: TV docs/19:180-200 记过一次事故, librarian 的模型 env
    变量名配错, 每次 LLM 调用失败被 except 吞掉降级成 [], 外面看永远 200,
    查了很久。

    ⚠️ 【什么能包、什么绝不能包】—— 加新工具前先读这段。
    本包装会把异常变成一个**看起来成功**、只多一个 error 字段的结果, 而 hint
    里明写着"写稿可以继续"。所以它只适用于:【读】+【拿不到也只是少点参考】。
    目前只有四个: list_projects / borrow_lessons / my_style / my_rules。
    (这个集合被 ci.yml 的 SAFE_OK 钉死, 增减都会红。)

    绝不能包的两类, 各有各的失败模式:
      · **合规读**(open_project) —— 拿不到 P0 硬约束就照常开写, 产出的是违规
        内容, 而调用方看到的是一份 p0 为空的正常简报。store.shared_memories()
        专门为此【故意不吞异常】, 外面再包一层 _safe 等于把那个设计原样抵消掉。
      · **写**(draw_angles / score_drafts / commit_drafts / record_rule / record_edit /
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

    ⚠️ fingerprint_count 是**查重基线**的条数(draft_fingerprints 的行), 含从 Truth Vault 补录进来的
    笔记副本。ingest_copies 是**补录进来的 item 数**, 独立的一个数, 不是 fingerprint_count 的子集:
    补录半途失败的副本有 item 没指纹, 会算进 ingest_copies 而不在 fingerprint_count 里, 所以两个数
    **别相减**。读法: ingest_copies 大 = 这个项目的"积累"大半是 TV 笔记的影子, 不是写作台写过那么多篇。

    这里看不到的项目就是不归你 —— 不要去猜别人的 project_id 试, 其它工具会拒绝。

    **用户要写 / 改某个品牌的小红书稿、却没说是哪个项目(只报了品牌名、只贴了 brief、只点了
    human-writing), 先调这个找项目, 再 open_project。** 没说写作台也一样: 写稿就走写作台。
    (写评论带的是笔记链接, 直接 comments_for, 它会按链接认项目。)

    返回里有 ``desk_gap`` 时: 你名下有项目最近发出去的帖写作台对不回来(多半是写的时候没调起写作台)。
    把 ``desk_gap.message`` 原样告诉用户, 同一场对话里说过就不再说, 然后照常继续。用户没有存档这一步, 别叫用户存档。
    """
    def _list():
        out = {"projects": core.list_projects(core.sb(), user_id=_user_id)}
        gap = _desk_gap(_user_id)
        if gap:
            out["desk_gap"] = gap
        return out
    return _safe(_list)


def _desk_gap(user_id: str | None, project_id: str | None = None) -> dict | None:
    """附加提醒: 查不到就不带, 不能把清单 / 简报弄坏。"""
    try:
        return core.desk_gaps(core.sb(), user_id=user_id, current_project_id=project_id)
    except Exception:  # noqa: BLE001
        logger.warning("desk_gap 查不到, 这次不带", exc_info=True)
        return None


def _archive_pending(user_id: str | None, project_id: str) -> dict | None:
    """附加提醒(W-29): 调用者在这个项目上抽了还没存档的牌, 和下一步该做什么。查不到就不带 ——
    不能把发牌 / 查重 / 记规则的结果弄坏(同 _desk_gap 的纪律)。"""
    if not user_id or not project_id:
        return None
    try:
        return core.archive_pending(core.sb(), project_id, user_id=user_id)
    except Exception:  # noqa: BLE001 — 附加提醒, 任何错都只少这一段
        logger.warning("archive_pending 查不到, 这次不带 (project=%s)", project_id, exc_info=True)
        return None


def _with_archive_pending(out: Any, user_id: str | None, project_id: str) -> Any:
    """工具成功之后才调: 有没存档的牌就在返回值上加一个 ``archive_pending`` 键, 别的键一个不动。"""
    if isinstance(out, dict) and "archive_pending" not in out:
        block = _archive_pending(user_id, project_id)
        if block:
            out["archive_pending"] = block
    return out


def open_project(project_id: str, tactic: str = "", draft_topic: str = "",
                 key_messages: str = "", target_audience: str = "",
                 tone: str = "", extra_instructions: str = "",
                 skill_version: str | None = None,
                 _user_id: str | None = None) -> dict:
    """打开一个项目, 一次拿到写这个项目需要的【全部】上下文。

    动笔前必须先调这个: 给任何品牌写或改小红书稿之前都要先开项目, 用户没提写作台、只报了品牌名、
    只贴了 brief、只点了 human-writing 也一样(不知道是哪个项目就先 list_projects)。写评论不走这里, 走 comments_for。返回:
      · stable —— 项目人格/定位, 照抄进你的写作上下文
      · p0 —— 【不可违反的硬约束】, 必须 100% 满足, 与任何偏好冲突时以它为准
      · p1 —— 项目调性偏好 + 调校笔记 + 正反案例, 理解意图后按适用性应用
      · tactics —— 这个项目配置好的战术方向清单
      · lessons —— 【真实爆款经验卡】, 已经替你向帆谷飞轮图书馆借好了
        (最多 5 张); lessons_status.status 说明是 borrowed / empty /
        not_configured / timeout / error 里的哪一种。只借钩子、结构、手法,
        严禁照抄标题主干或具体句子; synthetic=true 的卡指标未经验证。
        卡上的 validated_hits 是这张卡命中了哪几条已验证规律。
        external=true 的是【外部参考卡】(最多 2 张): 别家同品类的帖, 不是公司投的, 没有经过我们的数据验证;
        只借结构 / 钩子, 不抄内容, 也不要提它里面的任何品牌。
      · validated_patterns —— 【已验证规律】(有才出现): 在公司自己的历史笔记里、同一项目内
        对照验证过的写法, 每条 {label, instruction, evidence}。它们是表现好过的选项, 不是模板:
        **每篇最多用其中一条**(用的那条照它的 instruction 写), 一批里轮换着用, 也要有几篇一条都
        不用(具体哪篇用哪条见旁边的 validated_patterns_usage, 起点按天换)。它是提示不是硬约束, 和
        draw_angles 分到的坐标或 p0 冲突时以坐标和 p0 为准, 不贴题就不用。没有这个字段就是这次没有, 不要自己编规律, 也不要跟用户解释
        为什么没有。
      · recent_titles —— 【有才出现】本项目最近发出去的标题(published)和最近在写作台写过的标题
        (drafted)。照 recent_titles.task 做: 新写的稿子标题别和它们撞主干词和具体说法(标题句式照
        draw_angles 分到的写, 在同一种句式里换说法), 开头别复述它们, 人设和核心场景尽量错开。
        p0 / stable 定的人设、发牌分到的坐标和切入角度、用户点名的题目和改稿都优先于它。
        只用来避开, 不要模仿。
      · recent_structure —— 【有才出现】本项目最近 window 篇打过分的稿子里, 扎堆写成同一个样子的几处
        (concentrated 每条 {question, answer, share, of}: 哪一处、扎堆的那种写法、占比、占比的分母 —— 这一题有答案的
        篇数, 可能比 window 少)。照 recent_structure.task 做:
        这一批每一处都至少有一半的稿子换一种写法。p0 / stable、发牌分到的坐标和切入角度、用户点名的写法都优先于它;
        只管新写的稿, 改稿不受它约束。
      · delivery_requirements —— 【项目有交付模板才出现】score_drafts 时每篇 draft 要带什么: ``tags`` 是要给几个话题
        (``fixed_tags`` 写作台会加, 不用给), ``comments`` 是评论条数范围和每条要提到的词(null 就是不要评论),
        ``direction`` 为 true 时每篇要带一句切入角度。照 delivery_requirements.task 写, 一次带齐, 免得存完再返工。
      · edit_digest —— 【有才出现; ack 之后一周内不再出现, 没 ack 就每次打开项目都带】: 本项目最近发出去的稿子
        在发布前被人改了什么(写作台那版 → 发出去那版)。**先把 edit_digest.message 原样放在这一轮回复的最前面**,
        再照 edit_digest.task 做: 归纳成最多 3 条规则建议问用户, 同意的 record_rule, 用户做了决定之后调
        ack_edit_digest。说完再开始写稿。
      · flywheel_arm —— 这场写作落在哪个对照组。存档时【原样】传给 score_drafts 的 flywheel_arm,
        别改、别跟用户提。

    传入本次的 tactic / draft_topic / key_messages 会让正案例按【相关性】挑选
    (排不出相关性时按天轮换, 不会每次都是最近那几条 —— 那会让文风越写越窄)。所以知道要写什么就传。
    经验卡也是按这几个字段匹配的, 不传就只按项目定位借。传了 tactic 的, 存档时 score_drafts 也原样传同一个 tactic。

    ⚠️ **counts.hard_rules 是 0 不一定是错的。** 很多项目的约束确实都记在 soft 里
    (老工作台收反馈时不分"这一次"和"以后都这样", 一律存成 soft; 2026-09-20 全库
    465 条规则里 hard 只有 53 条)。传错 project_id 的真实表现是 403, 或者
    soft_rules 和 soft_rules_pool 【同时】为 0。只有后者才值得问一句。

    counts.angle_debt 大于 0 时, 返回里还会有 angle_debt / angle_debt_note:
    **调用者自己**在这个项目近期抽了没出稿的坐标, angle_debt.angles 里是具体是
    哪几个。把这个数说给用户听, 问一句接着写完还是放弃, 别默认再抽一批。

    这个工具出错会直接报错, 不会返回半份简报。报错就【停下来】, 不要凭记忆
    或常识补一份约束继续写 —— 这个项目的硬约束是什么, 只有库里那份算数。

    ``skill_version``: **每次都传** bywood-writing-desk 这个 skill 里 ``protocol_version:`` 那一行的
    12 位值(传整行也认)。skill 里没有这一行就传空。返回里有 ``skill_update`` 时, 说明 skill 不是最新版
    (或没核到版本): 把 ``skill_update.message`` **原样**(三行都要, 链接和「更新技能」那行一字不改)
    放在给用户回复的最前面, 然后照常继续写 —— 不拦写稿, 只是提醒升级。

    返回里有 ``desk_gap`` 时: 你名下有项目最近发出去的帖写作台对不回来。把 ``desk_gap.message`` 原样告诉
    用户(同一场对话里说过就不再说), 然后照常写; 当前这个项目也在里面的话, 那句话就是提醒这次在写作台里写。
    用户没有存档这一步, 别叫用户存档(存档是你调 score_drafts, 查重时记下的稿子发出去以后写作台也会自己对回来)。
    """
    brief = {"tactic": tactic, "draft_topic": draft_topic,
             "key_messages": key_messages, "target_audience": target_audience,
             "tone": tone, "extra_instructions": extra_instructions}
    # 故意不包 _safe: 见 _safe 文档「合规读」。P0 拿不到必须停, 不能给一份
    # p0 为空却看起来正常的简报。
    out = core.build_writing_brief(core.sb(), project_id,
                                   user_id=_user_id, brief=brief)
    # 人在发布前改了什么(D-120 / W-23)。附加信息: 查不到不能把简报弄坏 —— 合规那部分上面已经拿到了。
    try:
        digest = core.edit_digest(core.sb(), project_id, user_id=_user_id)
    except PermissionError:
        raise
    except Exception:  # noqa: BLE001
        logger.warning("open_project: 改动摘要查不到, 这次不带", exc_info=True)
        digest = None
    if digest:
        out["edit_digest"] = digest
    gap = _desk_gap(_user_id, project_id)
    if gap:
        out["desk_gap"] = gap
    # 服务端版本探针 (skillcheck)。协议文件读不了就跳过 —— 探针不能把合规读弄坏。
    try:
        digest = protocol_text()[1]
    except Exception:  # noqa: BLE001
        logger.warning("open_project: 协议文件读不了, 这次不核 skill 版本", exc_info=True)
        return out
    status, reported = skillcheck.report(_user_id, skill_version or None, digest)
    if status != skillcheck.UP_TO_DATE:
        out["skill_update"] = skillcheck.notice(status, reported, digest)
    return out


def draw_angles(project_id: str, n: int, avoid_days: int = 30,
                perpetual_bias: bool = False, _user_id: str | None = None) -> dict:
    """发牌: 给本批 n 篇稿子各分配一组互不相同的创作坐标。

    批量写稿前必须先调这个, 然后【每一篇严格按分到的那组坐标写, 不得互换】。
    同一批里内容形式和标题句式会尽量错开(各 8 种, 先各轮一遍再重复), 所以别把几篇写回同一种体裁。

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
    out = core.draw_angles(core.sb(), project_id, n,
                 avoid_days=avoid_days, user_id=_user_id,
                 perpetual_bias=perpetual_bias)
    # W-29: 发牌成功之后才查, 所以张数含这一批; 查不到就不带, 不影响发牌。
    return _with_archive_pending(out, _user_id, project_id)


def borrow_lessons(project_id: str, tactic: str = "", draft_topic: str = "",
                   key_messages: str = "", target_audience: str = "",
                   tone: str = "", flywheel_arm: str = "", _user_id: str | None = None) -> dict:
    """向帆谷飞轮图书馆【再】借几张真实爆款的经验卡。

    open_project 已经随简报借过一次(返回里的 lessons)。只在换了选题 / 战术、
    想按另一个方向再匹配一批时才调这个; 同样的参数再调一次拿到的是同一批卡。

    这些卡来自公司自己投放过、数据验证过的笔记, 由策展员提炼成「钩子类型 /
    结构骨架 / 为什么有效 / 可迁移手法」。写稿时可以借它的钩子或结构。
    例外: external=true 的是外部参考卡 (最多 2 张), 别家同品类的帖, 不是公司投的、没经过我们的数据验证 ——
    只借结构 / 钩子, 不抄内容, 不提它里面的任何品牌。

    ⚠️ 严禁照抄卡里的标题主干或具体句子 —— 只借手法。
    ⚠️ 标了 synthetic=true 的卡表示【指标未经验证】(疑似人工刷量), 只能凭内容
       判断借鉴, 不要把它的数据当依据。

    库里没有合适的卡时返回空列表, 这不是错误, 照常写就行。

    validated_patterns (可能是空列表) 与 open_project 里的同名字段同一个意思: 已验证规律,
    是表现好过的选项不是模板 —— 每篇最多用一条(照它的 instruction 写), 一批里轮换, 有几篇一条都不用
    (见 validated_patterns_usage); 是提示不是硬约束, 和发牌坐标或 p0 冲突时以它们为准; 空就是这次没有,
    不要自己编。
    flywheel_arm 也同 open_project: 存档时原样传给 score_drafts。调本工具时也把这场 open_project
    给的 flywheel_arm 原样传进来 —— 一场写作只分一次组, 不传会重新分。
    """
    return _safe(core.borrow_lessons, core.sb(), project_id, user_id=_user_id,
                 flywheel_arm=flywheel_arm or None,
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
    (angle_key 是 draw_angles 给的, 有就带上 —— 查重时写作台会顺手记下这一稿和它的 angle_key; 稿子发出去以后,
    写作台按内容从飞书把它对回来、给这张牌销账, 就算这一稿没存过档)。

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
      · pass —— 可以交付: 交给用户之前调 score_drafts(存档 + 参考分; 项目有交付模板时它回拼好的交付稿)。

    summary.semantic_degraded 为 true 时说明 embedding 不可用, 这次只跑了确定性
    查重, 同角度换说法的标题可能漏过 —— 【要告诉用户这件事】, 别默默交付。

    这个工具出错会直接报错而不是放行 —— 查重挂了必须停下来, 不能当作通过。
    """
    # 故意不包 _safe: 查重是硬闸, 出错必须冒泡。
    out = core.check_drafts(core.sb(), project_id, drafts, user_id=_user_id, record_snapshot=True)
    # W-29: 判定之后才查; 查不到就不带, 判定结果原样回去。
    return _with_archive_pending(out, _user_id, project_id)


def ack_edit_digest(project_id: str, through: str, rules_recorded: int = 0,
                    _user_id: str | None = None) -> dict:
    """看完 open_project 给的 edit_digest 之后调: 记下这份摘要看过了, 一周内不再出现。

    ``through`` 原样传回 edit_digest.through; ``rules_recorded`` 是这次按摘要记了几条规则(用户一条都没要就传 0)。
    摘要已经说给用户、用户没接话的, 在这一轮最后一条回复之前调(传 0) —— 不调的话下一场写作还会再给一遍同一份摘要。
    还没把 edit_digest.message 说给用户就别调: 调了这批改法就再也不会出现。
    """
    # 写操作, 不包 _safe: 没记上的话, 下一场又拿到同一份摘要, 用户会被问第二遍。
    try:
        return core.ack_edit_digest(core.sb(), project_id, through, user_id=_user_id,
                                    rules_recorded=rules_recorded)
    except core.ProjectNotFound:
        raise
    except ValueError as exc:
        raise InvalidInput(str(exc)) from exc


def judge_draft(project_id: str, title: str, body: str, judge_paras: str = "on_fail",
                banks: list[str] | None = None, brief: dict | None = None, hard_rules: dict | None = None,
                target: dict | None = None, validated: list | None = None,
                _user_id: str | None = None) -> dict:
    """判一篇稿子的内容(可选, 写后参与点): 特征题库 + 平台题库 + 人感题库, 项目放行了再加项目层。
    返回篇级画像 profile、硬伤 hard_fails(题、答案、概率、依据句)、修改单 plan、只记录的目标题 recorded、
    段级分布 para_stats、数据出境 policy、用的题库 banks。不改稿、不落账本、不拦交付。

    什么时候调: score_drafts 已经给每篇自动打过参考分; 这个工具是**深一层**的(还判段落、人感), 只在用户问「这篇
    哪里有问题」、或者想在 score_drafts 之外再细查一篇时调。一篇一调。
    怎么用返回值: hard_fails / plan 只说哪一句犯了哪条, 不给改法 —— 照着改稿, 改完最多再判一轮, 不要循环。
    judge_status 不是 ok(not_configured / timeout / policy_blocked / unavailable …)时: 如实告诉用户这次没拿到判定,
    查重和存档照常走, 不要把它当成"通过"也不要重试超过一次。它不是闸: 判定不过也能交付, 但要把硬伤告诉用户。
    project 必须是你有权限的项目, 且接了 tv_project_map(没接会回 not_configured 并写明补救)。
    """
    return core.judge_draft(core.sb(), project_id, title, body, user_id=_user_id, judge_paras=judge_paras,
                            banks=banks, brief=brief, hard_rules=hard_rules, target=target, validated=validated)


def repair_plan_for(project_id: str, title: str, body: str, banks: list[str] | None = None,
                    brief: dict | None = None, hard_rules: dict | None = None, target: dict | None = None,
                    validated: list | None = None, _user_id: str | None = None) -> dict:
    """判一篇稿子并只拿修改单(同 judge_draft 的一次调用, 返回更短): plan 每条 = 题号 + 现在的答案与概率 + 依据句 +
    该题定义, 只说哪一句犯了哪条, 不给改法; recorded 是没过闸二、只记录不下发的目标题(给了 target 才有)。
    用法和限制同 judge_draft: 写后可选、不落账本、不拦交付, judge_status 不是 ok 就如实告诉用户。
    """
    return core.repair_plan_for(core.sb(), project_id, title, body, user_id=_user_id, banks=banks, brief=brief,
                                hard_rules=hard_rules, target=target, validated=validated)


def list_banks(_user_id: str | None = None) -> dict:
    """判定服务上可用的题库清单(名字、版本、层、公开的题目 id; 暗题只报个数)。只在要给 judge_draft 指定 banks、
    或用户问"判的是什么"时调; 平时不用, judge_draft 不传 banks 就是默认三层。"""
    return core.list_banks()


def comments_for(link: str = "", project_id: str = "", post_title: str = "", post_body: str = "",
                 skill_version: str | None = None, _user_id: str | None = None) -> dict:
    """写评论的入口: 用户给一条发布链接(或贴帖子正文 + 说是哪个项目), 一次拿齐写评论要的东西。不用先 open_project。

    传 ``link`` 就行: 服务端按链接在 TV 里找到那篇帖子、它属于哪个项目、已有的评论。TV 里没有这条(每天同步一次; 短链和
    长链互相认不出)就回 ``resolved=false`` 和 ``need``: 照 ``note`` 让用户把帖子标题、正文贴过来, 再带 ``post_title`` /
    ``post_body`` / ``project_id`` 调一次。用户自己贴了正文时以贴的为准。

    返回:
      · post —— 要接的帖子(标题 + 正文)
      · existing_comments —— 帖子下已有的评论(TV 里有才有): 别重复里面已经问过的事
      · comment_rules —— 本项目管评论的规则(「评论区：」开头的), 必须守
      · hard_rules —— 本项目的硬约束(合规话术、禁词), 评论一样要守
      · slots —— 默认四个评论位: q1 / q2 提问、exp 补充经验、reply 贴主答疑(回 q1)。每位写一条, 位的要求在 note 里;
        不写背书、不编造用过某产品的亲历。用户另有要求(比如只要两条提问)就照用户的, 位的 id 照用
      · project_id —— 下一步 score_comments 要带的
    写完整组交给 ``score_comments``。评论不进 check_drafts / score_drafts, 也不存库。

    ``skill_version``: 同 open_project, 每次都传 skill 里 ``protocol_version:`` 那一行的值; 返回里有 ``skill_update`` 时把
    ``skill_update.message`` 原样放在回复最前面。
    帖子属于别人的项目时报 403, 不回帖子内容。
    """
    out = core.comments_for(core.sb(), user_id=_user_id, link=link, project_id=project_id,
                            post_title=post_title, post_body=post_body)
    try:
        digest = protocol_text()[1]
    except Exception:  # noqa: BLE001
        logger.warning("comments_for: 协议文件读不了, 这次不核 skill 版本", exc_info=True)
        return out
    status, reported = skillcheck.report(_user_id, skill_version or None, digest)
    if status != skillcheck.UP_TO_DATE:
        out["skill_update"] = skillcheck.notice(status, reported, digest)
    return out


def score_comments(comments: list[dict], link: str = "", project_id: str = "", post_title: str = "",
                   post_body: str = "", kind: str = "product", max_named: int = 1,
                   _user_id: str | None = None) -> dict:
    """一组评论写完后调(自动调, 不用等用户说): Jev 整组判一次, 回每条过没过和修改建议。只判, 不存库, 不发布。

    ``comments`` 传 ``[{"slot": "q1", "text": "..."}, {"slot": "reply", "text": "..."}, ...]``: slot 写 comments_for 给的
    评论位 id(自定义的评论位传整个对象); 同一个 id 只能有一条; 贴主答疑那条会自动接上它回的那一位。帖子参数和
    comments_for 那次一样: ``link``, 或 ``post_title`` / ``post_body`` + ``project_id``。
    ``kind``: product(默认, 卖货的品)/ agency(机构服务)/ shop(门店)。``max_named``: 整组最多几条点名品牌(默认 1)。

    怎么读返回值:
      · 每条 ``passed=true`` 且 ``needs_review=false`` 才算这条过。``summary.passed`` 是这组过了几条 —— 这就是整组的分数,
        照实告诉用户。
      · ``fix`` 是必须改的(主要行为、点名品牌、接住帖子、细节四项); ``hints`` 只是参考, 不改不算没过, 别为了消掉提示反复重写。
      · ``thread_fix`` 是整组层面的(点名太多、评论区没有摩擦), 按「位 xx」改那一位。
      · 没过的按 fix 改**一轮**, 再调一次本工具, 然后不管结果都交付并如实说明; 不要循环。
      · ``needs_review=true``(多半是像背书、或判得拿不准)交人工核实: 不能改成伪造的亲历, 也不要靠反复润色把风险抹掉。
      · ``judge_status`` 不是 ok 就说这次没判成, 不能说通过。
    """
    try:
        return core.score_comments(core.sb(), user_id=_user_id, comments=comments, link=link,
                                   project_id=project_id, post_title=post_title, post_body=post_body,
                                   kind=kind, max_named=max_named)
    except core.CommentInputError as exc:
        raise InvalidInput(str(exc)) from exc


def score_drafts(project_id: str, drafts: list[dict], flywheel_arm: str = "", tactic: str = "",
                 _user_id: str | None = None) -> dict:
    """稿子写完、过了 check_drafts 就调(**自动调, 不等用户说**): 一次做两件事 ——

      1. **存档**: 让它们参与以后的查重、给用到的角度销账、留下发布后能对回来的底稿;
      2. **打分**: Jev 给每篇一个参考分和最多 3 条修改建议。**不拦交付。**

    写手不用为这一步做任何事, 也没有"定稿 / 导出 / 审核"这些步骤了: 用户把定下来的稿子(可以手改过)自己贴进飞书,
    发出去之后系统每天按内容自动对回来, 不用问用户发没发。只传交付了的稿子; 你自己或用户当场毙掉的不传。

    **分数怎么读、怎么说**(在 ``judge.results`` 里, 按 ``index`` 对上你传的第几篇):
      · ``score`` 满分 100, 只看篇级硬约束(平台合规 + 特征题库里的合规题), 文笔好坏不在里面。Jev 有把握的问题每条 -25,
        拿不准的每条 -8, 有硬约束没判出来的每条 -10; 正文太短判不了的 score 为空(``why`` 里写着)。
      · ``fixes`` 是最该看的几条: ``sure=true`` 是 Jev 有把握的, ``sure=false`` 是拿不准、看一眼就行的。``more_fixes``
        是没列出的条数。``instruction`` 只说哪一句犯了哪条, 不给改法。
      · 把每篇的分数和 fixes **照实**告诉用户, 一两句话就够。用户要改哪篇: 按 fixes 改完, 那篇带
        ``replaces_version_id`` 再调一次本工具(会重新打分)。改一轮就够, 不要为了分数反复重写。
      · ``judge_status`` 不是 ok(超时、没配、被出境口径挡下)就说这篇这次没打成分, 不要重试, 存档照样成功了。

    **改稿用 ``replaces_version_id`` 替换, 不要当新稿再存一次。** 值是上次返回的 ``version_ids`` 里对应的那个。存档
    会先摘掉旧版的指纹再查重, 所以不会撞上它自己; 成功后旧稿原地升成新版本, ``replaced`` 里列出来, ``version_ids``
    里是新 id。当新稿再存一次的话, 会被自己的旧版拦下, 而旧的那条永远占着角度。整篇推翻换角度重写的才当新稿。

    ⚠️ 存档时【会再查一次重】, 而且自带和 check_drafts 同一套闸: 判 reject 的不存档, 在 ``rejected`` 里列出来
    (``gate="pre_commit"`` 是闸前拦下, ``gate="atomic_recheck"`` 是写入时撞上了队友刚存的稿子)。那几条要重写后
    重新走 check_drafts, 不能当作已交付。``gate="empty_body"`` 是这篇的 ``body`` 是空的(正文放错了字段): 不用重写,
    把正文放进 ``body`` 原样再调一次本工具。``gate_summary`` 里该告诉用户的(semantic_degraded / empty_history_warning)
    照 check_drafts 的规矩说。

    **每条都带 ``angle_key``**(draw_angles 分给它的那组)。``unattributed`` 非零就是有几条没带 —— 那几个角度没销账,
    下一批还会被抽到。这时返回里还会带 ``angle_debt``: 你在这个项目当前还挂着的坐标, **下一批按它们写, 别再抽新的**。
    ``angle_key`` 要**原样复制** draw_angles 给的那个(20 位十六进制串), 不要自己编号、缩写或改写: ``unknown_angle_keys``
    非零是有几篇带的 key 这个项目的台账里没有, ``used_angle_keys`` 非零是有几篇带的 key 已经销给了别的稿子(一个 key 只给一篇),
    哪几篇在 ``angle_key_mismatch`` 里(``problem``: unknown 编的或抄错的 / other_project 你在别的项目抽的牌, 先确认是不是
    存错了项目 / not_in_ledger 台账没写进去, 不是你的错 / used), ``angle_key_warning`` 说怎么办 —— 那几篇已经存档, 不用重存,
    但角度没销账; 接下来存的照 draw_angles 的原样带。

    **话题、评论、方向是每篇 draft 自己的字段, 不要塞进正文**(都可选, 项目要什么看简报的 ``delivery_requirements``):
    ``drafts=[{"title": ..., "body": ..., "angle_key": ..., "tags": [...], "comments": [{"role": "reader" | "author",
    "text": "..."}], "direction": "..."}]``。``tags`` 是话题列表(带不带 # 都行); ``comments`` 是这篇笔记自己带的评论;
    ``direction`` 是这篇的切入角度, 一句话(如「应届生签约前怎么查公司」), **不是** 产品向 / 流量向。它们不是本工具的
    顶层参数, 放在 drafts 外面会被丢掉。``body`` 只放正文, 不放【标题】【正文】标记、不放话题行 —— 交付模板不管话题的项目
    例外(简报的 ``delivery_requirements.task`` 会说): 那种模板不读 ``tags``, 话题照项目规则写在正文末尾那一行。

    **交付稿**: 项目配了交付模板时返回里有 ``delivery``: ``text`` 是按模板把这次存进去的几篇拼好的整批交付稿(表格或
    一篇一段, 标记、话题顺序、固定话题都由服务端加), ``problems`` 是代码查出来的版式问题(``index`` 对应你传的第几篇,
    ``what`` 说怎么改)。有 problems 就先改那几篇(带 ``replaces_version_id`` 再调一次; 新返回的 ``delivery`` 只含改过的
    那几篇, 按顺序替换原来那几篇: 表格只换那几行, 新返回的表头和分隔行不要再贴一遍; 一篇一段的换那几段), 然后把
    ``delivery.text`` **原样**交给用户 —— 不要自己重排、不要再加标记。没有 ``delivery`` 就照项目规则自己排。

    **规则检查**: ``rule_checks`` 是代码按项目里带 check 的规则逐篇查出来的命中(有命中才带), 每条 ``index`` /
    ``rule_id`` / ``severity`` / ``where`` / ``what``。``what`` 以「必须改」开头的是硬约束, 改完再交; 「建议改」的
    告诉用户。和分数一样不拦存档。必须改的全列, 建议改补到一共 40 条为止; ``rule_checks_more`` 是没列出的建议改条数,
    如实告诉用户还有这么多条没列。``rule_checks_status`` 是 ``"error"`` 就是这次没查成, 不能说规则都用代码查过了。
    ``rule_checks_unchecked`` 里的规则这次没查完(正则没在时限内跑完、限时的引擎跑不了, 或者存的 check 用不了): 算没查 —— 不是违规(``what`` 以
    「硬约束·没查完」/「建议·没查完」开头, 不是「必须改」), 也不能说它们守住了; ``from_index`` 是从哪篇起没查完, 之前查出来、
    列在 ``rule_checks`` 里的命中照常算; severity 是 hard 的照规则原文(``rule``)把那几篇自己核一遍。
    存完之后**再调一次 ``check_rules``**(传这次返回的 ``check_rules_version_ids``, 传过 ``tactic`` 的同样带上):
    那是按 Jev 编好的规则逐篇查, 和这里的 ``rule_checks``(人手设过 check 的那几条)不重复。``check_rules_version_ids`` 只有这批里的新稿: 带
    ``replaces_version_id`` 改过重存的不再查(改一轮就够), 全是重存时它是空的, 不用调, ``next_step`` 也不会再让你调。
    篇数多也整串传: check_rules 一次查 20 篇, 没查到的放在它返回的 ``remaining_version_ids`` 里, 照它的 ``next_step`` 接着调。

    ``project_isolation`` 是串项目的提示: 有可疑标记要说明并核对; 没发现或检查超时都不能声称已完整排除串项目。

    这个工具出错会直接报错。存档失败必须让你知道 —— 稿子没进指纹库的话, 下次 check_drafts 会把同样的内容再放行
    一次。看到报错就重试, 别当没事; 报"另一个写操作正在跑"(409)也是重试。带 ``identity_warning`` 时说明底稿没建成:
    查重不受影响, 但发布后对不回来, 如实告诉用户; 没建成的那几篇也不在 ``check_rules_version_ids`` 里, check_rules 查不了,
    不能说它们守住了规则(``next_step`` 会说)。

    ``flywheel_arm``: 把这场写作的 open_project (或 borrow_lessons) 返回里的 ``flywheel_arm`` **原样**传回来。
    它决定这批稿子算进哪个对照组; 不传服务端会猜, 但同一项目同时开着两场时会猜错。传的不是服务端这场借卡发给你的那个,
    以服务端为准, 返回里带 ``flywheel_arm_corrected``; 服务端中途重启过、核对不了的带 ``flywheel_arm_unverified``(你不用改)。

    ``tactic``: 这场 open_project 传过 ``tactic`` 的, 把同一个值**原样**传回来(没传过就不传)。规则检查按它和项目名判这批
    是产品向还是流量向, 和简报同一个判据; 不传的话只按项目名判, 简报里因为方向不符没给你的建议改规则, 这里会再报一遍。
    """
    # 故意不包 _safe: 这是【写】操作。_safe 会把异常变成一个看起来成功、只带 error 字段的结果, 而存档失败若被当成
    # 成功, 稿子静默缺席指纹库, 查重的历史出现空洞, 同样的稿子以后能再过一次闸。
    return core.commit_drafts(core.sb(), project_id, drafts, user_id=_user_id,
                              flywheel_arm=flywheel_arm or None, tactic=tactic or "")


def check_rules(project_id: str, version_ids: list[str], tactic: str = "", _user_id: str | None = None) -> dict:
    """score_drafts 存完档之后调一次(**自动调, 不等用户说**): 按这个项目的规则逐篇查刚存的稿子, 回每篇哪几条没守住。
    **只是建议, 不拦交付**, 存档已经成功了。

    ``version_ids`` 传 score_drafts 这次返回的 ``check_rules_version_ids``(这批里的新稿; 带 replaces_version_id 改过重存
    的不在里面, 不再查), 篇数再多也原样整串传: 一次查前 20 篇, 多出来的原样放在返回的 ``remaining_version_ids`` 里 ——
    有它就照 ``next_step`` 接着再调一次、传它(``tactic`` 照样带), 直到返回里没有它; 不是这个项目的稿子不查(列在
    ``excluded`` 里)。规则由 Jev 事先编成「是非题」和「代码检查」两种, 这里只用编好的; 还没有编译结果的规则这次查不了,
    数在 ``uncompiled_rules`` 里(``next_step`` 说为什么: 后台在编 / 这个项目编不了 / Jev 最近编译超时 / 上次编失败)
    —— 不用等, 也不用重试。要看话题 / 评论的规则这里查不了(这两样不随稿子存), 数在 ``rules.code_unavailable``; 编不了
    (原文超过 Jev 的上限之类)的数在 ``rules.not_compilable``, 编出来的检查不安全没跑的数在 ``rules.code_refused``,
    只查了一部分的(编译时就没全装进去, 或者只有一部分要看话题 / 评论、只查了标题正文那一半)数在 ``rules.partial``,
    用 record_rule 撤掉了代码检查、Jev 又只编成了代码检查(没有规则题)的数在 ``rules.code_withdrawn`` —— 这几种都
    **不能说(整条)守住了**。人手设过 check 的数在 ``rules.by_score_drafts``: score_drafts 存档时已经按它查过(那边
    ``rule_checks`` 里的「必须改」照改), 这里不重复查。``rules.by_code`` 是交给代码查的条数, 正则没在时限内查完的
    (``rules.code_timed_out``)也算在里面。

    怎么读返回值(``versions`` 一篇一项, 按 ``version_id`` 对上):
      · ``violations``: 没守住的规则, 每条 ``rule_id`` / ``severity`` / ``what`` / ``source``(jev 是 Jev 判的, code 是代码
        查的)/ ``p``(Jev 的把握)/ ``evidence``(Jev 指出的那一句, 有才带)。``what`` 以「必须改」开头的是硬约束:
        照着改, 改完那几篇用 score_drafts 带 ``replaces_version_id`` 再存一次, 改一轮就够, 不用再调本工具;
        以「建议改」开头的是软规则: 告诉用户, 改不改由用户定。
      · ``unsure``: Jev 拿不准的(``what`` 以「拿不准」开头), 不算没守住, 提一句就行, 不用改。
      · 每篇必须改全列, 建议改补到 ``violations`` 一共 12 条为止, ``unsure`` 最多列 5 条; ``more`` 是这篇没列出来的条数
        (``summary.more`` 是全部篇加起来的), 有就如实告诉用户还有这么多条没列。
      · ``judge_status`` 不是 ok 的那篇(``judge_note`` 说原因, 比如「没判完」「一道都没判出来」「正文是空的」)规则题这次
        没判成, **不能说它守住了规则**; 不用重试。代码查出来的 ``violations`` 照样有效。正文是空的那几篇另数在
        ``summary.empty_body``。
      · ``judge_status`` 是 ok、但 ``counts.unjudged`` 不是 0 的: 那几道题 Jev 没判出来(``unjudged`` 列着规则 id 和原因,
        比如 evidence_not_found = 判了「是」却指不出是哪一句, 不算没守住), 那几条**也不能说守住了**。``summary.unjudged``
        是全部篇加起来的题数。
      · ``code_unchecked``: 代码查的正则这次没在时限内查完(或者限时的引擎跑不了它)的规则, 每条 ``rule_id`` / ``severity`` /
        ``rule``(规则原文)。没查完的那部分不是违规, 也**不能说那几条守住了**; 同一篇里同一条已经列在 ``violations`` 的
        (先查到了命中、后面才没查完)照改; hard 的照原文自己核一遍, 不用重试。``summary.code_unchecked``
        是全部篇加起来的处数。
      · ``status`` 是 nothing_to_check / no_versions 时照 ``next_step`` 说一句就行。
      · ``status`` 是 error: 这次规则没查成(服务端读不出规则的编译结果), **不能说规则守住了**; 存档已经成功, 交付照常,
        不用重试。
    照 ``next_step`` 做, 把结果一两句告诉用户; 不要因为这个工具的结果拖着不交付, 也不要为了消掉「建议改」反复重写。

    ``tactic``: 同 score_drafts —— 这场 open_project 传过 ``tactic`` 的, 把同一个值**原样**传回来(没传过就不传)。
    哪些建议改规则要查按它和项目名判方向, 和简报同一个判据; 不传就只按项目名判。
    查的是交付出去的那一份(和 score_drafts 的 ``rule_checks`` 同一份): 项目有交付模板、模板管话题时, 正文末尾的话题
    (末尾那行、或最后一句后面直接跟着的一串)算话题行, 不算正文。
    """
    # 故意不包 _safe: 包成带 error 的"成功"会让模型说"规则都查过了"。Jev 那边的失败已经在每篇的 judge_status 里了;
    # 编译结果读不出来(migrations/015 没跑)core 自己回 status=error + 一句「没查成」, 记日志(2026-10-20 第三轮复审:
    # 原来抛出来, REST 是 500 = 「待会儿重试」, MCP 是工具错误, 服务端一行日志都没有)。走到异常这一步是规则池都读
    # 不出来, 必须让模型知道这次没查成。
    try:
        return core.check_rules(core.sb(), project_id, version_ids, user_id=_user_id, tactic=tactic or "")
    except core.RuleCheckInputError as exc:
        raise InvalidInput(str(exc)) from exc


def commit_drafts(project_id: str, drafts: list[dict], flywheel_arm: str = "", tactic: str = "",
                  _user_id: str | None = None) -> dict:
    """旧名, 和 ``score_drafts`` 完全一样(存档 + 参考分)。新版 skill 只调 score_drafts; 这个名字留着是为了还没升级
    skill 的写手 —— 别在新对话里调它, 参数、返回值、怎么读分数都看 score_drafts 的说明。"""
    # 同 score_drafts: 写操作, 不包 _safe。
    return core.commit_drafts(core.sb(), project_id, drafts, user_id=_user_id,
                              flywheel_arm=flywheel_arm or None, tactic=tactic or "")


INGEST_MAX_PER_CALL = 50

# 同一个项目的补录互斥在 core.ingest_published 里做(进程内锁 + migrations/009 的
# 库锁): 这里和 CLI 的 tv-sync 是两个进程, 锁必须在共用的那一层。
class InvalidInput(ValueError):
    """调用方参数不对, 不是服务端坏了。REST 层映成 400 —— 500 会让模型拿同一个
    超大载荷一直重试, 400 才说得清是"改参数"。"""


_TITLE_KEYS = ("title", "标题")
_BODY_KEYS = ("body", "正文", "内容")
_LINEAGE_KEYS = ("version_id", "_source_autowriter_version_id")


def _first(d: dict, keys: tuple) -> str:
    for k in keys:
        v = d.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def ingest_published(project_id: str, drafts: list[dict], source: str = "",
                     dry_run: bool = False, _user_id: str | None = None) -> dict:
    """把【已经发出去、但当时没走 score_drafts 存档】的稿子补进指纹库。**不过闸**。

    什么时候调: 用户说「这批已经发了/上周发的, 没入库」「补入库」「补指纹」
    「把飞书表里的稿子补进去」, 或者把一批**已经发在小红书上**的稿子粘给你要
    "入库"。跟 score_drafts 分清: 那是**刚写完要交付**的稿子(过闸、销角度);
    这里是**已经发过的历史**, 它跟库里谁重复都改变不了事实, 拦它没有意义 ——
    不拦, 照收, 让下一批查重能看见它们。

    怎么传:
      · ``drafts`` 每条 ``{"title": …, "body": …}``(也认「标题」「正文」两个键),
        从用户粘的表里逐条摘, 正文**整篇**给, 不要截断 —— 指纹按全文算, 只给
        开头会让后面的稿子撞不上它。**每条都要有正文**; 有一行没正文就整批
        报错、一行都不写, 补上正文再整批重传。
      · 表里 ``_source_autowriter_version_id`` 那列**有值**的行是真走过 commit 的,
        库里已经有, 这条可以原样带 ``version_id`` 传进来, 会被跳过并数在
        ``skipped_already_committed`` 里; 也可以自己不传。
      · 一次最多 50 条, 多了直接报错, 分几次调。**重复调是安全的**: 库里已有
        指纹的稿子会被跳过(``skipped_already_fingerprinted``), 不会翻倍; 同一个
        项目的补录跨进程互斥(库里的锁), 两次调用重叠也不会各建一份; 锁被别人
        持有太久会报「另一个补录正在跑」—— 那不是坏了, 稍后再调。
      · 用户给的表很长时, 先 ``dry_run=true`` 调一次, 把「会写 N 条、已有 M 条」
        告诉用户, 再正式调。dry-run 不写库。

    ⚠️ 报错与返回的分界 —— 这个工具和别的写类工具**不一样**:
      · **参数不对**(空列表、超过 50 条、有行没正文)、越权、库连不上 → 直接报错,
        一行都没写; 参数错的改好整批重传即可。
      · **半途失败不报错**: 身份建了、指纹没写上这种, 会**正常返回**, 带
        ``fingerprint_error`` 和 ``note``。所以每次调完都要看返回值, 不能只看
        有没有报错, 更不能"看到 fingerprint_error 就重试"。

    看返回值(``note`` 就是要念给用户的那句):
      · ``minted`` / ``fingerprinted`` 相等 → 这批补完了。
      · 只有 ``identity_error`` → 没建成的行**重传即可**。
      · ``fingerprint_error``(或 fingerprinted < minted) → **不要重传这批**: 它们的
        身份已经建了、指纹没写上, 重传看不见指纹会再建一份身份; 要工程侧对这个
        项目跑 backfill 补指纹。
      · 两者同时出现 → **先** backfill, **再**把这批原样重传一次(已补上指纹的会
        被跳过, 没建成身份的会补上)。顺序不能反。
    """
    if not isinstance(drafts, list) or not drafts:
        raise InvalidInput("drafts 要是非空列表, 每条 {title, body}")
    if len(drafts) > INGEST_MAX_PER_CALL:
        raise InvalidInput(f"一次最多 {INGEST_MAX_PER_CALL} 条(收到 {len(drafts)}), "
                           "分几次调 —— 重复调是安全的, 已有的会被跳过")
    committed = 0
    entries: list[dict] = []
    invalid: list[dict] = []
    for i, d in enumerate(drafts):
        if not isinstance(d, dict):
            invalid.append({"index": i, "reason": "不是对象", "got": type(d).__name__})
            continue
        if _first(d, _LINEAGE_KEYS):
            committed += 1        # 真走过 commit 的行, 库里已有身份和指纹
            continue
        body = _first(d, _BODY_KEYS)
        if not body:
            # 没正文的行算不出任何指纹, 写进去也挡不住谁, 而且重传时认不出来
            # (没有开头哈希)会再建一份 —— 所以整批拒掉, 让模型把正文补上。
            invalid.append({"index": i, "reason": "没有正文(body/正文)",
                            "keys": sorted(str(k) for k in d)[:8]})
            continue
        entries.append({"title": _first(d, _TITLE_KEYS), "body": body})
    if invalid:
        raise InvalidInput(f"有 {len(invalid)} 行没有可用的正文, 一行都没写: {invalid[:5]} "
                           "—— 每条要 {title, body}, 正文整篇给; 补好后整批重传, "
                           "已有的会被跳过")
    zero = {"received": len(drafts), "to_write": 0,
            "skipped_already_fingerprinted": 0, "skipped_duplicate_in_sheet": 0,
            "skipped_already_committed": committed,
            "minted": 0, "fingerprinted": 0, "batch_id": None,
            "identity_error": None, "fingerprint_error": None,
            "embedded": False, "dry_run": dry_run}
    if not entries:
        return {**zero, "note": f"这 {committed} 行都是走过 commit 的, 库里已有, 没有要补的。"}
    out = core.ingest_published(core.sb(), project_id, entries, user_id=_user_id,
                                source=source or "workbuddy", dry_run=dry_run)
    out["skipped_already_committed"] = committed
    out["received"] = len(drafts)
    fp_failed = bool(out.get("fingerprint_error")) or out["fingerprinted"] < out["minted"]
    id_failed = bool(out.get("identity_error"))
    missing = out["minted"] - out["fingerprinted"]
    if fp_failed and id_failed:
        out["note"] = (f"两种半途失败同时发生: {missing} 条建了身份没写上指纹, 另有几行身份"
                       "没建成。顺序必须是: **先**请工程侧对这个项目跑一次 backfill 把指纹"
                       "补上, **然后**再把这批原样重传一次(已补上指纹的会被跳过, 没建成"
                       "身份的会补上)。别反过来, 也别现在就重传。请把这句话转给工程。")
    elif fp_failed:
        out["note"] = (f"有 {missing} 条建了身份没写上指纹 —— 不要重传这批(重传会再建一份"
                       "身份)。这一步需要工程侧对这个项目跑一次 backfill 把指纹补上, "
                       "请把这句话转给工程。")
    elif id_failed:
        out["note"] = "有几条身份没建成, 这些行重传即可; 库里已有的会被跳过, 不会翻倍。"
    elif dry_run:
        out["note"] = (f"dry-run, 没动库: 会写 {out['to_write']} 条; 指纹库里已有 "
                       f"{out['skipped_already_fingerprinted']} 条、表内重复 "
                       f"{out['skipped_duplicate_in_sheet']} 条、已走过 commit "
                       f"{committed} 条(都跳过)。确认后去掉 dry_run 再调一次。")
    else:
        out["note"] = (f"补完: 建身份 {out['minted']} 条、写指纹 {out['fingerprinted']} 条; "
                       f"跳过已有指纹 {out['skipped_already_fingerprinted']}、表内重复 "
                       f"{out['skipped_duplicate_in_sheet']}、已走过 commit {committed}。")
    return out

# ══════════════════════════════════════════════════════════════════════
# 反馈学习
# ══════════════════════════════════════════════════════════════════════

def record_rule(project_id: str, content: str, severity: str = "soft",
                scope: str = "project", applicability: str = "", check: dict | None = None,
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

    check(可选): 规则是**词表 / 某个词能不能出现在哪 / 字数范围**时, 再带一份代码能查的形状, 存档时服务端逐篇查,
    命中回在 score_drafts 的 ``rule_checks`` 里。content 照旧写人话, check 是它的机器版:
      · {"kind": "forbidden_word", "words": ["词1", "词2"], "where": ["title", "body"]}  —— 这些词不能出现
      · {"kind": "required_phrase", "any": ["话术1", "话术2"], "where": ["body"]}         —— 至少出现一个
      · {"kind": "max_len", "max": 300, "min": 150, "where": "body"}                       —— 字数范围(不算空白和话题),
        max / min 至少给一个, where 只认 title / body
      · {"kind": "forbidden_regex", "pattern": "...", "where": [...]}                       —— 固定写法(正则最长 300 字, 会做安全检查)
      · {"kind": "free_text"}                                                               —— 撤掉这条的代码检查(存档时、
        check_rules 里编出来的都不跑; check_rules 的规则题照判。Jev 只把它编成了代码检查、没有规则题的, 撤掉之后就没有
        东西查它了, check_rules 数在 rules.code_withdrawn, 不说它守住了)
      where 可选 title / body / tags / comments, 不传就是 ["title", "body"]。语气、结构这类判断不了的不要带 check。
      词的开头 / 结尾是英文字母数字的, 那一头按词边界认: "Q群" 不算出现在「QQ群」里、"A货" 不算出现在「AA货」「1:1A货」里
      (所以「TV信号」里也不算有 "V信") —— 两种写法都要拦就都列上。
      check 写错会报错且这条规则不落库: 照报错改了再调一次。返回里的 ``check`` 是库里存下的那份。

    记下之后服务端会在后台把这条规则交给 Jev 编成写后检查(几分钟内好), 之后 ``check_rules`` 就会查它。不用等、不用管。
    """
    # 故意不包 _safe: 这是【写】操作, 且写的可能是 hard 合规规则。写失败若
    # 报成功, 这条规则会静默缺席之后的每一份简报 —— 而用户以为已经记住了。
    try:
        out = core.record_rule(core.sb(), project_id, content,
                               severity=severity, scope=scope, user_id=_user_id,
                               applicability=applicability, check=check)
    except core.rulecheck.CheckError as exc:
        # check 写错是参数问题(400), 不是服务坏了(500): 500 会让模型拿同一个 check 一直重试
        raise InvalidInput(f"check 写错了, 这条规则没记: {exc}") from exc
    # W-29: 记规则多半是用户看了稿给的反馈, 改完的稿子同样要存档。规则写成之后才查, 查不到就不带。
    return _with_archive_pending(out, _user_id, project_id)


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
    "get_protocol":   (get_protocol,   True),
    "create_project": (create_project, True),
    "list_projects":  (list_projects,  True),
    "open_project":   (open_project,   True),
    "draw_angles":    (draw_angles,    True),
    "borrow_lessons": (borrow_lessons, True),
    "check_drafts":   (check_drafts,   True),
    "score_drafts":   (score_drafts,   True),
    "check_rules":    (check_rules,    True),     # 存档后按编译好的规则逐篇查, 只作建议(2026-10-20, D-121)
    "commit_drafts":  (commit_drafts,  True),     # 旧名, 等同 score_drafts(D-120 过渡期, 老 skill 还在调)
    "judge_draft":    (judge_draft,    True),
    "repair_plan_for": (repair_plan_for, True),
    "list_banks":     (list_banks,     True),
    "ack_edit_digest": (ack_edit_digest, True),
    "comments_for":   (comments_for,   True),
    "score_comments": (score_comments, True),
    "ingest_published": (ingest_published, True),
    "record_rule":    (record_rule,    True),
    "record_edit":    (record_edit,    True),
    "save_my_style":  (save_my_style,  True),
    "label_example":  (label_example,  True),
    "my_style":       (my_style,       True),
    "my_rules":       (my_rules,       True),
    "set_rule_state": (set_rule_state, True),
    "reembed_my_rules": (reembed_my_rules, True),
}
