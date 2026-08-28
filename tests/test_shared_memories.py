"""``store.shared_memories`` —— 读项目强制规则的那条路。

这个函数此前**一条测试都没有**, 而它读的是禁词、必含合规话术、绝对不能提的
内容。它的失败模式全是无声的: 少读几条规则不会报错, 只会让这一批稿子少守
几条硬约束。

⚠️ 这里钉的是【被禁止的形态】:

  · 规则条数越过服务端单次上限时不许静默丢掉后面的
  · 被静音 / 空内容 / 非规则类型的行不许混进 P0/P1
  · pgvector 字符串不许原样交出去(相关性过滤会静默算成 0.0, 每条 soft 都被滤掉)
  · 查询失败不许降级成空列表
"""

from __future__ import annotations

import pytest

import db
from deskcore import store
from tests.fakes import FakeClient


ME = "11111111-1111-1111-1111-111111111111"
PID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

# 别人的项目 / 别人的人。这两个常量存在的唯一目的是当【诱饵】。
OTHER_USER = "99999999-9999-9999-9999-999999999999"
OTHER_PID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _rule(i, **kw):
    row = {"id": f"m-{i}", "content": f"规则 {i}", "severity": "soft",
           "scope": "project", "project_id": PID, "user_id": ME,
           "status": "confirmed", "memory_type": "rule",
           "muted_until": None, "rule_kind": None, "rule_payload": None,
           "created_at": f"2026-01-{(i % 28) + 1:02d}T00:00:00+00:00",
           "frequency": 1, "embedding": None}
    row.update(kw)
    return row


# ══════════════════════════════════════════════════════════════════════
# 诱饵 —— 每一行都对应 shared_memories 里的一个过滤条件
# ══════════════════════════════════════════════════════════════════════
# ⚠️ 这批行是这个文件的地基, 不是装饰。第一版的 fixture 【只有一个项目、
# 一个人、全是 confirmed】, 于是"过滤掉了零行"和"根本没有这个过滤"在测试里
# 长得一模一样。实测: 把 shared_memories 的三个过滤逐个删掉,
#
#     .eq("status", "confirmed")                → 322 条测试全绿
#     global 那路的 .eq("user_id", user_id)     → 322 条测试全绿
#     project 那路的 .eq("project_id", ...)     → 322 条测试全绿
#
# 第三条是跨租户泄漏, 而且就在【合规规则】的读取路径上 —— 读到的是别人项目的
# 禁词和必含话术。三条都是本文件号称在守的东西, 三条都没守住。
#
# 加了诱饵之后, 每个过滤条件都变成承重的: 少一个过滤, 对应的诱饵就会漏进结果。
DECOYS = [
    _rule(901, project_id=OTHER_PID, content="别人项目的规则 —— 绝不能出现"),
    _rule(902, scope="global", project_id=None, user_id=OTHER_USER,
          content="别人的通用规则 —— 绝不能出现"),
    _rule(903, status="candidate", content="还没确认的候选 —— 绝不能出现"),
    _rule(904, status="rejected", content="被否掉的 —— 绝不能出现"),
]
DECOY_IDS = {r["id"] for r in DECOYS}


def _client(rows, **kw):
    """建 FakeClient, **永远带上诱饵**。

    刻意做成"想不带都难": 每个用例都走这个入口, 于是每个用例都在顺带验证
    三个过滤条件还在。少写一个过滤, 对应的诱饵就会漏进结果, 用例里那句
    `_ids(...)` 当场红。
    """
    return FakeClient(rows={"memories": list(rows) + DECOYS}, **kw)


def _ids(hard, soft):
    """取回结果的 id 集合, 顺便断言【一条诱饵都没漏出来】。"""
    got = {m["id"] for m in hard + soft}
    leaked = got & DECOY_IDS
    assert not leaked, (
        f"过滤失守, 这些不该出现的行漏进了规则里: {sorted(leaked)} —— "
        "对应的是 project_id / user_id / status 三个过滤之一")
    return got


# ══════════════════════════════════════════════════════════════════════
# 隔离 —— 三条各自点名一个过滤条件
# ══════════════════════════════════════════════════════════════════════
# 上面每个用例都顺带验了这三条(都走 _ids)。这里再单独写一遍, 是因为顺带验到的
# 东西在失败时说不清是哪个过滤掉的 —— 而这三条的后果轻重完全不同。

def test_another_projects_rules_never_leak_in():
    """跨租户泄漏, 三条里最重的一条。

    读的是**合规规则** —— 别人项目的禁词、必含话术会被当成本项目的硬约束注入
    P0。而且反过来也成立: 本项目该守的没守、不该守的守了, 两边都错, 且不报错。
    """
    sb = _client([_rule(1)])
    hard, soft = store.shared_memories(sb, PID, ME)
    assert _ids(hard, soft) == {"m-1"}, "读到了别人项目的规则"


