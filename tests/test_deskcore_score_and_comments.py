"""D-120(2026-10-19): 存档时的判定给写手看(score_drafts), 评论按链接写(comments_for / score_comments)。

钉住:

  1. 参考分的口径: 有把握的硬伤 -25、拿不准的 -8、没判出来的硬约束 -10, 正文太短不给分; fixes 有把握的排前、
     最多 3 条; judge 没判成就没有分(不编一个 100);
  2. judge_client 把修改单留下来(只留看得懂的几个字段), 证据句只在 plan 里带一份;
  3. 发布链接 → 键: 长链 / 短链 / 抖音各自认得, 查询参数不进键, 认不出的回 None;
  4. comments_for: 按链接找到 TV 的帖子 → 反查项目 → 过归属闸 → 已有评论 + 评论规则 + 默认评论位;
     找不到就回 resolved=false 让用户补, 不报错; 帖子不归调用者时 403、不回内容;
  5. score_comments: slot 写 id 就行, 贴主答疑自动接上它回的那一位; id 认不出 / 重复 → 参数错(400), 一个请求都不发;
     回包压成每条过没过 + 修改建议。
"""
from __future__ import annotations

import pytest

import judge_client
from deskcore import core, tools, tvlink
from tests.test_deskcore_commit_judge import ME, OTHER_PROJ, PROJ, _client
from tests.test_deskcore_judge_tools import _Resp, wired  # noqa: F401 — fixture

LINK = "https://www.xiaohongshu.com/explore/690873f30000000007030eb7?xsec_token=abc&xsource=pc"
NOTE = {"note_id": "TUGE_phase1_recA", "project_id": "TUGE_phase1", "title": "秋招投了六十份简历",
        "body": "投到第六十份的时候我开始怀疑是不是邮箱坏了。" * 3, "raw_content": "",
        "publish_url": "https://www.xiaohongshu.com/explore/690873f30000000007030eb7?xsec_token=zzz",
        "publish_time": "2026-10-10T08:00:00", "comments_count": 2, "tier": "爆"}


def _ok(res=None, **kw):
    return judge_client.result(judge_client.JUDGE_OK, **{**(res or {}), **kw})


# ── 1. 参考分 ─────────────────────────────────────────────────────────────────────────────────

def test_clean_draft_scores_100_with_no_fixes():
    assert core.draft_score(_ok(passed=True)) == {"score": 100, "fixes": []}


def test_unsure_fails_cost_less_than_sure_ones_and_sure_fixes_come_first():
    res = _ok(passed=False,
              hard_fails=[["platform_health_v0.1", "fear_sell", "是", 0.56],
                          ["platform_health_v0.1", "efficacy_claim", "是", 0.91]],
              plan=[{"kind": "hard", "qid": "fear_sell", "p": 0.56, "instruction": "制造焦虑那句"},
                    {"kind": "hard", "qid": "efficacy_claim", "p": 0.91, "instruction": "疗效承诺那句"}])
    out = core.draft_score(res)
    assert out["score"] == 100 - 25 - 8
    assert [f["instruction"] for f in out["fixes"]] == ["疗效承诺那句", "制造焦虑那句"]
    assert [f["sure"] for f in out["fixes"]] == [True, False]


def test_the_production_shape_of_today_is_all_unsure():
    """10-19 线上: 影子期判出的硬伤 p 全在 0.54~0.69。一条 0.55 的疑似不该把一篇稿打到 75。"""
    out = core.draft_score(_ok(hard_fails=[["platform_health_v0.1", "efficacy_claim", "是", 0.55]],
                               plan=[{"kind": "hard", "qid": "efficacy_claim", "p": 0.55, "instruction": "x"}]))
    assert out["score"] == 92 and out["fixes"][0]["sure"] is False


def test_fixes_are_capped_and_the_rest_is_counted():
    plan = [{"kind": "hard", "qid": f"q{i}", "p": 0.9, "instruction": f"第 {i} 条"} for i in range(5)]
    out = core.draft_score(_ok(hard_fails=[["b", f"q{i}", "是", 0.9] for i in range(5)], plan=plan))
    assert len(out["fixes"]) == core.SCORE_MAX_FIXES and out["more_fixes"] == 2
    assert out["score"] == 0, "分数不为负"


def test_unjudged_constraints_cost_points_and_are_reported():
    out = core.draft_score(_ok(unjudged=2))
    assert out == {"score": 80, "fixes": [], "unjudged": 2}


