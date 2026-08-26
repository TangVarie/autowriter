"""在**真的 PostgreSQL** 上把整套 schema 跑一遍, 并逐例比对 SQL 与 Python。

⚠️ 这个文件**不是 pytest 用例**(所以没叫 test_*.py, pytest 不会收集它)。
它需要一个活的 PostgreSQL, 由 CI 里那一步单独起。本地跑法见文件末尾。

── 它验的三件事 ──────────────────────────────────────────────────────────

**① 基线 DDL 真的跑得起来**(审计 SUP-010)。``migrations/000_baseline.sql``
原来是 db.py 里一个 1162 行的 Python 字符串, 从来没有任何代码执行过它 ——
所以也从来没有任何东西验证过它。代价是真实发生过的: 修 COR-014 时发现它里面
那份 ``deskcore_commit_fingerprints`` 还带着 ``migrations/001`` 早就修好的 bug,
于是每个**新环境**都会带着一个老 bug 出生, 而没人会发现。

**② 基线 + 增量迁移能叠在一起**。空库 → 000 → 001..005 全部执行成功。
这才是"消双写"真正的意思: 不是只留一份, 而是**让两份必须对得上, 对不上就报错**。

**③ 下推 SQL 与 Python 算出来的数一样**(审计 COR-014)。两条路径不一致是最坏的
失败形态 —— 各自看都正常, 只有对比才看得见。

写 COR-014 那个迁移时, "读代码 + 肉眼 review" 漏掉了两个 bug, 两个都是真跑一次
就当场炸的: ``WITH`` 的作用域只有一条语句; 两个 CTE 之间漏了逗号。plpgsql 的
函数体对 Python 来说只是个字符串, 没有任何静态检查够得着它。

── Supabase shim ─────────────────────────────────────────────────────────
裸 PostgreSQL 上没有 ``auth.uid()``、没有 ``anon``/``authenticated``/
``service_role`` 三个角色、也没有 pgvector。这里给最小替身让 DDL 跑得动。
**被 shim 掉的东西不算验过** —— 向量那一路本来就不在本文件的比对范围内。
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from deskcore import fingerprint as fp     # noqa: E402
from deskcore import core                 # noqa: E402
import db as _awdb                        # noqa: E402

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


# 每次 psql 调用都是一个新 session, 所以 search_path 要**逐文件**带上。
# Supabase 的 SQL Editor 默认就有 extensions 在 search_path 里, DDL 里那些
# 不带前缀的 uuid_generate_v4() / 裸表名靠的正是它。这里对齐同一条件。
_SEARCH_PATH = "SET search_path = autowriter, extensions, public;\n"


def run_sql_text(text: str, label: str) -> None:
    r = subprocess.run(PSQL, input=_SEARCH_PATH + text,
                       capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"{label} 执行失败:\n{r.stderr[-4000:]}")


def _shim(text: str) -> str:
    """把 Supabase / pgvector 专有的东西换成裸 PG 上跑得动的最小替身。

    ⚠️ 每一处替换都意味着**那一部分没有被真的验证**。所以只换到"能跑"为止,
    不多换一行 —— 换得越多, 这个 harness 报的绿就越不值钱。
    """
    # pgvector: 装不了, 用 text 域顶替。带维度的 vector(768) 也一起换掉。
    text = re.sub(r"CREATE EXTENSION IF NOT EXISTS vector[^;]*;", "", text)
    text = re.sub(r"\bvector\(\d+\)", "extensions.vector", text)
    # ivfflat 索引需要真的 pgvector, 整条去掉(余弦那一路不在比对范围内)
    text = re.sub(r"CREATE INDEX[^;]*USING ivfflat[^;]*;", "", text)
    # migrations/001 里有个 DO $guard$ 块, 检查 vector 扩展确实装在 extensions
    # schema。**它是对的、而且很重要**(装错地方会让定稿入库在运行时才挂), 但这里
    # 根本没装真的 pgvector, 所以它必然触发。整块拿掉。
    # ⚠️ 也就是说"vector 装在哪"这件事**本 harness 验不了** —— 它只能靠真库上
    #    跑迁移时那个 guard 自己把关。
    text = re.sub(r"DO \$guard\$.*?\$guard\$;", "", text, flags=re.S)
    return text


SHIMS = """
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
DROP SCHEMA IF EXISTS auth CASCADE;
CREATE SCHEMA autowriter;
CREATE SCHEMA extensions;
CREATE SCHEMA auth;

CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA extensions;

-- Supabase 的 auth.uid(): RLS policy 里到处在用。这里回 NULL 就够 ——
-- 本 harness 全程用 superuser 跑, policy 不会被求值。
CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE
  AS $x$ SELECT NULL::uuid $x$;

-- pgvector 的最小替身。**向量那一路因此不算验过**。
CREATE DOMAIN extensions.vector AS text;
CREATE FUNCTION extensions."<=>"(extensions.vector, extensions.vector)
  RETURNS float8 LANGUAGE sql IMMUTABLE AS $x$ SELECT 1.0::float8 $x$;
CREATE OPERATOR extensions.<=> (
  LEFTARG = extensions.vector, RIGHTARG = extensions.vector,
  FUNCTION = extensions."<=>");

SET search_path = autowriter, extensions, public;
"""


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
    bad = 0

    # ── ① 基线 + ② 增量迁移 ────────────────────────────────────────────
    run_sql_text(SHIMS, "Supabase shim")
    print("  ✓ Supabase shim 就位(roles / auth.uid / uuid-ossp / vector 替身)")

    files = sorted((REPO / "migrations").glob("*.sql"))
    assert files, "migrations/ 下一个 .sql 都没有 —— 路径写错了?"
    for f in files:
        run_sql_text(_shim(f.read_text(encoding="utf-8")), f.name)
        print(f"  ✓ {f.name}")
    print(f"  ✓ 基线 + {len(files) - 1} 个增量迁移在空库上叠起来了(审计 SUP-010)")

    # 抽查几张表 / 几个函数真的建出来了 —— 免得 shim 把整段吃掉还报绿
    for tbl in ("projects", "items", "versions", "memories", "jobs",
                "draft_fingerprints", "angle_ledger"):
        n = sql("SELECT count(*) FROM information_schema.tables "
                f"WHERE table_schema='autowriter' AND table_name='{tbl}';")
        if n != "1":
            print(f"  [FAIL] 表 {tbl} 没建出来")
            bad += 1
    for fn in ("deskcore_check_drafts", "deskcore_commit_fingerprints",
               "deskcore_reserve_angles", "claim_one_job",
               "update_calibration_notes_cas", "deskcore_fingerprint_counts"):
        n = sql("SELECT count(*) FROM pg_proc p JOIN pg_namespace ns "
                "ON ns.oid = p.pronamespace "
                f"WHERE ns.nspname='autowriter' AND p.proname='{fn}';")
        if n == "0":
            print(f"  [FAIL] 函数 {fn} 没建出来")
            bad += 1
    # 决策出处那三列(审计 COR-004 / COR-007)。**基线和增量都要有** ——
    # migrations/README 的规矩是加列两边都改, 而这个 harness 跑的正是
    # 000 → 001..N, 只改一边的话这里就该红。
    for col in ("decision_source", "reviewer_id", "decided_at"):
        n = sql("SELECT count(*) FROM information_schema.columns "
                f"WHERE table_schema='autowriter' AND table_name='items' "
                f"AND column_name='{col}';")
        if n != "1":
            print(f"  [FAIL] items.{col} 没建出来 —— 000 与 006 只改了一边?")
            bad += 1
    # CHECK 的取值集合必须和 db.DecisionSource 对得上。两边各写一份迟早漂,
    # 而漂了的表现是"写进去被数据库拒", 出现在离现场很远的地方。
    for src in sorted(_awdb._DECISION_SOURCES):
        try:
            sql(f"INSERT INTO autowriter.items (id, user_id, status, decision_source) "
                f"VALUES (gen_random_uuid(), '{UID}', 'pending', '{src}');")
        except SystemExit:
            print(f"  [FAIL] db.DecisionSource 有 {src!r}, 但 006 的 CHECK 不认")
            bad += 1
    sql(f"DELETE FROM autowriter.items WHERE user_id='{UID}';")
    if not bad:
        print("  ✓ 抽查的 7 张表 + 6 个函数 + 决策出处三列都在, "
              "且 CHECK 与 db.DecisionSource 一致")

    # ── ②' 每张表都要授权给 service_role ───────────────────────────────
    # 2026-08-26 首次真部署踩的坑, 值得完整记一遍。
    #
    # ``migrations/001_deskcore.sql`` 建了四张表, 只给两个**函数**发了 EXECUTE,
    # 表本身一行 GRANT 都没有。上线当天 deskcore 除 list_projects 外每个工具都挂:
    #     permission denied for table draft_fingerprints   (42501)
    #
    # 为什么没人发现: 直觉里 ``service_role`` 是"超级权限"。它确实**绕过 RLS**,
    # 但**不绕过表级 GRANT** —— 两套独立机制。它在 public schema 下看着无所不能,
    # 靠的是 Supabase 给 public 配的 default privileges; ``autowriter`` 是本仓
    # 自建 schema, 没有这份默认授权, 新表出生就是零权限。
    #
    # 上一条前置检查("表建出来了吗")是绿的 —— 表确实建出来了。**建出来 ≠ 能访问**,
    # 而这中间的缝隙是靠人肉 curl 打线上才发现的。不该是这样, 所以钉在这里。
    #
    # 断言的是【不变量】而不是名单: 问"autowriter 下还有谁漏了", 不问"我列的这
    # 几张对不对"。以后加表忘了发 GRANT, 这条自己会红, 不需要谁想起来更新名单。
    needed = ("SELECT", "INSERT", "UPDATE", "DELETE")
    rows = sql(
        "SELECT c.relname || '|' || "
        + " || ',' || ".join(
            f"(CASE WHEN has_table_privilege('service_role', c.oid, '{p}')"
            f" THEN '' ELSE '{p}' END)" for p in needed)
        + " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
          "WHERE n.nspname='autowriter' AND c.relkind='r' "
          "AND NOT (" + " AND ".join(
              f"has_table_privilege('service_role', c.oid, '{p}')"
              for p in needed) + ") ORDER BY c.relname;")
    ungranted = [ln for ln in rows.splitlines() if ln.strip()]
    if ungranted:
        for ln in ungranted:
            tbl, missing = ln.split("|", 1)
            print(f"  [FAIL] autowriter.{tbl} 没授权给 service_role"
                  f"(缺 {','.join(p for p in missing.split(',') if p)}) —— "
                  "建了表没发 GRANT, 线上表现是 42501 permission denied")
        bad += len(ungranted)
    else:
        n_tbl = sql("SELECT count(*) FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid=c.relnamespace WHERE n.nspname='autowriter' "
                    "AND c.relkind='r';")
        print(f"  ✓ autowriter 下全部 {n_tbl} 张表都对 service_role 有 "
              "SELECT/INSERT/UPDATE/DELETE(建表 ≠ 能访问, 2026-08-26 的教训)")

    # ── ③ SQL 与 Python 算出来的数一样 ─────────────────────────────────
    # 跑完整套 schema 之后 draft_fingerprints 上是有 FK 的, 先把 project 建出来。
    # (这本身也是个信号: 之前那个手搭的最小骨架没有 FK, 也就测不到这一层。)
    sql(f"INSERT INTO autowriter.projects (id, name, owner_id) "
        f"VALUES ('{PID}', 'parity-check', '{UID}') ON CONFLICT (id) DO NOTHING;")
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

    # ── 成功那一支的 status 字面量, 必须正是 core.py 数的那个 ────────────
    # ⚠️ 这一条是补上来的, 而它补的正是本 harness 自己的洞: 上面只断言了
    #    **rejected** 分支, 于是 005 把成功分支从 'inserted' 写成 'written'
    #    时整套测试照绿。core.py 按 'inserted' 计数并销角度台账, 结果是
    #    「每次成功 commit 都报 written=0, 一条角度都不销账」—— 静默的。
    #
    #    测有意思的分支、放过无聊的分支, 而契约恰恰长在无聊的那条上。
    sql(f"DELETE FROM autowriter.draft_fingerprints WHERE project_id='{PID}';")
    clean = json.dumps([{"title": "干净的稿子", "opening": "y", "opening_hash": "ohk",
                         "ngram_hashes": sorted(fp.ngram_hashes(doc(rng, 40))),
                         "title_embedding": None, "version_id": "", "angle_key": ""}],
                       ensure_ascii=False)
    st = sql("SELECT status FROM "
             f"autowriter.deskcore_commit_fingerprints('{PID}'::uuid, '{clean}'::jsonb, "
             f"'{UID}'::uuid, {fp.NGRAM_JACCARD_HARD}, "
             f"{fp.NGRAM_CONTAIN_HARD}, {fp.CONTAIN_MIN_SAMPLE});")
    if st != core.COMMIT_STATUS_INSERTED:
        print(f"  [FAIL] 成功分支返回 {st!r}, 而 core.py 只认 "
              f"{core.COMMIT_STATUS_INSERTED!r} —— written 会永远是 0, "
              f"角度台账永远不销账")
        bad += 1
    else:
        print(f"  [ok  ] 成功分支返回 {st!r}, 与 core.py 的计数口径一致")

    # ── 样本量不够的命中不许挤掉真命中 ──────────────────────────────────
    # 场景: 库里同时有「被照搬的长稿」(c 高、样本量够) 和一条毫不相关、只跟
    # 本稿撞上**一个**低位 hash 的稿子。后者的 c 是 1.0 但样本量只有 1。
    # 若先按 c 取最大再看样本量, 冠军是那条无关的, 随后因样本量不够被丢掉,
    # 真正的抄袭源根本没进过决赛 —— 两道闸都会放行。
    sql(f"DELETE FROM autowriter.draft_fingerprints WHERE project_id='{PID}';")
    src = doc(rng, 120)
    copied = prefix(src, 12) + doc(rng, 2)          # 照搬 + 掺一点自己的 → c<1
    d_hs = sorted(fp.ngram_hashes(copied))
    sql("INSERT INTO autowriter.draft_fingerprints"
        "(project_id,title,opening_hash,ngram_hashes) VALUES"
        f"('{PID}','被照搬的长稿','ohL','{pg_array(fp.ngram_hashes(src))}');")
    # 诱饵: 只有一个 hash, 取本稿最小的那个 → 受限子域里两边都只剩它 →
    # c=1.0 而样本量=1。
    sql("INSERT INTO autowriter.draft_fingerprints"
        "(project_id,title,opening_hash,ngram_hashes) VALUES"
        f"('{PID}','无关的诱饵','ohD','{{{d_hs[0]}}}');")

    out = sql("SELECT best_c, c_title, c_sample FROM "
              f"autowriter.deskcore_check_drafts('{PID}'::uuid, "
              f"'{payload(d_hs)}'::jsonb);").split("|")
    if out[1] != "被照搬的长稿" or int(out[2]) < fp.CONTAIN_MIN_SAMPLE:
        print(f"  [FAIL] check 侧被诱饵挤掉了: c={out[0]} 归因={out[1]!r} "
              f"样本量={out[2]}(下限 {fp.CONTAIN_MIN_SAMPLE})")
        bad += 1
    else:
        print(f"  [ok  ] check 侧跳过样本量不足的诱饵, 归因到 {out[1]!r} "
              f"(c={float(out[0]):.3f} 样本量={out[2]})")

    rows = json.dumps([{"title": "照搬的那篇", "opening": "z", "opening_hash": "ohz",
                        "ngram_hashes": d_hs, "title_embedding": None,
                        "version_id": "", "angle_key": ""}], ensure_ascii=False)
    res = sql("SELECT status, coalesce(collided_with,'-') FROM "
              f"autowriter.deskcore_commit_fingerprints('{PID}'::uuid, '{rows}'::jsonb, "
              f"'{UID}'::uuid, {fp.NGRAM_JACCARD_HARD}, "
              f"{fp.NGRAM_CONTAIN_HARD}, {fp.CONTAIN_MIN_SAMPLE});")
    if res != "rejected|被照搬的长稿":
        print(f"  [FAIL] commit 侧也被诱饵挤掉了: {res}")
        bad += 1
    else:
        print("  [ok  ] commit 侧同样跳过诱饵, 拦住并归因到被照搬的那篇")

    # ── 迁移必须幂等: 整套再跑一遍不能报错 ──────────────────────────────
    for f in files:
        run_sql_text(_shim(f.read_text(encoding="utf-8")), f"{f.name}(第二遍)")
    print("  ✓ 整套迁移重复执行是干净 no-op(幂等)")

    print(f"\nschema + SQL/Python 一致性: {'全部通过' if not bad else f'{bad} 项不通过'}")
    return 1 if bad else 0


if __name__ == "__main__":
    # 本地跑法(需要 postgresql-16 客户端与服务端):
    #   B=/tmp/awpg && rm -rf $B && mkdir -p $B
    #   initdb -D $B/data -A trust -U postgres
    #   pg_ctl -D $B/data -o "-k $B -p 55432 -c listen_addresses=" -l $B/pg.log start
    #   python tests/sql_parity_check.py $B 55432
    sys.exit(main())
