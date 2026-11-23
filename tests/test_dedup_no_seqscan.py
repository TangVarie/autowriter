"""migrations/023: 查重的两个函数不许走全表顺序扫描(2026-11-23, docs/deskcore.md §3.38)。

指纹表大了以后计划器绕开 GIN 索引、逐行比全表, 一批 5 篇超过 PostgREST 的 8 秒上限,
check_drafts / score_drafts 报 APIError code=57014, 写手交付不了。修法只是给两个函数加
``SET enable_seqscan = off``。这里钉三件事: 迁移里两句都在(不是注释), 基线里也有(只跑 000 的
新库同样不慢), doctor 把 023 报成 unprobeable 并交出查的 SQL。纯文本 + 假库, 不需要数据库。
"""

from __future__ import annotations

import re
from pathlib import Path

from deskcore import core

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

SIGS = {
    "deskcore_check_drafts": r"UUID,\s*JSONB,\s*INT(?:EGER)?",
    "deskcore_commit_fingerprints": r"UUID,\s*JSONB,\s*UUID,\s*NUMERIC,\s*NUMERIC,\s*INT(?:EGER)?",
}


def _code(path: Path) -> str:
    """去掉整行注释(回滚说明里也写着这两个函数名)。"""
    return "\n".join(ln for ln in path.read_text(encoding="utf-8").splitlines()
                     if not ln.lstrip().startswith("--"))


def _has_no_seqscan(text: str, fn: str) -> bool:
    pat = (r"ALTER\s+FUNCTION\s+autowriter\." + fn + r"\s*\(\s*" + SIGS[fn]
           + r"\s*\)\s*SET\s+enable_seqscan\s*=\s*off\s*;")
    return re.search(pat, text, re.I) is not None


def test_023_sets_the_switch_on_both_functions():
    text = _code(MIGRATIONS / "023_dedup_no_seqscan.sql")
    for fn in SIGS:
        assert _has_no_seqscan(text, fn), f"023 里没有给 {fn} 加 enable_seqscan = off"
    # 只加开关: 不重建函数、不动授权、不碰数据
    assert not re.search(r"\b(CREATE|DROP|GRANT|REVOKE|INSERT|UPDATE|DELETE)\b", text, re.I)


def test_baseline_carries_the_same_switch():
    text = _code(MIGRATIONS / "000_baseline.sql")
    for fn in SIGS:
        assert _has_no_seqscan(text, fn), f"000_baseline.sql 里没有给 {fn} 加 enable_seqscan = off"


def test_the_switch_comes_after_both_functions_exist_in_the_baseline():
    text = _code(MIGRATIONS / "000_baseline.sql")
    for fn in SIGS:
        create = re.search(r"^CREATE (?:OR REPLACE )?FUNCTION autowriter\." + fn + r"\s*\(", text, re.M)
        alter = re.search(r"ALTER\s+FUNCTION\s+autowriter\." + fn + r"\s*\(", text, re.I)
        assert create and alter and alter.start() > create.start(), f"{fn}: 开关要写在建函数之后"


def test_doctor_reports_023_as_unprobeable_with_the_sql():
    assert core.MIGRATION_DEDUP_NO_SEQSCAN == "023_dedup_no_seqscan.sql"
    sql = core.MIGRATION_023_PROBE_SQL
    assert "enable_seqscan=off" in sql and "proconfig" in sql
    assert "deskcore_check_drafts" in sql and "deskcore_commit_fingerprints" in sql
    assert re.match(r"^\s*select\b", sql, re.I), "给负责人的核对 SQL 只能是 SELECT"
