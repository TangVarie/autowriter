"""`ingest_published` 作为**写作台工具**(MCP/REST)—— 运营手里只有 WorkBuddy。

2026-09-17: 234 条已发未入库的稿子要补指纹, 起初做成了 CLI 的 ``ingest --xlsx``,
而运营没有终端。所以同一个 core 函数再挂成工具: 运营把飞书表粘进对话, 模型
分批调它。这里盯的是工具层那几件事: 注册了且带身份; 走过 commit 的行跳过;
一次太多要拒; 半途失败的两种情形给出的 ``note`` 方向相反(重传 vs 不许重传);
协议正文和工具说明都讲清了它跟 commit_drafts 的区别。
"""

from __future__ import annotations

import pytest

from deskcore import core
from deskcore import tools as T
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
PROJ = "aaaaaaaa-0000-0000-0000-000000000001"


@pytest.fixture
def fake(monkeypatch):
    c = FakeClient(rows={"projects": [
        {"id": PROJ, "name": "途鸽", "brand": "途鸽", "owner_id": ME,
         "calibration_notes": "", "tactics": "[]", "custom_roles": []}]})
    monkeypatch.setattr(core, "sb", lambda: c)
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    return c


def _rows(n, prefix="发过的"):
    return [{"title": f"{prefix}{i}", "body": f"第 {i} 篇的正文, 已经在小红书上了, 各不相同。" * 5}
            for i in range(n)]


def test_registered_as_a_tool_that_needs_the_caller_identity():
    assert "ingest_published" in T.TOOLS
    assert T.TOOLS["ingest_published"][1] is True, "COR-015: 新工具默认要身份"


def test_rows_that_already_went_through_commit_are_skipped(fake):
    drafts = _rows(3)
    drafts[0]["version_id"] = "v-已入库"
    drafts[1]["_source_autowriter_version_id"] = "v-也入库了"
    out = T.ingest_published(PROJ, drafts, _user_id=ME)
    assert out["received"] == 3
    assert out["skipped_already_committed"] == 2
    assert (out["minted"], out["fingerprinted"]) == (1, 1)
    assert len(fake.rows["draft_fingerprints"]) == 1
    assert "补完" in out["note"]


def test_too_many_in_one_call_is_refused_before_touching_the_db(fake):
    with pytest.raises(ValueError, match="最多"):
        T.ingest_published(PROJ, _rows(T.INGEST_MAX_PER_CALL + 1), _user_id=ME)
    assert "draft_fingerprints" not in fake.rows or not fake.rows["draft_fingerprints"]
    with pytest.raises(ValueError):
        T.ingest_published(PROJ, [], _user_id=ME)


def test_dry_run_counts_but_writes_nothing_and_says_so(fake):
    out = T.ingest_published(PROJ, _rows(4), dry_run=True, _user_id=ME)
    assert out["dry_run"] is True and out["to_write"] == 4 and out["minted"] == 0
    assert not fake.rows.get("draft_fingerprints") and not fake.rows.get("versions")
    assert "没动库" in out["note"] and "去掉 dry_run" in out["note"]


def test_repeat_call_is_idempotent(fake):
    first = T.ingest_published(PROJ, _rows(3), _user_id=ME)
    again = T.ingest_published(PROJ, _rows(3), _user_id=ME)
    assert first["minted"] == 3
    assert again["minted"] == 0 and again["skipped_already_fingerprinted"] == 3
    assert len(fake.rows["draft_fingerprints"]) == 3, "重复调不许翻倍"


def test_fingerprint_failure_says_do_not_resend(fake, monkeypatch):
    """身份建了、指纹没写: 重传会再建一份身份。note 必须是"不要重传"。"""
    def _boom(sb, rows):
        raise RuntimeError("PostgREST 502")
    monkeypatch.setattr(core.store, "write_fingerprints", _boom)
    out = T.ingest_published(PROJ, _rows(2), _user_id=ME)      # 不抛
    assert out["minted"] == 2 and out["fingerprinted"] == 0
    assert out["fingerprint_error"]
    assert "不要重传" in out["note"] and "backfill" in out["note"]


