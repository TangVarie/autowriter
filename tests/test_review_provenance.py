"""审计 A-01 · 经代理转述的人审要留下证据, 且永远不冒充「人亲手点的」。

生产库里 ``decision_source='human'`` 的 32 行**全部**是 deskcore 的 ``review_drafts``
写的, 而且全落在模型自己的工具链里(``commit_drafts`` 之后 10~27 秒就"审完"),
deskcore 又不记成功的工具调用 —— 于是库里分不出「模型自己点了通过」和「用户说了
全过、模型照实记」。协议和 docstring 都写着「用户没表态不许调」, 但服务端没有一道
闸, 也判不了用户到底说没说过。

服务端能做的是把证据留下来。这里钉四件事:

  1. 工具这条路**只写** ``human_via_agent``; 用户的原话落 ``decision_note``;
     ``decided_within_s`` 由服务端按这批稿子的 ``created_at`` 算;
  2. ``user_words`` 缺 / 空 / 超长 → 报错, **一行不碰、一次库都不查**;
  3. ``deskcore/`` 下再没有任何代码把 ``'human'`` 当 decision_source 写(AST 守着,
     守卫本身也要被验证);
  4. ``migrations/012`` 真的带了那两列和扩过的 CHECK, 而且 CHECK 的取值集合与
     ``db._DECISION_SOURCES`` 是同一份(文本断言, 不起库)。
"""

from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import db
from deskcore import core, tools
from tests.fakes import FakeClient

REPO = Path(__file__).resolve().parent.parent

ME = "11111111-1111-1111-1111-111111111111"
PROJ = "aaaaaaaa-0000-0000-0000-000000000001"
BATCH = "bbbbbbbb-0000-0000-0000-000000000002"
ITEM_A, VER_A = "cccc0000-0000-0000-0000-00000000000a", "dddd0000-0000-0000-0000-00000000000a"
ITEM_B, VER_B = "cccc0000-0000-0000-0000-00000000000b", "dddd0000-0000-0000-0000-00000000000b"


def _client(batch_created_at: str | None = None) -> FakeClient:
    """两条待审稿。``batch_created_at`` 嵌在 versions → items → batches 那层
    (与 ``store.items_for_versions`` 的查询形状一致), None 表示 batch 行上没有。"""
    batches = {"project_id": PROJ}
    if batch_created_at is not None:
        batches["created_at"] = batch_created_at
    items = [
        {"id": ITEM_A, "batch_id": BATCH, "user_id": ME, "status": "pending",
         "decision_source": None, "reviewer_id": None, "decided_at": None,
         "decision_note": None, "decided_within_s": None},
        {"id": ITEM_B, "batch_id": BATCH, "user_id": ME, "status": "pending",
         "decision_source": None, "reviewer_id": None, "decided_at": None,
         "decision_note": None, "decided_within_s": None},
    ]
    versions = [{"id": vid, "items": {
        "id": it["id"], "status": it["status"], "decision_source": None,
        "batch_id": BATCH, "batches": dict(batches)}}
        for it, vid in zip(items, (VER_A, VER_B))]
    return FakeClient(rows={
        "projects": [{"id": PROJ, "name": "项目", "brand": "A", "owner_id": ME,
                      "calibration_notes": "", "tactics": "[]", "custom_roles": []}],
        "items": items,
        "versions": versions,
    })


def _row(c: FakeClient, item_id: str) -> dict:
    return next(r for r in c.rows["items"] if r["id"] == item_id)


def _decisions() -> list[dict]:
    return [{"version_id": VER_A, "decision": "approved"},
            {"version_id": VER_B, "decision": "needs_revision"}]


# ══════════════════════════════════════════════════════════════════════
# 1 · 工具这条路写的是 human_via_agent + 原话 + 距入库秒数
# ══════════════════════════════════════════════════════════════════════

