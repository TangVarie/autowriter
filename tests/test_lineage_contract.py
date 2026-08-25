"""导出的 lineage 列必须和 Truth Vault 认的列名**逐字相同**（跨库审计 COR-002）。

这条契约的特点是: 违反它**不报错**。飞书那边按列名匹配字段, 名字对不上的列
就是"未声明的列", 而 TV 的 D-021 会把带未声明列的**整行** quarantine —— 那条
笔记连正文带指标一起进不了库, autowriter 这边一切正常, TV 那边只是某天开始
少了一批数据。

所以判据必须是**外部的**: 下面这六个名字是从 truth-vault 抄过来的常量, 不是
从 exporter 读出来再和自己比。后者永远绿。

来源（truth-vault, 提交 2d94a67）:
  · ``docs/11-feishu-table-setup.md``「lineage 元数据列」的表
  · ``scripts/sync_feishu_notes_to_truth_vault.py`` 的
    ``_LINEAGE_FK_COLS`` / ``_LINEAGE_RAW_EXTRA_COLS``

改这里的任何一个名字之前, 先去改 TV —— 两边同时改, 而且要确认飞书表里的列
也跟着改了。
"""

from __future__ import annotations

import io

import pytest

openpyxl = pytest.importorskip("openpyxl")

import exporter


# ⚠️ 手抄自 truth-vault。**不要**改成 `exporter.LINEAGE_HEADERS` —— 那样这条
#    用例就变成"exporter 等于它自己", 永远绿, 什么都没在守。
TV_LINEAGE_COLUMNS = (
    "_source_autowriter_project_id",
    "_source_autowriter_batch_id",
    "_source_autowriter_item_id",     # → notes.source_autowriter_item_id (FK)
    "_source_autowriter_version_id",  # → notes.source_autowriter_version_id (FK)
    "_ai_engine",
    "_exported_at",
)

# 跨库审计 COR-002 抓到的两个错名字。它们**曾经**在 build_excel_document 里,
# 从写下那天起就没有任何一次真实导出暴露过 —— 因为那个函数没有调用方。
FORBIDDEN = (
    "_source_autowriter_ai_engine",
    "_source_autowriter_version_num",
)

ITEM = {
    "title": "标题一",
    "body": "正文第一行\n正文第二行",
    "keywords": ["关键词A", "关键词B"],
    "ai_engine": "claude",
    "version_num": 3,
    "project_id": "11111111-1111-1111-1111-111111111111",
    "batch_id":   "22222222-2222-2222-2222-222222222222",
    "item_id":    "33333333-3333-3333-3333-333333333333",
    "version_id": "44444444-4444-4444-4444-444444444444",
}


def _sheet(blob: bytes):
    return openpyxl.load_workbook(io.BytesIO(blob)).active


def _header_row(ws) -> list[str]:
    return [c.value for c in ws[1]]


# ══════════════════════════════════════════════════════════════════════
# 1 · 名字本身
# ══════════════════════════════════════════════════════════════════════

def test_exporter_uses_exactly_the_columns_tv_declares():
    """顺序也要一样 —— 两个 builder 都按位置写值, 顺序错了就是全体串位。"""
    assert exporter.LINEAGE_HEADERS == TV_LINEAGE_COLUMNS


def test_the_two_wrong_names_are_gone():
    assert not set(FORBIDDEN) & set(exporter.LINEAGE_HEADERS)


# ══════════════════════════════════════════════════════════════════════
# 2 · 单列导出（导出中心真正调的那个）
# ══════════════════════════════════════════════════════════════════════

def test_combined_excel_has_a_header_row():
    """没有表头 = 飞书认不出列 = lineage 白写。

    这正是原来那版的病根: ``enumerate(items, 1)`` 从第 1 行就写数据, 整张表
    没有一个列名, 而 lineage 还塞在隐藏的无名 B 列里。
    """
    ws = _sheet(exporter.build_combined_excel([ITEM]))
    assert _header_row(ws) == [exporter.CONTENT_HEADER, *TV_LINEAGE_COLUMNS]


def test_combined_excel_writes_lineage_under_the_right_headers():
    """按表头找列, 再核值 —— 不按固定下标, 免得以后加列时这条假绿。"""
    ws = _sheet(exporter.build_combined_excel([ITEM], exported_at="2026-08-25T00:00:00"))
    col = {name: i for i, name in enumerate(_header_row(ws), 1)}
    row = 2

    for header, key in (("_source_autowriter_project_id", "project_id"),
                        ("_source_autowriter_batch_id",   "batch_id"),
                        ("_source_autowriter_item_id",    "item_id"),
                        ("_source_autowriter_version_id", "version_id")):
        assert ws.cell(row=row, column=col[header]).value == ITEM[key], header

    assert ws.cell(row=row, column=col["_exported_at"]).value == "2026-08-25T00:00:00"


