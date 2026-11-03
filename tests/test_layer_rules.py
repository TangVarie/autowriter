"""贴型(content layers)之后, 带方向的规则跟着每篇卡的贴型走; 存档判定的 product_role 和贴型对不上时给一句提示。

钉的是(规则、稿子、key 全是合成的):
  · 简报: 带方向的规则在 P0 / P1 里注明只管哪种贴的稿, 末尾一句说清没标贴型的照常守; 原来那一行不改, root 的
    memory.build_layered_system_prompt 不动; 不带方向的原样;
  · score_drafts 的代码查规则(rulecheck): 卡标了流量贴 / 提问贴的稿只查流量向 + 通用的, 产品贴 / 精品贴只查产品向 + 通用的
    (hard、soft 一样); 没标贴型的稿照原来的方向闸; 一篇都没标就和原来一字不差;
  · check_rules 的两条路: Jev 规则题按贴型分组发各自的题库, 编出来的 code 只查它管的那几篇;
  · 判定回来的 product_role: 产品贴判出「未出现」、流量贴判出「主角」时那一篇带 layer_hint; 没标贴型不带, 回包不多键。
"""
from __future__ import annotations

import json

import pytest

import config
import judge_client
from deskcore import core, rulecheck, store, vocab
from deskcore.fingerprint import angle_key
from tests.fakes import FakeClient
from tests.test_delivery import ME, PROJ, desk_client, distinct_drafts
from tests.test_rulecheck import _rule

CELLS = [{"emotional_lever": "焦虑撬动", "human_truth_archetype": "健康焦虑", "content_format": "提问求助",
          "title_structure": t} for t in ("疑问句", "数字清单", "场景直述", "对比反转")]


def _ledger(*lys) -> tuple[list[dict], list[str]]:
    """每张卡一行(没用过的), 第 i 张标 lys[i](None 不标)。回 (台账行, 它们的 key)。"""
    rows, keys = [], []
    for i, ly in enumerate(lys):
        dims = dict(CELLS[i])
        d = {"v": 2, "pol": "v2", "src": "w"}
        if ly:
            d["ly"] = ly
        k = angle_key(dims)
        keys.append(k)
        rows.append({"id": f"l{i}", "project_id": PROJ, "angle_key": k, "dims": {**dims, "_draw": d},
                     "drawn_by": ME, "drawn_at": f"2026-11-0{i + 1}T00:00:00+00:00",
                     "consumed_at": None, "consumed_version_id": None})
    return rows, keys


# 产品向的项目(项目名判出方向) + 四条带检查的规则
RULES = [_rule(1, "required_phrase", {"any": ["示例产品名"]}, severity="soft", applicability="产品向"),
         _rule(2, "required_phrase", {"any": ["示例口播"]}, severity="soft", applicability="流量向"),
         _rule(3, "required_phrase", {"any": ["示例合规话术"]}, severity="hard", applicability="产品向"),
         _rule(4, "required_phrase", {"any": ["示例通用话术"]}, severity="hard")]


def _client(*lys):
    c = desk_client(memories=RULES)
    c.rows["projects"][0]["name"] = "示例项目 产品向"
    rows, keys = _ledger(*lys)
    c.rows["angle_ledger"] = rows
    return c, keys


def _by_draft(out) -> dict[int, list[str]]:
    got: dict[int, list[str]] = {}
    for h in out.get("rule_checks") or []:
        got.setdefault(h["index"], []).append(h["rule_id"])
    return {k: sorted(v) for k, v in got.items()}


# ══════════════════════════════════════════════════════════════════════
# 规则怎么判管不管这一篇
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("tag,sev,layer,direction,want", [
    (None, "soft", "T", "产品向", True), ("正文", "hard", "P", "", True),          # 不带方向词的(部位标注也算): 都管
    ("流量向", "soft", "T", "产品向", True), ("流量向", "hard", "Q", "", True),
    ("流量向", "soft", "P", "流量向", False), ("流量向", "hard", "J", "", False),    # 标了贴型: 方向闸不看了, hard 也跟着走
    ("产品向", "hard", "T", "产品向", False), ("产品向", "soft", "J", "流量向", True),
    ("产品向", "soft", None, "流量向", False), ("产品向", "soft", None, "", True),   # 没标贴型: 原来的方向闸
    ("产品向", "hard", None, "流量向", True),
])
def test_rule_applies_to_layer(tag, sev, layer, direction, want):
    rule = {"applicability": tag, "severity": sev}
    assert core.rule_applies_to_layer(rule, layer, direction) is want
    if layer is None and sev == "soft":
        keep, _drop = core.filter_by_applicability([rule], direction)
        assert bool(keep) is want, "没标贴型时和 filter_by_applicability 同一个判据"


