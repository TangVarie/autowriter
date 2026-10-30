"""tv-sync 每晚顺手补标题向量(2026-10-30 发布收尾复审)。

sportsix 有一条有标题、没向量的指纹, 之后每一批 check_drafts / score_drafts 都报 ``semantic_degraded``(标题语义没比全);
补它的 ``reembed`` 是运维命令, 没有任何东西排着跑。``tv-sync --all`` 是每晚的 cron(railway.cron.json), 本来就带着
embedding 的 key, 同步完顺手补 —— core.reembed_after_sync。这里钉的是:

  · 补的是有标题、没有当前模型向量的三种行(缺向量 / 来路不明 / 换模型作废), 跨所有项目, 空标题的不碰;
  · 每晚最多 TV_SYNC_REEMBED_MAX 行, 缺向量的排在前面, 没轮到的明晚接着补;
  · 补完, 那个项目的下一批查重不再报 semantic_degraded;
  · 尽力而为: embedding 调用失败 / 读库出错照其它步骤的警告报、退出码 1, 这晚写进去的对照一行不动;
    没配 embedding 不算故障(一次库都不查);
  · 只在 --all 且不是 --dry-run 时跑(同删过期底稿)。

所有项目名、标题都是合成的。
"""

from __future__ import annotations

import pytest

from deskcore import cli, core, store
from tests.fakes import FakeClient
from tests.test_tv_sync import NOTES, TV, _client as tv_client

CUR = core.dedup.EMBEDDING_MODEL
OLD = "text-embedding-004"
P1 = "aaaaaaaa-0000-0000-0000-000000000001"
P2 = "aaaaaaaa-0000-0000-0000-000000000002"


def _fp(rid, project, title, *, vec=None, model=None, created="2026-10-29T00:00:00+00:00"):
    return {"id": rid, "project_id": project, "title": title, "opening": "", "title_embedding": vec,
            "embedding_model": model, "opening_hash": None, "ngram_hashes": [], "created_at": created}


@pytest.fixture
def embed_on(monkeypatch):
    calls: list[list[str]] = []

    def _embed(texts):
        calls.append(list(texts))
        return [[0.5, 0.5] if (t or "").strip() else None for t in texts]
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: True)
    monkeypatch.setattr(core.dedup, "embed_texts", _embed)
    return calls


def test_it_fills_titled_rows_across_projects_and_leaves_good_and_untitled_rows_alone(embed_on):
    c = FakeClient(rows={"draft_fingerprints": [
        _fp("absent", P1, "缺向量的那条"),                                 # sportsix 那一条的形状
        _fp("unattr", P2, "来路不明", vec=[0.1, 0.2]),
        _fp("stale", P2, "老模型", vec=[0.1, 0.2], model=OLD),
        _fp("good", P1, "好的", vec=[0.9, 0.1], model=CUR),
        _fp("untitled", P1, "", vec=None),                                 # 空标题: 算不出, 也不算缺
    ]})
    out = core.reembed_after_sync(c)
    assert (out["pending"], out["fixed"], out["failed"]) == (3, 3, 0), out
    assert out["missing_vector"] == 1 and out["stale_model"] == 2 and out["projects"] == 2
    assert out["capped"] is False and "error" not in out
    by_id = {r["id"]: r for r in c.rows["draft_fingerprints"]}
    for rid in ("absent", "unattr", "stale"):
        assert by_id[rid]["title_embedding"] == [0.5, 0.5] and by_id[rid]["embedding_model"] == CUR, rid
    assert by_id["good"]["title_embedding"] == [0.9, 0.1], "当前模型的不重算"
    assert by_id["untitled"]["title_embedding"] is None, "空标题的不碰"
    assert "" not in [t for batch in embed_on for t in batch]


