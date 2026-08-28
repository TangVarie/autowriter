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


def rpc_missing(exc: Exception) -> bool:
    """这个异常是不是"**RPC** 还没部署"?

    判据从 commit_fingerprints_atomic 里提出来共用: 迁移没跑时降级、其余错误
    (权限 / 参数 / 库故障)必须原样上抛。把两者混为一谈会让真故障被当成
    "没迁移"静默降级 —— 那正是审计一直在追的那类问题。

    ⚠️ **只管函数, 不管表。** PostgREST 对"表不存在"回的是 ``PGRST205``
    ("Could not find the table ... in the schema cache"), 三个条件一个都不沾 ——
    所以这个函数对缺表返 False, 那是**对的**: 运行期某张表突然没了不该被当成
    "没跑迁移"然后降级, 那是真故障。要判"这个 schema 对象在不在"用
    ``schema_object_missing``。(2026-08-26 对着真 PostgREST 实测的错误形态,
    不是照记忆写的; codex review · #63)
    """
    msg = str(exc).lower()
    return ("could not find the function" in msg
            or "does not exist" in msg
            or "pgrst202" in msg)


def schema_object_missing(exc: Exception) -> bool:
    """这个异常是不是"**任何一种** schema 对象还没建"(表 / 列 / 函数)?

    比 ``rpc_missing`` 宽, 多认两个 PostgREST 的 schema-cache 码:

      · ``PGRST205`` —— 表不存在("Could not find the table ... in the schema cache")
      · ``PGRST204`` —— **写**路径上的列不存在("Could not find the '...' column ...")
        (读路径上的列不存在是 PG 自己的 ``42703 column ... does not exist``,
         已经被 rpc_missing 那句 "does not exist" 覆盖了)

    ⚠️ **只给部署自检 (core.migration_state) 用, 别拿去做运行期降级判据。**
    两者要的东西不同: 自检问的是"这个对象在不在", 运行期问的是"要不要降级"。
    运行期把缺表也当成"没跑迁移"就会把真故障静默吞掉。
    """
    return rpc_missing(exc) or any(
        code in str(exc).lower() for code in ("pgrst205", "pgrst204"))


def table_permission_denied(exc: Exception) -> bool:
    """这个异常是不是"**表建出来了, 但没授权**"?

    2026-08-26 首次真部署踩的形态。``migrations/001_deskcore.sql`` 建了四张表
    却只给两个**函数**发了 EXECUTE, 表本身一行 GRANT 都没有, 于是 deskcore 除
    ``list_projects`` 外每个工具都挂在::

        42501 permission denied for table draft_fingerprints

    坑在于 ``service_role`` **绕过 RLS 但不绕过表级 GRANT** —— 两套独立机制。
    它在 public schema 下看着无所不能, 靠的是 Supabase 给 public 配的 default
    privileges; ``autowriter`` 是本仓自建 schema, 没有这份默认授权。

    ⚠️ **只认表 / 关系, 不认函数。** ``permission denied for function`` 是另一
    回事(EXECUTE 没发, 或者调用方角色不对), 补救的 SQL 也不是同一条 —— 把它
    一起认进来, doctor 就会指挥人去跑 007, 而 007 一行函数权限都不管。同样地
    **不匹配裸 ``42501``**: 函数那条也是 42501。

    ⚠️ 与 ``schema_object_missing`` 一样, **只给部署自检用**。运行期把"没权限"
    当成可降级的情况, 就等于把一个配错的库伪装成一个功能少一点的库。
    """
    msg = str(exc).lower()
    return ("permission denied for table" in msg
            or "permission denied for relation" in msg)


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

def project_row(sb, project_id: str) -> dict | None:
    """项目整行; 不存在返回 None。

    ⚠️ 刻意不用 ``db.get_project`` —— 它用 ``.single()``, PostgREST 在 0 行时回
    406, postgrest-py 把它抛成 APIError。也就是说 ``db.get_project`` 【从不返回
    None】, 调用方那句 ``if project is None: raise ValueError("project not
    found")`` 是死代码, 传错 project_id 拿到的是一条看不懂的 406 报错。归属校验
    要区分"项目不存在"(可能只是 id 抄错)和"项目不是你的", 所以这里换成 limit(1)。
    """
    res = (sb.table("projects").select("*")
             .eq("id", project_id).limit(1).execute())
    rows = res.data or []
    return rows[0] if rows else None


def list_all_projects(sb, *, owner_id: str) -> list[dict]:
    """``owner_id`` 名下的项目。

    ⚠️ 这里【曾经不按 owner 过滤】, 理由写的是"deskcore 要让任何人都能打开任何
    项目(规则共享)"。审计 COR-015 指出那个选择的代价: 整套隔离就只剩 key 这一层,
    而 key 这一层有 ROB-003(配错就全开)。归属口径已按 ``projects.owner_id`` 定下来
    (与 db.py:224 那条 RLS policy `owner_id = auth.uid()` 同一判据)——
    deskcore 持 service_role 绕过 RLS, 就得自己把同一条谓词执行一遍。

    参数写成**关键字必填**是故意的: 将来若改成团队共享, 改的是
    ``core.assert_project_access`` 一处; 而任何人想在这里"顺手去掉过滤",
    都得先改签名, 改不动就不会不小心改回去。

    ⚠️ 必须翻页。原来是裸 select 无 limit —— PostgREST 的 db-max-rows(默认
    1000)会**静默截断**: 越过 1000 个项目之后, 后面的项目在模型眼里【根本不存在】,
    且没有任何提示(审计 COR-005 同款, 判据同样是空页收工 + offset 按实收行数前进)。
    ``name`` 有重名, 排序必须带 ``id`` 做次级键, 否则翻页会漏行也会重复行。
    """
    return _paged(lambda off, lim: (
        sb.table("projects")
          .select("id, name, brand, owner_id")
          .eq("owner_id", owner_id)
          .order("name").order("id")
          .range(off, off + lim - 1)
    ))


def projects_with_exact_name(sb, *, owner_id: str, name: str) -> list[dict]:
    """同 owner 下**名字相同**的项目 —— 大小写与首尾空格都不算差异。

    ⚠️ 只按 owner 查, 不查全库: 别人的项目叫什么跟这次建重不重没关系, 而把
    全库项目名暴露给调用方正是审计 COR-015 堵掉的那个洞。

    ⚠️ 判据从服务端的 ``.eq("name", name)`` 挪到了这里, 因为那个写法**漏得很
    安静**: PG 的 ``=`` 对文本大小写敏感, 于是先建 ``"Sportsix"`` 再建
    ``"sportsix"`` 会判成两个不同的项目, ``created=True``, 库里两行, 不报错。
    两个同名项目 = 两套互不可见的历史库, ``check_drafts`` 按 project_id 比,
    从此对这个方向永久失效。库里也没有唯一索引兜底(见下)。

    为什么把比对放 Python 而不是用 ``.ilike``:
      · ``ilike`` 要自己转义 ``% _ \\``, 转义写错的表现是"匹配不到"——又是一个
        安静的漏法, 而它正是本函数要堵的那类。
      · 首尾空格 ``ilike`` 也管不了(库里存着 ``"途鸽 "`` 时), 还是要 Python 复核,
        那就只保留一处判据。
      · 单个 owner 的项目是**几十条**量级(现网最多 29), 全取回来的代价可以忽略。
        真长到几百上千再回头做服务端过滤 —— 那时 ``_paged`` 已经在了。

    ⚠️ 这仍然是 check-then-insert, **不是原子的**: 两次并发调用会各自查空、各自
    建成。库上没有 ``UNIQUE (owner_id, lower(btrim(name)))``, 与 docs/deskcore.md
    §2.3-D「并发正确性交给数据库」的纪律相反。加索引要一次迁移, 且得先处理存量
    可能已有的重名行, 单独做 —— 记在 runbook 待办里。
    """
    want = (name or "").strip().casefold()
    if not want:
        return []
    rows = _paged(lambda off, lim: (
        sb.table("projects")
          .select("id, name, brand")
          .eq("owner_id", owner_id)
          .order("id")
          .range(off, off + lim - 1)
    ))
    return [r for r in rows
            if (r.get("name") or "").strip().casefold() == want]


