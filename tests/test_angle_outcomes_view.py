"""migrations/011 的视图(发牌台账 → 版本 → TV 笔记 → tier)—— 不连库也能守的那几条。

视图的**行为**(建得出来、口径、以 service_role 读得穿 RLS、非属主来建当场报错)在
``tests/sql_parity_check.py`` 里对着真 PostgreSQL 验; 那一步只在 CI 起库时跑。这里守的是
每次 pytest 都该拦住的三种漂移:

  1. 基线 000 与 011 的那段 DO 块**逐字相同** —— 与 test_baseline_parity 同一个理由:
     完整链条上 011 会把基线那版盖掉, 基线漂了从终态上永远看不出来;
  2. 认哪几种对照(match_kind)必须与 ``core._TV_BACKFILL_KINDS`` 是同一份名单 ——
     「哪些对照算真对上了」这件事在写回 TV 和算坐标结果两处必须同口径;
  3. 权限的形状: 只 GRANT 给 service_role, 且显式 REVOKE 掉 anon / authenticated。
"""

from __future__ import annotations

import re
from pathlib import Path

from deskcore import core

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
_BLOCK = re.compile(r"^DO \$v011\$.*?^\$v011\$;\n", re.S | re.M)


def _block(name: str) -> str:
    m = _BLOCK.search((MIGRATIONS / name).read_text(encoding="utf-8"))
    assert m, f"{name} 里找不到 DO $v011$ … $v011$; 那一段"
    return m.group(0)


def test_baseline_carries_the_same_view_block_as_011():
    assert _block("000_baseline.sql") == _block("011_angle_outcomes_view.sql"), (
        "v_angle_outcomes 在基线与 011 之间漂了。只跑 000 的新库会拿到基线那版, 而完整"
        "链条上 011 会把它盖掉 —— sql_parity_check 看不出来。把 011 里整段原样复制进基线。")


def test_match_kinds_are_the_same_list_as_the_tv_backfill_kinds():
    block = _block("011_angle_outcomes_view.sql")
    m = re.search(r"match_kind IN \(([^)]*)\)", block)
    assert m, "视图里找不到 match_kind 的过滤"
    kinds = set(re.findall(r"'([a-z_]+)'", m.group(1)))
    assert kinds == set(core._TV_BACKFILL_KINDS), (
        f"视图认的对照 {sorted(kinds)} 与写回 TV 认的 {sorted(core._TV_BACKFILL_KINDS)} "
        "不一致 —— ingested 是从笔记复制出来的版本(因果倒置), 两处都不该认")
    assert "ingested" not in kinds


def test_only_service_role_is_granted_and_the_view_reads_as_its_owner():
    block = _block("011_angle_outcomes_view.sql")
    assert "security_invoker = false" in block, (
        "要显式写 security_invoker = false: 三张底表 RLS 开着且没有 policy, 以调用者身份读"
        "的话, 除了 BYPASSRLS 的角色谁读都是 0 行")
    grants = re.findall(r"GRANT (\w+) ON autowriter\.v_angle_outcomes TO (\w+)", block)
    assert grants == [("SELECT", "service_role")], grants
    for role in ("PUBLIC", "anon", "authenticated"):
        assert f"REVOKE ALL ON autowriter.v_angle_outcomes FROM {role}" in block, (
            f"没有显式收回 {role}: 这是以属主身份读、绕过 RLS 的明细视图, 不能靠「本来就"
            "没发」—— 以后谁改了默认授权, 它会一下子露出去")
