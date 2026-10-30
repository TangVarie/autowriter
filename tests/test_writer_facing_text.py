"""回给模型、要它转告写手的话里, 不许有工程指令(2026-10-30 发布收尾)。

写手的反馈是写作台"像没做完": 每场开头和每批交付都听到一串看不懂、也做不了的话 ——
「跑 `python -m deskcore.cli backfill --project <id>` 补上」「要运维配 JUDGE_URL」「要运维 tv-map add」
「服务端日志有堆栈」。模型照念, 因为那就写在它要转告的字段里(degraded_note / next_step / detail / note / warning)。
写手手里没有任何一步能补这些, 而且写作台本来就没有要写手做的那一步(D-120)。

所以这里钉的是**形态**: 把查重降级、规则没编译、判定没接上、存档半途失败这些路径都走一遍, 把返回值里
**每一个字符串值**(键名是机器字段, 不算)拿出来, 不许出现命令、环境变量、迁移号、表名或"找运维 / 工程侧"。
工程上要的那句补救照样有, 只是改进了日志(这里也顺手断言它还在, 免得"删掉"冒充"挪走")。

布尔 / 计数这些机器字段(semantic_degraded / uncompiled_reason / judge_status …)保持原样, 也在这里断言。
"""

from __future__ import annotations

import logging
import threading

import pytest

import config
from deskcore import core, store, tools as T
from tests.fakes import FakeClient
from tests.test_rule_compile import ME, PROJ, make_client, mem

import dedup

# 不许出现在念给写手的话里的东西。"reembed --" 带空格和横杠: 工具名 reembed_my_rules 是模型自己能调的, 不算。
FORBIDDEN = ("python -m", "deskcore.cli", "backfill", "tv-map", "reembed --", "compile-rules",
             "运维", "工程侧", "转给工程", "服务端日志", "服务日志", "堆栈", "migrations/",
             "GOOGLE_API_KEY", "JUDGE_URL", "JUDGE_API_KEY", "RULE_COMPILE_AUTO", "tv_project_map", "pgvector")

BODIES = [
    ("秋招投了六十份简历没回音", "投到第六十份的时候我开始怀疑是不是邮箱坏了, 后来发现坏的是简历第一行。" * 4),
    ("绩点二点八也拿到了终面", "绩点这东西在群面之后就没人再提了, 面试官只问我上一段实习做成了什么。" * 4),
]


def _strings(obj, path="$"):
    """返回值里所有的字符串值, 连同它在哪。键名不算(那是机器字段)。"""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _strings(v, f"{path}[{i}]")


