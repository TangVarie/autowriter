"""deskcore/core.py — 写作台内核的纯逻辑层。

不 import FastAPI / MCP SDK —— cli.py 能直接调、能 selftest, 与 app.py 共用同一
份逻辑(librarian 那边 core/CLI/HTTP 三个 adapter 是同一个形状)。

【复用】本仓已有的东西, 不重写:
    memory.build_layered_system_prompt   五层 + hard/soft 分级
    db.get_service_client / get_project / set_item_example_label / upsert_memory
    dedup.embed_texts / cosine_similarity / embeddings_available
    librarian_client.build_brief / fetch_flywheel_lessons
    db._parse_pgvector                   (R-034: PostgREST 把 pgvector 序列化成
                                          字符串, 不归一的话 cosine 静默返回 0.0)

【新增】的只有四样(其余全是薄封装):
    1. 发牌 draw_angles + 跨批次台账
    2. 查重硬闸 check_drafts + 持久全量指纹库
    3. 正例按【相关性】选取, 取代 recency top-5(断掉趋同回路)
    4. 个人调校笔记分层(项目共享基线 + 个人叠加)。蒸馏本身【交给调用方模型做】,
       服务端只出材料和口径 —— 见 build_distillation_task / save_my_style。
"""

from __future__ import annotations

import base64
import logging
import random
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional

import config
import db
import dedup
# ⚠️ 新增的耦合(export_drafts 用它): exporter 顶层 import 了 docx / openpyxl。
#    两个都在 requirements.lock 里, 而 deskcore 的部署方式就是
#    `pip install -r requirements.lock -r deskcore/requirements.txt`
#    (见 deskcore/requirements.txt 头两行), 所以这条是成立的。
#    真要给 deskcore 做瘦镜像的话, 这一行会让**整个服务起不来**而不是只坏一个
#    工具 —— 那时候把它改成函数内延迟 import。
import exporter
import librarian_client
import memory

from . import fingerprint as fp
from . import store, tvlink, vocab

logger = logging.getLogger("deskcore.core")

MAX_POSITIVE = 5
MAX_NEGATIVE = 3

# 判定阈值与 angle_key 住在 fingerprint.py —— 那个模块只用标准库, 让 selftest
# 能在不装 supabase/anthropic 的裸环境里跑(本模块顶层 import db, 拖整条依赖链)。
angle_key = fp.angle_key
verdict = fp.verdict


def sb():
    return store.client()


# ══════════════════════════════════════════════════════════════════════
# 归属校验(审计 COR-015)
# ══════════════════════════════════════════════════════════════════════

class ProjectNotFound(ValueError):
    """project_id 在库里没有这一行。多半是 id 抄错, 不是权限问题。

    与 PermissionError 分开是有意的: 「不存在」和「不是你的」对调用方是两种
    完全不同的下一步(改 id / 换项目), 混成一种会让人反复试同一个错 id。
    """


def assert_project_access(client, project_id: str, *,
                          user_id: str | None) -> dict:
    """**整套项目归属校验的唯一实现**。通过则返回项目整行。

    审计 COR-015: 在此之前 deskcore 对 project_id 【没有任何归属校验】——
    ``check_drafts`` / ``borrow_lessons`` / ``list_projects`` 连调用者是谁都不问,
    其余工具虽然拿到了 user_id 却只用它读个人层, 从不核对项目归谁。于是任一
    持有效 key 的调用方传入他人 project_id 就能:

      · 读他人项目的全部成稿标题(``check_drafts`` 的 ``collided_with`` 会回显);
      · 往他人项目写指纹、写角度台账、写团队共享的 hard 规则。

    ``deskcore/store.py`` 原来明写着"不按 owner 过滤"是设计选择(项目规则团队
    共享)。本条指出的不是那个选择本身错, 而是它的代价: 整套隔离就只剩 key 这
    一层, 而 key 这一层有 ROB-003(配错就全开)。产品决策已定 —— **按
    ``projects.owner_id`` 隔离**, 读写两侧都校验。

    ── 为什么收敛成一个函数 ──────────────────────────────────────────
    归属口径是**产品决策**, 会变(今天按 owner, 明天可能按团队成员表)。散在十
    一个工具里就意味着改口径要改十一处, 而漏掉的那处不会报错, 只会继续放行。
    所以: 判据只写在这里, 将来换模型**只改这个函数体** —— 加一张
    ``project_members`` 表就是把下面那个 ``!=`` 换成一次成员查询, 调用方一行不动。

    ── 为什么 user_id 缺失是拒绝而不是放行 ────────────────────────────
    与 ROB-003 同一口径: 身份识别不出来时, "放行"意味着一个配置疏忽就等于把
    全部租户的项目数据开放出去。匿名 dev 模式(``DESKCORE_ALLOW_ANONYMOUS=1``)
    要能用就得同时配 ``DESKCORE_DEFAULT_USER_ID`` —— 那本来就是它该配的东西
    (identity.py 的 ``resolve`` 就是这么回的), 否则个人层(正负例/调校笔记)一样
    读不出东西。
    """
    if not project_id or not str(project_id).strip():
        raise ProjectNotFound("project_id 不能为空")
    if not user_id:
        raise PermissionError(
            "无法识别调用者身份, 拒绝访问项目数据 —— deskcore 持 service_role "
            "绕过 RLS, 认不出人就等于对所有租户开放。服务端要配 DESKCORE_KEYS "
            "(推荐)或 DESKCORE_API_KEY + DESKCORE_DEFAULT_USER_ID; 本地匿名 dev "
            "模式也要配 DESKCORE_DEFAULT_USER_ID。")

    project = store.project_row(client, project_id)
    if project is None:
        raise ProjectNotFound(f"project not found: {project_id}")

    # ↓↓↓ 换归属模型时【只改这三行】↓↓↓
    owner = project.get("owner_id")
    if str(owner or "") != str(user_id):
        raise PermissionError(
            f"project {project_id} 不属于当前调用者, 拒绝访问。"
            "deskcore 按 projects.owner_id 隔离 —— 项目规则在同一 owner 的项目"
            "之间共享, 跨 owner 不共享。project_id 是不是传错了?")
    # ↑↑↑ 换归属模型时【只改这三行】↑↑↑
    return project


# ══════════════════════════════════════════════════════════════════════
# 方向档(产品向 / 流量向)
# ══════════════════════════════════════════════════════════════════════
#
# 同一条技艺在两种方向上经常是【相反】的要求。三路独立质疑在 37 簇里反复
# 撞到同一件事:"结尾截断留白"和"结尾完整收尾"、"去品牌标签"和"正文嵌品牌
# 名"、"编号分点"和"叙事时间线" —— 每一对的项目集几乎互补, 一边全是产品/
# 体验向, 另一边全是流量/叙事向。也就是说它们不是互相矛盾的两条规则, 是
# 同一条规则的两个条件分支。没有这个维度, 技艺库必然自相打架。
#
# ⚠️ 判据【只认这两个词】, 不做泛化猜测。``applicability`` 是自由文本, 库里
# 已经有 "正文" / "改简历部分" 这类**部位**标注在用 —— 那些不是方向, 必须原样
# 放行。把闸门做成"有 applicability 就按它过滤"会把这些老行全部静默挡掉,
# 而调用方看到的一切都正常。这正是本仓库反复出现的那类事故。
DIRECTION_TAGS = ("产品向", "流量向")


def detect_direction(project_name: str, tactic: str = "") -> str:
    """从项目名 / 战术名判出这次写的是哪个方向。判不出来返回 ``""``。

    判不出来时上层**放行全部规则** —— 保守方向必须是"多注入", 不是"少注入":
    漏注入一条写手明明设过的规则是无声的, 多注入一条他至少看得见。
    """
    text = f"{project_name or ''} {tactic or ''}"
    hit = [t for t in DIRECTION_TAGS if t[:2] in text]   # "产品" / "流量"
    # 两个方向词同时出现(比如"产品直出-流量版")= 判不出来, 放行。
    return hit[0] if len(hit) == 1 else ""


def filter_by_applicability(rules: list[dict], direction: str) -> tuple[list[dict], list[dict]]:
    """按方向档筛 soft 规则。返回 ``(留下的, 挡掉的)``。

    规则的 ``applicability`` 里带方向词的才受这道闸管; 空的、或者写的是别的
    东西(部位标注之类)一律放行。``direction`` 为空时全部放行。
    """
    if not direction:
        return list(rules), []
    keep, dropped = [], []
    for r in rules:
        tag = (r.get("applicability") or "").strip()
        tagged = [t for t in DIRECTION_TAGS if t in tag]
        if tagged and direction not in tagged:
            dropped.append(r)
        else:
            keep.append(r)
    return keep, dropped


# ══════════════════════════════════════════════════════════════════════
# 写作简报
# ══════════════════════════════════════════════════════════════════════

def _brief_text(brief: dict) -> str:
    parts = [brief.get(k) or "" for k in
             ("tactic", "draft_topic", "key_messages", "target_audience",
              "tone", "extra_instructions")]
    return "\n".join(p for p in parts if p).strip()


def select_positive_examples(candidates: list[dict], brief: dict,
                             limit: int = MAX_POSITIVE) -> tuple[list[dict], str]:
    """按相关性 + 多样性挑正例, 取代 db.list_example_items 的 created_at DESC 取 5。

    为什么改: recency top-5 构成【趋同回路】—— 模型模仿最近 5 条 → 新稿被标
    positive → 窗口滚动 → 语感越收越窄。而监控这件事的 TV
    check_positive_saturation.py 只统计 external_source='truth_vault' 的行,
    那列生产库里全 NULL, 所以它从上线起永远打印"没有正例" —— 这个回路
    从来没被任何人看见过(TV D-041 / R-034 已修那个盲点)。

    embedding 不可用时退化成 recency(与原行为一致, 不会更差)。
    返回 (picked, mode)。
    """
    if not candidates:
        return [], "empty"

    ranked, mode = candidates, "recency_fallback"
    text = _brief_text(brief)
    if text and dedup.embeddings_available():
        vecs = dedup.embed_texts([text])
        if vecs:
            bvec = vecs[0]
            scored = []
            for c in candidates:
                emb = c.get("embedding")
                # 没算过向量的排后面但【不丢弃】—— 否则新项目(向量还没回填)
                # 会一条正例都取不到。
                scored.append((dedup.cosine_similarity(bvec, emb) if emb else -1.0, c))
            scored.sort(key=lambda t: t[0], reverse=True)
            ranked, mode = [c for _s, c in scored], "relevance"

    # 多样性限额在 fingerprint.cap_by_shape —— 纯逻辑放无依赖模块, 让 selftest
    # 能在裸环境里验它(这一条是趋同回路的最后一道闸, 值得单独回归)。
    return fp.cap_by_shape(ranked, limit), mode


def build_writing_brief(client, project_id: str, *, user_id: str | None = None,
                        brief: dict | None = None) -> dict:
    """一次拿全写作上下文 —— 治「一个项目 5 个提示词要点 5 次」。

    分层【直接复用 memory.build_layered_system_prompt】, 只在调用前把
    calibration_notes 拼成「项目共享基线 + 我的个人叠加」两段 —— 这是 deskcore
    唯一改动的语义(隔离口径: 项目规则团队共享 + 个人风格私有)。
    """
    brief = brief or {}
    # 归属校验 + 取项目行一次搞定 —— 这里本来就要 db.get_project, 所以 COR-015
    # 的校验在这条路径上【不多发一次查询】。
    project = assert_project_access(client, project_id, user_id=user_id)

    hard, soft = store.shared_memories(client, project_id, user_id)
    pool = store.labeled_examples(client, project_id, "positive", user_id)
    positives, pos_mode = select_positive_examples(pool, brief)
    negatives = store.labeled_examples(client, project_id, "negative", user_id)[:MAX_NEGATIVE]

    shared_calib = (project.get("calibration_notes") or "").strip()
    my_calib, _ = store.get_user_calibration(client, project_id, user_id) if user_id else ("", None)
    calib_parts = []
    if shared_calib:
        calib_parts.append(f"[项目共享基线]\n{shared_calib}")
    if my_calib:
        calib_parts.append("[我的个人风格 —— 从我手动改稿里提炼; 与项目基线冲突时以本节为准]\n"
                           + my_calib)
    calibration = "\n\n".join(calib_parts)

    # 战术后缀: app.py 的队列/快速生成两条路径都调 get_tactic_prompt_suffix 并
    # 把它作为独立一层注入(app.py:1066 / 3131 / 4857)。deskcore 不带的话, 项目
    # 配好的战术专属写作指令会【静默丢失】—— 传了 tactic 名却只影响正例排序。
    # (codex review P1)
    import projects as proj_module   # 纯函数; CI 的 import 图冒烟已覆盖该模块
    tactic_name = (brief.get("tactic") or "").strip()
    tactic_suffix = ""
    if tactic_name:
        tactic_suffix = proj_module.get_tactic_prompt_suffix(project, tactic_name) or ""

    # ⚠️ tactics 必须解码再返回。db.create_project 和战术设置页都是用
    # json.dumps 存的, PostgREST 原样带回一个【JSON 字符串】而不是 list ——
    # 直接透传的话, MCP 调用方拿到的 tactics 是一坨字符串, 想枚举有哪些战术
    # 只能自己猜着 json.loads。用 projects 里那个同款解析器
    # (get_tactic_prompt_suffix 内部就是它), 口径保持一处。(codex review)
    tactics = proj_module._parse_json_field(project.get("tactics"), []) or []

    # ── soft 规则: 相关性过滤 + 封顶 ────────────────────────────────
    # hard 全量保留(合规, 不能因为"跟本次不相关"就丢)。soft 要过一遍
    # autowriter 生产路径同样的两道(app.py:1096/3150 + db.get_confirmed_memories
    # 的 cap_per_scope), 否则 P1 会被历史偏好堆爆:
    #   · deskcore 的规则是【团队共享】的 —— 池子比 autowriter 单人视角大得多,
    #     这一层不做, 二十条互相打架的旧偏好会一起进 P1, 模型只能写出四不像;
    #   · 排序用 db._rank_memories_for_injection: 7 天内的新规则永远不被老的
    #     高频规则挤掉("我刚说过 → 立刻生效"), 这是工作台"规则不忘"的一部分。
    # (codex review round-5 P2)
    soft_ctx = " ".join(filter(None, [
        brief.get("tactic", ""), brief.get("draft_topic", ""),
        brief.get("key_messages", ""), brief.get("target_audience", ""),
        brief.get("tone", ""), brief.get("extra_instructions", ""),
    ])).strip()
    soft_report: dict = {}
    soft_all = len(soft)
    # ── 方向档闸(产品向 / 流量向) ──────────────────────────────────
    # 排在相关性过滤【前面】。相关性靠 embedding, 而库里绝大多数规则还没有
    # 向量 —— filter_soft_by_relevance 对无向量的一律放行。也就是说今天相关性
    # 这一层几乎拦不住东西, 方向闸是真正在起作用的那道。
    #
    # ⚠️ 顺序是 方向闸 → 相关性 → 排序封顶, 三者都在 cap 之前。别把相关性挪到
    # cap 后面: 那样 cap 先按"新 + 高频"挑满 12 条, 相关性再从里面删, 最后注入
    # 的会【少于 12 条】, 而池子里本来还有相关的排在第 13 位没能补上。
    direction = detect_direction(project.get("name") or "",
                                 brief.get("tactic") or "")
    soft, dropped_by_direction = filter_by_applicability(soft, direction)
    if soft_ctx:
        soft = memory.filter_soft_by_relevance(soft, soft_ctx, report_sink=soft_report)
    soft_cap = int(getattr(config, "MAX_INJECTED_MEMORIES_PER_SCOPE", 12) or 12)
    soft_by_scope = {"global": [], "project": []}
    for m in soft:
        soft_by_scope["global" if m.get("scope") == "global" else "project"].append(m)
    soft = (db._rank_memories_for_injection(soft_by_scope["global"], soft_cap)
            + db._rank_memories_for_injection(soft_by_scope["project"], soft_cap))

    layers = memory.build_layered_system_prompt(
        base_prompt=project.get("system_prompt") or "",
        tactic_suffix=tactic_suffix,
        global_memories=[m for m in soft + hard if m.get("scope") == "global"],
        project_memories=[m for m in soft + hard if m.get("scope") != "global"],
        calibration_notes=calibration,
        positive_examples=positives,
        negative_examples=negatives,
    )

    return {
        "project_id": project_id,
        "project_name": project.get("name") or "",
        "brand": project.get("brand") or "",
        "stable": layers.get("stable", ""),
        "tactic_layer": layers.get("tactic", ""),
        "p0": layers.get("p0", ""),
        "p1": layers.get("p1", ""),
        "tactics": tactics,
        "counts": {
            "hard_rules": len(hard),
            "soft_rules": len(soft),
            # 说清楚"注入了几条 / 池子里共几条", 否则用户定过的偏好没生效时
            # 完全看不出是被滤掉了还是根本没存进去。
            "soft_rules_pool": soft_all,
            "soft_filter_mode": soft_report.get("soft_filter_mode", "off"),
            "soft_rules_cap_per_scope": soft_cap,
            # 方向闸必须可见。被方向挡掉的规则和"根本没存进去"在写手眼里
            # 长得一模一样, 不回显就没法自查 —— 这套东西的全部意义就是让写手
            # 自己看得见、自己能改。
            "direction": direction or "未判定",
            "soft_dropped_by_direction": len(dropped_by_direction),
            "positive_examples": len(positives),
            "positive_pool": len(pool),
            "negative_examples": len(negatives),
            "has_shared_calibration": bool(shared_calib),
            "has_personal_calibration": bool(my_calib),
            "tactic_suffix_applied": bool(tactic_suffix),
        },
        "positive_selection_mode": pos_mode,
    }


# ══════════════════════════════════════════════════════════════════════
# 发牌
# ══════════════════════════════════════════════════════════════════════


def draw_angles(client, project_id: str, n: int, *, avoid_days: int = 30,
                user_id: str | None = None, seed: int | None = None,
                perpetual_bias: bool = False) -> dict:
    """发 n 张互不重复、且避开台账的创作坐标, 写入台账。

    这是 generator._assign_slot_coordinates(:1301) 的复活版。当年移除
    (generator.py:1576-1584)的理由是通用角度池跟项目自己的 role 设定打架 ——
    LLM 会锁定更具体的平台标签、把项目的 role 降级成"风格提示"。修法照注释里
    留的那条路: **切入角度优先用项目自己的 custom_roles**, 没配才用通用池。
    """
    # 归属校验放在 n<=0 的早返回【之前】: 每个项目级入口都以这一行开头, 才能在
    # CI 里用一条断言把"有没有漏掉某个工具"验死。代价是 n=0 时多一次查询,
    # 而 n=0 本来就是退化调用。(审计 COR-015)
    project = assert_project_access(client, project_id, user_id=user_id)
    if n <= 0:
        return {"angles": [], "requested": 0, "delivered": 0}

    custom = project.get("custom_roles") or []
    if isinstance(custom, str):
        import json
        try:
            custom = json.loads(custom)
        except (ValueError, TypeError):
            custom = []
    angles_pool = ([{"id": r.get("id", ""), "name": r.get("name", ""),
                     "brief": (r.get("prompt_suffix") or "").strip()}
                    for r in custom] if custom else vocab.default_angles())
    pool_source = "project.custom_roles" if custom else "generator.CREATIVE_ROLES_POOL"

    rng = random.Random(seed) if seed is not None else random.Random()

    space = [(lev, arc, fmt, st)
             for lev in vocab.EMOTIONAL_LEVERS
             for arc in vocab.HUMAN_TRUTH_ARCHETYPES
             for fmt in vocab.CONTENT_FORMATS
             for st in vocab.TITLE_STRUCTURES]
    rng.shuffle(space)

    # 过量供给候选给 RPC —— 它在事务里逐个查避重集、取前 n 个可用的。
    # 20 倍冗余(下限 500)足以覆盖"近期用掉很多"的项目。
    cand_cap = min(len(space), max(n * 20, 500))
    candidates = []
    for lev, arc, fmt, st in space[:cand_cap]:
        dims = {"emotional_lever": lev, "human_truth_archetype": arc,
                "content_format": fmt, "title_structure": st}
        candidates.append({"angle_key": angle_key(dims), "dims": dims})

    reserved = store.reserve_angles(client, project_id, candidates, n,
                                    user_id, avoid_days)
    atomic = reserved is not None
    if not atomic:
        # RPC 不在(迁移没跑)。降级到旧的"读-挑-插"三步, 并明确告知不是原子的。
        avoid = store.recent_angle_keys(client, project_id, avoid_days)
        reserved = [c for c in candidates if c["angle_key"] not in avoid][:n]
        store.record_draw(client, project_id, reserved, user_id)

    tilt = rng.choice(vocab.WORD_TILTS)  # 词感是"今天的心情", 全批一致

    # ⚠️ 角度池要洗牌, 不能每次都从 0 号开始取。
    # 原来是 angles_pool[len(picked) % len(angles_pool)] —— 每次发牌都从头数,
    # 于是【每批要的篇数少于角度数时, 后面的角度永远轮不到】: 6 个角度、每次
    # 只发 3 篇, 那就永远只用前 3 个, 另外 3 个一次都不会出现。其他维度都随机
    # 了, 唯独这一维退化成常量, 而这正是"跨批次别老写同样切入"要治的东西。
    # 用本次请求的 rng 洗牌: seed 相同则结果可复现(draw 支持 seed 参数)。
    # (codex review)
    angle_order = list(angles_pool)
    rng.shuffle(angle_order)

    picked: list[dict] = []
    for r in reserved:
        dims = r["dims"]
        lev = dims.get("emotional_lever", "")
        angle = angle_order[len(picked) % len(angle_order)]
        trends = ([vocab.TREND_EXCLUSIVE] if perpetual_bias
                  else vocab.normalize_trends([rng.choice(vocab.TREND_DEPENDENCIES)]))
        picked.append({
            "slot": len(picked) + 1,
            "angle_key": r["angle_key"],
            "dims": dims,
            "emotional_valence": vocab.valence_of(lev),
            "emotional_intensity": rng.choice(vocab.EMOTIONAL_INTENSITIES),
            "trend_dependencies": trends,
            "word_tilt": tilt,
            "angle_name": angle.get("name") or angle.get("id") or "",
            "angle_brief": angle.get("brief") or "",
            "boundary_rule": vocab.boundary_rules_for(lev),
        })

    if len(picked) < n:
        logger.warning("angle space exhausted (project=%s): asked %d got %d",
                       project_id, n, len(picked))

    return {
        "angles": picked,
        "requested": n,
        "delivered": len(picked),
        "prompt_block": render_angles_block(picked),
        "angle_pool_source": pool_source,
        "atomic_reservation": atomic,
        "combination_space": vocab.combination_space(),
        "note": ("发到的组合少于请求数, 说明近期用掉太多; 可以调小 avoid_days 或分批写。"
                 if len(picked) < n else ""),
        "warning": ("" if atomic else
                    "本次发牌不是原子的(deskcore_reserve_angles RPC 不存在, "
                    "migrations/001 可能没跑)。并发发牌时可能与队友撞车。"),
    }


