"""``create_project`` —— 新品牌进得来这条路。

为什么有这个工具(2026-08-27 现场): 此前 deskcore 的十二个工具里没有"新建
项目"这个动作 —— 建项目的代码只在停用中的 Streamlit 工作台里。于是文案想给
新品开项目时，模型如实回答"我没有这个工具"; 想跳过项目直接写, skill 又强制
要求先 ``open_project`` 取硬约束。**新品牌完全进不来**, 而这一层在任何文档里
都没写。

这里钉的是【被禁止的形态】, 不是"代码现在长这样":

  · 归属不许由调用方指定 —— 签名里出现 owner 参数就是安全回归
  · 同名不许建第二个 —— 两个同名项目 = 两套互不可见的历史库, 查重从此
    对这个方向失效, 且不报错
  · 建失败不许降级成带 error 的"成功" —— 调用方会拿着不存在的 project_id
    往下走, 后面每个工具都 404 而根因看不见
  · 没有身份不许建
"""

from __future__ import annotations

import inspect

import pytest

from deskcore import core, tools
from tests.fakes import FakeClient


ME = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


def _client(projects=None):
    return FakeClient(rows={"projects": list(projects or [])})


@pytest.fixture(autouse=True)
def _no_real_db(monkeypatch):
    """core.sb() 不许在单测里真去连库。"""
    monkeypatch.setattr(core, "sb", lambda: pytest.fail("不该碰真库"))


# ══════════════════════════════════════════════════════════════════════
# 注册
# ══════════════════════════════════════════════════════════════════════

def test_the_tool_is_actually_registered():
    """写了函数没挂进 TOOLS = 模型看不见它, 等于没做。"""
    assert "create_project" in tools.TOOLS, "create_project 没进 TOOLS 注册表"
    fn, needs_user = tools.TOOLS["create_project"]
    assert needs_user is True, "建项目必须知道调用者是谁, needs_user 不能是 False"


def test_it_needs_an_identity_and_says_so_in_the_signature():
    assert "_user_id" in inspect.signature(tools.create_project).parameters


# ══════════════════════════════════════════════════════════════════════
# 归属 —— 这一条是安全属性
# ══════════════════════════════════════════════════════════════════════

def test_caller_cannot_choose_the_owner():
    """签名里出现 owner 参数就等于允许往别人名下建项目。

    今天刚踩过同类的坑: 一把标着某人名字的 key 实际指向同事的账号, 拿它写稿
    会把稿子记到别人的历史库里。归属只能由服务端从 key 推出来。
    """
    for fn in (core.create_project, tools.create_project):
        params = set(inspect.signature(fn).parameters)
        bad = {p for p in params if "owner" in p.lower()}
        assert not bad, f"{fn.__name__} 的签名里有归属参数: {bad}"


def test_the_new_project_is_owned_by_the_caller():
    sb = _client()
    out = core.create_project(sb, "新品-方向一", brand="新品", user_id=ME)
    row = [r for r in sb.rows["projects"] if r["id"] == out["project_id"]][0]
    assert row["owner_id"] == ME, f"owner 不是调用者: {row['owner_id']}"


def test_no_identity_no_project():
    sb = _client()
    with pytest.raises(PermissionError):
        core.create_project(sb, "无主项目", user_id=None)
    assert not sb.rows["projects"], "没有身份却还是写进去了"


def test_someone_elses_project_with_the_same_name_does_not_block_me():
    """撞名检查只在**自己名下**查。

    查全库既拦错了人, 又等于把别人的项目名暴露给调用方 —— 那正是审计 COR-015
    堵掉的洞。
    """
    sb = _client([{"id": "p-other", "name": "途鸽", "brand": "途鸽",
                   "owner_id": OTHER}])
    out = core.create_project(sb, "途鸽", brand="途鸽", user_id=ME)
    assert out["created"] is True, "被别人的同名项目拦住了"
    assert out["project_id"] != "p-other"


# ══════════════════════════════════════════════════════════════════════
# 撞名 —— 建重了不报错, 但查重从此失效
# ══════════════════════════════════════════════════════════════════════

def test_same_name_returns_the_existing_one_instead_of_creating_a_twin():
    sb = _client([{"id": "p-1", "name": "途鸽-D8", "brand": "途鸽",
                   "owner_id": ME}])
    before = len(sb.rows["projects"])
    out = core.create_project(sb, "途鸽-D8", brand="途鸽", user_id=ME)
    assert out["created"] is False
    assert out["project_id"] == "p-1", "没把已有的那个还回去"
    assert len(sb.rows["projects"]) == before, "撞名了还是建了第二个"
    assert out.get("note"), "撞名时必须说明为什么没建, 否则调用方以为建成了"


