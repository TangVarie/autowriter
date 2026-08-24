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

import hashlib
import logging
import random

import config
import db
import dedup
import librarian_client
import memory

from . import fingerprint as fp
from . import store, vocab

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


def _history_probe(client, project_id: str, o_hashes: list[str],
                   grams: list[set], new_vecs):
    """返回 ``(probe, hist_size, hist_with_vec, hist_truncated)``。

    ``probe(i)`` → ``(best_sim, sim_title, best_j, j_title, exact, open_title)``
    —— 第 i 篇草稿与【历史】比对的三路最佳命中。title 为 None 表示这一路没有
    命中(不是"命中了一条空标题")。

    两条实现共用这一个形状:

      A. 下推路径(审计 SUP-002 / ROB-004 / ROB-011, 需要 migrations/004)
         整个比对在库里做完, 只回每篇一行结果。原来是把 4000 行 × 768 维拉进
         Python 逐对算 —— 百 MB 传输 + 三千万次乘加 + 占满线程池。

      B. Python 路径(migrations/004 没跑时)
         与下推前完全一致, 包括 4000 条的上限和 history_truncated 的警告。

    ⚠️ 只有"RPC 不存在"才降级到 B。查重是 deskcore 唯一不 fail-open 的路径,
    其它异常(权限 / 库故障)一律冒泡。
    """
    payload = [
        {
            "opening_hash": o_hashes[i] or None,
            "ngram_hashes": sorted(grams[i]),
            # 本批算不出向量时传 null, 库里那一路直接跳过 —— 与 Python 路径的
            # `if new_vecs and i < len(new_vecs)` 等价。
            "title_embedding": (f"[{','.join(repr(float(x)) for x in new_vecs[i])}]"
                                if new_vecs and i < len(new_vecs) else None),
        }
        for i in range(len(o_hashes))
    ]
    rows = store.check_drafts_sql(client, project_id, payload)   # 故意让异常冒泡
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
        total, with_vec = store.fingerprint_stats(client, project_id)

        def _probe(i: int):
            r = by_idx.get(i) or {}
            # NUMERIC 在 PostgREST 上可能回数字也可能回字符串, float() 两种都吃。
            # open_exact 不看它自己的布尔值而是看 open_title 有没有 ——
            # 万一布尔被序列化成字符串, `bool("false")` 是 True, 而
            # opening_exact 是**单独就判死**的最强信号, 没有任何东西兜得住
            # 这个误伤。RPC 里那一列本来就是 `open_title IS NOT NULL` 算出来的,
            # 这么取口径完全一致, 只是不依赖布尔的传输形态。
            return (
                float(r.get("best_sim") or 0.0),
                r.get("sim_title"),
                float(r.get("best_j") or 0.0),
                r.get("j_title"),
                r.get("open_title") is not None,
                r.get("open_title"),
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
    hist_vecs = [h.get("title_embedding") for h in history]
    with_vec = sum(1 for v in hist_vecs if v)

    def _probe_py(i: int):
        best_sim, sim_hit = 0.0, None
        if new_vecs and i < len(new_vecs):
            for hi, emb in enumerate(hist_vecs):
                if not emb:
                    continue
                s = dedup.cosine_similarity(new_vecs[i], emb)
                if s > best_sim:
                    best_sim, sim_hit = s, history[hi]
        best_j, j_hit = 0.0, None
        for hi, hg in enumerate(hist_grams):
            j = fp.jaccard(grams[i], hg)
            if j > best_j:
                best_j, j_hit = j, history[hi]
        open_hit = hist_open.get(o_hashes[i]) if o_hashes[i] else None
        return (
            best_sim, (sim_hit or {}).get("title", "") if sim_hit else None,
            best_j, (j_hit or {}).get("title", "") if j_hit else None,
            open_hit is not None,
            (open_hit or {}).get("title", "") if open_hit else None,
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
    写稿, 但查重挂了必须抛 —— 静默放行就是重演 config.py:132 那个
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
    o_hashes = [fp.opening_hash(b) for b in bodies]
    grams = [set(fp.ngram_hashes(b)) for b in bodies]

    new_vecs = dedup.embed_texts(titles) if dedup.embeddings_available() else None

    # ── 与历史比对: 优先下推到库里(审计 SUP-002 / ROB-004 / ROB-011)──────
    # ``hist`` 是一个「按 i 取三路最佳命中」的可调用对象, 两条路径共用同一个
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
    for i in range(len(drafts)):
        best_sim, sim_hit, best_j, j_hit, exact, open_hit = hist(i)

        # ⚠️ 每个信号的最佳命中【各记各的】, 且各自记清楚是本批内还是历史。
        # 原来共用一个 intra 变量, 只要任一信号的最佳命中来自本批内就无条件
        # 认定"撞的是本批内那篇" —— 但真正让 verdict 判死的可能是另一个信号、
        # 而那个信号的命中在历史里。于是报出去的 collided_with 是一条根本没
        # 引发拒绝的稿子, 人对着它改, 改完还是过不了。(codex review)
        # hit = (标题, 归属)  归属 ∈ {"本批内", "历史"}
        sim_best = (sim_hit or "", "历史") if sim_hit is not None else None
        j_best = (j_hit or "", "历史") if j_hit is not None else None
        open_best = (open_hit or "", "历史") if open_hit is not None else None

        for k in range(i):   # 本批内互比: 同批两篇撞车同样要拦
            # o_hashes[i] 为空 = 这篇没有正文开头。空 == 空【不算撞车】——
            # 否则两篇 title-only 的稿子会被判成开头完全一致(见
            # fp.opening_hash 的说明)。
            if o_hashes[i] and o_hashes[i] == o_hashes[k]:
                exact, open_best = True, (titles[k], "本批内")
                break
            jj = fp.jaccard(grams[i], grams[k])
            if jj > best_j:
                best_j, j_best = jj, (titles[k], "本批内")
            if new_vecs:
                s = dedup.cosine_similarity(new_vecs[i], new_vecs[k])
                if s > best_sim:
                    best_sim, sim_best = s, (titles[k], "本批内")

        status, reason, which = fp.deciding_signals(best_sim, exact, best_j)
        by_signal = {"opening": open_best, "title": sim_best, "ngram": j_best}
        # 按 verdict 实际依据的信号取命中; 两个弱信号并列时取第一个(它排在
        # reason 的最前面, 与用户读到的解释对得上)。pass 时没有依据信号,
        # 退回"最强的那个"只为让人知道最接近的是什么, 并注明是参考。
        hit = next((by_signal[s] for s in which if by_signal.get(s)), None)
        informational = False
        if hit is None:
            hit = open_best or sim_best or j_best
            informational = hit is not None
        collided, scope = hit if hit else ("", "")
        row = {
            "index": i, "title": titles[i], "status": status, "reason": reason,
            "collided_with": collided,
            "collided_scope": scope,
            "decided_by": which,
            "signals": {"title_similarity": round(best_sim, 4),
                        "opening_exact_match": exact,
                        "body_ngram_jaccard": round(best_j, 4)},
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
    }
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


def _placeholder_version_id(seed: str) -> str:
    """没有真实 version_id 时(稿子在 WorkBuddy 里写, 不落 autowriter.versions)
    造一个确定性 UUID, 只为把 consumed_version_id 置成非 NULL 表示"用掉了"。"""
    h = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def commit_drafts(client, project_id: str, drafts: list[dict],
                  *, user_id: str | None = None) -> dict:
    """定稿入库: 写指纹(同一事务内重查) + 给坐标销账。

    只收真正定稿的 —— 指纹库脏了(把废稿也记进去)会让后续正常选题被误杀。

    ⚠️ 入库【会再查一次重】。check_drafts 和本调用之间可能有别人先 commit 了
    撞车的稿子(两人各自 check 时看到的是同一份旧指纹集), 那条竞态窗口只能在
    写入的同一个事务里关掉。被判撞车的条目【不入库】, 在返回值的 rejected 里
    列出来, 调用方要让用户重写。
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
    # 指纹, 而且【补不回来】—— backfill 走的是 items×versions, WorkBuddy 写的
    # 稿子 version_id 为空、根本不在 autowriter.versions 里, backfill 永远看不到。
    # 结果就是查重从此对那批内容有个洞, 不报错、也没人知道。
    embed_configured = dedup.embeddings_available()
    vecs = dedup.embed_texts(titles) if embed_configured else None
    embed_failed = embed_configured and not vecs

    rows = []
    for i, d in enumerate(drafts):
        body = d.get("body") or ""
        emb = vecs[i] if vecs and i < len(vecs) else None
        rows.append({
            "version_id": d.get("version_id"),
            "title": titles[i],
            "opening": fp.opening_of(body),
            # RPC 侧按 text 转 vector, 这里给 pgvector 的字面量形式
            "title_embedding": ("[" + ",".join(repr(float(x)) for x in emb) + "]") if emb else None,
            "embedding_model": dedup.EMBEDDING_MODEL if emb else None,
            "opening_hash": fp.opening_hash(body),
            "ngram_hashes": fp.ngram_hashes(body),
            "angle_key": d.get("angle_key"),
        })

    outcome = store.commit_fingerprints_atomic(
        client, project_id, rows, user_id, fp.NGRAM_JACCARD_HARD)

    atomic = outcome is not None
    rejected: list[dict] = []
    if atomic:
        by_idx = {o["idx"]: o for o in outcome}
        written = sum(1 for o in outcome if o.get("status") == "inserted")
        for o in outcome:
            if o.get("status") == "rejected":
                rejected.append({
                    "index": o["idx"],
                    "title": titles[o["idx"]] if o["idx"] < len(titles) else "",
                    "collided_with": o.get("collided_with") or "",
                    "reason": o.get("detail") or "与库中已有稿件重复",
                })
    else:
        # RPC 不在: 降级直插, 并明确标出这次没有关掉竞态窗口。
        payload = []
        for i, r in enumerate(rows):
            payload.append({
                "project_id": project_id, "user_id": user_id,
                "version_id": r["version_id"], "title": r["title"],
                "opening": r["opening"],
                "title_embedding": vecs[i] if vecs and i < len(vecs) else None,
                "embedding_model": r["embedding_model"],
                "opening_hash": r["opening_hash"],
                "ngram_hashes": r["ngram_hashes"], "angle_key": r["angle_key"],
            })
        written = store.write_fingerprints(client, payload)

    # 只给真的入了库的坐标销账 —— 被拒的那条角度还没产出成稿, 不该占坑。
    inserted_idx = ({o["idx"] for o in outcome if o.get("status") == "inserted"}
                    if atomic else set(range(len(drafts))))
    consumed = 0
    attempted = 0
    for i, d in enumerate(drafts):
        if i not in inserted_idx:
            continue
        key = d.get("angle_key")
        if not key:
            continue
        attempted += 1
        vid = d.get("version_id") or _placeholder_version_id(key)
        if store.consume_angle(client, project_id, key, vid):
            consumed += 1

    out = {"written": written, "consumed_angles": consumed,
           "embedded": bool(vecs), "rejected": rejected,
           "atomic_recheck": atomic,
           "embedding_model": dedup.EMBEDDING_MODEL if vecs else None}
    if embed_failed and written:
        # 配了 embedding 却没拿到向量 = 故障(欠费/配额/网络), 不是"没配"。
        # 这几行【补不回来】: backfill 走 items×versions, 而这些稿子的
        # version_id 多半是空的、根本不在那张表里。所以必须当场说, 让人
        # 决定是先修 key 再 commit, 还是接受这批只有确定性指纹。
        out["embedding_warning"] = (
            f"配了 embedding 但本次取向量失败, {written} 条是【没有标题向量】入库的。"
            "它们以后只参与确定性查重(开头精确 + 四字串重合), 同角度换说法的标题"
            "比不出来。常见原因是 key 欠费/配额用尽/网络不通。"
            "⚠️ backfill 补不了这些行(它只扫 autowriter.versions), 要补得用 "
            "`python -m deskcore.cli reembed --project <id>`。")
    if attempted and consumed < attempted:
        # consume_angle 现在会在"台账里根本没这一行"时返回 False(见 store 里的
        # 说明)。差额必须说出来 —— 这些坐标下一批还会被抽到, 悄悄少算等于
        # 避重失效了却没人知道。
        out["angle_ledger_warning"] = (
            f"{attempted - consumed}/{attempted} 个坐标没能在台账上销账(多半是发牌时"
            "台账没写进去)。这些坐标下一批可能被重复抽到, 服务端日志有明细。")
    if rejected:
        out["note"] = (f"{len(rejected)} 条在入库时被判与库中已有稿件重复 —— "
                       "多半是你 check 之后、commit 之前有人先提交了撞车的稿子。"
                       "这几条没有入库, 要重写后重新走 check_drafts。")
    if not atomic:
        out["warning"] = ("本次入库没有做原子重查(deskcore_commit_fingerprints RPC "
                          "不存在, migrations/001 可能没跑)。并发 check/commit 时"
                          "可能有撞车的稿子一起进库。")
    return out


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

    典型触发场景: embedding 的 key 欠费/配额用尽那几天照常 commit 了稿子,
    它们只有确定性指纹。补上 key 之后跑这个。
    """
    if not dedup.embeddings_available():
        return {"error": "embedding 不可用(GOOGLE_API_KEY 未配或 SDK 缺失), 无法补向量",
                "fixed": 0, "pending": None}

    rows = store.fingerprints_missing_vectors(client, project_id)
    if not rows:
        return {"pending": 0, "fixed": 0, "failed": 0,
                "note": "没有缺向量的行, 不用补。"}

    fixed = failed = 0
    for start in range(0, len(rows), chunk):
        part = rows[start:start + chunk]
        vecs = dedup.embed_texts([r.get("title") or "" for r in part])
        if vecs is None:
            failed += len(part)
            logger.warning("reembed: embed_texts failed for chunk at %d", start)
            continue
        for r, v in zip(part, vecs):
            if v and store.set_fingerprint_vector(client, r["id"], v,
                                                  dedup.EMBEDDING_MODEL):
                fixed += 1
            else:
                failed += 1
        if progress:
            progress(min(start + chunk, len(rows)), len(rows))

    out = {"pending": len(rows), "fixed": fixed, "failed": failed,
           "embedding_model": dedup.EMBEDDING_MODEL}
    if failed:
        out["note"] = (f"{failed} 条没补上(embedding 调用失败或行已被改动)。"
                       "可以再跑一次, 已补好的不会重复处理。")
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
        # 缺向量的才补算, 有的直接复用(见 docstring)
        need = [i for i, r in enumerate(part) if not r.get("embedding")]
        fresh: list[list[float]] | None = None
        if can_embed and need:
            fresh = dedup.embed_texts([part[i]["title"] for i in need])
            if fresh is None:
                logger.warning("backfill: embed_texts failed for a chunk of %d; "
                               "writing those rows without vectors", len(need))

        payload = []
        for i, r in enumerate(part):
            vec = r.get("embedding")
            if vec:
                reused += 1
            elif fresh is not None:
                pos = need.index(i)
                vec = fresh[pos] if pos < len(fresh) else None
                if vec:
                    computed += 1
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
                # 复用的历史向量也是同一个模型产的(versions.embedding 就是
                # 标题向量), 所以两条路径记的是同一个名字。
                "embedding_model": dedup.EMBEDDING_MODEL if vec else None,
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
              "同角度换说法的标题比不出来。配好 embedding 后重跑本命令不会重复写入, "
              "但也【不会】给已写入的行补向量 —— 要补得先删掉这些行。")
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
                scope: str = "project", user_id: str | None = None) -> dict:
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
    return out


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
    selected = librarian_client.fetch_flywheel_lessons(brief)
    return {"lessons": selected, "count": len(selected)}


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
