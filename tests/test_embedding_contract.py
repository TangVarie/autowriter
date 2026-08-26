"""`dedup.embed_texts` 的契约 —— 2026-08-26 首次真部署踩出来的三条。

那次的形状值得完整记一遍, 因为三层加起来才让它这么难查:

  1. 写死的模型名 ``text-embedding-004`` 被 Google **下线**了, API 回 404;
  2. ``embed_texts`` 是裸 ``except Exception: return None``, **一行日志都不打**;
  3. 于是现象只是 ``check_drafts`` 的 ``semantic_degraded`` 悄悄变 true ——
     服务健康、``/health`` 全绿、Railway 日志干干净净, 四路信号里最贵的那一路
     从此不发言, 而没有任何东西说过一句话。

最后是靠人肉 curl 打 Google 的 API 才问出「模型 404」的。这个文件钉的就是
"下次别再这样"。

用例分三组:

  1. **维度**   —— 模型默认回 3072 维, 库里是 vector(768)。截断必须真的发生,
                   而且长度不对时必须**整批作废**;
  2. **留痕**   —— 每一条失败路径都要留下日志。降级可以静默, **降级的原因不行**;
  3. **一致性** —— 代码里的模型名 / 维度与 schema、文档不许漂开。
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

import pytest

import dedup

REPO_ROOT = Path(__file__).resolve().parent.parent


class _FakeResp:
    def __init__(self, vecs):
        self.embeddings = [type("E", (), {"values": v})() for v in vecs]


class _FakeModels:
    """记下调用参数, 按 dims 造回一批向量。"""

    def __init__(self, dims, n=None, boom=None):
        self.dims, self.n, self.boom = dims, n, boom
        self.calls = []

    def embed_content(self, *, model, contents, config=None):
        self.calls.append({"model": model, "n": len(contents), "config": config})
        if self.boom:
            raise self.boom
        count = self.n if self.n is not None else len(contents)
        return _FakeResp([[0.1] * self.dims for _ in range(count)])


class _FakeClient:
    def __init__(self, models):
        self.models = models


@pytest.fixture
def fake(monkeypatch):
    def _install(dims=768, n=None, boom=None):
        m = _FakeModels(dims, n, boom)
        monkeypatch.setattr(dedup, "_get_client", lambda: _FakeClient(m))
        return m
    return _install


# ══════════════════════════════════════════════════════════════════════
# 1 · 维度
# ══════════════════════════════════════════════════════════════════════

def test_asks_the_api_to_truncate_to_our_dim(fake):
    """必须显式要 768 维。

    ``gemini-embedding-001`` 默认回 **3072**，而 `draft_fingerprints.title_embedding`
    / `versions.embedding` / `memories.embedding` 三列都是 `vector(768)`。
    不要这个参数的话：写库直接报维度不符，而**只比不写**的路径更坏——
    `cosine_similarity` 对 `len(a) != len(b)` 返回 `0.0`，查重变哑弹且不报错。
    """
    m = fake(dims=768)
    out = dedup.embed_texts(["标题一", "标题二"])

    assert out is not None and len(out) == 2
    cfg = m.calls[0]["config"]
    assert cfg and cfg.get("output_dimensionality") == dedup.EMBEDDING_DIM, cfg


def test_wrong_dim_voids_the_whole_batch(fake, caplog):
    """维度不符 → 整批 None，**不许把不对的向量传下去**。

    传下去的后果不是报错，是"查重跑了、全 pass、看着一切正常"而一条都抓不到。
    宁可退化成纯确定性（那条路仍有效，且会在 summary 里报出来）。
    """
    fake(dims=3072)                     # 忘了截断 / 参数被谁改掉
    with caplog.at_level(logging.ERROR, logger="dedup"):
        out = dedup.embed_texts(["标题"])

    assert out is None
    assert "维度不符" in caplog.text, caplog.text
    # 两个数都要报出来 —— 只说"不符"不够, 排查的人需要知道差在哪
    assert "768" in caplog.text and "3072" in caplog.text, caplog.text


# ══════════════════════════════════════════════════════════════════════
# 2 · 每条失败路径都要留痕
# ══════════════════════════════════════════════════════════════════════

def test_api_failure_is_logged_not_swallowed(fake, caplog):
    """模型下线 / key 失效 / 配额用尽 —— 都从这条路出来，必须留下日志。

    ⚠️ 这条是这次事故的**正中靶心**：原来是裸 `except Exception: return None`，
    于是 `text-embedding-004` 被下线之后，日志里一个字都没有。
    """
    fake(boom=RuntimeError("404 models/xxx is not found for API version v1beta"))
    with caplog.at_level(logging.ERROR, logger="dedup"):
        out = dedup.embed_texts(["标题"])

    assert out is None
    assert caplog.records, "embed_content 抛异常却没有留下任何日志"
    assert "404" in caplog.text, caplog.text          # 原始错误要能看到


def test_no_client_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(dedup, "_get_client", lambda: None)
    with caplog.at_level(logging.WARNING, logger="dedup"):
        assert dedup.embed_texts(["x"]) is None
    assert caplog.records, "没有 client 时也要留一句，否则等于静默降级"


def test_every_failure_path_logs():
    """AST：`embed_texts` 里每个 `return None` 之前都得有日志调用。

    钉的是【被禁止的形态】——"悄悄返回 None"。这个函数的失败是**设计上要
    降级**的，所以它天然倾向于把异常吞掉；正因为如此，"降级要留痕"必须由
    断言守着，不能靠自觉。
    """
    tree = ast.parse((REPO_ROOT / "dedup.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "embed_texts")

    logged = {n.lineno for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and isinstance(n.func.value, ast.Name) and n.func.value.id == "logger"}
    bare = [n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Return)
            and isinstance(n.value, ast.Constant) and n.value.value is None
            and not any(abs(n.lineno - ln) <= 8 for ln in logged)]

    assert not bare, (
        f"embed_texts 第 {bare} 行是【无声的】return None —— "
        "降级可以静默, 降级的原因不行(2026-08-26 就是这么查了半天)")


# ══════════════════════════════════════════════════════════════════════
# 3 · 模型名 / 维度不许和 schema、文档漂开
# ══════════════════════════════════════════════════════════════════════

def test_retired_model_name_is_gone_everywhere():
    """`text-embedding-004` 已下线，仓库里不许再有人把它当成当前模型。

    只允许出现在**讲这段历史**的地方（注释 / 文档里点名它"已下线"）。
    """
    stale = []
    for f in list(REPO_ROOT.glob("*.py")) + list((REPO_ROOT / "migrations").glob("*.sql")):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if "text-embedding-004" not in line:
                continue
            if "下线" in line or "已下线" in line:      # 讲历史的那几行
                continue
            stale.append(f"{f.name}:{i}")
    assert not stale, f"这些地方还写着已下线的模型名: {stale}"


def test_dim_matches_the_schema():
    """`EMBEDDING_DIM` 必须与三列 `vector(N)` 一致。

    改模型时最容易漏的就是这条——而漏了之后 `cosine_similarity` 静默返回 0.0。
    """
    sql = (REPO_ROOT / "migrations" / "000_baseline.sql").read_text(encoding="utf-8")
    dims = {int(m) for m in re.findall(r"vector\((\d+)\)", sql)}
    assert dims == {dedup.EMBEDDING_DIM}, (
        f"schema 里的向量维度是 {sorted(dims)}, 而 dedup.EMBEDDING_DIM="
        f"{dedup.EMBEDDING_DIM} —— 两边必须一致")
