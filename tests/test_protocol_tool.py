"""写作台协议从服务端下发 (get_protocol) 的契约。

背景: 本地 SKILL.md 拷到 WorkBuddy 之后改了没人提醒, 一次「评论规则更新」的请求
因为旧协议没有"评论"的落点, 模型跑去翻交付文档反复读同一个文件, 被平台判定
死循环强杀。所以协议正文搬到 deskcore/protocol.md, 由 get_protocol 每次下发;
skills/bywood-writing-desk/SKILL.md 缩成一根引线。

这里锁四件事:
  1. get_protocol 返回的就是盘上那份 protocol.md, 不带 frontmatter, 不为空
  2. 协议正文覆盖 TOOLS 里的每一个工具 —— 加工具不写协议, 模型就不知道何时调它
  3. 引线 SKILL.md 真的是引线: 指向 get_protocol, 且不再携带协议正文
  4. REST 通道打得通, 且协议对所有人一样 —— 它是流程文本, 不按人裁剪
     (工具照旧绑身份, 那是 test_all_tools_now_require_caller_identity 守着的
     总保险, 不为它开例外)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import deskcore.tools as T

REPO = Path(__file__).resolve().parent.parent
STUB = REPO / "skills" / "bywood-writing-desk" / "SKILL.md"


def test_get_protocol_returns_the_file_on_disk():
    out = T.get_protocol()
    on_disk = T.PROTOCOL_PATH.read_text(encoding="utf-8").strip()
    assert out["protocol"] == on_disk
    assert out["chars"] == len(on_disk) > 1000, "协议不该是一句话"
    assert re.fullmatch(r"[0-9a-f]{12}", out["version"])
    assert not out["protocol"].startswith("---"), "协议正文不该带 skill frontmatter"


def test_protocol_version_tracks_content(tmp_path, monkeypatch):
    before = T.get_protocol()["version"]
    p = tmp_path / "protocol.md"
    p.write_text(T.PROTOCOL_PATH.read_text(encoding="utf-8") + "\n多一行\n",
                 encoding="utf-8")
    monkeypatch.setattr(T, "PROTOCOL_PATH", p)
    assert T.get_protocol()["version"] != before


def test_empty_protocol_raises_instead_of_returning_success(tmp_path, monkeypatch):
    """协议拿不到必须停, 不能回一个带 error 的"成功"让模型按旧版本继续。"""
    p = tmp_path / "protocol.md"
    p.write_text("   \n", encoding="utf-8")
    monkeypatch.setattr(T, "PROTOCOL_PATH", p)
    with pytest.raises(RuntimeError):
        T.get_protocol()


def test_protocol_mentions_every_tool():
    text = T.get_protocol()["protocol"]
    missing = [n for n in T.TOOLS if f"`{n}`" not in text]
    assert not missing, f"这些工具在协议里没有落点, 模型不会知道何时调它: {missing}"


def test_get_protocol_is_the_same_for_everyone():
    fn, needs_user = T.TOOLS["get_protocol"]
    assert fn is T.get_protocol
    assert needs_user is True, "不给「所有工具都绑身份」那条保险开例外"
    a = T.get_protocol(_user_id="11111111-1111-1111-1111-111111111111")
    b = T.get_protocol(_user_id=None)
    assert a == b, "协议不按人裁剪"


def test_skill_stub_points_at_get_protocol_and_carries_no_body():
    stub = STUB.read_text(encoding="utf-8")
    assert stub.startswith("---\nname: bywood-writing-desk\n")
    assert "`get_protocol`" in stub
    # 协议正文的章节标题一个都不该出现在引线里 —— 出现了就是两份正文又要各改各的。
    for heading in ("## 标准流程", "### 一、动笔前", "### 三、收反馈"):
        assert heading not in stub, f"引线里不该有协议正文: {heading}"
    assert len(stub.splitlines()) < 60, "引线该是十几行, 不该长回一份协议"


def test_rest_channel_serves_protocol(monkeypatch):
    testclient = pytest.importorskip("fastapi.testclient")
    import deskcore.app as A

    monkeypatch.setenv("DESKCORE_KEYS",
                       '{"k-test": {"user_id": "22222222-2222-2222-2222-222222222222", "name": "t"}}')
    client = testclient.TestClient(A.app, raise_server_exceptions=False)

    with_key = client.post("/tool/get_protocol", json={},
                           headers={"Authorization": "Bearer k-test"})
    assert with_key.status_code == 200
    assert with_key.json()["result"]["protocol"] == T.get_protocol()["protocol"]

    listed = client.get("/tools", headers={"Authorization": "Bearer k-test"}).json()
    assert any(t["name"] == "get_protocol" for t in listed["tools"])
