"""generation_service.py — 生成编排, **不依赖 Streamlit**。

审计 ROB-001 / ROB-002 / SUP-011 / SUP-012 是同一件事的四个症状: 队列与快速
生成的编排逻辑长在 ``app.py`` 里, 只能跟着 Streamlit 进程活。于是——

  · **ROB-001** 进程重启(发布 / OOM / 闲置回收) → 正在跑的队列整批蒸发,
    DB 里留下半成品 batch, UI 连"曾经在跑"都看不到;
  · **ROB-002** 用户 JWT 中途过期 → 剩下的 plan 全挂(而错误被 catch 成字符串);
  · **SUP-011** ``worker.py`` 那套 job 队列(领取 / 心跳 / sweeper / 重试)全套
    部署好了, 却只挂着一个 ``noop`` handler —— 运维成本照付, 问题一个没解;
  · **SUP-012** ``_queue_worker_impl`` 与 ``_quick_gen_worker`` 是两份重复编排,
    ``app.py:3134`` 的注释自陈已经漂移过一次。

这个模块是那四条的**前置**: 先把编排搬出 Streamlit, worker 进程才 import 得动。

── 这次搬迁是【逐字节】的 ────────────────────────────────────────────────
本文件的函数体与搬走之前**一个字符都没改**。这是刻意的: 搬家的风险不在某一行
写错, 而在搬完之后没人说得清"行为有没有变"。所以这一步只做移动, 不做任何
清理、重命名或合并 —— 那些放到后面单独的步骤, 每步都能单独 review。
``tests/test_generation_service_move.py`` 用 AST 把这件事钉死。

搬得动的前提是它们本来就不碰 ``st.*``: 十四个编排函数里 **st 属性访问是 0 次**
(用 AST 数过, 不是肉眼看的)。真正把它们锁死在 app.py 里的只有"定义写在那儿"
这一件事。

── 依赖 ──────────────────────────────────────────────────────────────────
只依赖纯业务模块(config / db / generator / memory / dedup / validator /
projects / librarian_client / telemetry)。**本模块不 import streamlit, 也不调
任何 ``st.*``** —— 这是它存在的全部意义, 加之前请先想清楚。

⚠️ 但 ``import streamlit`` 仍然会**间接**发生: ``db`` 里有个 ``st.cache_data``
的缓存 shim。所以判据不是"sys.modules 里有没有 streamlit"(那永远是有), 而是
**本模块自己碰不碰 ``st``**。``worker.py`` 早就在同一条件下跑了 —— 它需要的是
"不依赖 ScriptRunContext", 不是"streamlit 不在内存里"。

⚠️ 在 worker 进程里跑之前必须先设 ``AW_DISABLE_ST_CACHE=1``(``worker.py``
在 import db 之前就设了)。不设的话缓存 shim 会走真的 ``st.cache_data`` ——
跨进程缓存, app 那边的 ``.clear()`` 失效不了它, handler 会拿到 30-60 秒的旧
记忆/旧批次。这是 R-042 记过的坑。
"""

from __future__ import annotations

import threading
import uuid
from typing import Optional

import config
import db
import dedup as dedup_module
import generator as gen_module
import librarian_client
import memory as mem_module
import projects as proj_module
import telemetry
import validator

class _NullLock:
    """无操作的 context manager，用于老 session_state 没装 _lock 时的兜底。

    Day 4 加的线程态硬化里，启动后才会往 status["_lock"] 写真锁；如果用户
    在升级前的旧 session 里点了"清除"再点"启动"，过渡期可能读不到锁。
    用这个 placeholder 让 `with x or _NULL_LOCK:` 永远不报错。
    """
    def __enter__(self):
        return self
    def __exit__(self, *exc) -> None:
        return None


_NULL_LOCK = _NullLock()


def _resolve_queue_strategy(plan: dict, project: dict) -> dict:
    """根据"计划级 > 项目级 > 全局默认"优先级解析队列策略 (Day 5)。

    返回 dict 给 _run_semantic_dedup_pass 的 regen_ctx 覆盖用：
      - ``enabled``           — 是否开启自动重生
      - ``max_retries``       — 重生最多重试次数
      - ``threshold_override``— 阈值覆盖（None 表示用项目级或全局默认）

    策略含义：
      - ``stable``     ：阈值 0.95、重生开启、重试 3 次 → 重复率最低，速度慢
      - ``throughput`` ：阈值不覆盖、重生关闭 → 速度最快，重复率可能上升
      - ``None`` / 未配置：用 config 默认值（与改造前完全一致）
    """
    s = (plan or {}).get("strategy")
    if not s or s == "default":
        s = (project or {}).get("queue_strategy")
    if s == "stable":
        return {
            "enabled":            True,
            "max_retries":        3,
            "threshold_override": 0.95,
        }
    if s == "throughput":
        return {
            "enabled":            False,
            "max_retries":        int(getattr(config, "DEDUP_REGEN_MAX_RETRIES", 2)),
            "threshold_override": None,
        }
    return {
        "enabled":            bool(getattr(config, "ENABLE_DEDUP_REGEN", False)),
        "max_retries":        int(getattr(config, "DEDUP_REGEN_MAX_RETRIES", 2)),
        "threshold_override": None,
    }


# ── Generation Queue ────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────
# 进度条阶段权重（A3 修复：批次内的进度条之前只在 LLM 阶段动，setup /
# db_save / embedding 三个阶段什么都不显示，给用户"卡住了"的错觉）
#
# 用法：在每个阶段开始时调用 _set_phase_progress(status, "<phase>")；
# LLM 阶段的 _progress 回调通过 _llm_intra_progress 把 [0,1] 映射到 LLM
# 段的实际占比，避免覆盖前面阶段的进度。
# ─────────────────────────────────────────────────────────────────────────

_PHASE_WEIGHTS = {
    "setup":     (0.00, 0.05),   #  0% → 5%   读 DB / 拼 prompt
    "llm":       (0.05, 0.75),   #  5% → 75%  生成（占绝大部分时间）
    "db_save":   (0.75, 0.90),   # 75% → 90%  批量落库
    "embedding": (0.90, 1.00),   # 90% → 100% 语义查重 + embedding 持久化
}

# 跨批语义去重池的滑窗上限。长队列下同一项目会持续 append，O(n) 比较成本
# 与重复条目噪声都会上升；2000 条对应一个项目近期约 1-2 周的产出，足够
# "下一批别和最近 X 批撞" 的语义而不至于让 pool 变成全表扫描。
_QUEUE_POOL_MAX = 2000


def _set_phase_progress(status: dict, phase: str, intra: float = 0.0) -> None:
    """把 status["progress"] 设置到指定阶段的某个内部进度。

    ``phase`` 必须是 _PHASE_WEIGHTS 里的 key；``intra`` ∈ [0, 1] 表示
    该阶段内部的完成度（默认 0 = 阶段刚开始）。出参写到 status，不返回。
    """
    if phase not in _PHASE_WEIGHTS:
        return
    lo, hi = _PHASE_WEIGHTS[phase]
    intra = max(0.0, min(1.0, intra))
    status["progress"] = lo + (hi - lo) * intra


def _llm_intra_progress(status: dict, pct: float, msg: str) -> None:
    """LLM 引擎的 progress_callback 适配器：把引擎层的 [0,1] 映射到
    LLM 阶段在整批的占比，避免把 db_save / embedding 那部分覆盖掉。"""
    _set_phase_progress(status, "llm", pct)
    if msg:
        status["message"] = msg


