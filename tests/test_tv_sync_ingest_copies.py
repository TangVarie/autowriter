"""tv-sync 的三条加固(TV 2026-10-08 架构审计 A-02)。

生产库 10-08 查到 31 篇 TV 笔记的 ``source_autowriter_version_id`` 指向**从 TV 补录
进来的副本**(SPX 23 · HATHERINE 7 · LNKT 1): 补录副本留在对照索引里, 隔天另一条正文
相同的笔记(SPX 的父记录继承正文)``body_exact`` 命中它, ``--write-tv`` 就把"来自 TV 的
版本"写成了这条笔记的来源 —— tv_sync docstring 明说要排除的因果倒置, 绕了一条路回来。

同一次审计还看到: 补录分块循环和 ``--all`` 的每项目循环都没有 try, 一个 IngestBusy
让当晚整个 TV 项目一行对照都没有、后面的项目也一起跳掉。

这里钉的三个【被禁止的形态】:
  1. ``versions_for_linking`` 把 ``batches.params.source == 'ingest'`` 的版本放进索引;
  2. 补录某一块抛错之后, 前面已对上的笔记的对照跟着丢;
  3. ``tv-sync --all`` 里一个项目抛错, 后面的项目不跑。
"""

from __future__ import annotations

import pytest

from deskcore import cli, core, store
from tests.fakes import FakeClient
from tests.test_tv_sync import BODY_A, BODY_NEW, ME, NOTES, P1, TV, _client, _iso, _note


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)


def _add_ingest_copy(c, *, body=BODY_NEW, title="上周发的那篇"):
    """往假库里放一条【补录副本】: batch b2 的 params.source=ingest, 正文 = body。"""
    c.rows["batches"].append({"id": "b2", "project_id": P1, "user_id": ME,
                              "params": {"source": "ingest", "file": f"tv:{TV}"}})
    c.rows["items"].append({"id": "i2", "batch_id": "b2", "best_version_id": "v2",
                            "created_at": _iso(1), "user_id": ME, "status": "pending",
                            "versions": [{"id": "v2", "title": title, "body": body,
                                          "version_num": 1, "created_at": _iso(1)}],
                            "batches": {"project_id": P1,
                                        "params": {"source": "ingest", "file": f"tv:{TV}"}}})
    c.rows["versions"].append({"id": "v2", "item_id": "i2", "title": title, "body": body,
                               "version_num": 1, "created_at": _iso(1)})


# ══════════════════════════════════════════════════════════════════════
# 1. 补录副本不进对照索引
# ══════════════════════════════════════════════════════════════════════

def test_is_ingest_batch_recognises_dict_and_string_params():
    assert store.is_ingest_batch({"params": {"source": "ingest", "file": "tv:X"}})
    assert store.is_ingest_batch({"params": '{"source": "ingest"}'}), "老行 / 假件给字符串也要认"
    assert not store.is_ingest_batch({"params": {"source": "deskcore"}})
    assert not store.is_ingest_batch({"params": None})
    assert not store.is_ingest_batch({"params": "not json"})
    assert not store.is_ingest_batch(None)


def test_versions_for_linking_leaves_ingest_copies_out_of_the_index():
    c = _client(NOTES)
    _add_ingest_copy(c)
    got = {v["version_id"] for v in store.versions_for_linking(c, P1)}
    assert "v1" in got, "真写的版本照常在索引里"
    assert "v2" not in got, "补录副本不该在对照索引里 —— 它是从 TV 复制来的, 不是任何笔记的来源"
    # 反证: 同一条 item 不是补录来的就得在
    c.rows["items"][-1]["batches"] = {"project_id": P1, "params": {"source": "deskcore"}}
    assert "v2" in {v["version_id"] for v in store.versions_for_linking(c, P1)}