def test_tool_records_human_via_agent_with_the_users_words(monkeypatch):
    """走 ``tools.review_drafts``(MCP 暴露的那个), 不是直接调 core —— 要验的正是
    工具入口把 user_words 传下去了。"""
    created = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    c = _client(batch_created_at=created)
    monkeypatch.setattr(core, "sb", lambda: c)

    out = tools.review_drafts(PROJ, _decisions(), user_words="  这批可以发, 第二条重写  ",
                              _user_id=ME)

    assert out["reviewed"] == 2
    assert [r["outcome"] for r in out["results"]] == ["recorded", "recorded"]
    for item_id, status in ((ITEM_A, "approved"), (ITEM_B, "needs_revision")):
        row = _row(c, item_id)
        assert row["status"] == status
        assert row["decision_source"] == "human_via_agent"
        assert row["decision_source"] == db.DecisionSource.HUMAN_VIA_AGENT
        assert row["reviewer_id"] == ME
        assert row["decision_note"] == "这批可以发, 第二条重写", "原话 strip 后原样落库"
        assert 90 <= row["decided_within_s"] <= 95, \
            f"距批次创建的秒数要由服务端算出来: {row['decided_within_s']!r}"
        assert row["decided_at"]


def test_decided_within_s_is_null_when_the_batch_has_no_created_at():
    """batch 行上没有时间就存 NULL —— 不硬算、不失败, 决策本身照记。"""
    c = _client(batch_created_at=None)
    out = core.review_drafts(c, PROJ, _decisions()[:1], user_words="全过", user_id=ME)

    assert out["reviewed"] == 1
    row = _row(c, ITEM_A)
    assert row["status"] == "approved"
    assert row["decision_source"] == db.DecisionSource.HUMAN_VIA_AGENT
    assert row["decision_note"] == "全过"
    assert row.get("decided_within_s") is None


def test_unparseable_batch_time_is_also_null():
    c = _client(batch_created_at="不是时间")
    core.review_drafts(c, PROJ, _decisions()[:1], user_words="全过", user_id=ME)
    assert _row(c, ITEM_A).get("decided_within_s") is None


def test_commit_drafts_tells_the_model_not_to_review_on_its_own(monkeypatch):
    """入库之后模型最常犯的下一步就是紧接着自己调 review_drafts —— 纪律要出现在
    返回值里, 在最容易犯错的那一刻眼前。"""
    from deskcore import store as _store
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    c = FakeClient(rows={"projects": [
        {"id": PROJ, "name": "项目", "brand": "A", "owner_id": ME,
         "calibration_notes": "", "tactics": "[]", "custom_roles": []}]})
    c.rpc_impl["deskcore_commit_fingerprints"] = lambda args: [
        {"idx": i, "status": core.COMMIT_STATUS_INSERTED, "collided_with": None, "detail": None}
        for i in range(len(args.get("_rows") or []))]
    out = core.commit_drafts(c, PROJ, [{"title": "一条新稿子的标题", "body": "正文" * 40}],
                             user_id=ME)
    assert out["written"] == 1
    assert "待审" in out["next_step"] and "review_drafts" in out["next_step"] \
        and "user_words" in out["next_step"], out["next_step"]
    assert _store.DESKCORE_ITEM_STATUS == "pending"


# ══════════════════════════════════════════════════════════════════════
# 2 · user_words 不合法: 一行不碰, 一次库都不查
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("bad", [None, "", "   \n ", "x" * 201, "过" * 201, 123, ["全过"]])
def test_bad_user_words_write_nothing_and_touch_nothing(bad):
    c = _client(batch_created_at=datetime.now(timezone.utc).isoformat())
    out = core.review_drafts(c, PROJ, _decisions(), user_words=bad, user_id=ME)

    assert out["reviewed"] == 0 and out["results"] == []
    assert "user_words" in out["error"] and out.get("hint")
    assert c.calls == [], "校验不过就不该碰库 —— 连归属都不用查"
    for item_id in (ITEM_A, ITEM_B):
        row = _row(c, item_id)
        assert row["status"] == "pending"
        assert row["decision_source"] is None and row["decision_note"] is None


