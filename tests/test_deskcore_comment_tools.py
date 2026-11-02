import hashlib
import re

import pytest
import requests
import concurrent.futures as cf
import threading
import time
import judge_client

from deskcore import core, store, tools
from tests.test_deskcore_judge_tools import _Resp, wired
from tests.test_deskcore_commit_judge import ME, PROJ, OTHER_PROJ, _client
from tests.test_deskcore_score_and_comments import LINK, NOTE, THREAD_OK, _tv_client


def test_single_comment_contract_and_authenticated_project(wired):
    calls, reply = wired
    reply["fn"] = lambda *args: type("Reply", (), {"status_code": 200, "json": lambda self:
        {"passed": False, "plan": [{"qid": "register"}], "needs_review": True,
         "profile": {}, "hard_fails": [], "ambiguous": [], "ledger_rows": [{"secret": "x"}]}})()
    out = core.comment_repair_plan_for(_client(), PROJ, "标题", "正文", "评论", {"id": "q", "speech_act": ["提问"]}, user_id=ME)
    assert out["judge_status"] == "ok" and out["needs_review"] is True
    assert "ledger_rows" not in out
    method, url, headers, body, timeout = calls[0]
    assert url.endswith("/comment_repair_plan_for") and body["project"] == "TUGE_phase1"
    assert body["category"] == "教育" and "write" not in body and "published" not in body
    assert 0 < timeout[1] <= 18


def test_comment_tools_preserve_ownership_and_bad_response_is_not_ok(wired):
    calls, reply = wired
    client = _client()
    client.rows["projects"].append({"id": OTHER_PROJ, "owner_id": "someone-else"})
    with pytest.raises(PermissionError):
        core.judge_thread(client, OTHER_PROJ, "t", "b", [{"text": "x"}], user_id=ME)
    assert calls == []
    reply["fn"] = lambda *args: type("Reply", (), {"status_code": 200, "json": lambda self: {"detail": "oops"}})()
    out = core.judge_thread(_client(), PROJ, "t", "b", [{"text": "x"}], user_id=ME)
    assert out["judge_status"] == "error" and "passed" not in out


def test_comments_timeout_and_registered_tools(wired):
    _, reply = wired
    def fail(*args):
        raise requests.Timeout("slow")
    reply["fn"] = fail
    out = core.judge_thread(_client(), PROJ, "t", "b", [{"text": "x"}], user_id=ME)
    assert out["judge_status"] == "timeout" and "passed" not in out
    # D-120: 写手侧换成 comments_for / score_comments(按链接取帖子、整组判); 单条 / 整组两个旧入口只留在 core
    assert tools.TOOLS["comments_for"][1] and tools.TOOLS["score_comments"][1]
    assert "judge_thread" not in tools.TOOLS and "comment_repair_plan_for" not in tools.TOOLS


@pytest.mark.parametrize("body", [
    {"passed": True, "plan": []},
    {"passed": True, "plan": [], "needs_review": "false"},
    {"passed": True, "plan": [], "needs_review": True},
])
def test_missing_or_contradictory_review_contract_is_not_ok(wired, body):
    _, reply = wired
    reply["fn"] = lambda *args: type("Reply", (), {"status_code": 200, "json": lambda self: body})()
    out = core.comment_repair_plan_for(_client(), PROJ, "标题", "正文", "评论", {}, user_id=ME)
    assert out["judge_status"] == "error" and "passed" not in out


def test_http_phase_has_outer_deadline_and_surviving_work_is_bounded(monkeypatch):
    release, entered = threading.Event(), threading.Event()
    pool, slots = cf.ThreadPoolExecutor(max_workers=1), threading.BoundedSemaphore(1)
    monkeypatch.setattr(core, "_COMMENT_POOL", pool)
    monkeypatch.setattr(core, "_COMMENT_SLOTS", slots)
    monkeypatch.setattr(judge_client, "configured", lambda: True)
    monkeypatch.setattr(judge_client, "tool_timeout_sec", lambda: 0.7)
    monkeypatch.setattr(core, "_judge_project_context", lambda *a: {"project": "TUGE", "category": "教育"})
    def slow(*a, **kw):
        entered.set()
        assert release.wait(3)
        return {"judge_status": "ok", "response": {"passed": True}}
    monkeypatch.setattr(judge_client, "judge_comment_tool", slow)
    try:
        started = time.monotonic()
        out = core._comment_tool_call(None, PROJ, "/judge_thread", {})
        assert entered.is_set() and time.monotonic() - started < 1.2
        assert out["judge_status"] == "timeout" and "passed" not in out
        # Timeout did not release the running I/O slot: repeated calls cannot start more workers.
        for _ in range(20):
            assert core._comment_tool_call(None, PROJ, "/judge_thread", {})["judge_status"] == "timeout"
        assert len(pool._threads) == 1
    finally:
        release.set()
        pool.shutdown(wait=True)