def _assert_writer_safe(out, where: str) -> None:
    bad = [(p, f, s[:160]) for p, s in _strings(out) for f in FORBIDDEN if f in s]
    assert not bad, f"{where}: 念给写手的话里有工程指令: {bad}"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """不接判定、不接 embedding、不起后台补编 —— 正好是这几条降级路径。"""
    monkeypatch.setattr(config, "JUDGE_URL", "")
    monkeypatch.setattr(config, "RULE_COMPILE_AUTO", False)
    monkeypatch.setattr(core, "_RULE_COMPILE_STATE", {})
    monkeypatch.setattr(core, "_RULE_COMPILE_FAILED", {})
    monkeypatch.setattr(core, "_RULE_COMPILE_SLOTS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(core, "_RULE_COMPILE_JEV", {"slow_until": 0.0})
    monkeypatch.setattr(core, "_RULE_COMPILE_INFLIGHT", {})
    monkeypatch.setattr(dedup, "embeddings_available", lambda: False)


# ══════════════════════════════════════════════════════════════════════
# 1 · 一场写作从头走到尾: 开项目 → 发牌 → 查重 → 存档 → 规则检查
# ══════════════════════════════════════════════════════════════════════

def test_a_whole_session_in_degraded_mode_says_nothing_an_engineer_should_do(monkeypatch, caplog):
    """embedding 不可用(查重降级)、指纹库是空的、发牌 / 存档的 RPC 都不在(非原子)、判定没接、
    项目里有还没编译的规则 —— 每一个工具的返回都照样能念给写手听。"""
    c = make_client([mem(8, "示例硬规则: 刚记下还没编", severity="hard"), mem(9, "示例软规则: 也还没编")], [])
    monkeypatch.setattr(core, "sb", lambda: c)

    with caplog.at_level(logging.WARNING):
        brief = T.open_project(PROJ, _user_id=ME)
        _assert_writer_safe(brief, "open_project")
        assert brief["p0"], "简报本身照常"

        drawn = T.draw_angles(PROJ, 2, _user_id=ME)
        _assert_writer_safe(drawn, "draw_angles")
        keys = [a["angle_key"] for a in drawn["angles"]]
        drafts = [{"title": t, "body": b, "angle_key": k} for (t, b), k in zip(BODIES, keys)]

        checked = T.check_drafts(PROJ, drafts, _user_id=ME)
        _assert_writer_safe(checked, "check_drafts")
        s = checked["summary"]
        assert s["semantic_degraded"] is True, "机器字段照旧"
        assert s["degraded_note"] == "这次标题语义查重没跑全, 已记下; 交付照常, 不用你做什么。"
        assert s["empty_history_warning"]

        scored = T.score_drafts(PROJ, drafts, _user_id=ME)
        _assert_writer_safe(scored, "score_drafts")
        assert scored["written"] == 2
        assert scored["gate_summary"]["semantic_degraded"] is True
        assert scored["judge"]["summary"] == {"not_configured": 2}

        ruled = T.check_rules(PROJ, scored["check_rules_version_ids"], _user_id=ME)
        _assert_writer_safe(ruled, "check_rules")
        assert ruled["rules"]["uncompiled_reason"] == "judge_not_configured", ruled["rules"]
        assert "这次查不了" in ruled["next_step"]

    # 补救没丢: 挪进了日志
    assert "backfill --project" in caplog.text, "空指纹库的补救命令要进日志"
    assert "JUDGE_URL" in caplog.text, "没配 Jev 的原因要进日志"


# ══════════════════════════════════════════════════════════════════════
# 2 · 查重降级的几种来路
# ══════════════════════════════════════════════════════════════════════

def _fp(rid, *, vec=None):
    return {"id": rid, "project_id": PROJ, "title": f"历史标题{rid}", "opening": "",
            "title_embedding": list(vec) if vec else None,
            "embedding_model": dedup.EMBEDDING_MODEL if vec else None,
            "opening_hash": None, "ngram_hashes": [], "created_at": "2026-10-30T00:00:00Z"}


def _desk(history=()) -> FakeClient:
    return FakeClient(rows={"projects": [{"id": PROJ, "name": "p", "owner_id": ME, "calibration_notes": "",
                                          "tactics": "[]", "custom_roles": []}],
                            "draft_fingerprints": list(history)})


def test_history_rows_without_vectors_degrade_quietly(monkeypatch, caplog):
    """有标题的历史行没向量: degraded 照报(机器字段), 写手听到的是一句"没跑全、已记下"; reembed 的指路进日志。"""
    monkeypatch.setattr(dedup, "embeddings_available", lambda: True)
    monkeypatch.setattr(dedup, "embed_texts", lambda ts: [[0.3, 0.4] for _ in ts])
    c = _desk([_fp("a", vec=(0.4, -0.3)), _fp("no-vec")])
    with caplog.at_level(logging.WARNING, logger="deskcore.core"):
        out = core.check_drafts(c, PROJ, [{"title": t, "body": b} for t, b in BODIES], user_id=ME)
    _assert_writer_safe(out, "check_drafts")
    assert out["summary"]["semantic_degraded"] is True and out["summary"]["history_missing_embedding"] == 1
    assert out["summary"]["degraded_note"] == "这次标题语义查重没跑全, 已记下; 交付照常, 不用你做什么。"
    assert f"reembed --project {PROJ}" in caplog.text


def test_truncated_history_warning_is_writer_safe(monkeypatch, caplog):
    """Python 兜底路径撞到上限: 照样说"只比了最近这些", 但"下推到数据库 / migrations/004"只进日志。"""
    monkeypatch.setattr(store, "fingerprints", lambda sb, pid, limit=4000: ([_fp("a")], True))
    with caplog.at_level(logging.WARNING, logger="deskcore.core"):
        out = core.check_drafts(_desk(), PROJ, [{"title": BODIES[0][0], "body": BODIES[0][1]}], user_id=ME)
    _assert_writer_safe(out, "check_drafts")
    assert out["summary"]["history_truncated"] is True and out["summary"]["history_truncated_warning"]
    assert "migrations/004" in caplog.text


def test_score_drafts_embedding_failure_is_writer_safe(monkeypatch, caplog):
    """配了 embedding 却没拿到向量: 写手听到"这次没算出来、已记下", reembed 命令进日志。"""
    monkeypatch.setattr(dedup, "embeddings_available", lambda: True)
    monkeypatch.setattr(dedup, "embed_texts", lambda ts: None)
    c = _desk()
    c.rpc_impl["deskcore_commit_fingerprints"] = lambda args: [
        {"idx": i, "status": core.COMMIT_STATUS_INSERTED, "collided_with": None, "detail": None}
        for i in range(len(args.get("_rows") or []))]
    with caplog.at_level(logging.WARNING, logger="deskcore.core"):
        out = core.commit_drafts(c, PROJ, [{"title": BODIES[0][0], "body": BODIES[0][1]}], user_id=ME)
    _assert_writer_safe(out, "score_drafts")
    assert out["written"] == 1 and out["embedding_warning"]
    assert f"reembed --project {PROJ}" in caplog.text


# ══════════════════════════════════════════════════════════════════════
# 3 · 规则没编译 / 读不出来: next_step 只说"这次查不了", 不叫人去配什么
# ══════════════════════════════════════════════════════════════════════

V1 = "cccccccc-0000-4000-8000-0000000000c1"


def _version(vid=V1):
    return {"id": vid, "title": BODIES[0][0], "body": BODIES[0][1],
            "items": {"id": f"item-{vid[:8]}", "batches": {"project_id": PROJ}}}


def _rules_client(*, maps=True):
    return make_client([mem(8, "示例规则: 刚记下还没编", severity="hard")], [], maps=maps,
                       versions=[_version()])


@pytest.mark.parametrize("case, reason", [
    ("judge_not_configured", "judge_not_configured"),
    ("no_tv_map", "no_tv_map"),
    ("auto_off", "auto_off"),
])
def test_rules_not_compiled_paths_are_writer_safe(monkeypatch, caplog, case, reason):
    if case != "judge_not_configured":
        monkeypatch.setattr(config, "JUDGE_URL", "https://judge.example.invalid/")
        monkeypatch.setattr(config, "JUDGE_API_KEY", "jk-test")
    c = _rules_client(maps=case != "no_tv_map")
    with caplog.at_level(logging.WARNING, logger="deskcore.core"):
        out = core.check_rules(c, PROJ, [V1], user_id=ME)
    _assert_writer_safe(out, f"check_rules[{case}]")
    assert out["status"] == "nothing_to_check" and out["rules"]["uncompiled_reason"] == reason, out["rules"]
    assert "这次查不了" in out["next_step"] and "已记下" in out["next_step"]
    # 补救去了日志
    want = {"judge_not_configured": "JUDGE_URL", "no_tv_map": "tv-map add", "auto_off": "compile-rules"}[case]
    assert want in caplog.text, caplog.text


def test_unreadable_rule_compiles_is_writer_safe():
    c = _rules_client()
    c.missing_columns = {"rule_compiles": {"memory_id"}}
    out = core.check_rules(c, PROJ, [V1], user_id=ME)
    _assert_writer_safe(out, "check_rules[unreadable]")
    assert out["status"] == "error" and out["next_step"] == core.CHECK_RULES_UNREADABLE_STEP


def test_compile_rules_cli_keeps_the_remedy():
    """CLI 的 compile-rules 是工程侧自己看的: 没接 tv-map 时那句补救命令照样要在(ops_detail)。"""
    ctx = core._judge_project_context(_rules_client(maps=False), PROJ)
    assert ctx["project"] is None
    _assert_writer_safe({"detail": ctx["detail"]}, "judge detail")
    assert "tv-map add" in ctx["ops_detail"]


# ══════════════════════════════════════════════════════════════════════
# 4 · 其它直接念给写手的兜底话
# ══════════════════════════════════════════════════════════════════════

def test_safe_read_fallback_hint_is_writer_safe():
    out = T._safe(lambda: 1 / 0)
    assert out["error"].startswith("ZeroDivisionError")
    _assert_writer_safe({"hint": out["hint"]}, "_safe hint")


def test_judge_draft_not_configured_is_writer_safe():
    out = core.judge_draft(_rules_client(), PROJ, BODIES[0][0], BODIES[0][1], user_id=ME)
    assert out["judge_status"] == "not_configured"
    _assert_writer_safe(out, "judge_draft")
