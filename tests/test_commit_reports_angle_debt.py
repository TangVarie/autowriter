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


# ══════════════════════════════════════════════════════════════════════
# 4 · 带了 key、台账却对不上(W-31, 10-27)
#
# 10-26 sportsix: 发牌 20 张, 5 批存了 19 篇, 每篇都带 angle_key, 可一条都不在台账里(台账的 key 都是 20 位,
# 这 19 条是 7~8 位带下划线的编号 15 条、53 位的 4 条)。consume_angle 只记 warning, unattributed 只数没带 key 的,
# 回包只有 consumed_angles < written —— 写手那边的 AI 一直不知道 key 是错的, 20 张牌一天后回了池子。
#
# 被禁止的形态:
#   · 带了错 key 的不单独数、不说哪几篇、不说 key 该长什么样;
#   · 把 key 错了说成「发牌时台账没写进去」;
#   · 叫模型「按挂着的坐标写」—— 那几张牌对应的稿子其实写完存了, 再写一遍就是同一个角度两篇;
#   · 并发 / 查询出错没销成的, 冤枉成 key 错了;
#   · 查 key 状态失败把一次已经写进库的存档拖成报错;
#   · 每次存档都多查一次库(只该在有没销成的时候查)。
# ══════════════════════════════════════════════════════════════════════

def _hex(i) -> str:
    """真的发牌 key: fingerprint.angle_key 出来的 20 位十六进制。"""
    return fp.angle_key({"emotional_lever": f"e{i}", "human_truth_archetype": "h", "content_format": "c",
                         "title_structure": "t"})


def _card(i, *, user=ME, consumed=None):
    return {**_angle(i, user=user, consumed=consumed), "angle_key": _hex(i)}


SHORT_CODE = "A_01"                                           # 10-26 那 15 条的样子
LONG_KEY = "职场新人_签约前查公司_避坑清单_" + "x" * 36            # 53 位的那 4 条的样子
BODY_C = ("租房第一个月我把押金条拍了三遍, 后来退租时房东说没见过那张纸, 幸亏手机里还有原图, "
          "这件事让我明白收据这种东西永远要自己留一份。") * 3


def test_keys_the_ledger_has_never_seen_are_counted_named_and_explained():
    assert len(LONG_KEY) == 53
    c = _client([_card(1), _card(2), _card(3)])
    out = core.commit_drafts(c, PROJ, [
        {"title": "编号", "body": BODY_A, "angle_key": SHORT_CODE},
        {"title": "长串", "body": BODY_B, "angle_key": LONG_KEY},
        {"title": "对的", "body": BODY_C, "angle_key": _hex(3)},
    ], user_id=ME)

    assert out["written"] == 3 and out["consumed_angles"] == 1
    assert out["unattributed"] == 0, "带了 key 的不算没带"
    assert out["unknown_angle_keys"] == 2 and out["used_angle_keys"] == 0
    mm = out["angle_key_mismatch"]
    assert [(m["index"], m["problem"], m["length"]) for m in mm] == [(0, "unknown", 4), (1, "unknown", 53)]
    assert mm[0]["angle_key"] == SHORT_CODE
    assert mm[1]["angle_key"] == LONG_KEY[:core.ANGLE_KEY_SHOW_CHARS] + "…", "长串只露前一截"
    warn = out["angle_key_warning"]
    for must in ("2 篇", "台账里根本没有", f"{fp.ANGLE_KEY_HEX_LEN} 位十六进制", "原样复制", "不用重存",
                 "angle_key_mismatch"):
        assert must in warn, must
    # 差额全是 key 的问题: 不再笼统地报「多半是发牌时台账没写进去」
    assert "angle_ledger_warning" not in out
    # 还挂着的两张牌一并回去, 但不叫它「按它们写」—— 那两张对应的稿子刚存过
    assert {a["angle_key"] for a in out["angle_debt"]["angles"]} == {_hex(1), _hex(2)}
    assert "别按这些坐标再写一遍" in warn and "下一批按它们写" not in warn
    assert "unattributed_warning" not in out


def test_a_key_already_spent_on_another_draft_is_reported_as_used():
    """台账里有这个 key, 但已经销给了别的稿子(上一批用过 / 同一批两篇带了同一个 key)。"""
    c = _client([_card(1, consumed="v-old"), _card(2)])
    out = core.commit_drafts(c, PROJ, [
        {"title": "旧坐标", "body": BODY_A, "angle_key": _hex(1)},
        {"title": "同一个 key 甲", "body": BODY_B, "angle_key": _hex(2)},
        {"title": "同一个 key 乙", "body": BODY_C, "angle_key": _hex(2)},
    ], user_id=ME)

    assert out["written"] == 3 and out["consumed_angles"] == 1
    assert out["used_angle_keys"] == 2 and out["unknown_angle_keys"] == 0
    assert [(m["index"], m["problem"]) for m in out["angle_key_mismatch"]] == [(0, "used"), (2, "used")]
    assert "已经销给了别的稿子" in out["angle_key_warning"]
    assert "台账里根本没有" not in out["angle_key_warning"]
    assert "angle_ledger_warning" not in out