def render_angles_block(angles: list[dict]) -> str:
    """渲染成可直接贴进 prompt 的硬约束块。

    照 generator._build_slot_coordinates_block(:1350) 的形状, 但多带 essence
    维度和边界判据 —— 光给标签名模型会混(docs/05 §3 花 40 行讲焦虑 vs 恐惧
    怎么分是有原因的)。
    """
    if not angles:
        return ""
    lines = ["【本批次每篇的创作坐标（必须按编号对应，不得互换）】"]
    for a in angles:
        d = a["dims"]
        lines.append(
            f"第{a['slot']}篇：情绪杠杆={d['emotional_lever']}"
            f"({a['emotional_valence']}/{a['emotional_intensity']})"
            f" · 人性原型={d['human_truth_archetype']}"
            f" · 内容形式={d['content_format']}"
            f" · 标题句式={d['title_structure']}"
            f" · 切入角度={a['angle_name']}")
        # ⚠️ 时效依赖必须渲进来。它是发牌时抽的(perpetual_bias=True 时强制抽
        # 排他值「通用」), 但这个块才是文档让人贴进 prompt 的东西 —— 不渲染
        # 的话模型根本收不到, perpetual_bias 这个开关等于没接线, 除非调用方
        # 自己去翻原始 angles。(codex review)
        trends = a.get("trend_dependencies") or []
        if trends:
            note = ("（排他值：全篇不得出现任何时事、节日、平台事件、流行词等"
                    "时效元素）" if vocab.TREND_EXCLUSIVE in trends else "")
            lines.append(f"    ↳ 时效依赖：{'、'.join(trends)}{note}")
        if a.get("boundary_rule"):
            lines.append(f"    ↳ 判据：{a['boundary_rule']}")
        if a.get("angle_brief"):
            lines.append(f"    ↳ 角度：{a['angle_brief']}")
    lines.append(f"\n全批词感倾向：{angles[0].get('word_tilt', '')}")
    lines.append("以上坐标为硬约束。写完后每一篇要能说出自己用的是哪一组，说不出就是没按坐标写。")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# 查重硬闸
# ══════════════════════════════════════════════════════════════════════


class HistHit(NamedTuple):
    """一篇草稿与历史比对的**逐信号**最佳命中。

    每一路各记各的 —— 各路的最佳命中可能来自不同的历史稿, 混在一起报会让人
    对着一条根本没引发拒绝的稿子去改(codex review 记过这一条)。
    ``*_title`` 为 None 表示这一路没有命中(不是"命中了一条空标题")。
    """
    sim: float
    sim_title: str | None
    jac: float
    j_title: str | None
    open_exact: bool
    open_title: str | None
    contain: float                 # 审计 COR-014
    contain_title: str | None
    contain_sample: int            # 有效样本量, 低于阈值时这一路不发言


def _history_probe(client, project_id: str, o_hashes: list[str],
                   grams: list[set], new_vecs):
    """返回 ``(probe, hist_size, hist_with_vec, hist_truncated)``。

    ``probe(i)`` → ``HistHit`` —— 第 i 篇草稿与【历史】比对的四路最佳命中。

    两条实现共用这一个形状:

      A. 下推路径(审计 SUP-002 / ROB-004 / ROB-011, 需要 migrations/004)
         整个比对在库里做完, 只回每篇一行结果。原来是把 4000 行 × 768 维拉进
         Python 逐对算 —— 百 MB 传输 + 三千万次乘加 + 占满线程池。

      B. Python 路径(migrations/004 没跑时)
         与下推前完全一致, 包括 4000 条的上限和 history_truncated 的警告。

    ⚠️ 只有"RPC 不存在"才降级到 B。查重是 deskcore 唯一不 fail-open 的路径,
    其它异常(权限 / 库故障)一律冒泡。
    """
    # ⚠️ ``embed_texts`` 是**逐位对齐**的, 但某一位可能是 None —— 那条稿子没有
    # 可嵌入的标题(空标题)。所以判据必须是"这一位有没有向量", 不能只判
    # "这一批有没有向量、下标越没越界"。
    #
    # 2026-08-26 这里真崩过一次: 判据写成 `new_vecs and i < len(new_vecs)`,
    # 于是空标题那一位的 None 掉进 `for x in None`, 整个 check_drafts 回 500。
    # 起因是同一天把 embed_texts 从"整批全有或全无"改成了逐位可空(那个改动本身
    # 是对的 —— 一条空标题不该拖垮整批), 而**这个调用点没跟上新契约**。
    # 当时的回归夹具是 `lambda ts: [[0.1]] * len(ts)`, 永远不产生 None, 所以
    # 测试套一片绿 —— 假件比真实情况"整齐", 测出来的绿就是假的。
    def _vec_literal(i: int) -> Optional[str]:
        v = new_vecs[i] if new_vecs and i < len(new_vecs) else None
        return f"[{','.join(repr(float(x)) for x in v)}]" if v else None

    payload = [
        {
            "opening_hash": o_hashes[i] or None,
            "ngram_hashes": sorted(grams[i]),
            # 这一条算不出向量时传 null, 库里那一路直接跳过。
            "title_embedding": _vec_literal(i),
            # ⚠️ 必须带上模型名: 库里那一路要用它把**别的模型产的**历史向量
            # 排除掉。跨模型算余弦出来的数是垃圾且【不报错】—— 不带的话
            # migrations/008 之后的函数会退回"比全部非空向量"的老行为, 而
            # 老行为正是 #65 P1 说的那个洞。(codex review · #65 P1)
            #
            # 没有向量的那条不写模型名 —— 写了也没意义, 而且会让"这条比过没有"
            # 从回执里看不出来。
            "embedding_model": (dedup.EMBEDDING_MODEL
                                if _vec_literal(i) else None),
        }
        for i in range(len(o_hashes))
    ]
    # 阈值只在 fingerprint.py 里定义一处 —— SQL 里的 DEFAULT 只是兜底。
    rows = store.check_drafts_sql(client, project_id, payload,   # 故意让异常冒泡
                                  contain_min_sample=fp.CONTAIN_MIN_SAMPLE)
    if rows is not None:
        by_idx = {int(r.get("idx", -1)): r for r in rows}
        # ⚠️ 回执必须每篇一行。少一行就意味着那一篇【根本没比过】, 而下面
        # 取不到时会退化成全 0 信号 → 直接判 pass。查重是硬闸, 这种"静默放行"
        # 正是它最不能有的失败模式, 宁可整个调用报错。
        missing = [i for i in range(len(payload)) if i not in by_idx]
        if missing:
            raise RuntimeError(
                f"deskcore_check_drafts 回执缺 {len(missing)}/{len(payload)} 篇"
                f"(idx={missing[:5]}) —— 拒绝按'没撞车'放行")
        # 口径是"有【本模型】向量的条数" —— 见 store.fingerprint_stats 的说明。
        total, with_vec = store.fingerprint_stats(client, project_id,
                                                  dedup.EMBEDDING_MODEL)

        def _probe(i: int) -> HistHit:
            r = by_idx.get(i) or {}
            # NUMERIC 在 PostgREST 上可能回数字也可能回字符串, float() 两种都吃。
            # open_exact 不看它自己的布尔值而是看 open_title 有没有 ——
            # 万一布尔被序列化成字符串, `bool("false")` 是 True, 而
            # opening_exact 是**单独就判死**的最强信号, 没有任何东西兜得住
            # 这个误伤。RPC 里那一列本来就是 `open_title IS NOT NULL` 算出来的,
            # 这么取口径完全一致, 只是不依赖布尔的传输形态。
            #
            # ⚠️ best_c / c_title / c_sample 是 migrations/005 加的。老版本 RPC
            # (只跑过 004)回不出这三列 —— 取不到就当**这一路没跑**(sample=0,
            # 于是 deciding_signals 里 contain_ok 为 False, 包含度不发言),
            # 而不是当成"包含度为 0 = 没撞车"。两者的差别在下面的
            # contain_pushdown_missing 里报给调用方。(审计 COR-014)
            return HistHit(
                float(r.get("best_sim") or 0.0),
                r.get("sim_title"),
                float(r.get("best_j") or 0.0),
                r.get("j_title"),
                r.get("open_title") is not None,
                r.get("open_title"),
                float(r.get("best_c") or 0.0),
                r.get("c_title"),
                int(r.get("c_sample") or 0),
            )

        # 下推路径比的是【全量】—— 库里没有 4000 条那个上限, 所以永不截断。
        return _probe, total, with_vec, False

    logger.warning(
        "deskcore_check_drafts RPC 不存在(migrations/004 没跑) —— 本次退回 "
        "Python 逐对比对: 大项目会慢数十秒并占满线程池, 见审计 SUP-002/ROB-004。")
    _rpc_missing_telemetry("deskcore_check_drafts", project_id)

    history, truncated = store.fingerprints(client, project_id)
    hist_grams = [set(h.get("ngram_hashes") or []) for h in history]
    hist_open = {h.get("opening_hash"): h for h in history if h.get("opening_hash")}
    # ⚠️ 只把**本模型**产的向量算进比对。跨模型算余弦出来的数是垃圾, 而且
    # 【不报错】—— 混着比的结果是硬闸看起来跑了、结论却是噪声。
    # ``embedding_model`` 为 NULL 的行同样排除: 那是"来路不明", 不是"本模型"。
    # 排除掉的行仍然计入 total, 于是 with_vec < total → semantic_degraded 为
    # true 并把原因报出去。**宁可说自己没比全, 也不能拿垃圾数当比过了。**
    # (codex review · #65 P1; 与库里那一路 migrations/008 的过滤同一口径)
    hist_vecs = [h.get("title_embedding")
                 if h.get("embedding_model") == dedup.EMBEDDING_MODEL else None
                 for h in history]
    with_vec = sum(1 for v in hist_vecs if v)

    def _probe_py(i: int) -> HistHit:
        best_sim, sim_hit = 0.0, None
        if new_vecs and i < len(new_vecs):
            for hi, emb in enumerate(hist_vecs):
                if not emb:
                    continue
                s = dedup.cosine_similarity(new_vecs[i], emb)
                if s > best_sim:
                    best_sim, sim_hit = s, history[hi]
        best_j, j_hit = 0.0, None
        best_c, c_hit, c_sample = 0.0, None, 0
        for hi, hg in enumerate(hist_grams):
            # ⚠️ 两边都是 bottom-k **sketch**, 不是完整 gram 集 —— 必须走
            # sketch_overlap, 直接 fp.jaccard 会系统性偏低(审计 COR-014)。
            j, c, s = fp.sketch_overlap(grams[i], hg)
            if j > best_j:
                best_j, j_hit = j, history[hi]
            # 包含度**单独记它自己的最佳命中**: 抄袭源和"用词最像的那篇"经常
            # 不是同一条, 归错了人会对着无关的稿子改。
            #
            # ⚠️ 样本量的判据必须在**取最大之前**。原来是先按 c 取冠军、事后
            # 才看冠军的样本量够不够 —— 一条毫不相关、只跟本稿撞上一个低位
            # hash 的历史稿能拿到 c=1.0 / 样本量=1, 压过真正的抄袭源
            # (c=0.9 / 样本量>=15); 冠军随后因样本量不足被丢掉, 而真命中
            # 根本没进过决赛, 于是照搬长稿的稿子**两道闸都放行**。
            # SQL 侧(migrations/005 的 cbest 与 commit 的 LOOP)同一口径。
            if s >= fp.CONTAIN_MIN_SAMPLE and c > best_c:
                best_c, c_hit, c_sample = c, history[hi], s
        open_hit = hist_open.get(o_hashes[i]) if o_hashes[i] else None
        return HistHit(
            best_sim, (sim_hit or {}).get("title", "") if sim_hit else None,
            best_j, (j_hit or {}).get("title", "") if j_hit else None,
            open_hit is not None,
            (open_hit or {}).get("title", "") if open_hit else None,
            best_c, (c_hit or {}).get("title", "") if c_hit else None,
            c_sample,
        )

    return _probe_py, len(history), with_vec, truncated


def _rpc_missing_telemetry(name: str, project_id: str) -> None:
    """把"迁移没跑"埋一行点。静默降级最怕的就是没人知道它降级了。"""
    try:
        import telemetry
        telemetry.log_event("deskcore_rpc_missing", rpc=name,
                            project_id=project_id[:8],
                            hint="跑 migrations/004_deskcore_check_pushdown.sql")
    except Exception:
        pass


def check_drafts(client, project_id: str, drafts: list[dict],
                 *, user_id: str | None = None) -> dict:
    """比对全量历史 + 本批内互比。

    ⚠️ deskcore 唯一【不 fail-open】的路径。其它读类工具出错返回可用结构不阻塞
    写稿, 但查重挂了必须抛 —— 静默放行就是重演 config.py 那个
    ENABLE_DEDUP_REGEN 默认 "0"、查重跑了但不拦的老问题。

    ⚠️ 这是审计 COR-015 里【读侧最要命的那个】: 返回值的 ``collided_with`` 会回显
    撞车对象的**标题**。没有归属校验时, 拿一篇随便什么稿子去撞别人的项目, 就是
    一个可以反复调用的历史标题读取接口。所以 user_id 现在是必需的。
    """
    assert_project_access(client, project_id, user_id=user_id)
    if not drafts:
        return {"results": [], "summary": {"total": 0, "pass": 0, "warn": 0, "reject": 0}}

    titles = [(d.get("title") or "").strip() for d in drafts]
    bodies = [d.get("body") or "" for d in drafts]
    new_vecs = dedup.embed_texts(titles) if dedup.embeddings_available() else None
    return _gate(client, project_id, titles, bodies, new_vecs)


def _gate(client, project_id: str, titles: list[str], bodies: list[str],
          new_vecs) -> dict:
    """check_drafts 的判定主体: 与历史比对 + 本批内互比 + 逐条 verdict。

    从 check_drafts 里抽出来是为了让 **commit_drafts 也跑同一套**(2026-09-17)。
    在此之前入库只靠 RPC 的两路确定性信号(开头精确 / 四字串), 标题语义
    (TITLE_SIM_HARD)和本批内互比只在 check_drafts 里 —— 于是一个跳过 check
    直接 commit 的模型能把同题重写的稿子整批灌进库: 途鸽 09-10 一天里四个标题
    各入库两次, 两两 Jaccard 只有 0.33, RPC 全放行。闸不该依赖模型记得先调它。

    不做归属校验、不算向量: 两个调用方各自做完再进来, 向量按 ``titles`` 逐位
    对齐(某一位可以是 None —— 空标题算不出向量)。
    """
    o_hashes = [fp.opening_hash(b) for b in bodies]
    grams = [set(fp.ngram_hashes(b)) for b in bodies]


    # ── 与历史比对: 优先下推到库里(审计 SUP-002 / ROB-004 / ROB-011)──────
    # ``hist`` 是一个「按 i 取四路最佳命中」的可调用对象, 两条路径共用同一个
    # 形状, 下面拼 verdict 的代码因此完全不用分叉。
    hist, hist_size, hist_with_vec, hist_truncated = _history_probe(
        client, project_id, o_hashes, grams, new_vecs)

    # ⚠️ 降级判定不能只看"这批能不能算向量"(codex review P1)。
    # 历史行的 title_embedding 可能是 NULL —— 当初 commit 时 embedding 服务不可用
    # 就会这样, 而且没有回填路径。那种情况下 new_vecs 非空、看起来正常, 但每一条
    # 历史都在下面被跳过, 标题语义这一路【实际没跑】,
    # 却报 semantic_degraded=false 让调用方以为全套硬闸都过了。
    hist_missing_vec = hist_size - hist_with_vec
    semantic_ran = bool(new_vecs) and (hist_with_vec > 0 or hist_size == 0)
    degraded = not semantic_ran or hist_missing_vec > 0
    if degraded:
        logger.warning("semantic dedup degraded (project=%s): new_vecs=%s "
                       "history_with_vec=%d/%d",
                       project_id, bool(new_vecs), hist_with_vec, hist_size)

    results = []
    contain_ran = False          # 有没有任何一篇真的跑过包含度这一路
    for i in range(len(titles)):
        h = hist(i)
        best_sim, sim_hit, best_j, j_hit, exact, open_hit = (
            h.sim, h.sim_title, h.jac, h.j_title, h.open_exact, h.open_title)
        best_c, c_hit, c_sample = h.contain, h.contain_title, h.contain_sample

        # ⚠️ 每个信号的最佳命中【各记各的】, 且各自记清楚是本批内还是历史。
        # 原来共用一个 intra 变量, 只要任一信号的最佳命中来自本批内就无条件
        # 认定"撞的是本批内那篇" —— 但真正让 verdict 判死的可能是另一个信号、
        # 而那个信号的命中在历史里。于是报出去的 collided_with 是一条根本没
        # 引发拒绝的稿子, 人对着它改, 改完还是过不了。(codex review)
        # hit = (标题, 归属)  归属 ∈ {"本批内", "历史"}
        sim_best = (sim_hit or "", "历史") if sim_hit is not None else None
        j_best = (j_hit or "", "历史") if j_hit is not None else None
        open_best = (open_hit or "", "历史") if open_hit is not None else None
        c_best = (c_hit or "", "历史") if c_hit is not None else None

        for k in range(i):   # 本批内互比: 同批两篇撞车同样要拦
            # o_hashes[i] 为空 = 这篇没有正文开头。空 == 空【不算撞车】——
            # 否则两篇 title-only 的稿子会被判成开头完全一致(见
            # fp.opening_hash 的说明)。
            if o_hashes[i] and o_hashes[i] == o_hashes[k]:
                exact, open_best = True, (titles[k], "本批内")
                break
            # 同上: 本批内两篇也都是 sketch, 而且同一批里长短稿并存很常见
            # (一篇 300 字的短图文 + 一篇 1500 字的长测评)。(审计 COR-014)
            jj, cc, ss = fp.sketch_overlap(grams[i], grams[k])
            if jj > best_j:
                best_j, j_best = jj, (titles[k], "本批内")
            # 样本量先过闸再比大小 —— 与上面对历史稿那一路同一条理由。
            if ss >= fp.CONTAIN_MIN_SAMPLE and cc > best_c:
                best_c, c_best, c_sample = cc, (titles[k], "本批内"), ss
            if new_vecs:
                s = dedup.cosine_similarity(new_vecs[i], new_vecs[k])
                if s > best_sim:
                    best_sim, sim_best = s, (titles[k], "本批内")

        contain_ran = contain_ran or c_sample >= fp.CONTAIN_MIN_SAMPLE
        status, reason, which = fp.deciding_signals(
            best_sim, exact, best_j, best_c, c_sample)
        by_signal = {"opening": open_best, "title": sim_best,
                     "ngram": j_best, "contain": c_best}
        # 按 verdict 实际依据的信号取命中; 两个弱信号并列时取第一个(它排在
        # reason 的最前面, 与用户读到的解释对得上)。pass 时没有依据信号,
        # 退回"最强的那个"只为让人知道最接近的是什么, 并注明是参考。
        hit = next((by_signal[s] for s in which if by_signal.get(s)), None)
        informational = False
        if hit is None:
            hit = open_best or sim_best or j_best or c_best
            informational = hit is not None
        collided, scope = hit if hit else ("", "")
        row = {
            "index": i, "title": titles[i], "status": status, "reason": reason,
            "collided_with": collided,
            "collided_scope": scope,
            "decided_by": which,
            "signals": {"title_similarity": round(best_sim, 4),
                        "opening_exact_match": exact,
                        "body_ngram_jaccard": round(best_j, 4),
                        # 审计 COR-014。sample 一并报出去 —— 它低于
                        # CONTAIN_MIN_SAMPLE 时 containment 这个数字**没有参考
                        # 价值**, 不报的话看的人会拿一个纯噪声当结论。
                        "body_ngram_containment": round(best_c, 4),
                        "containment_sample": c_sample},
        }
        if informational:
            row["collided_note"] = ("这条判定为通过, collided_with 只是最接近的"
                                    "参照, 不是拦下它的原因。")
        results.append(row)

    summary = {
        "total": len(results),
        "pass": sum(1 for r in results if r["status"] == "pass"),
        "warn": sum(1 for r in results if r["status"] == "warn"),
        "reject": sum(1 for r in results if r["status"] == "reject"),
        "history_size": hist_size,
        "history_with_embedding": hist_with_vec,
        "history_missing_embedding": hist_missing_vec,
        "history_truncated": hist_truncated,
        "semantic_degraded": degraded,
        "containment_checked": contain_ran,
    }
    if hist_size and not contain_ran:
        # 审计 COR-014: 包含度这一路一次都没真的发言。两种可能, 都要说出来 ——
        # 静默不发言正是这条 finding 本身的形态(有个信号没跑, 而返回值看起来
        # 一切正常)。
        summary["containment_skipped_warning"] = (
            "「短稿照搬长稿」这一路本次没有生效: 要么草稿太短、与历史稿的长度差"
            "太大, 有效样本量不够(低于 "
            f"{fp.CONTAIN_MIN_SAMPLE} 就不发言, 免得噪声误杀); 要么服务端只跑了 "
            "migrations/004 而没跑 005(老版 RPC 回不出包含度)。"
            "另外三路照常跑了, 但整段照搬一篇长稿的重复可能漏掉 —— 要告诉用户。")
    if hist_truncated:
        # history_size 报的是【实际比过的条数】, 但项目的历史比这更多。
        # 不说出来的话, "比对全量历史" 就成了一句假话。
        # 下推路径不会走到这里(库里是全量比的, hist_truncated 恒为 False),
        # 只有 migrations/004 没跑、退回 Python 路径时才可能命中。
        summary["history_truncated_warning"] = (
            f"这个项目的历史指纹超过 {hist_size} 条, 本次只比了最近的这些。"
            "更老的稿子没参与查重, 跟它们的重复不会被发现 —— 要告诉用户。"
            "长期解法是把比对下推到数据库(migrations/004, 见 docs/deskcore.md §7)。")
    # 指纹库是空的但项目其实有历史 = 没回填。硬闸背后什么都没有, 必须说出来,
    # 不能让调用方以为"比对了全量历史然后没撞车"。
    if not hist_size:
        summary["empty_history_warning"] = (
            "指纹库里这个项目一条历史都没有。如果这不是全新项目, 说明【还没回填】——"
            "本次查重实际只在本批内部比对, 跟历史稿的重复不会被发现。"
            "跑 `python -m deskcore.cli backfill --project <id>` 补上。")

    if degraded:
        why = []
        if not new_vecs:
            why.append("本批标题算不出向量(GOOGLE_API_KEY 未配或 embedding 调用失败)")
        if hist_missing_vec:
            why.append(f"{hist_missing_vec}/{hist_size} 条历史没有 title_embedding"
                       f"(当初 commit 时 embedding 不可用, 且没有回填路径 —— "
                       f"跑 `python -m deskcore.cli backfill --project <id>` 可补)")
        summary["degraded_note"] = (
            "标题语义查重【没有完整跑】: " + "; ".join(why) +
            "。确定性信号(开头精确 + 四字串重合)仍然有效, 但同角度换说法的标题"
            "可能漏过 —— 要告诉用户。")
    return {"results": results, "summary": summary}