def test_identity_failure_says_resend_is_fine(fake, monkeypatch):
    monkeypatch.setattr(core.store, "mint_draft_identity",
                        lambda *a, **k: {"batch_id": None, "versions": {}, "error": "库抖了"})
    out = T.ingest_published(PROJ, _rows(2), _user_id=ME)
    assert out["minted"] == 0 and out["identity_error"]
    assert "重传即可" in out["note"] and "不要重传" not in out["note"]


def test_someone_elses_project_is_refused(fake):
    with pytest.raises(PermissionError):
        T.ingest_published(PROJ, _rows(1), _user_id="99999999-9999-9999-9999-999999999999")


# ── 协议与说明: 模型只看这两处 ────────────────────────────────────────────

def test_protocol_tells_the_model_when_this_is_not_commit_drafts():
    body, _ = T.protocol_text()
    assert "`ingest_published`" in body
    sec = body.split("`ingest_published`", 1)[1][:1200]
    assert "不过闸" in sec and "50" in sec and "dry_run" in sec
    assert "commit_drafts" in sec or "commit" in sec, "要讲清和 commit 的区别"
    # 请求→工具 那张表里也要有一行
    table_rows = [l for l in body.splitlines() if l.startswith("|") and "ingest_published" in l]
    assert table_rows and "已经发出去" in table_rows[0]
    # 它【不在】"一律直接报错、看到报错就重试"那份清单里(codex #81 P1: 半途失败
    # 不抛却按"重试"办, 会把身份建成两份), 而是单列例外, 讲清两种情形
    assert "`ingest_published` / `record_rule`" not in body
    exc = body.split("`ingest_published` 是另一种例外", 1)
    assert len(exc) == 2, "写类工具报错那一节要单列 ingest_published 的例外"
    assert "半途失败不报错" in exc[1][:400] and "不适用" in exc[1][:400]


def test_tool_docstring_covers_the_operating_rules():
    doc = T.ingest_published.__doc__
    for needle in ("不过闸", "50", "dry_run", "重复调是安全的", "不要重传", "commit_drafts",
                   "半途失败不报错", "先** backfill", "每条都要有正文"):
        assert needle in doc, needle
    assert "出错会直接报错" not in doc, "codex #81 P1: 不能再承诺'出错一律抛'"


def test_it_is_a_write_and_therefore_not_wrapped_in_safe():
    import inspect
    assert "_safe(" not in inspect.getsource(T.ingest_published)


# ══════════════════════════════════════════════════════════════════════
# codex review #81 (2026-09-17)
# ══════════════════════════════════════════════════════════════════════

def test_rows_without_a_body_are_rejected_as_a_batch_before_any_write(fake):
    """P2: 模型可能转发飞书原始键、拼错键或漏掉 body。以前静默补空串: 没正文的行
    写进去挡不住谁, 重传时又认不出(没开头哈希)会再建一份。现在整批拒、一行不写。"""
    rows = _rows(2) + [{"title": "只有标题"}, {"titel": "拼错了", "bdy": "x" * 40}]
    with pytest.raises(T.InvalidInput) as ei:
        T.ingest_published(PROJ, rows, _user_id=ME)
    msg = str(ei.value)
    assert "2 行" in msg and "没有可用的正文" in msg and "整批重传" in msg
    assert not fake.rows.get("versions") and not fake.rows.get("draft_fingerprints")
    assert isinstance(ei.value, ValueError), "REST 层按 InvalidInput 映 400, 但它仍是 ValueError"


def test_feishu_style_chinese_keys_are_accepted(fake):
    rows = [{"标题": "中文键", "正文": "用飞书原始列名粘过来的正文, 也要认。" * 4}]
    out = T.ingest_published(PROJ, rows, _user_id=ME)
    assert (out["minted"], out["fingerprinted"]) == (1, 1)
    assert fake.rows["versions"][0]["title"] == "中文键"


