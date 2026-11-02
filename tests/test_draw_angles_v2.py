"""core.draw_angles 接上发牌 v2 之后(angle cards v2 §2.1 / §2.8–§2.10 / §3.3)。

数据全是合成的: 项目、台账行、Profile(priors.uniform_profile 改 π 表)、外部参考 / 起量帖的 ref(ext:fx* / note:fx*)。
没有任何真实标题或正文。

钉的是接线:
  · 总开关 DESK_DRAW_POLICY=v1: 交给 RPC 的候选次序、发出来的牌(含切入角度 / 浓度 / 时效 / 词感)和 aceee73 一字不差
    (tests/golden/draw_v1_aceee73.json, 改 core 之前用 aceee73 的代码抓的); 反证: v2 同一个 seed 的候选次序不一样;
  · v2 策略里出异常 / 先验整个拿不到 → 这一批按 v1 发, 台账 src "fb"; TV 读不到、台账读不到都照常发(fail open);
  · pin: 每张牌都是点名的值, L0 / L1 照样挡; 值不对 → 报错、什么都不写;
  · repeat_of: 走非原子的 record_draw, _draw.src "rep"; 别的项目的 key / 超过 3 个 / 还没用掉的牌 → 报错、什么都不写;
  · 回包: project_status(也是 prompt_block 第二行)、batch_note、policy、angles[].reference(只有 e / h / rep)、
    凑不满时的 note、n / avoid_days 的夹子; angles[].dims 只有四个键。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deskcore import core, drawpolicy as dp, priors, project_now, store, vocab
from deskcore.fingerprint import angle_key
from tests.fakes import FakeClient
from tests.test_writer_facing_text import _assert_writer_safe

ME = "11111111-1111-1111-1111-111111111111"
PID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER = "cccccccc-cccc-cccc-cccc-cccccccccccc"
NOW = datetime.now(timezone.utc)
GOLDEN = json.loads((Path(__file__).parent / "golden" / "draw_v1_aceee73.json").read_text(encoding="utf-8"))


def _ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


def _project(pid=PID):
    return {"id": pid, "name": "测试项目", "brand": "测试品", "owner_id": ME, "system_prompt": "你是文案",
            "calibration_notes": "", "tactics": "[]", "default_params": "{}"}


class _World:
    """假库 + 仿 deskcore_reserve_angles 的 RPC: 按候选先后, 跳过「写过且 avoid_days 内」「没写、一天内抽过」的 key,
    取前 _want 个, 预留到的写进台账(和真 RPC 一样)。seen 记每次交给 RPC 的候选表。"""

    def __init__(self, ledger=(), *, rpc=True, cap=None):
        self.seen: list[list[dict]] = []
        self.cap = cap
        self.client = FakeClient(rows={"projects": [_project(), _project(OTHER)], "angle_ledger": list(ledger)},
                                 rpc_impl={"deskcore_reserve_angles": self._reserve} if rpc else None)

    def _blocked(self, key, avoid_days):
        for r in self.client.rows["angle_ledger"]:
            if r.get("project_id") != PID or r.get("angle_key") != key:
                continue
            if r.get("consumed_version_id"):
                if datetime.fromisoformat(r["consumed_at"]) >= NOW - timedelta(days=avoid_days):
                    return True
            elif datetime.fromisoformat(r["drawn_at"]) >= NOW - timedelta(days=1):
                return True
        return False

    def _reserve(self, args):
        self.seen.append(args["_candidates"])
        want = args["_want"] if self.cap is None else min(args["_want"], self.cap)
        out = []
        for c in args["_candidates"]:
            if len(out) >= want:
                break
            if self._blocked(c["angle_key"], args["_avoid_days"]) or c["angle_key"] in {o["reserved_key"] for o in out}:
                continue
            out.append({"reserved_key": c["angle_key"], "reserved_dims": c["dims"]})
        for o in out:
            self.client.rows["angle_ledger"].append({
                "id": f"r{len(self.client.rows['angle_ledger'])}", "project_id": PID, "angle_key": o["reserved_key"],
                "dims": o["reserved_dims"], "drawn_by": args["_drawn_by"], "drawn_at": NOW.isoformat(),
                "consumed_at": None, "consumed_version_id": None})
        return out

    def inserts(self):
        return [c for c in self.client.calls if c["table"] == "angle_ledger" and c["op"] == "insert"]


def _row(dims, *, days, consumed_days=None, project=PID, i=0):
    return {"id": f"l{i}", "project_id": project, "angle_key": angle_key(dims), "dims": dict(dims), "drawn_by": ME,
            "drawn_at": _ago(days=days),
            "consumed_version_id": f"v{i}" if consumed_days is not None else None,
            "consumed_at": _ago(days=consumed_days) if consumed_days is not None else None}


def _fixed_profile(monkeypatch, prof):
    monkeypatch.setattr(priors, "get_profile", lambda *a, **k: prof)


def _run_profile(**kw):
    p = priors.uniform_profile(PID)
    p.update(stage="run", n_res=200, ps="live", pp="live")
    p["pi"]["content_format"] = {"提问求助": 0.84, "场景植入": 0.16}
    p["evidence"] = {"content_format": {"提问求助": 0.88, "场景植入": 0.12},
                     "title_structure": {"疑问句": 0.66, "场景直述": 0.22, "感叹共鸣": 0.12},
                     "human_truth_archetype": {"健康焦虑": 0.27, "消费愉悦": 0.16, "经济焦虑": 0.1},
                     "emotional_lever": {"愤怒撬动": 0.2}}
    p["mix"].update(posts=224, per_day=3.73, window_days=60)
    p.update(kw)
    return p


def _ext_pool(n):
    return {"category": "运动补给", "state": "ok", "ok": True, "eligible": n,
            "pool": [{"ref": f"ext:fx{i:04d}", "w": 1.0, "velocity": 14.0, "comments": 300,
                      "posted_days_ago": 21, "title_structure": "数字清单",
                      "desc": {"opening_type": "身份自述", "turning_point": True, "ending_asks_reader": True,
                               "asks_for_help": None, "own_experience": None}} for i in range(n)]}


def _hits(n):
    return [{"ref": f"note:fx{i:04d}", "own": True, "w": 3.0, "tv_project": "FX_phase2",
             "content_format": "提问求助", "emotional_lever": vocab.EMOTIONAL_LEVERS[i],
             "human_truth_archetype": vocab.HUMAN_TRUTH_ARCHETYPES[i], "title_structure": "疑问句",
             "age_days": 5, "comments": 40,
             "desc": {"opening_type": "具体事件", "ending_asks_reader": True, "asks_for_help": None,
                      "turning_point": None}} for i in range(n)]


# ══════════════════════════════════════════════════════════════════════
# 总开关: DESK_DRAW_POLICY=v1 和 aceee73 一字不差
# ══════════════════════════════════════════════════════════════════════

def _boom(*a, **k):
    raise AssertionError("v1 是总开关, 不该去读 TV / 建先验")


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=lambda c: f"seed{c['seed']}-n{c['n']}")
def test_kill_switch_reproduces_the_aceee73_draw(case, monkeypatch):
    monkeypatch.setenv(core.DRAW_POLICY_ENV, "v1")
    monkeypatch.setattr(priors, "get_profile", _boom)
    monkeypatch.setattr(store, "ledger_window", _boom)
    w = _World()
    out = core.draw_angles(w.client, PID, case["n"], user_id=ME, seed=case["seed"])
    keys = [c["angle_key"] for c in w.seen[0]]
    assert len(keys) == case["n_candidates"]
    assert keys[:len(case["candidates_head"])] == case["candidates_head"]
    assert hashlib.sha256("\n".join(keys).encode()).hexdigest() == case["candidates_sha256"]
    got = [[a["angle_key"], a["angle_name"], a["emotional_intensity"], a["trend_dependencies"], a["word_tilt"],
            a["dims"]["emotional_lever"], a["dims"]["human_truth_archetype"], a["dims"]["content_format"],
            a["dims"]["title_structure"]] for a in out["angles"]]
    assert got == case["angles"]
    assert all(c["dims"]["_draw"]["pol"] == "v1" and c["dims"]["_draw"]["src"] == "v1" for c in w.seen[0])
    assert out["policy"]["name"] == "v1" and out["project_status"] == project_now.STATUS_V1
    assert "（本项目" not in out["prompt_block"], "v1 的卡面不带占比依据"


def test_the_kill_switch_golden_can_fail():
    """反证: v2 同一个 seed 交给 RPC 的候选次序和 v1 的不一样 —— 上面那条不是怎么发都绿。"""
    case = GOLDEN["cases"][6]
    w = _World()
    core.draw_angles(w.client, PID, case["n"], user_id=ME, seed=case["seed"])
    keys = [c["angle_key"] for c in w.seen[0]]
    assert hashlib.sha256("\n".join(keys).encode()).hexdigest() != case["candidates_sha256"]
    assert all(c["dims"]["_draw"]["pol"] == "v2" for c in w.seen[0])


def test_anything_but_v1_is_v2(monkeypatch):
    for v in ("", "v2", "V3", "off"):
        monkeypatch.setenv(core.DRAW_POLICY_ENV, v)
        assert core.draw_policy() == "v2"
    monkeypatch.setenv(core.DRAW_POLICY_ENV, " V1 ")
    assert core.draw_policy() == "v1"


# ══════════════════════════════════════════════════════════════════════
# fail open: 策略出错 / 先验拿不到 / TV 读不到 / 台账读不到
# ══════════════════════════════════════════════════════════════════════

def test_an_exception_inside_the_policy_falls_back_to_v1(monkeypatch, caplog):
    def broken(*a, **k):
        raise ZeroDivisionError("坏了")
    monkeypatch.setattr(dp, "build_candidates", broken)
    w = _World()
    out = core.draw_angles(w.client, PID, 5, user_id=ME, seed=1)
    assert out["delivered"] == 5 and out["policy"] == {"name": "v1", "fallback": True, "batch": out["policy"]["batch"]}
    assert {c["dims"]["_draw"]["src"] for c in w.seen[0]} == {"fb"}
    assert out["project_status"] == project_now.STATUS_DEGRADED
    assert "v2 策略出错" in caplog.text


def test_no_prior_at_all_falls_back_to_v1(monkeypatch):
    """全局表读不到、快照也坏了(Profile 是均匀的 ps="uniform")→ 按 v1 发(§2.10)。"""
    _fixed_profile(monkeypatch, priors.uniform_profile(PID))
    w = _World()
    out = core.draw_angles(w.client, PID, 4, user_id=ME, seed=2)
    assert out["delivered"] == 4 and out["policy"]["fallback"] is True
    assert {c["dims"]["_draw"]["src"] for c in w.seen[0]} == {"fb"}


def test_tv_unreachable_still_draws_from_the_snapshot(monkeypatch):
    def down(*a, **k):
        raise RuntimeError("TV 读不到")
    for fn in ("tv_notes_outcomes", "tv_title_answers", "tv_projects_meta", "tv_projects_of"):
        monkeypatch.setattr(store, fn, down)
    w = _World()
    out = core.draw_angles(w.client, PID, 5, user_id=ME, seed=3)
    assert out["delivered"] == 5 and out["policy"]["name"] == "v2" and out["policy"]["prior"] == "snap"
    assert out["project_status"] == project_now.STATUS_DEGRADED
    assert out["prompt_block"].splitlines()[1] == "本项目现在：" + project_now.STATUS_DEGRADED


def test_ledger_read_failure_leaves_exact_keys_to_the_rpc(monkeypatch):
    monkeypatch.setattr(store, "ledger_window", lambda *a, **k: None)
    w = _World()
    out = core.draw_angles(w.client, PID, 5, user_id=ME, seed=4)
    assert out["delivered"] == 5
    assert {c["dims"]["_draw"]["dd"] for c in w.seen[0]} == {"rpc_only"}


def test_ledger_window_is_read_fresh_with_the_dedup_window(monkeypatch):
    seen = []
    real = store.ledger_window
    monkeypatch.setattr(store, "ledger_window", lambda sb, pid, days, *a, **k: seen.append(days) or real(sb, pid, days))
    for avoid in (30, 45):
        core.draw_angles(_World().client, PID, 3, user_id=ME, seed=1, avoid_days=avoid)
    assert seen == [30, 45], "每次发牌现读一次, 窗口 max(30, avoid_days)"


# ══════════════════════════════════════════════════════════════════════
# L1: 30 天内抽过的 key 不再发
# ══════════════════════════════════════════════════════════════════════

def test_a_key_drawn_twenty_days_ago_is_never_dealt_again():
    """L1(§2.6): 20 天前抽过、从没写的 key, RPC 已经不挡了(只挡一天内的), Python 这一层挡。"""
    pinned = {"content_format": "提问求助", "title_structure": "疑问句", "human_truth_archetype": "健康焦虑"}
    old = [_row({**pinned, "emotional_lever": lev}, days=20, i=i) for i, lev in enumerate(vocab.EMOTIONAL_LEVERS[:6])]
    blocked = {r["angle_key"] for r in old}
    for seed in range(15):
        out = core.draw_angles(_World(old).client, PID, 3, user_id=ME, seed=seed,
                               pin={"内容形式": "提问求助", "标题句式": "问句", "人性原型": "健康焦虑"})
        assert out["delivered"] == 3
        assert not ({a["angle_key"] for a in out["angles"]} & blocked)


# ══════════════════════════════════════════════════════════════════════
# 写手点名: pin / repeat_of
# ══════════════════════════════════════════════════════════════════════

def test_pin_restricts_every_card_and_is_recorded():
    w = _World()
    out = core.draw_angles(w.client, PID, 6, user_id=ME, seed=5, pin={"标题句式": "问句"})
    assert out["delivered"] == 6
    assert {a["dims"]["title_structure"] for a in out["angles"]} == {"疑问句"}
    drawn = [r for r in w.client.rows["angle_ledger"]]
    assert all(r["dims"]["_draw"]["src"] == "pin" and r["dims"]["_draw"]["pin"] == ["title_structure"] for r in drawn)
    assert "点名" in out["batch_note"]


def test_pin_still_respects_l0():
    """L0: 写过的 key(avoid_days 内)RPC 照样挡, pin 了也一样。"""
    pinned = {"content_format": "提问求助", "title_structure": "疑问句", "human_truth_archetype": "健康焦虑"}
    used = [_row({**pinned, "emotional_lever": lev}, days=3, consumed_days=2, i=i)
            for i, lev in enumerate(vocab.EMOTIONAL_LEVERS[:10])]
    out = core.draw_angles(_World(used).client, PID, 2, user_id=ME, seed=1,
                           pin={"content_format": "提问求助", "title_structure": "疑问句",
                                "human_truth_archetype": "健康焦虑"})
    assert out["delivered"] == 2
    assert not ({a["angle_key"] for a in out["angles"]} & {r["angle_key"] for r in used})


@pytest.mark.parametrize("pin", [{"标题句式": "反问句"}, {"语气": "口语"}, "疑问句"])
def test_a_bad_pin_is_an_error_and_writes_nothing(monkeypatch, pin):
    _fixed_profile(monkeypatch, _run_profile())
    w = _World()
    out = core.draw_angles(w.client, PID, 5, user_id=ME, seed=1, pin=pin)
    assert out["error"] == "pin_invalid" and out["delivered"] == 0 and out["angles"] == []
    assert out["allowed"] and out["format_shares"] == {"提问求助": 0.84, "场景植入": 0.16}
    assert not w.client.rpc_calls and not w.inserts(), "报错就什么都不写"
    _assert_writer_safe(out, "pin error")


def test_repeat_of_redeals_through_record_draw():
    dims = {"emotional_lever": "愤怒撬动", "human_truth_archetype": "健康焦虑", "content_format": "提问求助",
            "title_structure": "疑问句"}
    old = _row(dims, days=2, consumed_days=1, i=1)
    w = _World([old])
    out = core.draw_angles(w.client, PID, 3, user_id=ME, seed=2, repeat_of=[old["angle_key"]])
    assert out["delivered"] == 3
    first = out["angles"][0]
    assert first["angle_key"] == old["angle_key"] and first["reference"] == {"kind": "repeat", "of": old["angle_key"]}
    assert set(first["dims"]) == set(dims)
    ins = w.inserts()
    assert len(ins) == 1 and ins[0]["payload"][0]["dims"]["_draw"]["src"] == "rep"
    assert ins[0]["payload"][0]["dims"]["_draw"]["of"] == old["angle_key"]
    assert core.REPEAT_LINE in out["prompt_block"]
    assert [len(c) > 0 for c in w.seen] == [True], "其余两张照常走 RPC"
    assert old["angle_key"] not in {a["angle_key"] for a in out["angles"][1:]}
    assert "点名要再写一次" in out["batch_note"]


def test_repeat_of_a_key_from_another_project_is_an_error_with_no_writes():
    dims = {"emotional_lever": "愤怒撬动", "human_truth_archetype": "健康焦虑", "content_format": "提问求助",
            "title_structure": "疑问句"}
    theirs = _row(dims, days=2, project=OTHER, i=1)
    w = _World([theirs])
    out = core.draw_angles(w.client, PID, 3, user_id=ME, seed=2, repeat_of=[theirs["angle_key"]])
    assert out["error"] == "repeat_of_invalid" and out["delivered"] == 0
    assert not w.client.rpc_calls and not w.inserts()
    too_many = core.draw_angles(w.client, PID, 5, user_id=ME, repeat_of=["a" * 20, "b" * 20, "c" * 20, "d" * 20])
    assert too_many["error"] == "repeat_of_invalid" and not w.inserts()


def test_more_repeats_than_the_batch_is_an_error_with_no_writes():
    """repeat_of 点的篇数多于这次要的篇数(夹过之后的 n): 报错、台账一行不写、RPC 不调(review on aw#140: n=1 点两个,
    原来两张都写进台账、回包 requested 1 delivered 2)。重复的 key 先去重再数; 正好等于 n 照常发。"""
    d1 = {"emotional_lever": "愤怒撬动", "human_truth_archetype": "健康焦虑", "content_format": "提问求助",
          "title_structure": "疑问句"}
    d2 = {"emotional_lever": "好奇驱动", "human_truth_archetype": "自我提升", "content_format": "教程攻略",
          "title_structure": "数字清单"}
    a, b = _row(d1, days=3, consumed_days=2, i=1), _row(d2, days=4, consumed_days=3, i=2)
    w = _World([a, b])
    out = core.draw_angles(w.client, PID, 1, user_id=ME, seed=2, repeat_of=[a["angle_key"], b["angle_key"]])
    assert out["error"] == "repeat_of_invalid" and out["requested"] == 1
    assert out["delivered"] == 0 and out["angles"] == []
    assert not w.client.rpc_calls and not w.inserts(), "报错就什么都不写"
    _assert_writer_safe(out, "repeat_of over n")
    same = core.draw_angles(w.client, PID, 1, user_id=ME, seed=2, repeat_of=[a["angle_key"], a["angle_key"]])
    assert "error" not in same and same["requested"] == same["delivered"] == 1
    assert len(w.inserts()) == 1 and len(w.inserts()[0]["payload"]) == 1
    # 新的一个台账(上面那次重写的 a 还没用, 再点 a 会被当成「还没用过」报错, 见下一条用例)
    w2 = _World([a, b])
    both = core.draw_angles(w2.client, PID, 2, user_id=ME, seed=2, repeat_of=[a["angle_key"], b["angle_key"]])
    assert "error" not in both and both["requested"] == both["delivered"] == 2
    assert len(w2.inserts()) == 1 and len(w2.inserts()[0]["payload"]) == 2
    assert not w.client.rpc_calls and not w2.client.rpc_calls


def test_repeat_of_a_card_not_yet_used_is_an_error_and_the_original_stays_the_only_row():
    """review round 2 on aw#140: 点名重写的 key 还有一张没用掉的牌(两天前抽的, 还没存档)。原来照样再插一行 —— 存档时
    consume_angle 销的是这个人【最早】那一行, 于是销掉的是原来那张、重写的那张一直挂着, 重写那篇的结果也记到原来那张的
    来源上。现在: 报错、台账一行不写、RPC 不调, 叫模型直接照原来那张卡的 angle_key 写; 照做之后销的就是原来那一行。"""
    dims = {"emotional_lever": "愤怒撬动", "human_truth_archetype": "健康焦虑", "content_format": "提问求助",
            "title_structure": "疑问句"}
    old = _row(dims, days=2, i=1)                       # 还没存档的那一张
    key = old["angle_key"]
    w = _World([dict(old)])
    out = core.draw_angles(w.client, PID, 3, user_id=ME, seed=2, repeat_of=[key])
    assert out.get("error") == "repeat_of_invalid", out.get("error")
    assert out["delivered"] == 0 and out["angles"] == [] and out["key"] == key
    assert key in out["message"] and "还没用" in out["message"] and "不用重写" in out["message"]
    assert out["dims"] == dims, "带上原来那张卡的四个维度, 卡不在手边也能照着写"
    assert not w.client.rpc_calls and not w.inserts(), "报错就什么都不写"
    _assert_writer_safe(out, "repeat_of not yet used")
    assert [r["id"] for r in w.client.rows["angle_ledger"] if r["angle_key"] == key] == ["l1"]
    # 模型照做: 用原来那张卡的 angle_key 写完存档 —— 销的就是原来那一行, 台账里这个 key 还是只有它
    assert store.consume_angle(w.client, PID, key, "v9", user_id=ME) is True
    rows = [r for r in w.client.rows["angle_ledger"] if r["angle_key"] == key]
    assert [(r["id"], r["consumed_version_id"]) for r in rows] == [("l1", "v9")]
    # 队友抽了没用的也算: 同一个 key 在这个项目里还挂着一张, 一样不重发
    theirs = dict(old, drawn_by=OTHER)
    w2 = _World([theirs])
    out2 = core.draw_angles(w2.client, PID, 1, user_id=ME, seed=2, repeat_of=[key])
    assert out2.get("error") == "repeat_of_invalid" and not w2.inserts()
    # 原来那张已经用掉(存档过)才是真的「再写一次」: 照常重发
    w3 = _World([_row(dims, days=2, consumed_days=1, i=1)])
    out3 = core.draw_angles(w3.client, PID, 1, user_id=ME, seed=2, repeat_of=[key])
    assert "error" not in out3 and out3["delivered"] == 1 and len(w3.inserts()) == 1


def test_only_repeats_needs_no_rpc():
    dims = {"emotional_lever": "好奇驱动", "human_truth_archetype": "自我提升", "content_format": "教程攻略",
            "title_structure": "数字清单"}
    old = _row(dims, days=4, consumed_days=3, i=1)
    w = _World([old])
    out = core.draw_angles(w.client, PID, 1, user_id=ME, repeat_of=[old["angle_key"]])
    assert out["delivered"] == 1 and out["atomic_reservation"] is True and out["warning"] == ""
    assert not w.client.rpc_calls


# ══════════════════════════════════════════════════════════════════════
# 回包
# ══════════════════════════════════════════════════════════════════════

def test_return_shape_status_note_policy_and_references(monkeypatch):
    prof = _run_profile(ext=_ext_pool(8), hits=_hits(5))
    _fixed_profile(monkeypatch, prof)
    seen_refs = set()
    for seed in range(6):
        w = _World()
        out = core.draw_angles(w.client, PID, 10, user_id=ME, seed=seed)
        assert out["delivered"] == 10
        for k in ("angles", "requested", "delivered", "prompt_block", "angle_pool_source", "atomic_reservation",
                  "combination_space", "note", "warning", "project_status", "batch_note", "policy"):
            assert k in out, k
        assert out["policy"]["name"] == "v2" and out["policy"]["stage"] == "run" and out["policy"]["prior"] == "live"
        assert out["policy"]["batch"] == w.seen[0][0]["dims"]["_draw"]["b"]
        assert out["project_status"].startswith("近60天发了224篇") and len(out["project_status"]) <= 120
        assert out["prompt_block"].splitlines()[1] == "本项目现在：" + out["project_status"]
        assert out["batch_note"].startswith("新抽的这10篇：") and "试新" not in out["batch_note"]
        for a in out["angles"]:
            assert set(a["dims"]) == {"emotional_lever", "human_truth_archetype", "content_format", "title_structure"}
            src = next(c["dims"]["_draw"]["src"] for c in w.seen[0] if c["angle_key"] == a["angle_key"])
            if src in ("e", "h"):
                assert a["reference"]["kind"] == {"e": "external", "h": "hit"}[src]
                seen_refs.add(src)
            else:
                assert "reference" not in a, "只有 e / h / rep 带参考"
        assert '"_draw"' not in json.dumps(out, ensure_ascii=False) and "ext:fx" not in json.dumps(out)
        _assert_writer_safe(out, "draw_angles v2")
    assert seen_refs == {"e", "h"}


def test_the_ledger_carries_each_batch_plan_for_the_report(monkeypatch):
    """台账上每张 v2 牌都带这一批的整张计划(n 和各来源份额), draw-report 的「来源计划」= 每批 n·份额、每批一次
    (review on aw#140)。两批各 10 张。"""
    import random

    from deskcore import drawreport
    prof = _run_profile(ext=_ext_pool(8), hits=_hits(5))
    _fixed_profile(monkeypatch, prof)
    shares = dp.slot_plan(10, prof, random.Random(0))["shares"]
    assert sum(1 for v in shares.values() if v > 0) == 4, "四种来源都在计划里"
    w = _World()
    for seed in (3, 4):
        assert core.draw_angles(w.client, PID, 10, user_id=ME, seed=seed)["delivered"] == 10
    cards = drawreport.card_rows(w.client.rows["angle_ledger"], now=NOW)
    h = drawreport.health(cards, now=NOW)[PID]
    assert sum(h["sources"].values()) == 20
    # 计划按每批实际排出来的槽位数(pk)记: 假 RPC 把主牌都留下了, 计划张数就等于实际落下来的张数; 和 n·份额差不过
    # 取整那一点(复审 on aw#140)
    assert {k: int(v) for k, v in h["sources_planned"].items()} == h["sources"]
    for k, v in shares.items():
        if v > 0:
            assert abs(h["sources_planned"].get(k, 0) - 20 * v) <= 2


def test_short_batches_say_so(monkeypatch):
    w = _World(cap=2)
    out = core.draw_angles(w.client, PID, 5, user_id=ME, seed=1)
    assert out["delivered"] == 2 and out["note"] == project_now.SHORT_NOTE


def test_n_and_avoid_days_are_clamped():
    w = _World()
    out = core.draw_angles(w.client, PID, 45, user_id=ME, seed=1, avoid_days=3)
    assert out["requested"] == 30 and out["delivered"] == 30
    assert core.DRAW_CLAMPED_NOTE.format(max=30) in out["note"]
    assert w.client.rpc_calls[-1][1]["_avoid_days"] == 14
    core.draw_angles(w.client, PID, 2, user_id=ME, seed=1, avoid_days=400)
    assert w.client.rpc_calls[-1][1]["_avoid_days"] == 90
    assert core.draw_angles(w.client, PID, 0, user_id=ME) == {"angles": [], "requested": 0, "delivered": 0}


def test_the_tool_passes_pin_and_repeat_of_through(monkeypatch):
    from deskcore import tools
    seen = {}

    def fake(client, pid, n, **kw):
        seen.update(kw, n=n)
        return {"error": "pin_invalid", "message": "x", "angles": [], "requested": n, "delivered": 0}
    monkeypatch.setattr(core, "sb", lambda: None)
    monkeypatch.setattr(core, "draw_angles", fake)
    out = tools.draw_angles(PID, 4, pin={"标题句式": "疑问句"}, repeat_of=["k"], _user_id=ME)
    assert seen["pin"] == {"标题句式": "疑问句"} and seen["repeat_of"] == ["k"] and seen["n"] == 4
    assert "archive_pending" not in out, "报错的那次什么都没发, 不提醒存档"
    doc = tools.draw_angles.__doc__
    for must in ("batch_note", "project_status", "只在用户明确要求时用", "pin", "repeat_of", "原样说一次"):
        assert must in doc, must
    assert "先各轮一遍再重复" not in doc


# ══════════════════════════════════════════════════════════════════════
# 复审回合
# ══════════════════════════════════════════════════════════════════════

def test_tv_down_but_mapping_readable_deals_inside_the_quota(monkeypatch):
    """重启后缓存是空的、TV 读不到, 写作台这边的对照表读得到: 按快照里本项目那一条的配比发, 不按 8 种形式平均发
    (§0 的阻断项: 配额外的形式)。卡上记 pp=table, 状态行说这次读不到最新的数据。"""
    for fn in ("tv_notes_outcomes", "tv_title_answers", "tv_projects_meta", "tv_notes_profile", "note_answers",
               "external_slice"):
        monkeypatch.setattr(store, fn, lambda *a, **k: None)
    monkeypatch.setattr(store, "tv_projects_of", lambda *a, **k: [{"tv_project_id": "XIWU_phase2"}])
    snap = priors.load_snapshot()["projects"]["XIWU_phase2"]["mix_recent"]["content_format"]
    support = {v for v, c in snap.items() if c / sum(snap.values()) >= 0.03}
    for seed in range(5):
        w = _World()
        out = core.draw_angles(w.client, PID, 10, user_id=ME, seed=seed)
        assert out["delivered"] == 10 and out["policy"]["name"] == "v2" and out["policy"]["prior"] == "snap"
        assert {a["dims"]["content_format"] for a in out["angles"]} <= support
        assert out["project_status"] == project_now.STATUS_TABLE
        assert {c["dims"]["_draw"].get("pp") for c in w.seen[0]} == {"table"}


@pytest.mark.parametrize("bypass_filter", [False, True])
def test_a_reference_that_renders_no_line_is_not_attached_or_counted(monkeypatch, bypass_filter):
    """起量帖的特征题没读到(desc 全空)/ 开头是「其他」、几个是非题都是否: 卡上不会有参考行, angles[].reference 就不带、
    batch_note 也不说「参照了…的结构」。bypass_filter: 绕过发牌那一道筛, 渲染前那一道也挡得住。"""
    blank = {"opening_type": "其他", "ending_asks_reader": False, "asks_for_help": None, "turning_point": False}
    hits = [dict(h, desc=dict(blank)) for h in _hits(5)]
    _fixed_profile(monkeypatch, _run_profile(hits=hits))
    if bypass_filter:
        monkeypatch.setattr(dp, "hit_anchors", lambda prof: list((prof or {}).get("hits") or []))
    for seed in range(5):
        w = _World()
        out = core.draw_angles(w.client, PID, 10, user_id=ME, seed=seed)
        assert out["delivered"] == 10
        assert all("reference" not in a for a in out["angles"])
        assert "参照" not in out["batch_note"] and "参考结构" not in out["prompt_block"]
