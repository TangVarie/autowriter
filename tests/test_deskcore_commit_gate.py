"""入库自带闸 + angle_key 记账(2026-09-17)。

途鸽 09-10 那天 66 条入库稿子里, 四个标题各入库了两次, 两两四字串 Jaccard 只有
0.33、开头也不同 —— 是**同一个故事重写了一遍**, 不是抄句子。它们进得了库, 是
因为入库那一步只靠 RPC 的两路确定性信号(开头精确 / 四字串), 标题语义和本批内
互比只在 ``check_drafts`` 里; 模型一旦跳过 check 直接 commit, 那两路就没人跑。
同一天 66 条里 49 条没带 ``angle_key``, 而台账是唯一防"同题再写一遍"的机制, RPC
对空 angle_key 一声不吭地照收。

所以这里盯两件事:

  1. ``commit_drafts`` **自己跑一遍**和 ``check_drafts`` 同一套判定, 判 reject 的
     不进 RPC —— 闸不依赖模型记得先调 check;
  2. 没带 ``angle_key`` 入库的稿子要被数出来说给调用方听。
"""

from __future__ import annotations

import math

import pytest

from deskcore import core
from deskcore import fingerprint as fp
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
PROJ = "aaaaaaaa-0000-0000-0000-000000000001"

BODY_A = ("今天下班路上想到一件事, 留学生秋招最费时间的其实不是投简历, "
          "而是等一个根本不会来的回音, 等到后来连刷新邮箱都成了一种自我安慰。") * 3
BODY_B = ("绩点二点八的澳八大回国找工作, 我一开始也以为没人要, 后来发现问题"
          "出在简历的第一行, 那行字把面试官最想看的东西藏到了最后。") * 3
BODY_C = ("凌晨收到转正邮件的时候我在屋里发了很久的呆, 三个月前那个被群面刷掉"
          "三次的人, 和现在盯着屏幕的人, 中间隔着一整个秋天的练习。") * 3


def _history(body, title="历史稿", vec=None) -> dict:
    return {"id": f"h-{abs(hash(body)) % 10**8}", "project_id": PROJ,
            "title": title, "opening": fp.opening_of(body),
            "opening_hash": fp.opening_hash(body),
            "ngram_hashes": fp.ngram_hashes(body),
            "title_embedding": vec,
            "embedding_model": core.dedup.EMBEDDING_MODEL if vec else None,
            "created_at": "2026-09-10T00:00:00+00:00"}


def _client(history=()) -> FakeClient:
    c = FakeClient(rows={
        "projects": [{"id": PROJ, "name": "项目", "brand": "A", "owner_id": ME,
                      "calibration_notes": "", "tactics": "[]", "custom_roles": []}],
        "draft_fingerprints": list(history),
    })
    # RPC 全部判 inserted —— 这个文件要证明的正是"闸前"那一层, RPC 那一层已经
    # 有别的测试守着。它照单全收, 所以凡是没被闸前拦下的都会被"写进去"。
    c.rpc_impl["deskcore_commit_fingerprints"] = lambda args: [
        {"idx": i, "status": core.COMMIT_STATUS_INSERTED,
         "collided_with": None, "detail": None}
        for i in range(len(args.get("_rows") or []))]
    return c


def _rows_reaching_rpc(c: FakeClient) -> list[list]:
    return [a.get("_rows") or [] for n, a in c.rpc_calls
            if n == "deskcore_commit_fingerprints"]


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)


# ══════════════════════════════════════════════════════════════════════
# 1 · 跳过 check_drafts 直接 commit, 照样进不了重复的稿子
# ══════════════════════════════════════════════════════════════════════

def test_commit_without_check_rejects_a_verbatim_duplicate_of_history():
    """⚠️ 这条是这个文件存在的理由。

    调用方**没调过** check_drafts, 直接 commit 一篇和历史稿开头一模一样的稿子。
    改之前: RPC 会拦住它(开头精确是 RPC 也跑的信号), 所以这条本来就过 ——
    它守的是"闸前那一层真的在跑"这件事本身: RPC 没收到它。
    """
    c = _client(history=[_history(BODY_A, title="上周发过的")])
    out = core.commit_drafts(c, PROJ, [{"title": "换了个标题", "body": BODY_A}],
                             user_id=ME)
    assert out["written"] == 0
    assert out["rejected"][0]["index"] == 0
    assert out["rejected"][0]["gate"] == "pre_commit"
    assert out["rejected"][0]["collided_with"] == "上周发过的"
    assert _rows_reaching_rpc(c) == [], "闸前判死的稿子不该再送进 RPC"


def test_commit_rejects_the_second_copy_within_the_batch():
    """本批内互比原来只在 check_drafts 里跑 —— 同一批塞两份一样的, RPC 是逐条
    插入、逐条比历史, 第二份会撞上刚插的第一份没错; 但 RPC 不存在(非原子路径)
    时两份会一起 write_fingerprints 进去。闸前互比把这条路也堵上。"""
    c = _client()
    out = core.commit_drafts(c, PROJ, [
        {"title": "第一份", "body": BODY_A},
        {"title": "第二份", "body": BODY_A},
    ], user_id=ME)
    assert out["written"] == 1
    assert [r["index"] for r in out["rejected"]] == [1]
    assert out["rejected"][0]["collided_scope"] == "本批内"
    assert out["rejected"][0]["collided_with"] == "第一份"
    assert len(_rows_reaching_rpc(c)[0]) == 1


