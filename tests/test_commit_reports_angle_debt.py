"""交闸这一刻把「你还挂着哪几个坐标」摆出来 —— 不依赖客户端的 skill 版本。

2026-09-21 实查生产库, Hatherine痘痘贴:

  · 9-14 / 9-15 三次 ``draw_angles`` 共抽了 24 张牌, 一张没销账;
  · 9-21 04:01 另一场会话一次 ``commit_drafts`` 交了 7 篇 —— 七条指纹同一微秒
    落库, ``ai_engine='deskcore'``, 确认走的就是 commit_drafts, 不是 backfill
    也不是 ingest;
  · 那 7 篇【一个 angle_key 都没带】。查代码: ``app.py`` 原样转发、``tools.py``
    原样透传、``core.py`` 两条写入路径(原子 RPC 与降级直插)都带着 angle_key
    —— **没有任何一层会丢它**。会话当天【一次 draw_angles 都没调】, 手里根本
    没有坐标可带。

所以这不是丢字段, 是**抽牌的会话和写稿的会话不是同一场**。简报里的
``angle_debt``(D-071) 正是为这件事造的, 当时也已经上线; 但"看到这个数该怎么办"
写在 ``skills/bywood-writing-desk/SKILL.md`` 里, 得靠客户端重新导入 skill 才
生效 —— 而运营那边还用着旧版。

这里钉的是【服务端自己在交闸那一刻把坐标摆出来】: 旧 skill 也躲不开, 因为它
是 ``commit_drafts`` 的返回值, 不是 prompt 里的一句话。

被禁止的形态:
  · 只报个数不给坐标(模型补不了一个它不知道是什么的东西 —— codex review on #86);
  · 把这一批刚销掉的坐标又报成欠账;
  · 把队友抽的牌记到你头上;
  · 查这笔账失败把一次【已经写进库】的 commit 拖成报错。
"""

from __future__ import annotations

import pytest

from deskcore import core, store
from deskcore import fingerprint as fp
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
MATE = "22222222-2222-2222-2222-222222222222"
PROJ = "aaaaaaaa-0000-0000-0000-000000000009"

BODY_A = ("今天下班路上想到一件事, 留学生秋招最费时间的其实不是投简历, "
          "而是等一个根本不会来的回音, 等到后来连刷新邮箱都成了一种自我安慰。") * 3
BODY_B = ("绩点二点八的澳八大回国找工作, 我一开始也以为没人要, 后来发现问题"
          "出在简历的第一行, 那行字把面试官最想看的东西藏到了最后。") * 3


def _angle(i, *, user=ME, consumed=None, days_ago=1):
    return {"id": f"ang-{i}", "project_id": PROJ, "angle_key": f"k{i}",
            "dims": {"钩子": f"h{i}"}, "drawn_by": user,
            "drawn_at": store.iso_ago(days_ago),
            "consumed_version_id": consumed,
            "consumed_at": store.iso_now() if consumed else None}


def _client(angles=()) -> FakeClient:
    c = FakeClient(rows={
        "projects": [{"id": PROJ, "name": "项目", "brand": "A", "owner_id": ME,
                      "calibration_notes": "", "tactics": "[]", "custom_roles": []}],
        "draft_fingerprints": [],
        "angle_ledger": list(angles),
    })
    # RPC 全部判 inserted —— 闸那一层别的文件守着, 这里要的是"都写进去了"之后
    # 的回执长什么样。
    c.rpc_impl["deskcore_commit_fingerprints"] = lambda args: [
        {"idx": i, "status": core.COMMIT_STATUS_INSERTED,
         "collided_with": None, "detail": None}
        for i in range(len(args.get("_rows") or []))]
    return c


# ══════════════════════════════════════════════════════════════════════
# 1 · 没带坐标时, 把当前挂着的坐标一起回去
# ══════════════════════════════════════════════════════════════════════