def test_a_sibling_note_with_the_same_body_does_not_link_to_the_ingest_copy():
    """SPX 的父记录继承让同一段正文出现在多条笔记上: n4 与补录副本 v2 正文相同。
    修之前 n4 → body_exact → v2 → --write-tv 把 v2 写成 n4 的来源(因果倒置)。"""
    sibling = _note("n4", "继承了同一段正文的兄弟记录", BODY_NEW)
    c = _client([sibling])
    _add_ingest_copy(c)
    out = core.tv_sync(c, TV, write_tv=True)
    link = {l["note_id"]: l for l in c.rows["tv_note_links"]}["n4"]
    assert out["counts"]["body_exact"] == 0
    assert link["version_id"] != "v2", "兄弟记录不能对到补录副本上"
    sent_for_n4 = [l for call in c.tv_calls["backfill"] for l in call if l["note_id"] == "n4"]
    assert all(l["version_id"] != "v2" for l in sent_for_n4), "--write-tv 不能把补录副本写回 TV 当来源"


# ══════════════════════════════════════════════════════════════════════
# 2. 补录分块抛错: 已对上的对照照样写, 没处理的记原因, 明晚重来
# ══════════════════════════════════════════════════════════════════════

def test_a_failing_ingest_chunk_does_not_drop_the_links_of_matched_notes(monkeypatch):
    c = _client(NOTES)

    def busy(*a, **k):
        raise core.IngestBusy("项目上另一个写操作正在跑")
    monkeypatch.setattr(core, "ingest_published", busy)

    out = core.tv_sync(c, TV)
    links = {l["note_id"]: l for l in c.rows["tv_note_links"]}
    assert links["n1"]["match_kind"] == "body_exact" and links["n1"]["version_id"] == "v1", \
        "一次锁冲突不能让当晚真对上的笔记一行对照都没有"
    assert links["n3"]["match_kind"] == "tv_lineage"
    assert links["n2"]["match_kind"] == "unmatched" and links["n2"]["version_id"] is None
    assert "补录分块失败" in links["n2"]["candidates"][0]["note"]
    assert out["ingest"]["chunk_error"].startswith("IngestBusy")
    assert out["ingest"]["skipped_after_failure"] == 1 and out["ingest"]["minted"] == 0
    assert out["links_written"] == 3


def test_the_unprocessed_note_is_retried_once_the_lock_clears(monkeypatch):
    c = _client(NOTES)
    real = core.ingest_published
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise core.IngestBusy("busy")
        return real(*a, **k)
    monkeypatch.setattr(core, "ingest_published", flaky)

    core.tv_sync(c, TV)                                    # 第一晚: 块失败
    again = core.tv_sync(c, TV)                            # 第二晚: 锁好了
    links = {l["note_id"]: l for l in c.rows["tv_note_links"]}
    assert again["counts"]["already_linked"] == 1, "n1 已对上跳过; n2 / n3 要再看"
    assert links["n2"]["match_kind"] == "ingested" and links["n2"]["version_id"]
    assert again["ingest"]["chunk_error"] is None and again["ingest"]["minted"] == 1


# ══════════════════════════════════════════════════════════════════════
# 3. --all: 一个项目炸了, 后面的照跑, 退出码非零
# ══════════════════════════════════════════════════════════════════════

def test_tv_sync_all_continues_past_a_project_that_raises(monkeypatch, capsys):
    c = _client(NOTES)
    c.rows["tv_project_map"].append({"tv_project_id": "AAA_phase1", "project_id": P1,
                                     "ingest_target": True, "note": ""})
    ran: list[str] = []
    real = core.tv_sync

    def one_bad(sb, tv, **kw):
        ran.append(tv)
        if tv == "AAA_phase1":
            raise core.IngestBusy("AAA 的锁没等到")
        return real(sb, tv, **kw)
    monkeypatch.setattr(core, "tv_sync", one_bad)
    monkeypatch.setattr(core, "sb", lambda: c)

    rc = cli.main(["tv-sync", "--all"])
    out = capsys.readouterr().out
    assert ran == ["AAA_phase1", TV], "排在炸掉的项目后面的也要跑"
    assert rc == 1, "有项目失败, 退出码要非零, 别假报绿"
    assert "AAA_phase1: ❌" in out and "IngestBusy" in out
    assert f"{TV}:" in out and "对上" in out, "好的项目照常出报表"


