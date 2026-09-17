"""把已发未入库的稿子从飞书表读回来 —— 解析层。

背景见 deskcore/ingest.py 的模块说明: 09-11 起 78% 的稿子没走 commit, 指纹库
对它们一无所知, 下一批查重看不见。补救是从飞书表读回来补指纹。

这里盯: 两种表形状都认; 带 version_id 的行(已入库)跳过; 认不出形状要把表头
列出来而不是静默读成空; 列名与 exporter 那边一致。
"""

from __future__ import annotations

import pytest

from deskcore import ingest

openpyxl = pytest.importorskip("openpyxl")


def _xlsx(tmp_path, header, rows, name="t.xlsx"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    p = tmp_path / name
    wb.save(p)
    return p


LINEAGE = ["_source_autowriter_project_id", "_source_autowriter_batch_id",
           "_source_autowriter_item_id", "_source_autowriter_version_id",
           "_ai_engine", "_exported_at"]


def test_export_shaped_sheet_is_parsed_and_committed_rows_are_skipped(tmp_path):
    p = _xlsx(tmp_path, ["内容", *LINEAGE], [
        ["标题：真走过 commit 的\n\n正文：它已经有指纹了",
         "p", "b", "i", "dddd0000-0000-0000-0000-00000000000a", "deskcore", "2026-09-15"],
        ["标题：没入库的一\n\n正文：第一段\n第二段", "", "", "", "", "", ""],
        ["标题：没入库的二\n\n正文：只有一段", None, None, None, None, None, None],
        [None, None, None, None, None, None, None],
    ])
    out = ingest.read_published_xlsx(p)
    assert out["shape"] == "export"
    assert out["skipped_with_lineage"] == 1, "带 version_id 的行已经在库里, 不许再补一份"
    assert out["skipped_empty"] == 1
    assert [(r.row, r.title, r.body) for r in out["rows"]] == [
        (3, "没入库的一", "第一段\n第二段"),
        (4, "没入库的二", "只有一段"),
    ]


def test_two_column_sheet_is_parsed(tmp_path):
    p = _xlsx(tmp_path, ["标题", "正文", "备注"], [
        ["甲", "甲的正文", "x"],
        ["", "", ""],
        ["乙", "乙的正文", None],
    ])
    out = ingest.read_published_xlsx(p)
    assert out["shape"] == "columns"
    assert [(r.title, r.body) for r in out["rows"]] == [("甲", "甲的正文"), ("乙", "乙的正文")]


def test_custom_column_names(tmp_path):
    p = _xlsx(tmp_path, ["笔记标题", "笔记正文"], [["a", "b"]])
    out = ingest.read_published_xlsx(p, title_col="笔记标题", body_col="笔记正文")
    assert [(r.title, r.body) for r in out["rows"]] == [("a", "b")]


def test_unrecognised_shape_names_the_headers_instead_of_reading_nothing(tmp_path):
    """静默读成 0 行最危险: 运营以为补完了, 指纹库照旧是空的。"""
    p = _xlsx(tmp_path, ["A", "B"], [["x", "y"]])
    with pytest.raises(ValueError) as e:
        ingest.read_published_xlsx(p)
    assert "A" in str(e.value) and "B" in str(e.value)
    assert "--title-col" in str(e.value)


def test_missing_custom_column_is_named(tmp_path):
    p = _xlsx(tmp_path, ["标题", "正文"], [["x", "y"]])
    with pytest.raises(ValueError) as e:
        ingest.read_published_xlsx(p, title_col="不存在的列")
    assert "不存在的列" in str(e.value)


@pytest.mark.parametrize("cell, expected", [
    ("标题：甲\n\n正文：一\n二", ("甲", "一\n二")),
    ("标题: 半角冒号\n正文: 也认", ("半角冒号", "也认")),
    ("标题：只有标题", ("只有标题", "")),
    ("运营手改过, 没有前缀的一整格", ("", "运营手改过, 没有前缀的一整格")),
    ("", ("", "")),
    (None, ("", "")),
])
def test_parse_content_cell(cell, expected):
    assert ingest.parse_content_cell(cell) == expected


def test_column_names_match_the_exporter():
    """列名手抄在 ingest 里(不想引 exporter 那一堆依赖) —— 那就得有人盯着两边一致,
    exporter 改了列名这里会静默读不到 lineage、把已入库的稿子再补一份。"""
    import exporter
    assert ingest.CONTENT_HEADER == exporter.CONTENT_HEADER
    assert ingest.VERSION_ID_HEADER in exporter.LINEAGE_HEADERS


# ══════════════════════════════════════════════════════════════════════
# 入库那一步(core.ingest_published): 身份 + 指纹, 不过闸
# ══════════════════════════════════════════════════════════════════════

from deskcore import core  # noqa: E402
from tests.fakes import FakeClient  # noqa: E402

ME = "11111111-1111-1111-1111-111111111111"
OTHER = "99999999-9999-9999-9999-999999999999"
PROJ = "aaaaaaaa-0000-0000-0000-000000000001"


def _client(owner=ME) -> FakeClient:
    return FakeClient(rows={"projects": [
        {"id": PROJ, "name": "途鸽", "brand": "途鸽", "owner_id": owner,
         "calibration_notes": "", "tactics": "[]", "custom_roles": []}]})


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)


