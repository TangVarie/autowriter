"""worker 的信任边界 —— codex review 的 P1 那几条。

UI 起线程跑生成时, ``db_client`` 是**用户自己的** Supabase 客户端, RLS 兜住了
一切: 查不到别人的项目, 也写不进去。ROB-001/002 把同一段编排搬进 worker 之后
这个前提**没了** —— worker 拿的是 service_role 客户端, 它绕过 RLS。

而 ``jobs`` 的 RLS 策略只约束 ``user_id = auth.uid()``, ``payload`` 是自由
JSONB。也就是说: 任何已登录用户都能插一条 user_id 是自己、payload 里
project_id 是**别人**项目的 job。

所以这个文件盯的不是"功能对不对", 是"**换了个客户端之后, 原来靠 RLS 挡住的
东西还挡不挡得住**"。
"""

from __future__ import annotations

import threading

import pytest

import generation_service as gs
import worker as W


OWNER = "user-me"
INTRUDER = "user-someone-else"


class _Recorder(list):
    def log(self, what, arg=None):
        self.append((what, arg))

    @property
    def names(self):
        return [w for w, _ in self]


@pytest.fixture()
def rec():
    return _Recorder()


def _install_db(monkeypatch, rec, *, owner=OWNER):
    """一个**如实模拟 service_role** 的假 db。

    关键在 get_project: 它像真的 service_role 一样, **不管你是谁都把行回给你**。
    假 db 要是自己偷偷做了归属过滤, 这条用例就永远绿, 而它本该测的正是
    "调用方有没有自己过滤"。
    """
    class _DB:
        WriteReturnedNoRow = RuntimeError

        @staticmethod
        def get_project(client, pid):
            rec.log("db.get_project", pid)
            return {"id": pid, "owner_id": owner, "system_prompt": "人格",
                    "calibration_notes": "校准", "custom_roles": None}

        @staticmethod
        def get_project_owned(client, pid, user_id):
            rec.log("db.get_project_owned", (pid, user_id))
            if user_id != owner:
                return None
            return {"id": pid, "owner_id": owner, "system_prompt": "人格",
                    "calibration_notes": "校准", "custom_roles": None}

        @staticmethod
        def get_confirmed_memories(client, user_id, project_id=None):
            rec.log("db.get_confirmed_memories", project_id)
            return ([], [])

        @staticmethod
        def list_example_items(client, pid, label, limit=5):
            rec.log("db.list_example_items", (pid, label))
            return []

        @staticmethod
        def get_session_instructions(client, user_id, project_id=None):
            rec.log("db.get_session_instructions", project_id)
            return []

    monkeypatch.setattr(gs, "db", _DB)
    return _DB


# ══════════════════════════════════════════════════════════════════════
# 1 · 越权: 快速生成不许碰不属于调用者的项目
# ══════════════════════════════════════════════════════════════════════

def test_quick_gen_refuses_a_project_the_caller_does_not_own(monkeypatch, rec):
    """payload 里塞别人的 project_id → 必须在**读到任何项目数据之前**就拒。

    "之前"这三个字是这条用例的全部重点。事后才发现不对没有意义 —— 那时
    system_prompt / 校准笔记 / 正反例已经被读出来了, 泄露已经发生。
    """
    _install_db(monkeypatch, rec)
    status = {"running": True, "errors": [], "warnings": [],
              "embedding_missing": [], "_lock": threading.Lock()}

    gs.quick_gen_worker({"project_id": "别人的项目", "count": 1},
                        INTRUDER, object(), status)

    assert status["errors"], "越权的 plan 居然没报错"
    joined = " ".join(status["errors"])
    assert "不属于" in joined or "找不到" in joined, joined

    # 项目相关的**任何**取数都不许发生
    leaked = [n for n in rec.names if n in (
        "db.get_confirmed_memories", "db.list_example_items",
        "db.get_session_instructions")]
    assert not leaked, f"拒之前已经读了项目数据: {leaked}"

    # 而且不许走那条不带归属过滤的老路
    assert "db.get_project" not in rec.names, (
        "还在用不带 owner 过滤的 get_project —— service_role 下等于没有访问控制")


def test_quick_gen_still_works_for_the_owner(monkeypatch, rec):
    """把闸修严之后, 本人自己的项目要照常跑得通(不然就是改成了拒绝一切)。"""
    _install_db(monkeypatch, rec)
    status = {"running": True, "errors": [], "warnings": [],
              "embedding_missing": [], "_lock": threading.Lock()}

    # 这里只关心"过了归属这一关", 后面缺 generator 的桩会自己抛 —— 抛在
    # 取数之后就说明闸放行了。
    gs.quick_gen_worker({"project_id": "我的项目", "count": 1},
                        OWNER, object(), status)

    assert ("db.get_project_owned", ("我的项目", OWNER)) in rec
    assert "db.get_confirmed_memories" in rec.names, (
        "本人的项目也被挡住了 —— 这不是修好, 是拒绝一切")


# ══════════════════════════════════════════════════════════════════════
# 2 · payload 校验: UI 上的上限在 worker 这边也得有
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("plan, why", [
    ({"project_id": "p", "count": 10 ** 6}, "count 无上限"),
    ({"project_id": "p", "count": 0}, "count 非正"),
    ({"project_id": "p", "count": 1, "engines": ["rm -rf"]}, "引擎不在白名单"),
    ({"project_id": "p", "count": 1, "engines": []}, "引擎列表为空"),
    ({"project_id": "p", "count": "全部"}, "count 不是整数"),
    ({"project_id": "p", "count": 1, "use_multi_role": True, "n_roles": 9999},
     "角色数无上限"),
    ({"count": 1}, "没有 project_id"),
])
def test_bad_plans_are_rejected_at_the_worker_boundary(plan, why):
    """UI 的控件上限**不是**安全边界 —— 用户可以直接往 jobs 里插 payload。

    这些 plan 都要在 worker 这一层就被打回, 而不是一路带到 generator 里去
    开一个 50 万线程的池, 或者打出无上限的付费模型调用。
    """
    with pytest.raises(ValueError):
        W._validate_plan(plan)