def test_too_short_to_judge_gets_no_score_not_a_perfect_one():
    out = core.draft_score(_ok(invalid_reason="text_too_short"))
    assert out["score"] is None and "太短" in out["why"]


@pytest.mark.parametrize("status", [judge_client.JUDGE_TIMEOUT, judge_client.JUDGE_NOT_CONFIGURED,
                                    judge_client.JUDGE_POLICY_BLOCKED])
def test_no_score_when_the_judge_did_not_answer(status):
    assert core.draft_score(judge_client.result(status)) == {}
    row = core._judge_row({"index": 0, "version_id": "v"}, judge_client.result(status))
    assert "score" not in row and row["judge_status"] == status


def test_the_block_is_visible_and_says_how_to_use_it():
    block = core._judge_block([], [])
    assert block["visible"] is True and "shadow" not in block
    assert "replaces_version_id" in block["note"] and "不拦交付" in block["note"]


# ── 2. judge_client 留下修改单 ─────────────────────────────────────────────────────────────────

def test_normalize_keeps_a_trimmed_plan_and_the_counts():
    norm = judge_client._normalize_ok({
        "passed": False, "hard_fails": [["b", "q", "是", 0.6, "依据句"]],
        "plan": [{"kind": "hard", "qid": "q", "p": 0.6, "evidence": "依据句", "definition": "长定义",
                  "instruction": "「题」现在判「是」。依据句：依据句"},
                 {"kind": "hard", "qid": "q2", "instruction": "   "}, "not a dict"],
        "unjudged": [["b", "q3", "missing"]], "invalid_reason": None})
    assert norm["hard_fails"] == [["b", "q", "是", 0.6]], "证据句不在 hard_fails 里重复一份"
    assert norm["plan"] == [{"kind": "hard", "qid": "q", "p": 0.6,
                             "instruction": "「题」现在判「是」。依据句：依据句"}]
    assert norm["plan_size"] == 3 and norm["unjudged"] == 1 and norm["invalid_reason"] is None


def test_result_shape_always_has_the_plan_fields():
    r = judge_client.result(judge_client.JUDGE_TIMEOUT)
    assert r["plan"] == [] and r["unjudged"] == 0 and r["invalid_reason"] is None


# ── 3. 发布链接 → 键 ───────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url, key", [
    (LINK, "xhs:690873f30000000007030eb7"),
    ("https://www.xiaohongshu.com/discovery/item/690873F30000000007030EB7", "xhs:690873f30000000007030eb7"),
    ("http://xhslink.com/o/A7x75NfzArL", "xhslink:A7x75NfzArL"),
    ("78 【标题】 http://xhslink.cn/A7x75NfzArL 复制后打开", "xhslink:A7x75NfzArL"),
    ("https://v.douyin.com/_1SZJuxIEqU/", "dy:_1SZJuxIEqU"),
    ("https://www.douyin.com/video/7412345678901234567", "dyv:7412345678901234567"),
    ("http://t.cn/A6abcd", "tcn:A6abcd"),
])
def test_link_keys(url, key):
    assert tvlink.link_key(url)[0] == key


@pytest.mark.parametrize("url", ["", None, "随便一段话", "https://example.com/a,b(c)"])
def test_unknown_links_have_no_key(url):
    assert tvlink.link_key(url) is None


def test_short_link_ids_are_case_sensitive():
    assert tvlink.link_key("xhslink.com/o/AbCdEf") != tvlink.link_key("xhslink.com/o/abcdef")


# ── 4. comments_for ────────────────────────────────────────────────────────────────────────────

def _tv_client(*, notes=None, comments=None, rules=None, **kw):
    c = _client(**kw)
    c._tv.rows["notes"] = list(notes if notes is not None else [dict(NOTE)])
    c._tv.rows["comments"] = list(comments if comments is not None else [
        {"comment_id": "c2", "note_id": NOTE["note_id"], "comment_order": 2, "comment_role": "贴主",
         "content": "可以看看简历第一行", "parent_comment_id": "c1", "is_pinned": False},
        {"comment_id": "c1", "note_id": NOTE["note_id"], "comment_order": 1, "comment_role": "素人",
         "content": "那后来怎么改的", "parent_comment_id": None, "is_pinned": False}])
    c.rows["memories"] = list(rules if rules is not None else [
        {"id": "m1", "project_id": PROJ, "scope": "project", "status": "confirmed", "severity": "soft",
         "content": "评论区：提到途鸽时不用代称", "memory_type": "rule", "created_at": "2026-10-01"},
        {"id": "m2", "project_id": PROJ, "scope": "project", "status": "confirmed", "severity": "hard",
         "content": "不能承诺包过", "memory_type": "rule", "created_at": "2026-10-01"},
        {"id": "m3", "project_id": PROJ, "scope": "project", "status": "confirmed", "severity": "soft",
         "content": "正文结尾留白", "memory_type": "rule", "created_at": "2026-10-01"}])
    return c