# ══════════════════════════════════════════════════════════════════════
# 简报
# ══════════════════════════════════════════════════════════════════════

def test_brief_marks_direction_rules_and_keeps_the_rest_verbatim():
    rows = [_rule(1, content="结尾要留白", severity="soft", applicability="产品向"),
            _rule(2, content="品牌名不进正文", severity="hard", applicability="流量向"),
            _rule(3, content="标题不超过二十字", severity="hard"),
            _rule(4, content="写清用了多久", severity="soft", applicability="正文")]
    c = desk_client(memories=[dict(r) for r in rows])
    out = core.build_writing_brief(c, PROJ, user_id=ME)
    assert "• 结尾要留白【只管产品贴/精品贴的稿】" in out["p1"]
    assert "• 品牌名不进正文【只管流量贴/提问贴的稿】" in out["p0"]
    assert "• 标题不超过二十字\n" in out["p0"] + "\n" and "• 写清用了多久" in out["p1"]
    assert "写清用了多久【" not in out["p1"] and "标题不超过二十字【" not in out["p0"]
    assert out["p0"].endswith(core.DIRECTION_RULE_TAIL) and out["p1"].endswith(core.DIRECTION_RULE_TAIL)
    assert "卡上没写是哪种贴的稿, 照常守" in core.DIRECTION_RULE_TAIL
    assert [r["content"] for r in c.rows["memories"]] == [r["content"] for r in rows], "库里的规则一个字不改"


def test_brief_without_direction_rules_is_unchanged(monkeypatch):
    import memory
    seen = {}
    real = memory.build_layered_system_prompt

    def spy(**kw):
        seen.update(kw)
        return real(**kw)
    monkeypatch.setattr(memory, "build_layered_system_prompt", spy)
    rows = [_rule(3, content="标题不超过二十字", severity="hard"), _rule(4, content="写清用了多久", severity="soft")]
    out = core.build_writing_brief(desk_client(memories=rows), PROJ, user_id=ME)
    assert core.DIRECTION_RULE_TAIL not in out["p0"] + out["p1"] and "【只管" not in out["p0"] + out["p1"]
    assert sorted(m["content"] for m in seen["project_memories"]) == ["写清用了多久", "标题不超过二十字"]


def _main_and_other(p1: str) -> tuple[str, str]:
    """简报 P1 → (原来那一截, 方向闸挡掉的那一截)。"""
    head = "[只管卡上写着"
    return (p1.split(head)[0], head + p1.split(head)[1]) if head in p1 else (p1, "")


def test_the_session_direction_gate_moves_the_other_soft_rules_to_their_own_block():
    """复审: 方向闸挡掉的 soft 规则(另一个方向的)卡上标了那一族贴型的稿照查, 所以简报里得给模型看 —— 不在原来那一截
    (那一截一个字不变、counts 照旧), 单独一截接在 P1 最后, 写明没标贴型的稿不用管。"""
    c = desk_client(memories=RULES)
    c.rows["projects"][0]["name"] = "示例项目 产品向"
    out = core.build_writing_brief(c, PROJ, user_id=ME)
    main, other = _main_and_other(out["p1"])
    assert "示例规则 2" not in main and out["counts"]["soft_dropped_by_direction"] == 1
    assert "示例规则 1【只管产品贴/精品贴的稿】" in main and main.rstrip().endswith(core.DIRECTION_RULE_TAIL)
    assert "示例规则 3【只管产品贴/精品贴的稿】" in out["p0"]
    assert other == core.OTHER_DIRECTION_BLOCK.format(names="流量贴/提问贴", direction="产品向", other="流量向",
                                                      bullets="• 示例规则 2")
    assert "卡上没写是哪种贴的稿不用管" in other and out["p1"].endswith(other)
    assert out["counts"]["soft_rules_for_other_layers"] == 1 and out["counts"]["soft_rules"] == 1

    # 一条被挡掉的都没有(项目名判不出方向): 没有这一截、没有这个键
    c2 = desk_client(memories=RULES)
    out2 = core.build_writing_brief(c2, PROJ, user_id=ME)
    assert "[只管卡上写着" not in out2["p1"] and "soft_rules_for_other_layers" not in out2["counts"]


def test_brief_without_a_p1_still_shows_the_other_direction_block():
    rows = [_rule(1, content="示例流量向规则", severity="soft", applicability="流量向")]
    c = desk_client(memories=rows)
    c.rows["projects"][0]["name"] = "示例项目 产品向"
    out = core.build_writing_brief(c, PROJ, user_id=ME)
    assert out["p1"].startswith("[只管卡上写着流量贴/提问贴的稿]") and "• 示例流量向规则" in out["p1"]


