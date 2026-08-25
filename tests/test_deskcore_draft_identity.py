"""写作台的定稿必须在库里**有身份**（batch / item / version）。

在此之前 ``commit_drafts`` 只写 ``draft_fingerprints``、``version_id`` 留空。
``deskcore/core.py`` 里那段注释自己写着「WorkBuddy 写的稿子 version_id 为空、
根本不在 autowriter.versions 里」。三层后果, 一层比一层远:

  · 回填(走 items × versions)永远扫不到它们;
  · 角度台账的 ``consumed_version_id`` 只能塞一个 hash 出来的假 UUID;
  · **导出的 lineage 没有 version_id 可写** —— 而 Truth Vault 的
    ``v_model_comparison`` 正是 JOIN 在 ``autowriter.versions.id`` 上。于是
    「写作台写出去的稿子爆没爆」这件事在数据上根本问不出来, 那个 view 长期
    查出来是空集, 还不报错。

本文件盯四件事:

  1. 入了库的建身份, **被判撞车的不建**（否则审核页会多出没交付的孤儿）;
  2. 建出来的 item 是 ``pending`` 且**不带任何决策戳**（见下面那条用例, 这是
     整个文件里最要紧的一条）;
  3. 指纹行里的 version_id 和真的建出来的那一行**是同一个**;
  4. 身份建失败时**不谎报** —— 返回值里不许出现建不成的 id。
"""

from __future__ import annotations

import pytest

from deskcore import core, store
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
PROJ = "aaaaaaaa-0000-0000-0000-000000000001"


def _client(**extra) -> FakeClient:
    rows = {"projects": [
        {"id": PROJ, "name": "项目", "brand": "A", "owner_id": ME,
         "calibration_notes": "", "tactics": "[]", "custom_roles": []},
    ]}
    rows.update(extra)
    return FakeClient(rows=rows)


def _commit(client, drafts, *, rpc=None):
    """跑一次 commit_drafts。默认让 RPC 全部判 inserted。"""
    if rpc is not None:
        client.rpc_impl["deskcore_commit_fingerprints"] = rpc
    else:
        client.rpc_impl["deskcore_commit_fingerprints"] = lambda args: [
            {"idx": i, "status": core.COMMIT_STATUS_INSERTED,
             "collided_with": None, "detail": None}
            for i in range(len(args.get("_rows") or []))
        ]
    return core.commit_drafts(client, PROJ, drafts, user_id=ME)


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch):
    """向量不是这个文件关心的东西, 关掉免得测试去打真 API。"""
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)


DRAFT = {"title": "标题一", "body": "正文一二三四五六七八", "keywords": ["a"]}


# ══════════════════════════════════════════════════════════════════════
# 1 · 入了库的建身份
# ══════════════════════════════════════════════════════════════════════

def test_committed_draft_gets_a_batch_item_and_version():
    c = _client()
    out = _commit(c, [DRAFT])

    assert out["batch_id"], "没建 batch"
    assert len(out["version_ids"]) == 1

    vid = out["version_ids"][0]
    versions = c.rows["versions"]
    assert len(versions) == 1 and versions[0]["id"] == vid
    assert versions[0]["title"] == "标题一"
    assert versions[0]["body"] == DRAFT["body"]

    items = c.rows["items"]
    assert len(items) == 1
    assert items[0]["batch_id"] == out["batch_id"]
    # best 指针要指向唯一那一版 —— 导出和"挑代表版本"都按它取
    assert items[0]["best_version_id"] == vid


def _rpc_rows(c) -> list[dict]:
    """交给 deskcore_commit_fingerprints 的那批指纹行。

    ⚠️ 原子路径下指纹是**在 RPC 里面**写的, 假件的 rows 里不会出现
    ``draft_fingerprints`` 表。所以判据取的是"我们递进去的东西" —— 那本来就是
    契约点所在。
    """
    name, args = c.rpc_calls[-1]
    assert name == "deskcore_commit_fingerprints", name
    return args["_rows"]


def test_the_fingerprint_and_the_version_share_one_id():
    """指纹行里写的 version_id 必须就是真建出来的那一行。

    对不上的话没有任何报错: 查重照常工作(它不看 version_id), 只是回填的幂等
    判据(``existing_fingerprint_version_ids``)和导出的 lineage 一起指向空气。
    """
    c = _client()
    out = _commit(c, [DRAFT])
    rows = _rpc_rows(c)
    assert len(rows) == 1
    assert rows[0]["version_id"] == out["version_ids"][0]
    assert c.rows["versions"][0]["id"] == rows[0]["version_id"]


