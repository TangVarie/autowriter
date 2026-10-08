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
