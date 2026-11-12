"""每次工具调用留一行只有数的记录(2026-11-11, deskcore/calllog.py, docs/deskcore.md §3.8)。

走真的 REST(POST /tool/{name})和真的 MCP(_register_mcp / _wrap)那两条路, 工具也是真的。钉住:
  1. 一次调用落一行 ``batch_metrics``(``meta.mode = 'tool_call'``, ``batch_id`` 空), 数是对的: 查重的过 / 拒 / 定的信号,
     评论那一组几条、每样检查打回几条、过了几条、那一格几行、帖子怎么对上的;
  2. 这一行里一个字的稿子、评论、帖子、规则、回包都没有(请求和回包里现编的标记串哪儿都找不到, 整行没有一个汉字);
  3. 被拒的、真故障的调用也留一行(结局 + 异常类型), 回包和不留痕时一模一样;
  4. 写不进库不改回包, 只记一行限频的日志; 队列有上限, 满了就丢、不等;
  5. 开关关掉、get_protocol、认不出调用者的都不记;
  6. 读这张表的地方(历史页的 db.list_batch_metrics)看不到这些行。
全部是合成项目、合成品牌、现编的文字和测试里现编的钥匙。
"""
from __future__ import annotations

import json
import logging
import re
import threading

import anyio
import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
pytest.importorskip("mcp")

from mcp import types                                     # noqa: E402

import db                                                 # noqa: E402
import deskcore.app as A                                  # noqa: E402
from deskcore import calllog, core, store, tools          # noqa: E402
from deskcore.identity import Caller                      # noqa: E402
from tests.fakes import FakeClient                        # noqa: E402
from tests.test_comment_set_score import (                # noqa: E402
    BRAND, BRANDED, COL, PLAIN, _client as _comment_client, _judge, _view)
from tests.test_deskcore_judge_tools import wired         # noqa: E402,F401 — fixture
from tests.test_deskcore_score_and_comments import LINK, ME, NOTE  # noqa: E402
from tests.test_deskcore_commit_judge import PROJ         # noqa: E402

FAKE_KEY = "k-calllog-0123456789abcdefFAKE"               # 测试里现编的, 不是任何真钥匙
KEYS = json.dumps({FAKE_KEY: {"user_id": ME, "name": "示例写手甲"}}, ensure_ascii=False)
H = {"X-Deskcore-Key": FAKE_KEY}
PID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CJK = re.compile(r"[㐀-鿿]")

# 现编的标记串: 请求里的(标题、正文、评论)和回包里的(判定服务给的修改建议), 都不许出现在落库的那一行里
REQ_MARK, REPLY_MARK, ASCII_MARK = "请求标记甲XQ7", "回包标记乙ZK9", "MARKER reply text with spaces"


@pytest.fixture()
def log(monkeypatch):
    """打开留痕(用例里默认关着, 见 conftest), 每条用例一个新的队列; 钥匙是现编的。"""
    monkeypatch.setenv(calllog.ENV_SWITCH, "1")
    monkeypatch.setenv("DESKCORE_KEYS", KEYS)
    calllog.reset()
    yield
    calllog.flush(5)
    calllog.reset()


def _rest():
    return fastapi_testclient.TestClient(A.app, raise_server_exceptions=False)


def _mcp_call(name: str, args: dict, *, user=ME, mcp=None):
    """走真的 _register_mcp / _wrap, 以 ``user`` 的身份经 MCP 的 tools/call 调一次, 回 CallToolResult。"""
    mcp = mcp or A._register_mcp()
    out: dict = {}

    async def run():
        token = A._caller.set(Caller(user))
        try:
            handler = mcp._mcp_server.request_handlers[types.CallToolRequest]
            req = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(name=name, arguments=args))
            out["result"] = (await handler(req)).root
        finally:
            A._caller.reset(token)
    anyio.run(run)
    return out["result"]


def _rows(c) -> list[dict]:
    assert calllog.flush(5), "后台没在 5 秒内写完"
    return [r for r in c.rows.get("batch_metrics", []) if (r.get("meta") or {}).get("mode") == "tool_call"]


