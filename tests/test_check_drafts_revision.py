"""改稿过 check_drafts: 带 replaces_version_id 就不和自己存档的旧版比(2026-10-30 发布收尾)。

写手那边的反馈: 改过的稿子一查重就被说成「正文开头与历史稿完全一致」—— 撞的是它自己存过档的那一版。原来 check_drafts
不认 replaces_version_id(只有 score_drafts 先摘旧版再查), archive_pending 那句话还叫模型改稿时跳过查重。现在 check_drafts
也认这个字段, 同一套核对(_lookup_replacements); 仍然只读, 一行指纹都不摘。这里钉的是:

  · 改稿带 replaces_version_id 过得去, 不带照旧被自己的旧版拦下;
  · 真撞上别的稿子照旧拦 —— 包括库里那一路只回最像的一条、别的稿子被旧版挡在后面的那种(开头 / 四字串 / 标题语义);
  · 两条实现(库里那一路、退回 Python 那一路)结论一样; 不带这个字段的批次一次多余的查询都不加;
  · id 对不上的判 reject、gate=replace_lookup, 和 score_drafts 拒的是同一种。

所有示例文本都是合成的。
"""

from __future__ import annotations

import copy

import pytest

import db
from deskcore import core, store
from deskcore import fingerprint as fp
from tests.test_deskcore_commit_judge import (BODY_A, BODY_A2, BODY_B, BODY_C, ME, OTHER_VID, PROJ, _client, _embed,
                                              _seed_version)


def _check_rpc(c):
    """deskcore_check_drafts 的假件, 和库里那一路同一个形状: 每一路只回最像的那一条的**标题**, 没法点名排除哪几行。
    开头精确撞车取表里排在前面的那条(SQL 的 LIMIT 1) —— 旧版先存, 正好把后来的别的稿子挡在后面。"""
    def _impl(args):
        hist = [r for r in c.rows.get("draft_fingerprints", []) if r.get("project_id") == args["_project_id"]]
        out = []
        for k, q in enumerate(args.get("_rows") or []):
            ng = set(q.get("ngram_hashes") or [])
            oh = q.get("opening_hash")
            v = db._parse_pgvector(q.get("title_embedding"))
            row = {"idx": k, "best_sim": 0.0, "sim_title": None, "best_j": 0.0, "j_title": None,
                   "open_exact": False, "open_title": None, "best_c": 0.0, "c_title": None, "c_sample": 0}
            hit = next((h for h in hist if oh and h.get("opening_hash") == oh), None)
            if hit is not None:
                row.update(open_exact=True, open_title=hit["title"])
            for h in hist:
                j, cc, s = fp.sketch_overlap(ng, set(h.get("ngram_hashes") or []))
                if j > row["best_j"]:
                    row.update(best_j=j, j_title=h["title"])
                if s >= args.get("_contain_min_sample", 15) and cc > row["best_c"]:
                    row.update(best_c=cc, c_title=h["title"], c_sample=s)
                e = db._parse_pgvector(h.get("title_embedding"))
                if v and e and h.get("embedding_model") == q.get("embedding_model"):
                    sim = core.dedup.cosine_similarity(v, e)
                    if sim > row["best_sim"]:
                        row.update(best_sim=sim, sim_title=h["title"])
            out.append(row)
        return out
    return _impl


@pytest.fixture(params=["rpc", "python"])
def path(request):
    """两条实现都要走一遍: 库里那一路(下推)、RPC 不在时退回的 Python 那一路。"""
    return request.param


def _archived(path, *more):
    """存过档的一篇(标题「秋招等回音」, 正文 BODY_A), 外加 ``more`` 里的几篇。回 (client, 那一篇的 version_id)。"""
    c = _client()
    out = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A}, *more], user_id=ME)
    assert out["written"] == 1 + len(more), out
    _embed(c)
    if path == "rpc":
        c.rpc_impl["deskcore_check_drafts"] = _check_rpc(c)
    return c, out["version_ids"][0]


def _ingested(c, title: str, body: str, *, opening_hash: str | None = None) -> None:
    """一条补录进来的指纹(不挂任何版本), 排在表的最后。"""
    c.rows["draft_fingerprints"].append({
        "id": f"fp-ingest-{len(c.rows['draft_fingerprints'])}", "project_id": PROJ, "user_id": ME, "version_id": None,
        "title": title, "opening": fp.opening_of(body),
        "opening_hash": opening_hash if opening_hash is not None else fp.opening_hash(body),
        "ngram_hashes": fp.ngram_hashes(body), "title_embedding": None, "embedding_model": None,
        "angle_key": None, "created_at": "2026-10-29T00:00:00+00:00"})