def test_a_batch_made_only_of_committed_rows_writes_nothing_and_says_so(fake):
    rows = [{"title": "已入库", "body": "x" * 40, "version_id": "v1"}]
    out = T.ingest_published(PROJ, rows, _user_id=ME)
    assert out["skipped_already_committed"] == 1 and out["minted"] == 0
    assert "没有要补的" in out["note"]
    assert not fake.rows.get("versions")


def test_both_failures_at_once_keep_both_recovery_steps_in_order(fake, monkeypatch):
    """P2: 身份部分失败 + 指纹失败同时发生时, 只说"不要重传"会让没建成身份的行
    永远缺席(backfill 补不了身份)。note 必须是: 先 backfill, 再重传。"""
    real_mint = core.store.mint_draft_identity

    def _partial(sb, project_id, user_id, tactic, drafts, **kw):
        out = real_mint(sb, project_id, user_id, tactic, drafts[:1], **kw)
        out["error"] = "第 2 行身份没建成"
        return out
    monkeypatch.setattr(core.store, "mint_draft_identity", _partial)
    monkeypatch.setattr(core.store, "write_fingerprints",
                        lambda sb, rows: (_ for _ in ()).throw(RuntimeError("502")))
    out = T.ingest_published(PROJ, _rows(2), _user_id=ME)
    assert out["minted"] == 1 and out["fingerprinted"] == 0
    assert out["identity_error"] and out["fingerprint_error"]
    n = out["note"]
    assert "backfill" in n and "重传" in n
    assert n.index("backfill") < n.index("重传一次"), "顺序: 先 backfill 再重传"
    assert "别现在就重传" in n


def test_overlapping_calls_for_the_same_project_do_not_double_write(fake, monkeypatch):
    """P1: REST 把工具丢进线程池, 两次调用会重叠; 都过完"已有指纹吗"那一读再各写
    一份, 幂等就只对串行成立。按项目加锁后, 后到的那次看得见先到的指纹。"""
    import threading
    import time
    real_write = core.store.write_fingerprints

    def _slow_write(sb, rows):
        time.sleep(0.15)          # 拉长"读过了、还没写"那扇窗
        return real_write(sb, rows)
    monkeypatch.setattr(core.store, "write_fingerprints", _slow_write)
    rows = _rows(3)
    results = []
    ts = [threading.Thread(target=lambda: results.append(
              T.ingest_published(PROJ, rows, _user_id=ME))) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(fake.rows["draft_fingerprints"]) == 3, "两次重叠的调用不许各写一份"
    assert sorted(r["minted"] for r in results) == [0, 3]
    assert sorted(r["skipped_already_fingerprinted"] for r in results) == [0, 3]


def test_bad_input_is_a_400_over_rest_not_a_500(monkeypatch):
    """P2: 超过上限 / 空批是调用方的错; 500 会让模型拿同一个超大载荷一直重试。"""
    testclient = pytest.importorskip("fastapi.testclient")
    import deskcore.app as A_
    monkeypatch.setenv("DESKCORE_KEYS", '{"k": {"user_id": "%s", "name": "t"}}' % ME)
    c = FakeClient(rows={"projects": [{"id": PROJ, "name": "途鸽", "owner_id": ME,
                                       "calibration_notes": "", "tactics": "[]", "custom_roles": []}]})
    monkeypatch.setattr(core, "sb", lambda: c)
    client = testclient.TestClient(A_.app, raise_server_exceptions=False)
    h = {"X-Deskcore-Key": "k"}
    r = client.post("/tool/ingest_published", headers=h,
                    json={"project_id": PROJ, "drafts": _rows(T.INGEST_MAX_PER_CALL + 1)})
    assert r.status_code == 400 and "最多" in r.json()["detail"]
    r = client.post("/tool/ingest_published", headers=h, json={"project_id": PROJ, "drafts": []})
    assert r.status_code == 400
    r = client.post("/tool/ingest_published", headers=h,
                    json={"project_id": PROJ, "drafts": [{"title": "没正文"}]})
    assert r.status_code == 400 and "正文" in r.json()["detail"]
    assert not c.rows.get("versions")
