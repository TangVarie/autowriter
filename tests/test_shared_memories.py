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
    sb = FakeClient(rows={"memories": rows}, max_rows=3)
    hard, soft = store.shared_memories(sb, PID, ME)
    got = {m["id"] for m in hard + soft}
    assert len(got) == 10, (
        f"只读回 {len(got)}/10 条规则 —— 服务端钳短时被静默截断了")


def test_it_stops_on_an_empty_page_not_a_short_page():
    """终止判据必须是空页。按短页收工的话, 服务端上限低于页大小时**每一页都是
    短页**, 第一页就收工 —— 截断照旧, 只是换了个地方发生。"""
    rows = [_rule(i) for i in range(7)]
    sb = FakeClient(rows={"memories": rows}, max_rows=2)
    hard, soft = store.shared_memories(sb, PID, ME)
    assert len(hard) + len(soft) == 7


# ══════════════════════════════════════════════════════════════════════
# 哪些行不许进去
# ══════════════════════════════════════════════════════════════════════

def test_muted_rules_stay_out():
    """静音是"临时关掉这条规则而不删"。漏过滤 = 用户以为关了、其实还在生效。"""
    rows = [_rule(1),
            _rule(2, muted_until="2099-01-01T00:00:00+00:00")]
    sb = FakeClient(rows={"memories": rows})
    hard, soft = store.shared_memories(sb, PID, ME)
    assert {m["id"] for m in hard + soft} == {"m-1"}


def test_an_expired_mute_comes_back():
    rows = [_rule(1, muted_until="2020-01-01T00:00:00+00:00")]
    sb = FakeClient(rows={"memories": rows})
    hard, soft = store.shared_memories(sb, PID, ME)
    assert len(soft) == 1, "静音期过了却没恢复"


def test_blank_content_stays_out():
    rows = [_rule(1), _rule(2, content="   "), _rule(3, content="")]
    sb = FakeClient(rows={"memories": rows})
    hard, soft = store.shared_memories(sb, PID, ME)
    assert {m["id"] for m in hard + soft} == {"m-1"}


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
    sb = FakeClient(rows={"memories": rows})
    hard, soft = store.shared_memories(sb, PID, ME)
    assert {m["id"] for m in hard + soft} == {"m-1", "m-2"}


# ══════════════════════════════════════════════════════════════════════
# 分层与作用域
# ══════════════════════════════════════════════════════════════════════

def test_hard_and_soft_are_split_and_missing_severity_counts_as_soft():
    rows = [_rule(1, severity="hard"), _rule(2, severity="soft"),
            _rule(3, severity=None), _rule(4, severity="HARD")]
    sb = FakeClient(rows={"memories": rows})
    hard, soft = store.shared_memories(sb, PID, ME)
    assert {m["id"] for m in hard} == {"m-1", "m-4"}, "大小写没归一"
    assert {m["id"] for m in soft} == {"m-2", "m-3"}, "severity 缺失该当 soft"


def test_global_rules_come_along_but_only_with_an_identity():
    g = _rule(9, scope="global", project_id=None)
    sb = FakeClient(rows={"memories": [_rule(1), g]})
    hard, soft = store.shared_memories(sb, PID, ME)
    assert {m["id"] for m in hard + soft} == {"m-1", "m-9"}

    sb2 = FakeClient(rows={"memories": [_rule(1), g]})
    hard2, soft2 = store.shared_memories(sb2, PID, None)
    assert {m["id"] for m in hard2 + soft2} == {"m-1"}, (
        "没有身份却带回了 global 规则 —— 那是按人存的, 会串到别人头上")


# ══════════════════════════════════════════════════════════════════════
# pgvector（R-034）
# ══════════════════════════════════════════════════════════════════════

def test_embeddings_are_deserialized_not_handed_over_as_strings():
    """PostgREST 把 vector 列当**字符串**回。直接喂 cosine_similarity 会因长度
    不等【静默返回 0.0】—— 于是每一条 soft 规则都低于阈值被滤掉, 而且不报错。"""
    rows = [_rule(1, embedding="[0.1,0.2,0.3]")]
    sb = FakeClient(rows={"memories": rows})
    _, soft = store.shared_memories(sb, PID, ME)
    emb = soft[0]["embedding"]
    assert isinstance(emb, list), f"embedding 还是 {type(emb).__name__}"
    assert emb == pytest.approx([0.1, 0.2, 0.3])


# ══════════════════════════════════════════════════════════════════════
# 失败不许降级
# ══════════════════════════════════════════════════════════════════════

def test_a_query_failure_raises_instead_of_returning_no_rules():
    """降级成空列表的话, build_writing_brief 会返回一个 p0 为空、却没有任何
    错误标记的正常简报 —— 调用方照常开写, 而这一批稿子不带任何硬约束。
    那是这个服务最坏的失败模式: 不报错、看起来正常、产出违规内容。"""
    class Boom(FakeClient):
        def _execute(self, q):
            if q.table_name == "memories":
                raise RuntimeError("库挂了")
            return super()._execute(q)

    with pytest.raises(RuntimeError):
        store.shared_memories(Boom(rows={"memories": []}), PID, ME)
