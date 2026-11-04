"""补录的帖, TV 正文后来改了(外部评审 11-04 F07, 2026-11-04)。

ingested 的对照是终态(D-064 / D-131): 不重对、不回填血缘, --rematch 也不碰 —— 这条身份线不动。可原来 tv-sync 在
``_pinned`` 那里直接跳过, 正文也不看: 运营在飞书里把补录过的帖整篇换掉, 新文字永远不进指纹库(W-40 不收 ingested, 以为
"版本就是这篇帖"), 照着新帖写的稿查重拦不住。这里钉住: 改过的记 drift['ingested_text_changed'] 和一条样本(不带正文),
改后的文字记一行指纹在补录的那一版下(按文字幂等), 记上了才更新 tv_text_fp; 对照、版本、TV 血缘都不动; 没改的什么都不做;
017 之前没记过指纹的拿那一版的正文比一次, 一样就只记指纹。
"""

from __future__ import annotations

import pytest

from deskcore import core, fingerprint as fp, store, tvlink
from tests.test_tv_sync import BODY_NEW, TV, _client, _note

BODY_SWAP = ("运营后来把这条笔记整篇换成了另一篇: 讲的是第一次带猫去打疫苗, 猫在航空箱里叫了一路, "
             "到了医院反而一声不吭, 医生说这是吓坏了, 回家之后要给它一个安静的角落待两天。") * 2


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)


def _link(c, nid="n2"):
    return next(l for l in c.rows["tv_note_links"] if l["note_id"] == nid)


def _rows(c, vid):
    return [r for r in c.rows["draft_fingerprints"] if r.get("version_id") == vid]


def _ingested_once():
    notes = [_note("n2", "上周发的那篇", BODY_NEW)]
    c = _client(notes)
    core.tv_sync(c, TV, write_tv=True)
    l = _link(c)
    assert l["match_kind"] == "ingested" and l["tv_text_fp"]
    # 假库没有真 join: store.versions_in_project 的 items!inner → batches!inner 靠这里把补录那一版的嵌套摆好
    items = {i["id"]: i for i in c.rows["items"]}
    batches = {b["id"]: b for b in c.rows["batches"]}
    for v in c.rows["versions"]:
        it = items.get(v.get("item_id"))
        if it and "items" not in v:
            v["items"] = {"id": it["id"], "batches": {"project_id": batches[it["batch_id"]]["project_id"]}}
    return notes, c, dict(l)


def _swap(notes, body, title="上周发的那篇"):
    notes[0]["raw_content"] = f"【标题】{title} 【正文】{body} #跑步 #能量胶"


def test_a_replaced_ingested_post_puts_its_new_text_in_the_library_without_touching_the_link():
    notes, c, before = _ingested_once()
    vid = before["version_id"]
    assert len(_rows(c, vid)) == 1
    backfills = len(c.tv_calls["backfill"])

    _swap(notes, BODY_SWAP)
    out = core.tv_sync(c, TV, write_tv=True, rematch=True)
    dr = out["drift"]
    assert out["counts"]["already_linked"] == 1 and dr["ingested_text_changed"] == 1
    (smp,) = [s for s in dr["samples"] if s["note_id"] == "n2"]
    assert smp["why"].startswith("ingested_drift") and smp["old_kind"] == "ingested" and smp["old_version"] == vid
    assert smp["new_kind"] is None and smp["overlap"] is not None and smp["overlap"] < 0.1
    assert not any(BODY_SWAP[:10] in str(v) for v in smp.values()), "样本不带正文"

    rows = _rows(c, vid)
    assert len(rows) == 2 and out["published_fp"]["written"] == 1, "新文字记在补录的那一版下"
    new = next(r for r in rows if r["opening_hash"] == fp.opening_hash(tvlink.strip_hashtags(BODY_SWAP)))
    assert new["ngram_hashes"] == fp.ngram_hashes(tvlink.strip_hashtags(BODY_SWAP))

    l = _link(c)
    assert (l["match_kind"], l["version_id"], l["item_id"], l["project_id"]) == \
        ("ingested", vid, before["item_id"], before["project_id"]), "身份那条线不动"
    assert l["tv_text_fp"] == tvlink.text_fp(tvlink.Note.from_row(notes[0])) != before["tv_text_fp"]
    assert len(c.tv_calls["backfill"]) == backfills or all(
        x["note_id"] != "n2" for call in c.tv_calls["backfill"][backfills:] for x in call), "不回填血缘"

    again = core.tv_sync(c, TV, write_tv=True)
    assert again["drift"]["ingested_text_changed"] == 0 and len(_rows(c, vid)) == 2, "第二晚什么都不多记"


def test_an_unchanged_or_hashtag_only_change_does_nothing():
    notes, c, before = _ingested_once()
    out = core.tv_sync(c, TV)
    assert out["drift"]["ingested_text_changed"] == 0 and out["published_fp"]["checked"] == 0
    notes[0]["raw_content"] = notes[0]["raw_content"].replace("#跑步 #能量胶", "#换了 #话题")
    out = core.tv_sync(c, TV)
    assert out["drift"]["ingested_text_changed"] == 0 and len(_rows(c, before["version_id"])) == 1


def test_a_legacy_link_without_a_fingerprint_is_compared_against_its_version_once():
    notes, c, before = _ingested_once()
    _link(c)["tv_text_fp"] = None                       # 017 之前的补录对照
    out = core.tv_sync(c, TV)
    assert out["drift"]["ingested_seeded"] == 1 and out["drift"]["ingested_text_changed"] == 0
    assert _link(c)["tv_text_fp"] == before["tv_text_fp"] and len(_rows(c, before["version_id"])) == 1

    _link(c)["tv_text_fp"] = None
    _swap(notes, BODY_SWAP)
    out = core.tv_sync(c, TV)
    assert out["drift"]["ingested_text_changed"] == 1 and out["drift"]["ingested_seeded"] == 0
    assert len(_rows(c, before["version_id"])) == 2 and _link(c)["tv_text_fp"] is not None


def test_the_fingerprint_waits_until_the_text_is_in_the_library(monkeypatch):
    notes, c, before = _ingested_once()
    _swap(notes, BODY_SWAP)
    real = store.write_fingerprints
    monkeypatch.setattr(store, "write_fingerprints", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    out = core.tv_sync(c, TV)
    assert out["published_fp"]["error"] and out["drift"]["fp_held"] == 1
    assert _link(c)["tv_text_fp"] == before["tv_text_fp"], "文字没记上就不记指纹: 明晚再核"
    monkeypatch.setattr(store, "write_fingerprints", real)
    out = core.tv_sync(c, TV)
    assert out["drift"]["ingested_text_changed"] == 1 and len(_rows(c, before["version_id"])) == 2
    assert _link(c)["tv_text_fp"] != before["tv_text_fp"]


def test_dry_run_counts_and_writes_nothing():
    notes, c, before = _ingested_once()
    _swap(notes, BODY_SWAP)
    out = core.tv_sync(c, TV, dry_run=True)
    assert out["drift"]["ingested_text_changed"] == 1 and out["published_fp"]["to_write"] == 1
    assert len(_rows(c, before["version_id"])) == 1 and _link(c)["tv_text_fp"] == before["tv_text_fp"]
