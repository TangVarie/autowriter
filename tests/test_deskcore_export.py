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


DEFAULT_ITEMS = [{
    "id": ITEM, "batch_id": BATCH, "best_version_id": VER,
    "created_at": "2026-08-25T00:00:00Z", "user_id": ME,
    "status": "pending",          # ← 写作台的稿子就是这个状态
    "versions": [{"id": VER, "title": "标题一", "body": "正文一二三",
                  "keywords": ["k1"], "ai_engine": "deskcore",
                  "version_num": 1}],
    "batches": {"project_id": PROJ},
}]


def _versions_table(items: list[dict]) -> list[dict]:
    """从 items 派生出 ``versions`` 表该有的样子。

    假件把 PostgREST 的 embedded join 建模成"行里本来就带着嵌套结构", 而两条查询
    路径的嵌套方向**是相反的**:

      · 按 batch 导 —— items 行里嵌 ``versions`` 和 ``batches``;
      · 按 version 点名导 —— versions 行里嵌 ``items``, items 里再嵌 ``batches``。

    从同一份源数据派生两张表, 免得两边手写着写着就漂了(而漂了之后测试仍然是绿的,
    只是两条路径各测各的幻觉)。
    """
    out = []
    for it in items:
        for v in it.get("versions") or []:
            out.append({**v, "items": {
                "id": it["id"], "batch_id": it["batch_id"],
                "batches": {"project_id": it["batches"]["project_id"]},
            }})
    return out


