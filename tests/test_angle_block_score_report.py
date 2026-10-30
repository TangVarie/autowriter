"""发牌块带 angle_key / 参考分只说要改的 / 撞车稿写手坚持要用时怎么说(2026-10-30 发布收尾)。

写手觉得写作台"像没做完", 这三处是交付那几轮里最常听到的:

  1. ``draw_angles`` 的 ``prompt_block`` 是模型贴进提示词、照着写的东西, 可块里没有 angle_key —— 查重 / 存档时模型
     自己编一个(10-26 sportsix 带上来的 key 长 7 / 8 / 53 位, 一个都销不了账)。现在每篇那行带上完整的 key,
     ``angles[].angle_key`` 照旧;
  2. 参考分: 10 月存档的 234 篇里 231 篇是 100 分, 「把每篇的分数告诉用户」写手听到的几乎全是一串 100。现在
     没有 fixes / hard_fails 的整批一句「合规检查都过了」, 只点出要改的那几篇(哪一句、犯了哪条), 不报分数;
     judge.results 里的 score / fixes 一个不少;
  3. 存档时被判撞车(pre_commit / atomic_recheck)的那几条, note 原来说「不能当作已交付」—— 协议却允许写手看过撞的
     是哪一条之后坚持要用。现在: 拒绝本身不变(没存档、没销牌), 写手坚持就原样交, 用一句话说清这篇不进写作台的
     历史稿、牌还挂着。

这里钉的是行为和说给模型的那几句话; 说给写手的话照 test_writer_facing_text 的规矩, 不带工程指令。
"""

from __future__ import annotations

import re

import pytest

import judge_client
from deskcore import core, fingerprint as fp, tools as T
from tests.fakes import FakeClient
from tests.test_deskcore_commit_judge import BODY_A, BODY_B, BODY_C, ME, PROJ, _client, judge_on  # noqa: F401 — fixture
from tests.test_desk_variety import PID as DRAW_PID, _draw_client
from tests.test_desk_variety import ME as DRAW_ME
from tests.test_writer_facing_text import FORBIDDEN


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)


def _writer_safe(text: str) -> None:
    bad = [f for f in FORBIDDEN if f in text]
    assert not bad, f"念给写手的话里有工程指令: {bad}: {text}"


# ══════════════════════════════════════════════════════════════════════
# 1 · prompt_block 每篇那行带完整的 angle_key
# ══════════════════════════════════════════════════════════════════════

_SLOT_LINE = re.compile(r"^第(\d+)篇 \[angle_key=([0-9a-f]+)\]：", re.M)


@pytest.mark.parametrize("rpc", [True, False], ids=["rpc", "fallback"])
def test_every_slot_line_of_the_block_carries_its_full_angle_key(rpc):
    out = core.draw_angles(_draw_client(rpc=rpc), DRAW_PID, 5, user_id=DRAW_ME, seed=7)
    assert out["delivered"] == 5
    found = {int(slot): key for slot, key in _SLOT_LINE.findall(out["prompt_block"])}
    assert found == {a["slot"]: a["angle_key"] for a in out["angles"]}, \
        "块里的 key 要和 angles[].angle_key 一一对上(按第几篇), 一个不少"
    assert all(len(k) == fp.ANGLE_KEY_HEX_LEN and fp.looks_like_angle_key(k) for k in found.values()), \
        "印的是完整的 key, 不截短 —— 截短了照样销不了账"
    # angles 里的 key 照旧(机器字段不动)
    assert all("angle_key" in a for a in out["angles"])


def test_the_block_says_to_copy_the_key_into_check_and_score_and_keep_it_out_of_the_text():
    out = core.draw_angles(_draw_client(rpc=True), DRAW_PID, 3, user_id=DRAW_ME, seed=1)
    tail = out["prompt_block"].splitlines()[-1]
    assert "check_drafts" in tail and "score_drafts" in tail and "原样复制" in tail
    assert "不写进标题和正文" in tail, "key 是记账用的, 别让模型把它写进稿子"


