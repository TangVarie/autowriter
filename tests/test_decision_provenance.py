"""``items.status`` 必须说得清是**谁**定的（跨库审计 2026-08-24 COR-004 / COR-007）。

原来一个 status 列同时装两件完全不同的事：

  · 人点了「通过」/「打回」        —— 人工审稿意见
  · 硬规则违规 / 查重重生耗尽      —— 机器检测结果

写进去之后长得一模一样。而 Truth Vault 的 ``sync_autowriter_decisions_to_
prepublish.py`` 把**全部**当人工反馈灌进 ``prepublish_evaluations``
(``evaluator_type='human'``) 去校准评估模型 —— 机器自己的判定被当成人的判断
喂回给模型学。训练标签污染，且没有任何地方会报错。

同一条 sync 还推断了另外两件事，也都推错了：``evaluator_id`` 取的是 item 的
**owner** 而不是点按钮的人；``created_at`` 取的是**同步那一刻**而不是决策时刻。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import db as DB


REPO = pathlib.Path(__file__).resolve().parent.parent


def _call_sites():
    """全仓每一处 ``update_item_status(...)``。

    产出 ``(文件名, 行号, source 的名字或 None, 所在函数名)``。
    ``source`` 取的是 ``db.DecisionSource.X`` 里的那个 ``X``；写成别的形态
    (字面量、变量)时回 ``"<非常量>"`` —— 那也不该出现, 但要和"压根没写"区分开。
    """
    for path in sorted(REPO.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # 先建"行号 → 所在函数"的映射, 免得为每个调用点重新走一遍树。
        owner: dict[int, str] = {}
        for fn_node in ast.walk(tree):
            if isinstance(fn_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for ln in range(fn_node.lineno, (fn_node.end_lineno or fn_node.lineno) + 1):
                    # 嵌套函数以**最内层**为准: 后写的覆盖先写的不行, 所以只在
                    # 还没被更内层占用时才写。ast.walk 是广度优先, 外层先到。
                    owner[ln] = fn_node.name
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name != "update_item_status":
                continue
            src = None
            for kw in node.keywords:
                if kw.arg != "source":
                    continue
                src = (kw.value.attr if isinstance(kw.value, ast.Attribute)
                       else "<非常量>")
            yield path.name, node.lineno, src, owner.get(node.lineno, "<模块顶层>")


class _FakeTable:
    def __init__(self, sink):
        self._sink = sink

    def update(self, payload):
        self._sink["payload"] = payload
        return self

    def eq(self, col, val):
        self._sink.setdefault("eq", []).append((col, val))
        return self

    def execute(self):
        return type("R", (), {"data": [{"id": "item-1", **self._sink["payload"]}]})()


class _FakeClient:
    def __init__(self):
        self.sink: dict = {}

    def table(self, name):
        self.sink["table"] = name
        return _FakeTable(self.sink)


@pytest.fixture()
def client(monkeypatch):
    # list_items.clear() 在真实 db 里是 streamlit 的缓存 shim, 这里不关心。
    monkeypatch.setattr(DB.list_items, "clear", lambda: None, raising=False)
    return _FakeClient()


# ══════════════════════════════════════════════════════════════════════
# 1 · 三个真值真的写进去了
# ══════════════════════════════════════════════════════════════════════

def test_human_decision_records_who_and_when(client):
    DB.update_item_status(client, "item-1", "approved",
                          source=DB.DecisionSource.HUMAN,
                          reviewer_id="user-42")
    p = client.sink["payload"]
    assert p["decision_source"] == "human"
    assert p["reviewer_id"] == "user-42"
    assert p["decided_at"], "没写决策时刻"


def test_auto_decision_has_no_reviewer(client):
    """自动判定没有「人」—— reviewer_id 必须是 NULL 而不是随便填一个。"""
    DB.update_item_status(client, "item-1", "needs_revision",
                          source=DB.DecisionSource.AUTO_HARD_RULE)
    p = client.sink["payload"]
    assert p["decision_source"] == "auto_hard_rule"
    assert p["reviewer_id"] is None


def test_reviewer_on_an_auto_decision_is_rejected(client):
    """给自动判定安一个审稿人 = 又一次伪造人工反馈, 当场拒。"""
    with pytest.raises(ValueError, match="reviewer_id"):
        DB.update_item_status(client, "item-1", "needs_revision",
                              source=DB.DecisionSource.AUTO_DEDUP,
                              reviewer_id="user-42")


def test_unknown_source_is_rejected(client):
    """取值是闭集, 且与 migrations/006 的 CHECK 是同一份。

    写进去才被数据库拒的话, 错误会出现在离现场很远的地方。
    """
    with pytest.raises(ValueError, match="decision_source"):
        DB.update_item_status(client, "item-1", "approved", source="老板说的")


def test_source_is_required(client):
    """少写这个参数要当场 TypeError —— 这正是把它做成必填的理由。"""
    with pytest.raises(TypeError):
        DB.update_item_status(client, "item-1", "approved")


# ══════════════════════════════════════════════════════════════════════
# 2 · 「选为最佳」不是一次决策
# ══════════════════════════════════════════════════════════════════════

def test_set_best_version_does_not_stamp_a_decision(client):
    """挑展示/导出用哪一版 ≠ 审稿。它不许碰 status, 更不许盖决策戳。

    这个问题是 ``source`` 做成必填之后**当场逼出来的**: 原来这条路借
    update_item_status 把当前 status 原样写回一遍, 在只有 status 一个字段时
    看不出问题; 补上 decision_source/decided_at 之后, 每点一次「选为最佳」
    就会盖一枚新的人工决策戳, 把刚补进来的出处数据自己污染掉。
    """
    DB.set_best_version(client, "item-1", "ver-9")
    p = client.sink["payload"]
    assert p == {"best_version_id": "ver-9"}, p
    assert "status" not in p and "decision_source" not in p


# ══════════════════════════════════════════════════════════════════════
# 3 · 新增的调用点不许漏
# ══════════════════════════════════════════════════════════════════════

def test_every_call_site_states_its_source():
    """全仓每一处 ``update_item_status(...)`` 都必须显式带 ``source=``。

    签名已经保证漏了会 TypeError, 但那是**运行期**才炸 —— 而这几条路径是
    「硬规则违规时」「用户点打回时」才走到, 平时的冒烟测试碰不到。这条把它
    提前到测试期。

    判据是**被禁止的形态**(调了但没说来源), 不是"当前有几处、分别是什么" ——
    以后新增调用点照样管用, 而且不会因为正当的重构就假警报。
    """
    offenders = [f"{p}:{ln}" for p, ln, src, _fn in _call_sites() if src is None]
    assert not offenders, (
        f"这些 update_item_status 调用没说来源: {offenders} —— "
        "机器判定和人工审稿写进去会长得一模一样, 而 TV 会把两者都当人工反馈"
        "去校准模型。见 migrations/006。")


# 哪个函数里的标记该是哪种来源。**说错比不说更糟** —— 不说会当场 TypeError,
# 说错了会安安静静地把查重的自动判定伪装成人工反馈。
_EXPECTED_SOURCE_BY_FUNC = {
    "_run_hard_constraint_check": "AUTO_HARD_RULE",   # 硬规则违规
    "_try_regen_one": "AUTO_DEDUP",                   # 查重重生耗尽
}


def test_each_auto_path_uses_its_own_source():
    """两条自动路径不许互相串, 也不许写成 HUMAN。

    ``_run_hard_constraint_check`` 有编排录音守着, ``_try_regen_one`` 没有 ——
    这条把两者一起钉住。判据是"这个函数里的标记必须是这个来源", 所以改了值
    会红, 而单纯挪动代码不会。
    """
    seen: dict[str, set[str]] = {}
    for _p, _ln, src, fn in _call_sites():
        if fn in _EXPECTED_SOURCE_BY_FUNC and src:
            seen.setdefault(fn, set()).add(src)

    for fn, want in _EXPECTED_SOURCE_BY_FUNC.items():
        got = seen.get(fn)
        assert got, f"{fn} 里找不到带 source 的 update_item_status 调用了"
        assert got == {want}, f"{fn} 的来源应该是 {want}, 实际 {sorted(got)}"


def test_the_scanner_would_actually_catch_a_bare_call(tmp_path):
    """守卫本身也要被验证 —— 上面那条永远绿的话, 它什么都没在守。

    喂一段确实漏了 source 的代码, 断言同一套判据会把它挑出来。
    """
    src = "import db\ndb.update_item_status(c, i, 'approved')\n"
    tree = ast.parse(src)
    hits = [n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and getattr(n.func, "attr", None) == "update_item_status"
            and not any(kw.arg == "source" for kw in n.keywords)]
    assert len(hits) == 1, "判据抓不到裸调用 —— 上面那条断言是空转的"