# (这里原来有个 _placeholder_version_id: 没有真实 version_id 时按 angle_key 的
#  sha256 造一个确定性假 UUID, 只为把 angle_ledger.consumed_version_id 置成非
#  NULL。commit_drafts 现在给每条没带 id 的稿子都造真 id 并建出 versions 行,
#  那个分支再也走不到 —— 删掉而不是留着, 留着的是一段永远不执行的代码加一句
#  "没有真实 version_id 时"的注释, 下一个读的人会以为这种情况还存在。)


# deskcore_commit_fingerprints 成功那一支返回的 status。**改这个字面量等于改
# 一份跨语言契约**: SQL 侧写什么、这里数什么, 必须是同一个词。
#
# 之所以要给它一个名字: migrations/005 曾把 SQL 侧从 'inserted' 改成 'written',
# 而这边照旧只数 'inserted' —— 后果不是报错, 是每次成功入库都报 written=0、
# 角度台账一条都不销账(于是同一个坐标可以被无限次抽到)。全程没有任何异常。
# 现在 tests/sql_parity_check.py 拿这个常量去比对 SQL 的真实返回值。
COMMIT_STATUS_INSERTED = "inserted"


def commit_drafts(client, project_id: str, drafts: list[dict],
                  *, user_id: str | None = None) -> dict:
    """定稿入库: 写指纹(同一事务内重查) + 给坐标销账。

    只收真正定稿的 —— 指纹库脏了(把废稿也记进去)会让后续正常选题被误杀。

    ⚠️ 入库【会再查一次重】。check_drafts 和本调用之间可能有别人先 commit 了
    撞车的稿子(两人各自 check 时看到的是同一份旧指纹集), 那条竞态窗口只能在
    写入的同一个事务里关掉。被判撞车的条目【不入库】, 在返回值的 rejected 里
    列出来, 调用方要让用户重写。

    ── 2026-08-25: 入库的稿子现在**有身份** ────────────────────────────
    真的入了库的那几条会同时建出 ``batches`` / ``items`` / ``versions`` 行,
    返回值带 ``batch_id`` 和 ``version_ids``。在此之前写作台的稿子只有指纹、
    没有 version_id, 于是:

      · 回填(走 items × versions)看不到它们, 欠费期间缺的向量补不回来;
      · 导出的 lineage 没有 version_id 可写, 而 Truth Vault 的
        ``v_model_comparison`` 正是 JOIN 在 ``autowriter.versions.id`` 上 ——
        "写作台写的稿子发出去爆没爆"在数据上根本问不出来。

    ⚠️ 建出来的 item 是 ``pending``、**不盖任何决策戳**。理由见
    ``store.DESKCORE_ITEM_STATUS`` —— 标 approved 会让每条定稿变成一条伪造的
    人工评价灌进 TV 的评估模型。
    """
    assert_project_access(client, project_id, user_id=user_id)   # 审计 COR-015
    if not drafts:
        return {"written": 0, "consumed_angles": 0, "rejected": []}

    titles = [(d.get("title") or "").strip() for d in drafts]
    # ⚠️ embeddings_available() 判的是【客户端对象能不能建起来】(有 key + SDK
    # 装了), 不是调用能不能成功, 而且那个 client 是进程级单例、启动时就缓存了。
    # 所以 key 欠费/被封/配额用尽之后它照样返回 True, 真正失败的是下面这次
    # embed_texts —— 它 catch 住异常返回 None。
    # 这两者必须分开记: configured 为真而 vecs 为空 = 【本该有向量却没拿到】,
    # 那是故障不是"没配"。不区分的话, 欠费那几天入库的稿子会安静地只有确定性
    # 指纹, 查重从此对那批内容有个洞, 不报错、也没人知道。
    #
    # (这里原来还写着"而且补不回来 —— backfill 走 items×versions, WorkBuddy
    #  写的稿子根本不在 autowriter.versions 里"。下面建身份那一步之后不再成立:
    #  这些稿子现在有真的 versions 行, backfill 扫得到。)
    embed_configured = dedup.embeddings_available()
    vecs = dedup.embed_texts(titles) if embed_configured else None
    embed_failed = embed_configured and not vecs
    bodies = [d.get("body") or "" for d in drafts]

    # ── 入库自带闸(2026-09-17)────────────────────────────────────────
    # 先跑一遍和 check_drafts **同一套**判定, 判 reject 的不进 RPC。理由见
    # _gate 的说明: 闸不该依赖模型记得先调 check_drafts。这里多一次历史比对
    # (一次 RPC), 换来的是"跳过 check 直接 commit"这条路从此进不了重复的稿子。
    # 出错照样上抛 —— 和 check_drafts 一样, 查重挂了不能当作通过。
    gate = _gate(client, project_id, titles, bodies, vecs)
    pre_rejected = {r["index"]: r for r in gate["results"]
                    if r.get("status") == "reject"}
    survivors = [i for i in range(len(drafts)) if i not in pre_rejected]

    # ── 先把 version_id 定下来 ──────────────────────────────────────────
    # 顺序是有讲究的: **先造 id 并写进指纹, 事后才建 versions 行**。
    #
    # 反过来(先建 versions 再写指纹)的话, 被查重判撞车的那几条会留下没有指纹的
    # 孤儿 item —— 它们会出现在审核页、进正例池、被回填扫到, 而对应的稿子其实
    # 根本没有交付。
    #
    # 现在这个顺序的失败模式是另一头: 指纹写进去了、身份没建成, 于是指纹的
    # version_id 指向一个不存在的行。那只是**退回到今天的状态**(lineage 断掉),
    # 查重一点不受影响 —— 而且下面会明说。两个方向的坏, 这个可逆。
    #
    # 调用方已经带了 version_id 的(稿子是 UI 生成的, 库里本来就有那一版)照旧用
    # 它自己的, 不重新造、也不会再建一遍身份。
    minted_ids: dict[int, str] = {}
    for i, d in enumerate(drafts):
        if not d.get("version_id"):
            minted_ids[i] = str(uuid.uuid4())

    rows = []
    for i in survivors:
        d = drafts[i]
        body = bodies[i]
        emb = vecs[i] if vecs and i < len(vecs) else None
        rows.append({
            "version_id": d.get("version_id") or minted_ids.get(i),
            "title": titles[i],
            "opening": fp.opening_of(body),
            # RPC 侧按 text 转 vector, 这里给 pgvector 的字面量形式
            "title_embedding": ("[" + ",".join(repr(float(x)) for x in emb) + "]") if emb else None,
            "embedding_model": dedup.EMBEDDING_MODEL if emb else None,
            "opening_hash": fp.opening_hash(body),
            "ngram_hashes": fp.ngram_hashes(body),
            "angle_key": d.get("angle_key"),
        })

    # 闸前就判死的那几条: 说清是哪一路信号、撞的是本批内还是历史。
    rejected: list[dict] = [
        {"index": i, "title": titles[i],
         "collided_with": r.get("collided_with") or "",
         "collided_scope": r.get("collided_scope") or "",
         "decided_by": r.get("decided_by"),
         "reason": r.get("reason") or "入库前判定与已有稿件重复",
         "gate": "pre_commit"}
        for i, r in sorted(pre_rejected.items())]
    # ⚠️ RPC 回的 idx 是**幸存者列表里的下标**, 不是调用方列表的。下面凡是把
    #    idx 翻回调用方视角的地方都要过 survivors[...]。
    if survivors:
        outcome = store.commit_fingerprints_atomic(
            client, project_id, rows, user_id, fp.NGRAM_JACCARD_HARD,
            # 阈值只在 fingerprint.py 里定义一处 —— SQL 里的 DEFAULT 只是兜底,
            # 真正生效的是这里传下去的值。两边写死两份就迟早对不上。(审计 COR-014)
            contain_hard=fp.NGRAM_CONTAIN_HARD,
            contain_min_sample=fp.CONTAIN_MIN_SAMPLE)
    else:
        outcome = []          # 全被闸前拦下, 没东西可写, 也别去碰 RPC

    atomic = outcome is not None
    rpc_anomalies = 0
    if atomic:
        written = sum(1 for o in outcome
                      if o.get("status") == COMMIT_STATUS_INSERTED)
        # ⚠️ idx 越界的回执**不许抛**: 指纹这时已经写进去了, 抛出去调用方会重试,
        #    重试会撞上自己刚写的指纹 —— 一次故障变成一句"你的稿子重复了"。
        #    记日志、数出来、报给调用方, 但不中断。(code review 2026-09-17)
        valid = []
        for o in outcome:
            idx = o.get("idx")
            if isinstance(idx, int) and 0 <= idx < len(survivors):
                valid.append(o)
            else:
                rpc_anomalies += 1
                logger.error("commit_drafts: RPC 回执 idx=%r 越界(幸存者 %d 条, "
                             "project=%s) —— 跳过这一行", idx, len(survivors), project_id)
        outcome = valid
        for o in outcome:
            if o.get("status") == "rejected":
                orig = survivors[o["idx"]]
                rejected.append({
                    "index": orig,
                    "title": titles[orig],
                    "collided_with": o.get("collided_with") or "",
                    "collided_scope": "历史",
                    "reason": o.get("detail") or "与库中已有稿件重复",
                    "gate": "atomic_recheck",
                })
        rejected.sort(key=lambda r: r["index"])
    else:
        # RPC 不在: 降级直插, 并明确标出这次没有关掉竞态窗口。
        payload = []
        for k, r in enumerate(rows):
            orig = survivors[k]
            payload.append({
                "project_id": project_id, "user_id": user_id,
                "version_id": r["version_id"], "title": r["title"],
                "opening": r["opening"],
                "title_embedding": vecs[orig] if vecs and orig < len(vecs) else None,
                "embedding_model": r["embedding_model"],
                "opening_hash": r["opening_hash"],
                "ngram_hashes": r["ngram_hashes"], "angle_key": r["angle_key"],
            })
        written = store.write_fingerprints(client, payload)

    # 只给真的入了库的坐标销账 —— 被拒的那条角度还没产出成稿, 不该占坑。
    inserted_idx = ({survivors[o["idx"]] for o in outcome
                     if o.get("status") == COMMIT_STATUS_INSERTED}
                    if atomic else set(survivors))

    # ── 给真的入了库的那几条建身份 ──────────────────────────────────────
    # 只建 inserted 的: 被判撞车的那几条没有交付, 不该在 items 里留一行。
    # mint_draft_identity 不抛: 指纹已经进库了, 这次 commit 的**主要目的**(让这些
    # 稿子参与以后的查重)已经达成。把整个调用报成失败会让调用方去重试, 而重试会被
    # 自己刚写进去的指纹判成撞车 —— 一次故障变成一句"你的稿子重复了", 现场完全对
    # 不上。它半途失败时会把**已经建成的那部分**连同 error 一起回来。
    minted = {"batch_id": None, "versions": {}, "error": None}
    to_mint = [
        {"version_id": minted_ids[i], "title": titles[i],
         "body": drafts[i].get("body") or "",
         "keywords": drafts[i].get("keywords") or []}
        for i in sorted(inserted_idx) if i in minted_ids
    ]
    if to_mint:
        minted = store.mint_draft_identity(
            client, project_id, str(user_id), "", to_mint)
    identity_error = minted.get("error")

    consumed = 0
    attempted = 0
    for i, d in enumerate(drafts):
        if i not in inserted_idx:
            continue
        key = d.get("angle_key")
        if not key:
            continue
        attempted += 1
        # 台账销账用**真的** version_id。以前这里塞的是一个按 angle_key 哈希出来的
        # 假 UUID(见上面删掉的 _placeholder_version_id) —— consumed_version_id 非
        # NULL 就够避重用了, 但那个 id 指不到任何一行, 没法回答"这个角度产出的那篇
        # 后来怎么样了"。
        vid = d.get("version_id") or minted_ids[i]
        if store.consume_angle(client, project_id, key, vid):
            consumed += 1

    # 入了库却没带 angle_key 的: 台账没法给它们销账, 同一个故事下一批还可能被
    # 抽到 —— 而那种重复(同题重写)指纹闸抓不到。途鸽 09-10 的 66 条里 49 条是
    # 这么进来的, 当天四个标题各入库两次。RPC 对空 angle_key 一声不吭地照收,
    # 所以只能在这里数出来说给调用方听。
    unattributed = sum(1 for i in inserted_idx if not drafts[i].get("angle_key"))
    out = {"written": written, "consumed_angles": consumed,
           "unattributed": unattributed,
           "embedded": bool(vecs), "rejected": rejected,
           "atomic_recheck": atomic,
           "gate_summary": gate["summary"],
           "embedding_model": dedup.EMBEDDING_MODEL if vecs else None,
           # 这次建出来的身份。导出要用 version_id, 所以直接回给调用方 ——
           # 不然它得再查一次才知道自己刚提交的稿子叫什么。
           #
           # ⚠️ 只报**真的建出了行**的那些(拿 minted["versions"] 过一遍),
           #    不是"我本来打算用这些 id"。建失败时报出去的 id 会让调用方拿它
           #    去导出, 而 TV 那边 JOIN 不到任何东西 —— 那比不报更坏。
           "batch_id": minted.get("batch_id"),
           "version_ids": [minted_ids[i] for i in sorted(inserted_idx)
                           if minted_ids.get(i) in minted.get("versions", {})]}
    if identity_error:
        # 说清楚**丢的是哪几条**: 查重没事, 断的是"发出去之后能不能归因回来"。
        # ⚠️ 报的是**实际没建成的条数**, 不是 len(to_mint) —— 半途失败时前面几条
        #    是真建成了的, 说"全都没建成"会让人去重做已经做完的事。
        done = len(out["version_ids"])
        out["identity_warning"] = (
            f"{len(to_mint)} 条稿子的指纹都入了库, 但只有 {done} 条建成了 "
            f"items/versions({identity_error})。查重不受影响; 受影响的是导出的 "
            f"lineage —— 没建成的那 {len(to_mint) - done} 条不在 version_ids 里, "
            "导出时不会有 version_id, Truth Vault 那边归因不回来"
            "(v_model_comparison 就 JOIN 在这个 id 上)。"
            + (f"已建成的那 {done} 条照常可以 export_drafts(batch_id="
               f"{out['batch_id']})。" if done else "")
            + "服务端日志有堆栈。")
    if embed_failed and written:
        # 配了 embedding 却没拿到向量 = 故障(欠费/配额/网络), 不是"没配"。
        # 必须当场说, 让人决定是先修 key 再 commit, 还是接受这批只有确定性指纹。
        # (2026-08-25 起这几行**补得回来**了 —— 稿子有了 versions 行, backfill
        #  和 reembed 都扫得到。文案里保留 reembed 的指路, 它更直接。)
        out["embedding_warning"] = (
            f"配了 embedding 但本次取向量失败, {written} 条是【没有标题向量】入库的。"
            "它们以后只参与确定性查重(开头精确 + 四字串重合), 同角度换说法的标题"
            "比不出来。常见原因是 key 欠费/配额用尽/网络不通。"
            "修好 key 之后用 `python -m deskcore.cli reembed --project <id>` 补齐。")
    if attempted and consumed < attempted:
        # consume_angle 现在会在"台账里根本没这一行"时返回 False(见 store 里的
        # 说明)。差额必须说出来 —— 这些坐标下一批还会被抽到, 悄悄少算等于
        # 避重失效了却没人知道。
        out["angle_ledger_warning"] = (
            f"{attempted - consumed}/{attempted} 个坐标没能在台账上销账(多半是发牌时"
            "台账没写进去)。这些坐标下一批可能被重复抽到, 服务端日志有明细。")
    if rpc_anomalies:
        out["rpc_anomalies"] = rpc_anomalies
        out["rpc_warning"] = (
            f"入库 RPC 回了 {rpc_anomalies} 行对不上号的回执(idx 越界), 已跳过。"
            "指纹可能已写入但这几条的身份/销账没做 —— 服务端日志有明细, 别重试, "
            "重试会撞上自己刚写的指纹。")
    if unattributed:
        out["unattributed_warning"] = (
            f"{unattributed}/{written} 条入库的稿子没带 angle_key, 台账无法给它们"
            "销账 —— 这些角度下一批还会被抽到, 同一个故事会被讲第二遍, 而那种"
            "重复查重闸抓不到。成批写的稿子每条都要带 draw_angles 分给它的 "
            "angle_key。")
    if rejected:
        pre = sum(1 for r in rejected if r.get("gate") == "pre_commit")
        race = len(rejected) - pre
        parts = []
        if pre:
            parts.append(f"{pre} 条在入库前的判定里就被拦下(和 check_drafts 同一套"
                         "闸 —— 说明这几条没过 check, 或者 check 之后又改回去了)")
        if race:
            parts.append(f"{race} 条在写入时被拦下(你 check 之后、commit 之前有人"
                         "先提交了撞车的稿子)")
        out["note"] = ("; ".join(parts)
                       + "。这几条没有入库, 要重写后重新走 check_drafts, "
                         "不能当作已交付。")
    if not atomic:
        out["warning"] = ("本次入库没有做原子重查(deskcore_commit_fingerprints RPC "
                          "不存在, migrations/001 可能没跑)。并发 check/commit 时"
                          "可能有撞车的稿子一起进库。")
    return out


# 人审只认这两个结论。**刻意不含 pending** —— 迭代后重置回待审是
# `DecisionSource.SYSTEM`(见 app.py 那处注释), 既不是审稿也不是检测, 让它从
# 人审入口进来就等于伪造一条人工决策。
HUMAN_DECISIONS = ("approved", "needs_revision")

MAX_REVIEW_DRAFTS = 200


def _is_uuid(value: str) -> bool:
    """格式合法的 UUID 才敢送进 ``.in_()``。

    ``versions.id`` 是 uuid 列。PostgreSQL 在**解析**阶段就会拒掉整条查询
    (`invalid input syntax for type uuid`), 不是跳过那一个值 —— 所以一个打错的
    id 会连累同一批里所有好的。
    """
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def review_drafts(client, project_id: str, decisions: list[dict],
                  user_id: str | None = None) -> dict:
    """给已定稿的稿子盖一枚**真实的人工审核决策**。

    ── 为什么非有不可(2026-09-16 评测 AW-01)─────────────────────────────
    ``commit_drafts`` 建的 item 是 ``pending`` 且不盖任何决策戳 —— 那是对的,
    理由见 ``store.DESKCORE_ITEM_STATUS`` 上面那整段: 定稿不等于审核, 把每次
    commit 写成 approved 会让「机器判定被当人工反馈」这个已经修过一次的 bug
    从新入口重犯一次, 而且灌进去的是**清一色正例**。

    但在此之前, 写作台这条路**根本没有**下一步: 只用它写稿、定稿、导出的团队
    永远产不出一条人工审核决定, 于是 TV 那边按
    ``status in ('approved','needs_revision')`` 捞行时一条也捞不到。缺的不是
    「把 pending 改成 approved」, 是**一个真的有人点过的动作**。

    这个函数就是那个动作, 三条纪律:

      1. **reviewer 恒为调用者**, 签名里没有 reviewer 参数。审计 COR-004 治的
         正是"把 owner 当 reviewer", 留个口子等于把它请回来。
      2. **只认 approved / needs_revision**, 见 ``HUMAN_DECISIONS``。
      3. **打回和通过同等公民**。一个只能点通过的审核入口产出的仍然是清一色
         正例, 与不做无异。

    ⚠️ 不设 ``best_version_id``。「选为最佳」在 UI 里是**独立于审稿**的动作
    (app.py 那处注释: 走 update_item_status 会给它盖上一枚人工决策戳, 把出处
    数据自己污染掉)。写作台的 item 每条只有一个版本, 代表版本自然落在它身上。

    返回 ``{"reviewed", "results"}``; 每条 result 带 ``outcome``:
      · ``recorded``   —— 决策已落库。附 ``previous_status`` 与
                          ``previously_decided_by`` —— 后者是**上一个决定的来源**
                          (``db.DecisionSource`` 的四个值), 只有 ``human`` 才代表
                          之前真有人审过; 机器打回的(``auto_hard_rule`` /
                          ``auto_dedup``)和系统置位(``system``)都不是
      · ``not_found``  —— 这个 version_id 不在本项目里(或根本不存在)
      · ``invalid``    —— 入参不合法: decision 不是那两个值之一, 或 version_id
                          不是合法 UUID(``detail`` 里说是哪一种)
      · ``duplicate``  —— 同一条稿子在本批里已经被决定过, **这条没生效**
      · ``failed``     —— 写库失败, ``detail`` 里是原因

    **部分失败照样回报已经成功的那几条**, 理由同 ``store.mint_draft_identity``:
    行已经改了而调用方以为没改, 比报一条失败更难查。
    """
    assert_project_access(client, project_id, user_id=user_id)   # 审计 COR-015
    if not decisions:
        return {"reviewed": 0, "results": []}
    if len(decisions) > MAX_REVIEW_DRAFTS:
        raise ValueError(
            f"一次最多审 {MAX_REVIEW_DRAFTS} 条, 收到 {len(decisions)} 条。"
            "分批调用 —— 单次太大时部分失败的定位成本会陡增。")

    wanted = [str(d.get("version_id") or "") for d in decisions]
    # ⚠️ 只把**格式合法**的 id 送进 .in_()。``versions.id`` 是 uuid 列, 混进一个
    # 手抖打错的值, PostgreSQL 会用
    # `invalid input syntax for type uuid` 拒掉**整条查询** —— 于是同一批里
    # 二十条好的也一起挂, 而这个函数的契约明写着部分失败要逐条报。
    # (codex review P2; 在真 PG 16.13 上复现过)
    known = store.items_for_versions(
        client, project_id, [v for v in wanted if _is_uuid(v)])

    results: list[dict] = []
    reviewed = 0
    # item_id → 本批里**先**决定它的那个 version_id。同一条稿子被点两次时,
    # 静默让最后一次生效是最糟的选择: 两条都报 recorded, 而第二条的
    # previous_status 取自更新前的快照、已经是错的, 把覆盖藏了起来。
    decided_items: dict[str, str] = {}
    for d in decisions:
        vid = str(d.get("version_id") or "")
        decision = str(d.get("decision") or "")
        row = {"version_id": vid, "decision": decision}

        if decision not in HUMAN_DECISIONS:
            results.append({**row, "outcome": "invalid",
                            "detail": f"decision 只能是 {' / '.join(HUMAN_DECISIONS)}"})
            continue
        if not _is_uuid(vid):
            results.append({**row, "outcome": "invalid",
                            "detail": "version_id 不是合法 UUID"})
            continue
        hit = known.get(vid)
        if hit is None:
            results.append({**row, "outcome": "not_found",
                            "detail": "这个 version_id 不在本项目里"})
            continue
        prior = decided_items.get(hit["item_id"])
        if prior is not None:
            # 拒绝而不是"最后一条生效": 同一条稿子在一批里被点两次是调用方的
            # 错, 悄悄取最后一条会让它永远发现不了。
            results.append({**row, "outcome": "duplicate",
                            "item_id": hit["item_id"],
                            "detail": f"同一条稿子本批里已经被 {prior} 决定过了, "
                                      f"这条没生效 —— 想改判就单独再调一次"})
            continue

        try:
            db.update_item_status(client, hit["item_id"], decision,
                                  source=db.DecisionSource.HUMAN,
                                  reviewer_id=user_id)
        except Exception as exc:                 # noqa: BLE001
            logger.exception("review_drafts 写库失败 (version=%s)", vid)
            results.append({**row, "outcome": "failed",
                            "item_id": hit["item_id"],
                            "detail": f"{type(exc).__name__}: {exc}"[:300]})
            continue

        reviewed += 1
        decided_items[hit["item_id"]] = vid
        results.append({**row, "outcome": "recorded",
                        "item_id": hit["item_id"],
                        "previous_status": hit.get("status"),
                        "previously_decided_by": hit.get("decision_source")})

    return {"reviewed": reviewed, "results": results}