def test_a_link_brings_back_the_post_its_comments_rules_and_slots():
    c = _tv_client()
    out = core.comments_for(c, user_id=ME, link=LINK)
    assert out["resolved"] is True and out["source"] == "tv" and out["project_id"] == PROJ
    assert out["post"]["title"] == NOTE["title"] and out["post"]["body"] == NOTE["body"]
    assert [x["text"] for x in out["existing_comments"]] == ["那后来怎么改的", "可以看看简历第一行"], "按评论顺序"
    assert out["existing_comments"][1]["is_reply"] is True
    assert out["comment_rules"] == ["评论区：提到途鸽时不用代称"]
    assert out["hard_rules"] == ["不能承诺包过"], "合规硬约束对评论一样管用; 正文偏好不带"
    assert [s["id"] for s in out["slots"]] == ["q1", "q2", "exp", "reply"]
    assert not any("背书" in a for s in out["slots"] for a in s["speech_act"]), "默认不放背书位"
    assert out["note_id"] == NOTE["note_id"] and out["tier"] == "爆"


def test_the_coarse_filter_is_rechecked_by_key():
    """ilike 不分大小写: 短链 id 大小写不同的另一条笔记不能被当成这条。"""
    other = dict(NOTE, note_id="X", publish_url="http://xhslink.com/o/abcdefgh")
    mine = dict(NOTE, note_id="Y", publish_url="http://xhslink.com/o/ABCDEFGH")
    c = _tv_client(notes=[other, mine], comments=[])
    out = core.comments_for(c, user_id=ME, link="xhslink.com/o/ABCDEFGH")
    assert out["note_id"] == "Y"


def test_link_not_in_tv_asks_for_the_text_instead_of_failing():
    c = _tv_client(notes=[])
    out = core.comments_for(c, user_id=ME, link=LINK)
    assert out["resolved"] is False and "帖子的标题和正文" in out["need"]
    assert "TV 里还没有这条链接" in out["note"]
    out = core.comments_for(c, user_id=ME, link=LINK, project_id=PROJ, post_title="t", post_body="贴过来的正文")
    assert out["resolved"] is True and out["source"] == "pasted" and out["post"]["body"] == "贴过来的正文"
    assert out["existing_comments"] == []


def test_pasted_text_wins_over_the_tv_copy():
    out = core.comments_for(_tv_client(), user_id=ME, link=LINK, post_body="发布后改过的正文")
    assert out["source"] == "tv" and out["post"]["body"] == "发布后改过的正文"


def test_nothing_given_says_what_is_missing():
    out = core.comments_for(_tv_client(), user_id=ME)
    assert out["resolved"] is False and len(out["need"]) == 2


def test_someone_elses_post_is_refused_without_leaking_it():
    c = _tv_client(maps=[{"tv_project_id": "TUGE_phase1", "project_id": OTHER_PROJ, "ingest_target": True}])
    c.rows["projects"].append({"id": OTHER_PROJ, "name": "别人的", "owner_id": "someone-else"})
    with pytest.raises(PermissionError) as e:
        core.comments_for(c, user_id=ME, link=LINK)
    assert "TUGE_phase1" not in str(e.value) and NOTE["title"] not in str(e.value)


def test_a_link_from_another_project_than_the_one_given_is_refused():
    c = _tv_client()
    c.rows["projects"].append({"id": OTHER_PROJ, "name": "我的另一个", "owner_id": ME})
    with pytest.raises(PermissionError):
        core.comments_for(c, user_id=ME, link=LINK, project_id=OTHER_PROJ)