# ── 没有链接时按贴来的正文找帖(2026-11-02) ───────────────────────────────────────────────────────
# 写手只贴正文 + project_id(飞书那一行的正文格整段复制), 原来找不到 TV 的笔记, existing_comments 永远是空的。现在在这个
# 写作台项目对着的 TV 项目里按正文找一次。文字全是编的; TV 的行照生产库的形状: title / body 列是空的, 全在 raw_content 里。
TV = "TUGE_phase1"
TITLE_A = "通勤路上背单词的第三个月"
BODY_A = ("每天地铁上那四十分钟我以前都在刷短视频，后来换成背单词，第一个月几乎每天都想放弃。\n\n"
          "真正坚持下来是因为我把单词本换成了自己录的语音，耳机里放着走路也能听。"
          "现在第三个月词汇量测出来涨了一千出头，想问问大家通勤时间还能拿来做什么更有用的事？")
TAGS = "#通勤 #背单词 #自我提升"
SLOGAN = "晨读打卡本每天十分钟坚持一百天"
_RUN_PATTERN = re.compile(f"%[{store.TV_TEXT_RUN_CHARS}]{{8,16}}%")


def _note(note_id, title=TITLE_A, body=BODY_A, *, project=TV, when="2026-10-20T16:00:00", count=1):
    return {"note_id": note_id, "project_id": project, "title": None, "body": None,
            "raw_content": f"【标题】{title}\n【正文】{body}\n\n{TAGS}",
            "publish_url": f"https://www.xiaohongshu.com/explore/{hashlib.sha1(note_id.encode()).hexdigest()[:24]}",
            "publish_time": when, "comments_count": count, "tier": "中"}


def _comment(note_id, text="自己录语音用的什么软件呀"):
    return {"comment_id": f"{note_id}-c1", "note_id": note_id, "comment_order": 1, "comment_role": "素人",
            "content": text, "parent_comment_id": None, "is_pinned": False}


def _notes_calls(c):
    return [x for x in c._tv.calls if x["table"] == "notes"]


def test_a_pasted_body_finds_the_post_and_brings_its_comments():
    c = _tv_client(notes=[_note("A")], comments=[_comment("A")])
    out = core.comments_for(c, user_id=ME, project_id=PROJ, post_body=BODY_A)
    assert out["resolved"] is True and out["source"] == "tv_by_text" and out["project_id"] == PROJ
    assert out["note_id"] == "A" and out["tier"] == "中"
    assert [x["text"] for x in out["existing_comments"]] == ["自己录语音用的什么软件呀"]
    assert out["post"] == {"title": "", "body": BODY_A}, "标题正文照旧用贴来的, TV 那份只用来取评论"
    assert out["tv_by_text_note"] == core.TV_BY_TEXT_NOTE and "tv_note_vanished" not in out
    calls = _notes_calls(c)
    assert len(calls) == len(core._text_probe(BODY_A)[1]) == 2, "两段都查(不提前停), 最多两次"
    for call in calls:
        assert ("in", "project_id", (TV,)) in call["filters"] and call["range"] == (0, store.TV_TEXT_PAGE - 1)
        (pattern,) = [f[2] for f in call["filters"] if f[0] == "ilike"]
        assert [f[1] for f in call["filters"] if f[0] == "ilike"] == ["raw_content"] and _RUN_PATTERN.fullmatch(pattern)


