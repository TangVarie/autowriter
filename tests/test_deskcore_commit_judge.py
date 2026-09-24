"""commit_drafts 的入库判定(judge, 影子期, 2026-09-24)。

设计审查 2026-09-23 §5 列了四条, 这里一条一条钉成行为(不钉源码, D-051):

  1. **位置**: 判定不在项目写锁里、也不在恢复指纹的那个大 try 里。判定一抛, 那个
     except 会把已替换稿的旧指纹放回去(新旧两版并存、下一版判自己撞车), 再 raise
     出去让调用方重试、撞上自己刚写的指纹; 就算自己包了 try, 锁里多等的 8 秒也全算进
     同项目其它 commit / 补录的等锁预算。
  2. **名单**: 调用方自带 version_id 的稿子不在 ``version_ids`` 里, 照 version_ids 判
     会整批漏掉; ``identity_error`` 没建成 versions 行的要跳过(账本 subject_id 会
     指向不存在的行)。
  3. **延迟**: 多篇并行、共用一个截止时间, 不能串成 N×8 秒。
  4. **影子期**: 判定怎么失败, commit 的结果都一个字不变。

外加覆盖面(§4 #1): 补录路径(ingest_published)不判。
"""

from __future__ import annotations

import contextlib
import itertools
import threading
import time
import uuid

import pytest
import requests

import config
import judge_client
from deskcore import core, store
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
PROJ = "aaaaaaaa-0000-0000-0000-000000000001"
CALLER_VID = "cccccccc-0000-0000-0000-00000000000c"

BODY_A = ("今天下班路上想到一件事, 留学生秋招最费时间的其实不是投简历, "
          "而是等一个根本不会来的回音, 等到后来连刷新邮箱都成了一种自我安慰。") * 3
BODY_A2 = ("今天下班路上想到一件事, 留学生秋招最费时间的其实不是投简历, "
           "而是等一个根本不会来的回音, 等到后来连刷新邮箱都成了一种自我安慰。"
           "改了结尾: 后来我把邮箱关了三天, 反而收到了两个面试。") * 2
BODY_B = ("绩点二点八的澳八大回国找工作, 我一开始也以为没人要, 后来发现问题"
          "出在简历的第一行, 那行字把面试官最想看的东西藏到了最后。") * 3
BODY_C = ("凌晨收到转正邮件的时候我在屋里发了很久的呆, 三个月前那个被群面刷掉"
          "三次的人, 和现在盯着屏幕的人, 中间隔着一整个秋天的练习。") * 3


_POOL = ("的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会"
         "可主发年动同工也能下过子说产种面而方后多定行学法所民得经十三之进着等部"
         "度家电力里如水化高自二理起小物现实加量都两体制机当使点从业本去把性好应")


def _distinct_drafts(n: int) -> list[dict]:
    """n 篇互不撞车的稿子(各自一串随机字, 同 sql_parity_check 的造法) —— 这几条要测的
    是并发与截止, 不能让查重闸先拦掉几篇。"""
    import random
    out = []
    for i in range(n):
        rng = random.Random(1000 + i)
        body = "".join(f"这一段说的是{''.join(rng.choice(_POOL) for _ in range(18))}。"
                       for _ in range(12))
        out.append({"title": f"第{i}篇{''.join(rng.choice(_POOL) for _ in range(6))}",
                    "body": body})
    return out


class _TVClient(FakeClient):
    """多一个 ``schema("truth_vault")``: TV 的项目表(品类)从那里读。"""

    def __init__(self, *, tv_projects=None, **kw):
        super().__init__(**kw)
        self._tv = FakeClient(rows={"projects": list(tv_projects or [])})
        self.schema_calls: list[str] = []

    def schema(self, name):
        self.schema_calls.append(name)
        assert name == "truth_vault"
        return self._tv


def _client(*, maps=None, tv_projects=None) -> _TVClient:
    c = _TVClient(
        rows={"projects": [{"id": PROJ, "name": "途鸽", "brand": "途鸽", "owner_id": ME,
                            "calibration_notes": "", "tactics": "[]", "custom_roles": []}],
              "tv_project_map": list(maps if maps is not None else [
                  {"tv_project_id": "TUGE_phase1", "project_id": PROJ, "ingest_target": True}])},
        tv_projects=tv_projects if tv_projects is not None else [
            {"project_id": "TUGE_phase1", "category": "教育"}])
    c.rpc_impl["deskcore_commit_fingerprints"] = _rpc_that_writes(c)
    return c


