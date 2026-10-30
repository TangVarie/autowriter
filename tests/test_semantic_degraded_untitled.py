"""空标题的历史行不算"缺标题向量"(2026-10-30 发布收尾)。

形态: ``_gate`` 把"历史行没有本模型的 title_embedding"当成标题语义那一路没跑全的证据(这一条本身是对的,
见 test_embedding_model_isolation)。可空标题的行**永远**算不出标题向量 —— embed_texts 对空串回 None,
reembed 也特意不扫它们 —— 原来照样算进缺的那一边。于是一个项目只要补录过一条空标题的帖(生产上 18 条这样的行里
17 条是补录副本), 之后**每一批** check_drafts / score_drafts 都报 semantic_degraded, 写手每次都听一遍
「标题语义查重没跑全」, 而那一路其实对每一条有标题的历史都比过了。

钉的是两头:

  1. 空标题、没向量的历史行不触发 degraded, 也不算进 history_missing_embedding(两条路径: 下推 / Python 兜底);
  2. **有标题**、没向量的行照样触发 —— 那是真没比, 不能跟着一起放掉。
"""

from __future__ import annotations

import pytest

from deskcore import core, store
from deskcore import fingerprint as fp
from tests.fakes import FakeClient

import dedup

PID = "11111111-1111-1111-1111-111111111111"
UID = "22222222-2222-2222-2222-222222222222"
_PROJECTS = [{"id": PID, "name": "p", "owner_id": UID}]
CUR = dedup.EMBEDDING_MODEL
OLD = "text-embedding-004"

BODY = ("下班路上想到一件事, 秋招最费时间的不是投简历, 而是等一个不会来的回音, "
        "等到后来连刷新邮箱都成了一种自我安慰。") * 2


def _fp(rid, *, title=None, model=CUR, vec=(0.4, -0.3)):      # 与新稿的 [0.3, 0.4] 正交: 标题语义不撞
    return {"id": rid, "project_id": PID, "title": f"标题{rid}" if title is None else title,
            "opening": "", "title_embedding": list(vec) if vec else None,
            "embedding_model": model if vec else None, "opening_hash": None,
            "ngram_hashes": [], "created_at": "2026-10-30T00:00:00Z"}


def _untitled(rid):
    """补录副本那种: 标题是空串, 没有向量(也不可能有)。"""
    return _fp(rid, title="", vec=None)


@pytest.fixture(autouse=True)
def _vectors(monkeypatch):
    monkeypatch.setattr(dedup, "embeddings_available", lambda: True)
    monkeypatch.setattr(dedup, "embed_texts", lambda ts: [[0.3, 0.4] if t.strip() else None for t in ts])


def _python_path(history) -> FakeClient:
    return FakeClient(rows={"projects": _PROJECTS, "draft_fingerprints": list(history)})   # 没有 RPC → Python 兜底


def _pushdown(history) -> FakeClient:
    """下推路径: RPC 只回比对结果, 计数走 store.fingerprint_stats。"""
    return FakeClient(rows={"projects": _PROJECTS, "draft_fingerprints": list(history)},
                      rpc_impl={"deskcore_check_drafts": lambda a: [
                          {"idx": i, "best_sim": 0, "sim_title": None, "best_j": 0, "j_title": None,
                           "open_exact": False, "open_title": None, "best_c": 0, "c_title": None, "c_sample": 0}
                          for i in range(len(a["_rows"]))]})


PATHS = pytest.mark.parametrize("make", [_python_path, _pushdown], ids=["python", "pushdown"])


# ══════════════════════════════════════════════════════════════════════
# 1 · 空标题的历史行不算缺
# ══════════════════════════════════════════════════════════════════════

@PATHS
def test_an_untitled_history_row_does_not_degrade(make):
    """⚠️ 这条是这个文件存在的理由: 一条补录进来的空标题帖, 不许让之后每一批都报「没跑全」。"""
    c = make([_fp("a"), _fp("b"), _untitled("ingest-copy")])
    out = core.check_drafts(c, PID, [{"title": "新标题", "body": BODY}], user_id=UID)
    s = out["summary"]
    assert s["semantic_degraded"] is False, s
    assert "degraded_note" not in s, s
    assert s["history_size"] == 3, "空标题的行照样是历史(开头 / 四字串照比), 只是不算缺向量"
    assert s["history_with_embedding"] == 2
    assert s["history_missing_embedding"] == 0, s
    assert s["history_untitled"] == 1


@PATHS
def test_a_history_of_only_untitled_rows_is_not_degraded(make):
    """历史里一条有标题的都没有: 标题语义这一路没有可比的对象, 不是"没跑"。"""
    c = make([_untitled("x"), _untitled("y")])
    out = core.check_drafts(c, PID, [{"title": "新标题", "body": BODY}], user_id=UID)
    s = out["summary"]
    assert s["semantic_degraded"] is False, s
    assert (s["history_with_embedding"], s["history_missing_embedding"], s["history_untitled"]) == (0, 0, 2)


