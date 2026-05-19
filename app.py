"""
XHS Content Workstation — Main Streamlit Application
小红书内容自动化工作台 v2.0

Entry point: streamlit run app.py
"""

from __future__ import annotations

import copy
import html as _html
import json
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import streamlit as st

import config
import auth
import db
import projects as proj_module
import memory as mem_module
import generator as gen_module
import image_handler
import exporter
import dedup as dedup_module
import telemetry

_BEIJING_TZ = timezone(timedelta(hours=8))


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
    并跳过保存。``error_prefix`` 控制日志前缀，例如 "计划 3（项目 A）"。
    """
    # 步骤 1：拼 items 的批量行，每个 slot 一条
    item_rows: list[dict] = []
    for slot in generation_results:
        item_rows.append({
            "batch_id": batch_id,
            "user_id":  user_id,
            **({"ai_review_notes": slot["ai_review_notes"]}
               if slot.get("ai_review_notes") else {}),
        })
    try:
        inserted_items = db.bulk_create_items(db_client, item_rows)
    except Exception as exc:
        errors_sink.append(f"{error_prefix}批量写入 items 失败 — {exc}")
        inserted_items = []

    # 步骤 2：从生成结果里抽 versions 行 + 顺便构造文本去重池要的 opening
    version_rows: list[dict] = []
    produced_titles: list[dict] = []
    for slot, item in zip(generation_results, inserted_items):
        for vr in slot["versions"]:
            if vr.error and not vr.title:
                errors_sink.append(f"{error_prefix}· {vr.ai_engine}：{vr.error}")
                continue
            version_rows.append({
                "item_id":     item["id"],
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

    # 步骤 3：批量插 versions
    inserted_versions: list[dict] = []
    try:
        inserted_versions = db.bulk_create_initial_versions(db_client, version_rows)
    except Exception as exc:
        errors_sink.append(f"{error_prefix}批量写入 versions 失败 — {exc}")

    return inserted_items, inserted_versions, produced_titles


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
        db.update_version_content(
            db_client, version_id,
            title=new_version.title,
            body=new_version.body,
            keywords=new_version.keywords,
            token_usage=new_version.token_usage,
            embedding=candidate_vec,
        )
        titles_in_order[dup_idx] = new_version.title.strip()
        new_vecs[dup_idx]        = candidate_vec
        version_rows[dup_idx]["title"] = new_version.title  # 给后面 queue_titles 用
        return True

    # 重试用尽 → 标记 needs_revision
    if item_id:
        try:
            db.update_item_status(db_client, item_id, "needs_revision")
        except Exception:
            pass
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
) -> None:
    """两个 worker 共用的语义查重 + embedding 持久化（修 R3）。

    流程：
      1. 给所有新标题算 768d embedding（一次 API 调用，批量请求）
      2. 写入 versions.embedding 列
      3. 与历史池对比，cos ≥ DEDUP_SEMANTIC_THRESHOLD 视为近似重复
      4. 本批内对比（多引擎撞车场景）
      5. 累积进 queue_embeddings，下一批能立刻看到

    若 ``regen_ctx`` 非空且 ``regen_ctx["enabled"]`` 为 True，会对每个命中
    的版本调用 _try_regen_one 自动重生：
      - 重生成功 → UPDATE versions 行、刷新 pool、不写警告
      - 重试 max_retries 仍命中 → 把 item.status 改成 needs_revision，并
        写警告到 ``errors_sink``
      - 重生功能关闭时，所有命中只写警告，不改 DB

    ``queue_embeddings`` 是 worker 自己维护的字典，按 project_id 分桶。
    Quick Generate 也建一份只有一个项目的字典。
    没配 GOOGLE_API_KEY 时整段跳过，不影响主流程。
    """
    regen_enabled = bool(regen_ctx and regen_ctx.get("enabled"))
    if not inserted_versions or not dedup_module.embeddings_available():
        return
    metrics.start_phase("embedding")
    try:
        titles_in_order = [r.get("title", "") for r in version_rows]
        new_vecs = dedup_module.embed_texts(titles_in_order)
        if not new_vecs:
            return
        # 步骤 2：持久化 embedding
        embed_rows = []
        for i, iv in enumerate(inserted_versions):
            if i < len(new_vecs):
                embed_rows.append({"id": iv.get("id"), "embedding": new_vecs[i]})
        if embed_rows:
            db.bulk_update_version_embeddings(db_client, embed_rows)

        # 步骤 3：对照历史池
        threshold = float(getattr(config, "DEDUP_SEMANTIC_THRESHOLD", 0.92))
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
            for dup_idx in sorted(duplicate_indices):
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

        # 步骤 5：累积给下一批用（regen 路径可能已经原地改过 new_vecs）
        pool = queue_embeddings.setdefault(project_id, [])
        for i, t in enumerate(titles_in_order):
            if i < len(new_vecs) and t:
                # 跳过 pool 里已经有的（regen 时会原地替换）
                pool.append({"title": t, "embedding": new_vecs[i]})
    finally:
        metrics.stop_phase("embedding")


def _queue_worker(
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
    status["running"] = True
    status["done"] = False
    status["total"] = len(plans)
    status.setdefault("completed", [])
    status.setdefault("errors", [])

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
            project_id = plan["project_id"]
            project = project_by_id.get(project_id)
            if not project:
                status["errors"].append(f"计划 {idx+1}：找不到项目 {project_id}")
                continue

            base_prompt = project.get("system_prompt", "")
            if not base_prompt.strip():
                status["errors"].append(f"计划 {idx+1}：项目未配置 System Prompt")
                continue

            # 单批次指标：用于事后分析"慢/卡/重"出在哪一段，对应阶段名：
            # setup（取数）/ llm（生成）/ db_save（落库）/ embedding（语义查重）
            metrics = telemetry.BatchMetrics(
                project_id=project_id,
                mode="queue",
                engines=list(plan.get("engines", [])),
                count=int(plan.get("count", 0)),
            )

            # ── [A 取数] 项目配置/记忆/示例/会话指令（带 per-project 缓存）──
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
            global_mems_for_plan  = mem_module.filter_soft_by_relevance(global_mems,  context_text)
            project_mems_for_plan = mem_module.filter_soft_by_relevance(project_mems, context_text)

            full_system_prompt = mem_module.build_system_prompt(
                base_prompt=base_prompt,
                global_memories=global_mems_for_plan,
                project_memories=project_mems_for_plan,
                tactic_suffix=tactic_suffix,
                calibration_notes=project.get("calibration_notes") or "",
                positive_examples=pos_examples or None,
                negative_examples=neg_examples or None,
                session_instructions=session_instr or None,
            )

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
                except Exception:
                    pass

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
            if use_multi_role:
                generation_results = gen_module.generate_batch_multi_role(
                    system_prompt=full_system_prompt,
                    tactic=tactic,
                    count=count,
                    engines=engines,
                    engine_models=engine_models or None,
                    target_audience=plan.get("target_audience", ""),
                    key_messages=plan.get("key_messages", ""),
                    tone=plan.get("tone", ""),
                    extra_instructions=extra_instr,
                    images=None,
                    progress_callback=_progress,
                    historical_titles=historical_titles or None,
                    use_thinking=use_thinking,
                    gemini_use_thinking=gemini_thinking,
                    custom_roles=custom_roles,
                    n_roles=n_roles,
                )
            else:
                generation_results = gen_module.generate_batch(
                    system_prompt=full_system_prompt,
                    tactic=tactic,
                    count=count,
                    engines=engines,
                    target_audience=plan.get("target_audience", ""),
                    key_messages=plan.get("key_messages", ""),
                    tone=plan.get("tone", ""),
                    extra_instructions=extra_instr,
                    images=None,
                    progress_callback=_progress,
                    historical_titles=historical_titles or None,
                    use_thinking=use_thinking,
                    engine_models=engine_models or None,
                    gemini_use_thinking=gemini_thinking,
                    user_id=user_id,
                    project_id=project_id,
                )

            metrics.stop_phase("llm")

            # ── [E 保存] 批量写 items + versions（统一服务 _save_batch_results）──
            metrics.start_phase("db_save")
            _set_phase_progress(status, "db_save")
            error_prefix = f"计划 {idx+1}（{proj_name}）："
            inserted_items, inserted_versions, produced_titles = _save_batch_results(
                db_client, batch_id, user_id, generation_results,
                error_prefix, status["errors"],
            )
            # version_rows 是 _save_batch_results 内部构造的临时变量；
            # 在这里从 inserted_versions 还原（embedding / 自动重生流程要用）
            version_rows = [
                {"title": v.get("title", ""), "ai_engine": v.get("ai_engine", "")}
                for v in inserted_versions
            ]
            saved = len(inserted_versions)
            metrics.stop_phase("db_save")

            # ── 语义查重（统一服务 _run_semantic_dedup_pass）──────────────────
            _set_phase_progress(status, "embedding")
            regen_ctx = {
                "enabled":             bool(getattr(config, "ENABLE_DEDUP_REGEN", False)),
                "max_retries":         int(getattr(config, "DEDUP_REGEN_MAX_RETRIES", 2)),
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
            _run_semantic_dedup_pass(
                db_client, inserted_versions, version_rows,
                queue_embeddings, project_id, error_prefix,
                status["errors"], metrics, regen_ctx=regen_ctx,
            )
            _set_phase_progress(status, "embedding", intra=1.0)

            # 累积本批的文本去重池，下一批立刻能看到（不依赖 DB 写入完成）
            if produced_titles:
                queue_titles.setdefault(project_id, []).extend(produced_titles)

            status["completed"].append({
                "plan_idx":    idx,
                "batch_id":    batch_id,
                "project_name": proj_name,
                "saved":       saved,
            })
            # 收尾：写一行 JSON 到 stdout + 挂到 status["metrics_list"] 给 UI
            metrics.batch_id = batch_id
            metrics.set_meta("saved", saved)
            metrics.close(status)

        except Exception as exc:
            status["errors"].append(f"计划 {idx+1}：{exc}")
            try:
                metrics.set_meta("error", str(exc)[:120])
                metrics.close(status)
            except Exception:
                pass

    status["running"] = False
    status["done"]    = True
    if not stop_event.is_set():
        n_ok  = len(status["completed"])
        n_err = len(status["errors"])
        status["message"] = f"全部完成 ✓ {n_ok} 计划成功" + (f"，{n_err} 失败" if n_err else "")


# Streamlit's @st.fragment auto-reruns only the decorated block, leaving
# the rest of the page alone.  Available since Streamlit 1.33; we fall back
# to the plain function on older versions.
_FRAGMENT = getattr(st, "fragment", None)


def _queue_banner_body() -> None:
    """The actual banner content.  Factored out so the same code runs both
    in fragment mode and as a normal function on Streamlit < 1.33."""
    qs = st.session_state.get("queue_state")
    if not qs:
        return
    completed = len(qs.get("completed", []))
    total     = qs.get("total", 0)
    msg       = qs.get("message", "")
    errors    = qs.get("errors", [])

    if qs.get("running"):
        banner = st.container()
        with banner:
            bcol_txt, bcol_btn = st.columns([5, 1])
            with bcol_txt:
                st.info(f"🔄 队列生成中 ({completed}/{total}) — {msg}")
            with bcol_btn:
                if st.button("⏹ 停止", key="global_stop_queue", use_container_width=True):
                    evt = st.session_state.get("queue_stop_event")
                    if evt:
                        evt.set()
    elif qs.get("done"):
        bcol_txt, bcol_btn = st.columns([5, 1])
        with bcol_txt:
            if errors:
                st.warning(f"✅ 队列完成 — {completed}/{total} 成功，{len(errors)} 失败：" + "；".join(errors))
            else:
                st.success(f"✅ 队列完成！{completed} 个批次已保存 — {msg}")
        with bcol_btn:
            if st.button("清除", key="clear_queue_status", use_container_width=True):
                st.session_state.pop("queue_state", None)
                st.session_state.pop("queue_stop_event", None)
                st.rerun()


if _FRAGMENT is not None:
    # When a queue is running, this fragment auto-reruns every 2 s without
    # forcing the rest of the page to re-execute.  Massive perceived-perf
    # win: pre-fragment, every 2 s the entire 3000+ line script re-ran just
    # to update the progress bar, which is why other UI felt frozen during
    # batch runs.
    _queue_banner = _FRAGMENT(run_every=2.0)(_queue_banner_body)
else:
    _queue_banner = _queue_banner_body


def _running_snapshot_body(qs_key: str = "queue_state") -> None:
    """Live progress bar that polls queue state via fragment refresh.

    ``qs_key`` is the session_state key to read (queue tab uses queue_state;
    the quick-generate path uses queue_state_qg).
    """
    qs = st.session_state.get(qs_key)
    if not qs or not qs.get("running"):
        return
    completed = len(qs.get("completed", []))
    total     = qs.get("total", 0)
    progress  = qs.get("progress")
    if progress is None and total:
        progress = completed / total
    elif progress is None:
        progress = 0.0
    st.progress(progress, text=qs.get("message", "生成中…"))
    st.caption("生成在后台运行，可切换到其他页面。")


if _FRAGMENT is not None:
    _render_running_snapshot = _FRAGMENT(run_every=2.0)(_running_snapshot_body)
else:
    _render_running_snapshot = _running_snapshot_body


def _quick_gen_snapshot_body() -> None:
    """Live progress for the quick-generate path, mirrors _running_snapshot_body
    but reads from the separate ``quick_gen_state`` key."""
    qgs = st.session_state.get("quick_gen_state")
    if not qgs or not qgs.get("running"):
        return
    pct = qgs.get("progress", 0.0)
    msg = qgs.get("message", "生成中…")
    st.progress(pct, text=msg)
    st.caption("生成在后台运行，可切换到其他页面。")


if _FRAGMENT is not None:
    _render_quick_gen_snapshot = _FRAGMENT(run_every=2.0)(_quick_gen_snapshot_body)
else:
    _render_quick_gen_snapshot = _quick_gen_snapshot_body


def _format_batch_label(batch: dict, project_name: str = "") -> str:
    """Format a batch label consistently: project · tactic · date · time (Beijing)."""
    tactic = batch.get("tactic", "通用")
    created_at = batch.get("created_at", "")
    # Parse and convert to Beijing time
    try:
        # Supabase returns ISO format like "2024-03-09T14:30:00.123456+00:00"
        dt_str = created_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        beijing_dt = dt.astimezone(_BEIJING_TZ)
        time_str = beijing_dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        time_str = created_at[:16] if created_at else "未知时间"

    parts = []
    if project_name:
        parts.append(project_name)
    if tactic:
        parts.append(tactic)
    parts.append(time_str)
    return " · ".join(parts)


# ── Page config ────────────────────────────────────────────────────────────
st.set_page_config(
    page_title=config.APP_TITLE,
    page_icon="✦",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ─────────────────────────────────────────────────────────────
st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

/* ═══════════════════════════════════════════
   Design tokens — Studio-grade (hard-edge + lime)
   No rounded corners. Lines are the primary
   structural element. Lime accent only for
   focus / active / chapter numbering.
   ═══════════════════════════════════════════ */
:root {
  /* Palette */
  --accent:        #C6F75C;
  --accent-2:      #A8E028;
  --accent-soft:   #E9FBB8;
  --bg:            #FFFFFF;
  --bg-soft:       #F5F5F3;
  --card:          #FFFFFF;
  --card-inverse:  #0A0A0A;
  --text-1:        #0A0A0A;
  --text-2:        #5A5A5A;
  --text-3:        #9A9A9A;
  --text-on-dark:  #FFFFFF;
  --border:        #E5E5E2;
  --border-mid:    #C9C9C3;
  --border-strong: #0A0A0A;
  --green:         #16A34A;
  --green-soft:    #F0FDF4;
  --amber:         #D97706;
  --amber-soft:    #FFFBEB;
  --slate:         #64748B;
  --slate-soft:    #F8FAFC;

  /* Shadows — kept extremely subtle; studio style relies on lines not depth */
  --shadow-sm:     0 1px 0 rgba(10,10,10,.04);
  --shadow-md:     0 2px 0 rgba(10,10,10,.06);

  /* Radii — hard edges */
  --r-sm:   0px;
  --r-md:   2px;
  --r-lg:   0px;
  --r-pill: 0px;

  /* Line weights */
  --line-thin:   1px;
  --line:        1.5px;
  --line-heavy:  2px;

  /* Font sizes (type scale) */
  --fs-hero:    3.25rem;
  --fs-title:   1.75rem;
  --fs-card:    1.125rem;
  --fs-body:    0.9375rem;
  --fs-meta:    0.75rem;
  --fs-tag:     0.72rem;

  /* Spacing rhythm */
  --sp-section: 3.5rem;
  --sp-block:   1.75rem;
  --sp-card:    1rem;
  --sp-line:    0.5rem;

  /* Fonts */
  --font-sans: "Space Grotesk", -apple-system, BlinkMacSystemFont, "Inter",
               "Segoe UI", Helvetica, Arial, sans-serif;
  --font-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
}

/* ── App background & typography ── */
.stApp, .stApp > .main {
  background: var(--bg) !important;
  font-family: var(--font-sans);
  color: var(--text-1);
}

/* ── Sidebar — left rail with chapter nav ── */
[data-testid="stSidebar"] {
  background: var(--bg) !important;
  border-right: var(--line) solid var(--border-strong) !important;
}
[data-testid="stSidebar"] .stMarkdown p,
[data-testid="stSidebar"] .stMarkdown small,
[data-testid="stSidebar"] label {
  color: var(--text-2) !important;
  font-size: 0.8125rem !important;
}
[data-testid="stSidebar"] .stRadio label {
  font-size: 0.9rem !important;
  font-weight: 500;
  color: var(--text-1) !important;
}

/* ── Sidebar nav — hard-edge chapter list (full-width rows) ── */
/* Nuclear: BaseWeb renders each radio option inside
   <div data-baseweb="radio"> which defaults to inline-block
   (content-width). Target those attribute wrappers directly. */
[data-testid="stSidebar"] [data-baseweb="radio-group"],
[data-testid="stSidebar"] [data-baseweb="radio"] {
  display: block !important;
  width: 100% !important;
  max-width: 100% !important;
  box-sizing: border-box !important;
}
/* Force every ancestor container to the full sidebar width */
[data-testid="stSidebar"] [data-testid="stRadio"],
[data-testid="stSidebar"] [data-testid="stRadio"] > div,
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"],
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] > div,
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] > * {
  width: 100% !important;
  max-width: 100% !important;
}
/* Switch the radiogroup from its default flex-wrap (which sizes
   each item to its content) to simple block stacking */
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] {
  display: block !important;
  border-top: var(--line-thin) solid var(--border) !important;
}
/* BaseWeb wraps radio items 2 levels deep; force EVERY div inside
   the radiogroup to full-width block so the final <label> actually
   resolves width:100% against the sidebar, not a content-sized
   wrapper */
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] div {
  display: block !important;
  width: 100% !important;
  max-width: 100% !important;
}
/* Item label = a full-width row */
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label {
  display: flex !important;
  align-items: center !important;
  width: 100% !important;
  box-sizing: border-box !important;
  border-radius: 0 !important;
  padding: 12px 14px 12px 18px !important;
  margin: 0 !important;
  transition: background 0.12s;
  cursor: pointer;
  border: none !important;
  border-bottom: var(--line-thin) solid var(--border) !important;
  border-left: 3px solid transparent !important;
  letter-spacing: 0.01em;
}
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label:hover {
  background: var(--bg-soft) !important;
}
/* Hide the default radio dot */
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label > div:first-child {
  display: none !important;
}
/* Text element spans the whole row */
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label > div:last-child,
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label > div:last-child > p {
  width: 100% !important;
  margin: 0 !important;
}
/* Selected nav item — lime left rail + bold label */
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label:has(input:checked) {
  background: var(--bg-soft) !important;
  border-left: 3px solid var(--accent) !important;
}
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label:has(input:checked) p,
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label:has(input:checked) div {
  color: var(--text-1) !important;
  font-weight: 700 !important;
}

/* ── Forms — hard-edge bordered frame ── */
[data-testid="stForm"] {
  border: var(--line) solid var(--text-1) !important;
  border-radius: 0 !important;
  padding: 1.5rem !important;
  background: var(--card) !important;
}
/* Forms inside the sidebar stay borderless to save width */
[data-testid="stSidebar"] [data-testid="stForm"] {
  border: none !important;
  padding: 0 !important;
}

/* ── Main content area ── */
.main .block-container {
  padding-top: 2.5rem !important;
  padding-bottom: 5rem !important;
  max-width: 1180px !important;
}

/* ── Headings ── */
h1 { font-size: var(--fs-title) !important; font-weight: 700 !important;
     color: var(--text-1) !important; letter-spacing: -0.03em;
     line-height: 1.1 !important; }
h2 { font-size: 1.375rem !important; font-weight: 700 !important;
     color: var(--text-1) !important; letter-spacing: -0.02em; }
h3 { font-size: 1.05rem !important; font-weight: 600 !important;
     color: var(--text-1) !important; letter-spacing: -0.01em; }

/* ── Buttons — hard-edge square ── */
.stButton > button {
  border-radius: 0 !important;
  font-size: 0.875rem !important;
  font-weight: 500 !important;
  padding: 0.6rem 1.1rem !important;
  border: var(--line) solid var(--text-1) !important;
  background: var(--card) !important;
  color: var(--text-1) !important;
  box-shadow: none !important;
  transition: background 0.12s, color 0.12s !important;
  letter-spacing: 0.005em;
}
.stButton > button:hover {
  background: var(--text-1) !important;
  color: var(--text-on-dark) !important;
  transform: none;
  box-shadow: none !important;
}
/* Primary / type=primary button — solid black square */
.stButton > button[kind="primary"] {
  background: var(--text-1) !important;
  border-color: var(--text-1) !important;
  color: var(--text-on-dark) !important;
  box-shadow: none !important;
  padding: 0.65rem 1.3rem !important;
}
.stButton > button[kind="primary"]:hover {
  background: var(--accent) !important;
  border-color: var(--text-1) !important;
  color: var(--text-1) !important;
}
/* Form submit buttons inherit primary treatment */
[data-testid="stFormSubmitButton"] > button {
  background: var(--text-1) !important;
  border: var(--line) solid var(--text-1) !important;
  color: var(--text-on-dark) !important;
  border-radius: 0 !important;
  padding: 0.65rem 1.3rem !important;
  font-weight: 500 !important;
}
[data-testid="stFormSubmitButton"] > button:hover {
  background: var(--accent) !important;
  border-color: var(--text-1) !important;
  color: var(--text-1) !important;
}

/* ── Inputs, textareas, selects — hard-edge ── */
.stTextInput > div > div > input,
.stTextArea > div > div > textarea,
.stSelectbox > div > div > div,
.stNumberInput > div > div > input,
.stDateInput > div > div > input {
  border-radius: 0 !important;
  border: var(--line) solid var(--border-mid) !important;
  background: var(--card) !important;
  font-size: 0.9rem !important;
  color: var(--text-1) !important;
  box-shadow: none !important;
  transition: border-color 0.15s !important;
}
.stTextInput > div > div > input:focus,
.stTextArea > div > div > textarea:focus,
.stNumberInput > div > div > input:focus {
  border-color: var(--text-1) !important;
  box-shadow: 0 0 0 3px rgba(198,247,92,.50) !important;
  outline: none !important;
}

/* ── Sliders ── */
[data-testid="stSlider"] .stSlider div[role="slider"] {
  background: var(--text-1) !important;
  border-radius: 0 !important;
}
[data-testid="stSlider"] [data-baseweb="slider"] > div > div > div {
  background: var(--accent) !important;
}

/* ── Expanders — hard-edge card ── */
.stExpander {
  border: var(--line) solid var(--border-strong) !important;
  border-radius: 0 !important;
  background: var(--card) !important;
  box-shadow: none !important;
  margin-bottom: 0 !important;
  margin-top: -1.5px !important;  /* collapse shared borders */
  overflow: hidden;
  transition: none;
}
.stExpander:hover {
  box-shadow: none !important;
  border-color: var(--border-strong) !important;
}
[data-testid="stExpander"] > details > summary {
  padding: 18px 22px !important;
  background: var(--card) !important;
  font-size: 0.95rem !important;
  font-weight: 500 !important;
  color: var(--text-1) !important;
  border-radius: 0 !important;
  letter-spacing: -0.005em;
}
[data-testid="stExpander"] > details[open] > summary {
  border-bottom: var(--line-thin) solid var(--border-strong) !important;
  border-radius: 0 !important;
  background: var(--card) !important;
}
[data-testid="stExpander"] > details > div {
  padding: 22px !important;
  background: var(--card) !important;
}

/* ── Dark copy card (approved status) ── */
.card-status-marker { display: none; }
.element-container:has(.card-status-marker) { height: 0; margin: 0 !important; padding: 0 !important; }
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] {
  background: var(--card-inverse) !important;
  border-color: var(--card-inverse) !important;
}
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] summary,
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] summary * {
  background: var(--card-inverse) !important;
  color: var(--text-on-dark) !important;
}
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] > details[open] > summary {
  border-bottom-color: #2A2A2A !important;
}
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] > details > div {
  background: var(--card-inverse) !important;
  color: var(--text-on-dark) !important;
}
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] .copy-title,
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] .copy-body,
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] p,
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] h1,
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] h2,
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] h3,
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] label,
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] span {
  color: var(--text-on-dark) !important;
}
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] .copy-body {
  border-left-color: var(--accent) !important;
}
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] .stButton > button {
  border-color: #FFFFFF !important;
  color: var(--text-on-dark) !important;
  background: transparent !important;
}
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] .stButton > button:hover {
  background: var(--accent) !important;
  color: var(--text-1) !important;
  border-color: var(--accent) !important;
}

/* ── Tabs — hard-edge ── */
.stTabs [data-baseweb="tab-list"] {
  gap: 0 !important;
  border-bottom: var(--line) solid var(--border-strong) !important;
}
.stTabs [data-testid="stTab"] {
  border-radius: 0 !important;
  font-size: 0.82rem !important;
  font-weight: 600 !important;
  color: var(--text-3) !important;
  padding: 12px 18px !important;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  border-right: var(--line-thin) solid var(--border) !important;
}
.stTabs [aria-selected="true"] {
  color: var(--text-1) !important;
  font-weight: 700 !important;
  border-bottom: 3px solid var(--accent) !important;
  background: var(--bg-soft) !important;
}

/* ── Info / warning / error / success boxes ── */
[data-testid="stAlert"] {
  border-radius: 0 !important;
  border: var(--line) solid var(--border-strong) !important;
  font-size: 0.875rem !important;
}

/* ── Dividers — use thin lines, large rhythm ── */
hr {
  border: none !important;
  border-top: var(--line-thin) solid var(--border) !important;
  margin: 1.5rem 0 !important;
}

/* ── Metrics — numbers over a black underline ── */
[data-testid="stMetric"] {
  background: transparent !important;
  border: none !important;
  border-bottom: var(--line-heavy) solid var(--text-1) !important;
  border-radius: 0 !important;
  padding: 14px 0 10px !important;
  box-shadow: none !important;
  transition: none;
}
[data-testid="stMetric"]:hover {
  box-shadow: none !important;
}
[data-testid="stMetricLabel"] {
  font-size: var(--fs-tag) !important;
  font-weight: 600 !important;
  text-transform: uppercase !important;
  letter-spacing: 0.12em !important;
  color: var(--text-3) !important;
}
[data-testid="stMetricValue"] {
  font-size: 2.25rem !important;
  font-weight: 700 !important;
  color: var(--text-1) !important;
  line-height: 1 !important;
  letter-spacing: -0.03em;
  margin-top: 4px;
}

/* ── Checkboxes & radios ── */
.stCheckbox label, .stRadio label {
  font-size: 0.875rem !important;
}

/* ── Progress bar — lime fill, hard edges ── */
.stProgress > div > div > div {
  background: var(--accent) !important;
  border-radius: 0 !important;
}
.stProgress > div > div {
  border-radius: 0 !important;
  background: var(--border) !important;
  height: 4px !important;
}

/* ── Multiselect — hard-edge lime tag ── */
.stMultiSelect > div > div {
  border-radius: 0 !important;
  border: var(--line) solid var(--border-mid) !important;
  background: var(--card) !important;
  font-size: 0.875rem !important;
}
.stMultiSelect span[data-baseweb="tag"] {
  background: var(--accent) !important;
  border: var(--line-thin) solid var(--text-1) !important;
  border-radius: 0 !important;
  font-size: 0.75rem !important;
  font-weight: 600 !important;
  color: var(--text-1) !important;
  letter-spacing: 0.01em;
}

/* ── Spinner ── */
.stSpinner > div {
  border-top-color: var(--text-1) !important;
}

/* ── Download button — hard-edge ── */
.stDownloadButton > button {
  border-radius: 0 !important;
  border: var(--line) solid var(--text-1) !important;
  background: var(--card) !important;
  font-size: 0.875rem !important;
  font-weight: 500 !important;
  color: var(--text-1) !important;
  padding: 0.6rem 1.1rem !important;
}
.stDownloadButton > button:hover {
  background: var(--text-1) !important;
  color: var(--text-on-dark) !important;
}

/* ════════════════════════════════════════════
   Studio-grade custom components
   ════════════════════════════════════════════ */

/* ── Hero page header (chapter + title + deco) ── */
.hero {
  display: grid;
  grid-template-columns: 1fr auto;
  align-items: start;
  gap: 2rem;
  margin-bottom: 2.5rem;
  padding-bottom: 2rem;
  border-bottom: var(--line-heavy) solid var(--text-1);
}
.hero-inner { min-width: 0; }
.hero-tag {
  font-family: var(--font-mono);
  font-size: var(--fs-tag);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.14em;
  color: var(--text-1);
  margin-bottom: 1.25rem;
  display: inline-flex; align-items: baseline; gap: 0.4em;
}
.hero-tag .hero-tag-num {
  color: var(--text-1);
  background: var(--accent);
  border: var(--line-thin) solid var(--text-1);
  padding: 3px 8px;
  line-height: 1;
}
.hero-tag .hero-tag-name {
  color: var(--text-1);
}
.hero-title {
  font-size: var(--fs-hero) !important;
  font-weight: 700 !important;
  letter-spacing: -0.04em !important;
  line-height: 0.98 !important;
  margin: 0 !important;
  color: var(--text-1);
}
.hero-sub {
  margin-top: 1rem;
  font-size: 0.95rem;
  color: var(--text-2);
  max-width: 48ch;
  line-height: 1.5;
}
.hero-deco {
  font-size: 4.5rem;
  line-height: 1;
  color: var(--text-1);
  font-weight: 700;
  align-self: start;
  padding-top: 0.5rem;
  user-select: none;
}

/* ── Section tag (▸ NUM / NAME) ── */
.section-tag {
  font-family: var(--font-mono);
  font-size: var(--fs-tag);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.14em;
  color: var(--text-1);
  margin: 0 0 0.75rem 0;
  display: inline-flex; align-items: center; gap: 0.5em;
}
.section-tag::before {
  content: "▸";
  color: var(--accent-2);
  font-size: 1em;
}
.section-divider {
  border: none !important;
  border-top: var(--line-thin) solid var(--text-1) !important;
  margin: var(--sp-section) 0 2rem !important;
  opacity: 1 !important;
}

/* ── Stat row (row of underlined numbers) ── */
.stat-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 0;
  margin: 0 0 2rem 0;
  border-top: var(--line-thin) solid var(--text-1);
  border-bottom: var(--line-heavy) solid var(--text-1);
}
.stat-badge {
  display: flex; flex-direction: column; align-items: flex-start; gap: 6px;
  background: transparent;
  border: none;
  border-right: var(--line-thin) solid var(--border);
  border-radius: 0;
  padding: 14px 18px;
  min-width: 0;
  transition: background 0.12s;
}
.stat-badge:last-child { border-right: none; }
.stat-badge:hover {
  background: var(--bg-soft);
  border-color: var(--border);
  transform: none;
  box-shadow: none;
}
.stat-badge .sb-num {
  font-size: 2rem; font-weight: 700; color: var(--text-1); line-height: 1;
  letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums;
}
.stat-badge .sb-lbl {
  font-family: var(--font-mono);
  font-size: var(--fs-tag); font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.12em;
  color: var(--text-3);
}
.stat-badge.green  .sb-num { border-bottom: 2px solid var(--green); padding-bottom: 2px; }
.stat-badge.amber  .sb-num { border-bottom: 2px solid var(--amber); padding-bottom: 2px; }
.stat-badge.slate  .sb-num { border-bottom: 2px solid var(--text-3); padding-bottom: 2px; }
.stat-badge.accent .sb-num { border-bottom: 2px solid var(--accent-2); padding-bottom: 2px; }

/* ── Stat-filter button row (review page) ─────────────────────
   Four clickable KPI cards. Each shows a mono label on top and
   a big number below. Cards are colour-coded via a 3px top rule
   (slate / amber / green / accent). Active = solid black with
   lime rule. Scoped via :has() + adjacent sibling so it doesn't
   touch other buttons on the page. */
.element-container:has(.review-stat-filters) + [data-testid="stHorizontalBlock"] .stButton > button {
  height: auto !important;
  min-height: 88px !important;
  padding: 14px 16px 16px !important;
  text-align: left !important;
  border: var(--line) solid var(--border-mid) !important;
  background: var(--card) !important;
  color: var(--text-1) !important;
  border-radius: 0 !important;
  border-top: 3px solid var(--text-3) !important;
  transition: background 0.12s, border-color 0.12s;
  display: flex !important;
  flex-direction: column !important;
  align-items: flex-start !important;
  justify-content: space-between !important;
  white-space: normal !important;
  gap: 8px;
}
.element-container:has(.review-stat-filters) + [data-testid="stHorizontalBlock"] .stButton > button:hover {
  background: var(--bg-soft) !important;
  border-color: var(--border-mid) !important;
  color: var(--text-1) !important;
}
/* Per-column top-rule colour: slate / amber / green / lime */
.element-container:has(.review-stat-filters) + [data-testid="stHorizontalBlock"] > div:nth-child(1) .stButton > button { border-top-color: var(--text-3) !important; }
.element-container:has(.review-stat-filters) + [data-testid="stHorizontalBlock"] > div:nth-child(2) .stButton > button { border-top-color: var(--amber)  !important; }
.element-container:has(.review-stat-filters) + [data-testid="stHorizontalBlock"] > div:nth-child(3) .stButton > button { border-top-color: var(--green)  !important; }
.element-container:has(.review-stat-filters) + [data-testid="stHorizontalBlock"] > div:nth-child(4) .stButton > button { border-top-color: var(--accent-2) !important; }
/* Active filter — solid black card with lime top rule */
.element-container:has(.review-stat-filters) + [data-testid="stHorizontalBlock"] .stButton > button[kind="primary"] {
  background: var(--text-1) !important;
  color: var(--text-on-dark) !important;
  border-color: var(--text-1) !important;
  border-top: 3px solid var(--accent) !important;
}
.element-container:has(.review-stat-filters) + [data-testid="stHorizontalBlock"] .stButton > button[kind="primary"]:hover {
  background: var(--text-1) !important;
  color: var(--text-on-dark) !important;
}
/* First paragraph = the label (small, monospace, uppercase) */
.element-container:has(.review-stat-filters) + [data-testid="stHorizontalBlock"] .stButton > button p:first-child {
  font-family: var(--font-mono) !important;
  font-size: var(--fs-tag) !important;
  font-weight: 600 !important;
  letter-spacing: 0.14em !important;
  text-transform: uppercase !important;
  color: var(--text-3) !important;
  margin: 0 !important;
  line-height: 1.2 !important;
}
.element-container:has(.review-stat-filters) + [data-testid="stHorizontalBlock"] .stButton > button[kind="primary"] p:first-child {
  color: var(--accent) !important;
}
/* Second paragraph = the number (big) */
.element-container:has(.review-stat-filters) + [data-testid="stHorizontalBlock"] .stButton > button p:last-child {
  font-size: 2.15rem !important;
  font-weight: 700 !important;
  letter-spacing: -0.03em !important;
  line-height: 1 !important;
  margin: 0 !important;
  font-variant-numeric: tabular-nums;
  align-self: flex-end;
}

/* ── Status tag (square, monospace) ── */
.status-pill {
  display: inline-flex; align-items: center; gap: 5px;
  font-family: var(--font-mono);
  border-radius: 0;
  font-size: var(--fs-tag); font-weight: 600;
  padding: 3px 10px; line-height: 1.5;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.status-pill.approved {
  background: var(--accent); color: var(--text-1);
  border: var(--line-thin) solid var(--text-1);
}
.status-pill.revision {
  background: var(--amber-soft); color: var(--amber);
  border: var(--line-thin) solid var(--amber);
}
.status-pill.pending {
  background: var(--bg-soft); color: var(--text-2);
  border: var(--line-thin) solid var(--border-mid);
}

/* ── Copy content ── */
.copy-title {
  font-size: var(--fs-card); font-weight: 700;
  color: var(--text-1); line-height: 1.3;
  margin-bottom: 6px;
  letter-spacing: -0.02em;
}
.copy-meta {
  font-family: var(--font-mono);
  font-size: var(--fs-meta); color: var(--text-3);
  margin-bottom: 14px;
  display: flex; align-items: center; gap: 10px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.copy-meta .len-ok  { color: var(--green); font-weight: 600; }
.copy-meta .len-bad { color: var(--amber); font-weight: 600; }
.copy-body {
  font-size: 0.94rem; line-height: 1.75;
  color: var(--text-1); white-space: pre-wrap;
  border-left: 2px solid var(--text-1);
  padding-left: 18px; margin: 14px 0;
}

/* ── Keyword tags — square lime chips ── */
.tag {
  display: inline-flex; align-items: center;
  background: var(--accent-soft);
  border: var(--line-thin) solid var(--text-1);
  border-radius: 0;
  padding: 3px 10px;
  font-size: 0.72rem; font-weight: 600;
  color: var(--text-1);
  margin-right: 6px; margin-bottom: 5px;
  letter-spacing: 0.01em;
  transition: background 0.12s;
}
.tag:hover { background: var(--accent); }

/* ── Engine badge — monospace, black/accent ── */
.engine-badge {
  display: inline-flex; align-items: center; gap: 4px;
  font-family: var(--font-mono);
  background: var(--text-1); color: var(--text-on-dark);
  border-radius: 0; padding: 3px 8px;
  font-size: var(--fs-tag); font-weight: 600;
  letter-spacing: 0.1em; text-transform: uppercase;
}
.engine-badge.gemini,
.engine-badge.claude { background: var(--text-1); }

/* ── Section label (▸ TEXT — hard-edge studio tag) ── */
.section-label {
  display: inline-flex; align-items: center;
  font-family: var(--font-mono);
  background: transparent;
  color: var(--text-1);
  border: none;
  border-left: 3px solid var(--accent);
  border-radius: 0;
  padding: 2px 0 2px 10px;
  font-size: var(--fs-tag); font-weight: 700;
  letter-spacing: 0.14em;
  margin: 1rem 0 0.75rem;
  text-transform: uppercase;
}

/* ── Memory row — hard-edge list item ── */
.mem-card {
  background: var(--card);
  border: none;
  border-bottom: var(--line-thin) solid var(--border);
  border-radius: 0;
  padding: 14px 4px;
  margin-bottom: 0;
  font-size: 0.9rem;
  color: var(--text-1);
  transition: background 0.12s;
}
.mem-card:hover {
  background: var(--bg-soft);
  border-color: var(--border);
  box-shadow: none;
}

/* ── Sidebar user block ── */
.user-block {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 12px;
  background: var(--bg-soft);
  border-radius: 0;
  border: var(--line-thin) solid var(--border);
  margin-bottom: 10px;
}
.user-avatar {
  width: 32px; height: 32px;
  background: var(--text-1); color: var(--accent);
  border-radius: 0;
  display: flex; align-items: center; justify-content: center;
  font-family: var(--font-mono);
  font-size: 0.85rem; font-weight: 700;
  flex-shrink: 0;
}
.user-email { font-size: 0.78rem; font-weight: 500; color: var(--text-1); word-break: break-all; }
.user-ver   { font-family: var(--font-mono); font-size: 0.68rem; color: var(--text-3); letter-spacing: 0.06em; }

/* ── Brand header in sidebar — studio wordmark ── */
.brand-header {
  display: grid;
  grid-template-columns: auto 1fr;
  align-items: center;
  gap: 10px;
  padding: 4px 0 16px;
  border-bottom: var(--line) solid var(--text-1);
  margin-bottom: 14px;
}
.brand-logo {
  font-size: 1.6rem; line-height: 1;
  color: var(--text-1);
  font-weight: 700;
  width: 32px; height: 32px;
  display: flex; align-items: center; justify-content: center;
  background: var(--text-1); color: var(--accent);
}
.brand-name {
  font-size: 0.95rem; font-weight: 700;
  color: var(--text-1); line-height: 1.2;
  letter-spacing: -0.02em;
  text-transform: uppercase;
}
.brand-sub {
  font-family: var(--font-mono);
  font-size: 0.65rem; color: var(--text-3);
  letter-spacing: 0.12em;
  text-transform: uppercase;
}

/* ── Chapter list in sidebar (nav header) ── */
.nav-heading {
  font-family: var(--font-mono);
  font-size: var(--fs-tag);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.14em;
  color: var(--text-3);
  margin: 1.5rem 0 0.5rem;
  padding-left: 4px;
}

/* ── CTA square (36px arrow button) ── */
.cta-square {
  display: inline-flex; align-items: center; justify-content: center;
  width: 36px; height: 36px;
  background: var(--text-1); color: var(--text-on-dark);
  border: var(--line) solid var(--text-1);
  border-radius: 0;
  font-size: 1.05rem;
  line-height: 1;
  transition: background 0.12s, color 0.12s;
  user-select: none;
}
.cta-square:hover {
  background: var(--accent);
  color: var(--text-1);
}

/* ── Login — studio two-column landing ── */
.login-wrap {
  display: grid;
  grid-template-columns: 1.1fr 1fr;
  gap: 4rem;
  align-items: center;
  min-height: 70vh;
  padding: 2rem 0;
}
@media (max-width: 860px) {
  .login-wrap { grid-template-columns: 1fr; gap: 2rem; }
}
.login-hero {
  text-align: left;
  padding: 0;
}
.login-hero .lh-mark {
  display: inline-flex;
  align-items: center; justify-content: center;
  width: 44px; height: 44px;
  background: var(--text-1); color: var(--accent);
  font-size: 1.6rem; font-weight: 700;
  margin-bottom: 1.75rem;
}
.login-hero .lh-badge {
  display: inline-block;
  font-family: var(--font-mono);
  background: var(--accent);
  color: var(--text-1);
  border: var(--line-thin) solid var(--text-1);
  border-radius: 0;
  padding: 3px 10px;
  font-size: var(--fs-tag); font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  margin-bottom: 1.25rem;
}
.login-hero .lh-title {
  font-size: 3rem; font-weight: 700;
  color: var(--text-1); letter-spacing: -0.04em;
  line-height: 0.98; margin: 0 0 0.75rem 0;
}
.login-hero .lh-sub {
  color: var(--text-2); font-size: 0.95rem;
  line-height: 1.5;
  max-width: 40ch;
}
.login-hero .lh-meta {
  margin-top: 2rem;
  display: flex; flex-wrap: wrap; gap: 1.5rem;
  padding-top: 1rem;
  border-top: var(--line-thin) solid var(--text-1);
  font-family: var(--font-mono);
  font-size: 0.68rem; color: var(--text-3);
  letter-spacing: 0.14em;
  text-transform: uppercase;
}
.login-hero .lh-meta b {
  color: var(--text-1);
  font-weight: 700;
  margin-right: 4px;
}
.login-frame {
  padding: 2rem;
  border: var(--line) solid var(--text-1);
  background: var(--card);
}
.login-frame-head {
  font-family: var(--font-mono);
  font-size: var(--fs-tag);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.14em;
  color: var(--text-1);
  margin-bottom: 1.25rem;
  padding-bottom: 0.75rem;
  border-bottom: var(--line-thin) solid var(--text-1);
}
</style>
""",
    unsafe_allow_html=True,
)


