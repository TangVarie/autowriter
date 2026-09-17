"""借来的经验卡: 证据属性要看得见, 借阅的结局要分得开。

补的是 2026-09-16 评测的两条, 它们是同一个病的两面 —— **把"不知道"渲染成
"知道"**:

  · AW-02 常规生成渲染丢失 `synthetic`。馆员会给指标未经验证(疑似刷量)的卡
    打这个标, 而 `render_flywheel_block` 一个字都不读 —— 同一张卡在
    `synthetic=true/false` 下生成的提示词**完全一样**, 而开头那句还无条件写着
    "现实中真爆过"。等于替未验证的数据背了书。
  · AW-05 借阅降级都塌成空列表。没匹配 / 没配 key / 超时 / 出错, 四种结局在
    数据上长得一模一样, 于是"经验到底有没有被用上"永远统计不出来。

两条都**不会报错**, 所以只能靠断言钉住。
"""

from __future__ import annotations

import pytest
import requests

import config
import librarian_client as lib
import memory

CARD = {
    "hook_type": "反差开场", "structure": "问题-转折-结果",
    "why_it_worked": "戳到了通勤族的痛点", "transferable_tactic": "先立靶再打",
    "borrow_what": "钩子写法", "why_relevant": "同品类同人群",
    "excerpt": "原文片段……",
}


# ══════════════════════════════════════════════════════════════════════
# AW-02 · 未验证的指标不许被渲染成已验证
# ══════════════════════════════════════════════════════════════════════

def test_synthetic_flag_changes_the_prompt():
    """⚠️ 这条是这个文件存在的理由。

    修之前两者**逐字相同** —— 调用模型无法从提示词里分辨它借鉴的"爆款"
    到底爆没爆过。
    """
    verified = memory.render_flywheel_block([{**CARD, "synthetic": False}])
    unverified = memory.render_flywheel_block([{**CARD, "synthetic": True}])
    assert verified != unverified, "同一张卡的验证状态必须改变提示词"
    assert "未经验证" in unverified
    assert "未经验证" not in verified


def test_lead_sentence_stops_claiming_it_really_went_viral():
    """开头那句原文是「下面是现实中真爆过 …」。

    这一批里只要有一张未验证的卡, 那句话就是假的 —— 而它恰恰是模型最容易
    采信的一句(比逐条的小字显眼得多)。
    """
    block = memory.render_flywheel_block([
        {**CARD, "synthetic": False},
        {**CARD, "synthetic": True},
    ])
    assert "真爆过" not in block, "一批里有未验证的卡, 就不能再断言'真爆过'"
    assert "数据不可采信" in block


def test_all_verified_keeps_the_strong_wording():
    """全是验证过的卡时不该自我削弱 —— 那会让真实指标也变得可疑。"""
    block = memory.render_flywheel_block([{**CARD, "synthetic": False}])
    assert "真爆过" in block
    assert "未经验证" not in block


@pytest.mark.parametrize("card", [
    CARD,                                   # 字段缺失
    {**CARD, "synthetic": None},
    {**CARD, "synthetic": False},
])
def test_absent_or_false_is_not_treated_as_unverified(card):
    """只有**显式为真**才算未验证。

    馆员没表态时替它下"未经验证"的结论, 会把所有真实爆款一起贬值 —— 那是
    另一个方向的同一个错。
    """
    assert "未经验证" not in memory.render_flywheel_block([card])


def test_unverified_mark_sits_on_the_card_it_belongs_to():
    """两张卡只有一张未验证时，标记不能糊在整批上。"""
    block = memory.render_flywheel_block([
        {**CARD, "hook_type": "验证过的", "synthetic": False},
        {**CARD, "hook_type": "没验证的", "synthetic": True},
    ])
    good, bad = block.split("· 钩子：没验证的")[0], block.split("· 钩子：没验证的")[1]
    assert "未经验证" not in good.split("· 钩子：验证过的")[-1]
    assert "未经验证" in bad


# ══════════════════════════════════════════════════════════════════════
# AW-05 · 四种空要分得开
# ══════════════════════════════════════════════════════════════════════

def _configured(monkeypatch):
    monkeypatch.setattr(config, "LIBRARIAN_URL", "https://tv.example/api")
    monkeypatch.setattr(config, "LIBRARIAN_API_KEY", "k-test")


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_not_configured_is_distinguishable(monkeypatch):
    """⚠️ 这条以前是**完全静默**的 return —— 连日志都没有。

    于是"这个部署根本没接飞轮"和"接了但这次没匹配"在数据上分不开，而前者
    是要人去补配置的。
    """
    monkeypatch.setattr(config, "LIBRARIAN_URL", "")
    monkeypatch.setattr(config, "LIBRARIAN_API_KEY", "")
    st: dict = {}
    assert lib.fetch_flywheel_lessons({"project_id": "p"}, status=st) == []
    assert st["state"] == lib.BORROW_NOT_CONFIGURED


def test_empty_result_is_not_an_error(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setattr(lib.requests, "post",
                        lambda *a, **k: _Resp({"selected": []}))
    st: dict = {}
    assert lib.fetch_flywheel_lessons({"project_id": "p"}, status=st) == []
    assert st["state"] == lib.BORROW_EMPTY, "通了但没匹配 ≠ 出错"


def test_borrowed_reports_the_count(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setattr(lib.requests, "post",
                        lambda *a, **k: _Resp({"selected": [CARD, CARD]}))
    st: dict = {}
    out = lib.fetch_flywheel_lessons({"project_id": "p"}, status=st)
    assert len(out) == 2
    assert st["state"] == lib.BORROW_BORROWED
    assert st["count"] == 2


def test_timeout_is_not_lumped_in_with_other_errors(monkeypatch):
    """超时要单独认出来 —— 它指向 TV 那边慢了, 和 4xx/解析失败是两回事。"""
    _configured(monkeypatch)

    def _boom(*a, **k):
        raise requests.Timeout("timed out")

    monkeypatch.setattr(lib.requests, "post", _boom)
    st: dict = {}
    assert lib.fetch_flywheel_lessons({"project_id": "p"}, status=st) == []
    assert st["state"] == lib.BORROW_TIMEOUT


def test_other_failures_report_error(monkeypatch):
    _configured(monkeypatch)

    def _boom(*a, **k):
        raise ValueError("响应不是 JSON")

    monkeypatch.setattr(lib.requests, "post", _boom)
    st: dict = {}
    assert lib.fetch_flywheel_lessons({"project_id": "p"}, status=st) == []
    assert st["state"] == lib.BORROW_ERROR
    assert "JSON" in st["detail"]


def test_all_five_states_are_distinct():
    """五个状态值不许重名 —— 重了就等于没分。"""
    states = [lib.BORROW_BORROWED, lib.BORROW_EMPTY, lib.BORROW_NOT_CONFIGURED,
              lib.BORROW_TIMEOUT, lib.BORROW_ERROR]
    assert len(set(states)) == 5


def test_failures_never_raise(monkeypatch):
    """借阅是增强项不是前置依赖 —— 加了状态之后这条不许松掉。"""
    _configured(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("服务挂了")

    monkeypatch.setattr(lib.requests, "post", _boom)
    assert lib.fetch_flywheel_lessons({"project_id": "p"}) == []


def test_status_is_optional():
    """不传 status 的老调用方一行都不用改。"""
    assert lib.fetch_flywheel_lessons({"project_id": "p"}) == []
