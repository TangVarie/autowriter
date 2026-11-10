"""deskcore/tools.py — MCP 工具面。

按写稿的流程分组: 取协议 → 开项目 → 写稿前 → 写稿后(查重 → 存档 + 参考分 → 规则检查) → 收反馈,
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
  · 认不出调用者 / 项目不归你 → 一律抛 PermissionError(_require_caller /
    core.assert_project_access), 不算"出错降级", 也不回带 error 的 dict。
"""

from __future__ import annotations

import contextlib
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


def _attach_skill_update(out: Any, tool: str, user_id: str | None, raw_version,
                         current: str | None = None) -> Any:
    """入口工具的版本探针 (skillcheck.probe): 这次调用报上来的是旧版, 就在返回值上加 ``skill_update``; 最新 / 没报都不加。
    ``current`` 不给就现读协议版本。协议文件读不了、探针出任何错都只记日志 —— 探针不能把合规读 (open_project) 弄坏。"""
    if not isinstance(out, dict):
        return out
    try:
        update = skillcheck.probe(user_id, raw_version, current or protocol_text()[1])
    except Exception:  # noqa: BLE001
        logger.warning("%s: 这次没核 skill 版本(协议文件读不了或探针出错)", tool, exc_info=True)
        return out
    if update:
        out["skill_update"] = update
    return out