# ══════════════════════════════════════════════════════════════════════
# 4. 清单 / 核对 / 审核页把"真写"和"补录副本"分开说 (TV 审计 B-19)
# ══════════════════════════════════════════════════════════════════════

def test_legacy_version_pages_marks_ingest_copies_but_keeps_them():
    """回填照常收补录副本(它们也要指纹, 不然正文相同的 TV 笔记隔天又补一份), 只是行里标出来。"""
    c = _client(NOTES)
    _add_ingest_copy(c)
    rows = {r["version_id"]: r for pg in store.legacy_version_pages(c, P1) for r in pg}
    assert "v2" in rows, "副本不能从回填里消失"
    assert rows["v2"]["ingest"] is True and rows["v1"]["ingest"] is False


def test_backfill_gap_reports_how_many_eligible_are_ingest_copies():
    c = _client(NOTES)
    _add_ingest_copy(c)
    c.rows.setdefault("draft_fingerprints", [])
    gap = core.backfill_gap(c, P1)
    assert gap["eligible"] >= 2 and gap["ingest_copies"] == 1, gap
    # 反证: 不是补录的同一条 → 0
    c.rows["items"][-1]["batches"] = {"project_id": P1, "params": {"source": "deskcore"}}
    assert core.backfill_gap(c, P1)["ingest_copies"] == 0


def test_list_projects_reports_ingest_copies_next_to_fingerprint_count():
    """fingerprint_count 是查重基线(含副本); ingest_copies 说其中多少是副本 —— 模型别把
    "1,800 条积累"读成"写过 1,800 篇"。"""
    c = _client(NOTES)
    _add_ingest_copy(c)
    c.rows.setdefault("projects", [])
    if not any(p.get("id") == P1 for p in c.rows["projects"]):
        c.rows["projects"].append({"id": P1, "name": "p", "brand": "", "owner_id": ME})
    c.rpc_impl["deskcore_fingerprint_counts"] = lambda a: [{"project_id": P1, "n": 7}]
    got = {p["project_id"]: p for p in core.list_projects(c, user_id=ME)}
    assert got[P1]["fingerprint_count"] == 7 and got[P1]["ingest_copies"] == 1, got[P1]
    # 反证: 没有补录批次 → 0, 且 fingerprint_count 不变
    c.rows["batches"] = [b for b in c.rows["batches"] if b["id"] != "b2"]
    got2 = {p["project_id"]: p for p in core.list_projects(c, user_id=ME)}
    assert got2[P1]["ingest_copies"] == 0 and got2[P1]["fingerprint_count"] == 7


def test_review_page_labels_ingest_batches_with_the_one_predicate():
    """app.py 是 Streamlit 脚本, import 就会跑页面(session_state 炸), 所以按源码钉:
    批次标签和审核页都得经 ``_is_ingest_batch`` → ``deskcore.store.is_ingest_batch``, 不许另抄一份口径。"""
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "_is_ingest_batch" in fns, "app.py 缺 _is_ingest_batch"
    helper = ast.get_source_segment(src, fns["_is_ingest_batch"])
    assert "from deskcore.store import is_ingest_batch" in helper, "口径只能来自 deskcore.store, 不许在 app.py 另抄"
    label = ast.get_source_segment(src, fns["_format_batch_label"])
    assert "_is_ingest_batch(batch)" in label and "补录副本" in label, "批次标签没标补录副本"
    review = ast.get_source_segment(src, fns["page_review"])
    assert "_is_ingest_batch(selected_batch)" in review and "不需要审核" in review, "审核页没对补录批次说明"
    # 真跑一遍标签函数: 把它的源码单独编译, 不 import app
    ns: dict = {"datetime": __import__("datetime").datetime, "timezone": __import__("datetime").timezone,
                "_BEIJING_TZ": __import__("datetime").timezone(__import__("datetime").timedelta(hours=8))}
    exec(helper + "\n" + label, ns)
    assert ns["_format_batch_label"]({"tactic": "通用", "created_at": "2026-10-08T00:00:00+00:00",
                                      "params": {"source": "ingest", "file": "tv:X"}}, "P").startswith("补录副本 · P")
    assert not ns["_format_batch_label"]({"tactic": "通用", "created_at": "2026-10-08T00:00:00+00:00",
                                          "params": {"source": "deskcore"}}, "P").startswith("补录副本")


