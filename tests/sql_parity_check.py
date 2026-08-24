"""在**真的 PostgreSQL** 上跑一遍迁移, 并逐例比对 SQL 与 Python 的结果。

⚠️ 这个文件**不是 pytest 用例**(所以没叫 test_*.py, pytest 不会收集它)。
它需要一个活的 PostgreSQL, 由 CI 里那一步单独起。本地跑法见文件末尾。

── 为什么值得单独有这么一个东西 ──────────────────────────────────────

写 COR-014 的迁移时, 光靠"读代码 + py_compile + 肉眼 review"漏掉了两个 bug,
两个都是真跑一次就当场炸的:

  1. ``WITH`` 的作用域只有**一条语句**。把 Jaccard 和包含度拆成两条 SELECT
     去读同一个 CTE, 第二条报 ``relation "metrics" does not exist``;
  2. 两个 CTE 之间漏了逗号。

这两个都不是"想清楚就不会犯"的错 —— plpgsql 的函数体对 Python 来说只是一个
字符串, 没有任何静态检查够得着它。而这些 SQL 是**查重硬闸**的实现。

比语法更重要的是第三件事: **两条路径算出来的数必须一样**。下推路径(RPC)和
Python 兜底路径给同一对稿子相反的结论, 是最坏的失败形态 —— 两边各自看都
"正常", 只有对比才看得见。

pgvector 在裸 PostgreSQL 上没有, 所以用一个最小 shim 让函数能建、能跑。
标题余弦那一路本来就不在本文件的比对范围内。
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from deskcore import fingerprint as fp     # noqa: E402

PGHOST = sys.argv[1] if len(sys.argv) > 1 else "/tmp/awpg"
PGPORT = sys.argv[2] if len(sys.argv) > 2 else "55432"
PSQL = ["psql", "-h", PGHOST, "-p", PGPORT, "-U", "postgres", "-v", "ON_ERROR_STOP=1"]

PID = "11111111-1111-1111-1111-111111111111"
UID = "22222222-2222-2222-2222-222222222222"

POOL = ("的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会"
        "可主发年动同工也能下过子说产种面而方后多定行学法所民得经十三之进着等部"
        "度家电力里如水化高自二理起小物现实加量都两体制机当使点从业本去把性好应")


def sql(text: str) -> str:
    r = subprocess.run(PSQL + ["-tAc", text], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"SQL 失败:\n{r.stderr[-3000:]}")
    return r.stdout.strip()


def sql_file(path: Path) -> None:
    r = subprocess.run(PSQL + ["-f", str(path)], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"{path.name} 执行失败:\n{r.stderr[-4000:]}")


def doc(rng: random.Random, n: int) -> str:
    return "".join(f"这一段说的是{''.join(rng.choice(POOL) for _ in range(18))}。"
                   for _ in range(n))


def prefix(text: str, n: int) -> str:
    return "。".join(text.split("。")[:n]) + "。"


def pg_array(hs) -> str:
    return "{" + ",".join(sorted(hs)) + "}"


def payload(hs) -> str:
    return json.dumps([{"opening_hash": "zz", "ngram_hashes": sorted(hs),
                        "title_embedding": None}], ensure_ascii=False)


def main() -> int:
    # ── 最小骨架 ────────────────────────────────────────────────────────
    sql("""
    DO $r$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon')
        THEN CREATE ROLE anon; END IF;
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated')
        THEN CREATE ROLE authenticated; END IF;
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role')
        THEN CREATE ROLE service_role; END IF;
    END $r$;
    DROP SCHEMA IF EXISTS autowriter CASCADE;
    DROP SCHEMA IF EXISTS extensions CASCADE;
    CREATE SCHEMA autowriter;
    CREATE SCHEMA extensions;
    -- pgvector 的最小 shim: 只要能建函数、能跑通。余弦不在本文件的比对范围内。
    CREATE DOMAIN extensions.vector AS text;
    CREATE FUNCTION extensions."<=>"(extensions.vector, extensions.vector)
      RETURNS float8 LANGUAGE sql IMMUTABLE AS $x$ SELECT 1.0::float8 $x$;
    CREATE OPERATOR extensions.<=> (
      LEFTARG = extensions.vector, RIGHTARG = extensions.vector,
      FUNCTION = extensions."<=>");
    CREATE TABLE autowriter.draft_fingerprints (
      id bigserial primary key, project_id uuid not null, version_id uuid,
      user_id uuid, title text not null default '', opening text not null default '',
      title_embedding extensions.vector, embedding_model text,
      opening_hash text, ngram_hashes text[], angle_key text,
      created_at timestamptz default now());
    CREATE INDEX ON autowriter.draft_fingerprints USING gin (ngram_hashes);
    """)
    print("  ✓ 骨架就位")

    sql_file(REPO / "migrations" / "005_deskcore_containment.sql")
    print("  ✓ migrations/005 执行通过(语法 + 类型 —— 这一关就抓到过两个 bug)")

    rng = random.Random(4242)
    long_a, long_b, same, mixed = doc(rng, 120), doc(rng, 300), doc(rng, 60), doc(rng, 60)
    cases = [
        ("短稿⊂长稿(12/120)", long_a, prefix(long_a, 12)),
        ("短稿⊂长稿(30/300)", long_b, prefix(long_b, 30)),
        ("完全相同(60/60)", same, same),
        ("完全无关(60/120)", doc(rng, 120), doc(rng, 60)),
        ("一半重合(60/60)", mixed, prefix(mixed, 30) + doc(rng, 30)),
        # 重名历史稿: 专验 LATERAL 没有按 title join 扇出成笛卡尔积
        ("重名历史稿", long_a, prefix(long_a, 12)),
    ]

    bad = 0
    for n, (label, hist_body, draft_body) in enumerate(cases):
        sql(f"DELETE FROM autowriter.draft_fingerprints WHERE project_id='{PID}';")
        hist_hs = fp.ngram_hashes(hist_body)
        title = "重名的稿子" if label == "重名历史稿" else f"历史{n}"
        sql("INSERT INTO autowriter.draft_fingerprints"
            "(project_id,title,opening_hash,ngram_hashes) VALUES"
            f"('{PID}','{title}','oh{n}','{pg_array(hist_hs)}');")
        if label == "重名历史稿":
            sql("INSERT INTO autowriter.draft_fingerprints"
                "(project_id,title,opening_hash,ngram_hashes) VALUES"
                f"('{PID}','{title}','ohx{n}','{pg_array(fp.ngram_hashes(doc(rng, 40)))}');")

        d_hs = fp.ngram_hashes(draft_body)
        out = sql("SELECT best_j, best_c, c_sample FROM "
                  f"autowriter.deskcore_check_drafts('{PID}'::uuid, "
                  f"'{payload(d_hs)}'::jsonb);").split("|")
        s_j, s_c, s_m = float(out[0]), float(out[1]), int(out[2])
        p_j, p_c, p_m = fp.sketch_overlap(set(d_hs), set(hist_hs))

        ok = abs(s_j - p_j) < 1e-6 and abs(s_c - p_c) < 1e-6 and s_m == p_m
        if not ok:
            bad += 1
        print(f"  [{'ok  ' if ok else 'FAIL'}] {label:<20} "
              f"SQL(J={s_j:.4f} C={s_c:.4f} m={s_m:<4}) "
              f"Py(J={p_j:.4f} C={p_c:.4f} m={p_m})")

    # ── 写入侧的原子重查也要拦住, 且被拒的不许入库 ──────────────────────
    sql(f"DELETE FROM autowriter.draft_fingerprints WHERE project_id='{PID}';")
    long_c = doc(rng, 120)
    short_c = prefix(long_c, 12)
    sql("INSERT INTO autowriter.draft_fingerprints"
        "(project_id,title,opening_hash,ngram_hashes) VALUES"
        f"('{PID}','被照搬的长稿','ohc','{pg_array(fp.ngram_hashes(long_c))}');")
    rows = json.dumps([{"title": "短的那篇", "opening": "x", "opening_hash": "ohs",
                        "ngram_hashes": sorted(fp.ngram_hashes(short_c)),
                        "title_embedding": None, "version_id": "", "angle_key": ""}],
                      ensure_ascii=False)
    res = sql("SELECT status, coalesce(collided_with,'-') FROM "
              f"autowriter.deskcore_commit_fingerprints('{PID}'::uuid, '{rows}'::jsonb, "
              f"'{UID}'::uuid, {fp.NGRAM_JACCARD_HARD}, "
              f"{fp.NGRAM_CONTAIN_HARD}, {fp.CONTAIN_MIN_SAMPLE});")
    if not res.startswith("rejected|被照搬的长稿"):
        print(f"  [FAIL] commit 侧没拦住短稿照搬长稿: {res}")
        bad += 1
    else:
        print("  [ok  ] commit 侧也拦住了, 且归因到被照搬的那篇")

    n_rows = sql(f"SELECT count(*) FROM autowriter.draft_fingerprints "
                 f"WHERE project_id='{PID}';")
    if n_rows != "1":
        print(f"  [FAIL] 被拒的稿子写进库了(现在 {n_rows} 行)")
        bad += 1
    else:
        print("  [ok  ] 被拒的没入库")

    print(f"\nSQL/Python 一致性: {'全部通过' if not bad else f'{bad} 例不一致'}")
    return 1 if bad else 0


if __name__ == "__main__":
    # 本地跑法(需要 postgresql-16 客户端与服务端):
    #   B=/tmp/awpg && rm -rf $B && mkdir -p $B
    #   initdb -D $B/data -A trust -U postgres
    #   pg_ctl -D $B/data -o "-k $B -p 55432 -c listen_addresses=" -l $B/pg.log start
    #   python tests/sql_parity_check.py $B 55432
    sys.exit(main())