def test_name_is_stripped_before_the_duplicate_check():
    """前后空格不该骗过撞名检查 —— 「途鸽 」和「途鸽」是同一个项目。"""
    sb = _client([{"id": "p-1", "name": "途鸽", "brand": "途鸽", "owner_id": ME}])
    out = core.create_project(sb, "  途鸽  ", user_id=ME)
    assert out["created"] is False and out["project_id"] == "p-1"


@pytest.mark.parametrize("existing, asked", [
    ("Sportsix", "sportsix"),          # 库里首字母大写, 又建一个小写的
    ("sportsix", "SPORTSIX"),          # 反过来, 且全大写
    ("RIO轻享", "rio轻享"),             # 中英混排, 只有英文那半差大小写
    ("途鸽 ", "途鸽"),                  # 库里那条带尾随空格(老工作台没 strip 就写进去了)
    (" 途鸽", "途鸽 "),                 # 两边都带空格
])
def test_case_and_whitespace_do_not_create_a_twin(existing, asked):
    """**这是自审揪出的唯一真 bug。**

    原来的判据是服务端 ``.eq("name", name)``, 而 PG 的 ``=`` 对文本大小写敏感。
    于是先建 ``Sportsix`` 再建 ``sportsix``, 第二次 ``created=True``, 库里两行,
    **不报错**。零脏数据即可复现。

    代价不是多一行: 两个同名项目 = 两套互不可见的历史库, ``check_drafts`` 按
    project_id 比, 从此对这个方向永久失效 —— 而这恰恰是 create_project 的
    docstring 里承诺"同名不建第二个"要防的那件事, 被自己的判据破掉了。

    空格那半同理: 老工作台的项目名输入框不 strip, 库里存着 ``"途鸽 "`` 时,
    再建一个 ``"途鸽"`` 照样是两行。
    """
    sb = _client([{"id": "p-1", "name": existing, "brand": "", "owner_id": ME}])
    before = len(sb.rows["projects"])
    out = core.create_project(sb, asked, user_id=ME)
    assert out["created"] is False, (
        f"库里已有「{existing}」, 又建了「{asked}」—— 建出了双胞胎")
    assert out["project_id"] == "p-1"
    assert len(sb.rows["projects"]) == before


def test_genuinely_different_names_still_get_created():
    """别矫枉过正: 只是长得像不算撞名, 否则新方向开不出来。"""
    sb = _client([{"id": "p-1", "name": "途鸽-D1", "brand": "途鸽", "owner_id": ME}])
    out = core.create_project(sb, "途鸽-D2", brand="途鸽", user_id=ME)
    assert out["created"] is True and out["project_id"] != "p-1"


def test_empty_name_is_refused():
    sb = _client()
    for bad in ("", "   ", None):
        with pytest.raises(ValueError):
            core.create_project(sb, bad, user_id=ME)
    assert not sb.rows["projects"]


# ══════════════════════════════════════════════════════════════════════
# 同品牌 —— 给信息, 不拦
# ══════════════════════════════════════════════════════════════════════

def test_same_brand_still_creates_but_surfaces_the_siblings():
    """「途鸽」已经有 D-1..D-7, 再建一个多半是误操作 —— 但也可能是真要开新
    方向。只有人能判断, 所以给信息不拦。"""
    rows = [{"id": f"p-{i}", "name": f"途鸽-D{i}", "brand": "途鸽",
             "owner_id": ME} for i in range(1, 8)]
    # ⚠️ 两条诱饵。没有它们的话, projects_with_brand 的 owner_id 和 brand
    # 两个过滤【删掉任意一个全仓测试都不红】—— 自审实测。这个函数持
    # service_role 绕 RLS, 那两个 .eq 是唯一防线。
    rows += [
        # 别人名下的同品牌: 漏 owner 过滤 = 把别人的项目名和 project_id 交出去
        # (审计 COR-015 堵的正是这个)。
        {"id": "p-other", "name": "途鸽-别人的内部方向", "brand": "途鸽",
         "owner_id": OTHER},
        # 自己名下的别的品牌: 漏 brand 过滤 = 给 RIO 建项目时列出七个途鸽兄弟,
        # 模型照着停下来问一个根本不存在的问题。
        {"id": "p-rio", "name": "RIO-破圈", "brand": "RIO", "owner_id": ME},
    ]
    sb = _client(rows)
    out = core.create_project(sb, "途鸽-D8薪资谈判", brand="途鸽", user_id=ME)
    assert out["created"] is True
    names = {s["name"] for s in out["siblings"]}
    ids = {s["project_id"] for s in out["siblings"]}
    assert len(out["siblings"]) == 7, f"兄弟项目数不对: {names}"
    assert "p-other" not in ids, "把别人名下的同品牌项目当成兄弟项目交出去了"
    assert "p-rio" not in ids, "串品牌了 —— 别的品牌的项目被当成兄弟"
    assert out["project_id"] not in ids, "把自己也算成兄弟项目了"
    assert out.get("siblings_note"), "列了兄弟项目却没说明它们不互相查重"


