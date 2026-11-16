"""新项目建出来之后提醒负责人接判定服务(deskcore/ownernotice.py, 2026-11-16)。

钉的是这几件事:

  · 建项目的回包里有一句给写手的话, 不带任何运维命令; 推没推成, 那句话说法不同
  · 没配 webhook 就一个请求都不发
  · 推失败(连不上 / 非 200 / 飞书回的 code 不是 0)不影响建项目, 也不谎称通知到了
  · 撞名没新建时不提醒 —— 那个项目不是这次建的
  · ``tv-map list`` 能列出在写但没接的项目, 归档的和早就不写的不列
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import requests

import config
from deskcore import cli, core, ownernotice, tools
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/abcdef12-3456-7890-abcd-ef1234567890"


class _Resp:
    def __init__(self, status=200, body=None, raises=False):
        self.status_code = status
        self._body = {"code": 0} if body is None else body
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("not json")
        return self._body


@pytest.fixture
def posts(monkeypatch):
    """记下发出去的请求; 默认飞书回成功。测试里不许真的打网络。"""
    sent = []
    state = {"resp": _Resp(), "exc": None}

    def fake_post(url, json=None, timeout=None, **kwargs):
        sent.append({"url": url, "json": json, "timeout": timeout})
        if state["exc"] is not None:
            raise state["exc"]
        return state["resp"]

    monkeypatch.setattr(requests, "post", fake_post)
    return sent, state


def _create(name="木南-新品", brand="木南"):
    return core.create_project(FakeClient(rows={"projects": []}), name, brand=brand, user_id=ME)


def test_without_a_webhook_nothing_is_sent_and_the_writer_is_asked_to_pass_it_on(monkeypatch, posts):
    sent, _ = posts
    monkeypatch.setattr(config, "DESKCORE_OWNER_WEBHOOK_URL", "", raising=False)
    out = _create()
    assert out["created"] is True
    assert sent == [], "没配就一个请求都不发"
    setup = out["judge_setup"]
    assert setup["connected"] is False and setup["owner_notified"] is False
    assert "木南-新品" in setup["message"] and "发给负责人" in setup["message"]
    assert "已经通知" not in setup["message"]


def test_with_a_webhook_the_owner_gets_the_project_and_the_command(monkeypatch, posts):
    sent, _ = posts
    monkeypatch.setattr(config, "DESKCORE_OWNER_WEBHOOK_URL", f"  {HOOK}  ", raising=False)
    out = _create()
    assert len(sent) == 1 and sent[0]["url"] == HOOK
    assert sent[0]["timeout"] == (2.0, 3.0), "建项目最多多等这么久"
    payload = sent[0]["json"]
    assert payload["msg_type"] == "text"
    text = payload["content"]["text"]
    assert text.startswith(f"【{ownernotice.KEYWORD}】"), "机器人开了关键词校验时靠开头这个词过"
    assert "木南-新品" in text and "木南" in text and out["project_id"] in text
    assert f"python -m deskcore.cli tv-map add --tv-project <TV项目号> --project {out['project_id']}" in text
    setup = out["judge_setup"]
    assert setup["owner_notified"] is True
    assert "已经通知负责人" in setup["message"] and "发给负责人" not in setup["message"]


def test_the_writer_never_sees_ops_commands(monkeypatch, posts):
    """同 _judge_project_context 的 detail: 补救命令只进负责人那一侧。"""
    for url in ("", HOOK):
        monkeypatch.setattr(config, "DESKCORE_OWNER_WEBHOOK_URL", url, raising=False)
        message = _create()["judge_setup"]["message"]
        for bad in ("tv-map", "tv_project_map", "python -m", "webhook", "project_id", "TV"):
            assert bad not in message, f"写手那句话里不该有 {bad!r}"


@pytest.mark.parametrize("failure", ["timeout", "refused", "http500", "feishu_code", "not_json", "odd_body"])
def test_a_failed_push_neither_breaks_creation_nor_claims_the_owner_knows(monkeypatch, posts, failure):
    sent, state = posts
    monkeypatch.setattr(config, "DESKCORE_OWNER_WEBHOOK_URL", HOOK, raising=False)
    if failure == "timeout":
        state["exc"] = requests.Timeout("read timed out")
    elif failure == "refused":
        state["exc"] = requests.ConnectionError("refused")
    elif failure == "http500":
        state["resp"] = _Resp(status=500)
    elif failure == "feishu_code":
        state["resp"] = _Resp(body={"code": 19024, "msg": "Key Words Not Found"})
    elif failure == "not_json":
        state["resp"] = _Resp(raises=True)
    else:
        state["resp"] = _Resp(body=["unexpected"])
    out = _create()
    assert out["created"] is True and out["project_id"], "项目已经在库里了, 提醒失败不能让建项目失败"
    assert len(sent) == 1, "不重试: 建项目不能因为提醒卡住"
    assert out["judge_setup"]["owner_notified"] is False
    assert "发给负责人" in out["judge_setup"]["message"]


def test_the_legacy_feishu_success_shape_counts(monkeypatch, posts):
    _, state = posts
    monkeypatch.setattr(config, "DESKCORE_OWNER_WEBHOOK_URL", HOOK, raising=False)
    state["resp"] = _Resp(body={"StatusCode": 0, "StatusMessage": "success"})
    assert _create()["judge_setup"]["owner_notified"] is True


def test_a_webhook_error_never_leaks_the_token_into_the_log(monkeypatch, posts, caplog):
    _, state = posts
    monkeypatch.setattr(config, "DESKCORE_OWNER_WEBHOOK_URL", HOOK, raising=False)
    import logger_utils
    monkeypatch.setattr(logger_utils, "_VALUE_SECRETS", None, raising=False)
    state["exc"] = requests.ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {HOOK}")
    with caplog.at_level("WARNING"):
        _create()
    assert "abcdef12-3456-7890-abcd-ef1234567890" not in caplog.text


def test_reusing_an_existing_project_sends_nothing(monkeypatch, posts):
    sent, _ = posts
    monkeypatch.setattr(config, "DESKCORE_OWNER_WEBHOOK_URL", HOOK, raising=False)
    existing = {"id": "p-old", "name": "木南-新品", "brand": "木南", "owner_id": ME}
    out = core.create_project(FakeClient(rows={"projects": [existing]}), "木南-新品", brand="木南", user_id=ME)
    assert out["created"] is False and "judge_setup" not in out
    assert sent == [], "那个项目不是这次建的, 不提醒"


def test_the_tool_tells_the_model_what_to_do_with_it():
    doc = tools.create_project.__doc__
    assert "judge_setup" in doc and "原样" in doc


# ════════════════════════════════════════════════════════
# tv-map list 后面那一段: 在写但没接的项目
# ════════════════════════════════════════════════════════

NOW = datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc)


def _ago(days):
    return (NOW - timedelta(days=days)).isoformat()


def _client():
    return FakeClient(rows={
        "projects": [
            {"id": "p-mapped", "name": "途鸽", "brand": "途鸽", "default_params": None},
            {"id": "p-new", "name": "木南-新品", "brand": "木南", "default_params": None},
            {"id": "p-busy", "name": "木南-老品", "brand": "木南", "default_params": {}},
            {"id": "p-idle", "name": "早就不写的", "brand": "旧", "default_params": None},
            {"id": "p-archived", "name": "标成不用的", "brand": "旧", "default_params": {"archived": True}},
        ],
        "batches": [
            {"id": "b1", "project_id": "p-mapped", "created_at": _ago(1)},
            {"id": "b2", "project_id": "p-new", "created_at": _ago(2)},
            {"id": "b3", "project_id": "p-busy", "created_at": _ago(3)},
            {"id": "b4", "project_id": "p-busy", "created_at": _ago(4)},
            {"id": "b5", "project_id": "p-idle", "created_at": _ago(45)},
            {"id": "b6", "project_id": "p-archived", "created_at": _ago(1)},
        ],
        "tv_project_map": [
            {"tv_project_id": "TUGE_phase1", "project_id": "p-mapped", "ingest_target": True, "note": ""},
        ],
    })


def test_only_projects_still_being_written_and_not_mapped_are_listed():
    missing = core.projects_missing_judge(_client(), now=NOW)
    assert [m["project_id"] for m in missing] == ["p-busy", "p-new"], "批次多的在前; 接了的、早就不写的、归档的都不列"
    assert missing[0] == {"project_id": "p-busy", "name": "木南-老品", "brand": "木南", "batches": 2}


def test_nothing_missing_when_everything_written_is_mapped():
    client = _client()
    client.rows["tv_project_map"] += [
        {"tv_project_id": "MN_phase1", "project_id": "p-new", "ingest_target": True, "note": ""},
        {"tv_project_id": "MN_phase1", "project_id": "p-busy", "ingest_target": False, "note": ""},
    ]
    assert core.projects_missing_judge(client, now=NOW) == []


def test_tv_map_list_prints_the_gap_after_the_table(monkeypatch, capsys):
    client = _client()
    real = core.projects_missing_judge
    monkeypatch.setattr(core, "projects_missing_judge", lambda sb: real(sb, now=NOW))

    class Args:
        action = "list"
        tv_project = None
        project = None
        ingest_target = False
        note = ""

    assert cli._tv_map(core, client, Args()) == 0
    out = capsys.readouterr().out
    assert "TUGE_phase1" in out
    assert "没接判定服务的项目(2 个)" in out
    assert "木南-老品" in out and "p-busy" in out and "木南-新品" in out
    assert "早就不写的" not in out and "标成不用的" not in out
    assert out.index("TUGE_phase1") < out.index("没接判定服务")


def test_tv_map_list_still_prints_the_table_when_the_gap_query_fails(monkeypatch, capsys):
    def boom(sb):
        raise RuntimeError("PGRST000 connection lost")

    monkeypatch.setattr(core, "projects_missing_judge", boom)

    class Args:
        action = "list"
        tv_project = None
        project = None
        ingest_target = False
        note = ""

    assert cli._tv_map(core, _client(), Args()) == 0
    out = capsys.readouterr().out
    assert "TUGE_phase1" in out and "没查出" in out and "PGRST000" in out