def _save_batch_results(
    db_client,
    batch_id: str,
    user_id: str,
    generation_results: list[dict],
    error_prefix: str,
    errors_sink: list,
) -> tuple[list[dict], list[dict], list[dict]]:
    """两个 worker 共用的批量保存逻辑（修 R1：Quick Generate 之前逐条 INSERT）。

    一次 bulk_create_items + 一次 bulk_create_initial_versions，把
    ~30 次 round trip 收敛成 2 次。返回三元组：

      - inserted_items   ：批量插入后的 items 行（含生成的 id）
      - inserted_versions：批量插入后的 versions 行
      - produced_titles  ：[{"title", "opening"}, ...]，用来喂跨批文本去重池

    生成失败的版本（vr.error & 空 title）按引擎名写到 ``errors_sink``，
    并跳过保存。某个 slot 的所有 version 都失败时，**不再为它创建 item** ——
    避免审核页出现没有任何 version 的孤儿 item（统计卡显示"总计 N / 待审核 N"
    却一条都点不开），把"整批生成失败"伪装成"成功但 0 内容"。
    ``error_prefix`` 控制日志前缀，例如 "计划 3（项目 A）"。
    """
    # 步骤 1：先按 slot 筛出有效 version（生成失败且无标题的丢弃 + 记错误）。
    #   关键修复：只为"至少有一条有效 version"的 slot 建 item。否则当某个 slot 的
    #   所有引擎都生成失败时（例如 Gemini 中转通道整批报错），旧逻辑仍会给它建一个
    #   没有任何 version 的孤儿 item —— 审核页就会出现"总计 N / 待审核 N"却一条都点
    #   不开（渲染时 item.versions 为空被 skip），用户看到的是"成功但没内容"。
    valid_slots: list[tuple[dict, list]] = []
    for slot in generation_results:
        valid_versions = []
        for vr in slot["versions"]:
            if vr.error and not vr.title:
                errors_sink.append(f"{error_prefix}· {vr.ai_engine}：{vr.error}")
                continue
            valid_versions.append(vr)
        if valid_versions:
            valid_slots.append((slot, valid_versions))

    # 整批没有任何有效内容：不建任何 item / version，直接返回空三元组。调用方据此
    # 把 saved=0 当作"失败"展示（⚠️ 而不是绿色 ✅），且审核页不会被孤儿 item 污染。
    if not valid_slots:
        return [], [], []

    # 步骤 2：只为有内容的 slot 批量建 item
    #
    # 审计 COR-002: 原来是 `zip(valid_slots, inserted_items)` —— 把 slot 和
    # 插入返回的行【按位置】配对。两个隐患:
    #   1. 依赖 PostgREST 的返回顺序等于入参顺序。SQL 层面 INSERT ... RETURNING
    #      没有这个保证, db.bulk_create_items 的 docstring 只是**断言**了它,
    #      没有任何东西在校验。顺序一旦变化, version 会挂到别的 item 上、
    #      ai_review_notes 也会张冠李戴, 而且完全无声。
    #   2. 返回行数少于入参时 zip 会**静默截断**尾部 —— 那几篇的正文就此丢失,
    #      既不报错也不告警, 看起来跟"模型本来就没写那几篇"一模一样。
    # 改法: id 由客户端预先生成并显式写进 insert(items.id 的 uuid_generate_v4()
    # 只是 DEFAULT, 给了值就用给的), 于是 slot ↔ item_id 由构造保证, 不再依赖
    # 任何返回顺序; 再逐个核对回执, 缺行就显式失败而不是丢。
    item_rows: list[dict] = []
    slot_item_ids: list[str] = []
    for slot, _ in valid_slots:
        item_id = str(uuid.uuid4())
        slot_item_ids.append(item_id)
        item_rows.append({
            "id":       item_id,
            "batch_id": batch_id,
            "user_id":  user_id,
            **({"ai_review_notes": slot["ai_review_notes"]}
               if slot.get("ai_review_notes") else {}),
        })
    try:
        inserted_items = db.bulk_create_items(db_client, item_rows)
    except Exception as exc:
        errors_sink.append(f"{error_prefix}批量写入 items 失败 — {exc}")
        return [], [], []

    returned_ids = {it.get("id") for it in inserted_items if isinstance(it, dict)}
    missing_ids = [i for i in slot_item_ids if i not in returned_ids]
    if missing_ids:
        # 部分插入。宁可整批失败也不能带着"少了几篇"继续往下走 ——
        # 后面 versions 会挂到不存在的 item 上, 审核页出现点不开的幽灵卡。
        errors_sink.append(
            f"{error_prefix}批量写入 items 只回执 {len(returned_ids)}/{len(slot_item_ids)} 行"
            f"，已回滚本批；请重试。"
        )
        try:
            db.delete_items(db_client, list(returned_ids))
        except Exception as cleanup_exc:
            errors_sink.append(
                f"{error_prefix}孤儿 item 回收失败 — {cleanup_exc}（审核页可能出现空卡）"
            )
        return [], [], []
    # 回执按我们给定的顺序重排, 让调用方拿到的 inserted_items 与 valid_slots 同序
    _by_id = {it["id"]: it for it in inserted_items if isinstance(it, dict) and it.get("id")}
    inserted_items = [_by_id[i] for i in slot_item_ids]

    # 步骤 3：从有效 version 抽 versions 行 + 顺便构造文本去重池要的 opening
    version_rows: list[dict] = []
    produced_titles: list[dict] = []
    for (slot, valid_versions), item_id in zip(valid_slots, slot_item_ids):
        for vr in valid_versions:
            version_rows.append({
                "item_id":     item_id,
                "ai_engine":   vr.ai_engine,
                "title":       vr.title,
                "body":        vr.body,
                "keywords":    vr.keywords,
                "token_usage": vr.token_usage,
            })
            opening = ""
            body = (vr.body or "").strip()
            if body:
                first_line = next((ln for ln in body.splitlines() if ln.strip()), "")
                opening = first_line.strip()[:25]
            if vr.title:
                produced_titles.append({"title": vr.title.strip(), "opening": opening})

    # 步骤 4：批量插 versions
    inserted_versions: list[dict] = []
    try:
        inserted_versions = db.bulk_create_initial_versions(db_client, version_rows)
    except Exception as exc:
        errors_sink.append(f"{error_prefix}批量写入 versions 失败 — {exc}")
        # R-042: items 已插入而 versions 全军覆没 → 审核页会出现 N 张点不开的
        # 幽灵卡(正是本函数 docstring 声称要消灭的孤儿 item, 旧修复只堵了
        # "slot 全失败"路径)。best-effort 回收已建 items; produced_titles 也
        # 不能返回 —— 这些标题根本不存在于 DB, 进队列标题池会让后续批次
        # 避开"幽灵标题"。
        try:
            db.delete_items(db_client, [it["id"] for it in inserted_items])
        except Exception as cleanup_exc:
            errors_sink.append(
                f"{error_prefix}孤儿 item 回收失败 — {cleanup_exc}（审核页可能出现空卡）"
            )
        return [], [], []

    return inserted_items, inserted_versions, produced_titles


def _run_hard_constraint_check(
    db_client,
    inserted_versions: list[dict],
    hard_rules: list[dict],
    error_prefix: str,
    errors_sink: list,
    metrics,
) -> None:
    """对刚保存的每条版本跑确定性硬约束校验（B3）。

    校验项由 ``validator.check_hard_rules`` 从硬规则文本里抽取
    （禁用词 / 必须出现 / 字数上限）。命中违规时：
      - 计入 ``metrics["hard_rule_violations"]``
      - 写一行警告到 ``errors_sink``（用户复审时能看到）
      - 把 item.status 改成 ``needs_revision``（不删数据，由用户决定怎么处理）

    抓不出 deterministic pattern 的规则（例如"语气要轻盈"）不会在这里
    判违规——交给 generator._apply_compliance_recheck 的 LLM 复检兜底。

    与自动重生的关系：B3 第一版只检测 + 标记，不触发重生（重生本身有
    成本，且违反硬约束的根因通常是 prompt 没传达清楚，重生改善有限）。
    后续可加 ``ENABLE_HARD_RULE_REGEN`` flag 来开启。
    """
    if not hard_rules or not inserted_versions:
        return
    # 审计 COR-011: 「不重复标记同一 item」原来只写在注释里, 代码没有去重。
    # 多引擎批次下同一个 item 有 N 个 version, 全违规就调 N 次
    # update_item_status —— 每次都会 list_items.clear() 把**全局**缓存击穿,
    # 且每次都触发 items 的 updated_at 触发器。状态本身是幂等的, 重复调用
    # 只是浪费, 但缓存反复失效会让审核页在整批落库期间持续回源。
    marked_items: set[str] = set()
    for iv in inserted_versions:
        if not iv:
            continue
        hits = validator.check_hard_rules(
            hard_rules,
            title=iv.get("title", ""),
            body=iv.get("body", ""),
            keywords=iv.get("keywords") or [],
        )
        if not hits:
            continue
        metrics.incr("hard_rule_violations", len(hits))
        item_id = iv.get("item_id")
        if item_id and item_id not in marked_items:
            # codex review 2026-08-24: 记进 marked_items 必须在**写成功之后**。
            # 记在前面的话, 第一条违规版本遇到一次瞬时库错误, 同一 item 后面
            # 几条违规版本就都被"已经标过了"跳过 —— item 停在原状态, 而
            # errors_sink 里那几行还在跟用户说"已标记 needs_revision"。
            # 说过的话和库里的状态对不上, 且没有任何报错。
            try:
                db.update_item_status(db_client, item_id, "needs_revision")
                marked_items.add(item_id)
            except Exception as exc:
                telemetry.log_event(
                    "hard_rule_status_update_failed",
                    item_id=item_id, error=str(exc)[:200],
                )
        for hit in hits:
            kind_label = {
                "forbidden_word":  "禁用词",
                "required_phrase": "缺必含词",
                "max_len":         "超长",
            }.get(hit["kind"], hit["kind"])
            errors_sink.append(
                f"{error_prefix}硬约束违反（{kind_label}）：《{iv.get('title', '')}》"
                f" — 规则「{hit['rule']}」匹配到「{hit['match']}」"
                f"；已标记 needs_revision"
            )


def _try_regen_one(
    *,
    db_client,
    project_id: str,
    dup_idx: int,
    inserted_versions: list[dict],
    version_rows: list[dict],
    titles_in_order: list[str],
    new_vecs: list[list[float]],
    queue_pool: list[dict],
    regen_ctx: dict,
    threshold: float,
    metrics,
    error_prefix: str,
    errors_sink: list,
) -> bool:
    """对单个被判定重复的版本做"避开重复 + 再生一次"的尝试。

    最多重试 ``DEDUP_REGEN_MAX_RETRIES`` 次。成功（新标题不再触发任何
    cos ≥ threshold 的命中）→ UPDATE versions 行 + 同步 titles/new_vecs/
    queue_pool；失败 → ``items.status = 'needs_revision'``、写警告。

    返回 True 表示重生成功；False 表示重试用尽。
    """
    if dup_idx >= len(inserted_versions) or dup_idx >= len(version_rows):
        return False
    iv = inserted_versions[dup_idx]
    if not iv or not iv.get("id"):
        return False
    version_id = iv["id"]
    item_id    = iv.get("item_id")
    engine     = (version_rows[dup_idx].get("ai_engine") or "claude")

    max_retries = int(regen_ctx.get("max_retries", 2))

    # 已生成池 = 原 historical + 本批其他成功标题（除自己以外）
    base_historical = list(regen_ctx.get("historical_titles") or [])
    sibling_titles = [
        {"title": t, "opening": ""}
        for i, t in enumerate(titles_in_order)
        if i != dup_idx and t
    ]

    avoid_extra = (
        (regen_ctx.get("extra_instructions") or "")
        + "\n\n【本次自动避让】请务必避开以下标题的任何角度、卖点和句式："
        + " / ".join(
            f"《{x['title']}》" for x in (base_historical + sibling_titles)[-15:]
        )
    ).strip()

    for attempt in range(1, max_retries + 1):
        metrics.incr("regen_attempts")
        try:
            results = gen_module.generate_batch(
                system_prompt=regen_ctx.get("system_prompt", ""),
                tactic=regen_ctx.get("tactic", ""),
                count=1,
                engines=[engine],
                target_audience=regen_ctx.get("target_audience", ""),
                key_messages=regen_ctx.get("key_messages", ""),
                tone=regen_ctx.get("tone", ""),
                extra_instructions=avoid_extra,
                images=None,
                progress_callback=None,
                historical_titles=base_historical + sibling_titles,
                use_thinking=regen_ctx.get("use_thinking", False),
                engine_models=regen_ctx.get("engine_models") or None,
                gemini_use_thinking=regen_ctx.get("gemini_use_thinking", False),
                user_id=regen_ctx.get("user_id", ""),
                project_id=project_id,
                metrics=metrics,
                metrics_source="dedup_regen",
            )
        except Exception as exc:
            errors_sink.append(f"{error_prefix}重生异常：{exc}")
            continue

        if not results:
            continue
        new_version = results[0]["versions"][0] if results[0].get("versions") else None
        if not new_version or new_version.error or not new_version.title:
            continue

        # 给新标题算 embedding，比较是否还触发命中
        new_vec_list = dedup_module.embed_texts([new_version.title])
        if not new_vec_list or not new_vec_list[0]:
            continue
        candidate_vec = new_vec_list[0]

        # 对照 queue_pool + 本批其他标题
        siblings_for_check = [
            (titles_in_order[i], new_vecs[i])
            for i in range(len(titles_in_order))
            if i != dup_idx and titles_in_order[i]
        ]
        check_titles = [h["title"]     for h in queue_pool] + [t for t, _ in siblings_for_check]
        check_vecs   = [h["embedding"] for h in queue_pool] + [v for _, v in siblings_for_check]
        still_hit = dedup_module.find_near_duplicates(
            [candidate_vec], [new_version.title],
            check_vecs, check_titles,
            threshold=threshold,
        )
        if still_hit:
            # 仍然撞车，继续重试
            continue

        # ── 重生成功：写 DB + 原地更新内存结构 ─────────────────────────
        # R-034: 必须确认 DB 写入成功才能更新内存池。旧逻辑无条件按成功
        # 处理 —— UPDATE 失败时 DB 里仍是旧重复文案, 而内存池已登记新标题,
        # 永久分叉(用户审到重复内容且零告警)。写失败按本次重试失败处理,
        # 继续下一次 attempt(或耗尽后如实标 needs_revision)。
        write_ok = db.update_version_content(
            db_client, version_id,
            title=new_version.title,
            body=new_version.body,
            keywords=new_version.keywords,
            token_usage=new_version.token_usage,
            embedding=candidate_vec,
        )
        if not write_ok:
            errors_sink.append(
                f"{error_prefix}重生写入 DB 失败（已重试生成但未落库），"
                f"内容保持原样。"
            )
            continue
        titles_in_order[dup_idx] = new_version.title.strip()
        new_vecs[dup_idx]        = candidate_vec
        version_rows[dup_idx]["title"] = new_version.title  # 给后面 queue_titles 用
        # R-042: inserted_versions 与 version_rows 是两份独立 dict(后者由
        # _save_batch_results 重建)。下游 _run_hard_constraint_check 读的是
        # inserted_versions —— 不回写的话, 它校验的是重生**前**的旧内容:
        # 旧文违规会把已换掉的 item 误标 needs_revision(假阳性), 新文却完全
        # 没被硬规则查过(假阴性)。三个字段一并同步。
        if dup_idx < len(inserted_versions) and isinstance(inserted_versions[dup_idx], dict):
            inserted_versions[dup_idx]["title"]    = new_version.title
            inserted_versions[dup_idx]["body"]     = new_version.body
            inserted_versions[dup_idx]["keywords"] = new_version.keywords
        return True

    # 重试用尽 → 标记 needs_revision
    if item_id:
        try:
            db.update_item_status(db_client, item_id, "needs_revision")
        except Exception as exc:
            telemetry.log_event(
                "regen_status_update_failed",
                item_id=item_id, error=str(exc)[:200],
            )
    errors_sink.append(
        f"{error_prefix}《{titles_in_order[dup_idx]}》重试 {max_retries} 次仍重复，"
        f"已标记 needs_revision；建议人工处理或删除。"
    )
    return False


