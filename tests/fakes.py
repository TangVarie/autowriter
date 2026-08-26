"""共享的 PostgREST 假件(审计 SUP-024 / §7.1)。

CI 的那些 heredoc 块里已经各自写过四五份形状相近的假件 —— 每次都要重新想
"``.not_.is_()`` 怎么表达"、"``count='exact'`` 是在 range 之前还是之后算"。
这里放**一份**, 新用例直接用。

设计取舍:

  · **只做真的被用到的那部分**。不试图实现 PostgREST 的全部语义 —— 那会变成
    第二个需要被测试的东西。缺什么加什么, 加的时候顺手写清楚语义。
  · **记录每一次调用**(``calls``)。很多断言要验的不是"结果对不对"而是
    "发了几次请求、请求里带了什么" —— 比如 SUP-004 的"查询次数与项目数无关"、
    codex P2 的"单次 .in_() 的 id 数 ≤ 100"。
  · **count 在 range 之前算**。这是 PostgREST 的真实行为(``count=exact`` 报的是
    满足过滤条件的总行数, 与分页无关), 也是 db.py 里多处翻页逻辑的前提。
  · ``max_rows`` 模拟服务端的 ``db-max-rows`` 钳位 —— 这是本仓踩过最多次的坑
    (COR-005/006/008 三条都是它), 假件必须能重现它。
"""

from __future__ import annotations

import uuid as _uuid

# 建表 SQL 里写了 NOT NULL、而且【有代码依赖这个约束真的会拦】的列。
# 不求全 —— 按本文件的第一条取舍, 只做真的被用到的那部分。
#
# 目前只有一条: ``draft_fingerprints.project_id``。``core.migration_state``
# 探 INSERT 权限时故意写 None 进去, 靠这条约束保证【无论如何插不进】。
# 见 migrations/001_deskcore.sql:110 与 000_baseline.sql 里同一张表的定义。
NOT_NULL_COLUMNS: dict[str, set[str]] = {
    "draft_fingerprints": {"project_id"},
}


class FakeResponse:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class FakeQuery:
    """链式 builder。每个方法返回 self, ``execute()`` 交回 FakeClient 求值。"""

    def __init__(self, client: "FakeClient", table: str):
        self.client = client
        self.table_name = table
        self.filters: list[tuple] = []
        self.cols = ""
        self.count_mode = None
        self.rng: tuple[int, int] | None = None
        self.orders: list[str] = []
        self._negate_next = False
        self.payload = None
        self.op = "select"

    # ── 取列 ────────────────────────────────────────────────────────
    def select(self, cols="*", count=None):
        self.cols, self.count_mode = cols, count
        return self

    # ── 过滤 ────────────────────────────────────────────────────────
    def eq(self, key, value):
        self.filters.append(("eq", key, value))
        return self

    def neq(self, key, value):
        self.filters.append(("neq", key, value))
        return self

    def in_(self, key, values):
        self.filters.append(("in", key, tuple(values)))
        return self

    def lt(self, key, value):
        self.filters.append(("lt", key, value))
        return self

    def gt(self, key, value):
        self.filters.append(("gt", key, value))
        return self

    def is_(self, key, value):
        # ``.not_.is_(k, "null")`` 与 ``.is_(k, "null")`` 是一对反义, 假件要分开
        self.filters.append(("isnot" if self._negate_next else "is", key, value))
        self._negate_next = False
        return self

    def or_(self, *args, **kwargs):
        # 真实语义太杂(``a.is.null,b.eq.x``), 调用方通常还会在 Python 侧复核一遍
        # (deskcore/store.py 的 _rows 就是这么写的)。这里当 no-op, 需要精确
        # 语义的用例请自己在断言里查 ``calls``。
        return self

    @property
    def not_(self):
        self._negate_next = True
        return self

    # ── 排序 / 分页 ──────────────────────────────────────────────────
    def order(self, column, desc=False, **kwargs):
        self.orders.append(column)
        return self

    def limit(self, n):
        self.rng = (0, n - 1)
        return self

    def range(self, start, end):
        self.rng = (start, end)
        return self

    # ── 写 ──────────────────────────────────────────────────────────
    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def upsert(self, payload, **kwargs):
        self.op, self.payload = "upsert", payload
        return self

    def delete(self):
        self.op = "delete"
        return self

    def execute(self):
        return self.client._execute(self)