def get_protocol(local_version: str | None = None,
                 _user_id: str | None = None) -> dict:
    """核对写作台协议的版本; 本地没有或已过期时下发全文。对话开始时调一次。

    ``local_version`` 传 skill 文件里 ``protocol_version`` 那一行的值。返回:
      · ``version``     —— 服务端协议的版本哈希
      · ``up_to_date``  —— True: 本地 skill 就是最新版, **照 skill 执行**, 本返回值
                          里没有正文; False: 本地是旧版, 正文在 ``protocol`` 里,
                          照它执行; 升级提醒就是下面的 ``skill_update``, 不另说别的;
                          None: 没传 local_version, 全文已返回
      · ``protocol``    —— 协议正文(只在需要时带)
      · ``skill_header`` —— skill 文件引线头里那几条独立规矩(和 protocol 一起带;
                          版本号盖的是两者, 头改了也算过期)
      · ``skill_update`` —— (你传的版本不是最新版时才有; 没传就没有) 升级提醒。把它的
                          ``message`` **原样**(三行都要)放进开场、复述项目名之后(同一条只放一次),
                          然后照常继续; 不另说别的升级话。

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
    # 服务端版本探针 (skillcheck): 这次报的是旧版才带升级提醒, 没报不提醒。不拦调用。
    _attach_skill_update(out, "get_protocol", _user_id, local_version, digest)
    # 宽松地取版本号: 模型可能把整行 "<!-- protocol_version: xxx -->" 或
    # "protocol_version: xxx" 原样传进来。只要里面有那 12 位十六进制就认。
    # 精确比较的后果是每场对话都判成"旧版", 全文再发一遍、还让运营白重装。
    # 与探针 (skillcheck.normalize) 同一个口径: 空串和「无」这类占位当没传, 认不出的按旧版。
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
        # 升级提醒只有一种说法(2026-10-30 复审): skill_update.message 原样进开场, 不另编一句「重新导入一下」。
        out["warning"] = (
            f"本地 skill 是旧版({str(local_version).strip()[:12]}), 服务端是 "
            f"{digest}。**照本返回值里的正文执行**; skill_update.message 原样(三行)放进开场、项目名之后 —— "
            f"重新导入的链接 {skillcheck.UPDATE_URL} 和「{skillcheck.UPDATE_COMMAND}」都在它里面, 不另说别的升级话。")
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
        # hint 是念给写手的: 堆栈已经在上面那行日志里, 这里只说"写稿照常"(2026-10-30 发布收尾)
        return {"error": f"{type(exc).__name__}: {exc}"[:300],
                "hint": "写稿可以继续, 但这次没拿到这部分数据; 已记下, 不用你做什么"}


def _require_caller(user_id: str | None, tool: str, what: str) -> None:
    """个人层 / 规则台账这几个工具认不出调用者时**拒绝**: 抛 PermissionError, 和项目归属拒绝同一个口径
    (core.assert_project_access; MCP 层收成工具错误, REST 映成 403)。

    2026-10-30 发布收尾: 原来这几个回一个带 ``error`` / ``hint`` 的 dict, 别的工具却抛 —— 同一件事两种形状, 带 error
    的 dict 在模型那边像一次成功的调用, 而 hint 里那句配置变量名会被原样念给写手。服务端怎么补(配 key 映射)只进日志。"""
    if user_id:
        return
    logger.warning("%s: 认不出调用者身份, 拒绝 —— 服务端要配 DESKCORE_KEYS(或 DESKCORE_API_KEY + "
                   "DESKCORE_DEFAULT_USER_ID)", tool)
    raise PermissionError(core.IDENTITY_REFUSED.format(what=what))


# ══════════════════════════════════════════════════════════════════════
# 写稿前
# ══════════════════════════════════════════════════════════════════════

def create_project(name: str, brand: str = "",
                   _user_id: str | None = None) -> dict:
    """给一个新品牌 / 新方向开一个项目。**建完立刻用返回的 project_id 调
    open_project 接管。**

    什么时候调: **只在用户明确要为一个新品牌 / 新方向开项目时**(不是在找一个已经在写的项目)。
    **找不到要写的项目不是调它的理由**: ``list_projects`` 里没有、带 include_archived=true 再查一遍也没有, 就告诉用户
    「写作台里找不到这个项目, 请找负责人看一下」, 这个项目先不写; 用户回「那你先建一个」也不建 —— 建出来的是一个空的
    同名项目, 历史稿和规则都在原来那个里, 两边互不查重。整个团队共用一个身份, 分不出说话的是不是负责人, 所以这一条不看是谁说的。

    **调之前先 list_projects(带 include_archived=true)看一眼**, 把同品牌已有的项目名念给用户确认真要新开 ——
    已经有的项目不要重建。返回值里
    ``created`` 是 false 就说明撞名了, 库里没新建, 直接用返回的那个
    project_id, 别改名重试。带 ``archived: true`` 的是撞上了标成暂时不用的同名项目: 照常用它(历史稿和规则都在
    它里面), 用户要写的就是这个品就不用跟用户提它暂时不用。

    返回里带 ``siblings`` 时要停下来问用户: 说明这个品牌名下已经有别的项目
    了(带 ``archived: true`` 的那几个标成了暂时不用)。新项目是**独立的一套历史库**, 跟它们不互相查重 —— 如果本意是
    在已有方向下继续写, 用那个已有项目才对。

    参数:
      name  —— 项目名, 建议带上方向, 例如「途鸽-D8薪资谈判」而不是光「途鸽」。
      brand —— 品牌名, 同品牌的项目靠它归堆。同一个品的项目 brand 要写一致。

    项目建在调用者这个身份名下(整个团队共用这一个身份), 签名里没有归属参数。
    """
    # 故意不包 _safe: 这是【写】操作。建失败若降级成带 error 的"成功", 调用方
    # 会拿着一个不存在的 project_id 往下走, 后面每个工具都 404 而根因看不见。
    with _core_value_errors_as_input():
        return core.create_project(core.sb(), name, brand=brand, user_id=_user_id)


def list_projects(include_archived: bool = False, _user_id: str | None = None) -> dict:
    """列出这个身份名下的项目(整个团队共用一个身份), 以及每个项目手上有多少积累。

    返回 project_id / 名称 / 品牌 / 已沉淀的硬规则与软偏好条数 / 历史成稿指纹数。
    不知道要写哪个项目时先调这个。

    默认只列在用的项目; 标成暂时不用的项目不列, 返回里的 archived_hidden 是没列出来的个数(没有就不带, 有的时候
    archived_hidden_note 一句话说怎么看它们)。
    用户要写的品在清单里找不到、而 archived_hidden 大于 0 时, 先带 include_archived=true 再调一次看看
    (那一份里暂时不用的项目带 archived: true), 找到了就用它; 还是找不到就告诉用户「写作台里找不到这个项目, 请找负责人
    看一下」, 不要 create_project 新建, 也不要问用户要 project_id。

    ⚠️ fingerprint_count 是**查重基线**的条数, 含从 Truth Vault 补录进来的
    笔记副本。ingest_copies 是**补录进来的 item 数**, 独立的一个数, 不是 fingerprint_count 的子集:
    补录半途失败的副本有 item 没指纹, 会算进 ingest_copies 而不在 fingerprint_count 里, 所以两个数
    **别相减**。读法: ingest_copies 大 = 这个项目的"积累"大半是 TV 笔记的影子, 不是写作台写过那么多篇。

    这里看不到的项目就是不在这个身份名下 —— 不要去猜别的 project_id 试, 其它工具会拒绝。

    **用户要写 / 改某个品牌的小红书稿、却没说是哪个项目(只报了品牌名、只贴了 brief、只点了
    human-writing), 先调这个找项目, 再 open_project。** 没说写作台也一样: 写稿就走写作台。
    (写评论带的是笔记链接, 直接 comments_for, 它会按链接认项目。)
    """
    # 2026-10-30 发布收尾: 不再带 desk_gap(「写作台提醒」, 见 core 里 DESK_GAP_* 那段) —— 一个团队共用一个身份,
    # 每个写手每场开头都听到别人项目的漏接推断, 而对不回来的多半是 W-38 之前写的。
    # 归档的项目默认不列(2026-10-30, 见 core.project_archived 上面那段); 藏了几个照实说, 免得模型找不到就去建重。
    def _list() -> dict:
        seen: dict = {}
        out = {"projects": core.list_projects(core.sb(), user_id=_user_id,
                                              include_archived=bool(include_archived), report_sink=seen)}
        if seen.get("archived_hidden"):
            out["archived_hidden"] = seen["archived_hidden"]
            out["archived_hidden_note"] = core.ARCHIVED_HIDDEN_NOTE.format(n=seen["archived_hidden"])
        return out
    return _safe(_list)


def _archive_pending(user_id: str | None, project_id: str) -> dict | None:
    """附加提醒(W-29): 这个身份在这个项目上抽了还没存档的牌, 和下一步该做什么。团队共用一个身份, 数到的是整个团队的牌,
    所以只挂在这一场确实在用牌的回包上(draw_angles、带 angle_key 的 check_drafts; 2026-10-30 复审)。查不到就不带 ——
    不能把发牌 / 查重的结果弄坏(同 open_project 里 edit_digest 那段的纪律)。"""
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
        not_configured / timeout / error 里的哪一种。**是 timeout 时**(卡这次没来得及选完, 还在后台接着选):
        动笔之前调一次 borrow_lessons, 参数和这次 open_project 一样(tactic / draft_topic / key_messages /
        target_audience / tone / extra_instructions 照传, flywheel_arm 传这份简报里的); 只调这一次, 还是没有卡就照常写。
        只借钩子、结构、手法,
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
      · recent_titles —— 【有才出现】本项目最近发出去的标题(published)、最近在写作台写过的标题(drafted),
        有的话还有 working(本项目发出去三天以上、评论多的几条)。照 recent_titles.task 做: 新写的稿子标题别和它们撞
        主干词和具体说法(标题句式照 draw_angles 分到的写, 在同一种句式里换说法), 开头别复述它们, 人设和核心场景尽量
        错开; working 里的可以学它的句式、语气和开头的路数, 别的只用来避开, 不要模仿。
        p0 / stable 定的人设、发牌分到的坐标和切入角度、用户点名的题目和改稿都优先于它。
      · project_now —— 【有才出现】这个项目现在的情况: 近 60 天发了什么(mix)、发出三天以上的里面哪种写法起量多
        (working, 只是计数)、写作台近两周写的稿子发出去了几篇(desk)、同品类有没有外部参考(external)。照
        project_now.task 用: 默认贴着 mix 写(draw_angles 的卡已经按这个比例发了), working 只在两种写法之间拿主意;
        message 原样放进开场(skill_update 之后、edit_digest 之前), 用户问这个项目现在怎么样时也用它回答。
        counts.project_now 是 ok / warming(这次还在准备, 下次打开就有) / unavailable(这次读不到) —— 后两种简报里没有
        project_now, 开场就不放这一句, 照常写, 不用跟用户提。
      · recent_structure —— 【有才出现】本项目最近 window 篇打过分的稿子里, 扎堆写成同一个样子的几处
        (concentrated 每条 {question, answer, share, of}: 哪一处、扎堆的那种写法、占比、占比的分母 —— 这一题有答案的
        篇数, 可能比 window 少)。照 recent_structure.task 做:
        这一批每一处都至少有一半的稿子换一种写法。p0 / stable、发牌分到的坐标和切入角度、用户点名的写法都优先于它;
        只管新写的稿, 改稿不受它约束。
      · delivery_requirements —— 【项目有交付模板才出现】score_drafts 时每篇 draft 要带什么: ``tags`` 是要给几个话题
        (``fixed_tags`` 写作台会加, 不用给), ``comments`` 是评论条数范围和每条要提到的词(null 就是不要评论),
        ``direction`` 为 true 时每篇要带一句切入角度。照 delivery_requirements.task 写, 一次带齐, 免得存完再返工。
      · edit_digest —— 【有才出现; ack 之后一周内不再出现, 没 ack 就每次打开项目都带】: 本项目最近发出去的稿子
        在发布前被人改了什么(写作台那版 → 发出去那版)。**它不挡写稿**: edit_digest.message 原样放进开场(复述项目名
        之后), 照常写完、交付这一批; 交付那条回复的末尾把照 edit_digest.task 归纳出的改法(最多 3 条)列给用户, 发出那条
        回复之前调 ack_edit_digest(rules_recorded=0); 用户之后要记哪几条, record_rule 记下再 ack 一次(见 ack_edit_digest)。
      · flywheel_arm —— 这场写作落在哪个对照组。存档时【原样】传给 score_drafts 的 flywheel_arm,
        别改、别跟用户提。
      · archived —— 【有才出现】这个项目标成了暂时不用。简报照常给全。用户点名要写这个项目或这个品牌的就照常写,
        不用跟用户提它暂时不用; 是你自己挑的这个项目(用户没点名), 照 archived_note 先跟用户确认一句这次要写的就是它。

    传入本次的 tactic / draft_topic / key_messages 会让正案例按【相关性】挑选
    (排不出相关性时按天轮换, 不会每次都是最近那几条 —— 那会让文风越写越窄)。所以知道要写什么就传。
    经验卡也是按这几个字段匹配的, 不传就只按项目定位借。传了 tactic 的, 存档时 score_drafts 也原样传同一个 tactic。

    ⚠️ **counts.hard_rules 是 0 不一定是错的。** 很多项目的约束确实都记在 soft 里
    (老工作台收反馈时不分"这一次"和"以后都这样", 一律存成 soft; 2026-09-20 全库
    465 条规则里 hard 只有 53 条)。传错 project_id 的真实表现是报 project not found(照报错里说的重新
    list_projects 抄 id), 或者 soft_rules 和 soft_rules_pool 【同时】为 0。只有后者才值得问一句。

    counts.angle_debt 大于 0 时, 返回里还会有 angle_debt / angle_debt_note: 这个项目近期发牌分到、
    还没有对应稿子的坐标, angle_debt.angles 里是具体是哪几个(一小时以内刚发的不在里面 —— 一个团队共用一个身份,
    那多半是队友正在写的牌)。这是给你挑坐标用的: 用户这次要写的对得上
    (比如要这个项目的一批稿、没点具体题目), 就优先按这些坐标写, 查重时带上各自的 angle_key, 不够再调
    draw_angles 补齐; 用户点了具体题目或角度就照用户的写。不用把这个数转告用户, 也不用问用户怎么处理。

    这个工具出错会直接报错, 不会返回半份简报。报错就【停下来】, 不要凭记忆
    或常识补一份约束继续写 —— 这个项目的硬约束是什么, 只有库里那份算数。

    ``skill_version``: **每次都传** bywood-writing-desk 这个 skill 里 ``protocol_version:`` 那一行的
    12 位值(传整行也认)。skill 里没有这一行就传空。返回里有 ``skill_update`` 时, 说明你这次传的版本不是最新版:
    把 ``skill_update.message`` **原样**(三行都要, 链接和「更新技能」那行一字不改)
    放进开场、复述项目名之后(同一条只放一次), 然后照常继续写 —— 不拦写稿, 只是提醒升级。没有 ``skill_update`` 就不用
    提版本的事。
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
    # 2026-10-30 发布收尾: 不再带 desk_gap(同 list_projects)。
    # 服务端版本探针 (skillcheck): 只看这次传的 skill_version —— 一个团队共用一个身份, 别人的旧版、重启后没核到都不算到这一场头上。
    return _attach_skill_update(out, "open_project", _user_id, skill_version)


def draw_angles(project_id: str, n: int, avoid_days: int = 30,
                perpetual_bias: bool = False, pin: dict | None = None,
                repeat_of: list[str] | None = None, layer: str | dict | None = None,
                _user_id: str | None = None) -> dict:
    """发牌: 给本批 n 篇稿子各分配一组互不相同的创作坐标。

    批量写稿前必须先调这个, 然后【每一篇按分到的那组坐标写, 不得互换编号】。
    牌是照这个项目平时发的样子发的: 内容形式按项目的流量 / 产品配比, 标题句式、情绪杠杆、人性原型跟着项目已发笔记
    的比例和评论多的写法走, 所以同一批里几篇同一种内容形式是正常的(那就是项目的配比), 不用自己再去错开体裁。
    同一组坐标一个月内不会再发; 同一批里不会有两篇是同一种「内容形式 + 标题句式 + 人性原型」。

    每组坐标含: 内容形式 / 标题句式 / 情绪杠杆 / 人性原型 / 切入角度, 每一样后面跟一句大白话, 外加情绪浓度、词感,
    以及易混杠杆的判据(比如「焦虑撬动」和「恐惧撬动」怎么分 —— 前者是模糊的未来担心, 后者要有具体已发生的威胁)。
    有的卡多一行「参考结构」: 照本项目或同品类最近评论多的一篇的结构写, 只借结构, 不抄说法、不提参考帖里的品牌
    (卡上分到产品向的内容形式时, 本项目的品牌照常写)。

    这些组合会避开本项目最近 avoid_days 天(14–90, 默认 30)已经用过的 —— 这是跨批次不重复的根本保证。光靠提示词让
    模型「注意不要重复」做不到: 模型是无状态的, 它不知道自己上个月写过什么。n 一次最多 30。

    perpetual_bias=True 要求「不依赖任何时效元素」, 写出来更能穿越周期(但会少掉当下感)。默认 False。

    返回里的 prompt_block 可以直接贴进生成提示词。它第二行是 project_status(这个项目现在的配比、这批照什么发);
    每一篇那行开头的方括号里是这一篇的 ``angle_key``(「第1篇 [angle_key=…]」, 和 ``angles[].angle_key`` 是同一个,
    20 位十六进制串): 这一篇调 check_drafts 和 score_drafts 时, 把方括号里那串**原样复制**进这一篇的 ``angle_key``,
    不要自己编号、缩写或改写, 也不写进标题和正文。

    交付这一批的那条回复里, 把 ``batch_note`` 原样说一次(不放进开场, 只说一次)。它只数这次新抽的牌: 这次交付里还有
    angle_debt 里补写的稿时, 也照原样说, 不要改成整批的篇数。用户问为什么这批是这样配的, 照 ``project_status`` 回答。``delivered`` 少于 ``requested`` 时照 ``note`` 跟用户说, 别硬凑。

    ``pin`` / ``repeat_of`` **只在用户明确要求时用**, 平时不传:
      · pin —— 用户点名「这批都写 X」(固定某一维), 写成 {维度: 值}, 比如 {"标题句式": "疑问句"} 或
        {"内容形式": "提问求助"}; 维度是 内容形式 / 标题句式 / 情绪杠杆 / 人性原型。值不在可选里会报错、什么都不发,
        报错里列着可选值和项目现在的内容形式占比 —— 照它跟用户确认, 别自己挑一个近似的。
      · repeat_of —— 用户点名「这个角度再写一次」, 传那一篇的 angle_key(至多 3 个、不多于这次要的篇数 n, 必须是这个项目发过的, 原样抄)。
        这几张原样再发一次(角度照旧、内容要新, 查重照常), 其余的照常发。原来那张卡还没用过(还没有稿子带着它存档)
        的不用点名, 直接照原来那张卡写; 点了会报错、什么都不发。

    ``layer``(贴型)**只在用户自己说了是哪种贴时传**, 平时不传, 也不要为它去问用户:
      · 用户说「这批都是产品贴」「写 5 篇流量贴」→ layer="产品贴" / "流量贴"(这一批每篇都是它);
      · 用户说「写 3 篇产品贴、2 篇流量贴」→ layer={"产品贴": 3, "流量贴": 2}(篇数加起来不超过 n, 剩下的不标)。
      可选: 产品贴(也认 产品向 / 直出贴 / 直给 / 好物分享-产品贴)、流量贴(流量向)、提问贴(提问)、精品贴(精品)。写错了会报
      layer_invalid、什么都不发, 照报错里列的可选值改了再发。标了的卡上多一行「这篇是X贴：…」, 照那一行写; 项目里标了
      【只管…的稿】的规则, 只管卡上写着那种贴的稿。
    """
    # 故意不包 _safe: 发牌要写台账。写失败却报成功, 同一个角度下批还能再抽,
    # 跨批次唯一性的承诺就破了(而且没人看得见)。
    # n / avoid_days 的夹子在 core 里(1–30 / 14–90): CLI 和 REST 走同一个函数。
    out = core.draw_angles(core.sb(), project_id, n,
                           avoid_days=avoid_days, user_id=_user_id,
                           perpetual_bias=perpetual_bias, pin=pin, repeat_of=repeat_of, layer=layer)
    if out.get("error"):
        return out                         # pin / repeat_of / layer 不对: 什么都没发, 也就没有要提醒存档的
    # W-29: 发牌成功之后才查, 所以张数含这一批; 查不到就不带, 不影响发牌。
    return _with_archive_pending(out, _user_id, project_id)


def borrow_lessons(project_id: str, tactic: str = "", draft_topic: str = "",
                   key_messages: str = "", target_audience: str = "",
                   tone: str = "", flywheel_arm: str = "", extra_instructions: str = "",
                   _user_id: str | None = None) -> dict:
    """向帆谷飞轮图书馆【再】借几张真实爆款的经验卡。

    open_project 已经随简报借过一次(返回里的 lessons)。只在换了选题 / 战术、
    想按另一个方向再匹配一批时才调这个; 同样的参数再调一次拿到的是同一批卡。
    **例外: open_project 返回的 lessons_status.status 是 timeout 时**, 卡还在后台接着选 —— 动笔之前用和那次
    open_project 一样的参数(tactic / draft_topic / key_messages / target_audience / tone / extra_instructions 照传,
    flywheel_arm 传简报里的)调一次, 选好了就能拿到; 只调这一次, 还是没有卡就照常写。

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
    # extra_instructions 也要能传(2026-10-30 发布收尾): 它在馆员的缓存键里, open_project 带了它而这里传不了的话,
    # "同样的参数"永远对不上那次在后台选好的卡, 又从冷路径选一遍。
    return _safe(core.borrow_lessons, core.sb(), project_id, user_id=_user_id,
                 flywheel_arm=flywheel_arm or None,
                 tactic=tactic, draft_topic=draft_topic,
                 key_messages=key_messages, target_audience=target_audience,
                 tone=tone, extra_instructions=extra_instructions)