def _rpc_that_writes(c):
    """RPC 判 inserted 并把指纹真写进假库 —— 替换那几条要看"旧指纹摘了、新的在"。"""
    def _impl(args):
        out = []
        for i, r in enumerate(args.get("_rows") or []):
            c.rows.setdefault("draft_fingerprints", []).append({
                "id": f"fp-{r['version_id']}", "project_id": PROJ, "user_id": ME,
                "version_id": r["version_id"], "title": r["title"],
                "opening": r["opening"], "opening_hash": r["opening_hash"],
                "ngram_hashes": r["ngram_hashes"], "title_embedding": None,
                "embedding_model": None, "angle_key": r.get("angle_key"),
                "created_at": "2026-09-24T00:00:00+00:00"})
            out.append({"idx": i, "status": core.COMMIT_STATUS_INSERTED,
                        "collided_with": None, "detail": None})
        return out
    return _impl


def _embed(c):
    """假库没有真 join(同 test_deskcore_draft_identity): 替换前把 item/batch 嵌进 versions。"""
    items = {i["id"]: i for i in c.rows.get("items", [])}
    batches = {b["id"]: b for b in c.rows.get("batches", [])}
    for v in c.rows.get("versions", []):
        it = items.get(v.get("item_id"))
        if it:
            v["items"] = {"id": it["id"], "status": it.get("status"),
                          "decision_source": it.get("decision_source"),
                          "batch_id": it.get("batch_id"),
                          "batches": {"project_id": (batches.get(it.get("batch_id")) or {}).get("project_id", PROJ)}}
    return c


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)


@pytest.fixture
def judge_on(monkeypatch):
    """接上 judge, 并把 judge_client.judge_draft 换成录音机(默认回 ok)。"""
    monkeypatch.setattr(config, "JUDGE_URL", "https://judge.example.invalid")
    monkeypatch.setattr(config, "JUDGE_API_KEY", "jk")
    monkeypatch.setattr(config, "JUDGE_TIMEOUT_SEC", 5.0)
    rec = {"payloads": [], "reply": lambda p: judge_client.result(
        judge_client.JUDGE_OK, http_status=200, passed=True, calls=2, written=20)}
    lock = threading.Lock()

    def fake(payload, *, timeout=None):
        with lock:
            rec["payloads"].append(payload)
        return rec["reply"](payload)
    monkeypatch.setattr(judge_client, "judge_draft", fake)
    return rec


def _subject_ids(rec) -> list[str]:
    return sorted(p["subject_id"] for p in rec["payloads"])


# ══════════════════════════════════════════════════════════════════════
# 1 · 位置: 锁外、大 try 外
# ══════════════════════════════════════════════════════════════════════

def test_judge_runs_after_the_project_write_lock_is_released(judge_on, monkeypatch):
    state = {"held": False, "entered": 0}
    real_lock = core._project_write_lock

    @contextlib.contextmanager
    def tracking_lock(client, project_id, holder):
        with real_lock(client, project_id, holder) as got:
            state["held"] = True
            state["entered"] += 1
            try:
                yield got
            finally:
                state["held"] = False
    monkeypatch.setattr(core, "_project_write_lock", tracking_lock)

    held_during_judge = []
    judge_on["reply"] = lambda p: (held_during_judge.append(state["held"]),
                                   judge_client.result(judge_client.JUDGE_OK, passed=True))[1]

    out = core.commit_drafts(_client(), PROJ, [{"title": "甲", "body": BODY_A},
                                               {"title": "乙", "body": BODY_B}], user_id=ME)
    assert state["entered"] == 1, "commit 本身仍要拿项目写锁"
    assert held_during_judge == [False, False], "判定时项目写锁还没放 —— 锁里多等的都算进别人的等锁预算"
    assert out["judge"]["summary"] == {"ok": 2}


