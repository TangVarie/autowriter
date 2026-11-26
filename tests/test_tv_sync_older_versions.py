"""tv-sync: 对照用的那一版(best)对不上的帖, 再按 item 对存过的另一版(2026-11-26)。

对照索引每个 item 只有一版; 帖的文字正好是这个 item 存过的另一版(改稿之前存的一轮)时, 主索引对不上它, 回流闸量「发布前
写作台里的文字」也不算它 —— 结构上的缺口。不是 11-26 那 255 篇对不上的帖的解释(它们和写作台里任何文字的重合都不到 15%)。
这里钉住接线(匹配本身在 tests/test_tvlink_by_item.py):
  1. 主索引照旧先对; 只有对不上 / 只是标题一样的才对另一版; 对上的照旧记 body_exact / title_exact / fuzzy / edited, 带 item_id,
     candidates 前面一句(不钉死对照), 单独报 older_version, counts 的键不变;
  2. 之后各步照常认它: W-40 把帖的文字记在那一版下、改稿差异按那一版算、--write-tv 写回那一版;
  3. 查重底稿先看(复审 11-26): 帖是只查过重的那一轮、比存过的那一版更像的, 按底稿转正, 不连那一版(不然两轮之间 AI 改的字
     会被记成人改的); 底稿认不出 / 分不出 / 不比它像的才连那一版; 底稿这晚读不出、帖没日期而底稿更像的, 先不连;
  4. 不认的: 发了之后才存的那版(也不当底稿的对手、不挡同 item 发布前存的那版)、和别的 item 的另一版分不出的(照旧补录, 分数取
     两遍里高的)、补录副本 / TV 同步来的 item 的另一版;
  5. 回流闸量「发布前写作台里的文字」、--rescore-misses 补分数, 另一版都算, 和每晚记的同一个口径;
  6. 指纹自愈: 另一版下有 W-40 记的帖的文字, best 没有行时照样补。
文字全是编的。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import config
from deskcore import core, store, tvlink
from tests import test_draft_snapshots as S
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


# ── 1. 对上存过的另一版 ─────────────────────────────────────────────────────────────────────────────────

def test_a_post_published_from_an_older_version_links_to_it():
    c = _older_client()
    assert tvlink.match_note(tvlink.Note.from_row(T.NOTES[0]),
                             tvlink.VersionIndex([tvlink.Version.from_row(r) for r in store.versions_for_linking(c, P1)])
                             ).kind == "unmatched", "前提: 只有定稿那一版时对不上"
    out = core.tv_sync(c, TV, write_tv=True)
    assert out["counts"] == {"already_linked": 0, "tv_lineage": 1, "body_exact": 1, "title_exact": 0, "fuzzy": 0,
                             "edited": 0, "ambiguous": 0, "unmatched": 1, "no_body": 0}, "counts 的键不变"
    assert (out["older_version"], out["versions"], out["older_versions_indexed"], out["matched"]) == (1, 1, 1, 1)
    assert "其中对上的是稿子存过的另一版 1" in out["note"]
    l = _links(c)["n1"]
    assert (l["match_kind"], l["version_id"], l["item_id"], l["project_id"], l["lag_days"]) == \
        ("body_exact", "v1", "i1", P1, 2)
    head = l["candidates"][0]
    assert head[core.TV_OLDER_KEY] is True and "另一版" in head["note"] and "对照用的是 v2" in head["note"]
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
    assert "其中对上的是稿子存过的另一版 1" in out and "另有存过的另一版 1 条" in out
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


def test_a_copy_back_sibling_does_not_hide_the_version_saved_before_publication():
    """同一个 item: v1 发布前存的, v1cb 是发了以后把帖抄回来存的(比 v1 更像帖), best 又整篇重写了。抄回来的那版当不了来源,
    第二遍当它不在 —— 不然并成一版时它把 v1 挡住, 自己又因为晚于发布不认, 整个 item 就对不上了(复审 11-26)。"""
    pub = T.BODY_A + "发出去之前运营在末尾补了一句, 说第一行要写最硬的那段经历。"
    c = _older_client([T._note("n1", "秋招投了六十份简历没回音", pub)])
    c.rows["versions"].append({"id": "v1cb", "item_id": "i1", "title": "抄回来存的", "body": pub, "version_num": 3,
                               "created_at": T._iso(3)})
    _join(c)
    out = core.tv_sync(c, TV)
    l = _links(c)["n1"]
    assert (l["match_kind"], l["version_id"], l["lag_days"], out["older_version"]) == ("body_exact", "v1", 2, 1)


# ── 2b. 查重底稿先看(复审 11-26) ───────────────────────────────────────────────────────────────
# 帖是哪一轮只查过重(check_drafts 记了底稿)的字, 而这个 item 早些时候存过另一轮: 第二遍对得上存的那一轮, 可底稿是一模一样的
# 那一轮 —— 按底稿转正, 不连存的那一轮(连了的话两轮之间 AI 自己改的字进改稿差异, 当成「人改了什么」进每周摘要)。

_S = ("周末骑了一百公里, 第六十公里的时候腿开始发抖, 补给站的香蕉早就吃完了。",
      "后来在路边小卖部买了两根能量胶, 撑到终点才发现袋子里还剩一根没开。",
      "回家量了一下, 心率最高到了一百八十五, 第二天腿疼得下不了楼梯。",
      "下次出门前我一定先把补给清单写在手机备忘录里, 别再靠路边小卖部救命。",
      "这一次我把水壶也换成了大号的, 七百五十毫升, 两壶够骑六十公里。",
      "教练说骑长途前一天要多吃碳水, 我晚饭吃了两大碗米饭。",
      "第二天早上五点出门, 路上几乎没有车, 风也不大。",
      "骑到第三十公里的时候太阳出来了, 我停下来抹了一层防晒。")
ROUND1 = "".join(_S[:3])                       # 存过的那一轮
ROUND2 = "".join(_S[:2]) + _S[3]               # 只查过重的下一轮: 最后一句换了
ROUND_K = "".join(_S)                          # 查过重的那一轮
ROUND_K2 = "".join(_S[:7]) + "骑到第三十公里时太阳出来, 我停在树荫下补了防晒和水。"   # AI 又改了一句, 存了


def _snap_case(post, snaps, versions, **note):
    """S 的项目再放一个 item i9: ``versions`` = [(vid, body, days)], 按顺序编 version_num, 最后一版是 best(整篇重写)。"""
    c = S._client([S._note("n1", "骑了一百公里", post, **note)], snaps)
    c.rows["items"].append({"id": "i9", "batch_id": "b1", "best_version_id": "v9", "created_at": S._iso(versions[0][2]),
                            "user_id": ME, "status": "pending"})
    for k, (vid, body, days) in enumerate(list(versions) + [("v9", REWRITE, versions[-1][2] + 1)], 1):
        c.rows["versions"].append({"id": vid, "item_id": "i9", "title": "骑了一百公里" if vid != "v9" else NEW_TITLE,
                                   "body": body, "version_num": k, "created_at": S._iso(days)})
    return _join(c)


def _diffs(c, nid="n1"):
    return [(r["version_id"], r["changed"], r["removed_count"], r["added_count"])
            for r in c.rows.get("tv_edit_diffs", []) if r["note_id"] == nid]


def _second_pass(c, nid="n1"):
    """只看主索引 + 第二遍(不看底稿)会连到哪一版。"""
    older: list = []
    best = [tvlink.Version.from_row(r) for r in store.versions_for_linking(c, S.P1, older=older)]
    index = tvlink.VersionIndex(best)
    ov = core._OlderVersions.build(index, best, [tvlink.Version.from_row(r) for r in older])
    note = next(tvlink.Note.from_row(r) for r in c.rpc_impl["deskcore_tv_notes"]({"_limit": 99}) if r["note_id"] == nid)
    m = core._match_with_older(note, index, ov)
    return m.version.version_id if m.version is not None else None


def test_a_checked_round_beats_an_earlier_saved_round():
    c = _snap_case(ROUND2, [S._snap("s2", "骑了一百公里", ROUND2)], [("v9old", ROUND1, 0)])
    assert _second_pass(c) == "v9old", "前提: 不看底稿的话第二遍连的是存过的那一轮"
    out = core.tv_sync(c, S.TV)
    l = _links(c)["n1"]
    assert l["match_kind"] == "body_exact" and l["version_id"] not in ("v9old", "v9") and l["score"] == 1.0
    assert (out["snapshots"]["promoted"], out["older_version"]) == (1, 0)
    assert c.rows["draft_snapshots"][0]["version_id"] == l["version_id"]
    assert _diffs(c) == [(l["version_id"], False, 0, 0)], "两轮之间 AI 改的那句不算人改的"


def test_a_checked_round_that_grew_from_a_saved_round_is_promoted():
    grown = S.BODY_S + "到家以后才发现手机里存了一路的照片, 一张都没发。"
    c = _snap_case(grown, [S._snap("s1", "骑了一百公里", grown, days=1)], [("v9old", S.BODY_S, 0)])
    assert _second_pass(c) == "v9old"
    out = core.tv_sync(c, S.TV)
    l = _links(c)["n1"]
    assert l["version_id"] not in ("v9old", "v9") and out["snapshots"]["promoted"] == 1 and out["older_version"] == 0
    assert _diffs(c) == [(l["version_id"], False, 0, 0)]


@pytest.mark.parametrize("post,want", [(ROUND_K, "snapshot"), (ROUND_K2, "v9old")],
                         ids=["post_is_the_checked_round", "post_is_the_saved_round"])
def test_one_ai_edit_apart_the_post_goes_to_whichever_round_it_is(post, want):
    """查过重的那一轮和存过的那一轮只差一句(包含度差不到 FUZZY_MARGIN, match_snapshot 本来会说分不出): 帖是哪一轮的字就归
    哪一轮。帖是存过的那一轮时连那一版, 底稿留着不转正。"""
    c = _snap_case(post, [S._snap("s1", "骑了一百公里", ROUND_K)], [("v9old", ROUND_K2, 1)])
    out = core.tv_sync(c, S.TV)
    l = _links(c)["n1"]
    if want == "snapshot":
        assert l["version_id"] == c.rows["draft_snapshots"][0]["version_id"] and out["older_version"] == 0
    else:
        assert l["version_id"] == "v9old" and out["older_version"] == 1 and out["snapshots"]["promoted"] == 0
        assert out["snapshots"]["ambiguous"] == 0 and c.rows["draft_snapshots"][0]["promoted_at"] is None
    assert _diffs(c) == [(l["version_id"], False, 0, 0)]


def test_a_copy_back_version_is_not_a_rival_for_the_snapshot():
    """发了以后才把帖抄回写作台存的另一版(第 5 天; 帖第 2 天发): 它当不了来源, 也不当发布前查过重的那份底稿的对手 —— 当了的话
    底稿一直分不出, 挂到 SNAPSHOT_MAX_AGE_DAYS 过了再被补录(复审 11-26)。best 当对手的样子不变。"""
    c = _snap_case(S.BODY_S, [S._snap("s1", "骑了一百公里", S.BODY_S)],
                   [("v9old", S.BODY_S + "存档那一版末尾多了一句。", 5)])
    out = core.tv_sync(c, S.TV)
    l = _links(c)["n1"]
    assert l["match_kind"] == "body_exact" and l["version_id"] == c.rows["draft_snapshots"][0]["version_id"]
    assert (out["snapshots"]["promoted"], out["snapshots"]["ambiguous"], out["older_version"]) == (1, 0, 0)


def test_an_undated_post_waits_when_a_snapshot_is_closer_than_the_saved_round():
    c = _snap_case(ROUND_K, [S._snap("s1", "骑了一百公里", ROUND_K)], [("v9old", ROUND_K2, 1)])
    note = c.rpc_impl["deskcore_tv_notes"]({"_limit": 9})[0]
    note["publish_time"] = None
    out = core.tv_sync(c, S.TV)
    l = _links(c)["n1"]
    assert l["match_kind"] == "unmatched" and l["version_id"] is None and "填了日期再对" in l["candidates"][-1]["note"]
    assert (out["snapshots"]["undated"], out["older_version"]) == (1, 0)
    note["publish_time"] = S._pub(2)
    out = core.tv_sync(c, S.TV)
    assert _links(c)["n1"]["version_id"] == c.rows["draft_snapshots"][0]["version_id"] and out["older_version"] == 0


def test_an_undated_post_with_no_snapshot_links_the_saved_round():
    row = T._note("n1", "秋招投了六十份简历没回音", T.BODY_A)
    row["publish_time"] = None
    c = _older_client([row])
    out = core.tv_sync(c, TV)
    l = _links(c)["n1"]
    assert (l["match_kind"], l["version_id"], l["lag_days"], out["older_version"]) == ("body_exact", "v1", None, 1)


def test_unreadable_snapshots_hold_the_older_match_for_the_night(monkeypatch):
    """底稿这晚读不出: 不知道有没有更像的那一轮, 先不连(连了就是终态: 有版本的对照每晚不重对), 记 unmatched 明晚再对。"""
    c = _older_client([T._note("n1", "秋招投了六十份简历没回音", T.BODY_A)])

    def boom(*a, **k):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(store, "pending_draft_snapshots", boom)
    out = core.tv_sync(c, TV)
    l = _links(c)["n1"]
    assert l["match_kind"] == "unmatched" and l["version_id"] is None and out["older_version"] == 0
    assert "读不出查重底稿" in l["candidates"][-1]["note"] and out["ingest"] is None
    monkeypatch.undo()
    out = core.tv_sync(c, TV)
    assert (_links(c)["n1"]["version_id"], out["older_version"]) == ("v1", 1)


def test_a_replaced_post_moves_to_another_saved_version_with_its_w34_note():
    """TV 里整篇换了(W-34), 重对对上的是 item 存过的另一版: 照样先过底稿那一步, 连上时 candidates 前面先是 W-34 那句。"""
    prev = {"note_id": "n1", "tv_project_id": TV, "project_id": P1, "version_id": "v7", "item_id": "i7",
            "match_kind": "body_exact", "score": 1.0, "lag_days": 0, "candidates": None, "tv_text_fp": "旧的指纹"}
    c = _older_client([T._note("n1", "秋招投了六十份简历没回音", T.BODY_A)], links=[prev])
    _item(c, "i7", [("v7", "原来连的那篇", T.BODY_NEW, 0)])
    out = core.tv_sync(c, TV)
    l = _links(c)["n1"]
    assert (l["match_kind"], l["version_id"], out["drift"]["relinked"], out["older_version"]) == ("body_exact", "v1", 1, 1)
    assert l["candidates"][0]["note"].startswith("W-34") and l["candidates"][1][core.TV_OLDER_KEY]


def test_a_relink_back_to_the_same_saved_version_is_written_at_once(monkeypatch):
    """重对还对回原来那一版(这里那一版是第二遍连上的另一版): 对照本来就在那一版上, 照旧马上写(不再绕一遍底稿) ——
    tv_sync 接着要改刚写的那一行(links[-1])的指纹。"""
    c = _older_client([T._note("n1", "秋招投了六十份简历没回音", T.BODY_A)])
    core.tv_sync(c, TV)
    notes = [T._note("n1", "秋招投了六十份简历没回音", T.BODY_A + "最后补一句。")]
    c.rpc_impl["deskcore_tv_notes"] = lambda a: notes if a.get("_after") is None else []
    monkeypatch.setattr(core.tvlink, "still_same", lambda *a, **k: False)     # 当成整篇换了, 逼它重对
    out = core.tv_sync(c, TV)
    l = _links(c)["n1"]
    assert out["drift"]["kept_after_rematch"] == 1 and out["counts"]["body_exact"] == 1
    assert (l["version_id"], out["older_version"], out["counts"]["unmatched"]) == ("v1", 1, 0)
    assert l["tv_text_fp"] == tvlink.text_fp(tvlink.Note.from_row(notes[0])), "指纹照常在差异算好之后记上"


def test_a_writer_edit_version_that_became_another_version_gets_no_diff():
    """record_edit 记下的写手手改版转正之后(batch 的 params.via = record_edit)又改了稿, 它成了另一版: 第二遍连上它、TV 自己
    指着它, 都不算改稿差异 —— 那是写手自己的字(AI → 人的差异在 style_edits 里)。另一版的行要和 best 的行一样带 via。"""
    notes = [T._note("n1", "秋招投了六十份简历没回音", T.BODY_A),
             T._note("n8", "TV 指向那一版", T.BODY_A, source_autowriter_version_id="v1")]
    c = _older_client(notes)
    c.rows["batches"][0]["params"] = {"source": "deskcore", "via": "record_edit"}
    _join(c)
    out = core.tv_sync(c, TV)
    assert _links(c)["n1"]["version_id"] == "v1" and _links(c)["n8"]["version_id"] == "v1" and out["older_version"] == 1
    assert _diffs(c, "n1") == [] and _diffs(c, "n8") == []


# ── 3. 量重合 / 补分数, 另一版都算 ────────────────────────────────────────────────────────────────

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
