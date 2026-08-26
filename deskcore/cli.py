"""deskcore/cli.py — 本地 CLI, 与 HTTP/MCP 共用同一个 core。

用法:
  python -m deskcore.cli selftest                 ← 不连库不联网, 验查重/发牌/词表
  python -m deskcore.cli health
  python -m deskcore.cli doctor [--project <uuid>]  ← 上线前必跑: 库跑到第几个迁移了
  python -m deskcore.cli projects --user <uuid>
  python -m deskcore.cli open  --project <uuid> --user <uuid> [--tactic ...]
  python -m deskcore.cli draw  --project <uuid> --user <uuid> -n 20 [--block]
  python -m deskcore.cli check --project <uuid> --user <uuid> --file drafts.json
  python -m deskcore.cli backfill --project <uuid>   ← 部署时必跑一次
  python -m deskcore.cli recompute-fingerprints --project <uuid>
                                                    ← 只在改了 normalize 之后跑

⚠️ ``--user`` 从可选变成必填(审计 COR-015): 归属校验在 core 层, CLI 与 MCP 走
同一个函数, 不带身份的调用现在一律被拒。传的是 ``projects.owner_id`` 里【已有的】
那个 UUID —— 与 DESKCORE_KEYS 里配的是同一个, 别新造。
backfill / reembed 没有 ``--user``: 见 main() 里那段说明。
"""

from __future__ import annotations

import argparse
import json
import sys


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


# ══════════════════════════════════════════════════════════════════════
# selftest —— 不连库、不联网, 只用标准库
#
# 查重与发牌是 deskcore 的两条命脉, 它们的回归必须在最便宜的环境里就能跑,
# 不能被 supabase / anthropic / mcp SDK 的安装问题挡住。所以这里用假 client,
# 且只 import deskcore 自己的纯模块。
# ══════════════════════════════════════════════════════════════════════

class _FakeSB:
    """只实现 store.fingerprints 用到的链式调用。"""

    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        self._name = name
        return self

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        class R:
            pass
        r = R()
        r.data = self._rows if self._name == "draft_fingerprints" else []
        return r