def test_a_judge_blowup_does_not_restore_the_stash_or_fail_the_commit(judge_on, monkeypatch):
    """替换成功之后判定整体炸了: 旧指纹【不许】被放回(新旧两版并存 = 下一版判自己撞车),
    入库也【不许】报失败(调用方重试会撞上自己刚写的指纹)。"""
    c = _client()
    first = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A}], user_id=ME)
    v1 = first["version_ids"][0]

    restored: list[list[int]] = []
    real_restore = core._restore_stash

    def spy(client, stash, indices):
        restored.append(list(indices))
        return real_restore(client, stash, indices)
    monkeypatch.setattr(core, "_restore_stash", spy)

    def blowup(*a, **k):
        raise RuntimeError("判定这一步整个炸了")
    monkeypatch.setattr(core, "_judge_committed", blowup)

    _embed(c)
    out = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A2,
                                        "replaces_version_id": v1}], user_id=ME)
    v2 = out["version_ids"][0]
    assert out["written"] == 1 and out["replaced"][0]["old_version_id"] == v1
    assert all(ix == [] for ix in restored), f"判定失败把旧指纹放回去了: {restored}"
    assert {f["version_id"] for f in c.rows["draft_fingerprints"]} == {v2}, "旧指纹回来了"
    assert "判定这一步整个炸了" in out["judge"]["error"]
    assert out["judge"]["shadow"] is True


# ══════════════════════════════════════════════════════════════════════
# 2 · 名单: 自带 version_id 的要判, 没建成身份的跳过
# ══════════════════════════════════════════════════════════════════════

def test_caller_supplied_version_ids_are_judged_too(judge_on):
    out = core.commit_drafts(_client(), PROJ, [
        {"title": "UI 生成的那版", "body": BODY_A, "version_id": CALLER_VID},
        {"title": "写作台新写的", "body": BODY_B},
    ], user_id=ME)
    minted = out["version_ids"]
    assert CALLER_VID not in minted and len(minted) == 1, "version_ids 的口径不变: 只报这次新建的"
    assert _subject_ids(judge_on) == sorted([CALLER_VID, minted[0]]), \
        "自带 version_id 的稿子被漏判了 —— 它不在 version_ids 里, 不能照 version_ids 判"
    by_vid = {r["version_id"]: r for r in out["judge"]["results"]}
    assert by_vid[CALLER_VID]["index"] == 0 and by_vid[minted[0]]["index"] == 1


def test_identity_error_drafts_are_skipped_not_judged(judge_on, monkeypatch):
    real_mint = store.mint_draft_identity

    def half_mint(sb, project_id, user_id, tactic, entries, **kw):
        ok = real_mint(sb, project_id, user_id, tactic, entries[:1], **kw)
        return {"batch_id": ok["batch_id"], "versions": ok["versions"],
                "error": "RuntimeError: 第二条 versions 插不进去"}
    monkeypatch.setattr(store, "mint_draft_identity", half_mint)

    out = core.commit_drafts(_client(), PROJ, [{"title": "甲", "body": BODY_A},
                                               {"title": "乙", "body": BODY_B}], user_id=ME)
    assert "identity_warning" in out and len(out["version_ids"]) == 1
    assert _subject_ids(judge_on) == out["version_ids"], "没建成 versions 行的也发出去了"
    assert [(s["index"], s["reason"]) for s in out["judge"]["skipped"]] == [(1, "identity_error")]


def test_rejected_and_empty_body_drafts_are_not_judged(judge_on):
    c = _client()
    core.commit_drafts(c, PROJ, [{"title": "上周发过的", "body": BODY_C}], user_id=ME)
    judge_on["payloads"].clear()
    out = core.commit_drafts(c, PROJ, [
        {"title": "换了个标题", "body": BODY_C},          # 撞历史, 闸前拒
        {"title": "只有标题", "body": ""},                  # 入库, 但判不出东西
        {"title": "正常的", "body": BODY_A},
    ], user_id=ME)
    assert [r["index"] for r in out["rejected"]] == [0]
    assert len(judge_on["payloads"]) == 1 and judge_on["payloads"][0]["title"] == "正常的"
    assert [(s["index"], s["reason"]) for s in out["judge"]["skipped"]] == [(1, "empty_body")]


