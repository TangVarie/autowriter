"""改稿重存时交付稿是原来那一整批(2026-10-30 发布收尾)。

原来 score_drafts 带 replaces_version_id 重存时, delivery 只拼改过的那几篇, 工具说明和 next_step 叫模型「按顺序替换原来
那几篇: 表格只换那几行, 表头不要再贴一遍」—— 和同一句里的「delivery.text 原样交给用户」打架; 模型自己拼表格, 写手拿到的是
两张半截的表。现在存档时把这一批的顺序和每篇的话题 / 评论 / 方向记进 batch 的 params(不加迁移), 改稿重存时服务端把原来那一
整批拼回来。这里钉的是:

  · 没改的几篇照它们现在的样子、改过的换在原位, 顺序不变, 一张表一个表头; 上一轮改过的那篇用它改过的样子;
  · 改的稿子分属几批的, 一批一段按建的先后排, 同一次另有新稿的排在最后; problems 只报这次传的那几篇、下标照写手传的;
  · 拼不回来(这之前存的 batch、批里的稿子对不上)就照旧只拼改过的几篇, delivery.scope / scope_fallback 说清, next_step 多一句;
  · 没模板的项目 batch 上一样不记、回包一个字不变; next_step 只说「原样整段交」, 不再叫模型替换其中几行。

所有示例文本都是合成的。
"""

from __future__ import annotations

import pytest

from deskcore import core, store
from tests.test_delivery import BLOCKS, ME, PROJ, TABLE, _params_with, desk_client, distinct_drafts
from tests.test_writer_facing_text import _assert_writer_safe


def _embed(c):
    """假库没有真 join(同 test_deskcore_draft_identity): items_for_versions 走 ``items!inner → batches!inner``,
    每次改稿前把当前的 item / batch 嵌进 versions。"""
    items = {i["id"]: i for i in c.rows.get("items", [])}
    batches = {b["id"]: b for b in c.rows.get("batches", [])}
    for v in c.rows.get("versions", []):
        it = items.get(v.get("item_id"))
        if it:
            v["items"] = {"id": it["id"], "status": it.get("status"), "decision_source": it.get("decision_source"),
                          "batch_id": it.get("batch_id"),
                          "batches": {"project_id": (batches.get(it.get("batch_id")) or {}).get("project_id")}}
    return c


def _full(drafts: list[dict], tag: str = "") -> list[dict]:
    """每篇都带齐 TABLE 模板要的: 两个话题、一条提到「示例品」的评论、方向 —— 拼出来没有 problems。"""
    for k, d in enumerate(drafts):
        d.update(tags=[f"话题{tag}{k}甲", f"话题{tag}{k}乙"],
                 comments=[{"role": "reader", "text": f"示例品{tag}第{k}条"}], direction=f"方向{tag}{k}")
    return drafts


def _rows(text: str) -> list[str]:
    return text.split("\n")


def _revise(d: dict, vid: str, note: str) -> dict:
    return {**d, "body": d["body"] + f"后来补了一句{note}。", "direction": f"改过的方向{note}", "replaces_version_id": vid}


@pytest.fixture
def table_batch():
    c = desk_client(default_params=_params_with(TABLE))
    drafts = _full(distinct_drafts(3, seed=5))
    first = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert first["written"] == 3 and first["delivery"]["problems"] == []
    return c, drafts, first


# ══════════════════════════════════════════════════════════════════════
# 1 · 改一篇: 拼回原来那一整批
# ══════════════════════════════════════════════════════════════════════

def test_revising_one_draft_delivers_the_whole_original_batch_in_place(table_batch):
    c, drafts, first = table_batch
    before = _rows(first["delivery"]["text"])
    assert "scope" not in first["delivery"], "存新稿的回包一个字不变"
    _embed(c)
    out = core.commit_drafts(c, PROJ, [_revise(drafts[1], first["version_ids"][1], "一")], user_id=ME)
    assert out["replaced"] and out["written"] == 1
    d = out["delivery"]
    assert (d["scope"], d["batches"], d["untouched"]) == ("whole_batch", 1, 2)
    after = _rows(d["text"])
    assert len(after) == len(before) == 5, "一个表头 + 分隔行 + 三行, 没有第二个表头"
    assert after[:2] == before[:2]
    assert after[2] == before[2] and after[4] == before[4], "没改的两篇原样、在原来的位置"
    assert after[3] != before[3] and "后来补了一句一" in after[3] and "改过的方向一" in after[3]
    assert d["problems"] == []
    meta = c.rows["batch_metrics"][-1]["meta"]["delivery"]
    assert meta == {"layout": "table", "drafts": 3, "problems": 0, "scope": "whole_batch", "batches": 1, "untouched": 2}


