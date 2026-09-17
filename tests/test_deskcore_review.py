"""写作台的人审入口: 把「有人真的看过并给了结论」这件事记进库。

这一步补的是 2026-09-16 评测 AW-01 指出的缺口: `commit_drafts` 建的 item 是
`pending` 且不盖任何决策戳（那是**对的**，理由见 `store.DESKCORE_ITEM_STATUS`），
但在此之前写作台这条路根本没有下一步 —— 只用它写稿、定稿、导出的团队永远产不出
一条人工审核决定，TV 那边按 `status in ('approved','needs_revision')` 捞行时一条
也捞不到。

所以这里盯的不是"能不能改状态"，是**改出来的那条记录是不是诚实的**：

  1. 决策来源必须是 `human`，审稿人必须是**真的点了这一下的那个人**；
  2. 打回和通过是同等公民 —— 只能记通过的入口产出的仍然是清一色正例，
     与不做无异（COR-004 治的正是这类"伪造的人工反馈"）；
  3. 不认 `pending` —— 迭代后重置回待审是 `SYSTEM`，从人审入口进来就是伪造；
  4. 跨项目的 version_id 一行也碰不到。
"""

from __future__ import annotations

import pytest

import db
from deskcore import core
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
SOMEONE_ELSE = "99999999-9999-9999-9999-999999999999"
PROJ = "aaaaaaaa-0000-0000-0000-000000000001"
OTHER_PROJ = "aaaaaaaa-0000-0000-0000-00000000000f"
BATCH = "bbbbbbbb-0000-0000-0000-000000000002"

ITEM_A, VER_A = "cccc0000-0000-0000-0000-00000000000a", "dddd0000-0000-0000-0000-00000000000a"
ITEM_B, VER_B = "cccc0000-0000-0000-0000-00000000000b", "dddd0000-0000-0000-0000-00000000000b"
# 别的项目的稿子 —— 本项目的审核入口一行也不该碰得到它
ITEM_X, VER_X = "cccc0000-0000-0000-0000-0000000000cc", "dddd0000-0000-0000-0000-0000000000dd"


def _items() -> list[dict]:
    return [
        {"id": ITEM_A, "batch_id": BATCH, "user_id": ME, "status": "pending",
         "decision_source": None, "reviewer_id": None, "decided_at": None,
         "best_version_id": None, "batches": {"project_id": PROJ}},
        {"id": ITEM_B, "batch_id": BATCH, "user_id": ME, "status": "pending",
         "decision_source": None, "reviewer_id": None, "decided_at": None,
         "best_version_id": None, "batches": {"project_id": PROJ}},
        {"id": ITEM_X, "batch_id": "bbbb0000-0000-0000-0000-00000000000f",
         "user_id": ME, "status": "pending",
         "decision_source": None, "reviewer_id": None, "decided_at": None,
         "best_version_id": None, "batches": {"project_id": OTHER_PROJ}},
    ]


def _versions(items: list[dict]) -> list[dict]:
    """versions 行里嵌 items、items 里再嵌 batches —— 与 `items_for_versions`
    的查询形状一致（同 `test_deskcore_export._versions_table` 的理由：从同一份
    源数据派生，免得两边手写着写着就漂了）。"""
    pair = {ITEM_A: VER_A, ITEM_B: VER_B, ITEM_X: VER_X}
    return [{"id": pair[it["id"]], "items": {
        "id": it["id"], "status": it["status"],
        "decision_source": it.get("decision_source"),
        "batch_id": it["batch_id"],
        "batches": {"project_id": it["batches"]["project_id"]},
    }} for it in items]


def _client(items=None, owner=ME) -> FakeClient:
    items = _items() if items is None else items
    return FakeClient(rows={
        "projects": [
            {"id": PROJ, "name": "项目", "brand": "A", "owner_id": owner,
             "calibration_notes": "", "tactics": "[]", "custom_roles": []},
        ],
        "items": items,
        "versions": _versions(items),
    })


def _row(c: FakeClient, item_id: str) -> dict:
    return next(r for r in c.rows["items"] if r["id"] == item_id)


# ══════════════════════════════════════════════════════════════════════
# 1 · 记下来的那条记录必须诚实
# ══════════════════════════════════════════════════════════════════════

def test_approve_records_a_real_human_decision():
    c = _client()
    out = core.review_drafts(c, PROJ, [
        {"version_id": VER_A, "decision": "approved"}], user_id=ME)

    assert out["reviewed"] == 1
    assert out["results"][0]["outcome"] == "recorded"
    assert out["results"][0]["item_id"] == ITEM_A

    row = _row(c, ITEM_A)
    assert row["status"] == "approved"
    assert row["decision_source"] == db.DecisionSource.HUMAN
    assert row["reviewer_id"] == ME, "审稿人必须是真的点了这一下的那个人"
    assert row["decided_at"], "缺 decided_at 的决策在 TV 那边无法排序/归档"


