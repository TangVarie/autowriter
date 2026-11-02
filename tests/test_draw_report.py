"""draw-report(angle cards v2 §5.2 / §5.3): 纯函数的口径 + CLI 走假库一遍(只读)。

数据全是合成的: 两个写作台项目、手写的台账行(dims._draw 照 drawpolicy 的键)、对照表 / 差异表 / 查重底稿各几行、
发出去的几篇只有评论数和 tier。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from deskcore import cli, drawreport as dr
from tests.fakes import FakeClient

NOW = datetime(2026, 12, 1, 4, 0, tzinfo=timezone.utc)
P1, P2 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _iso(days):
    return (NOW - timedelta(days=days)).isoformat()


def _card(i, *, src="w", project=P1, days=10, consumed_after=None, lever="愤怒撬动", p=0.02, pu=0.01,
          pv=(0.84, 0.6, 0.2, 0.3), pol="v2"):
    d = {"v": 2, "pol": pol, "b": "b0", "i": 1, "src": src, "sp": 0.5, "p": p, "pu": pu, "pv": list(pv),
         "st": "run", "ps": "live", "dd": "py"}
    dims = {"emotional_lever": lever, "human_truth_archetype": "健康焦虑", "content_format": "提问求助",
            "title_structure": "疑问句", "_draw": d}
    return {"id": f"l{i}", "project_id": project, "angle_key": f"{i:020x}", "dims": dims, "drawn_by": "u",
            "drawn_at": _iso(days),
            "consumed_version_id": f"v{i}" if consumed_after is not None else None,
            "consumed_at": _iso(days - consumed_after) if consumed_after is not None else None}


def test_m1_counts_only_cards_old_enough_and_consumed_within_three_days():
    rows = [_card(1, consumed_after=1), _card(2, consumed_after=5), _card(3), _card(4, days=1, consumed_after=0.5)]
    cards = {c["key"]: c for c in dr.card_rows(rows, now=NOW)}
    assert [cards[f"{i:020x}"]["m1"] for i in (1, 2, 3, 4)] == [1, 0, 0, None], "第 4 张还没到 D+3, 不算 0"
    assert all(c["m2"] is None for c in cards.values()), "没有对照表就读不到 M2"


def test_m2_m3_m5_m6_from_links_diffs_and_notes():
    rows = [_card(i, consumed_after=1) for i in range(1, 5)]
    adoption = {"by_version": {
        "v1": {"note_id": "n1", "title_changed": False, "draft_chars": 500, "published_chars": 480},
        "v2": {"note_id": "n2", "title_changed": True, "draft_chars": 500, "published_chars": 500},
        "v3": {"note_id": "n3", "title_changed": False, "draft_chars": 500, "published_chars": 300}}}
    notes = {"n1": {"publish_time": _iso(6), "comments_count": 25, "tier": "爆"},
             "n2": {"publish_time": _iso(6), "comments_count": 3, "tier": None},
             "n3": {"publish_time": _iso(1), "comments_count": 50, "tier": None}}
    cards = {c["key"][-1]: c for c in dr.card_rows(rows, adoption=adoption, notes=notes, now=NOW)}
    assert [cards[k]["m2"] for k in "1234"] == [1, 1, 1, 0]
    assert [cards[k]["m3"] for k in "1234"] == [1, 0, 0, 0], "标题改了 / 字数少过 0.85 都不算留着"
    assert [cards[k]["m5"] for k in "123"] == [1, 0, None], "发出不满三天的不算"
    assert [cards[k]["m6"] for k in "123"] == [1, 0, 0]


def test_d1_is_reject_or_warn_among_checks_after_the_draw():
    rows = [_card(1), _card(2), _card(3)]
    snaps = [{"project_id": P1, "angle_key": f"{1:020x}", "status": "reject", "checked_at": _iso(9)},
             {"project_id": P1, "angle_key": f"{2:020x}", "status": "pass", "checked_at": _iso(9)},
             {"project_id": P1, "angle_key": f"{3:020x}", "status": "warn", "checked_at": _iso(12)}]   # 抽之前的不算
    d1 = [c["d1"] for c in dr.card_rows(rows, snapshots=snaps, now=NOW)]
    assert d1 == [1, 0, None]


def test_mantel_haenszel_by_project():
    rows = []
    i = 0
    # P1: w 8/10, x 4/10;  P2: w 2/10, x 1/10
    for proj, src, y, n in ((P1, "w", 8, 10), (P1, "x", 4, 10), (P2, "w", 2, 10), (P2, "x", 1, 10)):
        for k in range(n):
            i += 1
            rows.append(_card(i, src=src, project=proj, consumed_after=1 if k < y else None))
    mh = dr.mantel_haenszel(dr.card_rows(rows, now=NOW), "w", "x", "m1")
    assert mh["strata"] == 2 and (mh["y_treat"], mh["n_treat"], mh["y_ctrl"], mh["n_ctrl"]) == (10, 20, 5, 20)
    assert mh["rd_mh"] == pytest.approx(0.25) and mh["rr_mh"] == pytest.approx(2.0)


def test_ipw_reweights_to_the_explore_distribution():
    """w 牌 p 大 pu 小(策略偏爱的格子), x 牌 p = pu: IPW 估计把 w 的权重压下去, 落在 x 一侧。"""
    rows = [_card(i, src="w", p=0.04, pu=0.01, consumed_after=1) for i in range(1, 9)]
    rows += [_card(i, src="x", p=0.01, pu=0.01) for i in range(9, 13)]
    est = dr.ipw_uniform(dr.card_rows(rows, now=NOW), "m1")
    assert est["cards"] == 12 and est["observed"] == pytest.approx(8 / 12, abs=1e-3)
    assert est["estimate"] == pytest.approx(2 / 6, abs=1e-3)


def test_x_only_lever_lift_uses_explore_cards_only():
    rows = [_card(i, src="x", lever="愤怒撬动", consumed_after=1) for i in range(1, 5)]
    rows += [_card(i, src="x", lever="共鸣释放") for i in range(5, 9)]
    rows += [_card(i, src="w", lever="共鸣释放", consumed_after=1) for i in range(9, 30)]   # w 不进
    got = dr.x_only_lever_lift(dr.card_rows(rows, now=NOW), "m1")
    assert got["cards"] == 8 and got["base_rate"] == 0.5
    assert got["lift"]["愤怒撬动"]["lift"] == pytest.approx((4 + 1) / 6 / 0.5, abs=1e-3)
    assert got["lift"]["共鸣释放"]["lift"] == pytest.approx((0 + 1) / 6 / 0.5, abs=1e-3)


def test_health_flags_off_quota_writer_share_and_fallbacks():
    rows = [_card(1), _card(2, pv=(0.0, 0.5, 0.1, 0.1)), _card(3, src="pin", pv=(0.0, 1, 1, 1)), _card(4, src="rep"),
            _card(5, src="fb"), _card(6, src="v1", pol="v1")]
    h = dr.health(dr.card_rows(rows, now=NOW), now=NOW)[P1]
    assert h["off_quota_share"] == 0.5, "pin 出来的配额外不算"
    assert h["writer_share"] == round(2 / 5, 3) and h["fb_share"] == round(1 / 6, 3)
    assert h["distinct_keys_per_card_30d"] == 1.0 and h["max_lever_share_14d"] == 1.0
    rep = dr.report(rows, now=NOW)
    assert rep["by_source"]["w"]["cards"] == 2 and {r["src"] for r in rep["table"]} >= {"w", "pin", "rep", "fb", "v1"}
    assert "draw-report" in dr.format_report(rep)


def test_legacy_rows_without_draw_are_their_own_source():
    row = _card(1)
    row["dims"].pop("_draw")
    assert dr.card_rows([row], now=NOW)[0]["src"] == "legacy"


def test_cli_reads_only_and_reports(monkeypatch, capsys):
    rows = [_card(i, src="w" if i % 2 else "x", consumed_after=1) for i in range(1, 7)]
    for r in rows:
        r["drawn_at"] = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        r["consumed_at"] = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    c = FakeClient(rows={
        "angle_ledger": rows,
        "tv_note_links": [{"note_id": "n1", "version_id": "v1", "match_kind": "body_exact"}],
        "tv_edit_diffs": [{"note_id": "n1", "title_changed": False, "draft_chars": 400, "published_chars": 400}],
        "draft_snapshots": [{"id": "s1", "project_id": P1, "angle_key": rows[0]["angle_key"], "status": "warn",
                             "checked_at": datetime.now(timezone.utc).isoformat(), "title": "不该被取", "body": "不该被取"}],
        "truth_vault.notes": [{"note_id": "n1", "publish_time": _iso(20), "comments_count": 22, "tier": None,
                               "raw_content": "不该被取"}],
    })

    class Args:
        days, project, json = 28, None, True
    assert cli._draw_report(c, Args) == 0
    out = capsys.readouterr().out
    assert '"cards": 6' in out and "m1" in out
    assert not [x for x in c.calls if x["op"] != "select"], "只读"
    snap = next(x for x in c.calls if x["table"] == "draft_snapshots")
    assert "title" not in snap["cols"] and "body" not in snap["cols"]
    notes = next(x for x in c.calls if x["table"] == "truth_vault.notes")
    assert "raw_content" not in notes["cols"] and "title" not in notes["cols"]