MAX_EXPORT_DRAFTS = 200


def export_drafts(client, project_id: str, *, batch_id: str | None = None,
                  version_ids: list[str] | None = None,
                  user_id: str | None = None) -> dict:
    """把定稿导成一个可以粘进飞书表的 Excel。

    ── 这一步在闭环里的位置 ────────────────────────────────────────────
    写作台 → 飞书表 → Truth Vault → (指标回流) → 写作台。第一段一直是**手工**
    的: 运营把稿子粘进飞书表。粘的时候如果不带 lineage, TV 就只知道"有这么一条
    笔记", 不知道它是谁写的哪一版 —— ``v_model_comparison`` 那个 view 就是这么
    长期查出空集的。

    所以这里导出的表, 除了内容列还带 ``exporter.LINEAGE_COLUMNS`` 那六个**命名
    可见列**(列名由 TV 定)。运营整片选中粘贴, lineage 就跟着过去了。

    ⚠️ 飞书表那边要**先建好这六列**, 否则粘过去是六列无处安放的数据。列名和
       字段类型见 truth-vault 的 ``docs/11-feishu-table-setup.md``。

    ── 为什么返回 base64 而不是文件路径 ────────────────────────────────
    deskcore 是个远端服务, 调用方读不到它的文件系统。给一个下载 URL 就得配一套
    单独的签名/鉴权 —— 而本仓的审计史上一半的坑都是"半套鉴权"。base64 让调用方
    自己落盘, 不新增任何鉴权面。

    正文全文本来就在调用方手里(稿子是它写的), 所以真正的额外开销只有 xlsx 的
    封装。``preview`` 只回标题和 id, 够核对导的是不是那一批, 不重复正文。
    """
    assert_project_access(client, project_id, user_id=user_id)
    if not batch_id and not version_ids:
        raise ValueError(
            "要导哪些稿子? 给 batch_id(commit_drafts 的返回值里有)或者 version_ids。"
            "不给的话只能靠猜, 而猜错了导出的是别的批次 —— 那会把错的 lineage "
            "粘进飞书表, 比导不出来更难查。")

    items = store.drafts_for_export(
        client, project_id, batch_id=batch_id, version_ids=version_ids,
        limit=MAX_EXPORT_DRAFTS)
    if not items:
        # 键的形状和成功那一支保持一致 —— 调用方不该为了空结果写第二套解析。
        return {"count": 0, "columns": [], "filename": None, "xlsx_base64": None,
                "preview": [], "missing_version_ids": sorted(version_ids or []),
                "truncated": False, "exported_at": None,
                "note": ("没找到可导的稿子。要么 batch_id / version_ids 不属于这个"
                         "项目, 要么那一批还没 commit_drafts —— 只有入了库的稿子"
                         "才有身份可导。")}

    # ── 少导了就必须说 ──────────────────────────────────────────────────
    # 本仓的审计里"静默截断"是反复出现的一类(COR-006 / ROB-011 都是它), 而导出
    # 这条路上它尤其阴: 少导几条 = 那几篇稿子发出去之后归因不回来, 而返回值看起来
    # 完全正常, 没有任何人会发现。所以两种少法都点名:
    #
    #   · 点名要了某些 version 却没找到 —— id 打错、不属于这个项目、或者还没 commit;
    #   · 命中数顶到上限 —— 后面可能还有, 这一次没导全。
    missing = sorted(set(version_ids or []) - {r["version_id"] for r in items})
    truncated = len(items) >= MAX_EXPORT_DRAFTS

    exported_at = datetime.now().isoformat(timespec="seconds")
    blob = exporter.build_combined_excel(items, exported_at=exported_at)
    stamp = exported_at.replace("-", "").replace(":", "").replace("T", "_")[:13]

    notes = ["把 xlsx_base64 解码写成 filename 那个文件, 打开、整片选中、粘进飞书表。"
             "⚠️ 飞书表要先有 columns 里的那几列, 列名逐字相同 —— 对不上的列会让 "
             "Truth Vault 把整行 quarantine。"]
    if missing:
        notes.append(
            f"⚠️ 点名的 {len(missing)} 个 version_id 没找到, **不在这个表里**: "
            f"{missing[:10]}{' …' if len(missing) > 10 else ''}。"
            "多半是 id 打错、不属于这个项目、或者那几条还没 commit_drafts。")
    if truncated:
        notes.append(
            f"⚠️ 命中数顶到上限 {MAX_EXPORT_DRAFTS} 条, 后面可能还有没导出来的。"
            "分批用 version_ids 点名导, 别把这一次当全量。")

    return {
        "count": len(items),
        "missing_version_ids": missing,
        "truncated": truncated,
        # 明着回一份列名: 运营要照着它在飞书表里建列, 而且对不上会整行被
        # quarantine(TV 的 D-021)。让它出现在返回值里, 不必去翻文档。
        "columns": [exporter.CONTENT_HEADER, *exporter.LINEAGE_HEADERS],
        "filename": f"deskcore_{stamp}.xlsx",
        "xlsx_base64": base64.b64encode(blob).decode("ascii"),
        "preview": [{"title": r["title"], "version_id": r["version_id"]}
                    for r in items],
        "exported_at": exported_at,
        "note": " ".join(notes),
    }


def reembed_fingerprints(client, project_id: str, *, chunk: int = 50,
                         progress=None) -> dict:
    """给指纹库里缺标题向量的行补上向量。

    ⚠️ 【故意没有归属校验】(审计 COR-015)。它和 backfill 一样不是 MCP 工具 ——
    只有 CLI 能调, 而跑 CLI 的人手里握着 service_role key(等价于直连库)。在这儿
    加一道 owner 校验挡不住任何人, 只会挡住"帮同事补一下向量"这类正当运维,
    还会给人一种"运维路径也隔离了"的错觉。隔离边界在 MCP/REST 那一面。

    **和 backfill 是两件事, 别混。** backfill 把 autowriter.versions 里的历史
    成稿【搬进】指纹库; reembed 修的是【已经在指纹库里、但当时没取到向量】的行。
    后者 backfill 够不着 —— 它扫的是 items × versions, 而 WorkBuddy 写的稿子
    version_id 为空、不在那张表里。

    典型触发场景两个:

      · embedding 的 key 欠费/配额用尽那几天照常 commit 了稿子, 它们只有确定性
        指纹。补上 key 之后跑这个。
      · **换了 embedding 模型**(2026-08-26 就换过一次)。老模型产的向量不能跟新
        向量比 —— 跨模型算余弦出来的数是垃圾且不报错 —— 所以它们被比对排除,
        必须用新模型重算才能重新参与。这一路是 codex review · #65 P1 补的:
        原来只扫 ``title_embedding IS NULL``, 于是换模型之后老行既进不了比对、
        又永远不会被重算, 卡在一个**没有出口**的状态里。
    """
    if not dedup.embeddings_available():
        return {"error": "embedding 不可用(GOOGLE_API_KEY 未配或 SDK 缺失), 无法补向量",
                "fixed": 0, "pending": None}

    rows = store.fingerprints_needing_vectors(client, project_id,
                                              dedup.EMBEDDING_MODEL)
    if not rows:
        return {"pending": 0, "fixed": 0, "failed": 0,
                "note": "没有需要重算向量的行, 不用补。"}

    # 分别报出来, 因为它们意味着不同的事: 缺向量是"当时没算成", 换模型是"算过
    # 但那一批已经作废"。混成一个数字, 看的人没法判断这次该不该意外。
    absent = sum(1 for r in rows if r.get("_expect") is store.VECTOR_ABSENT)
    stale = len(rows) - absent

    fixed = failed = 0
    for start in range(0, len(rows), chunk):
        part = rows[start:start + chunk]
        vecs = dedup.embed_texts([r.get("title") or "" for r in part])
        if vecs is None:
            failed += len(part)
            logger.warning("reembed: embed_texts failed for chunk at %d", start)
            continue
        for r, v in zip(part, vecs):
            # expect 是读到这行时它的状态 —— set_fingerprint_vector 拿它做 CAS,
            # 免得用更旧的批次盖掉别的进程刚刷好的结果。
            if v and store.set_fingerprint_vector(
                    client, r["id"], v, dedup.EMBEDDING_MODEL,
                    expect=r.get("_expect", store.VECTOR_ABSENT)):
                fixed += 1
            else:
                failed += 1
        if progress:
            progress(min(start + chunk, len(rows)), len(rows))

    out = {"pending": len(rows), "fixed": fixed, "failed": failed,
           "missing_vector": absent, "stale_model": stale,
           "embedding_model": dedup.EMBEDDING_MODEL}
    if stale:
        out["stale_model_note"] = (
            f"{stale} 条是【换模型作废】的旧向量(不是缺向量) —— 它们在重算完成"
            f"之前不参与标题语义比对。当前模型: {dedup.EMBEDDING_MODEL}")
    if failed:
        out["note"] = (f"{failed} 条没补上(embedding 调用失败或行已被改动)。"
                       "可以再跑一次, 已补好的不会重复处理。")
    return out


def recompute_fingerprints(client, project_id: str, *, progress=None) -> dict:
    """按**当前的** ``normalize`` 口径重算确定性指纹。审计 COR-014 的后续。

    ⚠️ 什么时候需要跑: 只在 ``fingerprint.normalize`` 的口径变了之后。存量指纹的
    ``opening_hash`` / ``ngram_hashes`` 是用**当时**的口径算的 —— 口径一变, 新稿
    算出来的四字串就和历史对不上, 查重在过渡期反而更弱, 而且**不报错**。
    ``backfill`` 补不了这个: 它按 ``version_id`` 幂等跳过, 只管"没有的行", 不重算
    已有的行。

    ── 能重算到什么程度 ──────────────────────────────────────────────────
    指纹表**不存正文**(只有 ``title`` 和 ``opening`` 前 25 字), 所以:

      · ``opening_hash`` —— **每一行都能重算**。它本来就是
        ``sha16(normalize(opening))``, 而 ``opening`` 原样存着。这一路是
        "单独就判死"的最强信号, 能全修回来是关键。
      · ``ngram_hashes`` —— 只有 ``version_id`` 非空的行能重算(正文在
        ``autowriter.versions`` 里)。WorkBuddy 经 ``commit_drafts`` 写进来的行
        ``version_id`` 是空的, **正文已经不存在了**, 这一路修不回来。

    返回值里的 ``ngram_unrecoverable`` 就是修不回来的行数。**它不为 0 就要告诉
    用户**: 那些行的四字串仍然是旧口径, 与新稿比对会偏低。想彻底修只能把那些
    稿子重新 commit 一遍。

    幂等: 重复跑是同一个结果(纯函数重算), 只是白写一遍。
    """
    total = recomputed = unrecoverable = 0
    for page in store.fingerprint_pages(client, project_id):
        need_body = [r for r in page if r.get("version_id")]
        bodies = (store.version_bodies(client, [r["version_id"] for r in need_body])
                  if need_body else {})
        for row in page:
            total += 1
            new_open = fp.sha16(fp.normalize(row.get("opening") or "")) \
                if (row.get("opening") or "").strip() else ""
            vid = row.get("version_id")
            body = bodies.get(str(vid)) if vid else None
            if body:
                new_grams = fp.ngram_hashes(body)
            else:
                new_grams = None
                unrecoverable += 1
            store.update_fingerprint_hashes(
                client, row["id"], opening_hash=new_open, ngram_hashes=new_grams)
            recomputed += 1
        if progress:
            progress(recomputed, total)

    out = {"scanned": total, "rewritten": recomputed,
           "ngram_unrecoverable": unrecoverable}
    if unrecoverable:
        out["warning"] = (
            f"{unrecoverable}/{total} 行的正文已经不在库里(version_id 为空, "
            "多半是 WorkBuddy 经 commit_drafts 写进来的), 四字串那一路**没能重算** "
            "—— 它们仍是旧的规范化口径, 与新稿比对会偏低。开头指纹已全部修好。"
            "要彻底修只能把那些稿子重新 commit 一遍。")
    return out


def backfill_fingerprints(client, project_id: str, *, with_embeddings: bool = True,
                          chunk: int = 50, progress=None) -> dict:
    """把项目的历史成稿补进指纹库。**部署后每个项目必跑一次。**

    ⚠️ 【故意没有归属校验】—— 理由同 reembed_fingerprints, 见那边。

    为什么必须有: migrations/001 建的是【空表】, 而 check_drafts 只读这张表、
    只有 commit_drafts 会往里写。不回填的话, 上线第一天号称"比对全量历史"的
    硬闸手里一条历史都没有 —— 几年的老稿子对它全是新的, 重复原样放行。
    (codex review: 这条先被指出过一次, 我加了 CLI 子命令和文档却【没写这个
    函数】, 于是 `deskcore.cli backfill` 每次都 AttributeError —— 等于回填这件
    事从头到尾没存在过, 而文档和 PR 都写着它已经有了。)

    幂等: 已经有指纹的 version_id 跳过, 重跑安全。

    embedding 的取法有讲究: `versions.embedding` 存的就是**标题**向量
    (app.py:520 只对 title 取向量), 与 draft_fingerprints.title_embedding 同义,
    所以历史行有向量就【直接复用】, 不重新调 API —— 几千条重算既慢又费钱。
    只有缺向量的才补算, 且 embedding 不可用时照常写入(向量留空), 确定性信号
    不受影响。
    """
    # 审计 ROB-011: 逐页消费, 不再把整个项目的历史成稿(全文 + 每条 768 个
    # Python float)同时留在内存。5000 条那一档原来是三四百 MB 峰值 —— 容器
    # OOM 重启, 而这是"部署后每个项目必跑一次"的动作。
    #
    # done_ids 仍然一次取全: 它只是一串 UUID(几 MB 顶天), 而它是【幂等性本身
    # 所依赖的那个集合】—— 分页判重会让跨页的重复漏过去(审计 COR-005 修的
    # 就是它被静默截断的那一版)。
    done_ids = store.existing_fingerprint_version_ids(client, project_id)
    can_embed = with_embeddings and dedup.embeddings_available()
    total = already = written = reused = computed = missing = 0
    todo_seen = processed = 0
    pending: list[dict] = []

    def _flush() -> None:
        nonlocal pending, written, reused, computed, missing, processed
        if not pending:
            return
        part, pending = pending, []
        processed += len(part)
        # ⚠️ **能算就全部重算, 不复用 versions.embedding。**(codex review · #65 P1)
        #
        # 原来是"历史行有向量就直接复用, 只给缺的补算", 理由是省钱省时间。那个
        # 理由建立在一个**再也不成立**的假设上: `versions.embedding` 与当前模型
        # 同源。那张表【没有模型标记】, 所以它的来路永远无法证明 —— 2026-08-26
        # 换掉 text-embedding-004 之后, 复用就等于把老模型的向量贴上新模型的标签
        # 写进指纹库, 而跨模型算余弦出来的数是垃圾且【不报错】。
        #
        # 现在的口径: 能调 embedding 就一律用**当前模型**现算(几千条一批, 成本
        # 可以忽略); 调不动才退回复用历史向量, 且那时 embedding_model 记 NULL ——
        # 来路不明就如实说不知道, 于是比对时被排除、reembed 能找到它重算。
        # **宁可少比, 不能拿垃圾数当比过了。**
        need = (list(range(len(part))) if can_embed
                else [i for i, r in enumerate(part) if not r.get("embedding")])
        fresh: list[list[float]] | None = None
        if can_embed and need:
            fresh = dedup.embed_texts([part[i]["title"] for i in need])
            if fresh is None:
                logger.warning("backfill: embed_texts failed for a chunk of %d; "
                               "falling back to legacy vectors (model unknown)",
                               len(need))

        payload = []
        for i, r in enumerate(part):
            # attested = 这个向量确实是【当前模型】产的。只有现算的才算数。
            vec, attested = None, False
            if fresh is not None and i in need:
                pos = need.index(i)
                vec = fresh[pos] if pos < len(fresh) else None
                if vec:
                    computed += 1
                    attested = True
            if not vec and r.get("embedding"):
                # 退路: 现算不成(或整批 embedding 不可用)才复用历史向量。
                vec, attested = r["embedding"], False
                reused += 1
            if not vec:
                missing += 1
            body = r.get("body") or ""
            payload.append({
                "project_id": project_id,
                "user_id": r.get("user_id"),
                "version_id": r.get("version_id"),
                "title": r.get("title") or "",
                "opening": fp.opening_of(body),
                "title_embedding": vec or None,
                # ⚠️ 只有【现算】的才敢写模型名。复用 versions.embedding 时记
                # NULL —— 那张表没有模型标记, 来路无法证明, 而贴一个错标签的
                # 后果是查重安静地拿垃圾数当结论。NULL 的行会被比对排除, 并且
                # 能被 reembed 找到重算, 是个有出口的状态。(codex review · #65 P1)
                "embedding_model": dedup.EMBEDDING_MODEL if attested else None,
                "opening_hash": fp.opening_hash(body),
                "ngram_hashes": fp.ngram_hashes(body),
                "angle_key": None,   # 历史稿不是发牌产出的, 没有坐标
            })
        written += store.write_fingerprints(client, payload)

    for page in store.legacy_version_pages(client, project_id):
        total += len(page)
        for r in page:
            if r.get("version_id") in done_ids:
                already += 1
                continue
            pending.append(r)
            todo_seen += 1
            if len(pending) >= chunk:
                _flush()
                if progress:
                    # 流式下没有事先算好的分母 —— 报的是【已处理 / 已发现待回填】,
                    # 分母会随着翻页往上走。CLI 那边的文案已经跟着改了。
                    progress(processed, todo_seen)
    _flush()
    if progress and todo_seen:
        progress(processed, todo_seen)

    if not total:
        return {"total": 0, "already": 0, "written": 0, "embedded": 0,
                "reused_embeddings": 0, "missing_embeddings": 0}
    if not todo_seen:
        return {"total": total, "already": already, "written": 0, "embedded": 0,
                "reused_embeddings": 0, "missing_embeddings": 0}

    out = {
        "total": total, "already": already, "written": written,
        "embedded": reused + computed,
        "reused_embeddings": reused, "computed_embeddings": computed,
        "missing_embeddings": missing,
    }
    if missing:
        out["note"] = (
            f"{missing}/{written} 条没有标题向量"
            + ("(GOOGLE_API_KEY 未配或 embedding 调用失败)。" if not can_embed or computed == 0
               else "。")
            + "这些历史稿只参与确定性查重(开头精确 + 四字串重合), "
              "同角度换说法的标题比不出来。配好 embedding 后跑 "
              "`deskcore.cli reembed --project <id>` 给它们补上 —— 重跑 backfill "
              "不会重复写入, 但补向量是 reembed 的活。")
    if reused:
        # 复用的那些没有模型标记, 所以**不参与**标题语义比对。不说出来的话
        # "embedded: N" 看着像全都能比, 而实际能比的只有 computed 那部分 ——
        # 又一次"写着已经有了, 实际没有"。(codex review · #65 P1)
        out["unattested_embeddings"] = reused
        out["unattested_note"] = (
            f"{reused} 条复用了 versions.embedding 的历史向量。那张表【没有模型"
            f"标记】, 来路无法证明, 所以按 embedding_model=NULL 写入 —— 它们"
            f"**不参与**标题语义比对。跑 `deskcore.cli reembed --project <id>` "
            f"用当前模型({dedup.EMBEDDING_MODEL})重算之后才会生效。")
    return out