def projects_with_brand(sb, *, owner_id: str, brand: str) -> list[dict]:
    """同 owner 下同品牌的项目 —— 建项目时用来提示"这个品已经有几个方向了"。

    ⚠️ 刻意**不用** ``.or_()`` 把它和上面那个合成一次查询: 测试假件把
    ``.or_()`` 当无操作处理(见 tests/fakes.py), 合起来写会让撞名检查在测试里
    永远"通过"而实际没过滤 —— 又是一次"绿灯是假的"。两次查询便宜得多。
    """
    if not (brand or "").strip():
        return []
    return _paged(lambda off, lim: (
        sb.table("projects")
          .select("id, name, brand")
          .eq("owner_id", owner_id)
          .eq("brand", brand)
          .order("name").order("id")
          .range(off, off + lim - 1)
    ))


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
    # ⚠️ 判据必须和 db._is_rule_memory 一致: memory_type IS NULL(老行) 或
    # 'rule'。原来写的是 `neq('session')` —— 那只排掉了 session, 于是
    # memory_type='note' 的行会被当成规则按 severity 塞进 P0/P1。note 不是
    # 写作规则, autowriter 自己的 db.get_confirmed_memories 一直是按
    # _is_rule_memory 过的。服务端先用 or_ 收窄, 拉回来再用 db._is_rule_memory
    # 复核一遍 —— 判据只有一个定义, 以后新增 memory_type 也不会漏。
    # (codex review round-5 P2)
    # ⚠️ 必须翻页。原来是裸 `.execute()` 无 range —— PostgREST 的 db-max-rows
    # (Supabase 默认 1000)会【静默钳短】: 越过 1000 条之后的规则在模型眼里
    # 【根本不存在】, 且没有任何提示。这条路径读的是**强制合规规则**, 被截掉
    # 的那几条不会报错, 只会让这一批稿子少守几条硬约束 —— 与本函数开头那段
    # "宁可报错也不能返回空 p0" 是同一个失败模式的另一半, 堵一半等于没堵。
    # (审计 COR-005/006/008 同款; _paged 的终止判据是空页而不是短页, 见它的
    # docstring —— 服务端钳短时每一页都是短页。)
    #
    # ``build`` 必须每次从 sb.table(...) 重新构造: postgrest-py 复用同一个
    # builder 时 .range() 的偏移会叠加。所以这里收的是**建查询的函数**,
    # 不是建好的查询。
    def _rows(build):
        rows = _paged(lambda off, lim: (
            build().eq("status", "confirmed")
                   .or_("memory_type.is.null,memory_type.eq.rule")
                   .order("created_at").order("id")
                   .range(off, off + lim - 1)
        ))
        return [r for r in rows if db._is_rule_memory(r)]

    # embedding: 给 memory.filter_soft_by_relevance 用。
    # ⚠️ R-034 —— PostgREST 把 pgvector 列当【字符串】回, 直接喂
    # dedup.cosine_similarity 会静默得 0.0, 于是【每一条】soft 规则都低于阈值
    # 被滤掉。必须过 db._parse_pgvector(autowriter 自己的 list_memories:1901-1906
    # 就是这么做的)。这个坑不修比不加相关性过滤更糟。
    cols = ("id, content, severity, scope, rule_kind, rule_payload, "
            "muted_until, user_id, memory_type, created_at, frequency, embedding")
    proj = _rows(lambda: sb.table("memories").select(cols)
                           .eq("project_id", project_id).eq("scope", "project"))
    glob = (_rows(lambda: sb.table("memories").select(cols)
                            .eq("scope", "global").eq("user_id", user_id))
            if user_id else [])

    # ⚠️ 静音判定必须复用 db.is_memory_muted_now, 不能在这里自己写一份
    # (审计 COR-007)。本地那版直接 datetime.fromisoformat 后与 aware now 比较,
    # 遇到两类真实数据会抛 TypeError/ValueError 被 except 吞掉 → 返回 True →
    # 【被静音的规则照常注入 P0/P1】:
    #   · muted_until 存成 naive ISO(无时区后缀)  → naive < aware 抛 TypeError
    #   · 某些 PG client 回 7 位微秒               → fromisoformat 只吃 6 位
    # db.is_memory_muted_now(:2819-2856) 正是为这两种输入写的, 它把 naive 按
    # UTC 解释、截掉多余微秒, 并在解析失败时保守返回"未静音"。判据只留一处。
    rows = [
        m for m in (glob + proj)
        if not db.is_memory_muted_now(m.get("muted_until"))
        and (m.get("content") or "").strip()
    ]
    for r in rows:
        if "embedding" in r:
            r["embedding"] = db._parse_pgvector(r.get("embedding"))   # R-034, 见上
    hard = [m for m in rows if (m.get("severity") or "soft").lower() == "hard"]
    soft = [m for m in rows if (m.get("severity") or "soft").lower() != "hard"]
    return hard, soft


