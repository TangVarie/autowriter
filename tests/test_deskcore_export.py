"""写作台的导出: 稿子 + **命名可见**的 lineage 列, 用来粘进飞书表。

这一步是「写作台 → 飞书表 → Truth Vault → 指标回流」这条闭环的第一段, 而这一段
一直是手工的（运营复制粘贴）。粘的时候不带 lineage, TV 就只知道"有这么一条笔记",
不知道它是谁写的哪一版 —— ``truth_vault.v_model_comparison`` 就是这么长期查出
空集的, 而且不报错。

这里盯三件事:

  1. **不看 status** —— 写作台的稿子是 pending 且永远不会变 approved, 拿导出中心
     那条路(``_collect_approved_items`` 第一行就是 status 判断)导等于永远导空;
  2. **列名是 TV 的那六个**, 且真的写进了文件;
  3. **不给范围就报错** —— 猜错批次会把错的 lineage 粘进飞书表, 比导不出来难查。
"""

from __future__ import annotations

import base64
import io

import pytest

openpyxl = pytest.importorskip("openpyxl")

import exporter
from deskcore import core
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
PROJ = "aaaaaaaa-0000-0000-0000-000000000001"
BATCH = "bbbbbbbb-0000-0000-0000-000000000002"
ITEM = "cccccccc-0000-0000-0000-000000000003"
VER = "dddddddd-0000-0000-0000-000000000004"


def _client(items=None) -> FakeClient:
    # 假件把 PostgREST 的 embedded join 建模成"行里本来就带着嵌套结构"。
    default = [{
        "id": ITEM, "batch_id": BATCH, "best_version_id": VER,
        "created_at": "2026-08-25T00:00:00Z", "user_id": ME,
        "status": "pending",          # ← 写作台的稿子就是这个状态
        "versions": [{"id": VER, "title": "标题一", "body": "正文一二三",
                      "keywords": ["k1"], "ai_engine": "deskcore",
                      "version_num": 1}],
        "batches": {"project_id": PROJ},
    }]
    return FakeClient(rows={
        "projects": [{"id": PROJ, "name": "项目", "brand": "A", "owner_id": ME,
                      "calibration_notes": "", "tactics": "[]",
                      "custom_roles": []}],
        "items": default if items is None else items,
    })


def _sheet(out: dict):
    return openpyxl.load_workbook(
        io.BytesIO(base64.b64decode(out["xlsx_base64"]))).active


# ══════════════════════════════════════════════════════════════════════
# 1 · 导得出来, 而且不看 status
# ══════════════════════════════════════════════════════════════════════

def test_exports_a_pending_deskcore_draft():
    """⚠️ 这条是这个文件存在的理由。

    ``app._collect_approved_items`` 第一行是 ``if item["status"] != "approved"``。
    写作台的稿子是 ``pending`` 且**永远不会**变成 approved —— 它们从没进过审核流
    (理由见 ``store.DESKCORE_ITEM_STATUS``: 标 approved 会让每条定稿变成一条伪造
    的人工评价灌进 TV 的评估模型)。所以导出必须按 batch/version 点名, 不看 status。
    """
    out = core.export_drafts(_client(), PROJ, batch_id=BATCH, user_id=ME)
    assert out["count"] == 1
    assert out["preview"] == [{"title": "标题一", "version_id": VER}]


def test_exported_sheet_carries_the_lineage_under_tv_column_names():
    out = core.export_drafts(_client(), PROJ, batch_id=BATCH, user_id=ME)
    ws = _sheet(out)
    header = [c.value for c in ws[1]]
    assert header == [exporter.CONTENT_HEADER, *exporter.LINEAGE_HEADERS]

    col = {name: i for i, name in enumerate(header, 1)}
    assert ws.cell(row=2, column=col["_source_autowriter_project_id"]).value == PROJ
    assert ws.cell(row=2, column=col["_source_autowriter_batch_id"]).value == BATCH
    assert ws.cell(row=2, column=col["_source_autowriter_item_id"]).value == ITEM
    assert ws.cell(row=2, column=col["_source_autowriter_version_id"]).value == VER
    assert ws.cell(row=2, column=col["_ai_engine"]).value == "deskcore"