def test_a_normal_plan_passes():
    """别把闸修成"什么都不让过"。"""
    W._validate_plan({"project_id": "p", "count": 3,
                      "engines": ["claude", "gemini"],
                      "use_multi_role": True, "n_roles": 3})


def test_duplicate_engines_are_collapsed():
    """引擎列表去重 —— 池的大小是 角色数 × 引擎数, 重复项会成倍放大它。

    ⚠️ 这里刻意**不是**"列表太长就拒绝": 去重之后长度天然被白名单卡死
    (最多就 KNOWN_ENGINES 那么多个), 再加一条长度上限只是重复。500 个
    重复的 claude 是合法输入, 塌成一个就没事了 —— 拒绝它反而是多余的严。
    """
    plan = {"project_id": "p", "count": 1, "engines": ["claude", "claude", "gemini"]}
    W._validate_plan(plan)
    assert plan["engines"] == ["claude", "gemini"], plan["engines"]

    huge = {"project_id": "p", "count": 1, "engines": ["claude"] * 500}
    W._validate_plan(huge)
    assert huge["engines"] == ["claude"], "500 个重复项没塌下来, 池会被放大 500 倍"
    assert len(huge["engines"]) <= len(W.generator.KNOWN_ENGINES)


# ══════════════════════════════════════════════════════════════════════
# 3 · 排空: SIGTERM 不许把当前批次腰斩成"成功"
# ══════════════════════════════════════════════════════════════════════

def test_shutdown_event_is_not_handed_to_the_orchestration(monkeypatch):
    """``_shutdown`` 只管"不再领新 job", 不许当成"切断当前编排"的信号。

    为什么这是条硬规则: ``process_one`` 在 handler 正常返回之后是**无条件**
    ``finish_job_success`` 的。编排一旦"提前收手然后正常返回", job 就变成终态
    success、进度 100%, 而没跑的 plan 静默消失 —— 滚动发布每次都会踩, 且
    kind 在 NON_IDEMPOTENT_JOB_KINDS 里(max_attempts=1), 重排也兜不回来。

    这条用例直接盯**传下去的那个事件对象是谁**: 只要它是 worker 的 _shutdown,
    就红。断言的是"被禁止的形态", 不是"当前的形态" —— 换个写法照样管用。
    """
    seen = {}

    def _fake_queue_worker(plans, user_id, sb, status, stop_event):
        seen["stop_event"] = stop_event
        status["completed"] = list(plans)

    monkeypatch.setattr(W.gen_service, "queue_worker", _fake_queue_worker)
    monkeypatch.setattr(W, "_fresh_status",
                        lambda sb, jid, **kw: dict(kw, errors=[], warnings=[],
                                                   embedding_missing=[],
                                                   completed=[]))

    W._shutdown.clear()
    try:
        W._generate_batch_handler(
            {"id": "j1", "user_id": OWNER,
             "payload": {"plans": [{"project_id": "p", "count": 1}]}},
            object())
    finally:
        W._shutdown.clear()

    assert seen["stop_event"] is not W._shutdown, (
        "又把 _shutdown 传进编排了 —— SIGTERM 会让这个 job 变成 100% 的假成功")
    assert not seen["stop_event"].is_set()


# ══════════════════════════════════════════════════════════════════════
# 4 · 多 plan 批次的进度要跨队列单调推进
# ══════════════════════════════════════════════════════════════════════

def test_batch_progress_advances_across_plans(monkeypatch):
    """三个 plan 的 job, 进度必须 0→100 走一遍, 不是 0→100 走三遍。

    队列编排每个 plan 都写 status["progress"](单个 plan 内部的 0..1), 所以
    "没写 progress 时才退回 current/total" 那条兜底在第一个 plan 之后就永远
    走不到了。写进 jobs 表的百分比于是每个 plan 都从 0 重来。
    """
    seen = []
    monkeypatch.setattr(W, "_report_progress",
                        lambda sb, jid, pct, msg: seen.append(pct))

    st = W._JobStatus(object(), "j", {"total": 3, "current": 0})
    st._PROGRESS_MIN_INTERVAL = 0.0        # 关掉节流, 只看数值
    for plan_idx in (0, 1, 2):
        st["current"] = plan_idx
        for intra in (0.0, 0.5, 1.0):
            st["progress"] = intra

    assert seen == sorted(seen), f"进度回退了: {seen}"
    assert seen[-1] == 100, seen
    # 三个 plan 里的第一个, 无论内部跑到多少, 都不该把总进度顶过 1/3。
    # (取前 4 次上报 = 换到第二个 plan 之前的全部。)
    assert max(seen[:4]) <= 34, f"第一个 plan 就报到了 {max(seen[:4])}%: {seen}"


def test_quick_gen_progress_is_untouched(monkeypatch):
    """单 plan(快速生成)没有 total, 走原来那条路 —— 别把它一起改了。"""
    seen = []
    monkeypatch.setattr(W, "_report_progress",
                        lambda sb, jid, pct, msg: seen.append(pct))
    st = W._JobStatus(object(), "j", {"progress": 0.0})
    st._PROGRESS_MIN_INTERVAL = 0.0
    for intra in (0.0, 0.4, 1.0):
        st["progress"] = intra
    assert seen == [0, 40, 100], seen