@pytest.mark.parametrize("pasted", [
    f"{TITLE_A}\n{BODY_A}\n\n{TAGS}",                               # 飞书正文格: 第一行是标题, 末尾一行话题
    f"【标题】{TITLE_A}\n【正文】{BODY_A}\n{TAGS}",                    # 带标记的整格
    f"{BODY_A}\n#通勤[话题]# #背单词[话题]#",                          # 只有正文, 话题是小红书那种写法
    "  " + BODY_A.replace("\n\n", "\n").replace("，", ", ") + "  ",   # 换行、标点和 TV 那份不一样
])
def test_the_feishu_cell_matches_with_or_without_title_line_and_tags(pasted):
    c = _tv_client(notes=[_note("A")], comments=[_comment("A")])
    out = core.comments_for(c, user_id=ME, project_id=PROJ, post_title="", post_body=pasted)
    assert out["source"] == "tv_by_text" and out["note_id"] == "A" and len(out["existing_comments"]) == 1
    assert out["post"]["body"] == pasted.strip()


def test_slightly_edited_text_still_matches_and_a_rewrite_sharing_a_sentence_does_not():
    edited = BODY_A.replace("涨了一千出头", "涨了一千多").replace("耳机里放着", "耳机里一直放着")
    rewrite = ("真正坚持下来是因为我把单词本换成了自己录的语音，这招是室友教我的。\n\n以前我总觉得背单词就得坐在书桌前，"
               "后来发现碎片时间反而记得牢，现在午饭排队的时候也会听两遍，想问问大家还有什么省时间的小办法？")
    sim = lambda text: core._text_score(core._text_probe(text)[0], TITLE_A, BODY_A)
    assert core.TEXT_MATCH_SIM <= sim(edited) < 1.0 and sim(rewrite) < 0.3
    c = _tv_client(notes=[_note("A")], comments=[_comment("A")])
    assert core.comments_for(c, user_id=ME, project_id=PROJ, post_body=edited)["note_id"] == "A"
    out = core.comments_for(c, user_id=ME, project_id=PROJ, post_body=rewrite)
    assert out["source"] == "pasted" and "note_id" not in out and out["existing_comments"] == []


# 同标题的另一版: 正文差几处, 正文对正文 0.897(阈值就是按这个量的, 生产库同标题不同链接的两组是 0.893 / 0.897)
SIBLING_A = BODY_A.replace("四十分钟", "半小时").replace("更有用的事", "更划算的事")


def test_a_same_title_sibling_is_not_pushed_over_the_line_by_the_title():
    """复审(on a3d12e6): 飞书正文格带着标题行贴过来, 原来比的是「标题 + 正文」对「标题 + 正文」, 同标题的另一版多出一截
    共同的四字串, 0.897 被推到 0.907、认成这一篇, 把它的评论安了过来。现在标题行按笔记的标题剥掉, 只比正文。"""
    form = lambda text: (core.fp.normalize(text), core.tvlink.grams(text))
    assert core._text_sim(form(BODY_A), form(SIBLING_A)) < core.TEXT_MATCH_SIM
    assert core._text_sim(form(TITLE_A + BODY_A), form(TITLE_A + SIBLING_A)) >= core.TEXT_MATCH_SIM, "带上标题会过线"
    c = _tv_client(notes=[_note("Y", body=SIBLING_A)], comments=[_comment("Y", "另一版下面的评论")])
    for pasted in (f"{TITLE_A}\n{BODY_A}\n\n{TAGS}", f"【标题】{TITLE_A}\n【正文】{BODY_A}", BODY_A):
        out = core.comments_for(c, user_id=ME, project_id=PROJ, post_body=pasted)
        assert out["source"] == "pasted" and "note_id" not in out and out["existing_comments"] == [], pasted[:20]
    # 这一篇自己在 TV 里时照样认得出(两版都在: 取原文那条)
    c = _tv_client(notes=[_note("Y", body=SIBLING_A, when="2026-10-28T16:00:00"), _note("A")],
                   comments=[_comment("Y", "另一版下面的评论"), _comment("A")])
    out = core.comments_for(c, user_id=ME, project_id=PROJ, post_body=f"{TITLE_A}\n{BODY_A}\n\n{TAGS}")
    assert out["source"] == "tv_by_text" and out["note_id"] == "A"
    assert [x["text"] for x in out["existing_comments"]] == ["自己录语音用的什么软件呀"]


