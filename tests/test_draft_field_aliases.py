"""正文 / 标题放错了字段(2026-10-30 发布收尾)。

core 只读 ``draft["body"]`` / ``draft["title"]``。模型把正文放进别的键(content / 正文 / text …), 稿子就成了只有标题的
一篇: 10-21 / 10-22 15 篇存成了只有标题的稿子, 10-26 一批 20 篇全被拒。那是模型放错了字段, 写手什么也没做错 ——
check_drafts / score_drafts(和旧名 commit_drafts)在工具层替它对上: 本名空着、别名里恰好一个有字才拿。

这里钉的【被禁止的形态】:
  · 别名里有正文, 查重却只按标题查(开头 / 四字串看不见), 底稿记不下这一篇;
  · 别名里有正文, 存档却被 empty_body 拒、角度不销账, 或存进 versions / 指纹的是空正文 —— 之后的规则检查
    (score_drafts 的 rule_checks、存档后的 check_rules)查的也就是空的;
  · 本名有字的被别名覆盖; 两个别名都有字时瞎猜一个;
  · 哪儿都没有正文的不再被拒(W-30 那道闸被这次放宽带松了);
  · 改了调用方传进来的 dict, 回包多出字段, 或日志里带了稿子内容。

所有标题、正文都是合成的示例。
"""

from __future__ import annotations

import logging

import pytest

import config
from deskcore import core, tools
from tests.test_delivery import distinct_drafts
from tests.test_deskcore_draft_identity import BODY_X, BODY_Y, ME, PROJ, _client, _embed, _fps, _rpc_that_writes
# _fresh_compile_state 是 test_rule_compile 的 autouse 夹具(清后台补编的进程内状态、关掉后台补编), 引进来才生效
from tests.test_rule_compile import _fresh_compile_state  # noqa: F401
from tests.test_rule_compile import PROJ as RULE_PROJ
from tests.test_rule_compile import code_result, jev_sha, make_client, mem, stored


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """不接判定、不接 embedding —— 这里只看正文有没有被读到。"""
    monkeypatch.setattr(config, "JUDGE_URL", "")
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)


def _desk(monkeypatch, **extra):
    c = _client(draft_snapshots=[], **extra)
    c.rpc_impl["deskcore_commit_fingerprints"] = _rpc_that_writes(c)
    monkeypatch.setattr(core, "sb", lambda: c)
    return c


# ══════════════════════════════════════════════════════════════════════
# 1 · 什么时候拿别名
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("draft, want_title, want_body", [
    ({"title": "标题", "content": "正文在 content 里"}, "标题", "正文在 content 里"),
    ({"标题": "中文键的标题", "正文": "中文键的正文"}, "中文键的标题", "中文键的正文"),
    ({"title": "标题", "body": "  \n ", "text": "正文在 text 里"}, "标题", "正文在 text 里"),
    ({"title": "标题", "body": None, "内容": "正文在 内容 里"}, "标题", "正文在 内容 里"),
    ({"title": "", "标题": "别名标题", "body": "正文"}, "别名标题", "正文"),
], ids=["content", "chinese-keys", "blank-body", "none-body", "title-alias"])
def test_an_empty_field_takes_the_one_alias_that_has_text(draft, want_title, want_body):
    before = dict(draft)
    (got,) = tools._draft_fields([draft], "score_drafts", PROJ)
    assert (got["title"], got["body"]) == (want_title, want_body)
    assert draft == before, "调用方传进来的那份不改"


@pytest.mark.parametrize("draft", [
    {"title": "标题", "body": "本名里的正文", "content": "别名里的另一段"},
    {"title": "本名标题", "标题": "别名标题", "body": "正文"},
], ids=["body", "title"])
def test_a_field_with_text_is_never_overwritten(draft):
    (got,) = tools._draft_fields([draft], "check_drafts", PROJ)
    assert (got["title"], got["body"]) == (draft["title"], draft["body"])