def test_next_step_says_deliver_the_text_whole_and_never_to_splice_rows(table_batch):
    c, drafts, first = table_batch
    _embed(c)
    out = core.commit_drafts(c, PROJ, [_revise(drafts[0], first["version_ids"][0], "零")], user_id=ME)
    for step in (first["next_step"], out["next_step"]):
        assert "delivery.text 原样整段交给用户" in step and "不要自己拼接、替换其中几行" in step
        assert "只换那几行" not in step and "按顺序替换原来那几篇" not in step
    assert core.DELIVERY_PARTIAL_NEXT_STEP not in out["next_step"]
    _assert_writer_safe(out, "改稿重存")


def test_a_second_revision_keeps_the_first_revision_as_revised(table_batch):
    """上一轮改过第 0 篇、这一轮改第 2 篇: 第 0 篇要是改过的样子(连同它改过的方向), 不是最初那一版。"""
    c, drafts, first = table_batch
    _embed(c)
    one = core.commit_drafts(c, PROJ, [_revise(drafts[0], first["version_ids"][0], "零")], user_id=ME)
    _embed(c)
    two = core.commit_drafts(c, PROJ, [_revise(drafts[2], first["version_ids"][2], "二")], user_id=ME)
    rows_one, rows_two = _rows(one["delivery"]["text"]), _rows(two["delivery"]["text"])
    assert rows_two[2] == rows_one[2] and "改过的方向零" in rows_two[2], "上一轮改过的那篇保持改过的样子"
    assert rows_two[3] == _rows(first["delivery"]["text"])[3]
    assert "改过的方向二" in rows_two[4]
    assert two["delivery"]["untouched"] == 2


def test_problems_are_only_for_the_drafts_passed_this_time_at_their_caller_index(table_batch):
    c, drafts, first = table_batch
    _embed(c)
    bad = {**_revise(drafts[2], first["version_ids"][2], "二"), "tags": ["只有一个"]}
    out = core.commit_drafts(c, PROJ, [bad], user_id=ME)
    assert [p["index"] for p in out["delivery"]["problems"]] == [0], out["delivery"]["problems"]


# ══════════════════════════════════════════════════════════════════════
# 2 · 改的稿子分属几批 / 同一次另有新稿
# ══════════════════════════════════════════════════════════════════════

def test_revisions_across_batches_render_each_batch_in_order_and_new_drafts_last():
    c = desk_client(default_params=_params_with(TABLE))
    a = _full(distinct_drafts(2, seed=11), "甲")
    b = _full(distinct_drafts(2, seed=12), "乙")
    out_a = core.commit_drafts(c, PROJ, a, user_id=ME)
    out_b = core.commit_drafts(c, PROJ, b, user_id=ME)
    c.rows["batches"][0]["created_at"] = "2026-10-29T08:00:00+00:00"
    c.rows["batches"][1]["created_at"] = "2026-10-30T08:00:00+00:00"
    _embed(c)
    fresh = _full(distinct_drafts(1, seed=13), "丙")[0]
    # 写手传的顺序: 后一批的那篇在前、新稿夹在中间、前一批的那篇在后
    out = core.commit_drafts(c, PROJ, [_revise(b[1], out_b["version_ids"][1], "乙"), fresh,
                                       _revise(a[0], out_a["version_ids"][0], "甲")], user_id=ME)
    d = out["delivery"]
    assert (d["scope"], d["batches"], d["untouched"]) == ("whole_batch", 2, 2)
    tables = d["text"].split("\n\n")
    assert len(tables) == 3, "每一批一张表, 新稿一张"
    t_a, t_b, t_new = (_rows(t) for t in tables)
    assert "改过的方向甲" in t_a[2] and t_a[3] == _rows(out_a["delivery"]["text"])[3], "先建的那一批在前"
    assert t_b[2] == _rows(out_b["delivery"]["text"])[2] and "改过的方向乙" in t_b[3]
    assert len(t_new) == 3 and "方向丙0" in t_new[2], "新稿拼在最后"
    assert d["problems"] == []