def test_a_body_that_itself_starts_with_the_title_is_not_trimmed():
    """正文自己以标题开头的笔记(生产库 3295 篇里 14 篇): 不剥, 贴不贴标题行都认。"""
    body = f"{TITLE_A}。{BODY_A}"
    c = _tv_client(notes=[_note("A", body=body)], comments=[_comment("A")])
    for pasted in (body, f"{TITLE_A}\n{body}\n{TAGS}"):
        out = core.comments_for(c, user_id=ME, project_id=PROJ, post_body=pasted)
        assert out["source"] == "tv_by_text" and out["note_id"] == "A", pasted[:20]


def test_posts_that_only_share_a_slogan_are_not_matched():
    """同项目的几篇共用一句话术(粗筛那一段正好是它): 粗筛能撞上, 全文一比都不到阈值 —— 不把别人的评论安过来。"""
    others = [_note(f"B{i}", title=f"早起第{i}周", body=f"开学第{i}周我终于把早起这件事固定下来了，闹钟定在六点{i}十分。"
                    f"{SLOGAN}，这句话贴在书桌上。现在每天早上能多读{i}篇英语短文，你们早起之后都做什么？") for i in range(1, 4)]
    pasted = (f"考研倒计时还剩九十天，我把复习计划拆成三块，上午背书下午刷题。{SLOGAN}，这是研友群里大家互相提醒的话。"
              "最近总担心政治来不及，有没有过来人说说怎么安排？")
    assert core._text_probe(pasted)[1][0] == SLOGAN, "这一段是全文最长的, 先拿它粗筛"
    c = _tv_client(notes=others, comments=[_comment(n["note_id"]) for n in others])
    out = core.comments_for(c, user_id=ME, project_id=PROJ, post_body=pasted)
    assert out["source"] == "pasted" and "note_id" not in out and out["existing_comments"] == []
    calls = _notes_calls(c)
    assert len(calls) == core.TEXT_MATCH_TRIES == 2, "最多粗筛两段"
    assert ("ilike", "raw_content", f"%{SLOGAN}%") in calls[0]["filters"]
    assert len(store.tv_notes_by_text_run(c, SLOGAN, [TV])) == 3, "第一段确实撞上了那三篇"


def test_the_same_text_in_a_tv_project_not_mapped_here_is_not_returned(monkeypatch):
    c = _tv_client(notes=[_note("X", project="OTHER_phase1")], comments=[_comment("X")],
                   maps=[{"tv_project_id": TV, "project_id": PROJ, "ingest_target": True},
                         {"tv_project_id": "OTHER_phase1", "project_id": OTHER_PROJ, "ingest_target": True}])
    c.rows["projects"].append({"id": OTHER_PROJ, "name": "别人的", "owner_id": "someone-else"})
    out = core.comments_for(c, user_id=ME, project_id=PROJ, post_body=BODY_A)
    assert out["source"] == "pasted" and "note_id" not in out and out["existing_comments"] == []
    assert all(("in", "project_id", (TV,)) in x["filters"] for x in _notes_calls(c)), "只查这个项目对着的 TV 项目"
    # 粗筛万一回了别的项目的行(查询写错 / 视图变了), Python 里再核一遍项目号
    monkeypatch.setattr(store, "tv_notes_by_text_run", lambda *a, **k: [_note("X", project="OTHER_phase1")])
    out = core.comments_for(c, user_id=ME, project_id=PROJ, post_body=BODY_A)
    assert out["source"] == "pasted" and "note_id" not in out


def test_an_unmapped_desk_project_does_not_query_tv():
    c = _tv_client(notes=[_note("A")], maps=[])
    out = core.comments_for(c, user_id=ME, project_id=PROJ, post_body=BODY_A)
    assert out["source"] == "pasted" and _notes_calls(c) == []


