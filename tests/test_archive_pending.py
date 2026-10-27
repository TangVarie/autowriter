"""W-29: 发牌 / 查重 / 记规则的返回里带上"这些牌还没存档, 下一步是什么"。

2026-10-20~10-22 途鸽五轮 73 张牌、百健士一轮 24 张, 一轮都没存档。生产日志里每一轮 open_project、draw_angles、
1~3 次 check_drafts 都回了 200, 然后调用方就不再调写作台了: score_drafts 一次都没到库。和 W-06 同一个形状,
D-071 定过不设硬闸, 所以只在离交付最近的几个返回值里提醒, 不拦任何东西。

这里钉的是【被禁止的形态】:
  · 有没存档的牌却不提醒, 或者张数 / 最早时间不对;
  · 把别人的牌、别的项目的牌、早就存过档的牌、窗口外的牌算进来;
  · 查这笔账失败把发牌 / 查重 / 记规则拖垮(查重是硬闸, 它的结果一个字都不能因为一段提醒丢掉);
  · 为这段提醒多打不止一次查询。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from deskcore import core, store
from deskcore import tools as T
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
MATE = "22222222-2222-2222-2222-222222222222"
PID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_PID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _hours_ago(h: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()


def _card(i, *, user=ME, project=PID, hours_ago=1.0, consumed=False):
    return {"id": f"ang-{i}", "project_id": project, "angle_key": f"k{i}", "dims": {},
            "drawn_by": user, "drawn_at": _hours_ago(hours_ago),
            "consumed_version_id": f"v-{i}" if consumed else None,
            "consumed_at": store.iso_now() if consumed else None}


def _client(cards=()):
    return FakeClient(rows={
        "projects": [
            {"id": PID, "name": "测试项目", "brand": "测试品", "owner_id": ME,
             "calibration_notes": "", "tactics": "[]", "custom_roles": []},
            {"id": OTHER_PID, "name": "我的另一个项目", "brand": "别的品", "owner_id": ME,
             "calibration_notes": "", "tactics": "[]", "custom_roles": []},
        ],
        "angle_ledger": list(cards), "draft_fingerprints": [],
    })


@pytest.fixture()
def use(monkeypatch):
    # 存档时 key 对不上的那一笔记在进程内存里(W-31): 每条用例从空的开始, 别的文件存过的不串进来
    monkeypatch.setattr(core, "_angle_key_misses", {})

    def _use(client):
        monkeypatch.setattr(core, "sb", lambda: client)
        return client
    return _use


def _ledger_calls(client):
    return [c for c in client.calls if c["table"] == "angle_ledger"]


# ══════════════════════════════════════════════════════════════════════
# 主路径: 只数调用者自己、这个项目、近 24 小时、还没存档的牌
# ══════════════════════════════════════════════════════════════════════

def test_check_drafts_carries_count_and_oldest_for_my_unsettled_cards(use):
    oldest = _card(3, hours_ago=5)
    c = use(_client([
        _card(1, hours_ago=2), _card(2, hours_ago=2),            # 这一轮(同一次发牌)
        oldest,                                                   # 上一轮, 也没存档
        _card(4, hours_ago=1, consumed=True),                     # 存过档的
        _card(5, user=MATE), _card(6, user=MATE, hours_ago=8),    # 队友的
        _card(7, project=OTHER_PID),                              # 我在别的项目的
        _card(8, hours_ago=24 * 3),                               # 窗口外
    ]))
    out = T.check_drafts(PID, [], _user_id=ME)
    block = out["archive_pending"]
    assert block == {"cards": 3, "oldest_drawn_at": oldest["drawn_at"],
                     "next_step": core.ARCHIVE_PENDING_NEXT_STEP}
    # 判定结果本身一个键都没少、没改
    assert out["results"] == [] and out["summary"]["total"] == 0
    # 只多一次查询, 而且是服务端计数 + 只取一行
    calls = _ledger_calls(c)
    assert len(calls) == 1, calls
    assert calls[0]["count"] == "exact" and calls[0]["range"] == (0, 0)
    assert ("eq", "drawn_by", ME) in calls[0]["filters"]
    assert ("eq", "project_id", PID) in calls[0]["filters"]


def test_no_block_when_nothing_is_pending(use):
    use(_client([_card(1, consumed=True), _card(2, user=MATE), _card(3, project=OTHER_PID),
                 _card(4, hours_ago=30)]))
    out = T.check_drafts(PID, [], _user_id=ME)
    assert "archive_pending" not in out
    assert out == {"results": [], "summary": {"total": 0, "pass": 0, "warn": 0, "reject": 0}}


def test_other_users_cards_never_count_for_me(use):
    """队友在同一个项目上抽了一堆没写: 记不到我头上, 也不能借这段提醒看到他抽了什么。"""
    use(_client([_card(i, user=MATE) for i in range(6)]))
    assert "archive_pending" not in T.check_drafts(PID, [], _user_id=ME)


def test_draw_counts_the_cards_it_just_drew(use):
    """挂在发牌返回上, 它说的就是"这一批写完要存档": 张数含刚发的牌(发牌成功之后才查)。"""
    earlier = _card(1, hours_ago=6)
    c = use(_client([earlier]))
    out = T.draw_angles(PID, 3, _user_id=ME)
    assert out["delivered"] == 3
    block = out["archive_pending"]
    assert block["cards"] == 4, block                      # 刚发的 3 张 + 6 小时前那张
    assert block["oldest_drawn_at"] == earlier["drawn_at"]
    assert "score_drafts" in block["next_step"]
    # 发牌本身的返回不受影响
    assert {"angles", "requested", "delivered", "prompt_block"} <= set(out)
    assert sum(1 for r in c.rows["angle_ledger"] if r["drawn_by"] == ME) == 4


def test_record_rule_carries_the_block(use, monkeypatch):
    use(_client([_card(1, hours_ago=3)]))
    stored = {"memory_id": "m-1", "severity": "soft", "scope": "project", "content": "别用数字开头"}
    monkeypatch.setattr(core, "record_rule", lambda *a, **k: dict(stored))
    out = T.record_rule(PID, "别用数字开头", _user_id=ME)
    assert out["archive_pending"]["cards"] == 1
    assert {k: v for k, v in out.items() if k != "archive_pending"} == stored


def test_no_user_no_block_and_no_query(use, monkeypatch):
    """认不出调用者时不查(工具本身会先被归属闸拒掉; 这里只验提醒这一层不多打请求)。"""
    c = use(_client([_card(1)]))
    monkeypatch.setattr(core, "check_drafts", lambda *a, **k: {"results": [], "summary": {}})
    out = T.check_drafts(PID, [], _user_id=None)
    assert "archive_pending" not in out
    assert _ledger_calls(c) == []


# ══════════════════════════════════════════════════════════════════════
# 兜底: 这段提醒怎么坏都不能把工具结果弄坏
# ══════════════════════════════════════════════════════════════════════

def test_helper_raising_does_not_break_check_drafts(use, monkeypatch, caplog):
    use(_client([_card(1)]))

    def boom(*a, **k):
        raise RuntimeError("PostgREST 503")
    monkeypatch.setattr(core, "archive_pending", boom)
    with caplog.at_level("WARNING", logger="deskcore.tools"):
        out = T.check_drafts(PID, [], _user_id=ME)
    assert out == {"results": [], "summary": {"total": 0, "pass": 0, "warn": 0, "reject": 0}}
    assert any("archive_pending" in r.getMessage() for r in caplog.records), "兜住了也要留痕"


def test_query_failure_inside_the_store_omits_the_block(use, monkeypatch):
    """store.angle_debt 自己兜住查询错误回 0 —— 这时不带提醒, 也不报成"0 张"。"""
    use(_client([_card(1)]))

    def boom(*a, **k):
        raise RuntimeError("PostgREST 503")
    monkeypatch.setattr(store, "angle_debt", boom)
    for call in (lambda: T.check_drafts(PID, [], _user_id=ME),
                 lambda: T.draw_angles(PID, 2, _user_id=ME)):
        assert "archive_pending" not in call()


def test_client_construction_failure_omits_the_block(monkeypatch):
    """第二次取 client 就炸(配置 / 网络): 工具已经拿到的结果照样回去。"""
    c = _client([_card(1)])
    got = iter([c])

    def sb():
        try:
            return next(got)
        except StopIteration:
            raise RuntimeError("client gone") from None
    monkeypatch.setattr(core, "sb", sb)
    out = T.check_drafts(PID, [], _user_id=ME)
    assert out["summary"]["total"] == 0 and "archive_pending" not in out


def test_the_tool_error_itself_still_propagates(use):
    """提醒在工具成功之后才查: 归属闸的拒绝照样冒出去, 不会被这一段吞成"成功"。"""
    use(_client([_card(1)]))
    with pytest.raises(PermissionError):
        T.check_drafts(PID, [], _user_id=MATE)


# ══════════════════════════════════════════════════════════════════════
# 口径
# ══════════════════════════════════════════════════════════════════════

def test_store_default_shape_is_unchanged_and_oldest_first_adds_one_key():
    c = _client([_card(1, hours_ago=1), _card(2, hours_ago=4)])
    newest = store.angle_debt(c, PID, ME, 1)
    assert set(newest) == {"unconsumed", "since_days", "last_drawn_at", "angles"}
    oldest = store.angle_debt(c, PID, ME, 1, sample=1, oldest_first=True)
    assert oldest["unconsumed"] == 2 and len(oldest["angles"]) == 1
    assert oldest["oldest_drawn_at"] == c.rows["angle_ledger"][1]["drawn_at"]
    assert oldest["oldest_drawn_at"] < newest["last_drawn_at"]


def test_next_step_says_archive_before_showing_and_how_to_resave_edits():
    step = core.ARCHIVE_PENDING_NEXT_STEP
    for must in ("score_drafts", "同一轮", "交给用户之前", "replaces_version_id", "angle_key"):
        assert must in step, must
    assert step.count("。") == 1 and step.endswith("。"), "一句话"
    # 改存过的那篇: 直接 score_drafts 带 replaces_version_id。叫它先跑 check_drafts 是错的 —— check_drafts 不认
    # replaces_version_id, 拿改稿和它自己存档的原版比, 必然 reject(review on W-29)。
    revise = step.split("replaces_version_id", 1)[0].rsplit(";", 1)[-1]
    assert "已经存过" in revise and "score_drafts" in revise, revise
    assert "先重跑 check_drafts" not in step and "不用先跑 check_drafts" in step
    # 只有整篇重写的新稿才先过 check_drafts
    assert "整篇推翻" in step and "先过 check_drafts" in step


def test_open_project_debt_note_points_at_score_drafts(use):
    """简报那句 angle_debt_note 原来说"写完照样每篇带 angle_key 提交", 现在说清楚提交就是同一轮里的 score_drafts。"""
    use(_client([_card(1, hours_ago=30), _card(2, hours_ago=50)]))
    c = core.sb()
    c.rows.update({"memories": [], "user_calibration_notes": [], "items": [], "versions": [],
                   "batches": [], "style_edits": []})
    c.rows["projects"][0].update({"system_prompt": "你是文案", "default_params": "{}"})
    out = core.build_writing_brief(c, PID, user_id=ME)
    note = out["angle_debt_note"]
    assert "score_drafts" in note and "同一轮" in note and "angle_key" in note
    assert "archive_pending" not in out, "简报已经有 angle_debt, 不再叠第二段"


# ══════════════════════════════════════════════════════════════════════
# W-31(10-27): 存档带的 angle_key 对不上台账, 下一次提醒要说清 —— 不然它只会说「还有 N 张没存档」
# ══════════════════════════════════════════════════════════════════════

BODY_1 = ("租房第一个月我把押金条拍了三遍, 后来退租时房东说没见过那张纸, 幸亏手机里还有原图, "
          "这件事让我明白收据这种东西永远要自己留一份。") * 3
BODY_2 = ("绩点二点八的澳八大回国找工作, 我一开始也以为没人要, 后来发现问题"
          "出在简历的第一行, 那行字把面试官最想看的东西藏到了最后。") * 3


def _archive(client, drafts, *, user=ME):
    client.rpc_impl["deskcore_commit_fingerprints"] = lambda args: [
        {"idx": i, "status": core.COMMIT_STATUS_INSERTED, "collided_with": None, "detail": None}
        for i in range(len(args.get("_rows") or []))]
    return core.commit_drafts(client, PID, drafts, user_id=user)


@pytest.fixture()
def no_embeddings(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)


def test_next_check_says_the_cards_were_archived_under_keys_the_ledger_does_not_have(use, no_embeddings):
    """10-26 sportsix 的形状: 牌发了, 稿子也存了, key 却是模型自己编的 —— 牌一张没销。"""
    c = use(_client([_card(1, hours_ago=2), _card(2, hours_ago=2)]))
    out = _archive(c, [{"title": "甲", "body": BODY_1, "angle_key": "A_01"},
                       {"title": "乙", "body": BODY_2, "angle_key": "A_02"}])
    assert out["written"] == 2 and out["unknown_angle_keys"] == 2

    for call in (lambda: T.check_drafts(PID, [], _user_id=ME),
                 lambda: T.draw_angles(PID, 1, _user_id=ME)):
        block = call()["archive_pending"]
        mm = block["angle_key_mismatch"]
        assert (mm["drafts"], mm["unknown"], mm["used"]) == (2, 2, 0)
        assert mm["keys"] == ["A_01", "A_02"]
        assert mm["last_at"]
        for must in ("已经写完存档", "不用重存", "别按这些牌再写一遍", "原样复制", "20 位"):
            assert must in mm["note"], must
        assert block["next_step"] == core.ARCHIVE_PENDING_NEXT_STEP, "原来那句不动"


def test_mismatches_add_up_across_batches_and_cost_no_extra_query(use, no_embeddings):
    c = use(_client([_card(i, hours_ago=1) for i in range(4)]))
    _archive(c, [{"title": "甲", "body": BODY_1, "angle_key": "A_01"}])
    _archive(c, [{"title": "乙", "body": BODY_2, "angle_key": "k0"},          # 对的: 销了
                 {"title": "丙", "body": BODY_1 + "又一篇", "angle_key": "k0"}])  # 同一个 key 第二篇: used
    n = len(_ledger_calls(c))
    block = T.check_drafts(PID, [], _user_id=ME)["archive_pending"]
    assert len(_ledger_calls(c)) == n + 1, "提醒照旧只多一次查询(对不上的那一笔在内存里)"
    mm = block["angle_key_mismatch"]
    assert (mm["drafts"], mm["unknown"], mm["used"]) == (2, 1, 1)
    assert mm["keys"] == ["A_01", "k0"]


def test_someone_elses_bad_keys_are_not_shown_to_me(use, no_embeddings):
    """记的是 (项目, 存档的人) 那一笔: 别人存档时 key 对不上, 不出现在我的提醒里; 我在别的项目上的也不算。"""
    c = use(_client([_card(1, hours_ago=1), _card(2, user=MATE, hours_ago=1)]))
    miss = [{"index": 0, "angle_key": "MATE_01", "length": 7, "problem": "unknown"}]
    core._note_angle_key_misses(PID, MATE, miss)
    core._note_angle_key_misses(OTHER_PID, ME, miss)
    block = T.check_drafts(PID, [], _user_id=ME)["archive_pending"]
    assert block["cards"] == 1 and "angle_key_mismatch" not in block
    assert core.archive_pending(c, PID, user_id=MATE)["angle_key_mismatch"]["keys"] == ["MATE_01"]


def test_no_block_without_pending_cards_and_the_note_expires_with_the_window(use, no_embeddings, monkeypatch):
    c = use(_client([_card(1, hours_ago=1)]))
    _archive(c, [{"title": "甲", "body": BODY_1, "angle_key": "A_01"}])
    assert "angle_key_mismatch" in T.check_drafts(PID, [], _user_id=ME)["archive_pending"]

    later = datetime.now(timezone.utc) + timedelta(days=core.ARCHIVE_PENDING_DAYS, hours=1)
    monkeypatch.setattr(core, "_utcnow", lambda: later)
    assert "angle_key_mismatch" not in T.check_drafts(PID, [], _user_id=ME)["archive_pending"], "过了窗口不再提"

    c.rows["angle_ledger"][0]["consumed_version_id"] = "v-x"
    monkeypatch.setattr(core, "_utcnow", lambda: datetime.now(timezone.utc))
    assert "archive_pending" not in T.check_drafts(PID, [], _user_id=ME), "牌都销了就不提"


def test_an_old_miss_is_not_added_to_a_new_one(use, no_embeddings, monkeypatch):
    """记的那一边也按窗口重新开始(W-31 复审): 一天前的那一笔不许接着累加进新的一笔, 否则提醒里的篇数和 key 越滚越旧。"""
    use(_client([_card(1, hours_ago=1)]))
    t0 = datetime.now(timezone.utc)
    monkeypatch.setattr(core, "_utcnow", lambda: t0)
    core._note_angle_key_misses(PID, ME, [{"index": 0, "angle_key": "OLD_01", "length": 6, "problem": "unknown"}])
    later = t0 + timedelta(days=core.ARCHIVE_PENDING_DAYS, hours=1)
    monkeypatch.setattr(core, "_utcnow", lambda: later)
    core._note_angle_key_misses(PID, ME, [{"index": 0, "angle_key": "NEW_01", "length": 6, "problem": "used"}])

    mm = T.check_drafts(PID, [], _user_id=ME)["archive_pending"]["angle_key_mismatch"]
    assert (mm["drafts"], mm["unknown"], mm["used"]) == (1, 0, 1)
    assert mm["keys"] == ["NEW_01"]


def test_the_note_only_says_invented_or_already_written_when_that_is_the_problem(use):
    """别的项目的牌、台账里没有这一行的: 这个项目挂着的牌跟那几篇无关, 不许说「这些牌其实写完存了」; 形状对的不说
    「不要自己编号」(W-31 复审)。"""
    use(_client([_card(1, hours_ago=1)]))
    key = "0123456789abcdef0123"
    core._note_angle_key_misses(PID, ME, [
        {"index": 0, "angle_key": key, "length": 20, "problem": "other_project", "project_id": OTHER_PID},
        {"index": 1, "angle_key": key[::-1], "length": 20, "problem": "not_in_ledger"}])
    mm = T.check_drafts(PID, [], _user_id=ME)["archive_pending"]["angle_key_mismatch"]
    assert (mm["drafts"], mm["unknown"], mm["used"]) == (2, 2, 0), "这个项目台账里没有的三种都算 unknown"
    assert mm["by_problem"] == {"other_project": 1, "not_in_ledger": 1}
    note = mm["note"]
    assert "存错了项目" in note and "发牌时台账没写进去" in note
    for bad in ("已经写完存档", "别按这些牌再写一遍", "不要自己编号"):
        assert bad not in note, bad

    core._note_angle_key_misses(PID, ME, [{"index": 0, "angle_key": "A_01", "length": 4, "problem": "unknown"}])
    note = T.check_drafts(PID, [], _user_id=ME)["archive_pending"]["angle_key_mismatch"]["note"]
    assert "已经写完存档" in note and "不要自己编号" in note and "存错了项目" in note