def test_another_users_global_rules_never_leak_in():
    """global 规则是【按人】存的。漏了 user_id 过滤, 同事的通用偏好会串到你
    每一次生成里, 而你完全不知道它从哪来。"""
    mine = _rule(9, scope="global", project_id=None)     # 我自己的 global
    sb = _client([_rule(1), mine])
    hard, soft = store.shared_memories(sb, PID, ME)
    assert _ids(hard, soft) == {"m-1", "m-9"}, "串到别人的 global 规则了"


def test_unconfirmed_memories_never_leak_in():
    """候选记忆是**系统自动抽出来、等人复核**的, 没被确认就不算规则。

    漏了 status 过滤 = 机器猜出来的东西直接变成硬约束。诱饵里 candidate 和
    rejected 各放了一条 —— 被否掉的那条尤其不能回来。
    """
    sb = _client([_rule(1)])
    hard, soft = store.shared_memories(sb, PID, ME)
    assert _ids(hard, soft) == {"m-1"}, "未确认/被否掉的记忆混进规则了"


# ══════════════════════════════════════════════════════════════════════
# 静默截断 —— 本仓 COR-005/006/008 那一族
# ══════════════════════════════════════════════════════════════════════

def test_rules_past_the_server_row_cap_are_not_silently_dropped():
    """服务端把单次请求钳短时, 后面的规则必须照样读回来。

    这条是本文件存在的首要理由。PostgREST 的 ``db-max-rows``(Supabase 默认
    1000)【不报错】, 只是少回几行 —— 越过上限之后的规则在模型眼里根本不存在。
    读的又恰恰是强制合规规则, 所以少的那几条不会有任何迹象。

    ``max_rows=3`` 模拟这个钳位。裸 ``.execute()`` 只会拿到 3 条。
    """
    rows = [_rule(i) for i in range(10)]
    sb = _client(rows, max_rows=3)
    hard, soft = store.shared_memories(sb, PID, ME)
    got = _ids(hard, soft)
    assert len(got) == 10, (
        f"只读回 {len(got)}/10 条规则 —— 服务端钳短时被静默截断了")


def test_it_stops_on_an_empty_page_not_a_short_page():
    """终止判据必须是空页。按短页收工的话, 服务端上限低于页大小时**每一页都是
    短页**, 第一页就收工 —— 截断照旧, 只是换了个地方发生。"""
    rows = [_rule(i) for i in range(7)]
    sb = _client(rows, max_rows=2)
    hard, soft = store.shared_memories(sb, PID, ME)
    assert len(hard) + len(soft) == 7


# ══════════════════════════════════════════════════════════════════════
# 哪些行不许进去
# ══════════════════════════════════════════════════════════════════════

def test_muted_rules_stay_out():
    """静音是"临时关掉这条规则而不删"。漏过滤 = 用户以为关了、其实还在生效。"""
    rows = [_rule(1),
            _rule(2, muted_until="2099-01-01T00:00:00+00:00")]
    sb = _client(rows)
    hard, soft = store.shared_memories(sb, PID, ME)
    assert _ids(hard, soft) == {"m-1"}


def test_an_expired_mute_comes_back():
    rows = [_rule(1, muted_until="2020-01-01T00:00:00+00:00")]
    sb = _client(rows)
    hard, soft = store.shared_memories(sb, PID, ME)
    assert len(soft) == 1, "静音期过了却没恢复"


def test_blank_content_stays_out():
    rows = [_rule(1), _rule(2, content="   "), _rule(3, content="")]
    sb = _client(rows)
    hard, soft = store.shared_memories(sb, PID, ME)
    assert _ids(hard, soft) == {"m-1"}


def test_non_rule_memory_types_stay_out():
    """note 不是写作规则。混进来会被按 severity 塞进 P0/P1。

    ⚠️ 假件把 ``.or_()`` 当无操作 —— 所以这条真正验的是**拉回来之后**用
    ``db._is_rule_memory`` 复核的那一步。服务端过滤和本地复核是两道, 缺了
    本地那道, 这条就会红。
    """
    rows = [_rule(1, memory_type="rule"),
            _rule(2, memory_type=None),        # 老行, 算规则
            _rule(3, memory_type="note"),
            _rule(4, memory_type="session")]
    sb = _client(rows)
    hard, soft = store.shared_memories(sb, PID, ME)
    assert _ids(hard, soft) == {"m-1", "m-2"}


# ══════════════════════════════════════════════════════════════════════
# 分层与作用域
# ══════════════════════════════════════════════════════════════════════

