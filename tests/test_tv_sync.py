"""core.tv_sync + CLI tv-sync / tv-map —— 编排层, 用假库走整条链。

单测 tvlink 证明判得对; 这里盯的是接线: 对照表怎么读、TV 笔记怎么翻页、对上的
怎么写 tv_note_links、对不上的怎么进 ingest_published 并把 version_id 记回来、
dry-run 一行不写、第二次跑增量、--write-tv 才碰 TV、没有补录目标时只记 unmatched。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from deskcore import cli, core, store
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
P1 = "aaaaaaaa-0000-0000-0000-000000000001"
TV = "SPX_phase1"
T0 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
BODY_A = ("投到第六十份的时候我开始怀疑是不是邮箱坏了, 后来发现坏的是简历第一行。"
          "改完之后一周里来了三个面试, 才知道之前的六十份全都倒在第一行上。")
BODY_NEW = ("这一篇写作台里根本没有: 上周发的, 当时没入库。"
            "指纹库不知道它, 下一批查重看不见它, 所以要从 TV 补回来。" * 2)


def _iso(days):
    return (T0 + timedelta(days=days)).isoformat()


def _client(notes, *, with_target=True, links=None):
    c = FakeClient(rows={
        "projects": [{"id": P1, "name": "sportsix", "brand": "sportsix", "owner_id": ME,
                      "calibration_notes": "", "tactics": "[]", "custom_roles": []}],
        "tv_project_map": [{"tv_project_id": TV, "project_id": P1,
                            "ingest_target": with_target, "note": ""}],
        "batches": [{"id": "b1", "project_id": P1, "user_id": ME}],
        "items": [{"id": "i1", "batch_id": "b1", "best_version_id": "v1",
                   "created_at": _iso(0), "user_id": ME, "status": "pending",
                   # 假库没有 embedded join, 手动嵌(同 test_e2e)
                   "versions": [{"id": "v1", "title": "秋招投了六十份", "body": BODY_A,
                                 "version_num": 1, "created_at": _iso(0)}],
                   "batches": {"project_id": P1}}],
        "versions": [{"id": "v1", "item_id": "i1", "title": "秋招投了六十份", "body": BODY_A,
                      "version_num": 1, "created_at": _iso(0)}],
        "tv_note_links": list(links or []),
        "draft_fingerprints": [],
    })
    calls = {"backfill": []}

    def _tv_notes(a):
        rows = [n for n in notes if a.get("_after") is None or n["note_id"] > a["_after"]]
        return rows[: a["_limit"]]

    def _backfill(a):
        calls["backfill"].append(a["_links"])
        return len(a["_links"])
    c.rpc_impl = {"deskcore_tv_notes": _tv_notes, "deskcore_tv_backfill_lineage": _backfill}
    c.tv_calls = calls
    return c


def _note(nid, title, body, days=2, **extra):
    return {"note_id": nid, "raw_content": f"【标题】{title} 【正文】{body} #跑步 #能量胶",
            "publish_time": _iso(days), "tier": "趴", "created_at": _iso(days), **extra}


NOTES = [
    _note("n1", "秋招投了六十份简历没回音", BODY_A),                 # 开头相同 → v1
    _note("n2", "上周发的那篇", BODY_NEW),                             # 库里没有 → 补录
    _note("n3", "TV 自己带了 lineage", "随便什么正文, 反正 TV 已经知道它是哪一版。" * 2,
          source_autowriter_version_id="vvvvvvvv-0000-0000-0000-00000000000v"),
]


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)


def test_dry_run_computes_everything_and_writes_nothing():
    c = _client(NOTES)
    out = core.tv_sync(c, TV, dry_run=True)
    assert out["counts"] == {"already_linked": 0, "tv_lineage": 1, "body_exact": 1,
                             "title_exact": 0, "fuzzy": 0, "ambiguous": 0, "unmatched": 1,
                             "no_body": 0}
    assert out["ingest"]["to_write"] == 1 and out["ingest"]["minted"] == 0
    assert "会补录 1" in out["note"]
    assert c.rows["tv_note_links"] == [] and c.rows["draft_fingerprints"] == []
    assert len(c.rows["versions"]) == 1 and not c.tv_calls["backfill"]


def test_real_run_links_matches_ingests_the_rest_and_is_incremental():
    c = _client(NOTES)
    out = core.tv_sync(c, TV)
    links = {l["note_id"]: l for l in c.rows["tv_note_links"]}
    assert links["n1"]["match_kind"] == "body_exact" and links["n1"]["version_id"] == "v1"
    assert links["n1"]["item_id"] == "i1" and links["n1"]["lag_days"] == 2
    assert links["n3"]["match_kind"] == "tv_lineage" and links["n3"]["version_id"] is None
    assert links["n2"]["match_kind"] == "ingested" and links["n2"]["version_id"]
    # 补录真的进了指纹库, 且剥了话题标签
    assert out["ingest"]["minted"] == 1 and out["ingest"]["fingerprinted"] == 1
    assert len(c.rows["draft_fingerprints"]) == 1
    minted = next(v for v in c.rows["versions"] if v["id"] == links["n2"]["version_id"])
    assert "#跑步" not in minted["body"] and minted["title"] == "上周发的那篇"
    prov = next(b for b in c.rows["batches"] if b["id"] == out["ingest"]["batch_id"])["params"]
    assert prov == {"source": "ingest", "file": f"tv:{TV}"}
    assert out["links_written"] == 3 and out["tv_backfilled"] == 0, "没 --write-tv 不碰 TV"
    assert not c.tv_calls["backfill"]

    # 第二次: 两条已对上跳过, n3(TV 的 id 认不出, 没 version_id)会再看一眼但不补
    again = core.tv_sync(c, TV)
    assert again["counts"]["already_linked"] == 2 and again["ingest"] is None
    assert len(c.rows["draft_fingerprints"]) == 1 and len(c.rows["versions"]) == 2


def test_write_tv_backfills_only_rows_with_a_version_and_marks_them_synced():
    # 没有补录目标 → n2 留在 unmatched、没有 version_id → 不回填
    c = _client(NOTES, with_target=False)
    out = core.tv_sync(c, TV, write_tv=True)
    assert out["tv_backfilled"] == 1
    sent = {l["note_id"] for l in c.tv_calls["backfill"][0]}
    assert sent == {"n1"}, "只有带(我们认得的)version_id 的才回填"
    links = {l["note_id"]: l for l in c.rows["tv_note_links"]}
    assert all(links[n].get("synced_to_tv_at") for n in sent)
    assert links["n2"]["match_kind"] == "unmatched" and not links["n2"].get("synced_to_tv_at")


def test_a_previously_linked_but_unsynced_row_is_backfilled_on_the_next_write_tv():
    """第一天没开 --write-tv, 第二天开了: 昨天对上的也要回填, 不只是今天新对上的。
    昨天补录的(n2, ingested)照样不写 —— 见下一个用例。"""
    c = _client(NOTES)
    core.tv_sync(c, TV)                              # 对上了, 没回填
    out = core.tv_sync(c, TV, write_tv=True)         # 全部 already_linked
    assert out["counts"]["already_linked"] == 2
    sent = {l["note_id"] for l in c.tv_calls["backfill"][0]}
    assert sent == {"n1"} and out["tv_backfilled"] == 1


def test_ingested_links_are_never_written_back_to_tv():
    """TV 2026-09-18 核对时的条件: ingested 的版本是我们从这条笔记复制进写作台的,
    写回 source_autowriter_* 等于说"笔记来源于版本 X"而 X 来源于笔记 —— 因果倒置,
    TV 的模型对比视图会多出上千行没信息量的 deskcore。真对上的(n1)照写; 补录的
    (n2)有 version_id 也不写、不标 synced, 无论是本次补的还是昨天补的。"""
    c = _client(NOTES)
    out = core.tv_sync(c, TV, write_tv=True)          # n2 本次补录
    links = {l["note_id"]: l for l in c.rows["tv_note_links"]}
    assert links["n2"]["match_kind"] == "ingested" and links["n2"]["version_id"]
    assert out["tv_backfilled"] == 1
    assert {l["note_id"] for l in c.tv_calls["backfill"][0]} == {"n1"}
    assert links["n1"].get("synced_to_tv_at") and not links["n2"].get("synced_to_tv_at")

    again = core.tv_sync(c, TV, write_tv=True)        # n2 现在是"昨天补的、没 synced"
    assert again["tv_backfilled"] == 0 and len(c.tv_calls["backfill"]) == 1, \
        "没 synced 的 ingested 行也不能在第二天被当成漏网之鱼补写"
    assert not {l["note_id"]: l for l in c.rows["tv_note_links"]}["n2"].get("synced_to_tv_at")


def test_tv_lineage_only_carries_a_version_id_we_actually_have():
    """tv_note_links.version_id 有外键: TV 带的 id 若不在我们库里, 硬写会让整个
    upsert 分块失败。认得出的照记, 认不出的留在 candidates 里给人看。"""
    c = _client([_note("n3", "TV 带了陈旧 lineage", "x" * 40,
                       source_autowriter_version_id="vvvvvvvv-0000-0000-0000-00000000000v"),
                 _note("n5", "TV 带了真 lineage", "y" * 40, source_autowriter_version_id="v1")])
    out = core.tv_sync(c, TV)
    assert out["counts"]["tv_lineage"] == 2
    links = {l["note_id"]: l for l in c.rows["tv_note_links"]}
    assert links["n3"]["version_id"] is None and links["n3"]["candidates"][0]["tv_version_id"].startswith("vvvv")
    assert links["n5"]["version_id"] == "v1" and links["n5"]["item_id"] == "i1"


def test_without_an_ingest_target_unmatched_is_recorded_not_ingested():
    c = _client(NOTES, with_target=False)
    out = core.tv_sync(c, TV)
    assert out["ingest"] is None and "没有补录目标" in out["note"]
    links = {l["note_id"]: l for l in c.rows["tv_note_links"]}
    assert links["n2"]["match_kind"] == "unmatched" and links["n2"]["version_id"] is None
    assert c.rows["draft_fingerprints"] == []


def test_ambiguous_keeps_candidates_and_does_not_guess():
    c = _client([_note("n9", "同一个标题两版", "跟两版都不像的正文, 只有标题对得上, 凑够二十个字。")])
    c.rows["items"].append({"id": "i2", "batch_id": "b1", "best_version_id": "v2",
                            "created_at": _iso(0), "user_id": ME, "status": "pending",
                            "versions": [{"id": "v2", "title": "同一个标题两版", "body": BODY_A,
                                          "version_num": 1, "created_at": _iso(0)}],
                            "batches": {"project_id": P1}})
    c.rows["items"][0]["versions"][0]["title"] = "同一个标题两版"
    out = core.tv_sync(c, TV)
    assert out["counts"]["ambiguous"] == 1 and out["ambiguous_samples"]
    link = c.rows["tv_note_links"][0]
    assert link["match_kind"] == "ambiguous" and link["version_id"] is None
    assert {x["version_id"] for x in link["candidates"]} == {"v1", "v2"}


def test_tv_resolve_ingest_turns_an_ambiguous_note_into_an_ingested_link(capsys, monkeypatch):
    """人看完「分不出」说"对不上": 正文补进指纹库, 对照改成 ingested 带 version_id,
    原 candidates 保留并加一条人工判定; 第二天 tv_sync 把它当已对上跳过; 已对上的
    / 不存在的 / 没正文的都不判。2026-09-18 途鸽那条(两版同标题的评论稿)的出口。"""
    c = _client([_note("n9", "同一个标题两版", "跟两版都不像的正文, 只有标题对得上, 凑够二十个字。")])
    c.rows["items"].append({"id": "i2", "batch_id": "b1", "best_version_id": "v2",
                            "created_at": _iso(0), "user_id": ME, "status": "pending",
                            "versions": [{"id": "v2", "title": "同一个标题两版", "body": BODY_A,
                                          "version_num": 1, "created_at": _iso(0)}],
                            "batches": {"project_id": P1}})
    c.rows["items"][0]["versions"][0]["title"] = "同一个标题两版"
    core.tv_sync(c, TV)
    assert c.rows["tv_note_links"][0]["match_kind"] == "ambiguous"
    n_versions = len(c.rows["versions"])

    out = core.tv_resolve(c, "n9", ingest=True)
    assert out["resolved"] and out["version_id"]
    link = c.rows["tv_note_links"][0]
    assert link["match_kind"] == "ingested" and link["version_id"] == out["version_id"]
    assert link["candidates"][0]["note"].startswith("人工判定")
    assert {x.get("version_id") for x in link["candidates"][1:]} == {"v1", "v2"}, "原候选保留"
    assert len(c.rows["versions"]) == n_versions + 1
    assert len(c.rows["draft_fingerprints"]) == 1

    again = core.tv_sync(c, TV)
    assert again["counts"]["already_linked"] == 1 and again["counts"]["ambiguous"] == 0

    with pytest.raises(ValueError, match="已经对上了"):
        core.tv_resolve(c, "n9", ingest=True)
    with pytest.raises(ValueError, match="没有"):
        core.tv_resolve(c, "nope", ingest=True)
    with pytest.raises(ValueError, match="只有 --ingest"):
        core.tv_resolve(c, "n9", ingest=False)

    monkeypatch.setattr(core, "sb", lambda: c)
    assert cli.main(["tv-resolve", "--note", "n9", "--ingest"]) == 2
    assert "已经对上了" in capsys.readouterr().out


def test_tv_notes_are_paged_by_keyset():
    notes = [_note(f"n{i:03d}", f"第 {i} 篇", f"第 {i} 篇的正文各不相同, 长度都够二十个字以上。" * 2)
             for i in range(7)]
    c = _client(notes)
    got = store.tv_notes(c, TV, page=3)
    assert [n["note_id"] for n in got] == [n["note_id"] for n in notes]
    assert [a["_after"] for _, a in c.rpc_calls if _ == "deskcore_tv_notes"] == [None, "n002", "n005"]


def test_unknown_tv_project_is_a_clear_error():
    with pytest.raises(ValueError, match="tv_project_map"):
        core.tv_sync(_client([]), "NOPE_phase9")


def test_fingerprint_failure_after_mint_is_reported_not_raised(monkeypatch):
    c = _client(NOTES)
    monkeypatch.setattr(store, "write_fingerprints",
                        lambda sb, rows: (_ for _ in ()).throw(RuntimeError("502")))
    out = core.tv_sync(c, TV)
    assert out["ingest"]["fingerprint_error"] and out["ingest"]["minted"] == 1


# ── CLI ───────────────────────────────────────────────────────────────

@pytest.fixture
def fake(monkeypatch):
    c = _client(NOTES)
    monkeypatch.setattr(core, "sb", lambda: c)
    return c


def test_cli_tv_sync_dry_run_then_real(fake, capsys):
    assert cli.main(["tv-sync", "--tv-project", TV, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out and "会补录 1" in out
    assert fake.rows["tv_note_links"] == []
    assert cli.main(["tv-sync", "--all"]) == 0
    out = capsys.readouterr().out
    assert "补录: 收到 1" in out and "建身份 1" in out
    assert len(fake.rows["tv_note_links"]) == 3


def test_cli_tv_sync_points_to_backfill_when_fingerprints_fail(fake, capsys, monkeypatch):
    monkeypatch.setattr(store, "write_fingerprints",
                        lambda sb, rows: (_ for _ in ()).throw(RuntimeError("502")))
    assert cli.main(["tv-sync", "--tv-project", TV]) == 1
    out = capsys.readouterr().out
    assert "backfill --project" in out and "不要**重跑" in out


def test_cli_tv_map_add_and_list(fake, capsys):
    assert cli.main(["tv-map", "add", "--tv-project", "LNKT_phase1", "--project", P1,
                     "--note", "雷诺考特"]) == 0
    out = capsys.readouterr().out
    assert "LNKT_phase1" in out and "SPX_phase1" in out and "补录目标" in out
    assert cli.main(["tv-map", "add"]) == 2
    assert cli.main(["tv-sync"]) == 2


# ── codex review #81 (2026-09-17, 第二轮) ─────────────────────────────

def test_bodyless_tv_note_is_recorded_not_ingested():
    """没正文的笔记补进去等于建一个没有开头哈希、没有四字串的身份: 对查重隐形,
    却被报成 ingested。记成 unmatched + 原因, 不补。"""
    c = _client([{"note_id": "n7", "raw_content": "【标题】只有标题", "publish_time": _iso(1),
                  "tier": "趴", "created_at": _iso(1)}])
    out = core.tv_sync(c, TV)
    assert out["counts"]["no_body"] == 1 and out["counts"]["unmatched"] == 0
    assert out["ingest"] is None and "没正文 1" in out["note"]
    link = c.rows["tv_note_links"][0]
    assert link["match_kind"] == "unmatched" and link["version_id"] is None
    assert "没有正文" in link["candidates"][0]["note"]
    assert len(c.rows["versions"]) == 1   # 没补


def test_note_text_does_not_claim_a_missing_target_when_nothing_needs_ingesting():
    c = _client([_note("n1", "秋招投了六十份简历没回音", BODY_A)])      # 全对上, 没有要补的
    out = core.tv_sync(c, TV)
    assert out["ingest"] is None
    assert "没有补录目标" not in out["note"] and "没有要补录的" in out["note"]


def test_tv_lineage_pointing_at_an_older_version_is_resolved_against_all_versions():
    """TV 带的 version_id 可能指向某个 item 的旧版(不是 best); 对照索引里只有 best
    那一版, 得再按全部版本反查, 不能标成"不在库里"。"""
    c = _client([_note("n8", "TV 指向旧版", "z" * 40, source_autowriter_version_id="v0")])
    # i1 的旧版 v0(best 是 v1); items_for_versions 走 versions → items!inner → batches!inner
    c.rows["versions"].append({"id": "v0", "item_id": "i1", "title": "旧版", "body": "old" * 20,
                               "version_num": 0, "created_at": _iso(-3),
                               "items": {"id": "i1", "status": "pending", "decision_source": None,
                                         "batch_id": "b1", "batches": {"project_id": P1}}})
    out = core.tv_sync(c, TV)
    assert out["counts"]["tv_lineage"] == 1
    link = c.rows["tv_note_links"][0]
    assert link["version_id"] == "v0" and link["item_id"] == "i1" and link["project_id"] == P1


def test_cli_orders_recovery_when_both_ingest_stages_fail(fake, capsys, monkeypatch):
    real_mint = store.mint_draft_identity

    def _partial(sb, project_id, user_id, tactic, drafts, **kw):
        out = real_mint(sb, project_id, user_id, tactic, drafts[:1], **kw)
        out["error"] = "第 2 行身份没建成"
        return out
    monkeypatch.setattr(store, "mint_draft_identity", _partial)
    monkeypatch.setattr(store, "write_fingerprints",
                        lambda sb, rows: (_ for _ in ()).throw(RuntimeError("502")))
    fake.rpc_impl["deskcore_tv_notes"] = lambda a: [] if a.get("_after") else [
        _note("m1", "第一篇", "第一篇没入库的正文各不相同, 长度够二十个字以上。" * 2),
        _note("m2", "第二篇", "第二篇没入库的正文也各不相同, 长度够二十个字以上。" * 2)]
    assert cli.main(["tv-sync", "--tv-project", TV]) == 1
    out = capsys.readouterr().out
    assert "两种半途失败同时发生" in out
    assert out.index("**先**跑 `backfill") < out.index("**然后**再跑一次 tv-sync")
    assert "重跑 tv-sync 即可" not in out, "不许再给一句相反的指令"


def test_hundreds_of_unmatched_notes_are_ingested_in_chunks_with_one_lock_each(monkeypatch):
    """sportsix 一次要补 461 条: 分几次调 ingest_published(每次各拿一次锁), 报表合并,
    written 的下标换算回整体, 每条笔记都拿到自己的 version_id。"""
    notes = [_note(f"n{i:03d}", f"第 {i} 篇", f"第 {i} 篇没入库的正文各不相同, 长度都够二十个字以上。" * 3)
             for i in range(250)]
    c = _client(notes)
    seen = []
    real = core.ingest_published

    def _spy(client, project_id, entries, **kw):
        seen.append(len(entries))
        return real(client, project_id, entries, **kw)
    monkeypatch.setattr(core, "ingest_published", _spy)
    out = core.tv_sync(c, TV)
    assert seen == [100, 100, 50]
    assert out["ingest"]["minted"] == 250 and out["ingest"]["fingerprinted"] == 250
    assert len(out["ingest"]["batch_ids"]) == 3
    links = {l["note_id"]: l for l in c.rows["tv_note_links"]}
    assert all(links[n["note_id"]]["match_kind"] == "ingested" and links[n["note_id"]]["version_id"]
               for n in notes)
    assert len({links[n["note_id"]]["version_id"] for n in notes}) == 250, "每条各自的 version_id"


def test_fingerprint_failure_mid_way_counts_the_unprocessed_rest_and_says_rerun(monkeypatch, capsys):
    """codex #82 P1: 第二块写指纹失败后, 后面的块一条都没处理, 却被静默记成 unmatched、
    不进 received/to_write, CLI 只说 backfill —— 照做的话最后那几十条永远没人补。"""
    notes = [_note(f"n{i:03d}", f"第 {i} 篇", f"第 {i} 篇没入库的正文各不相同, 长度都够二十个字以上。" * 3)
             for i in range(250)]
    c = _client(notes)
    n = {"calls": 0}
    real_write = store.write_fingerprints

    def _write(sb, rows):
        n["calls"] += 1
        if n["calls"] == 2:
            raise RuntimeError("502")
        return real_write(sb, rows)
    monkeypatch.setattr(store, "write_fingerprints", _write)
    out = core.tv_sync(c, TV)
    ing = out["ingest"]
    assert ing["received"] == 250 and ing["minted"] == 200 and ing["fingerprinted"] == 100
    assert ing["skipped_after_failure"] == 50
    links = {l["note_id"]: l for l in c.rows["tv_note_links"]}
    unprocessed = [l for l in links.values() if l["match_kind"] == "unmatched"]
    assert len(unprocessed) == 50 and all("再跑一次 tv-sync" in l["candidates"][0]["note"] for l in unprocessed)
    # CLI 的话要把两步都说出来
    monkeypatch.setattr(core, "sb", lambda: _client(notes))
    n["calls"] = 0
    assert cli.main(["tv-sync", "--tv-project", TV]) == 1
    text = capsys.readouterr().out
    assert "先**跑 `backfill" in text and "另有 50 条因此没处理到" in text and "再**跑一次 tv-sync" in text


def test_duplicate_notes_across_a_chunk_boundary_count_once_in_dry_run_and_real(monkeypatch):
    """codex #82 P2: 同正文的两条笔记落在两块里, dry-run 数两次、真跑第二份被判"已有
    指纹" —— 两边 to_write 对不上。先整批去重再分块, 重复的那条链到第一条的版本。"""
    body = "这一篇发了两次, 正文一模一样, 长度够二十个字以上。" * 3
    notes = [_note(f"n{i:03d}", f"第 {i} 篇", f"第 {i} 篇没入库的正文各不相同, 长度都够二十个字以上。" * 3)
             for i in range(120)]
    notes[5] = _note("n005", "第一次发", body)
    notes[115] = _note("n115", "第二次发", body)
    dry = core.tv_sync(_client(notes), TV, dry_run=True)
    c = _client(notes)
    real = core.tv_sync(c, TV)
    assert dry["ingest"]["to_write"] == real["ingest"]["to_write"] == 119
    assert dry["ingest"]["skipped_duplicate_in_sheet"] == real["ingest"]["skipped_duplicate_in_sheet"] == 1
    assert real["ingest"]["minted"] == 119
    links = {l["note_id"]: l for l in c.rows["tv_note_links"]}
    assert links["n115"]["match_kind"] == "ingested"
    assert links["n115"]["version_id"] == links["n005"]["version_id"]