# ══════════════════════════════════════════════════════════════════════
# 1 · 改稿带 replaces_version_id 过得去, 不带照旧被旧版拦下
# ══════════════════════════════════════════════════════════════════════

def test_a_revision_passes_with_replaces_version_id_and_fails_without_it(path):
    c, v1 = _archived(path)
    fps_before = copy.deepcopy(c.rows["draft_fingerprints"])

    bare = core.check_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A2}], user_id=ME)["results"][0]
    assert bare["status"] == "reject" and bare["collided_with"] == "秋招等回音", bare
    assert "replaces_version_id" not in bare

    out = core.check_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A2, "replaces_version_id": v1}], user_id=ME)
    r = out["results"][0]
    assert r["status"] == "pass", r
    assert r["replaces_version_id"] == v1, "回执带回替换的是哪一版"
    assert r["signals"]["opening_exact_match"] is False and r["signals"]["body_ngram_jaccard"] == 0, r
    assert out["summary"]["pass"] == 1 and out["summary"]["reject"] == 0
    assert c.rows["draft_fingerprints"] == fps_before, "check_drafts 只读: 旧版的指纹一行不摘、一个字不改"


def test_the_revision_then_archives_as_a_replacement_and_its_snapshot_is_closed():
    """查过重(带 replaces_version_id, 判 pass, 底稿照记)再存档替换: 底稿记成转正到新的那一版(W-38 复审那条照旧)。"""
    c, v1 = _archived("rpc")
    core.check_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A2, "replaces_version_id": v1}],
                      user_id=ME, record_snapshot=True)
    (snap,) = c.rows["draft_snapshots"]
    assert snap["status"] == "pass" and not snap.get("promoted_at")
    out = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A2, "replaces_version_id": v1}], user_id=ME)
    assert out["replaced"][0]["old_version_id"] == v1
    assert snap["promoted_at"] and snap["version_id"] == out["version_ids"][0]


def test_every_version_of_the_old_item_is_left_out_not_just_the_one_named(path):
    """改过一轮之后再改: 带的是最新那一版的 id, 第一版的指纹(以及 W-40 记在旧版下的发布文字)同样不算对手 ——
    和 score_drafts 摘的是同一批(旧 item 的全部版本)。"""
    c, v1 = _archived(path)
    v2 = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A2, "replaces_version_id": v1}],
                            user_id=ME)["version_ids"][0]
    _embed(c)
    # 第一版下面另有一行(发出去的文字, W-40): 也是这篇自己的
    c.rows["draft_fingerprints"].append({**next(r for r in c.rows["draft_fingerprints"] if r["version_id"] == v2),
                                         "id": "fp-published", "version_id": v1, "ngram_hashes": fp.ngram_hashes(BODY_A),
                                         "opening_hash": fp.opening_hash(BODY_A)})
    third = BODY_A2 + "又补了一句: 第三天晚上我把简历重新排了一遍。"
    r = core.check_drafts(c, PROJ, [{"title": "秋招等回音", "body": third, "replaces_version_id": v2}],
                          user_id=ME)["results"][0]
    assert r["status"] == "pass", r


# ══════════════════════════════════════════════════════════════════════
# 2 · 真撞上别的稿子照旧拦
# ══════════════════════════════════════════════════════════════════════

def test_a_revision_that_copies_another_draft_is_still_rejected(path):
    c, v1 = _archived(path, {"title": "简历第一行", "body": BODY_B})
    r = core.check_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_B, "replaces_version_id": v1}],
                          user_id=ME)["results"][0]
    assert r["status"] == "reject" and r["collided_with"] == "简历第一行", r
    assert r["replaces_version_id"] == v1


def test_another_draft_hidden_behind_the_old_version_on_the_opening_is_found(path):
    """开头和旧版一样, 库里另有一篇(别的号发的, 补录进来的)开头也一样: 库里那一路 LIMIT 1 回的是排在前面的旧版,
    那一篇被挡在后面。排除旧版之后要把它找出来, 照旧判死。"""
    c, v1 = _archived(path)
    _ingested(c, "别的号发的那篇", BODY_C, opening_hash=fp.opening_hash(BODY_A2))
    bare = core.check_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A2}], user_id=ME)["results"][0]
    assert bare["collided_with"] == "秋招等回音"
    r = core.check_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A2, "replaces_version_id": v1}],
                          user_id=ME)["results"][0]
    assert r["status"] == "reject" and r["decided_by"] == ["opening"], r
    assert r["collided_with"] == "别的号发的那篇", r