def test_score_drafts_gate_summary_uses_the_same_rule(monkeypatch):
    """score_drafts 自带同一套闸(_gate), 空标题的历史行在那边一样不算缺。"""
    c = _python_path([_fp("a"), _untitled("ingest-copy")])
    c.rpc_impl["deskcore_commit_fingerprints"] = lambda args: [
        {"idx": i, "status": core.COMMIT_STATUS_INSERTED, "collided_with": None, "detail": None}
        for i in range(len(args.get("_rows") or []))]
    c.rows["projects"][0].update({"calibration_notes": "", "tactics": "[]", "custom_roles": []})
    out = core.commit_drafts(c, PID, [{"title": "新标题", "body": BODY}], user_id=UID)
    assert out["written"] == 1
    assert out["gate_summary"]["semantic_degraded"] is False, out["gate_summary"]
    assert out["gate_summary"]["history_missing_embedding"] == 0


# ══════════════════════════════════════════════════════════════════════
# 2 · 有标题、没向量的照样算缺
# ══════════════════════════════════════════════════════════════════════

@PATHS
def test_a_titled_row_without_a_vector_still_degrades(make):
    """有标题却没向量 = 标题语义那一路真的没比这一条。不能跟着空标题一起放掉。"""
    c = make([_fp("a"), _fp("no-vec", vec=None), _untitled("ingest-copy")])
    out = core.check_drafts(c, PID, [{"title": "新标题", "body": BODY}], user_id=UID)
    s = out["summary"]
    assert s["semantic_degraded"] is True, s
    assert s["history_missing_embedding"] == 1, "只数有标题的那一条, 空标题的不算"
    assert s["history_untitled"] == 1
    assert s["degraded_note"], s


@PATHS
def test_a_titled_row_with_another_models_vector_still_degrades(make):
    """换模型作废的旧向量也是"缺"(test_embedding_model_isolation 那条口径), 有标题就照算。"""
    c = make([_fp("a"), _fp("old", model=OLD), _untitled("ingest-copy")])
    out = core.check_drafts(c, PID, [{"title": "新标题", "body": BODY}], user_id=UID)
    assert out["summary"]["semantic_degraded"] is True
    assert out["summary"]["history_missing_embedding"] == 1


# ══════════════════════════════════════════════════════════════════════
# 3 · 计数的口径
# ══════════════════════════════════════════════════════════════════════

def test_stats_count_untitled_rows_without_vectors_only():
    """第三个数 = 标题是空串**且**没有向量。有标题没向量的不在里面(那是真缺); 空标题却有向量的(不该有)
    已经数在 with_vec 里, 不能再减一次 —— 否则会把一条真缺的抵掉。"""
    sb = FakeClient(rows={"draft_fingerprints": [
        _fp("titled-vec"), _fp("titled-no-vec", vec=None), _untitled("untitled"),
        _fp("untitled-vec", title=""),
    ]})
    total, with_vec, untitled = store.fingerprint_stats(sb, PID, CUR)
    assert (total, with_vec, untitled) == (4, 2, 1)
    assert total - with_vec - untitled == 1, "剩下的那一条就是有标题没向量的"


def test_the_python_path_counts_the_same_way():
    """Python 兜底路径与 fingerprint_stats 同一口径(两条路径报出去的 summary 不许不一样)。"""
    rows = [_fp("titled-vec"), _fp("titled-no-vec", vec=None), _untitled("untitled"),
            _fp("untitled-vec", title="")]
    out_py = core.check_drafts(_python_path(rows), PID, [{"title": "新标题", "body": BODY}], user_id=UID)
    out_db = core.check_drafts(_pushdown(rows), PID, [{"title": "新标题", "body": BODY}], user_id=UID)
    keys = ("history_size", "history_with_embedding", "history_missing_embedding", "history_untitled",
            "semantic_degraded")
    assert {k: out_py["summary"][k] for k in keys} == {k: out_db["summary"][k] for k in keys}
    assert out_py["summary"]["history_missing_embedding"] == 1


def test_untitled_rows_still_take_part_in_the_deterministic_signals():
    """不算缺向量 ≠ 不参与查重: 空标题的补录副本照样用开头 / 四字串挡住照搬它的新稿。"""
    copy = {**_untitled("ingest-copy"), "opening": fp.opening_of(BODY),
            "opening_hash": fp.opening_hash(BODY), "ngram_hashes": fp.ngram_hashes(BODY)}
    out = core.check_drafts(_python_path([copy]), PID, [{"title": "换了个标题", "body": BODY}], user_id=UID)
    assert out["results"][0]["status"] == "reject", out["results"][0]
    assert out["summary"]["semantic_degraded"] is False