def _soft_rules_shown(out: dict, layer: str | None) -> set[str]:
    """简报告诉这一种卡要守的 soft 规则(示例规则 i → m-i): 原来那一截里没注贴型的 + 注了这一族的; 卡上标了贴型的再加上
    方向闸挡掉的那一截里写着这一族的。"""
    main, other = _main_and_other(out["p1"])
    fam = vocab.LAYER_DIRECTION.get(layer or "")
    got = set()
    for line in main.splitlines():
        if not line.startswith("• 示例规则 "):
            continue
        note = next((t for t, n in vocab.DIRECTION_RULE_NOTE.items() if n in line), None)
        if note is None or fam is None or note == fam:
            got.add("m-" + line.split("示例规则 ")[1][:1])
    if other and fam and core.DIRECTION_LAYER_NAMES[fam] in other.splitlines()[0]:
        got |= {"m-" + line.split("示例规则 ")[1][:1] for line in other.splitlines() if line.startswith("• 示例规则 ")}
    return got


def test_brief_and_per_draft_soft_checks_agree():
    """复审: 每篇被报「建议改」的 soft 规则, 简报里都告诉过模型这一种卡要守它; 反过来简报叫这一种卡守的、带检查的 soft
    规则, 这一篇都查了。"""
    lys = ("T", "P", None, "J")
    c, keys = _client(*lys)
    out = core.build_writing_brief(c, PROJ, user_id=ME)
    drafts = distinct_drafts(4)
    for d, k in zip(drafts, keys):
        d["angle_key"] = k
    got = _by_draft(core.commit_drafts(c, PROJ, drafts, user_id=ME))
    soft_ids = {r["id"] for r in RULES if r["severity"] == "soft"}
    for i, ly in enumerate(lys):
        assert set(got.get(i, [])) & soft_ids == _soft_rules_shown(out, ly), (i, ly)
    assert _soft_rules_shown(out, "T") == {"m-2"} and _soft_rules_shown(out, None) == {"m-1"}, "反证: 两族看到的不一样"


# ══════════════════════════════════════════════════════════════════════
# score_drafts 的代码查规则
# ══════════════════════════════════════════════════════════════════════

def test_score_drafts_checks_direction_rules_per_card_layer():
    c, keys = _client("T", "P", None, "Q")
    drafts = distinct_drafts(4)
    for d, k in zip(drafts, keys):
        d["angle_key"] = k
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert out["written"] == 4 and out["consumed_angles"] == 4
    assert _by_draft(out) == {
        0: ["m-2", "m-4"],                     # 流量贴: 流量向的 soft(产品向项目的方向闸原来会挡掉它)+ 通用的; 产品向的 hard 不管
        1: ["m-1", "m-3", "m-4"],              # 产品贴: 产品向的 + 通用的
        2: ["m-1", "m-3", "m-4"],              # 没标贴型: 原来的口径(方向闸挡掉流量向的 soft, hard 都管)
        3: ["m-2", "m-4"],                     # 提问贴: 同流量贴
    }
    assert c.rows["batch_metrics"][0]["meta"]["rule_checks"]["layered"] == 3


BROKEN = _rule(9, "required_phrase", {"bogus": 1}, severity="soft", applicability="流量向")   # 存的检查用不了


@pytest.mark.parametrize("lys,unchecked,n_rules", [
    ((None, None), [], 3),             # 没标贴型: 方向闸挡掉流量向的 soft(原来的口径)
    (("P", "P"), [], 3),               # 整批产品贴: 流量向的那几条哪一篇都不管 —— 不报「整批都没查」、不数
    (("T", "P"), ["m-9"], 4),          # 有一篇流量贴: 那条管得着它, 用不了就照报(用不了的不数进 rules)
])
def test_rules_that_apply_to_no_draft_are_not_reported_or_counted(lys, unchecked, n_rules):
    """复审: 标了贴型时池子先拿掉这一批哪一篇都不管的规则, rule_checks_unchecked 和 stats 只说管得着的。"""
    c, keys = _client(*lys)
    c.rows["memories"].append(dict(BROKEN))
    drafts = distinct_drafts(2)
    for d, k in zip(drafts, keys):
        d["angle_key"] = k
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert [u["rule_id"] for u in out.get("rule_checks_unchecked") or []] == unchecked
    meta = c.rows["batch_metrics"][0]["meta"]["rule_checks"]
    assert meta["rules"] == n_rules and meta.get("skipped", 0) == len(unchecked)


