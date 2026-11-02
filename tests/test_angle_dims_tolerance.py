"""台账 dims 多一个 ``_draw`` 键(angle cards v2 §5.1)时, 谁都不能出岔子。

v2 把每张牌的记账块(来源、概率、批次 …)放在 ``angle_ledger.dims._draw`` 里 —— 不加迁移、不改 RPC, RPC 原样存、
原样回。2026-11-02 查生产库: 1,699 行 dims 全是正好四个键, 所以 ``dims ? '_draw'`` 能干净地分出新行。
逐个看过的 dims 消费方(deskcore/ scripts/ tests/ migrations/):

  · fingerprint.angle_key —— 按名字取四个键, 多的键不进 key(下面钉着);
  · core._spread_order / render_angles_block —— 按名字取, 不整份打印;
  · core.draw_angles 回包的 angles[].dims —— 原样交给模型: 去掉下划线键(store.public_dims);
  · store.angle_debt —— 回给简报 / score_drafts / archive_pending: 去掉下划线键;
  · store.reserve_angles / record_draw / deskcore_reserve_angles —— 原样存、按 angle_key 避重, 本来就不看 dims;
  · v_angle_outcomes(011 / 013)—— a.dims 原样给 service_role, 那是 draw-report 要读的, 故意留着;
  · 没有任何一处假定"正好四个键"(没有 len(dims) == 4、没有整份比较)。
"""
from __future__ import annotations

import json

import pytest

from deskcore import core, store
from deskcore.fingerprint import angle_key
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
PID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER = "cccccccc-cccc-cccc-cccc-cccccccccccc"

FOUR = {"emotional_lever": "焦虑撬动", "human_truth_archetype": "健康焦虑",
        "content_format": "提问求助", "title_structure": "疑问句"}
DRAW = {"v": 2, "pol": "v2", "b": "abcdef012345", "i": 1, "src": "w", "p": 0.0123, "pu": 0.004,
        "pv": [0.88, 0.6, 0.17, 0.27], "st": "running", "ps": "live", "ref": "note:n-1"}


def _with_draw(dims=FOUR, **extra):
    return {**dims, "_draw": dict(DRAW, **extra)}


# ══════════════════════════════════════════════════════════════════════
# angle_key 只看四个主维度
# ══════════════════════════════════════════════════════════════════════

def test_angle_key_ignores_draw_block():
    assert angle_key(_with_draw()) == angle_key(FOUR)
    assert angle_key({**FOUR, "_draw": {}, "_x": 1, "emotional_intensity": "high"}) == angle_key(FOUR)


def test_angle_key_reads_exactly_the_four_named_keys():
    base = angle_key(FOUR)
    for k in FOUR:
        assert angle_key({**FOUR, k: FOUR[k] + "x"}) != base, k
    assert angle_key(dict(reversed(list(FOUR.items())))) == base, "键的先后不影响"


# ══════════════════════════════════════════════════════════════════════
# public_dims
# ══════════════════════════════════════════════════════════════════════

def test_public_dims_strips_underscore_keys_only():
    assert store.public_dims(_with_draw()) == FOUR
    assert store.public_dims({**FOUR, "_a": 1, "__b": 2, "c_": 3}) == {**FOUR, "c_": 3}
    assert store.public_dims({}) == {}
    assert store.public_dims(None) is None
    d = _with_draw()
    store.public_dims(d)
    assert "_draw" in d, "不改调用方手里的那一份"


# ══════════════════════════════════════════════════════════════════════
# angle_debt → 简报 / score_drafts / archive_pending
# ══════════════════════════════════════════════════════════════════════

def _ledger_row(i, dims, *, days_ago=0.25, user=ME, project=PID):
    return {"id": f"ang-{i}", "project_id": project, "angle_key": angle_key(dims), "dims": dims,
            "drawn_by": user, "drawn_at": store.iso_ago(days_ago),
            "consumed_version_id": None, "consumed_at": None}


def _client(ledger):
    return FakeClient(rows={
        "projects": [{"id": PID, "name": "测试项目", "brand": "测试品", "owner_id": ME, "system_prompt": "你是文案",
                      "calibration_notes": "", "tactics": "[]", "default_params": "{}"}],
        "memories": [], "user_calibration_notes": [], "items": [], "versions": [], "batches": [],
        "draft_fingerprints": [], "angle_ledger": list(ledger), "style_edits": [],
    })


def _debt_leaks(angles) -> bool:
    """angle_debt 的 angles 里有没有带出下划线键。"""
    return any(any(str(k).startswith("_") for k in (a.get("dims") or {})) for a in angles)


def _mixed_ledger():
    other = dict(FOUR, content_format="场景植入")
    return [_ledger_row(1, _with_draw()), _ledger_row(2, other)]       # 一行新(带 _draw)、一行老(四个键)