def test_a_healed_project_no_longer_reports_semantic_degraded(embed_on):
    """补完那一行, 这个项目下一批查重就比全了 —— 这正是 sportsix 每批都报 degraded 的那件事。"""
    c = FakeClient(rows={
        "projects": [{"id": P1, "name": "示例运动", "brand": "示例", "owner_id": "u1", "calibration_notes": "",
                      "tactics": "[]", "custom_roles": []}],
        "draft_fingerprints": [_fp("absent", P1, "缺向量的那条"), _fp("good", P1, "好的", vec=[0.9, 0.1], model=CUR)]})
    draft = [{"title": "一篇新稿", "body": "这一篇和历史上的哪一篇都不像, 只是拿来看查重那一路比没比全。" * 3}]
    before = core.check_drafts(c, P1, draft, user_id="u1")["summary"]
    assert before["semantic_degraded"] is True and before["history_missing_embedding"] == 1
    core.reembed_after_sync(c)
    after = core.check_drafts(c, P1, draft, user_id="u1")["summary"]
    assert after["semantic_degraded"] is False and after["history_missing_embedding"] == 0, after


def test_it_is_bounded_per_night_and_missing_vectors_go_first(embed_on, monkeypatch):
    rows = [_fp(f"s{i}", P1, f"老模型{i}", vec=[0.1], model=OLD, created=f"2026-10-2{i}T00:00:00+00:00")
            for i in range(3)]
    rows += [_fp(f"a{i}", P2, f"缺向量{i}", created=f"2026-10-0{i + 1}T00:00:00+00:00") for i in range(3)]
    c = FakeClient(rows={"draft_fingerprints": rows})
    out = core.reembed_after_sync(c, limit=4)
    assert out["pending"] == 4 and out["fixed"] == 4 and out["capped"] is True
    assert out["missing_vector"] == 3 and out["stale_model"] == 1, "缺向量的(查重直接少比的)排在换模型的前面"
    left = [r["id"] for r in c.rows["draft_fingerprints"] if r.get("embedding_model") != CUR]
    assert len(left) == 2
    nxt = core.reembed_after_sync(c, limit=4)
    assert nxt["fixed"] == 2 and nxt["capped"] is False, "没轮到的下一晚接着补"
    assert core.TV_SYNC_REEMBED_MAX == 200


def test_an_embedding_failure_is_an_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: True)
    monkeypatch.setattr(core.dedup, "embed_texts", lambda texts: None)        # 整批失败(欠费 / 配额)
    c = FakeClient(rows={"draft_fingerprints": [_fp("absent", P1, "缺向量的那条")]})
    out = core.reembed_after_sync(c)
    assert out["fixed"] == 0 and out["failed"] == 1 and "embedding" in out["error"]
    assert c.rows["draft_fingerprints"][0]["title_embedding"] is None


def test_not_configured_reads_nothing(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    monkeypatch.setattr(store, "fingerprints_needing_vectors", lambda *a, **k: pytest.fail("没配 embedding 还去查库"))
    assert core.reembed_after_sync(FakeClient(rows={})) == {"skipped": "not_configured"}


# ══════════════════════════════════════════════════════════════════════
# CLI: tv-sync --all 跑完补, 报个数; 补不成退出码 1, 对照照常写了
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def fake(monkeypatch):
    c = tv_client(NOTES)
    c.rows["draft_fingerprints"].append(_fp("absent", "aaaaaaaa-0000-0000-0000-000000000001", "缺向量的那条"))
    monkeypatch.setattr(core, "sb", lambda: c)
    return c


def test_cli_tv_sync_all_heals_after_the_sync_and_reports_counts(fake, embed_on, capsys):
    assert cli.main(["tv-sync", "--all"]) == 0
    out = capsys.readouterr().out
    assert len(fake.rows["tv_note_links"]) == 3, "同步照常"
    assert f"标题向量(每晚最多补 {core.TV_SYNC_REEMBED_MAX} 行): 补了" in out, out
    assert all(r.get("embedding_model") == CUR for r in fake.rows["draft_fingerprints"] if r.get("title")), \
        "补录进来的、原来就缺的, 有标题的都补上了"
    assert out.index("补录: 收到") < out.index("标题向量"), "同步完才补"


def test_cli_reembed_failure_is_reported_and_fails_the_run_but_keeps_the_sync(fake, monkeypatch, capsys):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: True)
    monkeypatch.setattr(core.dedup, "embed_texts", lambda texts: None)
    assert cli.main(["tv-sync", "--all"]) == 1
    out = capsys.readouterr().out
    assert "embedding 调用失败" in out and "对照照常写了" in out
    assert len(fake.rows["tv_note_links"]) == 3, "补向量失败不许撤掉这晚写进去的对照"


