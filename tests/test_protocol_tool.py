"""写作台协议从服务端下发 (get_protocol) 的契约。

背景: 本地 SKILL.md 拷到 WorkBuddy 之后改了没人提醒, 一次「评论规则更新」的请求
因为旧协议没有"评论"的落点, 模型跑去翻交付文档反复读同一个文件, 被平台判定
死循环强杀。所以协议正文搬到 deskcore/protocol.md, 由 get_protocol 每次下发;
skills/bywood-writing-desk/SKILL.md 缩成一根引线。

这里锁四件事:
  1. get_protocol 返回的就是盘上那份 protocol.md, 不带 frontmatter, 不为空
  2. 协议正文覆盖 TOOLS 里的每一个工具 —— 加工具不写协议, 模型就不知道何时调它
  3. skill 文件携带【完整协议】且与 protocol.md 逐字一致 —— 正文的主副本在
     skill 里(进系统提示), get_protocol 只核版本, 版本对不上时才下发正文。
     (2026-09-10 曾反过来只留引线, 09-11 起入库率从 110% 掉到 22%, 见
     tools.get_protocol 的说明)
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


def test_skill_carries_the_full_protocol_and_is_in_sync():
    """⚠️ 正文的主副本在 skill 文件里 —— 它进系统提示, 整场对话都在模型眼前。

    2026-09-10 曾反过来: skill 只留引线, 正文每次由 get_protocol 下发。09-11 起
    定稿入库率从 110% 掉到 22%, 写出去的稿子没有指纹, 下一批查重看不见它们。
    这条盯着两件事: skill 里真的有全文, 且和 protocol.md 逐字一致(否则又是
    两份正文各改各的)。改了 protocol.md 就跑 `deskcore.cli sync-skill`。
    """
    stub = STUB.read_text(encoding="utf-8")
    assert stub.startswith("---\nname: bywood-writing-desk\n")
    assert "`get_protocol`" in stub and "protocol_version" in stub
    state = T.skill_sync_state()
    assert state["in_sync"], (
        f"skill 里的协议({state['skill_version']})和 protocol.md"
        f"({state['protocol_version']})不是同一份 —— 跑 python -m deskcore.cli sync-skill")
    for heading in ("## 标准流程", "### 一、动笔前", "### 三、收反馈"):
        assert heading in stub, f"skill 里缺协议正文的章节: {heading}"


def test_get_protocol_with_the_current_version_is_tiny():
    """版本一致时**不发正文** —— 发了就等于回到 09-10 那个每场对话开头塞 34 KB 的形态。"""
    _, current = T.protocol_text()
    out = T.get_protocol(local_version=current)
    assert out["up_to_date"] is True
    assert "protocol" not in out
    assert out["version"] == current


def test_get_protocol_with_a_stale_version_sends_the_body_and_says_to_reimport():
    out = T.get_protocol(local_version="deadbeefcafe")
    assert out["up_to_date"] is False
    assert out["protocol"] == T.protocol_text()[0]
    assert "重新导入" in out["warning"]


def test_get_protocol_without_a_version_sends_the_body():
    """没装 skill 的客户端(或老客户端)照样能拿到全文。"""
    out = T.get_protocol()
    assert out["up_to_date"] is None
    assert out["protocol"] == T.protocol_text()[0]


def test_render_skill_refuses_a_file_without_the_marker():
    """没有标记行说明有人把 SKILL.md 改回了手工维护的一整份 —— 生成会盖掉他的改动。"""
    with pytest.raises(RuntimeError):
        T.render_skill("---\nname: x\n---\n# 手写的\n", "正文", "abcdef012345")


def test_render_skill_keeps_the_hand_written_head_and_replaces_the_body():
    head = "---\nname: x\n---\n\n# 引线头\n\n<!-- protocol_version: 000000000000 -->\n\n旧正文\n"
    out = T.render_skill(head, "新正文", "abcdef012345")
    assert out == ("---\nname: x\n---\n\n# 引线头\n\n"
                   "<!-- protocol_version: abcdef012345 -->\n\n新正文\n")


# ══════════════════════════════════════════════════════════════════════
# 「调用失败」不许被讲成「服务掉线」
#
# 2026-09-17 现场: 运营连着两天看到「DeskCore 服务又中断了（找不到规则操作
# 工具）。要重试连接，还是暂时跳过规则更新？」—— 而同一时刻 /health 是
# ok、18 个工具全在、`record_rule` 和 `set_rule_state` 一个不少。模型给自己
# 的困惑编了一个基础设施理由, 然后**让运营在这个假前提上做选择题**。
#
# 真正的伤害不是那句假话, 是那道选择题: 运营点「跳过」, 以为只是跳过一次
# 重连, 实际是那条规矩永久没进库 —— 而它不报错, 之后每次生成静默缺席。
# ══════════════════════════════════════════════════════════════════════

# 模型爱用的那几个编出来的原因。它们只许以「不许这么说」的形式出现。
FABRICATED_CAUSES = ("服务中断", "掉线", "网络波动")
FORBIDDING = ("不许", "不要", "不说", "不该")


def _blocks_about(text: str, needle: str) -> list[str]:
    """按 markdown 标题切块(标题连着它下面的正文算一块), 再挑出提到 needle 的。

    不按空行切: 标题天然自成一段, 而"这一段在讲什么"恰恰写在标题里 ——
    `## 工具调用失败 ≠ 服务掉线` 这行本身就是那条禁令。按空行切会把标题
    和它的正文拆开, 于是标题永远被判成"一段没有禁止语气的话"。
    """
    blocks, cur = [], []
    for line in text.splitlines():
        if line.startswith("#") and cur:
            blocks.append("\n".join(cur))
            cur = []
        cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return [b for b in blocks if needle in b]


@pytest.mark.parametrize("cause", FABRICATED_CAUSES)
def test_protocol_only_ever_mentions_a_made_up_cause_to_forbid_it(cause):
    """协议里出现「服务中断」这种词, 只能是在禁止模型这么说。

    如果哪天它出现在一句陈述里(比如"服务中断时可以跳过"), 这条就该红 ——
    模型读到的是同一份文本, 它分不清哪句是禁令哪句是许可。
    """
    text = T.get_protocol()["protocol"]
    said = _blocks_about(text, cause)
    assert said, f"协议里没有管住「{cause}」这个说法"
    for p in said:
        assert any(w in p for w in FORBIDDING), \
            f"「{cause}」出现在一段没有禁止语气的话里:\n{p}"


def test_protocol_tells_the_model_how_to_actually_check_the_service():
    """光禁止不够 —— 不给判定手段, 模型只会换个词接着编。

    `get_protocol` 是最轻的一个工具, 拿它探活是唯一不用猜的办法。
    """
    text = T.get_protocol()["protocol"]
    probe = [p for p in _blocks_about(text, "get_protocol")
             if "连不上" in p or "活" in p]
    assert probe, "协议没告诉模型怎么判断服务是不是真没了"


def test_protocol_forbids_offering_to_skip_a_rule_update():
    """⚠️ 这条钉的是那道选择题本身。

    「要不要跳过」不是一个可以抛给运营的问题: 跳过的代价(规矩永久不落库、
    且无声)他看不见, 而他看到的「跳过更新」四个字听起来毫无风险。
    """
    text = T.get_protocol()["protocol"]
    said = _blocks_about(text, "跳过")
    assert said, "协议里没有管住「跳过」"
    assert any(any(w in p for w in FORBIDDING) for p in said), \
        "「跳过」没有以禁令的形式出现"
    assert any("无声" in p or "静默" in p for p in said), \
        "要说清跳过的代价是无声的 —— 不说清, 禁令就只是一句没理由的规定"


@pytest.mark.parametrize("must_carry", ["重试", "跳过"] + list(FABRICATED_CAUSES))
def test_the_stub_carries_these_rules_on_its_own(must_carry):
    """⚠️ 这条是这一组里最要紧的。

    这两条要写在引线头里 —— 正文之前、模型读到的第一段。它们管的正是
    「`get_protocol` 没调通」和「随便哪个工具失败了」的那一刻, 模型最容易在
    那一刻编一个原因、再抛一道选择题给运营。

    以后有人来精简引线头、理由是"这些正文里已经写了", 这条会拦住他。
    """
    stub = STUB.read_text(encoding="utf-8")
    said = _blocks_about(stub, must_carry)
    assert said, f"引线里没有「{must_carry}」—— 协议拿不到时这条规矩就失效了"
    if must_carry in FABRICATED_CAUSES:
        assert any(any(w in p for w in FORBIDDING) for p in said), \
            f"引线里「{must_carry}」不是以禁令出现的"


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