def test_another_draft_hidden_behind_the_old_version_on_the_body_is_found(path):
    """正文和旧版、和库里另一篇一样像(打平, 旧版排在前面): 库里那一路回的是旧版的标题。排除旧版重比, 照旧判死。"""
    c, v1 = _archived(path)
    _ingested(c, "飞书上的另一篇", BODY_A, opening_hash="0" * 16)
    r = core.check_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A2, "replaces_version_id": v1}],
                          user_id=ME)["results"][0]
    assert r["status"] == "reject" and "ngram" in r["decided_by"], r
    assert r["collided_with"] == "飞书上的另一篇", r
    assert r["signals"]["opening_exact_match"] is False


@pytest.mark.parametrize("other_vec, verdict", [([0.95, 0.3122, 0.0, 0.0], "reject"), ([0.5, 0.866, 0.0, 0.0], "pass")])
def test_title_similarity_to_another_draft_is_judged_without_the_old_version(path, monkeypatch, other_vec, verdict):
    """标题语义: 改稿的标题和旧版一模一样(余弦 1.0), 库里另一篇标题很像(0.95)或不像(0.5)。库里那一路只回最像的旧版;
    排除旧版之后, 很像的那篇照旧判死, 不像的放行。"""
    vecs = {"秋招等回音": [1.0, 0.0, 0.0, 0.0]}
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: True)
    monkeypatch.setattr(core.dedup, "embed_texts", lambda ts: [vecs.get(t) for t in ts])
    c = _client()
    vid = "f0f0f0f0-0000-0000-0000-0000000000f1"
    _seed_version(c, vid, PROJ)
    model = core.dedup.EMBEDDING_MODEL
    for fid, ver, title, body, vec in (("fp-own", vid, "秋招等回音", BODY_A, vecs["秋招等回音"]),
                                       ("fp-other", None, "秋招等回信", BODY_B, other_vec)):
        c.rows.setdefault("draft_fingerprints", []).append({
            "id": fid, "project_id": PROJ, "user_id": ME, "version_id": ver, "title": title,
            "opening": fp.opening_of(body), "opening_hash": fp.opening_hash(body), "ngram_hashes": fp.ngram_hashes(body),
            "title_embedding": "[" + ",".join(str(x) for x in vec) + "]", "embedding_model": model,
            "angle_key": None, "created_at": "2026-10-28T00:00:00+00:00"})
    if path == "rpc":
        c.rpc_impl["deskcore_check_drafts"] = _check_rpc(c)
    bare = core.check_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_C}], user_id=ME)["results"][0]
    assert bare["status"] == "reject" and bare["collided_with"] == "秋招等回音"
    r = core.check_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_C, "replaces_version_id": vid}],
                          user_id=ME)["results"][0]
    assert r["status"] == verdict, r
    assert r["signals"]["title_similarity"] == pytest.approx(core.dedup.cosine_similarity([1.0, 0, 0, 0], other_vec),
                                                              abs=1e-3)
    if verdict == "reject":
        assert r["collided_with"] == "秋招等回信" and r["decided_by"] == ["title"], r


# ══════════════════════════════════════════════════════════════════════
# 3 · id 对不上: 和 score_drafts 同一套核对
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("bad, words", [("不是一个UUID", "UUID"), (OTHER_VID, "不是本项目")])
def test_a_bad_replaces_version_id_is_a_replace_lookup_reject_like_score_drafts(bad, words):
    c, _ = _archived("rpc")
    draft = {"title": "一篇新稿", "body": BODY_C, "replaces_version_id": bad}
    out = core.check_drafts(c, PROJ, [draft, {"title": "另一篇新稿", "body": BODY_B}], user_id=ME)
    r = out["results"][0]
    assert r["status"] == "reject" and r["gate"] == "replace_lookup" and words in r["reason"], r
    assert "不用重写" in r["reason"] and r["decided_by"] == [] and r["collided_with"] == ""
    assert out["results"][1]["status"] == "pass", "别的稿子不受连累"
    assert (out["summary"]["reject"], out["summary"]["pass"]) == (1, 1), "summary 跟着改判重算"
    committed = core.commit_drafts(c, PROJ, [draft], user_id=ME)
    assert committed["rejected"][0]["gate"] == "replace_lookup", "存档那边拒的是同一种"