def test_pasted_text_still_goes_through_the_ownership_gate():
    c = _tv_client()
    c.rows["projects"].append({"id": OTHER_PROJ, "name": "别人的", "owner_id": "someone-else"})
    with pytest.raises(PermissionError):
        core.comments_for(c, user_id=ME, project_id=OTHER_PROJ, post_body="正文")


def test_comments_for_is_an_entry_tool_for_the_skill_probe(monkeypatch):
    from deskcore import skillcheck
    skillcheck.reset()
    c = _tv_client()
    monkeypatch.setattr(core, "sb", lambda: c)
    out = tools.comments_for(link=LINK, _user_id=ME)
    assert out["skill_update"]["status"] == skillcheck.UNKNOWN
    _, current = tools.protocol_text()
    out = tools.comments_for(link=LINK, skill_version=current, _user_id=ME)
    assert "skill_update" not in out
    skillcheck.reset()


# ── 5. score_comments ──────────────────────────────────────────────────────────────────────────

THREAD_OK = {
    "passed": False, "needs_review": False, "hard_fails": [], "unjudged": [], "profile": {}, "code": {},
    "ambiguous": [], "advisory": [], "calls": 9, "policy": {},
    "plan": [{"slot": "q1", "why": "评论区没有摩擦：这一位改成审慎的追问"}],
    "hints": [{"slot": None, "why": "评论区参考题（未验收）不理想：praise_share"}],
    "comments": [
        {"passed": False, "needs_review": False, "profile": {}, "flags": [], "hard_fails": [], "ambiguous": [],
         "plan": [{"qid": "echoes_post", "instruction": "要接住帖子里的事"}],
         "hints": [{"qid": "register", "instruction": "口语一点"}], "advisory": [], "calls": 2},
        {"passed": True, "needs_review": False, "profile": {}, "flags": [], "hard_fails": [], "ambiguous": [],
         "plan": [], "hints": [], "advisory": [], "calls": 2}],
}


def test_slots_by_id_and_the_reply_picks_up_the_question(wired):
    calls, reply = wired
    reply["fn"] = lambda m, u, kw: _Resp(200, dict(THREAD_OK))
    c = _tv_client()
    out = core.score_comments(c, user_id=ME, link=LINK, comments=[
        {"slot": "q1", "text": "简历第一行写的什么呀"},
        {"slot": "reply", "text": "写的是最近一段实习做成了什么"}])
    (_m, url, _h, body, _t), = calls
    assert url.endswith("/judge_thread") and body["project"] == "TUGE_phase1"
    assert body["post_title"] == NOTE["title"]
    q1, rep = body["comments"]
    assert q1["slot"]["id"] == "q1" and q1["role"] == "读者位"
    assert rep["slot"]["id"] == "reply" and rep["role"] == "贴主"
    assert rep["reply_to_text"] == "简历第一行写的什么呀", "贴主答疑自动接上它回的那一位"
    assert out["judge_status"] == "ok" and out["summary"] == {"comments": 2, "passed": 1, "needs_review": 0}
    assert out["comments"][0]["fix"] == ["要接住帖子里的事"] and out["comments"][0]["hints"] == ["口语一点"]
    assert "fix" not in out["comments"][1]
    assert out["thread_fix"] == ["位 q1: 评论区没有摩擦：这一位改成审慎的追问"]
    assert out["thread_hints"] == ["评论区参考题（未验收）不理想：praise_share"]
    assert "ledger_rows" not in out and "profile" not in out, "回包压过, 不把整份判定原样灌给模型"


@pytest.mark.parametrize("comments, why", [
    ([], "不能为空"),
    ([{"slot": "q1", "text": "  "}], "没有 text"),
    ([{"slot": "q9", "text": "x"}], "不是 comments_for 给的评论位"),
    ([{"slot": "q1", "text": "a"}, {"slot": "q1", "text": "b"}], "重复"),
    ([{"slot": "q1", "text": "a"}] * 21, "最多"),
])
def test_bad_comment_input_is_a_400_and_sends_nothing(wired, monkeypatch, comments, why):
    calls, _ = wired
    c = _tv_client()
    monkeypatch.setattr(core, "sb", lambda: c)
    with pytest.raises(tools.InvalidInput) as e:
        tools.score_comments(comments, link=LINK, _user_id=ME)
    assert why in str(e.value)
    assert calls == []