def test_rejected_drafts_get_no_identity():
    """被判撞车的**不建身份** —— 它没有交付。

    建了的话, 审核页会多出一条谁也没写过的待审稿, 而且它会进回填、进正例池的
    候选。这正是"先建 versions 再写指纹"那个顺序会留下的孤儿。
    """
    c = _client()
    out = _commit(c, [DRAFT, {"title": "标题二", "body": "另一篇正文"}],
                  rpc=lambda args: [
                      {"idx": 0, "status": core.COMMIT_STATUS_INSERTED,
                       "collided_with": None, "detail": None},
                      {"idx": 1, "status": "rejected",
                       "collided_with": "库里的老稿", "detail": "四字串重合过高"},
                  ])
    assert len(out["rejected"]) == 1
    assert len(out["version_ids"]) == 1
    assert len(c.rows["items"]) == 1
    assert len(c.rows["versions"]) == 1


def test_caller_supplied_version_id_is_not_re_minted():
    """稿子是 UI 生成的(库里本来就有那一版)时, 不重新造 id、也不再建一遍身份。"""
    c = _client()
    existing = "99999999-9999-9999-9999-999999999999"
    out = _commit(c, [{**DRAFT, "version_id": existing}])
    assert out["version_ids"] == []
    assert not c.rows.get("versions")
    assert _rpc_rows(c)[0]["version_id"] == existing


# ══════════════════════════════════════════════════════════════════════
# 2 · 状态: 不许伪造一次人工审稿
# ══════════════════════════════════════════════════════════════════════

def test_minted_item_is_pending_with_no_decision_stamp():
    """⚠️ 这条是整个文件里最要紧的。

    truth-vault 的 ``sync_autowriter_decisions_to_prepublish.py`` 按
    ``status in ('approved', 'needs_revision')`` 捞行, 把捞到的**全部**写进
    ``prepublish_evaluations`` 且 ``evaluator_type='human'`` —— 它**不读**
    我们刚加的 ``decision_source``（跨库审计 COR-004 的三列, TV 侧还没接）。

    所以这里只要写 'approved', 每条写作台定稿都会立刻变成一条「人工评价」去
    校准 TV 的评估模型; 而写作台根本没有「打回」这个动作, 灌进去的会是清一色
    正例。COR-004 治的就是"机器判定被当人工反馈", 那样等于换个门重犯一次。

    pending + 决策三列全空 = 「这条稿子存在, 但没有任何审核决策」—— 是实话,
    而且 TV 的 ``.in_(...)`` 过滤天然把它排除在外。
    """
    c = _client()
    _commit(c, [DRAFT])
    item = c.rows["items"][0]
    assert item["status"] == "pending"
    for col in ("decision_source", "reviewer_id", "decided_at"):
        assert not item.get(col), f"写作台定稿不该带 {col}"


def test_the_status_constant_is_not_one_tv_syncs():
    """把上面那条的判据提到常量上: 改动 DESKCORE_ITEM_STATUS 就会红。

    这两个值是 TV 那边 ``_STATUS_TO_DECISION`` 的键 —— 手抄过来, 不从本仓读,
    否则就是自己和自己比。
    """
    assert store.DESKCORE_ITEM_STATUS not in ("approved", "needs_revision")


# ══════════════════════════════════════════════════════════════════════
# 3 · 角度台账用真 id
# ══════════════════════════════════════════════════════════════════════

def test_angle_ledger_consumes_with_the_real_version_id():
    """台账的 consumed_version_id 以前只能塞一个 hash 出来的假 UUID。"""
    c = _client(angle_ledger=[
        {"id": "led-1", "project_id": PROJ, "angle_key": "K1",
         "consumed_version_id": None, "consumed_at": None},
    ])
    out = _commit(c, [{**DRAFT, "angle_key": "K1"}])
    assert out["consumed_angles"] == 1
    assert c.rows["angle_ledger"][0]["consumed_version_id"] == out["version_ids"][0]


# ══════════════════════════════════════════════════════════════════════
# 4 · 建不成的时候不谎报
# ══════════════════════════════════════════════════════════════════════

