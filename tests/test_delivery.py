"""交付版式由代码拼(2026-10-20)。

10-20 生产诊断: 版式交给写手那边的模型照规则排, 时灵时不灵; 老的独立写作台在代码里拼, 从没出过这个问题。现在模板存在
``projects.default_params.delivery``, score_drafts 存完档按模板拼整批交付稿。这里钉的是:

  · 拼出来的样子(表格 / 一篇一段)、话题的顺序与去重、条件话题;
  · 只报代码判得准的问题, 下标是写手传的位置;
  · 模板校验是严的(不认识的键、冒号标记都拒), CLI 写之前先校验、写回不丢别的键、仍存成 JSON 字符串;
  · 打分路径: 有模板才带 delivery, 没模板一个字不变, 模板坏了不拦存档, 发给 judge 的东西不变。

所有示例文本都是合成的。
"""

from __future__ import annotations

import json
import random
import threading

import pytest

import config
import judge_client
from deskcore import cli, core, delivery
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
PROJ = "aaaaaaaa-0000-0000-0000-0000000000d1"

TABLE = {"layout": "table",
         "columns": [{"name": "方向", "field": "direction"}, {"name": "笔记", "field": "post"},
                     {"name": "评论", "field": "comments"}],
         "tags": {"content": 2, "fixed": ["#固定甲"], "conditional": [{"when_any": ["Alpha"], "add": "条件乙"}]},
         "comments": {"min": 1, "max": 2, "label_reader": "读者", "label_author": "作者",
                      "must_mention_any": ["示例品"]}}
BLOCKS = {"layout": "blocks", "tags": {"content": 1}}


# ══════════════════════════════════════════════════════════════════════
# 拼
# ══════════════════════════════════════════════════════════════════════

def test_table_is_a_markdown_table_with_one_row_per_draft():
    out = delivery.assemble(TABLE, [
        {"title": "标题一", "body": "第一段 a|b\n\n第二段", "tags": ["话题一", "话题二"], "direction": "方向一",
         "comments": [{"role": "reader", "text": "示例品怎么样"}, {"role": "author", "text": "示例品还行"}]},
        {"title": "标题二", "body": "只有一段", "tags": ["话题三", "话题四"], "direction": "方向二",
         "comments": [{"role": "reader", "text": "示例品贵吗"}]},
    ])
    lines = out["text"].split("\n")
    assert out["format"] == "table" and out["problems"] == []
    assert lines[0] == "| 方向 | 笔记 | 评论 |" and lines[1] == "| --- | --- | --- |"
    assert len(lines) == 4, "一篇一行, 正文里的换行要变成 <br>, 不能把表格撑破"
    assert lines[2] == ("| 方向一 | 【标题】标题一<br>【正文】第一段 a\\|b<br><br>第二段<br>#话题一 #话题二 #固定甲 | "
                        "读者示例品怎么样<br>作者示例品还行 |")
    assert lines[3].startswith("| 方向二 | 【标题】标题二<br>【正文】只有一段<br>#话题三 #话题四 #固定甲 |")


def test_blocks_are_separated_by_a_blank_line_and_comments_get_their_labels():
    tpl = {"layout": "blocks", "tags": {"content": 1},
           "comments": {"min": 0, "max": 3, "label_reader": "【评论】", "label_author": "作者回复："}}
    out = delivery.assemble(tpl, [
        {"title": "甲", "body": "正文甲", "tags": ["话题"],
         "comments": [{"role": "reader", "text": "问一句"}, {"role": "author", "text": "答一句"}]},
        {"title": "乙", "body": "正文乙", "tags": "#话题"},
    ])
    assert out["text"] == ("【标题】甲\n【正文】正文甲\n#话题\n【评论】问一句\n作者回复：答一句"
                           "\n\n"
                           "【标题】乙\n【正文】正文乙\n#话题")
    assert out["problems"] == []