class FakeClient:
    """``rows`` 是 {表名: [行, ...]}; ``rpc_impl`` 是 {函数名: callable(args)}。

    ``max_rows`` 模拟服务端 ``db-max-rows`` 钳位: 每次请求最多回这么多行,
    **不报错**。本仓的静默截断类 bug 全是它造成的, 假件必须能重现。
    """

    def __init__(self, rows=None, rpc_impl=None, max_rows: int | None = None,
                 missing_columns: dict[str, set[str]] | None = None):
        self.rows = {k: list(v) for k, v in (rows or {}).items()}
        self.rpc_impl = rpc_impl or {}
        self.max_rows = max_rows
        # {表名: {列名, ...}} —— 这些列在"库里还没有"。select 到它们时按
        # PostgREST 的真实形态报错, 而不是静默返回。
        #
        # 为什么假件要会这一手: 迁移没跑时**缺列是硬失败**(migrations/006 的
        # 三列就是), 而缺列与"这一行该字段是 NULL"在假件里长得一模一样。分不
        # 开的话, "库缺列会怎样"这类用例只能靠手搭一次性替身 —— 而手搭替身正是
        # 这个文件存在要消掉的东西。
        self.missing_columns = {k: set(v) for k, v in (missing_columns or {}).items()}
        # {表名: {列名, ...}} —— 这些列是 NOT NULL。写 None 进去按 PG 的真实
        # 形态报 23502, 而不是欣然存下一个 None。
        #
        # 为什么假件要会这一手: ``core.migration_state`` 探 INSERT 权限的办法
        # 就是**故意违反非空约束** —— 权限检查在约束检查之前, 所以没权限报
        # 42501、有权限报 23502, 而两种情况都一行都写不进去。这条"写不进去"
        # 的性质是那个探测在**生产库**上安全的全部依据。假件不模拟它的话,
        # 探测在测试里会"插入成功", 于是那条断言守的是一个不存在的世界 ——
        # 与本会话早先 `.select("id")` 那次是同一种假绿。
        self.not_null = {k: set(v) for k, v in (NOT_NULL_COLUMNS).items()}
        self.calls: list[dict] = []
        self.rpc_calls: list[tuple[str, dict]] = []

    def table(self, name):
        return FakeQuery(self, name)

    def rpc(self, name, args):
        self.rpc_calls.append((name, args))
        impl = self.rpc_impl.get(name)

        class _RpcQuery:
            @staticmethod
            def execute():
                if impl is None:
                    # 与 PostgREST 一致: 函数不存在时的报错文本带 PGRST202,
                    # deskcore/store.rpc_missing 认的就是它。
                    raise RuntimeError(
                        "PGRST202 Could not find the function in the schema cache")
                return FakeResponse(impl(args))

        return _RpcQuery()

    # ── 求值 ────────────────────────────────────────────────────────
    @staticmethod
    def _resolve(row, key):
        """按 key 取值, 支持 PostgREST 的 embedded 形态 ``表名.列名``。

        ``.eq("batches.project_id", pid)`` 配 ``batches!inner(project_id)`` 是本仓
        读跨表条件的标准写法(labeled_examples / legacy_versions / drafts_for_export
        都在用)。假件不认这个点号的话, 每一条这样的查询在测试里都**静默返回空**,
        而在真库里好好的 —— 于是"我的查询写对了没有"这件事测试根本管不到。
        """
        cur = row
        for part in key.split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
        return cur

    def _match(self, row, kind, key, value) -> bool:
        cur = self._resolve(row, key)
        if kind == "eq":
            return cur == value
        if kind == "neq":
            return cur != value
        if kind == "in":
            return cur in value
        if kind == "lt":
            return cur is not None and cur < value
        if kind == "gt":
            return cur is not None and cur > value
        if kind == "is":
            return cur is None
        if kind == "isnot":
            return cur is not None
        raise AssertionError(f"假件不认识这个过滤器: {kind}")

    def _execute(self, q: FakeQuery):
        self.calls.append({
            "table": q.table_name, "op": q.op, "cols": q.cols,
            "filters": list(q.filters), "range": q.rng,
            "count": q.count_mode, "orders": list(q.orders),
            "payload": q.payload,
        })
        table = self.rows.setdefault(q.table_name, [])

        # 缺列: PostgREST 的报错文本带 `does not exist`, 也正是
        # deskcore/store.rpc_missing 认的那一句。select 看 cols, 写看 payload。
        absent = self.missing_columns.get(q.table_name)
        if absent:
            touched = set()
            if q.op == "select":
                touched = {c.strip() for c in (q.cols or "").split(",") if c.strip()}
            elif isinstance(q.payload, dict):
                touched = set(q.payload)
            elif isinstance(q.payload, list):
                touched = {k for row in q.payload if isinstance(row, dict) for k in row}
            hit = sorted(touched & absent)
            if hit:
                raise RuntimeError(
                    f'column "{hit[0]}" of relation "{q.table_name}" does not exist')

        if q.op in ("insert", "upsert"):
            payload = q.payload if isinstance(q.payload, list) else [q.payload]
            # NOT NULL: 与 PG 一致报 23502。**在写进 table 之前** —— 这条约束
            # 是 core.migration_state 的 INSERT 权限探测在生产库上安全的依据,
            # 假件放行就等于那条探测的安全性从来没被测过。
            for row in payload:
                if not isinstance(row, dict):
                    continue
                for col in sorted(self.not_null.get(q.table_name, ())):
                    if col in row and row[col] is None:
                        raise RuntimeError(
                            f'{{"code":"23502","message":"null value in column '
                            f'\\"{col}\\" of relation \\"{q.table_name}\\" '
                            f'violates not-null constraint"}}')
            # 真库的 uuid PK 有 DEFAULT uuid_generate_v4(), 插入后 PostgREST 回的
            # 是**带 id 的整行**。假件不补这个的话, 一切"插完拿 id 去建下一层"的
            # 代码(batch → item → version)在测试里都拿到 None, 而在真库里好好的 ——
            # 那种假件说的谎, 测试是看不出来的。
            # 调用方自己带了 id 的(deskcore 建 versions 就是)原样保留。
            for row in payload:
                if isinstance(row, dict) and "id" not in row:
                    row["id"] = str(_uuid.uuid4())
            table.extend(payload)
            return FakeResponse(list(payload))

        hits = [r for r in table
                if all(self._match(r, *f) for f in q.filters)]

        if q.op == "delete":
            for r in hits:
                table.remove(r)
            return FakeResponse(hits)

        if q.op == "update":
            for r in hits:
                r.update(q.payload or {})
            return FakeResponse(hits)

        # select —— count 在分页【之前】算, 与 PostgREST 一致
        total = len(hits)
        if q.rng:
            hits = hits[q.rng[0]:q.rng[1] + 1]
        if self.max_rows is not None:
            hits = hits[:self.max_rows]     # 服务端钳位: 静默截断, 不报错
        return FakeResponse(hits, count=total if q.count_mode == "exact" else None)
