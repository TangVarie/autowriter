"""审计 ROB-001/002 + SUP-011/012 · 生成编排搬出 Streamlit。

这一步是**搬家**, 不是改逻辑。搬家最典型的失败方式不是某一行写错, 而是
"搬完之后没人说得清行为有没有变" —— 所以这里的用例分两类:

  1. **把"没变"钉死** —— 逐个定义与 app.py 里搬走之前的版本逐字节比对;
  2. **把"为什么能搬"钉死** —— 本模块不许碰 ``st.*``。那是它存在的全部意义,
     哪天有人顺手加一行 ``st.warning`` 就等于把 worker 进程重新锁死。

第 1 类用 git 取搬迁前的版本, 所以只在 git 仓库里有效; 不在仓库里(比如打包
后的运行环境)时跳过, 不让它变成一条假绿。
"""

from __future__ import annotations

import ast
import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

# 搬走的定义。顺序无所谓, 但**不许删**: 少一个就等于少验一个。
MOVED = [
    "_NullLock", "_NULL_LOCK", "_PHASE_WEIGHTS", "_QUEUE_POOL_MAX",
    "_SESSION_SYNC_LIMIT", "_SESSION_USER_TURN_PLACEHOLDER",
    "_resolve_queue_strategy", "_set_phase_progress", "_llm_intra_progress",
    "_save_batch_results", "_run_hard_constraint_check", "_try_regen_one",
    "_run_semantic_dedup_pass", "_format_version_for_session",
    "_resolve_engine_sessions",
    "_commit_session_tokens", "_update_session_occupancy",
    "_queue_worker",
]

# ⚠️ 搬迁之后**被有意改过**的定义: 名字 → (为什么改, 现在谁看着它)。
#
# 逐字节断言只能证明"没动过"。真要改的时候, 从 MOVED 里拿掉一个名字是**必须
# 的**, 但那一步也最容易变成"删掉就绿了"。所以规矩是: 拿掉的同时必须在这里
# 登记, 并指明接手的守卫 —— 下面 test_changed_definitions_are_still_guarded
# 会去核那个守卫文件真的存在、真的提到了这个名字。
#
# 逐字节证明"没动过", 行为测试证明"动了但该变的才变" —— 后者才是改代码需要
# 的东西, 前者只是搬家那一步的临时脚手架。
CHANGED_SINCE_MOVE: dict[str, tuple[str, str]] = {
    "_queue_worker_impl": (
        "SUP-012 抽掉了两段真正重复的代码(_call_generator / _persist_and_check)",
        "tests/test_generation_orchestration.py"),
    "_quick_gen_worker": (
        "同上; 另外归属校验改走 db.get_project_owned(codex P1)",
        "tests/test_generation_orchestration.py"),
    "_sync_approved_to_session": (
        "跨库审计 ROB-015: 原来丢弃 append_session_messages 的返回值, "
        "报的是「打算写几条」而不是「实际写进去几条」",
        "tests/test_session_sync_truthfulness.py"),
}

# 搬迁那一次的 commit 之前, app.py 里还有这些定义。用它取"搬走之前"的样子。
_MOVE_PARENT = "5e95bfb"


def _top_level_sources(src: str) -> dict[str, str]:
    """模块顶层每个 def / class / 简单赋值 的**原始源码片段**。"""
    tree = ast.parse(src)
    lines = src.split("\n")
    out: dict[str, str] = {}
    for node in tree.body:
        name = getattr(node, "name", None)
        if name is None and isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        if name:
            out[name] = "\n".join(lines[node.lineno - 1:node.end_lineno])
    return out