def test_reject_is_a_first_class_citizen():
    """只能点通过的审核入口产出的仍然是清一色正例 —— 与不做无异。"""
    c = _client()
    out = core.review_drafts(c, PROJ, [
        {"version_id": VER_A, "decision": "needs_revision"}], user_id=ME)

    assert out["reviewed"] == 1
    row = _row(c, ITEM_A)
    assert row["status"] == "needs_revision"
    assert row["decision_source"] == db.DecisionSource.HUMAN
    assert row["reviewer_id"] == ME


def test_reviewer_is_always_the_caller():
    """签名里没有 reviewer 参数 —— 传了也只是个被忽略的多余键。

    审计 COR-004 治的正是"把 owner 当 reviewer"。留个口子等于把它请回来。
    """
    c = _client()
    core.review_drafts(c, PROJ, [
        {"version_id": VER_A, "decision": "approved",
         "reviewer_id": SOMEONE_ELSE}], user_id=ME)
    assert _row(c, ITEM_A)["reviewer_id"] == ME


def test_previous_decision_is_reported_back():
    """改判要让调用方看得见，而不是闷头覆盖。"""
    items = _items()
    items[0].update({"status": "approved",
                     "decision_source": db.DecisionSource.HUMAN})
    c = _client(items)
    out = core.review_drafts(c, PROJ, [
        {"version_id": VER_A, "decision": "needs_revision"}], user_id=ME)

    r = out["results"][0]
    assert r["outcome"] == "recorded"
    assert r["previous_status"] == "approved"
    assert r["previously_decided_by"] == db.DecisionSource.HUMAN


@pytest.mark.parametrize("source", [
    db.DecisionSource.AUTO_HARD_RULE,
    db.DecisionSource.AUTO_DEDUP,
    db.DecisionSource.SYSTEM,
])
def test_previously_decided_by_can_be_a_machine(source):
    """⚠️ codex review P2: 这个字段是**上一个决定的来源**, 不是"有没有人审过"。

    硬规则违规、查重重生耗尽、迭代后重置 —— 这三种都会留下非空的
    `decision_source`, 而其中一个人都没看过稿子。把"非空"读成"有人审过"的后果
    不在库里而在嘴上: 模型会照着跟用户讲"这条之前有人审过, 你这次是改判", 凭空
    编出一个不存在的审稿人。
    """
    items = _items()
    items[0].update({"status": "needs_revision", "decision_source": source})
    c = _client(items)
    out = core.review_drafts(c, PROJ, [
        {"version_id": VER_A, "decision": "approved"}], user_id=ME)

    r = out["results"][0]
    assert r["outcome"] == "recorded"
    assert r["previously_decided_by"] == source
    assert r["previously_decided_by"] != db.DecisionSource.HUMAN, "非空 ≠ 人审过"


def test_the_docs_spell_out_every_source_this_field_can_carry():
    """⚠️ 这条钉的是**文档**, 因为这个字段唯一的读者是模型。

    上一条证明了机器来源真的会出现在返回值里；模型要不要跟用户说"之前有人审
    过", 全凭工具 docstring 和 `protocol.md` 那两段话。所以每加一种
    `DecisionSource`, 这两处就必须跟着点名 —— 少点一种, 模型对那一种的解释就
    只能靠猜。
    """
    import re
    from pathlib import Path

    from deskcore import tools

    texts = {
        "tools.review_drafts 的 docstring": tools.review_drafts.__doc__,
        "protocol.md": Path(core.__file__).with_name(
            "protocol.md").read_text(encoding="utf-8"),
    }
    for where, text in texts.items():
        said = [p for p in re.split(r"\n\s*\n", text)
                if "previously_decided_by" in p]
        assert said, f"{where} 里根本没解释 previously_decided_by"
        blob = "\n".join(said)
        for src in sorted(db._DECISION_SOURCES):
            assert src in blob, f"{where} 没说 {src} 这种来源是什么意思"


# ══════════════════════════════════════════════════════════════════════
# 2 · 拒绝的那几种情况, 一行都不许碰
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("bad", ["pending", "approve", "", "ok", "APPROVED"])
def test_only_two_decisions_are_accepted(bad):
    """⚠️ `pending` 也在拒绝之列: 迭代后重置回待审是 `DecisionSource.SYSTEM`,
    既不是审稿也不是检测。让它从人审入口进来就等于伪造一条人工决策。"""
    c = _client()
    out = core.review_drafts(c, PROJ, [
        {"version_id": VER_A, "decision": bad}], user_id=ME)

    assert out["reviewed"] == 0
    assert out["results"][0]["outcome"] == "invalid"
    assert _row(c, ITEM_A)["status"] == "pending", "非法决策不许改到库里"
    assert _row(c, ITEM_A)["decision_source"] is None


def test_other_projects_version_is_not_reachable():
    c = _client()
    out = core.review_drafts(c, PROJ, [
        {"version_id": VER_X, "decision": "approved"}], user_id=ME)

    assert out["reviewed"] == 0
    assert out["results"][0]["outcome"] == "not_found"
    assert _row(c, ITEM_X)["status"] == "pending", "别的项目的稿子一行都不许碰"
    assert _row(c, ITEM_X)["decision_source"] is None


