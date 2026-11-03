"""贴型(content layers): 写手说了「写 N 篇 X 贴」才给卡标贴型; 带方向的规则跟着每篇卡的贴型走; 参考帖按贴型优先。

数据全是合成的(项目、台账、Profile、参考帖的 ref)。钉的是:
  · 金标: 没点名贴型时 v2 一批和 c5ca36c 一字不差(候选表、台账 _draw、重发的行、牌、卡面; rng 消耗次序也一样);
  · layer 参数: 字符串 = 全部, dict = 各几篇、其余不标; 别名; 写错了报 layer_invalid、什么都不写; pin 里的「方向」归进
    layer、不当维度; layer 不进 angle_key / 去重;
  · 台账 _draw.ly(P/T/Q/J), 重发带原来那张的 ly, ledger_dims_for 带出来; _draw ≤ 300 字;
  · 卡面那一行(四种贴型各一句, 没贴型不加行);
  · 参考帖: 产品贴 / 精品贴优先产品出场的外部帖、流量贴 / 提问贴优先没出场的, 起量帖优先同一族(TV 有 content_layer 时);
    只是优先、不筛, 记的 p 和真实概率一致。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from deskcore import core
from tests import draw_v2_golden as G

GOLDEN_V2 = json.loads((Path(__file__).parent / "golden" / "draw_v2_c5ca36c.json").read_text(encoding="utf-8"))


# ══════════════════════════════════════════════════════════════════════
# 金标: 没点名贴型的 v2 和 c5ca36c 一字不差
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("entry", GOLDEN_V2["cases"],
                         ids=lambda e: f"{e['case']['kind']}-seed{e['case']['seed']}-n{e['case']['n']}")
def test_v2_without_a_layer_reproduces_the_c5ca36c_draw(entry, monkeypatch):
    monkeypatch.delenv(core.DRAW_POLICY_ENV, raising=False)
    assert G.capture(entry["case"], monkeypatch) == entry["expect"]


def test_the_v2_golden_can_fail(monkeypatch):
    """反证: 换一个 seed, 候选表和牌都不一样 —— 上面那条不是怎么发都绿。"""
    monkeypatch.delenv(core.DRAW_POLICY_ENV, raising=False)
    entry = GOLDEN_V2["cases"][2]
    got = G.capture(dict(entry["case"], seed=entry["case"]["seed"] + 100), monkeypatch)
    assert got["keys_sha256"] != entry["expect"]["keys_sha256"]
    assert got["angles"] != entry["expect"]["angles"]


# ══════════════════════════════════════════════════════════════════════
# A · layer 参数
# ══════════════════════════════════════════════════════════════════════

import copy                                                    # noqa: E402
import random                                                  # noqa: E402

from deskcore import drawpolicy as dp, priors, store, tools, vocab   # noqa: E402
from deskcore.fingerprint import angle_key                      # noqa: E402
from tests.fakes import FakeClient                              # noqa: E402

PID, ME = G.PID, G.ME
LINES = {code: "  · " + vocab.LAYER_LINE[code] for code in vocab.LAYERS}


def _run(monkeypatch, *, kind="run", seed=3, n=6, ledger=(), prof=None, **kw):
    """按 G 的合成 Profile 发一批(时间钉在 G.FIXED, 臂钉成 patterns), 回 (回包, 假 RPC 那个世界)。"""
    prof = prof if prof is not None else G.profile(kind)
    monkeypatch.delenv(core.DRAW_POLICY_ENV, raising=False)
    monkeypatch.setattr(priors, "get_profile", lambda *a, **k: copy.deepcopy(prof))
    monkeypatch.setattr(core, "flywheel_arm", lambda *a, **k: "patterns")
    monkeypatch.setattr(dp, "_utcnow", lambda: G.FIXED)
    w = G._World(list(ledger))
    return core.draw_angles(w.client, PID, n, user_id=ME, seed=seed, **kw), w


def _ledger_ly(w) -> list:
    return [r["dims"].get("_draw", {}).get("ly") for r in w.client.rows["angle_ledger"]]


def test_a_single_layer_marks_every_card_and_its_ledger_row(monkeypatch):
    out, w = _run(monkeypatch, layer="产品贴")
    assert out["delivered"] == 6
    assert [a["layer"] for a in out["angles"]] == ["产品贴"] * 6
    assert _ledger_ly(w) == ["P"] * 6
    assert out["prompt_block"].count(LINES["P"]) == 6
    assert all(set(a["dims"]) == set(dp.DIMS4) for a in out["angles"]), "ly 不进回给模型的 dims"
    assert '"_draw"' not in json.dumps(out, ensure_ascii=False) and '"ly"' not in json.dumps(out)


@pytest.mark.parametrize("said,code", [("产品向", "P"), ("直出贴", "P"), ("直给", "P"), ("好物分享-产品贴", "P"),
                                       ("流量向", "T"), ("流量贴", "T"), ("提问", "Q"), ("提问贴", "Q"),
                                       ("精品", "J"), ("精品贴", "J"), ("p", "P"), (" T ", "T")])
def test_layer_aliases(said, code):
    assert dp.normalize_layer(said, 5) == (code, None)


def test_counts_mark_that_many_and_leave_the_rest_unmarked(monkeypatch):
    out, w = _run(monkeypatch, n=7, layer={"产品贴": 3, "流量向": 2})
    got = [a.get("layer") for a in out["angles"]]
    assert sorted(x for x in got if x) == ["产品贴"] * 3 + ["流量贴"] * 2 and got.count(None) == 2
    assert sorted((x or "-") for x in _ledger_ly(w)) == ["-", "-", "P", "P", "P", "T", "T"]
    block = out["prompt_block"]
    assert block.count(LINES["P"]) == 3 and block.count(LINES["T"]) == 2 and LINES["Q"] not in block


@pytest.mark.parametrize("bad,why", [("软广贴", "不是可以标的贴型"), ({"产品贴": 4, "流量贴": 3}, "比这次要的 6 篇多"),
                                     ({"测评贴": 1}, "不是可以标的贴型"), ({"产品贴": -1}, "0 或正整数"),
                                     ({"产品贴": 1.5}, "0 或正整数"), (["产品贴"], "layer 要写成")])
def test_a_bad_layer_is_an_error_and_writes_nothing(monkeypatch, bad, why):
    out, w = _run(monkeypatch, layer=bad)
    assert out["error"] == "layer_invalid" and why in out["message"]
    assert out["allowed"] == ["产品贴", "流量贴", "提问贴", "精品贴"]
    assert out["angles"] == [] and out["delivered"] == 0 and out["prompt_block"] == ""
    assert w.seen == [] and w.client.rows["angle_ledger"] == [], "什么都不写"


@pytest.mark.parametrize("key", ["方向", "layer", "direction", "贴型"])
def test_direction_in_pin_becomes_the_layer_not_a_dimension(monkeypatch, key):
    out, w = _run(monkeypatch, pin={key: "流量向", "标题句式": "疑问句"})
    assert [a["layer"] for a in out["angles"]] == ["流量贴"] * 6
    assert all(a["dims"]["title_structure"] == "疑问句" for a in out["angles"])
    for r in w.client.rows["angle_ledger"]:
        assert r["dims"]["_draw"]["pin"] == ["title_structure"] and r["dims"]["_draw"]["ly"] == "T"
    only, _w = _run(monkeypatch, pin={key: "提问贴"})
    assert [a["layer"] for a in only["angles"]] == ["提问贴"] * 6
    assert all("pin" not in r["dims"]["_draw"] for r in _w.client.rows["angle_ledger"]), "只写了方向 = 没有 pin"


def test_pin_direction_and_layer_must_agree(monkeypatch):
    assert _run(monkeypatch, pin={"方向": "流量贴"}, layer="流量向")[0]["delivered"] == 6
    out, w = _run(monkeypatch, pin={"方向": "流量贴"}, layer="产品贴")
    assert out["error"] == "layer_invalid" and "不一样" in out["message"] and not w.client.rows["angle_ledger"]


def test_layer_never_enters_the_key_or_the_dedup(monkeypatch):
    """只有 w / x 槽的 Profile(没有参考帖可挑): 同一个 seed 标不标贴型, 交给 RPC 的候选 key 一个不差, dims 只多 _draw.ly。"""
    prof = G.profile("run")
    prof.pop("ext")
    prof["hits"] = []
    plain, w0 = _run(monkeypatch, prof=prof, n=8, seed=11)
    marked, w1 = _run(monkeypatch, prof=prof, n=8, seed=11, layer={"精品贴": 5})
    assert [c["angle_key"] for c in w1.seen[0]] == [c["angle_key"] for c in w0.seen[0]]
    for a, b in zip(w0.seen[0], w1.seen[0]):
        assert {**b["dims"]["_draw"]} == {**a["dims"]["_draw"], **({"ly": "J"} if "ly" in b["dims"]["_draw"] else {})}
        assert angle_key(b["dims"]) == b["angle_key"]
    assert [a["angle_key"] for a in marked["angles"]] == [a["angle_key"] for a in plain["angles"]]
    assert sum(1 for c in w1.seen[0][:8] if c["dims"]["_draw"].get("ly") == "J") == 5
    # 备牌跟着它那个槽位(每个槽位至多 alternates 张); 尾巴不知道顶的是哪个槽位, 混着的(5 篇精品 + 3 篇不标)不带
    marked_backups = [c for c in w1.seen[0][8:] if "ly" in c["dims"]["_draw"]]
    assert 0 < len(marked_backups) <= 5 * dp.PARAMS["alternates"]


def test_the_kill_switch_marks_layers_without_touching_the_v1_draw(monkeypatch):
    from tests.test_draw_angles_v2 import GOLDEN as V1_GOLDEN
    case = V1_GOLDEN["cases"][3]
    monkeypatch.setenv(core.DRAW_POLICY_ENV, "v1")
    monkeypatch.setattr(core, "flywheel_arm", lambda *a, **k: "patterns")
    w = G._World()
    out = core.draw_angles(w.client, PID, case["n"], user_id=ME, seed=case["seed"], layer="精品贴")
    keys = [c["angle_key"] for c in w.seen[0]]
    import hashlib
    assert hashlib.sha256("\n".join(keys).encode()).hexdigest() == case["candidates_sha256"]
    assert [a["angle_key"] for a in out["angles"]] == [x[0] for x in case["angles"]]
    assert all(a["layer"] == "精品贴" for a in out["angles"]) and all(c["dims"]["_draw"]["ly"] == "J" for c in w.seen[0])


def test_repeat_carries_the_original_cards_layer(monkeypatch):
    rows = G.ledger_rows()
    rows[1]["dims"]["_draw"]["ly"] = "T"                            # 消耗过的那张原来是流量贴
    out, w = _run(monkeypatch, n=4, ledger=rows, repeat_of=[rows[1]["angle_key"]])
    rep = [r for r in w.client.rows["angle_ledger"] if r["dims"]["_draw"].get("src") == "rep"]
    assert len(rep) == 1 and rep[0]["dims"]["_draw"]["ly"] == "T" and rep[0]["dims"]["_draw"]["of"] == rows[1]["angle_key"]
    assert out["angles"][0]["layer"] == "流量贴" and [a.get("layer") for a in out["angles"][1:]] == [None] * 3
    # 「这批都是产品贴」: 写手现在说的比原来那张卡上的新
    out2, w2 = _run(monkeypatch, n=4, ledger=rows, repeat_of=[rows[1]["angle_key"]], layer="产品贴")
    assert [a["layer"] for a in out2["angles"]] == ["产品贴"] * 4
    # 各几篇: 重发的那张先抵账(要 2 篇流量贴, 重发的是流量贴, 新抽的里只再标 1 篇)
    out3, _w3 = _run(monkeypatch, n=4, ledger=rows, repeat_of=[rows[1]["angle_key"]], layer={"流量贴": 2})
    assert [a.get("layer") for a in out3["angles"]].count("流量贴") == 2


def test_slot_layers_is_positional_and_never_uses_randomness():
    assert dp.slot_layers(None, 3) == [None] * 3
    assert dp.slot_layers("Q", 2) == ["Q", "Q"]
    assert dp.slot_layers({"P": 2, "T": 1}, 5) == ["P", "P", "T", None, None]
    assert dp.slot_layers({"P": 2, "T": 1}, 2, rep_layers=["P"]) == ["P", "T"]
    assert dp.slot_layers({"T": 4}, 2) == ["T", "T"]
    assert dp.common_layer(["P", "P"]) == "P" and dp.common_layer(["P", None]) is None and dp.common_layer([]) is None
    assert dp.split_pin_layer({"标题句式": "疑问句"})[1] is None
    pin = {"标题句式": "疑问句"}
    assert dp.split_pin_layer(pin)[0] is pin, "没有方向那一项时 pin 原样回"


def test_the_tool_passes_layer_through_and_tells_the_model_when_to_use_it(monkeypatch):
    seen = {}
    monkeypatch.setattr(core, "sb", lambda: None)
    monkeypatch.setattr(core, "draw_angles", lambda client, pid, n, **kw: seen.update(kw) or
                        {"error": "layer_invalid", "message": "x", "angles": [], "requested": n, "delivered": 0})
    tools.draw_angles(PID, 5, layer={"产品贴": 3, "流量贴": 2}, _user_id=ME)
    assert seen["layer"] == {"产品贴": 3, "流量贴": 2}
    doc = tools.draw_angles.__doc__
    for must in ("layer", "这批都是产品贴", "写 5 篇流量贴", '{"产品贴": 3, "流量贴": 2}', "不要为它去问用户", "layer_invalid"):
        assert must in doc, must


# ══════════════════════════════════════════════════════════════════════
# B · 台账: _draw.ly、300 字的预算、ledger_dims_for
# ══════════════════════════════════════════════════════════════════════

def _worst(kind: str) -> dict:
    """_draw 在两种最长的牌上, 每个可选键都取最长的值(build_candidates 写得出来的组合: 点名的牌没有参考帖, 带参考帖的
    牌没有 pin; ps 是 uniform 时整批退回 v1, 不会出现在 v2 的牌上)。"""
    pv = [0.123, 0.123, 0.123, 0.123]
    base = {"v": 2, "pol": "v2", "b": "f" * 12, "pn": 30, "i": 30, "sp": 0.25, "p": 1.23e-05, "pu": 1.23e-05,
            "pv": pv, "st": "warm", "ps": "stale", "dd": "rpc_only", "pp": "stale", "fa": "patterns"}
    if kind == "pin":
        return {**base, "pk": {"pin": 30}, "src": "pin", "sp": 1.0, "pin": list(dp.DIMS4), "l1": "relaxed"}
    return {**base, "pk": {"w": 18, "x": 6, "e": 4, "h": 3}, "src": "e", "g": 1, "ref": "note:" + "f" * 24, "reuse": 1}


def test_the_draw_block_budget_with_layers():
    """§5.1 的 300 字预算: 预算那条用例(test_drawpolicy 的 warm + 外部池 + 起量帖, n = 1 / 5 / 30)标了贴型照样在 300 字以内。
    最坏情形(每个可选键都取最长, 见 _worst)本来就超过 300(点名四个维度的牌 359、带参考帖的牌 333 字), 贴型只多 11 个字
    (``, "ly": "P"``); 没点名贴型的牌一个字不多(金标钉着)。"""
    from tests.test_drawpolicy import ARCS, concentrated as conc, ext_block, hit_entry as hit
    prof = conc(stage="warm", ext=ext_block(6), hits=[hit(i, arc=ARCS[i]) for i in range(4)])
    for n in (1, 5, 30):
        for spec in ("精品贴", {"产品贴": n}):
            rng = random.Random(3)
            plan = dp.slot_plan(n, prof, rng)
            for s_, ly in zip(plan["slots"], dp.slot_layers(dp.normalize_layer(spec, n)[0], n)):
                s_["ly"] = ly
            cands, _m = dp.build_candidates(prof, dp.ledger_state([], G.FIXED), plan, rng, extra={"fa": "explore"})
            assert cands[0]["dims"]["_draw"]["ly"] in ("J", "P")
            assert max(len(json.dumps(c["dims"]["_draw"], ensure_ascii=False)) for c in cands) <= 300
    for kind, before in (("pin", 359), ("ref", 333)):
        d = _worst(kind)
        assert len(json.dumps(d, ensure_ascii=False)) == before
        assert len(json.dumps({**d, "ly": "P"}, ensure_ascii=False)) == before + 11


def test_ledger_dims_for_carries_the_layer_under_the_draw_key_only():
    four = {"emotional_lever": "焦虑撬动", "human_truth_archetype": "健康焦虑", "content_format": "提问求助",
            "title_structure": "疑问句"}
    k = angle_key(four)
    rows = [{"id": "a", "project_id": PID, "angle_key": k, "dims": {**four, "_draw": {"src": "w", "ly": "Q"}},
             "drawn_at": G._ago(2)}]
    got = store.ledger_dims_for(FakeClient(rows={"angle_ledger": rows}), PID, k)
    assert got == {**four, "_draw": {"ly": "Q"}} and store.public_dims(got) == four
    rows[0]["dims"]["_draw"]["ly"] = "X"                             # 认不出的代号不带
    assert store.ledger_dims_for(FakeClient(rows={"angle_ledger": rows}), PID, k) == four


def test_ledger_layers_takes_the_latest_row_per_key_and_fails_soft(caplog):
    four = {"emotional_lever": "焦虑撬动", "human_truth_archetype": "健康焦虑", "content_format": "提问求助",
            "title_structure": "疑问句"}
    k1, k2 = angle_key(four), angle_key({**four, "title_structure": "数字清单"})
    rows = [{"id": "1", "project_id": PID, "angle_key": k1, "dims": {**four, "_draw": {"ly": "T"}}, "drawn_at": G._ago(9)},
            {"id": "2", "project_id": PID, "angle_key": k1, "dims": {**four, "_draw": {"ly": "P"}}, "drawn_at": G._ago(1)},
            {"id": "3", "project_id": PID, "angle_key": k2, "dims": dict(four), "drawn_at": G._ago(1)},
            {"id": "4", "project_id": "other", "angle_key": k2, "dims": {**four, "_draw": {"ly": "J"}},
             "drawn_at": G._ago(1)}]
    assert store.ledger_layers(FakeClient(rows={"angle_ledger": rows}), PID, [k1, k2, "编的 key"]) == {k1: "P"}

    class Boom(FakeClient):
        def _execute(self, q):
            raise RuntimeError("statement timeout")
    assert store.ledger_layers(Boom(), PID, [k1]) is None


# ══════════════════════════════════════════════════════════════════════
# C · 卡面那一行
# ══════════════════════════════════════════════════════════════════════

def _card(**over):
    from tests.test_angle_render import _card as base
    return base(**over)


@pytest.mark.parametrize("name,code", [("产品贴", "P"), ("流量贴", "T"), ("提问贴", "Q"), ("精品贴", "J")])
def test_each_layer_adds_exactly_its_line_under_the_slot_line(name, code):
    plain = core.render_angles_block([_card()])
    marked = core.render_angles_block([_card(layer=name)])
    lines = marked.splitlines()
    slot_at = next(i for i, ln in enumerate(lines) if ln.startswith("第1篇 [angle_key="))
    assert lines[slot_at + 1] == LINES[code]
    rest = lines[:slot_at + 1] + lines[slot_at + 2:]
    assert "\n".join(rest) == plain, "除了那一行一个字不变(提问求助的大白话四种贴型都一样)"
    assert vocab.LAYER_LINE[code].startswith(f"这篇是{name}：")


def test_exact_layer_texts():
    assert vocab.LAYER_LINE == {
        "P": "这篇是产品贴：先打一个痛点，再让本项目的产品在正文里出场、当成解决办法；一篇只讲一个点。",
        "T": "这篇是流量贴：冲着切口写，产品别硬插，可以融进场景；品牌名按本项目规则（多放评论区）。",
        "Q": "这篇是提问贴：用读者做决定时真会问的问题开头；品牌名按本项目规则（多放评论区的回答里）。",
        "J": "这篇是精品贴：产品融进切口，讲自己的感受，不写『分享好物、推荐』，文字像普通人写的。",
    }


@pytest.mark.parametrize("fmt", ["场景植入", "直给推荐"])
def test_traffic_cards_get_a_format_gloss_that_does_not_push_the_product(fmt):
    dims = {**_card()["dims"], "content_format": fmt}
    for name, want in (("流量贴", vocab.PLAIN_FORMAT_TRAFFIC[fmt]), ("提问贴", vocab.PLAIN_FORMAT_TRAFFIC[fmt]),
                       ("产品贴", vocab.PLAIN_FORMAT[fmt]), ("精品贴", vocab.PLAIN_FORMAT[fmt]), (None, vocab.PLAIN_FORMAT[fmt])):
        card = _card(dims=dims, **({"layer": name} if name else {}))
        assert f"  · 内容形式：{fmt}——{want}" in core.render_angles_block([card]), name
    assert "硬插" in vocab.PLAIN_FORMAT_TRAFFIC[fmt] and fmt not in vocab.PLAIN_FORMAT_TRAFFIC[fmt]
    assert set(vocab.PLAIN_FORMAT_TRAFFIC) <= set(vocab.CONTENT_FORMATS)


def test_no_layer_no_line_and_the_render_still_has_no_source_parameter():
    import inspect
    block = core.render_angles_block([_card(), _card(slot=2, angle_key="1" * 20)])
    assert "这篇是" not in block
    assert list(inspect.signature(core.render_angles_block).parameters) == ["angles", "status"]


# ══════════════════════════════════════════════════════════════════════
# E · 参考帖按贴型优先(只优先, 不筛; 记的 p 和真实概率一致)
# ══════════════════════════════════════════════════════════════════════

from tests.test_drawpolicy import NO_TAIL, T, cell, concentrated, empty_state, ext_entry, hit_entry  # noqa: E402


def _ext_pool_with_roles():
    pool = []
    for i in range(30):
        e = dict(ext_entry(i, title="数字清单" if i % 2 else "场景直述"), w=1.0 + (i % 7) / 3)
        e["prod"] = 1 if i % 3 == 0 else 0
        pool.append(e)
    return pool


def _one_slot(src, f, ly=None):
    s_ = {"i": 1, "src": src, "f": f, "sp": 0.2, "g": 0}
    if ly:
        s_["ly"] = ly
    return {"n": 1, "stage": "run", "shares": {src: 0.2}, "counts": {src: 1}, "formats": {f: 1}, "pin": None, "slots": [s_]}


@pytest.mark.parametrize("ly,want_prod", [("P", 1), ("J", 1), ("T", 0), ("Q", 0)])
def test_external_slot_prefers_the_matching_product_role_and_logs_the_right_p(ly, want_prod):
    pool = _ext_pool_with_roles()
    prof = concentrated(ext={"category": "运动补给", "state": "ok", "ok": True, "eligible": len(pool), "pool": pool})
    st = empty_state()
    pref = [e for e in pool if e["prod"] == want_prod]
    tot = sum(e["w"] for e in pref)
    cond = {t: dp.slot_distribution(prof, st, {"f": "提问求助", "src": "e", "t_only": t}) for t in ("数字清单", "场景直述")}
    by_ref = {e["ref"]: e for e in pool}
    for seed in range(10):
        cands, _meta = dp.build_candidates(prof, st, _one_slot("e", "提问求助", ly), random.Random(seed), params=NO_TAIL)
        d = cands[0]["dims"]["_draw"]
        assert d["src"] == "e" and d["ly"] == ly and by_ref[d["ref"]]["prod"] == want_prod
        c = dp.cell_of(cands[0]["dims"])
        want = sum(e["w"] / tot * cond[e[T]].get(c, (0.0, 0.0))[0] for e in pref)
        assert d["p"] == pytest.approx(float(f"{want:.3g}"), rel=1e-6)


def test_external_preference_falls_back_when_no_reference_matches():
    pool = [dict(e, prod=0) for e in _ext_pool_with_roles()]
    prof = concentrated(ext={"category": "运动补给", "state": "ok", "ok": True, "eligible": len(pool), "pool": pool})
    cands, _m = dp.build_candidates(prof, empty_state(), _one_slot("e", "提问求助", "P"), random.Random(2), params=NO_TAIL)
    assert cands[0]["dims"]["_draw"]["src"] == "e", "没有产品出场的参考帖也照样用, 只是优先、不筛"
    unknown = [{k: v for k, v in e.items() if k != "prod"} for e in pool]       # product_role 读不到: 不分档
    prof2 = concentrated(ext={"category": "运动补给", "state": "ok", "ok": True, "eligible": len(unknown), "pool": unknown})
    a, _ = dp.build_candidates(prof2, empty_state(), _one_slot("e", "提问求助", "T"), random.Random(5), params=NO_TAIL)
    b, _ = dp.build_candidates(prof2, empty_state(), _one_slot("e", "提问求助"), random.Random(5), params=NO_TAIL)
    assert [c["angle_key"] for c in a] == [c["angle_key"] for c in b]
    assert [{k: v for k, v in c["dims"]["_draw"].items() if k != "ly"} for c in a] == [c["dims"]["_draw"] for c in b]


def test_hit_slot_prefers_the_same_family_and_logs_the_right_p():
    """三篇起量帖是同一个格子(流量贴), 一篇是另一个格子(产品贴), 形式都和槽位一样: 产品贴的卡只用那一篇(p = 1),
    流量贴的卡只用那三篇(p = 1), 不标贴型的照原来 3/4、1/4。"""
    other = dict(hit_entry(9, lev="好奇驱动", arc="经济焦虑", ttl="场景直述"), ly="P")
    hits = [dict(hit_entry(i), ly="T") for i in range(3)] + [other]
    prof = concentrated(hits=hits)
    oc = ("好奇驱动", "经济焦虑", "提问求助", "场景直述")
    for ly, want in (("P", {oc: 1.0}), ("J", {oc: 1.0}), ("T", {cell(): 1.0}), ("Q", {cell(): 1.0}),
                     (None, {cell(): 0.75, oc: 0.25})):
        got = {}
        for seed in range(30):
            cands, _m = dp.build_candidates(prof, empty_state(), _one_slot("h", "提问求助", ly), random.Random(seed),
                                            params=NO_TAIL)
            got[dp.cell_of(cands[0]["dims"])] = cands[0]["dims"]["_draw"]["p"]
        assert got == want, ly


def test_hit_preference_keeps_the_format_tier_first():
    """形式一样且同一族 → 形式一样 → 同一族: 同一族的只有别的形式时, 先用形式一样的那篇。"""
    same_fmt = dict(hit_entry(1), ly="T")
    fam_only = dict(hit_entry(2, fmt="场景植入", lev="好奇驱动", arc="经济焦虑", ttl="场景直述"), ly="P")
    prof = concentrated(hits=[same_fmt, fam_only])
    for seed in range(10):
        cands, _m = dp.build_candidates(prof, empty_state(), _one_slot("h", "提问求助", "P"), random.Random(seed),
                                        params=NO_TAIL)
        assert cands[0]["dims"]["_draw"]["ref"] == same_fmt["ref"]


def test_pick_reference_tiers():
    pool = [{"ref": f"r{i}", "w": 1.0, "k": i % 3} for i in range(9)]
    rng = random.Random(0)
    got = {dp.pick_reference(pool, rng, cooldown_days=1, prefer=(lambda e: e["k"] == 5, lambda e: e["k"] == 2))[0]["k"]
           for _ in range(20)}
    assert got == {2}
    assert dp._preferred(pool, None) is pool and dp._preferred(pool, (lambda e: False,)) is pool


# ══════════════════════════════════════════════════════════════════════
# E / F · 先验: 外部池的 product_role、起量帖的 content_layer、投流事件
# ══════════════════════════════════════════════════════════════════════

from tests.test_priors import NOW as PNOW, ext_row, note  # noqa: E402


def test_external_pool_carries_prod_only_when_jev_answered():
    rows = [ext_row(posted_days=5 + i, comments=200 + 37 * i) for i in range(14)]
    roles = ["主角", "解决方案", "未出现", "只暗示", "顺带一提", "看不出", None]
    answers = {r["note_id"]: ({"product_role": roles[i % 7]} if roles[i % 7] else {}) for i, r in enumerate(rows)}
    out = priors.external_pool(rows, answers, category="运动补给", now=PNOW, registered=())
    base = priors.external_pool(rows, {}, category="运动补给", now=PNOW, registered=())
    assert out["pool"] and [e["ref"] for e in out["pool"]] == [e["ref"] for e in base["pool"]], "只多一个字段, 池子不变"
    assert out["eligible"] == base["eligible"] and out["age_ok"] == base["age_ok"]
    for e in out["pool"]:
        role = answers[e["ref"][4:]].get("product_role")
        want = 1 if role in ("主角", "解决方案") else 0 if role in ("未出现", "只暗示", "顺带一提") else None
        assert e.get("prod") == want and (want is not None or "prod" not in e)
    assert all("prod" not in e for e in base["pool"])
    assert "product_role" in priors.EXT_QIDS


def _hit_rows():
    rows = [note("AAA_phase2", age_days=3 + i % 15, comments=i, lever="好奇驱动") for i in range(30)]
    ans = {r["note_id"]: {"opening_type": "具体事件", "ending_asks_reader": "是"} for r in rows}
    return rows, ans


def test_hits_carry_the_tv_content_layer_when_it_is_readable():
    rows, ans = _hit_rows()
    plain = priors.project_aggregate(rows, ans, tv_ids=["AAA_phase2"], now=PNOW)
    assert len(plain["hits"]) == 3 and all("ly" not in h for h in plain["hits"])
    layers = {h["ref"][5:]: vocab.layer_code(v) for h, v in zip(plain["hits"], ("流量贴", "P", "精品"))}
    got = priors.project_aggregate(rows, ans, tv_ids=["AAA_phase2"], now=PNOW, layers=layers)
    assert [h["ref"] for h in got["hits"]] == [h["ref"] for h in plain["hits"]]
    assert [h["ly"] for h in got["hits"]] == ["T", "P", "J"]
    assert {k: v for k, v in got.items() if k != "hits"} == {k: v for k, v in plain.items() if k != "hits"}


def test_boosted_posts_are_not_clean_labels_or_hits():
    rows, ans = _hit_rows()
    plain = priors.project_aggregate(rows, ans, tv_ids=["AAA_phase2"], now=PNOW)
    top = plain["hits"][0]["ref"][5:]
    got = priors.project_aggregate(rows, ans, tv_ids=["AAA_phase2"], now=PNOW, boosted=frozenset({top}))
    assert top not in {h["ref"][5:] for h in got["hits"]}
    assert got["working"]["resolved"] == plain["working"]["resolved"] - 1
    row = next(r for r in rows if r["note_id"] == top)
    assert priors.is_clean(row) and not priors.is_clean(row, frozenset({top}))
    assert priors.label(row, has_cc=True, now=PNOW, boosted=frozenset({top})) is None
    g0 = priors.build_global(rows, {}, {}, now=PNOW)
    g1 = priors.build_global(rows, {}, {}, now=PNOW, boosted=frozenset({top}))
    assert g1["projects"]["AAA_phase2"]["label"]["n"] == g0["projects"]["AAA_phase2"]["label"]["n"] - 1
    assert g1["projects"]["AAA_phase2"]["mix"] == g0["projects"]["AAA_phase2"]["mix"], "配比照算(发了就是发了)"


def _tv_world(*, layers_column=True, boost_view=True):
    rows, _ans = _hit_rows()
    for r in rows:
        r["content_layer"] = "流量贴"
    missing = {} if layers_column else {"truth_vault.notes": {"content_layer"}}
    if not boost_view:
        missing["truth_vault.v_paid_boost_events"] = {"note_id"}
    c = FakeClient(rows={"truth_vault.notes": rows, "truth_vault.v_paid_boost_events": [
                             {"note_id": rows[29]["note_id"], "project_id": "AAA_phase2"}],
                         "truth_vault.projects": [{"project_id": "AAA_phase2", "category": "运动补给"}],
                         "tv_project_map": [{"tv_project_id": "AAA_phase2", "project_id": PID}],
                         "projects": [{"id": PID, "name": "示例项目", "brand": "示例品牌"}]},
                   missing_columns=missing)
    return c, rows


def test_fetch_project_reads_layers_and_boosts_separately_and_survives_their_absence(caplog):
    c, rows = _tv_world()
    got = priors.fetch_project(c, PID, now=PNOW)
    assert got["hits"] and all(h["ly"] == "T" for h in got["hits"])
    assert rows[29]["note_id"] not in {h["ref"][5:] for h in got["hits"]}, "投流事件里的那篇不当起量帖"
    outcome_cols = [q["cols"] for q in c.calls if q["table"] == "truth_vault.notes" and "raw_content" in (q["cols"] or "")]
    assert outcome_cols and all("content_layer" not in cols for cols in outcome_cols)
    assert "content_layer" not in store.TV_OUTCOME_COLS
    # 列还不在 / 视图读不到: 不分档、照原来的口径, 项目块照样建成(投流事件是进程缓存, 换一个冷的进程看)
    priors.reset_cache()
    c2, rows2 = _tv_world(layers_column=False, boost_view=False)
    with caplog.at_level("INFO"):
        got2 = priors.fetch_project(c2, PID, now=PNOW)
    assert got2["hits"] and all("ly" not in h for h in got2["hits"])
    assert rows2[29]["note_id"] in {h["ref"][5:] for h in got2["hits"]}
    assert "v_paid_boost_events unreadable" in caplog.text
    assert any("content_layer not there yet" in r.getMessage() and r.levelname == "INFO" for r in caplog.records), \
        "列还没加是预期之内的, 记 INFO 不记 WARN"


def test_fetch_global_drops_boosted_posts_and_warns_when_the_view_is_unreadable(caplog):
    c, rows = _tv_world()
    g = priors.fetch_global(c, now=PNOW)
    priors.reset_cache()                                     # 投流事件是进程缓存: 换一个冷的进程看读不到的那种
    c2, _ = _tv_world(boost_view=False)
    with caplog.at_level("WARNING"):
        g2 = priors.fetch_global(c2, now=PNOW)
    assert g["projects"]["AAA_phase2"]["label"]["n"] == g2["projects"]["AAA_phase2"]["label"]["n"] - 1
    assert "v_paid_boost_events unreadable" in caplog.text
    assert store.tv_paid_boost_note_ids(c) == {rows[29]["note_id"]}


def _boost_reads(c) -> int:
    return sum(1 for q in c.calls if q["table"] == "truth_vault.v_paid_boost_events")


def test_paid_boost_view_is_read_once_per_process_and_shared_between_builds(monkeypatch):
    """复审: 视图每读一次 ~0.5 秒, 又只在 TV 夜里同步时变 —— 全局表、项目块共用一份进程缓存(PAID_BOOST_TTL_S 一刷);
    不满一页就是读完了, 不再多读一页空的。"""
    clock = [1_000_000.0]
    monkeypatch.setattr(priors, "_NOW", [lambda: clock[0]])
    c, rows = _tv_world()
    g = priors.fetch_global(c, now=PNOW)
    p = priors.fetch_project(c, PID, now=PNOW)
    p2 = priors.fetch_project(c, PID, now=PNOW)
    assert _boost_reads(c) == 1, "一页没满就停, 后面两次建块用缓存"
    assert rows[29]["note_id"] not in {h["ref"][5:] for h in p["hits"] + p2["hits"]}
    assert g["projects"]["AAA_phase2"]["label"]["n"] >= 1
    clock[0] += priors.PAID_BOOST_TTL_S + 1
    priors.fetch_project(c, PID, now=PNOW)
    assert _boost_reads(c) == 2, "过了 TTL 再读一次"


def test_paid_boost_cache_keeps_the_old_set_when_a_refresh_fails_and_waits_before_retrying(monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(priors, "_NOW", [lambda: clock[0]])
    c, rows = _tv_world()
    assert priors.paid_boosted(c) == frozenset({rows[29]["note_id"]})
    clock[0] += priors.PAID_BOOST_TTL_S + 1
    broken, _ = _tv_world(boost_view=False)
    assert priors.paid_boosted(broken) == frozenset({rows[29]["note_id"]}), "刷不成: 有旧的用旧的"
    assert priors.paid_boosted(broken) == frozenset({rows[29]["note_id"]})
    assert _boost_reads(broken) == 1, "刚失败过, PAID_BOOST_RETRY_S 之内不再去读"
    clock[0] += priors.PAID_BOOST_RETRY_S + 1
    priors.paid_boosted(broken)
    assert _boost_reads(broken) == 2

    priors.reset_cache()                                     # 冷的进程、一次都没读到过: 当没有(调用方照原来的口径)
    assert priors.paid_boosted(broken) is None
    assert priors._paid_boosted(store, broken) == frozenset()


def test_paid_boost_reader_stops_on_a_short_page(monkeypatch):
    monkeypatch.setattr(store, "PAGE", 2)
    ids = [{"note_id": f"n{i}"} for i in range(5)]
    c = FakeClient(rows={"truth_vault.v_paid_boost_events": ids})
    assert store.tv_paid_boost_note_ids(c) == {f"n{i}" for i in range(5)}
    assert _boost_reads(c) == 3, "2 + 2 + 1: 第三页没满就停"
    c2 = FakeClient(rows={"truth_vault.v_paid_boost_events": ids[:4]})
    store.tv_paid_boost_note_ids(c2)
    assert _boost_reads(c2) == 3, "正好满页时多读一页空的才知道读完了"


def test_normalize_pin_skips_the_direction_key():
    assert dp.normalize_pin({"方向": "流量贴"}) == (None, None)
    assert dp.normalize_pin({"方向": "流量贴", "标题句式": "问句"}) == ({dp.T: "疑问句"}, None)
    assert dp.normalize_pin({"风格": "x"})[1]["error"] == "pin_invalid"


def test_paid_boost_cache_is_single_flight(monkeypatch):
    """冷的时候几个线程一起要: 只有一个去读视图, 别的等它; 手里有旧的就不等。"""
    import threading
    started, release = threading.Event(), threading.Event()
    reads = []

    class SlowStore:
        @staticmethod
        def tv_paid_boost_note_ids(sb):
            reads.append(1)
            started.set()
            release.wait(5)
            return {"n1"}
    got = []
    ts = [threading.Thread(target=lambda: got.append(priors.paid_boosted(None, SlowStore))) for _ in range(3)]
    ts[0].start()
    assert started.wait(5)
    for t in ts[1:]:
        t.start()
    release.set()
    for t in ts:
        t.join(5)
    assert len(reads) == 1 and got == [frozenset({"n1"})] * 3
