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
    assert "还有 3 个没有对应的稿子" in warn
    assert "angle_debt.angles" in warn
    # 还要说清下一步, 否则模型照样再抽一批。2026-10-30 复审: 照简报那句的写法 —— 对得上就优先按它们写、查重时带上
    # key; 说「这个项目」不说「你」(一个团队共用一个身份); 不叫「别再抽一批新的」(用户点了题目就照用户的写),
    # 不提放弃, 也不说存档。
    assert "优先按这些坐标写" in warn and "查重时带上它的 angle_key" in warn
    tail = warn[warn.index("这个项目近"):]
    for banned in ("别再抽", "放弃", "你在这个项目", "你抽", "存档", "下一批按它们写"):
        assert banned not in tail, (banned, tail)


# ── 刚抽的牌不列(2026-10-30 复审, 同简报 core.ANGLE_DEBT_MIN_AGE) ──
# 一个团队共用一个身份: 一小时以内抽的多半是队友正在写的牌, 叫这一场「优先按它们写」就是两个人写同一张牌。

MINUTES = 1 / (24 * 60)


def test_a_card_drawn_minutes_ago_is_not_listed_after_commit():
    c = _client([_angle(1, days_ago=5 * MINUTES)])
    out = core.commit_drafts(c, PROJ, [{"title": "没带", "body": BODY_A}], user_id=ME)
    assert out["unattributed"] == 1
    assert "angle_debt" not in out, "刚抽的牌不算欠账"
    assert out["unattributed_warning"].endswith("angle_key。"), "没有可列的就不接那一截"


def test_a_card_drawn_two_hours_ago_is_listed_after_commit():
    c = _client([_angle(1, days_ago=5 * MINUTES), _angle(2, days_ago=2 / 24)])
    out = core.commit_drafts(c, PROJ, [{"title": "没带", "body": BODY_A}], user_id=ME)
    assert out["angle_debt"]["unconsumed"] == 1
    assert [a["angle_key"] for a in out["angle_debt"]["angles"]] == ["k2"]
    assert "一小时以内刚发的多半有人正在写" in out["unattributed_warning"]


def test_cards_checked_with_their_key_are_not_listed_after_commit(monkeypatch):
    """查重时带过 key 的牌, 稿子已经写出来了(W-38): 和简报一样不列, 不然「优先按这些坐标写」会把同一个角度再写一遍。"""
    monkeypatch.setattr(core, "_checked_angle_keys", lambda *a, **k: {"k1": store.iso_now()})
    c = _client([_angle(1, days_ago=1), _angle(2, days_ago=1)])
    out = core.commit_drafts(c, PROJ, [{"title": "没带", "body": BODY_A}], user_id=ME)
    assert [a["angle_key"] for a in out["angle_debt"]["angles"]] == ["k2"]


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


def test_cards_drawn_before_the_floor_are_not_listed(monkeypatch):
    """W-38 上线之前抽的牌不算(core.ANGLE_DEBT_SINCE, 同简报): 那些多半写完发出去了, 叫模型「下一批按它们写」
    会再写一遍。下限挪到两天前(真实的下限是固定时刻): 三天前抽的不列, 一天前抽的照列。"""
    monkeypatch.setattr(core, "ANGLE_DEBT_SINCE", store.iso_ago(2))
    c = _client([_angle(1, days_ago=3), _angle(2, days_ago=1)])
    out = core.commit_drafts(c, PROJ, [{"title": "没带", "body": BODY_A}], user_id=ME)
    assert out["angle_debt"]["unconsumed"] == 1
    assert [a["angle_key"] for a in out["angle_debt"]["angles"]] == ["k2"]


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
    assert "别按这些坐标再写一遍" in warn and "下一批按它们写" not in warn and "优先按这些坐标写" not in warn
    assert "unattributed_warning" not in out
    # 说「这个项目」不说「你」, 也不再叫模型存档时从挂着的牌里找 key(一小时以内刚抽的不在里面, 照 draw_angles 的带)
    key_tail = warn[warn.index("这个项目近"):]
    assert "你在这个项目" not in key_tail and "存档时从里面找" not in key_tail, key_tail


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


# ══════════════════════════════════════════════════════════════════════
# 4b · 形状对的 key 不是「编的」(W-31 复审, 10-27)
#
# 生产库近 60 天: 916 条带 key 的指纹里 48 条在自己项目的台账里对不上, 其中 15 条是 20 位十六进制 —— 7 条是同一个人
# 在别的项目抽的牌(09-21), 8 条哪个台账都没有(09-10/11, 非原子发牌路径 record_draw 失败只记日志)。告诉写手「自己编的、
# 缩写的或抄错了」, 他会去改一个本来没错的 key; 真正要做的是确认是不是存错了项目 / 知道是台账的锅。
#
# 被禁止的形态:
#   · 形状对的 key 也说成编的 / 抄错的, 或者叫他「不要自己编号、缩写或改写」;
#   · 拿别人在别的项目抽的牌说「是你在别的项目抽的」(只看 drawn_by = 调用者);
#   · 有不带 key 的、也有 key 对不上的同一批里, 一句叫「下一批按挂着的坐标写」、一句叫「别按这些坐标再写」。
# ══════════════════════════════════════════════════════════════════════

OTHER_PROJ = "aaaaaaaa-0000-0000-0000-00000000000f"


