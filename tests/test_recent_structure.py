"""简报里的 recent_structure(2026-10-20): 最近打过分的稿子在哪几处都写成了同一个样子。

10-20 生产诊断: 稿子批批长得像。Jev 给每篇打分时答过 TV 的特征题库, 答案在 truth_vault.note_feature_answers
(subject_type = aw_version, subject_id = versions.id)。open_project 读回本项目最近 ≤30 篇的答案, 数一数,
占比 ≥ 0.6 的几处摆给模型, 让这一批有一半换个写法。钉住:
  · 读法: 版本联表核项目和补录(按版本时间取候选)、答案按主键排序翻页; 只取答案列, 不取 evidence / 正文; 只认 primary × fq-v0.1 × jev:*、题目版本对得上的有效答案;
    一个 item 算一篇(取最新的、有答案的那一版); 补录副本、别的项目、窗口外的版本都不算(窗口按版本时间, 不按批次);
    答案那次的版本 id 按篇摊, 改得多的一篇不挤掉别的稿子; 没判出答案的稿子不占名额(凑不够往下再问一轮, 最多三轮;
    补问那几轮失败不丢第一轮找到的)
  · 算法: 少于 12 篇不报; 某一题有效答案少于 12 篇不报; bool 题只报「是」扎堆; product_role(露出要求)不问;
    已验证规律用到的题一律不问
  · 给模型看的是中文标签不是题目 id; 哪儿都不扎堆 / 读挂了就不带这一栏, 简报照给
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from deskcore import core, store, structure, tools
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
PID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_PID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
NOW = datetime.now(timezone.utc)
NFA = "truth_vault.note_feature_answers"

# "中性"的答案: 每题都落在不扎堆的那一边(bool 题「否」、第一句四种轮着来) —— 用例只改想测的那一题
NEUTRAL = {"title_is_question": "否", "turning_point": "否",
           "negative_outcome_happened": "否", "comparison_group": "否", "calls_out_reader_group": "否"}
OPENINGS = ("观点断言", "提问", "数据事实", "身份自述")


def _neutral(i: int, **override) -> dict:
    return {**NEUTRAL, "opening_type": OPENINGS[i % len(OPENINGS)], **override}


def _ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


def _project(pid=PID):
    return {"id": pid, "name": "测试项目", "brand": "测试品", "owner_id": ME, "system_prompt": "你是文案",
            "calibration_notes": "", "tactics": "[]", "default_params": "{}"}


class _World:
    def __init__(self):
        self.batches: list[dict] = []
        self.items: list[dict] = []
        self.versions: list[dict] = []
        self.answers: list[dict] = []
        self._n = 0

    def draft(self, answers: dict | None = None, *, days: float = 1, pid=PID, source="deskcore",
              extractor="jev:1.13.0", run_tag="primary", bank="fq-v0.1", versions: dict | None = None,
              batch: str | None = None) -> str:
        """一篇稿子(默认一个批次一个 item, ``batch`` 给了就放进那个已有的批次); ``versions`` = {版本 id 后缀: 答案或
        None(这一版没判)}, 默认一版。某一版是后来改的(改稿往老 item 上加一版, 批次不变): 值写成 (答案, 几天前)。"""
        self._n += 1
        bid, iid = batch or f"b{self._n}", f"i{self._n}"
        if batch is None:
            self.batches.append({"id": bid, "project_id": pid, "params": {"source": source},
                                 "created_at": _ago(days=days)})
        versions = versions if versions is not None else {"v1": answers}
        vrows = []
        for k, (suffix, ans) in enumerate(versions.items()):
            vid = f"{iid}-{suffix}"
            ans, at = ans if isinstance(ans, tuple) else (ans, _ago(days=days, seconds=-k))
            at = _ago(days=at) if isinstance(at, (int, float)) else at
            vrows.append({"id": vid, "created_at": at, "title": "(标题不该被取)", "body": "(正文不该被取)"})
            # versions 表里的同一行: PostgREST 的 items!inner(batch_id) 内嵌在假件里就是这一小块
            parent = next(b for b in self.batches if b["id"] == bid)
            self.versions.append({**vrows[-1], "item_id": iid, "items": {"batch_id": bid, "batches": parent}})
            for qid, a in (ans or {}).items():
                self.answer(vid, qid, a, extractor=extractor, run_tag=run_tag, bank=bank)
        self.items.append({"id": iid, "batch_id": bid, "created_at": _ago(days=days), "versions": vrows})
        return iid

    def answer(self, vid, qid, ans, *, extractor="jev:1.13.0", run_tag="primary", bank="fq-v0.1",
               version=None, extracted_at=None, subject_type="aw_version"):
        self.answers.append({
            "subject_type": subject_type, "subject_id": vid, "question_id": qid,
            "question_version": version if version is not None else structure.QUESTIONS.get(qid, (1,))[0],
            "bank_version": bank, "bank_sha256": "ba0f570c", "extractor": extractor, "run_tag": run_tag,
            "answer": ans, "evidence": "(稿子原文片段, 不该被取)", "prob": 0.9, "invalid_reason": None,
            "extracted_at": extracted_at or _ago(days=0)})

    def client(self, cls=FakeClient, **kw):
        return cls(rows={
            "projects": [_project(), _project(OTHER_PID)],
            "batches": self.batches, "items": self.items, NFA: self.answers,
            "memories": [], "user_calibration_notes": [], "draft_fingerprints": [], "angle_ledger": [],
            "style_edits": [], "tv_note_links": [], "versions": self.versions,
        }, **kw)


def _read(c, **kw):
    return store.recent_structure_answers(c, PID, questions=structure.ASKED,
                                          bank_version=structure.BANK_VERSION, **kw)


def _brief(client):
    return core.build_writing_brief(client, PID, user_id=ME)


def _many(w, n, **override):
    for i in range(n):
        w.draft(_neutral(i, **override), days=1 + i / 100)


# ══════════════════════════════════════════════════════════════════════
# 读法
# ══════════════════════════════════════════════════════════════════════

def test_reads_by_version_time_and_never_touches_text():
    w = _World()
    _many(w, 3)
    c = w.client()
    got = _read(c)
    assert got == [_neutral(0), _neutral(1), _neutral(2)]
    assert [q["table"] for q in c.calls] == ["versions", "versions", NFA, NFA], "版本和答案各一页加一次空页"
    versions = c.calls[0]
    assert ("eq", "items.batches.project_id", PID) in versions["filters"]
    assert any(f[:2] == ("gte", "created_at") for f in versions["filters"])
    nfa = next(q for q in c.calls if q["table"] == NFA)
    assert "evidence" not in nfa["cols"] and "body" not in nfa["cols"], "账本只取答案列, 不取原文片段"
    assert ("eq", "subject_type", "aw_version") in nfa["filters"]
    assert ("eq", "run_tag", "primary") in nfa["filters"]
    assert ("eq", "bank_version", "fq-v0.1") in nfa["filters"]
    assert all(not (set((q["cols"] or "").replace("(", ",").replace(")", ",").replace(" ", "").split(","))
                    & {"title", "body", "content"}) for q in c.calls), "全程不取标题和正文"
    asked = next(f for f in nfa["filters"] if f[:2] == ("in", "question_id"))[2]
    assert set(asked) == set(structure.ASKED)


def test_only_valid_jev_primary_rows_of_the_pinned_version_count():
    w = _World()
    iid = w.draft({"title_is_question": "是"})
    vid = f"{iid}-v1"
    w.answer(vid, "opening_type", "具体事件", run_tag="shadow-x")           # 影子行不算
    w.answer(vid, "turning_point", "是", extractor="llm:claude-sonnet-5-5")  # 别的抽取器不算
    w.answer(vid, "comparison_group", "是", version=99)                      # 题目版本对不上不算
    w.answer(vid, "calls_out_reader_group", None)                            # 无效答案不算
    w.answer(vid, "turning_point", "不在取值里")                              # 闭集外不算
    w.answer(vid, "negative_outcome_happened", "是", bank="fq-v0.2")         # 别的题库版本不算
    w.answer(vid, "asks_for_help", "是")                                     # 规律题不问
    assert _read(w.client()) == [{"title_is_question": "是"}]


def test_latest_extraction_wins_when_jev_rejudged():
    w = _World()
    iid = w.draft({})
    w.answer(f"{iid}-v1", "opening_type", "具体事件", extracted_at=_ago(days=3))
    w.answer(f"{iid}-v1", "opening_type", "提问", extractor="jev:1.14.0", extracted_at=_ago(days=1))
    assert _read(w.client()) == [{"opening_type": "提问"}]


def test_answers_past_a_thousand_rows_are_paged_not_truncated():
    """主键是 (subject, 题, 题目版本, extractor, run_tag): Jev 换模型重判一次就多一整套行(codex review on aw#115)。80 个版本
    × 6 题 × 3 代 = 1,440 行, 过了 db-max-rows(1000); 原来不排序 .limit(1000), 最新那代答案在挑最新之前就被截掉了。"""
    w = _World()
    vids = []
    for i in range(40):
        iid = w.draft(versions={"v1": None, "v2": None}, days=1 + i / 100)
        vids += [f"{iid}-v1", f"{iid}-v2"]
    for gen, (extractor, opening) in enumerate([("jev:1.12.0", "具体事件"), ("jev:1.13.0", "具体事件"),
                                                ("jev:1.14.0", "提问")]):
        for vid in vids:
            for qid in structure.ASKED:
                w.answer(vid, qid, opening if qid == "opening_type" else "否", extractor=extractor,
                         extracted_at=_ago(days=3 - gen))
    assert len(w.answers) == 80 * len(structure.ASKED) * 3 > 1000
    c = w.client(max_rows=1000)
    got = _read(c)
    assert len(got) == structure.MAX_DRAFTS
    assert all(d == {**NEUTRAL, "opening_type": "提问"} for d in got), "每篇都该是最新那代(jev:1.14.0)的答案"
    nfa = [q for q in c.calls if q["table"] == NFA]
    assert len(nfa) == 3 and nfa[0]["orders"][:4] == ["subject_id", "question_id", "question_version", "extractor"]


def test_one_vote_per_item_newest_judged_version():
    w = _World()
    w.draft(versions={"v1": {"opening_type": "具体事件"}, "v2": {"opening_type": "提问"}}, days=1)
    w.draft(versions={"v1": {"opening_type": "数据事实"}, "v2": None}, days=2)      # 最新那版没判 → 退回上一版
    w.draft(versions={"v1": None}, days=3)                                           # 一版都没判 → 不算
    assert _read(w.client()) == [{"opening_type": "提问"}, {"opening_type": "数据事实"}]


def test_skips_ingest_copies_other_projects_and_old_batches_newest_first_capped():
    w = _World()
    w.draft({"opening_type": "身份自述"}, days=0.5, source="ingest")
    w.draft({"opening_type": "身份自述"}, days=0.5, pid=OTHER_PID)
    w.draft({"opening_type": "身份自述"}, days=store.STRUCTURE_BATCH_DAYS + 1)
    for i in range(5):
        w.draft({"opening_type": "具体事件" if i < 3 else "提问"}, days=1 + i)
    got = _read(w.client(), limit=4)
    assert got == [{"opening_type": "具体事件"}] * 3 + [{"opening_type": "提问"}]


def test_version_ids_per_request_are_bounded():
    w = _World()
    for i in range(store.STRUCTURE_ITEMS_MAX + 10):
        w.draft(versions={"v1": None, "v2": _neutral(i)}, days=1 + i / 100)
    c = w.client()
    got = _read(c)
    nfa = next(q for q in c.calls if q["table"] == NFA)
    ids = next(f for f in nfa["filters"] if f[:2] == ("in", "subject_id"))[2]
    assert len(ids) == store.STRUCTURE_VERSIONS_MAX, "一次 .in_() 的版本 id 封顶"
    assert len(got) == structure.MAX_DRAFTS, "每篇两版、只有新那版判过: 一轮 60 篇已经够了, 取最新 30 篇"


def test_revised_items_do_not_crowd_out_other_drafts():
    """版本 id 的封顶按 item 摊(codex review on aw#115): 先在整批版本上截 80 个再一篇一票的话, 改过几轮的稿子一篇占好几个
    名额 —— 30 篇各三版只剩 27 篇; 一篇改了几十轮的能把窗口压到 12 篇以下, 这一栏整个不出。"""
    w = _World()
    for i in range(30):
        w.draft(versions={f"v{k}": _neutral(i) for k in range(3)}, days=1 + i / 100)
    assert len(_read(w.client())) == 30

    w = _World()
    for i in range(13):
        w.draft(_neutral(i), days=2 + i / 100)
    w.draft(versions={f"v{k}": (_neutral(0, opening_type="具体事件"), 1 - k / 1000) for k in range(100)}, days=1.5)
    got = _read(w.client())
    assert len(got) == 14 and got[0]["opening_type"] == "具体事件"


def _rounds(c) -> list[tuple]:
    """答案那一步问了几轮: 每轮一组版本 id(同一轮翻页的几次请求 id 一样)。"""
    out: list[tuple] = []
    for q in c.calls:
        if q["table"] == NFA:
            ids = tuple(next(f for f in q["filters"] if f[:2] == ("in", "subject_id"))[2])
            if not out or out[-1] != ids:
                out.append(ids)
    return out


def test_unjudged_recent_drafts_do_not_empty_the_window():
    """没判出答案的稿子不占名额(codex review on aw#115): 原来先截最近 STRUCTURE_ITEMS_MAX 篇再问答案, judge 停过一阵、
    最近 60 篇都没答案的话这一栏就是空的 —— 窗口里明明还有 30 篇更早的、判过的。一轮凑不够就接着往下问一轮。"""
    w = _World()
    for i in range(store.STRUCTURE_ITEMS_MAX):
        w.draft(versions={"v1": None}, days=0.5 + i / 1000)          # judge 停的那几天写的: 一篇都没判
    for i in range(30):
        w.draft(_neutral(i), days=2 + i / 100)
    c = w.client()
    assert _read(c) == [_neutral(i) for i in range(30)]
    assert [len(r) for r in _rounds(c)] == [store.STRUCTURE_ITEMS_MAX, 30], "最近 60 篇一轮, 更早的 30 篇一轮"

    # 改过的稿子最新那版没判、上一版判过: 第一轮的 id 只够轮到前 20 篇的上一版, 剩下的下一轮接着问
    w = _World()
    for i in range(store.STRUCTURE_ITEMS_MAX):
        w.draft(versions={"v1": _neutral(i), "v2": None}, days=1 + i / 100)
    c = w.client()
    assert _read(c) == [_neutral(i) for i in range(30)]
    assert [len(r) for r in _rounds(c)] == [store.STRUCTURE_VERSIONS_MAX, store.STRUCTURE_ITEMS_MAX - 20]


def test_extra_rounds_are_bounded():
    """往下补问最多 STRUCTURE_ROUNDS_MAX 轮, 每轮 id 照样封顶: judge 停得再久, 开项目也只多这么几趟。"""
    w = _World()
    for i in range(store.STRUCTURE_ITEMS_MAX * store.STRUCTURE_ROUNDS_MAX):       # 一批 10 篇(批次只看最近 100 个)
        w.draft(versions={"v1": None}, days=0.5 + i / 1000, batch=w.batches[-1]["id"] if i % 10 else None)
    for i in range(30):
        w.draft(_neutral(i), days=2 + i / 100)
    c = w.client()
    assert len(w.items) == store.STRUCTURE_ITEMS_MAX * store.STRUCTURE_ROUNDS_MAX + 30 and len(w.batches) < 100
    assert _read(c) == []
    rounds = _rounds(c)
    assert len(rounds) == store.STRUCTURE_ROUNDS_MAX
    assert all(len(r) <= store.STRUCTURE_VERSIONS_MAX for r in rounds)
    assert len({v for r in rounds for v in r}) == sum(map(len, rounds)), "每一版只问一次"


class _LedgerFailsAfter(FakeClient):
    """答案那一步问到第 ``ok_rounds + 1`` 轮(换了一组版本 id)就报错, 同 PostgREST 一时 5xx。"""
    ok_rounds = 1

    def _execute(self, q):
        if q.table_name == NFA:
            ids = next(f for f in q.filters if f[:2] == ("in", "subject_id"))[2]
            seen = self.__dict__.setdefault("rounds_seen", [])
            if not seen or seen[-1] != ids:
                seen.append(ids)
            if len(seen) > self.ok_rounds:
                raise RuntimeError("503 upstream request timeout")
        return super()._execute(q)


def test_a_failed_extra_round_keeps_what_the_first_round_found(caplog):
    """往下补问的那几轮失败, 第一轮已经找到的照样交出去(codex review on aw#115): 最近 60 篇只判了 20 篇, 一轮凑不够 30 篇要往下
    补问; 补问那一趟一时报错就整个上抛的话, 简报丢掉这一栏 —— 原来只问一轮, 这 20 篇(够 MIN_DRAFTS)照样摆得出来。
    第一轮自己读不到照旧上抛(账本读不到就不带这一栏)。"""
    w = _World()
    for i in range(store.STRUCTURE_ITEMS_MAX):                       # judge 停过几阵: 最近 60 篇每三篇判了一篇
        w.draft(_neutral(i, opening_type="具体事件") if i % 3 == 0 else None, days=0.5 + i / 1000)
    for i in range(40):
        w.draft(_neutral(i, opening_type="具体事件"), days=2 + i / 100)
    first = [_neutral(i, opening_type="具体事件") for i in range(0, store.STRUCTURE_ITEMS_MAX, 3)]

    c = w.client(cls=_LedgerFailsAfter)
    with caplog.at_level("WARNING", logger="deskcore.store"):
        assert _read(c, limit=structure.MAX_DRAFTS) == first
    assert len(c.rounds_seen) == 2, "第二轮问过、报了错"
    assert any("补问" in r.getMessage() and r.exc_info for r in caplog.records), "补问失败要留一条 warning"
    shape = _brief(w.client(cls=_LedgerFailsAfter))["recent_structure"]
    assert shape["window"] == len(first) >= structure.MIN_DRAFTS

    flaky = w.client(cls=_LedgerFailsAfter)
    flaky.ok_rounds = 0
    with pytest.raises(RuntimeError):
        _read(flaky)


@pytest.mark.parametrize("old", [store.STRUCTURE_BATCH_DAYS + 10, store.STRUCTURE_BATCH_DAYS * 4 + 1, 1000])
def test_a_draft_revised_today_in_an_old_batch_counts(old):
    """窗口按版本自己的时间切, 不按批次建的时间(codex review on aw#115, 同 recent_drafted_titles 那条): 改稿是往老 item 上
    加一版, 批次还是原来那个。按批次切的话, 今天改的一篇老稿就不算了。"""
    w = _World()
    w.draft(versions={"v1": (_neutral(0, opening_type="提问"), old), "v2": (_neutral(0, opening_type="具体事件"), 0.1)},
            days=old)
    w.draft(versions={"v1": (_neutral(0, opening_type="数据事实"), old)}, days=old)      # 没改过的老稿照旧不算
    assert _read(w.client()) == [_neutral(0, opening_type="具体事件")]


def test_recent_revision_is_not_lost_behind_many_newer_batches():
    w = _World()
    w.draft(versions={"v2": (_neutral(0, opening_type="具体事件"), 0.1)}, days=1000)
    for i in range(store.RECENT_BATCHES_MAX + 1):
        w.draft(None, days=1 + i / 1000)
    assert _read(w.client()) == [_neutral(0, opening_type="具体事件")]


def test_no_batches_means_no_ledger_read():
    c = _World().client()
    assert _read(c) == []
    assert [q["table"] for q in c.calls] == ["versions"]


# ══════════════════════════════════════════════════════════════════════
# 算法
# ══════════════════════════════════════════════════════════════════════

def test_needs_twelve_drafts():
    drafts = [{**NEUTRAL, "opening_type": "具体事件"}] * (structure.MIN_DRAFTS - 1)
    assert structure.recent_structure(drafts) is None
    got = structure.recent_structure(drafts + [{**NEUTRAL, "opening_type": "具体事件"}])
    assert got["window"] == structure.MIN_DRAFTS
    assert got["concentrated"] == [{"question": "正文第一句是哪一种", "answer": "具体事件", "share": 1.0, "of": 12}]
    assert got["task"] == structure.TASK


def test_share_threshold_and_ordering():
    drafts = []
    for i in range(20):
        drafts.append({**NEUTRAL,
                       "opening_type": "具体事件" if i < 12 else ["提问", "数据事实", "身份自述", "其他"][i % 4],  # 0.60
                       "title_is_question": "是" if i < 17 else "否",                                             # 0.85
                       "turning_point": "是" if i < 11 else "否"})                                                # 0.55
    got = structure.recent_structure(drafts)
    assert got["window"] == 20
    assert got["concentrated"] == [
        {"question": "标题是不是问句", "answer": "是", "share": 0.85, "of": 20},
        {"question": "正文第一句是哪一种", "answer": "具体事件", "share": 0.6, "of": 20},
    ]


@pytest.mark.parametrize("role", ["主角", "解决方案", "顺带一提", "只暗示", "未出现"])
def test_bool_questions_only_flag_yes_and_product_role_is_never_asked(role):
    """「否」扎堆是已发笔记的常态; 产品当主角还是不出现, 哪一种扎堆都是项目的露出要求(产品向项目九成稿子产品是主角正是
    要求本身) —— 都不算趋同, 报出来就是叫一半稿子换一种露出方式, 和 p0 打架。"""
    assert structure.recent_structure([_neutral(i) for i in range(20)]) is None
    assert "product_role" not in structure.ASKED
    assert structure.recent_structure([_neutral(i, product_role=role) for i in range(20)]) is None


def test_share_counts_only_drafts_that_answered_that_question():
    drafts = [{"turning_point": "是"}] * 12 + [{"opening_type": "提问"}] * 8
    got = structure.recent_structure(drafts)
    assert got["window"] == 20
    assert got["concentrated"] == [{"question": "有没有「以前→转折→后来」的前后转折", "answer": "是", "share": 1.0,
                                    "of": 12}]
    # 那一题有效答案不够 12 篇: 不报
    assert structure.recent_structure([{"turning_point": "是"}] * 11 + [{"opening_type": "提问"}] * 9) is None


def test_each_share_carries_its_own_denominator():
    """share 的分母是这一题有答案的篇数, 不是 window(codex review on aw#115): 30 篇里只有 12 篇答了转折题、8 篇「是」,
    光给 window 30 和 0.67 读起来像是 20 篇都写了转折。每条带上 of, TASK 也得说清楚分母是它。"""
    drafts = ([{"turning_point": "是", "opening_type": "提问"}] * 8 + [{"turning_point": "否", "opening_type": "提问"}] * 4
              + [{"opening_type": ["具体事件", "身份自述", "数据事实"][i % 3]} for i in range(18)])
    got = structure.recent_structure(drafts)
    assert got["window"] == 30
    turn = [h for h in got["concentrated"] if h["question"] == "有没有「以前→转折→后来」的前后转折"]
    assert turn == [{"question": "有没有「以前→转折→后来」的前后转折", "answer": "是", "share": 0.67, "of": 12}]
    assert "of" in structure.TASK and "window" in structure.TASK and "分母" in structure.TASK
    for text in (structure.TASK, tools.open_project.__doc__ or ""):      # 给模型看的说明两处都不能再说「大多数篇」
        assert "大多数" not in text


def test_window_is_capped_at_thirty_newest():
    drafts = [{"opening_type": "提问"}] * 30 + [{"opening_type": "具体事件"}] * 30
    got = structure.recent_structure(drafts)
    assert got["window"] == structure.MAX_DRAFTS == 30
    assert got["concentrated"][0]["answer"] == "提问"


def test_model_sees_chinese_labels_not_question_ids():
    every = {qid: sorted(structure.FLAG_ANSWERS.get(qid, answers))[0]
             for qid, (_v, answers) in structure.ASKED.items()}
    got = structure.recent_structure([{**every, "opening_type": "其他"}] * 15)
    text = repr(got["concentrated"])
    assert len(got["concentrated"]) == len(structure.ASKED)
    for qid in structure.QUESTIONS:
        assert qid not in text, qid
    assert "其他(列清单、贴参数这类)" in text
    assert all(set(h) == {"question", "answer", "share", "of"} for h in got["concentrated"])


def test_validated_pattern_questions_are_never_asked():
    """第一批让模型轮换着用已验证规律, 那几处扎堆是有意的(TV prompts/desk_delivery_v1.json)。"""
    assert not (set(structure.QUESTIONS) & structure.PATTERN_QIDS)
    assert not (set(structure.ASKED) & structure.PATTERN_QIDS)
    assert {"ending_asks_reader", "asks_for_help"} <= structure.PATTERN_QIDS
    # 就算账本里有、而且扎堆, 也不进结果
    drafts = [_neutral(i, ending_asks_reader="是", asks_for_help="是") for i in range(20)]
    assert structure.recent_structure(drafts) is None


def test_task_names_the_priorities():
    for word in ("至少一半", "p0", "stable", "draw_angles", "切入角度", "改稿", "露出", "不用念给用户听"):
        assert word in structure.TASK, word


# ══════════════════════════════════════════════════════════════════════
# 简报
# ══════════════════════════════════════════════════════════════════════

def test_brief_carries_recent_structure():
    w = _World()
    _many(w, 14, opening_type="具体事件", negative_outcome_happened="是")
    w.draft(_neutral(1, negative_outcome_happened="是"), days=2)
    out = _brief(w.client())
    rs = out["recent_structure"]
    assert set(rs) == {"window", "concentrated", "task"}
    assert rs["window"] == 15
    assert rs["concentrated"] == [
        {"question": "有没有写到已经发生的坏结果", "answer": "是", "share": 1.0, "of": 15},
        {"question": "正文第一句是哪一种", "answer": "具体事件", "share": 0.93, "of": 15},
    ]


def test_brief_omits_it_when_nothing_is_concentrated_or_too_few():
    w = _World()
    _many(w, 20)
    assert "recent_structure" not in _brief(w.client())
    w2 = _World()
    _many(w2, structure.MIN_DRAFTS - 1, opening_type="具体事件")
    assert "recent_structure" not in _brief(w2.client())


@pytest.mark.parametrize("where", ["store", "ledger"])
def test_brief_survives_the_lookup_blowing_up(monkeypatch, where):
    w = _World()
    _many(w, 20, opening_type="具体事件")
    if where == "store":
        monkeypatch.setattr(store, "recent_structure_answers",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
        c = w.client()
    else:   # truth_vault 不在 Exposed schemas 里 / 列没有: PostgREST 报错
        c = w.client(missing_columns={NFA: {"extracted_at"}})
    out = _brief(c)
    assert "recent_structure" not in out and out["project_id"] == PID


def test_open_project_doc_tells_the_model_about_recent_structure():
    doc = tools.open_project.__doc__ or ""
    assert "recent_structure" in doc and "至少有一半" in doc and "优先于它" in doc


def test_hundreds_of_recent_versions_do_not_hide_older_distinct_items():
    w = _World()
    for i in range(10):
        w.draft(versions={f'v{n}': _neutral(i) for n in range(40)}, days=1 + i / 100)
    for i in range(30):
        w.draft(_neutral(i + 10), days=2 + i / 100)
    got = _read(w.client())
    assert got == [_neutral(i) for i in range(30)]