def test_two_drafts_replacing_the_same_version_reject_the_second():
    c, v1 = _archived("rpc")
    out = core.check_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A2, "replaces_version_id": v1},
                                      {"title": "秋招等回音", "body": BODY_A2 + "再改", "replaces_version_id": v1}],
                            user_id=ME)
    assert out["results"][0]["status"] in ("pass", "warn") and out["results"][0]["replaces_version_id"] == v1
    assert out["results"][1]["gate"] == "replace_lookup" and "同一篇" in out["results"][1]["reason"]


def test_a_batch_without_revisions_adds_no_lookups(monkeypatch):
    c, _ = _archived("rpc")
    for name in ("items_for_versions", "version_ids_of_items", "fingerprint_rows_for_versions",
                 "fingerprints_with_opening", "fingerprints"):
        monkeypatch.setattr(store, name, lambda *a, _n=name, **k: pytest.fail(f"新稿查重不该调 {_n}"))
    out = core.check_drafts(c, PROJ, [{"title": "一篇新稿", "body": BODY_C}], user_id=ME)
    assert out["results"][0]["status"] == "pass" and "replaces_version_id" not in out["results"][0]


def test_a_zero_score_hit_on_the_old_version_is_dropped_without_pulling_history(monkeypatch):
    """库里那一路某一路的最佳命中是旧版、可值本来就是 0(四字串一个都没对上, 只是排在第一): 去掉旧版之后也只会是 0,
    不用把项目指纹拉回来重比; 但旧版的标题要摘掉, 不然通过的稿子会把它自己的旧版报成「最接近的参照」。"""
    own = {0: [{"id": "fp-own", "title": "秋招等回音", "opening_hash": fp.opening_hash(BODY_A),
                "ngram_hashes": fp.ngram_hashes(BODY_A), "title_embedding": None, "embedding_model": None}]}
    hit = core.HistHit(0.0, None, 0.0, "秋招等回音", False, None, 0.0, None, 0)
    monkeypatch.setattr(store, "fingerprints", lambda *a, **k: pytest.fail("值是 0 的那一路不该重拉历史"))
    probe, cap = core._without_own_versions(object(), PROJ, lambda i: hit, own, [fp.opening_hash(BODY_C)],
                                            [set(fp.ngram_hashes(BODY_C))], None)
    assert probe(0).j_title is None and probe(0).jac == 0.0
    assert cap == 0, "没拉历史就谈不上截断"


def test_a_truncated_recheck_on_the_pushdown_path_says_so(monkeypatch):
    """库里那一路比的是全量, 但改稿那几篇排除旧版重比时要把历史拉回来(store.fingerprints 有条数上限)。拉不全的时候原来
    只记一行日志、history_truncated 照报 False —— 写手听不到「更早的没参与查重」(2026-10-30 复审)。上限调到 1 条来钉。"""
    c, v1 = _archived("rpc")
    _ingested(c, "飞书上的另一篇", BODY_A, opening_hash="0" * 16)      # 同 body 那条: 一定要重拉历史
    real = store.fingerprints
    monkeypatch.setattr(store, "fingerprints", lambda *a, **k: real(*a, **{**k, "limit": 1}))
    out = core.check_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A2, "replaces_version_id": v1}], user_id=ME)
    summ = out["summary"]
    assert summ["history_truncated"] is True, summ
    warn = summ["history_truncated_warning"]
    assert "改过的那几篇这次只和最近的 1 篇比了" in warn and "更早的没参与查重" in warn, warn
    assert summ["history_size"] == 2, "history_size 照旧是库里的全量"
    from tests.test_writer_facing_text import _assert_writer_safe
    _assert_writer_safe(summ, "check_drafts[revision truncated]")

    # 不截断(默认上限)、新稿(不重拉)都照旧是 False
    monkeypatch.setattr(store, "fingerprints", real)
    again = core.check_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A2, "replaces_version_id": v1}],
                              user_id=ME)["summary"]
    assert again["history_truncated"] is False and "history_truncated_warning" not in again
    fresh = core.check_drafts(c, PROJ, [{"title": "一篇新稿", "body": BODY_C}], user_id=ME)["summary"]
    assert fresh["history_truncated"] is False
