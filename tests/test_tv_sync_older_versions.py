"""tv-sync: 定稿那一版对不上的帖, 再按 item 对存过的旧版(2026-11-26)。

11-26 生产库(只读): 11-09 以来四个在用的项目 255 篇对不上的帖和写作台里任何文字的四字串重合都不到 15%; 写手在对话里改好几轮、
把最后一轮贴进飞书。存过档的那几轮是同一个 item 的几版, 对照索引只有 best —— 发出去的是中间那一版时就对不上, 被记成「别处写的」。
这里钉住接线(匹配本身在 tests/test_tvlink_by_item.py):
  1. 主索引照旧先对; 只有对不上 / 只是标题一样的才对旧版; 对上的照旧记 body_exact / title_exact / fuzzy / edited, 带 item_id,
     candidates 前面一句(不钉死对照), 单独报 older_version, counts 的键不变;
  2. 之后各步照常认它: W-40 把帖的文字记在那一版下、改稿差异按那一版算、--write-tv 写回那一版;
  3. 不认的: 发了之后才存的旧版、和别的 item 的旧版分不出的(照旧补录, 分数取两遍里高的)、补录副本 / TV 同步来的 item 的旧版;
  4. 回流闸量「发布前写作台里的文字」、--rescore-misses 补分数, 旧版都算, 和每晚记的同一个口径;
  5. 指纹自愈: 旧版下有 W-40 记的帖的文字, 定稿那一版没有行时照样补。
文字全是编的。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import config
from deskcore import core, store, tvlink
from tests import test_tv_sync as T
from tests.test_tv_link_miss_score import FILLER

TV, P1, ME = T.TV, T.P1, T.ME
REWRITE = ("后来这篇整个重写了, 讲的是周末第一次去打网球, 教练让我先对着墙打两百个正手, 打到一半手腕就酸得抬不起来。"
           "回家查了才知道握拍太紧, 第二周换了松一点的握法, 墙打到三百个也不累。")
NEW_TITLE = "重写之后换了标题"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    monkeypatch.setattr(core, "_gate_now", lambda: T.T0 + timedelta(days=10))


def _join(c):
    """假库没有真 join: items → versions / batches、versions → items → batches 按平铺的行重新嵌好。"""
    batches = {b["id"]: b for b in c.rows["batches"]}
    items = {i["id"]: i for i in c.rows["items"]}
    for it in c.rows["items"]:
        b = batches.get(it["batch_id"]) or {}
        it["versions"] = [{k: v.get(k) for k in ("id", "title", "body", "version_num", "created_at", "ai_engine")}
                          for v in c.rows["versions"] if v["item_id"] == it["id"]]
        it["batches"] = {"project_id": b.get("project_id"), "params": b.get("params")}
    for v in c.rows["versions"]:
        it = items.get(v["item_id"])
        if it:
            v["items"] = {"id": it["id"], "status": it.get("status"), "decision_source": None,
                          "batch_id": it["batch_id"], "batches": {"project_id": batches[it["batch_id"]]["project_id"]}}
    return c


def _revise(c, vid, body, *, title=NEW_TITLE, days=1, item="i1"):
    """这个 item 又存了一版(改稿), 成了 best; 原来那几版留在 item 下, 成了旧版。"""
    num = 1 + max(v["version_num"] for v in c.rows["versions"] if v["item_id"] == item)
    c.rows["versions"].append({"id": vid, "item_id": item, "title": title, "body": body, "version_num": num,
                               "created_at": T._iso(days)})
    next(i for i in c.rows["items"] if i["id"] == item)["best_version_id"] = vid
    return _join(c)


def _item(c, item, versions, *, batch="b1", best=None):
    """再放一个 item: ``versions`` 是 [(vid, title, body, days)], 按顺序编 version_num; best 默认最后一版。"""
    c.rows["items"].append({"id": item, "batch_id": batch, "best_version_id": best or versions[-1][0],
                            "created_at": T._iso(versions[0][3]), "user_id": ME, "status": "pending"})
    for n, (vid, title, body, days) in enumerate(versions, 1):
        c.rows["versions"].append({"id": vid, "item_id": item, "title": title, "body": body, "version_num": n,
                                   "created_at": T._iso(days)})
    return _join(c)


def _older_client(notes=T.NOTES, **kw):
    """T._client 的 i1: v1(BODY_A, 第 0 天)成了旧版, best 是第 1 天整篇重写的 v2。"""
    return _revise(T._client(notes, **kw), "v2", REWRITE)


def _links(c) -> dict[str, dict]:
    return {l["note_id"]: l for l in c.rows["tv_note_links"]}


# ── 1. 对上旧版 ─────────────────────────────────────────────────────────────────────────────────

def test_a_post_published_from_an_older_version_links_to_it():
    c = _older_client()
    assert tvlink.match_note(tvlink.Note.from_row(T.NOTES[0]),
                             tvlink.VersionIndex([tvlink.Version.from_row(r) for r in store.versions_for_linking(c, P1)])
                             ).kind == "unmatched", "前提: 只有定稿那一版时对不上"
    out = core.tv_sync(c, TV, write_tv=True)
    assert out["counts"] == {"already_linked": 0, "tv_lineage": 1, "body_exact": 1, "title_exact": 0, "fuzzy": 0,
                             "edited": 0, "ambiguous": 0, "unmatched": 1, "no_body": 0}, "counts 的键不变"
    assert (out["older_version"], out["versions"], out["versions_older"], out["matched"]) == (1, 1, 1, 1)
    assert "其中对上的是稿子存过的旧版 1" in out["note"]
    l = _links(c)["n1"]
    assert (l["match_kind"], l["version_id"], l["item_id"], l["project_id"], l["lag_days"]) == \
        ("body_exact", "v1", "i1", P1, 2)
    head = l["candidates"][0]
    assert head[core.TV_OLDER_KEY] is True and "旧版" in head["note"] and "定稿是 v2" in head["note"]
    assert not core._pinned(l) and not core._PIN_RE.search(head["note"]), "那一句不钉死对照"
    # W-40: 发出去的文字记在 v1 下; 改稿差异按 v1 算; --write-tv 把 v1 / i1 写回 TV
    assert [r["title"] for r in c.rows["draft_fingerprints"] if r["version_id"] == "v1"] == ["秋招投了六十份简历没回音"]
    (d,) = [r for r in c.rows["tv_edit_diffs"] if r["note_id"] == "n1"]
    assert d["version_id"] == "v1" and d["match_kind"] == "body_exact" and d["draft_title"] == "秋招投了六十份"
    sent = [s for call in c.tv_calls["backfill"] for s in call if s["note_id"] == "n1"]
    assert sent == [{"note_id": "n1", "version_id": "v1", "item_id": "i1"}]

    again = core.tv_sync(c, TV)
    assert again["counts"]["already_linked"] == 2 and again["older_version"] == 0
    assert _links(c)["n1"]["version_id"] == "v1"


def test_the_cli_reports_older_versions(monkeypatch, capsys):
    from deskcore import cli
    c = _older_client()
    monkeypatch.setattr(core, "sb", lambda: c)
    assert cli.main(["tv-sync", "--tv-project", TV, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "其中对上的是稿子存过的旧版 1" in out and "另有存过的旧版 1 条" in out
    assert c.rows["tv_note_links"] == []


def test_a_linked_older_version_is_still_rechecked_when_the_post_changes():
    """不是钉死的对照: TV 里改过字照常核(W-34), 还是那一篇就留着, 差异按改过的字重算。"""
    notes = [T._note("n1", "秋招投了六十份简历没回音", T.BODY_A)]
    c = _older_client(notes)
    core.tv_sync(c, TV)
    notes[0] = T._note("n1", "秋招投了六十份简历没回音", T.BODY_A + "最后补一句: 简历第一行写你最硬的那段经历。")
    out = core.tv_sync(c, TV)
    assert out["drift"]["text_changed"] == 1 and out["counts"]["already_linked"] == 1
    l = _links(c)["n1"]
    assert l["version_id"] == "v1" and l["candidates"][0][core.TV_OLDER_KEY]
    assert next(r for r in c.rows["tv_edit_diffs"] if r["note_id"] == "n1")["added_count"] == 1


def test_the_best_version_still_wins_when_it_matches():
    c = _revise(T._client(T.NOTES[:1]), "v2", T.BODY_A + "定稿在最后多写了一句, 说投简历之前先找人看一眼第一行。")
    out = core.tv_sync(c, TV)
    l = _links(c)["n1"]
    assert (l["match_kind"], l["version_id"], out["older_version"]) == ("body_exact", "v2", 0)
    assert not any(isinstance(x, dict) and x.get(core.TV_OLDER_KEY) for x in l["candidates"] or ())


def test_siblings_resolve_to_the_version_the_post_came_from():
    """同一天存了两版(第二版末尾多一句), 定稿又整篇重写: 帖是第一版的字 —— 连第一版, 不是分不出。"""
    c = T._client(T.NOTES[:1])
    _revise(c, "v1b", T.BODY_A + "这一句是第二版才加的, 帖里没有。", title="秋招投了六十份", days=0)
    _revise(c, "v2", REWRITE)
    core.tv_sync(c, TV)
    assert (_links(c)["n1"]["match_kind"], _links(c)["n1"]["version_id"]) == ("body_exact", "v1")


def test_a_tv_lineage_pointing_at_an_older_version_gets_its_lag_and_diff_without_asking_the_database(monkeypatch):
    note = T._note("n8", "TV 指向旧版", T.BODY_A, source_autowriter_version_id="v1")
    c = _older_client([note])
    asked = []
    real = store.items_for_versions
    monkeypatch.setattr(store, "items_for_versions", lambda *a, **k: asked.append(a[2]) or real(*a, **k))
    core.tv_sync(c, TV)
    l = _links(c)["n8"]
    assert (l["match_kind"], l["version_id"], l["item_id"], l["lag_days"]) == ("tv_lineage", "v1", "i1", 2)
    assert not asked, "旧版这晚已经读到了, 不用再反查"
    assert next(r for r in c.rows["tv_edit_diffs"] if r["note_id"] == "n8")["version_id"] == "v1"


# ── 2. 不认的 ───────────────────────────────────────────────────────────────────────────────────

def test_an_older_version_saved_after_publication_is_not_accepted():
    c = T._client(T.NOTES[:1])
    c.rows["versions"][0]["created_at"] = T._iso(5)          # 帖第 2 天发, 这一版第 5 天才存
    _revise(c, "v2", REWRITE, days=6)
    out = core.tv_sync(c, TV)
    l = _links(c)["n1"]
    assert l["match_kind"] == "ingested" and l["version_id"] not in ("v1", "v2") and out["older_version"] == 0
    assert l["score"] == 1.0, "分数还是帖的字有多少在写作台里(发了之后才存的那版也算, 同主索引)"


def test_older_versions_of_two_items_that_cannot_be_told_apart_are_not_guessed():
    c = _older_client(T.NOTES[:1])
    _item(c, "i3", [("v3", "另一个 item 的旧版", T.BODY_A, 0), ("v3b", "也重写了", REWRITE + "第三篇。", 1)])
    older: list = []
    rows = store.versions_for_linking(c, P1, older=older) + older
    m = tvlink.match_note_by_item(tvlink.Note.from_row(T.NOTES[0]),
                                  tvlink.VersionIndex([tvlink.Version.from_row(r) for r in rows]))
    assert m.kind == "ambiguous" and {x["version_id"] for x in m.candidates} == {"v1", "v3"}
    out = core.tv_sync(c, TV)
    l = _links(c)["n1"]
    assert l["match_kind"] == "ingested" and out["older_version"] == 0, "和以前一样补录, 不硬猜"
    assert l["score"] == 1.0, "分数取两遍里高的"


def test_ingest_copies_and_tv_synced_items_have_no_older_versions():
    c = T._client([T._note("n2", "上周发的那篇", T.BODY_NEW)])
    c.rows["batches"] += [{"id": "b2", "project_id": P1, "user_id": ME, "params": {"source": "ingest", "file": f"tv:{TV}"}},
                          {"id": "b3", "project_id": P1, "user_id": ME, "params": '{"source": "truth_vault_sync"}'}]
    _item(c, "i2", [("v2old", "补录的旧版", T.BODY_NEW, 0), ("v2new", "补录的新版", REWRITE, 1)], batch="b2")
    _item(c, "i3", [("v3old", "TV 同步的旧版", T.BODY_NEW, 0), ("v3new", "TV 同步的新版", REWRITE + "又一篇。", 1)],
          batch="b3")
    older: list = []
    best = {r["version_id"] for r in store.versions_for_linking(c, P1, older=older)}
    assert best == {"v1", "v3new"}, "定稿那份照旧: 补录副本不在, TV 同步来的在(这次不动它)"
    assert older == [], "旧版那份两种都不收"
    core.tv_sync(c, TV)
    l = _links(c)["n2"]
    assert l["match_kind"] == "ingested" and l["version_id"] not in ("v2old", "v3old")


def test_versions_for_linking_keeps_its_rows_and_the_older_rows_have_the_same_shape():
    c = _older_client()
    _item(c, "i4", [("v4a", "", "", 0), ("v4b", "第二版有标题", "", 1), ("v4c", "定稿", REWRITE + "第四篇。", 2)])
    plain = store.versions_for_linking(c, P1)
    older: list = []
    assert store.versions_for_linking(c, P1, older=older) == plain
    assert {r["version_id"] for r in plain} == {"v2", "v4c"}
    assert {r["version_id"]: r["item_id"] for r in older} == {"v1": "i1", "v4b": "i4"}, "标题正文都空的那版不要"
    assert all(set(r) == set(plain[0]) for r in older)
    v1 = next(r for r in older if r["version_id"] == "v1")
    assert (v1["title"], v1["body"], v1["created_at"], v1["project_id"]) == ("秋招投了六十份", T.BODY_A, T._iso(0), P1)


def test_an_older_version_is_a_rival_for_a_snapshot_like_any_saved_version():
    """查重底稿(W-38)转正之前要看存过档的版本里有没有一样像的(对手): 旧版也是存过档的。这里那一版是发了之后才存的 ——
    第二遍不连它(连上也算不了差异和「接住」), 可它和底稿分不出, 底稿也不转正, 记 ambiguous 等人看(同 best 当对手时)。"""
    from tests import test_draft_snapshots as S
    c = S._client([S._note("n1", "骑了一百公里", S.BODY_S)], [S._snap("s1", "骑了一百公里", S.BODY_S)])
    c.rows["items"].append({"id": "i9", "batch_id": "b1", "best_version_id": "v9", "created_at": S._iso(5),
                            "user_id": ME, "status": "pending"})
    c.rows["versions"] += [
        {"id": "v9old", "item_id": "i9", "title": "存档的标题", "body": S.BODY_S + "存档那一版末尾多了一句。",
         "version_num": 1, "created_at": S._iso(5)},
        {"id": "v9", "item_id": "i9", "title": NEW_TITLE, "body": REWRITE, "version_num": 2, "created_at": S._iso(6)}]
    _join(c)
    out = core.tv_sync(c, S.TV)
    l = _links(c)["n1"]
    assert l["match_kind"] == "ambiguous" and l["version_id"] is None and out["older_version"] == 0
    assert out["snapshots"]["ambiguous"] == 1 and out["snapshots"]["promoted"] == 0
    assert "v9old" in {x.get("version_id") for x in l["candidates"]}


# ── 3. 量重合 / 补分数, 旧版都算 ────────────────────────────────────────────────────────────────

def _copy_link(score=None):
    return {"note_id": "n2", "tv_project_id": TV, "project_id": P1, "version_id": "vcopy", "item_id": None,
            "match_kind": "ingested", "score": score, "lag_days": None, "candidates": None, "tv_text_fp": None}


def test_the_gate_counts_an_older_version_saved_before_publication_as_desk_text():
    from tests import test_tv_sync_gate as GT
    notes = [T._note("n2", "上周发的那篇", T.BODY_NEW)]
    c = GT._tv(notes, links=[_copy_link()])
    _item(c, "i5", [("v5old", "发之前存的一版", T.BODY_NEW, 1), ("v5", "定稿整篇重写", REWRITE, 3)])
    core.tv_sync(c, TV)
    row = _links(c)["n2"]
    assert row["gate_reasons"]["overlap"] == 1.0 and row["origin"] == "unclear", "发布之前写作台里就有这段字"
    assert row["match_kind"] == "ingested" and row["version_id"] == "vcopy", "钉住的补录对照照旧不动"

    c = GT._tv(notes, links=[_copy_link()])                    # 对照: 只有定稿那一版时量不到
    _item(c, "i5", [("v5", "定稿整篇重写", REWRITE, 3)])
    core.tv_sync(c, TV)
    assert _links(c)["n2"]["gate_reasons"]["overlap"] == 0.0 and _links(c)["n2"]["origin"] == "elsewhere"


def test_rescore_gives_the_same_number_the_nightly_sync_records():
    body = FILLER + T.BODY_A.split("。")[0] + "。"               # 借了旧版一句: 对不上, 分数在 0 和 EDITED_FLOOR 之间
    note = T._note("n2", "别处起的标题", body)
    nightly = _older_client([note], with_target=False)          # W-10: 只记 unmatched, 带分数
    core.tv_sync(nightly, TV)
    want = _links(nightly)["n2"]
    assert want["match_kind"] == "unmatched" and 0 < want["score"] < tvlink.EDITED_FLOOR
    best_only = tvlink.match_note(tvlink.Note.from_row(note), tvlink.VersionIndex(
        [tvlink.Version.from_row(r) for r in store.versions_for_linking(nightly, P1)])).score
    assert want["score"] > round(best_only, 3), "旧版那一句算进去了"

    later = _older_client([note], links=[_copy_link()])
    out = core.tv_rescore_misses(later, TV)
    assert out["written"] == 1 and _links(later)["n2"]["score"] == want["score"]


# ── 4. 指纹自愈 ─────────────────────────────────────────────────────────────────────────────────

def test_heal_still_rebuilds_the_best_when_only_an_older_version_has_rows(monkeypatch):
    """帖连到旧版 v1、W-40 把帖的文字记在 v1 下; 之后再改稿时进程在撤光整个 item 的行之后被杀, 定稿 v2 没有行了。第二晚 W-40
    先把 v1 那一行记回来 —— 自愈不能因为「同 item 别的版本有行」就当成改稿没挂上 item 跳过 v2: 有行的是更旧的那版。"""
    from tests import test_deskcore_commit_judge as J
    from tests.test_tv_sync_fingerprint_heal import _joinable, _rows
    monkeypatch.setattr(config, "JUDGE_URL", "")
    c = J._client()
    v1 = core.commit_drafts(c, J.PROJ, [{"title": "绩点二点八", "body": J.BODY_B}], user_id=J.ME)["version_ids"][0]
    J._embed(c)
    v2 = core.commit_drafts(c, J.PROJ, [{"title": "凌晨收到转正邮件", "body": J.BODY_C, "replaces_version_id": v1}],
                            user_id=J.ME)["version_ids"][0]
    assert not _rows(c, v1) and _rows(c, v2), "前提: 正常改稿, 旧版的行撤了"
    _joinable(c)
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    note = {"note_id": "n1", "raw_content": f"【标题】绩点二点八的那篇 【正文】{J.BODY_B}", "publish_time": tomorrow,
            "tier": "趴", "created_at": tomorrow}
    c.rpc_impl["deskcore_tv_notes"] = lambda a: [note] if a.get("_after") is None else []
    out = core.tv_sync(c, "TUGE_phase1")
    assert out["older_version"] == 1 and _links(c)["n1"]["version_id"] == v1
    assert [r["title"] for r in _rows(c, v1)] == ["绩点二点八的那篇"], "W-40: 帖的文字记在 v1 下"

    c.rows["draft_fingerprints"] = [r for r in c.rows["draft_fingerprints"] if r["version_id"] not in (v1, v2)]
    out = core.tv_sync(c, "TUGE_phase1")
    gap = out["fingerprint_gap"]
    assert _rows(c, v1), "第二晚 W-40 先把 v1 那一行记回来了"
    assert (gap["missing"], gap["rebuilt"], gap["skipped"]) == (1, 1, 0), gap
    assert _rows(c, v2), "定稿 v2 的文字补回来了"