def test_a_near_hit_on_the_first_probe_does_not_hide_the_exact_post_behind_the_second():
    """codex review on aw#138: 原文第一段里多了个逗号, 第一段的 ilike 漏掉它, 却查到一篇改过几个字的另一篇(过线);
    不能就此停下 —— 第二段查得到原文(规范化后一字不差), 两段一起排, 回原文和它自己的评论。"""
    first = core._text_probe(BODY_A)[1][0]
    original = _note("A", body=BODY_A.replace(first, first[:6] + "，" + first[6:]))
    other = _note("B", body=BODY_A.replace("涨了一千出头", "涨了一千多"), when="2026-10-28T16:00:00")
    assert core._text_score(core._text_probe(BODY_A)[0], TITLE_A, other["raw_content"].split("【正文】")[1]) \
        >= core.TEXT_MATCH_SIM, "另一篇确实过线, 否则测不到"
    c = _tv_client(notes=[original, other], comments=[_comment("A"), _comment("B", "别的帖子的评论")])
    out = core.comments_for(c, user_id=ME, project_id=PROJ, post_body=BODY_A)
    assert out["source"] == "tv_by_text" and out["note_id"] == "A"
    assert [x["text"] for x in out["existing_comments"]] == ["自己录语音用的什么软件呀"]
    assert len(_notes_calls(c)) == 2


def test_an_exact_vanished_row_on_the_first_probe_does_not_hide_the_live_row_behind_the_second():
    """codex review on aw#138 第二轮: 挪表后同一条链接两行, 已删那行第一段一字不差, 活的那行第一段里多了个逗号 ——
    两行规范化后都是 1.0。第一段查到已删那行就停的话回的是旧评论; 两段都查, 活的优先。"""
    first = core._text_probe(BODY_A)[1][0]
    old = _note(f"{TV}_old", when="2026-10-25T16:00:00")
    live = {**_note(f"{TV}_new", body=BODY_A.replace(first, first[:6] + "，" + first[6:]), when="2026-10-20T16:00:00"),
            "publish_url": old["publish_url"]}
    c = _tv_client(notes=[old, live], comments=[_comment(old["note_id"], "旧行的评论"), _comment(live["note_id"])])
    c._tv.rows["v_notes_vanished"] = [{"note_id": old["note_id"], "project_id": TV}]
    out = core.comments_for(c, user_id=ME, project_id=PROJ, post_body=BODY_A)
    assert out["note_id"] == live["note_id"] and "tv_note_vanished" not in out
    assert [x["text"] for x in out["existing_comments"]] == ["自己录语音用的什么软件呀"]


def test_live_wins_over_vanished_then_the_closest_then_the_latest():
    # 挪表: 同一条链接在 TV 里两行, 旧表那行已删
    old = _note(f"{TV}_old", when="2026-10-25T16:00:00")
    live = {**_note(f"{TV}_new", when="2026-10-20T16:00:00"), "publish_url": old["publish_url"]}
    c = _tv_client(notes=[old, live], comments=[_comment(old["note_id"], "旧行的评论"), _comment(live["note_id"])])
    c._tv.rows["v_notes_vanished"] = [{"note_id": old["note_id"], "project_id": TV}]
    out = core.comments_for(c, user_id=ME, project_id=PROJ, post_body=BODY_A)
    assert out["note_id"] == live["note_id"] and "tv_note_vanished" not in out, "发布更晚的已删行让位给活的那行"
    # 同一条链接里活的那行改过几个字(没那么像)也照样是它: 活的优先只看同一篇的几行
    edited = {**live, "raw_content": live["raw_content"].replace("涨了一千出头", "涨了一千多")}
    c = _tv_client(notes=[old, edited], comments=[_comment(old["note_id"], "旧行的评论"), _comment(live["note_id"])])
    c._tv.rows["v_notes_vanished"] = [{"note_id": old["note_id"], "project_id": TV}]
    out = core.comments_for(c, user_id=ME, project_id=PROJ, post_body=BODY_A)
    assert out["note_id"] == live["note_id"] and "tv_note_vanished" not in out
    # 复审(on a3d12e6): 别的帖子(链接不同)活着、只是够像, 不压过这篇只剩已删那行的原文
    repost = _note("R", body=BODY_A.replace("涨了一千出头", "涨了一千多"), when="2026-10-28T16:00:00")
    c = _tv_client(notes=[old, repost], comments=[_comment(old["note_id"], "旧行的评论"), _comment("R", "别的帖子")])
    c._tv.rows["v_notes_vanished"] = [{"note_id": old["note_id"], "project_id": TV}]
    out = core.comments_for(c, user_id=ME, project_id=PROJ, post_body=BODY_A)
    assert out["note_id"] == old["note_id"] and out["tv_note_vanished"] is True
    assert [x["text"] for x in out["existing_comments"]] == ["旧行的评论"]
    # 两篇一样像(同一段文案发了两次)时还是活的优先, 哪怕已删那行发布得更晚
    twin = _note("T", when="2026-10-21T16:00:00")
    c = _tv_client(notes=[old, twin], comments=[])
    c._tv.rows["v_notes_vanished"] = [{"note_id": old["note_id"], "project_id": TV}]
    assert core.comments_for(c, user_id=ME, project_id=PROJ, post_body=BODY_A)["note_id"] == "T"
    # 只剩已删的那行: 照样用, 说清楚是旧记录(按正文对上的说法, 不说「链接」)
    c = _tv_client(notes=[old], comments=[_comment(old["note_id"], "旧行的评论")])
    c._tv.rows["v_notes_vanished"] = [{"note_id": old["note_id"], "project_id": TV}]
    out = core.comments_for(c, user_id=ME, project_id=PROJ, post_body=BODY_A)
    assert out["note_id"] == old["note_id"] and out["tv_note_vanished"] is True
    assert out["tv_note_vanished_note"] == core.TV_VANISHED_BY_TEXT_NOTE and "链接" not in out["tv_note_vanished_note"]
    # 同一段文案改了几个字又发一次: 贴来的是哪一版就是哪一版, 都一样时取发布最近的
    repost = _note("R", body=BODY_A.replace("涨了一千出头", "涨了一千多"), when="2026-10-28T16:00:00")
    c = _tv_client(notes=[repost, _note("A")], comments=[])
    assert core.comments_for(c, user_id=ME, project_id=PROJ, post_body=BODY_A)["note_id"] == "A"
    twin = _note("T", when="2026-10-28T16:00:00")
    c = _tv_client(notes=[_note("A"), twin], comments=[])
    assert core.comments_for(c, user_id=ME, project_id=PROJ, post_body=BODY_A)["note_id"] == "T"