def test_score_drafts_without_layers_is_exactly_the_old_gate(monkeypatch):
    calls = []
    real = rulecheck.check
    monkeypatch.setattr(rulecheck, "check", lambda *a, **k: calls.append(k) or real(*a, **k))
    c, keys = _client(None, None)
    drafts = distinct_drafts(3)
    drafts[0]["angle_key"], drafts[1]["angle_key"] = keys
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert _by_draft(out) == {i: ["m-1", "m-3", "m-4"] for i in range(3)}
    assert "applies" not in calls[0], "一篇都没标就照原来那样调"
    assert "layered" not in c.rows["batch_metrics"][0]["meta"]["rule_checks"]


def test_a_ledger_read_failure_falls_back_to_the_old_gate(monkeypatch):
    monkeypatch.setattr(store, "ledger_layers", lambda *a, **k: None)
    c, keys = _client("T")
    drafts = distinct_drafts(1)
    drafts[0]["angle_key"] = keys[0]
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert _by_draft(out) == {0: ["m-1", "m-3", "m-4"]}


# ══════════════════════════════════════════════════════════════════════
# check_rules: Jev 规则题(按贴型分组的题库)和编出来的 code
# ══════════════════════════════════════════════════════════════════════

from tests.test_check_rules import answers, version  # noqa: E402
from tests.test_rule_compile import (PROJ as CPROJ, _fresh_compile_state, code_result, jev,  # noqa: E402,F401
                                     jev_sha, make_client, mem, question_result, stored)

V_T = "cccccccc-0000-4000-8000-0000000000f1"
V_P = "cccccccc-0000-4000-8000-0000000000f2"
V_N = "cccccccc-0000-4000-8000-0000000000f3"
R_PROD = mem(11, "示例软规则: 正文写到示例产品名", applicability="产品向")
R_TRAF = mem(12, "示例软规则: 正文不提品牌名", applicability="流量向")
R_ALL = mem(13, "示例硬规则: 不能写疗效", severity="hard")
R_PROD_CODE = mem(14, "示例硬规则: 正文不写「示例价格」", severity="hard", applicability="产品向")
R_TRAF_CODE = mem(15, "示例软规则: 正文不写「示例渠道」", applicability="流量向")
BODY = ("这是一段合成的正文, 只用来测试。第一句讲一件小事, 提到示例价格和示例渠道。第二句讲当时的感受。"
        "第三句收个尾。") * 2


def _q(rule, ask):
    return question_result(rule["id"], jev_sha(rule["content"], rule.get("applicability")), ask=ask, pass_if="否")


def _code(rule, word):
    return code_result(rule["id"], jev_sha(rule["content"], rule.get("applicability")),
                       {"kind": "forbidden_word", "words": [word], "where": ["body"]})


Q_PROD, Q_TRAF, Q_ALL = _q(R_PROD, "正文有没有漏掉示例产品名？"), _q(R_TRAF, "正文有没有提品牌名？"), _q(R_ALL, "有没有写疗效？")


def _rules_client(*, layers: dict[str, str]):
    mems = [R_PROD, R_TRAF, R_ALL, R_PROD_CODE, R_TRAF_CODE]
    comp = [stored(R_PROD, Q_PROD), stored(R_TRAF, Q_TRAF), stored(R_ALL, Q_ALL),
            stored(R_PROD_CODE, _code(R_PROD_CODE, "示例价格")), stored(R_TRAF_CODE, _code(R_TRAF_CODE, "示例渠道"))]
    c = make_client(mems, comp, versions=[version(v, body=BODY) for v in (V_T, V_P, V_N)])
    # 销给 V_T / V_P 的那两张卡(台账); V_N 的卡在指纹行里记着 key, 台账那张没标贴型
    rows, keys = _ledger(layers.get(V_T), layers.get(V_P), None)
    for r, vid in zip(rows, (V_T, V_P)):
        r.update(project_id=CPROJ, consumed_version_id=vid, consumed_at="2026-11-02T00:00:00+00:00")
    rows[2]["project_id"] = CPROJ
    c.rows["angle_ledger"] = rows
    c.rows["draft_fingerprints"] = [{"project_id": CPROJ, "version_id": V_N, "angle_key": keys[2],
                                     "created_at": "2026-11-02T00:00:00+00:00"}]
    return c


