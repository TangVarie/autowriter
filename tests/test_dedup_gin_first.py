"""019: 查重取候选先走四字串的 GIN 索引、再筛项目 —— 钉写法, 不钉计划。

`deskcore_check_drafts` / `deskcore_commit_fingerprints` 取候选的那一步原来是
``WHERE f.project_id = _project_id AND f.ngram_hashes && ng``: 计划器按项目索引取出整个项目的行、再逐行比
``&&``, GIN 索引轮不上(生产库 11-11: 千行的项目一篇 0.4 秒, 10 篇一组 3.7 秒, 一组稿子付三次)。019 换成
``cand AS MATERIALIZED (... WHERE f.ngram_hashes && (SELECT ng))``, 项目条件挪到读 ``cand`` 的那一层。

**两个记号缺一不可, 而缺了哪个都不报错、结论也不变, 只是悄悄慢回去**:

  · 去掉 ``MATERIALIZED``: 项目条件被推回 CTE 里, 回到按项目索引 + 逐行比;
  · 把 ``(SELECT ng)`` 写回 ``ng``: 数组的值对计划器可见, plpgsql 每条连接头几次执行按它估行数、选全表顺序
    扫描(生产库一篇 3 秒以上)。拿 ``OFFSET 0`` 当栅栏同样走到顺序扫描(生产库 4.8 秒)。

为什么是文本用例而不是在 CI 的 PostgreSQL 上断言计划: 夹具只有几行, 计划器对小表本来就选顺序扫描, 写法对不对
都一样 —— 那条断言只会量出夹具的形状。计划在生产库上用迁移文件末尾那句 EXPLAIN 核; 结果相等由
``tests/sql_parity_check.py`` 逐例对 Python。这里守的是「以后谁改这两条函数时把记号弄丢」。

另外钉三件事: 019 相对 008 / 005 **只**换了这一步(其余代码逐字相同); 签名到 ``AS $$`` 逐字节相同
(``CREATE OR REPLACE`` 换不了返回类型, 参数表变了则会悄悄多出一个重载); 文件不碰数据。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
M019 = "019_dedup_gin_first.sql"

# 函数名 → 019 之前最后定义它的增量(= 生产库在跑 019 之前的那一版)
PREVIOUS = {
    "deskcore_check_drafts": "008_embedding_model_isolation.sql",
    "deskcore_commit_fingerprints": "005_deskcore_containment.sql",
}

MATERIALIZED = "cand AS MATERIALIZED ("
HIDDEN_ARRAY = "f.ngram_hashes && (SELECT ng)"


def _block(fname: str, fn: str) -> str:
    """``CREATE ... FUNCTION <fn>(`` 到配对 ``$$;`` 的整块(同 test_baseline_parity._extract, 不去 schema 前缀)。"""
    text = (MIGRATIONS / fname).read_text(encoding="utf-8")
    m = re.search(r"^CREATE (?:OR REPLACE )?FUNCTION (?:autowriter\.)?" + re.escape(fn) + r"\s*\(", text, re.M)
    assert m, f"{fname} 里找不到 {fn}"
    end = text.find("\n$$;", m.start())
    assert end != -1, f"{fname} 里 {fn} 的函数体没有配对的 $$;"
    return text[m.start():end + len("\n$$;")]


def _code(block: str) -> str:
    """剥掉 ``--`` 注释、空白压成一个空格。注释里也写着那两个记号(解释为什么不能去掉), 不剥会假绿。"""
    return re.sub(r"\s+", " ", re.sub(r"--[^\n]*", "", block)).strip()


def _header(block: str) -> str:
    """``CREATE`` 到 ``AS $$``: 参数表、返回列、LANGUAGE、STABLE、search_path。"""
    return block[:block.index("AS $$")]


@pytest.mark.parametrize("fname", [M019, "000_baseline.sql"])
@pytest.mark.parametrize("fn", sorted(PREVIOUS))
def test_candidate_step_keeps_both_tokens(fname, fn):
    code = _code(_block(fname, fn))
    assert code.count(MATERIALIZED) == 1, f"{fname} · {fn}: 取候选的 CTE 不是 MATERIALIZED 了 —— 项目条件会被推回去"
    assert code.count(HIDDEN_ARRAY) == 1, f"{fname} · {fn}: && 右边不是 (SELECT ng) 了 —— 计划器看得见数组的值"
    # 两个记号在同一个 CTE 里: MATERIALIZED 的括号里就是那句 &&, 中间没有项目条件
    cte = code[code.index(MATERIALIZED):]
    cte = cte[:cte.index(HIDDEN_ARRAY) + len(HIDDEN_ARRAY)]
    assert "_project_id" not in cte, f"{fname} · {fn}: 项目条件写回 CTE 里了"
    assert re.fullmatch(
        re.escape(MATERIALIZED) + r" SELECT f\.title, f\.ngram_hashes AS hs, f\.project_id AS pid "
        r"FROM autowriter\.draft_fingerprints f WHERE " + re.escape(HIDDEN_ARRAY), cte), cte
    # 项目条件在读 cand 的那一层, 和 array_length 那一条一起
    assert code.count("WHERE c.pid = _project_id AND array_length(c.hs, 1) IS NOT NULL") == 1


@pytest.mark.parametrize("fname", [M019, "000_baseline.sql"])
@pytest.mark.parametrize("fn", sorted(PREVIOUS))
def test_forbidden_forms_are_gone(fname, fn):
    code = _code(_block(fname, fn))
    assert "OFFSET 0" not in code, f"{fname} · {fn}: OFFSET 0 的栅栏走顺序扫描(生产库实测 4.8 秒), 不能用"
    assert not re.search(r"ngram_hashes && ng\b", code), f"{fname} · {fn}: 还有一处 && 右边直接写 ng"
    assert not re.search(r"project_id = _project_id AND f\.ngram_hashes &&", code), \
        f"{fname} · {fn}: 项目条件和 && 又写在同一层了"


@pytest.mark.parametrize("fn", sorted(PREVIOUS))
def test_signature_is_byte_identical_to_the_version_in_production(fn):
    """``CREATE OR REPLACE`` 在返回类型变了时报 cannot change return type, 在参数表变了时不报错、多一个重载
    (调用当场 is not unique)。019 承诺「只换函数体」, 所以 ``CREATE`` 到 ``AS $$`` 一个字节都不许差。"""
    assert _header(_block(M019, fn)) == _header(_block(PREVIOUS[fn], fn))


# 019 相对上一版的全部改动, 按「剥注释、压空白」之后的代码写。把它反着换回去必须得到上一版。
_OLD_CAND = ("SELECT f.title, f.ngram_hashes AS hs FROM autowriter.draft_fingerprints f "
             "WHERE f.project_id = _project_id AND f.ngram_hashes && ng "
             "AND array_length(f.ngram_hashes, 1) IS NOT NULL")
_NEW_CAND = ("SELECT f.title, f.ngram_hashes AS hs, f.project_id AS pid FROM autowriter.draft_fingerprints f "
             "WHERE " + HIDDEN_ARRAY)
_NEW_FILTER = " WHERE c.pid = _project_id AND array_length(c.hs, 1) IS NOT NULL"
_REVERT = {
    "deskcore_check_drafts": [
        (f"WITH {MATERIALIZED} {_NEW_CAND} ),", f"WITH cand AS ( {_OLD_CAND} ),"),
        (_NEW_FILTER, ""),
    ],
    "deskcore_commit_fingerprints": [
        (f"FOR hit IN WITH {MATERIALIZED} {_NEW_CAND} ) SELECT c.title,", "FOR hit IN SELECT c.title,"),
        ("FROM cand c CROSS JOIN LATERAL", f"FROM ( {_OLD_CAND} ) c CROSS JOIN LATERAL"),
        (_NEW_FILTER, ""),
    ],
}


@pytest.mark.parametrize("fn", sorted(PREVIOUS))
def test_only_the_candidate_step_changed(fn):
    """判定公式、阈值、样本量闸、返回的字面量(``inserted`` / ``rejected``)、按模型过滤 —— 都不许跟着动。
    做法: 把 019 的代码按上表换回去, 必须与上一版的代码逐字相同。"""
    code = _code(_block(M019, fn))
    for new, old in _REVERT[fn]:
        assert code.count(new) == 1, f"{fn}: 019 里这一段应当正好出现一次: {new!r}"
        code = code.replace(new, old)
    assert code == _code(_block(PREVIOUS[fn], fn)), f"{fn}: 019 除了取候选的那一步, 还改了别的"


def test_019_only_replaces_two_function_bodies_and_touches_no_data():
    """owner 在 SQL Editor 里跑它的前提是「只换两条函数, 不碰数据」。函数体之外(剥掉注释和 ``$$..$$``)只许有:
    事务、SET LOCAL search_path、两条 CREATE OR REPLACE FUNCTION、对这两条函数的 REVOKE / GRANT。"""
    text = (MIGRATIONS / M019).read_text(encoding="utf-8")
    assert len(re.findall(r"^CREATE OR REPLACE FUNCTION ", text, re.M)) == 2
    outside = re.sub(r"\$\$.*?\$\$", "$$ $$", text, flags=re.S)
    outside = re.sub(r"--[^\n]*", "", outside)
    statements = [re.sub(r"\s+", " ", s).strip() for s in outside.split(";")]
    statements = [s for s in statements if s]
    allowed = re.compile(
        r"BEGIN|COMMIT|SET LOCAL search_path TO autowriter, extensions, public"
        r"|CREATE OR REPLACE FUNCTION autowriter\.(deskcore_check_drafts|deskcore_commit_fingerprints)\(.*\$\$ \$\$"
        r"|(REVOKE ALL|GRANT EXECUTE) ON FUNCTION autowriter\.(deskcore_check_drafts|deskcore_commit_fingerprints)"
        r"\([A-Z, ]+\) (FROM (PUBLIC|anon|authenticated)|TO service_role)")
    bad = [s for s in statements if not allowed.fullmatch(s)]
    assert not bad, f"019 里有不该出现的语句: {bad}"
    assert statements[0] == "BEGIN" and statements[-1] == "COMMIT"
    # 函数体里唯一的写还是 commit 那条原有的 INSERT(入库写指纹本身), 没有多出来的
    for fn in sorted(PREVIOUS):
        new, old = _code(_block(M019, fn)), _code(_block(PREVIOUS[fn], fn))
        for kw in ("INSERT INTO", "UPDATE ", "DELETE FROM", "TRUNCATE", "ALTER ", "DROP ", "CREATE "):
            assert new.count(kw) == old.count(kw), f"{fn}: 019 里 {kw!r} 的个数变了"


def test_019_carries_the_check_and_the_rollback_for_whoever_runs_it():
    """文件自己要说清跑完怎么核、怎么退 —— 这是一条从调用侧看不出跑没跑的迁移。"""
    text = (MIGRATIONS / M019).read_text(encoding="utf-8")
    tail = text[text.rindex("COMMIT;"):]
    assert "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF)" in tail and "autowriter.deskcore_check_drafts(p.project_id, d.j, 15)" in tail
    assert "Bitmap Index Scan on draft_fp_ngram_gin_idx" in tail
    assert "回滚" in tail and "008_embedding_model_isolation.sql" in tail and "005_deskcore_containment.sql" in tail
    # 校验和回滚都在注释里: COMMIT 之后没有一条会被执行的语句
    assert all(not ln.strip() or ln.lstrip().startswith("--") for ln in tail.splitlines()[1:])
