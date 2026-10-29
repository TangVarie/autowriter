"""W-38(2026-10-29): 查重时记下的底稿, tv-sync 拿飞书的帖来对。

写手没有「存档」这一步(D-120)。模型没调 score_drafts 的稿子, 写作台只在 check_drafts 那一刻见过它: 这里钉的是
  · check_drafts 把每篇(有正文的)记进 draft_snapshots, 记不上也不碰查重的判定;
  · tvlink.match_snapshot 比 match_note 严(底稿要早于帖、开头 / 标题一样也要包含度、分不出不硬猜);
  · tv_sync 对不上写作台版本的帖先对底稿, 对上了转成真的版本(建身份、写指纹、销牌、记转正、同一晚算改稿差异),
    对不上的照旧补录; 读不出底稿 / 转不成的那晚不补录(补录是终态);
  · 简报 / 入库后的 angle_debt 不把查重时带过的牌算成「抽了没写」。
"""

from __future__ import annotations

import contextlib
import random
from datetime import datetime, timedelta, timezone

import pytest

from deskcore import cli, core, store, tvlink
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
WRITER = "33333333-3333-3333-3333-333333333333"
P1 = "aaaaaaaa-0000-0000-0000-000000000001"
TV = "TUGE_phase1"
T0 = datetime(2026, 10, 20, 4, 0, tzinfo=timezone.utc)
KEY = "0123456789abcdef0123"
KEY2 = "fedcba9876543210fedc"
BODY_A = ("投到第六十份的时候我开始怀疑是不是邮箱坏了, 后来发现坏的是简历第一行。"
          "改完之后一周里来了三个面试, 才知道之前的六十份全都倒在第一行上。")
BODY_S = ("周末骑了一百公里, 第六十公里的时候腿开始发抖, 补给站的香蕉早就吃完了。"
          "后来在路边小卖部买了两根能量胶, 撑到终点才发现袋子里还剩一根没开。") * 2
BODY_OTHER = ("这一篇写作台里根本没有: 上周发的, 当时也没查过重。"
              "指纹库不知道它, 下一批查重看不见它, 所以要从 TV 补回来。") * 2


def _iso(days, hours=0):
    return (T0 + timedelta(days=days, hours=hours)).isoformat()


def _pub(days):
    """TV 的 publish_time: 北京那天的 0 点, 存成前一天 16:00 UTC(10-29 生产库 2542 篇里 2524 篇是这样)。T0 是北京 10-20 12:00。"""
    d = (T0 + timedelta(days=days)).date()
    return datetime(d.year, d.month, d.day, 16, tzinfo=timezone.utc).__sub__(timedelta(days=1)).isoformat()


def _snap(sid, title, body, *, days=0, key=KEY, user=WRITER, check="c1", pid=P1, **extra):
    return {"id": sid, "project_id": pid, "user_id": user, "check_id": check, "title": title, "body": body,
            "angle_key": key, "status": "pass", "flywheel_arm": "patterns", "flywheel_arm_source": "borrow",
            "text_fp": tvlink.text_fp(tvlink.Note(note_id="", title=title, body=tvlink.strip_hashtags(body),
                                                  publish_time=None)),
            "checked_at": _iso(days), "version_id": None, "promoted_at": None, "note_id": None, **extra}


def _note(nid, title, body, days=2, **extra):
    return {"note_id": nid, "raw_content": f"【标题】{title} 【正文】{body} #骑行 #能量胶",
            "publish_time": _pub(days), "tier": "趴", "created_at": _iso(days), **extra}


def _client(notes, snaps, *, with_target=True, ledger=None, links=None):
    c = FakeClient(rows={
        "projects": [{"id": P1, "name": "途鸽", "brand": "途鸽", "owner_id": ME,
                      "calibration_notes": "", "tactics": "[]", "custom_roles": []}],
        "tv_project_map": [{"tv_project_id": TV, "project_id": P1, "ingest_target": with_target, "note": ""}],
        "batches": [{"id": "b1", "project_id": P1, "user_id": ME, "params": {"source": "deskcore"}}],
        "items": [{"id": "i1", "batch_id": "b1", "best_version_id": "v1", "created_at": _iso(-5),
                   "user_id": ME, "status": "pending",
                   "versions": [{"id": "v1", "title": "秋招投了六十份", "body": BODY_A,
                                 "version_num": 1, "created_at": _iso(-5)}],
                   "batches": {"project_id": P1, "params": {"source": "deskcore"}}}],
        "versions": [{"id": "v1", "item_id": "i1", "title": "秋招投了六十份", "body": BODY_A,
                      "version_num": 1, "created_at": _iso(-5)}],
        "tv_note_links": list(links or []),
        "draft_fingerprints": [],
        "draft_snapshots": list(snaps),
        "angle_ledger": list(ledger if ledger is not None else [
            {"id": "a1", "project_id": P1, "angle_key": KEY, "dims": {}, "drawn_by": WRITER,
             "drawn_at": _iso(-1), "consumed_version_id": None, "consumed_at": None}]),
    })
    calls = {"backfill": []}

    def _tv_notes(a):
        rows = [n for n in notes if a.get("_after") is None or n["note_id"] > a["_after"]]
        return rows[: a["_limit"]]

    def _backfill(a):
        calls["backfill"].append(a["_links"])
        return len(a["_links"])
    c.rpc_impl = {"deskcore_tv_notes": _tv_notes, "deskcore_tv_backfill_lineage": _backfill}
    c.tv_calls = calls
    return c


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)


# ══════════════════════════════════════════════════════════════════════
# 1 · tvlink.match_snapshot —— 比 match_note 严
# ══════════════════════════════════════════════════════════════════════

def _v(vid, title, body, days):
    return tvlink.Version(version_id=vid, project_id=P1, title=title, body=tvlink.strip_hashtags(body),
                          created_at=tvlink._as_dt(_iso(days)))


def _n(title, body, days=2, *, publish=True):
    return tvlink.Note(note_id="n", title=title, body=tvlink.strip_hashtags(body),
                       publish_time=tvlink._as_dt(_pub(days)) if publish else None)


def _m(note, snaps, main=None):
    return tvlink.match_snapshot(note, snaps, tvlink.VersionIndex(snaps), main)


def _cjk(n, seed):
    """n 个随机汉字(四字串几乎不会和别的段撞)。按 seed 定, 不受用例顺序影响。"""
    rng = random.Random(seed)
    return "".join(chr(rng.randint(0x4E00, 0x9FA5)) for _ in range(n))


def _contain(post, draft):
    ng = tvlink.grams(tvlink.strip_hashtags(post))
    return len(ng & tvlink.grams(tvlink.strip_hashtags(draft))) / len(ng)


# 同一篇的前后两稿: 第一稿(标题 / 开头 O), 改了标题、开头、加了一段 Q 的第二稿; 帖保留第一稿的标题和开头、用了第二稿加的那段
_O, _M1, _M2, _E1, _O2, _Q, _R = (_cjk(n, i) for i, n in enumerate((45, 40, 150, 30, 45, 55, 84)))
REV1 = _O + "。" + _M1 + "。" + _M2 + "。" + _E1
REV2 = _O2 + "。" + _M1 + "。" + _M2 + "。" + _Q
POST_KEPT_TITLE = _O + "。" + _M1 + "。" + _Q + "。" + _R


def test_same_text_matches_and_lag_is_counted_in_beijing_days_from_the_first_check():
    m = _m(_n("骑了一百公里", BODY_S, days=3), [_v("s1", "骑了一百公里", BODY_S, 0)])
    assert m.kind == "body_exact" and m.version.version_id == "s1" and m.lag_days == 3 and m.score == 1.0


def test_written_and_published_the_same_beijing_day_counts():
    # 北京 10-20 12:00 查重、10-20 发(publish_time 存成 10-19 16:00 UTC): 按 UTC 日期减是 −1, 那样转不了正、也不算接住
    m = _m(_n("骑了一百公里", BODY_S, days=0), [_v("s1", "骑了一百公里", BODY_S, 0)])
    assert m.kind == "body_exact" and m.lag_days == 0
    assert tvlink.lag_days(_n("t", BODY_S, days=0), _v("s1", "t", BODY_S, 0)) == 0, "lag_days 本身也按北京日"