# ══════════════════════════════════════════════════════════════════════
# 写稿后
# ══════════════════════════════════════════════════════════════════════

# 11-02 owner 实测(西屋911gt, 16:14 北京): 开项目 → 发 5 张牌 → check_drafts 5 篇全过, 之后 score_drafts 一次都没调, 模型却跟
# 用户说「已存档并打分」, 报了 88 / 92 分和几条建议 —— 全是编的(库里 0 批、0 条 batch_metrics), 稿子也没先给用户看。和 10-28
# 途鸽那轮(W-29 / W-38)同一个形状: 查完重就当交付了。docstring 里写了「交给用户之前调 score_drafts」, 但模型读的是返回值,
# 所以把下一步写进返回值本身: 这几篇还没存、还没有分数和合规结论, 那些只在 score_drafts 的返回里。只改服务端说的话, 协议不动。
CHECK_NEXT_STEP = ("这 {n} 篇还没存档, 也还没有参考分和合规结论 —— 那些只在 score_drafts 的返回里。下一步: 把要交付的这几篇"
                   "原样(同样的标题、正文、angle_key, 改稿的带同样的 replaces_version_id)交给 score_drafts, 再把它返回的交付稿"
                   "(有 delivery 就原样给 delivery.text)交给用户。{warn}调 score_drafts 之前, 不要跟用户说已经存了或打过分, "
                   "不要报任何分数和合规结论, 也不要只给摘要不给全文。")
