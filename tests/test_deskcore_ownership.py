"""审计 COR-015 · deskcore 的项目归属校验。

修复之前 deskcore 对 ``project_id`` 【没有任何归属校验】: 十一个工具里三个
(``list_projects`` / ``borrow_lessons`` / ``check_drafts``)连"调用者是谁"都不问,
其余八个虽然拿到了 user_id 却只用它读个人层, 从不核对项目归谁。于是任一持有效
key 的调用方传入他人 project_id 就能读他人成稿标题、往他人项目写规则和指纹。

口径已定: **按 ``projects.owner_id`` 隔离**, 与 db.py 里那条 RLS policy
``owner_id = auth.uid()`` 同一判据 —— deskcore 持 service_role 绕过 RLS,
就得自己把同一条谓词执行一遍。

本文件盯三件事, 缺一不可:

  1. **判据本身对**   —— assert_project_access 的五种输入各自的结果;
  2. **每个入口都过了这道闸** —— 用 AST 遍历钉死, 不靠"我记得都加了";
  3. **拒绝不会被吞掉** —— _safe 不许把 PermissionError 变成"看起来成功"。

第 2 条是这里最有价值的一条: 归属校验最典型的失效方式不是判据写错, 而是
**新加了个工具忘了加校验**, 而那不会报错, 只会继续放行。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from deskcore import core, store, tools
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
MINE = "aaaaaaaa-0000-0000-0000-000000000001"
THEIRS = "bbbbbbbb-0000-0000-0000-000000000002"

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _client(**extra) -> FakeClient:
    rows = {"projects": [
        {"id": MINE, "name": "我的项目", "brand": "A", "owner_id": ME,
         "calibration_notes": "", "tactics": "[]", "custom_roles": []},
        {"id": THEIRS, "name": "别人的项目", "brand": "B", "owner_id": OTHER,
         "calibration_notes": "", "tactics": "[]", "custom_roles": []},
    ]}
    rows.update(extra)
    return FakeClient(rows=rows)


# ══════════════════════════════════════════════════════════════════════
# 1 · 判据本身
# ══════════════════════════════════════════════════════════════════════

def test_owner_passes_and_gets_the_row_back():
    """通过时把项目整行交回去 —— 这不是顺手, 是为了**不多发一次查询**。

    build_writing_brief / draw_angles / my_style / borrow_lessons 本来就要读项目行。
    如果校验只回 True/False, 这四条最热的路径每次都要多一个来回。
    """
    sb = _client()
    row = core.assert_project_access(sb, MINE, user_id=ME)
    assert row["id"] == MINE and row["owner_id"] == ME
    # 只查了一次 projects, 没有额外往返
    assert [c["table"] for c in sb.calls] == ["projects"]


def test_other_owners_project_is_refused():
    with pytest.raises(PermissionError) as exc:
        core.assert_project_access(_client(), THEIRS, user_id=ME)
    assert THEIRS in str(exc.value)


def test_missing_caller_identity_is_refused_not_waved_through():
    """认不出人时【拒绝】, 与 ROB-003 同一口径。

    deskcore 持 service_role 绕过 RLS —— "认不出就放行"意味着一个配置疏忽
    (漏配 DESKCORE_DEFAULT_USER_ID)就等于对所有租户开放。
    """
    sb = _client()
    with pytest.raises(PermissionError):
        core.assert_project_access(sb, MINE, user_id=None)
    # 而且是在**查库之前**就拒了 —— 认不出人时连项目存不存在都不该泄露
    assert sb.calls == []


def test_unknown_project_is_not_found_not_forbidden():
    """"不存在"和"不是你的"必须是两种错。

    混成一种的后果很具体: 调用方分不清该改 id 还是该换项目, 于是拿同一个
    错 id 反复试。ProjectNotFound 是 ValueError 的子类, 老的
    ``except ValueError`` 调用方不会因此漏接。
    """
    with pytest.raises(core.ProjectNotFound):
        core.assert_project_access(_client(), "no-such-project", user_id=ME)
    assert issubclass(core.ProjectNotFound, ValueError)
    assert not issubclass(core.ProjectNotFound, PermissionError)


def test_blank_project_id_is_rejected_before_querying():
    sb = _client()
    for bad in ("", "   ", None):
        with pytest.raises(core.ProjectNotFound):
            core.assert_project_access(sb, bad, user_id=ME)
    assert sb.calls == []


# ══════════════════════════════════════════════════════════════════════
# 2 · 每个入口都过了这道闸
# ══════════════════════════════════════════════════════════════════════

# core 里【背书 MCP 工具】的项目级入口。label_example 不在这里: 它按 item 校验
# 归属(正负例是个人资产, 粒度比项目更细), 那道校验早就在, 见 test 的最后一节。
GATED_ENTRYPOINTS = [
    "build_writing_brief", "draw_angles", "check_drafts", "commit_drafts",
    "export_drafts",
    "record_rule", "record_edit", "save_my_style", "my_style", "borrow_lessons",
]


def test_every_project_scoped_entrypoint_calls_the_gate():
    """AST 遍历: 上面每个函数体里都必须出现一次 ``assert_project_access(...)``。

    ⚠️ 这条断言的形态是有讲究的。它钉的是【被禁止的形态】——"一个项目级入口
    body 里找不到这个调用"—— 而不是"代码现在长这样"。所以:

      · 有人新加工具忘了加校验 → 只要把函数名列进 GATED_ENTRYPOINTS 就会红;
      · 有人重构了参数顺序、改了变量名、把校验挪到别的分支 → 不会误报。

    第三批的教训正相反: 当时写了 ``assert "_paged(" in body``, 钉的是当前形态,
    一次正当重构就假警报。(见 docs/audit-2026-08-23-full.md §0.4 末尾)
    """
    tree = ast.parse((REPO_ROOT / "deskcore" / "core.py").read_text(encoding="utf-8"))
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    missing = []
    for name in GATED_ENTRYPOINTS:
        node = fns.get(name)
        assert node is not None, f"core.py 里没有 {name} —— 改名了就同步改这份清单"
        called = any(
            isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
            and sub.func.id == "assert_project_access"
            for sub in ast.walk(node))
        if not called:
            missing.append(name)

    assert not missing, (
        f"这些项目级入口没有过归属校验: {missing} —— "
        "审计 COR-015 的整条修复就是靠「每个入口都以这一行开头」成立的")


@pytest.mark.parametrize("call", [
    lambda sb, pid: core.build_writing_brief(sb, pid, user_id=ME, brief={}),
    lambda sb, pid: core.draw_angles(sb, pid, 3, user_id=ME),
    lambda sb, pid: core.check_drafts(sb, pid, [], user_id=ME),
    lambda sb, pid: core.commit_drafts(sb, pid, [], user_id=ME),
    lambda sb, pid: core.export_drafts(sb, pid, batch_id="b-1", user_id=ME),
    lambda sb, pid: core.record_rule(sb, pid, "别用数字开头", user_id=ME),
    lambda sb, pid: core.record_edit(sb, pid, user_id=ME, my_title="改过的标题"),
    lambda sb, pid: core.save_my_style(sb, pid, "笔记", user_id=ME),
    lambda sb, pid: core.my_style(sb, pid, user_id=ME),
    lambda sb, pid: core.borrow_lessons(sb, pid, user_id=ME),
], ids=GATED_ENTRYPOINTS)
def test_entrypoint_refuses_someone_elses_project(call):
    """上一条验"调了校验", 这一条验"调了之后真的拦得住"。

    两条都要: AST 只知道那个函数名出现过, 不知道返回值有没有被无视。
    """
    with pytest.raises(PermissionError):
        call(_client(), THEIRS)


@pytest.mark.parametrize("call", [
    lambda sb, pid: core.draw_angles(sb, pid, 0, user_id=ME),
    lambda sb, pid: core.check_drafts(sb, pid, [], user_id=ME),
    lambda sb, pid: core.commit_drafts(sb, pid, [], user_id=ME),
], ids=["draw_angles(n=0)", "check_drafts([])", "commit_drafts([])"])
def test_gate_runs_before_the_degenerate_early_return(call):
    """空入参的早返回【不能】绕过校验。

    这三个函数都有"没东西可做就直接回"的分支。校验若排在它后面, 拿别人的
    project_id 调一次空 drafts 就能安静地拿到 200 —— 虽然这一次没读到东西,
    但它把"这个 project_id 有效"这件事确认了, 而且下次带上真 drafts 就读到了。
    """
    with pytest.raises(PermissionError):
        call(_client(), THEIRS)


def test_list_projects_only_returns_my_own():
    """清单按 owner 过滤 —— 它原来返回**全库**项目台账。

    危害不只是"看到了别人的项目名": 那份清单里带着 project_id, 而 project_id
    正是喂给其它十个工具的钥匙。
    """
    sb = _client()
    got = core.list_projects(sb, user_id=ME)
    assert [p["project_id"] for p in got] == [MINE]

    # 过滤要发生在**库里**(.eq), 不是拉回来再筛 —— 否则翻页会把别人的项目
    # 算进那 1000 行的配额, 我自己的项目反而被 db-max-rows 挤掉。
    first = sb.calls[0]
    assert ("eq", "owner_id", ME) in first["filters"], first


def test_list_projects_without_identity_is_refused():
    with pytest.raises(PermissionError):
        core.list_projects(_client(), user_id=None)


def test_list_projects_still_costs_a_fixed_number_of_queries():
    """加了 owner 过滤不能把 SUP-004 的成果吃回去。

    第三批把这里从"每项目 2 次"改成了【3 次固定查询】。owner 过滤只是第一次
    查询多一个 .eq, 与项目个数无关 —— 这条断言防的是有人"顺手"改成逐项目校验。
    """
    # ⚠️ 必须把 migrations/004 的 RPC 装上。不装的话 store.fingerprint_counts
    # 返回 None, list_projects 走【设计好的】逐项目 count 退路 —— 那是"迁移没跑"
    # 的正确行为, 不是回归。第一版就漏了这个, 于是断言在测别的东西。
    sb = FakeClient(
        rows={"projects": [
            {"id": f"p{i}", "name": f"n{i}", "brand": "", "owner_id": ME}
            for i in range(25)
        ]},
        rpc_impl={"deskcore_fingerprint_counts":
                  lambda args: [{"project_id": pid, "n": 0}
                                for pid in args["_project_ids"]]},
    )
    core.list_projects(sb, user_id=ME)
    # 翻页会多发一次拿空页的请求(空页收工, 见 store._paged), 所以是"常数"而
    # 不是某个精确值; 关键是它【不随项目数增长】。25 个项目远超任何 per-project
    # 方案的预算。
    assert len(sb.calls) + len(sb.rpc_calls) <= 6, (
        f"{len(sb.calls)} 次 table 查询 + {len(sb.rpc_calls)} 次 RPC —— "
        "看起来退回逐项目查询了")


def test_store_list_all_projects_requires_owner_keyword():
    """``owner_id`` 是**关键字必填**, 少传直接 TypeError。

    这是防"顺手去掉过滤"的机械保险: 想让它回到不过滤的老行为, 得先改签名。
    """
    with pytest.raises(TypeError):
        store.list_all_projects(_client())        # type: ignore[call-arg]


# ══════════════════════════════════════════════════════════════════════
# 3 · 拒绝不会被吞掉
# ══════════════════════════════════════════════════════════════════════

def test_all_tools_now_require_caller_identity():
    """TOOLS 里不该再有 needs_user=False。

    原来的三个 False 不是省事, 是"这三个工具连调用者是谁都不问"的直接写照 ——
    也正是越权读的入口。
    """
    anonymous = [n for n, (_fn, needs) in tools.TOOLS.items() if not needs]
    assert anonymous == [], (
        f"这些工具仍然不要求身份: {anonymous} —— "
        "新工具默认要 True; 要写 False 得先说清它凭什么不需要知道是谁在调")


def test_safe_wrapper_does_not_swallow_permission_errors():
    """``_safe`` 兜的是瞬时故障, 不该把"这个项目不是你的"包成看起来成功的结果。

    包了的后果有两层: 调用方模型看到 error 字段仍会继续拿同一个错 project_id
    去试下一个工具; REST 层的 403 也只对一部分工具成立了。
    """
    def _denied():
        raise PermissionError("nope")

    with pytest.raises(PermissionError):
        tools._safe(_denied)

    # 而普通异常照旧要被兜住(这是 _safe 存在的理由, 别一起改坏了)
    def _boom():
        raise RuntimeError("库抖了一下")

    out = tools._safe(_boom)
    assert isinstance(out, dict) and "error" in out and "hint" in out


def test_rest_layer_maps_denial_to_403_not_500():
    """``deskcore/app.py`` 必须把 PermissionError 映射成 403。

    ⚠️ 用 AST 读源码而不是 import —— app.py 需要 fastapi/mcp, 而 CI 跑 pytest
    那一步【只装 requirements.lock】。第三批就在这个边界上栽过一次
    (ModuleNotFoundError: fastapi), 所以这里不 import。

    为什么 500 不行: 500 对调用方的意思是"服务端坏了, 待会儿重试", 于是模型会
    拿同一个错 project_id 一直试; 而且正常的权限拒绝会去污染错误监控。
    """
    tree = ast.parse((REPO_ROOT / "deskcore" / "app.py").read_text(encoding="utf-8"))

    handlers = [h for node in ast.walk(tree) if isinstance(node, ast.Try)
                for h in node.handlers
                if isinstance(h.type, ast.Name) and h.type.id == "PermissionError"]
    assert handlers, "app.py 没有任何 except PermissionError —— 拒绝会掉进 500 兜底"

    codes = {kw.value.value
             for h in handlers for sub in ast.walk(h)
             if isinstance(sub, ast.Call)
             for kw in sub.keywords
             if kw.arg == "status_code" and isinstance(kw.value, ast.Constant)}
    assert 403 in codes, f"PermissionError 分支里没有 403, 只有 {codes or '没有状态码'}"


# ══════════════════════════════════════════════════════════════════════
# 4 · label_example 的粒度不变
# ══════════════════════════════════════════════════════════════════════

def test_label_example_still_checks_the_item_not_the_project():
    """正负例按 **item.user_id** 校验, 比项目粒度更细 —— 这道校验本来就在。

    别在统一归属口径的时候把它降级成项目级: 同一个项目里, A 的正负例是 A 的
    个人风格资产, B 改不得。
    """
    sb = FakeClient(rows={"items": [
        {"id": "item-mine", "user_id": ME},
        {"id": "item-theirs", "user_id": OTHER},
    ]})
    with pytest.raises(PermissionError):
        core.label_example(sb, "item-theirs", "positive", user_id=ME)

    with pytest.raises(ValueError):        # 不存在的 item
        core.label_example(sb, "item-nope", "positive", user_id=ME)