def test_angle_debt_strips_draw_block():
    debt = store.angle_debt(_client(_mixed_ledger()), PID, ME, 7)
    assert debt["unconsumed"] == 2
    assert not _debt_leaks(debt["angles"])
    assert all(set(a["dims"]) == set(FOUR) for a in debt["angles"])
    assert {a["angle_key"] for a in debt["angles"]} == {angle_key(FOUR), angle_key(dict(FOUR, content_format="场景植入"))}


def test_angle_debt_strip_test_can_fail(monkeypatch):
    """去掉 angle_debt 里那一道 strip, 上面那条就该红(spec §6 Tests: Stripping)。"""
    monkeypatch.setattr(store, "public_dims", lambda d: d)
    debt = store.angle_debt(_client(_mixed_ledger()), PID, ME, 7)
    assert _debt_leaks(debt["angles"])


def test_brief_never_shows_draw_block():
    out = core.build_writing_brief(_client(_mixed_ledger()), PID, user_id=ME)
    assert out["counts"]["angle_debt"] == 2
    blob = json.dumps(out, ensure_ascii=False, default=str)
    assert '"_draw"' not in blob and '"src"' not in blob
    assert all(set(a["dims"]) == set(FOUR) for a in out["angle_debt"]["angles"])


def test_archive_pending_counts_new_rows_like_old_ones():
    block = core.archive_pending(_client(_mixed_ledger()), PID, user_id=ME)
    assert block and block["cards"] == 2
    assert '"_draw"' not in json.dumps(block, ensure_ascii=False, default=str)


# ══════════════════════════════════════════════════════════════════════
# draw_angles: RPC 原样回 dims(带 _draw), 回包里不能有
# ══════════════════════════════════════════════════════════════════════

def _reserve_echo_with_draw(args):
    """仿 deskcore_reserve_angles, 但每张候选的 dims 都带 _draw(v2 的候选就长这样): RPC 原样存、原样回。"""
    out = []
    for c in args["_candidates"]:
        out.append({"reserved_key": c["angle_key"], "reserved_dims": _with_draw(c["dims"])})
        if len(out) >= args["_want"]:
            break
    return out


def test_draw_angles_returns_only_the_four_dims():
    c = FakeClient(rows={"projects": [{"id": PID, "name": "测试项目", "owner_id": ME}], "angle_ledger": []},
                   rpc_impl={"deskcore_reserve_angles": _reserve_echo_with_draw})
    out = core.draw_angles(c, PID, 5, user_id=ME, seed=7)
    assert out["delivered"] == 5
    for a in out["angles"]:
        assert set(a["dims"]) == set(FOUR), a["dims"]
        assert a["angle_key"] == angle_key(a["dims"])
    assert "_draw" not in out["prompt_block"] and '"_draw"' not in json.dumps(out, ensure_ascii=False)


def test_spread_order_ignores_extra_keys():
    """一批之内错开只按三个具名维度比: 候选多一个 _draw 不改变顺序。"""
    import random
    rng = random.Random(5)
    space = [{"emotional_lever": lv, "human_truth_archetype": "健康焦虑", "content_format": f, "title_structure": t}
             for lv in ("焦虑撬动", "好奇驱动", "愤怒撬动") for f in ("提问求助", "场景植入", "情感叙事")
             for t in ("疑问句", "场景直述", "感叹共鸣")]
    rng.shuffle(space)
    plain = [{"angle_key": angle_key(d), "dims": d} for d in space]
    tagged = [{"angle_key": angle_key(d), "dims": _with_draw(d, i=j)} for j, d in enumerate(space)]
    assert [c["angle_key"] for c in core._spread_order(plain, 6)] == \
        [c["angle_key"] for c in core._spread_order(tagged, 6)]


# ══════════════════════════════════════════════════════════════════════
# 新的台账读数也是同一个口径
# ══════════════════════════════════════════════════════════════════════

def test_ledger_dims_for_returns_public_dims_of_the_newest_row():
    old = dict(FOUR)
    rows = [_ledger_row(1, old, days_ago=20), _ledger_row(2, _with_draw(), days_ago=2)]
    c = _client(rows)
    assert store.ledger_dims_for(c, PID, angle_key(FOUR)) == FOUR


def test_ledger_window_keeps_draw_block_for_the_policy():
    """发牌策略自己要读 _draw(参考帖冷却、来源): ledger_window 原样给, 不 strip。"""
    rows = [_ledger_row(1, _with_draw())]
    got = store.ledger_window(_client(rows), PID, 30)
    assert got and got[0]["dims"]["_draw"]["src"] == "w"


@pytest.mark.parametrize("dims", [FOUR, _with_draw()])
def test_record_draw_stores_dims_verbatim(dims):
    c = _client([])
    store.record_draw(c, PID, [{"angle_key": angle_key(dims), "dims": dims}], ME)
    assert c.rows["angle_ledger"][0]["dims"] == dims