def test_a_replacement_is_judged_under_its_new_version_id(judge_on):
    c = _client()
    v1 = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A}],
                            user_id=ME)["version_ids"][0]
    judge_on["payloads"].clear()
    _embed(c)
    out = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A2,
                                        "replaces_version_id": v1}], user_id=ME)
    assert _subject_ids(judge_on) == [out["version_ids"][0]] != [v1]


def test_a_replacement_that_did_not_attach_is_skipped(judge_on, monkeypatch):
    c = _client()
    v1 = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A}],
                            user_id=ME)["version_ids"][0]
    judge_on["payloads"].clear()

    def _fail(*a, **k):
        raise RuntimeError("versions 插不进去")
    monkeypatch.setattr(store, "add_version_to_item", _fail)
    _embed(c)
    out = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A2,
                                        "replaces_version_id": v1}], user_id=ME)
    assert "replace_warning" in out and judge_on["payloads"] == []
    assert out["judge"]["skipped"][0]["reason"] == "replace_failed"


# ══════════════════════════════════════════════════════════════════════
# 3 · 项目号与品类(数据出境口径 docs/00 #7)
# ══════════════════════════════════════════════════════════════════════

def test_tv_project_and_category_are_sent(judge_on):
    c = _client()
    out = core.commit_drafts(c, PROJ, [{"title": "甲", "body": BODY_A}], user_id=ME)
    p = judge_on["payloads"][0]
    assert p["project"] == "TUGE_phase1" and p["category"] == "教育"
    assert p["subject_type"] == "aw_version" and p["write"] is True
    assert (out["judge"]["project"], out["judge"]["category"]) == ("TUGE_phase1", "教育")
    assert c.schema_calls == ["truth_vault"]


def test_any_prescription_mapping_makes_the_category_prescription(judge_on):
    """一个写作台项目对着两个 TV 项目、品类不一致: 出境这件事上宁严勿松。
    project 取补录目标那一行。"""
    c = _client(maps=[{"tv_project_id": "A_phase1", "project_id": PROJ, "ingest_target": False},
                      {"tv_project_id": "B_phase1", "project_id": PROJ, "ingest_target": True}],
                tv_projects=[{"project_id": "A_phase1", "category": "处方药"},
                             {"project_id": "B_phase1", "category": "OTC药"}])
    core.commit_drafts(c, PROJ, [{"title": "甲", "body": BODY_A}], user_id=ME)
    p = judge_on["payloads"][0]
    assert p["project"] == "B_phase1" and p["category"] == "处方药"


def test_category_lookup_failure_still_sends_the_project(judge_on):
    c = _client()

    def broken(name):
        raise RuntimeError("PGRST106 The schema must be one of the following: autowriter")
    c.schema = broken
    out = core.commit_drafts(c, PROJ, [{"title": "甲", "body": BODY_A}], user_id=ME)
    p = judge_on["payloads"][0]
    assert p["project"] == "TUGE_phase1" and "category" not in p
    assert out["judge"]["summary"] == {"ok": 1}


def test_no_tv_mapping_means_nothing_leaves_the_building(judge_on, monkeypatch):
    """没接 tv-map = 判不出项目号和品类。用写作台 UUID 凑一个的话, judge 认不出它、
    只能按未清关跑通用层 —— 一个没接 tv-map 的处方药项目的草稿就这样发给了 Jev。"""
    out = core.commit_drafts(_client(maps=[]), PROJ, [{"title": "甲", "body": BODY_A}],
                             user_id=ME)
    assert judge_on["payloads"] == []
    r = out["judge"]["results"][0]
    assert r["judge_status"] == judge_client.JUDGE_NOT_CONFIGURED
    assert "tv-map add" in out["judge"]["detail"], "要把补救命令说出来, 而且整批只说一次"
    assert "detail" not in r, "每篇重复同一句话只会把返回值撑长"


def test_not_configured_sends_nothing_and_reads_nothing(monkeypatch):
    monkeypatch.setattr(config, "JUDGE_URL", "")
    monkeypatch.setattr(config, "JUDGE_API_KEY", "")
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("没配置却发了请求"))
    monkeypatch.setattr(store, "tv_projects_of", lambda *a, **k: pytest.fail("没配置却去查了 tv-map"))
    out = core.commit_drafts(_client(), PROJ, [{"title": "甲", "body": BODY_A},
                                               {"title": "乙", "body": BODY_B}], user_id=ME)
    assert out["judge"]["summary"] == {"not_configured": 2}
    assert out["written"] == 2
    # 没接 judge 的部署每次 commit 都会带这一块, 行要短: 只有定位与结局
    assert all(set(r) == {"index", "version_id", "judge_status"} for r in out["judge"]["results"]), \
        out["judge"]["results"]