def rule_counts_bulk(sb, project_ids: list[str]) -> dict[str, tuple[int, int]]:
    """一次查全部项目的规则条数, 返回 ``{project_id: (hard, soft)}``(审计 SUP-004)。

    list_projects 原来是每个项目调一次 shared_memories —— 40 个项目就是 40 次
    往返, 而这是模型最常调的第一个工具。更亏的是: 它只用了 ``len(hard)`` /
    ``len(soft)`` 两个数字, 却把每条规则的 **768 维 embedding** 一起拉了回来。

    判据与 shared_memories 完全一致(``db._is_rule_memory`` + ``muted_until``
    + 非空 content), 只是不取 embedding、也不取 global scope ——
    list_projects 传的 user_id 本来就是 None, 那一路取的是空列表。

    两层都不能省:
      · **按 id 分块**(``db._in_chunks``) —— 一次把几百个 UUID 塞进 ``.in_()``
        会生成 10KB+ 的查询串, 网关直接 414, 而 list_projects 是模型开工调的
        第一个工具(codex review 2026-08-24; db.py 里其它批量读早就这么做了)。
      · **翻页**(``_paged``) —— 所有项目的规则加起来很容易越过 PostgREST 的
        db-max-rows, 静默截断的话计数会偏小而没有任何提示(审计 COR-005 同款)。
    """
    out: dict[str, tuple[int, int]] = {pid: (0, 0) for pid in project_ids}
    if not project_ids:
        return out
    cols = "id, project_id, severity, muted_until, memory_type, content"
    rows: list[dict] = []
    for chunk in db._in_chunks(list(project_ids)):
        rows += _paged(lambda off, lim, _c=chunk: (
            sb.table("memories").select(cols)
              .in_("project_id", _c)
              .eq("scope", "project").eq("status", "confirmed")
              .or_("memory_type.is.null,memory_type.eq.rule")
              .order("id")
              .range(off, off + lim - 1)
        ))
    for r in rows:
        if not db._is_rule_memory(r):
            continue
        if db.is_memory_muted_now(r.get("muted_until")):
            continue
        if not (r.get("content") or "").strip():
            continue
        pid = str(r.get("project_id") or "")
        if pid not in out:
            continue
        hard, soft = out[pid]
        if (r.get("severity") or "soft").lower() == "hard":
            out[pid] = (hard + 1, soft)
        else:
            out[pid] = (hard, soft + 1)
    return out


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
        # 判据走 rpc_missing —— 这里原来是它的**第二份手抄**(同样三个条件)。
        # 两份迟早漂开, 而漂开的后果是"某一路把真故障当成没跑迁移、静默降级",
        # 正是 rpc_missing 的 docstring 一直在防的事。(codex review · #63)
        if rpc_missing(exc):
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
    """把台账里这个角度标成已消耗。返回【是否真的改到了行】。

    ⚠️ 必须看受影响行数, 不能只看"没抛异常"。没有匹配的未消耗行时(最典型:
    非原子降级路径里 record_draw 的插入失败了, 台账根本没这一行), PostgREST
    照样返回成功、data 为空 —— 直接 return True 会让 commit_drafts 报告
    "已消耗", 而这个角度在台账上并不存在, 下一批立刻能再抽到同一个坐标。
    避重静默失效, 且没有任何痕迹。(codex review round-5 P2)
    """
    try:
        res = (sb.table("angle_ledger")
                 .update({"consumed_version_id": version_id, "consumed_at": iso_now()})
                 .eq("project_id", project_id).eq("angle_key", angle_key)
                 .is_("consumed_version_id", "null").execute())
    except Exception:
        logger.exception("mark angle consumed failed: %s", angle_key)
        return False
    if not (res.data or []):
        logger.warning(
            "angle %s (project=%s) had no unconsumed ledger row to mark; "
            "cross-batch avoidance will not see it as used", angle_key, project_id)
        return False
    return True


# ── 成稿指纹库 ────────────────────────────────────────────────────────────

PAGE = 1000        # PostgREST 默认 max-rows, 见下方说明


def _paged(build, *, page: int = PAGE, hard_cap: int | None = None) -> list[dict]:
    """按 offset 翻页拉全一个查询的结果(审计 COR-005 / COR-006)。

    ``build(offset, limit)`` 必须**每次从 sb.table(...) 重新构造** query ——
    postgrest-py 复用同一个 builder 时 ``.order()`` 会追加、``.range()`` 的偏移
    会叠加(db.py 的 list_items_for_batches 回归用例专门盯着这一点)。

    ⚠️ 终止判据是【空页】而不是【短页】。PostgREST 的 ``db-max-rows`` 会把请求
    钳短: 服务端上限低于 ``page`` 时**每一页都是短页**, 但后面明明还有行 ——
    按短页收工就是又一次静默截断, 正是本函数要根治的东西
    (db.py:1657-1665 为同一个坑留过完整说明)。代价只是末尾多发一次拿到空页
    的请求。

    offset 按【实收行数】前进, 不是按 ``page``: 服务端钳短时按 page 跳会直接
    漏掉中间那一段。

    ``hard_cap`` 非空时最多取这么多行(调用方的上界), 到顶即停。
    """
    return [r for pg in _paged_iter(build, page=page, hard_cap=hard_cap) for r in pg]


def _paged_iter(build, *, page: int = PAGE, hard_cap: int | None = None):
    """``_paged`` 的生成器形态: 逐页 yield, 调用方处理完一页就能让它被回收。

    审计 ROB-011: 回填原来先把【全部】历史成稿(全文 + 768 维向量)攒成一个列表
    再分块处理 —— 5000 条 × 768 个 Python float ≈ 三四百 MB 峰值, 容器 OOM
    重启, 而回填是"部署后每个项目必跑一次"的动作。逐页消费之后峰值只和
    ``page`` 有关, 与项目历史多大无关。
    """
    taken = 0
    offset = 0
    while True:
        want = page if hard_cap is None else min(page, hard_cap - taken)
        if want <= 0:
            return
        rows = build(offset, want).execute().data or []
        if not rows:
            return
        taken += len(rows)
        offset += len(rows)
        yield rows


def fingerprints(sb, project_id: str, limit: int = 4000) -> tuple[list[dict], bool]:
    """项目【全量】历史指纹。返回 (rows, truncated)。

    ⚠️ 故意不吞异常: 查重是硬闸, 读不到历史就不能放行。这是 deskcore 里唯一
    不 fail-open 的路径(其余读类工具出错返回可用结构不阻塞写稿)。

    ⚠️ 必须【翻页】而不是 .limit(4000)。PostgREST 的 max-rows 默认 1000, 超过
    的部分**静默截断**(db.py:1396-1404 已经为此踩过一次坑)。项目一旦攒过
    1000 条指纹, check_drafts 就只拿到最新的 1000 条、却照旧报 history_size
    说自己比了全量 —— 老稿子的重复从此原样放行, 而"比对全量历史"正是这套东西
    相对老工作台的核心卖点。(codex review)

    排序必须带 id 做次级键: 指纹是【整块 insert】的(commit/backfill 都成批写),
    同一批的 created_at 完全相同。只按 created_at 排, 翻页时同值行的相对顺序
    没有保证 —— 会漏行也会重复行, 而且不报错。
    """
    # ⚠️ ``embedding_model`` 必须取出来。这一列不是元数据装饰 —— 它是**比对的
    # 前置条件**: 跨模型算余弦出来的数是垃圾, 而且【不报错】。取不到它, 调用方
    # 就没法把老模型的行排除掉, 一次换模型能让硬闸安静地失灵。
    # (codex review · #65 P1; 001 的 COMMENT 早就写了这一列的用途, 只是从来
    #  没有任何代码真的用过它 —— 又一次"写着已经有了, 实际没有"。)
    cols = ("id, title, opening, title_embedding, embedding_model, "
            "opening_hash, ngram_hashes, created_at")
    rows: list[dict] = []
    truncated = False
    while len(rows) < limit:
        start = len(rows)
        end = min(start + PAGE, limit) - 1
        res = (sb.table("draft_fingerprints")
                 .select(cols)
                 .eq("project_id", project_id)
                 .order("created_at", desc=True)
                 .order("id", desc=True)
                 .range(start, end).execute())
        page = res.data or []
        rows.extend(page)
        # 审计 COR-008: 判据必须是【空页】而不是【短页】。服务端 db-max-rows
        # 低于 PAGE 时**每一页都是短页**, 按短页收工就只拿到第一页 —— 而
        # check_drafts 照旧报 history_size, "比对全量历史" 变成假话。
        # (本函数的 offset 本来就按 len(rows) 前进, 钳短不会漏中间那段。)
        if not page:
            break                      # 取完了
    else:
        # 没 break = 撞到 limit。再探一行, 确认后面是不是还有。
        probe = (sb.table("draft_fingerprints").select("id")
                   .eq("project_id", project_id)
                   .order("created_at", desc=True).order("id", desc=True)
                   .range(limit, limit).execute())
        truncated = bool(probe.data)

    for r in rows:
        r["title_embedding"] = db._parse_pgvector(r.get("title_embedding"))
    return rows, truncated