def _run_semantic_dedup_pass(
    db_client,
    inserted_versions: list[dict],
    version_rows: list[dict],
    queue_embeddings: dict,
    project_id: str,
    error_prefix: str,
    errors_sink: list,
    metrics,
    regen_ctx: Optional[dict] = None,
    project: Optional[dict] = None,
    status: Optional[dict] = None,
) -> None:
    """两个 worker 共用的语义查重 + embedding 持久化（修 R3）。

    流程：
      1. 给所有新标题算 768d embedding（一次 API 调用，批量请求）
      2. 写入 versions.embedding 列
      3. 与历史池对比，cos ≥ 阈值视为近似重复
      4. 本批内对比（多引擎撞车场景）
      5. 累积进 queue_embeddings，下一批能立刻看到

    若 ``regen_ctx`` 非空且 ``regen_ctx["enabled"]`` 为 True，会对每个命中
    的版本调用 _try_regen_one 自动重生：
      - 重生成功 → UPDATE versions 行、刷新 pool、不写警告
      - 重试 max_retries 仍命中 → 把 item.status 改成 needs_revision，并
        写警告到 ``errors_sink``
      - 重生功能关闭时，所有命中只写警告，不改 DB

    阈值解析优先级（Day 2）：
      1. ``regen_ctx["threshold_override"]``（队列策略覆盖）
      2. ``project["semantic_dedup_threshold"]`` （项目级配置）
      3. ``config.DEDUP_SEMANTIC_THRESHOLD`` （全局默认）

    ``queue_embeddings`` 是 worker 自己维护的字典，按 project_id 分桶。
    Quick Generate 也建一份只有一个项目的字典。
    没配 GOOGLE_API_KEY 时整段跳过，但会把降级原因写到 ``status["warnings"]``，
    UI 能看到"本批次已降级为纯文本去重"——不再静默。
    """
    regen_enabled = bool(regen_ctx and regen_ctx.get("enabled"))
    if not inserted_versions:
        return
    if not dedup_module.embeddings_available():
        telemetry.log_event(
            "dedup_degraded_no_embedding",
            project_id=project_id, version_count=len(inserted_versions),
        )
        metrics.set_meta("dedup_mode", "text_only")
        if status is not None:
            status.setdefault("warnings", []).append(
                f"项目 {project_id[:8] if project_id else '?'}：本批次语义去重已降级为纯文本（embedding 不可用）"
            )
        return
    metrics.start_phase("embedding")
    # 进度细化：embedding 阶段在整批占 90%→100%，把这 10% 再切成 5 步：
    #   0.0   嵌入计算开始
    #   0.2   嵌入计算完成
    #   0.4   embedding 落库完成
    #   0.6   去重对比完成
    #   ?     regen 循环按 idx 占 0.6→0.9
    #   1.0   pool 写回完成
    # 之前从 0% → 100% 中间没刷过——dedup 触发自动重生时进度会卡 90% 数十秒，
    # 用户以为系统挂了去手动重启队列，触发重复生成。
    def _emb_progress(intra: float):
        if status is not None:
            _set_phase_progress(status, "embedding", intra=intra)
    try:
        _emb_progress(0.0)
        titles_in_order = [r.get("title", "") for r in version_rows]
        new_vecs = dedup_module.embed_texts(titles_in_order)
        if not new_vecs:
            return
        _emb_progress(0.2)
        # 步骤 2：持久化 embedding（失败的 version_id 累到 status["embedding_missing"]）
        embed_rows = []
        for i, iv in enumerate(inserted_versions):
            if i < len(new_vecs):
                embed_rows.append({"id": iv.get("id"), "embedding": new_vecs[i]})
        failed_embed_ids: list[str] = []
        if embed_rows:
            db.bulk_update_version_embeddings(
                db_client, embed_rows, failed_sink=failed_embed_ids,
            )
        if failed_embed_ids and status is not None:
            status.setdefault("embedding_missing", []).extend(failed_embed_ids)
            metrics.incr("embedding_missing", len(failed_embed_ids))
        _emb_progress(0.4)

        # 步骤 3：解析阈值（队列策略 > 项目级 > 全局默认）
        if regen_ctx and regen_ctx.get("threshold_override") is not None:
            threshold = float(regen_ctx["threshold_override"])
        elif project and project.get("semantic_dedup_threshold") is not None:
            threshold = float(project["semantic_dedup_threshold"])
        else:
            threshold = float(getattr(config, "DEDUP_SEMANTIC_THRESHOLD", 0.92))
        metrics.set_meta("dedup_threshold", threshold)
        hist_pool = queue_embeddings.get(project_id, [])
        hits = []
        if hist_pool:
            hits = dedup_module.find_near_duplicates(
                new_vecs, titles_in_order,
                [h["embedding"] for h in hist_pool],
                [h["title"]     for h in hist_pool],
                threshold=threshold,
            )

        # 步骤 4：本批内查重
        intra = dedup_module.cross_batch_pairs(
            new_vecs, titles_in_order, threshold=threshold,
        )

        # 把"哪些 index 是重复"汇总成一个集合（去重 + 排序），方便自动重生用
        duplicate_indices: set[int] = set()
        for hit in hits:
            duplicate_indices.add(hit["index"])
        for pair in intra:
            # 本批内冲突时把后一个（j）当作要重生的；前一个先留着
            duplicate_indices.add(pair["j"])
        _emb_progress(0.6)

        # 没开启自动重生：直接写警告
        if not regen_enabled:
            metrics.incr("dedup_semantic_hits", len(hits) + len(intra))
            for hit in hits:
                errors_sink.append(
                    f"{error_prefix}近似重复：《{hit['title']}》 ↔ "
                    f"历史《{hit['best_match']}》（相似度 {hit['score']:.2f}）"
                )
            for pair in intra:
                errors_sink.append(
                    f"{error_prefix}本批内近似：《{pair['title_i']}》 ↔ "
                    f"《{pair['title_j']}》（相似度 {pair['score']:.2f}）"
                )
        else:
            # 自动重生：对每个 duplicate index 调一次 1-item 生成，更新 DB
            _dup_list = sorted(duplicate_indices)
            _n_dups = len(_dup_list) or 1  # 避免除零
            for _i, dup_idx in enumerate(_dup_list):
                ok = _try_regen_one(
                    db_client=db_client,
                    project_id=project_id,
                    dup_idx=dup_idx,
                    inserted_versions=inserted_versions,
                    version_rows=version_rows,
                    titles_in_order=titles_in_order,
                    new_vecs=new_vecs,
                    queue_pool=queue_embeddings.setdefault(project_id, []),
                    regen_ctx=regen_ctx,
                    threshold=threshold,
                    metrics=metrics,
                    error_prefix=error_prefix,
                    errors_sink=errors_sink,
                )
                if ok:
                    metrics.incr("regen_success")
                # 不论成功失败都计入 attempts（_try_regen_one 内部记录每次尝试）
                # regen 循环占 0.6 → 0.9，按完成比例线性插值
                _emb_progress(0.6 + 0.3 * (_i + 1) / _n_dups)

        # 步骤 5：累积给下一批用（regen 路径可能已经原地改过 new_vecs）
        # 加去重 + 滑窗上限：长队列下 pool 会一直 append 同项目历史，O(n^2)
        # 相似度比较 + 重复条目让命中解释噪声变大。
        # - 去重：按 title 精确匹配（embedding 一致的近似重复语义层已被合并，
        #   再用文本 key 兜底防止 worker 内部 regen 写两次）
        # - 滑窗：保留最近 _QUEUE_POOL_MAX 条；超出从队首丢（保留近期上下文）
        pool = queue_embeddings.setdefault(project_id, [])
        existing_titles = {entry.get("title") for entry in pool}
        for i, t in enumerate(titles_in_order):
            if i < len(new_vecs) and t and t not in existing_titles:
                pool.append({"title": t, "embedding": new_vecs[i]})
                existing_titles.add(t)
        if len(pool) > _QUEUE_POOL_MAX:
            del pool[: len(pool) - _QUEUE_POOL_MAX]
    finally:
        metrics.stop_phase("embedding")


# ── Phase 2.1: session 路由 helper ────────────────────────────────────────
# 跨批 prompt caching 的"对话会话"层接入点。worker 在 build_layered_system_
# prompt 之后调 _resolve_engine_sessions 为每个 engine 拿到对应 active session
# + 历史 messages prefix; 生成完成后调 _commit_session_tokens 把 input token
# 累加到 session 的 running_input_tokens(UI 软警告进度条用)。
#
# Phase 2.1 范围: 只做读 + token 累加。"approved version → assistant turn
# 写回 session_messages" 的 commit gate 留给 Phase 2.2; 在那之前 session
# 历史一直为空, prior_messages 实际拼上去也是空(行为 byte-identical Phase 1),
# 但数据通路是通的——下个 PR 接审核闸门, cache 命中自然开始生效。
#
# 所有失败都吞了走 fallback (空 session_ids + 空 prior_messages),保证
# session 层的 hiccup 不会让批次生成挂掉。