# ══════════════════════════════════════════════════════════════════════
# 4 · 延迟: 并行 + 共同截止; 以及 elapsed_ms
# ══════════════════════════════════════════════════════════════════════

def test_judging_is_parallel_and_bounded_by_one_shared_deadline(judge_on, monkeypatch):
    """6 篇、每篇 0.4 秒: 串行要 2.4 秒。并行应在一篇的时间左右做完。
    再放一篇永远不回的: 它在截止时被记成 timeout, 整批不因它多等。"""
    monkeypatch.setattr(config, "JUDGE_TIMEOUT_SEC", 1.5)
    release = threading.Event()

    def reply(p):
        if p["title"] == "卡住的":
            release.wait(10)
            return judge_client.result(judge_client.JUDGE_OK, passed=True)
        time.sleep(0.4)
        return judge_client.result(judge_client.JUDGE_OK, passed=True)
    judge_on["reply"] = reply

    drafts = _distinct_drafts(7)
    drafts[6]["title"] = "卡住的"
    try:
        out = core.commit_drafts(_client(), PROJ, drafts, user_id=ME)
    finally:
        release.set()
    assert out["written"] == 7, out.get("rejected")
    j = out["judge"]
    assert j["summary"] == {"ok": 6, "timeout": 1}, j["summary"]
    stuck = [r for r in j["results"] if r["index"] == 6][0]
    assert stuck["judge_status"] == judge_client.JUDGE_TIMEOUT
    # 截止 1.5 秒 + 余量; 串行的话光前 6 篇就 2.4 秒(而且会有几篇记成 timeout)
    assert out["phase_ms"]["judge"] < 2500, out["phase_ms"]


def test_queued_drafts_past_the_deadline_are_never_sent(judge_on, monkeypatch):
    """并发只有 2、截止 1 秒、每篇 0.6 秒: 第三、四篇在截止前开跑但回不来(timeout),
    第五、六篇还在排队 —— 它们要被取消, 一个请求都不许再发出去。"""
    monkeypatch.setattr(config, "JUDGE_TIMEOUT_SEC", 1.0)
    monkeypatch.setattr(config, "JUDGE_MAX_WORKERS", 2)

    def reply(p):
        time.sleep(0.6)
        return judge_client.result(judge_client.JUDGE_OK, passed=True)
    judge_on["reply"] = reply
    drafts = _distinct_drafts(6)
    out = core.commit_drafts(_client(), PROJ, drafts, user_id=ME)
    assert out["written"] == 6, out.get("rejected")
    time.sleep(1.0)                      # 让在飞的那两个跑完, 再数发出去了几个
    assert out["judge"]["summary"] == {"ok": 2, "timeout": 4}, out["judge"]["summary"]
    assert len(judge_on["payloads"]) == 4, "排队没轮到的也发出去了"


def test_elapsed_ms_and_phases_are_reported(judge_on):
    out = core.commit_drafts(_client(), PROJ, [{"title": "甲", "body": BODY_A}], user_id=ME)
    ph = out["phase_ms"]
    assert set(ph) == {"lock_wait", "commit", "judge", "total"}
    assert all(isinstance(v, int) and v >= 0 for v in ph.values())
    assert out["elapsed_ms"] == ph["total"] >= ph["commit"]


# ══════════════════════════════════════════════════════════════════════
# 5 · 影子期: commit 的结果一个字不变
# ══════════════════════════════════════════════════════════════════════

_JUDGE_KEYS = {"judge", "elapsed_ms", "phase_ms"}


def _deterministic_uuid(monkeypatch):
    counter = itertools.count(1)
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=next(counter)))