def fingerprint_stats(sb, project_id: str, model: str) -> tuple[int, int]:
    """(总条数, 有【可用】标题向量的条数) —— 两次 count, 不拉行。

    下推之后 check_drafts 不再把指纹拉进内存, 但它报出去的 summary 仍然要说清
    "比了多少条、其中多少条有向量"。后者尤其不能丢: ``hist_missing_vec > 0``
    正是 semantic_degraded 的判据之一 —— 历史行的 title_embedding 为 NULL 时
    标题语义这一路【实际没跑】, 不说出来调用方会以为全套硬闸都过了
    (core.check_drafts:380-388 为这个坑留过完整说明)。

    ⚠️ **"有向量" 的口径是 "有【本模型】的向量"**, 不是 "这一格非 NULL"。
    换模型之后老行的向量还在, 但它跟新向量算余弦出来的数是垃圾 —— 按非 NULL
    去数, 一个全是老模型行的项目会报 ``history_missing_embedding = 0``,
    于是 ``semantic_degraded`` 是 false, 而标题语义那一路**一条都没真的比**。
    这正好是这个函数存在的理由的反面。(codex review · #65 P1)

    ⚠️ 故意不吞异常, 与 fingerprints 同理: 查重是硬闸, 读不到就不能放行。
    """
    total = (sb.table("draft_fingerprints").select("id", count="exact")
               .eq("project_id", project_id).limit(1).execute()).count or 0
    with_vec = (sb.table("draft_fingerprints").select("id", count="exact")
                  .eq("project_id", project_id)
                  .not_.is_("title_embedding", "null")
                  .eq("embedding_model", model)
                  .limit(1).execute()).count or 0
    return int(total), int(with_vec)


def check_drafts_sql(sb, project_id: str, rows: list[dict],
                     contain_min_sample: int | None = None) -> list[dict] | None:
    """四路比对下推到库里(审计 SUP-002 / ROB-004 / ROB-011 / COR-014)。

    ``rows`` = [{opening_hash, ngram_hashes, title_embedding}, ...]，顺序即
    结果的 ``idx``。返回每条的 ``{idx, best_sim, sim_title, best_j, j_title,
    open_exact, open_title, best_c, c_title, c_sample}``；
    RPC 不存在(migrations/004 没跑)时返回 None, 由调用方降级回 Python 路径。

    ⚠️ 后三列(``best_c`` / ``c_title`` / ``c_sample``)是 **migrations/005** 加的
    包含度那一路。只跑过 004 的库回不出这三列 —— 那**不是错误**, 调用方按
    "这一路没跑"处理(``c_sample`` 取不到就是 0, 包含度不发言), 并在 summary 里
    报 ``containment_skipped_warning``。绝不能当成"包含度 = 0 = 没撞车"。

    为什么值得下推: 原来是把整个项目的指纹(4000 行 × 768 维)拉进 Python 再逐对
    算余弦 —— 百 MB 级传输 + 三千万次乘加, 单次数十秒, 且占着 uvicorn 线程池
    的一个槽。三条审计发现(SUP-002 慢 / ROB-011 OOM / ROB-004 线程池饥饿)是
    同一个根因。

    ⚠️ 只有"RPC 不存在"才降级。权限错、参数错、库故障一律上抛 —— 查重是硬闸。

    ⚠️ 签名有两版, 与 ``commit_fingerprints`` 同一套路。``_contain_min_sample``
    是修 Codex 那条 P1 时加的: 样本量必须在**取最大之前**过闸, 而阈值只能在
    fingerprint.py 里定义一处、传下来。只跑过 004 的库没有这个参数, PostgREST
    会报"找不到函数" —— 那不是故障, 所以先按 3 参调, 报找不到再按 2 参试一次,
    两次都找不到才是真的没跑迁移。
    """
    if not rows:
        return []
    base = {"_project_id": project_id, "_rows": rows}
    attempts = []
    if contain_min_sample is not None:
        attempts.append(("005", {**base, "_contain_min_sample": contain_min_sample}))
    attempts.append(("004", base))

    for n, (tag, args) in enumerate(attempts):
        try:
            res = sb.rpc("deskcore_check_drafts", args).execute()
        except Exception as exc:
            if not rpc_missing(exc):
                raise
            if n + 1 < len(attempts):
                logger.warning(
                    "deskcore_check_drafts 没有 migrations/005 那版签名, 回退到 "
                    "2 参旧版 —— 前三路照常, **包含度那一路不发言**"
                    "(短稿照搬长稿在 check 这一关拦不住)。跑 migrations/005 修好。")
                continue
            return None
        else:
            return res.data or []
    return None


def fingerprint_counts(sb, project_ids: list[str]) -> dict[str, int] | None:
    """一次拿一批项目的指纹条数(审计 SUP-004)。RPC 不存在时返回 None。"""
    if not project_ids:
        return {}
    try:
        res = sb.rpc("deskcore_fingerprint_counts",
                     {"_project_ids": list(project_ids)}).execute()
    except Exception as exc:
        if rpc_missing(exc):
            return None
        logger.exception("fingerprint counts failed")
        return None
    return {str(r["project_id"]): int(r.get("n") or 0) for r in (res.data or [])}


def legacy_versions(sb, project_id: str, limit: int = 5000) -> list[dict]:
    """项目历史成稿(items × versions), 供指纹回填。

    为什么必须有这个: 迁移建的是【空表】, 而 check_drafts 只读这张表, 只有
    commit_drafts 会往里写。也就是说刚上线那天, 号称"比对全量历史"的硬闸
    实际上一条历史都没有 —— 老稿子的重复会原样放行(codex review P1)。

    只取每个 item 的 best/最新版本(与 db.list_example_items 同口径), 因为中间
    的迭代版本不是"发出去的东西", 拿它们当查重基线会误伤后续正常改写。

    ⚠️ 必须翻页(审计 COR-006)。原来是裸 ``.limit(5000)`` —— 而 PostgREST 的
    ``db-max-rows`` 默认 1000, 服务端会把它**静默钳到 1000**。于是回填只覆盖
    最近 1000 条 item, 却报出一个看起来像全量的 total: 更老的稿子从来没进过
    指纹库, 跟它们的重复永远查不出来。而"比对全量历史"正是这套硬闸的卖点。
    ``id`` 做次级排序键: bulk insert 下 created_at 大量并列, 只按它翻页会漏行。
    """
    return [r for pg in legacy_version_pages(sb, project_id, limit=limit) for r in pg]


def legacy_version_pages(sb, project_id: str, limit: int | None = 5000,
                         page: int = PAGE):
    """``legacy_versions`` 的逐页形态(审计 ROB-011)。

    回填该走这个: 一页处理完就丢, 内存峰值只跟 ``page`` 有关。攒成一个大列表
    的话, 5000 条历史成稿的全文 + 每条 768 个 Python float 会同时在内存里 ——
    容器 OOM 就是这么来的, 而回填偏偏是"部署后每个项目必跑一次"的动作。

    ``limit=None`` 关掉上限, 扫全量。**只有核对用途该这么调**(见
    ``core.backfill_gap``): 一个"验收标准"如果只看了前 5000 条就报"回填完了",
    那它报的绿是假的 —— 而验收标准报假绿正是本仓最怕的那类失败。回填本身仍然
    带上限, 上限就是它的已知边界, 不该被核对路径顺手抹掉。(codex review · #63)
    """
    def _build(off, lim):
        return (sb.table("items")
                  .select("id, best_version_id, user_id, created_at, "
                          "versions(id, title, body, version_num, embedding), "
                          "batches!inner(project_id)")
                  .eq("batches.project_id", project_id)
                  .order("created_at", desc=True)
                  .order("id", desc=True)
                  .range(off, off + lim - 1))

    try:
        for raw in _paged_iter(_build, page=page, hard_cap=limit):
            out: list[dict] = []
            for item in raw:
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
            yield out
    except Exception:
        logger.exception("read legacy versions failed (project=%s)", project_id)
        raise


