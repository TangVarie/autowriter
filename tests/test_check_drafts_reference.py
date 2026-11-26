"""仿写时查有没有照抄参考原文(2026-11-26, check_drafts 的 reference_text, docs/deskcore.md §3.39)。

写手在 AI 对话里常让模型「照这篇写」, 贴进来的是一篇别家的帖。写作台原来看不见那篇参考稿, 照抄了也查不出来。
check_drafts 现在收一个可选的 ``reference_text``(一段或几段), 只在工具层叠在查重的判定之后。这里钉的是:

  · 纯函数 tvlink.reference_overlap: 逐字照搬的过 STRONG、正常仿写在 WARN 以下、正文太短不判、只撞话题标签的不算、
    几段参考稿取并集、最像的是第几段;
  · 不传 / 空串 / 空列表 / 全是空白: 回包和不传时一个字节都不差;
  · 本来 pass 的到线改判 warn(decided_by=["reference"], collided_* 清空、collided_note 去掉), 到 STRONG 换重一点的说法;
    summary 的三个数重算, next_step 跟着说有几篇 warn;
  · 本来就是 warn / reject 的(查重自己判的)status / reason / decided_by / collided_* 一个不动, 只多一句 reference_note;
    参考原文永远不出 reject;
  · 底稿(draft_snapshots)的 status 还是查重自己的判定;
  · 参考原文不进库(假库的每一张表、每一次调用)、不进回包、不进日志;
  · 段数 / 字数超了的不拦查重(复审): 判定照常、底稿照记(这一轮没丢), 只是这次没比, 回一句怎么办;
  · 形状不对(不是文字)的在任何库操作之前拒: 工具层 InvalidInput、REST 400、MCP 的错误结果(SDK 那一步拦下的也换成
    不带原文、说清是参数错的一句), 消息里只有第几段和类型名;
  · reference_index / 「第 k 段」是调用方列表里的位置(空白的段不比, 但占位置), 和报错里的段号同一套数;
  · 有照抄参考原文的 warn 时, next_step 不叫模型「换个切入」(仿写的结构和角度本来就该照参考稿);
  · 正文放在别名键里(content)的也照样比。
所有标题、正文、参考稿都是合成的示例。
"""

from __future__ import annotations

import json
import logging

import pytest

import config
from deskcore import core, fingerprint as fp, tools, tvlink
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
PROJ = "aaaaaaaa-0000-4000-8000-0000000000c3"

# 参考稿里现编的标记串: 它只在参考原文里(标题那一行), 稿子一个字都没抄它 —— 哪儿出现了就是参考原文漏出去了
REF_MARK = "参考标记丙QX3"
REF = (f"带爸妈出国, 最怕落地没网 {REF_MARK}\n"
       "第一次带爸妈出国自由行, 最怕的不是语言不通, 而是手机没信号。落地以后我花了四十分钟在机场排队买卡, "
       "我爸在旁边一直问是不是坏了。后来才知道出发前在家就能把流量包装好, 下飞机开机就有网, 一分钟都不用等。"
       "这次回来我把整个过程写下来, 给同样要带长辈出门的人做个参考。#出国流量 #带爸妈旅行")
REF_B = ("去年冬天在札幌, 雪大到出租车都停了, 我们一家站在路边干等。后来学会了出门前先看好公交的末班时间, "
         "再把酒店前台的电话存进手机, 真遇上了也不慌。")
# 几乎逐字照搬(换了几个字、删了半句): 过 STRONG
COPY = ("第一次带爸妈出国自由行, 最怕的不是语言不通, 而是手机没信号。落地以后我花了半小时在机场排队买卡, "
        "我爸在旁边一直问是不是坏了。后来才知道出发前在家就能把流量包装好, 下飞机开机就有网。"
        "这次回来把过程写下来, 给要带长辈出门的人做个参考。")
# 开头两句照搬、后面是自己的: 落在 WARN 和 STRONG 之间
MID = ("第一次带爸妈出国自由行, 最怕的不是语言不通, 而是手机没信号。落地以后我花了四十分钟在机场排队买卡。"
       "那天夜里十一点, 我妈拎着两个箱子站在出口, 我一边找插座一边给酒店打电话, 谁都联系不上, "
       "直到旁边一个留学生把热点借给我。")