def test_tags_are_normalised_deduped_and_ordered_content_fixed_conditional():
    tpl = {"layout": "blocks",
           "tags": {"content": 3, "fixed": ["固定甲", "#固定乙"],
                    "conditional": [{"when_any": ["alpha", "贝塔"], "add": "#条件丙"},
                                    {"when_any": ["不会出现"], "add": "条件丁"}]}}
    out = delivery.assemble(tpl, [{"title": "说说 ALPHA", "body": "正文",
                                   "tags": ["#话题A", "话题a", "#话题B[话题]#", "#固定甲", "#话题C #话题D"]}])
    tag_line = out["text"].split("\n")[-1]
    # 写手的去重(不分大小写)、和固定话题重名的不占名额、截到 3 个; 然后固定的, 然后命中的条件话题
    assert tag_line == "#话题A #话题B #话题C #固定甲 #固定乙 #条件丙"
    assert out["problems"] == []


def test_a_trailing_tag_line_in_the_body_is_lifted_not_duplicated():
    """10-20 查生产库: 约六分之一的稿子末尾带一行 #话题。模板也拼一行的话, 交付稿里就是两行。"""
    out = delivery.assemble(BLOCKS, [{"title": "甲", "body": "正文第一句。\n\n#话题甲 #话题乙\n"}])
    assert out["text"] == "【标题】甲\n【正文】正文第一句。\n#话题甲"
    given = delivery.assemble(BLOCKS, [{"title": "甲", "body": "正文第一句。\n#旧话题", "tags": ["新话题"]}])
    assert given["text"] == "【标题】甲\n【正文】正文第一句。\n#新话题", "单独传了 tags 就以它为准"


def test_an_explicitly_empty_tags_list_is_taken_at_its_word():
    """传了 ``tags: []`` 就是"这篇不带写手的话题", 不能再退回正文末尾摘下来的那几个(codex review on aw#115): 写手把话题从
    tags 里删光了, 交付稿里却还是那几个。没传(或传了个空字符串, 模型常这么填缺省)才用摘下来的。末尾那行照摘, 不留在正文里。"""
    body = "正文第一句。\n#旧话题"
    empty = delivery.assemble(BLOCKS, [{"title": "甲", "body": body, "tags": []}])
    assert empty["text"] == "【标题】甲\n【正文】正文第一句。"
    assert empty["problems"] == [{"index": 0, "what": "话题标签 0 个, 这个项目要 1 个; 在 tags 里补齐"}]
    for missing in ({}, {"tags": None}, {"tags": ""}):
        out = delivery.assemble(BLOCKS, [{"title": "甲", "body": body, **missing}])
        assert out["text"] == "【标题】甲\n【正文】正文第一句。\n#旧话题", missing
    assert delivery.delivered_post(BLOCKS, {"title": "甲", "body": body, "tags": []}) == ("正文第一句。", [])


def test_tags_trailing_the_last_sentence_are_lifted_too():
    """「冲鸭！#秋招 #求职」: 话题跟在最后一句后面、不单独成行, 不摘的话交付稿里还是两份话题, 而且不报任何问题。"""
    tpl = {"layout": "blocks", "tags": {"content": 2, "fixed": ["固定甲"]}}
    out = delivery.assemble(tpl, [{"title": "甲", "body": "正文一段。冲鸭！#秋招 #求职", "tags": ["秋招", "求职"]}])
    assert out["text"] == "【标题】甲\n【正文】正文一段。冲鸭！\n#秋招 #求职 #固定甲"
    lifted = delivery.assemble(tpl, [{"title": "甲", "body": "正文。冲鸭！#秋招[话题]# #求职"}])
    assert lifted["text"].endswith("【正文】正文。冲鸭！\n#秋招 #求职 #固定甲") and lifted["problems"] == []
    # 正文中间的话题、和英文连写的 # 不动
    assert delivery.lift_trailing_tags("先说#中间话题 再说结尾") == ("先说#中间话题 再说结尾", [])
    assert delivery.lift_trailing_tags("在学C#入门") == ("在学C#入门", [])


