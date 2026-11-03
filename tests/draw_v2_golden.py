"""发牌 v2 的金标(content layers 之前, c5ca36c 抓的): 没点名贴型(layer)时, v2 一批的候选表、台账 _draw、发出来的牌和卡面
必须和 c5ca36c 一字不差 —— 包括 rng 的消耗次序(候选的先后、批次号、切入角度 / 浓度 / 时效 / 词感都从同一个 rng 里来)。

数据全是合成的: Profile(run 阶段 + 外部参考池 + 起量帖; cold 阶段只有外部池)、台账几行老牌、点名重写一张。时间钉死在
FIXED(模块载入时刻)上, drawpolicy 的 now 和台账行的时刻都按它算, 年龄每次跑都一样; flywheel_arm 钉成 "patterns"。

``capture(case)`` 跑一次、回一条记录; tests/test_draw_layers.py 拿它和 tests/golden/draw_v2_c5ca36c.json 比。
重新生成(只有 v2 本来就要变的时候): python -m tests.draw_v2_golden > tests/golden/draw_v2_c5ca36c.json
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone

from deskcore import core, drawpolicy as dp, priors, store, vocab
from deskcore.fingerprint import angle_key
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
PID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
FIXED = datetime.now(timezone.utc).replace(microsecond=0)


def _ago(days: float) -> str:
    return (FIXED - timedelta(days=days)).isoformat()


def _project():
    return {"id": PID, "name": "测试项目", "brand": "测试品", "owner_id": ME, "system_prompt": "你是文案",
            "calibration_notes": "", "tactics": "[]", "default_params": "{}"}


def _ext_pool(n: int = 10) -> dict:
    ttl = vocab.TITLE_STRUCTURES
    pool = []
    for i in range(n):
        pool.append({"ref": f"ext:gx{i:04d}", "w": 1.0 + 0.37 * i, "velocity": 3.0 + i, "comments": 60 + 7 * i,
                     "posted_days_ago": 5 + i, "title_structure": ttl[i % len(ttl)] if i % 4 else None,
                     "desc": {"opening_type": ("身份自述", "具体事件")[i % 2], "turning_point": bool(i % 3),
                              "ending_asks_reader": bool(i % 2), "asks_for_help": None, "own_experience": None}})
    return {"category": "运动补给", "state": "ok", "ok": True, "eligible": n, "pool": pool}


def _hits(n: int = 6) -> list[dict]:
    fmts = ("提问求助", "场景植入", "情感叙事")
    return [{"ref": f"note:gx{i:04d}", "own": bool(i % 2), "w": 3.0 if i % 2 else 1.0, "tv_project": "GX_phase2",
             "content_format": fmts[i % 3], "emotional_lever": vocab.EMOTIONAL_LEVERS[(3 * i) % 12],
             "human_truth_archetype": vocab.HUMAN_TRUTH_ARCHETYPES[(5 * i) % 19],
             "title_structure": vocab.TITLE_STRUCTURES[i % 8], "age_days": 4 + i, "comments": 30 + 3 * i,
             "desc": {"opening_type": "具体事件", "ending_asks_reader": True, "asks_for_help": None,
                      "turning_point": bool(i % 2)}} for i in range(n)]


def profile(kind: str) -> dict:
    p = priors.uniform_profile(PID)
    p.update(ps="live", pp="live")
    p["mix"].update(posts=180, per_day=3.0, window_days=60)
    if kind == "run":
        p.update(stage="run", n_res=200)
        p["pi"]["content_format"] = {"提问求助": 0.6, "场景植入": 0.25, "情感叙事": 0.15}
        p["pi"]["title_structure"] = {t: (0.3 if t == "疑问句" else 0.1) for t in vocab.TITLE_STRUCTURES}
        p["evidence"] = {"content_format": {"提问求助": 0.62, "场景植入": 0.24},
                         "title_structure": {"疑问句": 0.4, "场景直述": 0.2},
                         "human_truth_archetype": {"健康焦虑": 0.3}, "emotional_lever": {"焦虑撬动": 0.2}}
        p["ext"] = _ext_pool(10)
        p["hits"] = _hits(6)
    elif kind == "cold":
        p.update(stage="cold", n_res=4)
        p["pi"]["content_format"] = {"情感叙事": 0.5, "教程攻略": 0.5}
        p["ext"] = _ext_pool(7)
    else:
        raise ValueError(kind)
    return p


_OLD = (("焦虑撬动", "健康焦虑", "提问求助", "疑问句", 2.5, None, "ext:gx0003"),
        ("愤怒撬动", "经济焦虑", "场景植入", "场景直述", 10.5, 9.5, None),
        ("好奇驱动", "消费愉悦", "提问求助", "数字清单", 20.5, 19.5, "note:gx0001"),
        ("焦虑撬动", "身份认同", "情感叙事", "疑问句", 5.5, 4.5, None))


def ledger_rows() -> list[dict]:
    rows = []
    for i, (lev, arc, fmt, ttl, drawn, consumed, ref) in enumerate(_OLD):
        dims = {"emotional_lever": lev, "human_truth_archetype": arc, "content_format": fmt, "title_structure": ttl}
        assert dp.cell_of(dims) is not None, dims
        d = {"v": 2, "pol": "v2", "b": f"old{i}", "i": 1, "src": "w"}
        if ref:
            d["ref"] = ref
        rows.append({"id": f"g{i}", "project_id": PID, "angle_key": angle_key(dims), "dims": {**dims, "_draw": d},
                     "drawn_by": ME, "drawn_at": _ago(drawn),
                     "consumed_version_id": f"gv{i}" if consumed is not None else None,
                     "consumed_at": _ago(consumed) if consumed is not None else None})
    return rows


CASES = [
    {"kind": "run", "seed": 0, "n": 3},
    {"kind": "run", "seed": 1, "n": 6},
    {"kind": "run", "seed": 2, "n": 10},
    {"kind": "run", "seed": 3, "n": 15},
    {"kind": "run", "seed": 4, "n": 10, "ledger": True},
    {"kind": "run", "seed": 5, "n": 8, "ledger": True, "repeat": True},
    {"kind": "run", "seed": 6, "n": 5, "pin": {"标题句式": "疑问句"}},
    {"kind": "cold", "seed": 7, "n": 6},
    {"kind": "cold", "seed": 8, "n": 4, "ledger": True},
]


class _World:
    """同 tests/test_draw_angles_v2._World 的假 RPC(按候选先后取前 _want 个没被挡的), 时间按 FIXED。"""

    def __init__(self, ledger=()):
        self.seen: list[list[dict]] = []
        self.client = FakeClient(rows={"projects": [_project()], "angle_ledger": list(ledger)},
                                 rpc_impl={"deskcore_reserve_angles": self._reserve})

    def _blocked(self, key, avoid_days):
        for r in self.client.rows["angle_ledger"]:
            if r.get("angle_key") != key:
                continue
            if r.get("consumed_version_id"):
                if datetime.fromisoformat(r["consumed_at"]) >= FIXED - timedelta(days=avoid_days):
                    return True
            elif datetime.fromisoformat(r["drawn_at"]) >= FIXED - timedelta(days=1):
                return True
        return False

    def _reserve(self, args):
        self.seen.append(args["_candidates"])
        out = []
        for c in args["_candidates"]:
            if len(out) >= args["_want"]:
                break
            if self._blocked(c["angle_key"], args["_avoid_days"]) or c["angle_key"] in {o["reserved_key"] for o in out}:
                continue
            out.append({"reserved_key": c["angle_key"], "reserved_dims": c["dims"]})
        for o in out:
            self.client.rows["angle_ledger"].append({
                "id": f"r{len(self.client.rows['angle_ledger'])}", "project_id": PID, "angle_key": o["reserved_key"],
                "dims": o["reserved_dims"], "drawn_by": args["_drawn_by"], "drawn_at": FIXED.isoformat(),
                "consumed_at": None, "consumed_version_id": None})
        return out


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def capture(case: dict, monkeypatch, **draw_kw) -> dict:
    """跑一次 case, 回 {候选表的 key 串 / 全部 dims(含 _draw)/ 重发那几行 / 回包里的牌和卡面} 的摘要。"""
    prof = profile(case["kind"])
    monkeypatch.setattr(priors, "get_profile", lambda *a, **k: copy.deepcopy(prof))
    monkeypatch.setattr(core, "flywheel_arm", lambda *a, **k: "patterns")
    monkeypatch.setattr(dp, "_utcnow", lambda: FIXED)
    rows = ledger_rows() if case.get("ledger") else []
    w = _World(rows)
    repeat = [rows[1]["angle_key"]] if case.get("repeat") else None
    out = core.draw_angles(w.client, PID, case["n"], user_id=ME, seed=case["seed"], pin=case.get("pin"),
                           repeat_of=repeat, **draw_kw)
    cands = w.seen[0] if w.seen else []
    # 重发那几行(record_draw 直接插的): 只比 key 和 dims —— drawn_at 是插入那一刻, id 是假件补的
    reps = [[{"angle_key": r["angle_key"], "dims": r["dims"]} for r in c["payload"]]
            for c in w.client.calls if c["table"] == "angle_ledger" and c["op"] == "insert"]
    return {
        "n_candidates": len(cands),
        "keys_sha256": hashlib.sha256("\n".join(c["angle_key"] for c in cands).encode()).hexdigest(),
        "dims_sha256": _sha([c["dims"] for c in cands]),
        "repeat_rows_sha256": _sha(reps),
        "angles": [[a["angle_key"], a["angle_name"], a["emotional_intensity"], a["trend_dependencies"], a["word_tilt"],
                    a["dims"], a.get("reference"), a.get("evidence")] for a in out["angles"]],
        "prompt_block_sha256": hashlib.sha256(out["prompt_block"].encode("utf-8")).hexdigest(),
        "batch": out["policy"].get("batch"),
        "batch_note": out["batch_note"],
        "project_status": out["project_status"],
    }


if __name__ == "__main__":                              # pragma: no cover — 只在重新生成金标时跑
    import os

    from _pytest.monkeypatch import MonkeyPatch

    os.environ.pop(core.DRAW_POLICY_ENV, None)
    got = []
    for case in CASES:
        mp = MonkeyPatch()
        try:
            got.append({"case": case, "expect": capture(case, mp)})
        finally:
            mp.undo()
    print(json.dumps({"captured_at_commit": "c5ca36c",
                      "note": ("DESK_DRAW_POLICY=v2 golden with no layer requested: candidate keys and dims (incl. _draw) "
                               "handed to deskcore_reserve_angles, repeat rows, delivered cards and the prompt block for "
                               "fixed seeds (synthetic profiles, see tests/draw_v2_golden.py). Regenerate only if v2 is "
                               "meant to change."),
                      "cases": got}, ensure_ascii=False, indent=1))