# 真正的仿写: 同一个题(出门在外没网), 换了人、换了事、换了说法
IMIT = ("带两个孩子去东京那次, 真正让我手忙脚乱的是到酒店前那段路: 地图打不开, 打车软件也转圈。"
        "小的在推车里哭, 大的问还有多远。现在出门前一晚我会先把境外流量开好, 落地就能导航, "
        "这一招比多带一个充电宝还管用。")


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """不接判定、不接 embedding: 查重只跑开头 / 四字串那几路(和参考原文无关的那一层照常)。"""
    monkeypatch.setattr(config, "JUDGE_URL", "")
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)


def _client() -> FakeClient:
    return FakeClient(rows={"projects": [{"id": PROJ, "name": "示例项目", "brand": "示例品", "owner_id": ME,
                                          "calibration_notes": "", "tactics": "[]", "custom_roles": []}],
                            "draft_fingerprints": [], "draft_snapshots": [], "angle_ledger": []})


def _desk(monkeypatch) -> FakeClient:
    c = _client()
    monkeypatch.setattr(core, "sb", lambda: c)
    return c


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _ov(body: str, *refs: str):
    return tvlink.reference_overlap(body, [tvlink.reference_grams(r) for r in refs])


# ══════════════════════════════════════════════════════════════════════
# 1 · 纯函数
# ══════════════════════════════════════════════════════════════════════

def test_a_near_verbatim_copy_is_over_the_strong_line_and_an_imitation_is_under_warn():
    copy, k = _ov(COPY, REF)
    assert copy >= fp.REFERENCE_CONTAIN_STRONG and k == 0
    mid, _ = _ov(MID, REF)
    assert fp.REFERENCE_CONTAIN_WARN <= mid < fp.REFERENCE_CONTAIN_STRONG
    imit, k = _ov(IMIT, REF)
    assert imit < fp.REFERENCE_CONTAIN_WARN and k == 0, "同题的正常仿写不到线"


def test_thresholds_borrow_the_gates_containment_lines():
    assert (fp.REFERENCE_CONTAIN_WARN, fp.REFERENCE_CONTAIN_STRONG) == (fp.NGRAM_CONTAIN_WARN, fp.NGRAM_CONTAIN_HARD)
    assert fp.REFERENCE_CONTAIN_WARN >= tvlink.EDITED_FLOOR + tvlink.FUZZY_MARGIN, "要比共用产品话术的那一档高出一截"
    assert f"{fp.REFERENCE_CONTAIN_WARN:.2f}" in tools.check_drafts.__doc__, "说明里写的线和常量是同一个数"


def test_too_short_or_no_reference_is_not_judged():
    assert _ov("落地就有网", REF) == (None, None), "四字串不到 FUZZY_MIN_GRAMS 不判"
    assert _ov("", REF) == (None, None)
    assert tvlink.reference_overlap(COPY, []) == (None, None)


def test_shared_hashtags_alone_count_for_nothing():
    tags = "\n#出国流量 #带爸妈旅行 #东京亲子游 #境外流量卡"
    other = "这一段和上面那篇毫无关系, 讲的是怎么在家里种出一盆不死的绿萝, 浇水要见干见湿。" + tags
    ov, _ = _ov(IMIT + tags, other)
    assert ov == 0.0


def test_several_references_are_unioned_and_the_closest_one_is_named():
    a = "第一次带爸妈出国自由行, 最怕的不是语言不通, 而是手机没信号。落地以后我花了四十分钟在机场排队买卡。"
    b = "去年冬天在札幌, 雪大到出租车都停了, 我们一家站在路边干等。后来学会了出门前先看好公交的末班时间。"
    patch = a + b
    alone_a, _ = _ov(patch, REF)
    alone_b, _ = _ov(patch, REF_B)
    both, k = _ov(patch, REF_B, REF)
    assert alone_a < fp.REFERENCE_CONTAIN_STRONG and alone_b < fp.REFERENCE_CONTAIN_STRONG, "单看哪一段都不到 STRONG"
    assert both >= fp.REFERENCE_CONTAIN_STRONG, "两段各抄一半拼起来的, 并集照样抓得住"
    assert k == (1 if alone_a > alone_b else 0), "最像的那段(交集最大)"
    _, tie = _ov(COPY, REF, REF)
    assert tie == 0, "一样大取前面那段"