@pytest.mark.parametrize("draft", [
    {"title": "标题", "正文": "一段", "content": "另一段"},
    {"title": "标题", "content": ["分成了", "两段"]},
    {"title": "标题", "content": "   "},
    {"title": "标题", "angle_key": "K1"},
], ids=["two-aliases", "not-a-string", "blank-alias", "nowhere"])
def test_no_guessing_when_the_alias_is_ambiguous_or_missing(draft):
    (got,) = tools._draft_fields([draft], "score_drafts", PROJ)
    assert not (got.get("body") or "").strip(), "分不清 / 没有就不填, 交给 W-30 的 empty_body"


def test_odd_inputs_pass_through_untouched():
    assert tools._draft_fields("不是列表", "check_drafts", PROJ) == "不是列表"
    assert tools._draft_fields([], "check_drafts", PROJ) == []
    weird = ["一个字符串", None, {"title": "t", "body": "b"}]
    assert tools._draft_fields(weird, "check_drafts", PROJ) == weird


def test_the_log_names_the_keys_but_not_the_text(caplog):
    with caplog.at_level(logging.WARNING, logger="deskcore.tools"):
        tools._draft_fields([{"title": "合成标题甲", "content": "合成正文乙"},
                             {"title": "合成标题丙", "content": "合成正文丁"}], "score_drafts", PROJ)
    assert "content→body" in caplog.text and "2" in caplog.text
    for text in ("合成标题甲", "合成正文乙", "合成正文丁"):
        assert text not in caplog.text


# ══════════════════════════════════════════════════════════════════════
# 2 · 查重看得见正文
# ══════════════════════════════════════════════════════════════════════

def test_check_drafts_compares_the_body_found_under_an_alias(monkeypatch):
    """两篇正文一样、标题不同。正文要是没被读到, 两篇都只按标题比, 都 pass。"""
    c = _desk(monkeypatch)
    out = tools.check_drafts(PROJ, [{"title": "秋招等回音", "content": BODY_X, "angle_key": "K1"},
                                    {"title": "换了个标题", "正文": BODY_X}], _user_id=ME)
    assert [r["status"] for r in out["results"]] == ["pass", "reject"]
    assert out["results"][1]["collided_with"] == "秋招等回音"
    rows = c.rows["draft_snapshots"]
    assert [(r["title"], r["body"], r["angle_key"]) for r in rows] == [
        ("秋招等回音", BODY_X, "K1"), ("换了个标题", BODY_X, None)], "底稿记的是正文, 发出去以后对得回来"


# ══════════════════════════════════════════════════════════════════════
# 3 · 存档存的是正文
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("tool", [tools.score_drafts, tools.commit_drafts], ids=["score_drafts", "commit_drafts"])
def test_score_drafts_archives_the_body_found_under_an_alias(monkeypatch, tool):
    c = _desk(monkeypatch, angle_ledger=[
        {"id": "led-1", "project_id": PROJ, "angle_key": "K1", "consumed_version_id": None, "consumed_at": None}])
    drafts = [{"title": "秋招等回音", "content": BODY_X, "angle_key": "K1"}]
    out = tool(PROJ, drafts, _user_id=ME)
    assert out["written"] == 1 and out["rejected"] == [] and out["consumed_angles"] == 1
    (v,) = c.rows["versions"]
    assert v["body"] == BODY_X and v["id"] == out["version_ids"][0]
    assert _fps(c)[v["id"]]["opening"], "指纹有开头, 以后的查重看得见它"
    assert c.rows["angle_ledger"][0]["consumed_version_id"] == v["id"]
    assert drafts == [{"title": "秋招等回音", "content": BODY_X, "angle_key": "K1"}], "调用方传进来的那份不改"