# warn 照协议第 4 条: 第一次查到的先换个切入重写一次再查, 还是 warn 才照常交(复审: 不分开说的话, 照返回值办事的模型会把头一回
# warn 的稿子原样存掉, 跳过那次重写)。服务端分不出这是第几次查, 所以两种都写上。
CHECK_NEXT_STEP_WARN = ("其中 {m} 篇是 warn: 这一轮第一次查到 warn 的, 先换个切入重写一次再查; 重查还是 warn 的, "
                        "照常和其余的一起交给 score_drafts。")


def _add_check_next_step(out: dict) -> None:
    """查重有过了的(pass / warn)就在返回里写明下一步是 score_drafts; 全是 reject 或没有稿子时不加。warn 的另起一句,
    照协议先重写一次再查。"""
    s = out.get("summary") if isinstance(out, dict) else None
    if not isinstance(s, dict):
        return
    warn = int(s.get("warn") or 0)
    n = int(s.get("pass") or 0) + warn
    if n > 0:
        out["next_step"] = CHECK_NEXT_STEP.format(n=n, warn=CHECK_NEXT_STEP_WARN.format(m=warn) if warn else "")


def check_drafts(project_id: str, drafts: list[dict],
                 _user_id: str | None = None) -> dict:
    """查重硬闸。成稿后【必须】调这个才能交付。

    drafts 传 [{"title": "...", "body": "...", "angle_key": "..."}, ...]
    (angle_key 是 draw_angles 给的: prompt_block 里这一篇那行方括号里的那串, 原样复制, 有就带上 —— 查重时写作台会顺手
    记下这一稿和它的 angle_key; 稿子发出去以后, 写作台按内容从飞书把它对回来、给这张牌销账, 就算这一稿没存过档)。

    **改的是已经存过的那篇**(score_drafts 返回过它的 version_id): 那篇带上 ``replaces_version_id``, 值和之后调 score_drafts
    时带的一样(上次返回的 ``version_ids`` 里对应的那个)。查重就不会拿它和它自己的旧版比, 别的稿子照常比; 这一行的返回
    带回 ``replaces_version_id``。不带的话, 改过的稿子会被它自己的旧版拦下(``collided_scope`` 是 ``历史``、``collided_with``
    就是用户要改的那篇): 那不是撞车, 不要重写, 补上 ``replaces_version_id`` 再查; 这场对话里拿不到它的 version_id(比如
    改的是别的对话里交付的稿子), 就照改好的交, 不用存 —— 发出去以后写作台按内容把它对回原来那篇。id 对不上(不是本项目的
    版本、格式不对、两篇替换同一篇)的那篇判 reject、``gate="replace_lookup"``: 稿子不用重写, 改对 id 再查一次。整篇推翻、
    换角度重写的是新稿, 不带这个字段。

    如果返回的 summary 里有 empty_history_warning, 说明查重库里这个项目还没有历史稿,
    本次只在本批内部比对 —— 别当作"和老稿子比过了没撞车", 把那一句照原样告诉用户就行;
    用户不用做什么。

    比对本项目【全部】历史成稿 + 本批内互比, 四个信号:
      · 标题语义相似度
      · 正文开头是否精确撞车(标题换了也能抓)
      · 正文四字串重合度(抓换皮的模板化写法)
      · 正文四字串【包含度】(抓短稿整段照搬长稿 —— 那种情况下上一条会被
        长度差稀释, 看起来像不重复)

    summary.containment_checked 为 false 时说明最后那一路本次没生效(多半是草稿太短、
    与历史稿长度差太大), summary.containment_skipped_warning 里有一句现成的话 ——
    照原样告诉用户, 整段照搬一篇长稿的重复这次可能漏掉。

    每条返回 pass / warn / reject:
      · reject —— 必须重写。不允许「少出一条」糊弄过去。reason 里说了是哪个
        信号命中: 撞开头就换开场视角, 撞正文就换比喻系统或叙事路径。
        只改标题没用。重写完再跑一次。用户看过撞的是哪一条还是坚持要用这篇: 原样交给他, 用一句话告诉他
        这篇不会存进写作台的历史稿(分给它的牌也没销账 —— 那是你这边的事, 不用跟用户提);
        这篇不用再传给 score_drafts(传了也存不进去)。``gate`` 是 ``replace_lookup`` 的不是撞车, 见上面 replaces_version_id 那段。
      · warn —— 可疑: 自己换个切入重写一次再查; 还是 warn 就照常交、照常 score_drafts, 不用问用户。
      · pass —— 可以交付: 交给用户之前调 score_drafts(存档 + 参考分; 项目有交付模板时它回拼好的交付稿)。

    summary.semantic_degraded 为 true 时说明标题语义这一路这次没跑全(另外三路照常),
    把 summary.degraded_note 那一句照原样告诉用户就行: 交付照常, 用户不用做什么,
    也不用重试。

    这个工具出错会直接报错而不是放行 —— 查重挂了必须停下来, 不能当作通过。
    """
    # 故意不包 _safe: 查重是硬闸, 出错必须冒泡。正文 / 标题放错了字段的先对上(_draft_fields), 不然这一篇只按标题查。
    drafts = _draft_fields(drafts, "check_drafts", project_id)
    out = core.check_drafts(core.sb(), project_id, drafts, user_id=_user_id, record_snapshot=True)
    _add_check_next_step(out)
    # W-29: 判定之后才查; 查不到就不带, 判定结果原样回去。2026-10-30 复审: 只在这批稿子带了 angle_key 时查 —— 团队共用一个
    # 身份, 没带牌的查重(改一篇别的对话交付的稿、用户点题的单篇)查到的全是队友手上的牌, 那句「都存完再给用户看」只会添乱。
    if isinstance(drafts, list) and any(isinstance(d, dict) and str(d.get("angle_key") or "").strip() for d in drafts):
        return _with_archive_pending(out, _user_id, project_id)
    return out


