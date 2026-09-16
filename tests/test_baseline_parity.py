"""基线 SQL 不许再从增量迁移上漂开。

背景(2026-09-16): `migrations/README.md` 的契约是「加表/加列/改函数必须两边都改」
—— `000_baseline.sql` 是 fresh install 的**源头**, 增量负责把已有库升到同一状态。
但 `deskcore_check_drafts` 上这条契约破过一次: 基线停在 2 参签名, 而 `005` 换成了
3 参(多一个 `_contain_min_sample` 样本量闸)、`008` 又加了按 `embedding_model` 过滤。

**为什么没人发现**: `tests/sql_parity_check.py` 跑的是完整链条(000 → 001..008),
`005` 会把基线那版 DROP 掉重建, 于是终态永远是对的 —— 漂移被自己的验证掩盖了。
只跑 `000` 的新库才会拿到老函数, 而那正是「fresh install 一把建全」承诺的路径。

实测过的后果(只跑 000 的空库):
  · `deskcore_check_drafts(_project_id uuid, _rows jsonb)`   ← 2 参, 没有样本量闸
  · `deskcore_commit_fingerprints(..., _contain_min_sample integer DEFAULT 15)` ← 6 参新版
  两者口径不一致 —— 而 `005` 自己写着「写入侧的重查必须和 deskcore_check_drafts
  同口径」。另外 Python 侧 `store.check_drafts_sql` 调的是 3 参签名, 在这种库上
  会退回 Python 逐对比对。

这份用例是**纯文本比对, 不需要数据库**, 所以它在每次 `pytest` 里都跑, 而不是
只在 CI 那个起真 PostgreSQL 的步骤里跑。DB 侧的对应断言在 `sql_parity_check.py`
(它单独验「只跑 000」那条路径的终态)。

⚠️ 这里比的是**字节级相同**。所以维护方式是: 改 `008` 里那个函数之后, 把整块
原样复制进基线。刻意不做「语义等价」的宽松比对 —— 那种比对写起来就得先解析
SQL, 而它一旦有 bug, 失效方式恰好是「看起来绿、其实漂了」, 正是本用例要治的病。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

# 函数名 → (最终形态由哪个文件定义, 说明)
# 一个函数被多个增量改过时, 写**最后**改它的那个。
FINAL_SOURCE = {
    "deskcore_check_drafts": (
        "008_embedding_model_isolation.sql",
        "005 换 3 参 + 包含度, 008 再加 embedding_model 过滤",
    ),
    "deskcore_commit_fingerprints": (
        "005_deskcore_containment.sql",
        "005 换 6 参 + 包含度重查",
    ),
}


def _extract(path: Path, fn: str) -> str | None:
    """抠出 ``CREATE ... FUNCTION <fn>(`` 到配对 ``$$;`` 的整块。

    schema 前缀(``autowriter.``)在比对前去掉 —— 基线与增量对限定名的习惯不同,
    而那是风格差异, 不是漂移。其余一个字节都不许差。
    """
    text = path.read_text(encoding="utf-8")
    pat = re.compile(
        r"^CREATE (?:OR REPLACE )?FUNCTION (?:autowriter\.)?" + re.escape(fn) + r"\s*\(",
        re.M,
    )
    m = pat.search(text)
    if not m:
        return None
    end = text.find("\n$$;", m.start())
    assert end != -1, f"{path.name} 里 {fn} 的函数体没有配对的 $$;"
    block = text[m.start():end + len("\n$$;")]
    return block.replace("autowriter.", "")


@pytest.mark.parametrize("fn", sorted(FINAL_SOURCE))
def test_baseline_carries_the_final_form_of(fn):
    """基线里的函数体必须与"最后改它的那个增量"逐字节相同。"""
    source_file, why = FINAL_SOURCE[fn]
    baseline = _extract(MIGRATIONS / "000_baseline.sql", fn)
    final = _extract(MIGRATIONS / source_file, fn)

    assert baseline is not None, f"000_baseline.sql 里找不到 {fn}"
    assert final is not None, f"{source_file} 里找不到 {fn}"
    assert baseline == final, (
        f"{fn} 在基线与 {source_file} 之间漂了({why})。\n"
        f"只跑 000 的 fresh install 会拿到基线那版 —— 而完整链条上 {source_file} "
        f"会把它盖掉, 所以 sql_parity_check 发现不了。\n"
        f"修法: 把 {source_file} 里那一整块原样复制进 000_baseline.sql。"
    )


@pytest.mark.parametrize("fn", sorted(FINAL_SOURCE))
def test_baseline_grants_match_the_final_signature(fn):
    """`GRANT EXECUTE` 的签名也要跟着函数签名走, 否则新库上那条 GRANT 打在空处。

    这不是假设: `007_deskcore_table_grants.sql` 整个的存在理由就是
    「建出来 ≠ 能访问」—— 签名写错的 GRANT 和没写是一回事, 而且同样不报错。
    """
    baseline = (MIGRATIONS / "000_baseline.sql").read_text(encoding="utf-8")
    final = (MIGRATIONS / FINAL_SOURCE[fn][0]).read_text(encoding="utf-8")

    def grant_sig(text: str) -> str | None:
        m = re.search(
            r"GRANT EXECUTE ON FUNCTION (?:autowriter\.)?"
            + re.escape(fn) + r"\(([^)]*)\) TO service_role",
            text,
        )
        return re.sub(r"\s+", " ", m.group(1)).strip().upper() if m else None

    b, f = grant_sig(baseline), grant_sig(final)
    assert b is not None, f"000_baseline.sql 里没有给 {fn} 发 GRANT EXECUTE"
    assert f is not None, f"{FINAL_SOURCE[fn][0]} 里没有给 {fn} 发 GRANT EXECUTE"
    assert b == f, (
        f"{fn} 的 GRANT 签名对不上: 基线 ({b}) vs {FINAL_SOURCE[fn][0]} ({f})。\n"
        f"签名写错的 GRANT 打在空处, 和没发一样 —— 而且不报错。"
    )


def test_final_source_really_points_at_the_last_increment():
    """`FINAL_SOURCE` 的值必须是**最后**定义该函数的那个增量。

    没有这条的话, 上面那两条会在一个特定的方式下变成假绿: 今天 `008` 是
    `deskcore_check_drafts` 的最终形态, 登记的也是 `008`; 等以后有人加个 `009`
    再改一次, 基线 == `008` 依然成立, 于是它报绿, 而真正的终态已经是 `009` 了。

    判的是不变量(「登记的 == 目录里最后一个定义它的文件」)而不是名单, 所以
    加 `009` 的人不需要记得回来改这里 —— 忘了的话这条自己会红。
    """
    for fn, (registered, _why) in sorted(FINAL_SOURCE.items()):
        defined_in = [
            f.name for f in sorted(MIGRATIONS.glob("0*.sql"))
            if f.name != "000_baseline.sql"
            and re.search(
                r"^CREATE (?:OR REPLACE )?FUNCTION (?:autowriter\.)?" + re.escape(fn) + r"\s*\(",
                f.read_text(encoding="utf-8"), re.M)
        ]
        assert defined_in, f"没有任何增量定义 {fn} —— FINAL_SOURCE 里这条该删了?"
        assert registered == defined_in[-1], (
            f"{fn} 登记的最终来源是 {registered}, 但目录里最后定义它的是 "
            f"{defined_in[-1]}(全部: {defined_in})。\n"
            f"把 FINAL_SOURCE[{fn!r}] 改成 {defined_in[-1]!r}, 并把那一整块重新"
            f"复制进 000_baseline.sql。"
        )


def test_every_function_the_increments_replace_is_listed_here():
    """增量里 **DROP 掉再重建** 的函数, 必须出现在 FINAL_SOURCE 里。

    守的是"下次有人再改一个函数却忘了同步基线"。判的是不变量而不是名单:
    新增一个被增量替换的函数, 忘了登记, 这条自己会红。
    """
    dropped = set()
    for f in sorted(MIGRATIONS.glob("0*.sql")):
        if f.name == "000_baseline.sql":
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            # ⚠️ 必须跳过注释行。`001` 的回滚说明里就躺着两条注释掉的
            # DROP FUNCTION —— 不跳的话它们会被当成真的替换, 逼着人往
            # FINAL_SOURCE 里登记两个根本没被替换过的函数。
            if line.lstrip().startswith("--"):
                continue
            m = re.search(r"DROP FUNCTION IF EXISTS (?:autowriter\.)?(\w+)\s*\(", line)
            if m:
                dropped.add(m.group(1))

    missing = sorted(dropped - set(FINAL_SOURCE))
    assert not missing, (
        f"这些函数被增量 DROP 掉重建过, 但没登记进 FINAL_SOURCE: {missing}\n"
        f"意味着基线里那版是不是最终形态没人验 —— 补一条登记(值写最后改它的那个文件)。"
    )