def _no_text(row: dict, *marks: str) -> str:
    """落库的那一行里没有任何现编的标记串, 也没有一个汉字。回那一行的 JSON 给调用方接着断言。"""
    blob = json.dumps(row, ensure_ascii=False)
    for mark in marks:
        assert mark and mark not in blob, f"落库的那一行里出现了文字: {mark!r}"
    assert not CJK.search(blob), "落库的那一行里有汉字"
    return blob


def _drafts_client() -> FakeClient:
    return FakeClient(rows={"projects": [{"id": PID, "name": "测试项目", "brand": "测试品", "owner_id": ME,
                                          "calibration_notes": "", "tactics": "[]", "custom_roles": []}],
                            "angle_ledger": [], "draft_fingerprints": []})


BODY_A = f"今天下班路上想到一件事, {REQ_MARK}的正文写的是一个完全虚构的故事, 讲的是等一封不会来的邮件。" * 3
BODY_B = "完全不一样的另一段文字, 说的是周末去爬山, 路上遇到一只很凶的鹅, 追了我半里地才停下来。" * 3
DRAFTS = [{"title": f"{REQ_MARK}标题一", "body": BODY_A}, {"title": "换了标题的第二篇", "body": BODY_A},
          {"title": "第三篇讲爬山", "body": BODY_B}]


