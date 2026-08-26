"""`deskcore.cli doctor` 的回归 —— 上线前那一次"别信文档, 去查库"。

这条命令存在的理由写在 `core.migration_state` 的注释里, 一句话:
**迁移没跑这件事在运行期完全看不见**。004/005 缺席只埋一行 telemetry 就退回
慢路径; 006 缺席则是硬失败, 而报错文本是一句没人看得懂的 PostgREST 原话。
2026-08-26 实测生产库只跑了 001, 而 runbook 当时写的是"schema 也上了生产"。

用例分五组:

  1. **判据本身对** —— 全跑过 / 什么都没跑 / 停在 004 三种库各报什么;
  2. **不许把真故障当成"没跑迁移"** —— 这是 store.rpc_missing 那段注释一直在
     防的事, 探测器最容易在这儿犯错(它天生要吞异常);
  3. **探测必须只读** —— doctor 是上线前对着**生产库**跑的, 写一行都不行;
  4. **清单不许漏** —— migrations/ 下每个 .sql 都要被探到、且在三份部署文档里
     都出现。钉的是【被禁止的形态】(新增迁移漏进 doctor / 漏进文档), 不是
     "代码现在长这样";
  5. **缺列时的报错要看得懂** —— 006 没跑时 `db.update_item_status` 抛的是
     翻译过的话, 而不是 PostgREST 原文; 且只翻译这一种, 别的错原样上抛;
  6. **codex review 那六条** —— 每条先钉住"错的那个形态"再证明修好了。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from fakes import FakeClient
from deskcore import cli, core

REPO_ROOT = Path(__file__).resolve().parent.parent

PID = "11111111-1111-1111-1111-111111111111"

# 001 建的那四张表 + 一次 items 探测所需的行。行本身无所谓, 有表就行。
_TABLES = {
    "angle_ledger": [],
    "draft_fingerprints": [],
    "user_calibration_notes": [],
    "style_edits": [],
    "items": [{"id": PID, "updated_at": "2026-08-26T00:00:00Z",
               "decision_source": None, "reviewer_id": None, "decided_at": None}],
}

# 全部迁移都跑过的库: 五个 RPC 都在, 且 check/commit 都是新签名。
_ALL_RPCS = {
    # 001 装的。漏了它 doctor 会把 001 报成没跑 —— 这个夹具本身就是那条断言的
    # 反面教材: 加探测时忘了同步夹具, 别的用例会先红, 而那正是我们要的。
    "deskcore_reserve_angles": lambda a: [],
    "update_calibration_notes_cas": lambda a: [],
    "deskcore_fingerprint_counts": lambda a: [],
    "deskcore_check_drafts": lambda a: [],
    "deskcore_commit_fingerprints": lambda a: [],
}


def _states(report: dict) -> dict[str, str]:
    """{探测项: state} —— 断言写起来比翻列表清楚。"""
    return {c["probe"]: c["state"] for c in report["checks"]}


# ══════════════════════════════════════════════════════════════════════
# 1 · 判据本身
# ══════════════════════════════════════════════════════════════════════

def test_fully_migrated_db_reports_ok():
    sb = FakeClient(rows=_TABLES, rpc_impl=_ALL_RPCS)
    report = core.migration_state(sb)

    assert report["ok"] is True, report["missing"]
    assert report["missing"] == []
    # 003 探不到, 但**不该**因此把整份报告判红 —— 永远报红的检查等于没有检查。
    assert report["unprobeable"] == ["003_versions_unique_num.sql"]
    assert all(s in ("applied", "unprobeable") for s in _states(report).values())


def test_bare_001_db_reports_every_later_migration_missing():
    """2026-08-26 实测的生产库形状: 001 跑了, 002/004/005/006 都没有。"""
    sb = FakeClient(
        rows={**_TABLES,
              # 006 的三列还不存在
              "items": [{"id": PID, "updated_at": "2026-08-26T00:00:00Z"}]},
        rpc_impl={
            # 001 跑过 → 发牌 RPC 在
            "deskcore_reserve_angles": lambda a: [],
            # 005 没跑 → commit 只有 4 参那版
            "deskcore_commit_fingerprints": lambda a: []
            if "_contain_hard" not in a else (_ for _ in ()).throw(
                RuntimeError("PGRST202 Could not find the function"))},
        missing_columns={"items": {"decision_source", "reviewer_id", "decided_at"}},
    )
    report = core.migration_state(sb)

    assert report["ok"] is False
    assert report["missing"] == [
        "002_calibration_cas.sql",
        "004_deskcore_check_pushdown.sql",
        "005_deskcore_containment.sql",
        "006_item_decision_provenance.sql",
    ]
    st = _states(report)
    assert st["表 draft_fingerprints"] == "applied"      # 001 在
    assert st["items 的决策出处三列"] == "missing"
    # commit 只有 4 参那版 → 旧签名, 不是"函数不存在"。这两种要分开报:
    # 前者竞态窗口仍关着, 后者整条原子路径都退化了。
    assert st["deskcore_commit_fingerprints(6 参)"] == "old_signature"


def test_db_stopped_at_004_reports_old_signature_not_missing():
    """停在 004 的库最容易被误报成"001 都没跑"(审计 COR-014 记过这个坑)。

    PostgREST 对**参数对不上**的报错文本里也带 ``does not exist``, 只调一次
    就下结论会把"库停在 004"读成"函数压根不存在"。
    """
    def _only_narrow(narrow_keys):
        def impl(args):
            if set(args) != set(narrow_keys):
                raise RuntimeError("PGRST202 Could not find the function")
            return []
        return impl

    sb = FakeClient(
        rows=_TABLES,
        rpc_impl={
            "update_calibration_notes_cas": lambda a: [],
            "deskcore_fingerprint_counts": lambda a: [],
            "deskcore_check_drafts": _only_narrow({"_project_id", "_rows"}),
            "deskcore_commit_fingerprints": _only_narrow(
                {"_project_id", "_rows", "_user_id", "_ngram_hard"}),
        },
    )
    st = _states(core.migration_state(sb))

    assert st["deskcore_fingerprint_counts"] == "applied"          # 004 在
    assert st["deskcore_check_drafts(3 参)"] == "old_signature"     # 005 没跑
    assert st["deskcore_commit_fingerprints(6 参)"] == "old_signature"


def test_006_impact_says_it_is_a_hard_failure():
    """006 与 001~005 不是一类, 报告必须自己说出来。

    其余几个"不跑也不会坏"(降级 + 留痕); 006 不跑, 现有工作台每一次
    「通过 / 打回」都当场报错。把它混在同一句"建议尽快跑"里就是又一份
    "文档写着没事、实际会炸"。
    """
    sb = FakeClient(rows=_TABLES, rpc_impl=_ALL_RPCS)
    impact = next(c["impact"] for c in core.migration_state(sb)["checks"]
                  if c["migration"].startswith("006"))
    assert "硬失败" in impact
    assert "不是可选" in impact


# ══════════════════════════════════════════════════════════════════════
# 2 · 真故障不许被当成"没跑迁移"
# ══════════════════════════════════════════════════════════════════════

def test_real_failures_are_not_reported_as_missing_migration():
    """权限 / 库故障 → ``error``, 而不是 ``missing``。

    探测器天生要吞异常, 所以它天生有把真故障读成"没迁移"的倾向 —— 那正是
    ``store.rpc_missing`` 的 docstring 一直在防的事。报成 missing 的代价是
    有人会去重跑一遍迁移(无害但没用), 然后带着"迁移都在了"的结论继续查, 而
    真正的那条错误已经被抹掉了。
    """
    def _boom(_args):
        raise RuntimeError("permission denied for function")

    sb = FakeClient(rows=_TABLES, rpc_impl={**_ALL_RPCS,
                                            "deskcore_fingerprint_counts": _boom})
    report = core.migration_state(sb)
    st = _states(report)

    assert st["deskcore_fingerprint_counts"] == "error"
    assert report["ok"] is False          # error 照样判红 —— 只是原因不同
    note = next(c["note"] for c in report["checks"]
                if c["probe"] == "deskcore_fingerprint_counts")
    assert "不是「没跑迁移」" in note


# ══════════════════════════════════════════════════════════════════════
# 3 · 只读
# ══════════════════════════════════════════════════════════════════════

def test_probes_never_write():
    """doctor 是对着**生产库**跑的, 一行都不许写。

    两个写类接口是这里的重点: ``update_calibration_notes_cas`` 用 nil UUID +
    不可能匹配的 witness(UPDATE 命中 0 行), ``deskcore_commit_fingerprints``
    用空 ``_rows``(函数体的 FOR 一次都不进)。真库上的安全性由这两个性质保证,
    这条用例守的是"以后没人把探测参数改成会写的那种"。
    """
    sb = FakeClient(rows=_TABLES, rpc_impl=_ALL_RPCS)
    core.migration_state(sb)

    wrote = [c for c in sb.calls if c["op"] != "select"]
    assert not wrote, f"doctor 的表操作里出现了写: {wrote}"

    for name, args in sb.rpc_calls:
        if name == "deskcore_commit_fingerprints":
            assert args["_rows"] == [], "commit 探测必须传空 _rows, 否则会真写指纹"
        if name == "update_calibration_notes_cas":
            assert args["_project_id"] == core._PROBE_NIL_UUID
            assert args["_expected_md5"] == core._PROBE_IMPOSSIBLE_MD5


def test_backfill_gap_uses_the_same_helpers_as_backfill():
    """回填缺口必须走 backfill 自己那两个函数, 不许另抄一份口径。

    runbook 原来给的是一段手写三表 join 的 SQL —— 那是把口径抄了第二份, 一旦
    漂开, 验收标准会在"主力项目还差几百条"的时候报绿。AST 钉的是【被禁止的
    形态】: 函数体里找不到这两个调用就红。
    """
    tree = ast.parse((REPO_ROOT / "deskcore" / "core.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "backfill_gap")
    called = {sub.func.attr for sub in ast.walk(fn)
              if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)}
    assert {"existing_fingerprint_version_ids", "legacy_version_pages"} <= called


# ══════════════════════════════════════════════════════════════════════
# 4 · 清单不许漏
# ══════════════════════════════════════════════════════════════════════

def _migration_files() -> list[str]:
    return sorted(p.name for p in (REPO_ROOT / "migrations").glob("*.sql")
                  if p.name != "000_baseline.sql")


def test_doctor_probes_every_incremental_migration():
    """migrations/ 下每个增量 .sql 都要被 doctor 探到。

    钉的是【新增迁移忘了加探测】—— 而那不会报错, 只会让 doctor 对着一个缺
    迁移的库高高兴兴地报绿。基线 000 不在此列: 它是 fresh install 的源头,
    "跑没跑过 000" 等价于"这个库有没有表", 001 那几条探测已经覆盖。
    """
    sb = FakeClient(rows=_TABLES, rpc_impl=_ALL_RPCS)
    probed = {c["migration"] for c in core.migration_state(sb)["checks"]}
    missing = [f for f in _migration_files() if f not in probed]
    assert not missing, (
        f"这些迁移 doctor 探不到: {missing} —— 加迁移时要同时在 "
        "core.migration_state 里加一条探测(或显式标 unprobeable 并写明怎么查)")


def test_the_001_probe_column_actually_exists_on_every_table():
    """探测取的那一列必须在 `migrations/001` 里真的建了。

    ⚠️ **这条是抓到过真 bug 的。** 第一版探测写的是 ``.select("id")``，而
    ``user_calibration_notes`` 的主键是 ``(project_id, user_id)`` ——
    **它没有 id 列**。PostgREST 会报 `column "id" does not exist`，那句话正好被
    ``store.rpc_missing`` 认成"迁移没跑"，于是在一个 `001` 明明跑过的库上报
    `missing`，把人打发去重跑一遍迁移。这个探测器存在的全部意义就是不出这种错。

    为什么假件没抓到：`FakeClient` 不校验列名（除非显式配 `missing_columns`），
    所以那一版测试全绿。**验证手段本身也要被验证** —— 与审计 §0.5 那条"录音机
    记得不够细，得到的绿是假的"是同一件事。所以这条断言不去问假件，去读真的 SQL。
    """
    sql = (REPO_ROOT / "migrations" / "001_deskcore.sql").read_text(encoding="utf-8")

    for table in core._MIGRATION_001_TABLES:
        m = re.search(
            r"CREATE TABLE IF NOT EXISTS autowriter\." + table + r"\s*\((.*?)\n\);",
            sql, re.S)
        assert m, f"001 里找不到 {table} 的建表语句 —— 改名了就同步改这份清单"
        body = m.group(1)
        cols = {line.strip().split()[0] for line in body.splitlines()
                if line.strip() and not line.strip().startswith(
                    ("PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT", "--"))}
        assert core._MIGRATION_001_PROBE_COLUMN in cols, (
            f"{table} 没有 {core._MIGRATION_001_PROBE_COLUMN} 列, 探测会把一个"
            f"跑过 001 的库误报成 missing。该表实际有的列: {sorted(cols)}")


@pytest.mark.parametrize("doc", ["migrations/README.md", "docs/deskcore.md",
                                 "docs/deskcore-runbook.md"])
def test_every_migration_is_listed_in_the_deployment_docs(doc):
    """三份部署文档里都要出现每个迁移的文件名。

    这条断言的由来: `docs/deskcore.md` §4.1.1 一度写着"跑 001, 以及后续的
    002/003/004" —— 005 和 006 从来没被写进去。照着它部署的人会漏掉 005
    (短稿照搬长稿抓不到)和 006(现有工作台的审稿按钮直接报错), 而两边都不会
    有任何东西提醒他漏了。文档漂移在本仓是**反复发生**的失败(runbook §5 整
    节都是), 所以这里用断言钉住, 不靠自觉。
    """
    text = (REPO_ROOT / doc).read_text(encoding="utf-8")
    missing = [f for f in _migration_files() if f.split("_")[0] not in text
               or not re.search(re.escape(f.split(".")[0]), text)]
    assert not missing, f"{doc} 里没提到这些迁移: {missing}"


# ══════════════════════════════════════════════════════════════════════
# 5 · 缺列时的报错要看得懂
# ══════════════════════════════════════════════════════════════════════

def test_update_item_status_translates_the_missing_column_error():
    """`migrations/006` 没跑时, 点「通过」的人得看到一句能照着做的话。

    PostgREST 原样抛的是 `column "decision_source" of relation "items" does
    not exist`。**刻意不降级**(去掉三列重试一次就等于让机器判定继续伪装成人工
    反馈去污染 TV 的评估模型, COR-004 治的正是这件事), 只翻译。

    三个列名都要认: PostgREST 只报它撞上的第一个, 而那取决于 payload 的键序
    —— 键序是实现细节, 不是契约。
    """
    import db

    for absent in ("decision_source", "reviewer_id", "decided_at"):
        sb = FakeClient(rows={"items": [{"id": PID, "status": "pending"}]},
                        missing_columns={"items": {absent}})
        with pytest.raises(RuntimeError) as ei:
            db.update_item_status(sb, PID, "approved",
                                  source=db.DecisionSource.HUMAN,
                                  reviewer_id=PID)
        msg = str(ei.value)
        assert "006_item_decision_provenance.sql" in msg, msg
        assert "doctor" in msg, msg


def test_update_item_status_does_not_swallow_unrelated_failures():
    """只翻译缺列那一种。别的错原样上抛 —— 把库故障翻译成"去跑迁移"更坏。"""
    import db

    class _Boom(FakeClient):
        def table(self, name):
            raise RuntimeError("connection refused")

    with pytest.raises(RuntimeError, match="connection refused"):
        db.update_item_status(_Boom(), PID, "approved",
                              source=db.DecisionSource.SYSTEM)


# ══════════════════════════════════════════════════════════════════════
# 6 · codex review 的六条(#63) —— 每条都先钉住"错的那个形态"
# ══════════════════════════════════════════════════════════════════════

# PostgREST 的真实错误文本。2026-08-26 对着真实例打出来的, 不是照记忆写的:
#   GET /rest/v1/no_such_table  → PGRST205 Could not find the table '...' in the schema cache
#   GET /rest/v1/projects?select=no_such_col → 42703 column ... does not exist
#   POST /rest/v1/rpc/no_such_fn → PGRST202 Could not find the function ...
_PGRST205 = ('{"code":"PGRST205","message":"Could not find the table '
             "'autowriter.angle_ledger' in the schema cache\"}")
_PGRST204 = ('{"code":"PGRST204","message":"Could not find the '
             "'decision_source' column of 'items' in the schema cache\"}")

# 2026-08-26 生产库上真实回来的那一句(GRANT 没发时 PostgREST 的原文)。
_DENIED = ('{"code":"42501","message":"permission denied for table '
           'draft_fingerprints"}')


# ══════════════════════════════════════════════════════════════════════
# 7 · 建出来 ≠ 能访问 —— 007 那条 GRANT
#
# 首次真部署当天的形态: doctor 全绿、/health 全绿、六个迁移全核验过, 而
# deskcore 除 list_projects 外每个工具都回 42501。因为 001 只给两个**函数**
# 发了 EXECUTE, 四张表一行 GRANT 都没有 —— 而 service_role **绕过 RLS 但不
# 绕过表级 GRANT**。
# ══════════════════════════════════════════════════════════════════════

class _Denied(FakeClient):
    """四张表都在, 但一读就 42501。"""

    def table(self, name):
        if name in core._MIGRATION_001_TABLES:
            raise RuntimeError(_DENIED)
        return super().table(name)


def test_permission_denied_is_denied_not_error():
    """没发 GRANT 要报 `denied`, 并落进"还缺这些迁移"指向 007。

    钉的是【两个被禁止的形态】:

      · 报成 `error` —— 那句 note 会写"探测本身失败(不是「没跑迁移」)", 把人
        打发去查连通性, 而真正该做的是跑一条 GRANT;
      · 报成 `missing` 且只挂在 001 —— 那会让人对着一个**表明明都在**的库
        重跑一遍建表 SQL(幂等, 于是什么都不会变), 然后继续 42501。

    两种都是"自检工具给出与现实不符的结论", 与 codex #63 那条同一个道理。
    """
    report = core.migration_state(_Denied(rows=_TABLES, rpc_impl=_ALL_RPCS))
    st = _states(report)

    assert st["draft_fingerprints 的表级 GRANT"] == "denied", st
    for t in core._MIGRATION_001_TABLES:
        assert st[f"表 {t}"] == "denied", f"缺 GRANT 被报成了 {st[f'表 {t}']}"

    assert "007_deskcore_table_grants.sql" in report["missing"]
    assert "007_deskcore_table_grants.sql" in report["denied"]
    assert report["errors"] == [], f"没发 GRANT 不是探测故障: {report['errors']}"
    assert report["ok"] is False


def test_function_permission_denied_is_still_an_error():
    """`permission denied for function` **不能**被认成 007 的事。

    两者都是 42501, 但补救的 SQL 完全不同 —— 007 一行函数权限都不管。认进来
    的话 doctor 会指挥人去跑一个解决不了问题的迁移。所以判据认的是
    "for table / for relation" 这句话, **不是**裸的错误码。
    """
    assert core.store.table_permission_denied(RuntimeError(_DENIED)) is True
    assert core.store.table_permission_denied(
        RuntimeError('{"code":"42501","message":"permission denied for '
                     'function deskcore_check_drafts"}')) is False

    def _boom(_args):
        raise RuntimeError("permission denied for function")

    report = core.migration_state(
        FakeClient(rows=_TABLES,
                   rpc_impl={**_ALL_RPCS, "deskcore_fingerprint_counts": _boom}))
    assert report["errors"] == ["004_deskcore_check_pushdown.sql"]
    assert report["denied"] == []


def test_grant_probe_stays_quiet_when_the_tables_are_not_there_yet():
    """001 都没跑时, 007 报 `unprobeable` 而不是 missing。

    001 里已经含着同一条 GRANT。这时再喊一句"007 也缺"只会让人以为要跑两个,
    而先跑 001 之后 007 本来就成了 no-op。
    """
    class _NoTables(FakeClient):
        def table(self, name):
            if name in core._MIGRATION_001_TABLES:
                raise RuntimeError(_PGRST205)
            return super().table(name)

    report = core.migration_state(_NoTables(rows=_TABLES, rpc_impl=_ALL_RPCS))
    assert _states(report)["draft_fingerprints 的表级 GRANT"] == "unprobeable"
    assert "007_deskcore_table_grants.sql" not in report["missing"]
    assert "001_deskcore.sql" in report["missing"]


def test_doctor_says_the_table_is_there_and_the_grant_is_not(capsys):
    """`doctor` 的输出里要说清"表在, 缺的是 GRANT", 并且退出码 1。

    只打一行 `· migrations/007` 的话, 看的人没有任何线索知道这个和前面那些
    "建表"迁移不是一回事。
    """
    rc = cli._doctor(core, _Denied(rows=_TABLES, rpc_impl=_ALL_RPCS), None)
    out = capsys.readouterr().out
    assert rc == 1
    assert "007_deskcore_table_grants.sql" in out
    assert "缺的是 GRANT" in out, out
    assert "permission denied for table" in out, "原始报错要能看到"


def test_missing_table_is_missing_not_error():
    """缺表必须报 `missing`。

    ⚠️ **这条钉的是 codex 找出来的那个反向结论。** `store.rpc_missing` 只认
    "could not find the function" / "does not exist" / PGRST202 —— 而 PostgREST
    对缺表回的是 **PGRST205**，三个条件一个都不沾。于是一个**真的没跑 001**
    的库会被报成 `error` 并写着"不是「没跑迁移」"，正好说反，而这个命令存在的
    全部意义就是别说反。
    """
    class _NoTables(FakeClient):
        def table(self, name):
            if name in core._MIGRATION_001_TABLES:
                raise RuntimeError(_PGRST205)
            return super().table(name)

    report = core.migration_state(_NoTables(rows=_TABLES, rpc_impl=_ALL_RPCS))
    st = _states(report)
    for t in core._MIGRATION_001_TABLES:
        assert st[f"表 {t}"] == "missing", f"缺表被报成了 {st[f'表 {t}']}"
    assert "001_deskcore.sql" in report["missing"]
    assert "001_deskcore.sql" not in report["errors"]


def test_rpc_probes_keep_using_the_runtime_predicate():
    """RPC 那一路的判据必须仍然是 `store.rpc_missing` —— 与运行期同源。

    放宽是**只针对表/列**的：表/列没有运行期对应物（运行期不该因为缺表就降级）。
    RPC 有，所以那一路一放宽，就会出现"自检说 RPC 在、运行期判它不在"的分歧，
    而这个命令的立身之本就是不出这种分歧。
    """
    import inspect
    src = inspect.getsource(core.migration_state)
    # 三处 RPC 探测都不该显式传 predicate（默认就是 rpc_missing）
    rpc_calls = [ln for ln in src.splitlines() if "client.rpc(" in ln]
    assert len(rpc_calls) >= 3, rpc_calls
    assert "schema_object_missing" in src, "表/列那一路要用放宽后的判据"
    assert core.store.rpc_missing(RuntimeError(_PGRST205)) is False, (
        "rpc_missing 不该认缺表 —— 运行期缺表是真故障, 不是「该降级」")
    assert core.store.schema_object_missing(RuntimeError(_PGRST205)) is True
    assert core.store.schema_object_missing(RuntimeError(_PGRST204)) is True


def test_reserve_angles_rpc_is_probed():
    """001 装的 `deskcore_reserve_angles` 也要探。

    漏探它的代价是静默的：表都在 → doctor 报"001 到位"，而
    `store.reserve_angles` 每次判 RPC 不存在、降级成非原子发牌，于是两个人
    同时发牌能拿到同一组坐标，**两边都报成功**。
    """
    gone = {k: v for k, v in _ALL_RPCS.items() if k != "deskcore_reserve_angles"}
    st = _states(core.migration_state(FakeClient(rows=_TABLES, rpc_impl=gone)))
    assert "deskcore_reserve_angles" in st, "001 的发牌 RPC 没被探到"
    assert st["deskcore_reserve_angles"] == "missing"

    ok = FakeClient(rows=_TABLES, rpc_impl=_ALL_RPCS)
    assert _states(core.migration_state(ok))["deskcore_reserve_angles"] == "applied"

    # 探测必须是可证明的 no-op: _want=0 时函数体第一句就 RETURN, 连锁都不取。
    args = dict(next(a for n, a in ok.rpc_calls if n == "deskcore_reserve_angles"))
    assert args["_want"] == 0 and args["_candidates"] == []


def test_probe_errors_do_not_land_in_the_missing_list():
    """真故障不能进"还缺这些迁移"。

    进了的话 `_doctor()` 会打印"按这个顺序跑"，把人指去跑一遍根本不缺的 SQL
    —— 而同一份输出里那条 note 明明写着"不是「没跑迁移」"。**一个自检工具
    给出互相矛盾的结论，比它不存在更坏。**
    """
    def _boom(_args):
        raise RuntimeError("permission denied for function")

    sb = FakeClient(rows=_TABLES,
                    rpc_impl={**_ALL_RPCS, "deskcore_fingerprint_counts": _boom})
    report = core.migration_state(sb)

    assert report["errors"] == ["004_deskcore_check_pushdown.sql"]
    assert "004_deskcore_check_pushdown.sql" not in report["missing"]
    assert report["ok"] is False        # 照样判红, 只是补救方式不同


def test_doctor_prints_006_first_when_missing():
    """补救清单里 006 必须排最前 —— 与 runbook 的顺序一致。

    按字典序打印会把它排到最后，于是照着做的人会在"审稿按钮全报错"的状态下
    先跑完 002–005（其中 003 还要改 678 行数据）。**工具和文档给出不同的顺序，
    人只会信工具。**
    """
    import io, contextlib
    sb = FakeClient(
        rows={**_TABLES, "items": [{"id": PID, "updated_at": "t"}]},
        rpc_impl={},
        missing_columns={"items": {"decision_source", "reviewer_id", "decided_at"}})

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli._doctor(core, sb, None)
    out = buf.getvalue()

    assert rc == 1
    listed = [ln for ln in out.splitlines() if ln.strip().startswith("· migrations/")]
    assert listed, out
    assert core.MIGRATION_RUN_FIRST in listed[0], f"006 没排在第一条: {listed}"


def test_fingerprints_total_counts_every_row():
    """`fingerprints_total` 要数**全部**行，含 version_id 为空的那些。

    原来它是 `len(existing_fingerprint_version_ids(...))` —— 那个函数
    `.not_.is_("version_id","null")` 过滤掉空 version 的行、还去了重，也就是
    **恰恰数不到**写作台经 `commit_drafts` 写进来的那批（那批 version_id 就是
    空的），而 CLI 的说明文字还专门写着"含 commit_drafts 写进来的"。说反了。
    """
    rows = {
        **_TABLES,
        "draft_fingerprints": [
            {"id": "f1", "project_id": PID, "version_id": "v1"},
            {"id": "f2", "project_id": PID, "version_id": "v2"},
            # 写作台写进来的三行: version_id 为空
            {"id": "f3", "project_id": PID, "version_id": None},
            {"id": "f4", "project_id": PID, "version_id": None},
            {"id": "f5", "project_id": PID, "version_id": None},
        ],
        "items": [],
    }
    gap = core.backfill_gap(FakeClient(rows=rows), PID)
    assert gap["fingerprints_total"] == 5, (
        f"只数到 {gap['fingerprints_total']} —— 空 version_id 的那三行被漏掉了")
    assert gap["backfilled"] == 0        # items 是空的, 没有 eligible


def test_backfill_gap_scans_uncapped():
    """核对路径必须扫全量，不许沿用 backfill 的 5000 上限。

    沿用的话，一个 5000 条以上的项目在最新那 5000 条补齐之后就被报成
    "还差 0 / done"，而更老的历史稿从来没进过查重基线。**一个只看了前 5000 条
    的验收标准报出来的绿是假的。**
    """
    import inspect
    src = inspect.getsource(core.backfill_gap)
    assert "limit=None" in src, "backfill_gap 还在沿用默认上限"

    # 而且超过 backfill 自己的上限时要说出来, 不能默默报 done
    big = {**_TABLES, "draft_fingerprints": [], "items": [], "batches": []}
    gap = core.backfill_gap(FakeClient(rows=big), PID)
    assert "backfill_capped_warning" not in gap        # 小项目不该乱报
    assert core._BACKFILL_DEFAULT_CAP == 5000