# Phase 2.2: 懒同步 approved 内容到 session 的参数 + helper
#
# 每个 (project, engine, model) session 最多带最近 50 条 approved 内容作为
# 对话历史(跟用户定的"半补建每 model 50 条"一致)。撞 context window 的
# 滚动 / seal 留 Phase 2.3。
_SESSION_SYNC_LIMIT = 50
# 历史 user turn 占位: 所有历史 turn 用同一句, 高度可 cache。真正的"避重
# 创作"指令在当前批的 user prompt(generate_batch 内部的 _make_user_prompt
# + dedup_block)里, 历史 user turn 只是让对话合法 + 给 assistant 内容
# (已 approved 的产出)一个挂载点, 让模型"看到"过往完整内容来避重。
_SESSION_USER_TURN_PLACEHOLDER = "请基于项目调性创作一条小红书文案。"


def _format_version_for_session(v: dict) -> str:
    """把一个 approved version 拼成 assistant turn 文本——完整 title + body +
    keywords(用户明确要完整内容做避重依据, 不是摘要)。"""
    parts: list[str] = []
    title = (v.get("title") or "").strip()
    body = (v.get("body") or "").strip()
    kws = v.get("keywords") or []
    if title:
        parts.append(f"标题：{title}")
    if body:
        parts.append(f"正文：{body}")
    if kws:
        kw_str = " ".join(str(k) for k in kws) if isinstance(kws, list) else str(kws)
        if kw_str.strip():
            parts.append(f"标签：{kw_str}")
    return "\n".join(parts)


def _sync_approved_to_session(
    db_client, session_id: str, project_id: str,
) -> int:
    """把该 project 下 approved 但还没进 session 的版本, 按时间顺序补成
    (user, assistant) 对话历史。幂等: 按 item_id 跳过已 commit 的。

    **不分引擎/来源**(claude / gemini / manual 全要): 避重针对项目所有已产出
    内容。每个 engine 的 session 都补"项目全部 approved", 内容相同分别发给
    各自模型 —— 否则 claude session 看不到 gemini / manual(手动精修最终稿)
    写过的, 避重就漏一大块(见 db.list_approved_versions_for_sync)。

    这一步是 Phase 2.2 的核心 —— 同时实现:
      - 增量 commit: 每次生成前把新审核通过的内容补进 session
      - 历史补建(原 Phase 2.4): 新 session 首次同步就把最近 50 条历史灌入

    turn 结构: user 占位 turn item_id=NULL(不占唯一约束), assistant turn 带
    item_id(幂等 + DB 唯一约束 session_messages_session_item_uniq 防并发重复)。

    返回新追加的 turn 对数。失败静默返回 0(不影响生成, 最多这次没拿到完整
    历史 prefix)。
    """
    try:
        committed = db.get_session_committed_item_ids(db_client, session_id)
        approved = db.list_approved_versions_for_sync(
            db_client, project_id, limit=_SESSION_SYNC_LIMIT,
        )
        new_msgs: list[dict] = []
        for v in approved:
            iid = v.get("item_id")
            if not iid or iid in committed:
                continue
            assistant_text = _format_version_for_session(v)
            if not assistant_text:
                continue
            bid = v.get("batch_id")
            # user 占位: item_id=None(不参与 (session_id,item_id) 唯一约束);
            # assistant: 带 item_id(幂等 + 唯一约束防并发重复同步)。
            new_msgs.append({"role": "user",
                             "content": {"text": _SESSION_USER_TURN_PLACEHOLDER},
                             "item_id": None, "batch_id": bid})
            new_msgs.append({"role": "assistant",
                             "content": {"text": assistant_text},
                             "item_id": iid, "batch_id": bid})
        if not new_msgs:
            return 0
        # ⚠️ **必须采信回执**。append_session_messages 是 read-max-then-insert
        # + 唯一约束重试, 重试耗尽时它**返回 0 而不抛异常** —— 上面那个
        # except 兜不住。原来这里把返回值整个丢掉、直接
        # ``return len(new_msgs) // 2``, 报的是"打算写几条"。
        #
        # 这个假成功比写失败更糟: 会话历史是避重用的上下文, 少几轮没有任何
        # 地方报错, 表现只是后面生成的稿子开始跟历史撞车, 而排查的人看到的
        # telemetry 写着"同步成功 N 对"。(跨库审计 2026-08-24 ROB-015)
        written = db.append_session_messages(db_client, session_id, new_msgs)
        try:
            written = int(written or 0)
        except (TypeError, ValueError):
            written = 0
        if written < len(new_msgs):
            telemetry.log_event(
                "session_sync_partial",
                session_id=session_id, project_id=project_id,
                intended=len(new_msgs), written=written,
            )
        # turn 是 (user, assistant) 成对写的; 落库行数是奇数说明只写进去半对,
        # 向下取整按"完整的几对"报 —— 宁可少报, 不可多报。
        return written // 2
    except Exception as exc:
        telemetry.log_event(
            "session_sync_failed",
            session_id=session_id, error=str(exc)[:200],
        )
        return 0


def _resolve_engine_sessions(
    db_client,
    project: dict,
    engines: list[str],
    engine_models: dict,
    user_id: str,
) -> tuple[dict[str, str], dict[str, list[dict]]]:
    """为本批的每个 engine 路由/创建 active session + 拉历史 messages。

    返回 ``(engine_session_ids, engine_prior_messages)``:
      engine_session_ids:    {"claude": "<uuid>", "gemini": "<uuid>"} —— 后续
                             token 累加 / commit 用。某 engine 路由失败时该
                             key 不出现(下游按缺 key 跳过累加,不影响生成)。
      engine_prior_messages: {"claude": [{role,content},...], "gemini": [...]}
                             —— 传给 ``generate_batch(engine_prior_messages=)``。
                             空 session(没有历史 turn)对应空 list。
    """
    base_prompt = (project.get("system_prompt") or "")
    project_id = project.get("id") or ""
    try:
        base_hash = config.compute_base_prompt_hash(base_prompt)
    except Exception as exc:
        telemetry.log_event("session_route_hash_failed", error=str(exc)[:120])
        return {}, {}

    engine_session_ids: dict[str, str] = {}
    engine_prior_messages: dict[str, list[dict]] = {}

    for eng in engines or []:
        # 解析该 engine 本批实际选用的 model(跟 generator._engine_call 同逻辑:
        # plan.engine_models 优先, 否则 config 默认)
        if eng == "claude":
            model_used = (engine_models or {}).get("claude", "") or config.CLAUDE_MODEL
        elif eng == "gemini":
            model_used = (engine_models or {}).get("gemini", "") or config.GEMINI_MODEL
        else:
            continue
        window = config.get_context_window(model_used)
        sess = db.get_or_create_active_session(
            db_client,
            project_id=project_id,
            engine=eng,
            model_id=model_used,
            base_prompt_hash=base_hash,
            user_id=user_id,
            window_limit=window,
        )
        if not sess or not sess.get("id"):
            # 路由失败(DB 不可达 / 表不存在),静默 fallback 到空 prefix——
            # generator 那边 prior_messages=[] 退化为 Phase 1 行为。
            continue
        engine_session_ids[eng] = sess["id"]
        # Phase 2.2: 先把 approved 但未进 session 的内容补成对话历史(懒同步),
        # 再拉完整历史。新 session 首次会补最近 50 条 approved 历史; 后续生成
        # 只补增量(新审核通过的)。失败不影响生成(prior 退化为已有部分)。
        _sync_approved_to_session(db_client, sess["id"], project_id)
        # 拉历史: 表里 content 是 JSONB,LLM SDK 那边由 GeminiEngine._msg_content
        # _to_text / ClaudeEngine 的 _build_content 各自识别 dict/str/list 形态。
        rows = db.list_session_messages(db_client, sess["id"]) or []
        engine_prior_messages[eng] = [
            {"role": r.get("role", "user"), "content": r.get("content")}
            for r in rows
        ]
    return engine_session_ids, engine_prior_messages


def _commit_session_tokens(
    db_client,
    engine_session_ids: dict[str, str],
    metrics_meta: dict,
) -> None:
    """累加本批 ``source='main'`` 的 input + cache_read + cache_create
    到对应 engine 的 session.running_input_tokens。

    **必须只算 source='main' 的 token**: 同一 batch 还会有 compliance_recheck
    / multi_role_select / multi_role_refine / dedup_regen 这些**不走 session
    prefix 的辅助调用** —— 它们也会按 ``claude/<model>`` 落进 ``by_model``
    总和, 如果直接读 by_model 会让 session.running_input_tokens 被无关请求
    虚高, session 窗口判断 / UI 进度条都错位(早 PR review 命中)。

    Phase 2.1+: telemetry 多记一个 ``by_source_model[source][model]`` 二维
    维度, 这里精确读 ``by_source_model['main']`` 累加。fallback 到老
    by_model(理论不会发生 —— 同一 BatchMetrics 实例 在内存里, 但稳妥起见
    保留 fallback)。
    """
    if not engine_session_ids:
        return
    totals = (metrics_meta or {}).get("token_totals") or {}
    # Phase 2.1 review #2: 只算 source='main' 的 token, 排除 compliance_recheck
    # / multi_role_* / dedup_regen 等不走 session prefix 的辅助调用。
    # Review #3: fallback gate **只在 ``by_source_model`` 整段缺失/非法时触发**,
    # 不能在 main 桶存在但为空时退到 by_model — 那种情况意味着本批主调用
    # 全部失败,只有辅助调用记了 token,这时正确行为是 delta=0(不动 session),
    # 而不是把辅助调用 token 当成 session 的累加(那正是 review #2 要消除的
    # leakage)。
    has_by_source_model = (
        "by_source_model" in totals
        and isinstance(totals.get("by_source_model"), dict)
    )
    if has_by_source_model:
        main_by_model = totals["by_source_model"].get("main") or {}
        if not isinstance(main_by_model, dict):
            main_by_model = {}
    else:
        # 老版 BatchMetrics 不写 by_source_model 时退到 by_model。
        # 这条路只在 schema 升级期或 retro batch 重放时走;新批次都有
        # by_source_model。
        main_by_model = totals.get("by_model") or {}
        if not isinstance(main_by_model, dict):
            return
    if not main_by_model:
        # 主调用 token=0 + by_source_model 存在: 本批 main 没成功(全失败,
        # 或者根本没调 main 比如 multi_role 只跑了 select/refine 没 main)
        # → delta=0, session 不动。
        return
    for eng, session_id in engine_session_ids.items():
        delta = 0
        prefix = f"{eng}/"
        for model_full, usage in main_by_model.items():
            if not isinstance(usage, dict) or not model_full.startswith(prefix):
                continue
            # 一次请求的总 input = input(非缓存) + cache_read + cache_create
            # —— Anthropic 三互斥字段加起来才反映"用了多少 context"。
            # Gemini 的 cache_read 是 input 子集, 严格说会重复一次;
            # 但 implicit cache 命中量本来就小, over-counting 不影响判断
            # (running_input_tokens 只用于软警告 / UI 进度条)。
            delta += int(usage.get("input") or 0)
            delta += int(usage.get("cache_read") or 0)
            delta += int(usage.get("cache_create") or 0)
        if delta > 0:
            db.add_session_running_tokens(db_client, session_id, delta)