# ══════════════════════════════════════════════════════════════════════
# 2 · 不传 / 传空: 一个字节都不差
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("empty", [None, "", "   \n", [], [""], [" ", "\n\t"]],
                         ids=["none", "empty-str", "blank-str", "empty-list", "list-of-empty", "list-of-blank"])
def test_no_reference_means_the_reply_is_byte_identical(monkeypatch, empty):
    drafts = [{"title": "带爸妈出国没网", "body": COPY}, {"title": "东京那段路", "body": IMIT}]
    _desk(monkeypatch)
    plain = tools.check_drafts(PROJ, drafts, _user_id=ME)
    _desk(monkeypatch)
    given = tools.check_drafts(PROJ, drafts, reference_text=empty, _user_id=ME)
    assert _dump(given) == _dump(plain)
    assert "reference" not in plain["summary"] and not any("reference_overlap" in r for r in plain["results"])


# ══════════════════════════════════════════════════════════════════════
# 3 · 改判: 只从 pass 到 warn
# ══════════════════════════════════════════════════════════════════════

def test_a_copied_draft_becomes_warn_and_the_counts_follow(monkeypatch):
    _desk(monkeypatch)
    drafts = [{"title": "带爸妈出国没网", "body": COPY}, {"title": "东京那段路", "body": IMIT}]
    out = tools.check_drafts(PROJ, drafts, reference_text=REF, _user_id=ME)
    copy, imit = out["results"]

    assert copy["status"] == "warn" and copy["decided_by"] == ["reference"]
    assert copy["collided_with"] == "" and copy["collided_scope"] == "" and "collided_note" not in copy
    assert copy["reference_overlap"] == round(_ov(COPY, REF)[0], 3) and copy["reference_index"] == 0
    assert copy["reason"] == tools.REFERENCE_REASON_STRONG.format(pct=f"{_ov(COPY, REF)[0]:.0%}", which="")
    assert "基本是照着参考原文改的" in copy["reason"] and "重写一次再查" in copy["reason"]

    assert imit["status"] == "pass" and imit["reference_overlap"] == 0.0 and "reference_index" not in imit
    assert imit["decided_by"] == [] and imit["reason"] == ""

    s = out["summary"]
    assert (s["pass"], s["warn"], s["reject"], s["total"]) == (1, 1, 0, 2)
    assert s["reference"] == {"count": 1, "checked": 2, "flagged": 1}
    assert "这 2 篇还没存档" in out["next_step"] and "其中 1 篇是 warn" in out["next_step"]


def test_a_partly_copied_draft_gets_the_milder_reason(monkeypatch):
    """MID 和 COPY 开头一样, 放一批会被本批内拦下(那是查重自己的判定), 所以单查。"""
    _desk(monkeypatch)
    out = tools.check_drafts(PROJ, [{"title": "机场那一夜", "body": MID}], reference_text=REF, _user_id=ME)
    (mid,) = out["results"]
    assert mid["status"] == "warn" and mid["decided_by"] == ["reference"]
    assert mid["reason"] == tools.REFERENCE_REASON.format(pct=f"{_ov(MID, REF)[0]:.0%}", which="")
    assert "仿写成了照抄" in mid["reason"] and "基本是照着" not in mid["reason"]