# ── Authentication gate ────────────────────────────────────────────────────
db_client, current_user = auth.require_auth()
user_id: str = current_user["id"]


# ── Sidebar ────────────────────────────────────────────────────────────────
with st.sidebar:
    # Brand header — studio wordmark
    st.markdown(
        "<div class='brand-header'>"
        "<div class='brand-logo'>✦</div>"
        "<div><div class='brand-name'>AutoWriter</div>"
        f"<div class='brand-sub'>XHS · v{config.APP_VERSION}</div></div>"
        "</div>",
        unsafe_allow_html=True,
    )

    # User block
    safe_email = _html.escape(current_user['email'])
    avatar_char = _html.escape(current_user['email'][0].upper())
    st.markdown(
        f"<div class='user-block'>"
        f"<div class='user-avatar'>{avatar_char}</div>"
        f"<div><div class='user-email'>{safe_email}</div><div class='user-ver'>SIGNED IN</div></div>"
        f"</div>",
        unsafe_allow_html=True,
    )
    if st.button("退出登录", use_container_width=True):
        auth.sign_out()
        st.rerun()

def _render_error_panel(err: Exception) -> None:
    """Surface a real error message + remediation hints instead of letting
    Streamlit show its redacted red box.  Used by the auth-step and router-
    step error boundaries below."""
    import traceback as _traceback
    import postgrest.exceptions as _pg_exc

    st.error("⚠️ 页面渲染时发生错误。原始信息：")
    err_repr = repr(err)
    err_msg = str(err)
    is_pg = isinstance(err, _pg_exc.APIError)
    pg_detail: dict = {}
    if is_pg:
        try:
            arg = err.args[0] if err.args else {}
            if isinstance(arg, dict):
                pg_detail = arg
        except Exception:
            pass
        st.code(
            "PostgREST APIError\n"
            f"message: {pg_detail.get('message', err_msg)}\n"
            f"code:    {pg_detail.get('code', '')}\n"
            f"hint:    {pg_detail.get('hint', '')}\n"
            f"details: {pg_detail.get('details', '')}",
            language="text",
        )
    else:
        st.code(err_repr, language="text")

    with st.expander("🔍 完整 traceback（截图发给开发者最有用）", expanded=False):
        st.code(_traceback.format_exc(), language="text")

    st.markdown("**常见排查方向**：")
    if is_pg:
        msg_lower = (str(pg_detail.get("message", "")) + " " + err_msg).lower()
        code_str = str(pg_detail.get("code", "")).strip()
        if "column" in msg_lower and ("does not exist" in msg_lower or "not found" in msg_lower):
            st.markdown(
                "- 看起来数据库少一列。最近版本（2.7.3）需要 `ALTER TABLE items "
                "ADD COLUMN IF NOT EXISTS feedback_draft TEXT;`，去 Supabase SQL Editor 跑一下。"
            )
        if "jwt" in msg_lower or "expired" in msg_lower or "401" in code_str or "401" in msg_lower:
            st.markdown("- token 过期或无效。点击下方「🔄 重置会话」按钮重登一次即可。")
        if "row level security" in msg_lower or "permission" in msg_lower or "rls" in msg_lower or code_str.startswith("42"):
            st.markdown(
                "- RLS 策略阻止了访问。确认登录的账号和数据所属账号一致；"
                "新建项目失败时常见此错。"
            )
        if "could not connect" in msg_lower or "timeout" in msg_lower:
            st.markdown(
                "- 数据库连不上。检查 Supabase 项目是否被自动暂停（免费版闲置一段时间会暂停），"
                "去 Supabase Dashboard 唤醒一下。"
            )
    st.markdown(
        "- 在 Streamlit Cloud 控制台右下角点 **Manage app** → **Logs** "
        "可看到完整 stderr，比这里的截图更详细。"
    )

    if st.button("🔄 重置会话并重登"):
        auth.sign_out()
        st.rerun()
    st.stop()