def _update_session_occupancy(
    db_client,
    engine_session_ids: dict[str, str],
    metrics_meta: dict,
) -> list[str]:
    """SET 每个 engine session 的当前窗口占用 ``last_prefix_tokens``(= 本批
    单次主调用的 prefix 大小), 达到 ``SESSION_SEAL_THRESHOLD × window_limit``
    时自动封窗(seal, reason='window_full')。下一批会路由到新 session, 新
    session 懒同步最近 50 条 approved 历史(避重自动接续)+ dedup_block 照常
    注入, 所以无需额外"摘要注入"。

    与 ``_commit_session_tokens`` 的分工:
      - _commit_session_tokens → 累加 ``running_input_tokens``(跨批 SUM, 只做
        "本会话累计消耗"统计)
      - 这里 → SET ``last_prefix_tokens``(当前占用, 用于进度条 + 封窗判断)

    占用口径按引擎不同(只取该 engine 单次主调用; 同 engine 多 model 取 max):
      - claude: ``input + cache_read + cache_create``(三互斥字段相加才是真占用)
      - gemini: ``input``(=prompt_token_count, 已含 cache_read 子集, 不重复加)

    返回被自动封窗的 engine 名列表(供 UI 提示)。失败静默(埋点不抛)。
    """
    if not engine_session_ids:
        return []
    totals = (metrics_meta or {}).get("token_totals") or {}
    bsm = totals.get("by_source_model")
    main_by_model = (bsm.get("main") if isinstance(bsm, dict) else None) or {}
    if not isinstance(main_by_model, dict) or not main_by_model:
        return []
    sealed: list[str] = []
    for eng, session_id in engine_session_ids.items():
        prefix_tokens = 0
        pfx = f"{eng}/"
        for model_full, usage in main_by_model.items():
            if not isinstance(usage, dict) or not model_full.startswith(pfx):
                continue
            inp = int(usage.get("input") or 0)
            if eng == "claude":
                cand = (
                    inp
                    + int(usage.get("cache_read") or 0)
                    + int(usage.get("cache_create") or 0)
                )
            else:
                cand = inp
            if cand > prefix_tokens:
                prefix_tokens = cand
        if prefix_tokens <= 0:
            continue
        window = db.set_session_prefix_tokens(db_client, session_id, prefix_tokens)
        if window > 0 and prefix_tokens >= window * config.SESSION_SEAL_THRESHOLD:
            if db.seal_session(db_client, session_id, "window_full"):
                sealed.append(eng)
                telemetry.log_event(
                    "session_auto_sealed",
                    session_id=session_id, engine=eng,
                    prefix_tokens=prefix_tokens, window=window,
                    threshold=config.SESSION_SEAL_THRESHOLD,
                )
    return sealed


def _call_generator(*, multi_role: bool, system_prompt, tactic, count, engines,
                    engine_models, plan, extra_instructions, images,
                    progress_callback, historical_titles, use_thinking,
                    gemini_thinking, custom_roles, n_roles, metrics,
                    engine_prior_messages, user_context_block,
                    user_id=None, project_id=None):
    """两条编排调 generator 的那一段, 收成一处(审计 SUP-012)。

    在此之前这段在 ``_queue_worker_impl`` 和 ``_quick_gen_worker`` 里各有一份,
    每份又分 multi_role / 单角色两个分支 —— 同一组 ~18 个参数写了**四遍**。
    加一个参数要改四处, 漏一处不会报错, 只会让两条路径的行为悄悄分叉。
    这正是 SUP-012 说的"下次还会漂"。

    ⚠️ 两条路径**保留的差异**在入参上显式表达, 不在函数体里判断:
      · ``images`` —— 队列不支持图片(plan 里根本没这个键), 它传 None;
      · ``user_id`` / ``project_id`` —— 只有单角色那一路要;
      · ``progress_callback`` —— 队列的会带 "计划 X/Y" 前缀。
    """
    common = dict(
        system_prompt=system_prompt,
        tactic=tactic,
        count=count,
        engines=engines,
        target_audience=plan.get("target_audience", ""),
        key_messages=plan.get("key_messages", ""),
        tone=plan.get("tone", ""),
        extra_instructions=extra_instructions,
        images=images,
        progress_callback=progress_callback,
        historical_titles=historical_titles or None,
        use_thinking=use_thinking,
        gemini_use_thinking=gemini_thinking,
        metrics=metrics,
        engine_prior_messages=engine_prior_messages,
        user_context_block=user_context_block,
    )
    if multi_role:
        return gen_module.generate_batch_multi_role(
            engine_models=engine_models or None,
            custom_roles=custom_roles,
            n_roles=n_roles,
            **common,
        )
    return gen_module.generate_batch(
        engine_models=engine_models or None,
        user_id=user_id,
        project_id=project_id,
        **common,
    )


def _persist_and_check(*, db_client, batch_id, user_id, generation_results,
                       error_prefix, errors_sink, metrics, status,
                       dedup_pool, project_id, project, regen_ctx,
                       hard_rules):
    """落库 → 语义查重 → 硬约束校验。两条编排的收尾段, 收成一处(审计 SUP-012)。

    返回 ``(inserted_versions, produced_titles)``。

    ⚠️ 三步的**顺序不能动**: 先落库拿到 version id, 再查重(命中要标 needs_revision
    并可能触发重生), 最后跑硬约束(它也标 needs_revision)。查重放到硬约束后面的话,
    重生出来的新内容不会再过一遍硬约束。
    """
    metrics.start_phase("db_save")
    _set_phase_progress(status, "db_save")
    inserted_items, inserted_versions, produced_titles = _save_batch_results(
        db_client, batch_id, user_id, generation_results,
        error_prefix, errors_sink,
    )
    # version_rows 是 _save_batch_results 内部构造的临时变量; 这里从
    # inserted_versions 还原(embedding / 自动重生流程要用)。
    version_rows = [
        {"title": v.get("title", ""), "ai_engine": v.get("ai_engine", "")}
        for v in inserted_versions
    ]
    metrics.stop_phase("db_save")

    _set_phase_progress(status, "embedding")
    _run_semantic_dedup_pass(
        db_client, inserted_versions, version_rows,
        dedup_pool, project_id, error_prefix,
        errors_sink, metrics, regen_ctx=regen_ctx,
        project=project, status=status,
    )
    _run_hard_constraint_check(
        db_client, inserted_versions, hard_rules,
        error_prefix, errors_sink, metrics,
    )
    _set_phase_progress(status, "embedding", intra=1.0)
    return inserted_versions, produced_titles


def _queue_worker(
    plans: list[dict],
    user_id: str,
    db_client,
    status: dict,
    stop_event: threading.Event,
) -> None:
    """外层 shim：保证状态机在任何异常路径下都能落回 done。

    历史上 _queue_worker_impl 的尾部已经有 with lock: status[done]=True 的收尾，
    但只覆盖正常流；如果预取项目失败之后的代码（如 dict 构造）意外抛 SystemExit
    或线程被打断，phase 会永久卡在 "running"，UI 启动+停止按钮全 disabled，用户
    没有逃生口。这个 finally 保险确保不论怎样 phase 都能回 done。
    """
    try:
        _queue_worker_impl(plans, user_id, db_client, status, stop_event)
    except BaseException as exc:
        # 不让异常被静默吞：埋点 + 写一行 status.errors 让用户看到
        try:
            status.setdefault("errors", []).append(f"队列异常退出：{exc}")
            telemetry.log_event(
                "queue_worker_unexpected_exit",
                error=str(exc)[:200],
            )
        except Exception:
            pass
    finally:
        try:
            with (status.get("_lock") or _NULL_LOCK):
                status["running"] = False
                status["done"]    = True
                if status.get("phase") not in ("done",):
                    status["phase"] = "done"
        except Exception:
            status["running"] = False
            status["done"]    = True
            status["phase"]   = "done"