def test_a_template_without_tags_leaves_the_body_alone():
    out = delivery.assemble({"layout": "blocks"}, [{"title": "甲", "body": "正文。\n#话题甲", "tags": ["别的"]}])
    assert out["text"] == "【标题】甲\n【正文】正文。\n#话题甲"


# ══════════════════════════════════════════════════════════════════════
# 问题: 只报代码判得准的, 下标按写手传的位置
# ══════════════════════════════════════════════════════════════════════

def test_problems_are_reported_against_the_callers_index():
    out = delivery.assemble(TABLE, [
        {"title": "【标题】甲", "body": "【正文】正文", "tags": ["只有一个"], "direction": "",
         "comments": [{"role": "reader", "text": "没提到品名"}, {"role": "author", "text": "示例品"},
                      {"role": "reader", "text": "示例品"}]},
        {"title": "乙", "body": "正文", "tags": ["一", "二"], "direction": "方向",
         "comments": [{"role": "boss", "text": "示例品"}]},
    ], indices=[4, 7])
    by = {}
    for p in out["problems"]:
        by.setdefault(p["index"], []).append(p["what"])
    first = " / ".join(by[4])
    assert "标题里已经有「【标题】」" in first and "正文里已经有「【正文】」" in first, "模型自己又包了一层标记"
    assert "话题标签 1 个, 这个项目要 2 个" in first
    assert "评论 3 条, 这个项目要 1~2 条" in first
    assert "第 1 条评论没提到「示例品」" in first and "第 2 条评论" not in first
    assert "表格有「方向」列, 这篇没带 direction" in first
    assert by[7] == ["第 1 条评论的 role 是「boss」, 只认 reader / author"]


def test_comments_and_direction_are_not_checked_when_the_template_does_not_ask():
    out = delivery.assemble(BLOCKS, [{"title": "甲", "body": "正文", "tags": ["话题"]}])
    assert out["problems"] == []


# ══════════════════════════════════════════════════════════════════════
# 模板校验
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("bad, words", [
    ({**TABLE, "colour": "red"}, "不认识的键"),
    ({**TABLE, "tags": {**TABLE["tags"], "order": 1}}, "不认识的键"),
    ({**TABLE, "columns": [{"name": "笔记", "field": "post", "width": 3}]}, "不认识的键"),
    ({**TABLE, "comments": {**TABLE["comments"], "tone": "x"}}, "不认识的键"),
    ({**TABLE, "tags": {"conditional": [{"when_any": ["a"], "add": "b", "x": 1}]}}, "不认识的键"),
    ({**TABLE, "markers": {"title": "标题：", "body": "正文："}}, "冒号"),
    ({**TABLE, "layout": "list"}, "layout"),
    ({"layout": "table"}, "columns"),
    ({"layout": "table", "columns": [{"name": "方向", "field": "direction"}]}, "field=post"),
    ({"layout": "table", "columns": [{"name": "笔记", "field": "post"}, {"name": "评", "field": "comments"}]},
     "comments 没配"),
    ({"layout": "table", "columns": [{"name": "a", "field": "post"}, {"name": "b", "field": "post"}]}, "两次"),
    ({"layout": "table", "columns": [{"name": "a|b", "field": "post"}]}, "表头"),
    ({"layout": "blocks", "columns": [{"name": "笔记", "field": "post"}]}, "只给 table 用"),
    ({"layout": "blocks", "tags": {"content": 11}}, "0~10"),
    ({"layout": "blocks", "tags": {"content": True}}, "0~10"),
    ({"layout": "blocks", "tags": {"fixed": ["两个 词"]}}, "不是一个话题"),
    ({"layout": "blocks", "comments": {"min": 3, "max": 1}}, "min ≤ max"),
    ({"layout": "blocks", "comments": {"min": 0, "max": 1, "label_reader": "a\nb"}}, "label_reader"),
    ({"layout": "blocks", "comments": {"min": 1, "max": 2}}, "都要非空"),
    ({"layout": "blocks", "comments": {"min": 1, "max": 2, "label_reader": "读者：", "label_author": ""}}, "都要非空"),
    ({"layout": "table", "columns": [{"name": "内容", "field": "post"}], "comments": {"min": 2, "max": 3}},
     "没有 field=comments 的列"),
    ("table", "JSON 对象"),
])
def test_bad_templates_are_rejected_with_a_reason(bad, words):
    with pytest.raises(delivery.TemplateError) as exc:
        delivery.validate(bad)
    assert words in str(exc.value)