# ══════════════════════════════════════════════════════════════════════
# 部署自检 —— 这个库到底跑到第几个迁移了
# ══════════════════════════════════════════════════════════════════════
#
# ── 为什么非有这一段不可 ──────────────────────────────────────────────
#
# `migrations/README.md` 写着「五个迁移都设计成不跑也不会坏」。那句话是真的,
# 代价却是**没跑这件事在运行期完全看不见**: 004/005 缺席只埋一行 telemetry 就
# 退回慢路径, 短稿照搬长稿从此抓不到; 006 缺席则是另一种 —— 现有工作台每一次
# 「通过 / 打回」都会撞上 PostgREST 的 `column ... does not exist`。
#
# 这正是本仓反复栽的那个形状(runbook §5「文档与实际不一致」整节都是它):
# 文字写着已经有了, 实际没有, 而且不报错。2026-08-26 实测生产库
# (kduysqedr) 的结果是 **只跑了 001** —— 而 runbook §0 当时写的是
# 「schema 也上了生产」。查一次库就知道, 但在这之前没有任何一条命令能查。
#
# ── 三条实现纪律 ──────────────────────────────────────────────────────
#
# 1. **判据必须与真正消费它的代码同源。** 这里一律走 `store.rpc_missing`,
#    不另写一套字符串匹配。deskcore.md §4.2 记过反例: /health 自己读一遍 env,
#    于是回显着一个从未被调用过的模型名, 配错依然当场看不见。
#
# 2. **探测一律只读。** 写类 RPC 用「空 `_rows`」调 —— 那时函数体的 FOR 循环
#    一次都不进(见 migrations/005 的函数体), 只取一次事务级 advisory lock,
#    不写任何行。CAS 那个用 nil UUID + 不可能匹配的 witness, UPDATE 命中 0 行。
#
# 3. **探不到的要说"探不到", 不许猜。** 003 建的是唯一索引, PostgREST 看不见
#    索引 —— 所以它报 `unprobeable` 并把该跑的 SQL 一起交出来, 而不是按
#    "别的都在, 它八成也在" 蒙一个 applied。蒙对了没人受益, 蒙错了正好复现
#    这一整段想根治的那个失败形态。

# 匹配不到任何真实行的 UUID。探测只读的前提之一 —— 用它当 project_id/user_id,
# 就算某个探测函数真的会写, 它也没有行可写。
_PROBE_NIL_UUID = "00000000-0000-0000-0000-000000000000"

# 一定不等于任何一份 calibration_notes 的 md5(md5('') 是 d41d8c...)。
_PROBE_IMPOSSIBLE_MD5 = "0" * 32

# 001 建的四张表, 以及探它们用的那一列。四张表都有 ``project_id``(它们全都
# 挂在项目下), 这不是巧合而是设计 —— 但**别把它当理所当然**: 见下面探测处的
# 注释, 用 ``id`` 探的第一版在 user_calibration_notes 上就是错的。
_MIGRATION_001_TABLES = ("angle_ledger", "draft_fingerprints",
                         "user_calibration_notes", "style_edits")
_MIGRATION_001_PROBE_COLUMN = "project_id"

# backfill 自己那条路的上限(``store.legacy_version_pages`` 的默认 limit)。
# backfill_gap 刻意**不**用它扫描, 只用它判"这个项目大到 backfill 追不平了"。
_BACKFILL_DEFAULT_CAP = 5000

# 唯一一个"不跑就当场坏"的迁移 —— 其余几个缺席都是降级 + 留痕。所以它在补救
# 清单里必须排最前面, 与 runbook 的 `006 → 002 → 003 → 004 → 005` 一致。
MIGRATION_RUN_FIRST = "006_item_decision_provenance.sql"

# 缺 GRANT 的唯一补救出口。denied 状态**一律**指向这里, 不管报 denied 的那条
# 探测挂在哪个迁移名下 —— 见 migration_state 末尾对这条口径的完整说明。
MIGRATION_GRANTS = "007_deskcore_table_grants.sql"

MIGRATION_UNPROBEABLE_SQL = (
    "select indexname from pg_indexes where schemaname='autowriter' "
    "and indexname='versions_item_version_uniq';"
)

# 008 换的是**函数体**, 签名和返回列一个字没动 —— 从 PostgREST 这一面看,
# 跑没跑过完全一样。所以只能交出这句 SQL, 不许蒙。
MIGRATION_EMBEDDING_ISOLATION = "008_embedding_model_isolation.sql"

# 009 建两张表 + 两个跨 schema 的 RPC。表探列名取 tv_project_id(两张都有)。
MIGRATION_TV_LINKS = "009_tv_links.sql"
_MIGRATION_009_TABLES = ("tv_project_map", "tv_note_links")   # ingest_locks 单独探(列名不同)
_MIGRATION_009_PROBE_COLUMN = "tv_project_id"
# 010 开的是 RLS, PostgREST 探不到(service_role 绕 RLS, 开没开读起来一样)。
MIGRATION_TV_LINKS_RLS = "010_tv_links_rls.sql"
MIGRATION_010_PROBE_SQL = (
    "select relname, relrowsecurity from pg_class c join pg_namespace n on n.oid=c.relnamespace "
    "where n.nspname='autowriter' and relname in ('tv_project_map','tv_note_links','ingest_locks');"
)
MIGRATION_008_PROBE_SQL = (
    "select prosrc like '%f.embedding_model = _model%' as has_model_filter "
    "from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
    "where n.nspname='autowriter' and p.proname='deskcore_check_drafts';"
)


def _probe_ok(fn, *, predicate=None) -> tuple[str, str]:
    """跑一个只读探测。返回 (state, note)。

    判据说"这个对象不存在"→ ``missing``; 其余异常 → ``error``(**不是**
    missing) —— 权限、参数、库故障被当成"迁移没跑"正是 store.py 里那一大段
    注释在防的事。

    ⚠️ **两种判据, 按探的是什么分**(codex review · #63, 实测确认):

      · **RPC** 探测用 ``store.rpc_missing``(默认) —— 与运行期降级用的**同一个**
        函数。同源是这里的重点: 自检说"RPC 在", 运行期就不该判它不在。
      · **表 / 列** 探测用 ``store.schema_object_missing`` —— 因为
        ``rpc_missing`` 对缺表返 False: PostgREST 回的是 ``PGRST205
        Could not find the table ... in the schema cache``, 三个条件一个都不沾。
        照旧用它的话, 一个**真的没跑 001** 的库会被报成 ``error`` 并写着
        "不是「没跑迁移」" —— 正好说反, 而这个命令存在的全部意义就是别说反。
        表/列这一路没有运行期对应物(运行期不该因为缺表就降级), 所以宽一档不
        破坏同源那条纪律。

    ⚠️ **"没授权"在两种判据之前先被拎出来, 报成 ``denied``。**
    ``42501 permission denied for table`` 说的是"表建出来了, 但 GRANT 没发",
    它既不是 missing(对象在)也不该混进 error(它有确定的补救 SQL: 007)。
    不单独分一档的话, 一个**只缺 GRANT** 的库会让 001 那几条表探测全报
    ``error`` 并写着"不是「没跑迁移」", 而同一份输出里 007 报 missing ——
    自检工具给出互相矛盾的结论, 比它不存在更坏(与 codex #63 那条同一个道理)。
    """
    check = predicate or store.rpc_missing
    try:
        fn()
    except Exception as exc:                       # noqa: BLE001 — 探测就是要看异常
        if store.table_permission_denied(exc):
            return "denied", f"{type(exc).__name__}: {exc}"[:200]
        if check(exc):
            return "missing", f"{type(exc).__name__}: {exc}"[:200]
        return "error", (f"探测本身失败(不是「没跑迁移」): "
                         f"{type(exc).__name__}: {exc}"[:200])
    return "applied", "ok"


def _probe_signature(client, name: str, wide: dict, narrow: dict,
                     wide_tag: str, narrow_tag: str) -> tuple[str, str]:
    """先按新签名调, 报"找不到"再按旧签名调 —— 与 store.py 的两处同一套路。

    区分"函数完全不存在"和"函数在、但还是旧签名"是这里的全部意义:
    PostgREST 对**参数对不上**的报错文本里也带 ``does not exist``, 只看一次
    调用会把"库停在 004"误报成"001 都没跑"(审计 COR-014 记过这个坑)。
    """
    state, note = _probe_ok(lambda: client.rpc(name, wide).execute())
    if state != "missing":
        return (wide_tag if state == "applied" else state), note
    state, note = _probe_ok(lambda: client.rpc(name, narrow).execute())
    if state == "applied":
        return narrow_tag, "函数在, 但还是旧签名"
    return state, note


def migration_state(client) -> dict:
    """逐个探测: 这个库跑到第几个迁移了, 缺的那些各自会怎样。

    只读。返回 ``{"ok": bool, "checks": [...], "missing": [...],
    "unprobeable": [...]}``; ``checks`` 每项带 ``migration`` / ``state`` /
    ``impact``, 让人不必翻文档就知道缺了要紧不要紧。

    ``ok`` 的口径是**没有一项 missing 或 error**。``unprobeable`` 不算不 ok
    —— 探不到不等于没跑, 把它算进去会让这条命令永远报红, 而永远报红的检查
    等于没有检查。
    """
    checks: list[dict] = []

    def _add(migration: str, what: str, state: str, note: str, impact: str) -> None:
        checks.append({"migration": migration, "probe": what,
                       "state": state, "note": note, "impact": impact})

    # ── 001: 四张表 + items.updated_at ──
    #
    # ⚠️ 取的列是 ``project_id`` 而不是 ``id``。``user_calibration_notes`` 的
    # 主键是 ``(project_id, user_id)`` —— **它没有 id 列**。探一个不存在的列,
    # PostgREST 报的是 `column "id" does not exist`, 而那句话正好被
    # ``rpc_missing`` 认成"迁移没跑" —— 于是在一个 001 明明跑过的库上报 missing,
    # 把人打发去重跑一遍迁移。这个探测器存在的全部意义就是不出这种错。
    #
    # 第一版就是 ``.select("id")``, 而假件不校验列名, 所以测试给了个假的绿 ——
    # 与审计 §0.5 那条"录音机记得不够细, 得到的绿是假的"同一件事。现在
    # ``tests/test_migration_doctor.py`` 会去 001 的 SQL 里核对这个列真的存在。
    for table in _MIGRATION_001_TABLES:
        state, note = _probe_ok(
            lambda t=table: client.table(t)
            .select(_MIGRATION_001_PROBE_COLUMN).limit(1).execute(),
            predicate=store.schema_object_missing)
        _add("001_deskcore.sql", f"表 {table}", state, note,
             "deskcore 整个不可用")
    state, note = _probe_ok(
        lambda: client.table("items").select("id,updated_at").limit(1).execute(),
        predicate=store.schema_object_missing)
    _add("001_deskcore.sql", "items.updated_at", state, note,
         "TV 的 sync_autowriter_decisions_to_prepublish 会降级回只按 created_at, "
         "迟到的人工决策重新开始漏收")

    # 001 建的还有这个 RPC —— 漏探它的代价是**静默的**: 表都在, doctor 报
    # "001 到位", 而 store.reserve_angles 每次都判 RPC 不存在、降级成非原子发牌,
    # 于是两个人同时发牌能拿到同一组角度坐标, 两边都报成功。部分/手工部署或
    # schema 漂移下这是会发生的。(codex review · #63)
    #
    # ``_want: 0`` 是可证明的 no-op: 函数体第一句就是 `IF _want <= 0 THEN
    # RETURN; END IF;`, 连 advisory lock 都还没取。
    state, note = _probe_ok(lambda: client.rpc("deskcore_reserve_angles", {
        "_project_id": _PROBE_NIL_UUID,
        "_candidates": [],
        "_drawn_by": _PROBE_NIL_UUID,
        "_want": 0,
        "_avoid_days": 30,
    }).execute())
    _add("001_deskcore.sql", "deskcore_reserve_angles", state, note,
         "发牌退回非原子路径 —— 两人同时发牌可能拿到同一组角度坐标, "
         "而两边都报成功(台账事后也看不出来)")

    # ── 002: CAS RPC。nil 项目 + 不可能匹配的 witness → UPDATE 命中 0 行 ──
    state, note = _probe_ok(lambda: client.rpc("update_calibration_notes_cas", {
        "_project_id": _PROBE_NIL_UUID,
        "_expected_md5": _PROBE_IMPOSSIBLE_MD5,
        "_notes": "",
    }).execute())
    _add("002_calibration_cas.sql", "update_calibration_notes_cas", state, note,
         "长笔记(>4000 字级)的自动学习静默停摆 —— 四条学习路径全部不再写回, "
         "而且不报错")

    # ── 003: 唯一索引。PostgREST 看不见索引, 只能交出 SQL ──
    _add("003_versions_unique_num.sql", "versions_item_version_uniq (唯一索引)",
         "unprobeable",
         "PostgREST 读不到 pg_indexes; 用 SQL Editor 跑: " + MIGRATION_UNPROBEABLE_SQL,
         "少了数据库层对重复 version_num 的保护(应用层重试本身不依赖它)")

    # ── 004: 指纹计数 RPC ──
    state, note = _probe_ok(lambda: client.rpc(
        "deskcore_fingerprint_counts",
        {"_project_ids": [_PROBE_NIL_UUID]}).execute())
    _add("004_deskcore_check_pushdown.sql", "deskcore_fingerprint_counts",
         state, note, "list_projects 退回逐项目 count(2N+1 次查询)")

    # ── 004/005: check 的两版签名 ──
    state, note = _probe_signature(
        client, "deskcore_check_drafts",
        wide={"_project_id": _PROBE_NIL_UUID, "_rows": [],
              "_contain_min_sample": fp.CONTAIN_MIN_SAMPLE},
        narrow={"_project_id": _PROBE_NIL_UUID, "_rows": []},
        wide_tag="applied", narrow_tag="old_signature")
    _add("005_deskcore_containment.sql", "deskcore_check_drafts(3 参)",
         state, note,
         "缺席→查重退回 Python 逐对比对(慢, 回到 4000 条上限); "
         "旧签名→包含度那一路不发言, **短稿整段照搬长稿在 check 这一关拦不住**")

    # ── 001/005: commit 的两版签名。空 _rows 时函数体的 FOR 一次都不进 ──
    state, note = _probe_signature(
        client, "deskcore_commit_fingerprints",
        wide={"_project_id": _PROBE_NIL_UUID, "_rows": [],
              "_user_id": _PROBE_NIL_UUID,
              "_ngram_hard": fp.NGRAM_JACCARD_HARD,
              "_contain_hard": fp.NGRAM_CONTAIN_HARD,
              "_contain_min_sample": fp.CONTAIN_MIN_SAMPLE},
        narrow={"_project_id": _PROBE_NIL_UUID, "_rows": [],
                "_user_id": _PROBE_NIL_UUID,
                "_ngram_hard": fp.NGRAM_JACCARD_HARD},
        wide_tag="applied", narrow_tag="old_signature")
    _add("005_deskcore_containment.sql", "deskcore_commit_fingerprints(6 参)",
         state, note,
         "缺席→定稿退回直插, check/commit 之间的竞态窗口重新打开; "
         "旧签名→竞态仍关着, 但写入侧不做包含度重查")

    # ── 006: 决策出处三列。⚠️ 这一条不跑是**硬失败**, 不是降级 ──
    state, note = _probe_ok(lambda: client.table("items")
                            .select("id,decision_source,reviewer_id,decided_at")
                            .limit(1).execute(),
                            predicate=store.schema_object_missing)
    _add("006_item_decision_provenance.sql", "items 的决策出处三列", state, note,
         "⚠️ 硬失败: db.update_item_status 无条件写这三列, 缺列会让现有工作台的"
         "「通过 / 打回」、硬规则自动标记、查重自动标记**全部报错**。"
         "这个迁移与其它几个不同, **不是可选的**")

    # ── 007: 四张表的表级 GRANT。⚠️ 也是**硬失败** ──
    #
    # 探的是"读得到吗", 不是"表在吗" —— 上面 001 那几条已经回答了后者, 而
    # 2026-08-26 首次真部署证明这两件事**不是一回事**: 表全在, doctor 全绿,
    # /health 全绿, 而 deskcore 除 list_projects 外每个工具都挂在
    # `42501 permission denied for table draft_fingerprints`。
    #
    # 为什么 001 那几条探测挡不住这个: 它们跑在同一个 service_role 上、报的是
    # 同一个 42501 —— 现在由 _probe_ok 统一识别成 denied, 所以两边给的是同一个
    # 结论, 补救指向同一个文件。这里再单列一条, 是为了让"缺 GRANT"在
    # missing 清单里有个**编号**可跑, 而不是只留一句 note 让人自己想办法。
    #
    # 用 draft_fingerprints 当代表: 四张表在 001 / 007 里是同一条 GRANT 语句
    # 发的, 不存在只授权了其中一张的中间态。
    #
    # ⚠️ **四个权限都要探, 不能只探 SELECT。**(codex review · #65)
    # 只探读的话, 一个"读得到但写不进"的库(手工补授权补漏了 / 后来被人收回过)
    # 会报全绿, 而定稿入库、发牌、存个人笔记照旧在 42501 上挂 —— 正好是这条
    # 检查存在的理由的反面。007 承诺四个权限, 就得验四个。
    #
    # 怎么在【不写一行】的前提下探写权限(与本节第 2 条纪律一致)。
    #
    # ⚠️ 关键在于三个探测都必须是**逻辑上不可能生效**的, 而不是"大概不会命中"。
    # 这是对着生产库跑的命令, "nil UUID 应该没有对应行"这种概率论不够格 ——
    # 真有那么一行的话, UPDATE 会清空它的标题、DELETE 会把它删掉。
    #
    #   · INSERT —— payload 是 ``{"project_id": None}``, 而这一列是 NOT NULL。
    #                权限检查在约束检查【之前】, 所以没权限报 42501, 有权限报
    #                23502(非空约束)。**无论库里有什么数据, 它都插不进去。**
    #   · UPDATE / DELETE —— 过滤是 ``id = X AND id <> X``, 对任何一行都是假。
    #                有权限就是成功的空操作, 没权限才报 42501。
    #
    # tests 里有两条断言守着: 一条钉住这三个形态(哪天谁把矛盾条件改成普通
    # 过滤, 这个自检命令就成了删库命令); 一条让假件真的模拟 NOT NULL ——
    # 探测的安全性靠的是那条约束, 假件不模拟它, 测试给的绿就是假的。
    _grant_probes = (
        ("SELECT", lambda t: t.select(_MIGRATION_001_PROBE_COLUMN).limit(1)),
        ("INSERT", lambda t: t.insert({"project_id": None})),
        ("UPDATE", lambda t: t.update({"title": ""})
                              .eq("id", _PROBE_NIL_UUID)
                              .neq("id", _PROBE_NIL_UUID)),
        ("DELETE", lambda t: t.delete()
                              .eq("id", _PROBE_NIL_UUID)
                              .neq("id", _PROBE_NIL_UUID)),
    )
    for priv, build in _grant_probes:
        state, note = _probe_ok(
            lambda b=build: b(client.table("draft_fingerprints")).execute(),
            predicate=store.schema_object_missing)
        if priv == "INSERT" and state in ("error", "missing"):
            # 有权限时 INSERT 必然挂在非空约束上 —— 那**正是通过**, 不是故障。
            # (也认 23503: 万一哪天这一列不再是 NOT NULL, 外键仍然会拦下。)
            if any(k in note.lower() for k in
                   ("23502", "23503", "not-null", "not null", "foreign key")):
                state, note = "applied", "ok(约束拦下, 说明 INSERT 权限是有的)"
        if state == "missing":
            # 表本身还不在 = 001 都没跑, 上面那几条已经在喊了。这里再喊一遍
            # "007 也缺"只会让人以为要跑两个 —— 而 001 里已经含着同一条 GRANT。
            state, note = "unprobeable", ("001 的四张表还不在, 先跑 001 "
                                          "(它里面已经含着这条 GRANT)")
        _add(MIGRATION_GRANTS,
             f"draft_fingerprints 的 {priv} 权限", state, note,
             "⚠️ 硬失败: service_role 绕过 RLS 但**不绕过表级 GRANT**。缺了它, "
             "deskcore 除 list_projects 外每个工具都在 42501 permission denied 上挂, "
             "而 /health 仍然全绿(它探的是连得上, 不是访问得了)")

    # ── 008: 标题语义比对按 embedding 模型隔离 ──
    #
    # PostgREST 看不见函数体, 所以"这一版有没有那句过滤"从这一面探不到 ——
    # 与 003 的唯一索引同类, 如实报 unprobeable 并把该跑的 SQL 交出来。
    #
    # ⚠️ **别用"函数在不在"冒充这条。** 008 是 CREATE OR REPLACE, 签名和返回列
    # 一个字都没动 —— 跑没跑过, 从调用侧看**完全一样**, 直到某天有人换了模型
    # 才发现比对一直是混着算的。蒙一个 applied 正好复现这套东西要根治的形态。
    _add(MIGRATION_EMBEDDING_ISOLATION,
         "deskcore_check_drafts 按模型过滤(函数体)",
         "unprobeable",
         "PostgREST 读不到函数体; 用 SQL Editor 跑: " + MIGRATION_008_PROBE_SQL,
         "缺席→标题语义那一路把【别的模型产的】历史向量也算进来。跨模型的余弦"
         "是噪声: 既会放过真重复, 也会误杀无关稿, 而 semantic_degraded 照报 "
         "false —— 失灵的同时还说自己跑过了")

    # ── 009: TV 对照的两张表 + 读 TV 的 RPC。只读探测: 表 select 1 行, RPC 用一个
    #    不存在的 TV 项目名(返回空集; 库里没有 truth_vault 时函数自己返回空)。
    for table in _MIGRATION_009_TABLES:
        state, note = _probe_ok(
            lambda t=table: client.table(t)
            .select(_MIGRATION_009_PROBE_COLUMN).limit(1).execute(),
            predicate=store.schema_object_missing)
        _add(MIGRATION_TV_LINKS, f"表 {table}", state, note,
             "tv-sync 整个不可用: TV 的笔记认不回写作台的版本, 已发未入库的稿子"
             "补不进指纹库, 「爆没爆」回不到写作台")
    state, note = _probe_ok(lambda: client.rpc("deskcore_tv_notes", {
        "_tv_project_id": "__doctor_probe__", "_since": None,
        "_after": None, "_limit": 1}).execute())
    _add(MIGRATION_TV_LINKS, "deskcore_tv_notes", state, note,
         "读不到 TV 的笔记 —— tv-sync 一条都对不了")
    # 回填 RPC: 空 _links → UPDATE 一行都不碰(codex #81: 只探读的那一半, 部署了一半
    # 的库会被报成 applied, 而 --write-tv 到运行时才炸)。
    state, note = _probe_ok(lambda: client.rpc("deskcore_tv_backfill_lineage",
                                               {"_links": []}).execute())
    _add(MIGRATION_TV_LINKS, "deskcore_tv_backfill_lineage", state, note,
         "tv-sync --write-tv 到运行时才失败: 对照写不回 TV 的 source_autowriter_*")
    # 补录锁: 表 + 放锁 RPC(nil 项目 + 探针专用 holder: 那一行逻辑上不可能存在,
    # DELETE 命中 0 行)。拿锁 RPC 不探 —— 探一次就真的会写一行。
    state, note = _probe_ok(
        lambda: client.table("ingest_locks").select("project_id").limit(1).execute(),
        predicate=store.schema_object_missing)
    _add(MIGRATION_TV_LINKS, "表 ingest_locks", state, note,
         "补录退回进程内锁: 服务进程与 tv-sync 进程重叠时同一批稿子会建两份身份")
    state, note = _probe_ok(lambda: client.rpc("deskcore_ingest_unlock", {
        "_project_id": _PROBE_NIL_UUID, "_holder": "__doctor_probe__"}).execute())
    _add(MIGRATION_TV_LINKS, "deskcore_ingest_unlock", state, note,
         "同上: 跨进程互斥失效(deskcore_ingest_lock 与它同一个迁移, 不单独探)")

    # ── 010: 009 那三张表开 RLS。PostgREST 这一面探不到 relrowsecurity(service_role
    #    绕 RLS, 开没开读起来一样), 与 003 / 008 同类: 如实报 unprobeable 并交出
    #    该跑的 SQL。漏跑不影响任何功能, 只是 Supabase advisor 会一直报。
    _add(MIGRATION_TV_LINKS_RLS,
         "tv_project_map / tv_note_links / ingest_locks 的 RLS",
         "unprobeable",
         "PostgREST 读不到 pg_class.relrowsecurity; 用 SQL Editor 跑: " + MIGRATION_010_PROBE_SQL,
         "功能不坏(anon 对这三张表没有表级 GRANT, service_role 绕 RLS); 差的是 "
         "Supabase advisor 一直报 rls_disabled, 以及以后谁给 anon 发了 GRANT 会一下子"
         "把整张表露出去")

    # ⚠️ ``error`` 不进 ``missing``(codex review · #63)。原来它进 —— 于是一次
    # 权限/连通性故障会让 doctor 打印"还缺这些迁移, 按编号顺序跑", 把人指去跑
    # 一遍根本不缺的 SQL, 而同一份输出里那条 note 明明写着"不是「没跑迁移」"。
    # 一个自检工具给出**互相矛盾的**结论, 比它不存在更坏。
    #
    # 两者都判红(见 ok), 但补救方式完全不同, 所以必须分开报。
    #
    # ``denied`` 同理再分一档: 它既不是"没跑迁移"(对象在), 也不是"探测本身坏了"
    # (它有确定的补救 SQL)。但它**进 missing** —— 因为对跑这条命令的人来说,
    # 该做的事就是去跑 migrations/007, 与其它缺席迁移的动作完全一样。
    #
    # ⚠️ **denied 一律记到 007 名下, 不管这条探测挂在哪个迁移上。**(codex #65)
    # 缺 GRANT 时 001 那四条表探测也会报 denied —— 按"探测挂在哪个迁移就算哪个"
    # 聚合的话, missing 里会同时出现 001 和 007, 而 doctor 紧接着打印"还缺这些
    # 迁移, 按这个顺序跑"。于是人对着一个**表和函数都好好在那儿**的库重跑一遍
    # 建表 SQL(幂等, 什么都不会变), 然后继续 42501。
    #
    # 那些 denied 的探测行**照常显示**(state 就是 denied, 一眼看出是哪几张表),
    # 只是不进【该跑什么】那份清单 —— 清单要回答的是动作, 不是现象。
    # 这跟上面 error 不进 missing 是同一条纪律: 一个自检工具给出互相矛盾的
    # 结论, 比它不存在更坏。
    denied = sorted({c["migration"] for c in checks if c["state"] == "denied"})
    missing = sorted(
        {c["migration"] for c in checks
         if c["state"] in ("missing", "old_signature")}
        | ({MIGRATION_GRANTS} if denied else set()))
    errors = sorted({c["migration"] for c in checks if c["state"] == "error"})
    return {
        "ok": not missing and not errors,
        "checks": checks,
        "missing": missing,
        "denied": denied,
        "errors": errors,
        "unprobeable": sorted({c["migration"] for c in checks
                               if c["state"] == "unprobeable"}),
    }