def _queue_worker_impl(
    plans: list[dict],
    user_id: str,
    db_client,
    status: dict,
    stop_event: threading.Event,
) -> None:
    """后台 daemon 线程：顺序执行队列里的所有生成计划。

    ─────────────────────────────────────────────────────────────────────
    每个 plan 的处理流程分 5 步：
      [A] 取项目配置 + 记忆 + 示例 + 会话指令（首次访问该项目时查 DB，
          后续从内存 cache 取，避免 N 个 plan 查 N 次）
      [B] 按本次 tactic + key_messages 过滤 soft 规则的相关性
      [C] 合并历史标题池（DB 历史 + 本队列已生成）→ 喂给 generator
      [D] 调用 generator.generate_batch（或 generate_batch_multi_role）
      [E] 批量保存 items + versions；如果开启了 embedding 服务，
          同步算 embedding + 跨批语义查重

    顶层有 4 个 in-memory 缓存，全部按 project_id 分桶：
      - mem_cache / example_cache / session_cache：避免重复查 Supabase
      - queue_titles：本队列已生成的标题，让下一批的去重指令立刻看到
      - queue_embeddings：本队列累积的标题 + 768d embedding，
        让下一批的语义查重立刻看到（首次访问项目时预热）
    ─────────────────────────────────────────────────────────────────────
    """
    # Day 4: 用锁保护状态机三态写入；UI 通过 phase 守卫按钮，避免高频点击竞态
    queue_lock = status.get("_lock")
    if queue_lock is None:
        queue_lock = threading.Lock()
        status["_lock"] = queue_lock
    with queue_lock:
        status["running"] = True
        status["done"] = False
        status["phase"] = "running"
    status["total"] = len(plans)
    status.setdefault("completed", [])
    status.setdefault("errors", [])
    status.setdefault("warnings", [])
    status.setdefault("embedding_missing", [])

    # ── [A 预热] 队列开始前查一次项目列表，循环里按 id 取，省去 per-plan 查询 ──
    try:
        all_projects = db.list_projects(db_client, user_id)
    except Exception as exc:
        status["errors"].append(f"预取项目列表失败：{exc}")
        all_projects = []
    project_by_id = {p["id"]: p for p in all_projects}
    mem_cache: dict[str, tuple] = {}      # project_id → (global_mems, project_mems)
    example_cache: dict[str, tuple] = {}  # project_id → (pos_examples, neg_examples)
    session_cache: dict[str, list] = {}   # project_id → session_instructions

    # ── 跨批去重池（文本匹配版）─────────────────────────────────────────
    # 修复"4 批 × 10 篇 → 30 篇重复"的核心：之前每批只能看 DB 里已经写入的标题，
    # 同队列前面批次的内容可能还没落库或刚落库，下一批看不到 → 还会撞角度。
    # 这里在内存里累积，每批生成完立刻 append，下一批 prompt 直接看到。
    queue_titles: dict[str, list[dict]] = {}  # project_id → [{title, opening}, ...]

    # ── 跨批去重池（语义版）────────────────────────────────────────────
    # 同 queue_titles 的设计，但带 768d Gemini embedding，能识别"换字不换义"
    # 的近似重复（例：'炫耀' 改 '展示' 文本匹配抓不到）。
    # 每个 project_id 首次出现时从 DB 拉历史 embedding 预热一次。
    queue_embeddings: dict[str, list[dict]] = {}  # project_id → [{title, embedding}, ...]
    primed_projects: set[str] = set()

    for idx, plan in enumerate(plans):
        if stop_event.is_set():
            status["message"] = f"已停止（完成 {len(status['completed'])}/{len(plans)}）"
            break

        proj_name = plan.get("project_name", f"计划{idx+1}")
        tactic    = plan.get("tactic", "")
        status["current"] = idx
        status["message"] = f"计划 {idx+1}/{len(plans)} — {proj_name} · {tactic or '通用'}"

        try:
            # R-039: metrics 必须是 try 内**第一条**语句。旧位置在 project 取数
            # 之后 —— 异常发生在创建之前时(plan 缺键 / system_prompt 为 NULL 的
            # AttributeError 等), 首个 plan 的 except 里是 NameError(被内层 try
            # 吞掉, 错误卡片直接丢失); 第 ≥2 个 plan 则对**上一个 plan 已 close
            # 的** metrics 再 set_meta+close —— metrics_list 多出一张错误归因到
            # 错批次的重复卡片(quick gen 路径早已用"创建前置"修过, queue 未回灌)。
            metrics = telemetry.BatchMetrics(
                project_id=str(plan.get("project_id") or ""),
                mode="queue",
                engines=list(plan.get("engines", [])),
                count=int(plan.get("count", 0) or 0),
            )
            project_id = plan["project_id"]
            project = project_by_id.get(project_id)
            if not project:
                status["errors"].append(f"计划 {idx+1}：找不到项目 {project_id}")
                continue

            base_prompt = project.get("system_prompt", "")
            if not base_prompt.strip():
                status["errors"].append(f"计划 {idx+1}：项目未配置 System Prompt")
                continue

            # ── [A 取数] 项目配置/记忆/示例/会话指令（带 per-project 缓存）──
            # 单批次指标阶段名：setup（取数）/ llm（生成）/ db_save（落库）/
            # embedding（语义查重）
            metrics.start_phase("setup")
            _set_phase_progress(status, "setup")
            tactic_suffix = proj_module.get_tactic_prompt_suffix(project, tactic) if tactic else ""
            if project_id not in mem_cache:
                mem_cache[project_id] = db.get_confirmed_memories(
                    db_client, user_id, project_id=project_id
                )
            global_mems, project_mems = mem_cache[project_id]
            if project_id not in example_cache:
                example_cache[project_id] = (
                    db.list_example_items(db_client, project_id, "positive", limit=5),
                    db.list_example_items(db_client, project_id, "negative", limit=3),
                )
            pos_examples, neg_examples = example_cache[project_id]
            if project_id not in session_cache:
                session_cache[project_id] = db.get_session_instructions(
                    db_client, user_id, project_id=project_id
                )
            session_instr = session_cache[project_id]

            # ── [B 相关性筛选] 用本批 tactic+卖点过滤 soft 规则 ────────────
            # 把明显不相关的 soft 规则丢掉（例如生成酒类文案时，"标题别用数字"
            # 这种通用偏好可能与本次无关）。Hard 规则永远全部注入；老规则没有
            # embedding 时直接放行（不丢失）。
            context_text = " ".join(filter(None, [
                tactic,
                plan.get("key_messages", ""),
                plan.get("target_audience", ""),
                plan.get("extra_instructions", ""),
            ])).strip()
            # Day 4: 注入可视化 ─ 记录本批"硬/软/会话各注入多少、过滤掉哪些"
            inject_report: dict = {"filtered": []}
            # 审计 SUP-005: ctx 向量算【一次】给两遍过滤共用。以前两遍各算一次,
            # 同一段文本发了两次 embedding —— 调用数与这一步的延迟白白翻倍。
            #
            # codex review 2026-08-24: 但两个记忆表**都空**时一次也不能算。
            # filter_soft_by_relevance 开头就有 `if not memories: return`,
            # 所以老写法在新项目 / 无记忆项目上是 0 次调用; 把 prepare 提到外面
            # 之后反而变成每批都多打一次 embedding —— 本来要省的地方成了净增。
            _soft_ctx = (mem_module.prepare_soft_context(context_text)
                         if (global_mems or project_mems) else None)
            global_mems_for_plan  = mem_module.filter_soft_by_relevance(
                global_mems,  context_text, report_sink=inject_report,
                context=_soft_ctx,
            )
            project_mems_for_plan = mem_module.filter_soft_by_relevance(
                project_mems, context_text, report_sink=inject_report,
                context=_soft_ctx,
            )

            # Phase 1：用 layered builder 拿 5 段 dict（stable/tactic/p0/p1/p2），
            # 直接传给 generator —— Claude 路径会按 cache_control 分层、
            # Gemini 路径会拼回单字符串。跟 build_system_prompt 返回的字符串
            # byte-identical（layered_system_prompt_to_string 验证过）。
            # ── R-032: 向 TV 飞轮馆员借阅"真实爆款经验"（pull 模型, docs/15）。
            # 失败/超时/未配 → []，飞轮块自动不出现，写稿照常用 owner 自有正例。
            # R-038: 飞轮块不再进 system P2(每批必变会堵死 session 历史缓存),
            # 改经 user_context_block 注入当前 user turn —— 模型看到的内容不变。
            flywheel_lessons = librarian_client.fetch_flywheel_lessons(
                librarian_client.build_brief(
                    project,
                    tactic=tactic,
                    key_messages=plan.get("key_messages", ""),
                    target_audience=plan.get("target_audience", ""),
                    tone=plan.get("tone", ""),
                    extra_instructions=plan.get("extra_instructions", ""),
                )
            )
            flywheel_block = mem_module.render_flywheel_block(flywheel_lessons)
            inject_report["flywheel_lessons"] = len(flywheel_lessons or [])
            full_system_prompt = mem_module.build_layered_system_prompt(
                base_prompt=base_prompt,
                global_memories=global_mems_for_plan,
                project_memories=project_mems_for_plan,
                tactic_suffix=tactic_suffix,
                calibration_notes=project.get("calibration_notes") or "",
                positive_examples=pos_examples or None,
                negative_examples=neg_examples or None,
                session_instructions=session_instr or None,
                report_sink=inject_report,
            )
            metrics.set_meta("injection", inject_report)

            engines          = plan.get("engines", ["claude"])
            count            = plan.get("count", 1)
            engine_models    = plan.get("engine_models", {})
            use_thinking     = plan.get("use_thinking", False)
            gemini_thinking  = plan.get("gemini_use_thinking", False)
            extra_instr      = plan.get("extra_instructions", "")
            use_multi_role   = plan.get("use_multi_role", False)
            n_roles          = plan.get("n_roles", 3)
            custom_roles     = proj_module._parse_json_field(project.get("custom_roles"), []) or None

            batch_params = {
                "target_audience":  plan.get("target_audience", ""),
                "key_messages":     plan.get("key_messages", ""),
                "tone":             plan.get("tone", ""),
                "extra_instructions": extra_instr,
                "use_thinking":     use_thinking,
                "gemini_use_thinking": gemini_thinking,
                "engine_models":    engine_models,
                "use_multi_role":   use_multi_role,
            }
            batch = db.create_batch(
                db_client, user_id,
                project_id=project_id,
                tactic=tactic or "通用",
                params=batch_params,
                ai_engines=engines,
            )
            batch_id = batch["id"]

            if extra_instr and extra_instr.strip():
                mem_module.ingest_user_instruction(
                    db_client, user_id, extra_instr,
                    project_id=project_id,
                    project_name=project.get("name", ""),
                    batch_id=batch_id,
                )

            # ── [C 合并历史] DB 历史 + 本队列已生成的内存池 ─────────────────
            db_titles = db.get_recent_titles_and_openings(db_client, project_id)

            # 项目首次出现：拉一次历史 embedding 预热语义查重池
            if (project_id not in primed_projects
                and dedup_module.embeddings_available()):
                primed_projects.add(project_id)
                try:
                    hist_rows = db.get_recent_titles_openings_with_embeddings(
                        db_client, project_id
                    )
                    seed = [
                        {"title": h["title"], "embedding": h["embedding"]}
                        for h in hist_rows
                        if h.get("embedding") and h.get("title")
                    ]
                    if seed:
                        queue_embeddings[project_id] = seed
                except Exception as exc:
                    telemetry.log_event(
                        "embedding_prime_failed",
                        project_id=project_id, error=str(exc)[:200],
                    )
                    status.setdefault("warnings", []).append(
                        f"项目历史向量加载失败：{str(exc)[:120]}（去重池为空，本次队列重复率可能上升）"
                    )

            # 优先取内存池里的最新批次，再补 DB 历史（按 title+opening 去重），
            # 控制 prompt 的总长度 — _build_dedup_instruction 内部会截到最近 N 条
            pool = list(queue_titles.get(project_id, []))
            seen = {(p["title"], p.get("opening", "")) for p in pool}
            for h in (db_titles or []):
                key = (h.get("title", ""), h.get("opening", ""))
                if key in seen:
                    continue
                pool.append(h)
                seen.add(key)
            historical_titles = pool

            # ── [C+] Phase 2.1: session 路由 — 为每个 engine 拿历史 prefix ──
            # 失败时 (engine_session_ids 空 / engine_prior_messages 缺 key)
            # 自动 fallback 为空 prefix, 生成行为不受影响。
            engine_session_ids, engine_prior_messages = _resolve_engine_sessions(
                db_client, project, engines, engine_models or {}, user_id,
            )
            if engine_session_ids:
                metrics.set_meta("session_ids", engine_session_ids)

            metrics.stop_phase("setup")

            # ── [D 生成] 调用 generator；多引擎模式内部会串行复用 dedup pool ──
            _total   = count * len(engines)
            _done_n  = [0]
            def _progress(pct: float, msg: str, _idx=idx, _n=len(plans), _t=_total) -> None:
                # 把引擎层的 [0,1] 映射到 LLM 阶段在整批的占比（5%-75%），
                # 同时附上 "计划 X/Y" 前缀方便用户知道当前在跑哪一批
                _llm_intra_progress(status, pct, f"计划 {_idx+1}/{_n} — {msg}")

            metrics.start_phase("llm")
            _set_phase_progress(status, "llm")
            metrics.incr("llm_calls", len(engines))
            generation_results = _call_generator(
                multi_role=use_multi_role,
                system_prompt=full_system_prompt, tactic=tactic, count=count,
                engines=engines, engine_models=engine_models, plan=plan,
                extra_instructions=extra_instr,
                # 队列不支持图片 —— plan 字典里根本没有 image_prompt / images
                # 两个键(见 app.py 里"添加计划"构造的那份)。这是产品上就没做,
                # 不是漏传。
                images=None,
                progress_callback=_progress,
                historical_titles=historical_titles,
                use_thinking=use_thinking, gemini_thinking=gemini_thinking,
                custom_roles=custom_roles, n_roles=n_roles, metrics=metrics,
                engine_prior_messages=engine_prior_messages,
                user_context_block=flywheel_block,
                user_id=user_id, project_id=project_id,
            )

            metrics.stop_phase("llm")
            # token usage 累加在 generator 内部的 _engine_call 边界完成
            # （一次 API 调用 = 一次累加），避免对 ``count`` 个共享同一份
            # token_usage 的 version 重复加导致 count 倍膨胀。
            # Phase 2.1: 把本批的 input + cache_read + cache_create token
            # 累加到对应 engine 的 session 的 running_input_tokens(累计消耗统计)。
            _commit_session_tokens(db_client, engine_session_ids, metrics.meta)
            # Phase 2.3: SET 当前窗口占用 + 达阈值自动封窗(下一批自动开新窗)。
            sealed_engines = _update_session_occupancy(
                db_client, engine_session_ids, metrics.meta,
            )
            if sealed_engines:
                with (status.get("_lock") or _NULL_LOCK):
                    status.setdefault("sealed_engines", []).extend(sealed_engines)

            # ── [E 保存] 批量写 items + versions（统一服务 _save_batch_results）──
            error_prefix = f"计划 {idx+1}（{proj_name}）："
            # Day 5：解析队列策略（计划级 > 项目级 > config 默认）
            # ⚠️ 提到落库之前算 —— _resolve_queue_strategy 是纯函数(只读 plan /
            # project / config, 不碰库), 所以位置无所谓; 挪上来是为了让
            # _persist_and_check 能一次拿全参数。
            strategy_ctx = _resolve_queue_strategy(plan, project)
            metrics.set_meta(
                "queue_strategy",
                plan.get("strategy") or (project or {}).get("queue_strategy") or "default",
            )
            regen_ctx = {
                "enabled":             strategy_ctx["enabled"],
                "max_retries":         strategy_ctx["max_retries"],
                "threshold_override":  strategy_ctx["threshold_override"],
                "system_prompt":       full_system_prompt,
                "tactic":              tactic,
                "engines":             engines,
                "engine_models":       engine_models,
                "target_audience":     plan.get("target_audience", ""),
                "key_messages":        plan.get("key_messages", ""),
                "tone":                plan.get("tone", ""),
                "extra_instructions":  extra_instr,
                "use_thinking":        use_thinking,
                "gemini_use_thinking": gemini_thinking,
                "historical_titles":   historical_titles,
                "user_id":             user_id,
            }
            # ── [E 保存] + 语义查重 + 硬约束校验(统一走 _persist_and_check)──
            inserted_versions, produced_titles = _persist_and_check(
                db_client=db_client, batch_id=batch_id, user_id=user_id,
                generation_results=generation_results,
                error_prefix=error_prefix, errors_sink=status["errors"],
                metrics=metrics, status=status,
                dedup_pool=queue_embeddings, project_id=project_id,
                project=project, regen_ctx=regen_ctx,
                hard_rules=(validator.filter_hard(global_mems_for_plan)
                            + validator.filter_hard(project_mems_for_plan)),
            )
            saved = len(inserted_versions)

            # 累积本批的文本去重池，下一批立刻能看到（不依赖 DB 写入完成）。
            # 同步加去重 + 滑窗（与 queue_embeddings 一致），避免长队列下成本
            # 飙升与重复噪声污染下一批的 dedup_instruction。
            if produced_titles:
                tpool = queue_titles.setdefault(project_id, [])
                seen_titles = {entry.get("title") for entry in tpool}
                for entry in produced_titles:
                    t = entry.get("title") if isinstance(entry, dict) else None
                    if t and t not in seen_titles:
                        tpool.append(entry)
                        seen_titles.add(t)
                if len(tpool) > _QUEUE_POOL_MAX:
                    del tpool[: len(tpool) - _QUEUE_POOL_MAX]

            status["completed"].append({
                "plan_idx":    idx,
                "batch_id":    batch_id,
                "project_name": proj_name,
                "saved":       saved,
            })
            # 收尾：写一行 JSON 到 stdout + 挂到 status["metrics_list"] 给 UI +
            # Day 5: 持久化到 batch_metrics 表，给历史页 / 性能看板用
            metrics.batch_id = batch_id
            metrics.set_meta("saved", saved)
            metrics.close(
                status,
                persist=lambda d: db.insert_batch_metrics(
                    db_client,
                    batch_id=d.get("batch_id") or batch_id,
                    project_id=project_id,
                    user_id=user_id,
                    phase_ms=d.get("phase_ms") or {},
                    counters=d.get("counters") or {},
                    meta={k: v for k, v in (d.get("meta") or {}).items()
                          if k != "injection"},
                    injection=(d.get("meta") or {}).get("injection") or {},
                ),
            )

        except Exception as exc:
            status["errors"].append(f"计划 {idx+1}：{exc}")
            try:
                metrics.set_meta("error", str(exc)[:120])
                metrics.close(status)
            except Exception:
                pass

    with (status.get("_lock") or _NULL_LOCK):
        status["running"] = False
        status["done"]    = True
        status["phase"]   = "done"
    if not stop_event.is_set():
        n_ok  = len(status["completed"])
        n_err = len(status["errors"])
        status["message"] = f"全部完成 ✓ {n_ok} 计划成功" + (f"，{n_err} 失败" if n_err else "")