def _git_show(rev: str, path: str) -> str | None:
    r = subprocess.run(["git", "-C", str(REPO), "show", f"{rev}:{path}"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


# ══════════════════════════════════════════════════════════════════════
# 1 · 搬家是逐字节的
# ══════════════════════════════════════════════════════════════════════

def test_every_moved_definition_is_byte_identical():
    """搬走的每个定义, 与搬迁前 app.py 里的那份**一个字符都不能差**。

    为什么要求到逐字节: 这一步唯一的价值就是"可以断定行为没变"。顺手改个变量
    名、顺手把一个 if 提前、顺手删一行注释 —— 每一个都让这个断定失效, 而
    review 的人看着 1800 行的 diff 根本分辨不出哪些是"搬"哪些是"改"。
    清理和合并是后面单独的步骤。
    """
    old_app = _git_show(_MOVE_PARENT, "app.py")
    if old_app is None:
        pytest.skip(f"取不到 {_MOVE_PARENT}:app.py（浅克隆或非 git 环境）")

    before = _top_level_sources(old_app)
    after = _top_level_sources(
        (REPO / "generation_service.py").read_text(encoding="utf-8"))

    missing_before = [n for n in MOVED if n not in before]
    assert not missing_before, (
        f"{_MOVE_PARENT} 的 app.py 里没有这些定义: {missing_before} —— "
        "清单写错了, 或者 _MOVE_PARENT 指错了 commit")

    missing_after = [n for n in MOVED if n not in after]
    assert not missing_after, f"generation_service.py 里少了: {missing_after}"

    differing = [n for n in MOVED if before[n] != after[n]]
    assert not differing, (
        f"这些定义在搬迁过程中被改动了: {differing} —— "
        "这一步只做移动。要改就单独一个 commit, 否则没人说得清行为有没有变")


def test_app_no_longer_defines_them():
    """app.py 里不许留下重复定义 —— 两份会漂, 而且漂了不报错。"""
    cur = _top_level_sources((REPO / "app.py").read_text(encoding="utf-8"))
    leftover = [n for n in list(MOVED) + list(CHANGED_SINCE_MOVE) if n in cur]
    assert not leftover, f"app.py 里还留着: {leftover}"


def test_changed_definitions_are_still_guarded():
    """从逐字节清单里拿掉的每一个, 都必须**真的**有守卫接手。

    这条防的是唯一那个走捷径的办法: 改了代码之后, 把名字从 MOVED 里删掉就
    绿了 —— 而删掉之后它就完全没人看着了。所以每拿掉一个, 这里要求:

      1. 它仍然存在于 generation_service.py（不是被顺手删了）;
      2. 登记的守卫文件**真的存在**;
      3. 那个文件里**真的提到了这个名字**（不是随便填一个路径应付）。

    第 3 条是有意做成字符串检查的: 它挡不住一个故意写来骗过它的守卫, 但挡得住
    "顺手填个看起来合理的文件名"这种真正会发生的情况。
    """
    gs = _top_level_sources(
        (REPO / "generation_service.py").read_text(encoding="utf-8"))
    for name, (why, guard_path) in CHANGED_SINCE_MOVE.items():
        assert name in gs, f"generation_service.py 里没有 {name}（{why}）"
        guard = REPO / guard_path
        assert guard.exists(), (
            f"{name} 登记的守卫 {guard_path} 不存在 —— 它现在没人看着")
        # 私有名在测试里常写成不带下划线的公开别名, 两种都认。
        text = guard.read_text(encoding="utf-8")
        assert name in text or name.lstrip("_") in text, (
            f"守卫 {guard_path} 里根本没提到 {name}")


# ══════════════════════════════════════════════════════════════════════
# 2 · 为什么它能搬: 不碰 st
# ══════════════════════════════════════════════════════════════════════

def test_generation_service_never_touches_streamlit():
    """本模块不许 ``import streamlit``, 也不许出现任何 ``st.*``。

    ⚠️ 判据刻意**不是**"sys.modules 里有没有 streamlit" —— 那永远是有的:
    ``db`` 里有个 ``st.cache_data`` 的缓存 shim, import 链上躲不掉。
    worker.py 早就在同一条件下跑了, 它需要的是"不依赖 ScriptRunContext",
    不是"streamlit 不在内存里"。

    所以这里用 AST 查**本文件自己**的用法。哪天有人顺手加一行 ``st.warning``,
    worker 进程就会在没有 ScriptRunContext 的环境里炸掉(或者更糟: 静默什么
    也不显示), 而那正是这次搬迁要根治的东西。
    """
    tree = ast.parse((REPO / "generation_service.py").read_text(encoding="utf-8"))

    bad_imports = [
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
        for a in n.names if a.name.split(".")[0] == "streamlit"
    ] + [
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
        and (n.module or "").split(".")[0] == "streamlit"
    ]
    assert not bad_imports, f"generation_service.py 直接 import 了 streamlit: {bad_imports}"

    st_uses = [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
        and n.value.id == "st"
    ]
    assert not st_uses, f"generation_service.py 里出现了 st.* (行 {st_uses})"


def test_public_names_exist_and_point_at_the_private_ones():
    """对外的公开名必须真的指向那几个实现, 不能是空壳。"""
    import generation_service as gs
    pairs = [("NULL_LOCK", "_NULL_LOCK"), ("queue_worker", "_queue_worker"),
             ("quick_gen_worker", "_quick_gen_worker"),
             ("queue_worker_impl", "_queue_worker_impl"),
             ("save_batch_results", "_save_batch_results"),
             ("resolve_queue_strategy", "_resolve_queue_strategy")]
    for public, private in pairs:
        assert getattr(gs, public) is getattr(gs, private), (public, private)


def test_app_imports_them_back_under_the_old_names():
    """UI 代码一行没动, 靠的是 app.py 把名字 ``as`` 回原样。

    这条防的是"搬完之后顺手把 app.py 里的调用点也改了" —— 那会让搬迁的
    diff 里混进真正的改动。
    """
    tree = ast.parse((REPO / "app.py").read_text(encoding="utf-8"))
    aliases = {
        a.asname or a.name
        for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
        and n.module == "generation_service" for a in n.names
    }
    assert {"_NULL_LOCK", "_queue_worker", "_quick_gen_worker"} <= aliases, aliases