def test_commit_rejects_a_same_title_when_the_embeddings_say_so(monkeypatch):
    """途鸽那四对同标题稿子就是从这条缝进来的: 标题语义(TITLE_SIM_HARD)只在
    check_drafts 里判, RPC 不看向量。"""
    def _vec(title: str) -> list[float]:
        # 同标题 → 同向量(余弦 1.0); 不同标题 → 正交(余弦 0)。
        seed = sum(map(ord, title)) % 3
        v = [0.0, 0.0, 0.0]
        v[seed] = 1.0
        return v
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: True)
    monkeypatch.setattr(core.dedup, "embed_texts", lambda ts: [_vec(t) for t in ts])

    same = "被外企群面刷掉三次，才发现这个致命雷区"
    c = _client(history=[_history(BODY_B, title=same, vec=_vec(same))])
    out = core.commit_drafts(c, PROJ, [
        {"title": same, "body": BODY_C},           # 正文完全不同, 只有标题撞
    ], user_id=ME)
    assert out["written"] == 0
    r = out["rejected"][0]
    assert r["gate"] == "pre_commit"
    assert "title" in (r["decided_by"] or []), r
    assert r["collided_with"] == same


def test_rejected_indices_refer_to_the_callers_list_not_the_survivors():
    """⚠️ RPC 回的 idx 是幸存者列表里的下标。中间一条被闸前拦下之后, 后面那条在
    RPC 眼里是 idx=1, 在调用方眼里是 index=2 —— 翻错了会让人重写错稿子。"""
    c = _client(history=[_history(BODY_B, title="历史")])
    out = core.commit_drafts(c, PROJ, [
        {"title": "甲", "body": BODY_A},
        {"title": "乙", "body": BODY_B},       # 撞历史, 闸前拦下
        {"title": "丙", "body": BODY_C},
    ], user_id=ME)
    assert out["written"] == 2
    assert [r["index"] for r in out["rejected"]] == [1]
    sent = _rows_reaching_rpc(c)[0]
    assert [r["title"] for r in sent] == ["甲", "丙"]
    assert len(out["version_ids"]) == 2
    minted_titles = {v["title"] for v in c.rows.get("versions", [])}
    assert minted_titles == {"甲", "丙"}, "被拦下的那条不许建身份"


def test_everything_rejected_means_nothing_is_written_and_rpc_is_not_called():
    c = _client(history=[_history(BODY_A), _history(BODY_B)])
    out = core.commit_drafts(c, PROJ, [
        {"title": "a", "body": BODY_A}, {"title": "b", "body": BODY_B},
    ], user_id=ME)
    assert out["written"] == 0
    assert len(out["rejected"]) == 2
    assert _rows_reaching_rpc(c) == []
    assert out["version_ids"] == []
    assert "check_drafts" in out["note"]


def test_the_gate_summary_is_returned_so_the_caller_sees_degradation():
    """check_drafts 会报 semantic_degraded / empty_history_warning; 入库自带闸之后
    这些也要能从 commit 的返回值里看到, 否则跳过 check 的调用方永远不知道。"""
    c = _client()
    out = core.commit_drafts(c, PROJ, [{"title": "x", "body": BODY_A}], user_id=ME)
    assert out["gate_summary"]["semantic_degraded"] is True
    assert "empty_history_warning" in out["gate_summary"]


# ══════════════════════════════════════════════════════════════════════
# 2 · 没带 angle_key 的要数出来
# ══════════════════════════════════════════════════════════════════════

def test_unattributed_drafts_are_counted_and_warned():
    c = _client()
    out = core.commit_drafts(c, PROJ, [
        {"title": "带坐标", "body": BODY_A, "angle_key": "k1"},
        {"title": "没带", "body": BODY_B},
        {"title": "也没带", "body": BODY_C, "angle_key": ""},
    ], user_id=ME)
    assert out["written"] == 3
    assert out["unattributed"] == 2
    assert "angle_key" in out["unattributed_warning"]


def test_all_attributed_means_no_warning():
    c = _client()
    out = core.commit_drafts(c, PROJ, [
        {"title": "甲", "body": BODY_A, "angle_key": "k1"},
        {"title": "乙", "body": BODY_B, "angle_key": "k2"},
    ], user_id=ME)
    assert out["unattributed"] == 0
    assert "unattributed_warning" not in out


def test_rejected_drafts_do_not_count_as_unattributed():
    """被拦下的没入库, 它有没有坐标跟台账无关 —— 数进去会让警告对不上 written。"""
    c = _client(history=[_history(BODY_A)])
    out = core.commit_drafts(c, PROJ, [
        {"title": "撞了", "body": BODY_A},                     # 没坐标, 但被拦
        {"title": "好的", "body": BODY_B, "angle_key": "k"},
    ], user_id=ME)
    assert out["written"] == 1
    assert out["unattributed"] == 0


# ══════════════════════════════════════════════════════════════════════
# 3 · RPC 回执对不上号: 指纹已写入, 不许再抛
# ══════════════════════════════════════════════════════════════════════

def test_out_of_range_rpc_idx_is_reported_not_raised():
    """指纹在 RPC 里已经写进去了。这时抛 500, 调用方会重试, 重试撞上自己刚写的
    指纹 —— 一次故障变成一句"你的稿子重复了"。"""
    c = _client()
    c.rpc_impl["deskcore_commit_fingerprints"] = lambda args: [
        {"idx": 0, "status": core.COMMIT_STATUS_INSERTED, "collided_with": None, "detail": None},
        {"idx": 7, "status": core.COMMIT_STATUS_INSERTED, "collided_with": None, "detail": None},
        {"idx": "x", "status": "rejected", "collided_with": "?", "detail": "?"},
    ]
    out = core.commit_drafts(c, PROJ, [{"title": "只有一条", "body": BODY_A}], user_id=ME)
    assert out["rpc_anomalies"] == 2
    assert "别重试" in out["rpc_warning"]
    assert out["version_ids"] and len(out["version_ids"]) == 1
    assert out["rejected"] == []