def test_identity_failure_is_reported_and_no_ids_are_claimed(monkeypatch):
    """身份没建成时: 指纹照样算数, 但**不许**把没建成的 id 报出去。

    报出去的话调用方会拿它去导出, 而 TV 那边 JOIN 不到任何东西 —— 比不报更坏:
    表面上 lineage 齐全, 实际归因永远落空, 且没有任何地方会报错。
    """
    monkeypatch.setattr(
        store, "mint_draft_identity",
        lambda *a, **kw: {"batch_id": None, "versions": {},
                          "error": "RuntimeError: versions 表写不进去"})
    c = _client()
    out = _commit(c, [DRAFT])

    assert out["written"] == 1, "指纹是入了库的, 不该因为身份没建成就报失败"
    assert out["version_ids"] == []
    assert "identity_warning" in out
    assert "version_id" in out["identity_warning"]


def test_partial_mint_reports_the_ones_that_made_it(monkeypatch):
    """半途失败时, **已经建成的那几条要报出来**。

    5 条里前 2 条的 items/versions 已经在库里了。把整次 mint 当作没发生、连
    batch_id 一起丢掉的话, 那两条谁也找不回来 —— 它们的指纹指向真实存在的
    version, 却没有任何人知道该去导出它们。行在库里而调用方以为没有, 比
    "报了个建不成的 id"更难查。

    同时: 报出去的条数必须是**实际建成的**, 不是"这次打算建几条"。
    """
    good = "aaaa1111-0000-0000-0000-00000000aaaa"
    monkeypatch.setattr(
        store, "mint_draft_identity",
        lambda sb, pid, uid, tactic, entries: {
            "batch_id": "batch-9",
            # 只有第一条建成了
            "versions": {entries[0]["version_id"]: "item-1"},
            "error": "RuntimeError: 第二条炸了"})

    c = _client()
    out = _commit(c, [DRAFT, {"title": "标题二", "body": "另一篇正文"}])

    assert out["batch_id"] == "batch-9", "batch_id 丢了就等于那几条找不回来"
    assert len(out["version_ids"]) == 1
    w = out["identity_warning"]
    assert "只有 1 条建成" in w, w
    assert "batch-9" in w, "要告诉调用方已建成的那部分怎么导"


def test_a_failed_version_insert_leaves_no_orphan_item():
    """item 建完、version 没建成时, 那个 item 要被**收掉**。

    留着的话它会出现在审核页上: 一条没有任何版本的空待审稿, 谁也不知道它是什么,
    而且它会进回填、进正例池的候选。(codex review P1 的残留部分 —— 上一轮我只
    修了"已建成的那部分要报出来", 没管这半条建了一半的。)

    batch 本身**故意**留着: 它是已经建成的那几条的归属, 上一层要靠 batch_id 把
    它们交回给调用方。
    """
    class _VersionsExplode(FakeClient):
        def _execute(self, q):
            if q.table_name == "versions" and q.op == "insert":
                raise RuntimeError("versions 写不进去")
            return super()._execute(q)

    c = _VersionsExplode(rows=_client().rows)
    c.rpc_impl["deskcore_commit_fingerprints"] = lambda args: [
        {"idx": 0, "status": core.COMMIT_STATUS_INSERTED,
         "collided_with": None, "detail": None}]

    out = core.commit_drafts(c, PROJ, [DRAFT], user_id=ME)

    assert out["written"] == 1, "指纹入了库, 不该报失败"
    assert out["version_ids"] == []
    assert "identity_warning" in out
    assert not c.rows.get("items"), (
        f"留下了没有版本的孤儿 item: {c.rows.get('items')} —— "
        "它会出现在审核页上, 而谁也说不清它是什么")


def test_identity_failure_does_not_raise():
    """整个调用不许因此抛错。

    抛了的话调用方会重试, 而重试会被自己刚写进去的指纹判成撞车 —— 一次故障
    变成一次"你的稿子重复了", 现场完全对不上。
    """
    c = _client()
    c.rows["versions"] = []

    class _Explode(FakeClient):
        def table(self, name):
            if name == "versions":
                raise RuntimeError("boom")
            return super().table(name)

    ec = _Explode(rows=c.rows)
    ec.rpc_impl["deskcore_commit_fingerprints"] = lambda args: [
        {"idx": 0, "status": core.COMMIT_STATUS_INSERTED,
         "collided_with": None, "detail": None}]
    out = core.commit_drafts(ec, PROJ, [DRAFT], user_id=ME)
    assert out["written"] == 1
    assert "identity_warning" in out