def test_a_draft_with_no_body_anywhere_is_still_rejected(monkeypatch):
    """W-30 那道闸不因为认别名而放松: 哪儿都没正文的、两个别名分不清的, 照旧不存档、不销角度, 回包说清收到了哪些字段。"""
    c = _desk(monkeypatch)
    out = tools.score_drafts(PROJ, [{"title": "只有标题", "angle_key": "K1"},
                                    {"title": "两段正文", "正文": BODY_X, "content": BODY_Y},
                                    {"title": "好好的一篇", "body": BODY_Y}], _user_id=ME)
    assert [(r["index"], r["gate"]) for r in out["rejected"]] == [(0, "empty_body"), (1, "empty_body")]
    assert "angle_key, title" in out["rejected"][0]["reason"]
    assert "content, title, 正文" in out["rejected"][1]["reason"]
    assert out["written"] == 1 and [v["title"] for v in c.rows["versions"]] == ["好好的一篇"]


def test_the_reply_has_no_extra_fields(monkeypatch):
    _desk(monkeypatch)
    plain = tools.score_drafts(PROJ, [{"title": "秋招等回音", "body": BODY_X}], _user_id=ME)
    _desk(monkeypatch)
    aliased = tools.score_drafts(PROJ, [{"title": "秋招等回音", "content": BODY_X}], _user_id=ME)
    assert set(aliased) == set(plain)


# ══════════════════════════════════════════════════════════════════════
# 4 · 规则检查查的是正文
# ══════════════════════════════════════════════════════════════════════

R_SET = {"id": "m-1", "content": "示例硬规则: 正文不用「示例禁词」", "severity": "hard", "scope": "project",
         "project_id": PROJ, "user_id": ME, "status": "confirmed", "memory_type": "rule", "muted_until": None,
         "rule_kind": "forbidden_word", "rule_payload": {"words": ["示例禁词"], "where": ["body"]},
         "created_at": "2026-10-01T00:00:00+00:00", "frequency": 1, "applicability": None, "embedding": None}


def test_rule_checks_at_scoring_read_the_body_found_under_an_alias(monkeypatch):
    _desk(monkeypatch, memories=[R_SET])
    d = distinct_drafts(1)[0]
    out = tools.score_drafts(PROJ, [{"title": d["title"], "content": d["body"] + "示例禁词"}], _user_id=ME)
    assert [(h["index"], h["rule_id"], h["severity"]) for h in out["rule_checks"]] == [(0, "m-1", "hard")]


def test_check_rules_after_scoring_reads_the_body_found_under_an_alias(monkeypatch):
    """check_rules 收的是 version_ids, 读的是存进 versions 的那份 —— 存档时对上了正文, 它查到的就是正文。"""
    rule = mem(4, "示例软规则: 正文不用「示例禁词」")
    code = code_result(rule["id"], jev_sha(rule["content"]),
                       {"kind": "forbidden_word", "words": ["示例禁词"], "where": ["body"]})
    c = make_client([rule], [stored(rule, code)])
    c.rows["draft_fingerprints"] = []
    c.rpc_impl["deskcore_commit_fingerprints"] = _rpc_that_writes(c)
    monkeypatch.setattr(core, "sb", lambda: c)
    d = distinct_drafts(1)[0]
    saved = tools.score_drafts(RULE_PROJ, [{"title": d["title"], "content": d["body"] + "示例禁词"}], _user_id=ME)
    assert saved["written"] == 1
    _embed(c)
    for v in c.rows["versions"]:            # 假库没有真 join; _embed 按 test_deskcore_draft_identity 的项目嵌, 这里换成本项目
        v["items"]["batches"]["project_id"] = RULE_PROJ
    out = tools.check_rules(RULE_PROJ, saved["check_rules_version_ids"], _user_id=ME)
    assert out["status"] == "checked"
    (v,) = out["versions"]
    assert [(e["rule_id"], e["source"]) for e in v["violations"]] == [(rule["id"], "code")]
    assert out["summary"].get("empty_body", 0) == 0
