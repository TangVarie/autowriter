"""换 embedding 模型时, 老向量必须**退出比对**而不是混着算。

codex review · aw#65 P1。形态值得完整记一遍, 因为它骗过了当时在场的每一道检查:

  · ``draft_fingerprints.embedding_model`` 从 migrations/001 起就存在, 它的
    COMMENT 白纸黑字写着"换 embedding 供应商时唯一的救命稻草" ——
    **而四路查重里没有任何一路读过它**。SQL 侧和 Python 侧都是拿
    ``title_embedding IS NOT NULL`` 当"可比";
  · 维度守卫拦不住: text-embedding-004 和 gemini-embedding-001 **都是 768 维**,
    写库不报错、长度校验也过;
  · 后果是**双向**的 —— 跨模型余弦是噪声, 真重复算出来很低(放行), 无关的算出来
    很高(误杀);
  · 而 ``semantic_degraded`` 报 **false**: 历史行确实"有向量"。这一路不但失灵,
    还在报告里说自己跑过了。

用例分四组, 一组钉一条出路:

  1. **比对**   —— 别的模型 / 来路不明的向量都不参与, 且这件事要被报出去;
  2. **回填**   —— 不许给来路不明的历史向量贴上当前模型的标签;
  3. **重算**   —— reembed 必须够得着那些行(否则它们卡在没有出口的状态里);
  4. **SQL 侧** —— migrations/008 与 Python 侧同一口径, 且是干净的 REPLACE。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fakes import FakeClient
import dedup
from deskcore import core, store

REPO_ROOT = Path(__file__).resolve().parent.parent

PID = "11111111-1111-1111-1111-111111111111"
UID = "22222222-2222-2222-2222-222222222222"
# 归属校验(COR-015)是每个工具的第一道闸 —— 夹具里必须有这一行,
# 否则用例连比对逻辑都走不到。
_PROJECTS = [{"id": PID, "name": "p", "owner_id": UID}]

CUR = dedup.EMBEDDING_MODEL
OLD = "text-embedding-004"          # 已下线的那个


def _fp(rid, model, vec=(0.1, 0.2)):
    return {"id": rid, "project_id": PID, "title": f"标题{rid}",
            "opening": "", "title_embedding": list(vec) if vec else None,
            "embedding_model": model, "opening_hash": None,
            "ngram_hashes": [], "created_at": "2026-08-26T00:00:00Z"}


# ══════════════════════════════════════════════════════════════════════
# 1 · 比对: 只认本模型
# ══════════════════════════════════════════════════════════════════════

def test_python_path_ignores_other_models_vectors(monkeypatch):
    """Python 兜底路径只把**本模型**的向量算进比对。

    钉的是【被禁止的形态】——"库里有向量就拿来算余弦"。混着算不会报错, 只会让
    硬闸的结论变成噪声, 而这正是最难发现的一种坏。
    """
    rows = {"projects": _PROJECTS,
            "draft_fingerprints": [_fp("a", OLD), _fp("b", None),
                                   _fp("c", CUR)]}
    sb = FakeClient(rows=rows)          # 没有 RPC → 走 Python 路径

    seen: list = []
    monkeypatch.setattr(dedup, "cosine_similarity",
                        lambda a, b: seen.append(b) or 0.0)
    monkeypatch.setattr(dedup, "embed_texts", lambda ts: [[0.1, 0.2]] * len(ts))
    monkeypatch.setattr(dedup, "embeddings_available", lambda: True)

    out = core.check_drafts(sb, PID, [{"title": "新标题", "body": "正文"}],
                            user_id=UID)

    assert len(seen) == 1, (
        f"比了 {len(seen)} 条 —— 老模型 / 来路不明的向量也被算进去了")
    assert out["summary"]["history_size"] == 3
    # 三条里只有一条可用 → 必须如实说这一路没比全, 而不是报 0 缺失。
    assert out["summary"]["history_with_embedding"] == 1
    assert out["summary"]["history_missing_embedding"] == 2


def test_stats_count_only_usable_vectors():
    """``fingerprint_stats`` 的"有向量"口径 = 有【本模型】的向量。

    按非 NULL 去数的话, 一个全是老模型行的项目会报 missing_embedding = 0,
    于是 semantic_degraded 是 false —— 而标题语义那一路一条都没真的比。
    """
    sb = FakeClient(rows={"draft_fingerprints":
                          [_fp("a", OLD), _fp("b", None), _fp("c", CUR)]})
    total, with_vec = store.fingerprint_stats(sb, PID, CUR)
    assert (total, with_vec) == (3, 1)


def test_pushdown_payload_carries_the_model():
    """下推给 SQL 的每条都要带 ``embedding_model``。

    不带的话 migrations/008 的函数会走兼容路径(比全部非空向量) —— 也就是退回
    这条 P1 说的那个洞, 而且**静默**。
    """
    sb = FakeClient(rows={"projects": _PROJECTS,
                          "draft_fingerprints": []},
                    rpc_impl={"deskcore_check_drafts": lambda a: [
                        {"idx": 0, "best_sim": 0, "best_j": 0, "best_c": 0,
                         "c_sample": 99, "open_exact": False}],
                        "deskcore_fingerprint_counts": lambda a: []})
    import unittest.mock as _m
    with _m.patch.object(dedup, "embed_texts", lambda ts: [[0.1]] * len(ts)), \
         _m.patch.object(dedup, "embeddings_available", lambda: True):
        core.check_drafts(sb, PID, [{"title": "t", "body": "b"}], user_id=UID)

    args = next(a for n, a in sb.rpc_calls if n == "deskcore_check_drafts")
    assert args["_rows"][0]["embedding_model"] == CUR, args["_rows"][0]


# ══════════════════════════════════════════════════════════════════════
# 2 · 回填: 不许给来路不明的向量贴标签
# ══════════════════════════════════════════════════════════════════════

def test_backfill_never_labels_legacy_vectors_with_the_current_model(monkeypatch):
    """``versions.embedding`` 没有模型标记, 复用它就必须记 NULL。

    ⚠️ 这是这条 P1 里**最能悄悄毁掉数据**的一处: 贴错标签之后, 那些行会被
    当成"本模型产的"参与比对, 而且再也不会被 reembed 挑出来重算 —— 库里从此
    混着两套向量空间, 没有任何东西能分辨。
    """
    monkeypatch.setattr(dedup, "embeddings_available", lambda: False)
    sb = FakeClient(rows={
        "draft_fingerprints": [],
        "items": [{"id": "i1", "project_id": PID, "user_id": "u"}],
        "versions": [{"id": "v1", "item_id": "i1", "title": "老标题",
                      "body": "老正文", "embedding": [0.3, 0.4],
                      "version_num": 1}],
    })
    monkeypatch.setattr(store, "legacy_version_pages",
                        lambda c, p, limit=None, **k: iter([[{
                            "version_id": "v1", "user_id": "u",
                            "title": "老标题", "body": "老正文",
                            "embedding": [0.3, 0.4]}]]))

    out = core.backfill_fingerprints(sb, PID)

    written = sb.rows["draft_fingerprints"]
    assert written, "一行都没写进去"
    assert written[0]["title_embedding"] == [0.3, 0.4], "历史向量该复用"
    assert written[0]["embedding_model"] is None, (
        "复用了来路不明的历史向量却贴上了当前模型的标签 —— "
        f"{written[0]['embedding_model']!r}")
    # 而且要**说出来**, 否则 "embedded: 1" 看着像它能参与比对。
    assert out.get("unattested_embeddings") == 1, out
    assert "reembed" in out.get("unattested_note", ""), out


def test_backfill_computes_fresh_when_it_can(monkeypatch):
    """能调 embedding 时一律现算, 不复用 —— 现算的才敢写模型名。"""
    monkeypatch.setattr(dedup, "embeddings_available", lambda: True)
    monkeypatch.setattr(dedup, "embed_texts", lambda ts: [[0.9, 0.9]] * len(ts))
    sb = FakeClient(rows={"draft_fingerprints": []})
    monkeypatch.setattr(store, "legacy_version_pages",
                        lambda c, p, limit=None, **k: iter([[{
                            "version_id": "v1", "user_id": "u",
                            "title": "老标题", "body": "老正文",
                            "embedding": [0.3, 0.4]}]]))

    core.backfill_fingerprints(sb, PID)

    row = sb.rows["draft_fingerprints"][0]
    assert row["title_embedding"] == [0.9, 0.9], "该现算却复用了历史向量"
    assert row["embedding_model"] == CUR


# ══════════════════════════════════════════════════════════════════════
# 3 · 重算: 那些行必须有出口
# ══════════════════════════════════════════════════════════════════════

def test_reembed_reaches_stale_and_unattributed_rows():
    """reembed 的工作清单要覆盖三种"不可用", 不只是 NULL 向量。

    只扫 NULL 的话, 换模型之后老行**既进不了比对、又永远不会被重算** ——
    卡在一个没有出口的状态里, 而且不报错。修一半比不修更坏。
    """
    sb = FakeClient(rows={"draft_fingerprints": [
        _fp("none", None, vec=None),    # ① 从来没算过
        _fp("unattr", None),            # ② 有向量, 来路不明
        _fp("stale", OLD),              # ③ 有向量, 老模型
        _fp("good", CUR),               # ④ 当前模型 —— 不该被挑出来
    ]})
    got = store.fingerprints_needing_vectors(sb, PID, CUR)
    ids = {r["id"] for r in got}

    assert ids == {"none", "unattr", "stale"}, ids
    expect = {r["id"]: r["_expect"] for r in got}
    assert expect["none"] is store.VECTOR_ABSENT
    assert expect["unattr"] is None
    assert expect["stale"] == OLD, "CAS 要用行里读到的真实模型名"


def test_reembed_reports_stale_separately(monkeypatch):
    """"缺向量"和"换模型作废"是两件事, 报告里不能揉成一个数字。"""
    monkeypatch.setattr(dedup, "embeddings_available", lambda: True)
    monkeypatch.setattr(dedup, "embed_texts", lambda ts: [[0.5]] * len(ts))
    sb = FakeClient(rows={"draft_fingerprints": [
        _fp("none", None, vec=None), _fp("stale", OLD)]})

    out = core.reembed_fingerprints(sb, PID)
    assert out["missing_vector"] == 1 and out["stale_model"] == 1, out
    assert CUR in out.get("stale_model_note", ""), out


def test_set_vector_is_a_cas_not_a_blind_overwrite():
    """回写要 CAS —— 读和写之间别的进程刷过的行不许被旧批次盖掉。"""
    sb = FakeClient(rows={"draft_fingerprints": [_fp("x", CUR, vec=[9.0])]})

    # 以为它还是老模型 → 不该写进去
    assert store.set_fingerprint_vector(sb, "x", [1.0], CUR, expect=OLD) is False
    assert sb.rows["draft_fingerprints"][0]["title_embedding"] == [9.0]

    # 如实说它现在是 CUR → 写得进去
    assert store.set_fingerprint_vector(sb, "x", [1.0], CUR, expect=CUR) is True
    assert sb.rows["draft_fingerprints"][0]["title_embedding"] == [1.0]


# ══════════════════════════════════════════════════════════════════════
# 4 · SQL 侧与 Python 侧同一口径
# ══════════════════════════════════════════════════════════════════════

def test_migration_008_filters_by_model():
    raw = (REPO_ROOT / "migrations" / "008_embedding_model_isolation.sql"
           ).read_text(encoding="utf-8")
    # 只看**会被数据库执行的那部分** —— 解释"为什么不用 IS NOT DISTINCT FROM"
    # 的注释里必然出现那句话, 连注释一起查等于断言自己打自己。
    sql = "\n".join(re.sub(r"--.*$", "", ln) for ln in raw.splitlines())

    assert "f.embedding_model = _model" in sql, (
        "008 没有按模型过滤那一句 —— 那它什么都没改")
    # `=` 而不是 IS NOT DISTINCT FROM: embedding_model 为 NULL 的行是"来路不明",
    # 必须一起排除, 而 NULL = X 恰好就是 NULL(不匹配)。
    assert "IS NOT DISTINCT FROM" not in sql, (
        "用 IS NOT DISTINCT FROM 会把来路不明的行也算进来")
    # 签名不变 = 干净的 CREATE OR REPLACE, 不需要 DROP、不产生重载残留。
    assert "CREATE OR REPLACE FUNCTION" in sql
    assert "DROP FUNCTION" not in sql, (
        "008 不该 DROP —— 签名一个字都没动, REPLACE 就够")


def test_doctor_admits_it_cannot_probe_008():
    """008 换的是函数体, 从调用侧探不到 —— 必须如实说"探不到", 不许蒙。

    蒙一个 applied 正好复现这套东西要根治的形态: 报告说跑过了, 实际没跑, 而
    差别要等到某天有人换模型才暴露。
    """
    assert core.MIGRATION_EMBEDDING_ISOLATION.startswith("008_")
    assert "prosrc" in core.MIGRATION_008_PROBE_SQL, (
        "探不到的时候得把该跑的 SQL 交出来, 不能只说探不到")
    assert "embedding_model" in core.MIGRATION_008_PROBE_SQL


def test_the_column_comment_is_no_longer_a_lie():
    """001 的 COMMENT 说这一列是"换供应商时唯一的救命稻草" —— 现在得有人真用它。

    这条钉的不是某一行代码, 而是"文档承诺的能力必须在代码里能找到对应物"。
    """
    src = (REPO_ROOT / "deskcore" / "core.py").read_text(encoding="utf-8")
    assert "embedding_model" in src
    # 比对那两处都要按模型过滤
    assert 'h.get("embedding_model") == dedup.EMBEDDING_MODEL' in src, (
        "Python 兜底路径没有按模型过滤")
    store_src = (REPO_ROOT / "deskcore" / "store.py").read_text(encoding="utf-8")
    assert '.eq("embedding_model", model)' in store_src, (
        "fingerprint_stats 没有按模型过滤")