# ══════════════════════════════════════════════════════════════════════
# 5. codex review on #93: 补录块炸了退出码非零; 副本计数自己翻页 (不走 1000 行静默钳位的裸 in_)
# ══════════════════════════════════════════════════════════════════════

def test_cli_tv_sync_exits_nonzero_when_an_ingest_chunk_fails(monkeypatch, capsys):
    """块失败后对照照写、没处理的记 unmatched 明晚重来 —— 但这不是一次健康的同步: Railway 的
    cron 日志里退出码得非零, 否则它和正常跑长得一样 (codex review on #93)。"""
    c = _client(NOTES)
    monkeypatch.setattr(core, "sb", lambda: c)

    def busy(*a, **k):
        raise core.IngestBusy("项目上另一个写操作正在跑")
    monkeypatch.setattr(core, "ingest_published", busy)

    assert cli.main(["tv-sync", "--tv-project", TV]) == 1
    out = capsys.readouterr().out
    assert "补录有一块炸了" in out and "IngestBusy" in out and "明晚重来" in out
    assert len(c.rows["tv_note_links"]) == 3, "退出码非零不等于对照不写: 对上的三行还是要落"


def _ingest_rows(n_items: int, *, batch="b_big", project=P1):
    return ({"id": batch, "project_id": project, "user_id": ME, "params": {"source": "ingest", "file": "tv:x"}},
            [{"id": f"{batch}_i{i:05d}", "batch_id": batch, "user_id": ME, "status": "pending"} for i in range(n_items)])


def test_ingest_copy_counts_pages_items_itself_when_the_rpc_is_missing():
    """RPC 没部署时自己翻页数, 不走 ``db.get_batch_item_counts`` 的裸 ``in_`` 退路 —— 那条被服务端
    db-max-rows 在 1000 行静默钳掉, 而要数的正是上千条副本的项目 (codex review on #93)。
    反证: 假库 max_rows=1000 模拟钳位, 1,200 条副本要数出 1,200 而不是 1,000。"""
    b, items = _ingest_rows(1200)
    other_b, other_items = _ingest_rows(5, batch="b_small", project="P_other")
    c = FakeClient(rows={"batches": [b, other_b], "items": items + other_items}, max_rows=1000)
    got = store.ingest_copy_counts(c, [P1, "P_other"])
    assert got == {P1: 1200, "P_other": 5}, got
    assert [n for n, _ in c.rpc_calls] == ["batch_item_counts"], "先试 RPC, 没有再翻页"


def test_ingest_copy_counts_prefers_the_rpc_when_it_exists():
    b, items = _ingest_rows(3)
    c = FakeClient(rows={"batches": [b], "items": items},
                   rpc_impl={"batch_item_counts": lambda a: [{"batch_id": "b_big", "total": 3}]})
    assert store.ingest_copy_counts(c, [P1]) == {P1: 3}


def test_ingest_copy_counts_does_not_fall_back_to_the_clamped_helper():
    """按【调用】看, 不按文本看: docstring 里提到那个退路是允许的, 调它不行。"""
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(store.ingest_copy_counts)))
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    called |= {n.func.id for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "get_batch_item_counts" not in called, "裸 in_ 退路回来了: 1000 行以上的副本会被静默钳掉"
    assert "_paged" in called and "in_" in called and "rpc" in called