# ── 补录的互斥(codex review #81 P1) ─────────────────────────────────────
# ingest_published 的幂等靠"先读库里有没有指纹、再写"; 两个调用重叠时都过完那
# 一读再各写一份。调用方有两种进程: 服务进程(WorkBuddy 工具)和 CLI/cron
# (tv-sync), 进程内的锁管不到对方, 所以主锁是库里的一行(migrations/009 的
# ingest_locks, 带 TTL, 持有方崩了也能被接管); 进程内锁只是省得同进程的并发
# 请求都去轮询库。锁 RPC 没部署(doctor 会报)时退回进程内锁并记 warning ——
# 那是降级, 不是"拿到了"。
INGEST_LOCK_TTL_SEC = 600          # 一批 ≤ 50 条远用不了; tv-sync 一个项目几百条也够
INGEST_LOCK_WAIT_SEC = 90          # 等不到就报 IngestBusy, 让调用方稍后再来
INGEST_LOCK_POLL_SEC = 2.0
INGEST_EMBED_CHUNK = 100           # embed_content 单次上限; 也限住指纹请求体
_ingest_proc_locks: dict[str, threading.Lock] = {}
_ingest_proc_guard = threading.Lock()


class IngestBusy(RuntimeError):
    """另一个补录正在这个项目上跑(锁被别的进程持有, 等了 INGEST_LOCK_WAIT_SEC 还没放)。
    REST 层映成 409: 不是坏了, 稍后重来即可。"""


def _ingest_proc_lock(project_id: str) -> threading.Lock:
    with _ingest_proc_guard:
        return _ingest_proc_locks.setdefault(str(project_id), threading.Lock())


def _acquire_ingest_lock(client, project_id: str, holder: str) -> bool | None:
    """轮询拿库锁。True 拿到; None 锁 RPC 没部署(降级); 等超时抛 IngestBusy。"""
    deadline = time.monotonic() + INGEST_LOCK_WAIT_SEC
    while True:
        got = store.try_ingest_lock(client, project_id, holder, INGEST_LOCK_TTL_SEC)
        if got is None:
            logger.warning("ingest lock RPC 没部署(migrations/009), 本次只用进程内锁 "
                           "(project=%s)", project_id)
            return None
        if got:
            return True
        if time.monotonic() >= deadline:
            raise IngestBusy(f"项目 {project_id} 上另一个补录正在跑(锁被别的进程持有), "
                             f"等了 {INGEST_LOCK_WAIT_SEC}s 没放 —— 稍后再试, 不要并行补录")
        time.sleep(INGEST_LOCK_POLL_SEC)


def ingest_published(client, project_id: str, entries: list[dict], *,
                     user_id: str, source: str, dry_run: bool = False) -> dict:
    """``_ingest_published_unlocked`` 的加锁形态 —— 调用方一律走这个。

    dry_run 不写库, 不拿锁(拿了只会让真在跑的补录多等)。
    """
    # 先过归属闸再去拿锁: 拿不到锁要等 90s, 等完再告诉人"不是你的项目"没有道理。
    assert_project_access(client, project_id, user_id=user_id)   # 审计 COR-015
    if dry_run:
        return _ingest_published_unlocked(client, project_id, entries, user_id=user_id,
                                          source=source, dry_run=True)
    holder = f"{source}:{uuid.uuid4().hex[:8]}"
    with _ingest_proc_lock(project_id):
        got = _acquire_ingest_lock(client, project_id, holder)
        try:
            return _ingest_published_unlocked(client, project_id, entries, user_id=user_id,
                                              source=source, dry_run=False)
        finally:
            if got:
                try:
                    store.ingest_unlock(client, project_id, holder)
                except Exception:                    # noqa: BLE001
                    logger.exception("ingest unlock failed (project=%s); 锁会在 TTL 后自动"
                                     "过期", project_id)


def _ingest_published_unlocked(client, project_id: str, entries: list[dict], *,
                               user_id: str, source: str, dry_run: bool = False) -> dict:
    """把【已经发出去、但没走 commit_drafts】的稿子补进库: 身份 + 指纹, **不过闸**。

    ── 为什么不过闸 ────────────────────────────────────────────────────
    这些稿子已经发在小红书上了。它跟库里谁重复都改变不了这个事实, 拦它没有
    任何意义 —— 拦了, 下一批照样撞上它。回填的逻辑(backfill_fingerprints)是
    一样的: 已发生的历史原样入库。

    ── 为什么要建身份 ──────────────────────────────────────────────────
    只写指纹也能挡重复, 但 items/versions 行是导出 lineage 和 TV 归因的凭据;
    这些稿子既然发出去了, 之后指标回流时 TV 会按 version_id 找它们。
    出处记在 ``batches.params``({"source": "ingest", "file": 表名}), 让人以后
    分得清这批是补进来的, 不是写作台当场写的。**不碰 items.external_source**:
    那列是 TV 同步的标记('truth_vault'), TV 按它认自己的行, 借用会搅乱对接。

    ── 幂等(code review 2026-09-17)────────────────────────────────────
    重跑必须安全: 运营看到一条警告就会再跑一遍。所以入库前先按正文开头哈希
    (``opening_hash``)查一遍指纹库, **已经有指纹的行跳过**; 同一张表里正文相同
    的行也只收第一条。两种跳过都数出来报。

    但"身份建了、指纹没写"那种半途失败**不是靠重跑修**: 重跑看不到它的指纹,
    会再建一份身份。修法是 ``backfill --project`` —— 它按 version_id 幂等, 会把
    有身份没指纹的行补上。返回里 ``fingerprinted < minted`` 时就该走那条路。

    ``dry_run`` 只数不写(但会查已有指纹, 所以 dry-run 的数字就是真跑会写的数)。
    """
    assert_project_access(client, project_id, user_id=user_id)   # 审计 COR-015
    cleaned = [{"title": (e.get("title") or "").strip(),
                "body": (e.get("body") or "").strip(), "index": i}
               for i, e in enumerate(entries)]
    cleaned = [e for e in cleaned if e["title"] or e["body"]]
    # 表内去重 + 库内去重, 都按【开头哈希 + 整篇四字串 sketch】—— 只按开头会把
    # 同一个模板开头的不同稿子全判成重复(codex review; 见 store 里那段说明)。
    # 没有正文(title-only)的行算不出任何一样, 只能照收 —— 它们本来也拦不住谁。
    dup_in_sheet = already = 0
    seen: set[tuple] = set()
    keys = [(fp.opening_hash(e["body"]), tuple(sorted(fp.ngram_hashes(e["body"]))))
            for e in cleaned]
    known = store.existing_fingerprint_sketches(
        client, project_id, [oh for oh, _ in keys if oh])
    keep: list[dict] = []
    for e, (oh, sk) in zip(cleaned, keys):
        if oh and (oh, sk) in seen:
            dup_in_sheet += 1
            continue
        if oh and sk in known.get(oh, ()):
            already += 1
            continue
        if oh:
            seen.add((oh, sk))
        keep.append(e)
    cleaned = keep
    out = {"received": len(entries), "to_write": len(cleaned),
           "skipped_already_fingerprinted": already,
           "skipped_duplicate_in_sheet": dup_in_sheet,
           "minted": 0, "fingerprinted": 0, "batch_id": None,
           "identity_error": None, "fingerprint_error": None,
           "embedded": False, "dry_run": dry_run, "written": []}
    if dry_run or not cleaned:
        return out
    to_mint = [{"version_id": str(uuid.uuid4()), "title": e["title"],
                "body": e["body"], "keywords": [], "index": e["index"]} for e in cleaned]
    minted = store.mint_draft_identity(client, project_id, str(user_id), "",
                                       to_mint,
                                       batch_params={"source": "ingest",
                                                     "file": source})
    done = [m for m in to_mint if m["version_id"] in minted.get("versions", {})]
    out.update(minted=len(done), batch_id=minted.get("batch_id"),
               identity_error=minted.get("error"),
               # 调用方传进来的第几条 → 建成的 version_id(tv_sync 记对照要用)
               written=[{"index": m["index"], "version_id": m["version_id"]} for m in done])
    if not done:
        return out
    # 按 INGEST_EMBED_CHUNK 条一批: embed_content 单次最多 100 条, 指纹行带 400 个
    # 四字串 hash + 768 维向量, 几百行一个请求体会顶到网关上限。tv-sync 一次
    # 补几百条是常态(2026-09-17 dry-run: sportsix 461 / 雷诺考特 357 / 西屋 301)。
    embedded_any = False
    can_embed = dedup.embeddings_available()
    for start in range(0, len(done), INGEST_EMBED_CHUNK):
        chunk = done[start:start + INGEST_EMBED_CHUNK]
        vecs = dedup.embed_texts([m["title"] for m in chunk]) if can_embed else None
        payload = []
        for i, m in enumerate(chunk):
            vec = vecs[i] if vecs and i < len(vecs) and vecs[i] else None
            payload.append({
                "project_id": project_id, "user_id": user_id,
                "version_id": m["version_id"], "title": m["title"],
                "opening": fp.opening_of(m["body"]),
                "title_embedding": vec,
                "embedding_model": dedup.EMBEDDING_MODEL if vec else None,
                "opening_hash": fp.opening_hash(m["body"]),
                "ngram_hashes": fp.ngram_hashes(m["body"]),
                "angle_key": None,       # 补进来的稿子不是发牌产出的, 没有坐标
            })
        # ⚠️ 身份已经建好了。这一步再抛出去, 调用方(CLI)就到不了"跑 backfill"那句,
        #    而一次自然的重跑看不到这几条的指纹, 会再建一份身份(codex review)。
        #    所以吞掉、写进返回值, 让 CLI 把正确的补救路径说出来。
        try:
            out["fingerprinted"] += store.write_fingerprints(client, payload)
        except Exception as exc:  # noqa: BLE001
            logger.exception("ingest: 身份建成 %d 条后写指纹失败 (project=%s, 已写 %d)",
                             len(done), project_id, out["fingerprinted"])
            out["fingerprint_error"] = f"{type(exc).__name__}: {exc}"[:300]
            break
        embedded_any = embedded_any or bool(vecs)
    out["embedded"] = embedded_any
    return out


# ── 「发了角度没入库」: 流程漏斗的泄漏 ──────────────────────────────────
# 09-11 起 78% 的角度发出去了、稿子没入库, 这个数字在库里躺了一周没人看 ——
# 服务本身一直是 ok 的, 而它是使用层的故障。所以 doctor 和 /health 都报它。
LEAK_WINDOW_DAYS = 7
LEAK_MIN_DRAWN = 20        # 样本太小不报警: 一个人试写 5 条没入库不是事故
LEAK_ALERT_PCT = 50


def pipeline_leak(client, days: int = LEAK_WINDOW_DAYS) -> dict:
    """近 ``days`` 天: 发了多少角度、多少条真的入库销了账, 按项目分。

    ``ok`` 为 False 的判据: 发了至少 LEAK_MIN_DRAWN 个角度, 且超过 LEAK_ALERT_PCT
    没入库。健康的日子(09-10)是 18%; 塌了的日子(09-14/15)是 100% / 88%。
    这不是服务健康 —— /health 的顶层 ok 不看它; 它说的是"流程在漏"。
    """
    per = store.angle_leak(client, days)
    drawn = sum(p["drawn"] for p in per)
    consumed = sum(p["consumed"] for p in per)

    def _breach(d: int, c: int) -> bool:
        return d >= LEAK_MIN_DRAWN and round(100 * (d - c) / d) > LEAK_ALERT_PCT

    for p in per:
        p["leak_pct"] = (round(100 * (p["drawn"] - p["consumed"]) / p["drawn"])
                         if p["drawn"] else 0)
        p["ok"] = not _breach(p["drawn"], p["consumed"])
    pct = round(100 * (drawn - consumed) / drawn) if drawn else 0
    # ⚠️ 逐项目也判, 不只看总量: 一个 30/30 全漏的项目会被另一个 100/100 全入库
    #    的大项目摊成 23%、判成健康 —— 而那个小项目的指纹一条都没进(codex review)。
    over = [p for p in per if not p["ok"]]
    ok = not _breach(drawn, consumed) and not over
    return {
        "window_days": days, "drawn": drawn, "consumed": consumed,
        "leak_pct": pct, "ok": ok,
        "projects_over_threshold": len(over),
        "projects": per,
        "note": ("发出去的角度里没走到 commit_drafts 的比例。没入库的稿子没有"
                 "指纹, 下一批查重看不见它们。健康时约 20%, 超过 "
                 f"{LEAK_ALERT_PCT}% 就该去看是谁的会话在半路停的"
                 + ("" if ok else
                    f" —— 现在就超了({len(over)} 个项目单独超线)" if over
                    else " —— 现在就超了")),
    }


def backfill_gap(client, project_id: str) -> dict:
    """这个项目的指纹回填还差多少 —— 用**与 backfill 完全同一套口径**算。

    ⚠️ 故意不做归属校验, 理由同 backfill_fingerprints / reembed_fingerprints:
    这是运维命令, 跑它的人手里握着 service_role key。

    ── 为什么不是一句 SQL ──────────────────────────────────────────────
    runbook 里原本给的是一段手写 SQL(``items × batches × versions`` 三表 join)。
    那是把回填的口径**抄了第二份**: backfill 只取 items × versions 里"有版本的
    item", 而抄出来的那份一旦和 `store.legacy_version_pages` 漂开, 验收标准
    就会在"主力项目还差几百条"的时候报绿 —— 而验收标准报绿正是本仓最怕的那
    一类失败。这里直接调回填自己用的那两个函数, 两者不可能漂。

    ⚠️ **扫全量, 不带上限**(``limit=None``)。``legacy_version_pages`` 默认
    ``limit=5000`` —— 沿用它的话, 一个 5000 条以上的项目在最新那 5000 条补齐之后
    就会被报成"还差 0 / done"，而更老的历史稿从来没进过查重基线。**一个只看了
    前 5000 条的验收标准报出来的绿是假的**, 而验收标准报假绿正是本仓最怕的那类
    失败(runbook §5 整节)。(codex review · #63)

    回填本身**仍然**带 5000 上限, 那是它的已知边界 —— 所以 ``eligible`` 超过
    上限时这里会带一条 ``backfill_capped_warning``: 那种项目靠 backfill 追不平,
    需要先把 cap 调高。诚实地说出来, 好过让两个数永远对不上而没人知道为什么。

    只读: 只数数, 不写指纹。
    """
    done = store.existing_fingerprint_version_ids(client, project_id)
    eligible: set[str] = set()
    for page in store.legacy_version_pages(client, project_id, limit=None):
        for row in page:
            vid = row.get("version_id")
            if vid:
                eligible.add(str(vid))

    todo = eligible - done
    out = {
        "project_id": project_id,
        "eligible": len(eligible),         # backfill 口径下"应有"的条数
        "backfilled": len(eligible & done),
        "todo": len(todo),
        # ⚠️ 真的去数一次行数。这里原来写的是 len(done) —— 而 done 来自
        # existing_fingerprint_version_ids, 它过滤掉了 version_id 为空的行、
        # 还去了重。也就是说它**恰恰数不到**写作台经 commit_drafts 写进来的
        # 那批(那批的 version_id 就是空的), 而 CLI 的说明文字还专门写着
        # "含 commit_drafts 写进来的" —— 正好说反。(codex review · #63)
        "fingerprints_total": store.fingerprint_row_count(client, project_id),
    }
    out["done"] = not todo
    if todo:
        out["next"] = (f"python -m deskcore.cli backfill --project {project_id}")
    if len(eligible) > _BACKFILL_DEFAULT_CAP:
        out["backfill_capped_warning"] = (
            f"这个项目有 {len(eligible)} 条待回填, 超过 backfill 自己的上限 "
            f"{_BACKFILL_DEFAULT_CAP} —— 跑一次追不平, 得先把 "
            f"store.legacy_version_pages 的 limit 调高")
    return out


# ══════════════════════════════════════════════════════════════════════
# 反馈学习
# ══════════════════════════════════════════════════════════════════════

# ⚠️ 这里【故意没有】resolve_model / 任何 LLM 调用。
#
# deskcore 曾经自己拿 Anthropic 客户端做调校笔记蒸馏, 那违反了本方案自己的
# 原则 ——「推理归 WorkBuddy, MCP 只做轻量数据操作」。蒸馏现在由调用方模型做
# (见 build_distillation_task / save_my_style), 服务端只出材料和口径。
#
# 收益不只是"少一个依赖": deskcore 不再需要 ANTHROPIC_API_KEY / DESKCORE_MODEL,
# 少一个中转站故障点, 也不再有"回显的模型和实际调用的模型不同源"这类问题 ——
# 那个坑上一轮刚踩过(/health 读 env, core 读一个 config 里根本不存在的属性,
# 于是回显着一个从未被调用过的模型名)。没有模型可配, 就没有配错的余地。
#
# ⚠️ 加新功能时别顺手把 LLM 调用加回来。要模型干活就把材料和指令交出去,
# 让调用方的模型做 —— 它本来就跑在一个有模型的环境里。



def record_rule(client, project_id: str, content: str, *, severity: str = "soft",
                scope: str = "project", user_id: str | None = None,
                applicability: str = "") -> dict:
    """沉淀一条规则(团队共享)。severity='hard' 的下次进 P0。

    复用 db.upsert_memory —— 它带并发安全(keyed lock + CAS 重试)和
    rule_kind/rule_payload 的向后兼容 strip, 别绕过它自己 insert。
    force_confirmed=True: 这条路径是用户明确说「以后都这样」才走的, 不需要
    频次阈值(那是给自动抽取的候选留人工复核用的)。
    """
    # 审计 COR-015。scope='global' 时 project_id 其实不参与写入, 但照样校验 ——
    # 规则是"每个项目级入口都以这一行开头", 留例外就等于留一个以后会被忘掉的
    # 缺口, 而这里的代价只是一次主键查询。
    assert_project_access(client, project_id, user_id=user_id)
    content = (content or "").strip()
    if not content:
        raise ValueError("rule content must not be empty")
    severity = (severity or "soft").lower()
    if severity not in ("hard", "soft"):
        raise ValueError("severity must be 'hard' or 'soft'")
    if scope not in ("project", "global"):
        raise ValueError("scope must be 'project' or 'global'")

    row = db.upsert_memory(
        client, user_id=user_id, scope=scope, content=content,
        source_feedback="deskcore",
        project_id=project_id if scope == "project" else None,
        force_confirmed=True, severity=severity,
        # 用户是【明确指定】了 hard/soft 才走到这里的, 所以命中已存在的规则时
        # 要把 severity 也写回去。db.upsert_memory 默认不写(自动抽取传的
        # severity 是猜的, 会把手设的 hard 降回 soft), 这条路径必须显式打开 ——
        # 否则"把这条改成硬约束"会返回成功但库里还是 soft, 继续待在 P1。
        # (codex review round-5 P1)
        update_severity=True,
    )
    # 以【库里实际的值】为准回报, 别回报入参 —— 回报入参正是上面那个 bug 之所以
    # 看不见的原因: 库里没改, 返回值却说改了。
    stored = (row or {}).get("severity") or severity
    out = {"memory_id": (row or {}).get("id"), "severity": stored,
           "scope": scope, "content": content}
    if stored != severity:
        out["warning"] = (f"请求 severity={severity}, 但库里这条现在是 {stored} —— "
                          "以库里的为准, 请把这个差异告诉用户")

    # applicability 得单独写一次: db.upsert_memory 不认这个字段(它是 autowriter
    # 那边的老签名), 而它正是方向档的载体。
    # ⚠️ 写失败必须**上抛**, 不能吞。吞掉的话这条规则会以"无方向"落库 → 之后
    # 每一个方向的项目都注入它, 而调用方收到的是一个成功返回。
    applicability = (applicability or "").strip()
    if applicability and out["memory_id"]:
        bad = [t for t in DIRECTION_TAGS if t in applicability]
        if applicability not in DIRECTION_TAGS and bad:
            raise ValueError(
                f"applicability={applicability!r} 里带了方向词但不等于 "
                f"{list(DIRECTION_TAGS)} 之一。方向档是精确匹配的, "
                "半个词会让这条规则在两个方向上都被挡掉。")
        saved = store.update_memory_fields(
            client, out["memory_id"], {"applicability": applicability})
        out["applicability"] = (saved or {}).get("applicability")

    return out