def selftest() -> int:
    """验四件事: 精确撞开头能拦、换皮改写能拦、正常变体不误伤、词表副本没被手改。"""
    from . import fingerprint as fp
    from . import vocab

    ok = True

    # ── 1. vendor 的词表副本完整性 ──
    v_ok, v_note = vocab.vendor_checksum_ok()
    print(f"vendored vocab: {'OK' if v_ok else 'FAIL'} ({v_note})")
    if not v_ok:
        print("  ✗ 词表副本被手改过。改词表要走上游 TV + 重新 vendor,"
              " 见 deskcore/vendor/README.md")
        ok = False
    if set(vocab.LEVER_TO_VALENCE) != set(vocab.EMOTIONAL_LEVERS):
        print("  ✗ lever→valence 派生表没覆盖全部 lever")
        ok = False
    if vocab.normalize_trends(["通用", "当代流行词"]) != ["通用"]:
        print("  ✗ 「通用」排他规则失效")
        ok = False

    # ── 2. 查重 ──
    # core 会 import db/dedup/memory(要装依赖); selftest 要能在裸环境跑,
    # 所以这里只测 verdict + fingerprint 这两块纯逻辑, 用手搭的历史池。
    hist_body = ("上周去闺蜜家看到她桌上放了一盒这个，随手拍了张照。\n"
                 "回来自己也买了一盒，用到现在大概两周。")
    hist_grams = set(fp.ngram_hashes(hist_body))
    hist_open = fp.opening_hash(hist_body)

    cases = [
        ("开头精确撞车(标题完全不同)",
         "完全不同的标题在这里", hist_body + "\n后面接了别的内容。", "reject"),
        ("换皮改写(标题也换了)",
         "另一个标题",
         "上周去闺蜜家看到她桌上放了一盒这个，随手拍了张照片。\n"
         "回来自己也买了一盒，用到现在差不多两周。", "reject"),
        ("真正不同的稿子",
         "加班到十点，回家路上买了这个",
         "地铁末班车上刷手机，看到有人在讨论换季干燥。\n"
         "第二天下班顺路去店里拿了一支。", "pass"),
    ]

    print("\n查重自测（无 embedding，纯确定性信号）:")
    from .fingerprint import verdict
    for label, title, body, expect in cases:
        exact = fp.opening_hash(body) == hist_open
        j = fp.jaccard(set(fp.ngram_hashes(body)), hist_grams)
        status, reason = verdict(0.0, exact, j)   # title_sim=0 模拟 embedding 不可用
        mark = "ok" if (status == expect or (expect == "reject" and status != "pass")) else "FAIL"
        if mark == "FAIL":
            ok = False
        print(f"  [{status:6s}] {label:22s} opening_exact={str(exact):5s} "
              f"ngram={j:.3f}  → 期望 {expect}  {mark}"
              + (f"\n           ↳ {reason}" if reason else ""))

    # ── 2b. n-gram 截断必须内容稳定 (codex P1 回归) ──
    # 按名次均匀采样(step=len/cap 取第 i*step 个)不是内容稳定的: 一个字的增删
    # 会改变整张排序表的名次, 两篇几乎相同的长文可能采出完全不同的子集,
    # Jaccard 掉到连 warn 线都够不上, 直接从硬闸溜过去。
    # bottom-k(取全局最小的 cap 个 hash)稳定, 因为某个 gram 在不在结果里只取决于
    # 它自己的 hash 值与第 k 小值的关系, 与文本长度、其它 gram 的名次无关。
    import random as _rnd
    _r = _rnd.Random(7)
    _pool = "的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方后多定行学法所民得经十三之进着等部度家电力里如水化高自二理起小物现实加量都两体制机当使点从业本去把性好应开它合还因由其些然前外天政四日那社义事平形相全表间样与关各重新线内数正心反你明看原又么利比或但质气第向道命此变条只没结解问意建月公无系军很情者最立代想已通并提直题党程展五果料象员革位入常文总次品式活设及管特件长求老头基资边流路级少图山统接知较将组见计别她手角期根论运农指几九区强放决西被干做必战先回则任取据处队南给色光门即保治北造百规热领七海口东导器压志世金增争济阶油思术极交受联什认六共权收证改清己美再采转更单风切打白教速花带安场身车例真务具万每目至达走积示议声报斗完类八离华名确才科张信马节话米整空元况今集温传土许步群广石记需段研界拉林律叫且究观越织装影算低持音众书布复容儿须际商非验连断深难近矿千周委素技备半办青省列习响约支般史感劳便团往酸历市克何除消构府称太准精值号率族维划选标写存候毛亲快效斯院查江型眼王按格养易置派层片始却专状育厂京识适属圆包火住调满县局照参红细引听该铁价严龙飞"
    _long = "".join(_r.choice(_pool) for _ in range(3000))
    _edited = _long[:1500] + "×" + _long[1500:]          # 中间插一个字

    def _all_grams(t):
        nm = fp.normalize(t)
        return {fp.sha16(nm[i:i + 4]) for i in range(len(nm) - 3)}

    truth = fp.jaccard(_all_grams(_long), _all_grams(_edited))     # 不截断的地面真值
    est = fp.jaccard(set(fp.ngram_hashes(_long)), set(fp.ngram_hashes(_edited)))
    n_full = len(_all_grams(_long))
    print(f"\nn-gram 截断稳定性（{n_full} grams，远超 cap={fp.NGRAM_CAP}）:")
    print(f"  地面真值 J={truth:.3f}   截断后估计 J={est:.3f}   偏差 {abs(truth - est):.3f}")
    if n_full <= fp.NGRAM_CAP:
        print("  ✗ 测试文本没超过 cap，没测到截断路径")
        ok = False
    elif abs(truth - est) > 0.15:
        print("  ✗ 截断后的估计偏离真值太多 —— 采样不是内容稳定的，"
              "长文的近似重复会从硬闸溜过去")
        ok = False
    else:
        print("  ok（bottom-k 采样，偏差在容忍范围内）")

    # ── 2c. 正例多样性上限必须真的执行 (codex P2 回归) ──
    # 全部候选共用同一个开头形态时, 补满那一趟若不限次数, 单一形态会占满
    # 全部 slot —— 趋同回路等于没断。
    same_shape = [{"title": f"t{i}", "body": "都用同一个开头这句话开场。\n然后各写各的" + str(i)}
                  for i in range(10)]
    picked = fp.cap_by_shape(same_shape, limit=5)
    print(f"\n正例多样性上限: 10 条同开头候选 → 选出 {len(picked)} 条 (上限 5//2=2)")
    if len(picked) > 2:
        print("  ✗ 单一开头形态占了超过一半的 slot，多样性约束没生效")
        ok = False
    else:
        print("  ok（宁可少给，不让单一形态占满）")

    # ── 3. 发牌指纹 ──
    from .fingerprint import angle_key
    d = {"emotional_lever": "焦虑撬动", "human_truth_archetype": "健康焦虑",
         "content_format": "情感叙事", "title_structure": "疑问句"}
    if angle_key(d) != angle_key({**d, "word_tilt": "自嘲", "emotional_intensity": "high"}):
        print("  ✗ angle_key 不该受叠加项影响(否则换个词感就被当成没用过, 台账白记)")
        ok = False
    else:
        print("\n发牌指纹: OK（叠加项不影响 angle_key）")

    print(f"组合空间: {vocab.combination_space()} 组 "
          f"(lever {len(vocab.EMOTIONAL_LEVERS)} × archetype {len(vocab.HUMAN_TRUTH_ARCHETYPES)}"
          f" × format {len(vocab.CONTENT_FORMATS)} × structure {len(vocab.TITLE_STRUCTURES)})")
    print("\nselftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# ══════════════════════════════════════════════════════════════════════
# doctor —— 上线前的那一次"别信文档, 去查库"
# ══════════════════════════════════════════════════════════════════════

_STATE_MARK = {
    "applied": "✓",
    "missing": "✗",
    "old_signature": "△",     # 函数在, 但停在旧签名(库只跑到 004)
    "unprobeable": "?",
    "error": "!",             # 探测本身失败 —— 不等于"没跑迁移"
}


def _doctor(core, sb, project_id: str | None) -> int:
    """把 ``core.migration_state`` 的结果排版出来, 并给出下一步。

    退出码: 有 missing / old_signature / error 时返 1 —— 让它能直接写进
    上线脚本, 而不是靠人读输出。``unprobeable`` 不影响退出码(见
    ``core.migration_state`` 的说明: 永远报红的检查等于没有检查)。
    """
    state = core.migration_state(sb)

    print("迁移状态(实测这个库, 不是照文档抄):\n")
    last = None
    for c in state["checks"]:
        if c["migration"] != last:
            print(f"  {c['migration']}")
            last = c["migration"]
        mark = _STATE_MARK.get(c["state"], "?")
        print(f"    {mark} {c['probe']:38s} {c['state']}")
        if c["state"] != "applied":
            print(f"        ↳ {c['note']}")
            print(f"        ↳ 不跑的后果: {c['impact']}")

    if state["unprobeable"]:
        print("\n探不到(PostgREST 够不着, 得自己用 SQL Editor 查):")
        for m in state["unprobeable"]:
            print(f"  ? {m}")

    # ⚠️ error 单独一段, **不能**混进"还缺这些迁移"(codex review · #63)。
    # 权限/连通性故障混进去的话, 这里会指挥人去跑一遍根本不缺的 SQL, 而上面
    # 那条 note 明明写着"不是「没跑迁移」"—— 自检工具给出互相矛盾的结论,
    # 比它不存在更坏。两者都判红, 但补救方式完全不同。
    if state.get("errors"):
        print("\n探测失败(**不是**缺迁移 —— 先查权限 / 连通性, 别急着跑 SQL):")
        for m in state["errors"]:
            print(f"  ! {m}")

    if state["missing"]:
        # 006 提前: 它是唯一一个不跑就当场坏的, 其余缺席都只是降级。按字典序
        # 打印会把它排在最后, 于是照着做的人会在"审稿按钮全报错"的状态下先跑
        # 完 002-005(其中 003 还要改数据), 而那份 runbook 写的是 006 优先。
        # 工具和文档给出不同的顺序, 人只会信工具。
        ordered = ([core.MIGRATION_RUN_FIRST]
                   if core.MIGRATION_RUN_FIRST in state["missing"] else [])
        ordered += [m for m in state["missing"] if m != core.MIGRATION_RUN_FIRST]

        print("\n还缺这些迁移 —— **按这个顺序跑**:")
        for m in ordered:
            first = "   ← 先跑这个" if m == core.MIGRATION_RUN_FIRST else ""
            print(f"  · migrations/{m}{first}")
        if core.MIGRATION_RUN_FIRST in state["missing"]:
            print("\n⚠️ 006 与其它几个不是一类: 它不跑不是降级, 是现有工作台的"
                  "「通过 / 打回」当场报错 —— 所以它排在最前面, "
                  "别让 003 那种要改数据的迁移把它挡在后面。")
    elif not state.get("errors"):
        print("\n迁移: 全部到位。")

    if project_id:
        gap = core.backfill_gap(sb, project_id)
        print(f"\n指纹回填缺口({project_id}):")
        print(f"  应有(items × versions 的最新版) : {gap['eligible']}")
        print(f"  已回填                          : {gap['backfilled']}")
        print(f"  还差                            : {gap['todo']}")
        print(f"  指纹表这个项目共                : {gap['fingerprints_total']} 行"
              "  (含写作台 commit_drafts 写进来的, 它们不在上面的分母里)")
        if gap.get("backfill_capped_warning"):
            print(f"\n  ⚠️ {gap['backfill_capped_warning']}")
        if not gap["done"]:
            print(f"\n  → {gap['next']}")
        elif gap["eligible"] == 0:
            print("\n  ⚠️ 这个项目一条历史成稿都没有 —— 不是全新项目的话, "
                  "检查 project_id 是不是传错了。")

    return 0 if state["ok"] else 1


# ══════════════════════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="deskcore", description="写作台内核 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selftest", help="不连库验查重/发牌/词表")
    sub.add_parser("health", help="回显配置与依赖可用性")

    # doctor 也是运维命令(同 backfill/reembed), 所以没有 --user。它只读,
    # 且不碰任何具体项目的内容 —— 传 --project 时只数条数, 不看正文。
    p = sub.add_parser(
        "doctor",
        help="上线前自检: 这个库跑到第几个迁移了, 缺的那些各自会怎样")
    p.add_argument("--project", default=None,
                   help="附带报这个项目的指纹回填缺口(与 backfill 同口径)")

    # ⚠️ 审计 COR-015 之后, 凡是走 MCP 工具那条路的子命令都要 --user: 归属校验
    # 在 core 层, CLI 和 MCP 走的是同一个函数, 不带身份一样会被拒。
    # backfill / reembed **刻意不要** —— 它们不是工具, 是运维命令, 跑它们的人
    # 手里已经握着 service_role key(等价于直连库), 在这儿加一道校验只会挡住
    # "帮同事补一下指纹"这类正当操作, 换不到任何隔离(能跑 CLI 的人本来就能
    # psql)。安全边界在 MCP/REST 那一面, 不在这儿。
    p = sub.add_parser("projects", help="列项目(仅 --user 名下)")
    p.add_argument("--user", required=True)

    p = sub.add_parser("open", help="打开项目, 打印完整写作简报")
    p.add_argument("--project", required=True)
    p.add_argument("--tactic", default="")
    p.add_argument("--topic", default="")
    p.add_argument("--user", required=True)

    p = sub.add_parser("draw", help="发牌")
    p.add_argument("--project", required=True)
    p.add_argument("-n", type=int, default=10)
    p.add_argument("--avoid-days", type=int, default=30)
    p.add_argument("--user", required=True)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--block", action="store_true", help="只打印可贴进 prompt 的坐标块")

    p = sub.add_parser("backfill", help="把历史成稿补进指纹库(部署时必跑一次)")
    p.add_argument("--project", required=True)
    p.add_argument("--no-embeddings", action="store_true",
                   help="只写确定性指纹(开头 + 四字串), 不算标题向量")

    p = sub.add_parser("reembed",
                       help="给指纹库里【缺标题向量】的行补向量(欠费恢复后跑)")
    p.add_argument("--project", required=True)

    p = sub.add_parser(
        "recompute-fingerprints",
        help="按当前 normalize 口径重算确定性指纹(**只在改了 normalize 之后跑**)")
    p.add_argument("--project", required=True)

    p = sub.add_parser("check", help="查重")
    p.add_argument("--project", required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--file", required=True,
                   help='JSON 文件: [{"title": "...", "body": "..."}, ...]')

    args = ap.parse_args(argv)
    if args.cmd == "selftest":
        return selftest()

    # 以下要连库, 到这一步才 import(让 selftest 不需要任何第三方依赖)
    from . import core

    if args.cmd == "health":
        import dedup
        from . import vocab
        v_ok, v_note = vocab.vendor_checksum_ok()
        _print({"embeddings": dedup.embeddings_available(),
                "vendored_vocab": {"ok": v_ok, "note": v_note}})
        return 0

    sb = core.sb()
    if args.cmd == "doctor":
        return _doctor(core, sb, args.project)
    if args.cmd == "projects":
        _print(core.list_projects(sb, user_id=args.user))
    elif args.cmd == "open":
        _print(core.build_writing_brief(
            sb, args.project, user_id=args.user,
            brief={"tactic": args.tactic, "draft_topic": args.topic}))
    elif args.cmd == "draw":
        out = core.draw_angles(sb, args.project, args.n,
                               avoid_days=args.avoid_days,
                               user_id=args.user, seed=args.seed)
        print(out["prompt_block"]) if args.block else _print(out)
    elif args.cmd == "backfill":
        def _prog(done, total):
            # 审计 ROB-011 之后回填是【流式】的 —— 分母是"目前为止发现的待回填
            # 条数", 会随着翻页往上走, 不是一开始就算好的总数。写清楚免得看的人
            # 以为进度条卡住或倒退。
            print(f"  已回填 {done} 条 / 已发现 {total} 条待回填", flush=True)
        out = core.backfill_fingerprints(
            sb, args.project, with_embeddings=not args.no_embeddings, progress=_prog)
        _print(out)
        if out["written"] == 0 and out["already"] == 0:
            print("\n⚠️ 这个项目没有任何历史成稿 —— 如果不是全新项目, "
                  "检查 project_id 是不是传错了。")
    elif args.cmd == "reembed":
        def _prog2(done, total):
            print(f"  {done}/{total}", flush=True)
        _print(core.reembed_fingerprints(sb, args.project, progress=_prog2))
    elif args.cmd == "recompute-fingerprints":
        def _prog3(done, total):
            print(f"  已重算 {done} 行 / 已扫描 {total} 行", flush=True)
        out = core.recompute_fingerprints(sb, args.project, progress=_prog3)
        _print(out)
        if out.get("ngram_unrecoverable"):
            print("\n⚠️ " + out["warning"])
    elif args.cmd == "check":
        with open(args.file, encoding="utf-8") as fh:
            drafts = json.load(fh)
        _print(core.check_drafts(sb, args.project, drafts, user_id=args.user))
    return 0


if __name__ == "__main__":
    sys.exit(main())