def test_warning_names_the_outstanding_coordinates_not_just_a_count():
    """只说"你有 2 条没带"等于让模型去补一个它不知道是什么的东西。"""
    c = _client([_angle(1), _angle(2), _angle(3)])
    out = core.commit_drafts(c, PROJ, [
        {"title": "没带", "body": BODY_A},
        {"title": "也没带", "body": BODY_B},
    ], user_id=ME)

    assert out["written"] == 2 and out["unattributed"] == 2
    assert out["angle_debt"]["unconsumed"] == 3
    # 坐标本身必须在回执里 —— 这是 codex review on #86 那一条的同一个理由。
    keys = [a["angle_key"] for a in out["angle_debt"]["angles"]]
    assert set(keys) == {"k1", "k2", "k3"}
    warn = out["unattributed_warning"]
    assert "3 个抽了没写的坐标" in warn
    assert "angle_debt.angles" in warn
    # 还要说清下一步, 否则模型照样再抽一批。
    assert "别再抽一批新的" in warn


def test_debt_is_computed_after_this_batch_settles_its_own_angles():
    """这一批刚销掉的不该再报成欠账 —— 否则每次交闸都虚报一遍自己刚写完的。"""
    c = _client([_angle(1), _angle(2)])
    out = core.commit_drafts(c, PROJ, [
        {"title": "带了", "body": BODY_A, "angle_key": "k1"},
        {"title": "没带", "body": BODY_B},
    ], user_id=ME)

    assert out["consumed_angles"] == 1 and out["unattributed"] == 1
    assert out["angle_debt"]["unconsumed"] == 1, "k1 这一批销掉了, 不能再算进欠账"
    assert [a["angle_key"] for a in out["angle_debt"]["angles"]] == ["k2"]


def test_teammates_unwritten_angles_are_not_charged_to_you():
    """队友抽的牌不该记在你头上 —— 同 angle_debt 在简报里的私有口径。"""
    c = _client([_angle(1, user=MATE), _angle(2, user=MATE)])
    out = core.commit_drafts(
        c, PROJ, [{"title": "没带", "body": BODY_A}], user_id=ME)

    assert out["unattributed"] == 1
    assert "angle_debt" not in out
    assert "坐标" not in out["unattributed_warning"]


# ══════════════════════════════════════════════════════════════════════
# 2 · 不该说话的时候别说
# ══════════════════════════════════════════════════════════════════════

def test_no_outstanding_angles_means_no_tail():
    """台账上没挂账就别硬接一句 —— 用户指定题目的单篇本来就允许不带坐标。"""
    c = _client([_angle(1, consumed="v-1")])
    out = core.commit_drafts(
        c, PROJ, [{"title": "没带", "body": BODY_A}], user_id=ME)

    assert out["unattributed"] == 1
    assert "angle_debt" not in out
    assert out["unattributed_warning"].endswith("angle_key。")


def test_fully_attributed_commit_never_looks_up_the_debt():
    """每条都带了坐标就没有这个话题; 顺带也别白付一次查询。"""
    c = _client([_angle(1), _angle(2)])
    calls = []
    real = store.angle_debt
    core.store.angle_debt = lambda *a, **k: (calls.append(a), real(*a, **k))[1]
    try:
        out = core.commit_drafts(c, PROJ, [
            {"title": "甲", "body": BODY_A, "angle_key": "k1"},
            {"title": "乙", "body": BODY_B, "angle_key": "k2"},
        ], user_id=ME)
    finally:
        core.store.angle_debt = real

    assert out["unattributed"] == 0
    assert "unattributed_warning" not in out and "angle_debt" not in out
    assert calls == [], "没有漏带的就不该去查这笔账"


# ══════════════════════════════════════════════════════════════════════
# 3 · 这一段坏了也绝不许拖垮 commit
# ══════════════════════════════════════════════════════════════════════

def test_debt_lookup_failure_never_turns_a_written_commit_into_an_error():
    """指纹这时已经写进库了。

    抛出去 → 调用方按协议重试 → 重试撞上自己刚写的指纹 → 一句提醒把一次成功
    的入库变成"你的稿子重复了", 而现场完全对不上。同 rpc_anomalies /
    identity_error 的纪律。
    """
    c = _client([_angle(1)])

    def _boom(*a, **k):
        raise RuntimeError("台账查询炸了")

    real = store.angle_debt
    core.store.angle_debt = _boom
    try:
        out = core.commit_drafts(
            c, PROJ, [{"title": "没带", "body": BODY_A}], user_id=ME)
    finally:
        core.store.angle_debt = real

    assert out["written"] == 1, "指纹已经写进去了, 不许因为一句提醒报失败"
    assert out["unattributed"] == 1
    # 主警告照常给 —— 丢的只是那一截坐标。
    assert "angle_key" in out["unattributed_warning"]
    assert "angle_debt" not in out