def test_ai_engine_keeps_its_case():
    """TV 按 ``_ai_engine`` GROUP BY 出模型胜率 —— 'CLAUDE' 和 'claude' 会被
    算成两个引擎, 而两边各自的爆率都是错的。展示用的大写只能留在展示列。"""
    ws = _sheet(exporter.build_combined_excel([ITEM]))
    col = {name: i for i, name in enumerate(_header_row(ws), 1)}
    assert ws.cell(row=2, column=col["_ai_engine"]).value == "claude"


def test_combined_excel_still_puts_the_copy_in_one_cell():
    """A 列的用法**没变**: 一篇稿子一格, 格内换行。改动只在 lineage 那几列。"""
    ws = _sheet(exporter.build_combined_excel([ITEM]))
    cell = ws.cell(row=2, column=1).value
    assert cell.startswith("标题：标题一")
    assert "正文：正文第一行\n正文第二行" in cell
    assert "#关键词A #关键词B" in cell


def test_lineage_columns_are_not_hidden():
    """隐藏列**只有整表导入才跟着走**。运营的实际动作是选中可见列复制粘贴 ——
    一粘隐藏列就没了, 而这正是 lineage 从上线起就没到过飞书的原因。
    truth-vault 的 docs/11 把这条单独列为待解决的坑。"""
    ws = _sheet(exporter.build_combined_excel([ITEM]))
    hidden = [letter for letter, dim in ws.column_dimensions.items() if dim.hidden]
    assert not hidden, f"这些列是隐藏的, 粘贴时会丢: {hidden}"


def test_missing_ids_do_not_shift_the_later_columns():
    """缺 id 的稿子(比如手写的、没走过生成流程的)不能把后面的列挤位。

    ⚠️ 判据**不是**"那一格等于空串" —— openpyxl 写空串, 读回来是 ``None``,
    这是它的正常行为, 不是 bug。真正要钉的是**位置**: 前面几列没值时,
    ``_exported_at`` 仍然落在表头指的那一列上。串位之后每个值看起来都还是
    合法字符串, 没有任何东西会报错, 只有 TV 那边悄悄归因到错的 item 上。
    """
    ws = _sheet(exporter.build_combined_excel(
        [{"title": "无出处", "body": "x"}], exported_at="2026-08-25T00:00:00"))
    col = {name: i for i, name in enumerate(_header_row(ws), 1)}
    assert not ws.cell(row=2, column=col["_source_autowriter_item_id"]).value
    assert ws.cell(row=2, column=col["_exported_at"]).value == "2026-08-25T00:00:00"


# ══════════════════════════════════════════════════════════════════════
# 3 · 分列导出（当前无调用方, 但契约同一份）
# ══════════════════════════════════════════════════════════════════════

def test_legacy_excel_uses_the_same_contract():
    ws = _sheet(exporter.build_excel_document([ITEM], "项目", "品牌", "战术"))
    headers = _header_row(ws)
    assert headers[-len(TV_LINEAGE_COLUMNS):] == list(TV_LINEAGE_COLUMNS)
    assert not set(FORBIDDEN) & set(headers)


def test_legacy_excel_display_columns_are_untouched():
    """前 6 列是给人看的, 保持原样(大写引擎名、"v3")—— 契约只管后 6 列。"""
    ws = _sheet(exporter.build_excel_document([ITEM], "项目", "品牌", "战术"))
    assert [c.value for c in ws[2]][:6] == [
        1, "标题一", "正文第一行\n正文第二行", "#关键词A #关键词B", "CLAUDE", "v3"]


# ══════════════════════════════════════════════════════════════════════
# 4 · 守卫本身
# ══════════════════════════════════════════════════════════════════════

def test_the_header_check_would_catch_a_renamed_column(monkeypatch):
    """上面几条要是永远绿, 它们什么都没在守。

    把契约表换成一个改了名的版本, 断言 build_combined_excel 产出的表头确实跟着
    变了 —— 也就是说这些用例读的是**真的写进文件的东西**, 不是常量自比。
    """
    bad = tuple((("_wrong_name" if n == "_ai_engine" else n), k)
                for n, k in exporter.LINEAGE_COLUMNS)
    monkeypatch.setattr(exporter, "LINEAGE_COLUMNS", bad)
    monkeypatch.setattr(exporter, "LINEAGE_HEADERS", tuple(n for n, _ in bad))

    ws = _sheet(exporter.build_combined_excel([ITEM]))
    assert "_wrong_name" in _header_row(ws)
    assert _header_row(ws) != [exporter.CONTENT_HEADER, *TV_LINEAGE_COLUMNS]