def ack_edit_digest(project_id: str, through: str, rules_recorded: int = 0,
                    _user_id: str | None = None) -> dict:
    """记下 open_project 给的 edit_digest 已经说给用户了, 一周内不再出现。

    ``through`` 原样传回 edit_digest.through(第二次 ack 也可以抄第一次返回的 ``through``), 不要猜、不要自己拼: 不是摘要
    给出去的那个时间会报 400。到交付时要是摘要已经不在眼前了, 就用开场那次一样的参数再调一次 open_project 拿回来(没 ack 之前
    每次打开都带)。``rules_recorded`` 是一共按摘要记了几条规则。
    什么时候调(和协议第 1 条同一个时机): edit_digest.message 已经放进了开场; 这一批交付完, 要在交付那条回复的末尾
    把归纳出的改法(最多 3 条; 没看出规律就说没看出)列给用户 —— **发出这条回复之前**调一次, rules_recorded 传 0, 不等
    用户回话(写手多半拿了稿子就走, 等他回话就永远不 ack, 下一场写作还会再给一遍同一份摘要)。这场不写稿的, 在办完用户
    这次的事、说出改法的那条回复之前调。之后用户再要记哪条, record_rule 照记, 再调一次 ack(through 不变, rules_recorded
    填一共记了几条)。
    edit_digest.message 和归纳出的改法都不在这一轮的回复里就别调: 调了这批改法就再也不会出现。
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
    只贴了正文 + ``project_id``(用户从飞书整格复制的正文就行, 带不带标题行、话题都可以)时, 服务端也按正文在这个项目的
    TV 笔记里找一次: 对上了 ``source`` 是 ``tv_by_text``, 已有评论照样带回来; 对不上 ``source`` 是 ``pasted``、
    ``existing_comments`` 是空的, 照常写, 不用再问用户要链接。

    返回:
      · post —— 要接的帖子(标题 + 正文)
      · source —— ``tv``(按链接找到)/ ``tv_by_text``(按贴来的正文对上, 带一句 ``tv_by_text_note``; 用户说不是这一篇
        就别用它的 ``existing_comments``, 照贴来的正文写)/ ``pasted``
      · existing_comments —— 帖子下已有的评论(TV 里找到了才有): 别重复里面已经问过的事、用过的说法。项目配了飞书那一列时,
        这条帖子那一格里已有的维护评论也在里面(``source`` 是列名)
      · comment_rules —— 本项目管评论的规则(「评论区：」开头的), 必须守
      · hard_rules —— 本项目的硬约束(合规话术、禁词), 评论一样要守
      · style —— 所有项目的评论都管的写法底线: 不用句号、不用方言语气词(伐、侬、阿拉)、不写 v / vx / 加微这类像要联系方式的字、
        不写「帖子里」(帖里、帖子中也一样, 接帖子就直接说那件事)、不用「X那栏」「X那次」「X那阵」这类含糊指代(直接说是哪件事)。
        项目规则里说要混进这些也不行, 以这几句为准。管整组的: 以品牌评价收尾(最后一两小句落在品牌上)的最多四分之一
        (20 条最多 5 条), 其余以事实、感受、细节或提问收尾, 收尾别都用同一个句式, 品牌放在前面的句子里自然提不受限;
        同一个说法(日期、活动名、合同条款这类)整组只用一次, 开头别几条一样。还有一句只是提示: 长评论也多用短句
      · comment_set —— 只有项目定了一组评论的配比才有: ``lines`` 是按项目的数写好的几句(提品牌的最多几成、至少几成中性、
        至少几成一句话短评), 和 style 一样要守, 项目规则里说法不一样的以它为准; ``column`` 是这组评论要贴进的飞书那一列
      · slots —— 默认四个评论位: q1 / q2 提问、exp 经验或推荐、reply 贴主回复(回 q1)。每位写一条, 位的要求在 note 里;
        每位都可自然点名品牌, 不要求每条点名。可写感受、比较、推荐、答疑, 亲历和旁观推荐也可用;
        具体经历、效果和数字依提供素材, 不编造事实; 没有素材时写提问或一般讨论。用户另有要求(比如只要两条提问)就照用户的, 位的 id 照用
      · project_id —— 下一步 score_comments 要带的
    写完整组交给 ``score_comments``。评论不进 check_drafts / score_drafts, 也不存库。

    ``skill_version``: 同 open_project, 每次都传 skill 里 ``protocol_version:`` 那一行的值; 返回里有 ``skill_update`` 时把
    ``skill_update.message`` 原样放在开场(同一条只放一次)。
    帖子属于别人的项目时报 403, 不回帖子内容。这种报错(403、笔记对不上项目、不属于当前调用者、无法识别调用者身份)
    不重试, 照报错里给的下一步做: 问写的是哪个项目、请用户把帖子正文贴过来, 或者请用户找负责人看一下。
    """
    out = core.comments_for(core.sb(), user_id=_user_id, link=link, project_id=project_id,
                            post_title=post_title, post_body=post_body)
    return _attach_skill_update(out, "comments_for", _user_id, skill_version)