@pytest.fixture
def wired(jev, monkeypatch):                    # noqa: F811
    monkeypatch.setattr(config, "JUDGE_DRAFT_TOOL_TIMEOUT_SEC", 18.0)
    monkeypatch.setattr(core, "_kick_rule_compile", lambda *a, **k: "started")
    jev.judge = answers(**{q["questions"][0]["qid"]: {"answer": "是", "p": 0.9, "ambiguous": False}
                           for q in (Q_PROD, Q_TRAF, Q_ALL)})
    return jev


def _banks(jev) -> dict[str, list[str]]:
    return {x["json"]["subject_id"]: sorted(q["id"] for q in x["json"]["project_bank"]["questions"])
            for x in jev.calls if x["url"].endswith("/judge_draft")}


def _rule_ids(row) -> list[str]:
    return sorted({e["rule_id"] for e in row["violations"]})


def test_check_rules_sends_each_layer_group_its_own_bank_and_runs_code_per_layer(wired):
    c = _rules_client(layers={V_T: "T", V_P: "J"})
    out = core.check_rules(c, CPROJ, [V_T, V_P, V_N], user_id=ME)
    qid = {r["id"]: q["questions"][0]["qid"] for r, q in ((R_PROD, Q_PROD), (R_TRAF, Q_TRAF), (R_ALL, Q_ALL))}
    assert _banks(wired) == {
        V_T: sorted([qid[R_TRAF["id"]], qid[R_ALL["id"]]]),          # 流量贴: 流量向 + 通用(项目名是产品向也一样)
        V_P: sorted([qid[R_PROD["id"]], qid[R_ALL["id"]]]),          # 精品贴: 产品向 + 通用
        V_N: sorted([qid[R_PROD["id"]], qid[R_ALL["id"]]]),          # 没标: 原来的方向闸(产品向项目)
    }
    rows = {r["version_id"]: r for r in out["versions"]}
    assert _rule_ids(rows[V_T]) == sorted([R_TRAF["id"], R_ALL["id"], R_TRAF_CODE["id"]])
    assert _rule_ids(rows[V_P]) == sorted([R_PROD["id"], R_ALL["id"], R_PROD_CODE["id"]])
    assert _rule_ids(rows[V_N]) == sorted([R_PROD["id"], R_ALL["id"], R_PROD_CODE["id"]])
    assert rows[V_T]["layer"] == "流量贴" and rows[V_P]["layer"] == "精品贴" and "layer" not in rows[V_N]
    assert out["rules"]["pool"] == 5, "有稿子标了贴型时池子里留着管得着某一篇的规则(这里五条都管得着), 逐篇分组"


def test_check_rules_without_layers_is_the_old_single_bank(wired):
    c = _rules_client(layers={})
    out = core.check_rules(c, CPROJ, [V_T, V_P, V_N], user_id=ME)
    banks = _banks(wired)
    assert len(set(map(tuple, banks.values()))) == 1 and len(banks) == 3
    assert all("layer" not in r for r in out["versions"])
    assert out["rules"]["pool"] == 3, "方向闸照旧挡掉两条流量向的 soft"
    for r in out["versions"]:
        assert _rule_ids(r) == sorted([R_PROD["id"], R_ALL["id"], R_PROD_CODE["id"]])


def test_check_rules_skips_jev_for_a_group_with_no_questions(wired):
    """流量贴那一组只剩 code(没有规则题): 那一篇不发 Jev, code 照查。"""
    mems = [R_PROD, R_TRAF_CODE]
    comp = [stored(R_PROD, Q_PROD), stored(R_TRAF_CODE, _code(R_TRAF_CODE, "示例渠道"))]
    c = make_client(mems, comp, versions=[version(v, body=BODY) for v in (V_T, V_P)])
    rows, _keys = _ledger("T", "P")
    for r, vid in zip(rows, (V_T, V_P)):
        r.update(project_id=CPROJ, consumed_version_id=vid, consumed_at="2026-11-02T00:00:00+00:00")
    c.rows["angle_ledger"] = rows
    out = core.check_rules(c, CPROJ, [V_T, V_P], user_id=ME)
    assert list(_banks(wired)) == [V_P]
    by = {r["version_id"]: r for r in out["versions"]}
    assert _rule_ids(by[V_T]) == [R_TRAF_CODE["id"]] and "judge_status" not in by[V_T]
    assert _rule_ids(by[V_P]) == [R_PROD["id"]]


R_TRAF_NEW = mem(16, "示例软规则: 还没编译的流量向规则", applicability="流量向")


