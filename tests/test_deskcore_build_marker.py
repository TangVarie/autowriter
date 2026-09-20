"""/health 要能不带 key 回答「跑着的是哪一份代码」。

2026-09-20 同一个问题一天卡了两次:

  · 早上我把「Railway 还没重新部署」当事实写进了记录 —— 其实**没核实**, 它早就
    自动上线了。owner 一句「这东西不是 github 更新之后自动拉取的吗」才逼出实测。
  · 下午 Railway 一次部署真的失败了, 服务停在 6 小时前的版本; 而**从外面没有任何
    办法看出来** —— /health 不带版本, /tools 要 key。我去查的时候拿到的是
    ``{"detail": "missing X-Deskcore-Key"}``, 差点照着这个报错体断言「新功能没上线」。

「代码进了 main」和「跑着的是那一份」是两件事, 分辨它们不该需要一把 key。

这里钉的是【被禁止的形态】: /health 不报版本、报了但会漂、或者这个**诊断字段本身
把被诊断的东西弄挂**(它是 Railway 的 healthcheckPath, 一挂就是重启风暴)。
"""

from __future__ import annotations

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

from deskcore import tools                    # noqa: E402
import deskcore.app as A                      # noqa: E402


KEYS = ('{"k-test": {"user_id": "22222222-2222-2222-2222-222222222222",'
        ' "name": "t"}}')


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("DESKCORE_KEYS", KEYS)
    return fastapi_testclient.TestClient(A.app, raise_server_exceptions=False)


# ══════════════════════════════════════════════════════════════════════
# 主路径: 不带 key 就能读到
# ══════════════════════════════════════════════════════════════════════

def test_health_reports_which_build_is_running_without_a_key(client):
    """就是这条: 没有 key 也要答得出「跑的是哪一份」。"""
    r = client.get("/health")           # 故意不带 Authorization
    assert r.status_code == 200
    build = r.json()["build"]
    assert build["commit"], "没有 commit = 又回到那天下午查不出来的状态"
    assert build["protocol_version"], "没有 protocol_version = 判不了 skill 同没同步"


def test_protocol_version_is_the_same_one_get_protocol_would_serve():
    """两处各算一次就会漂 —— 漂了比不报更坏: 它会让人以为已经对上了。"""
    assert A._protocol_version() == tools.protocol_text()[1]


# ══════════════════════════════════════════════════════════════════════
# commit 的取值顺序
# ══════════════════════════════════════════════════════════════════════

def test_railway_injected_sha_wins(monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "a" * 40)
    assert A._build_commit() == "a" * 12          # 截到 12 位, 和 protocol_version 一个长度


@pytest.mark.parametrize("var", ["GIT_COMMIT", "SOURCE_COMMIT",
                                 "COMMIT_SHA", "HEROKU_SLUG_COMMIT"])
def test_other_platforms_variables_are_accepted(monkeypatch, var):
    """换个平台部署时别又变成「查不出来」。"""
    for v in ("RAILWAY_GIT_COMMIT_SHA", "GIT_COMMIT", "SOURCE_COMMIT",
              "COMMIT_SHA", "HEROKU_SLUG_COMMIT"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv(var, "b" * 40)
    assert A._build_commit() == "b" * 12


def test_blank_env_var_falls_through_instead_of_reporting_empty(monkeypatch):
    """平台把变量设成空串是常见的 —— 报一个空 commit 等于没报, 还更容易被当成真的。"""
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "   ")
    monkeypatch.setenv("GIT_COMMIT", "c" * 40)
    assert A._build_commit() == "c" * 12


# ══════════════════════════════════════════════════════════════════════
# 兜底: 这个诊断字段绝不能把 /health 弄挂
# ══════════════════════════════════════════════════════════════════════

def test_unknown_when_nothing_is_available(monkeypatch):
    """查不到就照实说 unknown, 不编、也不抛。"""
    for v in ("RAILWAY_GIT_COMMIT_SHA", "GIT_COMMIT", "SOURCE_COMMIT",
              "COMMIT_SHA", "HEROKU_SLUG_COMMIT"):
        monkeypatch.delenv(v, raising=False)

    import subprocess
    def boom(*a, **k):
        raise FileNotFoundError("git: not found")
    monkeypatch.setattr(subprocess, "run", boom)
    assert A._build_commit() == "unknown"


def test_a_broken_protocol_file_does_not_take_health_down(client, monkeypatch):
    """**这条是要害。** /health 是 Railway 的 healthcheckPath —— 让它 500 就是重启风暴。

    诊断字段坏了只该显示 unknown; 协议文件真的缺了, 该在 get_protocol 那儿照实抛。
    """
    def boom():
        raise RuntimeError("协议文件为空")
    monkeypatch.setattr(tools, "protocol_text", boom)

    r = client.get("/health")
    assert r.status_code == 200, "诊断字段把被诊断的东西弄挂了"
    assert r.json()["build"]["protocol_version"] == "unknown"


def test_build_block_does_not_change_the_top_level_ok(client, monkeypatch):
    """版本查不到不是「不健康」—— 把它算进 ok 会让 Railway 白重启一轮。

    判据是**同一个环境下 ok 前后一致**, 不是 ok 等于某个定值: 这个测试环境里
    db/vocab/auth 本来就可能是 false, 拿定值去断言只会测到环境而不是这条规矩。
    """
    before = client.get("/health").json()["ok"]

    monkeypatch.setattr(A, "BUILD_COMMIT", "unknown")
    def boom():
        raise RuntimeError("no protocol")
    monkeypatch.setattr(tools, "protocol_text", boom)

    body = client.get("/health").json()
    assert body["build"] == {"commit": "unknown", "protocol_version": "unknown"}
    assert body["ok"] == before, "build 查不到把整体 ok 拖下水了 —— Railway 会白重启"