def test_a_lost_race_is_not_blamed_on_the_key(monkeypatch):
    """台账里有这一行、还没销, 是这次没销成(并发 / 写台账出错): 照旧报 angle_ledger_warning, 不说 key 错了。"""
    c = _client([_card(1)])
    monkeypatch.setattr(store, "consume_angle", lambda *a, **k: False)
    out = core.commit_drafts(c, PROJ, [{"title": "甲", "body": BODY_A, "angle_key": _hex(1)}], user_id=ME)

    assert out["consumed_angles"] == 0
    assert out["unknown_angle_keys"] == 0 and out["used_angle_keys"] == 0
    assert "angle_key_mismatch" not in out and "angle_key_warning" not in out
    assert "1/1" in out["angle_ledger_warning"] and "台账里有这一行" in out["angle_ledger_warning"]


def test_a_failed_state_lookup_never_breaks_the_commit_and_says_it_could_not_tell(monkeypatch):
    c = _client([_card(1)])

    def boom(*a, **k):
        raise RuntimeError("PostgREST 503")
    monkeypatch.setattr(store, "angle_key_states", boom)
    out = core.commit_drafts(c, PROJ, [{"title": "甲", "body": BODY_A, "angle_key": SHORT_CODE}], user_id=ME)

    assert out["written"] == 1, "指纹已经写进去了, 不许因为一段提醒报失败"
    assert out["unknown_angle_keys"] == 0 and "angle_key_mismatch" not in out
    assert "没查清" in out["angle_ledger_warning"]


def test_a_clean_commit_never_looks_up_key_states(monkeypatch):
    """每篇都销成了就没有这个话题; 别为它白付一次查询。"""
    c = _client([_card(1), _card(2)])
    calls = []
    real = store.angle_key_states
    monkeypatch.setattr(store, "angle_key_states", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    out = core.commit_drafts(c, PROJ, [
        {"title": "甲", "body": BODY_A, "angle_key": _hex(1)},
        {"title": "乙", "body": BODY_B, "angle_key": _hex(2)},
    ], user_id=ME)
    assert out["consumed_angles"] == 2 and out["unknown_angle_keys"] == 0
    assert calls == []


def test_state_lookup_is_scoped_to_the_project_and_never_sends_unsafe_keys():
    """模型编的 key 什么字符都可能有: postgrest-py 的 in_() 不转义 " 和 \\, 一个坏 key 就让整条查询报错。
    那种形状本来就不可能在台账里(台账的 key 全是 20 位十六进制), 直接算 unknown, 不送去查。"""
    other = "aaaaaaaa-0000-0000-0000-00000000000f"
    c = _client([_card(1), _card(2, consumed="v-2"),
                 {**_card(3), "project_id": other}])                       # 别的项目里有, 本项目没有
    weird = ['a"b', "x\\y", "a,b(c)", "空 格", "z" * 65]
    got = store.angle_key_states(c, PROJ, [_hex(1), _hex(2), _hex(3), "nope_1", *weird])
    assert got == {_hex(1): "open", _hex(2): "used", _hex(3): "unknown", "nope_1": "unknown",
                   **{w: "unknown" for w in weird}}
    calls = [x for x in c.calls if x["table"] == "angle_ledger"]
    assert calls, "能查的那几个要去查"
    for call in calls:                                                      # 翻页: 一页 + 收尾的空页
        (sent,) = [f[2] for f in call["filters"] if f[0] == "in"]
        assert set(sent) == {_hex(1), _hex(2), _hex(3), "nope_1"}
        assert ("eq", "project_id", PROJ) in call["filters"]


def test_state_lookup_failure_returns_none_not_a_guess():
    class Boom:
        def table(self, *a, **k):
            raise RuntimeError("PostgREST 503")
    assert store.angle_key_states(Boom(), PROJ, [_hex(1)]) is None
    assert store.angle_key_states(Boom(), PROJ, ['a"b']) == {'a"b': "unknown"}, "不用查的照样分得清"


def test_metrics_count_the_mismatches_without_storing_the_keys():
    c = _client([_card(1)])
    core.commit_drafts(c, PROJ, [
        {"title": "编号", "body": BODY_A, "angle_key": SHORT_CODE},
        {"title": "长串", "body": BODY_B, "angle_key": LONG_KEY},
        {"title": "没带", "body": BODY_C},
    ], user_id=ME)
    (row,) = [r for r in c.rows["batch_metrics"] if (r.get("meta") or {}).get("mode") == "deskcore_commit"]
    assert row["counters"]["unknown_angle_keys"] == 2 and row["counters"]["used_angle_keys"] == 0
    assert row["counters"]["unattributed"] == 1
    assert row["meta"]["angle_key_mismatch"] == [{"index": 0, "problem": "unknown", "length": 4},
                                                 {"index": 1, "problem": "unknown", "length": 53}]
    blob = repr(row)
    assert SHORT_CODE not in blob and "职场新人" not in blob, "key 本身不进 batch_metrics"
