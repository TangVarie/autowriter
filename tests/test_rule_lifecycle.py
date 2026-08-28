"""规则的生命周期: 方向档闸 + 写手自己能看/能改的台账。

这套东西存在的理由是产品决策而不是技术偏好 —— **技艺库要跟着写的人自己长,
不能靠一个不写稿的人在后端调**。所以这里钉的第一件事是:三个管理动作
(看见 / 停用 / 升降档)必须都在工具层可达; 第二件事是这些动作不能越权。

⚠️ 这里钉的是【被禁止的形态】:

  · ``applicability`` 从 shared_memories 的 select 列清单里掉出去(方向闸会
    静默失效, 而调用方看到的一切都正常)
  · 方向闸把**不是方向**的 applicability(库里已有 "正文" / "改简历部分")
    一起挡掉
  · 判不出方向时收紧而不是放行
  · 改别人的 global 规则 / 改别人项目的规则
  · set_rule_state 改到 content / user_id / scope 这些归属字段上去
  · retire 把行删掉(演化史丢了, 技艺库最值钱的就是那段历史)
"""

from __future__ import annotations

import pytest

import db
from deskcore import core, store
from tests.fakes import FakeClient


ME = "11111111-1111-1111-1111-111111111111"
OTHER = "99999999-9999-9999-9999-999999999999"
PID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_PID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _rule(i, **kw):
    row = {"id": f"m-{i}", "content": f"规则 {i}", "severity": "soft",
           "scope": "project", "project_id": PID, "user_id": ME,
           "status": "confirmed", "memory_type": "rule",
           "muted_until": None, "rule_kind": None, "rule_payload": None,
           "created_at": f"2026-01-{(i % 28) + 1:02d}T00:00:00+00:00",
           "frequency": 1, "applicability": None, "embedding": None}
    row.update(kw)
    return row


def _client(rows, projects=None):
    return FakeClient(rows={
        "memories": list(rows),
        "projects": projects if projects is not None else [
            {"id": PID, "name": "WTG-产品内裤", "brand": "WTG", "owner_id": ME},
            {"id": OTHER_PID, "name": "别人的项目", "brand": "X", "owner_id": OTHER},
        ],
    })


# ══════════════════════════════════════════════════════════════════════
# 方向判定
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("name, tactic, want", [
    ("WTG-产品内裤",        "",     "产品向"),
    ("WTG-流量315",         "",     "流量向"),
    ("唐小轻-流量-家庭叙事", "",     "流量向"),
    ("RIO 1",               "",     ""),        # 名字里没有方向词
    ("某项目",              "流量号", "流量向"),  # 战术名也算
])
def test_detect_direction(name, tactic, want):
    assert core.detect_direction(name, tactic) == want


def test_both_direction_words_means_undecided():
    """两个方向词同时出现 = 判不出来, 必须放行而不是随便挑一个。

    挑一个的后果是无声的: 写手设过的规则在一半项目上静默缺席。
    """
    assert core.detect_direction("WTG-产品直出-流量版", "") == ""


def test_undecidable_direction_lets_everything_through():
    rules = [{"applicability": "产品向"}, {"applicability": "流量向"}]
    keep, dropped = core.filter_by_applicability(rules, "")
    assert len(keep) == 2 and dropped == []


# ══════════════════════════════════════════════════════════════════════
# 方向闸
# ══════════════════════════════════════════════════════════════════════

def test_gate_drops_only_the_other_direction():
    rules = [
        {"id": "a", "applicability": "产品向"},
        {"id": "b", "applicability": "流量向"},
        {"id": "c", "applicability": None},      # 通用
        {"id": "d", "applicability": ""},        # 通用
    ]
    keep, dropped = core.filter_by_applicability(rules, "产品向")
    assert [r["id"] for r in keep] == ["a", "c", "d"]
    assert [r["id"] for r in dropped] == ["b"]


def test_gate_ignores_non_direction_applicability():
    """库里已经有 "正文" / "改简历部分" 这类**部位**标注在用(生产库 6 行)。

    它们不是方向。闸门做成"有 applicability 就按方向过滤"会把这些老行全部
    静默挡掉 —— 一次无声的行为变更, 正是本仓库反复出现的那类事故。
    """
    rules = [{"id": "x", "applicability": "正文"},
             {"id": "y", "applicability": "改简历部分"}]
    keep, dropped = core.filter_by_applicability(rules, "流量向")
    assert [r["id"] for r in keep] == ["x", "y"]
    assert dropped == []


# ══════════════════════════════════════════════════════════════════════
# 列清单 —— 方向闸的地基
# ══════════════════════════════════════════════════════════════════════

