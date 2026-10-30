"""W-40: 对上了写作台版本的帖, 发出去的文字也记进指纹库(core._fingerprint_published, tv_sync 收尾那一步)。

盯的是: 改过再发的帖记一行在那一版下、时刻 / 写手 / angle_key 跟着那一版最早那行; 按文字幂等(第二晚不再记, 和底稿只差
话题标签的不记); 以前对上的第一晚一起补; 没核过现在正文的对照(整篇换了没挪、差异没算成)不记; 那一版没有指纹行的不补;
dry-run 只数; 写失败不挡对照、退出码 1。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from deskcore import cli, core, fingerprint as fp, tvlink
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
WRITER = "22222222-2222-2222-2222-222222222222"
P1 = "aaaaaaaa-0000-0000-0000-000000000001"
TV = "SPX_phase1"
KEY = "k-angle-1"
T0 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
TITLE = "秋招投了六十份简历没回音"
DRAFT = ("投到第六十份的时候我开始怀疑是不是邮箱坏了, 后来发现坏的是简历第一行。"
         "改完之后一周里来了三个面试, 才知道之前的六十份全都倒在第一行上。")
# 标题一样、正文改过很多的发出去的版本(title_exact)
REWRITE = ("六十份简历石沉大海那阵子, 我每天早上第一件事就是刷邮箱。"
           "后来一个学姐只看了一眼就说: 你第一行写的是求职意向, 没人关心这个。换成项目结果之后, 一周三个面试。")


def _iso(days):
    return (T0 + timedelta(days=days)).isoformat()


def _fp_row(rid, vid, body, *, title=TITLE, days=0, user=WRITER, key=KEY):
    return {"id": rid, "project_id": P1, "version_id": vid, "user_id": user, "title": title,
            "opening": fp.opening_of(body), "opening_hash": fp.opening_hash(body),
            "ngram_hashes": fp.ngram_hashes(body), "angle_key": key, "created_at": _iso(days),
            "title_embedding": None, "embedding_model": None}


def _client(notes, *, draft=DRAFT, fps=None, links=None):
    c = FakeClient(rows={
        "projects": [{"id": P1, "name": "sportsix", "brand": "sportsix", "owner_id": ME,
                      "calibration_notes": "", "tactics": "[]", "custom_roles": []}],
        "tv_project_map": [{"tv_project_id": TV, "project_id": P1, "ingest_target": True, "note": ""}],
        "batches": [{"id": "b1", "project_id": P1, "user_id": WRITER}],
        "items": [{"id": "i1", "batch_id": "b1", "best_version_id": "v1", "created_at": _iso(0),
                   "user_id": WRITER, "status": "pending",
                   "versions": [{"id": "v1", "title": TITLE, "body": draft, "version_num": 1, "created_at": _iso(0)}],
                   "batches": {"project_id": P1}}],
        "versions": [{"id": "v1", "item_id": "i1", "title": TITLE, "body": draft, "version_num": 1,
                      "created_at": _iso(0)}],
        "tv_note_links": list(links or []),
        "draft_fingerprints": list([_fp_row("f1", "v1", draft)] if fps is None else fps),
    })

    def _tv_notes(a):
        rows = [n for n in notes if a.get("_after") is None or n["note_id"] > a["_after"]]
        return rows[: a["_limit"]]
    c.rpc_impl = {"deskcore_tv_notes": _tv_notes, "deskcore_tv_backfill_lineage": lambda a: len(a["_links"])}
    return c


def _note(nid, title, body, days=2, **extra):
    return {"note_id": nid, "raw_content": f"【标题】{title} 【正文】{body} #求职 #简历",
            "publish_time": _iso(days), "tier": "趴", "created_at": _iso(days), **extra}


def _rows(c, vid="v1"):
    return [r for r in c.rows["draft_fingerprints"] if r.get("version_id") == vid]


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)


def test_a_rewritten_post_records_its_text_under_the_version_it_links_to():
    c = _client([_note("n1", TITLE, REWRITE)])
    out = core.tv_sync(c, TV)
    link = next(l for l in c.rows["tv_note_links"] if l["note_id"] == "n1")
    assert link["match_kind"] == "title_exact" and link["version_id"] == "v1"
    rows = _rows(c)
    assert len(rows) == 2 and out["published_fp"]["written"] == 1
    new = next(r for r in rows if r["id"] != "f1")
    assert new["opening_hash"] == fp.opening_hash(REWRITE) and new["ngram_hashes"] == fp.ngram_hashes(REWRITE)
    # 是那一版的另一种写法: 时刻、写手、angle_key 跟着那一版最早那行, 不记成今晚、不记成项目 owner
    assert new["created_at"] == _iso(0) and new["user_id"] == WRITER and new["angle_key"] == KEY
    assert new["project_id"] == P1 and new["title"] == TITLE
    # 查重认得出它了: 照着发出去的帖写的下一篇撞的是这一行
    assert any(r["opening_hash"] == fp.opening_hash(REWRITE) for r in c.rows["draft_fingerprints"])


def test_it_is_idempotent_by_text_across_nights():
    c = _client([_note("n1", TITLE, REWRITE)])
    core.tv_sync(c, TV)
    again = core.tv_sync(c, TV)
    assert len(_rows(c)) == 2 and again["published_fp"]["written"] == 0 and not again["published_fp"]["error"]


def test_posts_identical_to_the_draft_or_differing_only_by_hashtags_add_nothing():
    # 底稿原样带着话题标签存的; 帖读进来剥了话题 —— 只差话题, 不另记一行
    c = _client([_note("n1", TITLE, DRAFT)], draft=DRAFT + "\n#求职 #简历")
    out = core.tv_sync(c, TV)
    assert out["counts"]["body_exact"] == 1
    assert len(_rows(c)) == 1 and out["published_fp"]["written"] == 0
    c2 = _client([_note("n1", TITLE, DRAFT)])
    assert core.tv_sync(c2, TV)["published_fp"]["written"] == 0 and len(_rows(c2)) == 1


def test_body_exact_with_a_rewritten_tail_is_recorded():
    # body_exact 只是前 40 字一样: 后面改过的照样要记
    tail = DRAFT[:60] + "后来我把整段经历改成了三个数字, 面试官第一句话就问其中那个转化率是怎么做到的。" * 2
    c = _client([_note("n1", TITLE, tail)])
    out = core.tv_sync(c, TV)
    assert out["counts"]["body_exact"] == 1 and out["published_fp"]["written"] == 1 and len(_rows(c)) == 2


def test_links_from_earlier_nights_are_backfilled_on_the_first_night():
    n = _note("n1", TITLE, REWRITE)
    note = tvlink.Note.from_row(n)
    prev = {"note_id": "n1", "tv_project_id": TV, "project_id": P1, "version_id": "v1", "item_id": "i1",
            "match_kind": "title_exact", "score": 1.0, "lag_days": 2, "candidates": None,
            "tv_text_fp": tvlink.text_fp(note), "synced_to_tv_at": None, "updated_at": _iso(3)}
    c = _client([n], links=[prev])
    out = core.tv_sync(c, TV)
    assert out["counts"]["already_linked"] == 1, "对照本身不重写"
    assert out["published_fp"]["checked"] == 1 and out["published_fp"]["written"] == 1 and len(_rows(c)) == 2


def test_a_link_not_checked_against_the_current_text_is_left_alone():
    # 上次核的是别的正文(tv_text_fp 对不上), 这晚又核不了(整篇换了、TV 自己的 lineage 还指着它 → blocked): 不记
    n = _note("n1", "别的标题", "整篇换成了别的东西, 和那一版没有关系。" * 4,
              source_autowriter_version_id="v1")
    prev = {"note_id": "n1", "tv_project_id": TV, "project_id": P1, "version_id": "v1", "item_id": "i1",
            "match_kind": "tv_lineage", "score": None, "lag_days": 2, "candidates": None,
            "tv_text_fp": "old-fingerprint", "synced_to_tv_at": None, "updated_at": _iso(3)}
    c = _client([n], links=[prev])
    out = core.tv_sync(c, TV)
    assert out["drift"]["blocked"] == 1
    assert out["published_fp"]["checked"] == 0 and len(_rows(c)) == 1


def test_a_text_change_is_recorded_only_once_its_diff_is_done(monkeypatch):
    # 改过字、还是那一篇(W-34): 正文指纹记上了才算核过; 差异没算成(指纹先不记)的这晚不记帖的文字
    edited = DRAFT.replace("三个面试", "四个面试") + "现在回头看, 那一行字比我写的所有项目经历都重要。"
    n = _note("n1", TITLE, edited)
    prev = {"note_id": "n1", "tv_project_id": TV, "project_id": P1, "version_id": "v1", "item_id": "i1",
            "match_kind": "title_exact", "score": 1.0, "lag_days": 2, "candidates": None,
            "tv_text_fp": "before-the-edit", "synced_to_tv_at": None, "updated_at": _iso(3)}
    real = core._tv_edit_diffs

    def _broken(*a, **k):
        out = real(*a, **k)
        out["error"] = "boom"
        return out
    monkeypatch.setattr(core, "_tv_edit_diffs", _broken)
    c = _client([n], links=[dict(prev)])
    out = core.tv_sync(c, TV)
    assert out["drift"]["text_changed"] == 1 and out["drift"]["fp_held"] == 1
    assert out["published_fp"]["checked"] == 0 and len(_rows(c)) == 1
    monkeypatch.setattr(core, "_tv_edit_diffs", real)
    out = core.tv_sync(c, TV)
    assert out["drift"]["fp_written"] == 1 and out["published_fp"]["written"] == 1 and len(_rows(c)) == 2


def test_a_version_without_fingerprint_rows_still_gets_the_post_at_its_own_time():
    # 改稿 replaces_version_id 会把整个 item 的指纹撤了重写(老版本下一行都不剩), 帖还连着老版本: 照样记, 时刻取那一版建的时刻,
    # 写手记项目 owner(review: 以前这里每晚 no_rows 跳过, 发出去的文字永远进不了库)
    c = _client([_note("n1", TITLE, REWRITE)], fps=[])
    out = core.tv_sync(c, TV)
    assert out["published_fp"]["no_rows"] == 1 and out["published_fp"]["written"] == 1
    (row,) = _rows(c)
    assert row["opening_hash"] == fp.opening_hash(REWRITE) and row["created_at"] == _iso(0)
    assert row["user_id"] == ME and row["angle_key"] is None
    assert core.tv_sync(c, TV)["published_fp"]["written"] == 0, "第二晚那一版已经有这一行了"


def test_rows_removed_by_a_later_replace_come_back_the_next_night():
    c = _client([_note("n1", TITLE, REWRITE)])
    core.tv_sync(c, TV)
    assert len(_rows(c)) == 2
    # 写手改稿(replaces_version_id=v1): commit 把这个 item 所有版本的指纹撤掉, 新版本自己记一行
    c.rows["draft_fingerprints"] = [r for r in c.rows["draft_fingerprints"] if r.get("version_id") != "v1"]
    out = core.tv_sync(c, TV)
    assert out["published_fp"]["written"] == 1
    assert [r["opening_hash"] for r in _rows(c)] == [fp.opening_hash(REWRITE)]


def test_a_post_that_only_changed_the_title_records_the_new_title():
    # 运营只改了标题、正文原样发: 正文的指纹库里有, 标题没有 —— 查重单看标题也会判死, 所以照样记一行(带发出去的标题)
    c = _client([_note("n1", "简历第一行写错了, 六十份全白投", DRAFT)])
    out = core.tv_sync(c, TV)
    assert out["counts"]["body_exact"] == 1 and out["published_fp"]["written"] == 1
    new = next(r for r in _rows(c) if r["id"] != "f1")
    assert new["title"] == "简历第一行写错了, 六十份全白投" and new["opening_hash"] == fp.opening_hash(DRAFT)
    assert core.tv_sync(c, TV)["published_fp"]["written"] == 0
    # 标题只差标点 / 空白的不算改了
    c2 = _client([_note("n1", TITLE + "!", DRAFT)])
    assert core.tv_sync(c2, TV)["published_fp"]["written"] == 0


def test_a_post_without_a_title_is_compared_by_body_only():
    c = _client([_note("n1", "", DRAFT)])
    out = core.tv_sync(c, TV)
    assert out["counts"]["body_exact"] == 1
    assert out["published_fp"]["checked"] == 1 and out["published_fp"]["written"] == 0 and len(_rows(c)) == 1


def test_ingested_copies_get_no_second_row():
    other = ("这一篇写作台里根本没有: 上周发的, 当时没入库。指纹库不知道它, 下一批查重看不见它。" * 2)
    c = _client([_note("n2", "上周发的那篇", other)])
    out = core.tv_sync(c, TV)
    vid = next(l for l in c.rows["tv_note_links"] if l["note_id"] == "n2")["version_id"]
    assert out["ingest"]["fingerprinted"] == 1 and len(_rows(c, vid)) == 1
    assert out["published_fp"]["checked"] == 0


def test_dry_run_counts_and_writes_nothing():
    c = _client([_note("n1", TITLE, REWRITE)])
    out = core.tv_sync(c, TV, dry_run=True)
    assert out["published_fp"]["to_write"] == 1 and out["published_fp"]["written"] == 0 and len(_rows(c)) == 1


def test_the_cli_prints_the_line_and_a_failed_write_keeps_the_links_and_exits_nonzero(monkeypatch, capsys):
    args = type("A", (), {"tv_project": TV, "all": False, "dry_run": False, "write_tv": False,
                          "since": None, "rematch": False})()
    c = _client([_note("n1", TITLE, REWRITE)])
    assert cli._tv_sync(core, c, args) == 0
    assert "帖的文字进指纹库(W-40): 记了 1 篇" in capsys.readouterr().out

    def _boom(sb, rows):
        raise RuntimeError("insert failed")
    monkeypatch.setattr(core.store, "write_fingerprints", _boom)
    c2 = _client([_note("n1", TITLE, REWRITE)])
    assert cli._tv_sync(core, c2, args) == 1
    assert "帖的文字没记进指纹库" in capsys.readouterr().out
    assert next(l for l in c2.rows["tv_note_links"] if l["note_id"] == "n1")["version_id"] == "v1", "对照照常写了"
    assert len(_rows(c2)) == 1


def test_the_lock_rereads_and_skips_text_recorded_meanwhile(monkeypatch):
    # 锁外读完、拿到锁之前, 别的 tv-sync 刚把同样的文字记上了: 锁里重读, 不再记第二行
    c = _client([_note("n1", TITLE, REWRITE)])
    real = core._project_write_lock

    class _Lock:
        def __init__(self, *a, **k):
            self._inner = real(*a, **k)

        def __enter__(self):
            c.rows["draft_fingerprints"].append(_fp_row("f-meanwhile", "v1", REWRITE))
            return self._inner.__enter__()

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)
    monkeypatch.setattr(core, "_project_write_lock", _Lock)
    out = core.tv_sync(c, TV)
    assert out["published_fp"]["written"] == 0 and len(_rows(c)) == 2


def _add_version(c, vid, title, body, *, rows=True, days=0):
    c.rows["items"].append({"id": f"i-{vid}", "batch_id": "b1", "best_version_id": vid, "created_at": _iso(days),
                            "user_id": WRITER, "status": "pending",
                            "versions": [{"id": vid, "title": title, "body": body, "version_num": 1,
                                          "created_at": _iso(days)}],
                            "batches": {"project_id": P1}})
    c.rows["versions"].append({"id": vid, "item_id": f"i-{vid}", "title": title, "body": body, "version_num": 1,
                               "created_at": _iso(days)})
    if rows:
        c.rows["draft_fingerprints"].append(_fp_row(f"f-{vid}", vid, body, title=title, days=days, key="k-angle-2"))


def test_the_reread_under_the_lock_keeps_versions_already_done():
    # n1 的文字前一晚已经记在 v1 下, n2 这晚要记在 v2 下: 锁里重读要把两版都读回来, 不能把 v1 当成"没有指纹行"再记一遍
    title2 = "面试官只问了我一个数字"
    draft2 = "第一次面试被问到转化率的时候我愣住了, 因为那个数字是我编进简历的。后来我把每个数字都换成能讲出来历的那个。"
    post2 = "被问到转化率那一刻我才明白, 简历上每个数字都要能讲出来历。后来我只写自己能讲半小时的那三个数。"
    c = _client([_note("n1", TITLE, REWRITE), _note("n2", title2, post2)])
    _add_version(c, "v2", title2, draft2, days=1)
    c.rows["draft_fingerprints"].append(_fp_row("f-pub", "v1", REWRITE))
    out = core.tv_sync(c, TV)
    assert out["published_fp"]["written"] == 1 and out["published_fp"]["no_rows"] == 0
    assert len(_rows(c, "v1")) == 2 and len(_rows(c, "v2")) == 2
    new = next(r for r in _rows(c, "v2") if r["id"] != "f-v2")
    assert new["created_at"] == _iso(1) and new["angle_key"] == "k-angle-2"


def test_inserts_go_in_chunks(monkeypatch):
    monkeypatch.setattr(core, "INGEST_EMBED_CHUNK", 1)
    sizes = []
    real = core.store.write_fingerprints

    def _spy(sb, rows):
        sizes.append(len(rows))
        return real(sb, rows)
    monkeypatch.setattr(core.store, "write_fingerprints", _spy)
    other = REWRITE.replace("学姐", "学长").replace("一周三个面试", "第二周就拿到了两个 offer")
    c = _client([_note("n1", TITLE, REWRITE), _note("n2", TITLE, other)])
    out = core.tv_sync(c, TV)
    assert out["published_fp"]["written"] == 2 and sizes == [1, 1]


def test_a_punctuation_only_edit_does_not_add_a_row():
    # 开头哈希取的是原文前 25 字: 逗号换成全角、补个空格, 窗口一挪哈希就变 —— W-40 只按去标点后的正文比, 不另记一行
    c = _client([_note("n1", TITLE, REWRITE)])
    core.tv_sync(c, TV)
    assert len(_rows(c)) == 2
    c2 = _client([_note("n1", TITLE, REWRITE.replace(", ", "，").replace(": ", "："))], fps=list(c.rows["draft_fingerprints"]))
    assert core.tv_sync(c2, TV)["published_fp"]["written"] == 0 and len(_rows(c2)) == 2


def test_a_post_identical_to_a_version_without_rows_is_recorded():
    # 那一版的行被改稿撤光了, 帖和它一字不差: 库里没有任何一行盖着它, 照样记(codex on aw#133)
    c = _client([_note("n1", TITLE, DRAFT)], fps=[])
    out = core.tv_sync(c, TV)
    assert out["published_fp"]["written"] == 1 and out["published_fp"]["no_rows"] == 1
    (row,) = _rows(c)
    assert row["opening_hash"] == fp.opening_hash(DRAFT) and row["created_at"] == _iso(0)
    # 只差话题标签的也一样: 那一版带话题的那行不在了, 剥了话题的帖就是唯一的一行
    c2 = _client([_note("n1", TITLE, DRAFT)], draft=DRAFT + "\n#求职 #简历", fps=[])
    assert core.tv_sync(c2, TV)["published_fp"]["written"] == 1 and len(_rows(c2)) == 1


def test_rows_removed_while_waiting_for_the_lock_still_get_the_version_time(monkeypatch):
    # 锁外读的时候那一版还有行, 拿到锁之前被改稿撤光了: 锁里重读发现没行, 时刻照样取那一版建的时刻, 不记成今晚(codex on aw#133)
    c = _client([_note("n1", TITLE, REWRITE)])
    real = core._project_write_lock

    class _Lock:
        def __init__(self, *a, **k):
            self._inner = real(*a, **k)

        def __enter__(self):
            c.rows["draft_fingerprints"] = [r for r in c.rows["draft_fingerprints"] if r.get("version_id") != "v1"]
            return self._inner.__enter__()

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)
    monkeypatch.setattr(core, "_project_write_lock", _Lock)
    out = core.tv_sync(c, TV)
    assert out["published_fp"]["written"] == 1 and out["published_fp"]["no_rows"] == 1
    (row,) = _rows(c)
    assert row["created_at"] == _iso(0), row["created_at"]