def test_a_key_copied_from_the_block_is_one_the_ledger_knows(monkeypatch):
    """模型照块里抄的 key 去存档, 台账认得: 发牌时记进台账的就是这几个 key(降级路径直接写 angle_ledger)。"""
    c = _draw_client(rpc=False)
    out = core.draw_angles(c, DRAW_PID, 3, user_id=DRAW_ME, seed=11)
    copied = [key for _, key in _SLOT_LINE.findall(out["prompt_block"])]
    ledger = {r["angle_key"] for r in c.rows["angle_ledger"]}
    assert copied and set(copied) <= ledger


def test_render_angles_block_is_empty_for_no_angles():
    assert core.render_angles_block([]) == ""


def test_draw_angles_doc_tells_the_model_to_copy_the_key_from_the_block():
    doc = T.draw_angles.__doc__ or ""
    assert "[angle_key=" in doc and "angles[].angle_key" in doc
    assert "check_drafts" in doc and "score_drafts" in doc and "原样复制" in doc
    _writer_safe(doc)


# ══════════════════════════════════════════════════════════════════════
# 2 · 参考分: 只说要改的那几篇, 其余一句「合规检查都过了」
# ══════════════════════════════════════════════════════════════════════

def _row(index, **res):
    return core._judge_row({"index": index, "version_id": f"v{index}"},
                           judge_client.result(judge_client.JUDGE_OK, **res))


def _fix(instruction="「功效承诺」现在判「是」。依据句：吃了就好", p=0.9):
    return {"hard_fails": [["platform_health_v0.1", "efficacy_claim", "是", p]],
            "plan": [{"kind": "hard", "qid": "efficacy_claim", "p": p, "instruction": instruction}]}


def test_a_clean_batch_is_one_line_without_numbers():
    step = core._score_report_step(core._judge_block([_row(0, passed=True), _row(1, passed=True)], []))
    assert "这 2 篇合规检查都过了" in step and "「合规检查都过了」" in step
    assert "不用报分数" in step and "index 是" not in step
    assert "score" not in step, "全过的一批不要叫模型去念分数"


def test_only_the_drafts_with_fixes_are_named_and_the_rest_is_one_line():
    block = core._judge_block([_row(0, passed=True), _row(1, **_fix()), _row(2, passed=True),
                               _row(3, **_fix(p=0.55))], [])
    step = core._score_report_step(block)
    assert "index 是 1、3 的 2 篇有要改的地方" in step
    assert "只把这几篇告诉用户" in step and "哪一句" in step and "犯了哪条" in step
    assert "另外 2 篇合规检查都过了" in step
    assert "不用报分数" in step
    # 数据一个不少: 每篇的分数、修改建议都还在
    assert [r["score"] for r in block["results"]] == [100, 75, 100, 92]
    assert block["results"][1]["fixes"][0]["instruction"].startswith("「功效承诺」")


def test_hard_fails_without_a_plan_still_count_as_something_to_change():
    """修改单没回来(plan 空)也算要改: 依据句在修改单里, 这时说 hard_fails 里是哪一条没过。"""
    row = _row(0, hard_fails=[["platform_health_v0.1", "fear_sell", "是", 0.9]])
    assert row["fixes"] == [] and row["hard_fails"]
    step = core._score_report_step(core._judge_block([row], []))
    assert "index 是 0 的 1 篇" in step and "hard_fails 里是哪一条没过" in step
    assert "合规检查都过了" not in step


def test_fixes_cut_off_by_the_cap_still_count():
    row = _row(0, plan_size=2)          # 修改单两条, 都没带 instruction(截掉了): fixes 空、more_fixes 还在
    assert row.get("more_fixes") == 2
    assert "index 是 0 的 1 篇" in core._score_report_step(core._judge_block([row], []))


def test_only_unjudged_constraints_is_not_something_to_change():
    """只有"没判出来"的硬约束: 没有一处可改, 归进整批那一句; 分数照扣、unjudged 照报。"""
    row = _row(0, unjudged=2)
    assert row["score"] == 80 and row["unjudged"] == 2
    step = core._score_report_step(core._judge_block([row], []))
    assert "这 1 篇合规检查都过了" in step and "index 是" not in step


@pytest.mark.parametrize("status", [judge_client.JUDGE_TIMEOUT, judge_client.JUDGE_NOT_CONFIGURED,
                                    judge_client.JUDGE_POLICY_BLOCKED, judge_client.JUDGE_ERROR])