def test_ingest_mints_identity_with_provenance_and_writes_fingerprints():
    c = _client()
    out = core.ingest_published(c, PROJ, [
        {"title": "发出去的一", "body": "第一篇的正文, 已经在小红书上了。" * 5},
        {"title": "发出去的二", "body": "第二篇的正文, 也已经发了。" * 5},
        {"title": "", "body": ""},                    # 空行, 不写
    ], user_id=ME, source="途鸽-9月.xlsx")
    assert (out["received"], out["to_write"], out["minted"], out["fingerprinted"]) == (3, 2, 2, 2)
    assert out["identity_error"] is None and out["batch_id"]

    items = c.rows["items"]
    assert {i["status"] for i in items} == {"pending"}, "补进来的更不能盖决策戳"
    assert "decision_source" not in items[0]
    # ⚠️ items.external_source 是 TV 同步的标记('truth_vault'), TV 按它认自己
    #    的行 —— 补进来的稿子**不许**借用它记出处, 出处在 batch 上。
    assert all("external_source" not in i for i in items)

    versions = {v["id"]: v for v in c.rows["versions"]}
    fps = c.rows["draft_fingerprints"]
    assert {f["version_id"] for f in fps} == set(versions), "指纹指向真建出来的 version"
    assert all(f["angle_key"] is None for f in fps)
    assert all(f["opening_hash"] and f["ngram_hashes"] for f in fps)
    assert c.rows["batches"][0]["params"] == {"source": "ingest", "file": "途鸽-9月.xlsx"}, \
        "出处记在 batch 上, 以后分得清这批不是写作台当场写的"


def test_ingest_does_not_run_the_gate():
    """已经发出去的稿子跟库里谁重复都改变不了事实 —— 拦它没有意义, 拦了下一批
    照样撞它。所以一模一样的正文也要照收。"""
    c = _client()
    body = "完全一样的正文, 发过两次。" * 6
    out = core.ingest_published(c, PROJ, [
        {"title": "a", "body": body}, {"title": "b", "body": body},
    ], user_id=ME, source="x")
    assert out["fingerprinted"] == 2
    assert not any(n == "deskcore_commit_fingerprints" for n, _ in c.rpc_calls)


def test_dry_run_writes_nothing():
    c = _client()
    out = core.ingest_published(c, PROJ, [{"title": "a", "body": "b" * 40}],
                                user_id=ME, source="x", dry_run=True)
    assert out["dry_run"] is True and out["to_write"] == 1 and out["minted"] == 0
    assert "items" not in c.rows and "draft_fingerprints" not in c.rows


def test_someone_elses_project_is_refused():
    with pytest.raises(PermissionError):
        core.ingest_published(_client(owner=OTHER), PROJ,
                              [{"title": "a", "body": "b" * 40}], user_id=ME, source="x")