def test_a_snapshot_checked_after_the_post_was_published_is_not_its_source():
    # 发了之后才查重的那一稿(抄回来的)不认 —— 同 DIFF_REV 2 / 判据 C 的"接住"口径
    assert _m(_n("骑了一百公里", BODY_S, days=0), [_v("s1", "骑了一百公里", BODY_S, 1)]).kind == "unmatched"


def test_too_old_snapshots_and_posts_without_a_publish_date_are_ignored():
    days = tvlink.WINDOW_AFTER_DAYS + 1
    assert _m(_n("骑了一百公里", BODY_S, days=days), [_v("s1", "骑了一百公里", BODY_S, 0)]).kind == "unmatched"
    # 没发布日期的不认: TV 读到它的时刻可能比发布晚很久(新接的表), 拿它当上限会把发了之后才写的稿认成来源
    assert _m(_n("骑了一百公里", BODY_S, publish=False), [_v("s1", "骑了一百公里", BODY_S, 0)]).kind == "unmatched"


def test_same_title_different_body_is_not_promoted():
    # match_note 的 title_exact 一条候选就认、不看包含度; 转正要建版本, 这里必须看
    assert _m(_n("骑了一百公里", BODY_OTHER), [_v("s1", "骑了一百公里", BODY_S, 0)]).kind == "unmatched"


def test_heavily_rewritten_posts_need_more_than_the_main_floor():
    # 帖里只有一部分来自底稿: 主索引 0.30 就认 edited, 转正要到 SNAPSHOT_EDITED_FLOOR(W-34 的「整篇换了」线就是 0.30)
    sents = BODY_S.split("。")
    note_body = sents[0] + "。" + BODY_OTHER          # 约四成来自底稿
    m = _m(_n("t", note_body), [_v("s1", "t2", BODY_S, 0)])
    ng = tvlink.grams(note_body)
    c = len(ng & tvlink.grams(BODY_S)) / len(ng)
    assert tvlink.EDITED_FLOOR <= c < tvlink.SNAPSHOT_EDITED_FLOOR, c
    assert m.kind == "unmatched", m


def test_a_heavily_rewritten_post_that_kept_the_title_or_opening_only_needs_the_main_floor():
    # 开头 / 标题一样的不受 SNAPSHOT_EDITED_FLOOR 管(codex on aw#132): 照样转正, 不补录成副本
    sents = BODY_S.split("。")
    note_body = sents[0] + "。" + BODY_OTHER
    m = _m(_n("骑了一百公里", note_body), [_v("s1", "骑了一百公里", BODY_S, 0)])
    assert m.kind == "title_exact" and m.version.version_id == "s1", m
    assert tvlink.EDITED_FLOOR <= m.score < tvlink.SNAPSHOT_EDITED_FLOOR, m.score
    opening = BODY_S[:45] + BODY_OTHER
    m = _m(_n("t", opening), [_v("s1", "t2", BODY_S, 0)])
    assert m.kind == "body_exact" and m.score < tvlink.SNAPSHOT_EDITED_FLOOR, m
    # 包含度不到 EDITED_FLOOR 的, 标题一样也不认(同上面 test_same_title_different_body_is_not_promoted)
    assert _m(_n("骑了一百公里", BODY_OTHER), [_v("s1", "骑了一百公里", BODY_S, 0)]).kind == "unmatched"


def test_revisions_of_one_draft_pick_the_one_that_was_published():
    v1 = _v("s1", "骑了一百公里", BODY_S, 0)
    v2 = _v("s2", "骑了一百公里", BODY_S + "最后那根能量胶我留着, 下次骑二百公里的时候再开。", 1)
    # 发出去的是第一稿: 两稿都完整包含它(帖在稿里 1.0 / 1.0), 第一稿多出来的少 → 第一稿
    m = _m(_n("骑了一百公里", BODY_S, days=2), [v1, v2])
    assert m.kind == "body_exact" and m.version.version_id == "s1", m
    # 发出去的是第二稿: 只有第二稿完整包含 → 第二稿
    assert _m(_n("骑了一百公里", v2.body, days=2), [v1, v2]).version.version_id == "s2"