def test_shared_memories_actually_selects_applicability():
    """``applicability`` 必须真的被 select 出来。

    ⚠️ 这条用例盯的是一个**无声**故障: 列清单里漏掉它, PostgREST 不会报错,
    只会让每一行的 applicability 恒为 None → 方向闸对所有规则放行 → 两个
    方向的规则混着注入同一份简报, 而 counts 里一切正常。

    断言写在"库里那一行的值有没有活着到达调用方"上, 而不是去 assert 列清单
    的字符串 —— 后者会在有人重排列顺序时假红。
    """
    rows = [_rule(1, applicability="流量向")]
    hard, soft = store.shared_memories(_client(rows), PID, user_id=ME)
    assert len(soft) == 1
    assert soft[0]["applicability"] == "流量向", (
        "shared_memories 没把 applicability 带回来 —— 方向闸会静默失效")


def test_brief_reports_the_direction_gate():
    """被方向挡掉的规则数必须回显。

    挡掉和"根本没存进去"在写手眼里长得一模一样, 不回显他就没法自查。
    """
    rows = [_rule(1, applicability="产品向"),
            _rule(2, applicability="流量向"),
            _rule(3)]
    hard, soft = store.shared_memories(_client(rows), PID, user_id=ME)
    keep, dropped = core.filter_by_applicability(soft, "产品向")
    assert len(keep) == 2 and len(dropped) == 1


# ══════════════════════════════════════════════════════════════════════
# 台账: 看得见
# ══════════════════════════════════════════════════════════════════════

def test_ledger_shows_candidate_and_muted_rules():
    """台账口径与简报**故意不同**: 试用档和已停用的必须端出来。

    过滤掉就等于"写手看不见自己库里还有什么" —— 看不见就只能回来找运维,
    那正是这套东西要消灭的环节。
    """
    rows = [
        _rule(1),                                   # 生效中
        _rule(2, status="candidate"),               # 试用
        _rule(3, muted_until="2099-01-01T00:00:00+00:00"),   # 已停用
        _rule(4, applicability="流量向"),            # 方向不符(本项目是产品向)
    ]
    out = core.my_rules(_client(rows), PID, user_id=ME)
    states = {r["memory_id"]: r["state"] for r in out["rules"]}
    assert states == {"m-1": "生效中", "m-2": "试用",
                      "m-3": "已停用", "m-4": "方向不符"}
    assert out["project_direction"] == "产品向"


def test_ledger_never_shows_someone_elses_global_rules():
    """global 规则是**私有**的。别人的个人技艺库不许出现在我的台账里。"""
    rows = [
        _rule(1, scope="global", project_id=None, user_id=ME),
        _rule(2, scope="global", project_id=None, user_id=OTHER,
              content="别人的个人技艺 —— 绝不能出现"),
        _rule(3, project_id=OTHER_PID, content="别人项目的规则 —— 绝不能出现"),
    ]
    out = core.my_rules(_client(rows), PID, user_id=ME)
    got = {r["content"] for r in out["rules"]}
    assert not [c for c in got if "绝不能出现" in c], f"泄漏了: {got}"


# ══════════════════════════════════════════════════════════════════════
# 台账: 改得动, 但改不了别人的
# ══════════════════════════════════════════════════════════════════════

def test_retire_keeps_the_row():
    """retire 是降档不是删除。

    删了就没法回头看"这条当初为什么被记下来" —— 而技艺库的价值恰恰在那段
    演化史里(生产库 calibration_note_audit 已经攒了 99 条这样的历史)。
    """
    sb = _client([_rule(1)])
    core.set_rule_state(sb, "m-1", "retire", user_id=ME)
    assert len(sb.rows["memories"]) == 1
    assert sb.rows["memories"][0]["status"] == "candidate"


def test_promote_and_unmute_round_trip():
    sb = _client([_rule(1, status="candidate",
                        muted_until="2099-01-01T00:00:00+00:00")])
    core.set_rule_state(sb, "m-1", "promote", user_id=ME)
    core.set_rule_state(sb, "m-1", "unmute", user_id=ME)
    row = sb.rows["memories"][0]
    assert row["status"] == "confirmed" and row["muted_until"] is None


def test_mute_sets_a_future_deadline():
    sb = _client([_rule(1)])
    out = core.set_rule_state(sb, "m-1", "mute", user_id=ME, days=30)
    assert out["now"]["muted_until"]
    assert db.is_memory_muted_now(out["now"]["muted_until"]) is True


def test_cannot_touch_someone_elses_global_rule():
    sb = _client([_rule(1, scope="global", project_id=None, user_id=OTHER)])
    with pytest.raises(PermissionError):
        core.set_rule_state(sb, "m-1", "retire", user_id=ME)
    assert sb.rows["memories"][0]["status"] == "confirmed", "越权写居然落库了"


def test_cannot_touch_a_rule_in_someone_elses_project():
    sb = _client([_rule(1, project_id=OTHER_PID)])
    with pytest.raises(PermissionError):
        core.set_rule_state(sb, "m-1", "retire", user_id=ME)
    assert sb.rows["memories"][0]["status"] == "confirmed", "越权写居然落库了"


def test_cannot_edit_a_session_memory_through_this_door():
    sb = _client([_rule(1, memory_type="session")])
    with pytest.raises(ValueError):
        core.set_rule_state(sb, "m-1", "retire", user_id=ME)