def test_validate_fills_defaults_and_is_idempotent():
    norm = delivery.validate(BLOCKS)
    assert norm["markers"] == {"title": "【标题】", "body": "【正文】"}
    assert norm["columns"] == [] and norm["comments"] is None
    assert delivery.validate(norm) == norm
    assert delivery.validate(delivery.validate(TABLE)) == delivery.validate(TABLE)


def test_default_params_is_read_as_a_json_string_or_a_dict():
    as_str = {"default_params": json.dumps({"isolation_terms": ["词"], "delivery": BLOCKS}, ensure_ascii=False)}
    assert delivery.template_of(as_str)["layout"] == "blocks"
    assert delivery.template_of({"default_params": {"delivery": BLOCKS}})["layout"] == "blocks"
    assert delivery.template_of({"default_params": json.dumps({"isolation_terms": ["词"]})}) is None
    assert delivery.template_of({"default_params": None}) is None
    with pytest.raises(delivery.TemplateError):
        delivery.template_of({"default_params": "{not json"})


# ══════════════════════════════════════════════════════════════════════
# CLI: 写之前先校验, 写回不丢别的键, 仍存成 JSON 字符串
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def cli_client(monkeypatch):
    c = FakeClient(rows={"projects": [{"id": PROJ, "name": "示例项目", "brand": "示例品牌", "owner_id": ME,
                                       "default_params": json.dumps({"isolation_terms": ["示例词"]},
                                                                    ensure_ascii=False)}]})
    monkeypatch.setattr(core, "sb", lambda: c)
    return c


def _params(c) -> dict:
    raw = c.rows["projects"][0]["default_params"]
    assert isinstance(raw, str), "生产库这一列全是 JSON 字符串, 别写成 dict 混进去"
    return json.loads(raw)


def test_cli_set_show_clear_round_trip(cli_client, tmp_path, capsys):
    f = tmp_path / "tpl.json"
    f.write_text(json.dumps(TABLE, ensure_ascii=False), encoding="utf-8")
    assert cli.main(["delivery-format", "--project", PROJ, "--set", str(f)]) == 0
    stored = _params(cli_client)
    assert stored["isolation_terms"] == ["示例词"], "写模板把隔离词抹掉了"
    assert stored["delivery"] == delivery.validate(TABLE)
    printed = capsys.readouterr().out
    assert "| 方向 | 笔记 | 评论 |" in printed, "设完要让运维看得见拼出来的样子"

    assert cli.main(["delivery-format", "--project", PROJ, "--show"]) == 0
    assert "【标题】示例标题" in capsys.readouterr().out

    assert cli.main(["delivery-format", "--project", PROJ, "--clear"]) == 0
    assert _params(cli_client) == {"isolation_terms": ["示例词"]}


def test_cli_refuses_a_bad_template_and_writes_nothing(cli_client, tmp_path, capsys):
    before = cli_client.rows["projects"][0]["default_params"]
    f = tmp_path / "bad.json"
    f.write_text(json.dumps({**BLOCKS, "markers": {"title": "标题："}}, ensure_ascii=False), encoding="utf-8")
    assert cli.main(["delivery-format", "--project", PROJ, "--set", str(f)]) == 1
    assert "模板没写进去" in capsys.readouterr().out
    assert cli_client.rows["projects"][0]["default_params"] == before
    assert not [x for x in cli_client.calls if x["op"] == "update"]


