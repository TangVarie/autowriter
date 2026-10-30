"""简报要在开头把「你自己抽了没写的角度」摆出来(D-071)。

2026-09-20 查生产库(truth-vault 侧顺着 deskcore ``/health`` 的 pipeline 指标挖的):

  · 近 7 天发牌 **214** 个角度, 台账上只有 **20** 个销了账;
  · 194 个没销账的里, **138** 个所在的会话连一篇真稿都没入库 —— 会话停在发牌
    之后就散了; 另外 56 个是写了稿但提交时没带 ``angle_key``;
  · ``/health`` 早就在报这个比例, 但那是运维视角、没人天天看, 而且它没排除
    ``tv-sync`` 导入的副本(同窗口 1507 条指纹里 1468 条是导入的), 读出来是假的。

修法不是再劝一遍模型, 是把这笔账摆到写手每场对话都会看的地方 —— 简报里。
这里钉的是【被禁止的形态】: 简报不报这笔账、把别人的账记到你头上、
或者查这笔账失败把简报拖垮。

2026-10-30 发布收尾: 写手说每场开头都被问一遍「近 N 天抽了 X 个角度还没出稿, 接着写完还是放弃」, 而那些牌大多
写完发出去了 —— W-38(10-29 08:26 UTC)之前只有 score_drafts 记稿子, 写手又没有存档这一步(D-120), 对不回来、销不了账。
所以又钉两条: W-38 之前抽的牌不算(core.ANGLE_DEBT_SINCE); 那句话是说给模型听的(对得上就优先按这些坐标写), 不叫它
问用户「接着写还是放弃」, 不说「你抽了」(一个团队共用一个身份), 不提存档。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from deskcore import core, store
from deskcore import tools as T
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
MATE = "22222222-2222-2222-2222-222222222222"
PID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _angle(i, *, user=ME, consumed=None, days_ago=1):
    return {"id": f"ang-{i}", "project_id": PID, "angle_key": f"k{i}",
            "dims": {}, "drawn_by": user,
            "drawn_at": store.iso_ago(days_ago),
            "consumed_version_id": consumed,
            "consumed_at": store.iso_now() if consumed else None}


def _client(angles):
    return FakeClient(rows={
        "projects": [{"id": PID, "name": "测试项目", "brand": "测试品",
                      "owner_id": ME, "system_prompt": "你是文案",
                      "calibration_notes": "", "tactics": "[]", "default_params": "{}"}],
        "memories": [], "user_calibration_notes": [],
        "items": [], "versions": [], "batches": [],
        "draft_fingerprints": [], "angle_ledger": list(angles), "style_edits": [],
    })


def _brief(client):
    return core.build_writing_brief(client, PID, user_id=ME)


# ══════════════════════════════════════════════════════════════════════
# 主路径
# ══════════════════════════════════════════════════════════════════════

def test_brief_reports_unwritten_angles_and_says_what_to_do():
    out = _brief(_client([_angle(1), _angle(2), _angle(3)]))
    assert out["counts"]["angle_debt"] == 3
    note = out["angle_debt_note"]
    assert "有 3 个还没有对应的稿子" in note
    # 光报个数字没用 —— 得告诉模型这一场该怎么处置, 否则它照样接着抽新的。
    assert "优先按这些坐标写" in note and "draw_angles" in note
    assert out["angle_debt"]["since_days"] == core.ANGLE_DEBT_DAYS
    assert out["angle_debt"]["last_drawn_at"]


def test_note_is_for_the_model_not_a_question_for_the_writer():
    """2026-10-30: 原来那句叫模型把数字说给用户、问「接着写完还是放弃」—— 写手每场开头都被问一遍自己做不了主的事。"""
    note = _brief(_client([_angle(1), _angle(2)]))["angle_debt_note"]
    for banned in ("放弃", "你抽", "你在这个项目", "接着写完", "存档", "score_drafts"):
        assert banned not in note, (banned, note)
    assert note.startswith("这个项目"), "一个团队共用一个身份: 说「这个项目」, 不说「你」"
    assert "不用转告用户" in note and "不用问用户" in note
    # 用户点了题目就照用户的写 —— 这些坐标只在对得上的时候优先
    assert "照用户的写" in note


def test_no_debt_means_no_note_at_all():
    """没欠账就别在简报里占地方 —— 每场都印一句"你有 0 个"等于噪音。"""
    out = _brief(_client([_angle(1, consumed="v-1")]))
    assert out["counts"]["angle_debt"] == 0
    assert "angle_debt_note" not in out and "angle_debt" not in out


def test_consumed_angles_are_not_debt():
    out = _brief(_client([_angle(1, consumed="v-1"), _angle(2), _angle(3, consumed="v-3")]))
    assert out["counts"]["angle_debt"] == 1


def test_only_my_own_angles_count():
    """队友抽的牌不记在你头上 —— 和 scope='global' 的私有口径一致。"""
    out = _brief(_client([_angle(1), _angle(2, user=MATE), _angle(3, user=MATE)]))
    assert out["counts"]["angle_debt"] == 1


def test_old_angles_fall_out_of_the_window(monkeypatch):
    """窗口是 7 天。半个月前抽的牌早就不在避重集里了, 再翻出来催无意义。"""
    monkeypatch.setattr(core, "ANGLE_DEBT_SINCE", None)         # 只看窗口, 不让 W-38 那个下限替它挡掉
    out = _brief(_client([_angle(1, days_ago=core.ANGLE_DEBT_DAYS + 3), _angle(2)]))
    assert out["counts"]["angle_debt"] == 1


# ══════════════════════════════════════════════════════════════════════
# 下限: W-38 上线之前抽的牌不算(2026-10-30 发布收尾)
# ══════════════════════════════════════════════════════════════════════

def test_the_floor_is_the_w38_deploy():
    """W-38(查重时就记下稿子)是 2026-10-29 08:26 UTC 上线的; 那之前抽的牌大多写完发出去了, 只是对不回来。"""
    assert datetime.fromisoformat(core.ANGLE_DEBT_SINCE) == datetime(2026, 10, 29, 8, 26, tzinfo=timezone.utc)


def test_a_draw_before_the_floor_is_not_debt_one_after_is(monkeypatch):
    """下限挪到两天前(真实的下限是固定时刻, 测试用相对时间才不随日期变): 三天前抽的不算, 一天前抽的算。"""
    monkeypatch.setattr(core, "ANGLE_DEBT_SINCE", store.iso_ago(2))
    out = _brief(_client([_angle(1, days_ago=3), _angle(2, days_ago=1)]))
    assert out["counts"]["angle_debt"] == 1
    assert [a["angle_key"] for a in out["angle_debt"]["angles"]] == ["k2"]
    assert "有 1 个还没有对应的稿子" in out["angle_debt_note"]
    assert out["angle_debt"]["since_days"] == core.ANGLE_DEBT_DAYS, "返回形状不变"


def test_only_draws_before_the_floor_means_no_note(monkeypatch):
    monkeypatch.setattr(core, "ANGLE_DEBT_SINCE", store.iso_ago(2))
    out = _brief(_client([_angle(1, days_ago=3), _angle(2, days_ago=4)]))
    assert out["counts"]["angle_debt"] == 0
    assert "angle_debt_note" not in out and "angle_debt" not in out


# ══════════════════════════════════════════════════════════════════════
# 刚抽的牌不算(2026-10-30 发布收尾复审)
#
# 一个团队共用一个身份(同一个 user_id): 队友几分钟前刚抽、正在写的牌, 原来会出现在另一个写手新开那场的简报里, 模型照
# 「优先按这些坐标写」再写一遍 —— 两个人写同一张牌。抽牌到查重通常只隔几分钟, 查过重的已按 W-38 的底稿排除, 所以只数
# 抽了一小时以上的(core.ANGLE_DEBT_MIN_AGE); 断掉的那场过一小时再开, 它的牌照样在里面。
# ══════════════════════════════════════════════════════════════════════

MINUTES = 1 / (24 * 60)


def test_a_card_drawn_minutes_ago_is_not_in_the_brief_and_not_recommended():
    out = _brief(_client([_angle(1, days_ago=5 * MINUTES)]))
    assert out["counts"]["angle_debt"] == 0
    assert "angle_debt" not in out and "angle_debt_note" not in out, "队友正在写的牌不该被推荐给另一场"


def test_a_card_drawn_two_hours_ago_is_debt_and_recommended():
    older = _angle(2, days_ago=2 / 24)
    out = _brief(_client([_angle(1, days_ago=5 * MINUTES), older]))
    assert out["counts"]["angle_debt"] == 1
    assert [a["angle_key"] for a in out["angle_debt"]["angles"]] == ["k2"], "两小时前那张照列, 刚抽的那张不列"
    assert out["angle_debt"]["last_drawn_at"] == older["drawn_at"], "最近一次发牌报的是列出来的那张"
    note = out["angle_debt_note"]
    assert "有 1 个还没有对应的稿子" in note and "优先按这些坐标写" in note
    assert "一小时以内" in note, "说清刚发的为什么不在里面, 免得模型以为牌丢了"


def test_the_min_age_is_an_hour():
    assert core.ANGLE_DEBT_MIN_AGE.total_seconds() == 3600


def test_store_until_cuts_both_the_main_and_the_redrawn_query():
    """store.angle_debt 的 until 只数它之前抽的; 「查重之后又抽到一次」补回来的那条查询同一个上界。"""
    c = _client([_angle(1, days_ago=3), _angle(2, days_ago=5 * MINUTES)])
    until = core._angle_debt_until()
    assert store.angle_debt(c, PID, ME, 7, until=until)["unconsumed"] == 1
    assert store.angle_debt(c, PID, ME, 7)["unconsumed"] == 2, "不给 until 照旧全数(archive_pending 走这一路)"
    c2 = _client([_angle(1, days_ago=5 * MINUTES)])
    checked = {"k1": store.iso_ago(4)}                          # 四天前查过重, 几分钟前又抽到一次
    assert store.angle_debt(c2, PID, ME, 7, exclude_keys=checked)["unconsumed"] == 1
    assert store.angle_debt(c2, PID, ME, 7, exclude_keys=checked, until=until)["unconsumed"] == 0


def test_store_floor_never_widens_the_window():
    """下限比窗口还早时按窗口数: 下限只往里收, 不往外放。"""
    c = _client([_angle(1, days_ago=10), _angle(2)])
    assert store.angle_debt(c, PID, ME, 7, since=store.iso_ago(30))["unconsumed"] == 1
    assert store.angle_debt(c, PID, ME, 7, since="不是时间")["unconsumed"] == 1, "读不懂的下限当没给"
    assert store.angle_debt(c, PID, ME, 30, since=store.iso_ago(5))["unconsumed"] == 1


def test_store_floor_also_holds_for_cards_redrawn_after_a_check():
    """查重之后又抽到的那一次照旧算欠着(codex on aw#132) —— 但那一次也得在下限之后: 补回来的那条查询同一个起点。"""
    c = _client([_angle(1, days_ago=3)])
    checked = {"k1": store.iso_ago(4)}                         # 四天前查过重, 三天前又抽到一次
    assert store.angle_debt(c, PID, ME, 7, exclude_keys=checked)["unconsumed"] == 1
    assert store.angle_debt(c, PID, ME, 7, exclude_keys=checked, since=store.iso_ago(2))["unconsumed"] == 0


# ══════════════════════════════════════════════════════════════════════
# 兜底: 这一段绝不能拖垮简报
# ══════════════════════════════════════════════════════════════════════

def test_lookup_failure_never_breaks_the_brief(monkeypatch, caplog):
    """P0 已经拿到了, 欠账查不到只是少句提醒 —— 和借卡同一条纪律。"""
    def boom(*a, **k):
        raise RuntimeError("PostgREST 503")
    monkeypatch.setattr(store, "angle_debt", boom)
    with caplog.at_level("ERROR"):
        out = _brief(_client([_angle(1)]))
    assert out["p0"] is not None and out["counts"]["angle_debt"] == 0
    assert "angle_debt_note" not in out
    # 兜住了也要留痕, 不能吞成看似成功。
    assert any("angle debt" in r.message for r in caplog.records)


def test_store_layer_returns_zero_without_user_id():
    """没有 user_id 时宁可不报, 不报错(简报本身另有归属校验)。"""
    assert store.angle_debt(_client([_angle(1)]), PID, None, 7)["unconsumed"] == 0


def test_store_layer_swallows_query_errors(caplog):
    class Boom:
        def table(self, *a, **k):
            raise RuntimeError("PostgREST 503")
    with caplog.at_level("ERROR"):
        got = store.angle_debt(Boom(), PID, ME, 7)
    assert got == {"unconsumed": 0, "since_days": 7, "last_drawn_at": None, "angles": []}
    assert any("angle debt" in r.message for r in caplog.records)


# ══════════════════════════════════════════════════════════════════════
# 协议
# ══════════════════════════════════════════════════════════════════════

def test_protocol_tells_the_model_to_raise_the_debt_with_the_user():
    body, _ = T.protocol_text()
    assert "angle_debt" in body, "协议没提这笔账, 模型不会知道简报里多了什么"
    skill = T.SKILL_PATH.read_text(encoding="utf-8")
    assert "angle_debt" in skill, "改了 protocol.md 要跑 `sync-skill` 重新生成 SKILL.md"


def test_protocol_no_longer_claims_every_rule_is_soft():
    """2026-09-20 实查: 全库 465 条规则里 hard 已有 53 条。

    协议原文写的是"一条 hard 都没有" —— 那会让模型把真的配置问题当成正常。
    """
    body, _ = T.protocol_text()
    assert "一条 hard 都没有" not in body


# ══════════════════════════════════════════════════════════════════════
# codex review on #86
# ══════════════════════════════════════════════════════════════════════

def test_debt_carries_the_actual_coordinates_not_just_a_count():
    """新开一场对话时, 上一次 draw_angles 的返回不在上下文里了。

    只报个数 = 让模型"写完你不知道是哪几个的角度", 它只能忽略或者再抽一批 ——
    恰好是这条提醒要防的事。
    """
    out = _brief(_client([_angle(1), _angle(2), _angle(3)]))
    keys = [a["angle_key"] for a in out["angle_debt"]["angles"]]
    assert sorted(keys) == ["k1", "k2", "k3"], keys
    assert all("dims" in a for a in out["angle_debt"]["angles"])
    assert "angle_debt.angles" in out["angle_debt_note"]


def test_debt_sample_is_capped_and_says_how_many_it_left_out():
    n = store.ANGLE_DEBT_SAMPLE + 5
    out = _brief(_client([_angle(i) for i in range(n)]))
    assert out["counts"]["angle_debt"] == n
    assert len(out["angle_debt"]["angles"]) == store.ANGLE_DEBT_SAMPLE
    assert "还有 5 个没列" in out["angle_debt_note"]


def test_count_survives_server_side_max_rows_clamp():
    """个数走服务端精确计数, 不是数取回来的行数。

    PostgREST 默认 max-rows 就是 1000; 取回来再 len() 会在欠账最多的那一刻
    悄悄报成 1000。这里把钳位调到 5 来复现同一件事。
    """
    angles = [_angle(i) for i in range(9)]
    client = FakeClient(rows={
        "projects": [{"id": PID, "name": "测试项目", "brand": "测试品",
                      "owner_id": ME, "system_prompt": "你是文案",
                      "calibration_notes": "", "tactics": "[]", "default_params": "{}"}],
        "memories": [], "user_calibration_notes": [], "items": [], "versions": [],
        "batches": [], "draft_fingerprints": [], "angle_ledger": angles, "style_edits": [],
    }, max_rows=5)
    assert store.angle_debt(client, PID, ME, 7)["unconsumed"] == 9


def test_consume_marks_only_my_own_row_when_the_key_was_reissued():
    """坐标占坑 1 天过期后会被重新发牌, 台账里可能同时有两条未消耗行。

    按 (project, key) 一把 update 会把队友那条也标成消耗: 他的欠账凭空消失,
    而这一篇并不是他写的。
    """
    mate_row = _angle(9, user=MATE, days_ago=3)
    my_row = _angle(9, user=ME, days_ago=0)
    my_row["id"] = "ang-mine"
    client = _client([mate_row, my_row])
    assert store.consume_angle(client, PID, "k9", "v-new", user_id=ME) is True
    rows = {r["id"]: r for r in client.rows["angle_ledger"]}
    assert rows["ang-mine"]["consumed_version_id"] == "v-new"
    assert rows["ang-9"]["consumed_version_id"] is None, "队友那条不该被一起销掉"
    # 队友的欠账还在
    assert store.angle_debt(client, PID, MATE, 7)["unconsumed"] == 1


def test_consume_falls_back_to_the_oldest_row_when_none_is_mine():
    """队友把坐标转交给你的情形: 自己没抽过, 仍然要销得掉。"""
    client = _client([_angle(9, user=MATE, days_ago=3)])
    assert store.consume_angle(client, PID, "k9", "v-new", user_id=ME) is True
    assert client.rows["angle_ledger"][0]["consumed_version_id"] == "v-new"


def test_consume_still_reports_false_when_there_is_no_ledger_row(caplog):
    """round-5 那条纪律不许松: 台账没这一行就得照实报 False + 留痕。"""
    with caplog.at_level("WARNING"):
        got = store.consume_angle(_client([]), PID, "k-nope", "v-1", user_id=ME)
    assert got is False
    assert any("no unconsumed ledger row" in r.message for r in caplog.records)


def test_open_project_tool_description_no_longer_claims_every_rule_is_soft():
    """MCP 客户端拿到的是 tools.open_project.__doc__, 协议改了它没改就自相矛盾。"""
    doc = T.open_project.__doc__ or ""
    assert "一条 hard 都没有" not in doc
    assert "angle_debt" in doc, "工具说明里要提这个字段, 否则模型不知道简报多了什么"


def test_open_project_tool_description_does_not_send_the_debt_to_the_user():
    """工具说明是服务端实时下发的, 和协议对不上时以它为准(2026-10-30): 不能再叫模型问用户「接着写完还是放弃」。"""
    doc = T.open_project.__doc__ or ""
    part = doc[doc.index("counts.angle_debt"):].split("\n\n", 1)[0]
    assert "放弃" not in part and "说给用户听" not in part and "调用者自己" not in part, part
    assert "优先按这些坐标写" in part and "不用把这个数转告用户" in part