@pytest.mark.parametrize("refs, index, which, count", [
    ([REF_B, "  ", REF], 2, "(第 3 段)", 2),
    (["", REF], 1, "(第 2 段)", 1),
    ([REF, "\n"], 0, "", 1),
    ([REF], 0, "", 1),
], ids=["blank-in-the-middle", "blank-first", "blank-last", "one"])
def test_the_segment_is_named_by_its_place_in_the_callers_list(monkeypatch, refs, index, which, count):
    """复审: 空白的段不比, 但不挪位置 —— reference_index 和「第 k 段」指的是调用方传的那个列表里的那一个, 和报错里的段号
    (也是调用方的位置)同一套数。只有一段、而且就在第一个位置时不说「第几段」。"""
    _desk(monkeypatch)
    out = tools.check_drafts(PROJ, [{"title": "带爸妈出国没网", "body": COPY}], reference_text=refs, _user_id=ME)
    (row,) = out["results"]
    assert row["status"] == "warn" and row["reference_index"] == index
    assert refs[row["reference_index"]] == REF, "指的就是调用方列表里照抄的那一段"
    assert row["reason"] == tools.REFERENCE_REASON_STRONG.format(pct=f"{_ov(COPY, REF)[0]:.0%}", which=which)
    assert out["summary"]["reference"]["count"] == count, "count 是比了几段(空白的不算)"
    with pytest.raises(tools.InvalidInput) as exc:
        tools._reference_texts(list(refs) + [7])
    assert f"第 {len(refs) + 1} 段不是文字" in str(exc.value), "报错的段号也是调用方的位置"


def test_a_draft_too_short_to_judge_is_left_alone(monkeypatch):
    _desk(monkeypatch)
    out = tools.check_drafts(PROJ, [{"title": "短", "body": "落地就有网"}], reference_text=REF, _user_id=ME)
    (row,) = out["results"]
    assert row["status"] == "pass" and row["reference_overlap"] is None and "reference_index" not in row
    assert out["summary"]["reference"] == {"count": 1, "checked": 0, "flagged": 0}


def _fixed_verdict(monkeypatch, rows):
    """core 的判定换成固定的几行(只看叠上去的那一层)。"""
    verdict = {"results": rows, "summary": {"total": len(rows), "pass": 0, "warn": 0, "reject": 0,
                                            "semantic_degraded": False}}
    for r in rows:
        verdict["summary"][r["status"]] += 1
    monkeypatch.setattr(core, "check_drafts",
                        lambda client, pid, ds, *, user_id=None, record_snapshot=False: verdict)
    monkeypatch.setattr(core, "sb", lambda: None)
    return verdict


def test_history_verdicts_keep_their_status_reason_and_attribution(monkeypatch):
    reject = {"index": 0, "title": "甲", "status": "reject", "reason": "正文开头与历史稿完全一致",
              "collided_with": "历史上的那篇", "collided_scope": "历史", "decided_by": ["opening"], "signals": {}}
    warn = {"index": 1, "title": "乙", "status": "warn", "reason": "标题接近(cos=0.850)",
            "collided_with": "本批的甲", "collided_scope": "本批内", "decided_by": ["title"], "signals": {}}
    near = {"index": 2, "title": "丙", "status": "pass", "reason": "", "collided_with": "最接近的那篇",
            "collided_scope": "历史", "decided_by": [], "signals": {},
            "collided_note": "这条判定为通过, collided_with 只是最接近的参照, 不是拦下它的原因。"}
    gate = {"index": 3, "title": "丁", "status": "reject", "reason": "replaces_version_id 不对", "gate": "replace_lookup",
            "collided_with": "", "collided_scope": "", "decided_by": [], "signals": {}}
    before = [dict(r) for r in (reject, warn, near, gate)]
    _fixed_verdict(monkeypatch, [reject, warn, near, gate])
    drafts = [{"title": t, "body": COPY} for t in ("甲", "乙", "丙", "丁")]
    out = tools.check_drafts(PROJ, drafts, reference_text=REF, _user_id=ME)
    rows = out["results"]

    for got, was in ((rows[0], before[0]), (rows[1], before[1]), (rows[3], before[3])):
        for key in ("status", "reason", "decided_by", "collided_with", "collided_scope", "gate"):
            assert got.get(key) == was.get(key), (was["title"], key)
        assert got["reference_note"] == tools.REFERENCE_NOTE_STRONG.format(pct=f"{_ov(COPY, REF)[0]:.0%}", which="")
        assert got["reference_index"] == 0
    assert rows[2]["status"] == "warn" and rows[2]["decided_by"] == ["reference"] and "collided_note" not in rows[2]
    assert rows[2]["collided_with"] == "" and "reference_note" not in rows[2]
    assert [r["status"] for r in rows].count("reject") == 2, "参考原文不出 reject"
    s = out["summary"]
    assert (s["pass"], s["warn"], s["reject"]) == (0, 2, 2) and s["reference"]["flagged"] == 4