def test_exactly_200_chars_is_still_fine():
    c = _client()
    out = core.review_drafts(c, PROJ, _decisions()[:1], user_words="过" * 200, user_id=ME)
    assert out["reviewed"] == 1
    assert _row(c, ITEM_A)["decision_note"] == "过" * 200


def test_the_tool_signature_makes_user_words_mandatory(monkeypatch):
    """MCP 层按签名生成 schema; 没有默认值的参数就是必填 —— 模型省不掉它。"""
    import inspect
    p = inspect.signature(tools.review_drafts).parameters["user_words"]
    assert p.default is inspect.Parameter.empty
    assert p.annotation in (str, "str")
    c = _client()
    monkeypatch.setattr(core, "sb", lambda: c)
    with pytest.raises(TypeError):
        tools.review_drafts(PROJ, _decisions(), _user_id=ME)      # noqa: 故意漏参
    assert c.calls == []


# ══════════════════════════════════════════════════════════════════════
# 3 · db 层: 两列只在传了时才写; human_via_agent 是「有人」的来源
# ══════════════════════════════════════════════════════════════════════

def test_update_item_status_adds_the_new_columns_only_when_given():
    """老调用点(Streamlit 的按钮)一个字没改, payload 也不许多出两列 —— 否则没跑
    012 的库上「通过 / 打回」会当场报错。"""
    c = FakeClient(rows={"items": [{"id": "i1", "status": "pending"}]})
    db.update_item_status(c, "i1", "approved", source=db.DecisionSource.HUMAN,
                          reviewer_id=ME)
    payload = c.calls[-1]["payload"]
    assert payload["decision_source"] == "human"
    assert "decision_note" not in payload and "decided_within_s" not in payload

    db.update_item_status(c, "i1", "approved", source=db.DecisionSource.HUMAN_VIA_AGENT,
                          reviewer_id=ME, decision_note="全过", decided_within_s=12)
    payload = c.calls[-1]["payload"]
    assert payload["decision_source"] == "human_via_agent"
    assert payload["reviewer_id"] == ME
    assert payload["decision_note"] == "全过" and payload["decided_within_s"] == 12


@pytest.mark.parametrize("source", [db.DecisionSource.AUTO_HARD_RULE,
                                    db.DecisionSource.AUTO_DEDUP,
                                    db.DecisionSource.SYSTEM])
def test_machine_sources_cannot_carry_a_note_or_a_reviewer(source):
    c = FakeClient(rows={"items": [{"id": "i1", "status": "pending"}]})
    with pytest.raises(ValueError, match="decision_note"):
        db.update_item_status(c, "i1", "needs_revision", source=source, decision_note="x")
    with pytest.raises(ValueError, match="reviewer_id"):
        db.update_item_status(c, "i1", "needs_revision", source=source, reviewer_id=ME)
    assert c.calls == []


def test_db_refuses_a_note_longer_than_the_check_allows():
    c = FakeClient(rows={"items": [{"id": "i1", "status": "pending"}]})
    with pytest.raises(ValueError, match="200"):
        db.update_item_status(c, "i1", "approved", source=db.DecisionSource.HUMAN_VIA_AGENT,
                              reviewer_id=ME, decision_note="x" * 201)
    assert c.calls == []


def test_missing_012_columns_are_translated_into_which_migration_to_run():
    c = FakeClient(rows={"items": [{"id": "i1", "status": "pending"}]},
                   missing_columns={"items": {"decision_note", "decided_within_s"}})
    with pytest.raises(RuntimeError, match="012_review_via_agent_provenance"):
        db.update_item_status(c, "i1", "approved", source=db.DecisionSource.HUMAN_VIA_AGENT,
                              reviewer_id=ME, decision_note="全过", decided_within_s=3)


# ══════════════════════════════════════════════════════════════════════
# 4 · deskcore/ 下再没有代码写 'human'
# ══════════════════════════════════════════════════════════════════════

def _is_human_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and node.value == "human":
        return True
    return isinstance(node, ast.Attribute) and node.attr == "HUMAN"