# ── 1. 查重: REST 和 MCP 各一次, 数一样 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("channel", ["rest", "mcp"])
def test_check_drafts_leaves_one_counts_only_row(log, monkeypatch, channel):
    c = _drafts_client()
    monkeypatch.setattr(core, "sb", lambda: c)
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    args = {"project_id": PID, "drafts": DRAFTS}
    if channel == "rest":
        r = _rest().post("/tool/check_drafts", headers=H, json=args)
        assert r.status_code == 200, r.text
        result = r.json()["result"]
    else:
        res = _mcp_call("check_drafts", args)
        assert res.isError is False
        result = json.loads(res.content[0].text)
    assert result["summary"]["reject"] == 1 and result["results"][1]["decided_by"] == ["opening"]
    assert REQ_MARK in json.dumps(result, ensure_ascii=False), "回包里本来就有稿子的标题(用例的前提)"

    (row,) = _rows(c)
    assert row["batch_id"] is None and row["project_id"] == PID and row["user_id"] == ME
    assert row["counters"] == {} and row["injection"] == {}
    meta = row["meta"]
    assert meta["mode"] == "tool_call" and meta["v"] == calllog.VERSION
    assert (meta["tool"], meta["channel"], meta["outcome"], meta["items"]) == ("check_drafts", channel, "ok", 3)
    assert isinstance(meta["elapsed_ms"], int) and meta["elapsed_ms"] >= 0 and row["phase_ms"] == {"total": meta["elapsed_ms"]}
    assert meta["reply_chars"] == len(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    assert meta["check"] == {"total": 3, "pass": 2, "warn": 0, "reject": 1, "decided_by": {"reject": {"opening": 1}},
                             "semantic_degraded": True}
    assert "error_class" not in meta and "project8" not in meta
    _no_text(row, REQ_MARK, "标题", "测试项目", "测试品", FAKE_KEY)


# ── 2. 评论: comments_for(MCP) + score_comments(REST), 每样检查的条数 ────────────────────────────

def _comment_texts() -> list[str]:
    texts = list(BRANDED + PLAIN)                         # 12 条提品牌 + 8 条不提, 上限 50% → 超出 2 条
    texts[13] = f"同问价格。{REQ_MARK}"                   # 一个句号
    texts[14] = f"帖子里说的书单在哪{REQ_MARK}"           # 提了帖子
    return texts


def test_comment_tools_leave_rows_with_per_check_counts_and_no_text(log, monkeypatch, wired):
    _calls, reply = wired
    c = _comment_client()
    monkeypatch.setattr(core, "sb", lambda: c)
    texts = _comment_texts()
    views = [_view("旁观推荐" if BRAND in t else "提问") for t in texts]
    views[0]["plan"] = [{"qid": "detail_level", "instruction": f"{REPLY_MARK}: 补一个具体的情境"}]
    views[1]["hints"] = [{"qid": "register", "instruction": ASCII_MARK}]
    reply["fn"] = _judge(views, plan=[{"instruction": f"整组的建议{REPLY_MARK}"}])

    res = _mcp_call("comments_for", {"link": LINK, "skill_version": tools.protocol_text()[1]})
    assert res.isError is False and json.loads(res.content[0].text)["resolved"] is True
    earlier = [f"前面判过的第一条{REQ_MARK}", "前面判过的第二条"]
    r = _rest().post("/tool/score_comments", headers=H, json={
        "link": LINK, "comments": [{"text": t} for t in texts], "earlier_comments": earlier})
    assert r.status_code == 200, r.text
    out = r.json()["result"]
    assert out["judge_status"] == "ok" and out["passed"] is False and out["delivery_column"]["lines"] == 20
    assert REPLY_MARK in r.text and ASCII_MARK in r.text and REQ_MARK in r.text, "回包里本来就有这些字(用例的前提)"

    first, second = _rows(c)
    assert (first["meta"]["tool"], first["meta"]["channel"]) == ("comments_for", "mcp")
    assert first["project_id"] == second["project_id"] == PROJ
    assert first["meta"]["skill"] == "up_to_date", "入口工具这次报上来的 skill 版本只记这一个词"
    block = first["meta"]["comments"]
    assert block == {"resolved": True, "source": "tv", "post8": block["post8"], "existing": 2, "comment_rules": 1,
                     "hard_rules": 1, "has_set": True}
    assert re.fullmatch(r"[0-9a-f]{8}", block["post8"])

    meta = second["meta"]
    assert (meta["tool"], meta["channel"], meta["outcome"], meta["items"]) == ("score_comments", "rest", "ok", 20)
    assert meta["reply_chars"] == len(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    got = meta["comments"]
    assert got["post8"] == block["post8"], "同一篇帖子两次调用是同一个 8 位代号(数「一篇帖子判了几次」用)"
    checks = got.pop("checks")
    assert got == {"resolved": True, "source": "tv", "post8": block["post8"], "n": 20, "earlier": 2, "no_slot": 20,
                   "existing": 2, "judge_status": "ok", "passed": False,
                   "passed_n": out["summary"]["passed"], "needs_review": 0, "delivery_lines": 20}
    rows = out["comments"]
    assert checks == {
        "fix": 1, "style_fix": sum(1 for x in rows if x.get("style_fix")),
        "thread_fix": len(out["thread_fix"]), "thread_hints": len(out.get("thread_hints") or []),
        "style": {"period": 1, "post_ref": 1},            # 写法底线: 一条句号、一条提了帖子
        # 提品牌 12 条、上限 10 条 → 超出 2 条; 那两条带标记串的和 earlier_comments 里的第一条说法重复 → 2 条
        "brand_over": 2, "repeats": 2, "endings_over": 0, "short_by": 0, "neutral_by": 0}
    assert checks["style_fix"] == 4, "两条写法底线(也是重复的那两条) + 两条提品牌超出"
    for row in (first, second):
        _no_text(row, REQ_MARK, REPLY_MARK, ASCII_MARK, "MARKER", BRAND, COL, NOTE["title"], NOTE["note_id"],
                 "xiaohongshu", FAKE_KEY, *texts, *earlier)


def test_an_unresolved_post_is_recorded_as_such(log, monkeypatch):
    c = _comment_client()
    c._tv.rows["notes"] = []
    monkeypatch.setattr(core, "sb", lambda: c)
    r = _rest().post("/tool/comments_for", headers=H, json={"link": LINK})
    assert r.status_code == 200 and r.json()["result"]["resolved"] is False
    (row,) = _rows(c)
    assert row["project_id"] is None, "帖子没对上项目: 那一列空着"
    assert row["meta"]["comments"] == {"resolved": False, "need": 2} and row["meta"]["skill"] == "unknown"
    _no_text(row, "xiaohongshu")


BOGUS = "deadbeef-dead-beef-dead-beefdeadbeef"             # 不是哪个项目的 id(抄错的、别人的)


@pytest.mark.parametrize("tool, extra", [("comments_for", {}), ("score_comments", {"comments": [{"text": "这个怎么买"}]})])
def test_an_unresolved_post_never_puts_the_callers_project_id_in_the_column(log, monkeypatch, wired, tool, extra):
    """2026-11-12 复审 TC-1: 帖子没对上时回的是一个普通的 200(resolved=false), 带来的 project_id 没过归属校验。原来照填进
    项目那一列: 抄错的 id 撞外键、整行写不进去(正是要数的那种调用), 别人的 id 则把这次调用记到了调用方打不开的项目下。"""
    c = _comment_client()
    c._tv.rows["notes"] = []
    monkeypatch.setattr(core, "sb", lambda: c)
    r = _rest().post(f"/tool/{tool}", headers=H, json={"link": LINK, "project_id": BOGUS, **extra})
    assert r.status_code == 200 and r.json()["result"]["resolved"] is False
    (row,) = _rows(c)
    assert row["project_id"] is None and row["meta"]["project8"] == BOGUS[:8]
    assert row["meta"]["outcome"] == "ok" and row["meta"]["comments"]["resolved"] is False


def test_a_post_lookup_that_never_ran_is_not_recorded_as_resolved(log, monkeypatch, wired):
    """评论线程占满(查帖子超时同一个形状): 帖子根本没查, 回包里没有 project_id、也没有 resolved。原来记成
    resolved=true、项目那一列填调用方带来的 id。"""
    c = _comment_client()
    monkeypatch.setattr(core, "sb", lambda: c)
    monkeypatch.setattr(core, "_bounded_submit", lambda *a, **k: None)
    r = _rest().post("/tool/score_comments", headers=H,
                     json={"link": LINK, "project_id": BOGUS, "comments": [{"text": "这个怎么买"}]})
    assert r.status_code == 200 and r.json()["result"]["judge_status"] == "timeout"
    (row,) = _rows(c)
    assert row["project_id"] is None and row["meta"]["project8"] == BOGUS[:8]
    block = row["meta"]["comments"]
    assert "resolved" not in block and "post8" not in block and block["judge_status"] == "timeout" and block["n"] == 1


def test_the_rounds_per_post_query_only_divides_calls_that_carry_a_post():
    """2026-11-12 复审 TC-2: 帖子没对上 / 没查成的 score_comments 也是 outcome = ok, 却没有 post8: 原来算进分子、不进分母,
    「一篇帖子判几轮」读高了。docs §3.8 那条查询的分子只数带 post8 的, 没带的另列一栏。"""
    from pathlib import Path
    doc = (Path(__file__).resolve().parent.parent / "docs" / "deskcore.md").read_text(encoding="utf-8")
    query = doc[doc.index("-- 评论(W-51)"):]
    query = query[:query.index(";")]
    assert "filter (where meta->>'outcome' = 'ok' and meta->'comments' ? 'post8')::numeric" in query
    assert "as no_post" in query
    assert "count(*) filter (where meta->>'outcome' = 'ok')::numeric" not in query


# ── 3. 被拒的、真故障的调用也留一行, 回包不变 ────────────────────────────────────────────────────

def test_a_refused_call_is_recorded_and_still_a_400(log, monkeypatch, wired):
    c = _comment_client()
    monkeypatch.setattr(core, "sb", lambda: c)
    body = {"link": LINK, "project_id": PROJ,
            "comments": [{"slot": "q1", "text": f"第一条{REQ_MARK}"}, {"slot": "q1", "text": "第二条"}]}
    r = _rest().post("/tool/score_comments", headers=H, json=body)
    assert r.status_code == 400 and "q1 重复了" in r.json()["detail"]
    (row,) = _rows(c)
    meta = row["meta"]
    assert (meta["outcome"], meta["error_class"], meta["items"]) == ("refused", "deskcore.tools.InvalidInput", 2)
    assert row["project_id"] is None and meta["project8"] == PROJ[:8], "没成的调用不填项目那一列, 只留前 8 位"
    assert "comments" not in meta and "reply_chars" not in meta
    _no_text(row, REQ_MARK, "重复")


@pytest.mark.parametrize("channel", ["rest", "mcp"])
def test_a_real_failure_is_recorded_without_its_message(log, monkeypatch, channel):
    c = _drafts_client()
    monkeypatch.setattr(core, "sb", lambda: c)

    def boom(project_id: str, drafts: list[dict], _user_id=None):
        raise RuntimeError(f"撞车: {drafts[0]['title']} {ASCII_MARK}")
    monkeypatch.setattr(tools, "TOOLS", {"check_drafts": (boom, True)})
    args = {"project_id": PID, "drafts": DRAFTS}
    if channel == "rest":
        r = _rest().post("/tool/check_drafts", headers=H, json=args)
        assert r.status_code == 500 and re.fullmatch(r"RuntimeError \(ref [0-9a-f]{8}\)", r.json()["detail"])
    else:
        res = _mcp_call("check_drafts", args)
        assert res.isError is True and re.search(r"RuntimeError \(ref [0-9a-f]{8}\)$", res.content[0].text)
    (row,) = _rows(c)
    meta = row["meta"]
    assert (meta["channel"], meta["outcome"], meta["error_class"], meta["items"]) == (channel, "failed", "RuntimeError", 3)
    assert row["project_id"] is None and meta["project8"] == PID[:8] and "check" not in meta
    _no_text(row, REQ_MARK, ASCII_MARK, "MARKER", "撞车")


def test_an_error_shaped_result_is_not_recorded_as_ok(log, monkeypatch):
    c = _drafts_client()
    monkeypatch.setattr(core, "sb", lambda: c)
    monkeypatch.setattr(tools, "TOOLS", {"my_rules": (lambda project_id, _user_id=None: {
        "error": f"RuntimeError: {ASCII_MARK}", "hint": "写稿可以继续"}, True)})
    assert _rest().post("/tool/my_rules", headers=H, json={"project_id": PID}).status_code == 200
    (row,) = _rows(c)
    assert row["meta"]["outcome"] == "error_result" and row["project_id"] is None and row["meta"]["project8"] == PID[:8]
    _no_text(row, ASCII_MARK, "MARKER")


# ── 4. 写不进库不改回包; 队列有上限 ──────────────────────────────────────────────────────────────

def test_a_store_failure_does_not_change_the_reply(log, monkeypatch, caplog):
    def call(c):
        monkeypatch.setattr(core, "sb", lambda: c)
        monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
        r = _rest().post("/tool/check_drafts", headers=H, json={"project_id": PID, "drafts": DRAFTS})
        m = _mcp_call("check_drafts", {"project_id": PID, "drafts": DRAFTS})
        assert calllog.flush(5)
        return r.status_code, r.json(), m.isError, m.content[0].text

    monkeypatch.setenv(calllog.ENV_SWITCH, "0")
    baseline = call(_drafts_client())                     # 不留痕时的回包
    assert baseline[0] == 200 and calllog.stats()["written"] == 0

    monkeypatch.setenv(calllog.ENV_SWITCH, "1")
    broken = _drafts_client()
    real_table = broken.table

    def table(name):
        if name == "batch_metrics":
            raise RuntimeError(f"Failing row contains ({ASCII_MARK})")
        return real_table(name)
    broken.table = table
    with caplog.at_level(logging.INFO):
        assert call(broken) == baseline, "留痕写不进库, 回包一个字都不变"
    assert calllog.stats() == {"written": 0, "dropped": 0, "failed": 2, "pending": 0, "queue_max": calllog.QUEUE_MAX}
    assert not broken.rows.get("batch_metrics")
    mine = [x for x in caplog.records if x.name == "deskcore.calllog"]
    assert len(mine) == 1 and mine[0].levelno == logging.WARNING, "两次都没写进去, 只记一行(限频)"
    assert "RuntimeError" in mine[0].getMessage()
    assert ASCII_MARK not in caplog.text and "Failing row" not in caplog.text, "驱动的报错原文不进日志"


def test_the_queue_is_bounded_and_a_full_queue_drops_instead_of_waiting(log, monkeypatch):
    calllog.reset(maxsize=3)
    gate, entered, written = threading.Event(), threading.Event(), []

    def slow_store(row):
        entered.set()
        assert gate.wait(10)
        written.append(row)
    monkeypatch.setattr(calllog, "_store", slow_store)
    monkeypatch.setattr(tools, "TOOLS", {"my_rules": (lambda project_id, _user_id=None: {"rules": []}, True)})
    client = _rest()
    try:
        replies = []
        for k in range(12):                               # 后台卡在第一行上: 这 12 次调用照样马上回
            replies.append(client.post("/tool/my_rules", headers=H, json={"project_id": PID}))
            if k == 0:
                assert entered.wait(5)
        assert all(r.status_code == 200 and r.json() == {"result": {"rules": []}} for r in replies)
        stats = calllog.stats()
        assert stats["pending"] == 3 == stats["queue_max"], "队列里最多等 3 项"
        assert stats["dropped"] == 12 - 1 - 3, "第一行在后台手里, 3 项在队列里, 其余 8 项丢掉"
        assert calllog.flush(0.05) is False, "写不完就不等了"
    finally:
        gate.set()
    assert calllog.flush(5)
    assert len(written) == 4 and calllog.stats()["written"] == 4 and calllog.stats()["dropped"] == 8


def test_shutdown_waits_briefly_for_the_rows_still_queued(log, monkeypatch):
    """进程退出(app 的 lifespan 收尾)时尽力把队列里剩下的写完: 这一行还在后台手里, 收尾等它写完再走。"""
    import time
    written = []

    def slow_store(row):
        time.sleep(0.3)
        written.append(row)
    monkeypatch.setattr(calllog, "_store", slow_store)
    monkeypatch.setattr(A, "_mcp", None)

    async def lifespan():
        async with A._lifespan(A.app):
            assert calllog.submit(tool="my_rules", channel="rest", user_id=ME, args={}, sink={}, elapsed_ms=3,
                                  result={"rules": []}) is True
            assert written == []
    anyio.run(lifespan)
    assert len(written) == 1 and written[0]["meta"]["tool"] == "my_rules"


# ── 5. 不记的几种 ────────────────────────────────────────────────────────────────────────────────

def test_the_switch_get_protocol_and_unknown_callers_are_not_recorded(log, monkeypatch):
    c = _drafts_client()
    monkeypatch.setattr(core, "sb", lambda: c)
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    client = _rest()
    assert client.post("/tool/get_protocol", headers=H, json={}).status_code == 200
    assert _mcp_call("get_protocol", {}).isError is False
    # 认不出调用者(batch_metrics.user_id 是非空的 uuid 列, 不编一个填进去): 工具自己报 403, 不留痕
    assert _mcp_call("check_drafts", {"project_id": PID, "drafts": DRAFTS}, user=None).isError is True
    assert _mcp_call("check_drafts", {"project_id": PID, "drafts": DRAFTS}, user="not-a-uuid").isError is True
    monkeypatch.setenv(calllog.ENV_SWITCH, "0")
    assert client.post("/tool/check_drafts", headers=H, json={"project_id": PID, "drafts": DRAFTS}).status_code == 200
    assert _rows(c) == [] and calllog.stats()["written"] == 0
    for off in ("0", "false", "OFF", "no"):
        monkeypatch.setenv(calllog.ENV_SWITCH, off)
        assert calllog.enabled() is False
    monkeypatch.delenv(calllog.ENV_SWITCH)
    assert calllog.enabled() is True, "默认开"


def test_note_is_a_no_op_outside_a_recorded_call():
    assert calllog.active() is False
    calllog.note(n=3)                                     # CLI、用例直接调 core: 没人在收, 什么都不做
    sink: dict = {}
    assert calllog.collecting(sink, lambda: (calllog.active(), calllog.note(n=3))[0]) is True
    assert sink == {"n": 3} and calllog.active() is False


# ── 6. 最后一道筛 + 落库的函数 + 读的地方 ────────────────────────────────────────────────────────

def test_clean_keeps_only_numbers_booleans_and_known_words():
    dirty = {"n": 3, "ok": True, "word": "deskcore.tools.InvalidInput code=57014", "id8": "0a1b2c3d",
             "text": "一段评论的文字", "spaced": "two words", "punct": "a,b", "list": ["x", 1], "float": 0.5, "none": None,
             "中文键": 1, "nested": {"kind": 2, "text": "不行", "deep": {"a": {"b": {"c": {"d": 1}}}}}, "long": "x" * 81}
    assert calllog._clean(dirty) == {"n": 3, "ok": True, "word": "deskcore.tools.InvalidInput code=57014",
                                     "id8": "0a1b2c3d", "nested": {"kind": 2}}


def test_build_never_copies_text_even_if_core_notes_some():
    job = {"tool": "score_comments", "channel": "mcp", "user_id": ME, "elapsed_ms": 12.7,
           "args": {"project_id": PROJ, "comments": [{"text": REQ_MARK}], "post_body": REQ_MARK},
           "sink": {"n": 1, "source": REQ_MARK, "style": {"period": 1, REQ_MARK: 2}, "post8": "0a1b2c3d", "skill": ASCII_MARK},
           "result": {"judge_status": f"{REPLY_MARK}", "project_id": PROJ, "passed": "yes",
                      "summary": {"comments": 1, "passed": REPLY_MARK, "needs_review": 0},
                      "comments": [{"index": 0, "fix": [REPLY_MARK]}], "thread_fix": [REPLY_MARK],
                      "delivery_column": {"column": REPLY_MARK, "text": REQ_MARK, "lines": 1}}}
    row = calllog.build(job)
    assert row["project_id"] == PROJ and row["user_id"] == ME and row["phase_ms"] == {"total": 12}
    assert row["meta"]["comments"] == {"resolved": True, "post8": "0a1b2c3d", "n": 1, "needs_review": 0, "delivery_lines": 1,
                                       "checks": {"fix": 1, "style_fix": 0, "thread_fix": 1, "thread_hints": 0,
                                                  "style": {"period": 1}}}
    _no_text(row, REQ_MARK, REPLY_MARK, ASCII_MARK)


def test_the_store_function_always_stamps_the_mode():
    c = FakeClient()
    store.insert_tool_call_metrics(c, project_id=None, user_id=ME, phase_ms={"total": 1}, counters={},
                                   meta={"tool": "check_drafts", "mode": "deskcore_commit"})
    (row,) = c.rows["batch_metrics"]
    assert row["meta"]["mode"] == store.TOOL_CALL_MODE == calllog.MODE == "tool_call"
    assert row["batch_id"] is None and row["project_id"] is None and row["injection"] == {}


def test_the_history_page_reader_only_sees_rows_with_a_batch_id():
    """历史页读最近 N 条再按 batch_id 挂到批次上: 写作台的两种行(batch_id 都是 NULL)不在库里筛掉的话, 一天几十上百行的
    调用留痕会把真正的批次指标挤出最近 N 条。"""
    rows = [{"id": f"gen-{k}", "batch_id": f"b-{k}", "project_id": PID, "user_id": ME, "meta": {"mode": "queue"},
             "created_at": f"2026-08-1{k}T00:00:00+00:00"} for k in range(2)]
    rows += [{"id": "commit-1", "batch_id": None, "project_id": PID, "user_id": ME, "meta": {"mode": "deskcore_commit"},
              "created_at": "2026-11-10T00:00:00+00:00"}]
    rows += [{"id": f"call-{k}", "batch_id": None, "project_id": PID, "user_id": ME, "meta": {"mode": "tool_call"},
              "created_at": f"2026-11-11T00:{k:02d}:00+00:00"} for k in range(60)]
    c = FakeClient(rows={"batch_metrics": rows})
    got = db.list_batch_metrics(c, PID, limit=50, user_id=ME)
    assert [r["id"] for r in got] == ["gen-1", "gen-0"]
    (call,) = c.calls
    assert ("isnot", "batch_id", "null") in call["filters"], "筛在库里做, 不是读回来再丢"