def test_check_rules_pool_only_holds_rules_that_apply_to_some_target(wired):
    """复审: 几篇全是产品向那一族时, 流量向的规则(编了的、还没编的)哪一篇都不管 —— 不进池子、不数进 uncompiled(不为它们
    补编)、next_step 不提; 有一篇流量贴时照旧算。"""
    c = _rules_client(layers={V_T: "P", V_P: "J"})
    c.rows["memories"].append(dict(R_TRAF_NEW))
    out = core.check_rules(c, CPROJ, [V_T, V_P], user_id=ME)
    assert out["rules"]["pool"] == 3 and out["uncompiled_rules"] == 0 and out["rules"]["uncompiled"] == 0
    assert "uncompiled_reason" not in out["rules"] and "还没有编译结果" not in out.get("next_step", "")
    assert all(R_TRAF["id"] not in _rule_ids(r) and R_TRAF_CODE["id"] not in _rule_ids(r) for r in out["versions"])

    wired.calls.clear()
    c2 = _rules_client(layers={V_T: "T", V_P: "J"})
    c2.rows["memories"].append(dict(R_TRAF_NEW))
    out2 = core.check_rules(c2, CPROJ, [V_T, V_P], user_id=ME)
    assert out2["rules"]["pool"] == 6 and out2["uncompiled_rules"] == 1


def test_version_layers_reads_the_consumed_row_then_the_fingerprint_key():
    c = _rules_client(layers={V_T: "T"})
    c.rows["angle_ledger"][2]["dims"]["_draw"]["ly"] = "Q"            # V_N 那张卡(指纹行记的 key)
    assert store.version_layers(c, CPROJ, [V_T, V_P, V_N, "nope"]) == {V_T: "T", V_N: "Q"}

    class Boom(FakeClient):
        def _execute(self, q):
            raise RuntimeError("statement timeout")
    assert store.version_layers(Boom(), CPROJ, [V_T]) is None
    assert core._version_layers(Boom(), CPROJ, [V_T]) == {}


# ══════════════════════════════════════════════════════════════════════
# G · 判定的 product_role 和贴型对不上: 一句提示(不拦存档, 不多调 Jev)
# ══════════════════════════════════════════════════════════════════════

def test_judge_client_keeps_only_product_role_from_the_profile():
    body = {"passed": True, "hard_fails": [], "plan": [], "unjudged": [],
            "profile": {"product_role": "未出现", "opening_type": "具体事件"}}
    assert judge_client._normalize_ok(body)["product_role"] == "未出现"
    assert "product_role" not in judge_client._normalize_ok({**body, "profile": {"opening_type": "具体事件"}})
    assert "product_role" not in judge_client._normalize_ok({k: v for k, v in body.items() if k != "profile"})


@pytest.fixture
def judged(monkeypatch):
    monkeypatch.setattr(config, "JUDGE_URL", "https://judge.example.invalid")
    monkeypatch.setattr(config, "JUDGE_API_KEY", "jk")
    roles: dict[str, str] = {}
    sent: list[dict] = []

    def fake(payload, *, timeout=None):
        sent.append(payload)
        extra = {"product_role": roles[payload["title"]]} if payload["title"] in roles else {}
        return judge_client.result(judge_client.JUDGE_OK, http_status=200, passed=True, calls=2, written=20, **extra)
    monkeypatch.setattr(judge_client, "judge_draft", fake)
    return roles, sent


def _judged_client(*lys):
    c, keys = _client(*lys)
    c.rows["memories"] = []
    c.rows["tv_project_map"] = [{"tv_project_id": "DEMO_phase1", "project_id": PROJ, "ingest_target": True}]
    return c, keys


def test_layer_hints_for_a_product_card_without_the_product_and_a_traffic_card_led_by_it(judged):
    roles, sent = judged
    c, keys = _judged_client("P", "T", "J", None)
    drafts = distinct_drafts(5)
    for d, k in zip(drafts, keys):
        d["angle_key"] = k
    for d, role in zip(drafts, ("未出现", "主角", "解决方案", "未出现", "主角")):
        roles[d["title"]] = role
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert len(sent) == 5, "只用存档那次判定的回包, 不多调"
    rows = {r["index"]: r for r in out["judge"]["results"]}
    assert rows[0]["layer_hint"] == core.LAYER_HINT["product"].format(name="产品贴", role="未出现")
    assert rows[1]["layer_hint"] == core.LAYER_HINT["traffic"].format(name="流量贴", role="主角")
    assert all("layer_hint" not in rows[i] for i in (2, 3, 4)), "精品贴判出解决方案 / 没标贴型的不提示"
    assert all(core.JUDGE_PRODUCT_ROLE_KEY not in r and "product_role" not in r for r in rows.values())
    assert "index 是 0、1 的 2 篇带了 layer_hint" in out["next_step"]
    assert core.JUDGE_PRODUCT_ROLE_KEY not in json.dumps(c.rows["batch_metrics"][0], ensure_ascii=False, default=str)


