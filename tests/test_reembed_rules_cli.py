"""``deskcore.cli reembed-rules`` —— 给规则补向量的运维入口。

这个命令的价值全在**它失败时说了什么**。补向量是一件"没补上也完全看不出来"
的事: 规则照样在库里、照样注入, 只是 ``filter_soft_by_relevance`` 对没向量的
一律放行, 于是相关性过滤静默失效, 技艺库变成按「谁票多谁新」筛而不是按
「这次写什么」筛。

⚠️ 这里钉的是【被禁止的形态】:

  · 没配 GOOGLE_API_KEY / pgvector 没建, 却报成功(退出码 0)
  · 一轮补不完就当补完了(``backfill_memory_embeddings`` 一次最多 50 条)
  · 查到了缺向量的行、一条都没补上, 却继续空转 —— 那是**死循环**, 因为
    每轮都会查到同一批行
  · 转满上限还没补完, 却报成功
"""

from __future__ import annotations

import pytest

from deskcore import cli


class _FakeDb:
    """按脚本依次返回 backfill_memory_embeddings 的结果。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def backfill_memory_embeddings(self, client, user_id, max_rows=50):
        self.calls.append({"user_id": user_id, "max_rows": max_rows})
        return self.script.pop(0) if self.script else {"status": "noop"}


@pytest.fixture
def run(monkeypatch):
    """跑一次 CLI, 返回 (退出码, 打印出来的东西, 假 db)。"""
    def _run(script, argv_extra=()):
        fake = _FakeDb(script)
        monkeypatch.setitem(__import__("sys").modules, "db", fake)
        monkeypatch.setattr(cli, "_print", lambda o: print(o), raising=True)

        import deskcore.core as _core
        monkeypatch.setattr(_core, "sb", lambda: object(), raising=True)

        code = cli.main(["reembed-rules", "--user", "u-1", *argv_extra])
        return code, fake
    return _run


def test_no_api_key_is_a_failure_not_a_success(run, capsys):
    """没配 GOOGLE_API_KEY 必须是**非零退出码**。

    ``db.backfill_memory_embeddings`` 的 docstring 明说: 这几种情况以前一律
    塌成 int 0, 于是"点了按钮显示没有需要补算的记忆"既包含真的不用补, 也包含
    没配 key 这种真错误。这个命令不许再塌回去。
    """
    code, _ = run([{"status": "no_embedding_sdk"}])
    assert code == 1
    assert "no_embedding_sdk" in capsys.readouterr().out


def test_schema_missing_is_a_failure(run):
    code, _ = run([{"status": "schema_missing", "hint": "pgvector 迁移没跑"}])
    assert code == 1


def test_it_keeps_paging_until_there_is_nothing_left(run):
    """一轮最多 50 条 —— 一次调用补不完, 必须翻页直到 noop。"""
    code, fake = run([
        {"status": "ok", "updated": 50},
        {"status": "ok", "updated": 50},
        {"status": "ok", "updated": 7},
        {"status": "noop"},
    ])
    assert code == 0
    assert len(fake.calls) == 4, "没翻页 —— 补了一轮就当补完了"


def test_a_round_that_updates_nothing_stops_instead_of_spinning(run, capsys):
    """查到了缺向量的行却一条都没补上 → **停**, 不许继续转。

    ⚠️ 这是死循环的入口。``backfill_memory_embeddings`` 对算不出向量的行是
    ``continue`` 跳过的, 所以完全可能 ``status='ok'`` 而 ``updated=0``, 同时
    缺向量的行一条没少 —— 下一轮查到的还是同一批。"补不完就一直转"在这里
    会一直转下去, 而且每轮都在花 embedding 的钱。
    """
    code, fake = run([{"status": "ok", "updated": 0}] * 5)
    assert code == 1
    assert len(fake.calls) == 1, "updated=0 之后还在转 —— 这是死循环"
    assert "stalled" in capsys.readouterr().out


def test_hitting_the_round_cap_is_reported_as_not_finished(run, capsys):
    """转满上限还没补完 = **没补完**, 不许报成功。"""
    code, fake = run([{"status": "ok", "updated": 1}] * 10,
                     argv_extra=["--max-rounds", "3"])
    assert code == 1
    assert len(fake.calls) == 3
    out = capsys.readouterr().out
    assert "max_rounds_reached" in out and "没补完" in out


def test_nothing_to_do_is_success_but_says_so(run, capsys):
    """真的没有缺向量的行 = 成功, 但要说清楚是"没有"而不是"补好了"。"""
    code, _ = run([{"status": "noop"}])
    assert code == 0
    assert "没有缺向量的规则" in capsys.readouterr().out


def test_batch_size_reaches_the_db_layer(run):
    _, fake = run([{"status": "noop"}], argv_extra=["--batch", "17"])
    assert fake.calls[0]["max_rows"] == 17


def test_it_only_touches_the_user_you_named(run):
    """规则按 user_id 归属。一次只补一个人 —— 别把别人的库也算上。"""
    _, fake = run([{"status": "ok", "updated": 3}, {"status": "noop"}])
    assert {c["user_id"] for c in fake.calls} == {"u-1"}