# ══════════════════════════════════════════════════════════════════════
# 规则台账: 让写手自己看见、自己升降、自己停用
# ══════════════════════════════════════════════════════════════════════
#
# 这三个操作以前只有直连数据库才能做, 也就是只有运维能做。那意味着技艺库
# 的进化速度被"写手找运维"这个环节卡死, 而真正知道一条规则好不好用的只有
# 天天写的那个人。把控制权交到工具层, 这套东西才谈得上自己长。

_RULE_ACTIONS = ("mute", "unmute", "retire", "promote", "set_direction")


def my_rules(client, project_id: str, *, user_id: str) -> dict:
    """我这个项目上**全部**规则的台账 —— 含试用档和被我关掉的。

    与简报的口径差别是故意的: 简报只给"现在生效的", 台账要给"库里有的",
    否则写手看不见自己还有什么待裁决、什么被自己静音了。
    """
    project = assert_project_access(client, project_id, user_id=user_id)
    rows = store.rules_ledger(client, user_id=user_id, project_id=project_id)
    direction = detect_direction(project.get("name") or "")

    out = []
    for r in rows:
        muted = db.is_memory_muted_now(r.get("muted_until"))
        status = (r.get("status") or "candidate").lower()
        tag = (r.get("applicability") or "").strip()
        blocked = bool(direction) and bool([t for t in DIRECTION_TAGS if t in tag]) \
            and direction not in tag
        if status != "confirmed":
            state = "试用"          # 不进简报, 等升档
        elif muted:
            state = "已停用"
        elif blocked:
            state = "方向不符"      # 在别的方向上生效, 这个项目上不注入
        else:
            state = "生效中"
        out.append({
            "memory_id": r.get("id"),
            "content": r.get("content") or "",
            "state": state,
            "severity": (r.get("severity") or "soft").lower(),
            "scope": r.get("scope"),
            "direction": tag or "通用",
            "votes": r.get("frequency") or 1,
            "muted_until": r.get("muted_until"),
            "created_at": r.get("created_at"),
        })
    order = {"生效中": 0, "方向不符": 1, "试用": 2, "已停用": 3}
    out.sort(key=lambda x: (order.get(x["state"], 9), -int(x["votes"] or 1)))
    counts: dict = {}
    for r in out:
        counts[r["state"]] = counts.get(r["state"], 0) + 1

    # 缺向量的条数要露出来。没向量的规则不会报错、照样注入, 只是**不参与
    # 相关性筛选** —— 于是"这个项目跟这条规则根本不相干, 它却还是进了简报"
    # 这件事从外面完全看不出来。这是这套东西唯一一个还需要跑一次运维命令
    # (cli reembed-rules)才能补上的洞, 不显示出来就没人会想起去跑。
    # None = 查不出来(不是 0)。查不出来就什么都不显示, 别把"不知道"渲染成
    # 一个看起来正常的数字。
    missing = store.count_rules_missing_embedding(
        client, user_id=user_id, project_id=project_id)
    if missing:
        counts["缺向量"] = missing

    res = {
        "project_id": project_id,
        "project_name": project.get("name") or "",
        "project_direction": direction or "未判定",
        "rules": out,
        "counts": counts,
        "actions": ("改这些用 set_rule_state: mute(停用一段时间) / unmute(恢复) / "
                    "retire(降回试用档, 不再进简报) / promote(试用→生效) / "
                    "set_direction(设成 产品向 / 流量向 / 通用)"),
    }
    if missing:
        res["note_missing_embedding"] = (
            f"有 {missing} 条规则没有向量。它们照常注入, 但**不参与相关性筛选** —— "
            "也就是跟本次要写的东西不相干时也会进简报。"
            # ⚠️ 这句话是直接送到模型眼前的, 它会盖过 SKILL.md 里的指引。
            # 原来写的是"运维跑一次 cli reembed-rules" —— 那正好把用户推回
            # 运维环节, 而这一轮做 reembed_my_rules 就是为了消灭那个环节。
            # (codex review · PR #74)
            "**调 reembed_my_rules 就能补**(一次 50 条, 按返回的 remaining 重复调"
            "直到归零), 不需要找运维。"
            "走 record_rule 新记的规则会在写入时自动算(db.upsert_memory), "
            "所以这个数只会往下走, 不会自己涨。")
    return res


def reembed_my_rules(client, *, user_id: str, batch: int = 50) -> dict:
    """给**我自己**缺向量的规则补算, 一次一批。

    为什么做成工具而不是只留 CLI: 补向量需要 Supabase 的 service_role key 和
    GOOGLE_API_KEY, 两样都只在服务端有 —— 做成 CLI 就意味着每次都要有人进
    Railway 的 shell, 也就是"又得找运维"。这条路径只碰调用者自己名下的行
    (``db.backfill_memory_embeddings`` 里那句 ``.eq("user_id", user_id)``),
    所以它跟其它工具是同一套归属口径, 不是新开的管理面。

    **一次只补一批**: 补算要逐条调 embedding API, 一次几百条会把工具调用拖到
    超时。返回 ``remaining`` 让调用方自己决定要不要再来一次 —— 与 Streamlit
    那个「立即补算(最多 50 条)」按钮同一个节奏。
    """
    if not user_id:
        raise PermissionError("无法识别调用者身份, 拒绝补算")
    batch = max(1, min(int(batch or 50), 50))

    out = db.backfill_memory_embeddings(client, user_id, max_rows=batch)
    status = (out or {}).get("status")
    updated = int((out or {}).get("updated") or 0)

    # ⚠️ 失败状态必须**抛**, 不能返回一个带 error 字段的字典。
    # MCP 会把"返回了字典"当成调用成功, 于是调用方模型看到的是一次成功调用 +
    # 一个 remaining 数字, 完全可能当作"补完了"往下走。SKILL.md 里也写着这个
    # 工具"出错一律直接报错" —— 返回字典会让代码和文档对不上。
    # (codex review · PR #74 · P1)
    if status == "no_embedding_sdk":
        raise RuntimeError(
            "服务端没配 GOOGLE_API_KEY, 补算功能不可用 —— 这不是「没有需要补的」, "
            "是环境缺配置, 要告诉运维")
    if status == "schema_missing":
        raise RuntimeError(
            f"memories.embedding 列不存在, 需要先跑 pgvector 迁移: "
            f"{(out or {}).get('hint') or ''}")
    if status == "query_failed":
        raise RuntimeError(f"补算查询失败: {(out or {}).get('error')}")

    # ⚠️ 剩余条数**必须重新查库**, 不能拿"上次查到多少减去补了多少"去算 ——
    # backfill 对算不出向量的行是跳过的, 那种行会永远留在待补集合里。用减法
    # 会得出一个一直在降、最后归零的假数字, 而库里其实一条没少。
    #
    # ⚠️ 而且必须用**补算口径**(这个人名下所有 scope), 不是台账口径
    # (global + 单个项目)。backfill 只按 user_id 过滤, 台账口径会漏掉他在别的
    # 项目里的项目规则 → 第一批之后就报 remaining: 0 让人收工。
    # (codex review · PR #74 · P1)
    remaining = store.count_own_rules_missing_embedding(client, user_id=user_id)

    res = {"updated": updated, "remaining": remaining, "status": status}
    if status == "noop":
        res["note"] = "没有缺向量的规则了 —— 已经补齐。"
    elif updated == 0 and remaining:
        # 查到了却一条没补上 = 再调一次还是同一批, 明说别空转。
        res["warning"] = (f"这一批查到了缺向量的行却一条都没补上(还剩 {remaining} 条)。"
                          "**不要接着调** —— 再调还是同一批。服务端日志里有每行"
                          "失败的原因(telemetry: backfill_memory_row_failed)。")
    elif remaining:
        res["next"] = f"还剩 {remaining} 条, 再调一次这个工具接着补。"
    return res


def set_rule_state(client, memory_id: str, action: str, *,
                   user_id: str, days: int = 90, direction: str = "") -> dict:
    """写手自己改一条规则的档位。**不改内容** —— 改内容是 record_rule 的事。

    归属校验分两路, 因为这两种规则的归属定义本来就不同:
      · scope='global' —— 私人技艺库, 判 ``user_id`` 相等;
      · scope='project' —— 团队共享, 判项目归属(assert_project_access)。
    少判任何一路都等于让持 key 的人改别人的规则。
    """
    action = (action or "").strip().lower()
    if action not in _RULE_ACTIONS:
        raise ValueError(f"action 必须是 {list(_RULE_ACTIONS)} 之一, 收到 {action!r}")

    row = store.memory_row(client, memory_id)
    if row is None:
        raise ProjectNotFound(f"规则不存在: {memory_id}")
    if not db._is_rule_memory(row):
        raise ValueError(f"{memory_id} 不是一条写作规则(memory_type="
                         f"{row.get('memory_type')!r}), 拒绝改动")

    if (row.get("scope") or "") == "global":
        if str(row.get("user_id") or "") != str(user_id):
            raise PermissionError(
                "这条是别人的个人技艺库里的规则, 不能改。global 规则是私有的。")
    else:
        assert_project_access(client, row.get("project_id"), user_id=user_id)

    if action == "mute":
        days = int(days or 90)
        if days < 1:
            raise ValueError("mute 的天数至少是 1")
        until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        fields = {"muted_until": until}
    elif action == "unmute":
        fields = {"muted_until": None}
    elif action == "retire":
        # 降回试用档 = 不再进简报, 但**不删**。删了就没法回头看"这条当初为什么
        # 被记下来", 而技艺库的价值恰恰在那段演化史里。
        fields = {"status": "candidate"}
    elif action == "promote":
        fields = {"status": "confirmed"}
    else:                                   # set_direction
        direction = (direction or "").strip()
        if direction in ("", "通用"):
            fields = {"applicability": None}
        elif direction in DIRECTION_TAGS:
            fields = {"applicability": direction}
        else:
            raise ValueError(f"direction 只能是 {list(DIRECTION_TAGS)} 或 '通用', "
                             f"收到 {direction!r}")

    saved = store.update_memory_fields(client, memory_id, fields)
    return {
        "memory_id": memory_id,
        "content": (row.get("content") or "")[:120],
        "action": action,
        # 一律以库里的值回报。回报入参 = record_rule 那条 severity bug 的复刻。
        "now": {
            "status": (saved or {}).get("status"),
            "muted_until": (saved or {}).get("muted_until"),
            "applicability": (saved or {}).get("applicability") or "通用",
        },
    }


_CALIB_SYSTEM = """\
你在维护一份「个人写作调校笔记」。输入是同一个人对 AI 初稿做的手动精修 —— \
左边是 AI 写的，右边是这个人改成的样子。

你的任务：从这些改动里提炼出【这个人的语感偏好】，写成可执行的短句。

铁律：
1. 只写从改动里【看得出来】的偏好。看不出来就不写，宁可少写。
2. 不要复述具体内容（"把郁可唯改成了别的明星"没有价值）；要写模式\
（"倾向去掉明星名，用泛指的场景代替"）。
3. 每条一行，以动词开头，可以直接当写作指令用。
4. 合并同类项。新观察和已有笔记说的是一件事，就改写已有那条让它更准，\
不要并列两条。
5. 与已有笔记冲突时以新观察为准（人的偏好会变）。
6. 总长度控制在 20 行以内。超了就合并最弱的几条。

只输出笔记正文，不要解释，不要 markdown 标题。"""


def record_edit(client, project_id: str, *, user_id: str,
                ai_title: str = "", ai_body: str = "",
                my_title: str = "", my_body: str = "",
                note: str | None = None, distill: bool = True) -> dict:
    """喂一条手动精修 diff —— 【信号 A, 最高权重】, "裂变"的入口。

    口径照 memory.generate_calibration_notes(:1154-1210): 只收人真的动手改了的
    对子。没改就通过的稿子【不算教学材料】—— memory.py:1191-1194 明确拒绝从
    那里学, 理由是模型会从偶然选择里编造风格规则。
    """
    assert_project_access(client, project_id, user_id=user_id)   # 审计 COR-015
    if not (my_title or my_body):
        raise ValueError("my_title/my_body must not both be empty")
    if ((ai_title or "").strip() == (my_title or "").strip()
            and (ai_body or "").strip() == (my_body or "").strip()):
        return {"stored": False,
                "reason": "AI 版与手改版完全一致, 不是有效信号, 未入库"}

    store.add_style_edit(client, project_id, user_id,
                         ai_title=ai_title or "", ai_body=ai_body or "",
                         my_title=my_title or "", my_body=my_body or "", note=note)
    out: dict = {"stored": True}
    if not distill:
        out["next_step"] = ("本次没有要蒸馏(distill=false)。diff 已入库, "
                            "下次蒸馏时会连它一起算。")
        return out

    task = build_distillation_task(client, project_id, user_id=user_id)
    if task is None:                      # 理论上不会 —— 上面刚存了一条
        out["next_step"] = "没有可蒸馏的材料。"
        return out
    out["distillation_task"] = task
    out["next_step"] = (
        "⚠️ 这一步【还没做完】。请按 distillation_task.instruction 的口径, "
        "拿 existing_notes 和 edits 提炼出【更新后的完整笔记】, 然后调用 "
        "save_my_style 写回去, 并把 distillation_task.edit_ids 原样传回。"
        "不写回去的话这次精修等于白喂 —— diff 存下来了, 但文风不会变。")
    return out


def build_distillation_task(client, project_id: str, *, user_id: str,
                            max_edits: int = 8) -> dict | None:
    """把"该蒸馏什么"打包成一份【交给调用方模型做】的任务。返回 None 表示没料。

    ⚠️ 这里【不调 LLM】。deskcore 原来自己拿 Anthropic 客户端蒸馏, 那违反了本
    方案自己的原则 ——「推理归 WorkBuddy, MCP 只做轻量数据操作」。搬走之后
    deskcore 不再需要 ANTHROPIC_API_KEY / DESKCORE_MODEL, 也少一个中转站故障点;
    而写作台本来就跑在一个有模型的环境里, 让它做这点文本提炼是顺手的事。

    返回的三样东西调用方模型全都要用上:
      existing_notes —— 已有笔记, 要在它基础上改写而不是另起一份
      edits          —— 本次要吸收的手动精修对子
      instruction    —— 提炼口径(原 _CALIB_SYSTEM), 口径留在服务端是刻意的:
                        它是这套东西的一部分, 不该由每个平台各自发挥。
    """
    edits = store.recent_style_edits(client, project_id, user_id, max_edits)
    if not edits:
        return None
    existing, _ = store.get_user_calibration(client, project_id, user_id)

    blocks = []
    for e in edits:
        b = ("AI 原版：\n"
             f"  标题：{e.get('ai_title','')}\n"
             f"  正文：{(e.get('ai_body') or '')[:400]}\n"
             "手改版：\n"
             f"  标题：{e.get('my_title','')}\n"
             f"  正文：{(e.get('my_body') or '')[:400]}")
        if e.get("note"):
            b += f"\n  本人备注：{e['note']}"
        blocks.append(b)

    total_pending = store.count_pending_distillation(client, project_id, user_id)
    out = {
        "instruction": _CALIB_SYSTEM,
        "existing_notes": existing,
        "edits": "\n\n---\n".join(blocks),
        "edit_count": len(edits),
        # ⚠️ 这批的确切 id。save_my_style 只销这几条的账 —— 见
        # store.mark_edits_distilled 的说明(全量销账会吃掉快照外的行)。
        "edit_ids": [e["id"] for e in edits if e.get("id")],
    }
    if total_pending > len(edits):
        out["more_pending"] = total_pending - len(edits)
        out["note"] = (
            f"这个人还有 {total_pending - len(edits)} 条精修没进本次快照"
            f"(一次最多取 {max_edits} 条)。写回之后【再调一次 record_edit 或看 "
            "my_style】会拿到下一批, 别以为一次就吸收完了。")
    return out


def save_my_style(client, project_id: str, notes: str, *, user_id: str,
                  edit_ids: list[str] | None = None) -> dict:
    """把调用方模型蒸馏好的笔记写回, 并把【这一批】diff 标记为已吸收。

    ⚠️ 只有【真的写回来】才算完成一次学习。record_edit 只是把 diff 存下来 +
    把任务交出去; 中间断掉的话 diff 还在库里(下次还会拿到), 但笔记不会变 ——
    也就是"喂了稿子却没变得更像我"。my_style 的 pending_distillation 就是给
    这个断点用的可见性。

    ⚠️ edit_ids 必须原样回传 record_edit 给的那一份。不传 = 只改笔记、不销任何
    账 —— 这正是"用户说这条笔记不对, 直接改一下"那种用法应有的行为: 手动改写
    笔记【不等于】吸收了那些待处理的精修, 顺手把它们标掉会让它们静默消失。
    (codex review #56 P1)
    """
    assert_project_access(client, project_id, user_id=user_id)   # 审计 COR-015
    notes = (notes or "").strip()
    if not notes:
        raise ValueError("notes 不能为空 —— 空笔记会把已有的个人风格清掉")
    store.save_user_calibration(client, project_id, user_id, notes)
    marked = store.mark_edits_distilled(client, project_id, user_id, edit_ids or [])
    out = {"saved": True, "notes": notes, "lines": len(notes.splitlines()),
           "edits_absorbed": marked,
           "pending_distillation": store.count_pending_distillation(
               client, project_id, user_id)}
    if edit_ids and marked < len(edit_ids):
        out["warning"] = (
            f"传了 {len(edit_ids)} 个 edit_id 但只销掉 {marked} 条 —— "
            "多半是其中几条已经被别处吸收过了。笔记已保存。")
    if not edit_ids:
        out["note"] = ("没传 edit_ids, 所以只更新了笔记、没有销账。"
                       "如果这是在吸收精修而不是手动改写笔记, "
                       "要把 record_edit 返回的 edit_ids 原样传进来。")
    if out["pending_distillation"]:
        out["still_pending"] = (
            f"还有 {out['pending_distillation']} 条精修没被吸收 —— "
            "看 my_style 的 pending_distillation_task 接着做。")
    return out


def label_example(client, item_id: str, label: str | None,
                  *, user_id: str | None = None) -> dict:
    """标正/负例。复用 db.set_item_example_label —— 它顺带清三处缓存。

    ⚠️ 【必须校验归属】。items.example_label 是【个人】风格资产(私有层), 而
    deskcore 持 service_role 绕 RLS —— 没有这道校验的话, 任何拿到别人 item
    UUID 的调用方都能改别人的正负例池, 进而污染那个人的写作风格。
    RLS 在 Streamlit 路径下挡住了这件事, 到了 service_role 路径就得自己挡。

    ⚠️ 负例【只取人工标注】。TV D-040 讲得很清楚: 「赢」需要真的好, 「输」有
    太多无辜理由(撞流量墙 / 账号限流 / 时机), 从数据反推负例会把被埋没的
    好内容也标成垃圾, 污染负面特征库。
    """
    if label not in ("positive", "negative", None):
        raise ValueError("label must be 'positive', 'negative' or None")
    if not user_id:
        raise PermissionError(
            "label_example 需要调用者身份 —— 正负例是个人资产, 不能匿名改。"
            "服务端要配 DESKCORE_KEYS 或 DESKCORE_DEFAULT_USER_ID。")

    owner = store.item_owner(client, item_id)
    if owner is None:
        raise ValueError(f"item not found: {item_id}")
    if str(owner) != str(user_id):
        raise PermissionError(
            f"item {item_id} 属于别人, 不能改它的 example_label。"
            "正负例是个人风格资产(私有层), 项目规则才是团队共享的。")

    db.set_item_example_label(client, item_id, label)
    return {"item_id": item_id, "example_label": label}


def my_style(client, project_id: str, *, user_id: str) -> dict:
    """我在这个项目上的风格资产。"""
    # 校验 + 取行一次搞定(原来这里就要 get_project)。审计 COR-015
    project = assert_project_access(client, project_id, user_id=user_id)
    shared = (project or {}).get("calibration_notes") or ""
    mine, updated = store.get_user_calibration(client, project_id, user_id)
    pending = store.count_pending_distillation(client, project_id, user_id)
    out = {
        "project_id": project_id,
        "shared_calibration": shared.strip(),
        "my_calibration": mine,
        "my_calibration_updated_at": updated,
        "edits_fed": store.count_style_edits(client, project_id, user_id),
        "my_positive_examples": len(store.labeled_examples(client, project_id, "positive", user_id)),
        "my_negative_examples": len(store.labeled_examples(client, project_id, "negative", user_id)),
        # ⚠️ 蒸馏搬到调用方模型之后, record_edit 和 save_my_style 是两步。
        # 两步之间断掉 = diff 存了但笔记没更新, 也就是"喂了稿子却没变得更像我",
        # 而且【完全无声】。这个计数就是那个断点的可见性: 大于 0 说明有精修
        # 还没被吸收进笔记。
        "pending_distillation": pending,
    }
    # ⚠️ 光报个数字不够 —— 断点最典型的形态就是【会话没了】(超时、换了个
    # session), 那时 record_edit 那次的返回值也一起丢了。只给数字的话, 调用方
    # 拿不回材料和口径, 我在 SKILL.md 里写的"重新提炼一次写回"根本做不到,
    # 除非让用户把同一条精修再喂一遍(那会造重复行)。
    # 所以 pending > 0 时把任务原样带上, 恢复就是自明的。
    # (codex review #56 P2)
    if pending:
        task = build_distillation_task(client, project_id, user_id=user_id)
        if task:
            out["pending_distillation_task"] = task
            out["next_step"] = (
                "有精修还没被吸收进笔记。按 pending_distillation_task.instruction "
                "提炼出更新后的完整笔记, 调 save_my_style 写回, "
                "并把 task 里的 edit_ids 原样传回去。")
    return out