def test_the_brand_lookup_really_filters_by_owner_in_the_query():
    """除了看结果, 也钉住**那次查询本身**带了 owner 过滤。

    只断结果的话, 将来有人把两条查询合成一句 ``.or_()``(store.py 的注释里正好
    邀请过这件事)照样绿 —— 假件把 ``.or_()`` 当无操作。
    """
    sb = _client([{"id": "p-x", "name": "别人的", "brand": "途鸽", "owner_id": OTHER}])
    core.create_project(sb, "途鸽-新方向", brand="途鸽", user_id=ME)
    proj_queries = [c for c in sb.calls
                    if c["table"] == "projects" and c["op"] == "select"]
    assert proj_queries, "根本没查 projects"
    assert all(("eq", "owner_id", ME) in q["filters"] for q in proj_queries), (
        "有一次 projects 查询没带 owner 过滤: "
        f"{[q['filters'] for q in proj_queries]}")


def test_siblings_note_says_the_libraries_are_separate():
    """这句话是这个字段存在的唯一理由 —— 新项目跟兄弟项目【不互相查重】。
    只列个名单而不说这件事, 调用方看不出风险在哪。"""
    sb = _client([{"id": "p-1", "name": "RIO-破圈", "brand": "RIO", "owner_id": ME}])
    out = core.create_project(sb, "RIO-流量", brand="RIO", user_id=ME)
    assert "查重" in out["siblings_note"]


def test_no_brand_means_no_sibling_lookup():
    sb = _client([{"id": "p-1", "name": "别的", "brand": "", "owner_id": ME}])
    out = core.create_project(sb, "没写品牌的项目", user_id=ME)
    assert out["created"] is True and out["siblings"] == []


# ══════════════════════════════════════════════════════════════════════
# 失败不许降级
# ══════════════════════════════════════════════════════════════════════

def test_a_write_that_returns_no_id_raises_instead_of_pretending():
    """"建成功了但没拿到 id" 必须抛。

    降级成带 error 的"成功"的话, 调用方会拿着一个不存在的 project_id 往下走,
    后面每个工具都 404, 而根因在三步之前且看不见。
    """
    import db
    sb = _client()
    orig = db.create_project
    try:
        db.create_project = lambda *a, **k: {}          # 没有 id
        with pytest.raises(RuntimeError):
            core.create_project(sb, "会失败的项目", user_id=ME)
    finally:
        db.create_project = orig


def _code_only(src: str) -> str:
    """剥掉 docstring 和注释 —— 只留真正会执行的代码。

    ⚠️ 不剥的话这条断言会被【它自己要求写的那句解释】绊倒: 源码里那行
    "故意不包 _safe: ……" 注释本身就含 `_safe`。本会话已经踩过一次同款
    (SQL 里的 `IS NOT DISTINCT FROM` 注释), 那次还一路踩到了生产库上。
    """
    import ast
    tree = ast.parse(src.strip())
    fn = tree.body[0]
    body = fn.body[1:] if (fn.body and isinstance(fn.body[0], ast.Expr)
                           and isinstance(fn.body[0].value, ast.Constant)
                           and isinstance(fn.body[0].value.value, str)) else fn.body
    return "\n".join(ast.unparse(n) for n in body)


def test_the_tool_does_not_swallow_errors_into_a_success_shape():
    """写类工具报错要真的抛, 不能返回 {"error": ...} —— 那会被当成已完成。"""
    code = _code_only(inspect.getsource(tools.create_project))
    assert "_safe" not in code, "create_project 被 _safe 包了, 写失败会伪装成成功"


def test_every_write_tool_stays_unwrapped():
    """断言的是【不变量】而不是这一个函数: 所有写类工具都不许被 _safe 包。

    只钉 create_project 的话, 下一个新增的写工具照样会掉进同一个坑。
    """
    WRITE_TOOLS = ("create_project", "draw_angles", "commit_drafts",
                   "record_rule", "record_edit", "save_my_style",
                   "label_example")
    wrapped = [n for n in WRITE_TOOLS
               if "_safe" in _code_only(inspect.getsource(getattr(tools, n)))]
    assert not wrapped, f"这些写类工具被 _safe 包了, 失败会伪装成成功: {wrapped}"