def existing_fingerprint_version_ids(sb, project_id: str) -> set[str]:
    """已经有指纹的 version_id —— 回填要幂等, 重跑不能造重复行。

    ⚠️ 必须翻页(审计 COR-005)。这是**幂等性本身所依赖的那个集合**: 原来是一次
    裸 select 无 limit 无翻页, 被 PostgREST 的 ``db-max-rows``(默认 1000)静默
    截断之后, 超出的 version_id 看起来"还没有指纹" —— 重跑 backfill 会给它们
    **再插一遍**。而重复指纹会抬高后续 Jaccard / 余弦, 把正常选题误判成撞车。
    幂等的读一旦不全, 幂等就是假的, 且没有任何报错。
    ``id`` 做排序键保证翻页确定(主键唯一稳定)。
    """
    try:
        rows = _paged(lambda off, lim: (
            sb.table("draft_fingerprints").select("version_id")
              .eq("project_id", project_id)
              .not_.is_("version_id", "null")
              .order("id")
              .range(off, off + lim - 1)
        ))
        return {r["version_id"] for r in rows if r.get("version_id")}
    except Exception:
        logger.exception("read existing fingerprint version_ids failed")
        raise


def fingerprint_row_count(sb, project_id: str) -> int:
    """这个项目在指纹表里**一共**多少行 —— 含 version_id 为空的那些。

    ⚠️ 与 ``existing_fingerprint_version_ids`` 不是一回事, 别拿后者的 len 冒充
    它: 那个函数 ``.not_.is_("version_id","null")`` 过滤掉了空 version 的行、
    而且返回的是 **set**(去重)。也就是说它数不到写作台经 commit_drafts 写进来
    的那批 —— 那批恰恰**就是** version_id 为空的。(codex review · #63: 我原来
    正是拿 len(那个 set) 当"总行数"报出去, 说明文字还专门写着"含 commit_drafts
    写进来的", 正好说反。)
    """
    res = (sb.table("draft_fingerprints").select("id", count="exact")
             .eq("project_id", project_id).limit(1).execute())
    return int(res.count or 0)


# ``set_fingerprint_vector`` 的 ``expect`` 哨兵: "这一行本来就该没有向量"。
# 用独立哨兵而不是 None —— None 是个**真实取值**(有向量但不知道哪个模型产的)。
VECTOR_ABSENT = object()


def fingerprints_needing_vectors(sb, project_id: str, model: str,
                                 limit: int = 2000) -> list[dict]:
    """指纹库里【标题向量不可用】的行。给 reembed 用。

    "不可用" 有三种, 三种都要修:

      1. ``title_embedding IS NULL``            —— 从来没算过(原来只有这一种);
      2. 有向量但 ``embedding_model IS NULL``   —— 来路不明(老行, 或 backfill
         复用了 ``versions.embedding`` 这种没有模型标记的历史向量);
      3. 有向量但 ``embedding_model`` 不是当前模型 —— 换过模型了。

    ⚠️ **后两种是 2026-08-26 换模型时补的(codex review · #65 P1)。** 原来只扫
    第 1 种, 于是换模型之后老行既进不了比对(模型不符被排除)、又永远不会被
    重算 —— 卡在一个**没有出口**的状态里, 而且不报错。修一半比不修更坏。

    为什么不能靠 backfill 补: backfill 扫的是 items × versions, 而 WorkBuddy
    写的稿子 version_id 是空的、根本不在 autowriter.versions 里。欠费那几天
    commit 进来的行, backfill 永远看不到 —— 只能从指纹表这一侧修。

    ⚠️ 拆成三次查询而不是一句 ``.or_()``: PostgREST 的 or 语法(``a.is.null,
    b.neq.x``)在假件里是 no-op, 写成 or 就等于这三条过滤【没有任何测试守着】,
    而这里恰恰是"漏一种就卡死"的地方。三次平凡过滤换来三条都能被断言,
    值这个来回 —— reembed 是运维命令, 不是热路径。

    ⚠️ 同 legacy_versions: 裸 ``.limit(2000)`` 会被服务端钳到 db-max-rows,
    reembed 一次只修最近的那批却报"补完了"(审计 COR-006)。翻页取全, ``id``
    做次级键。整批读完之后才开始写(set_fingerprint_vector), 所以翻页期间
    过滤条件不会被自己改动影响。
    """
    def _page(extra):
        return _paged(
            lambda off, lim: extra(
                sb.table("draft_fingerprints")
                  .select("id, title, embedding_model")
                  .eq("project_id", project_id)
                  .neq("title", ""))
                .order("created_at", desc=True)
                .order("id", desc=True)
                .range(off, off + lim - 1),
            hard_cap=limit,
        )

    rows: list[dict] = []
    seen: set[str] = set()
    for tag, extra in (
        # 1 · 压根没算过
        (VECTOR_ABSENT, lambda q: q.is_("title_embedding", "null")),
        # 2 · 有向量, 但来路不明
        (None, lambda q: q.not_.is_("title_embedding", "null")
                          .is_("embedding_model", "null")),
        # 3 · 有向量, 但是别的模型产的
        ("other", lambda q: q.not_.is_("title_embedding", "null")
                             .not_.is_("embedding_model", "null")
                             .neq("embedding_model", model)),
    ):
        for r in _page(extra):
            rid = str(r.get("id"))
            if rid in seen:
                continue
            seen.add(rid)
            # 回写时要做 CAS, 得知道"我以为它现在是什么"。第 3 类用行里读到的
            # 真实模型名, 不用占位的 "other"。
            r["_expect"] = (r.get("embedding_model") if tag == "other" else tag)
            rows.append(r)
            if len(rows) >= limit:
                return rows
    return rows


def set_fingerprint_vector(sb, row_id: str, vec: list[float], model: str,
                           *, expect=VECTOR_ABSENT) -> bool:
    """给一条指纹写标题向量 + 记下是哪个模型产的。**带 CAS**。

    ``expect`` 说的是"我读到这一行的时候它是什么状态", 三种:

      · ``VECTOR_ABSENT``(默认) —— 当时没有向量。只在仍然没有时才写。
      · ``None``               —— 当时有向量但没有模型标记(来路不明)。
      · ``"<模型名>"``          —— 当时是这个模型产的向量。

    为什么要 CAS 而不是无条件覆盖: reembed 是**先整批读、再逐行写**的(见
    fingerprints_needing_vectors 的说明), 读和写之间可能有别的进程把同一行
    刷成了当前模型。无条件覆盖会用一个更旧的批次盖掉更新的结果, 而且不报错。

    原来的写法是硬编码 ``.is_("title_embedding","null")`` —— 那既是 CAS 也是
    "只补空行"的过滤器。换模型之后这两件事必须分开: 过滤在
    fingerprints_needing_vectors, CAS 在这里。(codex review · #65 P1)
    """
    q = (sb.table("draft_fingerprints")
           .update({"title_embedding": vec, "embedding_model": model})
           .eq("id", row_id))
    if expect is VECTOR_ABSENT:
        q = q.is_("title_embedding", "null")
    elif expect is None:
        q = q.not_.is_("title_embedding", "null").is_("embedding_model", "null")
    else:
        q = q.eq("embedding_model", expect)
    return bool(q.execute().data)