# ══════════════════════════════════════════════════════════════════════
# 飞轮经验卡(转调 TV 馆员)
# ══════════════════════════════════════════════════════════════════════

def borrow_lessons(client, project_id: str, *, user_id: str | None = None,
                   **delta) -> dict:
    """复用 librarian_client 的 build_brief + fetch_flywheel_lessons(R-032)。

    那边已经处理好 fail-open(超时/非 200/未配 → [], 绝不阻塞写稿)和 brief 的
    字段集对齐(docs/15 §0 契约)。这里只做项目查询 + 转发。

    ⚠️ 借来的经验卡本身是公司公共资产, 但**发给馆员的 brief 是拿项目行拼的**
    (品牌 / 定位 / 战术), 所以入口照样要校验归属 —— 否则它就成了一个"用别人的
    project_id 就能读出那个项目怎么定位"的接口。(审计 COR-015)
    """
    project = assert_project_access(client, project_id, user_id=user_id)
    brief = librarian_client.build_brief(project, **delta)
    brief["consumer"] = "deskcore"
    # ⚠️ 空列表有**五种**来路, 对调用方的含义完全不同(2026-09-16 评测 AW-05):
    # 没匹配上是正常的, 没配 key 是部署漏了, 超时是 TV 那边慢了。只回一个
    # count=0 的话, 模型只能猜, 而它多半会猜成"这个项目没有可借的经验"。
    st: dict = {}
    selected = librarian_client.fetch_flywheel_lessons(brief, status=st)
    return {"lessons": selected, "count": len(selected),
            "status": st.get("state"),
            "elapsed_ms": st.get("elapsed_ms"),
            "detail": st.get("detail") or ""}


def create_project(client, name: str, *, brand: str = "",
                   user_id: str | None = None) -> dict:
    """新建一个项目。owner **恒为调用者**, 不接受 owner 参数。

    为什么必须有这个工具(2026-08-27): 在此之前 deskcore 的十二个工具里没有
    "新建项目"这个动作 —— 建项目的代码只在停用中的 Streamlit 工作台里。
    于是现场卡成死结: 文案想给新品牌开项目, 模型如实回答"我没有这个工具";
    想跳过项目直接写, skill 又强制要求先 open_project 取硬约束。**新品牌
    完全进不来**, 而这一层在任何文档里都没写。

    ⚠️ 签名里【没有】owner_id, 这是安全属性不是疏忽。归属恒等于
    ``user_id``(即这把 key 映射到的人)。加一个 owner 参数就等于允许调用方
    往别人名下建项目 —— 今天刚踩过同类的坑: 一把标着"Ziao"的 key 实际指向
    同事的账号, 拿它写稿会把稿子记到别人的历史库里。归属只能由服务端定。

    撞名保护分两层, 都不是可选的:

    · **同名 → 不建**, 把已有项目的 id 还回去(``created=False``)。建重了的
      代价不是多一行: 两个同名项目 = 两套互不可见的历史库, 查重从此对这个
      方向失效, 而且**不报错**。宁可返回已有的。
    · **同品牌 → 照建, 但把兄弟项目列出来**(``siblings``)。"途鸽"已经有
      D-1..D-7 七个子项目, 再建一个叫"途鸽"的顶层项目多半是误操作, 但也可能
      是真的要开新方向 —— 这个只有人能判断, 所以给信息不拦。
    """
    if not user_id:
        raise PermissionError(
            "无法识别调用者身份, 不能建项目 —— 项目归属按 owner 隔离。"
            "服务端要配 DESKCORE_KEYS 或 DESKCORE_DEFAULT_USER_ID。")

    name = (name or "").strip()
    if not name:
        raise ValueError("project name must not be empty")
    brand = (brand or "").strip()

    dupes = store.projects_with_exact_name(client, owner_id=user_id, name=name)
    if dupes:
        hit = dupes[0]
        return {
            "project_id": hit["id"],
            "name": hit.get("name") or name,
            "brand": hit.get("brand") or "",
            "created": False,
            "note": (f"你名下已经有一个叫「{name}」的项目, 没有新建 —— "
                     "直接用这个 project_id。建同名的第二个会让历史稿分裂成"
                     "两套互不可见的库, 查重从此对这个方向失效。"
                     "确实要另开方向的话, 换一个能区分的名字。"),
        }

    row = db.create_project(client, user_id=user_id, name=name, brand=brand)
    pid = (row or {}).get("id")
    if not pid:
        # 不静默返回半个结果: 调用方拿不到 id 就没法接着 open_project, 而
        # "建成功了但没 id"会让人以为项目在库里, 下一步才炸且看不出根因。
        raise RuntimeError(
            f"建项目之后没拿到 id(name={name!r}) —— PostgREST 没回插入行, "
            "多半是 service client 的 returning 行为变了")

    siblings = [{"project_id": p["id"], "name": p.get("name") or ""}
                for p in store.projects_with_brand(client, owner_id=user_id,
                                                   brand=brand)
                if p["id"] != pid] if brand else []

    out = {"project_id": pid, "name": name, "brand": brand, "created": True,
           "siblings": siblings}
    if siblings:
        out["siblings_note"] = (
            f"品牌「{brand}」名下已经有 {len(siblings)} 个项目。新建的这个是"
            "独立的一套历史库, 跟它们【不互相查重】。如果本意是在已有方向下"
            "继续写, 应该用那个项目而不是这个新的 —— 告诉用户, 让他确认。")
    return out


def list_projects(client, *, user_id: str | None = None) -> list[dict]:
    """**我的**项目清单 + 每个项目手上有多少料。

    审计 COR-015: 这个清单原来返回**全库所有项目**(名称/品牌/owner_id/规则条数/
    指纹数), 而且是三个 needs_user=False 的工具之一 —— 也就是任一持有效 key 的
    调用方都能把整个库的项目台账拉出来, 顺带拿到一批可以喂给其它工具的
    project_id。现在按 owner 过滤, 判据收在 ``assert_project_access`` 的同一处口径。

    审计 SUP-004: 原来每个项目发 2 次查询(规则一次 + 指纹 count 一次), 40 个
    项目 = 81 次往返 —— 而这是模型最常调的第一个工具, 每次开工都要等它。
    更亏的是规则那次走的是 shared_memories, 它为了给相关性过滤准备数据会把每条
    规则的 **768 维 embedding** 一起拉回来, 而这里只用了两个 len()。

    现在是【3 次固定查询】: 项目清单 + 规则批量计数 + 指纹批量计数, 与项目
    个数无关(加 owner 过滤不改变这个性质 —— 它只是第一次查询多一个 .eq)。
    指纹计数走 migrations/004 的 RPC(PostgREST 不会 GROUP BY);
    RPC 没部署时退回逐项目 count —— 慢, 但清单仍然是对的。
    """
    if not user_id:
        raise PermissionError(
            "无法识别调用者身份, 不能列项目 —— 清单按 owner 隔离。"
            "服务端要配 DESKCORE_KEYS 或 DESKCORE_DEFAULT_USER_ID。")
    projects = store.list_all_projects(client, owner_id=user_id)
    if not projects:
        return []
    pids = [p["id"] for p in projects]

    rule_counts = store.rule_counts_bulk(client, pids)
    fp_counts = store.fingerprint_counts(client, pids)
    if fp_counts is None:
        # 迁移没跑: 退回 N 次 count。单个项目查失败按 0 计, 与旧行为一致 ——
        # 计数只是清单上的提示, 不该让整个 list_projects 挂掉。
        _rpc_missing_telemetry("deskcore_fingerprint_counts", pids[0])
        fp_counts = {}
        for pid in pids:
            try:
                fp_counts[pid] = (client.table("draft_fingerprints")
                                    .select("id", count="exact")
                                    .eq("project_id", pid).limit(1)
                                    .execute()).count or 0
            except Exception:
                fp_counts[pid] = 0

    out = []
    for p in projects:
        pid = p["id"]
        hard_n, soft_n = rule_counts.get(pid, (0, 0))
        out.append({"project_id": pid, "name": p.get("name") or "",
                    "brand": p.get("brand") or "", "owner_id": p.get("owner_id"),
                    "hard_rules": hard_n, "soft_rules": soft_n,
                    "fingerprint_count": int(fp_counts.get(pid, 0) or 0)})
    return out


# ══════════════════════════════════════════════════════════════════════
# 写作台 ↔ TV 的稿子对照 + 补录 (migrations/009, deskcore/tvlink.py)
# ══════════════════════════════════════════════════════════════════════

def _tv_owner(client, project_id: str) -> str:
    row = store.project_row(client, project_id)
    if row is None:
        raise ProjectNotFound(f"project not found: {project_id}")
    owner = row.get("owner_id")
    # 系统任务, 以项目 owner 的身份跑 —— 补录进去的稿子归 owner, 与运营在
    # WorkBuddy 里自己 commit 的一样。这里过一遍归属闸是为了让"每个项目级入口
    # 都调过 assert_project_access"这条不变量成立, 不是因为会拒(owner 必过)。
    assert_project_access(client, project_id, user_id=owner)
    return str(owner)


TV_SYNC_INGEST_CHUNK = 100    # 一次 ingest_published 补几条; 与 tools 的 50 上限无关


def _resolve_any_version(client, project_of: dict, version_id: str):
    """TV 自己带的 version_id 可能指向某个 item 的**旧版**(不是 best), 对照索引里
    只有 best 那一版。按对照表里的每个写作台项目再问一次库(items_for_versions
    只认本项目的 version, 别的项目的查不出来)。回 (project_id, item_id) 或 None。"""
    for pid in project_of:
        try:
            hit = store.items_for_versions(client, pid, [version_id]).get(version_id)
        except Exception:                            # noqa: BLE001
            logger.exception("tv_sync: 按 version_id 反查 item 失败 (project=%s)", pid)
            hit = None
        if hit:
            return pid, hit.get("item_id")
    return None


# 哪些对照可以写回 TV。ingested 排除: 版本是从这条笔记复制来的, 不是它的来源。
_TV_BACKFILL_KINDS = frozenset({"body_exact", "title_exact", "fuzzy", "tv_lineage"})


def _backfillable(link: dict) -> bool:
    return bool(link.get("version_id")) and link.get("match_kind") in _TV_BACKFILL_KINDS


def tv_sync(client, tv_project_id: str, *, dry_run: bool = False,
            write_tv: bool = False, since: str | None = None,
            rematch: bool = False) -> dict:
    """把一个 TV 项目的笔记对到写作台的版本; 对不上的补录进指纹库。

    ── 为什么是它, 不是让运营抄 ID ──────────────────────────────────
    2026-09-17: TV 5966 条笔记里带写作台 lineage 的是 0 条, 原设计让运营把
    export_drafts 的六个 ID 列手抄进飞书, 三周零匹配。两边在同一个库里, 内容
    都是写作台产的, 按内容对就行 —— 运营只管写和发。

    ── 流程 ────────────────────────────────────────────────────────────
      1. tv_project_map 告诉我们这个 TV 项目对应哪些写作台项目(WTG 有 15 个
         方向)、其中哪一个是补录目标(ingest_target)。
      2. 拉 TV 的笔记(RPC 跨 schema) + 各写作台项目的定稿版本。
      3. tvlink.match_note 逐条判: body_exact / title_exact / fuzzy / ambiguous /
         unmatched。TV 自己已经带 source_autowriter_version_id 的直接记 tv_lineage。
      4. unmatched → ingest_published(剥掉话题标签, source="tv:<项目>", 不过闸,
         按全文幂等), 建成的 version_id 记回对照。
      5. 对照写进 tv_note_links(upsert, 按 note_id)。已经对上且没要求 rematch
         的笔记跳过 —— 每天跑一次, 增量。
      6. ``write_tv`` 才把对照写回 TV 的 source_autowriter_* 两列(只填 NULL 的
         行; 那是 TV 的列, 默认不碰)。**只回写真对上的**(body_exact / title_exact
         / fuzzy / tv_lineage); ``ingested`` 的不写 —— 那些版本是从这条笔记复制
         来的, 写回去因果倒置(见 _backfillable)。

    ``dry_run``: 全部算、一行不写(对照不写、指纹不写、TV 不写), 报表照出。
    """
    maps = [m for m in store.tv_project_map(client) if m["tv_project_id"] == tv_project_id]
    if not maps:
        raise ValueError(f"tv_project_map 里没有 {tv_project_id!r} —— 先跑 "
                         "`python -m deskcore.cli tv-map add --tv-project … --project … "
                         "--ingest-target`")
    target = next((m for m in maps if m.get("ingest_target")), None)
    owners = {m["project_id"]: _tv_owner(client, m["project_id"]) for m in maps}

    versions = [tvlink.Version.from_row(v) for m in maps
                for v in store.versions_for_linking(client, m["project_id"])]
    index = tvlink.VersionIndex(versions)
    known = {v.version_id: v for v in versions}
    project_of = {m["project_id"]: m for m in maps}
    notes = [tvlink.Note.from_row(r) for r in store.tv_notes(client, tv_project_id, since=since)]
    existing = store.tv_links(client, tv_project_id)

    now = store.iso_now()
    counts: dict[str, int] = {k: 0 for k in (
        "already_linked", "tv_lineage", "body_exact", "title_exact", "fuzzy",
        "ambiguous", "unmatched", "no_body")}
    links: list[dict] = []
    to_ingest: list[tvlink.Note] = []
    ambiguous_samples: list[dict] = []

    def _link(n: tvlink.Note, kind: str, *, project_id=None, version_id=None,
              item_id=None, score=None, lag=None, candidates=None) -> dict:
        return {"note_id": n.note_id, "tv_project_id": tv_project_id,
                "project_id": project_id, "version_id": version_id, "item_id": item_id,
                "match_kind": kind, "score": score, "lag_days": lag,
                "candidates": candidates or None, "updated_at": now}

    for n in notes:
        prev = existing.get(n.note_id)
        if prev and prev.get("version_id") and not rematch:
            counts["already_linked"] += 1
            continue
        if n.tv_version_id:
            # TV 自己带的 lineage。只在那个 version 真在我们库里时才写 version_id ——
            # tv_note_links.version_id 有外键, 一个陈旧/别处的 id 会让整个 upsert
            # 分块失败; 认不出的记在 candidates 里给人看, 不写外键列。
            counts["tv_lineage"] += 1
            tvid = str(n.tv_version_id)
            v = known.get(tvid)
            hit = ((v.project_id, v.item_id) if v else _resolve_any_version(client, project_of, tvid))
            links.append(_link(n, "tv_lineage",
                               project_id=hit[0] if hit else None,
                               version_id=tvid if hit else None,
                               item_id=hit[1] if hit else None,
                               candidates=None if hit else [{"tv_version_id": tvid,
                                                              "note": "TV 带的 version_id 不在对照的写作台项目里"}]))
            continue
        if not n.body:
            # 没正文: 对不了, 也不能补 —— 空正文建出来的身份没有开头哈希和四字串,
            # 对查重是隐形的, 却会被报成"已补录"(codex #81 P2)。记下来给人看。
            counts["no_body"] += 1
            links.append(_link(n, "unmatched", candidates=[{"note": "TV 里没有正文, 无法对照也无法补录"}]))
            continue
        m = tvlink.match_note(n, index)
        counts[m.kind] += 1
        if m.version is not None:
            links.append(_link(n, m.kind, project_id=m.version.project_id,
                               version_id=m.version.version_id, item_id=m.version.item_id,
                               score=m.score, lag=m.lag_days, candidates=m.candidates or None))
        elif m.kind == "ambiguous":
            links.append(_link(n, "ambiguous", score=m.score, candidates=m.candidates))
            if len(ambiguous_samples) < 10:
                ambiguous_samples.append({"note_id": n.note_id, "title": n.title[:40],
                                          "candidates": m.candidates[:3]})
        else:
            to_ingest.append(n)

    ingest: dict | None = None
    dup_of: dict[int, int] = {}          # to_ingest 下标 → 正文相同的第一条的下标
    if to_ingest and target is not None:
        # 先在整批里按 ingest 同一把尺子(开头哈希 + 四字串 sketch)去重, 再分块:
        # 否则跨块的重复在 dry-run 里被数两次、真跑时第二份被判"已有指纹", 两边
        # 的 to_write 对不上(codex #82)。重复的那条最后链到第一条建成的版本。
        seen_key: dict[tuple, int] = {}
        uniq: list[int] = []
        for i, n in enumerate(to_ingest):
            key = (fp.opening_hash(n.body), tuple(sorted(fp.ngram_hashes(n.body))))
            first = seen_key.get(key) if key[0] else None
            if first is None:
                if key[0]:
                    seen_key[key] = i
                uniq.append(i)
            else:
                dup_of[i] = first
        # 几百条分几次调: 每次一把锁(TTL 内做得完)、请求体有上限、半途失败只影响
        # 一批。各批的计数合并成一份 ingest 报表, written 的下标换算回整体下标。
        ingest = {"received": len(to_ingest), "to_write": 0,
                  "skipped_already_fingerprinted": 0,
                  "skipped_duplicate_in_sheet": len(dup_of), "minted": 0, "fingerprinted": 0,
                  "batch_id": None, "batch_ids": [], "identity_error": None,
                  "fingerprint_error": None, "embedded": False, "dry_run": dry_run,
                  "written": [], "skipped_after_failure": 0}
        for start in range(0, len(uniq), TV_SYNC_INGEST_CHUNK):
            idxs = uniq[start:start + TV_SYNC_INGEST_CHUNK]
            entries = [{"title": to_ingest[i].title, "body": to_ingest[i].body} for i in idxs]
            part = ingest_published(client, target["project_id"], entries,
                                    user_id=owners[target["project_id"]],
                                    source=f"tv:{tv_project_id}", dry_run=dry_run)
            for k in ("to_write", "skipped_already_fingerprinted",
                      "skipped_duplicate_in_sheet", "minted", "fingerprinted"):
                ingest[k] += part.get(k, 0)
            ingest["embedded"] = ingest["embedded"] or bool(part.get("embedded"))
            if part.get("batch_id"):
                ingest["batch_ids"].append(part["batch_id"])
                ingest["batch_id"] = ingest["batch_id"] or part["batch_id"]
            for key in ("identity_error", "fingerprint_error"):
                if part.get(key) and not ingest[key]:
                    ingest[key] = part[key]
            ingest["written"].extend({"index": idxs[w["index"]], "version_id": w["version_id"]}
                                     for w in part.get("written", []))
            if part.get("fingerprint_error"):
                # 指纹写失败后别再往下补(先 backfill); 没处理到的要数出来并说清:
                # backfill 之后还得再跑一次 tv-sync 补它们(codex #82 P1)。
                ingest["skipped_after_failure"] = len(uniq) - (start + len(idxs))
                break
        by_index = {w["index"]: w["version_id"] for w in ingest.get("written", [])}
        for i, first in dup_of.items():
            if first in by_index:
                by_index[i] = by_index[first]
        item_of: dict[str, dict] = {}
        if by_index and not dry_run:
            try:
                item_of = store.items_for_versions(client, target["project_id"],
                                                   list(by_index.values()))
            except Exception:                        # noqa: BLE001
                logger.exception("tv_sync: 取补录稿子的 item_id 失败, 对照先不带 item_id")
        for i, n in enumerate(to_ingest):
            vid = by_index.get(i)
            why = None
            if not vid and ingest.get("fingerprint_error"):
                why = [{"note": "补录半途指纹写失败, 这条还没处理: 先 backfill, 再跑一次 tv-sync"}]
            links.append(_link(n, "ingested" if vid else "unmatched",
                               project_id=target["project_id"], version_id=vid,
                               item_id=(item_of.get(vid) or {}).get("item_id") if vid else None,
                               candidates=why))
    else:
        for n in to_ingest:
            links.append(_link(n, "unmatched"))

    written_links = 0
    backfilled = 0
    if not dry_run:
        written_links = store.tv_links_upsert(client, links)
        if write_tv:
            # ingested 不回写(TV 2026-09-18 核对时的条件): 那些版本是我们从这条
            # 笔记复制进写作台的, 写回去等于说"这条笔记来源于版本 X"而 X 来源于
            # 这条笔记 —— 因果倒置, TV 的模型对比视图会多出上千行没信息量的
            # deskcore。它们留在 tv_note_links 里就够了(查重靠的是指纹, 不靠 TV
            # 那两列)。
            fresh = {l["note_id"] for l in links}
            pending = [l for l in links if _backfillable(l)]
            pending += [dict(r, note_id=nid) for nid, r in existing.items()
                        if _backfillable(r) and not r.get("synced_to_tv_at")
                        and nid not in fresh]
            backfilled = store.tv_backfill_lineage(client, pending)
            if pending:
                store.tv_mark_synced(client, [l["note_id"] for l in pending])

    matched = counts["body_exact"] + counts["title_exact"] + counts["fuzzy"]
    return {
        "tv_project_id": tv_project_id,
        "desk_projects": [m["project_id"] for m in maps],
        "ingest_target": target["project_id"] if target else None,
        "versions": len(versions), "notes": len(notes),
        "counts": counts, "matched": matched,
        "ingest": ingest, "links_written": written_links,
        "tv_backfilled": backfilled, "write_tv": write_tv, "dry_run": dry_run,
        "ambiguous_samples": ambiguous_samples,
        "note": (f"{len(notes)} 条笔记: 已对上 {counts['already_linked']}, 本次对上 {matched}"
                 f"(开头 {counts['body_exact']} / 标题 {counts['title_exact']} / 模糊 "
                 f"{counts['fuzzy']}), 分不出 {counts['ambiguous']}, 对不上 "
                 f"{counts['unmatched']}"
                 + (f", 没正文 {counts['no_body']}" if counts["no_body"] else "")
                 + (f" → 补录 {ingest['minted']}/{ingest['to_write']}"
                    if ingest and not dry_run else
                    f" → 会补录 {ingest['to_write']}" if ingest else
                    " → 没有补录目标, 只记 unmatched" if (to_ingest and target is None) else
                    " → 没有要补录的")),
    }