def test_cli_refuses_to_overwrite_default_params_it_cannot_parse(cli_client, tmp_path):
    cli_client.rows["projects"][0]["default_params"] = "{broken"
    f = tmp_path / "tpl.json"
    f.write_text(json.dumps(BLOCKS), encoding="utf-8")
    assert cli.main(["delivery-format", "--project", PROJ, "--set", str(f)]) == 1
    assert cli_client.rows["projects"][0]["default_params"] == "{broken", "拿 {} 去合并会把别的键一起抹掉"


# ══════════════════════════════════════════════════════════════════════
# 打分路径: score_drafts / commit_drafts
# ══════════════════════════════════════════════════════════════════════

_POOL = ("的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方"
         "后多定行学法所民得经十三之进着等部度家电力里如水化高自二理起小物现实加量都两体制机当使点从业本去把性好应")


def distinct_drafts(n: int, seed: int = 0) -> list[dict]:
    """n 篇互不撞车的合成稿(同 test_deskcore_commit_judge 的造法), 免得查重闸先拦掉几篇。"""
    out = []
    for i in range(n):
        rng = random.Random(seed * 100 + i)
        body = "".join(f"这一段说的是{''.join(rng.choice(_POOL) for _ in range(18))}。" for _ in range(10))
        out.append({"title": f"第{i}篇{''.join(rng.choice(_POOL) for _ in range(6))}", "body": body})
    return out


def desk_client(*, default_params=None, memories=(), maps=()) -> FakeClient:
    proj = {"id": PROJ, "name": "示例项目", "brand": "示例品牌", "owner_id": ME, "calibration_notes": "",
            "tactics": "[]", "custom_roles": []}
    if default_params is not None:
        proj["default_params"] = default_params
    c = FakeClient(rows={"projects": [proj], "memories": list(memories), "tv_project_map": list(maps)})

    def _rpc(args):
        rows = []
        for k, r in enumerate(args.get("_rows") or []):
            c.rows.setdefault("draft_fingerprints", []).append({
                "id": f"fp-{r['version_id']}", "project_id": PROJ, "user_id": ME, "version_id": r["version_id"],
                "title": r["title"], "opening": r["opening"], "opening_hash": r["opening_hash"],
                "ngram_hashes": r["ngram_hashes"], "title_embedding": None, "embedding_model": None,
                "angle_key": r.get("angle_key"), "created_at": "2026-10-20T00:00:00+00:00"})
            rows.append({"idx": k, "status": core.COMMIT_STATUS_INSERTED, "collided_with": None, "detail": None})
        return rows
    c.rpc_impl["deskcore_commit_fingerprints"] = _rpc
    return c


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    monkeypatch.setattr(config, "JUDGE_URL", "")


def _params_with(tpl) -> str:
    return json.dumps({"isolation_terms": ["示例词"], "delivery": tpl}, ensure_ascii=False)


def test_score_drafts_carries_the_assembled_batch_when_the_project_has_a_template():
    c = desk_client(default_params=_params_with(BLOCKS))
    drafts = distinct_drafts(2)
    drafts[0]["tags"] = ["话题甲"]
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert out["written"] == 2
    d = out["delivery"]
    assert d["format"] == "blocks"
    assert d["text"].count("【标题】") == 2 and "#话题甲" in d["text"]
    assert d["problems"] == [{"index": 1, "what": "话题标签 0 个, 这个项目要 1 个; 在 tags 里补齐"}]
    assert "delivery.text" in out["next_step"], "下一步要说清: 原样交 delivery.text"
    meta = c.rows["batch_metrics"][0]["meta"]
    assert meta["delivery"] == {"layout": "blocks", "drafts": 2, "problems": 1}


def test_rejected_drafts_are_not_in_the_delivery_and_problem_indices_follow_the_caller():
    c = desk_client(default_params=_params_with(BLOCKS))
    old = distinct_drafts(1, seed=9)[0]
    core.commit_drafts(c, PROJ, [old], user_id=ME)
    fresh = distinct_drafts(1, seed=3)[0]
    out = core.commit_drafts(c, PROJ, [{**old, "title": "换了个标题"}, fresh], user_id=ME)
    assert [r["index"] for r in out["rejected"]] == [0]
    assert out["delivery"]["text"].count("【标题】") == 1 and "换了个标题" not in out["delivery"]["text"]
    assert [p["index"] for p in out["delivery"]["problems"]] == [1]


