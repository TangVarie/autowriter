"""dsh 插件(clients/dsh-plugin)写死在代码里的那几样, 和服务端对得上。

插件按名字拦工具(没开项目不许发牌、查重、存档), 按 ``project_id`` 认项目,
按 REST ``/tool/get_protocol`` 回包里的 ``protocol`` / ``version`` / ``skill_header``
拼系统提示。这些都在 ``clients/dsh-plugin/lib/desk.js`` 里写死了, 服务端改名或改
回包, 插件不会报错, 只会默默放行或拼不出提示。这里直接读那份 JS, 逐样对服务端:

  1. 插件拦的工具、记账用的旧名映射、提到的每个 ``mcp__deskcore__*`` 工具, 服务端都有
  2. 被拦的工具和 ``open_project`` 都收 ``project_id``(插件按它认项目)
  3. ``get_protocol`` 不带版本时回 ``protocol`` / ``version`` / ``skill_header`` 三样字符串
  4. 插件发钥匙用的头, 服务端认

改这几样之前先改插件、发一版插件; 这个测试红了就是提醒这件事。

两个包都对一遍: 正常版 ``clients/dsh-plugin``, 和给停在 DSH 0.2.1 的 Intel Mac 用的兼容版
``clients/dsh-plugin-compat-021``(2026-11-18)。兼容版是单独维护的一份, 服务端改了这几样, 两份都要跟。
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

import pytest

import deskcore.tools as T

REPO = Path(__file__).resolve().parent.parent
PACKAGES = ("dsh-plugin", "dsh-plugin-compat-021")
APP_PY = REPO / "deskcore" / "app.py"


@pytest.fixture(params=PACKAGES, autouse=True)
def _package(request, monkeypatch):
    """每条用例对两个包各跑一遍。"""
    monkeypatch.setattr(sys.modules[__name__], "DESK_JS", REPO / "clients" / request.param / "lib" / "desk.js")
    return request.param


DESK_JS = REPO / "clients" / PACKAGES[0] / "lib" / "desk.js"


def _js() -> str:
    return DESK_JS.read_text(encoding="utf-8")


def _gated() -> set[str]:
    m = re.search(r"GATED_TOOLS\s*=\s*new Set\(\[([^\]]*)\]\)", _js())
    assert m, "desk.js 里找不到 GATED_TOOLS"
    names = set(re.findall(r"'([a-z_]+)'", m.group(1)))
    assert names, "GATED_TOOLS 是空的"
    return names


def _aliases() -> dict[str, str]:
    m = re.search(r"const ALIASES\s*=\s*\{([^}]*)\}", _js())
    assert m, "desk.js 里找不到 ALIASES"
    return dict(re.findall(r"([a-z_]+)\s*:\s*'([a-z_]+)'", m.group(1)))


def _referenced() -> set[str]:
    js = _js()
    names = set(re.findall(r"\$\{TOOL_PREFIX\}([a-z_]+)", js))
    names |= set(re.findall(r"tool === '([a-z_]+)'", js))
    names |= set(re.findall(r"/tool/([a-z_]+)", js))
    return names


def test_server_name_matches_prefix():
    assert re.search(r"SERVER_NAME\s*=\s*'deskcore'", _js()), "插件的 MCP 服务名变了, 工具名前缀会对不上"


def test_every_tool_the_plugin_names_exists_on_the_server():
    aliases = _aliases()
    names = _gated() | _referenced() | set(aliases) | set(aliases.values())
    assert {"draw_angles", "check_drafts", "score_drafts", "open_project", "get_protocol"} <= names
    missing = sorted(n for n in names if n not in T.TOOLS)
    assert not missing, f"插件里写死的工具服务端没有了: {missing}(先改插件、发一版插件)"


def test_gated_tools_and_open_project_take_project_id():
    for name in sorted(_gated() | {"open_project"} | set(_aliases())):
        fn = T.TOOLS[name][0]
        assert "project_id" in inspect.signature(fn).parameters, f"{name} 不收 project_id 了, 插件按它认项目"


def test_get_protocol_reply_has_what_the_plugin_reads():
    js = _js()
    for field in ("protocol", "version", "skill_header"):
        assert f"result.{field}" in js or f"result?.{field}" in js, f"插件不再读 {field}, 这个测试也该跟着改"
    out = T.get_protocol()
    for field in ("protocol", "version", "skill_header"):
        assert isinstance(out.get(field), str), f"get_protocol 不带版本时没回 {field}"
    assert out["protocol"].strip()


def test_key_header_the_plugin_sends_is_accepted():
    m = re.search(r"'(X-[A-Za-z-]+-Key)'\s*:\s*key", _js())
    assert m, "desk.js 里找不到发钥匙的头"
    assert f'request.headers.get("{m.group(1).lower()}")' in APP_PY.read_text(encoding="utf-8")


def test_each_run_reads_its_own_package(_package):
    assert DESK_JS.parent.parent.name == _package