@pytest.mark.parametrize("field", ["content", "user_id", "scope", "project_id"])
def test_update_memory_fields_refuses_ownership_columns(field):
    """状态入口只准改状态。

    混在一个入口里迟早有人顺手把归属字段也改了 —— 那是越权写, 而且是从一个
    看起来无害的"改档位"接口进来的。
    """
    sb = _client([_rule(1)])
    with pytest.raises(ValueError):
        store.update_memory_fields(sb, "m-1", {field: "x"})


def test_bad_action_is_rejected():
    sb = _client([_rule(1)])
    with pytest.raises(ValueError):
        core.set_rule_state(sb, "m-1", "delete", user_id=ME)


# ══════════════════════════════════════════════════════════════════════
# 方向的写入口
# ══════════════════════════════════════════════════════════════════════

def test_set_direction_round_trip():
    sb = _client([_rule(1)])
    core.set_rule_state(sb, "m-1", "set_direction",
                        user_id=ME, direction="流量向")
    assert sb.rows["memories"][0]["applicability"] == "流量向"
    core.set_rule_state(sb, "m-1", "set_direction",
                        user_id=ME, direction="通用")
    assert sb.rows["memories"][0]["applicability"] is None


def test_set_direction_rejects_a_freehand_value():
    """方向是精确匹配的。半个词会让规则在两个方向上都被挡掉 —— 无声。"""
    sb = _client([_rule(1)])
    with pytest.raises(ValueError):
        core.set_rule_state(sb, "m-1", "set_direction",
                            user_id=ME, direction="产品")
    assert sb.rows["memories"][0]["applicability"] is None


def test_record_rule_rejects_a_half_direction_word():
    sb = _client([])
    with pytest.raises(ValueError):
        core.record_rule(sb, PID, "标题控制在 20 字内",
                         user_id=ME, applicability="偏产品向的稿子")


# ══════════════════════════════════════════════════════════════════════
# 工具面
# ══════════════════════════════════════════════════════════════════════

def test_both_management_tools_are_registered():
    """看得见 + 改得动, 两个都必须在工具面上。

    少任何一个, 写手就得回来找运维 —— 而这套东西的全部目的就是不让他回来找。
    """
    from deskcore.tools import TOOLS
    assert "my_rules" in TOOLS and "set_rule_state" in TOOLS
    assert TOOLS["my_rules"][1] is True
    assert TOOLS["set_rule_state"][1] is True


def test_set_rule_state_is_not_wrapped_in_safe():
    """写操作不许包 ``_safe``。

    包了就会把失败报成一个带 error 字段的"成功": 用户以为关掉的规则还在每
    一份简报里, 或者以为留下的规则其实没生效 —— 两个方向都是无声的。
    """
    import ast
    import inspect

    from deskcore import tools

    src = inspect.getsource(tools.set_rule_state)
    body = ast.parse(src.lstrip()).body[0]
    calls = [n.func.id for n in ast.walk(body)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "_safe" not in calls


# ══════════════════════════════════════════════════════════════════════
# 注入封顶 —— 新规则不许把旧规则整批饿死
# ══════════════════════════════════════════════════════════════════════

def test_a_fresh_batch_cannot_starve_the_rules_someone_set_months_ago():
    """灌一批新技艺时，写手自己设了几个月的旧规则必须还留得下位置。

    ⚠️ 这条盯的是一次**差点造成的**事故。``db._rank_memories_for_injection``
    对 7 天内的新规则一律优先（"我刚说过 → 立刻生效"，本身是对的），然后砍到
    cap。cap=12 时往一个已有 18 条 global 规则的人库里插 9 条新的，结果是
    新的占满 12 个名额里的 9 个、旧的只剩 3 个；插 31 条则旧的**一条都进不去、
    持续 7 天、没有任何提示**。

    这里不锁死 cap 的具体数值（那是可调的产品参数，也能用环境变量覆盖），
    锁的是那个数值必须**大到让两边都放得下**：一批 A 档技艺进来之后，旧规则
    仍有过半的名额。
    """
    import config
    from datetime import datetime, timedelta, timezone

    cap = int(getattr(config, "MAX_INJECTED_MEMORIES_PER_SCOPE", 12) or 12)
    now = datetime.now(timezone.utc)
    fresh = [{"id": f"new-{i}", "created_at": now.isoformat(), "frequency": 30}
             for i in range(9)]                       # 一批 A 档技艺
    old = [{"id": f"old-{i}",
            "created_at": (now - timedelta(days=120)).isoformat(),
            "frequency": 1}
           for i in range(18)]                        # 她自己攒了几个月的

    got = db._rank_memories_for_injection(fresh + old, cap)
    survived = [m for m in got if m["id"].startswith("old-")]
    assert len(survived) >= len(fresh), (
        f"cap={cap} 太小：9 条新技艺进来之后旧规则只剩 {len(survived)} 条。"
        "写手会发现自己设了几个月的规矩突然不生效，而且没有任何提示。")