def test_a_milder_history_warn_gets_the_milder_note(monkeypatch):
    _fixed_verdict(monkeypatch, [{"index": 0, "title": "甲", "status": "warn", "reason": "正文有 41% 与历史稿重合",
                                  "collided_with": "历史上的那篇", "collided_scope": "历史", "decided_by": ["contain"]}])
    out = tools.check_drafts(PROJ, [{"title": "甲", "body": MID}], reference_text=REF, _user_id=ME)
    (row,) = out["results"]
    assert row["reason"] == "正文有 41% 与历史稿重合" and row["decided_by"] == ["contain"]
    assert row["reference_note"] == tools.REFERENCE_NOTE.format(pct=f"{_ov(MID, REF)[0]:.0%}", which="")


@pytest.mark.parametrize("raw, shown, status, reason", [
    (0.3995, 0.4, "warn", "REFERENCE_REASON"),
    (0.3994, 0.399, "pass", None),
    (0.5995, 0.6, "warn", "REFERENCE_REASON_STRONG"),
    (0.5994, 0.599, "warn", "REFERENCE_REASON"),
])
def test_the_line_is_judged_on_the_number_that_is_reported(monkeypatch, raw, shown, status, reason):
    """回出去的 reference_overlap 是 3 位小数; 判线和换说法也用这个数, 不然模型看到 0.4 却没改判、看到「60%」却是轻的那句。"""
    _fixed_verdict(monkeypatch, [{"index": 0, "title": "甲", "status": "pass", "reason": "", "collided_with": "",
                                  "collided_scope": "", "decided_by": []}])
    monkeypatch.setattr(tvlink, "reference_overlap", lambda body, sets: (raw, 0))
    out = tools.check_drafts(PROJ, [{"title": "甲", "body": COPY}], reference_text=REF, _user_id=ME)
    (row,) = out["results"]
    assert row["reference_overlap"] == shown and row["status"] == status
    if reason:
        assert row["reason"] == getattr(tools, reason).format(pct=f"{shown:.0%}", which="")
    else:
        assert "reference_index" not in row


def test_a_real_in_batch_reject_stays_a_reject_and_snapshots_keep_the_gates_verdict(monkeypatch):
    """走真的 core: 同一批两篇开头一样, 第二篇被本批内的第一篇拦下。第一篇照抄参考原文 → 回包里 warn, 底稿里还是 pass。"""
    c = _desk(monkeypatch)
    drafts = [{"title": "带爸妈出国没网", "body": COPY}, {"title": "换了个标题", "body": COPY},
              {"title": "东京那段路", "body": IMIT}]
    out = tools.check_drafts(PROJ, drafts, reference_text=REF, _user_id=ME)
    first, second, third = out["results"]
    assert first["status"] == "warn" and first["decided_by"] == ["reference"]
    assert second["status"] == "reject" and second["decided_by"] == ["opening"]
    assert second["collided_with"] == "带爸妈出国没网" and second["collided_scope"] == "本批内"
    assert second["reason"] == "正文开头与历史稿完全一致" and "reference_note" in second
    assert third["status"] == "pass"
    snaps = c.rows["draft_snapshots"]
    assert [(r["title"], r["status"]) for r in snaps] == [
        ("带爸妈出国没网", "pass"), ("换了个标题", "reject"), ("东京那段路", "pass")], "底稿记的是查重自己的判定"


# ══════════════════════════════════════════════════════════════════════
# 4 · 参考原文哪儿都不去
# ══════════════════════════════════════════════════════════════════════