# Project switcher (also rendered in sidebar via projects module).  Wrapped
# because list_projects can hit RLS / schema / token issues right at app
# entry, which would otherwise be redacted by Streamlit's red box.
try:
    selected_project = proj_module.render_project_switcher(db_client, user_id)
except Exception as _switcher_err:
    _render_error_panel(_switcher_err)

_NAV_ITEMS = {
    "01 · 生成":  "生成工作台",
    "02 · 审核":  "审核与迭代",
    "03 · 导出":  "导出中心",
    "04 · 记忆":  "记忆管理",
    "05 · 设置":  "项目设置",
    "06 · 历史":  "批次历史",
}

with st.sidebar:
    st.markdown("<div class='nav-heading'>▸ CHAPTERS</div>", unsafe_allow_html=True)
    _nav_choice = st.radio(
        "导航",
        list(_NAV_ITEMS.keys()),
        label_visibility="collapsed",
    )
    page = _NAV_ITEMS[_nav_choice]


# ── Global queue banner ────────────────────────────────────────────────────
_queue_banner()

# ── Route to pages ─────────────────────────────────────────────────────────

if selected_project is None and page not in ("项目设置",):
    st.info("👈 请先在左侧创建或选择一个项目，然后开始使用。")
    st.stop()


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: 生成工作台
# ═══════════════════════════════════════════════════════════════════════════

