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


def _creates(text: str, fn: str) -> list[re.Match]:
    return list(re.finditer(r"^CREATE (?:OR REPLACE )?FUNCTION (?:autowriter\.)?" + fn + r"\s*\(", text, re.M | re.I))


def test_the_switch_comes_after_the_last_create_in_the_baseline():
    """CREATE OR REPLACE 会把 proconfig 换掉: 开关必须在【最后一次】建这个函数之后。"""
    text = _code(MIGRATIONS / "000_baseline.sql")
    for fn in SIGS:
        creates = _creates(text, fn)
        alters = list(re.finditer(r"ALTER\s+FUNCTION\s+autowriter\." + fn + r"\s*\(", text, re.I))
        assert creates and alters, fn
        assert alters[-1].start() > creates[-1].start(), f"{fn}: 开关要写在基线里最后一次建这个函数之后"


def test_later_migrations_that_recreate_the_functions_keep_the_switch():
    """023 之后谁再 CREATE OR REPLACE 这两个函数(照基线 / 019 的维护方式整块复制), 同一个文件里
    就得再给它加 enable_seqscan = off —— 写在函数头的 SET 里, 或者建完之后 ALTER。不然开关
    悄悄没了, 查重又会超时(2026-11-23 的 57014)。真库上的终态由 tests/sql_parity_check.py 再验一次。"""
    for f in sorted(MIGRATIONS.glob("0*.sql")):
        num = f.name.split("_", 1)[0]
        if not num.isdigit() or int(num) < 23:
            continue
        text = _code(f)
        for fn in SIGS:
            creates = _creates(text, fn)
            if not creates:
                continue
            in_header = re.search(r"SET\s+enable_seqscan\s*(?:=|TO)\s*(?:off|false)", text, re.I)
            after = [m for m in re.finditer(r"ALTER\s+FUNCTION\s+autowriter\." + fn + r"\s*\([^)]*\)\s*SET\s+"
                                            r"enable_seqscan\s*=\s*(?:off|false)", text, re.I)
                     if m.start() > creates[-1].start()]
            assert in_header or after, f"{f.name} 重建了 {fn}, 却没再加 enable_seqscan = off(见 migrations/023)"


def test_doctor_reports_023_as_unprobeable_with_the_sql():
    assert core.MIGRATION_DEDUP_NO_SEQSCAN == "023_dedup_no_seqscan.sql"
    sql = core.MIGRATION_023_PROBE_SQL
    assert "enable_seqscan=off" in sql and "enable_seqscan=false" in sql and "proconfig" in sql
    assert "deskcore_check_drafts" in sql and "deskcore_commit_fingerprints" in sql
    assert re.match(r"^\s*select\b", sql, re.I), "给负责人的核对 SQL 只能是 SELECT"
