"""审计 COR-014 的后续 · 改了 ``normalize`` 之后的指纹全量重算。

``normalize`` 是**存量指纹的计算口径**。它一变, 库里的 ``opening_hash`` /
``ngram_hashes`` 就全部作废: 新稿算出来的四字串和历史对不上, 查重在过渡期反而
更弱, 而且**不报错**。``backfill`` 补不了这个 —— 它按 ``version_id`` 幂等跳过,
只管"没有的行"。

这个重算能修到什么程度是由**指纹表存了什么**决定的, 不是想修多少就修多少:

  · ``opening_hash`` 每一行都能修 —— 它本来就是 ``sha16(normalize(opening))``,
    而 ``opening`` 原样存着。这一路是"单独就判死"的最强信号, 全修回来是关键。
  · ``ngram_hashes`` 只有 ``version_id`` 非空的行能修(正文在 versions 表里)。
    WorkBuddy 经 ``commit_drafts`` 写进来的行正文已经不存在了。

**修不回来的那部分必须被报出来**, 否则就是又一次"看起来做完了"。
"""

from __future__ import annotations

from deskcore import core
from deskcore import fingerprint as fp
from tests.fakes import FakeClient

PID = "aaaaaaaa-0000-0000-0000-000000000001"
BODY = "他说“这个真的好用”，回购了三次。第二段讲的是日常使用里的细节和感受。" * 6


def _client(rows):
    """指纹行 + 对应的 versions 行。"""
    versions = [{"id": r["version_id"], "body": r.pop("_body")}
                for r in rows if r.get("version_id") and "_body" in r]
    return FakeClient(rows={"draft_fingerprints": rows, "versions": versions})


def test_opening_hash_is_repaired_for_every_row():
    """开头指纹每一行都能修 —— 包括正文已经丢了的那些。

    这一路是**单独就判死**的最强信号, 也是最容易被口径变更悄悄弄坏的:
    两篇一模一样的稿子, 一篇旧口径一篇新口径, 开头哈希就对不上了。
    """
    opening = "他说“这个真的好用”，回购了三次"
    stale = "0" * 16                                   # 旧口径留下的哈希
    rows = [
        {"id": "f1", "project_id": PID, "version_id": None,
         "opening": opening, "opening_hash": stale, "ngram_hashes": ["old"]},
        {"id": "f2", "project_id": PID, "version_id": "v2", "_body": BODY,
         "opening": opening, "opening_hash": stale, "ngram_hashes": ["old"]},
    ]
    sb = _client(rows)
    out = core.recompute_fingerprints(sb, PID)

    want = fp.sha16(fp.normalize(opening))
    for r in sb.rows["draft_fingerprints"]:
        assert r["opening_hash"] == want, r
    assert out["scanned"] == 2 and out["rewritten"] == 2


def test_ngrams_are_repaired_only_where_the_body_survives():
    rows = [
        {"id": "f1", "project_id": PID, "version_id": None,
         "opening": "开头一", "opening_hash": "x", "ngram_hashes": ["old"]},
        {"id": "f2", "project_id": PID, "version_id": "v2", "_body": BODY,
         "opening": "开头二", "opening_hash": "x", "ngram_hashes": ["old"]},
    ]
    sb = _client(rows)
    out = core.recompute_fingerprints(sb, PID)

    by_id = {r["id"]: r for r in sb.rows["draft_fingerprints"]}
    assert by_id["f2"]["ngram_hashes"] == fp.ngram_hashes(BODY)
    # 正文没了的那行, 四字串**保持原样** —— 不能拿空数组去覆盖, 那等于把这行
    # 从查重里删掉, 比留着旧口径更糟。
    assert by_id["f1"]["ngram_hashes"] == ["old"]
    assert out["ngram_unrecoverable"] == 1


def test_unrecoverable_rows_are_reported_loudly():
    """修不回来的部分必须出现在返回值里, 而且要说清后果。

    不报的话就是又一次"看起来做完了" —— 而这次的后果是查重对那批内容一直偏低。
    """
    rows = [{"id": f"f{i}", "project_id": PID, "version_id": None,
             "opening": "开头", "opening_hash": "x", "ngram_hashes": ["old"]}
            for i in range(3)]
    out = core.recompute_fingerprints(_client(rows), PID)
    assert out["ngram_unrecoverable"] == 3
    assert "warning" in out and "重新 commit" in out["warning"]


def test_no_warning_when_everything_was_repairable():
    rows = [{"id": "f1", "project_id": PID, "version_id": "v1", "_body": BODY,
             "opening": "开头", "opening_hash": "x", "ngram_hashes": ["old"]}]
    out = core.recompute_fingerprints(_client(rows), PID)
    assert out["ngram_unrecoverable"] == 0 and "warning" not in out


def test_recompute_is_idempotent():
    """纯函数重算, 跑两遍结果一样 —— 运维命令必须敢重跑。"""
    rows = [{"id": "f1", "project_id": PID, "version_id": "v1", "_body": BODY,
             "opening": "他说“好”", "opening_hash": "x", "ngram_hashes": ["old"]}]
    sb = _client(rows)
    core.recompute_fingerprints(sb, PID)
    first = dict(sb.rows["draft_fingerprints"][0])
    core.recompute_fingerprints(sb, PID)
    assert sb.rows["draft_fingerprints"][0] == first


def test_empty_opening_stays_empty_not_a_constant_hash():
    """空开头必须回空串, 不能回 ``sha16("")``。

    否则所有 title-only 的行会拿到**同一个**非空哈希, 之后每条 title-only 新稿
    都被判成"正文开头与历史稿完全一致"而 reject —— 而 opening_exact 是单独就
    判死的强信号, 没有任何东西兜得住这个误伤。(codex review 记过这一条,
    重算这条路径**也必须守它**。)
    """
    rows = [{"id": "f1", "project_id": PID, "version_id": None,
             "opening": "   ", "opening_hash": "deadbeefdeadbeef",
             "ngram_hashes": []}]
    sb = _client(rows)
    core.recompute_fingerprints(sb, PID)
    assert sb.rows["draft_fingerprints"][0]["opening_hash"] == ""


def test_cli_exposes_the_command():
    """运维要跑得着它 —— 光有函数没有子命令等于没有。"""
    import deskcore.cli as cli
    import argparse
    import contextlib
    import io
    with contextlib.redirect_stderr(io.StringIO()), \
            contextlib.redirect_stdout(io.StringIO()):
        try:
            cli.main(["recompute-fingerprints"])       # 缺 --project, 应报参数错
        except SystemExit as exc:
            assert exc.code == 2, exc.code
        else:
            raise AssertionError("recompute-fingerprints 子命令不存在或没要求 --project")
    assert isinstance(argparse.ArgumentParser(), argparse.ArgumentParser)