def test_cli_reembed_lookup_crash_is_caught(fake, embed_on, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("PostgREST 503")
    monkeypatch.setattr(store, "fingerprints_needing_vectors", boom)
    assert cli.main(["tv-sync", "--all"]) == 1
    out = capsys.readouterr().out
    assert "标题向量没补成: RuntimeError: PostgREST 503" in out
    assert len(fake.rows["tv_note_links"]) == 3


def test_cli_without_embedding_is_not_a_failure(fake, monkeypatch, capsys):
    """没配 embedding 是部署层面的选择(同存档时的判定), 不是故障: 说一句, 退出码不变。"""
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    assert cli.main(["tv-sync", "--all"]) == 0
    assert "没配 embedding" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["tv-sync", "--all", "--dry-run"], ["tv-sync", "--tv-project", TV]])
def test_cli_only_heals_on_the_nightly_all_run(fake, embed_on, monkeypatch, argv, capsys):
    monkeypatch.setattr(core, "reembed_after_sync", lambda *a, **k: pytest.fail("只在 --all 且不是 --dry-run 时补"))
    assert cli.main(argv) == 0
    assert "标题向量" not in capsys.readouterr().out


def test_it_stops_between_chunks_at_its_time_budget(embed_on, monkeypatch):
    """复审 2026-10-30: embedding 客户端没设请求超时, 这一步不能拖住 14:00 那趟 cron。到点就在块与块之间停, 剩下的不算失败,
    stopped_early 照实报, 明晚接着补。第一块总要做完(到点前已经开始)。"""
    c = FakeClient(rows={"draft_fingerprints": [_fp(f"a{i}", P1, f"缺向量{i}") for i in range(5)]})
    monkeypatch.setattr(core.time, "monotonic", lambda: 100.0)                 # 第二块开始前就已经过点
    got = core.reembed_fingerprints(c, None, chunk=2, limit=10, deadline=50.0)
    assert got["fixed"] == 2 and got["failed"] == 0 and got.get("stopped_early") is True, got
    assert sum(1 for r in c.rows["draft_fingerprints"] if r.get("embedding_model") == CUR) == 2


def test_the_nightly_step_reports_a_budget_stop_as_capped(embed_on, monkeypatch):
    monkeypatch.setattr(core, "TV_SYNC_REEMBED_BUDGET_SEC", -1)                 # 一开始就超了: 只做第一块(50 行)
    c = FakeClient(rows={"draft_fingerprints": [_fp(f"b{i}", P1, f"缺{i}") for i in range(120)]})
    out = core.reembed_after_sync(c)
    assert out["fixed"] == 50 and out["stopped_early"] is True and out["capped"] is True and "error" not in out, out


def test_cli_reports_an_error_even_when_nothing_was_pending(fake, monkeypatch, capsys):
    """复审 2026-10-30: 原来先看「没有要补的」就回 0, embedding 在两次判断之间不可用时(pending 是 0、error 有值)一声不吭。"""
    monkeypatch.setattr(core, "reembed_after_sync",
                        lambda *a, **k: {"pending": 0, "fixed": 0, "failed": 0, "error": "embedding 不可用"})
    assert cli.main(["tv-sync", "--all"]) == 1
    assert "标题向量没补成: embedding 不可用" in capsys.readouterr().out