def test_the_returned_columns_match_the_file():
    """返回值里的 ``columns`` 是给用户拿去建飞书表列的。

    和文件里真实的表头对不上就等于让人建错列, 而建错列的后果是 TV 把整行
    quarantine —— 所以两者必须是同一份, 不能各写各的。
    """
    out = core.export_drafts(_client(), PROJ, batch_id=BATCH, user_id=ME)
    assert out["columns"] == [c.value for c in _sheet(out)[1]]


def test_version_ids_narrow_the_selection():
    other_item = {
        "id": "eeeeeeee-0000-0000-0000-000000000005", "batch_id": BATCH,
        "best_version_id": "ffffffff-0000-0000-0000-000000000006",
        "created_at": "2026-08-25T00:01:00Z", "user_id": ME, "status": "pending",
        "versions": [{"id": "ffffffff-0000-0000-0000-000000000006",
                      "title": "标题二", "body": "另一篇", "keywords": [],
                      "ai_engine": "deskcore", "version_num": 1}],
        "batches": {"project_id": PROJ},
    }
    c = _client(items=[*_client().rows["items"], other_item])
    out = core.export_drafts(c, PROJ, version_ids=[VER], user_id=ME)
    assert out["count"] == 1
    assert out["preview"][0]["version_id"] == VER


# ══════════════════════════════════════════════════════════════════════
# 2 · 不给范围就报错
# ══════════════════════════════════════════════════════════════════════

def test_refuses_to_guess_the_scope():
    """两个都不给 = 只能靠猜。猜错了导的是别的批次, 而错的 lineage 一旦粘进
    飞书表, TV 会安安静静地把这批笔记归因到不相干的稿子上。"""
    with pytest.raises(ValueError, match="batch_id"):
        core.export_drafts(_client(), PROJ, user_id=ME)


def test_nothing_found_says_why():
    """空结果不能是一个光秃秃的 0 —— 用户要能分清"批次错了"和"还没 commit"。"""
    out = core.export_drafts(_client(), PROJ, batch_id="不存在的批次", user_id=ME)
    assert out["count"] == 0
    assert out["xlsx_base64"] is None
    assert "commit_drafts" in out["note"]


# ══════════════════════════════════════════════════════════════════════
# 3 · 和 commit_drafts 接得上
# ══════════════════════════════════════════════════════════════════════

def test_commit_then_export_round_trip(monkeypatch):
    """commit 回的 batch_id 直接能喂给 export —— 这是这条链唯一的接口。

    接不上的话调用方得自己去猜"我刚提交的那批叫什么", 而猜的办法只有按时间
    倒排, 多人同时用同一个项目时就会导到别人的稿子。
    """
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    c = FakeClient(rows={
        "projects": [{"id": PROJ, "name": "项目", "brand": "A", "owner_id": ME,
                      "calibration_notes": "", "tactics": "[]",
                      "custom_roles": []}],
    })
    c.rpc_impl["deskcore_commit_fingerprints"] = lambda args: [
        {"idx": 0, "status": core.COMMIT_STATUS_INSERTED,
         "collided_with": None, "detail": None}]

    committed = core.commit_drafts(
        c, PROJ, [{"title": "新稿", "body": "正文正文正文", "keywords": []}],
        user_id=ME)
    assert committed["batch_id"] and committed["version_ids"]

    # commit 建出来的 items 行是平的; 导出走的是 embedded join, 假件不会自己
    # 把 versions 嵌进去 —— 这里手动补上那一层, 模拟 PostgREST 的返回形状。
    ver_id = committed["version_ids"][0]
    for row in c.rows["items"]:
        row["versions"] = [v for v in c.rows["versions"] if v["id"] == ver_id]
        row["batches"] = {"project_id": PROJ}

    out = core.export_drafts(c, PROJ, batch_id=committed["batch_id"], user_id=ME)
    assert out["count"] == 1
    ws = _sheet(out)
    col = {name: i for i, name in enumerate([c.value for c in ws[1]], 1)}
    assert ws.cell(row=2, column=col["_source_autowriter_version_id"]).value == ver_id
