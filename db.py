"""
Supabase database operations for XHS Content Workstation.

Tables:
  projects  – project configuration per user
  batches   – generation batches linked to a project
  items     – individual copy items within a batch
  versions  – text versions of each item (with AI engine + feedback)
  memories  – project-level or global feedback memories
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Optional
from datetime import datetime, timezone, timedelta

from supabase import create_client, Client
from supabase.client import ClientOptions
import config
import telemetry

try:
    import streamlit as st
    _HAS_ST = True
except Exception:
    _HAS_ST = False

# R-042: worker.py 独立进程里 streamlit 同样可 import(同一份 requirements),
# _HAS_ST=True 会让缓存 shim 走真 st.cache_data —— 跨进程缓存无法被 app 的
# .clear() 失效, Phase 2 的 worker handler 一旦调 get_confirmed_memories 等
# 就会拿 30-60s 旧数据(用户改完记忆立刻排队生成, worker 用旧记忆)。worker
# 入口设 AW_DISABLE_ST_CACHE=1 让 shim 在该进程退化为透传。
import os as _os
if _os.environ.get("AW_DISABLE_ST_CACHE", "") in ("1", "true", "True"):
    _HAS_ST = False


def _cache_data(**kwargs):
    """Streamlit cache_data shim — no-op decorator when Streamlit isn't loaded
    (e.g. unit-test imports), and otherwise delegate to ``st.cache_data``.

    Callers pass the underlying Client positionally as a ``_client`` parameter
    so Streamlit's hasher skips it (leading underscore == unhashable).  Cache
    invalidation is done by writers calling ``<reader>.clear()`` after mutating
    the underlying row.
    """
    if _HAS_ST:
        return st.cache_data(**kwargs)
    def passthrough(fn):
        fn.clear = lambda: None  # match cache_data API for unconditional callers
        return fn
    return passthrough


def _cache_resource(**kwargs):
    """Same shim, for objects whose identity matters (e.g. Supabase Client)."""
    if _HAS_ST:
        return st.cache_resource(**kwargs)
    def passthrough(fn):
        fn.clear = lambda: None
        return fn
    return passthrough


# 审计 SUP-007 / ROB-013: 这个缓存原来【无上限、无过期】, 而 key 里带的
# access_token 是**每小时轮换**的 —— 同一个人每小时就新增一个 Client, 每个
# Client 自带 httpx 连接池和一把文件描述符, 而且永远不会被回收。跑得久一点就是
# Supabase 连接数爬满 / Too many open files, 表现是所有查询突然开始超时。
#
# ttl 取 2 小时: 必须【大于】token 的有效期(约 1 小时), 否则活跃会话的 client
# 会在用得正欢的时候被踢掉、每次重建连接池 —— 那是另一种浪费。
# max_entries 是并发用户数的上限, 不是"最多缓存几个 token": 超出后 Streamlit
# 按 LRU 淘汰, 被淘汰的人下次请求重建一个 —— 慢一点, 不会错。
_CLIENT_CACHE_TTL = 7200
_CLIENT_CACHE_MAX = 64


@_cache_resource(show_spinner=False, ttl=_CLIENT_CACHE_TTL,
                 max_entries=_CLIENT_CACHE_MAX)
def _make_client_cached(supabase_url: str, anon_key: str, access_token: str) -> Client:
    """Per-token Supabase client singleton.  Keyed on the token so each
    authenticated user gets their own client; ``access_token=""`` returns the
    anonymous client.

    2026-05-21: schema='autowriter' 让所有 ``client.table("items")`` 等调用
    透明指向 ``autowriter.items``（共享 Supabase + schema 隔离，避免和
    sanshengliubu 在 public 里冲突）。前置条件：autowriter-migrations 已跑
    且 Supabase Dashboard → Settings → API → Exposed schemas 已包含
    ``autowriter``。

    ⚠️ 登出【不要】调 ``.clear()``(审计 SUP-007)。``st.cache_resource`` 是
    **进程级**的 —— clear() 会把所有在线用户的 client 一起清掉, 一个人点登出,
    其他人下一次查询全部重建连接池。而且根本没必要: 这个缓存**按 token 分键**,
    换个人登录本来就拿不到上一个人的 client。登出的人那一份留到 ttl 到期即可。
    """
    client = create_client(
        supabase_url, anon_key,
        options=ClientOptions(schema="autowriter"),
    )
    if access_token:
        client.postgrest.auth(access_token)
    return client


def get_client(access_token: Optional[str] = None) -> Client:
    """Return a Supabase client, optionally authenticated with the user JWT.

    Cached per-token via ``_make_client_cached`` so each Streamlit rerun
    reuses the same Client (and underlying httpx connection pool) instead of
    rebuilding it.  Falls back to a fresh client when Streamlit isn't loaded.
    """
    return _make_client_cached(
        config.SUPABASE_URL, config.SUPABASE_ANON_KEY, access_token or ""
    )


# supabase-py 的子客户端是【惰性】建的: client._postgrest / _storage /
# _functions 在被访问之前都是 None(实测 supabase 2.30)。所以关连接必须走这几个
# **私有**属性 —— 用 client.postgrest 这样的属性访问会把还不存在的子客户端
# **现建一个**出来, 本意是释放描述符, 结果反而多占一个。
_CLOSABLE_SUBCLIENTS = ("_postgrest", "_storage", "_functions")


def close_client(client) -> None:
    """尽力关掉一个一次性 Supabase client 底下的 httpx 连接(审计 ROB-013)。

    supabase-py 没有统一的 ``close()``: postgrest 上叫 ``aclose``(名字带 a,
    实际是同步的), auth 上叫 ``close``, storage / functions 则是各自的
    ``_client`` 是个 httpx.Client。一次性 client 用完不关, 连接和文件描述符
    要等 GC —— 而 ``auth._try_refresh_session`` / ``_try_cookie_restore``
    是每次 rerun 都可能走的路径, 攒起来就是 Too many open files, 表现却是
    "所有查询突然开始超时", 极难往这上面想。

    ⚠️ 只用于 ``auth._fresh_auth_client()`` 那种一次性对象。
    绝不能对 ``get_client()`` 返回的缓存 client 调 —— 那是别人还在用的。

    每个子客户端单独 try: 关不掉一个不该拦住其余的。
    """
    targets = []
    auth = client.__dict__.get("auth")     # auth 是 __init__ 里就建好的
    if auth is not None:
        targets.append(auth)
    for attr in _CLOSABLE_SUBCLIENTS:
        sub = getattr(client, attr, None)  # 已经建过才不是 None; 不触发惰性构造
        if sub is not None:
            targets.append(sub)

    for sub in targets:
        for holder in (sub, getattr(sub, "_client", None), getattr(sub, "session", None)):
            if holder is None:
                continue
            fn = getattr(holder, "close", None) or getattr(holder, "aclose", None)
            if not callable(fn):
                continue
            try:
                res = fn()
                # 万一哪天上游把它改成真的 async: 协程不 await 会留一条
                # "coroutine was never awaited" 警告, 关掉它。
                if hasattr(res, "close"):
                    res.close()
            except Exception:
                pass


def get_service_client() -> Client:
    """构造一个 service_role Supabase client（绕 RLS）。

    **仅供后台 worker 进程（worker.py）使用** —— service_role 能读写所有用户
    的数据, 绝不能在 Streamlit app 路径里调用。需要 ``SUPABASE_SERVICE_ROLE_KEY``
    环境变量（见 config.py）; 未配时抛错而不是静默退化, 避免 worker 拿 anon key
    走 RLS 永远领不到 job 还查不出原因。

    不走 ``_make_client_cached``（那个按 anon_key + access_token 缓存）: service
    client 是 worker 进程级单例, 由 worker.py 持有一份即可。
    """
    key = getattr(config, "SUPABASE_SERVICE_ROLE_KEY", "")
    if not key:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY 未配置 —— worker 无法绕 RLS 领取 job。"
            "请在 worker 主机的环境变量里设置（不要硬编码）。"
        )
    return create_client(
        config.SUPABASE_URL, key,
        options=ClientOptions(schema="autowriter"),
    )


# ── DDL helpers (run once during setup) ───────────────────────────────────

# ── schema 的源头搬去了 migrations/000_baseline.sql(审计 SUP-010)──────────
# 这里原来是一个 1162 行的 Python 字符串 CREATE_TABLES_SQL。搬走的理由不是
# "文件太大", 而是**没有任何代码执行过它, 所以也没有任何东西验证过它** ——
# 修 COR-014 时就发现它里面那份 deskcore_commit_fingerprints 还带着
# migrations/001 早就修好的 bug, 于是每个新环境都会带着一个老 bug 出生。
#
# 现在它是 .sql 文件, CI 会在一个空库上真的跑一遍、再叠 001..005, 两边对不上
# 就当场红。要读 schema 就去看那个文件; 要改就两边都改(增量迁移 + 那份基线)。
# ── 写入返回 0 行的统一处理（审计 COR-010）────────────────────────────────

class WriteReturnedNoRow(RuntimeError):
    """一次 INSERT/UPDATE 期望回一行、实际 0 行。

    PostgREST 对"匹配 0 行"的 UPDATE **不报错**, 只返回空 data(行已被并发删除 /
    被 RLS 拦下 / id 根本不存在)。全库有十余处直接 ``res.data[0]`` ——
    那会抛 ``IndexError: list index out of range``, 在 Streamlit 上表现为一片
    红屏, 既看不出是哪一步、也不知道该怎么办。

    ``update_version_content``(:1291-1299) 早就为同一个问题做过处理, 但只修了
    那一个函数。本异常 + ``_first_row`` 把口径推广到全部写入点。
    """


def _first_row(res, op: str, **ctx) -> dict:
    """取写入返回的第一行; 0 行时抛 ``WriteReturnedNoRow``(带可读上下文)。

    ``res`` 允许为 None —— 调用方的重试循环理论上总会给它赋值, 但真为 None 时
    抛本异常也比 ``AttributeError`` 有用。
    """
    rows = getattr(res, "data", None) or []
    if rows:
        return rows[0]
    ctx_str = " ".join(f"{k}={v}" for k, v in ctx.items() if v)
    telemetry.log_event(
        "write_returned_no_row", op=op,
        **{k: str(v)[:80] for k, v in ctx.items() if v},
    )
    raise WriteReturnedNoRow(
        f"{op} 没有返回任何行（{ctx_str or '无上下文'}）。"
        "常见原因：这行已被并发删除、或当前登录账号无权改它（RLS）。"
        "刷新页面后重试；仍失败请把这条信息发给开发者。"
    )


# ── Project CRUD ──────────────────────────────────────────────────────────

@_cache_data(ttl=60, show_spinner=False)
def list_projects(_client: Client, user_id: str) -> list[dict]:
    res = (
        _client.table("projects")
        .select("*")
        .eq("owner_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return res.data or []


def get_project(client: Client, project_id: str) -> Optional[dict]:
    """按 id 取项目, **不带归属过滤** —— 安全性完全靠 client 身上的 RLS。

    ⚠️ 只在 client 是**用户自己的** Supabase 客户端时才可以用。传 service_role
    客户端进来等于没有任何访问控制(service_role 绕过 RLS), 见下面
    ``get_project_owned`` 的说明。worker 里一律用那个。
    """
    res = (
        client.table("projects")
        .select("*")
        .eq("id", project_id)
        .single()
        .execute()
    )
    return res.data


def get_project_owned(client: Client, project_id: str,
                      user_id: str) -> Optional[dict]:
    """按 id + owner 取项目; 不是这个人的就返回 None。

    为什么必须有这个: worker 拿的是 **service_role** 客户端, 它绕过 RLS。而
    ``jobs`` 的 RLS 策略只约束 ``user_id = auth.uid()``, ``payload`` 是自由
    JSONB —— 任何已登录用户都能插一条 user_id 是自己、payload 里的 project_id
    是**别人**项目的 job。worker 领到之后若用不带过滤的 get_project, 就会读走
    对方的 system_prompt / calibration_notes / 正反例, 并在对方项目下写内容。

    把过滤写进**查询本身**而不是查完再比对: 后者一旦有人漏写一次 if 就又破了,
    前者是这条 SQL 根本回不出别人的行。(同 deskcore 那次 COR-015 的口径。)
    """
    res = (
        client.table("projects")
        .select("*")
        .eq("id", project_id)
        .eq("owner_id", user_id)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def create_project(
    client: Client,
    user_id: str,
    name: str,
    brand: str = "",
    system_prompt: str = "",
    tactics: Optional[list] = None,
    default_params: Optional[dict] = None,
) -> dict:
    data = {
        "name": name,
        "brand": brand,
        "system_prompt": system_prompt,
        "tactics": json.dumps(tactics or []),
        "default_params": json.dumps(default_params or {}),
        "owner_id": user_id,
    }
    res = client.table("projects").insert(data).execute()
    list_projects.clear()
    return _first_row(res, "创建项目", name=name, user_id=user_id)


def _record_schema_drift(missing_cols: list[str]) -> None:
    """R-027: 把列漂移记到 ``st.session_state`` 让主页面渲染一次可见告警。

    之前 ``update_project`` 撞"列不存在"只剥列 + telemetry，用户在 UI 改了
    值、DB 没生效却没有任何提示。这里在有 Streamlit ScriptRunContext 时（即
    UI 主线程）把缺失列塞进 session_state，``app.py`` 顶部统一 pop 出来
    ``st.warning``。worker 线程没有 context，写入会抛 → 被吞掉（那边本来就
    只能靠 telemetry）。
    """
    if not _HAS_ST or not missing_cols:
        return
    try:
        import streamlit as st
        prev = st.session_state.get("_schema_drift_cols") or []
        st.session_state["_schema_drift_cols"] = sorted(set(list(prev) + list(missing_cols)))
    except Exception:
        pass


def update_project(client: Client, project_id: str, updates: dict) -> dict:
    # Serialise JSON fields if passed as Python objects
    for key in ("tactics", "default_params", "reference_files"):
        if key in updates and not isinstance(updates[key], str):
            updates[key] = json.dumps(updates[key])

    # 列缺失精确兜底：用户的 Supabase 部署可能没运行 Day 2 / Day 3 / Day 5 等
    # ALTER TABLE 迁移；这时往 ``queue_strategy`` / ``semantic_dedup_threshold``
    # / ``system_prompt_tone`` 等"新列"里写值会撞 PGRST204
    # "Could not find the 'X' column of 'projects' in the schema cache"。
    # 之前是直接红屏让用户保存不了项目设置；现在剥掉缺失列后重试一次，让
    # name / brand 等核心字段照常保存，新列只是不生效，并埋一行 telemetry
    # 提醒运维去跑迁移。
    _NEW_COLUMNS = (
        "semantic_dedup_threshold", "queue_strategy",
        "system_prompt_tone", "system_prompt_exec",
        "custom_roles", "calibration_notes",
    )
    # R-042: PGRST204 每次只报**一个**缺失列名 —— 旧实现只剥一次且重试不在
    # try 内, 同时缺 ≥2 个新列(未跑 Day2+Day3 迁移)时第二个缺列直接红屏,
    # 恰是这段兜底要避免的结果。改成循环剥列: 每轮捕获 → 识别 → 剥 → 重试,
    # 至多 len(_NEW_COLUMNS) 轮; 非缺列错误原样上抛。
    payload = dict(updates)
    all_missing: list[str] = []
    res = None
    for _ in range(len(_NEW_COLUMNS) + 1):
        try:
            res = (
                client.table("projects")
                .update(payload)
                .eq("id", project_id)
                .execute()
            )
            break
        except Exception as exc:
            msg = str(exc)
            # 只在错误明确指向"列缺失"且涉及已知新列时才剥列重试
            hit_cols = [c for c in _NEW_COLUMNS if c in msg and c in payload]
            if not hit_cols:
                raise
            all_missing.extend(hit_cols)
            telemetry.log_event(
                "update_project_schema_fallback",
                project_id=project_id,
                missing_columns=hit_cols,
                error=msg[:200],
            )
            payload = {k: v for k, v in payload.items() if k not in hit_cols}
            if not payload:
                # 这次写入的全部字段都是"新列"，剥完什么都没了，直接返回当前行
                _record_schema_drift(all_missing)
                cur = client.table("projects").select("*").eq("id", project_id).execute()
                return (cur.data or [{}])[0]
    if all_missing:
        # R-027: 不再静默——把缺失列塞 session_state 让主页面显式告警一次。
        _record_schema_drift(all_missing)
    list_projects.clear()
    return _first_row(res, "保存项目设置", project_id=project_id)


def delete_project(client: Client, project_id: str) -> None:
    client.table("projects").delete().eq("id", project_id).execute()
    list_projects.clear()


class CalibrationCasRpcMissing(RuntimeError):
    """``update_calibration_notes_cas`` RPC 还没部署(migrations/002 没跑)。

    调用方据此退回旧的全文 witness 路径 —— 那条路在笔记短时是好的, 只有超过
    网关 URL 上限才会失败。硬失败会让未迁移的部署彻底学不动, 比现状更糟。
    """


def update_calibration_notes_cas(
    client: Client,
    project_id: str,
    expected_before_text: str,
    notes: str,
) -> Optional[dict]:
    """带 CAS 的 calibration_notes 写入 —— witness 走 md5, 不进 URL(COR-003)。

    返回被更新的行; **返回 None 表示 CAS 冲突**(别的写入抢先改了这一列, 调用方
    应重读重试)。RPC 不存在时抛 ``CalibrationCasRpcMissing``。

    为什么不能沿用 ``.eq("calibration_notes", <全文>)``: PostgREST 把过滤条件放
    在 URL query string 里, 而这一列的软上限是 4000 字。中文 URL-encode 后一个
    字变 9 个字符, 越过网关的请求行上限之后**每一次**自动学习写入都失败, 并被
    上层 ``except Exception: return None`` 吞掉。详见
    ``migrations/002_calibration_cas.sql`` 里那段注释。

    md5 在这里只做变更检测(冲突的代价是多重试一轮), 不是安全用途。
    """
    expected_md5 = hashlib.md5(
        (expected_before_text or "").encode("utf-8"), usedforsecurity=False,
    ).hexdigest()
    try:
        res = client.rpc("update_calibration_notes_cas", {
            "_project_id":   project_id,
            "_expected_md5": expected_md5,
            "_notes":        notes,
        }).execute()
    except Exception as exc:
        msg = str(exc).lower()
        if ("could not find the function" in msg
                or "does not exist" in msg
                or "pgrst202" in msg):
            raise CalibrationCasRpcMissing(str(exc)[:200]) from exc
        raise
    rows = res.data or []
    if not rows:
        return None          # CAS 冲突: 并发已经改过这一列
    # RPC 绕过 db.update_project, 必须自己失效 list_projects(ttl=60)。否则
    # "刚沉淀完笔记就排下一批"时, 队列 worker 从缓存拿旧 project 行拼 prompt,
    # 刚学的笔记看不到 —— 体感"学了没生效"。(R-036 同款理由)
    try:
        list_projects.clear()
    except Exception:
        pass
    return rows[0]


# ── Batch CRUD ─────────────────────────────────────────────────────────────

def create_batch(
    client: Client,
    user_id: str,
    project_id: str,
    tactic: str,
    params: dict,
    ai_engines: list[str],
) -> dict:
    data = {
        "project_id": project_id,
        "tactic": tactic,
        "params": params,
        "ai_engines": ai_engines,
        "user_id": user_id,
    }
    res = client.table("batches").insert(data).execute()
    list_batches.clear()
    return _first_row(res, "创建批次", project_id=project_id, user_id=user_id)


@_cache_data(ttl=30, show_spinner=False)
def list_batches(_client: Client, project_id: str, limit: int = 20) -> list[dict]:
    res = (
        _client.table("batches")
        .select("*")
        .eq("project_id", project_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []


def mark_batch_auto_calibrated(client: Client, batch_id: str) -> bool:
    """把批次标记为"已被太子学习过"。

    审核页全部通过时触发自动调教笔记反思后调用，记一个时间戳到
    ``batches.auto_calibrated_at``。再次打开同一批次时，gate 检查这一列就
    能跳过重复学习，省一次 Claude 调用 + token。

    返回 True 表示写入成功，False 表示失败（列缺失 / 网络）—— 调用方据此
    决定要不要在 UI 上提示一次。
    """
    try:
        client.table("batches").update({
            "auto_calibrated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", batch_id).execute()
        try:
            list_batches.clear()
        except Exception:
            pass
        return True
    except Exception as exc:
        msg = str(exc)
        if "auto_calibrated_at" in msg:
            telemetry.log_event(
                "mark_batch_auto_calibrated_missing_column",
                hint="run additive ALTER on batches.auto_calibrated_at",
            )
        else:
            telemetry.log_event(
                "mark_batch_auto_calibrated_failed",
                batch_id=batch_id, error=msg[:200],
            )
        return False


def delete_batch(client: Client, batch_id: str) -> None:
    """Delete a batch and all its items/versions (cascade order)."""
    # 1. Collect item ids
    items_res = (
        client.table("items")
        .select("id")
        .eq("batch_id", batch_id)
        .execute()
    )
    item_ids = [r["id"] for r in (items_res.data or [])]

    # 2. Delete versions
    if item_ids:
        client.table("versions").delete().in_("item_id", item_ids).execute()

    # 3. Delete items
    client.table("items").delete().eq("batch_id", batch_id).execute()

    # 4. Delete batch
    client.table("batches").delete().eq("id", batch_id).execute()
    list_batches.clear()
    # list_items 也要 invalidate（cache key 含 batch_id；这里宽口径全 clear，
    # cache 体积小成本可忽略）
    try:
        list_items.clear()
    except Exception:
        pass


# ── Item CRUD ──────────────────────────────────────────────────────────────

def create_item(
    client: Client,
    user_id: str,
    batch_id: str,
    ai_review_notes: Optional[str] = None,
) -> dict:
    data: dict[str, Any] = {"batch_id": batch_id, "user_id": user_id}
    if ai_review_notes:
        data["ai_review_notes"] = ai_review_notes
    res = client.table("items").insert(data).execute()
    return _first_row(res, "创建条目", batch_id=batch_id, user_id=user_id)


def delete_items(client: Client, item_ids: list[str]) -> None:
    """按 id 批量删除 items(R-042: _save_batch_results 的孤儿回收用)。

    versions 对 items 是 ON DELETE CASCADE(本场景下 versions 本就没插成功),
    直接删 items 即可。分块防超长 .in_(); 删后清 list_items 缓存。"""
    ids = [i for i in (item_ids or []) if i]
    if not ids:
        return
    for i in range(0, len(ids), 100):
        client.table("items").delete().in_("id", ids[i:i + 100]).execute()
    try:
        list_items.clear()
    except Exception:
        pass


def bulk_create_items(client: Client, rows: list[dict]) -> list[dict]:
    """Insert N item rows in a single round trip; returns the inserted rows
    with their generated ``id`` columns in input order.  Each row should
    carry ``batch_id``, ``user_id``, and optionally ``ai_review_notes``."""
    if not rows:
        return []
    res = client.table("items").insert(rows).execute()
    try:
        list_items.clear()
    except Exception:
        pass
    return res.data or []


def update_version_content(
    client: Client,
    version_id: str,
    title: str,
    body: str,
    keywords: Optional[list] = None,
    token_usage: Optional[dict] = None,
    embedding: Optional[list[float]] = None,
) -> bool:
    """重写一条 version 的内容（用于自动重生场景）。返回主 UPDATE 是否成功。

    只更新传入的字段；id / item_id / version_num / ai_engine / created_at 不动。
    R-034: 此前主 UPDATE 也被 ``except: pass`` 吞掉(docstring 只声称 embedding
    静默)——调用方 ``_try_regen_one`` 按成功继续更新内存去重池, DB 瞬断时
    DB 里仍是旧重复文案而内存认为已替换, 永久分叉且零告警。现在主写失败
    返回 False + telemetry(仍不抛, daemon 线程里不让单条写失败炸整批);
    embedding 子写保持 best-effort, 但主写失败时跳过(不给旧内容配新向量)。
    """
    updates: dict[str, Any] = {"title": title, "body": body}
    if keywords is not None:
        updates["keywords"] = keywords
    if token_usage is not None:
        updates["token_usage"] = token_usage
    ok = True
    try:
        res = client.table("versions").update(updates).eq("id", version_id).execute()
        if not (getattr(res, "data", None) or []):
            # R-034 review #2: PostgREST 对 0 行匹配的 UPDATE 不抛错(行已被
            # 并发删除 / RLS 拦截), 返回空 data —— 同样必须按失败处理, 否则
            # 调用方仍会以"已替换"更新内存去重池, 恰好复现本函数要消灭的
            # DB/内存分叉。
            ok = False
            telemetry.log_event(
                "version_content_update_no_match", version_id=version_id,
            )
    except Exception as exc:
        ok = False
        telemetry.log_event(
            "version_content_update_failed",
            version_id=version_id, error=str(exc)[:200],
        )
    try:
        list_items.clear()
    except Exception:
        pass
    if ok and embedding is not None:
        try:
            client.table("versions").update({"embedding": embedding}).eq("id", version_id).execute()
        except Exception:
            # embedding 列没迁移的部署还能用 —— 保持静默(仅向量缺失, 内容已对)
            pass
    return ok


def update_version_embedding(
    client: Client, version_id: str, vec: list[float]
) -> bool:
    """Persist a 768-dim embedding for a single version row.  Tolerant of
    older deployments where the pgvector column hasn't been added yet —
    fails silently so the generation path doesn't blow up on a missing
    column.  Returns True on success, False on failure."""
    if not vec:
        return False
    try:
        client.table("versions").update({"embedding": vec}).eq("id", version_id).execute()
        return True
    except Exception as exc:
        telemetry.log_event(
            "embedding_row_update_failed",
            version_id=version_id, error=str(exc)[:200],
        )
        return False


def bulk_update_version_embeddings(
    client: Client,
    rows: list[dict],
    failed_sink: Optional[list[str]] = None,
) -> None:
    """Persist many version embeddings in one round trip.  Each row needs
    ``{"id": <version_id>, "embedding": [..]}``.  Falls back to per-row
    UPDATE if upsert isn't allowed by RLS for this user.

    Best-effort: errors are swallowed so an embedding failure never blocks
    the user's batch — but with full telemetry so累积衰减可以被发现.
    ``failed_sink`` 给调用方一个直接拿到本次失败 version_id 列表的入口，
    UI 可以在批次完成后展示"⚠ N 条版本缺少向量"提示。
    """
    if not rows:
        return
    try:
        client.table("versions").upsert(rows).execute()
        return
    except Exception as exc:
        err_msg = str(exc)
        telemetry.log_event(
            "embedding_upsert_fallback",
            count=len(rows), error=err_msg[:200],
        )
        # 如果错误明确指向"列不存在"（pgvector 迁移没跑），不再逐行重试 N 次：
        # 直接把所有 id 标记 failed，省 N-1 次必败的 round trip + 日志风暴。
        low = err_msg.lower()
        if (
            "embedding" in low
            and ("column" in low or "does not exist" in low or "schema" in low)
        ):
            telemetry.log_event(
                "embedding_column_missing",
                hint="run versions.embedding pgvector migration",
            )
            if failed_sink is not None:
                for r in rows:
                    failed_sink.append(r.get("id", ""))
            return
    # Per-row fallback
    for r in rows:
        ok = update_version_embedding(client, r.get("id", ""), r.get("embedding") or [])
        if not ok and failed_sink is not None:
            failed_sink.append(r.get("id", ""))


def _parse_pgvector(val) -> Optional[list[float]]:
    """把 PostgREST 读回的 pgvector 值归一成 ``list[float]``(R-034)。

    PostgREST 对 ``vector(768)`` 列的 JSON 序列化是**字符串** ``"[0.1,...]"``,
    不是数组。此前全库无任何反序列化,下游 ``dedup.cosine_similarity`` 对
    list-vs-str 因长度不等**静默返回 0.0** —— 后果是(a)已 backfill 向量的
    soft 规则在 ``memory.filter_soft_by_relevance`` 里 score=0 全部被丢弃不
    注入;(b)跨批语义查重的 DB 历史池(队列预热 + 重生避重)自上线起 0 命中。
    本函数在 DB 读取边界统一归一: 已是 list 原样返回; None/空/解析失败返回
    None(调用方按"无向量"处理, 与历史行为一致)。
    """
    if val is None or isinstance(val, list):
        return val
    if isinstance(val, str):
        s = val.strip()
        if s.startswith("[") and s.endswith("]"):
            inner = s[1:-1].strip()
            if not inner:
                return None
            try:
                return [float(x) for x in inner.split(",")]
            except ValueError:
                return None
    return None


def _paged_select(build, *, page: int = 1000, hard_cap: Optional[int] = None) -> list[dict]:
    """按 offset 翻页拉全一个查询的结果(审计 COR-008)。

    ``build(offset, limit)`` 必须**每次从 client.table(...) 重新构造** query ——
    postgrest-py 复用同一个 builder 时 ``.order()`` 会追加、``.range()`` 的偏移
    会叠加(``list_items_for_batches`` 的回归用例专门盯着这一点)。

    ⚠️ 终止判据是【空页】而不是【短页】。PostgREST 的 ``db-max-rows`` 会把请求
    钳短: 服务端上限低于 ``page`` 时**每一页都是短页**, 但后面明明还有行 ——
    按短页收工就是静默截断。``list_items_for_batches``(:1657-1665 起) 早就
    为这个坑改过, 但同文件另外三处、以及 deskcore 那处都没跟上, 本函数把
    口径收成一处。代价只是末尾多发一次拿到空页的请求。

    offset 按【实收行数】前进, 不是按 ``page``: 服务端钳短时按 page 跳会直接
    漏掉中间那一段。``hard_cap`` 非空时最多取这么多行。

    与 ``deskcore/store._paged`` 是同一套语义(那边不能 import db 之外的东西,
    保持两份薄实现比引入依赖更合适)。
    """
    out: list[dict] = []
    offset = 0
    while True:
        want = page if hard_cap is None else min(page, hard_cap - len(out))
        if want <= 0:
            break
        rows = build(offset, want).execute().data or []
        out.extend(rows)
        if not rows:
            break
        offset += len(rows)
    return out


def _in_chunks(seq: list, size: int = 100):
    """把 id 列表切成 ≤size 的块, 供 ``.in_()`` 分批查询(R-034)。

    一次塞几百个 UUID 进 ``.in_()`` 会生成 10KB+ 的查询串(414 风险), 且
    结果行数超过 PostgREST max-rows(默认 1000)时**静默截断**。同文件
    ``get_session_committed_item_ids`` 已用同样的分块套路。
    """
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def get_recent_titles_openings_with_embeddings(
    client: Client, project_id: str, limit: int = 150
) -> list[dict]:
    """Like ``get_recent_titles_and_openings`` but also returns the stored
    embedding when present.  Used by the embedding-based dedup path; if a
    historical version doesn't have an embedding yet, the entry's
    ``embedding`` key is None and the caller can decide to skip it or
    backfill on demand.

    Returns ``{"version_id", "title", "opening", "embedding"}`` per item.
    """
    pairs = _collect_recent_canonical_versions(
        client, project_id, limit, with_embedding=True,
    )
    out: list[dict] = []
    for _item, chosen in pairs:
        body = (chosen.get("body") or "").strip()
        first_line = next((ln for ln in body.splitlines() if ln.strip()), "")
        out.append({
            "version_id": chosen.get("id"),
            "title":      (chosen.get("title") or "").strip(),
            "opening":    first_line.strip()[:25],
            # R-034: pgvector 字符串归一成 list[float](见 _parse_pgvector)
            "embedding":  _parse_pgvector(chosen.get("embedding")),
        })
    # R-034: 收集器返回新→旧; 反转成旧→新。消费方(_build_dedup_instruction
    # 取 historical[-20:]、topup 的合并 dict 以尾部优先)都以"尾部=最新"为约定。
    out.reverse()
    return out


def _collect_recent_canonical_versions(
    client: Client,
    project_id: str,
    limit: int,
    with_embedding: bool,
) -> list[tuple[dict, dict]]:
    """分页遍历最近 40 个 batch 的 items(新→旧), 为每个 item 选 canonical
    version(best_version_id 优先, 否则最大 version_num), 跳过无版本/
    ``（解析失败）``占位, **集齐 limit 条有效记录即停**。

    R-034(review #1): 封顶必须发生在**过滤之后** —— 旧的 ``limit*2`` 预过滤
    截断在"最近一段恰是失败批次"(正是触发本轮修复的事故场景)时, 会把窗口
    内更早的有效标题永远挡在池外, 恰好削弱事故项目的去重。分页按需拉取,
    多数情况第一页即集齐; 极端情况也最多遍历完 40-batch 窗口。

    created_at 在 bulk insert 下大量并列(同一语句共享 NOW()), 以 id 作第二
    排序键保证 ``.range()`` 分页不丢行/不重复。返回 (item, chosen_version)
    列表, 顺序新→旧, 长度 ≤ limit。
    """
    batches = list_batches(client, project_id, limit=40)
    if not batches:
        return []
    batch_ids = [b["id"] for b in batches]

    base_fields = "id, item_id, title, body, version_num"
    out: list[tuple[dict, dict]] = []
    page_size = max(limit, 100)
    offset = 0
    embedding_ok = with_embedding
    while len(out) < limit:
        items_res = (
            client.table("items")
            .select("id, best_version_id")
            .in_("batch_id", batch_ids)
            .order("created_at", desc=True)
            .order("id", desc=True)
            .range(offset, offset + page_size - 1)
            .execute()
        )
        items = items_res.data or []
        if not items:
            break
        offset += len(items)
        item_ids = [it["id"] for it in items]

        def _fetch_versions(fields: str) -> list[dict]:
            rows: list[dict] = []
            for chunk in _in_chunks(item_ids):
                # 审计 COR-008: 按 item 数分块**不等于**按行数分块 —— 一块 100
                # 个 item, 只要平均迭代过 10 版就能超过 max-rows(常见 1000),
                # 多出来的 version 静默丢掉, _pick 就可能选不到 best/最新那条。
                # 每块内部再翻页; .order("id") 给翻页一个唯一稳定的次级键。
                rows.extend(_paged_select(
                    lambda off, lim, _c=chunk, _f=fields: (
                        client.table("versions")
                        .select(_f)
                        .in_("item_id", _c)
                        .order("id")
                        .range(off, off + lim - 1)
                    )
                ))
            return rows

        if embedding_ok:
            try:
                version_rows = _fetch_versions(base_fields + ", embedding")
            except Exception:
                # embedding column not yet migrated — 本页及后续页都降级
                embedding_ok = False
                version_rows = _fetch_versions(base_fields)
        else:
            version_rows = _fetch_versions(base_fields)

        versions_by_item: dict[str, list[dict]] = {}
        for v in version_rows:
            versions_by_item.setdefault(v["item_id"], []).append(v)

        for item in items:
            versions = versions_by_item.get(item["id"], [])
            if not versions:
                continue
            chosen = None
            best_vid = item.get("best_version_id")
            if best_vid:
                for v in versions:
                    if v.get("id") == best_vid:
                        chosen = v
                        break
            if not chosen:
                chosen = max(versions, key=lambda v: v.get("version_num", 0))
            title = (chosen.get("title") or "").strip()
            if not title or title == "（解析失败）":
                continue
            out.append((item, chosen))
            if len(out) >= limit:
                break
        # 审计 COR-008: 这里原来还有一句 `if len(items) < page_size: break`。
        # 上面 :1660 已经按【空页】正确收工了, 这句是叠在它之上的【短页】判据 ——
        # 服务端 db-max-rows 低于 page_size(默认 max(limit,100)=150) 时每一页
        # 都是短页, 于是第一页就返回, 跨批去重的历史池被腰斩且无声。
        # 删掉它: 代价是末尾多发一次拿到空页的请求。
    return out


def bulk_create_initial_versions(client: Client, rows: list[dict]) -> list[dict]:
    """Insert a batch of first-version rows in one round trip.  Each row
    should carry ``item_id``, ``ai_engine``, ``title``, ``body``, and
    optionally ``keywords`` / ``token_usage``.

    Used by the batch-generation save path; iteration / manual-edit paths
    still go through ``create_version`` because they need the next available
    version_num for an existing item.

    ⚠️ ``version_num`` 在**每个 item 内**按入参顺序编 1..N, 不是全部写 1。
    (codex review, 2026-08-24 —— 我在 COR-004 里把"同一 item 出现重复
    version_num"当成罕见竞态, 其实**多引擎批次天生就这样**: 一个 item 有几个
    引擎就有几条首版, 原来全部硬编码成 1。migrations/003 装上
    ``UNIQUE(item_id, version_num)`` 之后, 每一个多引擎批次的这次 insert 都会
    撞 23505; 而 app._save_batch_results 的失败路径会把已建的 items 删掉 ——
    于是**整批生成完什么也没存下来**, 用户只看到一行"批量写入 versions 失败"。
    这是本次审计自己引入的、最严重的一处回归。

    编号口径与 migrations/003 的重编号一致(同 item 内按既有顺序从 1 连续排),
    所以历史数据迁移后与新写入的数据是同一套语义。``create_version`` 之后
    自然从 N+1 接着走。

    副作用(可接受): 挑"代表版本"的 ``_pick_version`` 按 max(version_num) 取,
    以前 N 条并列 1 时靠 (created_at, id) 做 tie-break —— 也就是**任意**一条;
    现在会稳定取到入参里的最后一个引擎。从"不确定"变成"确定", 而真正要紧的
    场合用户会在审核页显式指定 best_version_id。"""
    if not rows:
        return []
    seq: dict[str, int] = {}
    payload = []
    for r in rows:
        item_id = r["item_id"]
        seq[item_id] = seq.get(item_id, 0) + 1
        payload.append({
            "item_id":     item_id,
            "version_num": seq[item_id],
            "ai_engine":   r["ai_engine"],
            "title":       r.get("title", ""),
            "body":        r.get("body", ""),
            "keywords":    r.get("keywords") or [],
            "feedback":    r.get("feedback"),
            "images":      r.get("images") or [],
            "token_usage": r.get("token_usage") or {},
        })
    res = client.table("versions").insert(payload).execute()
    # 审计 COR-009: 失效必须在 INSERT **之后**。原来 clear() 写在前面 ——
    # clear 与 insert 之间任何并发读(另一个标签页 / 队列横幅那个 2s 自动刷新
    # 的 fragment)都会把缓存回填成【写入前】的旧快照, 此后 30s 内审核页看不到
    # 刚生成的版本, 而用户只会觉得"生成完了但卡片是空的"。
    try:
        list_items.clear()
    except Exception:
        pass
    return res.data or []


@_cache_data(ttl=30, show_spinner=False)
def list_items(_client: Client, batch_id: str) -> list[dict]:
    """List items + their versions for one batch.

    30s cache：审核页用户连续点"通过/打回/迭代"按钮时不重复查 DB。所有写
    items / versions 的路径都要在写入后调用 ``list_items.clear()``——这种
    write-through invalidation 是手动的，遗漏点会让 UI 显示旧状态。已知调
    用点（必须 clear）：
      - update_item_status                — items.status
      - set_item_example_label            — items.example_label
      - delete_batch                      — cascade 删 items
      - bulk_create_initial_versions      — 新建 versions（影响 list_items 的 versions(*) 嵌套）
      - bulk_create_items                  — 新建 items
      - update_version_content            — versions.title/body/keywords
    cache 失败回原始查询（_cache_data shim 在 no-streamlit 环境是 no-op）。
    """
    res = (
        _client.table("items")
        .select("*, versions(*)")
        .eq("batch_id", batch_id)
        .order("created_at")
        .execute()
    )
    return res.data or []


def list_items_for_batches(
    client: Client, batch_ids: list[str]
) -> dict[str, list[dict]]:
    """Bulk-fetch items for many batches in one round trip.

    Returns ``{batch_id: [items]}``. 给 page_export 这种需要遍历多个 batch
    的入口用，消除 N+1 — 之前每个 batch 一次 query，50 个 batch 就要 50
    次 RT。现在一次 ``in_(batch_ids)`` 拿回全部，client 侧按 batch_id 分桶。

    Streamlit cache 不做这里：export 页交互低频，但 batch_ids 集合频繁
    变化（用户勾选），cache_data 反而命中率低。

    ⚠️ 必须分页。调用方 (app.py page_export) 传的是 list_batches(limit=50) 的
    全部批次，一批最多 20 条 item —— 上界正好 50×20 = 1000，**贴着 PostgREST
    的 max-rows 上限**，零余量。一旦哪天 limit 或单批容量往上挪一格，这里就会
    【静默截断】：导出页少几篇，不报错、不告警，跟"那几篇本来就没写"看起来一样。
    (2026-08-23 审计: 当前最大项目 445 条, 还没撞上, 属于时间问题)

    分页写法照搬本文件 _collect_recent_canonical_versions(:1454) 那套：created_at 作主排序，
    id 作【唯一且稳定】的次级键。少了次级键就不能翻页 —— created_at 在 bulk
    insert 下大量并列(同一语句共享 NOW())，无序 OFFSET 跨页会漏行/重行。
    """
    if not batch_ids:
        return {}
    grouped: dict[str, list[dict]] = {bid: [] for bid in batch_ids}
    page_size = 500
    offset = 0
    while True:
        try:
            res = (
                client.table("items")
                .select("*, versions(*)")
                .in_("batch_id", batch_ids)
                .order("created_at")
                .order("id")
                .range(offset, offset + page_size - 1)
                .execute()
            )
        except Exception as exc:
            telemetry.log_event(
                "list_items_for_batches_failed",
                n_batches=len(batch_ids), offset=offset, error=str(exc)[:200],
            )
            # 首页就失败 → 整体降级为空(维持原语义); 已经取到几页 → 保留已取的,
            # 少几篇总比整页空白强, 且上面已留痕。
            if offset == 0:
                return {bid: [] for bid in batch_ids}
            break
        page = res.data or []
        _bucket_items(page, grouped)
        # ⚠️ 终止判据是【空页】而不是【短页】。PostgREST 的 max-rows 会把请求
        # 钳短: 服务端配的上限低于 page_size 时, 每一页都是"短页"但后面明明还有行。
        # 按 len(page) < page_size 收工 = 又一次静默截断, 正是本函数要治的病。
        # 代价只是末尾多发一次拿到空页的请求。
        # offset 按【实拿到的行数】前进, 不是按 page_size —— 服务端钳短时按
        # page_size 跳会直接漏掉中间那段。(codex aw#57 review)
        if not page:
            break
        offset += len(page)
    return grouped


def _bucket_items(page: list[dict], grouped: dict[str, list[dict]]) -> None:
    """把一页 items 按 batch_id 分桶进 grouped(就地改)。

    只收 grouped 里已有的 batch_id —— 那是调用方问的那批。PostgREST 的
    in_() 不会回别的批次, 这层判断是防御性的, 保持原行为。
    """
    for item in page:
        bid = item.get("batch_id")
        if bid in grouped:
            grouped[bid].append(item)


def update_item_status(
    client: Client, item_id: str, status: str, best_version_id: Optional[str] = None,
    clear_best_version: bool = False,
) -> dict:
    """更新 item 状态; ``best_version_id`` truthy 时一并写入。

    R-036: ``clear_best_version=True`` 显式把 best_version_id 置 NULL ——
    此前全代码库没有任何清除路径(``if best_version_id`` 只在 truthy 时写),
    迭代出新版本后旧的"最佳"指针仍指着老版本, 卡片/导出永远展示旧文。
    仅在未同时传入新 best_version_id 时生效。
    """
    updates: dict[str, Any] = {"status": status}
    if best_version_id:
        updates["best_version_id"] = best_version_id
    elif clear_best_version:
        updates["best_version_id"] = None
    res = client.table("items").update(updates).eq("id", item_id).execute()
    try:
        list_items.clear()
    except Exception:
        pass
    return _first_row(res, "更新条目状态", item_id=item_id, status=status)


def _best_effort_item_patch(
    client: Client, item_id: str, patch: dict, op: str,
) -> bool:
    """给 items 打一个**不阻塞主流程**的补丁; 返回是否真的改到了行。

    审计 ROB-018: 这四个草稿写入原来只 ``except: pass``, 既吞异常也不看
    受影响行数。PostgREST 对匹配 0 行的 UPDATE **不报错**(行已被并发删除 /
    RLS 拦下), 于是"保存草稿"这件事可以从头到尾一次没成功过, 而它存在的
    全部意义就是崩溃后能把用户打的字捞回来 —— 静默失效等于功能不存在。

    仍然不抛(迭代不能被一次草稿写入拖住), 但两种失败都留痕, 可以 grep。
    """
    try:
        res = (
            client.table("items").update(patch).eq("id", item_id).execute()
        )
    except Exception as exc:
        telemetry.log_event(
            "item_draft_patch_failed", op=op, item_id=item_id,
            error=str(exc)[:200],
        )
        return False
    if not (getattr(res, "data", None) or []):
        telemetry.log_event("item_draft_patch_no_match", op=op, item_id=item_id)
        return False
    return True


def save_feedback_draft(client: Client, item_id: str, draft: str) -> None:
    """Persist a user's in-progress iteration feedback before the AI call.
    Used to recover the typed text if anything goes wrong mid-iteration."""
    if not item_id:
        return
    _best_effort_item_patch(
        client, item_id, {"feedback_draft": (draft or "")}, "save_feedback_draft",
    )


def clear_feedback_draft(client: Client, item_id: str) -> None:
    """Clear a previously-saved iteration feedback draft (call on success)."""
    if not item_id:
        return
    _best_effort_item_patch(
        client, item_id, {"feedback_draft": None}, "clear_feedback_draft",
    )


def save_manual_edit_draft(client: Client, item_id: str, payload: dict) -> None:
    """Persist a user's in-progress manual-refine edits (title / body /
    keywords + the version they were editing).  Stored as JSONB so we can
    overwrite atomically; restoration only applies when ``base_version_id``
    matches the currently-displayed version, so switching the "best" pick
    doesn't surface a stale draft against the wrong baseline."""
    if not item_id or not isinstance(payload, dict):
        return
    _best_effort_item_patch(
        client, item_id, {"manual_edit_draft": payload}, "save_manual_edit_draft",
    )


def clear_manual_edit_draft(client: Client, item_id: str) -> None:
    """Drop the manual-refine draft, e.g. after a successful save."""
    if not item_id:
        return
    _best_effort_item_patch(
        client, item_id, {"manual_edit_draft": None}, "clear_manual_edit_draft",
    )


# ── Version CRUD ───────────────────────────────────────────────────────────

# create_version 撞 (item_id, version_num) 唯一约束时的重试参数(审计 COR-004)。
# 并发迭代同一个 item 是低频事件, 3 次足够; 退避很短, 因为冲突方已经写完了。
_VERSION_NUM_RETRIES = 3
_VERSION_NUM_RETRY_DELAY = 0.05  # seconds

def create_version(
    client: Client,
    item_id: str,
    ai_engine: str,
    title: str,
    body: str,
    keywords: Optional[list] = None,
    feedback: Optional[str] = None,
    images: Optional[list] = None,
    token_usage: Optional[dict] = None,
) -> dict:
    """给 item 追加一个版本, ``version_num`` = 当前最大值 + 1。

    审计 COR-004: 这里是「读 max → +1 → 插入」, 而 ``(item_id, version_num)``
    上原来**没有唯一约束**。同一个 item 并发迭代(两个标签页、或"AI 迭代"与
    "手动精修"同时提交)会各自读到同一个 max, 双双写入同一个 version_num。
    后果不是报错而是**排序不确定**: ``list_approved_versions_for_sync`` 和
    ``_pick_version`` 都按 ``max(version_num, created_at, id)`` 挑代表版本,
    并列时挑中哪条要看 tie-break, 于是 best 指针可能指向被覆盖的那一版,
    导出和送模型避重的都是旧文。

    修法两层:
      · migrations/003 加 ``UNIQUE(item_id, version_num)`` —— 让数据库来保证,
        并发写入必有一方撞 23505 而不是两条都进去;
      · 这里捕获唯一冲突后重读 max 重试。唯一约束缺失(迁移没跑)时行为与
        原来完全一致 —— 只是没有那道保护。
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(_VERSION_NUM_RETRIES):
        existing = (
            client.table("versions")
            .select("version_num")
            .eq("item_id", item_id)
            .order("version_num", desc=True)
            .limit(1)
            .execute()
        )
        next_num = (existing.data[0]["version_num"] + 1) if existing.data else 1

        data = {
            "item_id": item_id,
            "version_num": next_num,
            "ai_engine": ai_engine,
            "title": title,
            "body": body,
            "keywords": keywords or [],
            "feedback": feedback,
            "images": images or [],
            "token_usage": token_usage or {},
        }
        try:
            res = client.table("versions").insert(data).execute()
        except Exception as exc:
            last_exc = exc
            if _is_unique_violation(exc) and attempt < _VERSION_NUM_RETRIES - 1:
                # 并发写入抢先占了这个号: 退避一下重读 max 再试
                telemetry.log_event(
                    "create_version_num_conflict",
                    item_id=item_id, version_num=next_num, attempt=attempt + 1,
                )
                time.sleep(_VERSION_NUM_RETRY_DELAY * (attempt + 1))
                continue
            raise
        try:
            list_items.clear()
        except Exception:
            pass
        return _first_row(res, "创建版本", item_id=item_id, version_num=next_num)
    # 控制流到这里说明重试用尽且最后一次是唯一冲突
    assert last_exc is not None
    raise last_exc


def list_versions(client: Client, item_id: str) -> list[dict]:
    res = (
        client.table("versions")
        .select("*")
        .eq("item_id", item_id)
        .order("version_num")
        .execute()
    )
    return res.data or []


def get_latest_version(client: Client, item_id: str) -> Optional[dict]:
    res = (
        client.table("versions")
        .select("*")
        .eq("item_id", item_id)
        .order("version_num", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def get_batch_item_counts(client: Client, batch_ids: list[str]) -> dict:
    """
    Fetch item counts by status for multiple batches in a single query.
    Returns {batch_id: {"total": N, "approved": N, "pending": N, "needs_revision": N}}

    优先走 PG RPC ``batch_item_counts``（服务端 GROUP BY，只回 N 行聚合结果）；
    RPC 不存在（老部署没跑新迁移）或网络错误时 fallback 到 client-side 聚合，
    保证可用性不退化。
    """
    if not batch_ids:
        return {}
    # 路径 A：RPC 服务端聚合
    try:
        res = client.rpc(
            "batch_item_counts", {"batch_ids": batch_ids}
        ).execute()
        rows = res.data or []
        if rows:
            counts: dict[str, dict] = {}
            for row in rows:
                bid = row.get("batch_id")
                if not bid:
                    continue
                counts[bid] = {
                    "total":          int(row.get("total") or 0),
                    "approved":       int(row.get("approved") or 0),
                    "pending":        int(row.get("pending") or 0),
                    "needs_revision": int(row.get("needs_revision") or 0),
                }
            # 即使某些 batch_id 在 items 里完全没行（边界情况），RPC 也不会回
            # 那一行；用 0 填回去保持 caller 看到的语义不变。
            for bid in batch_ids:
                counts.setdefault(bid, {
                    "total": 0, "approved": 0,
                    "pending": 0, "needs_revision": 0,
                })
            return counts
        # rows 为空也是合法结果（所有 batch 都没 item），直接返回 0-填充
        return {
            bid: {"total": 0, "approved": 0, "pending": 0, "needs_revision": 0}
            for bid in batch_ids
        }
    except Exception as exc:
        # 路径 B：fallback —— 老部署 / RPC 函数不存在 / 网络错误
        msg = str(exc)
        telemetry.log_event(
            "batch_item_counts_rpc_fallback", error=msg[:200],
        )
        try:
            res = (
                client.table("items")
                .select("batch_id, status")
                .in_("batch_id", batch_ids)
                .execute()
            )
        except Exception:
            return {}
        counts = {}
        for row in (res.data or []):
            bid = row["batch_id"]
            if bid not in counts:
                counts[bid] = {"total": 0, "approved": 0, "pending": 0, "needs_revision": 0}
            counts[bid]["total"] += 1
            status = row.get("status", "pending")
            if status in counts[bid]:
                counts[bid][status] += 1
        return counts


def get_recent_titles_and_openings(
    client: Client, project_id: str, limit: int = 150
) -> list[dict]:
    """
    Fetch title + first-line opening of each content item across a wide recent
    window, for cross-batch deduplication.

    Covers the last 40 batches and all items regardless of status — rejected/
    pending items still pollute future output if we let the model re-invent the
    same angles.  Each entry is ``{"title": str, "opening": str}`` where opening
    is the first non-empty line of the body, truncated to 25 characters.

    R-034: 经 ``_collect_recent_canonical_versions`` 分页收集 —— 按 created_at
    desc 遍历、**过滤后**集齐 limit 条有效记录即停(预过滤封顶会在"最近一段
    恰是失败批次"时把更早的有效标题挡在池外), versions 分块查防 max-rows
    静默截断。输出旧→新(尾部=最新), 与 _build_dedup_instruction 取
    historical[-20:] 的约定对齐。
    """
    pairs = _collect_recent_canonical_versions(
        client, project_id, limit, with_embedding=False,
    )
    out: list[dict] = []
    for _item, chosen in pairs:
        body = (chosen.get("body") or "").strip()
        first_line = next((ln for ln in body.splitlines() if ln.strip()), "")
        out.append({
            "title":   (chosen.get("title") or "").strip(),
            "opening": first_line.strip()[:25],
        })
    out.reverse()
    return out


def get_recent_titles(client: Client, project_id: str, limit: int = 100) -> list[str]:
    """Backwards-compatible wrapper returning just titles."""
    return [t["title"] for t in get_recent_titles_and_openings(client, project_id, limit=limit)]


# ── Memory CRUD ────────────────────────────────────────────────────────────

def list_memories(
    client: Client,
    user_id: str,
    scope: Optional[str] = None,
    project_id: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    q = client.table("memories").select("*").eq("user_id", user_id)
    if scope:
        q = q.eq("scope", scope)
    if project_id:
        q = q.eq("project_id", project_id)
    if status:
        q = q.eq("status", status)
    res = q.order("frequency", desc=True).execute()
    rows = res.data or []
    # R-034: select("*") 会把 pgvector 的 embedding 列以字符串形态带回;
    # 下游 memory.filter_soft_by_relevance 拿它算 cosine 得 0 分, 已 backfill
    # 向量的 soft 规则会被全部静默过滤掉。在读取边界统一归一成 list[float]。
    for r in rows:
        if "embedding" in r:
            r["embedding"] = _parse_pgvector(r.get("embedding"))
    return rows


def _invalidate_memory_caches() -> None:
    """Drop every memory-related cache after a write so the next read pulls fresh
    rows.  Called from every memory mutator."""
    for fn in (get_confirmed_memories, get_session_instructions, list_example_items):
        try:
            fn.clear()
        except Exception:
            pass


# 进程内 per-content 锁：AI 合并器并发分类反馈时，多个 worker 线程会对同一条
# 规则 (user_id, scope, content_hash, project_id) 同时调 upsert_memory。原先的
# select→update 不原子：两边都读到 freq=5 后都写 freq=6，frequency 计数器丢一次
# 增量；select→insert 同样可双插重复行污染 dedup。schema 上没有 UNIQUE 约束
# （会和历史脏数据冲突无法添加），所以用 app 层的 keyed lock 兜底单进程部署。
# 多 worker / 多 instance 场景仍有残余竞态，但 UPDATE 走 CAS 至少能检测出冲突
# 并重试。
#
# 审计 SUP-008: 这个字典原来**永不删项** —— 每见过一条规则文本就永久留一把
# Lock。规则是用户反馈驱动的、内容各不相同, 长跑进程里它只增不减。
#
# 加上限的做法有个坑必须避开: 【不能淘汰正在被人用的锁】。一旦把某个 key 的
# Lock 换成新对象, 已经拿着旧锁的线程和随后拿到新锁的线程就不再互斥 ——
# 而这把锁存在的全部意义就是互斥。所以这里按【引用计数】淘汰: 只有当前没有
# 任何调用方持有或正准备持有(users == 0)的项才可能被清掉。
#
# 计数在拿锁【之前】就 +1(还没 acquire 时也算"正在用"), 否则会出现这个窗口:
# 线程 A 取到锁对象但还没 acquire → 别的线程触发淘汰 → 线程 C 拿到一把新锁
# → A 和 C 各锁各的。这个竞态很窄, 但后果正是 frequency 丢增量 / 双插重复行,
# 而那正是本锁要防的东西。
_MEMORY_UPSERT_LOCKS: "OrderedDict[str, list]" = OrderedDict()   # key -> [Lock, users]
_MEMORY_UPSERT_LOCKS_GUARD = threading.Lock()
_MEMORY_UPSERT_LOCKS_MAX = 512


@contextlib.contextmanager
def _memory_upsert_lock(key: str):
    """按 key 串行化, 锁池带上限(审计 SUP-008)。"""
    with _MEMORY_UPSERT_LOCKS_GUARD:
        entry = _MEMORY_UPSERT_LOCKS.get(key)
        if entry is None:
            entry = [threading.Lock(), 0]
            _MEMORY_UPSERT_LOCKS[key] = entry
        entry[1] += 1                       # 先占住, 再去 acquire
        _MEMORY_UPSERT_LOCKS.move_to_end(key)
        _evict_idle_memory_locks()
    try:
        with entry[0]:
            yield
    finally:
        with _MEMORY_UPSERT_LOCKS_GUARD:
            entry[1] -= 1


def _evict_idle_memory_locks() -> None:
    """把最久没用、且【当前没人用】的锁清掉。调用方必须已持有 GUARD。

    从 LRU 端往新的方向扫, 跳过 users > 0 的项。全都在用时就不淘汰 ——
    此刻字典确实超上限, 但那是真实并发量, 不是泄漏; 用完自然会掉下来。
    """
    if len(_MEMORY_UPSERT_LOCKS) <= _MEMORY_UPSERT_LOCKS_MAX:
        return
    for k in list(_MEMORY_UPSERT_LOCKS.keys()):
        if len(_MEMORY_UPSERT_LOCKS) <= _MEMORY_UPSERT_LOCKS_MAX:
            break
        if _MEMORY_UPSERT_LOCKS[k][1] == 0:
            del _MEMORY_UPSERT_LOCKS[k]


def upsert_memory(
    client: Client,
    user_id: str,
    scope: str,
    content: str,
    source_feedback: str,
    project_id: Optional[str] = None,
    auto_confirm_threshold: int = 3,
    force_confirmed: bool = False,
    severity: str = "soft",
    update_severity: bool = False,
    applicability: Optional[str] = None,
    rule_kind: Optional[str] = None,
    rule_payload: Optional[dict] = None,
) -> dict:
    """
    Insert a new memory candidate or increment frequency of an existing one.

    ``force_confirmed`` (used by the AI merger) creates the row already in the
    ``confirmed`` state, skipping the frequency threshold — callers that set
    this flag have already decided the rule is intentional.

    ``update_severity`` (2026-08, codex review round-5 P1) —— 命中已存在的
    规则时，是否把 ``severity`` 也写回去。默认 **False**，因为绝大多数调用方
    是自动抽取（``memory.py`` 的 merger / 反馈链），它们传的 severity 是
    **猜的**；打开会让一次自动抽取把用户手动设过的 hard 规则悄悄降回 soft。
    只有【用户明确指定严重级别】的路径才该传 True —— 目前只有 deskcore 的
    ``record_rule``（用户说"以后都这样"并选了 hard/soft）。不传的话，把一条
    已存在的 soft 规则改成 hard 会**返回成功但库里还是 soft**，于是它继续待在
    P1 而不是 P0，用户以为设成硬约束了、其实没有。

    Day 3 新增：``rule_kind`` + ``rule_payload`` 用于结构化硬规则
    （``forbidden_word`` / ``required_phrase`` / ``max_len`` / ``forbidden_regex``）。
    迁移未跑的老部署 insert 失败后会自动 strip 这两列重试，保持向后兼容。

    并发安全（2026-05）：
      - 进程内 keyed lock 串行化同一 (user, scope, content, project_id) 的并发调用
      - UPDATE 路径用 CAS（match old frequency），冲突时重读重试最多 3 次
    """
    lock_key = hashlib.sha256(
        f"{user_id}|{scope}|{project_id or ''}|{content}".encode("utf-8")
    ).hexdigest()
    with _memory_upsert_lock(lock_key):
        return _upsert_memory_locked(
            client, user_id, scope, content, source_feedback,
            project_id=project_id,
            auto_confirm_threshold=auto_confirm_threshold,
            force_confirmed=force_confirmed,
            severity=severity,
            update_severity=update_severity,
            applicability=applicability,
            rule_kind=rule_kind,
            rule_payload=rule_payload,
        )


def _upsert_memory_locked(
    client: Client,
    user_id: str,
    scope: str,
    content: str,
    source_feedback: str,
    project_id: Optional[str] = None,
    auto_confirm_threshold: int = 3,
    force_confirmed: bool = False,
    severity: str = "soft",
    update_severity: bool = False,
    applicability: Optional[str] = None,
    rule_kind: Optional[str] = None,
    rule_payload: Optional[dict] = None,
) -> dict:
    # Try to find an existing memory with the same content
    # scope 隔离很重要：``project_id=None`` 的 global 反馈必须只匹配 project_id IS NULL
    # 的行——之前漏了 IS NULL 过滤，导致 global 反馈会去匹配并 +1 某个项目级同
    # 文本规则的 frequency，scope 边界被打穿。
    def _read_existing() -> Optional[dict]:
        q = (
            client.table("memories")
            .select("*")
            .eq("user_id", user_id)
            .eq("scope", scope)
            .eq("content", content)
        )
        if project_id:
            q = q.eq("project_id", project_id)
        else:
            q = q.is_("project_id", "null")
        rows = q.execute().data or []
        return rows[0] if rows else None

    # CAS UPDATE 循环：每轮读最新 frequency，写时用 .eq("frequency", old) 兜底。
    # 0 行受影响 → 别的并发已经改了这一行，重读重试。
    for attempt in range(3):
        row = _read_existing()
        if row is None:
            break  # 转入下方 INSERT 路径
        old_freq = row["frequency"]
        new_freq = old_freq + 1
        if force_confirmed or new_freq >= auto_confirm_threshold:
            new_status = "confirmed"
        else:
            new_status = row["status"]
        # update_severity 打开时把 severity 一起写回 —— 否则把已存在的 soft
        # 规则提升成 hard 会"返回成功但库里还是 soft"，那条规则继续待在 P1
        # 而不是 P0（codex review round-5 P1）。默认关闭的理由见公开签名文档。
        patch = {"frequency": new_freq, "status": new_status}
        if update_severity:
            patch["severity"] = severity
        res = (
            client.table("memories")
            .update(patch)
            .eq("id", row["id"])
            .eq("frequency", old_freq)  # CAS：仅当 frequency 未变时才写
            .execute()
        )
        if res.data:
            _invalidate_memory_caches()
            return res.data[0]
        # 冲突：别的 worker 抢先改了 frequency，下一轮重读
        telemetry.log_event(
            "upsert_memory_cas_retry",
            attempt=attempt + 1, scope=scope,
            content_preview=content[:60],
        )
    else:
        # 3 次 CAS 都冲突，best-effort 强写一次避免完全失败
        telemetry.log_event(
            "upsert_memory_cas_exhausted",
            scope=scope, content_preview=content[:60],
        )
        latest = _read_existing()
        if latest is not None:
            new_freq = latest["frequency"] + 1
            new_status = (
                "confirmed" if force_confirmed or new_freq >= auto_confirm_threshold
                else latest["status"]
            )
            patch = {"frequency": new_freq, "status": new_status}
            if update_severity:
                patch["severity"] = severity
            res = (
                client.table("memories")
                .update(patch)
                .eq("id", latest["id"])
                .execute()
            )
            _invalidate_memory_caches()
            return res.data[0] if res.data else latest
        # 兜底落空（不该发生），继续走 INSERT

    # INSERT 路径：row 为 None，需要创建新规则
    data: dict[str, Any] = {
        "scope": scope,
        "content": content,
        "source_feedback": source_feedback,
        "user_id": user_id,
        "frequency": 1,
        "status": "confirmed" if force_confirmed else "candidate",
    }
    if project_id:
        data["project_id"] = project_id
    # Severity / applicability are new columns added by the additive
    # migration block in migrations/000_baseline.sql.  Try with them first; if the
    # column doesn't exist yet (older deployment), retry without so the
    # write still succeeds and the row degrades to "soft / global".
    if severity and severity.lower() in ("hard", "soft"):
        data["severity"] = severity.lower()
    if applicability:
        data["applicability"] = applicability[:32]
    if rule_kind and rule_kind in (
        "forbidden_word", "required_phrase", "max_len",
        "forbidden_regex", "free_text",
    ):
        data["rule_kind"] = rule_kind
    if rule_payload is not None:
        data["rule_payload"] = rule_payload

    # Compute the embedding once at write time so the relevance ranker
    # can use it without paying an API call per generation.  Only soft
    # rules are filtered; hard rules always inject, but we still embed
    # so the data is uniform.  Embedding failure is non-fatal.
    try:
        import dedup as _dedup
        if _dedup.embeddings_available():
            vecs = _dedup.embed_texts([content])
            if vecs and vecs[0]:
                data["embedding"] = vecs[0]
    except Exception as exc:
        # 之前 silent pass。本路径非致命（规则没向量也能注入），但持续
        # 失败会导致 soft-rule 相关性筛选完全降级为"全部注入"——用户
        # 看到 system_prompt 暴涨却不知所以。埋一行让运维能查。
        telemetry.log_event(
            "memory_embedding_compute_failed",
            content_preview=content[:60],
            error=str(exc)[:200],
        )

    try:
        res = client.table("memories").insert(data).execute()
    except Exception as exc:
        # 仅在错误明确指向"新列缺失"（未跑迁移）时才剥列重试；其它错误
        # 抛回去让调用方/UI 看见。之前裸 except 会把 RLS 拒绝、唯一冲突、
        # 网络中断都当成"老部署"，导致 rule_kind / rule_payload 静默丢失。
        msg = str(exc)
        new_cols = ("severity", "applicability", "embedding",
                    "rule_kind", "rule_payload")
        if not any(col in msg for col in new_cols):
            raise
        telemetry.log_event(
            "upsert_memory_schema_fallback",
            error=msg[:200],
        )
        for col in new_cols:
            data.pop(col, None)
        res = client.table("memories").insert(data).execute()
    _invalidate_memory_caches()
    return _first_row(res, "写入记忆", scope=scope, content=content[:60])


def insert_calibration_audit(
    client: Client,
    project_id: str,
    source: str,
    before_text: str,
    append_lines: list[str],
    after_text: str,
) -> None:
    """记录一次调教笔记的写入。失败时静默——审计不能拖死主流程。

    旧部署没运行新表迁移时，会被 ``except`` 接住静默丢弃，调用方无感。
    """
    if not project_id:
        return
    try:
        client.table("calibration_note_audit").insert({
            "project_id":   project_id,
            "source":       (source or "unknown")[:32],
            "before_text":  before_text or "",
            "append_lines": append_lines or [],
            "after_text":   after_text or "",
        }).execute()
    except Exception:
        pass


def list_calibration_audit(
    client: Client,
    project_id: str,
    limit: int = 30,
    before_ts: Optional[str] = None,
) -> list[dict]:
    """查看某个项目调教笔记的写入历史（UI 排障用）。

    ``before_ts`` 给分页用：传入上一页最旧一条的 ``created_at`` 字符串，
    返回结果会严格早于该时间戳。不传则返回最新 ``limit`` 条。
    """
    if not project_id:
        return []
    try:
        q = (
            client.table("calibration_note_audit")
            .select("*")
            .eq("project_id", project_id)
            .order("created_at", desc=True)
            .limit(limit)
        )
        if before_ts:
            q = q.lt("created_at", before_ts)
        res = q.execute()
        return res.data or []
    except Exception:
        return []


def insert_batch_metrics(
    client: Client,
    batch_id: str,
    project_id: str,
    user_id: str,
    phase_ms: dict,
    counters: dict,
    meta: dict,
    injection: dict,
) -> None:
    """落一条本批次的指标快照到 ``batch_metrics`` 表。

    Day 5：批次完成后调用，让历史页可以离线查"哪一批慢/重/违规多"，
    不依赖刷 stdout 日志。失败不抛——埋点掉链子不能拖死生成主流程。
    """
    if not batch_id:
        return
    try:
        client.table("batch_metrics").insert({
            "batch_id":   batch_id,
            "project_id": project_id,
            "user_id":    user_id,
            "phase_ms":   phase_ms or {},
            "counters":   counters or {},
            "meta":       meta or {},
            "injection":  injection or {},
        }).execute()
    except Exception as exc:
        telemetry.log_event(
            "batch_metrics_persist_failed",
            batch_id=batch_id, error=str(exc)[:200],
        )


def record_user_login(
    client: Client,
    user_id: str,
    ip: Optional[str],
    user_agent: Optional[str],
) -> None:
    """登录成功后落一行到 user_logins，用于"一号多人共享"检测。

    失败静默（不能因为审计写入失败把登录流程拖死）。client 需要带新签发
    的 JWT —— RLS 用 ``auth.uid() = user_id`` 校验。
    """
    if not user_id:
        return
    try:
        client.table("user_logins").insert({
            "user_id":    user_id,
            "ip":         ip or None,
            "user_agent": (user_agent or "")[:500] or None,
        }).execute()
    except Exception as exc:
        telemetry.log_event(
            "user_login_persist_failed",
            user_id=user_id, error=str(exc)[:200],
        )


@_cache_data(ttl=60, show_spinner=False)
def list_batch_metrics(
    _client: Client, project_id: str, limit: int = 50,
    user_id: Optional[str] = None,
) -> list[dict]:
    """读最近 N 条批次指标。历史页用，60s 缓存避免重复查询。

    ``user_id`` 仅作为缓存 key 用（RLS 已经按行过滤）。不传也能用，但同
    一会话内多账户切换时可能拿到上一个账户缓存的结果——所以建议传。
    """
    if not project_id:
        return []
    try:
        res = (
            _client.table("batch_metrics")
            .select("*")
            .eq("project_id", project_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception:
        return []


def count_calibration_audit(client: Client, project_id: str) -> int:
    """项目调教笔记历史的总条数。UI 用于显示 "X / Y 条" 标签。"""
    if not project_id:
        return 0
    try:
        res = (
            client.table("calibration_note_audit")
            .select("id", count="exact")
            .eq("project_id", project_id)
            .limit(1)
            .execute()
        )
        return int(getattr(res, "count", 0) or 0)
    except Exception:
        return 0


def backfill_memory_embeddings(
    client: Client, user_id: str, max_rows: int = 50
) -> dict:
    """Best-effort backfill: find up to ``max_rows`` memories owned by
    ``user_id`` that have no embedding stored, compute them, and write back.

    Returns a status dict instead of a bare int so UI can tell apart these
    scenarios that all used to collapse to "0":
      - ``{"status": "ok", "updated": N}``        — 正常补算了 N 条
      - ``{"status": "noop"}``                    — 没有缺向量的记忆需要补
      - ``{"status": "no_embedding_sdk"}``        — 未配 GOOGLE_API_KEY
      - ``{"status": "schema_missing", "hint": …}`` — pgvector 列没建 / 迁移未跑
      - ``{"status": "query_failed", "error": …}`` — 网络 / RLS / 其它

    之前一律返回 int 0，按钮点了显示"没有需要补算的记忆"既包含真正无需补的
    情况，也包含 schema 缺失等真错误，用户无从判断为什么"没反应"。
    """
    try:
        import dedup as _dedup
    except Exception as exc:
        return {"status": "no_embedding_sdk", "error": str(exc)[:200]}
    if not _dedup.embeddings_available():
        return {"status": "no_embedding_sdk"}
    try:
        res = (
            client.table("memories")
            .select("id, content, embedding")
            .eq("user_id", user_id)
            .is_("embedding", "null")
            .limit(max_rows)
            .execute()
        )
    except Exception as exc:
        msg = str(exc)
        telemetry.log_event(
            "backfill_memory_embeddings_query_failed",
            user_id=user_id, error=msg[:200],
        )
        low = msg.lower()
        if (
            "embedding" in low
            and ("column" in low or "does not exist" in low or "schema" in low)
        ):
            return {
                "status": "schema_missing",
                "hint": "memories.embedding 列不存在，需要先跑 pgvector 迁移",
                "error": msg[:200],
            }
        return {"status": "query_failed", "error": msg[:200]}
    rows = res.data or []
    if not rows:
        return {"status": "noop"}
    texts = [r.get("content", "") for r in rows]
    vecs = _dedup.embed_texts(texts)
    if not vecs or len(vecs) != len(rows):
        return {"status": "query_failed", "error": "embed_texts 返回长度不匹配"}
    updated = 0
    for r, v in zip(rows, vecs):
        if not v:
            continue
        try:
            client.table("memories").update({"embedding": v}).eq("id", r["id"]).execute()
            updated += 1
        except Exception as exc:
            telemetry.log_event(
                "backfill_memory_row_failed",
                memory_id=r.get("id"), error=str(exc)[:200],
            )
    if updated:
        _invalidate_memory_caches()
    return {"status": "ok", "updated": updated}


def increment_memory_frequency(client: Client, memory_id: str) -> dict:
    """
    Bump an existing memory's frequency counter and mark it confirmed.  Used
    by the AI merger's ``merge`` path when a new feedback is deemed semantically
    equivalent to an existing rule.

    用乐观并发（CAS）替代原本的 read-then-write：UPDATE 时 WHERE 同时匹配旧
    frequency，0 行受影响说明被别人抢先 +1 了，重读重试。这样队列 worker / UI
    同时给同一规则反馈不会丢更新——之前两个连接都读到 freq=5、都写 6 会少
    一次增量，``MEMORY_AUTO_CONFIRM_THRESHOLD=3`` 这种小阈值下"自动确认"行为
    被悄悄拖慢。
    """
    MAX_CAS_RETRIES = 5
    for _attempt in range(MAX_CAS_RETRIES):
        row = (
            client.table("memories").select("*").eq("id", memory_id).execute()
        )
        if not row.data:
            raise ValueError(f"memory {memory_id} not found")
        cur = row.data[0]
        cur_freq = cur.get("frequency", 1)
        res = (
            client.table("memories")
            .update({"frequency": cur_freq + 1, "status": "confirmed"})
            .eq("id", memory_id)
            .eq("frequency", cur_freq)  # CAS：旧值变了 → 0 行受影响 → 重试
            .execute()
        )
        if res.data:
            _invalidate_memory_caches()
            return res.data[0]
        # CAS 失败：被别的事务抢先；下一次循环重读再试
    # 重试上限——极端并发或行被删时落到这里。回退到无 CAS 的最后一次写入，
    # 至少保证 frequency 不倒退（写入值取最新读到的 +1）。
    telemetry.log_event(
        "memory_increment_cas_exhausted",
        memory_id=memory_id, retries=MAX_CAS_RETRIES,
    )
    row = client.table("memories").select("*").eq("id", memory_id).execute()
    if not row.data:
        raise ValueError(f"memory {memory_id} not found")
    cur = row.data[0]
    res = (
        client.table("memories")
        .update({"frequency": cur.get("frequency", 1) + 1, "status": "confirmed"})
        .eq("id", memory_id)
        .execute()
    )
    _invalidate_memory_caches()
    return _first_row(res, "累加记忆频次", memory_id=memory_id)


def update_memory(client: Client, memory_id: str, updates: dict) -> dict:
    res = (
        client.table("memories").update(updates).eq("id", memory_id).execute()
    )
    _invalidate_memory_caches()
    return _first_row(res, "更新记忆", memory_id=memory_id)


def delete_memory(client: Client, memory_id: str) -> None:
    client.table("memories").delete().eq("id", memory_id).execute()
    _invalidate_memory_caches()


def set_item_example_label(
    client: Client, item_id: str, label: Optional[str]
) -> dict:
    """Set or clear the example_label on an item ('positive', 'negative', or None).

    同时清掉 list_example_items / list_items / list_labeled_items 三处
    cache，让"审核与迭代"卡片、Memory Manager 的"已确认"列表、注入路径
    都立刻反映新状态。
    """
    res = (
        client.table("items")
        .update({"example_label": label})
        .eq("id", item_id)
        .execute()
    )
    for fn in (list_example_items, list_items, list_labeled_items):
        try:
            fn.clear()
        except Exception:
            pass
    return _first_row(res, "标记正负例", item_id=item_id, label=label)


@_cache_data(ttl=120, show_spinner=False)
def list_example_items(
    _client: Client, project_id: str, label: str, limit: int = 5
) -> list[dict]:
    """
    Return recent items marked with the given label ('positive' or 'negative').
    Each dict has {title, body} from the item's best or latest version.

    2026-05-21：原实现取最近 50 个 batch 再 ``in_(batch_ids)`` 过滤，TV
    同步进来的 special-batch 一旦滚出 50-batch 窗口就读不到——飞轮中断。
    改用 PostgREST embedded inner join：``batches!inner(project_id)`` 让
    外层 items 行按 batches.project_id 直接过滤，不依赖窗口位置。
    """
    res = (
        _client.table("items")
        .select(
            "id, best_version_id, created_at, "
            "versions(id, title, body, version_num), "
            "batches!inner(project_id)"
        )
        .eq("batches.project_id", project_id)
        .eq("example_label", label)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )

    examples: list[dict] = []
    for item in (res.data or []):
        item_versions = item.get("versions", [])
        if not item_versions:
            continue
        best_vid = item.get("best_version_id")
        chosen = None
        if best_vid:
            for v in item_versions:
                if v.get("id") == best_vid:
                    chosen = v
                    break
        if not chosen:
            chosen = max(item_versions, key=lambda v: v.get("version_num", 0))
        title = (chosen.get("title") or "").strip()
        body = (chosen.get("body") or "").strip()
        if title or body:
            examples.append({"title": title, "body": body})
    return examples


# ── 负例候选审核（example_label_proposal）─────────────────────────────────
# TV 飞轮把候选负例写到 items.example_label_proposal，用户在 Memory Manager
# 的"负例候选审核" tab 人工确认才升级为 example_label='negative'。这样
# build_system_prompt 只读 example_label，候选不会污染 prompt 池。

@_cache_data(ttl=30, show_spinner=False)
def list_negative_proposals(
    _client: Client, project_id: str, limit: int = 50,
) -> list[dict]:
    """List items with a pending example_label_proposal in this project.

    Returns list of dicts with: item_id, title, body, proposal (3 个负例来源
    label), batch_id, created_at. 用 batches!inner 跨所有 batch 取，不依赖
    最近 N batch 窗口（同 list_example_items 的设计）。
    """
    res = (
        _client.table("items")
        .select(
            "id, best_version_id, created_at, batch_id, "
            "example_label_proposal, "
            "versions(id, title, body, version_num), "
            "batches!inner(project_id, tactic)"
        )
        .eq("batches.project_id", project_id)
        .not_.is_("example_label_proposal", "null")
        .is_("example_label", "null")  # 已经确认为 example 的不再列在候选里
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )

    out: list[dict] = []
    for item in (res.data or []):
        versions = item.get("versions") or []
        if not versions:
            continue
        best_vid = item.get("best_version_id")
        chosen = next((v for v in versions if v.get("id") == best_vid), None) \
            or max(versions, key=lambda v: v.get("version_num", 0))
        out.append({
            "item_id":  item.get("id"),
            "batch_id": item.get("batch_id"),
            "tactic":   (item.get("batches") or {}).get("tactic"),
            "title":    (chosen.get("title") or "").strip(),
            "body":     (chosen.get("body") or "").strip(),
            "proposal": item.get("example_label_proposal"),
            "created_at": item.get("created_at"),
        })
    return out


def confirm_negative_proposal(client: Client, item_id: str) -> dict:
    """User 在 UI 上点"确认为负例"：写 example_label='negative'，清空 proposal。

    清四份 cache：
    - list_example_items     注入路径要立刻看到新增的负例
    - list_negative_proposals 审核 tab 要把这条移出"待审核"段
    - list_items             审核与迭代页的卡片要立刻显示新 label
    - list_labeled_items     "已确认 example 池"段要立刻看到新进池的这条
                              （30s TTL 不清会让用户 rerun 后看不到自己刚
                              确认的 item，flow 看起来不一致）
    """
    res = (
        client.table("items")
        .update({"example_label": "negative", "example_label_proposal": None})
        .eq("id", item_id)
        .execute()
    )
    for fn in (list_example_items, list_negative_proposals, list_items, list_labeled_items):
        try:
            fn.clear()
        except Exception:
            pass
    return (res.data or [{}])[0]


def dismiss_negative_proposal(client: Client, item_id: str) -> dict:
    """User 点"驳回"：只清空 proposal，不写 example_label。

    清 list_negative_proposals cache 让审核 tab 立刻把这条移出。
    """
    res = (
        client.table("items")
        .update({"example_label_proposal": None})
        .eq("id", item_id)
        .execute()
    )
    try:
        list_negative_proposals.clear()
    except Exception:
        pass
    return (res.data or [{}])[0]


@_cache_data(ttl=30, show_spinner=False)
def list_labeled_items(
    _client: Client, project_id: str, limit: int = 50,
) -> list[dict]:
    """List items already in the example pool (example_label IS NOT NULL).

    给 Memory Manager 的"已确认 example 管理"用，让用户能撤销 / 切换标签。
    返回 item_id 等管理需要的字段，跟 list_example_items 区分（后者只供
    prompt 注入用，没必要带 id）。
    """
    res = (
        _client.table("items")
        .select(
            "id, batch_id, best_version_id, created_at, example_label, "
            "versions(id, title, body, version_num), "
            "batches!inner(project_id, tactic)"
        )
        .eq("batches.project_id", project_id)
        .not_.is_("example_label", "null")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )

    out: list[dict] = []
    for item in (res.data or []):
        versions = item.get("versions") or []
        if not versions:
            continue
        best_vid = item.get("best_version_id")
        chosen = next((v for v in versions if v.get("id") == best_vid), None) \
            or max(versions, key=lambda v: v.get("version_num", 0))
        out.append({
            "item_id":  item.get("id"),
            "batch_id": item.get("batch_id"),
            "tactic":   (item.get("batches") or {}).get("tactic"),
            "title":    (chosen.get("title") or "").strip(),
            "body":     (chosen.get("body") or "").strip(),
            "label":    item.get("example_label"),
            "created_at": item.get("created_at"),
        })
    return out


def _is_rule_memory(row: dict) -> bool:
    """True if a memory row should be treated as a durable rule (default for
    rows predating the ``memory_type`` column)."""
    mt = row.get("memory_type")
    return mt is None or mt == "rule"


def _rank_memories_for_injection(mems: list[dict], cap: int) -> list[dict]:
    """Rank rule memories for System Prompt injection.

    Two-tier selection so that fresh captures — especially the frequency=1
    rules created by the merger's ``rule`` path — are never starved by a
    backlog of older high-frequency rules:

      1. Rules created within the last 7 days are always included (newest
         first).  This guarantees "I just said it → it took effect".
      2. Remaining slots (cap - len(recent)) are filled from older rules by
         ``frequency DESC`` then ``created_at DESC``.

    Hard ceiling at ``cap``: in the pathological case where the user creates
    more than ``cap`` rules in a week, we still truncate (newest kept).
    """
    if not mems:
        return mems
    if not cap or cap <= 0:
        return sorted(
            mems,
            key=lambda m: (int(m.get("frequency") or 0), str(m.get("created_at") or "")),
            reverse=True,
        )

    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    recent: list[dict] = []
    older: list[dict] = []
    for m in mems:
        created = str(m.get("created_at") or "")
        (recent if created >= cutoff else older).append(m)

    recent.sort(key=lambda m: str(m.get("created_at") or ""), reverse=True)
    older.sort(
        key=lambda m: (int(m.get("frequency") or 0), str(m.get("created_at") or "")),
        reverse=True,
    )

    recent = recent[:cap]
    remaining = max(0, cap - len(recent))
    return recent + older[:remaining]


@_cache_data(ttl=60, show_spinner=False)
def get_confirmed_memories(
    _client: Client,
    user_id: str,
    project_id: Optional[str] = None,
    cap_per_scope: Optional[int] = None,
) -> tuple[list[dict], list[dict]]:
    """
    Returns (global_memories, project_memories), both filtered to 'confirmed'.

    Hard rules (``severity='hard'``) bypass ``cap_per_scope`` and are always
    injected in full — they're compliance / brand lines.  Soft rules are
    ranked (recent first, then frequency) and capped per scope.

    Session-typed memories are excluded — they load through
    :func:`get_session_instructions` into a separate high-priority prompt slot.

    Rows with a future ``muted_until`` are filtered out so the user can
    temporarily silence a rule without deleting it.
    """
    if cap_per_scope is None:
        cap_per_scope = int(getattr(config, "MAX_INJECTED_MEMORIES_PER_SCOPE", 12) or 12)

    def _split_and_rank(mems: list[dict]) -> list[dict]:
        rule_mems = [m for m in mems if _is_rule_memory(m) and not _is_muted(m)]
        hard = [m for m in rule_mems if (m.get("severity") or "soft").lower() == "hard"]
        soft = [m for m in rule_mems if (m.get("severity") or "soft").lower() != "hard"]
        return hard + _rank_memories_for_injection(soft, cap_per_scope)

    global_mems = _split_and_rank(
        list_memories(_client, user_id, scope="global", status="confirmed")
    )
    project_mems: list[dict] = []
    if project_id:
        project_mems = _split_and_rank(
            list_memories(
                _client, user_id, scope="project",
                project_id=project_id, status="confirmed",
            )
        )
    return global_mems, project_mems


def parse_ts(value) -> Optional[datetime]:
    """把 PG 回来的时间戳统一解析成 **aware UTC** datetime; 解析不了返回 None。

    容忍四种真实形态:
      - ``datetime`` 对象(aware 或 naive; naive 按 UTC 解释)
      - ISO 串带 ``+00:00``
      - ISO 串带 ``Z``
      - ISO 串**无时区后缀**(老数据), 以及 7 位微秒(某些 PG client 会这么回,
        而 ``fromisoformat`` 只吃到 6 位)

    ⚠️ 时间戳一律走这里, 不要在别处再写一份。用字符串字典序比较 ISO 串是
    本仓反复踩过的坑: naive 与 aware 混排会判反(``+`` 的码位小于任何数字),
    带微秒与不带微秒混排也会差一秒(``.`` 的码位大于 ``+``)。COR-007 就是
    deskcore 自己写了一份判定, 对 naive 输入抛异常后 fail-open 成"没静音"。
    """
    if not value:
        return None
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            s = str(value).strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(s)
            except ValueError:
                # 截掉超出 6 位的小数秒后重试
                dt = datetime.fromisoformat(re.sub(r"\.(\d{6})\d+", r".\1", s))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def is_memory_muted_now(muted_until) -> bool:
    """True iff ``muted_until`` (raw column value) is in the future, UTC.

    历史上 db._is_muted 和 memory.py 的 UI 各持一份字符串字典序比较，且写入
    端用 aware ISO（``...+00:00``）而读取端用 naive ISO（无 tz 后缀）。当
    两个字符串前缀相同时 ``+`` (43) < 任何数字 → aware 字符串恒大于 naive，
    边界条件下静音的"刚到期"瞬间会判错。

    本函数把 ``muted_until`` 统一解析为 aware UTC datetime 后用 datetime
    比较，对以下输入都鲁棒：
      - datetime 对象（aware 或 naive，naive 默认按 UTC 解释）
      - ISO 字符串带 ``+00:00`` 或 ``Z``
      - ISO 字符串无 tz 后缀（兼容老数据）

    Failure-safe：解析失败一律返回 False（"未静音"）—— 用户看到一条规则
    生效，比"明明设置静音但不生效"的反向 bug 影响小。
    """
    mu = parse_ts(muted_until)
    if mu is None:
        return False
    return mu > datetime.now(timezone.utc)


def _is_muted(memory_row: dict) -> bool:
    """Backcompat shim — delegate to ``is_memory_muted_now``.

    保留旧名字让 db.py 内部其它调用点（如 ``get_confirmed_memories``）继续
    工作，新代码应直接用 ``is_memory_muted_now``。
    """
    return is_memory_muted_now(memory_row.get("muted_until"))


@_cache_data(ttl=30, show_spinner=False)
def get_session_instructions(
    _client: Client,
    user_id: str,
    project_id: Optional[str] = None,
) -> list[dict]:
    """
    Fetch unexpired session-level instructions for the current user/project.

    Session memories carry ad-hoc instructions that the user gave mid-flow
    ("from now on avoid numbers in titles") — they live outside the frequency/
    candidate/confirmed pipeline and inject at the highest priority of the
    system prompt.  Returns [] if the ``memory_type`` column isn't present yet
    (i.e. schema migration hasn't been run), so the feature degrades cleanly.
    """
    try:
        q = (
            _client.table("memories")
            .select("*")
            .eq("user_id", user_id)
            .eq("memory_type", "session")
        )
        if project_id:
            # R-040: 全文件唯一一处 f-string 拼 PostgREST filter(其余都是参数化
            # .eq)。project_id 当前全部来自 DB 返回的 UUID, 但作为防御边界先
            # 真解析一次 —— 解析失败按"无项目过滤"处理(只取 global 会话指令),
            # 不让构造串改写查询语义。
            try:
                import uuid as _uuid
                _pid = str(_uuid.UUID(str(project_id)))
                q = q.or_(f"project_id.eq.{_pid},project_id.is.null")
            except (ValueError, AttributeError, TypeError):
                q = q.is_("project_id", "null")
        res = q.order("created_at", desc=True).execute()
    except Exception:
        return []

    rows = res.data or []
    # 用 timezone-aware 比对：之前 ``datetime.utcnow()`` 返回 naive，而 PG 的
    # TIMESTAMPTZ 字符串带 ``+00:00`` 偏移，按字典序字符串比较在边界微秒/格式
    # 略差时不可靠（最坏会让已过期的 session_instruction 仍然注入 prompt，
    # 24h TTL 名存实亡）。改成解析为 aware datetime 后比较。
    now_aware = datetime.now(timezone.utc)
    fresh: list[dict] = []
    for row in rows:
        expires = row.get("expires_at")
        if not expires:
            fresh.append(row)
            continue
        try:
            # PG 常见格式：``2026-05-20T11:00:00+00:00`` 或带 ``.123456``。
            # fromisoformat 在 3.11+ 接受 ``Z`` 后缀，3.10 不支持 → 兜底替换。
            expires_aware = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            if expires_aware.tzinfo is None:
                expires_aware = expires_aware.replace(tzinfo=timezone.utc)
        except Exception:
            # 解析失败时保守保留——比误删用户当前会话的临时指令体感好
            fresh.append(row)
            continue
        if expires_aware < now_aware:
            continue
        fresh.append(row)
    return fresh


def insert_session_instruction(
    client: Client,
    user_id: str,
    content: str,
    source_feedback: str = "",
    project_id: Optional[str] = None,
    source_batch_id: Optional[str] = None,
    ttl_hours: int = 24,
) -> Optional[dict]:
    """
    Insert a session-level memory row.  Returns None if the schema migration
    hasn't run yet (so callers can silently skip the feature).
    """
    from datetime import timedelta
    # timezone-aware：写入端与 ``get_session_instructions`` 的过期判定保持同口径
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=max(1, ttl_hours))).isoformat()
    payload: dict[str, Any] = {
        "scope": "project" if project_id else "global",
        "content": content.strip(),
        "source_feedback": source_feedback or "会话指令",
        "user_id": user_id,
        "frequency": 1,
        "status": "confirmed",
        "memory_type": "session",
        "expires_at": expires_at,
    }
    if project_id:
        payload["project_id"] = project_id
    if source_batch_id:
        payload["source_batch_id"] = source_batch_id
    try:
        res = client.table("memories").insert(payload).execute()
        _invalidate_memory_caches()
        return (res.data or [None])[0]
    except Exception as exc:
        telemetry.log_event(
            "session_instruction_insert_failed",
            project_id=project_id, error=str(exc)[:200],
        )
        return None


# ── Phase 2: Generation Session CRUD ──────────────────────────────────────
# 跨批 prompt caching 的"对话会话"层。每个 session 按 (project, engine,
# model, base_prompt_hash) 唯一; 工作流是:
#
#   batch 开始前 → get_or_create_active_session() 拿 session.id
#                → list_session_messages(session.id) 拿历史 prefix
#                → 把 prefix 拼到 LLM 调用的 messages 数组前面
#                → 调用 + 累加 metrics + token tally 到 session
#   batch 通过审核后 → append_session_messages(...) 把"本批 user 指令 +
#                通过的 assistant 输出"写入 session_messages
#   batch 撞窗口 / context error → seal_session(session.id, reason)
#                → 下一批触发新 session 自动创建
#
# 本 PR 仅提供数据层 helpers,worker / app 接入留给后续 PR。完整 happy path
# 测试在 Phase 2.1 接入业务时一起跑;现阶段 helpers 不被任何业务调用。


def get_or_create_active_session(
    client: Client,
    project_id: str,
    engine: str,
    model_id: str,
    base_prompt_hash: str,
    user_id: str,
    window_limit: int,
) -> Optional[dict]:
    """Find existing ``status='active'`` session matching the 4 routing keys,
    or insert one with ``window_limit`` snapshotted at creation time.

    Returns the session row (含 id) or None **only when both lookup and
    insert paths exhaust without success**:

      lookup OK + found      → return existing
      lookup OK + not found  → insert; OK → return new; conflict → re-lookup
      lookup FAILED          → still try insert (transient PostgREST GET
                               errors shouldn't kill the routing); insert OK
                               → return new; insert FAILED → re-lookup once
                               more to recover if race partner just won

    The route_uniq partial index (UNIQUE on project/engine/model/hash WHERE
    status='active') makes the insert deterministic under concurrency —
    second worker hits 23505, we catch and re-lookup to bind to the winner.
    """
    def _lookup() -> Optional[dict]:
        res = (
            client.table("generation_sessions")
            .select("*")
            .eq("project_id", project_id)
            .eq("engine", engine)
            .eq("model_id", model_id)
            .eq("base_prompt_hash", base_prompt_hash)
            .eq("status", "active")
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None

    # 1. Initial lookup — failure here doesn't short-circuit; insert path may
    #    still succeed (or fail and we re-lookup as race recovery).
    try:
        existing = _lookup()
        if existing is not None:
            return existing
    except Exception as exc:
        telemetry.log_event(
            "generation_session_lookup_failed",
            project_id=project_id, engine=engine, model_id=model_id,
            error=str(exc)[:200],
        )
        # Continue to insert path

    # 2. Insert. UNIQUE partial index makes this race-safe.
    try:
        payload = {
            "project_id":        project_id,
            "engine":            engine,
            "model_id":          model_id,
            "base_prompt_hash":  base_prompt_hash,
            "status":            "active",
            "window_limit":      int(window_limit),
            "user_id":           user_id,
        }
        res = client.table("generation_sessions").insert(payload).execute()
        if res.data:
            return res.data[0]
    except Exception as exc:
        telemetry.log_event(
            "generation_session_create_failed",
            project_id=project_id, engine=engine, model_id=model_id,
            error=str(exc)[:200],
        )
        # Insert failure most commonly = 23505 (another worker just won
        # the race for this routing key). Re-lookup to bind to the winner.
        try:
            recovered = _lookup()
            if recovered is not None:
                return recovered
        except Exception:
            pass

    return None


def list_session_messages(client: Client, session_id: str) -> list[dict]:
    """按 ``turn_idx`` 升序返回该 session 全部 messages。返回空 list 表示
    新 session 或读取失败(看 stdout 日志区分)。
    """
    try:
        res = (
            client.table("session_messages")
            .select("turn_idx, role, content, batch_id")
            .eq("session_id", session_id)
            .order("turn_idx", desc=False)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        telemetry.log_event(
            "session_messages_list_failed",
            session_id=session_id, error=str(exc)[:200],
        )
        return []


def get_session_committed_item_ids(client: Client, session_id: str) -> set:
    """返回该 session 已经 commit 过的 item_id 集合(Phase 2.2 懒同步幂等用)。

    分页拉全(Phase 2.2 review #2): PostgREST 单次响应被 project max-rows
    (常见 1000)截断, session 超过那么多 committed turn 后单次 select 会漏,
    导致已同步的 item 被当成新的重复 append。这里用 .range() 翻页直到拉完。

    **必须 .order() 才能安全分页**(review): 不指定排序时 PostgREST 跨页
    返回顺序不确定, 可能某些 item_id 跨页被跳过 / 重复, committed set 不全 →
    懒同步把已同步的当新 append, 重新引入重复。按主键 ``id``(绝对唯一稳定)
    排序保证分页确定。

    读取失败返回空 set(调用方可能重复 commit, 但 DB 唯一约束
    session_messages_session_item_uniq 兜底防重复行)。
    """
    out: set = set()
    try:
        # 审计 COR-008: 原来是「短页收工 + offset 按 page 跳」。服务端把每页
        # 钳短时前者第一页就停、后者会跳过中间那一段 —— 两种都让 committed
        # set 不全, 于是懒同步把已同步的 item 当成新的重复 append。
        for r in _paged_select(lambda off, lim: (
            client.table("session_messages")
            .select("item_id")
            .eq("session_id", session_id)
            .not_.is_("item_id", "null")
            .order("id")
            .range(off, off + lim - 1)
        )):
            if r.get("item_id"):
                out.add(r["item_id"])
        return out
    except Exception as exc:
        telemetry.log_event(
            "session_committed_items_failed",
            session_id=session_id, error=str(exc)[:200],
        )
        return out


def list_approved_versions_for_sync(
    client: Client,
    project_id: str,
    limit: int = 50,
) -> list[dict]:
    """Phase 2.2 懒同步: 查该 project 下 status='approved' 的 items 的代表
    版本 —— **不分引擎/来源**(claude / gemini / manual 手动精修 全要)。

    为什么不按 engine 过滤: 避重应该针对项目所有已产出内容。如果 claude
    session 只放 claude 写的, 就看不到 gemini 写的、更看不到用户手动精修
    (ai_engine='manual')的最终稿 —— 而 manual 恰恰是最该避免重复的采纳内容。
    每个 engine 的 session 都补"项目全部 approved", 内容相同、分别发给各自
    模型, cache 各自命中(prefix 字节一致即可, 与内容由谁产生无关)。

    版本选择(Phase 2.3 修): 优先 ``best_version_id`` 指向的版本; **为空时
    回退到该 item 最新(version_num 最大)的版本**。普通"通过"按钮不写
    best_version_id(只有手动精修 / 显式"选为最佳"才写), 旧实现要求
    best_version_id 非空, 项目级实测漏掉 ~46% 已通过内容。回退后所有
    approved item 都进避重历史。

    返回 ``[{item_id, batch_id, title, body, keywords, created_at}, ...]``,
    按 item 创建时间升序(老的在前, 符合对话历史顺序), 最多 ``limit`` 条。

    Review #3 (known limitation): 用 ``items.created_at`` 近似审核时间排序。
    items 表没有 approved_at 字段, 延迟审核的老 item(创建早、审核晚)会按
    创建时间排序; over-fetch 1000 缓解。要精确需加 approved_at 列(后续 PR)。
    """
    try:
        # 该 project 下所有 status='approved' 的 items, 按 created_at desc 取最近
        # limit 条。不再要求 best_version_id 非空(否则漏掉近一半已通过内容)。
        items_res = (
            client.table("items")
            .select("id, batch_id, best_version_id, created_at, batches!inner(project_id)")
            .eq("batches.project_id", project_id)
            .eq("status", "approved")
            .order("created_at", desc=True)
            .limit(max(limit, 1))
            .execute()
        )
        items = items_res.data or []
        if not items:
            return []

        item_ids = [it["id"] for it in items if it.get("id")]
        if not item_ids:
            return []

        # 批量取这些 item 的全部 version, 按 item 分组。
        # item_id 列表分批(避免 in_ 列表过长); 每批内部再 .range() 翻页拉全——
        # 否则单次 in_ 命中的 version 行数超过 project max-rows(常见 1000)会被
        # 静默截断, _pick_version 可能漏掉 best_version_id 指向的行 / 回退到非
        # 最新版本(同 get_session_committed_item_ids 的分页理由)。必须 .order
        # ("id") 才能安全翻页(主键唯一稳定, 跨页不跳不重)。
        # 审计 COR-008: 翻页改走 _paged_select —— 原来是「短页收工 + offset 按
        # page 跳」, 服务端钳短时前者第一页就停、后者跳过中间那段, 恰好是上面
        # 这段注释声称已经解决的那个静默截断。
        versions_by_item: dict = {}
        id_chunk = 200
        for i in range(0, len(item_ids), id_chunk):
            sub = item_ids[i:i + id_chunk]
            for v in _paged_select(lambda off, lim, _s=sub: (
                client.table("versions")
                .select("id, item_id, version_num, title, body, keywords, created_at")
                .in_("item_id", _s)
                .order("id")
                .range(off, off + lim - 1)
            )):
                versions_by_item.setdefault(v.get("item_id"), []).append(v)

        def _pick_version(it: dict) -> Optional[dict]:
            """优先 best_version_id 指向的版本; 没有(或指向的版本已不存在)则
            回退到该 item 最新(version_num 最大 → created_at 最新)的版本。"""
            cand = versions_by_item.get(it.get("id")) or []
            if not cand:
                return None
            bvid = it.get("best_version_id")
            if bvid:
                for v in cand:
                    if v.get("id") == bvid:
                        return v
            return max(
                cand,
                key=lambda v: (
                    int(v.get("version_num") or 0),
                    str(v.get("created_at") or ""),
                    str(v.get("id") or ""),
                ),
            )

        matched = []
        for it in items:  # items 已按 created_at desc, 最近 limit 条
            v = _pick_version(it)
            if not v:
                continue
            matched.append({
                "item_id":    it["id"],
                "batch_id":   it.get("batch_id"),
                "title":      v.get("title", ""),
                "body":       v.get("body", ""),
                "keywords":   v.get("keywords", []),
                "created_at": it.get("created_at"),
            })
        # matched 按 created_at desc; 翻成升序让对话历史从老到新。
        matched.reverse()
        return matched
    except Exception as exc:
        telemetry.log_event(
            "approved_versions_sync_query_failed",
            project_id=project_id,
            error=str(exc)[:200],
        )
        return []


_APPEND_RETRY_MAX = 3
_APPEND_RETRY_BASE_DELAY = 0.05  # seconds


def _is_unique_violation(exc: BaseException) -> bool:
    """Heuristic detection of PostgreSQL 23505 unique_violation in Supabase
    Python client errors. The SDK wraps everything in generic exceptions,
    so we fall back to substring match on the error message — concrete
    forms vary by client version but always contain '23505', 'duplicate',
    or 'unique' somewhere.
    """
    s = str(exc).lower()
    return "23505" in s or "duplicate key" in s or "unique constraint" in s


def append_session_messages(
    client: Client,
    session_id: str,
    messages: list[dict],
    batch_id: Optional[str] = None,
) -> int:
    """批量追加 messages 到 session。

    ``messages`` 形如 ``[{"role": "user", "content": {...}, "item_id": ...}, ...]``;
    ``turn_idx`` 由本函数自动计算(从当前 max + 1 开始递增,bulk insert)。
    ``batch_id`` 是触发本次 commit 的 batch(可空,但通常都有)。
    每条 message 可带可选 ``item_id`` / ``batch_id``(覆盖整批默认值),
    Phase 2.2 懒同步用 item_id 标记 turn 来源 + 幂等去重。

    Returns: 成功插入的行数;失败返回 0。

    并发安全: read-max-then-insert 是 race-prone 的(两个 worker 同时往
    一个 session 写会同时读到一样的 max → insert 时撞
    ``session_messages_session_turn_uniq`` 23505),所以包了 retry loop。
    碰到 unique violation 退避一下重新算 max + 重试,最多 N 次。其它
    错误立即返回 0(transient 错误另外的层级会自然恢复;持久错误重试也
    没用)。

    实际使用: Phase 2.1+ 的 worker 是队列串行单线程,正常情况下不会有
    并发追加同 session 的场景;此处加 retry 是防御性 + 给"未来允许并行
    生成"留余地。
    """
    if not messages:
        return 0

    last_exc: Optional[BaseException] = None
    for attempt in range(_APPEND_RETRY_MAX):
        try:
            # 拿当前最大 turn_idx, 新插入从 next_idx 起累加
            cur = (
                client.table("session_messages")
                .select("turn_idx")
                .eq("session_id", session_id)
                .order("turn_idx", desc=True)
                .limit(1)
                .execute()
            )
            next_idx = ((cur.data[0]["turn_idx"] + 1) if cur.data else 0)

            rows = []
            for i, m in enumerate(messages):
                role = (m.get("role") or "").lower()
                if role not in ("user", "assistant"):
                    continue
                content = m.get("content")
                # content 必须是 JSON-serializable 的 dict/list — Supabase 会
                # 拒绝裸字符串往 JSONB 列写。统一包成 {"text": str} 形式以兼容
                # 调用方传 str 的便利写法。
                if isinstance(content, str):
                    content = {"text": content}
                rows.append({
                    "session_id":   session_id,
                    "turn_idx":     next_idx + i,
                    "role":         role,
                    "content":      content,
                    # per-message item_id/batch_id 覆盖整批默认(懒同步一次写多
                    # 个 item 的 turn, 各自带自己的 item_id/batch_id)
                    "batch_id":     m.get("batch_id", batch_id),
                    "item_id":      m.get("item_id"),
                })
            if not rows:
                return 0
            res = client.table("session_messages").insert(rows).execute()
            return len(res.data or [])
        except Exception as exc:
            last_exc = exc
            if _is_unique_violation(exc) and attempt < _APPEND_RETRY_MAX - 1:
                # 并发 race: 让对方先 commit 完,我们重读 max + 重试
                import time as _time
                _time.sleep(_APPEND_RETRY_BASE_DELAY * (attempt + 1))
                continue
            break

    telemetry.log_event(
        "session_messages_append_failed",
        session_id=session_id,
        error=str(last_exc)[:200] if last_exc else "unknown",
        retries=_APPEND_RETRY_MAX,
    )
    return 0


def add_session_running_tokens(client: Client, session_id: str, delta: int) -> None:
    """累加 running_input_tokens + 更新 last_used_at。

    Supabase Python client 不支持服务端 increment 表达式,所以 read-modify-
    write。并发场景下偶尔丢更新可以接受——这个值只用于软警告 UI,精度不
    关键(实际撞窗口靠 API 返回 context_length_exceeded 兜底)。
    """
    if delta <= 0:
        return
    try:
        cur = (
            client.table("generation_sessions")
            .select("running_input_tokens")
            .eq("id", session_id)
            .single()
            .execute()
        )
        cur_value = int((cur.data or {}).get("running_input_tokens") or 0)
        client.table("generation_sessions").update({
            "running_input_tokens": cur_value + int(delta),
            "last_used_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", session_id).execute()
    except Exception as exc:
        telemetry.log_event(
            "session_tokens_update_failed",
            session_id=session_id, error=str(exc)[:200],
        )


def set_session_prefix_tokens(
    client: Client, session_id: str, prefix_tokens: int,
) -> int:
    """SET ``last_prefix_tokens``(当前窗口占用,非累加)+ 更新 last_used_at。

    跟 ``add_session_running_tokens`` 不同: 这里是 SET 覆盖, 因为它表示"本
    session 当前 prefix 多大"——每批单次主调用的 prefix 大小, 不该累加。

    返回该 session 的 ``window_limit``(供调用方做封窗阈值判断), 失败返回 0。
    PostgREST update 默认回传被改的行, 顺便把 window_limit 带回来省一次 GET。
    """
    if prefix_tokens < 0:
        return 0
    try:
        res = (
            client.table("generation_sessions")
            .update({
                "last_prefix_tokens": int(prefix_tokens),
                "last_used_at": datetime.now(timezone.utc).isoformat(),
            })
            .eq("id", session_id)
            .execute()
        )
        if res.data:
            return int(res.data[0].get("window_limit") or 0)
    except Exception as exc:
        telemetry.log_event(
            "session_prefix_tokens_update_failed",
            session_id=session_id, error=str(exc)[:200],
        )
    return 0


def seal_session(client: Client, session_id: str, reason: str) -> bool:
    """标记 session 为 sealed,记录 seal_reason + sealed_at。

    幂等: 已 sealed 的 session 再调一次只是覆写 sealed_at 时间(无害),
    不报错。reason 必须在 schema CHECK 列表内
    (window_full / context_error / base_changed / manual),
    否则 PG 拒绝。

    审计 ROB-018: 必须看**受影响行数**, 不能只看"没抛异常"。0 行匹配时
    PostgREST 照样返回成功 —— 而 seal 失败意味着这个 session 继续被路由到,
    prefix 会一路涨过 window_limit 直到 API 真的返回 context_length_exceeded。
    调用方 (_update_session_occupancy) 拿返回值决定要不要告诉用户"已封窗",
    谎报成功就变成"提示说换了新窗、实际还在老窗上撞墙"。
    """
    try:
        res = client.table("generation_sessions").update({
            "status":      "sealed",
            "seal_reason": reason,
            "sealed_at":   datetime.now(timezone.utc).isoformat(),
        }).eq("id", session_id).execute()
    except Exception as exc:
        telemetry.log_event(
            "session_seal_failed",
            session_id=session_id, reason=reason, error=str(exc)[:200],
        )
        return False
    if not (getattr(res, "data", None) or []):
        telemetry.log_event(
            "session_seal_no_match", session_id=session_id, reason=reason,
        )
        return False
    return True


def list_project_sessions(
    client: Client,
    project_id: str,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """列出某项目的 session(UI session 进度面板用)。``status`` None 时
    返回所有状态;给 'active' / 'sealed' 时过滤。
    """
    try:
        q = (
            client.table("generation_sessions")
            .select("*")
            .eq("project_id", project_id)
            .order("last_used_at", desc=True)
            .limit(limit)
        )
        if status:
            q = q.eq("status", status)
        res = q.execute()
        return res.data or []
    except Exception as exc:
        telemetry.log_event(
            "project_sessions_list_failed",
            project_id=project_id, error=str(exc)[:200],
        )
        return []


def count_session_messages(client: Client, session_id: str) -> int:
    """返回该 session 的 message 行数(UI 面板显示"历史轮数"用)。

    用 PostgREST 的 ``count='exact'`` 只取计数, 不拉 content(JSONB 可能很大),
    比 ``list_session_messages`` 轻得多。失败返回 0。
    """
    try:
        res = (
            client.table("session_messages")
            .select("id", count="exact")
            .eq("session_id", session_id)
            .limit(1)
            .execute()
        )
        return int(getattr(res, "count", 0) or 0)
    except Exception as exc:
        telemetry.log_event(
            "session_messages_count_failed",
            session_id=session_id, error=str(exc)[:200],
        )
        return 0


# ── Job queue (R-018) ──────────────────────────────────────────────────────
# DB-backed job 队列的数据层。UI 侧用 insert_job / get_job / cancel_job /
# list_user_jobs（走 authed client + RLS）; worker 侧用 claim_one_job /
# heartbeat / progress / finish / sweep（走 service client 绕 RLS）。
# 状态机: pending →(claim)→ running →(handler)→ success | failed;
# 失败且未超 max_attempts → 回 pending + next_retry_at 退避; sweeper 把心跳
# 超时的 running 行也退回。详见 worker.py。

def _now_iso() -> str:
    """UTC ISO 字符串(秒精度), 给 jobs 的时间戳列用。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# 这些 kind 的 handler **不幂等**: 每个 plan 跑完就往 items/versions 落库了。
# 重试会把已经成功的那部分再生成一遍 —— 用户看到的是凭空多出来的重复内容,
# 而且没有任何地方会报错(job 那边看起来只是"重试了一次然后成功")。
#
# 所以入队时强制 max_attempts=1。真正的失败要人看见、由人决定重跑哪一段,
# 不能由退避重试自动来。(审计 ROB-001 / SUP-011)
NON_IDEMPOTENT_JOB_KINDS = frozenset({"generate_batch", "quick_gen"})


def insert_job(
    client: Client,
    kind: str,
    payload: dict,
    user_id: str,
    project_id: Optional[str] = None,
    priority: int = 0,
    max_attempts: int = 3,
) -> dict:
    """入队一个 job（UI 侧用 authed client; RLS 要求 user_id = auth.uid()）。

    ⚠️ ``kind`` 在 ``NON_IDEMPOTENT_JOB_KINDS`` 里时, ``max_attempts`` 会被
    强制成 1 —— 理由见那个常量上面那段。
    """
    row: dict[str, Any] = {
        "kind": kind,
        "payload": payload or {},
        "user_id": user_id,
        "priority": int(priority),
        "max_attempts": int(max_attempts),
    }
    if project_id:
        row["project_id"] = project_id
    if kind in NON_IDEMPOTENT_JOB_KINDS and int(max_attempts) != 1:
        # 见 NON_IDEMPOTENT_JOB_KINDS 的说明。这里【直接改掉】而不是抛错:
        # 调用方多半只是没传 max_attempts, 用了签名上那个 3 的默认值; 为这个
        # 拒绝入队会让生成整个用不了, 而重试的后果是凭空多出重复内容。
        telemetry.log_event("job_max_attempts_forced", kind=kind,
                            requested=int(max_attempts))
        row["max_attempts"] = 1
    res = client.table("jobs").insert(row).execute()
    return _first_row(res, "入队任务", kind=kind, user_id=user_id)


def get_job(client: Client, job_id: str) -> Optional[dict]:
    """读单个 job 行（UI 轮询进度用）。不存在 / 无权限时返回 None。"""
    try:
        res = client.table("jobs").select("*").eq("id", job_id).single().execute()
        return res.data
    except Exception:
        return None


def list_user_jobs(client: Client, user_id: str, limit: int = 20) -> list[dict]:
    """列某用户最近的 job（UI 历史 / 队列面板用）。"""
    res = (
        client.table("jobs").select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []


def cancel_job(client: Client, job_id: str) -> None:
    """UI 取消: 仅当还在 pending/claimed/running 时置 cancelled（终态的不动）。"""
    try:
        (
            client.table("jobs")
            .update({"status": "cancelled", "finished_at": _now_iso()})
            .eq("id", job_id)
            .in_("status", ["pending", "claimed", "running"])
            .execute()
        )
    except Exception as exc:
        telemetry.log_event("job_cancel_failed", job_id=str(job_id), error=str(exc)[:200])


def claim_one_job(
    client: Client, worker_id: str, kinds: Optional[list[str]] = None
) -> Optional[dict]:
    """worker 领取一个待处理 job（走 claim_one_job RPC, service client）。

    返回领到的 job 行（已置 running）或 None（队列空）。RPC 内部用
    FOR UPDATE SKIP LOCKED, 多 worker 并发安全。
    """
    res = client.rpc(
        "claim_one_job", {"_worker_id": worker_id, "_kinds": kinds}
    ).execute()
    rows = res.data or []
    return rows[0] if rows else None


def update_job_progress(
    client: Client, job_id: str, pct: int, message: Optional[str] = None
) -> None:
    """handler 更新进度; UI 轮询同一行即可看到。失败不抛（埋点）。

    审计 ROB-018: 0 行匹配也要留痕。job 被取消 / 被 sweeper 退回重领之后,
    stale worker 仍会一路刷进度, 每次都"成功"但一行没改 —— 进度条在 UI 上
    冻住不动, 而日志里什么都没有。
    """
    patch: dict[str, Any] = {"progress_pct": int(pct)}
    if message is not None:
        patch["progress_message"] = message
    try:
        res = client.table("jobs").update(patch).eq("id", job_id).execute()
    except Exception as exc:
        telemetry.log_event("job_progress_failed", job_id=str(job_id), error=str(exc)[:200])
        return
    if not (getattr(res, "data", None) or []):
        telemetry.log_event("job_progress_no_match", job_id=str(job_id), pct=int(pct))


def heartbeat_job(client: Client, job_id: str, worker_id: Optional[str] = None) -> None:
    """worker 心跳线程刷 heartbeat_at; sweeper 据此判断 worker 是否还活着。

    CAS: 传 worker_id 时只在 claimed_by 仍是本 worker 时刷新。否则一个心跳曾
    失联、job 已被 sweeper 退回 + 他人重领的 stale worker, 其心跳线程会把别人的
    行"续命", 让那行永远不被 sweeper 回收（僵尸保活）。
    """
    try:
        q = client.table("jobs").update({"heartbeat_at": _now_iso()}).eq("id", job_id)
        if worker_id is not None:
            q = q.eq("claimed_by", worker_id)
        q.execute()
    except Exception as exc:
        telemetry.log_event("job_heartbeat_failed", job_id=str(job_id), error=str(exc)[:200])


def finish_job_success(
    client: Client, job_id: str, result: Optional[dict] = None,
    worker_id: Optional[str] = None,
) -> bool:
    """handler 成功返回 → 置 success + 100% + 写 result。

    CAS: 只在仍 ``status='running'`` 且（传了 worker_id 时）``claimed_by=worker_id``
    时写。防止本 worker 心跳曾失联 → sweeper 退回 → 他人重领后, 这个 stale worker
    迟到的成功覆盖掉新 attempt 的状态/结果, 或盖掉用户已取消的 job。
    返回是否真的写入（False = 行已不归本 worker, 跳过）。
    """
    q = (
        client.table("jobs")
        .update({"status": "success", "finished_at": _now_iso(),
                 "progress_pct": 100, "result": result or {}})
        .eq("id", job_id).eq("status", "running")
    )
    if worker_id is not None:
        q = q.eq("claimed_by", worker_id)
    res = q.execute()
    if not res.data:
        telemetry.log_event("job_finish_skipped_not_owner", job_id=str(job_id), worker=worker_id)
    return bool(res.data)


def mark_job_failed(
    client: Client, job_id: str, error_text: str, worker_id: Optional[str] = None,
) -> bool:
    """直接置 failed（无重试语义）。用于"无 handler"等不该重试的情形。

    CAS: 同 finish_job_success —— 只在 running +（可选）claimed_by 命中时写,
    不覆盖已被重领/取消/完成的行。返回是否真的写入。
    """
    q = (
        client.table("jobs")
        .update({"status": "failed", "finished_at": _now_iso(), "error_text": error_text})
        .eq("id", job_id).eq("status", "running")
    )
    if worker_id is not None:
        q = q.eq("claimed_by", worker_id)
    res = q.execute()
    if not res.data:
        telemetry.log_event("job_fail_skipped_not_owner", job_id=str(job_id), worker=worker_id)
    return bool(res.data)


def fail_or_requeue_job(
    client: Client, job: dict, error_text: str,
    expected_claimed_by: Optional[str] = None,
) -> str:
    """handler 抛错 / sweeper 回收后的处理: 还有重试次数 → 回 pending + 指数退避;
    否则 failed。退避 = 30 × 2^(attempts-1) 秒（30 / 60 / 120 …）。attempts 已在
    claim 时 +1, 所以这里直接比 ``attempts >= max_attempts``。

    CAS（防并发互踩, review #1/#3）: 只在行仍 active（claimed/running）且（传了
    ``expected_claimed_by`` 时）claimed_by 仍是该值才写。
      - worker 异常路径: ``expected_claimed_by`` = 本 WORKER_ID
      - sweeper 路径: ``expected_claimed_by`` = 候选行的 claimed_by（那个疑似死掉
        的 worker）
    这样 sweeper 读候选后、写之前若行已被另一 worker 重领（claimed_by 变了）或已
    完成/取消（status 变了）, CAS 落空跳过, 不会把别人的 running 打回 pending 造成
    重复执行 / 丢进度。返回新 status: ``'failed'`` / ``'pending'`` / ``'skipped'``。
    """
    attempts = int(job.get("attempts") or 0)
    max_attempts = int(job.get("max_attempts") or 1)
    if attempts >= max_attempts:
        patch: dict[str, Any] = {
            "status": "failed", "finished_at": _now_iso(), "error_text": error_text,
        }
        target = "failed"
    else:
        backoff = 30 * (2 ** max(0, attempts - 1))
        next_retry = (datetime.now(timezone.utc) + timedelta(seconds=backoff)).isoformat(timespec="seconds")
        patch = {
            "status": "pending", "error_text": error_text, "next_retry_at": next_retry,
            # 清掉 claim 痕迹, 让它能被重新领取
            "claimed_by": None, "claimed_at": None, "heartbeat_at": None, "started_at": None,
        }
        target = "pending"
    q = (
        client.table("jobs").update(patch)
        .eq("id", job["id"]).in_("status", ["claimed", "running"])
    )
    if expected_claimed_by is not None:
        q = q.eq("claimed_by", expected_claimed_by)
    res = q.execute()
    if not res.data:
        telemetry.log_event(
            "job_requeue_skipped",
            job_id=str(job.get("id")), expected_claimed_by=expected_claimed_by,
        )
        return "skipped"
    return target


def sweep_dead_jobs(client: Client, timeout_seconds: int) -> int:
    """把心跳超时的 claimed/running job 退回重试 / 置 failed（超次数）。

    worker 主循环定期调一次。返回回收的 job 数。失败安全（埋点不抛）。
    每条退回都带 ``expected_claimed_by=候选行的 claimed_by`` 做 CAS, 防止读候选
    后、写之前该行已被另一 worker 重领 —— 那种情况跳过, 不打断新 worker（review #3）。

    审计 ROB-009: 光靠 ``heartbeat_at < cutoff`` 会**永远漏掉 heartbeat_at 为
    NULL 的行**。SQL 里 ``NULL < x`` 求值为 NULL 而不是 true, 所以那种行既不
    满足条件、也不会被任何一轮 sweep 看到 —— 一条 claimed/running 但没有心跳的
    job 就此永久占位, 既不执行也不回收, 而队列面板上它一直显示"运行中"。
    claim_one_job 正常会写 heartbeat_at, 但手工改状态、或将来新增的领取路径
    漏写, 都会造出这种行; sweeper 是最后一道回收闸, 不该对它盲。
    NULL 心跳的行改用 ``claimed_at`` 判超时(两者都为 NULL 时无条件视为僵尸)。
    分两次查而不是拼 or= 过滤串: 时间戳里的 ``+`` 在 query string 里会被解成
    空格、``.`` 又是 PostgREST 过滤语法的分隔符, 手拼容易出隐蔽的错。
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat(timespec="seconds")
    try:
        stale = (
            client.table("jobs").select("*")
            .in_("status", ["claimed", "running"])
            .lt("heartbeat_at", cutoff)
            .execute()
        ).data or []
        # 心跳为 NULL 的候选: 按 claimed_at 判超时
        no_hb = (
            client.table("jobs").select("*")
            .in_("status", ["claimed", "running"])
            .is_("heartbeat_at", "null")
            .execute()
        ).data or []
        cutoff_dt = parse_ts(cutoff)
        orphans = []
        for j in no_hb:
            claimed = parse_ts(j.get("claimed_at"))
            # claimed_at 也为空 = 这行根本没走过正常领取路径, 无条件视为僵尸
            if claimed is None or (cutoff_dt is not None and claimed < cutoff_dt):
                orphans.append(j)
        if orphans:
            telemetry.log_event(
                "job_sweep_null_heartbeat", count=len(orphans),
                ids=",".join(str(j.get("id")) for j in orphans[:5]),
            )
        seen: set = set()
        dead = []
        for j in stale + orphans:
            jid = j.get("id")
            if jid in seen:
                continue
            seen.add(jid)
            dead.append(j)
    except Exception as exc:
        telemetry.log_event("job_sweep_query_failed", error=str(exc)[:200])
        return 0
    recovered = 0
    for job in dead:
        try:
            new_status = fail_or_requeue_job(
                client, job,
                f"heartbeat stale (cutoff={cutoff}); worker likely died, recovered by sweeper",
                expected_claimed_by=job.get("claimed_by"),
            )
            if new_status != "skipped":
                recovered += 1
        except Exception as exc:
            telemetry.log_event(
                "job_sweep_recover_failed",
                job_id=str(job.get("id")), error=str(exc)[:200],
            )
    return recovered