def _human_writes(tree: ast.AST, label: str) -> list[str]:
    """两种写法都抓: ``update_item_status(..., source=HUMAN)`` 和
    ``{"decision_source": "human"}`` 这样的直写 payload。"""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name == "update_item_status":
                for kw in node.keywords:
                    if kw.arg == "source" and _is_human_literal(kw.value):
                        found.append(f"{label}:{node.lineno} update_item_status(source=human)")
        elif isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "decision_source"
                        and _is_human_literal(v)):
                    found.append(f"{label}:{node.lineno} {{'decision_source': 'human'}}")
    return found


def test_no_code_in_deskcore_writes_human_as_decision_source():
    """'human' 从此只属于 Streamlit 里真的点了按钮的那条路(app.py)。deskcore 里
    任何工具都是模型在调, 没有一条路上有人点过按钮。"""
    offenders: list[str] = []
    for path in sorted((REPO / "deskcore").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders += _human_writes(tree, str(path.relative_to(REPO)))
    assert not offenders, (
        f"deskcore 里还有代码把 'human' 当 decision_source 写: {offenders} —— "
        "工具这条路要写 db.DecisionSource.HUMAN_VIA_AGENT(审计 A-01)")


def test_the_human_scanner_would_actually_catch_both_shapes():
    """守卫本身也要被验证 —— 上面那条永远绿的话, 它什么都没在守。"""
    planted = (
        "import db\n"
        "db.update_item_status(c, i, 'approved', source=db.DecisionSource.HUMAN)\n"
        "db.update_item_status(c, i, 'approved', source='human')\n"
        "row = {'status': 'approved', 'decision_source': 'human'}\n"
        "ok = db.update_item_status(c, i, 'approved', source=db.DecisionSource.HUMAN_VIA_AGENT)\n"
        "ok2 = {'decision_source': db.DecisionSource.HUMAN_VIA_AGENT}\n"
    )
    hits = _human_writes(ast.parse(planted), "planted")
    assert len(hits) == 3, hits
    assert all(":2 " in h or ":3 " in h or ":4 " in h for h in hits), hits


# ══════════════════════════════════════════════════════════════════════
# 5 · 迁移文件本身(文本断言, 不起库)
# ══════════════════════════════════════════════════════════════════════

MIGRATION = REPO / "migrations" / "012_review_via_agent_provenance.sql"
BASELINE = REPO / "migrations" / "000_baseline.sql"


def _check_values(sql: str) -> set[str]:
    m = re.search(r"CHECK \(decision_source IN\s*\(([^)]*)\)", sql)
    assert m, "找不到 decision_source 的 CHECK"
    return set(re.findall(r"'([a-z_]+)'", m.group(1)))


def test_migration_012_carries_both_columns_and_the_extended_check():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS decision_note TEXT" in sql
    assert "char_length(decision_note) <= 200" in sql
    assert "ADD COLUMN IF NOT EXISTS decided_within_s INTEGER" in sql
    assert "items_decision_source_check" in sql and "'human_via_agent'" in sql
    assert "DROP CONSTRAINT" in sql, "老的内联 CHECK 要先摘掉, 否则新值照样被拒"
    assert "CREATE INDEX IF NOT EXISTS" in sql
    # CHECK 的取值集合与 db.DecisionSource 是同一份 —— 两边各写一份迟早漂
    assert _check_values(sql) == set(db._DECISION_SOURCES)
    # 200 这个数也只有一份
    assert f"<= {db.DECISION_NOTE_MAX_CHARS})" in sql


def test_baseline_got_the_same_change():
    """migrations/README 的规矩: 加列必须两边都改, 否则新环境缺列。"""
    sql = BASELINE.read_text(encoding="utf-8")
    assert "decision_note TEXT" in sql and "decided_within_s INTEGER" in sql
    assert "items_human_via_agent_decision_idx" in sql
    assert _check_values(sql) == set(db._DECISION_SOURCES)