def test_no_layers_no_hints_and_the_judge_block_is_unchanged(judged):
    roles, _sent = judged
    c, _keys = _judged_client()
    drafts = distinct_drafts(2)
    for d in drafts:
        roles[d["title"]] = "未出现"
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    for r in out["judge"]["results"]:
        assert "layer_hint" not in r and core.JUDGE_PRODUCT_ROLE_KEY not in r
    assert "layer_hint" not in out["next_step"]


def test_layer_vocab_maps_both_directions():
    assert {vocab.LAYER_DIRECTION[c] for c in ("P", "J")} == {"产品向"}
    assert {vocab.LAYER_DIRECTION[c] for c in ("T", "Q")} == {"流量向"}
    assert set(vocab.DIRECTION_RULE_NOTE) == set(core.DIRECTION_TAGS)


# ══════════════════════════════════════════════════════════════════════
# H · 改稿的贴型跟着原来那篇走, 不跟着同一个 key 后来重发的那张(Codex review on aw#141)
# ══════════════════════════════════════════════════════════════════════

def test_a_revision_keeps_the_layer_of_the_card_it_was_written_from(monkeypatch):
    """原来那篇按产品贴的卡写、存档销了牌; 后来写手点名再写一次这个角度(repeat_of), 这次说是流量贴 —— 台账多一行 ly=T,
    抽得更晚。改原来那篇(带 replaces_version_id, 还带着原来的 angle_key)查规则要照产品贴查; 按重发那张写的新稿才是流量贴。"""
    from deskcore import priors
    from tests.test_delivery_revision import _embed
    c, keys = _client("P")
    c.rows["angle_ledger"][0]["drawn_at"] = "2026-10-01T00:00:00+00:00"
    drafts = distinct_drafts(2, seed=7)
    first = core.commit_drafts(c, PROJ, [{**drafts[0], "angle_key": keys[0]}], user_id=ME)
    assert first["consumed_angles"] == 1 and _by_draft(first) == {0: ["m-1", "m-3", "m-4"]}
    v1 = first["version_ids"][0]

    monkeypatch.setattr(priors, "get_profile", lambda *a, **k: priors.uniform_profile(PROJ))
    rep = core.draw_angles(c, PROJ, 1, user_id=ME, repeat_of=[keys[0]], layer="流量贴")
    assert rep["delivered"] == 1 and rep["angles"][0]["layer"] == "流量贴"
    assert store.ledger_layers(c, PROJ, [keys[0]]) == {keys[0]: "T"}, "按 key 找到的是重发那张"

    _embed(c)
    revised = {**drafts[0], "body": drafts[0]["body"] + "后来补了一句。", "angle_key": keys[0], "replaces_version_id": v1}
    out = core.commit_drafts(c, PROJ, [revised, {**drafts[1], "angle_key": keys[0]}], user_id=ME)
    assert out["written"] == 2 and out["replaced"]
    assert _by_draft(out) == {0: ["m-1", "m-3", "m-4"],        # 改原来那篇: 产品贴(产品向 + 通用)
                              1: ["m-2", "m-4"]}               # 按重发那张写的新稿: 流量贴(流量向 + 通用)


def test_draft_layers_reads_the_old_version_first_and_falls_back_to_the_key(monkeypatch):
    asked = []
    monkeypatch.setattr(store, "version_layers", lambda c, p, vids: asked.append(vids) or {"old-1": "P"})
    monkeypatch.setattr(store, "ledger_layers", lambda c, p, keys: {"k": "T"})
    drafts = [{"angle_key": "k"}, {"angle_key": "k"}, {"angle_key": "k"}, {}]
    got = core._draft_layers(None, PROJ, drafts, [0, 1, 2, 3], replaced={0: "old-1", 1: "old-2", 9: "old-9"})
    assert got == {0: "P", 1: "T", 2: "T"}, "旧版那条线找不到的(old-2)才按 key 找"
    assert asked == [["old-1", "old-2"]], "只查这次存进去的那几篇"
    monkeypatch.setattr(store, "version_layers", lambda *a: None)                # 读不到: 按 key
    assert core._draft_layers(None, PROJ, drafts, [0, 1, 2, 3], replaced={0: "old-1"}) == {0: "T", 1: "T", 2: "T"}
    asked.clear()
    monkeypatch.setattr(store, "version_layers", lambda c, p, vids: asked.append(vids) or {})
    assert core._draft_layers(None, PROJ, drafts, [0]) == {0: "T"} and asked == [], "没有改稿就不走旧版那条线"


