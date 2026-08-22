"""deskcore/cli.py — 本地 CLI, 与 HTTP/MCP 共用同一个 core。

用法:
  python -m deskcore.cli selftest                 ← 不连库不联网, 验查重/发牌/词表
  python -m deskcore.cli health
  python -m deskcore.cli projects
  python -m deskcore.cli open  --project <uuid> [--tactic ...] [--user <uuid>]
  python -m deskcore.cli draw  --project <uuid> -n 20 [--block]
  python -m deskcore.cli check --project <uuid> --file drafts.json
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
    elif args.cmd == "check":
        with open(args.file, encoding="utf-8") as fh:
            drafts = json.load(fh)
        _print(core.check_drafts(sb, args.project, drafts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