def test_malformed_version_id_does_not_take_the_whole_batch_down():
    """⚠️ codex review P2, 在真 PG 16.13 上复现过。

    `versions.id` 是 uuid 列, `.in_()` 里混进一个打错的值, PostgreSQL 在**解析**
    阶段就拒掉整条查询(`invalid input syntax for type uuid`)——不是跳过那一个。
    于是一条手抖打错的 id 会连累同一批里所有好的, 而这个函数的契约明写着部分
    失败要逐条报。
    """
    c = _client()
    out = core.review_drafts(c, PROJ, [
        {"version_id": VER_A, "decision": "approved"},
        {"version_id": "手抖打错的id", "decision": "approved"},
        {"version_id": VER_B, "decision": "needs_revision"},
    ], user_id=ME)

    assert out["reviewed"] == 2, "好的那两条必须照常落库"
    assert [r["outcome"] for r in out["results"]] == [
        "recorded", "invalid", "recorded"]
    assert "UUID" in out["results"][1]["detail"]
    assert _row(c, ITEM_A)["status"] == "approved"
    assert _row(c, ITEM_B)["status"] == "needs_revision"


def test_same_item_twice_is_refused_not_silently_overwritten():
    """⚠️ codex review P2。

    同一条稿子在一批里被点两次(还是相反的结论)时, 静默让最后一次生效是最糟的
    选择: 两条都会报 recorded, 而第二条的 previous_status 取自更新前的快照、
    已经是错的 —— 覆盖就这么被藏起来了。拒绝才能让调用方发现自己点重了。
    """
    c = _client()
    out = core.review_drafts(c, PROJ, [
        {"version_id": VER_A, "decision": "approved"},
        {"version_id": VER_A, "decision": "needs_revision"},
    ], user_id=ME)

    assert out["reviewed"] == 1
    assert [r["outcome"] for r in out["results"]] == ["recorded", "duplicate"]
    assert VER_A in out["results"][1]["detail"]
    assert _row(c, ITEM_A)["status"] == "approved", "第一条生效, 第二条没生效"


def test_someone_elses_project_is_refused():
    c = _client(owner=SOMEONE_ELSE)
    with pytest.raises(PermissionError):
        core.review_drafts(c, PROJ, [
            {"version_id": VER_A, "decision": "approved"}], user_id=ME)


def test_batch_cap_is_enforced():
    c = _client()
    too_many = [{"version_id": VER_A, "decision": "approved"}
                ] * (core.MAX_REVIEW_DRAFTS + 1)
    with pytest.raises(ValueError):
        core.review_drafts(c, PROJ, too_many, user_id=ME)


# ══════════════════════════════════════════════════════════════════════
# 3 · 部分成功要照实报
# ══════════════════════════════════════════════════════════════════════

def test_partial_success_reports_both_sides():
    """一条好的 + 一条坏的 —— 好的那条必须真的落库, 坏的那条必须被点名。

    行已经改了而调用方以为没改, 比报一条失败更难查(同
    `store.mint_draft_identity` 的取舍)。
    """
    c = _client()
    out = core.review_drafts(c, PROJ, [
        {"version_id": VER_A, "decision": "approved"},
        {"version_id": VER_X, "decision": "approved"},       # 别的项目
        {"version_id": VER_B, "decision": "bogus"},          # 非法决策
    ], user_id=ME)

    assert out["reviewed"] == 1
    outcomes = [r["outcome"] for r in out["results"]]
    assert outcomes == ["recorded", "not_found", "invalid"]
    assert _row(c, ITEM_A)["status"] == "approved"
    assert _row(c, ITEM_B)["status"] == "pending"
    assert _row(c, ITEM_X)["status"] == "pending"


def test_empty_input_is_a_noop():
    c = _client()
    assert core.review_drafts(c, PROJ, [], user_id=ME) == {
        "reviewed": 0, "results": []}


# ══════════════════════════════════════════════════════════════════════
# 4 · 入库仍然【不】盖决策戳 —— 这条不许因为有了审核入口就松掉
# ══════════════════════════════════════════════════════════════════════

def test_commit_still_does_not_stamp_a_decision():
    """有了人审入口之后，更要守住"定稿 ≠ 审核"。

    最容易犯的下一个错是"既然现在能记人审了，那就让 commit 顺手记一条" ——
    那等于把这个洞原样搬回来: 灌进去的是清一色正例, 而且没有任何人真的看过。
    """
    from deskcore import store
    assert store.DESKCORE_ITEM_STATUS == "pending"
    src = __import__("inspect").getsource(store._mint_entries)
    assert "decision_source" not in src, \
        "入库路径不许写 decision_source —— 定稿不是审核"
    assert "reviewer_id" not in src, \
        "入库路径不许写 reviewer_id —— 没有人审过这条稿子"