def test_hard_and_soft_are_split_and_missing_severity_counts_as_soft():
    rows = [_rule(1, severity="hard"), _rule(2, severity="soft"),
            _rule(3, severity=None), _rule(4, severity="HARD")]
    sb = _client(rows)
    hard, soft = store.shared_memories(sb, PID, ME)
    assert {m["id"] for m in hard} == {"m-1", "m-4"}, "大小写没归一"
    assert {m["id"] for m in soft} == {"m-2", "m-3"}, "severity 缺失该当 soft"


def test_global_rules_come_along_but_only_with_an_identity():
    g = _rule(9, scope="global", project_id=None)
    sb = _client([_rule(1), g])
    hard, soft = store.shared_memories(sb, PID, ME)
    assert _ids(hard, soft) == {"m-1", "m-9"}

    sb2 = _client([_rule(1), g])
    hard2, soft2 = store.shared_memories(sb2, PID, None)
    assert _ids(hard2, soft2) == {"m-1"}, (
        "没有身份却带回了 global 规则 —— 那是按人存的, 会串到别人头上")


# ══════════════════════════════════════════════════════════════════════
# pgvector（R-034）
# ══════════════════════════════════════════════════════════════════════

def test_embeddings_are_deserialized_not_handed_over_as_strings():
    """PostgREST 把 vector 列当**字符串**回。直接喂 cosine_similarity 会因长度
    不等【静默返回 0.0】—— 于是每一条 soft 规则都低于阈值被滤掉, 而且不报错。"""
    rows = [_rule(1, embedding="[0.1,0.2,0.3]")]
    sb = _client(rows)
    _, soft = store.shared_memories(sb, PID, ME)
    emb = soft[0]["embedding"]
    assert isinstance(emb, list), f"embedding 还是 {type(emb).__name__}"
    assert emb == pytest.approx([0.1, 0.2, 0.3])


# ══════════════════════════════════════════════════════════════════════
# 失败不许降级
# ══════════════════════════════════════════════════════════════════════

class _BoomOn(FakeClient):
    """只让**某一层**的查询炸, 另一层照常。"""

    def __init__(self, *a, scope, **kw):
        super().__init__(*a, **kw)
        self._scope = scope

    def _execute(self, q):
        if q.table_name == "memories" and ("eq", "scope", self._scope) in q.filters:
            raise RuntimeError(f"{self._scope} 那层的库挂了")
        return super()._execute(q)


@pytest.mark.parametrize("scope", ["project", "global"])
def test_a_query_failure_raises_instead_of_returning_no_rules(scope):
    """降级成空列表的话, build_writing_brief 会返回一个 p0 为空、却没有任何
    错误标记的正常简报 —— 调用方照常开写, 而这一批稿子不带任何硬约束。
    那是这个服务最坏的失败模式: 不报错、看起来正常、产出违规内容。

    ⚠️ **必须两层各钉一条。** 第一版的假件对 memories 表一律抛, 而 project
    那层先算先抛 —— 于是 global 那层单独被 try/except 吞掉时测试照样绿, 反过来
    也一样。自审实测: 给 project 层包上 ``except: proj = []`` → 325 条全绿。
    "查询失败不许降级"这句话当时只守住了一半, 而且守住的还不是更要命的那半。
    """
    with pytest.raises(RuntimeError):
        store.shared_memories(_BoomOn(rows={"memories": []}, scope=scope), PID, ME)


def test_the_column_list_still_brings_scope_and_created_at_back():
    """``scope`` 和 ``created_at`` 少了都不报错, 但各自坏一件事:

    · 少 ``scope`` → core.py 里三处 ``m.get("scope") == "global"`` 恒假, 通用
      硬约束被贴上「项目硬约束」的标签注入, P0 的小节标题错位。
    · 少 ``created_at`` → ``db._rank_memories_for_injection`` 的"7 天内新规则
      优先"整档变死代码(空串跟 cutoff 比恒假), 排序退化。规则数超过每个 scope
      12 条封顶时才会真丢规则, 没超只是乱序 —— 但那正是现网 8 个项目的处境。

    对照: 删 ``severity`` / ``muted_until`` / ``embedding`` 本来就会红, 说明
    假件的列裁剪确实在起作用; 这两列补上就闭合了。
    """
    g = _rule(9, scope="global", project_id=None)
    sb = _client([_rule(1), g])
    hard, soft = store.shared_memories(sb, PID, ME)
    got = hard + soft
    assert got, "前提坏了: 一条规则都没取到"
    for m in got:
        missing = {"scope", "created_at"} - set(m)
        assert not missing, f"select 的列清单漏了 {missing}: {sorted(m)}"