def fingerprint_pages(sb, project_id: str, page: int = PAGE):
    """项目的全部指纹行, 逐页 yield。给"换了规范化口径之后重算"用。

    只取重算需要的列: ``opening`` 能重算 opening_hash, ``version_id`` 决定
    ngram_hashes 能不能重算(正文只在 autowriter.versions 里, 指纹表**不存正文**)。

    逐页而不是一次拉全 —— 同 legacy_version_pages 的理由(审计 ROB-011)。
    ``id`` 做次级排序键, 否则 created_at 大量并列时翻页会漏行。
    """
    return _paged_iter(
        lambda off, lim: (
            sb.table("draft_fingerprints")
              .select("id, version_id, opening, opening_hash")
              .eq("project_id", project_id)
              .order("created_at").order("id")
              .range(off, off + lim - 1)
        ),
        page=page,
    )


def version_bodies(sb, version_ids: list[str]) -> dict[str, str]:
    """按 version_id 取正文。分块 + 翻页, 理由同 rule_counts_bulk。"""
    out: dict[str, str] = {}
    for chunk in db._in_chunks(list(version_ids)):
        for row in _paged(lambda off, lim, _c=chunk: (
                sb.table("versions").select("id, body")
                  .in_("id", _c)
                  .order("id")
                  .range(off, off + lim - 1))):
            out[str(row["id"])] = row.get("body") or ""
    return out


def update_fingerprint_hashes(sb, row_id: str, *, opening_hash: str,
                              ngram_hashes: list[str] | None) -> bool:
    """重写一行的确定性指纹。``ngram_hashes=None`` 表示这行的正文找不回来,
    只更新 opening_hash, 四字串那一路保持原样(并由调用方报出去)。"""
    patch: dict = {"opening_hash": opening_hash}
    if ngram_hashes is not None:
        patch["ngram_hashes"] = ngram_hashes
    res = (sb.table("draft_fingerprints").update(patch)
             .eq("id", row_id).execute())
    return bool(res.data)


def write_fingerprints(sb, rows: list[dict]) -> int:
    """直插指纹(不查重)。只给【回填】用 —— 回填的是已发生的历史, 本来就该原样入库。

    定稿入库【不要】走这里, 走 commit_fingerprints_atomic。
    """
    if not rows:
        return 0
    sb.table("draft_fingerprints").insert(rows).execute()
    return len(rows)


# ── 定稿的身份 (batch / item / version) ───────────────────────────────────

# 写作台产出的 version 在 ``versions.ai_engine`` 里的取值。
#
# 为什么不写具体模型名: 稿子是**调用方**(WorkBuddy / Claude Code / 任何接了
# 这个 MCP 的客户端)写的, deskcore 只收成品 —— 服务端并不知道对面是哪个模型,
# 而且客户端报上来的也不可信。写一个诚实的"来自写作台"比编一个模型名好。
#
# TV 的 ``v_model_comparison`` 按 ``ai_engine`` GROUP BY 出胜率, 所以这个值会
# 自成一档: 「UI 批量生成(claude / gemini)」vs「写作台手写」。那正是想看的对比。
# 那个 view 已经在排除 ``'truth_vault_sync'``(TV 回写的占位版本), 同一个模式。
DESKCORE_AI_ENGINE = "deskcore"

# 写作台定稿落库时给 items 的状态。
#
# ⚠️ **必须是 pending, 不能是 approved** —— 这不是保守, 是避开一个已经修过一次
#    的 bug 的新入口。truth-vault 的 ``sync_autowriter_decisions_to_prepublish.py``
#    按 ``status in ('approved', 'needs_revision')`` 捞行, 把捞到的**全部**写进
#    ``prepublish_evaluations`` 且 ``evaluator_type='human'`` —— 它现在**不读**
#    我们刚加的 ``decision_source``(跨库审计 COR-004 的那三列 TV 侧还没接)。
#
#    也就是说: 这里只要写 'approved', 每一条写作台定稿都会立刻变成一条"人工评价"
#    去校准 TV 的评估模型。而写作台从来没有"打回"这个动作, 灌进去的会是**清一色
#    正例**。COR-004 治的正是"机器判定被当人工反馈", 这里换个门重犯一次。
#
#    pending + decision_source 留 NULL = 「这条稿子存在, 但没有任何审核决策」——
#    这是实话, 而且 TV 的 ``.in_(...)`` 过滤天然把它排除在外。
#
#    代价: 这些 item 会出现在审核页的待审列表里。导出中心也只导 approved, 所以
#    写作台的稿子走自己的导出(tools.export_drafts), 不蹭那条路。
DESKCORE_ITEM_STATUS = "pending"


def mint_draft_identity(sb, project_id: str, user_id: str, tactic: str,
                        entries: list[dict]) -> dict:
    """给写作台的定稿建 batch → items → versions, 让它们在库里**有身份**。

    ── 为什么非有不可 ──────────────────────────────────────────────────
    在此之前, ``commit_drafts`` 只写 ``draft_fingerprints``, ``version_id``
    留空。core.py 里那段注释自己写着"WorkBuddy 写的稿子 version_id 为空、根本
    不在 autowriter.versions 里"。后果有三层, 一层比一层远:

      · 回填(``backfill_fingerprints`` 走 items × versions)永远看不到它们;
      · 角度台账的 ``consumed_version_id`` 只能塞一个 hash 出来的假 UUID;
      · **导出的 lineage 没有 version_id 可写** —— 而 TV 的
        ``v_model_comparison`` 正是 JOIN 在 ``autowriter.versions.id`` 上。
        于是"写作台写的稿子发出去爆没爆"这件事, 在数据上根本问不出来。

    ── id 是调用方先造好的 ────────────────────────────────────────────
    ``entries`` 里的 ``version_id`` 必须是**已经确定**的 UUID: 指纹行在这之前
    就已经带着它写进库了(见 core.commit_drafts 的顺序说明)。这里做的是把那个
    id 兑现成真实的 versions 行, 不是分配 id。

    每次调用建**一个** batch —— 一次 commit 就是一次交付, 这是最自然的分组,
    也让"这批是写作台写的"在 UI 里一眼可见。

    ── 半途失败要**报出已经建成的那部分** ──────────────────────────────
    返回 ``{"batch_id", "versions", "error"}``; ``error`` 非空 = 没建完。

    这里刻意不上抛。上抛的话调用方只能把整次 mint 当作没发生 —— 而 5 条里前 2 条
    的 items/versions **已经在库里了**, 连 batch_id 一起丢掉之后, 那两条谁也找不
    回来: 它们的指纹指向真实存在的 version, 却没有任何人知道该去导出它们。
    行在库里而调用方以为没有, 是比"报了个建不成的 id"更难查的一种不一致。

    ⚠️ 但**绝不**把没建成的 id 混进 ``versions`` —— 那个字典是"真的有这一行"的
    唯一凭据, 调用方拿它去导出。宁可少报, 不可多报。
    """
    if not entries:
        return {"batch_id": None, "versions": {}, "error": None}

    minted: dict[str, str] = {}
    batch_id = None
    try:
        batch = db.create_batch(
            sb, user_id=user_id, project_id=project_id, tactic=tactic or "",
            params={"source": "deskcore"},
            ai_engines=[DESKCORE_AI_ENGINE])
        batch_id = batch["id"]
        _mint_entries(sb, batch_id, user_id, entries, minted)
    except Exception as exc:                    # noqa: BLE001
        logger.exception("mint draft identity failed (project=%s, batch=%s, "
                         "已建成 %d/%d)", project_id, batch_id,
                         len(minted), len(entries))
        return {"batch_id": batch_id, "versions": minted,
                "error": f"{type(exc).__name__}: {exc}"}
    return {"batch_id": batch_id, "versions": minted, "error": None}


