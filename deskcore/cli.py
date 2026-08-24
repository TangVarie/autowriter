"""deskcore/cli.py — 本地 CLI, 与 HTTP/MCP 共用同一个 core。

用法:
  python -m deskcore.cli selftest                 ← 不连库不联网, 验查重/发牌/词表
  python -m deskcore.cli health
  python -m deskcore.cli projects
  python -m deskcore.cli open  --project <uuid> [--tactic ...] [--user <uuid>]
  python -m deskcore.cli draw  --project <uuid> -n 20 [--block]
  python -m deskcore.cli check --project <uuid> --file drafts.json
  python -m deskcore.cli backfill --project <uuid>   ← 部署时必跑一次
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
    print(f"\nn-gram 截断稳定性（{n_full} grams，远超 cap=200）:")
    print(f"  地面真值 J={truth:.3f}   截断后估计 J={est:.3f}   偏差 {abs(truth - est):.3f}")
    if n_full <= 200:
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

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="deskcore", description="写作台内核 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selftest", help="不连库验查重/发牌/词表")
    sub.add_parser("health", help="回显配置与依赖可用性")
    sub.add_parser("projects", help="列项目")

    p = sub.add_parser("open", help="打开项目, 打印完整写作简报")
    p.add_argument("--project", required=True)
    p.add_argument("--tactic", default="")
    p.add_argument("--topic", default="")
    p.add_argument("--user", default=None)

    p = sub.add_parser("draw", help="发牌")
    p.add_argument("--project", required=True)
    p.add_argument("-n", type=int, default=10)
    p.add_argument("--avoid-days", type=int, default=30)
    p.add_argument("--user", default=None)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--block", action="store_true", help="只打印可贴进 prompt 的坐标块")

    p = sub.add_parser("backfill", help="把历史成稿补进指纹库(部署时必跑一次)")
    p.add_argument("--project", required=True)
    p.add_argument("--no-embeddings", action="store_true",
                   help="只写确定性指纹(开头 + 四字串), 不算标题向量")

    p = sub.add_parser("reembed",
                       help="给指纹库里【缺标题向量】的行补向量(欠费恢复后跑)")
    p.add_argument("--project", required=True)

    p = sub.add_parser("check", help="查重")
    p.add_argument("--project", required=True)
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
    if args.cmd == "projects":
        _print(core.list_projects(sb))
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
    elif args.cmd == "check":
        with open(args.file, encoding="utf-8") as fh:
            drafts = json.load(fh)
        _print(core.check_drafts(sb, args.project, drafts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