def test_the_reference_is_not_stored_echoed_or_logged(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    plain = _desk(monkeypatch)
    tools.check_drafts(PROJ, [{"title": "带爸妈出国没网", "body": COPY}, {"title": "东京那段路", "content": IMIT}],
                       _user_id=ME)
    c = _desk(monkeypatch)
    out = tools.check_drafts(PROJ, [{"title": "带爸妈出国没网", "body": COPY}, {"title": "东京那段路", "content": IMIT}],
                             reference_text=[REF, REF_B], _user_id=ME)
    assert out["results"][0]["status"] == "warn", "用例的前提: 这次真的比了"
    assert set(c.rows) == set(plain.rows), "没有多写哪张表"
    assert len(c.rows["draft_snapshots"]) == 2
    for text in (REF_MARK, "四十分钟", "札幌"):            # 标记串, 和只有参考原文里才有的字
        assert text not in _dump(c.rows), f"参考原文进了库: {text}"
        assert text not in _dump(c.calls) and text not in _dump(c.rpc_calls)
        assert text not in _dump(out), f"参考原文回显了: {text}"
        assert text not in caplog.text, f"参考原文进了日志: {text}"


def test_a_body_under_an_alias_is_compared_too(monkeypatch):
    _desk(monkeypatch)
    out = tools.check_drafts(PROJ, [{"title": "带爸妈出国没网", "content": COPY}], reference_text=REF, _user_id=ME)
    (row,) = out["results"]
    assert row["status"] == "warn" and row["reference_overlap"] >= fp.REFERENCE_CONTAIN_STRONG


# ══════════════════════════════════════════════════════════════════════
# 5 · 超了上限的不拦查重; 形状不对的在任何库操作之前拒, 消息里没有原文
# ══════════════════════════════════════════════════════════════════════

OVER = [
    ([REF, REF_B, REF, REF_B], 4, len(REF)),                       # 写手贴了四篇「照这几篇写」
    (REF + "后面补的字" * 1200, 1, len(REF) + 6000),                # 一篇公众号那么长的
    (["", REF_B, REF + "长" * 6000], 2, len(REF) + 6000),
]


@pytest.mark.parametrize("over, n, longest", OVER, ids=["too-many", "too-long", "too-long-third"])
def test_an_over_limit_reference_does_not_cost_the_round(monkeypatch, caplog, over, n, longest):
    """复审(major): 原来超了上限是整次 InvalidInput —— 查重没判、底稿没记, 协议又说「工具报错就是查重挂了, 停下来」,
    这一轮就又成了写作台没见过的那一版。现在: 查重照常判、底稿照记, 回包和不传参考原文时一样, 只多一句怎么办和
    summary.reference.skipped; 参考原文照样一个字都不出去。"""
    caplog.set_level(logging.DEBUG)
    drafts = [{"title": "带爸妈出国没网", "body": COPY}, {"title": "东京那段路", "body": IMIT}]
    plain_db = _desk(monkeypatch)
    plain = tools.check_drafts(PROJ, drafts, _user_id=ME)
    c = _desk(monkeypatch)
    out = tools.check_drafts(PROJ, drafts, reference_text=over, _user_id=ME)

    assert [(r["title"], r["status"]) for r in c.rows["draft_snapshots"]] == \
        [(r["title"], r["status"]) for r in plain_db.rows["draft_snapshots"]] == \
        [("带爸妈出国没网", "pass"), ("东京那段路", "pass")], "这一轮的底稿照记"
    note = out.pop("reference_skipped_note")
    assert note == tools.REFERENCE_SKIPPED_NOTE.format(max_n=3, max_chars=6000, n=n, longest=longest)
    assert out["summary"].pop("reference") == {"count": n, "checked": 0, "flagged": 0, "skipped": True}
    assert _dump(out) == _dump(plain), "判定、next_step 和不传参考原文时一个字节都不差"
    assert "再调一次 check_drafts" in note and "拆成两三段" in note
    for text in (REF_MARK, "四十分钟", "札幌"):
        assert text not in _dump(c.rows) and text not in note and text not in caplog.text


def test_the_limits_are_exact():
    edge = ["甲" * tools.REFERENCE_MAX_CHARS] * tools.REFERENCE_MAX
    assert tools._reference_texts(edge) == (list(enumerate(edge)), None)
    assert tools._reference_texts(f"  {'乙' * tools.REFERENCE_MAX_CHARS}  \n") == \
        ([(0, "乙" * tools.REFERENCE_MAX_CHARS)], None), "按去掉首尾空白之后的字数算"
    assert tools._reference_texts("丙" * (tools.REFERENCE_MAX_CHARS + 1))[1] is not None
    assert tools._reference_texts(edge + ["丁"])[1] is not None
    assert tools._reference_texts(edge + ["  "])[1] is None, "空白的段不算数"


BAD = [
    ([REF, 5], ("第 2 段不是文字",)),
    (["", REF_B, None], ("第 3 段不是文字",)),
    ({"text": REF}, ("收到的是 dict",)),
]


@pytest.mark.parametrize("bad, says", BAD, ids=["not-a-string", "none-in-the-list", "a-dict"])
def test_a_reference_of_the_wrong_shape_is_refused_before_anything_runs(monkeypatch, bad, says):
    touched = []
    monkeypatch.setattr(core, "sb", lambda: touched.append("sb"))
    monkeypatch.setattr(core, "check_drafts", lambda *a, **k: touched.append("check_drafts"))
    with pytest.raises(tools.InvalidInput) as exc:
        tools.check_drafts(PROJ, [{"title": "带爸妈出国没网", "body": COPY}], reference_text=bad, _user_id=ME)
    msg = str(exc.value)
    for s in says:
        assert s in msg, (s, msg)
    assert "不是查重挂了" in msg and "不带 reference_text" in msg, "协议: 只有这种报错不是查重挂了"
    assert REF_MARK not in msg and "四十分钟" not in msg and "札幌" not in msg, "报错里不带原文"
    assert touched == [], "参数不对就不碰库、不查重"


# ══════════════════════════════════════════════════════════════════════
# 6 · next_step: 照抄参考原文的 warn 不叫模型「换个切入」
# ══════════════════════════════════════════════════════════════════════

def test_next_step_does_not_tell_a_copied_imitation_to_change_its_angle(monkeypatch):
    """复审: 改判出来的 warn 的 reason 说「只借它的结构和角度」, next_step 原来那句却说「先换个切入重写」—— 两个方向。"""
    _desk(monkeypatch)
    out = tools.check_drafts(PROJ, [{"title": "带爸妈出国没网", "body": COPY}], reference_text=REF, _user_id=ME)
    step = out["next_step"]
    assert tools.CHECK_NEXT_STEP_WARN_REFERENCE.format(m=1) in step and "结构和角度留着" in step
    assert tools.CHECK_NEXT_STEP_WARN.format(m=1) not in step
    # 查重自己判的 warn(没有照抄参考原文的那种)照旧是原来那句: 不传参考原文时回包一个字节都不差
    history = {"results": [{"index": 0, "status": "warn", "decided_by": ["contain"]}],
               "summary": {"total": 1, "pass": 0, "warn": 1, "reject": 0}}
    tools._add_check_next_step(history)
    assert tools.CHECK_NEXT_STEP_WARN.format(m=1) in history["next_step"]


FAKE_KEY = "k-reference-0123456789FAKE"            # 测试里现编的钥匙


def _rest(monkeypatch):
    testclient = pytest.importorskip("fastapi.testclient")
    import deskcore.app as A
    monkeypatch.setenv("DESKCORE_KEYS", json.dumps({FAKE_KEY: {"user_id": ME, "name": "示例写手"}}))
    return testclient.TestClient(A.app, raise_server_exceptions=False)


def test_rest_takes_the_reference_and_answers_400_on_a_bad_one(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    client = _rest(monkeypatch)
    _desk(monkeypatch)
    drafts = [{"title": "带爸妈出国没网", "body": COPY}]
    ok = client.post("/tool/check_drafts", headers={"X-Deskcore-Key": FAKE_KEY},
                     json={"project_id": PROJ, "drafts": drafts, "reference_text": REF})
    assert ok.status_code == 200, ok.text[:300]
    assert ok.json()["result"]["results"][0]["decided_by"] == ["reference"]
    assert REF_MARK not in ok.text

    over = client.post("/tool/check_drafts", headers={"X-Deskcore-Key": FAKE_KEY},
                       json={"project_id": PROJ, "drafts": drafts, "reference_text": [REF] * 4})
    assert over.status_code == 200, over.text[:300]
    assert over.json()["result"]["summary"]["reference"]["skipped"] is True and REF_MARK not in over.text

    bad = client.post("/tool/check_drafts", headers={"X-Deskcore-Key": FAKE_KEY},
                      json={"project_id": PROJ, "drafts": drafts, "reference_text": [REF, 5]})
    assert bad.status_code == 400, bad.text[:300]
    assert "第 2 段不是文字" in bad.json()["detail"] and REF_MARK not in bad.text
    assert REF_MARK not in caplog.text


def _mcp_call(name: str, args: dict):
    """走真的 _register_mcp / _wrap, 经 MCP 的 tools/call 调一次(同 tests/test_tool_call_log 的那个)。"""
    pytest.importorskip("mcp")
    import anyio
    from mcp import types
    import deskcore.app as A
    from deskcore.identity import Caller
    mcp = A._register_mcp()
    out: dict = {}

    async def run():
        token = A._caller.set(Caller(ME))
        try:
            handler = mcp._mcp_server.request_handlers[types.CallToolRequest]
            req = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(name=name, arguments=args))
            out["result"] = (await handler(req)).root
        finally:
            A._caller.reset(token)
    anyio.run(run)
    return out["result"]


def test_mcp_takes_the_reference_and_reports_a_bad_one_as_an_error(monkeypatch, caplog):
    pytest.importorskip("fastapi")
    caplog.set_level(logging.DEBUG)
    _desk(monkeypatch)
    drafts = [{"title": "带爸妈出国没网", "body": COPY}]
    ok = _mcp_call("check_drafts", {"project_id": PROJ, "drafts": drafts, "reference_text": [REF]})
    assert ok.isError is False
    assert json.loads(ok.content[0].text)["results"][0]["decided_by"] == ["reference"]
    assert REF_MARK not in ok.content[0].text

    for over in ([REF] * 4, REF + "长" * 6000):
        res = _mcp_call("check_drafts", {"project_id": PROJ, "drafts": drafts, "reference_text": over})
        assert res.isError is False, res.content[0].text[:200]
        got = json.loads(res.content[0].text)
        assert got["summary"]["reference"]["skipped"] is True and "reference_skipped_note" in got
        assert REF_MARK not in res.content[0].text
    # 类型不对的在 SDK 那一步就被 pydantic 拦下(到不了工具)。SDK 原来的消息带一截截断的输入(参考原文的开头); 复审之后
    # 换成只有字段路径和错误说明、说清是参数错的一句(app._reference_text_refusal)。服务端那行日志只有字段路径和错误类型。
    for wrong in ([REF, 5], {"text": REF}, [REF_MARK + "参考原文开头" * 20, None]):
        res = _mcp_call("check_drafts", {"project_id": PROJ, "drafts": drafts, "reference_text": wrong})
        text = res.content[0].text
        assert res.isError is True and "不是查重挂了" in text and "reference_text" in text, text[:300]
        assert REF_MARK not in text and "四十分钟" not in text and "input_value" not in text, text[:300]
    assert REF_MARK not in caplog.text and "reference_text" in caplog.text


def test_other_bad_arguments_keep_the_sdks_own_message(monkeypatch):
    """换消息只换 reference_text 的那种; drafts 传错类型照旧是 SDK 的原话(不在这次改动里)。"""
    _desk(monkeypatch)
    res = _mcp_call("check_drafts", {"project_id": PROJ, "drafts": "不是列表"})
    assert res.isError is True and "不是查重挂了" not in res.content[0].text