def test_drafts_without_a_score_are_one_line_and_never_called_compliant(status):
    rows = [core._judge_row({"index": i, "version_id": f"v{i}"}, judge_client.result(status)) for i in range(2)]
    step = core._score_report_step(core._judge_block(rows, []))
    assert step == "2 篇这次没打成参考分(不影响存档和交付): 跟用户提一句就行, 不用重试。", \
        "不看是哪种失败: 没接判定、超时、被挡下, 回包除了 judge 块之外一个字都不变"
    assert "合规检查都过了" not in step


def test_too_short_skipped_and_a_whole_judge_failure_are_not_called_compliant():
    short = _row(0, invalid_reason="text_too_short")
    assert short["score"] is None
    step = core._score_report_step(core._judge_block([short, _row(1, passed=True)],
                                                     [{"index": 2, "reason": "identity_error"}]))
    assert "这 1 篇合规检查都过了" in step and "2 篇这次没打成参考分" in step
    whole = core._score_report_step(core._judge_block([], [], error="判定这一步整体失败"))
    assert whole.startswith("这批这次没打成参考分") and "合规检查都过了" not in whole
    assert core._score_report_step(None).startswith("这批这次没打成参考分")


def test_score_drafts_names_only_the_draft_to_fix_and_keeps_every_score(judge_on):
    def reply(p):
        if p["title"] == "乙":
            return judge_client.result(judge_client.JUDGE_OK, passed=False, **_fix("「疗效」那句: 吃了就好"))
        return judge_client.result(judge_client.JUDGE_OK, passed=True)
    judge_on["reply"] = reply
    out = core.commit_drafts(_client(), PROJ, [{"title": "甲", "body": BODY_A}, {"title": "乙", "body": BODY_B},
                                               {"title": "丙", "body": BODY_C}], user_id=ME)
    assert out["written"] == 3
    step = out["next_step"]
    assert step.startswith(core.ARCHIVED_STEP_HEAD + "参考分(不拦交付): judge.results 里 index 是 1 的 1 篇"), step
    assert "另外 2 篇合规检查都过了" in step
    assert "每篇的 score" not in step and "照实告诉用户" not in step.split("check_rules")[0]
    # 后面那几句照旧: check_rules、改稿怎么改、贴飞书
    assert "check_rules" in step and "replaces_version_id" in step and "飞书" in step
    by_index = {r["index"]: r for r in out["judge"]["results"]}
    assert [by_index[i]["score"] for i in range(3)] == [100, 75, 100], "分数照旧都在数据里"
    assert by_index[1]["fixes"][0]["instruction"] == "「疗效」那句: 吃了就好"
    assert out["judge"]["note"] == core.SCORE_NOTE
    _writer_safe(step)


def test_score_drafts_on_a_clean_batch_says_one_line(judge_on):
    out = core.commit_drafts(_client(), PROJ, [{"title": "甲", "body": BODY_A}, {"title": "乙", "body": BODY_B}],
                             user_id=ME)
    assert out["next_step"].startswith(core.ARCHIVED_STEP_HEAD + "这 2 篇合规检查都过了"), out["next_step"]
    assert all(r["score"] == 100 for r in out["judge"]["results"])


def test_score_drafts_without_a_judge_says_no_score_and_nothing_else(monkeypatch):
    import config
    monkeypatch.setattr(config, "JUDGE_URL", "")
    out = core.commit_drafts(_client(), PROJ, [{"title": "甲", "body": BODY_A}], user_id=ME)
    step = out["next_step"]
    assert step.startswith(core.ARCHIVED_STEP_HEAD + "1 篇这次没打成参考分"), step
    assert "合规检查都过了" not in step
    _writer_safe(step)


def test_a_failing_report_line_never_breaks_the_archive(judge_on, monkeypatch):
    def boom(_judge):
        raise RuntimeError("写不出那一句")
    monkeypatch.setattr(core, "_score_report_step", boom)
    out = core.commit_drafts(_client(), PROJ, [{"title": "甲", "body": BODY_A}], user_id=ME)
    assert out["written"] == 1 and out["next_step"].startswith(core.ARCHIVED_STEP_HEAD + "然后调一次 check_rules")