def _mint_entries(sb, batch_id: str, user_id: str, entries: list[dict],
                  minted: dict[str, str]) -> None:
    """逐条建 item + version, 建成一条就往 ``minted`` 里记一条。

    ``minted`` 是**传进来的**而不是返回的 —— 中途抛异常时, 上面那层要拿到已经
    建成的那部分。
    """
    for e in entries:
        item = (sb.table("items").insert({
            "batch_id": batch_id,
            "user_id": user_id,
            "status": DESKCORE_ITEM_STATUS,
        }).execute().data or [{}])[0]
        item_id = item.get("id")
        if not item_id:
            # 不往下走: versions.item_id 是可空的 FK, 带 NULL 插进去**不会报错**,
            # 只会留下一条挂不到任何 item 上的版本 —— 导出时 item_id 那一列是空的,
            # TV 那边归因到一半断掉, 而这里一路都是"成功"。
            raise RuntimeError(
                f"建 item 之后没拿到 id(batch={batch_id}) —— PostgREST 没回插入行, "
                "多半是 service client 的 returning 行为变了")

        # ⚠️ item 建完、version 没建成的话, 那个 item 会**留在审核页上**: 一条没有
        #    任何版本的空待审稿, 谁也不知道它是什么。所以这一步失败要把刚建的
        #    item 收掉再上抛。(codex review P1 的残留部分)
        #
        #    没有做成事务 RPC: 那要新加一条迁移 + 一个 plpgsql 函数, 而这里真正会
        #    留下垃圾的只有"item 建了 version 没建"这一种中间态, 补一次回收就够。
        #    batch 本身留着是**有意的** —— 它是已经建成的那几条的归属, 上一层要靠
        #    batch_id 把它们交回给调用方。
        try:
            sb.table("versions").insert({
                # ⚠️ 显式 id: 指纹行里写的就是它, 让数据库另发一个就等于两边对不上。
                "id": e["version_id"],
                "item_id": item_id,
                "version_num": 1,     # 新建的 item, 不可能有并发的第二版
                "ai_engine": e.get("ai_engine") or DESKCORE_AI_ENGINE,
                "title": e.get("title") or "",
                "body": e.get("body") or "",
                "keywords": e.get("keywords") or [],
            }).execute()

            # best_version_id 指向唯一那一版 —— 导出和"挑代表版本"都按它取。
            sb.table("items").update(
                {"best_version_id": e["version_id"]}).eq("id", item_id).execute()
        except Exception:
            try:
                db.delete_items(sb, [item_id])
            except Exception:
                # 回收失败只记一行 —— 真正要往上报的是原始异常, 别被它顶掉。
                logger.exception("orphan item %s cleanup failed", item_id)
            raise

        # 三次写全成了才记 —— minted 是"真的有这一行"的凭据。
        minted[e["version_id"]] = item_id


def drafts_for_export(sb, project_id: str, *, batch_id: str | None = None,
                      version_ids: list[str] | None = None,
                      limit: int = 200) -> list[dict]:
    """取要导出的稿子, 产出 ``exporter`` 认的行形状(带四个 lineage id)。

    ── 为什么不复用导出中心那条路 ──────────────────────────────────────
    ``app._collect_approved_items`` 第一行就是 ``if item["status"] != "approved"``。
    而写作台的稿子是 ``pending`` 且**永远不会**变成 approved —— 它们从没进过审核
    流(理由见 ``DESKCORE_ITEM_STATUS``)。拿那条路导等于永远导出空。

    所以这里按 **batch / version 直接点名**, 不看 status。语义也更对: 调用方要导的
    是"我刚提交的那一批", 不是"审核通过的那些"。

    ``batches!inner(project_id)`` 这个 inner join 顺带把归属钉死: 别的项目的
    batch_id 传进来会查不到行, 而不是导出别人的稿子。

    ⚠️ 出错**上抛**, 不返回空列表。空列表会被 core 报成 "没找到可导的稿子",
       于是一次数据库故障和"这批确实没有"长得一模一样 —— 调用方看到一个像是
       成功的 count: 0 就不会重试, 也不会报告故障。(codex review P2)
    """
    if version_ids:
        return _export_rows_by_version(sb, project_id, version_ids, limit)
    if batch_id:
        return _export_rows_by_batch(sb, project_id, batch_id, limit)
    return []


def _export_rows_by_version(sb, project_id: str, version_ids: list[str],
                            limit: int) -> list[dict]:
    """点名了 version 就**从 versions 表查**, 过滤下推到数据库。

    ⚠️ 原来这条路是"先取项目里最早的 200 个 item, 再在 Python 里挑 version"。
       项目一旦超过 200 个 item, **刚 commit 的那一批永远落在窗口外** —— 拿它
       返回的 version_id 原样去导出会得到"没找到可导的稿子", 而 id 明明是对的。
       ``order(created_at)`` 还是升序(最早的在前), 所以最新的那批是第一个被挤掉
       的。(codex review P1)

    ``items!inner(...)`` 一路 inner 到 batches, 归属仍然由数据库钉死。
    """
    rows: list[dict] = []
    for chunk in db._in_chunks(list(dict.fromkeys(version_ids)), 100):
        res = (sb.table("versions")
                 .select("id, title, body, keywords, ai_engine, version_num, "
                         "items!inner(id, batch_id, batches!inner(project_id))")
                 .in_("id", chunk)
                 .eq("items.batches.project_id", project_id)
                 .limit(limit)
                 .execute())
        for v in (res.data or []):
            item = v.get("items") or {}
            rows.append(_export_row(project_id, item, v))
        if len(rows) >= limit:
            break
    return rows[:limit]


def _export_rows_by_batch(sb, project_id: str, batch_id: str,
                          limit: int) -> list[dict]:
    """只给了 batch 就取每个 item 的**代表版本**(与导出中心同口径)。"""
    res = (sb.table("items")
             .select("id, batch_id, best_version_id, created_at, "
                     "versions(id, title, body, keywords, ai_engine, version_num), "
                     "batches!inner(project_id)")
             .eq("batches.project_id", project_id)
             .eq("batch_id", batch_id)
             .order("created_at", desc=False)
             .limit(limit)
             .execute())

    out: list[dict] = []
    for item in (res.data or []):
        chosen = _representative_version(item)
        if chosen is not None:
            out.append(_export_row(project_id, item, chosen))
    return out