def _quick_gen_worker(plan: dict, user_id: str, db_client, status: dict) -> None:
    """快速生成的后台 worker（单批次版的 _queue_worker）。

    现在也产出 telemetry.BatchMetrics 一致的指标日志（setup / llm /
    db_save / embedding 四个阶段 + dedup/regen 计数器），方便和队列模式
    做对比。
    """
    # metrics 必须在 try 块外创建，否则 except / finally 引用 metrics 会
    # UnboundLocalError；这两行实例化几乎不可能抛错（纯 dataclass init）。
    metrics = telemetry.BatchMetrics(
        project_id=plan.get("project_id", ""),
        mode="quick",
        engines=list(plan.get("engines", [])),
        count=int(plan.get("count", 0)),
    )
    try:
        metrics.start_phase("setup")
        qlock = status.get("_lock")
        if qlock is None:
            qlock = threading.Lock()
            status["_lock"] = qlock
        with qlock:
            status["running"] = True
            status["phase"]   = "running"
        status["message"] = "正在构建提示词…"
        status.setdefault("warnings", [])
        status.setdefault("embedding_missing", [])
        _set_phase_progress(status, "setup")

        project_id = plan["project_id"]
        # ⚠️ 必须走**带 owner 过滤**的那个。这段编排现在两种客户端都会跑到:
        # UI 传的是用户自己的客户端(RLS 挡着), worker 传的是 service_role
        # (绕过 RLS)。而 jobs 的 RLS 策略只管 user_id, payload 是自由 JSONB ——
        # 任何人都能插一条 project_id 指向别人项目的 job。用不带过滤的
        # get_project, worker 就会把对方的 system_prompt / 校准笔记 / 正反例
        # 读出来, 并在对方项目下写内容。(队列那条路本来就安全: 它的项目字典
        # 是 db.list_projects(client, user_id) 建的, 那个查询自带 owner 过滤。)
        project = db.get_project_owned(db_client, project_id, user_id)
        if not project:
            raise RuntimeError(f"项目不存在, 或不属于当前调用者: {project_id}")

        tactic          = plan.get("tactic", "")
        engines         = plan.get("engines", ["claude"])
        engine_models   = plan.get("engine_models", {})
        count           = plan.get("count", 3)
        use_thinking    = plan.get("use_thinking", False)
        gemini_thinking = plan.get("gemini_use_thinking", False)
        use_multi_role  = plan.get("use_multi_role", False)
        n_roles         = plan.get("n_roles", 3)
        extra_instr     = plan.get("extra_instructions", "")
        image_prompt    = plan.get("image_prompt", "")
        images          = plan.get("images") or None
        custom_roles    = proj_module._parse_json_field(project.get("custom_roles"), []) or None

        global_mems, project_mems = db.get_confirmed_memories(
            db_client, user_id, project_id=project_id
        )
        pos_examples      = db.list_example_items(db_client, project_id, "positive", limit=5)
        neg_examples      = db.list_example_items(db_client, project_id, "negative", limit=3)
        calibration_notes = project.get("calibration_notes") or ""
        session_instr     = db.get_session_instructions(db_client, user_id, project_id=project_id)

        tactic_suffix = proj_module.get_tactic_prompt_suffix(project, tactic) if tactic else ""

        # Day 4：注入可视化（quick gen 与 queue worker 行为一致）
        # Phase 1：同 worker 路径，用 layered builder。
        inject_report: dict = {"filtered": []}
        # R-039: 补齐 queue 路径的 [B 相关性筛选] —— 上面那行"与 queue worker
        # 行为一致"的注释此前是假的: quick gen 把 soft 规则全量注入, 同一项目
        # 两条路 prompt 不同、注入可视化的"过滤 N 条"恒为 0。hard 规则不受
        # filter_soft_by_relevance 影响, 全量保留。
        # PR #54 review: image_prompt 也要进相关性上下文 —— 它最终经
        # combined_extra 发给模型, 不参与过滤的话, 图片相关的 soft 规则
        # (如"产品图描述偏好")会因与 tactic/卖点语义距离远而被静默滤掉。
        _qg_context_text = " ".join(filter(None, [
            tactic,
            plan.get("key_messages", ""),
            plan.get("target_audience", ""),
            extra_instr,
            image_prompt,
        ])).strip()
        # 审计 SUP-005: 同 _queue_worker_impl —— ctx 向量算一次给两遍共用,
        # 且两个记忆表都空时一次也不算(codex review; 见那边的完整说明)。
        _qg_soft_ctx = (mem_module.prepare_soft_context(_qg_context_text)
                        if (global_mems or project_mems) else None)
        global_mems = mem_module.filter_soft_by_relevance(
            global_mems, _qg_context_text, report_sink=inject_report,
            context=_qg_soft_ctx,
        )
        project_mems = mem_module.filter_soft_by_relevance(
            project_mems, _qg_context_text, report_sink=inject_report,
            context=_qg_soft_ctx,
        )
        # ── R-032: 同 _queue_worker_impl —— 借阅飞轮经验（fail-open 成 []）。
        # R-038: 改经 user_context_block 注入 user turn, 不再进 system P2。
        flywheel_lessons = librarian_client.fetch_flywheel_lessons(
            librarian_client.build_brief(
                project,
                tactic=tactic,
                key_messages=plan.get("key_messages", ""),
                target_audience=plan.get("target_audience", ""),
                tone=plan.get("tone", ""),
                extra_instructions=extra_instr,
            )
        )
        flywheel_block = mem_module.render_flywheel_block(flywheel_lessons)
        inject_report["flywheel_lessons"] = len(flywheel_lessons or [])
        full_system_prompt = mem_module.build_layered_system_prompt(
            base_prompt=project.get("system_prompt", ""),
            global_memories=global_mems,
            project_memories=project_mems,
            tactic_suffix=tactic_suffix,
            calibration_notes=calibration_notes,
            positive_examples=pos_examples or None,
            negative_examples=neg_examples or None,
            session_instructions=session_instr or None,
            report_sink=inject_report,
        )
        metrics.set_meta("injection", inject_report)

        combined_extra = extra_instr
        if image_prompt:
            combined_extra = (combined_extra + "\n\n【参考图片说明】\n" + image_prompt).strip()

        batch = db.create_batch(
            db_client, user_id,
            project_id=project_id,
            tactic=tactic or "通用",
            params={
                "target_audience":    plan.get("target_audience", ""),
                "key_messages":       plan.get("key_messages", ""),
                "tone":               plan.get("tone", ""),
                "extra_instructions": extra_instr,
                "use_thinking":       use_thinking,
                "gemini_use_thinking": gemini_thinking,
                "engine_models":      engine_models,
                "use_multi_role":     use_multi_role,
            },
            ai_engines=engines,
        )
        batch_id = batch["id"]
        status["batch_id"] = batch_id

        # Route extra_instructions through the AI merger so useful rules sink
        # into permanent memory (or calibration notes) and one-offs stay in
        # the 24h session layer.
        if extra_instr and extra_instr.strip():
            mem_module.ingest_user_instruction(
                db_client, user_id, extra_instr,
                project_id=project_id,
                project_name=project.get("name", ""),
                batch_id=batch_id,
            )

        historical_titles = db.get_recent_titles_and_openings(db_client, project_id)

        # R-039: 预热 DB 历史向量池(对齐 queue 的 primed_projects 逻辑)。
        # 旧的空 dict 让 _run_semantic_dedup_pass 的"历史对比"整段跳过 ——
        # quick gen 永远检不出与库内历史"换字不换义"的语义重复, 且零提示。
        # PR #54 review: 必须在 _save_batch_results **之前**取 —— 保存后再取,
        # 本批刚插入的版本(尚无 embedding)会按"最新"挤占 limit 名额, 再被
        # 下面的 embedding 过滤丢掉 → 大批次能把真历史整段挤出查重池。
        # queue 路径的预热同样发生在保存前(首见项目时), 此处对齐。
        quick_queue_embeddings: dict[str, list[dict]] = {}
        if dedup_module.embeddings_available():
            try:
                _hist_rows = db.get_recent_titles_openings_with_embeddings(
                    db_client, project_id
                )
                _seed = [
                    {"title": h["title"], "embedding": h["embedding"]}
                    for h in _hist_rows
                    if h.get("embedding") and h.get("title")
                ]
                if _seed:
                    quick_queue_embeddings[project_id] = _seed
            except Exception as _exc:
                telemetry.log_event(
                    "embedding_prime_failed",
                    project_id=project_id, error=str(_exc)[:200],
                )
                status.setdefault("warnings", []).append(
                    f"项目历史向量加载失败：{str(_exc)[:120]}（语义查重缺历史维度）"
                )

        # Phase 2.1: session 路由 — 同 _queue_worker_impl 逻辑
        engine_session_ids, engine_prior_messages = _resolve_engine_sessions(
            db_client, project, engines, engine_models or {}, user_id,
        )
        if engine_session_ids:
            metrics.set_meta("session_ids", engine_session_ids)

        metrics.stop_phase("setup")

        def _progress(pct: float, msg: str) -> None:
            # 把引擎层 [0,1] 映射到 LLM 阶段在整批的占比，
            # 不会覆盖后续 db_save / embedding 阶段
            _llm_intra_progress(status, pct, msg)

        status["message"] = "正在生成内容…"
        metrics.start_phase("llm")
        _set_phase_progress(status, "llm")
        metrics.incr("llm_calls", len(engines))

        generation_results = _call_generator(
            multi_role=use_multi_role,
            system_prompt=full_system_prompt, tactic=tactic, count=count,
            engines=engines, engine_models=engine_models, plan=plan,
            # combined_extra 里拼了 image_prompt —— 快速生成支持图片, 队列不支持。
            extra_instructions=combined_extra,
            images=images,
            progress_callback=_progress,
            historical_titles=historical_titles,
            use_thinking=use_thinking, gemini_thinking=gemini_thinking,
            custom_roles=custom_roles, n_roles=n_roles, metrics=metrics,
            engine_prior_messages=engine_prior_messages,
            user_context_block=flywheel_block,
            user_id=user_id, project_id=project_id,
        )

        metrics.stop_phase("llm")
        # token usage 累加在 generator 内部完成（见 _engine_call）
        # Phase 2.1: 累加 input/cache 到对应 session 的 running_input_tokens
        _commit_session_tokens(db_client, engine_session_ids, metrics.meta)
        # Phase 2.3: SET 当前窗口占用 + 达阈值自动封窗(下一批自动开新窗)。
        sealed_engines = _update_session_occupancy(
            db_client, engine_session_ids, metrics.meta,
        )
        if sealed_engines:
            with (status.get("_lock") or _NULL_LOCK):
                status.setdefault("sealed_engines", []).extend(sealed_engines)

        # ── 批量保存：复用与 _queue_worker 完全相同的 _save_batch_results ──
        # （修 R1：之前是 create_item / create_version 逐条 INSERT，
        #  ~30 round trip；改批量后收敛为 2 次）
        # R-039: 直接挂到 status —— 旧的本地 list 只在成功跑到结尾才赋给
        # status["errors"], 中途任何一步抛异常(_save_batch_results 之后的
        # dedup/硬约束/occupancy), 已收集的"某引擎生成失败"等全部丢失,
        # 运行中 UI 也看不到(queue 路径从一开始就是直挂的)。
        errors = status.setdefault("errors", [])

        # ── 语义查重（同 _queue_worker 走 _run_semantic_dedup_pass）─────
        # Quick Generate 只跑一个批次，所以 queue_embeddings 是个只有当前
        # project_id 的临时字典；命中重复直接写到 errors 数组里。
        _set_phase_progress(status, "embedding")
        # (历史向量池 quick_queue_embeddings 已在保存批次**之前**预热 ——
        #  见 historical_titles 取数处; PR #54 review: 在保存之后预热会让刚
        #  落库的无向量版本挤占 limit 名额, 把真历史挤出语义查重池。)
        # Day 5：quick gen 也支持策略覆盖（plan 字段同 queue）
        strategy_ctx = _resolve_queue_strategy(plan, project)
        metrics.set_meta(
            "queue_strategy",
            plan.get("strategy") or (project or {}).get("queue_strategy") or "default",
        )
        regen_ctx = {
            "enabled":             strategy_ctx["enabled"],
            "max_retries":         strategy_ctx["max_retries"],
            "threshold_override":  strategy_ctx["threshold_override"],
            "system_prompt":       full_system_prompt,
            "tactic":              tactic,
            "engines":             engines,
            "engine_models":       engine_models,
            "target_audience":     plan.get("target_audience", ""),
            "key_messages":        plan.get("key_messages", ""),
            "tone":                plan.get("tone", ""),
            "extra_instructions":  combined_extra,
            "use_thinking":        use_thinking,
            "gemini_use_thinking": gemini_thinking,
            "historical_titles":   historical_titles,
            "user_id":             user_id,
        }
        # ── 落库 + 语义查重 + 硬约束校验(与 queue 走同一个 _persist_and_check)──
        inserted_versions, _produced_titles = _persist_and_check(
            db_client=db_client, batch_id=batch_id, user_id=user_id,
            generation_results=generation_results,
            # Quick Generate 只有一个批次, 不需要 "计划 N（项目）：" 前缀
            error_prefix="", errors_sink=errors,
            metrics=metrics, status=status,
            dedup_pool=quick_queue_embeddings, project_id=project_id,
            project=project, regen_ctx=regen_ctx,
            hard_rules=(validator.filter_hard(global_mems)
                        + validator.filter_hard(project_mems)),
        )
        saved_count = len(inserted_versions)

        status["saved_count"] = saved_count
        status["n_results"]   = len(generation_results)
        # (errors 已实时挂在 status["errors"] 上, 无需结尾回写)
        status["message"]     = f"生成完成！{len(generation_results)} 篇，{saved_count} 个版本已保存。"
        metrics.batch_id = batch_id
        metrics.set_meta("saved", saved_count)

    except Exception as exc:
        status.setdefault("errors", []).append(str(exc))
        status["message"] = f"生成失败：{exc}"
        try:
            metrics.set_meta("error", str(exc)[:120])
        except Exception:
            pass
    finally:
        # Day 5：持久化批次指标（quick gen 也走同一张表，模式由 d["mode"] 区分）。
        # 放到 finally：即使 try 内意外抛 BaseException（SystemExit / 内存错误）
        # 也要把 status 状态机拨回 done，否则 UI 启动/停止按钮永远 disabled。
        try:
            _quick_batch_id = status.get("batch_id") or metrics.batch_id
            _quick_project_id = plan.get("project_id", "")
            metrics.close(
                status,
                persist=(
                    (lambda d: db.insert_batch_metrics(
                        db_client,
                        batch_id=d.get("batch_id") or _quick_batch_id,
                        project_id=_quick_project_id,
                        user_id=user_id,
                        phase_ms=d.get("phase_ms") or {},
                        counters=d.get("counters") or {},
                        meta={k: v for k, v in (d.get("meta") or {}).items()
                              if k != "injection"},
                        injection=(d.get("meta") or {}).get("injection") or {},
                    ))
                    if _quick_batch_id and _quick_project_id else None
                ),
            )
        except Exception:
            pass
        try:
            with (status.get("_lock") or _NULL_LOCK):
                status["running"] = False
                status["done"]    = True
                status["phase"]   = "done"
        except Exception:
            # 最后兜底：连锁都拿不到也要确保 phase=done
            status["running"] = False
            status["done"]    = True
            status["phase"]   = "done"

# ── 公开名 ────────────────────────────────────────────────────────────────
# 模块内部一律沿用搬迁前的下划线名字(这样这次搬迁才是逐字节可验的), 对外
# 另给一组不带下划线的名字。调用方(app.py / worker.py)用这一组。
NULL_LOCK = _NULL_LOCK
queue_worker = _queue_worker
quick_gen_worker = _quick_gen_worker
queue_worker_impl = _queue_worker_impl
save_batch_results = _save_batch_results
resolve_queue_strategy = _resolve_queue_strategy