def test_a_key_drawn_in_another_project_says_wrong_project_not_invented():
    c = _client([_card(2), {**_card(1), "id": "ang-x1", "project_id": OTHER_PROJ}])
    out = core.commit_drafts(c, PROJ, [{"title": "甲", "body": BODY_A, "angle_key": _hex(1)}], user_id=ME)

    assert out["written"] == 1 and out["consumed_angles"] == 0
    assert out["unknown_angle_keys"] == 1, "这个项目的台账里没有, 照样算进 unknown_angle_keys"
    assert out["angle_key_mismatch"] == [{"index": 0, "angle_key": _hex(1), "length": 20,
                                          "problem": "other_project", "project_id": OTHER_PROJ}]
    warn = out["angle_key_warning"]
    assert "别的项目抽的牌" in warn and "存错了项目" in warn
    for bad in ("自己编的", "抄错了", "不要自己编号"):
        assert bad not in warn, bad
    assert "angle_ledger_warning" not in out


def test_a_well_formed_key_in_no_ledger_of_mine_is_put_on_the_ledger_write():
    """哪个台账都没有(队友在别的项目抽过同一个 key 不算我的): 多半是发牌时台账没写进去, 不是写手的错。"""
    c = _client([{**_card(9, user=MATE), "id": "ang-m9", "project_id": OTHER_PROJ}])
    out = core.commit_drafts(c, PROJ, [{"title": "甲", "body": BODY_A, "angle_key": _hex(9)}], user_id=ME)

    assert out["unknown_angle_keys"] == 1
    (m,) = out["angle_key_mismatch"]
    assert (m["problem"], m["length"]) == ("not_in_ledger", 20) and "project_id" not in m, "别人的牌不报"
    warn = out["angle_key_warning"]
    assert "发牌时台账没写进去" in warn and "不是你编的或抄错的" in warn
    for bad in ("自己编的、缩写的", "不要自己编号"):
        assert bad not in warn, bad


def test_the_elsewhere_lookup_failing_falls_back_to_not_in_ledger(monkeypatch):
    c = _client([{**_card(1), "id": "ang-x1", "project_id": OTHER_PROJ}])

    def boom(*a, **k):
        raise RuntimeError("PostgREST 503")
    monkeypatch.setattr(store, "angle_keys_drawn_elsewhere", boom)
    out = core.commit_drafts(c, PROJ, [{"title": "甲", "body": BODY_A, "angle_key": _hex(1)}], user_id=ME)
    assert out["written"] == 1 and [m["problem"] for m in out["angle_key_mismatch"]] == ["not_in_ledger"]


def test_invented_and_well_formed_keys_in_one_batch_are_told_apart():
    c = _client([_card(2), {**_card(1), "id": "ang-x1", "project_id": OTHER_PROJ}])
    out = core.commit_drafts(c, PROJ, [
        {"title": "编号", "body": BODY_A, "angle_key": SHORT_CODE},
        {"title": "别的项目", "body": BODY_B, "angle_key": _hex(1)},
    ], user_id=ME)
    assert out["unknown_angle_keys"] == 2
    assert [m["problem"] for m in out["angle_key_mismatch"]] == ["unknown", "other_project"]
    warn = out["angle_key_warning"]
    assert "1 篇带的 angle_key 在这个项目的台账里根本没有" in warn and "自己编的" in warn
    assert "1 篇带的 angle_key 是你在别的项目抽的牌" in warn and "原样复制" in warn


def test_unattributed_and_bad_keys_in_one_batch_do_not_give_opposite_orders():
    """挂着的牌里有一张其实刚写完存了(带错了 key): 不许一句叫「下一批按它们写」、另一句叫「别按这些坐标再写」。"""
    c = _client([_card(1), _card(2)])
    out = core.commit_drafts(c, PROJ, [
        {"title": "错 key", "body": BODY_A, "angle_key": "A_01"},
        {"title": "没带", "body": BODY_B},
    ], user_id=ME)
    assert out["unattributed"] == 1 and out["unknown_angle_keys"] == 1
    assert "unattributed_warning" in out and "angle_key_warning" in out
    assert {a["angle_key"] for a in out["angle_debt"]["angles"]} == {_hex(1), _hex(2)}, "挂着的牌照样回去"
    assert "别按这些坐标再写一遍" in out["angle_key_warning"]
    for k, v in out.items():
        assert "下一批按它们写" not in repr(v) and "优先按这些坐标写" not in repr(v), k


def test_an_open_row_wins_over_a_spent_row_for_the_same_key():
    """同一个 key 在这个项目里有两行: 一行已销给了别的稿子, 一行还没销(过期后重新发了牌)。没销成是别的原因, 不是
    key 的问题 —— 和行的先后无关。"""
    c = _client([{**_card(5), "id": "ang-5a"}, {**_card(5, consumed="v-old"), "id": "ang-5b"}])
    assert store.angle_key_states(c, PROJ, [_hex(5)]) == {_hex(5): "open"}


def test_elsewhere_lookup_is_mine_only_and_never_sends_unsafe_keys():
    c = _client([{**_card(1), "id": "ang-x1", "project_id": OTHER_PROJ},
                 {**_card(2, user=MATE), "id": "ang-x2", "project_id": OTHER_PROJ},
                 _card(3)])                                                  # 本项目的不算「别的项目」
    got = store.angle_keys_drawn_elsewhere(c, PROJ, ME, [_hex(1), _hex(2), _hex(3), 'a"b'])
    assert got == {_hex(1): OTHER_PROJ}
    for call in [x for x in c.calls if x["table"] == "angle_ledger"]:
        (sent,) = [f[2] for f in call["filters"] if f[0] == "in"]
        assert 'a"b' not in sent
        assert ("eq", "drawn_by", ME) in call["filters"] and ("neq", "project_id", PROJ) in call["filters"]
    assert store.angle_keys_drawn_elsewhere(c, PROJ, None, [_hex(1)]) == {}, "不知道是谁就不查"