def _representative_version(item: dict) -> dict | None:
    """一个 item 导出**哪一版** —— 与 ``app._collect_approved_items`` 同一口径。

    ⚠️ ``best_version_id`` 为空是**正常状态**, 不是异常: 每次「AI 迭代」都会
       显式清掉这个指针(app.py 的 R-036 —— 不清的话卡片和导出会一直停在迭代前
       的旧版本)。原来这里写的是 ``if best and v["id"] != best: continue``,
       于是 best 为空时**每一版都会被 append** —— 导出的表里同一篇稿子出现好几
       行, 新旧混在一起, 而每一行看上去都是合法的。用户照着它发, 发出去的可能
       是被迭代掉的那一版。(codex review P1)

    指针指向一个不在列表里的 version 时也回退到最新版 —— 与那边的
    ``next(..., versions[-1])`` 一致。
    """
    versions = sorted(item.get("versions") or [],
                      key=lambda v: v.get("version_num") or 0)
    if not versions:
        return None
    best = item.get("best_version_id")
    if best:
        return next((v for v in versions if v.get("id") == best), versions[-1])
    return versions[-1]


def _export_row(project_id: str, item: dict, v: dict) -> dict:
    """exporter 认的行形状。lineage 的 key 与 ``exporter.LINEAGE_COLUMNS`` 对应。"""
    return {
        "title": (v.get("title") or "").strip(),
        "body": (v.get("body") or "").strip(),
        "keywords": v.get("keywords") or [],
        "ai_engine": v.get("ai_engine") or "",
        "version_num": v.get("version_num") or 1,
        "project_id": project_id,
        "batch_id": item.get("batch_id"),
        "item_id": item.get("id"),
        "version_id": v.get("id"),
    }


def commit_fingerprints_atomic(sb, project_id: str, rows: list[dict],
                               user_id: str | None,
                               ngram_hard: float,
                               *,
                               contain_hard: float | None = None,
                               contain_min_sample: int | None = None,
                               ) -> list[dict] | None:
    """定稿入库 + 【同一事务内重新查一遍】, 走 deskcore_commit_fingerprints RPC。

    为什么不能直接 insert: check_drafts 和 commit_drafts 是两次独立调用。两个
    队友各自 check 时都看到同一份旧指纹集、双双 pass, 然后各自 commit —— 两篇
    撞车的稿子都进了库。发牌那边已经用 advisory lock 串行化了, 这边不能留着。

    返回每条的 {idx, status, collided_with, detail}; RPC 不存在(迁移没跑)时
    返回 None, 由调用方降级。

    ⚠️ **两个版本的签名都要试**(审计 COR-014)。migrations/005 把这个函数从 4 参
    改成了 6 参(多了包含度的两个阈值)。只跑过 001~004 的库上还是 4 参版本, 而
    PostgREST 对"参数对不上"的报错文本里带 ``does not exist`` —— 那正好被
    ``rpc_missing()`` 认成"迁移压根没跑", 于是**整条原子写入路径会静默退化成
    直插**, check/commit 之间的竞态窗口重新打开, 而日志只说"migrations/001
    还没跑?"(完全误导)。分两步升级的库上这一定会发生。

    所以顺序是: 先按 6 参调; 只有它报"找不到"时才回退到 4 参再试一次;
    两次都找不到才是真的没跑迁移。
    """
    if not rows:
        return []
    base = {
        "_project_id": project_id,
        "_rows": rows,
        "_user_id": user_id,
        "_ngram_hard": ngram_hard,
    }
    attempts = []
    if contain_hard is not None or contain_min_sample is not None:
        wide = dict(base)
        if contain_hard is not None:
            wide["_contain_hard"] = contain_hard
        if contain_min_sample is not None:
            wide["_contain_min_sample"] = contain_min_sample
        attempts.append(("005", wide))
    attempts.append(("001", base))

    for n, (tag, args) in enumerate(attempts):
        try:
            res = sb.rpc("deskcore_commit_fingerprints", args).execute()
        except Exception as exc:
            if not rpc_missing(exc):
                raise
            if n + 1 < len(attempts):
                logger.warning(
                    "deskcore_commit_fingerprints 没有 migrations/005 那版签名, "
                    "回退到 4 参旧版 —— 写入侧仍会关竞态窗口, 但**不做包含度重查**"
                    "(短稿照搬长稿在 commit 这一关拦不住)。跑 migrations/005 修好。")
                continue
            logger.error(
                "deskcore_commit_fingerprints RPC 不存在(两种签名都试过) —— "
                "migrations/001 还没跑? 本次降级为直插(并发 check/commit 可能撞车)。")
            return None
        else:
            if tag == "001" and len(attempts) > 1:
                _rpc_missing_telemetry_commit_narrow()
            return res.data or []
    return None


def _rpc_missing_telemetry_commit_narrow() -> None:
    """回退到 4 参旧版时埋一行点。静默降级最怕的就是没人知道它降级了。"""
    try:
        import telemetry
        telemetry.log_event("deskcore_rpc_signature_old",
                            rpc="deskcore_commit_fingerprints",
                            hint="跑 migrations/005_deskcore_containment.sql")
    except Exception:
        pass


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
    """取最近的未吸收精修。**必须带 id** —— 销账要按这批的确切 id 来, 见
    mark_edits_distilled 的说明。
    """
    try:
        res = (sb.table("style_edits")
                 .select("id, ai_title, ai_body, my_title, my_body, note")
                 .eq("project_id", project_id).eq("user_id", user_id)
                 .eq("distilled", False)
                 .order("created_at", desc=True).limit(limit).execute())
        return res.data or []
    except Exception:
        logger.exception("read style_edits failed")
        return []


def mark_edits_distilled(sb, project_id: str, user_id: str,
                         edit_ids: list[str]) -> int:
    """把【指定的这几条】精修标记为已吸收。返回真的改到了几行。

    ⚠️ edit_ids 是必需的, 不能退回"把这个人所有未吸收的都标掉"。
    蒸馏任务是一份【快照】(默认只取最近 8 条), 而笔记只覆盖了快照里那几条。
    按 user+project 全量销账会吃掉两类不在快照里的行:
      · 待吸收超过 8 条时, 第 9 条往后的从没进过任何一份笔记
      · 从"拿到任务"到"写回笔记"之间新 record_edit 进来的那些
    它们会从 pending_distillation 里消失, 却从来没影响过笔记 —— 用户喂了稿子,
    计数归零看着正常, 而那几条精修等于白喂。(codex review #56 P1)
    """
    if not edit_ids:
        return 0
    try:
        res = (sb.table("style_edits").update({"distilled": True})
                 .eq("project_id", project_id).eq("user_id", user_id)
                 .in_("id", list(edit_ids))
                 .eq("distilled", False).execute())
        return len(res.data or [])
    except Exception:
        logger.exception("mark style_edits distilled failed (notes are saved)")
        return 0


def count_pending_distillation(sb, project_id: str, user_id: str) -> int:
    """还没被吸收进调校笔记的精修条数。

    蒸馏搬到调用方模型之后, record_edit(存 diff, 交任务) 和 save_my_style(写回)
    是两步。中间断掉的话 diff 还在、笔记没变 —— "喂了稿子却没变得更像我", 而且
    没有任何报错。my_style 把这个数报出来, 断点才看得见。
    """
    try:
        return (sb.table("style_edits").select("id", count="exact")
                  .eq("project_id", project_id).eq("user_id", user_id)
                  .eq("distilled", False)
                  .limit(1).execute()).count or 0
    except Exception:
        logger.exception("count pending distillation failed")
        return 0


def count_style_edits(sb, project_id: str, user_id: str) -> int:
    try:
        return (sb.table("style_edits").select("id", count="exact")
                  .eq("project_id", project_id).eq("user_id", user_id)
                  .limit(1).execute()).count or 0
    except Exception:
        logger.exception("count style_edits failed")
        return 0