def _client(items=None) -> FakeClient:
    items = DEFAULT_ITEMS if items is None else items
    return FakeClient(rows={
        "projects": [{"id": PROJ, "name": "项目", "brand": "A", "owner_id": ME,
                      "calibration_notes": "", "tactics": "[]",
                      "custom_roles": []}],
        "items": items,
        "versions": _versions_table(items),
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
    c = _client(items=[*DEFAULT_ITEMS, other_item])
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
# 2b · 少导了必须说
# ══════════════════════════════════════════════════════════════════════

def test_missing_version_ids_are_named():
    """点名要了却没找到的, 要**点名报回来**。

    静默少导是本仓审计反复出现的一类(COR-006 / ROB-011 都是它), 而在导出这条路上
    它尤其阴: 少导几条 = 那几篇稿子发出去之后归因不回来, 返回值却完全正常。
    """
    ghost = "00000000-dead-dead-dead-000000000000"
    out = core.export_drafts(_client(), PROJ, version_ids=[VER, ghost], user_id=ME)
    assert out["count"] == 1
    assert out["missing_version_ids"] == [ghost]
    assert ghost in out["note"]


def test_hitting_the_cap_is_announced(monkeypatch):
    """命中数顶到上限 = 后面可能还有。不说的话这一次会被当成全量。"""
    monkeypatch.setattr(core, "MAX_EXPORT_DRAFTS", 1)
    out = core.export_drafts(_client(), PROJ, batch_id=BATCH, user_id=ME)
    assert out["truncated"] is True
    assert "上限" in out["note"]


def test_a_full_export_is_not_flagged():
    """没少导就不许报警 —— 每次都带个警告等于没有警告。"""
    out = core.export_drafts(_client(), PROJ, batch_id=BATCH, user_id=ME)
    assert out["truncated"] is False
    assert out["missing_version_ids"] == []
    assert "⚠️ 点名" not in out["note"] and "上限" not in out["note"]


# ══════════════════════════════════════════════════════════════════════
# 2c · codex review 三条
# ══════════════════════════════════════════════════════════════════════

def test_version_ids_are_filtered_in_the_database_not_after_a_window():
    """点名 version 时**不许**先取项目里最早的 N 个 item 再在 Python 里挑。

    原来那条路是 items 表 + ``order(created_at)`` 升序 + ``limit(200)``。项目一旦
    超过 200 个 item, **刚 commit 的那批永远落在窗口外** —— 拿返回的 version_id
    原样去导会得到"没找到可导的稿子", 而 id 明明是对的。升序更糟: 最新的是第一个
    被挤掉的。(codex review P1)

    判据是**被禁止的形态**: 不许从 items 表带 limit 地捞。所以只要过滤真的下推到
    了数据库, 换写法也不会假警报。
    """
    c = _client()
    core.export_drafts(c, PROJ, version_ids=[VER], user_id=ME)
    from_items = [q for q in c.calls
                  if q["table"] == "items" and q["op"] == "select"]
    assert not from_items, (
        f"点名 version 时还在扫 items 表: {from_items} —— "
        "项目超过 limit 个 item 之后, 新提交的那批就再也导不出来了")

    versions_q = [q for q in c.calls if q["table"] == "versions"]
    assert versions_q, "应该直接查 versions 表"
    assert any(f[0] == "in" and f[1] == "id" for f in versions_q[0]["filters"]), \
        "version_ids 必须作为 .in_('id', ...) 下推到数据库"


def test_a_new_version_beyond_the_item_window_is_still_found(monkeypatch):
    """把上一条落到行为上: 窗口设成 1, 点名第 2 个 item 的 version 照样导得出来。"""
    monkeypatch.setattr(core, "MAX_EXPORT_DRAFTS", 1)
    newest_v = "ffffffff-0000-0000-0000-000000000006"
    newest = {
        "id": "eeeeeeee-0000-0000-0000-000000000005", "batch_id": BATCH,
        "best_version_id": newest_v, "created_at": "2026-08-25T09:00:00Z",
        "user_id": ME, "status": "pending",
        "versions": [{"id": newest_v, "title": "最新那篇", "body": "正文",
                      "keywords": [], "ai_engine": "deskcore", "version_num": 1}],
        "batches": {"project_id": PROJ},
    }
    c = _client(items=[*DEFAULT_ITEMS, newest])
    out = core.export_drafts(c, PROJ, version_ids=[newest_v], user_id=ME)
    assert out["count"] == 1, "最新提交的那条被窗口挤掉了"
    assert out["preview"][0]["version_id"] == newest_v


def test_item_without_best_version_exports_only_the_latest():
    """``best_version_id`` 为空是**正常状态** —— 每次「AI 迭代」都会显式清掉它
    (app.py 的 R-036)。

    原来写的是 ``if best and v["id"] != best: continue``, 于是 best 为空时**每一版
    都会被 append** —— 同一篇稿子在导出的表里占好几行, 新旧混在一起, 而每行看上去
    都合法。用户照着发, 发出去的可能是被迭代掉的那一版。(codex review P1)
    """
    iterated = {
        "id": "eeeeeeee-0000-0000-0000-000000000007", "batch_id": BATCH,
        "best_version_id": None,          # ← 迭代之后就是这个状态
        "created_at": "2026-08-25T08:00:00Z", "user_id": ME, "status": "pending",
        "versions": [
            {"id": "v-old", "title": "迭代前", "body": "旧正文", "keywords": [],
             "ai_engine": "claude", "version_num": 1},
            {"id": "v-new", "title": "迭代后", "body": "新正文", "keywords": [],
             "ai_engine": "claude", "version_num": 2},
        ],
        "batches": {"project_id": PROJ},
    }
    c = FakeClient(rows={
        "projects": [{"id": PROJ, "name": "项目", "brand": "A", "owner_id": ME,
                      "calibration_notes": "", "tactics": "[]",
                      "custom_roles": []}],
        "items": [iterated],
    })
    out = core.export_drafts(c, PROJ, batch_id=BATCH, user_id=ME)
    assert out["count"] == 1, "同一篇稿子导出了多行 —— 新旧版本混在一起了"
    assert out["preview"][0]["version_id"] == "v-new", "导的该是最新那版"


class _BoomOnExecute(FakeClient):
    """在 ``execute()`` 上炸, **不是**在 ``table()`` 上。

    ⚠️ 这个区别是这条用例能不能证明东西的关键。修之前的代码长这样:

        q = sb.table("items").select(...)      # ← try 外面
        try:
            res = q.execute()                  # ← 只有这一行在 try 里
        except Exception:
            return []

    假件若在 ``table()`` 就抛, 异常压根到不了那个 except, 于是用例在**修之前也是
    绿的** —— 它看起来在守"故障要上抛", 实际什么都没守。第一版就是这么写的,
    靠"新断言必须先红"这一条才发现。
    """

    def __init__(self, rows=None, boom_tables=("items", "versions")):
        super().__init__(rows=rows)
        self._boom = set(boom_tables)

    def _execute(self, q):
        if q.table_name in self._boom:
            raise RuntimeError("PostgREST 502")
        return super()._execute(q)


@pytest.mark.parametrize("kwargs", [
    {"batch_id": BATCH},
    {"version_ids": [VER]},
], ids=["by-batch", "by-version"])
def test_a_database_failure_is_not_reported_as_an_empty_selection(kwargs):
    """查询挂了要**上抛**, 不能变成一个像成功的 ``count: 0``。

    吞掉的话, 数据库故障 / schema 错 / 查询写错和"这批确实没有"长得一模一样 ——
    调用方看到 count: 0 就不会重试, 也不会报告故障。(codex review P2)

    两条路径都要验: 按 batch 和按 version 走的是不同的查询。
    """
    c = _BoomOnExecute(rows=_client().rows)
    with pytest.raises(RuntimeError, match="502"):
        core.export_drafts(c, PROJ, user_id=ME, **kwargs)


def test_empty_result_keeps_the_same_key_shape():
    """空结果和成功结果的键要一样 —— 调用方不该为空结果写第二套解析。"""
    ok = core.export_drafts(_client(), PROJ, batch_id=BATCH, user_id=ME)
    empty = core.export_drafts(_client(), PROJ, batch_id="没有这个批次", user_id=ME)
    assert set(ok) - set(empty) == set(), sorted(set(ok) - set(empty))


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