def test_the_score_note_no_longer_asks_for_every_number():
    note = core.SCORE_NOTE
    assert "把每篇的分数" not in note and "不用逐篇报给用户" in note
    assert "「合规检查都过了」" in note and "fixes" in note and "hard_fails" in note
    assert "不拦交付" in note and "replaces_version_id" in note
    _writer_safe(note)


def test_score_drafts_doc_matches():
    doc = T.score_drafts.__doc__ or ""
    assert "把每篇的分数和 fixes" not in doc
    assert "分数不用逐篇报给用户" in doc and "「合规检查都过了」" in doc
    assert "next_step" in doc and "index" in doc


# ══════════════════════════════════════════════════════════════════════
# 3 · 撞车被拒: 拒绝不变, 写手坚持就原样交、一句话说清后果
# ══════════════════════════════════════════════════════════════════════

_INSIST = ("坚持要用", "原样交给他", "不会存进写作台的历史稿", "牌也还挂着没销账")


def _commit_client():
    c = FakeClient(rows={"projects": [{"id": PROJ, "name": "项目", "brand": "A", "owner_id": ME,
                                       "calibration_notes": "", "tactics": "[]", "custom_roles": []}]})
    c.rpc_impl["deskcore_commit_fingerprints"] = lambda args: [
        {"idx": i, "status": core.COMMIT_STATUS_INSERTED, "collided_with": None, "detail": None}
        for i in range(len(args.get("_rows") or []))]
    return c


def test_a_pre_commit_reject_says_what_happens_if_the_writer_insists(monkeypatch):
    import config
    monkeypatch.setattr(config, "JUDGE_URL", "")
    c = _client()
    core.commit_drafts(c, PROJ, [{"title": "上周发过的", "body": BODY_C}], user_id=ME)
    out = core.commit_drafts(c, PROJ, [{"title": "撞历史的", "body": BODY_C, "angle_key": "k" * 20},
                                       {"title": "甲", "body": BODY_A}], user_id=ME)
    # 拒绝本身不变: 那一篇没存档、没销牌
    assert [(r["index"], r["gate"]) for r in out["rejected"]] == [(0, "pre_commit")]
    assert out["written"] == 1 and out["consumed_angles"] == 0
    note = out["note"]
    for must in _INSIST:
        assert must in note, (must, note)
    assert "不能当作已交付" not in note
    assert "check_drafts" in note, "默认还是重写后重新查重"
    _writer_safe(note)


def test_a_race_reject_gets_the_same_sentence():
    c = _commit_client()
    c.rpc_impl["deskcore_commit_fingerprints"] = lambda args: [
        {"idx": i, "status": "rejected", "collided_with": "队友刚存的", "detail": "撞车"}
        for i in range(len(args.get("_rows") or []))]
    out = core.commit_drafts(c, PROJ, [{"title": "一条新稿子的标题", "body": "正文" * 40}], user_id=ME)
    assert out["written"] == 0 and [r["gate"] for r in out["rejected"]] == ["atomic_recheck"]
    assert all(must in out["note"] for must in _INSIST) and "不能当作已交付" not in out["note"]


def test_a_wrong_replaces_version_id_is_not_told_to_deliver_as_is():
    """id 给错不是撞车: 稿子没问题、改对 id 再存, 不说"坚持要用就原样交"那一套。"""
    out = core.commit_drafts(_commit_client(), PROJ, [{"title": "改稿", "body": "正文" * 40,
                                                       "replaces_version_id": "not-a-uuid"}], user_id=ME)
    assert [r["gate"] for r in out["rejected"]] == ["replace_lookup"]
    assert "改对 id 再提交一次" in out["note"] and "坚持要用" not in out["note"]


def test_check_drafts_and_score_drafts_docs_allow_the_writer_to_insist():
    for fn in (T.check_drafts, T.score_drafts):
        doc = fn.__doc__ or ""
        assert "不能当作已交付" not in doc, fn.__name__
        for must in _INSIST:
            assert must in doc, (fn.__name__, must)
    assert "不用再传给 score_drafts" in (T.check_drafts.__doc__ or "")