def test_two_different_drafts_that_both_fit_are_ambiguous_not_guessed():
    half = BODY_S[: len(BODY_S) // 2]
    a = _v("s1", "A", half + BODY_A, 0)
    b = _v("s2", "B", half + BODY_OTHER, 0)
    m = _m(_n("帖", half, days=1), [a, b])
    assert m.kind == "ambiguous" and m.version is None and len(m.candidates) == 2


def test_an_archived_version_just_as_close_makes_it_ambiguous():
    # 同一个故事写了两版: 一版存了档(主索引), 一版只查了重 —— 帖离两版差不多近, 主索引自己也会判 ambiguous
    snap = _v("s1", "骑了一百公里", BODY_S, 0)
    archived = tvlink.Version(version_id="v9", project_id=P1, title="骑行", body=BODY_S + "多写了一句。",
                              created_at=tvlink._as_dt(_iso(0)))
    m = _m(_n("骑了一百公里", BODY_S, days=1), [snap], tvlink.VersionIndex([archived]))
    assert m.kind == "ambiguous" and {c["version_id"] for c in m.candidates} == {"s1", "v9"}, m
    assert _m(_n("骑了一百公里", BODY_S, days=1), [snap], tvlink.VersionIndex([])).kind == "body_exact"


def test_an_exact_hit_below_a_revision_of_the_same_draft_is_still_promoted():
    # 开头 / 标题一样的那份分数低一点(0.37), 分数高的(0.40)是它改过标题 / 开头的下一稿: 认一样的那份, 不补录(复审 on aw#132)
    assert tvlink.EDITED_FLOOR <= _contain(POST_KEPT_TITLE, REV1) < _contain(POST_KEPT_TITLE, REV2) \
        < tvlink.SNAPSHOT_EDITED_FLOOR
    s1, s2 = _v("s1", "骑了一百公里", REV1, 0), _v("s2", "百公里之后", REV2, 1)
    m = _m(_n("骑了一百公里", POST_KEPT_TITLE, days=3), [s1, s2])
    assert m.kind == "body_exact" and m.version.version_id == "s1", m


def test_an_exact_hit_below_an_unrelated_draft_is_ambiguous_not_ingested():
    # 分数高的那份不是它的前后几稿(别的稿子): 分不出, 不硬猜也不补录
    other = _v("s2", "别的稿子", _cjk(45, 90) + "。" + _M1 + "。" + _Q + "。" + _cjk(300, 91), 1)
    s1 = _v("s1", "骑了一百公里", REV1, 0)
    assert _contain(POST_KEPT_TITLE, other.body) > _contain(POST_KEPT_TITLE, REV1)
    assert not tvlink._same_draft(s1, other, tvlink.VersionIndex([s1, other]))
    assert _m(_n("骑了一百公里", POST_KEPT_TITLE, days=3), [s1, other]).kind == "ambiguous"


def test_product_talk_shared_with_another_draft_does_not_make_an_exact_hit_ambiguous():
    # 开头 / 标题一样、包含度 0.32 的底稿 vs 只是共用产品话术(0.19)的别的稿子(存过档的 / 别的底稿): 同 match_note 的精确档,
    # 不拿只是内容像的去比(复审 on aw#132)
    talk = _cjk(45, 20)
    snap_body = _cjk(45, 21) + "。" + _cjk(30, 22) + "。" + _cjk(150, 23)
    post = snap_body[:76] + "。" + talk + "。" + _cjk(110, 24)
    other_body = _cjk(200, 25) + "。" + talk
    assert tvlink.EDITED_FLOOR <= _contain(post, snap_body) < tvlink.SNAPSHOT_EDITED_FLOOR
    assert 0.1 < _contain(post, other_body) < tvlink.EDITED_FLOOR
    s = _v("s1", "骑了一百公里", snap_body, 0)
    archived = tvlink.Version(version_id="v9", project_id=P1, title="别的稿子", body=other_body,
                              created_at=tvlink._as_dt(_iso(0)))
    note = _n("骑了一百公里", post, days=2)
    assert _m(note, [s], tvlink.VersionIndex([archived])).kind == "body_exact"
    assert _m(note, [s, _v("s2", "别的稿子", other_body, 0)]).kind == "body_exact"


def test_body_identical_revisions_pick_the_title_the_post_kept():
    # 只改了标题又查了一次(正文一样, 两份分数打平): 取标题和帖一样的那份, 不取最晚查的(复审 on aw#132)
    body = _cjk(200, 30)
    first, retitled = _v("s1", "原来的标题写法", body, 0), _v("s2", "改过的标题写法", body, 1)
    m = _m(_n("原来的标题写法", body, days=2), [first, retitled])
    assert m.version.version_id == "s1", m
    assert _m(_n("改过的标题写法", body, days=2), [first, retitled]).version.version_id == "s2"


def test_short_posts_are_not_matched():
    assert _m(_n("短", "骑了一百公里", days=1), [_v("s1", "短", "骑了一百公里", 0)]).kind == "unmatched"


# ══════════════════════════════════════════════════════════════════════
# 2 · check_drafts 记底稿
# ══════════════════════════════════════════════════════════════════════

def _desk(rows=None):
    return FakeClient(rows={
        "projects": [{"id": P1, "name": "途鸽", "owner_id": WRITER, "calibration_notes": "",
                      "tactics": "[]", "custom_roles": []}],
        "draft_fingerprints": [], "draft_snapshots": [], **(rows or {})})


def test_check_drafts_records_every_draft_with_a_body_and_its_angle_key():
    c = _desk()
    out = core.check_drafts(c, P1, [{"title": "骑了一百公里", "body": BODY_S + " #骑行", "angle_key": KEY},
                                    {"title": "只有标题", "body": "  #骑行 "},
                                    {"title": "秋招", "body": BODY_A}], user_id=WRITER, record_snapshot=True)
    assert out["summary"]["total"] == 3
    rows = c.rows["draft_snapshots"]
    assert [r["title"] for r in rows] == ["骑了一百公里", "秋招"], "没正文的(剥了话题是空的)不记"
    assert len({r["check_id"] for r in rows}) == 1, "一次查重一个 check_id"
    assert rows[0]["angle_key"] == KEY and rows[1]["angle_key"] is None
    assert rows[0]["user_id"] == WRITER and rows[0]["project_id"] == P1
    assert rows[0]["status"] == "pass" and rows[0]["flywheel_arm"] in core.FLYWHEEL_ARMS
    assert rows[0]["text_fp"] == tvlink.text_fp(tvlink.Note(note_id="", title="骑了一百公里", body=BODY_S,
                                                           publish_time=None)), "指纹不看话题标签"
    assert c.rows["draft_fingerprints"] == [], "底稿不进指纹库, 不挡后面的稿子"


def test_only_the_tool_path_records_snapshots(monkeypatch):
    # 运维的 `cli check` 试跑(core.check_drafts 默认)不记: 不然试跑的稿子挂在写手名下, 发出去以后会被转正
    c = _desk()
    core.check_drafts(c, P1, [{"title": "骑了一百公里", "body": BODY_S}], user_id=WRITER)
    assert c.rows["draft_snapshots"] == []
    from deskcore import tools
    monkeypatch.setattr(core, "sb", lambda: c)
    tools.check_drafts(P1, [{"title": "骑了一百公里", "body": BODY_S, "angle_key": KEY}], _user_id=WRITER)
    assert [r["angle_key"] for r in c.rows["draft_snapshots"]] == [KEY]


def test_a_failed_snapshot_write_never_changes_the_verdict(monkeypatch, caplog):
    c = _desk()
    want = core.check_drafts(c, P1, [{"title": "骑了一百公里", "body": BODY_S}], user_id=WRITER, record_snapshot=True)

    def boom(*a, **k):
        raise RuntimeError("PGRST205 Could not find the table 'autowriter.draft_snapshots' in the schema cache")
    monkeypatch.setattr(store, "insert_draft_snapshots", boom)
    got = core.check_drafts(c, P1, [{"title": "骑了一百公里", "body": BODY_S}], user_id=WRITER, record_snapshot=True)
    assert got == want
    assert "记底稿失败" in caplog.text


def test_check_drafts_still_raises_when_the_gate_itself_fails(monkeypatch):
    # 记底稿 fail-open 不能把查重本身也变成 fail-open
    c = _desk()
    monkeypatch.setattr(core, "_gate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("history down")))
    with pytest.raises(RuntimeError):
        core.check_drafts(c, P1, [{"title": "t", "body": BODY_S}], user_id=WRITER, record_snapshot=True)
    assert c.rows["draft_snapshots"] == []


# ══════════════════════════════════════════════════════════════════════
# 3 · tv_sync: 对上底稿 → 转成版本
# ══════════════════════════════════════════════════════════════════════

def _links(c):
    return {l["note_id"]: l for l in c.rows["tv_note_links"]}


def test_a_post_written_from_an_unarchived_draft_is_promoted_not_ingested():
    snaps = [_snap("s1", "骑了一百公里", BODY_S + "\n#骑行", days=0)]
    notes = [_note("n1", "骑了一百公里", BODY_S, days=2), _note("n2", "外面写的", BODY_OTHER, days=2)]
    c = _client(notes, snaps)
    out = core.tv_sync(c, TV)
    links = _links(c)
    l1 = links["n1"]
    vid = core._snapshot_version_id(P1, snaps[0]["text_fp"])
    assert l1["match_kind"] == "body_exact" and l1["version_id"] == vid and l1["lag_days"] == 2
    assert l1["item_id"] and l1["project_id"] == P1
    assert any(core.SNAPSHOT_NOTE == e.get("note") for e in l1["candidates"])
    assert not core._pinned(l1), "W-38 的备注不能把对照钉死(W-34 要能重对)"
    assert links["n2"]["match_kind"] == "ingested", "对不上底稿的照旧补录"

    ver = next(v for v in c.rows["versions"] if v["id"] == vid)
    assert ver["created_at"] == snaps[0]["checked_at"], "版本的时刻是第一次查重, 不是转正那一刻"
    item = next(i for i in c.rows["items"] if i["id"] == l1["item_id"])
    assert item["user_id"] == WRITER and item["created_at"] == snaps[0]["checked_at"]
    batch = next(b for b in c.rows["batches"] if b["id"] == item["batch_id"])
    assert batch["params"]["source"] == "deskcore" and batch["params"]["via"] == "snapshot"
    assert batch["params"]["flywheel_arm"] == "patterns" and batch["params"]["check_id"] == "c1"
    assert not store.is_ingest_batch(batch["params"]), "不是补录副本"

    fps = [f for f in c.rows["draft_fingerprints"] if f["version_id"] == vid]
    assert len(fps) == 1 and fps[0]["angle_key"] == KEY and fps[0]["user_id"] == WRITER
    (card,) = c.rows["angle_ledger"]
    assert card["consumed_version_id"] == vid, "牌销给转正的版本"
    (s,) = c.rows["draft_snapshots"]
    assert s["version_id"] == vid and s["promoted_at"] and s["note_id"] == "n1"

    sn = out["snapshots"]
    assert (sn["matched_notes"], sn["promoted"], sn["minted"], sn["angles_consumed"]) == (1, 1, 1, 1)
    assert out["counts"]["body_exact"] == 1 and out["counts"]["unmatched"] == 1
    assert sum(out["counts"].values()) == len(notes), "counts 加起来仍等于笔记数"
    assert "其中对上查重底稿 1" in out["note"]
    # 同一晚就算改稿差异(版本进了 known)
    assert any(d["note_id"] == "n1" for d in c.rows.get("tv_edit_diffs", []))


def test_the_main_index_wins_over_snapshots():
    # 存过档的那一版(v1)和底稿是同一篇: 对 v1, 底稿不动
    c = _client([_note("n1", "秋招投了六十份", BODY_A)], [_snap("s1", "秋招投了六十份", BODY_A, days=-6)])
    out = core.tv_sync(c, TV)
    assert _links(c)["n1"]["version_id"] == "v1" and out["snapshots"]["matched_notes"] == 0
    assert c.rows["draft_snapshots"][0]["promoted_at"] is None


def test_dry_run_reports_and_writes_nothing():
    c = _client([_note("n1", "骑了一百公里", BODY_S)], [_snap("s1", "骑了一百公里", BODY_S)])
    out = core.tv_sync(c, TV, dry_run=True)
    assert out["snapshots"]["matched_notes"] == 1 and out["counts"]["body_exact"] == 1
    assert out["ingest"] is None, "对上底稿的不会再去补录"
    assert len(c.rows["versions"]) == 1 and c.rows["tv_note_links"] == []
    assert c.rows["draft_snapshots"][0]["promoted_at"] is None
    assert c.rows["angle_ledger"][0]["consumed_version_id"] is None


def test_ambiguous_snapshots_are_held_as_ambiguous_not_ingested():
    # 分不出是哪一份底稿: 帖本来就是写作台的稿, 补成副本就再也认不回来 —— 记 ambiguous, 每晚重判, 等人看
    half = BODY_S[: len(BODY_S) // 2]
    snaps = [_snap("s1", "A", half + BODY_A), _snap("s2", "B", half + BODY_OTHER, key=KEY2)]
    c = _client([_note("n1", "帖", half)], snaps)
    out = core.tv_sync(c, TV)
    l = _links(c)["n1"]
    assert l["match_kind"] == "ambiguous" and l["version_id"] is None and out["ingest"] is None
    assert out["snapshots"]["ambiguous"] == 1 and out["counts"]["ambiguous"] == 1 and out["counts"]["unmatched"] == 0
    assert not core._pinned(l), "每晚重判"
    assert out["ambiguous_samples"] and len(c.rows["versions"]) == 1


def test_reject_only_snapshots_are_not_promoted():
    # 查重打回过、没再通过的(撞了库里的稿, 多半是照抄发出去的帖)不转正
    c = _client([_note("n1", "骑了一百公里", BODY_S)], [_snap("s1", "骑了一百公里", BODY_S, status="reject")])
    out = core.tv_sync(c, TV)
    assert _links(c)["n1"]["match_kind"] == "ingested" and out["snapshots"]["promoted"] == 0
    # 同一篇后来又查过一次、通过了 → 转正
    c = _client([_note("n1", "骑了一百公里", BODY_S)], [_snap("s1", "骑了一百公里", BODY_S, status="reject"),
                                                        _snap("s2", "骑了一百公里", BODY_S, days=0.1)])
    assert core.tv_sync(c, TV)["snapshots"]["promoted"] == 1


def test_a_post_already_in_the_fingerprint_library_is_not_laundered_into_a_desk_version():
    # 帖的文字已经作为补录副本记进指纹库(补录每晚跳过它、记 unmatched): 不拿一份底稿把早就公开的帖转成写作台的稿
    c = _client([_note("n1", "骑了一百公里", BODY_S)], [_snap("s1", "骑了一百公里", BODY_S)])
    c.rows["draft_fingerprints"].append({"id": "fpx", "project_id": P1, "version_id": "vx",
                                         "opening_hash": core.fp.opening_hash(BODY_S),
                                         "ngram_hashes": core.fp.ngram_hashes(BODY_S)})
    out = core.tv_sync(c, TV)
    assert out["snapshots"]["already_fingerprinted"] == 1 and out["snapshots"]["promoted"] == 0
    assert _links(c)["n1"]["version_id"] is None and len(c.rows["versions"]) == 1


def test_promotion_only_consumes_the_writers_own_earlier_card():
    TEAMMATE = "44444444-4444-4444-4444-444444444444"
    ledger = [{"id": "t1", "project_id": P1, "angle_key": KEY, "dims": {}, "drawn_by": TEAMMATE,
               "drawn_at": _iso(-1), "consumed_version_id": None, "consumed_at": None},
              {"id": "w2", "project_id": P1, "angle_key": KEY, "dims": {}, "drawn_by": WRITER,
               "drawn_at": _iso(1), "consumed_version_id": None, "consumed_at": None}]       # 查重之后才重抽的
    c = _client([_note("n1", "骑了一百公里", BODY_S)], [_snap("s1", "骑了一百公里", BODY_S)], ledger=ledger)
    out = core.tv_sync(c, TV)
    assert out["snapshots"]["promoted"] == 1 and out["snapshots"]["angles_consumed"] == 0
    assert all(r["consumed_version_id"] is None for r in c.rows["angle_ledger"]), "队友的牌、之后重抽的牌都不销"


def test_a_key_added_on_a_later_check_consumes_the_card_drawn_before_that_check():
    # 先没带 key 查了一次, 抽了牌再带着 key 查同一篇: 销牌按带 key 的那次查重为界, 不按第一次(codex on aw#132)
    snaps = [_snap("s1", "骑了一百公里", BODY_S, days=-3, key=None),
             _snap("s2", "骑了一百公里", BODY_S, days=0, check="c2")]
    ledger = [{"id": "a1", "project_id": P1, "angle_key": KEY, "dims": {}, "drawn_by": WRITER,
               "drawn_at": _iso(-1), "consumed_version_id": None, "consumed_at": None}]
    c = _client([_note("n1", "骑了一百公里", BODY_S, days=2)], snaps, ledger=ledger)
    out = core.tv_sync(c, TV)
    vid = core._snapshot_version_id(P1, snaps[0]["text_fp"])
    assert out["snapshots"]["angles_consumed"] == 1 and c.rows["angle_ledger"][0]["consumed_version_id"] == vid
    assert next(v for v in c.rows["versions"] if v["id"] == vid)["created_at"] == snaps[0]["checked_at"], \
        "版本时刻照旧是第一次查重"


def test_an_edited_post_also_puts_its_published_text_in_the_fingerprint_library():
    # 以前补录会把发出去的文字记进指纹库; 转正之后后面的稿子照样要能撞上发出去的那一版
    published = BODY_S + "后来我又补了一根盐丸, 才没抽筋。"
    c = _client([_note("n1", "骑了一百公里", published)], [_snap("s1", "骑了一百公里", BODY_S)])
    out = core.tv_sync(c, TV)
    vid = _links(c)["n1"]["version_id"]
    rows = [f for f in c.rows["draft_fingerprints"] if f["version_id"] == vid]
    assert len(rows) == 2 and out["snapshots"]["fingerprinted"] == 2
    assert {tuple(sorted(r["ngram_hashes"])) for r in rows} == {
        tuple(sorted(core.fp.ngram_hashes(BODY_S))), tuple(sorted(core.fp.ngram_hashes(tvlink.strip_hashtags(published))))}


def test_a_misconfigured_snapshot_table_does_not_stop_ingest(monkeypatch):
    # 缺权限 / 缺列是配置问题: 扣下补录会每晚都扣 —— 照旧补录, 退出码 1 让人去修
    def denied(*a, **k):
        raise RuntimeError("{'code': '42501', 'message': 'permission denied for table draft_snapshots'}")
    monkeypatch.setattr(store, "pending_draft_snapshots", denied)
    c = _client([_note("n1", "外面写的", BODY_OTHER)], [])
    out = core.tv_sync(c, TV)
    assert _links(c)["n1"]["match_kind"] == "ingested" and out["snapshots"]["held"] == 0
    assert out["snapshots"]["error"]


def test_unreadable_snapshots_hold_ingest_for_the_night(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(store, "pending_draft_snapshots", boom)
    c = _client([_note("n1", "外面写的", BODY_OTHER)], [])
    out = core.tv_sync(c, TV)
    l = _links(c)["n1"]
    assert l["match_kind"] == "unmatched" and l["version_id"] is None, "补录是终态: 读不出底稿的那晚不补"
    assert "读不出查重底稿" in l["candidates"][0]["note"]
    assert out["ingest"] is None and out["snapshots"]["held"] == 1 and out["snapshots"]["error"]


def test_missing_018_skips_the_pass_and_ingests_as_before(monkeypatch):
    monkeypatch.setattr(store, "pending_draft_snapshots", lambda *a, **k: None)
    c = _client([_note("n1", "外面写的", BODY_OTHER)], [])
    out = core.tv_sync(c, TV)
    assert _links(c)["n1"]["match_kind"] == "ingested"
    assert out["snapshots"]["applied"] is False and out["snapshots"]["error"] is None


def test_pending_snapshots_maps_only_the_018_table_to_none():
    class Missing(FakeClient):
        def _execute(self, q):
            if q.table_name == "draft_snapshots":
                raise RuntimeError("{'code': 'PGRST205', 'message': \"Could not find the table "
                                   "'autowriter.draft_snapshots' in the schema cache\"}")
            return super()._execute(q)
    assert store.pending_draft_snapshots(Missing(rows={}), [P1], since=_iso(0)) is None

    class Other(FakeClient):
        def _execute(self, q):
            if q.table_name == "draft_snapshots":
                raise RuntimeError("permission denied for table draft_snapshots")
            return super()._execute(q)
    with pytest.raises(RuntimeError):
        store.pending_draft_snapshots(Other(rows={}), [P1], since=_iso(0))


def test_a_failed_promotion_leaves_the_note_unmatched_for_tomorrow(monkeypatch):
    monkeypatch.setattr(store, "mint_draft_identity",
                        lambda *a, **k: {"batch_id": None, "versions": {}, "error": "RuntimeError: db down"})
    c = _client([_note("n1", "骑了一百公里", BODY_S)], [_snap("s1", "骑了一百公里", BODY_S)])
    out = core.tv_sync(c, TV)
    l = _links(c)["n1"]
    assert l["match_kind"] == "unmatched" and l["version_id"] is None, "没转成也不补录: 补成副本就再也认不回来"
    assert "明晚再试" in l["candidates"][-1]["note"]
    assert out["ingest"] is None and out["snapshots"]["failed_notes"] == 1 and out["snapshots"]["error"]
    assert c.rows["draft_snapshots"][0]["promoted_at"] is None
    # 第二晚好了: unmatched 没版本, 每晚重判 → 这回转正
    monkeypatch.undo()
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    again = core.tv_sync(c, TV)
    assert _links(c)["n1"]["match_kind"] == "body_exact" and again["snapshots"]["promoted"] == 1


def test_a_half_done_promotion_is_finished_the_next_night_without_a_second_version(monkeypatch):
    snap = _snap("s1", "骑了一百公里", BODY_S)
    c = _client([_note("n1", "骑了一百公里", BODY_S)], [snap])

    def no_fp(*a, **k):
        raise RuntimeError("fingerprint write failed")
    monkeypatch.setattr(store, "write_fingerprints", no_fp)
    out = core.tv_sync(c, TV)
    vid = core._snapshot_version_id(P1, snap["text_fp"])
    assert _links(c)["n1"]["version_id"] == vid, "版本已经建成: 对照照连"
    assert out["snapshots"]["error"] and c.rows["draft_snapshots"][0]["promoted_at"] is None
    assert c.rows["angle_ledger"][0]["consumed_version_id"] is None
    # 第二晚: 版本在对照索引里(假库不做 embedded join, 手动嵌), 补齐指纹 / 销牌 / 标记, 不再建一份
    monkeypatch.undo()
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    item = next(i for i in c.rows["items"] if i["id"] == _links(c)["n1"]["item_id"])
    ver = next(v for v in c.rows["versions"] if v["id"] == vid)
    item.update(best_version_id=vid, versions=[dict(ver)], batches={"project_id": P1, "params": {"source": "deskcore"}})
    ver["items"] = {"id": item["id"], "batches": {"project_id": P1}}      # items_for_versions 的 inner join
    n_versions = len(c.rows["versions"])
    again = core.tv_sync(c, TV)
    assert again["snapshots"]["repaired"] == 1 and len(c.rows["versions"]) == n_versions
    assert [f["version_id"] for f in c.rows["draft_fingerprints"]] == [vid]
    assert c.rows["angle_ledger"][0]["consumed_version_id"] == vid
    assert c.rows["draft_snapshots"][0]["promoted_at"]


def test_projects_without_an_ingest_target_still_promote():
    c = _client([_note("n1", "骑了一百公里", BODY_S), _note("n2", "外面写的", BODY_OTHER)],
                [_snap("s1", "骑了一百公里", BODY_S)], with_target=False)
    out = core.tv_sync(c, TV)
    assert _links(c)["n1"]["match_kind"] == "body_exact" and _links(c)["n2"]["match_kind"] == "unmatched"
    assert out["snapshots"]["promoted"] == 1


def test_one_snapshot_two_posts_one_version_one_card():
    c = _client([_note("n1", "骑了一百公里", BODY_S), _note("n2", "骑了一百公里", BODY_S, days=3)],
                [_snap("s1", "骑了一百公里", BODY_S)])
    out = core.tv_sync(c, TV)
    links = _links(c)
    assert links["n1"]["version_id"] == links["n2"]["version_id"]
    assert out["snapshots"]["minted"] == 1 and out["snapshots"]["angles_consumed"] == 1


def test_a_link_stored_as_minus_one_by_the_old_utc_lag_is_repaired():
    # 北京当天写、当天发的: 以前按 UTC 日期减记成 −1(当成发了之后才存的); 按北京日重算是 0 —— 改回来, 才算得了差异、算接住
    c = _client([_note("n1", "秋招投了六十份", BODY_A, days=0)], [], links=[
        {"note_id": "n1", "tv_project_id": TV, "project_id": P1, "version_id": "v1", "item_id": "i1",
         "match_kind": "body_exact", "score": 1.0, "lag_days": -1, "candidates": None,
         "tv_text_fp": tvlink.text_fp(tvlink.Note.from_row(_note("n1", "秋招投了六十份", BODY_A, days=0)))}])
    for v in c.rows["versions"] + [c.rows["items"][0]["versions"][0]]:
        v["created_at"] = _iso(0)                       # 北京 10-20 12:00 存的
    out = core.tv_sync(c, TV)
    assert out["edit_diffs"]["lag_filled"] == 1 and _links(c)["n1"]["lag_days"] == 0
    assert any(d["note_id"] == "n1" for d in c.rows.get("tv_edit_diffs", []))


def test_a_failed_fingerprint_lookup_holds_promotion_without_failing_the_night(monkeypatch):
    # 转正前查指纹库失败: 不能让整个 TV 项目这晚失败(对照一行都不写), 也不能补录 —— 记 unmatched, 明晚再对
    monkeypatch.setattr(store, "existing_fingerprint_sketches",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("statement timeout")))
    c = _client([_note("n1", "骑了一百公里", BODY_S), _note("n2", "秋招投了六十份", BODY_A)],
                [_snap("s1", "骑了一百公里", BODY_S)])
    out = core.tv_sync(c, TV)
    links = _links(c)
    assert links["n2"]["version_id"] == "v1", "别的对照照常写"
    assert links["n1"]["match_kind"] == "unmatched" and "明晚再试" in links["n1"]["candidates"][-1]["note"]
    assert out["snapshots"]["error"] and out["snapshots"]["promoted"] == 0 and out["ingest"] is None


def test_the_repair_night_also_records_the_published_text_and_the_note(monkeypatch):
    published = BODY_S + "后来我又补了一根盐丸, 才没抽筋。"
    snap = _snap("s1", "骑了一百公里", BODY_S)
    c = _client([_note("n1", "骑了一百公里", published)], [snap])
    monkeypatch.setattr(store, "write_fingerprints", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    core.tv_sync(c, TV)
    monkeypatch.undo()
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    vid = core._snapshot_version_id(P1, snap["text_fp"])
    item = next(i for i in c.rows["items"] if i["id"] == _links(c)["n1"]["item_id"])
    ver = next(v for v in c.rows["versions"] if v["id"] == vid)
    item.update(best_version_id=vid, versions=[dict(ver)], batches={"project_id": P1, "params": {"source": "deskcore"}})
    ver["items"] = {"id": item["id"], "batches": {"project_id": P1}}
    out = core.tv_sync(c, TV)
    assert out["snapshots"]["repaired"] == 1 and out["snapshots"]["fingerprinted"] == 2, out["snapshots"]
    assert c.rows["draft_snapshots"][0]["note_id"] == "n1"


def test_the_repair_night_finds_a_post_the_main_index_links_tonight(monkeypatch):
    # 前一晚建了版本就断了(对照也没写上): 今晚主索引才把帖连上那一版(在这晚的 links 里, 不在 existing 里) ——
    # 发出去的文字那一行指纹一样要补(codex on aw#132)
    published = BODY_S + "后来我又补了一根盐丸, 才没抽筋。"
    snap = _snap("s1", "骑了一百公里", BODY_S)
    c = _client([_note("n1", "骑了一百公里", published)], [snap])
    monkeypatch.setattr(store, "write_fingerprints", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    core.tv_sync(c, TV)
    monkeypatch.undo()
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    vid = core._snapshot_version_id(P1, snap["text_fp"])
    item = next(i for i in c.rows["items"] if i["id"] == _links(c)["n1"]["item_id"])
    c.rows["tv_note_links"] = [l for l in c.rows["tv_note_links"] if l["note_id"] != "n1"]
    ver = next(v for v in c.rows["versions"] if v["id"] == vid)
    item.update(best_version_id=vid, versions=[dict(ver)], batches={"project_id": P1, "params": {"source": "deskcore"}})
    ver["items"] = {"id": item["id"], "batches": {"project_id": P1}}
    out = core.tv_sync(c, TV)
    assert _links(c)["n1"]["version_id"] == vid, "主索引今晚连上了那一版"
    assert out["snapshots"]["repaired"] == 1 and out["snapshots"]["fingerprinted"] == 2, out["snapshots"]
    assert c.rows["draft_snapshots"][0]["note_id"] == "n1"


def _half_done(monkeypatch, notes, snap, *, drop_link=False):
    """第一晚: 版本建成了, 指纹没写上(或连对照也没写上)。回 (client, version_id), 已经把版本手动嵌进对照索引(假库不做
    embedded join)。"""
    c = _client(notes, [snap])
    monkeypatch.setattr(store, "write_fingerprints", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    core.tv_sync(c, TV)
    monkeypatch.undo()
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    vid = core._snapshot_version_id(P1, snap["text_fp"])
    item = next(i for i in c.rows["items"] if i["id"] == _links(c)[notes[0]["note_id"]]["item_id"])
    if drop_link:
        c.rows["tv_note_links"] = [l for l in c.rows["tv_note_links"] if l["note_id"] != notes[0]["note_id"]]
    ver = next(v for v in c.rows["versions"] if v["id"] == vid)
    item.update(best_version_id=vid, versions=[dict(ver)], batches={"project_id": P1, "params": {"source": "deskcore"}})
    ver["items"] = {"id": item["id"], "batches": {"project_id": P1}}
    return c, vid


def test_another_tv_project_on_the_same_desk_project_does_not_close_a_half_done_promotion(monkeypatch):
    # 两个 TV 项目接同一个写作台项目: 帖在 TV 里, 先跑的是 TV2(它那里没有这篇帖)。TV2 补底稿那一行指纹、销牌, 不记转正;
    # TV 跑的时候补上发出去的文字那一行、记转正(codex on aw#132)
    published = BODY_S + "后来我又补了一根盐丸, 才没抽筋。"
    c, vid = _half_done(monkeypatch, [_note("n1", "骑了一百公里", published)], _snap("s1", "骑了一百公里", BODY_S))
    c.rows["tv_project_map"].append({"tv_project_id": "TV2", "project_id": P1, "ingest_target": False, "note": ""})
    real = c.rpc_impl["deskcore_tv_notes"]
    c.rpc_impl["deskcore_tv_notes"] = lambda a: [] if a["_tv_project_id"] == "TV2" else real(a)
    other = core.tv_sync(c, "TV2")
    assert other["snapshots"]["waiting_post"] == 1 and other["snapshots"]["repaired"] == 0, other["snapshots"]
    assert c.rows["draft_snapshots"][0]["promoted_at"] is None, "帖不在这里: 不记转正"
    assert [f["version_id"] for f in c.rows["draft_fingerprints"]] == [vid]
    assert c.rows["angle_ledger"][0]["consumed_version_id"] == vid
    again = core.tv_sync(c, TV)
    assert again["snapshots"]["repaired"] == 1 and again["snapshots"]["fingerprinted"] == 1, again["snapshots"]
    assert len(c.rows["draft_fingerprints"]) == 2, "发出去的文字那一行补上了, 底稿那一行没重写"
    assert c.rows["draft_snapshots"][0]["promoted_at"] and c.rows["draft_snapshots"][0]["note_id"] == "n1"
    assert sum(1 for r in c.rows["angle_ledger"] if r["consumed_version_id"]) == 1


def test_a_snapshot_archived_while_tv_sync_waits_for_the_lock_is_not_promoted_twice(monkeypatch):
    # tv-sync 读了底稿、还没拿到锁, commit_drafts 把同样文字的稿子存了档、底稿记到了那一版: 拿到锁再看一眼, 这晚不转,
    # 不补录(记 unmatched), 明晚主索引对存了档的那一版(codex on aw#132)
    snap = _snap("s1", "骑了一百公里", BODY_S)
    c = _client([_note("n1", "骑了一百公里", BODY_S)], [snap])
    real_lock = core._project_write_lock

    @contextlib.contextmanager
    def lock_after_archive(client, project_id, holder):
        if holder.startswith("tv-sync-snapshot"):
            store.supersede_snapshots(client, project_id, {snap["text_fp"]: "v-archived"})
        with real_lock(client, project_id, holder) as got:
            yield got
    monkeypatch.setattr(core, "_project_write_lock", lock_after_archive)
    n_versions = len(c.rows["versions"])
    out = core.tv_sync(c, TV)
    l1 = _links(c)["n1"]
    assert l1["match_kind"] == "unmatched" and "刚存了档" in l1["candidates"][-1]["note"], l1
    assert len(c.rows["versions"]) == n_versions, "不按底稿另建一份"
    assert c.rows["draft_snapshots"][0]["version_id"] == "v-archived"
    assert c.rows["angle_ledger"][0]["consumed_version_id"] is None
    assert out["snapshots"]["superseded"] == 1 and out["ingest"] is None, out["snapshots"]


def test_a_post_without_a_publish_date_that_matches_a_snapshot_waits_instead_of_being_ingested():
    # 飞书里还没填发布日期就被 TV 读进来了: 对得上底稿的先不补录(补录是终态), 填了日期那晚转正; 对不上的照旧补录(复审 on aw#132)
    row = _note("n1", "骑了一百公里", BODY_S, days=1)
    row["publish_time"] = None
    other = _note("n2", "外面写的", BODY_OTHER, days=1)
    other["publish_time"] = None
    c = _client([row, other], [_snap("s1", "骑了一百公里", BODY_S + "\n#骑行")])
    out = core.tv_sync(c, TV)
    links = _links(c)
    assert links["n1"]["match_kind"] == "unmatched" and "填了日期再对" in links["n1"]["candidates"][-1]["note"]
    assert links["n2"]["match_kind"] == "ingested", "对不上底稿的照旧补录"
    assert out["snapshots"]["undated"] == 1 and c.rows["draft_snapshots"][0]["promoted_at"] is None
    row["publish_time"] = _pub(2)
    out = core.tv_sync(c, TV)
    assert _links(c)["n1"]["match_kind"] == "body_exact" and out["snapshots"]["promoted"] == 1
    assert c.rows["angle_ledger"][0]["consumed_version_id"] == _links(c)["n1"]["version_id"]


def test_a_rewritten_second_post_the_same_night_links_to_the_version_promoted_tonight():
    # 同一篇改写给另一个号发的(开头 / 标题都换了, 包含度 0.39, 不到底稿那一档的线): 明晚主索引会连上刚转正的那一版, 今晚
    # 补录成副本就再也连不上了 —— 今晚就按主索引的规矩对一次刚转正的几版(复审 on aw#132)
    x = _cjk(260, 40)
    rewritten = _cjk(50, 41) + "。" + x[60:140] + "。" + _cjk(70, 42)
    assert tvlink.EDITED_FLOOR <= _contain(rewritten, x) < tvlink.SNAPSHOT_EDITED_FLOOR
    snap = _snap("s1", "骑了一百公里", x + "\n#骑行")
    c = _client([_note("n1", "骑了一百公里", x), _note("n2", "另一个号的标题", rewritten)], [snap])
    out = core.tv_sync(c, TV)
    links = _links(c)
    vid = core._snapshot_version_id(P1, snap["text_fp"])
    assert links["n1"]["version_id"] == vid and links["n2"]["version_id"] == vid, links["n2"]
    assert links["n2"]["match_kind"] == "edited" and out["snapshots"]["same_night"] == 1
    assert out["ingest"] is None, "没有补录副本"
    assert sum(out["counts"].values()) == 2
    assert "其中对上查重底稿 2" in out["note"]
    # 改写过的那篇的文字跟着这一版一起记了指纹(codex on aw#132): 以后的查重看得见它的新开头
    oh = core.fp.opening_hash(tvlink.strip_hashtags(rewritten))
    assert any(f["version_id"] == vid and f["opening_hash"] == oh for f in c.rows["draft_fingerprints"])


def test_a_rewritten_second_post_is_held_when_an_archived_version_is_almost_as_close():
    # 同一晚对今晚转正的版本时, 存过档的版本也是对手(codex on aw#132): 主索引两样都在的时候会判分不出, 不能只拿今晚这几版比
    # 就硬连上; 分不出的不补录, 明晚主索引重判
    x = _cjk(260, 40)
    tail = _cjk(70, 42)
    rewritten = _cjk(50, 41) + "。" + x[60:140] + "。" + tail
    archived = _cjk(100, 43) + "。" + tail[:58]
    assert tvlink.EDITED_FLOOR <= _contain(rewritten, x) < tvlink.SNAPSHOT_EDITED_FLOOR
    assert 0.2 < _contain(rewritten, archived) < tvlink.EDITED_FLOOR
    assert _contain(rewritten, x) - _contain(rewritten, archived) < tvlink.EDITED_MARGIN
    snap = _snap("s1", "骑了一百公里", x)
    c = _client([_note("n1", "骑了一百公里", x), _note("n2", "另一个号的标题", rewritten)], [snap])
    for v in [c.rows["versions"][0], c.rows["items"][0]["versions"][0]]:
        v.update(title="别的稿子", body=archived)
    out = core.tv_sync(c, TV)
    links = _links(c)
    assert links["n1"]["version_id"] == core._snapshot_version_id(P1, snap["text_fp"])
    assert links["n2"]["match_kind"] == "unmatched" and "分不出" in links["n2"]["candidates"][0]["note"], links["n2"]
    assert out["snapshots"]["held_rival"] == 1 and out["ingest"] is None


def test_an_undated_post_keeps_waiting_past_the_publication_window():
    # 还没填发布日期的帖试对内容时不卡 30 天的窗口(拿今天当发布日期, 等得久了会被它挡掉): 一直等到底稿过期(codex on aw#132)
    row = _note("n1", "骑了一百公里", BODY_S, days=1)
    row["publish_time"] = None
    snap = _snap("s1", "骑了一百公里", BODY_S, days=-25)         # 离今天 30 多天, 还在 45 天里
    c = _client([row], [snap])
    out = core.tv_sync(c, TV)
    assert _links(c)["n1"]["match_kind"] == "unmatched" and out["snapshots"]["undated"] == 1, out["snapshots"]


def test_the_repair_night_ignores_a_post_that_w34_found_replaced(monkeypatch):
    # 前一晚转了一半; 今晚之前运营把这篇帖整篇换成了别的(W-34 挪走了它): 补齐时不能拿换上去的文字记到底稿那一版的指纹下
    replaced = ("这条帖子被运营整篇换成了另一篇: 讲的是周末去海边露营, 帐篷被风吹跑了两次, "
                "最后在沙滩上睡了一夜, 第二天早上被涨潮的水泡醒。") * 2
    notes = [_note("n1", "骑了一百公里", BODY_S)]
    c, vid = _half_done(monkeypatch, notes, _snap("s1", "骑了一百公里", BODY_S))
    notes[0]["raw_content"] = f"【标题】海边露营 【正文】{replaced}"
    out = core.tv_sync(c, TV)
    assert out["drift"]["replaced"] == 1
    sketch = tuple(sorted(core.fp.ngram_hashes(tvlink.strip_hashtags(replaced))))
    assert not [f for f in c.rows["draft_fingerprints"]
                if f["version_id"] == vid and tuple(sorted(f["ngram_hashes"])) == sketch]
    assert _links(c)["n1"]["match_kind"] == "ingested", "换上去的那篇照 W-34 的老规矩补录"


def test_a_post_ingested_while_tv_sync_waits_for_the_lock_is_not_promoted_too(monkeypatch):
    # 锁外核过帖的文字不在指纹库、拿到锁之前别的补录把它记成了副本: 拿到锁再核一次, 这晚不转, 不另建一份(codex on aw#132)
    c = _client([_note("n1", "骑了一百公里", BODY_S)], [_snap("s1", "骑了一百公里", BODY_S)])
    real_lock = core._project_write_lock

    @contextlib.contextmanager
    def lock_after_ingest(client, project_id, holder):
        if holder.startswith("tv-sync-snapshot"):
            plain = tvlink.strip_hashtags(BODY_S)
            client.rows["draft_fingerprints"].append({
                "project_id": project_id, "version_id": "v-copy", "opening_hash": core.fp.opening_hash(plain),
                "ngram_hashes": core.fp.ngram_hashes(plain)})
        with real_lock(client, project_id, holder) as got:
            yield got
    monkeypatch.setattr(core, "_project_write_lock", lock_after_ingest)
    n_versions = len(c.rows["versions"])
    out = core.tv_sync(c, TV)
    l1 = _links(c)["n1"]
    assert l1["match_kind"] == "unmatched" and "刚被补录" in l1["candidates"][-1]["note"], l1
    assert len(c.rows["versions"]) == n_versions and c.rows["angle_ledger"][0]["consumed_version_id"] is None
    assert c.rows["draft_snapshots"][0]["promoted_at"] is None and out["snapshots"]["promoted"] == 0


def test_a_rejected_recheck_after_promotion_is_closed_without_consuming_a_new_card():
    # 转正之后同样文字又查了一次(撞了它自己, reject), 带的是一张新牌: 只记成已转正, 不拿新牌销给旧版本
    snap = _snap("s1", "骑了一百公里", BODY_S, key=None)
    c = _client([_note("n1", "骑了一百公里", BODY_S)], [snap])
    core.tv_sync(c, TV)
    vid = core._snapshot_version_id(P1, snap["text_fp"])
    item = next(i for i in c.rows["items"] if i["id"] == _links(c)["n1"]["item_id"])
    ver = next(v for v in c.rows["versions"] if v["id"] == vid)
    item.update(best_version_id=vid, versions=[dict(ver)], batches={"project_id": P1, "params": {"source": "deskcore"}})
    ver["items"] = {"id": item["id"], "batches": {"project_id": P1}}
    c.rows["angle_ledger"].append({"id": "a2", "project_id": P1, "angle_key": KEY2, "dims": {}, "drawn_by": WRITER,
                                   "drawn_at": _iso(2), "consumed_version_id": None, "consumed_at": None})
    c.rows["draft_snapshots"].append(_snap("s2", "骑了一百公里", BODY_S, key=KEY2, days=3, status="reject"))
    core.tv_sync(c, TV)
    assert next(r for r in c.rows["angle_ledger"] if r["id"] == "a2")["consumed_version_id"] is None
    assert next(r for r in c.rows["draft_snapshots"] if r["id"] == "s2")["promoted_at"]


def test_cli_prunes_old_snapshots_once_per_run(monkeypatch, capsys):
    old = dict(_snap("s0", "旧稿", BODY_A), checked_at=store.iso_ago(core.SNAPSHOT_RETAIN_DAYS + 1))
    fresh = dict(_snap("s1", "新稿", BODY_OTHER), checked_at=store.iso_now())
    c = _client([], [old, fresh])
    args = type("A", (), {"tv_project": None, "all": True, "dry_run": False, "write_tv": False,
                          "since": None, "rematch": False})()
    assert cli._tv_sync(core, c, args) == 0
    assert [r["id"] for r in c.rows["draft_snapshots"]] == ["s1"]
    assert "删了 1 行" in capsys.readouterr().out
    # 018 没跑: 不报错
    monkeypatch.setattr(store, "delete_old_snapshots", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("PGRST205 Could not find the table 'autowriter.draft_snapshots'")))
    assert core.prune_snapshots(c) is None


def test_cli_prints_the_snapshot_line_and_fails_loudly_on_errors(monkeypatch, capsys):
    c = _client([_note("n1", "骑了一百公里", BODY_S)], [_snap("s1", "骑了一百公里", BODY_S)])
    args = type("A", (), {"tv_project": TV, "all": False, "dry_run": False, "write_tv": False,
                          "since": None, "rematch": False})()
    assert cli._tv_sync(core, c, args) == 0
    assert "查重底稿(W-38): 1 篇帖对上了没存档的底稿, 转成版本 1" in capsys.readouterr().out

    def boom(*a, **k):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(store, "pending_draft_snapshots", boom)
    c2 = _client([_note("n9", "外面写的", BODY_OTHER)], [])
    assert cli._tv_sync(core, c2, args) == 1
    assert "读不出查重底稿" in capsys.readouterr().out


# ══════════════════════════════════════════════════════════════════════
# 4 · 查重时带过的牌不算「抽了没写」
# ══════════════════════════════════════════════════════════════════════

def test_angle_debt_skips_cards_whose_drafts_were_checked():
    ledger = [{"id": f"a{i}", "project_id": P1, "angle_key": k, "dims": {}, "drawn_by": WRITER,
               "drawn_at": store.iso_now(), "consumed_version_id": None, "consumed_at": None}
              for i, k in enumerate((KEY, KEY2))]
    c = _desk({"angle_ledger": ledger, "draft_snapshots": [dict(_snap("s1", "t", BODY_S), checked_at=store.iso_now())]})
    keys = core._checked_angle_keys(c, P1, WRITER, core.ANGLE_DEBT_DAYS)
    assert set(keys) == {KEY}
    debt = store.angle_debt(c, P1, WRITER, core.ANGLE_DEBT_DAYS, exclude_keys=keys)
    assert debt["unconsumed"] == 1 and [a["angle_key"] for a in debt["angles"]] == [KEY2]
    # archive_pending 不排除: 刚查过的那几张正是该存档的(参考分、规则检查、交付模板)
    assert core.archive_pending(c, P1, user_id=WRITER)["cards"] == 2
    # 别人的底稿不算我的
    assert not core._checked_angle_keys(c, P1, ME, core.ANGLE_DEBT_DAYS)


def test_a_card_drawn_again_after_the_check_still_counts_as_debt():
    # 没销的牌一天后回池子: 同一张牌查重之后又抽到一次, 那一次还没写 —— 照旧算欠着(codex on aw#132)
    ledger = [{"id": "a1", "project_id": P1, "angle_key": KEY, "dims": {"n": 1}, "drawn_by": WRITER,
               "drawn_at": store.iso_ago(3), "consumed_version_id": None, "consumed_at": None},
              {"id": "a2", "project_id": P1, "angle_key": KEY, "dims": {"n": 2}, "drawn_by": WRITER,
               "drawn_at": store.iso_now(), "consumed_version_id": None, "consumed_at": None}]
    c = _desk({"angle_ledger": ledger,
               "draft_snapshots": [dict(_snap("s1", "t", BODY_S), checked_at=store.iso_ago(2))]})
    keys = core._checked_angle_keys(c, P1, WRITER, core.ANGLE_DEBT_DAYS)
    debt = store.angle_debt(c, P1, WRITER, core.ANGLE_DEBT_DAYS, exclude_keys=keys)
    assert debt["unconsumed"] == 1 and [a["dims"] for a in debt["angles"]] == [{"n": 2}], debt
    assert debt["last_drawn_at"] == ledger[1]["drawn_at"]
    # 只给 key(不带查重时刻)的老调用照旧整张排除
    assert store.angle_debt(c, P1, WRITER, core.ANGLE_DEBT_DAYS, exclude_keys={KEY})["unconsumed"] == 0


def test_checked_keys_are_status_aware_and_never_break_the_debt_query():
    now = store.iso_now()
    snaps = [dict(_snap("s1", "t", BODY_S), checked_at=now, status="reject"),                       # 只被打回过: 还欠着
             dict(_snap("s2", "t2", BODY_A, key=KEY2), checked_at=store.iso_ago(1), status="reject"),
             dict(_snap("s3", "t2", BODY_A + "改了", key=KEY2), checked_at=now, status="pass"),       # 打回后改过、通过了
             dict(_snap("s4", "t3", BODY_OTHER, key='bad", key,(x)'), checked_at=now)]               # 模型编的 key
    c = _desk({"draft_snapshots": snaps})
    assert set(core._checked_angle_keys(c, P1, WRITER, core.ANGLE_DEBT_DAYS)) == {KEY2}
    # 编出来的 key 带引号 / 逗号 / 括号, 放进 not.in 会让整条查询 400、欠账整个读成 0: 不放进去
    assert 'bad", key,(x)' not in store.snapshotted_angle_keys(c, P1, WRITER, core.ANGLE_DEBT_DAYS)


def test_archiving_a_checked_draft_closes_its_snapshot(monkeypatch):
    # 存了档的稿子有版本了, 发出去以后主索引对得上 —— 同样文字的底稿记成已转正, 不留着等飞书
    from tests.test_e2e_protocol_flow import BODIES
    c = _desk({"angle_ledger": [], "batches": [], "items": [], "versions": []})
    drafts = [{"title": t, "body": b} for t, b in BODIES[:2]]
    core.check_drafts(c, P1, drafts, user_id=WRITER, record_snapshot=True)
    out = core.commit_drafts(c, P1, drafts[:1], user_id=WRITER)
    assert out["written"] == 1
    done, pending = ([r for r in c.rows["draft_snapshots"] if (r.get("promoted_at") is not None) == flag]
                     for flag in (True, False))
    assert [r["title"] for r in done] == [BODIES[0][0]] and done[0]["version_id"]
    assert [r["title"] for r in pending] == [BODIES[1][0]], "没存档的那篇照旧等飞书"


def test_a_failed_archive_does_not_close_the_snapshot(monkeypatch):
    # 版本没建成(mint 半途失败)就不能把底稿记成已转正: 那个 version_id 不存在, 底稿再也转不了正
    from tests.test_e2e_protocol_flow import BODIES
    c = _desk({"angle_ledger": [], "batches": [], "items": [], "versions": []})
    drafts = [{"title": t, "body": b} for t, b in BODIES[:1]]
    core.check_drafts(c, P1, drafts, user_id=WRITER, record_snapshot=True)
    monkeypatch.setattr(store, "mint_draft_identity",
                        lambda *a, **k: {"batch_id": None, "versions": {}, "error": "RuntimeError: db down"})
    core.commit_drafts(c, P1, drafts, user_id=WRITER)
    assert all(r.get("promoted_at") is None for r in c.rows["draft_snapshots"])


def test_the_brief_skips_checked_cards_but_commit_and_archive_pending_do_not(monkeypatch):
    seen = []
    real = store.angle_debt

    def spy(*a, **k):
        seen.append(k.get("exclude_keys"))
        return real(*a, **k)
    monkeypatch.setattr(store, "angle_debt", spy)
    monkeypatch.setattr(core, "_checked_angle_keys", lambda *a, **k: {KEY})
    from tests.test_deskcore_angle_debt import _client as debt_client, ME as DME, PID as DPID
    core.build_writing_brief(debt_client([]), DPID, user_id=DME)
    assert seen and seen[-1] == {KEY}, "简报按查重时带过的牌排除"
    seen.clear()
    core.archive_pending(debt_client([]), DPID, user_id=DME)
    assert seen == [None], "archive_pending 不排除: 刚查过的正该存档"


def test_checked_angle_keys_fail_open(monkeypatch):
    class Broken(FakeClient):
        def _execute(self, q):
            if q.table_name == "draft_snapshots":
                raise RuntimeError("PGRST205 draft_snapshots")
            return super()._execute(q)
    assert not core._checked_angle_keys(Broken(rows={}), P1, WRITER, 7)


def test_archive_pending_next_step_tells_the_model_not_to_put_archiving_on_the_user():
    step = core.ARCHIVE_PENDING_NEXT_STEP
    assert "别跟用户提「存档」" in step and "写作台会自己从飞书对回来" in step


def test_a_checked_draft_archived_as_a_replacement_closes_its_snapshot():
    # score_drafts 带 replaces_version_id 存成了旧 item 的新一版: 同样文字的底稿也记成已转正(codex on aw#132) —— 不然这一版
    # 之后又被替换掉、主索引对不上它的时候, 底稿会被另转一份, 同一篇两个 item
    from tests.test_deskcore_commit_judge import BODY_A as JA, BODY_A2 as JA2, ME as JME, PROJ, _client as jclient, _embed
    c = jclient()
    v1 = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": JA}], user_id=JME)["version_ids"][0]
    _embed(c)
    core.check_drafts(c, PROJ, [{"title": "秋招等回音", "body": JA2}], user_id=JME, record_snapshot=True)
    out = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": JA2, "replaces_version_id": v1}], user_id=JME)
    v2 = out["version_ids"][0]
    assert out["replaced"][0]["old_version_id"] == v1
    (s,) = c.rows["draft_snapshots"]
    assert s["promoted_at"] and s["version_id"] == v2