def _hero_header(chapter: str, title: str, subtitle: str = "") -> None:
    """Render a studio-grade page hero.

    chapter  — "01 / GENERATE" (number is split on first '/' to style it)
    title    — big hero sentence
    subtitle — small gray description (optional)
    """
    if " / " in chapter:
        num, name = chapter.split(" / ", 1)
    else:
        num, name = chapter, ""
    tag_html = (
        f"<span class='hero-tag-num'>{_html.escape(num)}</span>"
        + (f"<span class='hero-tag-name'>/ {_html.escape(name)}</span>" if name else "")
    )
    sub_html = f"<div class='hero-sub'>{_html.escape(subtitle)}</div>" if subtitle else ""
    st.markdown(
        f"<section class='hero'>"
        f"<div class='hero-inner'>"
        f"<div class='hero-tag'>{tag_html}</div>"
        f"<h1 class='hero-title'>{_html.escape(title)}</h1>"
        f"{sub_html}"
        f"</div>"
        f"<div class='hero-deco'>✦</div>"
        f"</section>",
        unsafe_allow_html=True,
    )


def _section_divider() -> None:
    """Hard-edge section divider (heavy black horizontal line)."""
    st.markdown("<hr class='section-divider' />", unsafe_allow_html=True)


def _quick_gen_worker(plan: dict, user_id: str, db_client, status: dict) -> None:
    """快速生成的后台 worker（单批次版的 _queue_worker）。

    现在也产出 telemetry.BatchMetrics 一致的指标日志（setup / llm /
    db_save / embedding 四个阶段 + dedup/regen 计数器），方便和队列模式
    做对比。
    """
    metrics = telemetry.BatchMetrics(
        project_id=plan.get("project_id", ""),
        mode="quick",
        engines=list(plan.get("engines", [])),
        count=int(plan.get("count", 0)),
    )
    metrics.start_phase("setup")
    try:
        status["running"] = True
        status["message"] = "正在构建提示词…"
        _set_phase_progress(status, "setup")

        project_id = plan["project_id"]
        project    = db.get_project(db_client, project_id)
        if not project:
            raise RuntimeError("项目不存在")

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
        full_system_prompt = mem_module.build_system_prompt(
            base_prompt=project.get("system_prompt", ""),
            global_memories=global_mems,
            project_memories=project_mems,
            tactic_suffix=tactic_suffix,
            calibration_notes=calibration_notes,
            positive_examples=pos_examples or None,
            negative_examples=neg_examples or None,
            session_instructions=session_instr or None,
        )

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
        metrics.stop_phase("setup")

        def _progress(pct: float, msg: str) -> None:
            # 把引擎层 [0,1] 映射到 LLM 阶段在整批的占比，
            # 不会覆盖后续 db_save / embedding 阶段
            _llm_intra_progress(status, pct, msg)

        status["message"] = "正在生成内容…"
        metrics.start_phase("llm")
        _set_phase_progress(status, "llm")
        metrics.incr("llm_calls", len(engines))

        if use_multi_role:
            generation_results = gen_module.generate_batch_multi_role(
                system_prompt=full_system_prompt,
                tactic=tactic,
                count=count,
                engines=engines,
                engine_models=engine_models or None,
                target_audience=plan.get("target_audience", ""),
                key_messages=plan.get("key_messages", ""),
                tone=plan.get("tone", ""),
                extra_instructions=combined_extra,
                images=images,
                progress_callback=_progress,
                historical_titles=historical_titles or None,
                use_thinking=use_thinking,
                gemini_use_thinking=gemini_thinking,
                custom_roles=custom_roles,
                n_roles=n_roles,
            )
        else:
            generation_results = gen_module.generate_batch(
                system_prompt=full_system_prompt,
                tactic=tactic,
                count=count,
                engines=engines,
                target_audience=plan.get("target_audience", ""),
                key_messages=plan.get("key_messages", ""),
                tone=plan.get("tone", ""),
                extra_instructions=combined_extra,
                images=images,
                progress_callback=_progress,
                historical_titles=historical_titles or None,
                use_thinking=use_thinking,
                engine_models=engine_models or None,
                gemini_use_thinking=gemini_thinking,
                user_id=user_id,
                project_id=project_id,
            )

        metrics.stop_phase("llm")

        # ── 批量保存：复用与 _queue_worker 完全相同的 _save_batch_results ──
        # （修 R1：之前是 create_item / create_version 逐条 INSERT，
        #  ~30 round trip；改批量后收敛为 2 次）
        metrics.start_phase("db_save")
        _set_phase_progress(status, "db_save")
        errors: list[str] = []
        inserted_items, inserted_versions, _produced_titles = _save_batch_results(
            db_client, batch_id, user_id, generation_results,
            error_prefix="",  # Quick Generate 不需要 "计划 N（项目）：" 前缀
            errors_sink=errors,
        )
        version_rows = [
            {"title": v.get("title", ""), "ai_engine": v.get("ai_engine", "")}
            for v in inserted_versions
        ]
        saved_count = len(inserted_versions)
        metrics.stop_phase("db_save")

        # ── 语义查重（同 _queue_worker 走 _run_semantic_dedup_pass）─────
        # Quick Generate 只跑一个批次，所以 queue_embeddings 是个只有当前
        # project_id 的临时字典；命中重复直接写到 errors 数组里。
        _set_phase_progress(status, "embedding")
        quick_queue_embeddings: dict[str, list[dict]] = {}
        regen_ctx = {
            "enabled":             bool(getattr(config, "ENABLE_DEDUP_REGEN", False)),
            "max_retries":         int(getattr(config, "DEDUP_REGEN_MAX_RETRIES", 2)),
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
        _run_semantic_dedup_pass(
            db_client, inserted_versions, version_rows,
            quick_queue_embeddings, project_id,
            error_prefix="", errors_sink=errors, metrics=metrics,
            regen_ctx=regen_ctx,
        )
        _set_phase_progress(status, "embedding", intra=1.0)

        status["saved_count"] = saved_count
        status["n_results"]   = len(generation_results)
        status["errors"]      = errors
        status["message"]     = f"生成完成！{len(generation_results)} 篇，{saved_count} 个版本已保存。"
        metrics.batch_id = batch_id
        metrics.set_meta("saved", saved_count)

    except Exception as exc:
        status.setdefault("errors", []).append(str(exc))
        status["message"] = f"生成失败：{exc}"
        metrics.set_meta("error", str(exc)[:120])

    metrics.close(status)
    status["running"] = False
    status["done"]    = True


def _render_queue_tab() -> None:
    """Render the batch queue builder and executor UI."""
    st.markdown(
        "<div class='section-label'>批次队列</div>"
        "<p style='font-size:0.85rem;color:var(--text-2);margin-top:4px;margin-bottom:16px'>"
        "添加多个生成计划，点击「启动队列」后在后台依次执行，切换页面不影响生成。</p>",
        unsafe_allow_html=True,
    )

    # Load projects for the dropdown
    all_projects = db.list_projects(db_client, user_id)
    if not all_projects:
        st.info("暂无项目，请先在「项目设置」中创建项目。")
        return

    proj_id_to_obj  = {p["id"]: p for p in all_projects}
    proj_id_to_name = {p["id"]: p.get("name", "未命名") for p in all_projects}
    proj_ids        = list(proj_id_to_obj.keys())

    # Session state
    st.session_state.setdefault("gen_queue_plans", [])
    plans: list[dict] = st.session_state["gen_queue_plans"]
    qs = st.session_state.get("queue_state", {})
    is_running = qs.get("running", False)

    # ── Plan list ──────────────────────────────────────────────────────
    plans_to_delete: list[int] = []
    for i, plan in enumerate(plans):
        with st.expander(
            f"计划 {i+1} — {plan.get('project_name', '?')} · "
            f"{plan.get('tactic', '通用') or '通用'} · "
            f"{'/'.join(e.upper() for e in plan.get('engines', ['claude']))} · "
            f"{plan.get('count', 1)} 篇",
            expanded=(i == len(plans) - 1),
        ):
            pc1, pc2 = st.columns(2)
            with pc1:
                sel_pid = st.selectbox(
                    "项目", proj_ids,
                    format_func=lambda pid: proj_id_to_name.get(pid, pid),
                    index=proj_ids.index(plan["project_id"]) if plan["project_id"] in proj_ids else 0,
                    key=f"qp_proj_{i}",
                )
                plan["project_id"]   = sel_pid
                plan["project_name"] = proj_id_to_name.get(sel_pid, "")

                sel_proj = proj_id_to_obj.get(sel_pid, {})
                plan_tactic_names = proj_module.get_tactic_names(sel_proj)
                tactic_opts = ["（无）"] + plan_tactic_names
                t_idx = tactic_opts.index(plan.get("tactic", "（无）")) if plan.get("tactic", "（无）") in tactic_opts else 0
                sel_tactic = st.selectbox("战术方向", tactic_opts, index=t_idx, key=f"qp_tactic_{i}")
                plan["tactic"] = "" if sel_tactic == "（无）" else sel_tactic

            with pc2:
                plan["count"] = st.number_input(
                    "篇数", min_value=1, max_value=config.MAX_GENERATION_COUNT,
                    value=plan.get("count", 3), key=f"qp_count_{i}",
                )
                q_eng_mode = st.radio(
                    "模式", ["单引擎", "多引擎比稿", "🎭 三省法"],
                    horizontal=True, key=f"qp_eng_mode_{i}",
                    index=2 if plan.get("use_multi_role") else (1 if len(plan.get("engines", [])) > 1 else 0),
                )
                plan["use_multi_role"] = (q_eng_mode == "🎭 三省法")
                if q_eng_mode == "单引擎":
                    q_eng = st.selectbox(
                        "引擎", gen_module.AVAILABLE_ENGINES,
                        format_func=lambda e: "Claude" if e == "claude" else "Gemini",
                        key=f"qp_eng_{i}",
                    )
                    plan["engines"] = [q_eng]
                elif q_eng_mode == "多引擎比稿":
                    plan["engines"] = gen_module.AVAILABLE_ENGINES[:2] or ["claude"]
                else:
                    # 三省法：默认 Claude，可选加 Gemini 增加多样性
                    q_mr_eng = st.multiselect(
                        "参与角色起草的引擎",
                        gen_module.AVAILABLE_ENGINES,
                        default=plan.get("engines", ["claude"]),
                        format_func=lambda e: "Claude" if e == "claude" else "Gemini",
                        key=f"qp_mr_eng_{i}",
                    )
                    plan["engines"] = q_mr_eng or ["claude"]
                    plan["n_roles"] = st.slider(
                        "抽取角色数", min_value=2, max_value=6,
                        value=plan.get("n_roles", 3), key=f"qp_nroles_{i}",
                        help="每次从角色池中随机抽取，数量越多并行路数越多",
                    )

            q_em: dict[str, str] = {}
            qm1, qm2 = st.columns(2)
            _saved_em = plan.get("engine_models") or {}
            if "claude" in plan["engines"]:
                _saved_cm = _saved_em.get("claude", config.CLAUDE_MODEL)
                _cm_keys = list(config.CLAUDE_MODELS.keys())
                with qm1:
                    q_em["claude"] = st.selectbox(
                        "Claude 模型", _cm_keys,
                        format_func=lambda m: config.CLAUDE_MODELS.get(m, m),
                        index=_cm_keys.index(_saved_cm) if _saved_cm in _cm_keys else 0,
                        key=f"qp_cm_{i}",
                    )
            if "gemini" in plan["engines"]:
                _saved_gm = _saved_em.get("gemini", config.GEMINI_MODEL)
                _gm_keys = list(config.GEMINI_MODELS.keys())
                with qm2:
                    q_em["gemini"] = st.selectbox(
                        "Gemini 模型", _gm_keys,
                        format_func=lambda m: config.GEMINI_MODELS.get(m, m),
                        index=_gm_keys.index(_saved_gm) if _saved_gm in _gm_keys else 0,
                        key=f"qp_gm_{i}",
                    )
            plan["engine_models"] = q_em

            # Claude thinking is now selected via model name (-thinking suffix);
            # only Gemini keeps a runtime toggle.
            if "gemini" in plan["engines"]:
                plan["gemini_use_thinking"] = st.checkbox(
                    "Gemini 思考模式",
                    value=plan.get("gemini_use_thinking", False),
                    key=f"qp_gthink_{i}",
                    help="thinking_budget=-1 动态分配（Gemini 3.x 默认开启思考）",
                )

            plan["extra_instructions"] = st.text_input(
                "补充说明", value=plan.get("extra_instructions", ""),
                placeholder="可选", key=f"qp_extra_{i}",
            )

            if st.button("🗑 删除此计划", key=f"qp_del_{i}"):
                plans_to_delete.append(i)

    for idx in sorted(plans_to_delete, reverse=True):
        plans.pop(idx)
    if plans_to_delete:
        st.rerun()

    # ── Controls ───────────────────────────────────────────────────────
    btn_col1, btn_col2 = st.columns(2)
    with btn_col1:
        if st.button("➕ 添加计划", use_container_width=True, disabled=is_running):
            default_pid = proj_ids[0]
            plans.append({
                "project_id":          default_pid,
                "project_name":        proj_id_to_name.get(default_pid, ""),
                "tactic":              "",
                "engines":             ["claude"],
                "engine_models":       {"claude": config.CLAUDE_MODEL},
                "count":               3,
                "use_thinking":        False,
                "gemini_use_thinking": False,
                "use_multi_role":      False,
                "n_roles":             3,
                "extra_instructions":  "",
            })
            st.rerun()

    with btn_col2:
        if not is_running:
            if st.button(
                "🚀 启动队列", type="primary", use_container_width=True,
                disabled=(not plans),
            ):
                stop_evt = threading.Event()
                status: dict = {
                    "running": False, "done": False, "total": 0,
                    "current": 0, "message": "准备中…",
                    "completed": [], "errors": [],
                }
                st.session_state["queue_state"]      = status
                st.session_state["queue_stop_event"] = stop_evt
                t = threading.Thread(
                    target=_queue_worker,
                    args=(copy.deepcopy(plans), user_id, db_client, status, stop_evt),
                    daemon=True,
                )
                t.start()
                st.rerun()
        else:
            if st.button("⏹ 停止队列", use_container_width=True):
                evt = st.session_state.get("queue_stop_event")
                if evt:
                    evt.set()
                st.rerun()

    # Live status within tab
    if is_running:
        # The fragment-based _queue_banner already auto-refreshes the
        # progress info every 2 s without re-running the whole page.
        # Keep a static snapshot here for users on the queue tab, but no
        # longer time.sleep + st.rerun — that was the main cause of the
        # 1.5 s "click and wait" lag during batch runs (every interaction
        # was racing the next forced rerun).
        _render_running_snapshot()

    # Completed results summary
    if qs.get("done") and qs.get("completed"):
        st.markdown("<div class='section-label' style='margin-top:16px'>已完成的批次</div>", unsafe_allow_html=True)
        for item in qs["completed"]:
            st.success(
                f"✅ {item['project_name']} · 批次 {item['batch_id'][:8]}… · "
                f"已保存 {item['saved']} 个版本"
            )


def page_generate(project: dict) -> None:
    pname = _html.escape(project.get("name", ""))
    _hero_header("01 / GENERATE", "构思你的下一个爆款。", f"项目 · {pname}")

    project_name = project.get("name", "")
    base_prompt  = project.get("system_prompt", "")
    tactic_names = proj_module.get_tactic_names(project)

    # ── Sidebar: quick-generate controls ──────────────────────────────
    with st.sidebar:
        st.markdown(
            "<div class='nav-heading'>▸ QUICK GEN</div>",
            unsafe_allow_html=True,
        )

        if tactic_names:
            tactic = st.selectbox("战术方向", ["（不使用战术方向）"] + tactic_names)
            if tactic == "（不使用战术方向）":
                tactic = ""
        else:
            st.caption("未配置战术方向，将不应用战术方向。")
            tactic = ""
        count = st.slider("生成数量", 1, config.MAX_GENERATION_COUNT, config.DEFAULT_GENERATION_COUNT)

        use_multi_role = st.toggle(
            "🎭 多角色起草（三省法）",
            help="从角色池中随机抽取 N 个视角并行起草，AI 自动评选最优版并附评审意见。"
                 "质量下限更高，延迟与单次生成基本相同。与多引擎比稿互斥。",
        )

        engine_models: dict[str, str] = {}

        if use_multi_role:
            _proj_custom_roles = proj_module._parse_json_field(project.get("custom_roles"), [])
            _role_pool_size = len(_proj_custom_roles) if _proj_custom_roles else len(gen_module.CREATIVE_ROLES_POOL)
            n_roles = st.slider(
                "抽取角色数", min_value=2, max_value=min(_role_pool_size, 6), value=3,
                help=f"每次从角色池（{_role_pool_size} 个）中随机抽取，引入不可预测性。角色池可在「项目设置 → 角色池」中配置。",
            )
            # Multi-role mode: choose which engines participate
            engines = st.multiselect(
                "参与角色起草的引擎",
                gen_module.AVAILABLE_ENGINES,
                default=["claude"],
                format_func=lambda e: "Claude" if e == "claude" else "Gemini",
                help=f"选两个引擎：{n_roles}角色×2引擎={n_roles*2}路并行，差异性最大",
            ) or ["claude"]
            if "claude" in engines:
                engine_models["claude"] = st.selectbox(
                    "Claude 模型",
                    list(config.CLAUDE_MODELS.keys()),
                    index=list(config.CLAUDE_MODELS.keys()).index(config.CLAUDE_MODEL)
                          if config.CLAUDE_MODEL in config.CLAUDE_MODELS else 0,
                    format_func=lambda m: config.CLAUDE_MODELS.get(m, m),
                )
            if "gemini" in engines:
                engine_models["gemini"] = st.selectbox(
                    "Gemini 模型",
                    list(config.GEMINI_MODELS.keys()),
                    index=list(config.GEMINI_MODELS.keys()).index(config.GEMINI_MODEL)
                          if config.GEMINI_MODEL in config.GEMINI_MODELS else 0,
                    format_func=lambda m: config.GEMINI_MODELS.get(m, m),
                )
            use_thinking = False
            gemini_use_thinking = False
            if "gemini" in engines:
                gemini_use_thinking = st.checkbox(
                    "Gemini 思考模式",
                    help="thinking_budget=-1 动态分配；对 2.5 Pro 效果明显。",
                )
        else:
            # Standard mode: single or multi-engine
            engine_mode = st.radio(
                "AI 引擎模式",
                ["单引擎", "多引擎比稿"],
                help="多引擎比稿会同时用 Claude 和 Gemini 生成，便于对比。",
            )
            if engine_mode == "单引擎":
                engines = [st.selectbox(
                    "引擎", gen_module.AVAILABLE_ENGINES,
                    format_func=lambda e: "Claude" if e == "claude" else "Gemini",
                )]
            else:
                engines = gen_module.AVAILABLE_ENGINES[:2]
                if len(engines) < 2:
                    st.warning("Gemini 未配置，将仅使用 Claude。")
                    engines = ["claude"]

            if "claude" in engines:
                engine_models["claude"] = st.selectbox(
                    "Claude 模型", list(config.CLAUDE_MODELS.keys()),
                    index=list(config.CLAUDE_MODELS.keys()).index(config.CLAUDE_MODEL)
                          if config.CLAUDE_MODEL in config.CLAUDE_MODELS else 0,
                    format_func=lambda m: config.CLAUDE_MODELS.get(m, m),
                )
            if "gemini" in engines:
                engine_models["gemini"] = st.selectbox(
                    "Gemini 模型", list(config.GEMINI_MODELS.keys()),
                    index=list(config.GEMINI_MODELS.keys()).index(config.GEMINI_MODEL)
                          if config.GEMINI_MODEL in config.GEMINI_MODELS else 0,
                    format_func=lambda m: config.GEMINI_MODELS.get(m, m),
                )

            use_thinking = False
            gemini_use_thinking = False
            if "gemini" in engines:
                gemini_use_thinking = st.checkbox(
                    "Gemini：思考模式",
                    help="thinking_budget=-1 动态分配；对 2.5 Pro 效果明显。",
                )

        with st.expander("⚙️ 高级参数"):
            target_audience   = st.text_input("目标人群", placeholder="例：25-35岁职场女性")
            key_messages      = st.text_input("核心卖点/关键词", placeholder="例：低度数、清爽、派对感")
            tone              = st.text_input("语气偏好", placeholder="例：活泼口语化、朋友间分享")
            extra_instructions = st.text_area("补充说明", height=80, placeholder="其他要求...")

    # ── Main area: tabs ────────────────────────────────────────────────
    tab_quick, tab_queue = st.tabs(["✍️  快速生成", "📋  批次队列"])

    # ── TAB 1: Quick generate ──────────────────────────────────────────
    with tab_quick:
        st.markdown("<div class='section-label' style='margin-bottom:6px'>参考图片（可选）</div>", unsafe_allow_html=True)
        encoded_images = image_handler.render_image_uploader()
        image_prompt = ""
        if encoded_images:
            image_prompt = st.text_area(
                "图片提示词",
                placeholder="说明图片内容或用途。例：这是品牌产品实拍图，请参考图片视觉风格和产品细节进行创作。",
                height=80,
                key="image_prompt_input",
                help="上传图片后填写，可显著提升 AI 对图片的利用率。",
            )

        global_mems, project_mems = db.get_confirmed_memories(
            db_client, user_id, project_id=project["id"]
        )
        pos_examples = db.list_example_items(db_client, project["id"], "positive", limit=5)
        neg_examples = db.list_example_items(db_client, project["id"], "negative", limit=3)
        calibration_notes = project.get("calibration_notes") or ""

        context_parts = []
        mem_count = len(global_mems) + len(project_mems)
        if mem_count > 0:
            context_parts.append(f"🧠 {mem_count} 条记忆")
        if calibration_notes.strip():
            context_parts.append("📝 调校笔记")
        if pos_examples:
            context_parts.append(f"⭐ {len(pos_examples)} 个正案例")
        if neg_examples:
            context_parts.append(f"👎 {len(neg_examples)} 个反案例")
        if context_parts:
            st.info("已载入上下文：" + " · ".join(context_parts))

        qgs        = st.session_state.get("quick_gen_state")
        qg_running = bool(qgs and qgs.get("running"))
        qg_done    = bool(qgs and qgs.get("done"))

        if qg_running:
            # Fragment-driven polling replaces the old sleep+rerun loop;
            # the quick-generate path now refreshes the progress bar
            # without forcing the whole script to re-execute.
            _render_quick_gen_snapshot()
        elif qg_done:
            errors_list = qgs.get("errors", [])
            if errors_list:
                st.error("部分内容生成失败：\n" + "\n".join(
                    f"• {e}" for e in list(dict.fromkeys(errors_list))
                ))
            saved = qgs.get("saved_count", 0)
            n_res = qgs.get("n_results", 0)
            if saved > 0:
                st.success(f"✅ 生成完成！共 {n_res} 篇，{saved} 个版本。")
            elif not errors_list:
                st.warning("生成完成，但没有内容被保存，请检查配置。")
            bid = qgs.get("batch_id")
            if bid:
                st.session_state["review_batch_id"] = bid
                st.info("👉 前往「审核与迭代」页面查看结果。")
            if st.button("🔄 再次生成", use_container_width=True):
                st.session_state.pop("quick_gen_state", None)
                st.rerun()
        else:
            if st.button("🚀 开始生成", type="primary", use_container_width=True,
                         disabled=not base_prompt.strip()):
                if not base_prompt.strip():
                    st.warning("⚠️ 当前项目尚未配置 System Prompt，请先在「项目设置」中填写。")
                    st.stop()

                qg_plan = {
                    "project_id":       project["id"],
                    "tactic":           tactic,
                    "engines":          engines,
                    "engine_models":    engine_models,
                    "count":            count,
                    "use_thinking":     use_thinking,
                    "gemini_use_thinking": gemini_use_thinking,
                    "use_multi_role":   use_multi_role,
                    "n_roles":          n_roles if use_multi_role else 3,
                    "target_audience":  target_audience,
                    "key_messages":     key_messages,
                    "tone":             tone,
                    "extra_instructions": extra_instructions,
                    "image_prompt":     image_prompt,
                    "images":           encoded_images or [],
                }
                qg_status: dict = {
                    "running": False, "done": False,
                    "message": "准备中…", "progress": 0.0,
                    "batch_id": None, "saved_count": 0,
                    "n_results": 0, "errors": [],
                }
                st.session_state["quick_gen_state"] = qg_status
                threading.Thread(
                    target=_quick_gen_worker,
                    args=(qg_plan, user_id, db_client, qg_status),
                    daemon=True,
                ).start()
                st.rerun()

    # ── TAB 2: Batch Queue ─────────────────────────────────────────────
    with tab_queue:
        _render_queue_tab()


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: 审核与迭代
# ═══════════════════════════════════════════════════════════════════════════

QUICK_FEEDBACK_TAGS = [
    "语气再软一点", "更口语化", "调皮一点", "更专业",
    "加个生活场景", "突出产品卖点", "增加情感共鸣", "换个角度写",
    "开头换个钩子", "标题太长了", "加个反转结构", "结尾加CTA",
    "太硬广，软一下", "注意平台敏感词", "别提竞品", "检查蓝字覆盖",
]


def page_review(project: dict) -> None:
    pname = _html.escape(project.get("name", ""))
    _hero_header("02 / REVIEW", "打磨每一篇成稿。", f"项目 · {pname}")

    # Batch selector
    batches = db.list_batches(db_client, project["id"])
    if not batches:
        st.info("暂无批次，请先在「生成工作台」生成内容。")
        return

    project_name = project.get("name", "")
    # Use batch ID suffix to ensure uniqueness in case of same tactic+time
    batch_options = {
        f"{_format_batch_label(b, project_name)} ({b['id'][:6]})": b["id"]
        for b in batches
    }
    default_key = None
    stored_bid = st.session_state.get("review_batch_id")
    for k, v in batch_options.items():
        if v == stored_bid:
            default_key = k
            break

    selected_batch_label = st.selectbox(
        "选择批次",
        list(batch_options.keys()),
        index=list(batch_options.keys()).index(default_key) if default_key else 0,
    )
    batch_id = batch_options[selected_batch_label]
    selected_batch = next(b for b in batches if b["id"] == batch_id)

    # Load items
    items = db.list_items(db_client, batch_id)
    if not items:
        st.info("该批次暂无文案。")
        return

    pending  = sum(1 for it in items if it["status"] == "pending")
    approved = sum(1 for it in items if it["status"] == "approved")
    revision = sum(1 for it in items if it["status"] == "needs_revision")

    # Stat-filter row — 4 clickable cards, the active one acts as
    # the current filter. Replaces the old stat-row + radio pair.
    current_filter = st.session_state.setdefault("review_filter", "all")

    # Marker used by CSS (:has() adjacent selector) to scope the
    # big-number button treatment to this one block only.
    st.markdown(
        "<div class='review-stat-filters' style='display:none'></div>",
        unsafe_allow_html=True,
    )
    _stat_defs = [
        ("all",            "总计",   len(items)),
        ("pending",        "待审核", pending),
        ("approved",       "已通过", approved),
        ("needs_revision", "待修改", revision),
    ]
    _cols = st.columns(4, gap="small")
    # Label on top (small, monospace, uppercase), number below (big)
    for _col, (_key, _label, _num) in zip(_cols, _stat_defs):
        with _col:
            if st.button(
                f"{_label}\n\n{_num}",
                key=f"rf_{_key}",
                use_container_width=True,
                type="primary" if current_filter == _key else "secondary",
            ):
                st.session_state["review_filter"] = _key
                st.rerun()

    if current_filter == "all":
        filtered_items = items
    else:
        filtered_items = [it for it in items if it["status"] == current_filter]

    st.divider()

    if not filtered_items:
        st.info("当前筛选条件下没有文案。")

    # ── Item cards ─────────────────────────────────────────────────────
    for item in filtered_items:
        versions = sorted(item.get("versions", []), key=lambda v: v.get("version_num", 0))
        if not versions:
            continue
        _render_item_card(item, versions, selected_batch, project)

    # ── 太子自动学习：全部通过时静默更新调教笔记 ──────────────────────────
    _taizi_key = f"taizi_{batch_id}"
    if (items and all(it["status"] == "approved" for it in items)
            and not st.session_state.get(_taizi_key)):
        st.session_state[_taizi_key] = True
        _auto_update_calibration_notes(project, batch_id, items)

    # ── Quick batch actions ────────────────────────────────────────────
    if pending > 0 or revision > 0:
        st.divider()
        col_approve_all, col_spacer = st.columns([1, 3])
        with col_approve_all:
            if st.button("✅ 全部通过", key="approve_all_btn", use_container_width=True):
                for it in items:
                    if it["status"] in ("pending", "needs_revision"):
                        db.update_item_status(db_client, it["id"], "approved")
                st.rerun()

    # ── Batch actions ──────────────────────────────────────────────────
    st.divider()
    st.markdown("<div class='section-label'>批量操作</div>", unsafe_allow_html=True)
    st.caption("多批次合并导出请前往「📤 导出中心」页面。")

    col_feishu, col_calib = st.columns(2)

    with col_feishu:
        if st.button("🔔 推送本批次到飞书", use_container_width=True):
            if not config.FEISHU_WEBHOOK_URL:
                st.warning("飞书 Webhook 未配置（FEISHU_WEBHOOK_URL）。")
            else:
                approved_items = _collect_approved_items(items)
                ok = exporter.push_to_feishu(
                    items=approved_items,
                    project_name=project.get("name", ""),
                    brand=project.get("brand", ""),
                    tactic=selected_batch.get("tactic", ""),
                )
                st.success("已推送到飞书。") if ok else st.error("飞书推送失败，请检查 Webhook 配置。")

    with col_calib:
        if st.button("🧠 更新调教笔记", use_container_width=True):
            _generate_calibration_notes_ui(project, batch_id, items)
        st.caption("对整个批次做整体反思；日常每次迭代已自动增量更新。")

    # 调教笔记预览 + 确认保存
    calib_key = f"pending_calibration_{batch_id}"
    if calib_key in st.session_state:
        st.markdown("---")
        st.markdown("<div class='section-label'>调教笔记预览（可编辑后保存）</div>", unsafe_allow_html=True)
        edited = st.text_area(
            "调教笔记",
            value=st.session_state[calib_key],
            height=240,
            key=f"calib_edit_{batch_id}",
            label_visibility="collapsed",
        )
        save_col, discard_col = st.columns(2)
        with save_col:
            if st.button("💾 保存到项目设置", key=f"save_calib_{batch_id}", use_container_width=True):
                mem_module.save_calibration_notes(db_client, project["id"], edited)
                del st.session_state[calib_key]
                st.success("调教笔记已保存，下次生成时生效。")
                st.rerun()
        with discard_col:
            if st.button("✕ 放弃", key=f"discard_calib_{batch_id}", use_container_width=True):
                del st.session_state[calib_key]
                st.rerun()


def _render_item_card(
    item: dict,
    versions: list[dict],
    batch: dict,
    project: dict,
) -> None:
    """Render a single copy item card with review controls."""
    item_id = item["id"]
    status = item["status"]

    # Use best version if set, else latest
    best_vid = item.get("best_version_id")
    if best_vid:
        display_version = next(
            (v for v in versions if v["id"] == best_vid), versions[-1]
        )
    else:
        display_version = versions[-1]

    status_icon  = {"pending": "⏳", "approved": "✅", "needs_revision": "✏️"}.get(status, "⏳")
    status_label = {"pending": "待审核", "approved": "已通过", "needs_revision": "待修改"}.get(status, "待审核")
    engine_raw = display_version.get("ai_engine", "")
    engine_short = engine_raw.split("/")[0].lower() if engine_raw else ""
    ver_num = display_version.get("version_num", 1)
    title_str = display_version.get("title", "（无标题）") or "（无标题）"

    # Resolve thinking flag from batch params for the badge
    _bp = batch.get("params") or {}
    if isinstance(_bp, str):
        try:
            _bp = json.loads(_bp)
        except Exception:
            _bp = {}
    _thinking_on = (
        _bp.get("use_thinking", False) if engine_short == "claude"
        else _bp.get("gemini_use_thinking", False)
    )

    expander_label = (
        f"{status_icon} {title_str[:60]}{'…' if len(title_str) > 60 else ''}  "
        f"— {status_label} · v{ver_num}"
    )
    # Status marker: CSS uses :has() on this sibling to style the following
    # expander (approved → dark card; other statuses → default white card).
    st.markdown(
        f"<div class='card-status-marker card-{status}'></div>",
        unsafe_allow_html=True,
    )
    with st.expander(expander_label, expanded=(status in ("pending", "needs_revision"))):
        # Engine badge: show full model name + thinking indicator
        eng_cls = engine_short if engine_short in ("claude", "gemini") else ""
        thinking_tag = "&nbsp;🧠" if _thinking_on else ""
        st.markdown(
            f"<span class='engine-badge {eng_cls}'>{_html.escape(engine_raw.upper())}{thinking_tag}</span>",
            unsafe_allow_html=True,
        )

        # AI review notes (from multi-role drafting)
        ai_notes = item.get("ai_review_notes", "")
        if ai_notes:
            with st.expander("🔍 AI 评审意见", expanded=False):
                st.caption(ai_notes)

        # Multi-version comparison (if multiple engines were used)
        unique_engines = {v.get("ai_engine", "").split("/")[0] for v in versions}
        is_multi_engine = len(unique_engines) > 1
        best_vid = item.get("best_version_id")

        if is_multi_engine and not best_vid:
            # 对比模式：两引擎并排，各自独立改写，全局迭代区隐藏
            _render_version_comparison(versions, item_id, item, batch, project, _bp)
            show_global_iteration = False
        else:
            # 单篇模式（单引擎 或 已选最佳）
            _render_single_version(display_version)
            show_global_iteration = True

        # Status controls（始终显示）
        col_approve, col_revise = st.columns(2)
        with col_approve:
            if st.button("✅ 通过", key=f"approve_{item_id}", use_container_width=True):
                db.update_item_status(db_client, item_id, "approved")
                st.rerun()
        with col_revise:
            if st.button("✏️ 需修改", key=f"revise_{item_id}", use_container_width=True):
                db.update_item_status(db_client, item_id, "needs_revision")
                st.rerun()

        # Example label controls
        example_label = item.get("example_label")
        ex_col1, ex_col2, ex_col3 = st.columns(3)
        with ex_col1:
            is_pos = example_label == "positive"
            if st.button(
                "⭐ 正案例" if not is_pos else "⭐ 已标为正案例",
                key=f"pos_ex_{item_id}",
                use_container_width=True,
                type="primary" if is_pos else "secondary",
            ):
                db.set_item_example_label(db_client, item_id, None if is_pos else "positive")
                st.rerun()
        with ex_col2:
            is_neg = example_label == "negative"
            if st.button(
                "👎 反案例" if not is_neg else "👎 已标为反案例",
                key=f"neg_ex_{item_id}",
                use_container_width=True,
                type="primary" if is_neg else "secondary",
            ):
                db.set_item_example_label(db_client, item_id, None if is_neg else "negative")
                st.rerun()
        with ex_col3:
            if example_label:
                label_text = "⭐ 正案例" if example_label == "positive" else "👎 反案例"
                st.caption(f"已标记：{label_text}")

        # Feedback & iteration（对比模式未选最佳时隐藏，由各引擎列内部处理）
        if show_global_iteration and status in ("pending", "needs_revision"):
            st.markdown("<div class='section-label' style='margin-top:12px'>修改意见</div>", unsafe_allow_html=True)

            # Restore any previously-typed feedback that didn't make it through
            # iteration (token expiry, server error, etc.) so the user doesn't
            # have to re-type. Saved by db.save_feedback_draft on submit, cleared
            # by db.clear_feedback_draft on iteration success.
            saved_draft = (item.get("feedback_draft") or "").strip()
            feedback_key = f"feedback_{item_id}"
            if saved_draft and feedback_key not in st.session_state:
                st.session_state[feedback_key] = saved_draft
                st.caption("📌 已自动恢复你上次没保存成功的反馈")

            # Auto-save: on every rerun, if the textarea / tags state differs
            # from what's in DB, persist it.  Doesn't trigger any extra rerun
            # itself — just piggybacks on whatever rerun the user already
            # caused (clicking around, switching tabs, etc.) so an unexpected
            # crash mid-typing doesn't lose the draft.
            _auto_tags = st.session_state.get(f"tags_{item_id}", [])
            _auto_text = st.session_state.get(feedback_key, "")
            _auto_combined = (
                "、".join(_auto_tags)
                + ("；" + _auto_text if _auto_text else "")
            ).strip("、；")
            if _auto_combined and _auto_combined != saved_draft:
                try:
                    db.save_feedback_draft(db_client, item_id, _auto_combined)
                except Exception:
                    pass

            # Quick tags
            selected_tags = st.multiselect(
                "快捷反馈标签",
                QUICK_FEEDBACK_TAGS,
                key=f"tags_{item_id}",
                label_visibility="collapsed",
            )
            feedback_text = st.text_area(
                "详细反馈（可选）",
                key=feedback_key,
                height=80,
                placeholder="在此填写具体修改意见...",
            )

            combined_feedback = (
                "、".join(selected_tags)
                + ("；" + feedback_text if feedback_text else "")
            ).strip("、；")

            # Engine + model selector for iteration
            iter_col_eng, iter_col_mod = st.columns(2)
            with iter_col_eng:
                iter_engine = st.selectbox(
                    "引擎",
                    gen_module.AVAILABLE_ENGINES,
                    format_func=lambda e: "Claude" if e == "claude" else "Gemini",
                    key=f"iter_engine_{item_id}",
                )
            with iter_col_mod:
                if iter_engine == "claude":
                    iter_model = st.selectbox(
                        "模型",
                        list(config.CLAUDE_MODELS.keys()),
                        format_func=lambda m: config.CLAUDE_MODELS.get(m, m),
                        key=f"iter_model_{item_id}",
                        index=list(config.CLAUDE_MODELS.keys()).index(config.CLAUDE_MODEL)
                              if config.CLAUDE_MODEL in config.CLAUDE_MODELS else 0,
                    )
                else:
                    iter_model = st.selectbox(
                        "模型",
                        list(config.GEMINI_MODELS.keys()),
                        format_func=lambda m: config.GEMINI_MODELS.get(m, m),
                        key=f"iter_model_{item_id}",
                        index=list(config.GEMINI_MODELS.keys()).index(config.GEMINI_MODEL)
                              if config.GEMINI_MODEL in config.GEMINI_MODELS else 0,
                    )

            # Claude thinking is selected via model name; no runtime toggle.
            iter_use_thinking = False

            if st.button("🔄 重新生成", key=f"regen_{item_id}", use_container_width=True):
                if not combined_feedback:
                    st.warning("请先填写修改意见或选择快捷标签。")
                else:
                    # Persist the typed feedback BEFORE the AI call so it
                    # survives any failure during iteration (token expiry,
                    # server error, network drop).  Cleared on success.
                    db.save_feedback_draft(db_client, item_id, combined_feedback)
                    # 已选最佳时只用该引擎的版本作历史，避免跨引擎混淆
                    disp_engine_short = display_version.get("ai_engine", "").split("/")[0]
                    iter_versions = (
                        [v for v in versions if v.get("ai_engine", "").split("/")[0] == disp_engine_short]
                        if best_vid else versions
                    )
                    _run_iteration(
                        item, iter_versions, batch, project,
                        combined_feedback, iter_engine, iter_model,
                        use_thinking_override=iter_use_thinking,
                    )

        # ── Manual edit ───────────────────────────────────────────────
        with st.expander("✏️ 手动精修", expanded=False):
            cur_title    = display_version.get("title", "") or ""
            cur_body     = display_version.get("body", "") or ""
            cur_keywords = _normalise_keywords(display_version.get("keywords"))
            base_kw_str  = "，".join(cur_keywords)

            # Widget keys include the version id so that switching the "best"
            # pick in multi-engine compare mode gives us a fresh widget (else
            # Streamlit keeps the session_state value from the first render
            # and ignores the new ``value=`` prop).
            vid = display_version.get("id", "latest")
            title_key = f"edit_title_{item_id}_{vid}"
            body_key  = f"edit_body_{item_id}_{vid}"
            kw_key    = f"edit_kw_{item_id}_{vid}"
            save_key  = f"save_edit_{item_id}_{vid}"

            # Load any saved manual-edit draft for this exact version.  A
            # draft is only used as the initial widget value if it was
            # captured against the same base version_id we're showing — that
            # way switching the "best" pick doesn't surface stale text.
            raw_draft = item.get("manual_edit_draft")
            saved_draft: dict = {}
            if isinstance(raw_draft, dict):
                saved_draft = raw_draft
            elif isinstance(raw_draft, str) and raw_draft.strip():
                try:
                    saved_draft = json.loads(raw_draft)
                except Exception:
                    saved_draft = {}

            draft_applies = saved_draft.get("base_version_id") == display_version.get("id")
            restored = False
            if draft_applies:
                if title_key not in st.session_state and saved_draft.get("title") is not None:
                    st.session_state[title_key] = saved_draft.get("title", "")
                    restored = True
                if body_key not in st.session_state and saved_draft.get("body") is not None:
                    st.session_state[body_key] = saved_draft.get("body", "")
                    restored = True
                if kw_key not in st.session_state and saved_draft.get("keywords_raw") is not None:
                    st.session_state[kw_key] = saved_draft.get("keywords_raw", "")
                    restored = True
            if restored:
                st.caption("📌 已自动恢复你上次没保存成功的手动精修")

            # Auto-save: any divergence from the base version → write to DB.
            # Piggybacks on existing reruns, no extra UI cost (see 2.7.6).
            _cur_title_in_state = st.session_state.get(title_key, cur_title)
            _cur_body_in_state  = st.session_state.get(body_key, cur_body)
            _cur_kw_in_state    = st.session_state.get(kw_key, base_kw_str)
            if (_cur_title_in_state != cur_title
                or _cur_body_in_state != cur_body
                or _cur_kw_in_state != base_kw_str):
                payload = {
                    "title": _cur_title_in_state,
                    "body":  _cur_body_in_state,
                    "keywords_raw": _cur_kw_in_state,
                    "base_version_id": display_version.get("id"),
                }
                if payload != saved_draft:
                    try:
                        db.save_manual_edit_draft(db_client, item_id, payload)
                    except Exception:
                        pass

            edit_title = st.text_input(
                "标题", value=cur_title, key=title_key,
            )
            edit_body = st.text_area(
                "正文", value=cur_body, height=300, key=body_key,
            )
            edit_kw_raw = st.text_input(
                "关键词（逗号分隔）",
                value=base_kw_str,
                key=kw_key,
                placeholder="关键词1，关键词2，关键词3",
            )

            if st.button("💾 保存手动修改并通过", key=save_key, use_container_width=True):
                new_kw = [k.strip() for k in re.split(r"[,，、\s]+", edit_kw_raw) if k.strip()]
                new_title = edit_title.strip()
                new_body  = edit_body.strip()
                new_version = db.create_version(
                    db_client,
                    item_id=item_id,
                    ai_engine="manual",
                    title=new_title,
                    body=new_body,
                    keywords=new_kw,
                    feedback="手动精修",
                )
                try:
                    mem_module.update_calibration_from_manual_edit(
                        db_client,
                        project_id=project["id"],
                        ai_title=cur_title,
                        ai_body=cur_body,
                        manual_title=new_title,
                        manual_body=new_body,
                    )
                except Exception:
                    pass
                db.update_item_status(
                    db_client, item_id, "approved",
                    best_version_id=new_version["id"],
                )
                # Drop the auto-saved draft now that the user committed.
                try:
                    db.clear_manual_edit_draft(db_client, item_id)
                except Exception:
                    pass
                st.success("已保存修改并标记为通过。调教笔记已同步更新。")
                st.rerun()


def _normalise_keywords(raw) -> list[str]:
    """Ensure keywords is always a clean list of strings."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(k).strip() for k in raw if str(k).strip()]
    if isinstance(raw, str):
        # Could be a JSON string like '["k1","k2"]' or plain comma-separated
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(k).strip() for k in parsed if str(k).strip()]
        except Exception:
            pass
        return [k.strip() for k in re.split(r'[,，、\s]+', raw) if k.strip()]
    return []


def _render_single_version(version: dict) -> None:
    title    = version.get("title", "") or ""
    body     = version.get("body", "") or ""
    keywords = _normalise_keywords(version.get("keywords"))
    raw_text = version.get("raw_text", "")

    title_len = len(title)

    safe_title = _html.escape(title)
    safe_body  = _html.escape(body)

    st.markdown(
        f"<div class='copy-title'>{safe_title}</div>"
        f"<div class='copy-meta'><span class='len-neutral'>{title_len} 字</span></div>"
        f"<div class='copy-body'>{safe_body}</div>",
        unsafe_allow_html=True,
    )
    if keywords:
        kw_html = " ".join(f"<span class='tag'>#{_html.escape(k)}</span>" for k in keywords)
        st.markdown(kw_html, unsafe_allow_html=True)

    # Single copy-ready block (title + body + tags) — Streamlit's built-in
    # code-block copy icon gives one-click copy of everything at once.
    if title or body:
        with st.expander("📋 复制全文（标题 + 正文 + 标签）", expanded=False):
            parts: list[str] = []
            if title:
                parts.append(f"【标题】\n{title}")
            if body:
                kw_line = " ".join(f"#{k}" for k in keywords) if keywords else ""
                body_block = f"【正文】\n{body}"
                if kw_line:
                    body_block += f"\n\n{kw_line}"
                parts.append(body_block)
            elif keywords:
                parts.append(" ".join(f"#{k}" for k in keywords))
            st.code("\n\n".join(parts), language=None)

    if not title and raw_text:
        with st.expander("⚠️ 解析失败 — 查看原始 AI 输出", expanded=True):
            st.code(raw_text, language=None)


def _render_version_comparison(
    versions: list[dict],
    item_id: str,
    item: dict,
    batch: dict,
    project: dict,
    _bp: dict,
) -> None:
    """Side-by-side multi-engine version comparison with per-engine iteration."""
    by_engine: dict[str, list[dict]] = {}
    for v in versions:
        engine = v.get("ai_engine", "unknown")
        by_engine.setdefault(engine, []).append(v)

    cols = st.columns(len(by_engine))
    engine_list = list(by_engine.keys())
    status = item["status"]

    for col, engine in zip(cols, engine_list):
        with col:
            eng_short = engine.split("/")[0].lower()
            eng_cls = eng_short if eng_short in ("claude", "gemini") else ""
            st.markdown(
                f"<span class='engine-badge {eng_cls}'>{_html.escape(engine.upper())}</span>",
                unsafe_allow_html=True,
            )
            latest = sorted(by_engine[engine], key=lambda x: x.get("version_num", 0))[-1]
            _render_single_version(latest)

            # 选为最佳：只记录 best_version_id，不直接通过
            if st.button("选为最佳", key=f"best_{item_id}_{engine}"):
                db.update_item_status(
                    db_client, item_id, status,
                    best_version_id=latest["id"],
                )
                st.rerun()

            # 每引擎独立迭代入口
            if status in ("pending", "needs_revision"):
                with st.expander("🔄 改写此版", expanded=False):
                    eng_versions = [
                        v for v in versions
                        if v.get("ai_engine", "").split("/")[0] == eng_short
                    ]
                    # Restore last-saved draft (shared per-item across engines)
                    eng_saved_draft = (item.get("feedback_draft") or "").strip()
                    eng_feedback_key = f"feedback_{item_id}_{engine}"
                    if eng_saved_draft and eng_feedback_key not in st.session_state:
                        st.session_state[eng_feedback_key] = eng_saved_draft

                    # Auto-save: persist draft on every rerun if it's diverged
                    # from DB.  Same rationale as the global iteration form —
                    # piggyback on existing reruns, no extra UI cost.
                    _eng_auto_tags = st.session_state.get(f"tags_{item_id}_{engine}", [])
                    _eng_auto_text = st.session_state.get(eng_feedback_key, "")
                    _eng_auto_combined = (
                        "、".join(_eng_auto_tags)
                        + ("；" + _eng_auto_text if _eng_auto_text else "")
                    ).strip("、；")
                    if _eng_auto_combined and _eng_auto_combined != eng_saved_draft:
                        try:
                            db.save_feedback_draft(db_client, item_id, _eng_auto_combined)
                        except Exception:
                            pass

                    sel_tags = st.multiselect(
                        "快捷标签",
                        QUICK_FEEDBACK_TAGS,
                        key=f"tags_{item_id}_{engine}",
                        label_visibility="collapsed",
                    )
                    fb_text = st.text_area(
                        "详细反馈（可选）",
                        key=eng_feedback_key,
                        height=68,
                        placeholder="修改意见...",
                    )
                    combined_fb = (
                        "、".join(sel_tags) + ("；" + fb_text if fb_text else "")
                    ).strip("、；")

                    if eng_short == "claude":
                        iter_model = st.selectbox(
                            "模型",
                            list(config.CLAUDE_MODELS.keys()),
                            format_func=lambda m: config.CLAUDE_MODELS.get(m, m),
                            key=f"iter_model_{item_id}_{engine}",
                            index=list(config.CLAUDE_MODELS.keys()).index(config.CLAUDE_MODEL)
                                  if config.CLAUDE_MODEL in config.CLAUDE_MODELS else 0,
                        )
                        iter_think = False
                    else:
                        iter_model = st.selectbox(
                            "模型",
                            list(config.GEMINI_MODELS.keys()),
                            format_func=lambda m: config.GEMINI_MODELS.get(m, m),
                            key=f"iter_model_{item_id}_{engine}",
                            index=list(config.GEMINI_MODELS.keys()).index(config.GEMINI_MODEL)
                                  if config.GEMINI_MODEL in config.GEMINI_MODELS else 0,
                        )
                        iter_think = False

                    if st.button("🔄 改写", key=f"regen_{item_id}_{engine}", use_container_width=True):
                        if not combined_fb:
                            st.warning("请先填写修改意见或选择快捷标签。")
                        else:
                            db.save_feedback_draft(db_client, item_id, combined_fb)
                            _run_iteration(
                                item, eng_versions, batch, project,
                                combined_fb, eng_short, iter_model,
                                use_thinking_override=iter_think,
                            )


def _run_iteration(
    item: dict,
    versions: list[dict],
    batch: dict,
    project: dict,
    feedback: str,
    engine_name: str,
    model_override: str = "",
    use_thinking_override: bool | None = None,
) -> None:
    """Run one iteration for an item and save the new version."""
    project_id = project["id"]
    batch_id   = batch.get("id")

    # Route iteration feedback through the AI merger so it lands in the right
    # layer (merge / new rule / taste → calibration note / session).
    ingest_action: Optional[str] = None
    if feedback and feedback.strip():
        ingest_result = mem_module.ingest_user_instruction(
            db_client, user_id, feedback,
            project_id=project_id,
            project_name=project.get("name", ""),
            batch_id=batch_id,
        )
        ingest_action = (ingest_result or {}).get("action")

    base_prompt = project.get("system_prompt", "")
    global_mems, project_mems = db.get_confirmed_memories(
        db_client, user_id, project_id=project_id
    )
    session_instr = db.get_session_instructions(db_client, user_id, project_id=project_id)
    tactic = batch.get("tactic", "")
    tactic_suffix = proj_module.get_tactic_prompt_suffix(project, tactic)
    full_system_prompt = mem_module.build_system_prompt(
        base_prompt=base_prompt,
        global_memories=global_mems,
        project_memories=project_mems,
        tactic_suffix=tactic_suffix,
        calibration_notes=project.get("calibration_notes") or "",
        session_instructions=session_instr or None,
    )

    batch_params = batch.get("params") or {}
    if isinstance(batch_params, str):
        try:
            batch_params = json.loads(batch_params)
        except Exception:
            batch_params = {}

    original_user_prompt = gen_module.reconstruct_user_prompt(batch_params, tactic)

    _bp = batch_params if isinstance(batch_params, dict) else {}
    iter_use_thinking = _bp.get("use_thinking", False)
    iter_gemini_thinking = _bp.get("gemini_use_thinking", False)
    # Use explicitly selected model, fall back to batch-saved model
    iter_model = model_override or (_bp.get("engine_models") or {}).get(engine_name, "")
    # If caller passes an explicit thinking override, honour it; else fall back to batch setting
    if use_thinking_override is not None:
        thinking_flag = use_thinking_override
    else:
        thinking_flag = iter_use_thinking if engine_name == "claude" else (iter_gemini_thinking if engine_name == "gemini" else False)
    with st.spinner(f"正在用 {engine_name.upper()}{' (深度思考)' if thinking_flag else ''} 迭代…"):
        result = gen_module.iterate_copy(
            system_prompt=full_system_prompt,
            original_user_prompt=original_user_prompt,
            version_history=versions,
            feedback=feedback,
            engine_name=engine_name,
            use_thinking=thinking_flag,
            model=iter_model,
        )

    if result.error:
        st.error(f"迭代失败：{result.error}")
        return

    db.create_version(
        db_client,
        item_id=item["id"],
        ai_engine=result.ai_engine,
        title=result.title,
        body=result.body,
        keywords=result.keywords,
        feedback=feedback,
        token_usage=result.token_usage,
    )

    # Incrementally update calibration notes from this single iteration.
    # Every correction the user makes carries a "why" — capture it now rather
    # than waiting for the whole batch to go green.  Runs silently; failure
    # does not block the main flow.
    try:
        prev_version = versions[-1] if versions else {}
        mem_module.update_calibration_from_iteration(
            db_client,
            project_id=project_id,
            old_title=prev_version.get("title", "") or "",
            old_body=prev_version.get("body", "") or "",
            feedback=feedback or "",
            new_title=result.title or "",
            new_body=result.body or "",
        )
    except Exception:
        pass

    # Reset item to pending so it gets reviewed again
    db.update_item_status(db_client, item["id"], "pending")

    # Iteration succeeded — drop the saved draft so the textarea doesn't
    # auto-restore it on the next render.
    db.clear_feedback_draft(db_client, item["id"])

    # Surface the merger's routing decision so the user can see whether
    # their feedback became a permanent rule, a taste note, or a 24h
    # session instruction — otherwise it feels like typed reasons vanish.
    ingest_label = {
        "rule":    "📌 反馈已沉淀为永久规则（记忆管理里可查）",
        "merge":   "📌 反馈并入了已有规则（使用次数 +1）",
        "taste":   "🎨 反馈已追加到调教笔记",
        "session": "⏳ 反馈已加入 24h 会话指令",
    }.get(ingest_action or "")
    if ingest_label:
        st.toast(ingest_label, icon="✅")

    st.success("迭代成功！")
    st.rerun()


def _collect_approved_items(items: list[dict]) -> list[dict]:
    """Build a flat list of approved items for export."""
    result = []
    for item in items:
        if item["status"] != "approved":
            continue
        versions = sorted(item.get("versions", []), key=lambda v: v.get("version_num", 0))
        if not versions:
            continue
        best_vid = item.get("best_version_id")
        if best_vid:
            v = next((x for x in versions if x["id"] == best_vid), versions[-1])
        else:
            v = versions[-1]
        result.append({
            "title": v.get("title", ""),
            "body": v.get("body", ""),
            "keywords": v.get("keywords", []),
            "ai_engine": v.get("ai_engine", ""),
            "version_num": v.get("version_num", 1),
        })
    return result


def _auto_update_calibration_notes(project: dict, batch_id: str, items: list[dict]) -> None:
    """
    太子自动学习：批次全部通过后静默生成并保存调教笔记，无需人工确认。
    失败时静默跳过，不打断用户操作。
    """
    existing = (project.get("calibration_notes") or "").rstrip()
    try:
        with st.spinner("🧠 太子学习中…"):
            notes = mem_module.generate_calibration_notes(
                project_name=project.get("name", ""),
                existing_notes=existing,
                items_with_versions=items,
            )
            # Append-only contract: generate_calibration_notes returns the
            # existing text plus any newly appended observations.  Skip the
            # save when nothing changed so the row's timestamp / dedup ordering
            # stays untouched.
            if notes and notes.rstrip() != existing:
                mem_module.save_calibration_notes(db_client, project["id"], notes)
                st.toast("🧠 调教笔记已新增观察（太子学习完成）")
    except Exception:
        pass  # 静默失败，不影响主流程


def _generate_calibration_notes_ui(project: dict, batch_id: str, items: list[dict]) -> None:
    """Ask AI to reflect on this batch's *explicit* user signals and update
    calibration notes.  Only iteration feedback and manual-edit diffs count —
    bare "approved" drafts are ignored by the generator."""
    calib_key = f"pending_calibration_{batch_id}"
    existing = (project.get("calibration_notes") or "").strip()

    # Count explicit signals for the pre-flight message.
    signal_count = 0
    for it in items:
        versions = sorted(it.get("versions") or [], key=lambda v: v.get("version_num", 0))
        if len(versions) < 2:
            continue
        has_feedback = any(
            (v.get("feedback") or "").strip() and v.get("feedback") != "手动精修"
            for v in versions[1:]
        )
        has_manual_edit = (versions[-1].get("ai_engine") or "").lower() == "manual"
        if has_feedback or has_manual_edit:
            signal_count += 1

    if signal_count == 0:
        st.info(
            "本批次没有可学习的显式信号（需要至少 1 条带文字反馈的迭代，"
            "或一次手动精修）。调教笔记保持不变。"
        )
        return

    with st.spinner("AI 正在分析本批次互动，生成调教笔记…"):
        try:
            notes = (mem_module.generate_calibration_notes(
                project_name=project.get("name", ""),
                existing_notes=existing,
                items_with_versions=items,
            ) or "").strip()
            if not notes or notes == existing:
                st.info("本批次的显式信号与现有笔记一致，调教笔记保持不变。")
                return
            st.session_state[calib_key] = notes
            st.rerun()
        except Exception as e:
            st.error(f"生成失败：{e}")


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: 导出中心
# ═══════════════════════════════════════════════════════════════════════════

def page_export(project: dict) -> None:
    pname = _html.escape(project.get("name", ""))
    _hero_header("03 / EXPORT", "装箱发货。", f"项目 · {pname}")

    batches = db.list_batches(db_client, project["id"], limit=50)
    if not batches:
        st.info("暂无批次，请先在「生成工作台」生成内容。")
        return

    st.markdown(
        "<p style='font-size:0.875rem;color:var(--text-2);margin-bottom:16px'>"
        "勾选要导出的批次，点击「生成导出文件」统一输出为 Excel。"
        "每篇内容独占一个单元格，格式为：标题 / 正文 / 关键词。</p>",
        unsafe_allow_html=True,
    )

    project_name = project.get("name", "")

    # ── Batch selector table ────────────────────────────────────────────
    # Pre-load approved counts for all batches in bulk (one query per batch,
    # acceptable for ≤50 batches; can be optimised later with a view).
    batch_meta: list[dict] = []
    for batch in batches:
        items = db.list_items(db_client, batch["id"])
        approved = sum(1 for it in items if it["status"] == "approved")
        total    = len(items)
        batch_meta.append({
            "batch":    batch,
            "items":    items,
            "approved": approved,
            "total":    total,
        })

    batch_selections: dict[str, bool] = {}
    for meta in batch_meta:
        batch    = meta["batch"]
        approved = meta["approved"]
        total    = meta["total"]
        label    = _format_batch_label(batch, project_name)
        tactic   = batch.get("tactic", "通用") or "通用"

        col_ck, col_info = st.columns([1, 11])
        with col_ck:
            checked = st.checkbox(
                "选择", key=f"exp_batch_{batch['id']}",
                label_visibility="collapsed",
                value=st.session_state.get(f"exp_batch_{batch['id']}", False),
            )
        with col_info:
            badge_color = "green" if approved == total and total > 0 else "amber"
            st.markdown(
                f"<span style='font-size:0.9rem;font-weight:500'>{_html.escape(label)}</span>"
                f"&nbsp;&nbsp;<span style='font-size:0.8rem;color:var(--text-3)'>"
                f"{tactic} · {approved}/{total} 篇已通过</span>",
                unsafe_allow_html=True,
            )
        batch_selections[batch["id"]] = checked

    selected_ids = [bid for bid, v in batch_selections.items() if v]
    n_selected   = len(selected_ids)

    # Quick-select buttons
    qcol1, qcol2, _ = st.columns([2, 2, 8])
    with qcol1:
        if st.button("全选", key="exp_select_all"):
            for meta in batch_meta:
                st.session_state[f"exp_batch_{meta['batch']['id']}"] = True
            st.rerun()
    with qcol2:
        if st.button("取消全选", key="exp_deselect_all"):
            for meta in batch_meta:
                st.session_state[f"exp_batch_{meta['batch']['id']}"] = False
            st.rerun()

    st.divider()

    # ── Export controls ─────────────────────────────────────────────────
    # Show previously generated export if available
    exp_state = st.session_state.get("export_center_result")
    if exp_state:
        st.success(f"文件已就绪，共 {exp_state['count']} 篇内容，来自 {exp_state['n_batches']} 个批次。")
        dl_col, clr_col = st.columns([3, 1])
        with dl_col:
            st.download_button(
                label=f"📊 下载 Excel（{exp_state['count']} 篇）",
                data=exp_state["bytes"],
                file_name=exp_state["filename"],
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                type="primary",
            )
        with clr_col:
            if st.button("清除", key="exp_clear", use_container_width=True):
                del st.session_state["export_center_result"]
                st.rerun()

    # Map batch_id → items for fast lookup (already loaded above)
    batch_items_map = {meta["batch"]["id"]: meta["items"] for meta in batch_meta}

    gen_label = f"📥 生成导出文件（{n_selected} 个批次）" if n_selected else "📥 生成导出文件"
    if st.button(gen_label, type="primary", disabled=(n_selected == 0), use_container_width=True):
        all_items: list[dict] = []
        for bid in selected_ids:
            all_items.extend(_collect_approved_items(batch_items_map.get(bid, [])))

        if not all_items:
            st.warning("选中的批次中没有已通过的内容，请先在「审核与迭代」中通过稿件。")
        else:
            try:
                xlsx_bytes = exporter.build_combined_excel(all_items)
                brand = project.get("brand", "") or project_name
                filename = f"xhs_{brand}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
                st.session_state["export_center_result"] = {
                    "bytes":    xlsx_bytes,
                    "count":    len(all_items),
                    "n_batches": n_selected,
                    "filename": filename,
                }
                st.rerun()
            except RuntimeError as e:
                st.error(str(e))

    # ── Feishu push (multi-batch) ───────────────────────────────────────
    if config.FEISHU_WEBHOOK_URL and n_selected > 0:
        st.divider()
        if st.button("🔔 推送选中批次到飞书", use_container_width=True):
            all_items = []
            for bid in selected_ids:
                all_items.extend(_collect_approved_items(batch_items_map.get(bid, [])))
            if not all_items:
                st.warning("选中的批次中没有已通过的内容。")
            else:
                ok = exporter.push_to_feishu(
                    items=all_items,
                    project_name=project_name,
                    brand=project.get("brand", ""),
                    tactic="混合批次",
                )
                st.success("已推送到飞书。") if ok else st.error("飞书推送失败，请检查 Webhook 配置。")


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: 项目设置
# ═══════════════════════════════════════════════════════════════════════════

def page_project_settings(project: Optional[dict]) -> None:
    if project is None:
        st.info("请先在左侧创建或选择一个项目。")
        return
    _hero_header("05 / SETTINGS", "配置你的工作流。", project.get("name", ""))
    proj_module.render_project_settings(db_client, project, user_id)


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: 记忆管理
# ═══════════════════════════════════════════════════════════════════════════

def page_memory(project: Optional[dict]) -> None:
    pid   = project["id"] if project else None
    pname = project.get("name", "") if project else ""
    _hero_header("04 / MEMORY", "训练你的风格。", pname)
    mem_module.render_memory_manager(db_client, user_id, project_id=pid, project_name=pname)


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: 批次历史
# ═══════════════════════════════════════════════════════════════════════════

def page_history(project: dict) -> None:
    _hero_header("06 / HISTORY", "回顾每一次生成。", f"项目 · {project.get('name', '')}")

    batches = db.list_batches(db_client, project["id"], limit=50)
    if not batches:
        st.info("暂无历史批次。")
        return

    # Pre-load item counts for all batches to avoid N+1 queries
    batch_item_counts = db.get_batch_item_counts(db_client, [b["id"] for b in batches])

    for batch in batches:
        batch_params = batch.get("params") or {}
        if isinstance(batch_params, str):
            try:
                batch_params = json.loads(batch_params)
            except Exception:
                batch_params = {}

        counts = batch_item_counts.get(batch["id"], {"total": 0, "approved": 0, "pending": 0, "needs_revision": 0})

        batch_label = _format_batch_label(batch, project.get("name", ""))
        with st.expander(
            f"📦 {batch_label}  ·  共{counts['total']}篇"
            f"（✅{counts['approved']} ⏳{counts['pending']} ✏️{counts['needs_revision']}）"
        ):
            col1, col2 = st.columns(2)
            with col1:
                st.markdown(f"**批次ID：** `{batch['id'][:8]}…`")
                engines_raw = batch.get("ai_engines", "[]")
                if isinstance(engines_raw, str):
                    try:
                        engines_raw = json.loads(engines_raw)
                    except Exception:
                        engines_raw = [engines_raw]
                st.markdown(f"**AI引擎：** {', '.join(e.upper() for e in engines_raw)}")
            with col2:
                st.markdown(f"**目标人群：** {batch_params.get('target_audience', '—')}")
                st.markdown(f"**核心卖点：** {batch_params.get('key_messages', '—')}")

            btn_col, del_col = st.columns([3, 1])
            with btn_col:
                if counts['total'] > 0:
                    if st.button("查看此批次", key=f"view_batch_{batch['id']}"):
                        st.session_state["review_batch_id"] = batch["id"]
                        st.rerun()
            with del_col:
                confirm_key = f"confirm_del_{batch['id']}"
                if st.session_state.get(confirm_key):
                    st.warning("确认删除？此操作不可撤销。")
                    yes_col, no_col = st.columns(2)
                    with yes_col:
                        if st.button("确认删除", key=f"do_del_{batch['id']}", type="primary"):
                            db.delete_batch(db_client, batch["id"])
                            st.session_state.pop(confirm_key, None)
                            st.success("已删除")
                            st.rerun()
                    with no_col:
                        if st.button("取消", key=f"cancel_del_{batch['id']}"):
                            st.session_state.pop(confirm_key, None)
                            st.rerun()
                else:
                    if st.button("🗑️ 删除批次", key=f"del_batch_{batch['id']}"):
                        st.session_state[confirm_key] = True
                        st.rerun()


# ═══════════════════════════════════════════════════════════════════════════
# ROUTER
# ═══════════════════════════════════════════════════════════════════════════

def _render_page() -> None:
    """Dispatch to the selected page.  Wrapped by an error boundary below
    so unhandled DB / API errors don't show Streamlit's redacted red box —
    the user sees the actual cause and can recover without re-logging in."""
    if page == "生成工作台":
        if selected_project:
            page_generate(selected_project)
    elif page == "审核与迭代":
        if selected_project:
            page_review(selected_project)
    elif page == "导出中心":
        if selected_project:
            page_export(selected_project)
    elif page == "记忆管理":
        page_memory(selected_project)
    elif page == "项目设置":
        page_project_settings(selected_project)
    elif page == "批次历史":
        if selected_project:
            page_history(selected_project)


try:
    _render_page()
except Exception as _err:
    _render_error_panel(_err)