# ══════════════════════════════════════════════════════════════════════
# 3 · 拼不回来: 照旧只拼改过的几篇, 说清楚
# ══════════════════════════════════════════════════════════════════════

def test_a_batch_stored_before_the_record_falls_back_to_the_revised_drafts_and_says_so(table_batch):
    c, drafts, first = table_batch
    c.rows["batches"][0]["params"].pop(core.DELIVERY_PARTS_KEY)          # 这之前存的 batch: 没记顺序和话题评论
    _embed(c)
    out = core.commit_drafts(c, PROJ, [_revise(drafts[1], first["version_ids"][1], "一")], user_id=ME)
    d = out["delivery"]
    assert (d["scope"], d["scope_fallback"]) == ("this_call", "batch_not_recorded")
    assert len(_rows(d["text"])) == 3 and "后来补了一句一" in d["text"], "只有改过的那一篇"
    assert core.DELIVERY_PARTIAL_NEXT_STEP in out["next_step"] and "原样" in core.DELIVERY_PARTIAL_NEXT_STEP
    assert c.rows["batch_metrics"][-1]["meta"]["delivery"]["scope_fallback"] == "batch_not_recorded"
    _assert_writer_safe(out, "拼不回整批")


def test_a_batch_whose_drafts_no_longer_match_the_record_falls_back(table_batch):
    c, drafts, first = table_batch
    bid = c.rows["batches"][0]["id"]
    c.rows["items"].append({"id": "item-extra", "batch_id": bid, "status": "pending", "best_version_id": "v-extra"})
    c.rows["versions"].append({"id": "v-extra", "item_id": "item-extra", "version_num": 1, "title": "多出来的", "body": "正文"})
    _embed(c)
    out = core.commit_drafts(c, PROJ, [_revise(drafts[1], first["version_ids"][1], "一")], user_id=ME)
    assert (out["delivery"]["scope"], out["delivery"]["scope_fallback"]) == ("this_call", "batch_changed")


def test_a_failed_lookup_falls_back_without_touching_the_archive(table_batch, monkeypatch):
    c, drafts, first = table_batch
    _embed(c)
    monkeypatch.setattr(store, "batch_drafts", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("库一时读不出来")))
    out = core.commit_drafts(c, PROJ, [_revise(drafts[1], first["version_ids"][1], "一")], user_id=ME)
    assert out["written"] == 1 and out["replaced"]
    assert (out["delivery"]["scope"], out["delivery"]["scope_fallback"]) == ("this_call", "lookup_failed")


# ══════════════════════════════════════════════════════════════════════
# 4 · 记下的那一份
# ══════════════════════════════════════════════════════════════════════

def test_the_batch_records_order_and_parts_only_when_the_project_has_a_template():
    c = desk_client(default_params=_params_with(BLOCKS))
    drafts = distinct_drafts(2, seed=21)
    drafts[0]["tags"] = []                                  # 传了空列表 ≠ 没传: 原样记
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    rec = c.rows["batches"][0]["params"][core.DELIVERY_PARTS_KEY]
    assert rec["order"] == out["version_ids"]
    assert rec["parts"] == {out["version_ids"][0]: {"tags": []}, out["version_ids"][1]: {}}

    plain = desk_client()
    core.commit_drafts(plain, PROJ, distinct_drafts(2, seed=21), user_id=ME)
    assert core.DELIVERY_PARTS_KEY not in plain.rows["batches"][0]["params"], "没模板就不记"


def test_a_revision_adds_its_parts_to_the_original_batch_record(table_batch):
    c, drafts, first = table_batch
    _embed(c)
    out = core.commit_drafts(c, PROJ, [_revise(drafts[1], first["version_ids"][1], "一")], user_id=ME)
    assert len(c.rows["batches"]) == 1, "改稿不建新 batch"
    rec = c.rows["batches"][0]["params"][core.DELIVERY_PARTS_KEY]
    assert rec["order"] == first["version_ids"], "顺序不动"
    assert rec["parts"][out["version_ids"][0]]["direction"] == "改过的方向一"
