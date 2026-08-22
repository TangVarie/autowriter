"""deskcore/store.py — deskcore 独有的查询形状。

设计取舍: 能复用的一律调 ``db.*``（``get_service_client`` / ``get_project`` /
``list_example_items`` / ``set_item_example_label`` / ``upsert_memory`` …），
本模块只放 db.py 里**没有的查询形状**:

  · 按 project 读【全团队共享】的规则 —— db.get_confirmed_memories 是按 user_id
    过滤的(Streamlit 单用户视角), 而 deskcore 的口径是「项目规则团队共享」。
  · 带 embedding 的正负例 —— db.list_example_items 只返回 {title, body}, 相关性
    选取需要向量。
  · 四张新表(发牌台账 / 成稿指纹 / 个人调校笔记 / 手动精修 diff)的读写。

为什么不直接往 db.py 加: R-020 已经把 db.py(3376 行) / memory.py / app.py 标为
「改动成本高的巨型文件」, 再往里塞只会更糟。这里是新增能力, 单独一个薄模块。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import db

logger = logging.getLogger("deskcore.store")


def client():
    """service_role client（已带 ClientOptions(schema='autowriter')）。

    deskcore 绕 RLS 是必须的: 「项目规则团队共享」这条口径要求跨 owner 读规则,
    RLS 的 user_id = auth.uid() 做不到。隔离口径改由服务端自己执行 ——
    共享层按 project_id 读全量, 个人层显式带 user_id 过滤。
    """
    return db.get_service_client()


def iso_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 项目 ──────────────────────────────────────────────────────────────────

def list_all_projects(sb) -> list[dict]:
    """全部项目, 不按 owner 过滤。

    db.list_projects 是 .eq("owner_id", user_id) —— 那是 Streamlit 里「我的项目」
    的视角。deskcore 要让任何人都能打开任何项目(规则共享), 所以这里不过滤,
    但把 owner_id 带出去, 调用方需要时能显示归属。
    """
    res = (sb.table("projects")
             .select("id, name, brand, owner_id")
             .order("name")
             .execute())
    return res.data or []


# ── 规则(共享层) ──────────────────────────────────────────────────────────

def shared_memories(sb, project_id: str,
                    user_id: str | None = None) -> tuple[list[dict], list[dict]]:
    """项目的 confirmed 规则。

    返回 (hard, soft)。muted_until 未到期的过滤掉(用户临时静音一条规则而不删)。
    同时带上 scope='global' 的通用规则。

    ⚠️ 【故意不吞异常】。这里读的是项目的强制合规规则(禁词/必含话术/绝对不能
    提的内容)。查询失败若降级成空列表, build_writing_brief 会返回一个 p0 为空、
    却没有任何错误标记的正常简报 —— 调用方照常开写, 而这一批稿子【不带任何
    硬约束】。那正是这个服务存在的意义所在, 也是最坏的失败模式: 不报错、
    看起来一切正常、产出的却是违规内容。宁可整个 open_project 报错。
    """
    def _rows(query):
        return (query.eq("status", "confirmed")
                     .neq("memory_type", "session")
                     .execute()).data or []

    cols = "id, content, severity, scope, rule_kind, rule_payload, muted_until, user_id"
    proj = _rows(sb.table("memories").select(cols)
                   .eq("project_id", project_id).eq("scope", "project"))
    glob = (_rows(sb.table("memories").select(cols)
                    .eq("scope", "global").eq("user_id", user_id))
            if user_id else [])

    now = datetime.now(timezone.utc)

    def _active(m: dict) -> bool:
        mu = m.get("muted_until")
        if not mu:
            return True
        try:
            return datetime.fromisoformat(str(mu).replace("Z", "+00:00")) < now
        except (ValueError, TypeError):
            return True

    rows = [m for m in (glob + proj) if _active(m) and (m.get("content") or "").strip()]
    hard = [m for m in rows if (m.get("severity") or "soft").lower() == "hard"]
    soft = [m for m in rows if (m.get("severity") or "soft").lower() != "hard"]
    return hard, soft


# ── 正负例(个人层, 带向量) ────────────────────────────────────────────────

def labeled_examples(sb, project_id: str, label: str,
                     user_id: str | None = None, limit: int = 60) -> list[dict]:
    """取正/负例候选, 带 version embedding。

    走 PostgREST embedded inner join(``batches!inner(project_id)``) —— 与
    db.list_example_items:2176-2180 同一个做法, 绕开「最近 50 batch 窗口」那个坑
    (TV 同步进来的 special batch 一滚出窗口就读不到, 飞轮中断)。

    user_id 非空时只取本人的: 正负例是个人风格资产, 属私有层。
    """
    q = (sb.table("items")
           .select("id, best_version_id, created_at, user_id, "
                   "versions(id, title, body, version_num, embedding), "
                   "batches!inner(project_id)")
           .eq("batches.project_id", project_id)
           .eq("example_label", label)
           .order("created_at", desc=True)
           .limit(limit))
    if user_id:
        q = q.eq("user_id", user_id)
    try:
        res = q.execute()
    except Exception:
        logger.exception("read %s examples failed (project=%s)", label, project_id)
        return []

    out: list[dict] = []
    for item in (res.data or []):
        versions = item.get("versions") or []
        if not versions:
            continue
        best = item.get("best_version_id")
        chosen = next((v for v in versions if v.get("id") == best), None)
        if chosen is None:
            chosen = max(versions, key=lambda v: v.get("version_num") or 0)
        title = (chosen.get("title") or "").strip()
        body = (chosen.get("body") or "").strip()
        if not (title or body):
            continue
        out.append({
            "item_id": item["id"],
            "version_id": chosen.get("id"),
            "title": title,
            "body": body,
            # select("*") 之外的显式取列同样会把 pgvector 带回字符串形态,
            # 这里统一归一(同 db.list_memories 的 R-034 处理)。
            "embedding": db._parse_pgvector(chosen.get("embedding")),
        })
    return out


def item_owner(sb, item_id: str) -> str | None:
    """item 归谁。label_example 校验归属用 —— service_role 绕了 RLS, 归属校验
    必须自己做, 否则任何人都能改别人的正负例池。"""
    res = sb.table("items").select("user_id").eq("id", item_id).limit(1).execute()
    rows = res.data or []
    return rows[0].get("user_id") if rows else None


# ── 发牌台账 ──────────────────────────────────────────────────────────────

def recent_angle_keys(sb, project_id: str, avoid_days: int) -> set[str]:
    """近期用过的角度组合。

    两档时效:
      · 真出了稿的(consumed_version_id 非 NULL)按 **consumed_at** 算 avoid_days
      · 只抽了没写的(占位)按 drawn_at 算 1 天 —— 抽了不写不该长期占坑, 否则
        连点几次发牌就把组合空间锁死了

    ⚠️ consumed 那一档【必须按 consumed_at 而不是 drawn_at】: 审稿定稿常常拖
    几天, 若按 drawn_at 算, 一条今天刚定稿、但上个月抽的角度会立刻不在避重集里
    (drawn_at 已超窗), 下一批马上重用刚发出去的角度。avoid_days 调小时更明显。
    """
    keys: set[str] = set()
    try:
        used = (sb.table("angle_ledger").select("angle_key")
                  .eq("project_id", project_id)
                  .not_.is_("consumed_version_id", "null")
                  .gte("consumed_at", iso_ago(avoid_days)).execute()).data or []
        keys.update(r["angle_key"] for r in used)
        held = (sb.table("angle_ledger").select("angle_key")
                  .eq("project_id", project_id)
                  .is_("consumed_version_id", "null")
                  .gte("drawn_at", iso_ago(1)).execute()).data or []
        keys.update(r["angle_key"] for r in held)
    except Exception:
        logger.exception("read angle ledger failed (project=%s); "
                         "drawing WITHOUT cross-batch avoidance", project_id)
    return keys


def reserve_angles(sb, project_id: str, candidates: list[dict],
                   want: int, user_id: str | None,
                   avoid_days: int) -> list[dict] | None:
    """原子预留: 走 deskcore_reserve_angles RPC。

    读避重集 + 挑 + 插入三步在一个事务里, 同项目由事务级 advisory lock 串行化。
    不这样做的话, 两个队友同时发牌会各自读到"没用过"再各自插入, 同一个角度被
    两批同时用掉, 而两边都报告成功。

    candidates 过量供给(远多于 want), 函数取前 want 个可用的。
    返回 [{"angle_key","dims"}, ...]; RPC 不存在(迁移没跑)时返回 None,
    由调用方决定怎么降级。
    """
    if want <= 0 or not candidates:
        return []
    try:
        res = sb.rpc("deskcore_reserve_angles", {
            "_project_id": project_id,
            "_candidates": candidates,
            "_drawn_by": user_id,
            "_want": want,
            "_avoid_days": avoid_days,
        }).execute()
    except Exception as exc:
        msg = str(exc).lower()
        if "could not find the function" in msg or "does not exist" in msg or "pgrst202" in msg:
            logger.error("deskcore_reserve_angles RPC 不存在 —— migrations/001 还没跑? "
                         "本次降级为非原子发牌(并发时可能撞车)。")
            return None
        raise
    return [{"angle_key": r.get("reserved_key"), "dims": r.get("reserved_dims") or {}}
            for r in (res.data or []) if r.get("reserved_key")]


def record_draw(sb, project_id: str, angles: list[dict], user_id: str | None) -> None:
    """非原子降级路径: RPC 不可用时直接插台账。

    失败不阻塞发牌, 但必须留痕 —— 否则下次避重静默失效。
    """
    if not angles:
        return
    rows = [{"project_id": project_id, "angle_key": a["angle_key"],
             "dims": a["dims"], "drawn_by": user_id} for a in angles]
    try:
        sb.table("angle_ledger").insert(rows).execute()
    except Exception:
        logger.exception("write angle_ledger failed (project=%s); cross-batch "
                         "avoidance will not see this draw", project_id)


def consume_angle(sb, project_id: str, angle_key: str, version_id: str) -> bool:
    try:
        (sb.table("angle_ledger")
           .update({"consumed_version_id": version_id, "consumed_at": iso_now()})
           .eq("project_id", project_id).eq("angle_key", angle_key)
           .is_("consumed_version_id", "null").execute())
        return True
    except Exception:
        logger.exception("mark angle consumed failed: %s", angle_key)
        return False


# ── 成稿指纹库 ────────────────────────────────────────────────────────────

def fingerprints(sb, project_id: str, limit: int = 4000) -> list[dict]:
    """项目【全量】历史指纹。

    ⚠️ 故意不吞异常: 查重是硬闸, 读不到历史就不能放行。这是 deskcore 里唯一
    不 fail-open 的路径(其余读类工具出错返回可用结构不阻塞写稿)。
    """
    res = (sb.table("draft_fingerprints")
             .select("id, title, opening, title_embedding, opening_hash, "
                     "ngram_hashes, created_at")
             .eq("project_id", project_id)
             .order("created_at", desc=True)
             .limit(limit).execute())
    rows = res.data or []
    for r in rows:
        r["title_embedding"] = db._parse_pgvector(r.get("title_embedding"))
    return rows


def legacy_versions(sb, project_id: str, limit: int = 5000) -> list[dict]:
    """项目历史成稿(items × versions), 供指纹回填。

    为什么必须有这个: 迁移建的是【空表】, 而 check_drafts 只读这张表, 只有
    commit_drafts 会往里写。也就是说刚上线那天, 号称"比对全量历史"的硬闸
    实际上一条历史都没有 —— 老稿子的重复会原样放行(codex review P1)。

    只取每个 item 的 best/最新版本(与 db.list_example_items 同口径), 因为中间
    的迭代版本不是"发出去的东西", 拿它们当查重基线会误伤后续正常改写。
    """
    try:
        res = (sb.table("items")
                 .select("id, best_version_id, user_id, created_at, "
                         "versions(id, title, body, version_num, embedding), "
                         "batches!inner(project_id)")
                 .eq("batches.project_id", project_id)
                 .order("created_at", desc=True)
                 .limit(limit).execute())
    except Exception:
        logger.exception("read legacy versions failed (project=%s)", project_id)
        raise

    out: list[dict] = []
    for item in (res.data or []):
        versions = item.get("versions") or []
        if not versions:
            continue
        best = item.get("best_version_id")
        chosen = next((v for v in versions if v.get("id") == best), None)
        if chosen is None:
            chosen = max(versions, key=lambda v: v.get("version_num") or 0)
        title = (chosen.get("title") or "").strip()
        body = (chosen.get("body") or "").strip()
        if not (title or body):
            continue
        out.append({
            "version_id": chosen.get("id"),
            "user_id": item.get("user_id"),
            "title": title,
            "body": body,
            "embedding": db._parse_pgvector(chosen.get("embedding")),
        })
    return out


def existing_fingerprint_version_ids(sb, project_id: str) -> set[str]:
    """已经有指纹的 version_id —— 回填要幂等, 重跑不能造重复行。"""
    try:
        res = (sb.table("draft_fingerprints").select("version_id")
                 .eq("project_id", project_id)
                 .not_.is_("version_id", "null").execute())
        return {r["version_id"] for r in (res.data or []) if r.get("version_id")}
    except Exception:
        logger.exception("read existing fingerprint version_ids failed")
        raise


def write_fingerprints(sb, rows: list[dict]) -> int:
    """直插指纹(不查重)。只给【回填】用 —— 回填的是已发生的历史, 本来就该原样入库。

    定稿入库【不要】走这里, 走 commit_fingerprints_atomic。
    """
    if not rows:
        return 0
    sb.table("draft_fingerprints").insert(rows).execute()
    return len(rows)


def commit_fingerprints_atomic(sb, project_id: str, rows: list[dict],
                               user_id: str | None,
                               ngram_hard: float) -> list[dict] | None:
    """定稿入库 + 【同一事务内重新查一遍】, 走 deskcore_commit_fingerprints RPC。

    为什么不能直接 insert: check_drafts 和 commit_drafts 是两次独立调用。两个
    队友各自 check 时都看到同一份旧指纹集、双双 pass, 然后各自 commit —— 两篇
    撞车的稿子都进了库。发牌那边已经用 advisory lock 串行化了, 这边不能留着。

    返回每条的 {idx, status, collided_with, detail}; RPC 不存在(迁移没跑)时
    返回 None, 由调用方降级。
    """
    if not rows:
        return []
    try:
        res = sb.rpc("deskcore_commit_fingerprints", {
            "_project_id": project_id,
            "_rows": rows,
            "_user_id": user_id,
            "_ngram_hard": ngram_hard,
        }).execute()
    except Exception as exc:
        msg = str(exc).lower()
        if "could not find the function" in msg or "does not exist" in msg or "pgrst202" in msg:
            logger.error("deskcore_commit_fingerprints RPC 不存在 —— migrations/001 "
                         "还没跑? 本次降级为直插(并发 check/commit 可能撞车)。")
            return None
        raise
    return res.data or []


# ── 个人调校笔记 / 精修 diff(私有层) ──────────────────────────────────────

def get_user_calibration(sb, project_id: str, user_id: str) -> tuple[str, str | None]:
    try:
        r = (sb.table("user_calibration_notes").select("notes, updated_at")
               .eq("project_id", project_id).eq("user_id", user_id)
               .limit(1).execute())
        if r.data:
            return (r.data[0].get("notes") or "").strip(), r.data[0].get("updated_at")
    except Exception:
        logger.exception("read user calibration failed")
    return "", None


def save_user_calibration(sb, project_id: str, user_id: str, notes: str) -> None:
    (sb.table("user_calibration_notes")
       .upsert({"project_id": project_id, "user_id": user_id, "notes": notes.strip()},
               on_conflict="project_id,user_id").execute())


def add_style_edit(sb, project_id: str, user_id: str, **fields) -> None:
    row = {"project_id": project_id, "user_id": user_id}
    row.update({k: v for k, v in fields.items() if v is not None})
    sb.table("style_edits").insert(row).execute()


def recent_style_edits(sb, project_id: str, user_id: str, limit: int = 8) -> list[dict]:
    try:
        res = (sb.table("style_edits")
                 .select("ai_title, ai_body, my_title, my_body, note")
                 .eq("project_id", project_id).eq("user_id", user_id)
                 .order("created_at", desc=True).limit(limit).execute())
        return res.data or []
    except Exception:
        logger.exception("read style_edits failed")
        return []


def mark_edits_distilled(sb, project_id: str, user_id: str) -> None:
    try:
        (sb.table("style_edits").update({"distilled": True})
           .eq("project_id", project_id).eq("user_id", user_id)
           .eq("distilled", False).execute())
    except Exception:
        logger.exception("mark style_edits distilled failed (notes are saved)")


def count_style_edits(sb, project_id: str, user_id: str) -> int:
    try:
        return (sb.table("style_edits").select("id", count="exact")
                  .eq("project_id", project_id).eq("user_id", user_id)
                  .limit(1).execute()).count or 0
    except Exception:
        logger.exception("count style_edits failed")
        return 0