def _run(monkeypatch, *, configure: bool, post=None) -> dict:
    _deterministic_uuid(monkeypatch)
    monkeypatch.setattr(config, "JUDGE_URL", "https://judge.example.invalid" if configure else "")
    monkeypatch.setattr(config, "JUDGE_API_KEY", "jk" if configure else "")
    if post is not None:
        monkeypatch.setattr(requests, "post", post)
    c = _client()
    core.commit_drafts(c, PROJ, [{"title": "上周发过的", "body": BODY_C}], user_id=ME)
    return core.commit_drafts(c, PROJ, [
        {"title": "撞历史的", "body": BODY_C},
        {"title": "甲", "body": BODY_A, "angle_key": "k1"},
        {"title": "UI 版", "body": BODY_B, "version_id": CALLER_VID},
    ], user_id=ME)


def _timeout(*a, **k):
    raise requests.Timeout("read timed out")


def _policy(*a, **k):
    class R:
        status_code = 403

        @staticmethod
        def json():
            return {"detail": "policy: 处方药项目的未发布稿不跑"}
    return R()


def _explode(*a, **k):
    raise RuntimeError("judge 那边整个挂了")


@pytest.mark.parametrize("post,want", [(_timeout, "timeout"), (_policy, "policy_blocked"),
                                       (_explode, "error")])
def test_commit_result_is_identical_whatever_the_judge_does(monkeypatch, post, want):
    """走真的 judge_client(只换掉 requests.post): 超时 / 被出境口径挡 / 整个挂了,
    commit 返回的每一个字段都与没接 judge 时一样 —— 除了多出来的 judge 和计时。"""
    baseline = _run(monkeypatch, configure=False)
    judged = _run(monkeypatch, configure=True, post=post)
    assert {k: v for k, v in judged.items() if k not in _JUDGE_KEYS} == \
           {k: v for k, v in baseline.items() if k not in _JUDGE_KEYS}
    assert judged["judge"]["summary"] == {want: 2}
    assert baseline["judge"]["summary"] == {"not_configured": 2}
    assert judged["judge"]["note"] == core.JUDGE_SHADOW_NOTE


# ══════════════════════════════════════════════════════════════════════
# 6 · 落 batch_metrics(耗时样本 + 判定结局)
# ══════════════════════════════════════════════════════════════════════

def test_a_metrics_row_records_timing_and_judge_outcomes(judge_on):
    c = _client()
    out = core.commit_drafts(c, PROJ, [{"title": "甲", "body": BODY_A},
                                       {"title": "只有标题", "body": ""}], user_id=ME)
    rows = c.rows["batch_metrics"]
    assert len(rows) == 1
    m = rows[0]
    assert m["batch_id"] is None, "挂到批次上会让历史页给写作台批次显示一块全是 0 的性能指标"
    assert m["project_id"] == PROJ and m["user_id"] == ME
    assert m["meta"]["mode"] == "deskcore_commit" and m["meta"]["batch_id"] == out["batch_id"]
    assert m["phase_ms"] == out["phase_ms"]
    assert m["counters"]["judge_ok"] == 1 and m["counters"]["judge_skipped"] == 1
    assert [j["status"] for j in m["meta"]["judge"]] == ["ok"]
    assert m["meta"]["judge_skipped"] == [{"index": 1, "reason": "empty_body"}]


def test_a_metrics_failure_does_not_fail_the_commit(judge_on, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("batch_metrics 42501")
    monkeypatch.setattr(store, "insert_commit_metrics", boom)
    out = core.commit_drafts(_client(), PROJ, [{"title": "甲", "body": BODY_A}], user_id=ME)
    assert out["written"] == 1 and out["judge"]["summary"] == {"ok": 1}


# ══════════════════════════════════════════════════════════════════════
# 7 · 覆盖面: 补录路径不判(设计审查 §4 #1 —— 已发布的由 TV 事后抽取覆盖)
# ══════════════════════════════════════════════════════════════════════

def test_ingest_published_never_calls_the_judge(judge_on):
    out = core.ingest_published(_client(), PROJ, [
        {"title": "发出去的一", "body": "第一篇的正文, 已经在小红书上了。" * 5},
    ], user_id=ME, source="tv:TUGE_phase1")
    assert out["fingerprinted"] == 1
    assert judge_on["payloads"] == [], "补录的是已发布笔记, 由 TV 的事后抽取覆盖, 不走 judge"
    assert "judge" not in out