def test_bad_kind_is_a_400(wired, monkeypatch):
    c = _tv_client()
    monkeypatch.setattr(core, "sb", lambda: c)
    with pytest.raises(tools.InvalidInput):
        tools.score_comments([{"slot": "q1", "text": "x"}], link=LINK, kind="brand", _user_id=ME)


def test_judge_not_answering_is_said_plainly(wired):
    _, reply = wired
    reply["fn"] = lambda m, u, kw: _Resp(503, {"detail": "评论判定服务未开启"})
    out = core.score_comments(_tv_client(), user_id=ME, link=LINK, comments=[{"slot": "q1", "text": "x"}])
    assert out["judge_status"] != "ok" and "passed" not in out and out["detail"]


def test_unresolved_post_is_passed_back_not_judged(wired):
    calls, _ = wired
    out = core.score_comments(_tv_client(notes=[]), user_id=ME, link=LINK, comments=[{"slot": "q1", "text": "x"}])
    assert out["resolved"] is False and calls == []


def test_score_comments_checks_ownership_before_judging(wired):
    calls, _ = wired
    c = _tv_client()
    c.rows["projects"].append({"id": OTHER_PROJ, "name": "别人的", "owner_id": "someone-else"})
    with pytest.raises(PermissionError):
        core.score_comments(c, user_id=ME, project_id=OTHER_PROJ, post_body="正文",
                            comments=[{"slot": "q1", "text": "x"}])
    assert calls == []


# ── codex review on aw#107 ─────────────────────────────────────────────────────────────────────

def test_a_malformed_ok_body_is_an_error_not_a_perfect_score():
    for body in ({"passed": False}, {"passed": True, "hard_fails": [], "plan": []},
                 {"passed": True, "hard_fails": "x", "plan": [], "unjudged": []}):
        assert judge_client._normalize_ok(body) is None, body


def test_confident_fixes_past_the_cap_are_not_crowded_out():
    plan = ([{"kind": "hard", "qid": f"u{i}", "p": 0.55, "instruction": f"拿不准 {i}"} for i in range(15)]
            + [{"kind": "hard", "qid": "s", "p": 0.95, "instruction": "有把握的那条"}])
    norm = judge_client._normalize_ok({"passed": False, "hard_fails": [], "plan": plan, "unjudged": []})
    assert norm["plan"][0]["instruction"] == "有把握的那条" and norm["plan_size"] == 16
    out = core.draft_score(_ok(**norm))
    assert out["fixes"][0] == {"sure": True, "instruction": "有把握的那条"}
    assert out["more_fixes"] == 16 - core.SCORE_MAX_FIXES, "没列出的按 judge 回的总数算"


def test_custom_slot_without_an_id_is_a_400(wired, monkeypatch):
    calls, _ = wired
    c = _tv_client()
    monkeypatch.setattr(core, "sb", lambda: c)
    with pytest.raises(tools.InvalidInput) as e:
        tools.score_comments([{"slot": {"speech_act": ["提问"]}, "text": "x"}], link=LINK, _user_id=ME)
    assert "带 id" in str(e.value) and calls == []


def test_a_short_per_comment_answer_is_not_reported_as_ok(wired):
    _, reply = wired
    reply["fn"] = lambda m, u, kw: _Resp(200, dict(THREAD_OK, comments=THREAD_OK["comments"][:1]))
    out = core.score_comments(_tv_client(), user_id=ME, link=LINK,
                              comments=[{"slot": "q1", "text": "a"}, {"slot": "reply", "text": "b"}])
    assert out["judge_status"] == "error" and "comments" not in out and "1 条" in out["detail"]


def test_a_tv_note_without_body_asks_for_the_text():
    c = _tv_client(notes=[dict(NOTE, body="", raw_content="")])
    out = core.comments_for(c, user_id=ME, link=LINK)
    assert out["resolved"] is False and out["need"] == ["帖子的正文"] and out["project_id"] == PROJ
    out = core.comments_for(c, user_id=ME, link=LINK, post_body="贴来的正文")
    assert out["resolved"] is True and out["post"]["body"] == "贴来的正文"


def test_hostnames_are_case_insensitive_but_ids_keep_their_case():
    assert tvlink.link_key("HTTPS://WWW.XIAOHONGSHU.COM/explore/690873F30000000007030EB7")[0] == \
        "xhs:690873f30000000007030eb7"
    assert tvlink.link_key("http://XHSLINK.COM/o/AbCdEf")[0] == "xhslink:AbCdEf"