@pytest.mark.parametrize("broken", ["tv_projects_of", "tv_notes_by_text_run", "tv_note_comments"])
def test_lookup_errors_fall_back_to_the_pasted_path(monkeypatch, broken):
    def boom(*_a, **_k):
        raise RuntimeError("upstream request timeout")
    monkeypatch.setattr(store, broken, boom)
    c = _tv_client(notes=[_note("A")], comments=[_comment("A")])
    out = core.comments_for(c, user_id=ME, project_id=PROJ, post_body=BODY_A)
    assert out["resolved"] is True and out["source"] == "pasted" and out["post"]["body"] == BODY_A
    assert "note_id" not in out and out["existing_comments"] == [] and "tv_by_text_note" not in out


def test_vanished_view_unreadable_keeps_the_text_match(monkeypatch):
    monkeypatch.setattr(store, "tv_vanished_note_ids", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("PGRST205")))
    c = _tv_client(notes=[_note("A")], comments=[_comment("A")])
    out = core.comments_for(c, user_id=ME, project_id=PROJ, post_body=BODY_A)
    assert out["source"] == "tv_by_text" and out["note_id"] == "A" and "tv_note_vanished" not in out


def test_a_comment_read_failure_on_the_link_path_still_raises(monkeypatch):
    monkeypatch.setattr(store, "tv_note_comments", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(RuntimeError):
        core.comments_for(_tv_client(), user_id=ME, link=LINK)


@pytest.mark.parametrize("pasted", [
    "。。。！！！……", "好看", "？？ 。。  ——", "abc def ghi",
    "我们，你们，他们，大家好，好的呀，可以的，没问题，谢谢你，再见了，明天见",   # 够长, 但没有一段连续 8 个字
    f"{TAGS} #学习打卡 #每天进步一点点 #通勤时间",                                # 全是话题
])
def test_short_or_punctuation_only_text_sends_no_query(pasted):
    c = _tv_client(notes=[_note("A")], comments=[_comment("A")])
    out = core.comments_for(c, user_id=ME, project_id=PROJ, post_body=pasted)
    assert out["source"] == "pasted" and _notes_calls(c) == []
    assert not any(x["table"] == "tv_project_map" for x in c.calls)


def test_filter_strings_carry_only_plain_runs():
    """贴来的字里有 % _ , ( ) 空格: 进过滤串的只有一段纯汉字 / 字母 / 数字。"""
    pasted = ("限时50%_off,(抢购)  a_b%c,d(e)f 真正坚持下来是因为我把单词本换成了自己录的语音，"
              "x%y_z(1,2)，每天地铁上那四十分钟我以前都在刷短视频。")
    c = _tv_client(notes=[_note("A")], comments=[])
    core.comments_for(c, user_id=ME, project_id=PROJ, post_body=pasted)
    patterns = [f[2] for x in _notes_calls(c) for f in x["filters"] if f[0] == "ilike"]
    assert patterns and all(_RUN_PATTERN.fullmatch(p) for p in patterns), patterns
    for bad in ("单词本换成了%自己录", "abc,def(ghi)", "a_b_c_d_e_f", "短的", "超" * 17, "真正坚持 下来是因为"):
        assert store.tv_notes_by_text_run(c, bad, [TV]) == []
    assert store.tv_notes_by_text_run(c, "单词本换成了自己录", []) == []
    assert len(_notes_calls(c)) == len(patterns), "不合规的段一个请求都不发"


def test_a_link_not_found_with_a_pasted_body_falls_back_to_the_text():
    c = _tv_client(notes=[_note("A")], comments=[_comment("A")])
    out = core.comments_for(c, user_id=ME, link=LINK, project_id=PROJ, post_body=BODY_A)
    assert out["source"] == "tv_by_text" and out["note_id"] == "A" and len(out["existing_comments"]) == 1


def test_the_by_text_note_does_not_send_the_model_round_in_circles():
    """复审(on a3d12e6): 链接在 TV 里没找到、改贴正文对上了别的帖子, 原来那句话叫模型「要发布链接再调一次」—— 带着同一条
    链接再调还是走贴正文这条路、对上同一篇。现在说的是用户说不是这一篇就别用这份评论, 照贴来的正文写。"""
    note = core.TV_BY_TEXT_NOTE
    assert "用户说不是这一篇" in note and "别用 existing_comments / note_id" in note and "照贴来的正文写" in note
    assert "链接再调" not in note and "给发布链接" not in note
    c = _tv_client(notes=[_note("A")], comments=[_comment("A")])
    first = core.comments_for(c, user_id=ME, link=LINK, project_id=PROJ, post_body=BODY_A)
    assert first["tv_by_text_note"] == note
    assert "不是这一篇" in tools.comments_for.__doc__ and "existing_comments" in tools.comments_for.__doc__


def test_the_link_path_does_not_look_the_text_up():
    c = _tv_client(notes=[dict(NOTE), _note("A")])
    out = core.comments_for(c, user_id=ME, link=LINK, post_body=BODY_A)
    assert out["source"] == "tv" and out["note_id"] == NOTE["note_id"] and "tv_by_text_note" not in out
    assert not any(f[1] == "raw_content" for x in _notes_calls(c) for f in x["filters"])


def test_score_comments_does_not_look_the_text_up(wired):
    calls, reply = wired
    reply["fn"] = lambda m, u, kw: _Resp(200, dict(THREAD_OK))
    c = _tv_client(notes=[_note("A")], comments=[_comment("A")])
    c._tv.rows["v_notes_vanished"] = [{"note_id": "A", "project_id": TV}]
    out = core.score_comments(c, user_id=ME, project_id=PROJ, post_title=TITLE_A, post_body=BODY_A,
                              comments=[{"slot": "q1", "text": "录语音用的什么呀"}, {"slot": "reply", "text": "手机自带的"}])
    (_m, _u, _h, body, _t), = calls
    assert body["post_title"] == TITLE_A and body["post_body"] == BODY_A
    assert out["judge_status"] == "ok" and "tv_note_vanished" not in out
    # 判定那一步本来就读 TV 项目的品类(_judge_project_context); 笔记和已删视图一次都不查
    assert not [x for x in c._tv.calls if x["table"] in ("notes", "v_notes_vanished", "comments")]


def test_the_tool_says_pasted_text_is_looked_up_too():
    doc = tools.comments_for.__doc__
    assert "tv_by_text" in doc and "pasted" in doc and "不重试" in doc and "照报错里给的下一步" in doc