ITEM = "dddddddd-0000-4000-8000-0000000000e1"
V_ORIG = "cccccccc-0000-4000-8000-0000000000e1"
V_REV = "cccccccc-0000-4000-8000-0000000000e2"


def _revision_client(*, rev_project=None):
    """一篇稿子两版: V_ORIG 按产品贴的卡写、销了牌; V_REV 是改稿重存的新版(不销牌, 指纹行记着同一个 key)。同一个 key
    后来被 repeat_of 换成流量贴重发过(台账多一行 ly=T, 抽得更晚、还没用)。"""
    mems = [R_PROD, R_TRAF, R_ALL, R_PROD_CODE, R_TRAF_CODE]
    comp = [stored(R_PROD, Q_PROD), stored(R_TRAF, Q_TRAF), stored(R_ALL, Q_ALL),
            stored(R_PROD_CODE, _code(R_PROD_CODE, "示例价格")), stored(R_TRAF_CODE, _code(R_TRAF_CODE, "示例渠道"))]
    vs = []
    for num, vid in enumerate((V_ORIG, V_REV), start=1):
        v = version(vid, body=BODY)
        pid = rev_project if (vid == V_REV and rev_project) else CPROJ
        v.update(item_id=ITEM, version_num=num, items={"id": ITEM, "batches": {"project_id": pid}})
        vs.append(v)
    c = make_client(mems, comp, versions=vs)
    dims = dict(CELLS[0])
    k = angle_key(dims)
    c.rows["angle_ledger"] = [
        {"id": "orig", "project_id": CPROJ, "angle_key": k, "dims": {**dims, "_draw": {"src": "w", "ly": "P"}},
         "drawn_by": ME, "drawn_at": "2026-10-01T00:00:00+00:00", "consumed_version_id": V_ORIG,
         "consumed_at": "2026-10-02T00:00:00+00:00"},
        {"id": "reissue", "project_id": CPROJ, "angle_key": k,
         "dims": {**dims, "_draw": {"src": "rep", "of": k, "ly": "T"}},
         "drawn_by": ME, "drawn_at": "2026-11-02T00:00:00+00:00", "consumed_version_id": None, "consumed_at": None}]
    c.rows["draft_fingerprints"] = [{"project_id": CPROJ, "version_id": V_REV, "angle_key": k,
                                     "created_at": "2026-11-02T00:00:00+00:00"}]
    return c, k


def test_version_layers_follows_the_item_for_a_revision_that_never_consumed_a_card():
    c, k = _revision_client()
    assert store.ledger_layers(c, CPROJ, [k]) == {k: "T"}, "反证: 按指纹行的 key 找到的是重发那张"
    assert store.version_layers(c, CPROJ, [V_REV]) == {V_REV: "P"}
    assert store.version_layers(c, CPROJ, [V_ORIG, V_REV]) == {V_ORIG: "P", V_REV: "P"}
    c.calls.clear()
    assert store.version_layers(c, CPROJ, [V_ORIG]) == {V_ORIG: "P"}
    assert not [q for q in c.calls if q["table"] == "versions"], "自己销过牌的不查同一篇别的版本"
    # 别的项目的版本查不出 item(同 items_for_versions): 不走这一步, 照原来按指纹行的 key 找
    c2, _k = _revision_client(rev_project="aaaaaaaa-0000-0000-0000-00000000ffff")
    assert store.version_layers(c2, CPROJ, [V_REV]) == {V_REV: "T"}


def test_version_layers_lineage_read_failure_is_fail_soft(monkeypatch):
    c, _k = _revision_client()

    def boom(*a, **k):
        raise RuntimeError("statement timeout")
    monkeypatch.setattr(store, "items_for_versions", boom)
    assert store.version_layers(c, CPROJ, [V_REV]) is None
    assert core._version_layers(c, CPROJ, [V_REV]) == {}


def test_check_rules_on_a_revised_version_uses_the_original_cards_layer(wired):
    c, _k = _revision_client()
    out = core.check_rules(c, CPROJ, [V_REV], user_id=ME)
    qid = {r["id"]: q["questions"][0]["qid"] for r, q in ((R_PROD, Q_PROD), (R_TRAF, Q_TRAF), (R_ALL, Q_ALL))}
    assert _banks(wired) == {V_REV: sorted([qid[R_PROD["id"]], qid[R_ALL["id"]]])}, "产品贴: 产品向 + 通用"
    row = out["versions"][0]
    assert row["layer"] == "产品贴"
    assert _rule_ids(row) == sorted([R_PROD["id"], R_ALL["id"], R_PROD_CODE["id"]])