def score_comments(comments: list[dict], link: str = "", project_id: str = "", post_title: str = "",
                   post_body: str = "", kind: str = "product", max_named: int | None = None,
                   earlier_comments: list[str] | None = None, _user_id: str | None = None) -> dict:
    """一组评论写完后调(自动调, 不用等用户说): Jev 整组判一次, 回每条过没过和修改建议。只判, 不存库, 不发布。

    ``comments`` 传 ``[{"slot": "q1", "text": "..."}, {"slot": "reply", "text": "..."}, ...]``: slot 写 comments_for 给的
    评论位 id(自定义的评论位传整个对象); 同一个 id 只能有一条; 贴主答疑那条会自动接上它回的那一位。帖子参数和
    comments_for 那次一样: ``link``, 或 ``post_title`` / ``post_body`` + ``project_id``。一次最多 20 条。
    ``kind``: product(默认, 卖货的品)/ agency(机构服务)/ shop(门店)。``max_named``: 默认 None, 不设固定品牌点名配额;
    品牌可自然提及, 不要求每条点名。用户明确要限制时才传整数(整组最多几条点名品牌, 0 表示不点名)。项目定了提品牌的上限
    (comments_for 的 comment_set)时由代码数, max_named 不用传, 传了也不转给判定服务(回包里 ``max_named_note`` 说一句)。
    ``earlier_comments``: 一批超过 20 条要分几次判时, 后面几次把前面几次判过的评论文字放进来(最多 60 条): 只拿来查重复的
    说法和开头, 不送去判。

    怎么读返回值:
      · 每条 ``passed=true`` 且 ``needs_review=false`` 才算这条过。``summary.passed`` 是这组过了几条 —— 这就是整组的分数,
        照实告诉用户。
      · ``fix`` 是必须改的(主要行为、点名品牌、接住帖子、细节四项); ``hints`` 只是参考, 不改不算没过, 别为了消掉提示反复重写。
      · ``style_fix`` 是代码查出来的写法底线(句号、方言语气词、像要联系方式的 v / vx / 加微、「帖子里」、「X那次」「X那栏」这类
        含糊指代; 整组里和前面重复的说法、第 3 条起一样的开头; 以品牌评价收尾的超过四分之一时超出的那几条; 项目定了提品牌的
        上限时超出的那几条, 都按顺序留前面的), 有它那条就算没过, 和 fix 一样照着改、不用问用户。判定没判成(``judge_status``
        不是 ok)时它在 ``style_issues`` 里, 照样先改掉。
      · ``thread_fix`` 是整组必须改的, 按「位 xx」「第 k 条」改那一条。代码数出来的整组那一句在**第一句**, 以「整组代码查」开头:
        提品牌超了几条、中性的或一句话短评不够几条(项目定了配比时)、重复的说法、品牌收尾超了几条。中性、短评不够是整组没过
        (``passed=false``), 照那一句点名的几条改。其余几句是判定服务的(如违反用户显式设置的品牌点名配额)。
        ``thread_hints`` 是整组参考(如同句式、摩擦等), 不用为消提示重写。
      · 没过的按 fix 改**一轮**, 再调一次本工具(整组全部送, 顺序和第一次一样, 不要只送改过的那几条: 品牌收尾、提品牌、
        中性、短评的条数都按整组算), 然后不管结果都交付并如实说明; 不要循环。交付时每条评论写全文, 不缩写、不省略。
      · ``delivery_column``(项目配了飞书那一列才有): ``text`` 是这组评论按那一格的样子拼好的(一行一条, 不编号、不加标签),
        交给用户时原样整段给, 说清贴进这条帖子那一行的 ``column`` 那一格: 格子里已经有内容就接在最后另起一行贴, 不要覆盖;
        列名不贴进格子。贴几行就算发了几条, 只交真要发的那几条。改过再判时用最后一次回的那一份; 一批分几次判的,
        每一段用它最后一次判回的 text, 按判的顺序接起来交(段和段之间不加空行)。
      · 亲历背书、旁观推荐不因表达类型本身转人工。``needs_review=true`` 时按具体疑点交人工核实: 那几条的全文和疑点在
        ``review_list`` 里(``label`` / ``text`` / ``why``, 每条的行里也带 ``text`` 和 ``review_why``)。跟用户说时**逐条照 text
        原样写全文**, 不缩写、不省略、不用「…」代替, 后面跟上 why 里的疑点。具体经历、效果和数字依提供素材, 不编造事实,
        也不要靠反复润色把疑点抹掉。
      · ``judge_status`` 不是 ok 就说这次没判成, 不能说通过。
    """
    try:
        return core.score_comments(core.sb(), user_id=_user_id, comments=comments, link=link,
                                   project_id=project_id, post_title=post_title, post_body=post_body,
                                   kind=kind, max_named=max_named, earlier_comments=earlier_comments)
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
      · ``fixes`` 是最该看的几条: ``sure=true`` 是 Jev 有把握的, ``sure=false`` 是拿不准的(也照改, 见下)。``more_fixes``
        是没列出的条数。``instruction`` 只说哪一句犯了哪条, 不给改法。
      · **分数不用逐篇报给用户。** 这一批每篇都打成了分、而且都没有 ``fixes``、``hard_fails``、``unjudged`` 时, 整批说一句
        「合规检查都过了」就行。只有 ``unjudged`` 的那几篇是有几道硬约束这次没判出来: 没有可改的地方, 但
        **不能说都过了**, 跟用户提一句就行, 不影响交付, 不用重试。``score`` / ``fixes`` 照旧都在返回里, 用户问起分数再说。
      · **有 ``fixes`` 或 ``hard_fails`` 的那几篇是查出了合规问题, 不用等用户说, 自己改**(``next_step`` 会点出是哪几个
        index): 照 fixes 改一轮(sure=false 拿不准的也改; 没有 fixes 的照 hard_fails 里没过的那一条改), 这次要调 check_rules
        的先调完、和它的「必须改」放在同一轮改; 那几篇带 ``replaces_version_id`` 先过 check_drafts, 再调一次本工具(会重新
        打分), 交给用户的是改好的那一版, 交付那条回复里一句话说清改了哪篇的哪里。改过一轮还查出来的照实说一句, 不再接着改,
        不要为了分数反复重写。
      · ``judge_status`` 不是 ok(超时、没配、被出境口径挡下)就说这篇这次没打成分, 不能说过了, 也不要重试, 存档照样成功了。
      · 带 ``layer_hint`` 的那篇: 卡上标的贴型和判出来的产品角色对不上(产品贴里产品没出场、流量贴里产品成了主角), 照那一句
        跟用户提一下; 只是参考, 不扣分, 用户觉得没问题就不用改。

    **改稿用 ``replaces_version_id`` 替换, 不要当新稿再存一次。** 值是上次返回的 ``version_ids`` 里对应的那个。存档
    会先摘掉旧版的指纹再查重, 所以不会撞上它自己; 成功后旧稿原地升成新版本, ``replaced`` 里列出来, ``version_ids``
    里是新 id。当新稿再存一次的话, 会被自己的旧版拦下, 而旧的那条永远占着角度。整篇推翻换角度重写的才当新稿。
    改稿和新稿一样**先过 check_drafts** 再存: 那篇在 check_drafts 里也带上同一个 ``replaces_version_id``。

    ⚠️ 存档时【会再查一次重】, 而且自带和 check_drafts 同一套闸: 判 reject 的不存档, 在 ``rejected`` 里列出来
    (``gate="pre_commit"`` 是闸前拦下, ``gate="atomic_recheck"`` 是写入时撞上了队友刚存的稿子)。那几条没有存档, 要重写后
    重新走 check_drafts; 用户看过撞的是哪一条还是坚持要用, 就原样交给他, 用一句话告诉他这篇不会存进写作台的历史稿
    (``note`` 里也是这句; 分给它的牌没销账是你这边的事, 不用跟用户提)。这两种之外的不是撞车, 稿子不用重写:
    ``gate="replace_lookup"`` 是 ``replaces_version_id`` 对不上, 改对 id 再调一次; ``gate="empty_body"`` 是这篇的
    ``body`` 是空的(正文放错了字段), 把正文放进 ``body`` 原样再调一次本工具; ``gate="version_lookup"`` 是稿子上带了本项目里
    查不到的 ``version_id``, 新稿去掉它、改稿换成 ``replaces_version_id``, 再调一次。``gate_summary`` 里该告诉用户的(semantic_degraded / empty_history_warning)
    照 check_drafts 的规矩说。

    **每条都带 ``angle_key``**(draw_angles 分给它的那组)。``unattributed`` 非零就是有几条没带 —— 那几个角度没销账,
    下一批还会被抽到。这时返回里还会带 ``angle_debt``: 这个项目还没有对应稿子的坐标(一小时以内刚发的、查重时带过 key 的
    不在里面), 用户接下来要写的对得上就优先按它们写, 查重时带上它的 angle_key; 用户点了具体题目就照用户的写。
    ``angle_key`` 要**原样复制** draw_angles 给的那个(prompt_block 里这一篇那行方括号里的 20 位十六进制串), 不要自己编号、
    缩写或改写: ``unknown_angle_keys`` 非零是有几篇带的 key 这个项目的台账里没有, ``used_angle_keys`` 非零是有几篇带的 key
    已经销给了别的稿子(一个 key 只给一篇),
    哪几篇在 ``angle_key_mismatch`` 里(``problem``: unknown 编的或抄错的 / other_project 你在别的项目抽的牌, 先确认是不是
    存错了项目 / not_in_ledger 台账没写进去, 不是你的错 / used), ``angle_key_warning`` 说怎么办 —— 那几篇已经存档, 不用重存,
    但角度没销账; 接下来存的照 draw_angles 的原样带。

    **话题、评论、方向是每篇 draft 自己的字段, 不要塞进正文**(都可选, 项目要什么看简报的 ``delivery_requirements``):
    ``drafts=[{"title": ..., "body": ..., "angle_key": ..., "tags": [...], "comments": [{"role": "reader" | "author",
    "text": "..."}], "direction": "..."}]``。``tags`` 是话题列表(带不带 # 都行); ``comments`` 是这篇笔记自己带的评论;
    ``direction`` 是这篇的切入角度, 一句话(如「应届生签约前怎么查公司」), **不是** 产品向 / 流量向。它们不是本工具的
    顶层参数, 放在 drafts 外面会被丢掉。``body`` 只放正文, 不放【标题】【正文】标记、不放话题行 —— 交付模板不管话题的项目
    例外(简报的 ``delivery_requirements.task`` 会说): 那种模板不读 ``tags``, 话题照项目规则写在正文末尾那一行。

    **交付稿**: 项目配了交付模板时返回里有 ``delivery``: ``text`` 是按模板拼好的整批交付稿(表格或一篇一段, 标记、话题
    顺序、固定话题都由服务端加), ``problems`` 是代码查出来的版式问题(``index`` 对应你这次传的第几篇, ``what`` 说怎么改)。
    存新稿时它拼的是这次存进去的几篇; **改稿重存(带 ``replaces_version_id``)时它拼的是原来那一整批**: 没改的几篇照它们
    现在的样子、改过的换在原位, 顺序和上次一样(``delivery.scope`` 是 ``whole_batch``; 改的稿子分属几批的, 一批一段依次排,
    同一次另有新稿的排在最后)。有 problems 就先改那几篇(带 ``replaces_version_id`` 先过 check_drafts、再调一次本工具,
    新返回的 ``delivery.text`` 就是改好之后的整批)。**不管哪种, 都把 ``delivery.text`` 原样整段交给用户** ——
    不要自己拼接、替换其中几行或几段, 不要重排, 不要再加标记(用户坚持要用、被拒没存进去的那篇不在里面: 放在 ``delivery.text`` 后面单独给)。``delivery.scope`` 是 ``this_call`` 时(原来那一批这次没能一起拼出来, ``scope_fallback`` 是
    原因)它只有这次存进去的几篇: 照样原样交, 跟用户说明这是改过的那几篇。没有 ``delivery`` 就照项目规则自己排。

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
    # 正文 / 标题放错了字段的先对上(_draft_fields): 存进 versions 的正文就是对上的那份, 之后 check_rules 查的也是它。
    drafts = _draft_fields(drafts, "score_drafts", project_id)
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
        照着改(参考分查出合规问题的那几篇也在这一轮一起改), 改完那几篇带 ``replaces_version_id`` 先过 check_drafts、
        再用 score_drafts 存一次, 交付时一句话说清改了哪里; 改一轮就够, 不用再调本工具;
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
      · 带 ``layer`` 的那篇: 发牌卡上标了贴型(产品贴 / 流量贴 / 提问贴 / 精品贴), 只标了流量向 / 产品向的规则按它查 ——
        不管这种贴的那几条这篇不查, 不算没守住。
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
    # 同 score_drafts: 写操作, 不包 _safe; 放错字段的一样先对上。
    drafts = _draft_fields(drafts, "commit_drafts", project_id)
    return core.commit_drafts(core.sb(), project_id, drafts, user_id=_user_id,
                              flywheel_arm=flywheel_arm or None, tactic=tactic or "")


INGEST_MAX_PER_CALL = 50

# 同一个项目的补录互斥在 core.ingest_published 里做(进程内锁 + migrations/009 的
# 库锁): 这里和 CLI 的 tv-sync 是两个进程, 锁必须在共用的那一层。
class InvalidInput(ValueError):
    """调用方参数不对, 不是服务端坏了。REST 层映成 400 —— 500 会让模型拿同一个
    超大载荷一直重试, 400 才说得清是"改参数"。"""


@contextlib.contextmanager
def _core_value_errors_as_input():
    """core 里给调用方看的参数提示(``raise ValueError("mute 的天数至少是 1")`` 这类)收成 InvalidInput。

    app 层只把 InvalidInput 等几类拒绝的消息原样回给调用方, 别的异常只回类型和 ref(外部评审 11-04 X1a)。不收的话这几句
    提示到了 MCP 那边就只剩 "ValueError (ref …)", 模型看不出该改哪个参数, 日志里还多一条当成故障的 ERROR; REST 那一路也该是
    400(改参数), 不是 500。ProjectNotFound / ProjectIsolationRejected 也是 ValueError 的子类, 原样放走, 免得丢了 404。"""
    try:
        yield
    except (core.ProjectNotFound, core.ProjectIsolationRejected, InvalidInput):
        raise
    except ValueError as exc:
        raise InvalidInput(str(exc)) from exc


# 头一个是本名, 后面是别名。ingest_published 按顺序取第一个有字的; check_drafts / score_drafts 只在本名空着、别名里恰好
# 一个有字时才拿别名(_draft_fields)。content / text 是模型把正文放错字段时最常用的英文键(W-30 记下的字段名里就有 content)。
_TITLE_KEYS = ("title", "标题")
_BODY_KEYS = ("body", "正文", "内容", "content", "text")
_LINEAGE_KEYS = ("version_id", "_source_autowriter_version_id")


def _first(d: dict, keys: tuple) -> str:
    for k in keys:
        v = d.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _draft_fields(drafts: Any, tool: str, project_id: str) -> Any:
    """check_drafts / score_drafts 收到的每篇稿子: ``body`` / ``title`` 空着(没有、None、全是空白), 而别名键
    (_BODY_KEYS / _TITLE_KEYS 里本名之后那几个)里**恰好只有一个**是有字的字符串 —— 就拿它当 body / title。

    core 只读 ``draft["body"]`` / ``draft["title"]``。模型把正文放进了别的键, 稿子就成了只有标题的一篇: 10-21 / 10-22
    15 篇照样存了档(查重看不见、发出去对不回来, W-30 之后改成拒), 10-26 一批 20 篇全被拒(W-31)。那是模型放错了字段,
    写手什么也没做错; 意思明确的在这一层替它对上。
      · 本名有字的**绝不覆盖**; 两个以上别名有字(分不清哪个是正文)、或别名的值不是字符串的, 原样不动 —— 正文还是空的,
        照旧走 W-30 的 empty_body 拒, 回包说清收到了哪些字段;
      · 只换出新的 dict, 不改调用方传进来的那份; 别名键原样留着(core 不读它们);
      · 对上了几篇只进日志(给工程看模型还在往哪个键里放), 回包一个字段不多。"""
    if not isinstance(drafts, list):
        return drafts
    out: list = []
    moved: dict[str, int] = {}
    for d in drafts:
        if not isinstance(d, dict):
            out.append(d)
            continue
        fixed = d
        for keys in (_TITLE_KEYS, _BODY_KEYS):
            name = keys[0]
            v = d.get(name)
            if v is not None and str(v).strip():
                continue
            hits = [k for k in keys[1:] if isinstance(d.get(k), str) and d[k].strip()]
            if len(hits) == 1:
                fixed = {**fixed, name: d[hits[0]]}
                moved[f"{hits[0]}→{name}"] = moved.get(f"{hits[0]}→{name}", 0) + 1
        out.append(fixed)
    if moved:
        logger.warning("%s: project=%s 有稿子的 title / body 放在了别的字段, 按别名收下: %s",
                       tool, str(project_id)[:40], dict(sorted(moved.items())))
    return out


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
        身份已经建了、指纹没写上, 重传看不见指纹会再建一份身份。服务端已经记下了,
        补指纹不归写手管, 照 ``note`` 说一句就行, 用户不用做什么。
      · 两者同时出现 → 同样**先别重传**: 现在重传会把指纹没写上的那几条再建一份。
        照 ``note`` 说一句就行, 用户不用做什么。
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
    # note 是念给写手的(2026-10-30 发布收尾): 补救要先对这个项目跑 backfill 补指纹(两种失败同时发生时补完再把
    # 这批原样重传一次, 顺序不能反), 那是工程侧的事, 只进日志; 写手那边只说"先别重传"。
    if fp_failed:
        logger.warning("ingest_published: project=%s 有 %d 条建了身份没写上指纹(%s)%s —— 补救: 先跑 "
                       "`python -m deskcore.cli backfill --project %s` 补指纹%s",
                       project_id, missing, out.get("fingerprint_error"),
                       f", 另有几行身份没建成({out.get('identity_error')})" if id_failed else "",
                       project_id, ", 然后把这批原样重传一次(已补上的会被跳过)" if id_failed else "")
    if fp_failed and id_failed:
        out["note"] = (f"这批没补完: {missing} 条只存了一半, 另有几行没存上。已记下, 这批先别重传 —— "
                       "现在重传会把存了一半的那几条再存一份。不用你做什么。")
    elif fp_failed:
        out["note"] = (f"有 {missing} 条只存了一半。已记下, 这批不要重传(重传会再存一份), "
                       "不用你做什么。")
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

    scope: "project" 只对本项目生效; "global" 对这个身份名下的**所有项目**生效 —— 整个团队共用一个身份, 所以 global
    等于所有品牌、所有写手都守。只有用户说这条「所有品都这样」时才用 global; 某个写手自己的习惯(「我写东西都这样」)
    不是 global: 记在这个项目上(scope="project"), 或者不记。拿不准就用 project。

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
    except core.RuleInputError as exc:
        # 方向档 / 原文 / 档位写错同理(外部审计 11-02 F06): 校验都在写库之前, 报这个时一行没写
        raise InvalidInput(f"这条规则没记: {exc}") from exc
    # 2026-10-30 复审: 不再带 archive_pending(W-29 原来带, 理由是记规则多半是看了稿给的反馈、改完的稿子同样要存档)。团队共用
    # 一个身份, 一场只说了「以后别用感叹号」的对话也会拿到队友没存的牌和「都存完再给用户看」; 而这场真抽了牌的, 改完的稿子
    # 照样要过 check_drafts(带 angle_key), 提醒在那里一样到。
    return out


def record_edit(project_id: str, ai_title: str, ai_body: str,
                my_title: str, my_body: str, note: str = "",
                _user_id: str | None = None) -> dict:
    """用户手动改了稿子时调这个 —— 这是让文风变得像本人的【最强信号】。

    传 AI 原版和用户改成的样子。调校笔记按项目记: 整个团队共用一个身份, 这份笔记是写这个项目的人共用的(和项目基线
    冲突时以它为准), 不是哪位写手私有的 —— 别跟用户说「这只影响你自己」。

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
    _require_caller(_user_id, "record_edit", "调校笔记记不了")
    # 故意不包 _safe: 写。这是"裂变"的唯一入口, 静默失败 = 文风永远长不出来。
    with _core_value_errors_as_input():
        return core.record_edit(core.sb(), project_id, user_id=_user_id,
                                ai_title=ai_title, ai_body=ai_body,
                                my_title=my_title, my_body=my_body,
                                note=note or None)


def label_example(item_id: str, label: str, _user_id: str | None = None) -> dict:
    """把某条历史稿标成正面案例或反面案例。

    label 传 "positive" / "negative" / "none"(撤销)。

    只能标这个身份名下的稿子(整个团队共用一个身份), 不在名下的会报错。标了的会注入这个项目以后每个人的写作 ——
    正负例是写这个项目的人共用的, 不是哪位写手私有的。

    正例会作为学习样本注入以后的写作; 负例作为「主动规避」的反面教材。

    ⚠️ 负例只标【你看了内容、判定它就是差】的稿子。不要因为某条数据不好就标
    负例 —— 数据不好有太多与内容无关的原因(没进流量池、账号限流、时机不对),
    那样会把被埋没的好内容也标成垃圾。
    """
    _require_caller(_user_id, "label_example", "不能标正负例")
    # 故意不包 _safe: 写。标记没落库却报成功, 用户不会再标第二次。
    with _core_value_errors_as_input():
        return core.label_example(core.sb(), item_id,
                                  None if label in ("none", "", None) else label,
                                  user_id=_user_id)


def my_style(project_id: str, _user_id: str | None = None) -> dict:
    """看这个项目上积累的风格资产: 调校笔记、喂过多少次精修、正负例数。

    也用于回答「你现在记住了我什么」这类问题。整个团队共用一个身份: 调校笔记、正负例和项目规则一样, 都是这个项目上
    记下的、写这个项目的人共用的一份 —— 别把它说成是某个人的个人偏好。

    ⚠️ 看一眼 pending_distillation。大于 0 说明有精修【还没被吸收进笔记】——
    多半是上次调了 record_edit 却没接着调 save_my_style(比如会话中断了)。
    这时返回值里会直接带上 pending_distillation_task, **材料和口径都在里面**,
    照着提炼完调 save_my_style 就能补上, 不需要让用户把稿子再喂一遍。
    """
    _require_caller(_user_id, "my_style", "调校笔记看不了")
    return _safe(core.my_style, core.sb(), project_id, user_id=_user_id)


def save_my_style(project_id: str, notes: str,
                  edit_ids: list[str] | None = None,
                  _user_id: str | None = None) -> dict:
    """把提炼好的调校笔记写回去 —— record_edit 的【第二步】。这是这个项目上大家共用的那一份(团队共用一个身份)。

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
    _require_caller(_user_id, "save_my_style", "调校笔记存不了")
    # 故意不包 _safe: 写。这是"裂变"闭环的最后一步, 静默失败 = 前面白做。
    with _core_value_errors_as_input():
        return core.save_my_style(core.sb(), project_id, notes,
                                  user_id=_user_id, edit_ids=edit_ids)


def my_rules(project_id: str, _user_id: str | None = None) -> dict:
    """列出这个项目上的**全部规则**(含这个身份名下的 global 规则), 含试用档和已停用的。整个团队共用一个身份,
    这些规则写这个项目的人都守, 不是哪位写手私有的。

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
    _require_caller(_user_id, "my_rules", "规则台账不可用")
    # 读, 可降级: 看不到台账不影响写稿。认不出人不是"看不到台账", 上面那行已经拒了(同项目归属的口径)。
    return _safe(core.my_rules, core.sb(), project_id, user_id=_user_id)


def reembed_my_rules(batch: int = 50, _user_id: str | None = None) -> dict:
    """给这个身份名下缺向量的规则补算向量, 一次一批(最多 50 条)。

    什么时候调: `my_rules` 的 counts 里出现「缺向量」时。用户也可能直接说
    「补一下向量」「规则的相关性筛选没生效」。

    **没向量的规则不会报错、照常注入**, 只是不参与相关性筛选 —— 也就是跟
    这次要写的东西毫不相干时也会挤进简报。补完就正常了。

    返回 `remaining` 大于 0 就**再调一次**, 直到它变成 0。
    ⚠️ 但返回里带 `warning`(查到了却一条没补上)时**不要再调** —— 再调还是
    同一批, 只会白花钱。把 warning 原样告诉用户。

    只补这个身份名下的规则(整个团队共用一个身份, 就是团队的规则), 不在名下的补不到。
    """
    _require_caller(_user_id, "reembed_my_rules", "补算功能不可用")
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
    _require_caller(_user_id, "set_rule_state", "拒绝改动规则")
    # 故意不包 _safe: 写。静默失败 = 用户以为关掉了的规则还在每一份简报里,
    # 或者以为留下的规则其实没生效 —— 两个方向都是无声的。
    with _core_value_errors_as_input():
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