def test_no_template_means_the_response_is_unchanged():
    """老项目、老调用方: 没模板就不带 delivery, 多传的字段也不改变任何别的返回值。"""
    plain = core.commit_drafts(desk_client(), PROJ, distinct_drafts(2), user_id=ME)
    extra = distinct_drafts(2)
    extra[0].update(tags=["话题"], comments=[{"role": "reader", "text": "评论"}], direction="方向")
    rich = core.commit_drafts(desk_client(), PROJ, extra, user_id=ME)
    for out in (plain, rich):
        assert "delivery" not in out and "rule_checks" not in out
    volatile = {"version_ids", "batch_id", "elapsed_ms", "phase_ms", "judge", "project_isolation"}
    assert {k: v for k, v in plain.items() if k not in volatile} == \
           {k: v for k, v in rich.items() if k not in volatile}


def test_the_brief_tells_the_writer_what_each_draft_must_carry():
    """不进简报的话, 第一次 score_drafts 才知道要几个话题 / 几条评论 / 要不要方向, 整批带着 problems 再存一次。"""
    c = desk_client(default_params=_params_with(TABLE))
    req = core.build_writing_brief(c, PROJ, user_id=ME)["delivery_requirements"]
    assert req == {"layout": "table", "tags": 2, "fixed_tags": ["固定甲"],
                   "comments": {"min": 1, "max": 2, "must_mention_any": ["示例品"]}, "direction": True,
                   "task": req["task"]}
    for words in ("tags 给 2 个话题", "#固定甲", "comments 给 1~2 条", "示例品", "direction", "不是 产品向 / 流量向"):
        assert words in req["task"]
    assert "delivery_requirements" not in core.build_writing_brief(desk_client(), PROJ, user_id=ME)
    broken = desk_client(default_params=json.dumps({"delivery": {"layout": "table"}}))
    assert "delivery_requirements" not in core.build_writing_brief(broken, PROJ, user_id=ME), "模板坏了不拦简报"


def test_a_broken_template_never_breaks_scoring():
    c = desk_client(default_params=json.dumps({"delivery": {"layout": "table"}}))
    out = core.commit_drafts(c, PROJ, distinct_drafts(1), user_id=ME)
    assert out["written"] == 1 and "delivery" not in out
    assert "columns" in c.rows["batch_metrics"][0]["meta"]["delivery"]["error"]


def test_tags_comments_and_direction_are_not_sent_to_the_judge(monkeypatch):
    monkeypatch.setattr(config, "JUDGE_URL", "https://judge.example.invalid")
    monkeypatch.setattr(config, "JUDGE_API_KEY", "jk")
    payloads, lock = [], threading.Lock()

    def fake(payload, *, timeout=None):
        with lock:
            payloads.append(payload)
        return judge_client.result(judge_client.JUDGE_OK, http_status=200, passed=True, calls=1, written=1)
    monkeypatch.setattr(judge_client, "judge_draft", fake)
    maps = [{"tv_project_id": "DEMO_phase1", "project_id": PROJ, "ingest_target": True}]

    plain = distinct_drafts(1)
    core.commit_drafts(desk_client(maps=maps), PROJ, plain, user_id=ME)
    rich = [{**plain[0], "tags": ["话题甲"], "direction": "方向",
             "comments": [{"role": "reader", "text": "示例评论"}]}]
    core.commit_drafts(desk_client(maps=maps, default_params=_params_with(TABLE)), PROJ, rich, user_id=ME)

    assert len(payloads) == 2
    a, b = ({k: v for k, v in p.items() if k != "subject_id"} for p in payloads)
    assert a == b, "加了话题 / 评论 / 方向之后发给 judge 的东西变了"
    assert b["body"] == plain[0]["body"]
    assert not {"tags", "comments", "direction"} & set(b)
