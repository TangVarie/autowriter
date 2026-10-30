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
  python -m deskcore.cli reembed-rules --user <uuid> ← 给规则补向量(见 main() 里那段)
  python -m deskcore.cli sync-skill [--check]      ← 改了 protocol.md 之后跑: 把正文
                                                    接进 skills/…/SKILL.md(不连库)
  python -m deskcore.cli ingest --project <uuid> --user <uuid> --xlsx 表.xlsx [--dry-run]
  python -m deskcore.cli tv-map add --tv-project SPX_phase1 --project <uuid> --ingest-target
  python -m deskcore.cli delivery-format --project <uuid> (--show | --set 模板.json | --clear)
                                                    ← 项目的交付版式模板(score_drafts 按它拼交付稿)
  python -m deskcore.cli compile-rules (--project <uuid> | --all) [--force] [--dry-run]
                                                    ← 把规则交给 Jev 编成写后检查, 结果缓存进
                                                      rule_compiles(check_rules 用; 后台也会自己补编)
  python -m deskcore.cli tv-sync --all [--dry-run] [--write-tv]
                                                    ← TV 的笔记按内容对到写作台的版本,
                                                      对不上的补进指纹库(每天跑一次)
                                                    ← 把已发出去、没走 commit 的稿子
                                                      从飞书表补进指纹库(不过闸)

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


def _sync_skill(*, check: bool) -> int:
    """把 protocol.md 全文接进技能文件; ``check`` 只比对不写。不连库。"""
    from . import tools as T
    state = T.skill_sync_state()
    if check:
        mark = "一致" if state["in_sync"] else "不一致"
        print(f"skill 里的协议 {state['skill_version']} / protocol.md {state['protocol_version']}"
              f" → {mark}")
        if not state["in_sync"]:
            print("  → 跑 `python -m deskcore.cli sync-skill` 重新生成, 然后一起提交。")
        return 0 if state["in_sync"] else 1
    body, digest = T.protocol_text()
    current = T.SKILL_PATH.read_text(encoding="utf-8")
    T.SKILL_PATH.write_text(T.render_skill(current, body, digest), encoding="utf-8")
    print(f"已写入 {T.SKILL_PATH} (protocol_version {digest}, {len(body)} 字符)。"
          "记得让运营重新导入一次 skill。")
    return 0


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
    "denied": "⛔",           # 表建出来了, 但没发 GRANT(42501) —— 跑 007
}


# W-22: 超线项目的原因标签(core.pipeline_leak 给的)。/health 只给总数, 哪个项目是哪种只在这里看得到。
_LEAK_CAUSE = {
    "delivered_without_commit": "← 同期有飞书同步回来的补录副本(发出去的帖没对回写作台)",
    "not_delivered": "← 飞书里也没同步回来",
    "unknown": "← 原因查不到(补录副本数读失败)",
}


def _print_leak(core, sb) -> None:
    """近 7 天「发了角度没入库」。09-11 起这个数字在库里躺了一周没人看。"""
    try:
        leak = core.pipeline_leak(sb)
    except Exception as exc:  # noqa: BLE001 — doctor 的其它段不该被它拖死
        print(f"\n流程漏斗: 探不到({type(exc).__name__}: {exc})")
        return
    print(f"\n流程漏斗(近 {leak['window_days']} 天): 发角度 {leak['drawn']} / "
          f"入库销账 {leak['consumed']} / 没入库 {leak['leak_pct']}%")
    for prj in leak["projects"]:
        label = prj.get("name") or prj["project_id"][:8]
        cause = _LEAK_CAUSE.get(prj.get("likely_cause"), "")
        synced = prj.get("synced_back")
        tail = f"  {cause}" if cause else ""
        if cause and synced is not None:
            tail += f"(窗口内飞书同步回来 {synced} 条)"
        print(f"  {label:22s} 发 {prj['drawn']:4d}  入库 {prj['consumed']:4d}  "
              f"没入库 {prj['leak_pct']:3d}%{tail}")
    if not leak["ok"]:
        print(f"\n  ⚠️ {leak['note']}")
        print("     只作参考: 牌在存档、或发出去以后被 tv-sync 从查重底稿对回来时才销(W-38), 没写的、写了没发的本来就不销。"
              "发出去的帖 tv-sync 每晚会自己对回来或补录 —— 别手动 `ingest --xlsx` 补(补成副本就再也认不回写作台的稿)。")


def _ingest(core, sb, args) -> int:
    from pathlib import Path as _P
    from . import ingest as I
    parsed = I.read_published_xlsx(args.xlsx, sheet=args.sheet,
                                   title_col=args.title_col, body_col=args.body_col)
    print(f"表 {args.xlsx}: 形状={parsed['shape']}, 解析到 {len(parsed['rows'])} 条, "
          f"带 version_id(真走过 commit)跳过 {parsed['skipped_with_lineage']} 条, "
          f"空行 {parsed['skipped_empty']} 条")
    for r in parsed["rows"][:5]:
        print(f"  第 {r.row} 行  {r.title[:24]!r}  正文 {len(r.body)} 字")
    if len(parsed["rows"]) > 5:
        print(f"  … 还有 {len(parsed['rows']) - 5} 条")
    source = args.source or _P(args.xlsx).name
    out = core.ingest_published(
        sb, args.project, [{"title": r.title, "body": r.body} for r in parsed["rows"]],
        user_id=args.user, source=source, dry_run=args.dry_run)
    skipped = (f"指纹库里已有 {out['skipped_already_fingerprinted']} 条、"
               f"表内重复 {out['skipped_duplicate_in_sheet']} 条(都跳过)")
    if args.dry_run:
        print(f"\n--dry-run: 会写 {out['to_write']} 条; {skipped}。没动库。")
        return 0
    print(f"\n{skipped}; 建身份 {out['minted']}/{out['to_write']}, 写指纹 "
          f"{out['fingerprinted']}, batch_id={out['batch_id']}, 带标题向量={out['embedded']}")
    rc = 0
    if out["identity_error"]:
        print(f"  ⚠️ 身份没建全: {out['identity_error']}。没建成的行**重跑本命令**会"
              "补上(已有指纹的行会被跳过, 不会翻倍)。")
        rc = 1
    if out.get("fingerprint_error") or out["fingerprinted"] < out["minted"]:
        why = f"({out['fingerprint_error']})" if out.get("fingerprint_error") else ""
        print(f"  ⚠️ 有 {out['minted'] - out['fingerprinted']} 条建了身份没写上指纹{why} —— "
              "**不要重跑本命令**(重跑看不到它们的指纹, 会再建一份身份); 跑 "
              f"`python -m deskcore.cli backfill --project {args.project}`, 它按 "
              "version_id 幂等, 会把有身份没指纹的行补上。")
        rc = 1
    return rc


def _tv_map(core, sb, args) -> int:
    from . import store as S
    if args.action == "add":
        if not (args.tv_project and args.project):
            print("add 需要 --tv-project 与 --project")
            return 2
        S.tv_map_upsert(sb, args.tv_project, args.project,
                        ingest_target=args.ingest_target, note=args.note)
    rows = S.tv_project_map(sb)
    if not rows:
        print("tv_project_map 是空的 —— 用 `tv-map add --tv-project SPX_phase1 "
              "--project <uuid> --ingest-target` 加。")
        return 0
    for r in rows:
        mark = "← 补录目标" if r.get("ingest_target") else ""
        print(f"  {r['tv_project_id']:16s} → {r['project_id']}  {mark} {r.get('note') or ''}")
    return 0


def _tv_resolve(core, sb, args) -> int:
    try:
        out = core.tv_resolve(sb, args.note, ingest=args.ingest)
    except ValueError as e:
        print(f"没判: {e}")
        return 2
    print(out["note"])
    return 0 if out["resolved"] else 1


def _tv_sync(core, sb, args) -> int:
    from . import store as S
    if not args.tv_project and not args.all:
        print("要 --tv-project <id> 或 --all")
        return 2
    targets = ([args.tv_project] if args.tv_project else
               sorted({m["tv_project_id"] for m in S.tv_project_map(sb)}))
    if not targets:
        print("tv_project_map 是空的, 没有可同步的 TV 项目。")
        return 1
    rc = 0
    for tv in targets:
        try:
            out = core.tv_sync(sb, tv, dry_run=args.dry_run, write_tv=args.write_tv,
                               since=args.since, rematch=args.rematch)
        except Exception as exc:                        # noqa: BLE001
            # 一个 TV 项目炸了(锁 / 库 / 映射), 别让 --all 把后面的项目一起跳掉: 这是
            # 每天只跑一次的 cron, 一个项目的错不该让其它项目当天一行对照都没有
            # (TV 2026-10-08 审计 A-02)。记下来、退出码非零, 继续下一个。
            print(f"\n{tv}: ❌ tv-sync 失败: {type(exc).__name__}: {exc}")
            rc = 1
            continue
        head = "[dry-run] " if args.dry_run else ""
        print(f"\n{head}{tv}: {out['note']}")
        print(f"  写作台版本 {out['versions']} 条(项目 {len(out['desk_projects'])} 个), "
              f"对照写入 {out['links_written']}, 回填 TV {out['tv_backfilled']}")
        ing = out.get("ingest")
        if ing:
            print(f"  补录: 收到 {ing['received']}, 要写 {ing['to_write']}, 指纹库已有 "
                  f"{ing['skipped_already_fingerprinted']}, 表内重复 "
                  f"{ing['skipped_duplicate_in_sheet']}, 建身份 {ing['minted']}, "
                  f"写指纹 {ing['fingerprinted']}")
            fp_failed = bool(ing.get("fingerprint_error")) or ing["fingerprinted"] < ing["minted"]
            id_failed = bool(ing.get("identity_error"))
            missing = ing["minted"] - ing["fingerprinted"]
            if fp_failed and id_failed:
                left = ing.get("skipped_after_failure", 0)
                print(f"  ⚠️ 两种半途失败同时发生: {missing} 条建了身份没写上指纹"
                      f"({ing.get('fingerprint_error') or ''}), 另有几行身份没建成"
                      f"({ing['identity_error']})"
                      + (f", 还有 {left} 条因此没处理到" if left else "")
                      + f"。顺序必须是: **先**跑 `backfill --project "
                      f"{out['ingest_target']}` 把指纹补上, **然后**再跑一次 tv-sync 补没建成"
                      "的和没处理到的。别反过来, 也别现在就重跑 tv-sync。")
                rc = 1
            elif fp_failed:
                left = ing.get("skipped_after_failure", 0)
                print(f"  ⚠️ 有 {missing} 条建了身份没写上指纹"
                      f"({ing.get('fingerprint_error') or ''}) —— **先**跑 `backfill --project "
                      f"{out['ingest_target']}` 补指纹, **不要**重跑 tv-sync 指望它补这几条"
                      + (f"; 另有 {left} 条因此没处理到(记为 unmatched), backfill 之后"
                         "**再**跑一次 tv-sync 补它们" if left else ""))
                rc = 1
            elif id_failed:
                print(f"  ⚠️ 身份没建全: {ing['identity_error']} —— 重跑 tv-sync 即可, "
                      "已有的会被跳过")
                rc = 1
            if ing.get("chunk_error"):
                # 补录某一块炸了(锁 / 库): 已对上的对照照常写了, 没处理的记 unmatched 明晚重来 —— 但这不是
                # 一次健康的同步, 退出码得非零, 否则 Railway 的 cron 日志里它和正常跑长得一样 (codex review on #93)。
                print(f"  ⚠️ 补录有一块炸了({ing['chunk_error']}), {ing.get('skipped_after_failure', 0)} 条记为 "
                      "unmatched、明晚重来; 已对上的对照照常写了。")
                rc = 1
        sn = out.get("snapshots") or {}
        if not sn.get("applied", True):
            print("  · 查重底稿: 018 没跑, 没有底稿可对(对不上的照旧补录)")
        elif (sn.get("matched_notes") or sn.get("failed_notes") or sn.get("repaired") or sn.get("waiting_post")
              or sn.get("undated") or sn.get("held_rival")):
            # W-38: 写作台查过重、没存过档的稿子, 发出去以后按内容对回来、转成版本
            print(f"  查重底稿(W-38): {sn['matched_notes']} 篇帖对上了没存档的底稿, 转成版本 {sn['promoted']}"
                  f"(新建 {sn['minted']}), 销牌 {sn['angles_consumed']}, 写指纹 {sn['fingerprinted']}"
                  + (f"(其中 {sn['same_night']} 篇是改写过的, 按主索引的规矩连上今晚转正的版本)" if sn.get("same_night") else "")
                  + (f"; {sn['held_rival']} 篇和今晚转正的、存过档的分不出, 没补录, 明晚再对" if sn.get("held_rival") else "")
                  + (f"; {sn['undated']} 篇对得上底稿、还没填发布日期, 没补录, 填了再对" if sn.get("undated") else "")
                  + (f"; 补齐前一晚转了一半的 {sn['repaired']}" if sn.get("repaired") else "")
                  + (f"; 转了一半、帖不在这个 TV 项目里的 {sn['waiting_post']} 份补了指纹和销牌, 等连着帖的那个 TV 项目"
                     "记转正" if sn.get("waiting_post") else "")
                  + (f"; {sn['failed_notes']} 篇这晚没转成、没补录, 明晚再试" if sn.get("failed_notes") else "")
                  + (f"(其中 {sn['superseded']} 份底稿刚存了档, 明晚按存的那一版对)" if sn.get("superseded") else ""))
        if sn.get("held"):
            print(f"  ⚠️ 读不出查重底稿, 这晚 {sn['held']} 篇对不上的没补录(记 unmatched, 明晚再对)。")
        if sn.get("error"):
            print(f"  ⚠️ 查重底稿那一步出错: {sn['error']} —— 已对上的对照照常写了; 没做完的明晚接着做。")
            rc = 1
        pf = out.get("published_fp") or {}
        if pf.get("to_write"):
            # W-40: 对上写作台版本的帖, 发出去的文字记在那一版下(按文字幂等; 第一晚会把以前对上的一起补上)
            print(f"  帖的文字进指纹库(W-40): " + (f"会记 {pf['to_write']} 篇" if out.get("dry_run") else
                                                 f"记了 {pf['written']} 篇")
                  + f"(核了 {pf['checked']} 篇对上写作台版本的帖, 其余的文字已经在库里)")
        if pf.get("error"):
            print(f"  ⚠️ 帖的文字没记进指纹库: {pf['error']} —— 对照照常写了; 下一晚按文字再补。")
            rc = 1
        ed = out.get("edit_diffs") or {}
        if ed:
            print(f"  改动差异: 算了 {ed.get('computed', 0)} 对(改过的 {ed.get('changed', 0)}, 补算以前的 "
                  f"{ed.get('backfilled', 0)}, 重算 {ed.get('recomputed', 0)}), 写入 {ed.get('written', 0)}"
                  + (f"; 对照补上 lag {ed['lag_filled']} 条" if ed.get("lag_filled") else "")
                  + (f"; 降回旧口径 {ed['demoted']} 行" if ed.get("demoted") else "")
                  + (f"(其中飞书里删了的 {ed['vanished']} 篇)" if ed.get("demoted") and ed.get("vanished") else ""))
        if ed.get("error"):
            # 对照已经照常写了; 但差异一行没进库, 每周摘要就一直出不来 —— 不能让 cron 日志看起来健康(codex review on aw#107)
            print(f"  ⚠️ 改动差异没写成: {ed['error']} —— 先跑 `python -m deskcore.cli doctor` 看 013 上没上; "
                  "对照不受影响, 修好后下一晚会把没算的补上。")
            rc = 1
        if ed.get("demote_error"):
            print(f"  ⚠️ 该降回旧口径的差异行没降成: {ed['demote_error']} —— 这几行还会进每周摘要; 下一晚会再降。")
            rc = 1
        if ed.get("lag_error"):
            print(f"  ⚠️ 对照的 lag_days 没写回: {ed['lag_error']} —— 本次差异已按补出的 lag 排除抄回的那版, 下一晚会再补。")
            rc = 1
        if ed.get("vanished_error"):
            print(f"  ⚠️ 查不了飞书里删了哪些篇(v_notes_vanished): {ed['vanished_error']} —— 删了的篇的差异这晚没降, "
                  "还会进每周摘要; 下一晚会再查。")
            rc = 1
        rc = max(rc, _print_tv_drift(out, tv))
        for smp in out["ambiguous_samples"]:
            print(f"  ? 分不出 {smp['note_id']} 「{smp['title']}」: "
                  + "; ".join(f"{c['version_id'][:8]}({c['score']}, {c['lag_days']}d)"
                              for c in smp["candidates"]))
    if args.all and not args.dry_run:
        # W-38: 查重底稿留够天数就删(存的是稿子全文)。一晚一次, 不按 TV 项目
        try:
            pruned = core.prune_snapshots(sb)
        except Exception as exc:                        # noqa: BLE001
            print(f"\n⚠️ 删过期的查重底稿失败: {type(exc).__name__}: {exc} —— 明晚再删。")
            rc = 1
        else:
            if pruned:
                print(f"\n查重底稿: 删了 {pruned} 行超过 {core.SNAPSHOT_RETAIN_DAYS} 天的")
    return rc


def _print_tv_drift(out: dict, tv: str) -> int:
    """已对上的对照 TV 正文变了没有(W-34)。回这一段该给的退出码。"""
    dr = out.get("drift") or {}
    keys = ("seeded", "text_changed", "replaced", "kept_after_rematch", "relinked", "deferred", "blocked",
            "clear_missed", "unverifiable", "no_body_kept", "over_cap", "fp_held")
    if not any(dr.get(k) for k in keys) and not dr.get("clear_error") and not dr.get("fp_error"):
        return 0
    print(f"  正文核对: 第一次记指纹 {dr.get('seeded', 0)}, 改过字还是那一篇 {dr.get('text_changed', 0)}, "
          f"整篇换了 {dr.get('replaced', 0)}(挪了 {dr.get('relinked', 0)}"
          + (f", 撤回 TV 旧血缘 {dr['tv_cleared']}" if dr.get("tv_cleared") else "") + ")"
          + (f", 重对还是原来那版 {dr['kept_after_rematch']}" if dr.get("kept_after_rematch") else "")
          + (f", 原来那版取不到、明晚再核 {dr['unverifiable']}" if dr.get("unverifiable") else "")
          + (f", TV 正文是空的、没动 {dr['no_body_kept']}" if dr.get("no_body_kept") else "")
          + (f"; 记指纹 {dr['fp_written']} 行" if dr.get("fp_written") else ""))
    samples = dr.get("samples") or []
    # 预演和超上限时一条不漏: 放行之前人要看全清单(review on aw#128); 平时最多 10 条
    shown = samples if (out.get("dry_run") or dr.get("over_cap")) else samples[:10]
    for smp in shown:
        ov = "?" if smp.get("overlap") is None else smp["overlap"]
        new = (f"{smp['new_kind']} {(smp.get('new_version') or '')[:8]}".rstrip() if smp.get("new_kind") else "—")
        print(f"  ↻ {smp['note_id']}: {smp['old_kind']} {smp['old_version'][:8]} → {new} (重叠 {ov}) {smp['why']}")
    if len(samples) > len(shown):
        print(f"  …另有 {len(samples) - len(shown)} 条, 用 --dry-run 看全部")
    rc = 0
    if dr.get("over_cap"):
        print(f"  ⚠️ 正文整篇换了的有 {dr['over_cap']} 条, 超过每晚上限 {dr.get('relink_max')}: 一条都没挪, 差异已隔离。"
              f"先 `tv-sync --tv-project {tv} --write-tv --dry-run` 看清单; 都该挪就 "
              f"`TV_SYNC_RELINK_MAX={dr['over_cap']} python -m deskcore.cli tv-sync --tv-project {tv} --write-tv`"
              "(runbook §1.13)")
        rc = 1
    if dr.get("clear_error"):
        print(f"  ⚠️ 撤回 TV 旧血缘失败: {dr['clear_error']} —— 库 / RPC 出错(或 service_role 没有这个函数的 EXECUTE, 看 "
              "`doctor`)。这晚一条都没挪, 差异已隔离, 修好后下一晚会挪。")
        rc = 1
    if dr.get("clear_missed"):
        print(f"  ⚠️ {dr['clear_missed']} 条 TV 里的血缘已经不是我们写的那一对(被改过, 或 item 对不上): 没挪, 差异已隔离, "
              "到 TV 里看那一行再人工处理(runbook §1.13)。")
        rc = 1
    if dr.get("fp_error"):
        print(f"  ⚠️ 正文指纹没记上: {dr['fp_error']} —— 只是下一晚再核一遍。")
        rc = 1
    if dr.get("fp_held"):
        print(f"  {dr['fp_held']} 条改过字的差异这晚没算成, 指纹先不记, 下一晚再核、再算。")
    if dr.get("deferred") and not dr.get("clear_error"):
        print(f"  {dr['deferred']} 条整篇换了、但没开 --write-tv(撤回不了 TV 的旧血缘): 没挪, 差异已隔离, 下次带 --write-tv 跑时挪。")
    if dr.get("blocked") and any((s.get("old_kind") == "tv_lineage") for s in (dr.get("samples") or [])):
        print("  ⚠️ TV 自己带的 lineage 指着的那版和现在的正文不是一篇: 没挪(那是 TV 说的), 差异已隔离, 要人看。")
    return rc


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

        denied = set(state.get("denied") or ())
        print("\n还缺这些迁移 —— **按这个顺序跑**:")
        for m in ordered:
            first = "   ← 先跑这个" if m == core.MIGRATION_RUN_FIRST else ""
            # 缺 GRANT 和缺对象长得完全不一样, 别让人对着一个"表明明在"的库
            # 反复重跑建表 SQL 找不着北。
            why = "   (表在, 缺的是 GRANT)" if m in denied else ""
            print(f"  · migrations/{m}{first}{why}")
        if core.MIGRATION_RUN_FIRST in state["missing"]:
            print("\n⚠️ 006 与其它几个不是一类: 它不跑不是降级, 是现有工作台的"
                  "「通过 / 打回」当场报错 —— 所以它排在最前面, "
                  "别让 003 那种要改数据的迁移把它挡在后面。")
    elif not state.get("errors"):
        print("\n迁移: 全部到位。")

    _print_leak(core, sb)
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

def _delivery_format(core, sb, args) -> int:
    """模板写错 / default_params 解析不了: 打印原因, 退出码 1, 一个字不写。"""
    from . import delivery
    template = core.SHOW_ONLY                       # --show; --clear 不看它
    if args.set_file is not None:                   # --set "" (部署脚本里变量没设)也要去读、报读不了, 不能当成 --show
        try:
            with open(args.set_file, encoding="utf-8") as fh:
                template = json.load(fh)
        except (OSError, ValueError) as exc:
            print(f"读不了模板文件 {args.set_file}: {exc}")
            return 1
        if template is None:
            # 文件里是 JSON null: 原来和 --show 用的是同一个 None, 一个字不写还退出码 0(codex review on aw#115)
            print(f"模板没写进去: {args.set_file} 里是 null。要删掉模板用 --clear")
            return 1
    try:
        out = core.delivery_format(sb, args.project, template=template, clear=args.clear)
    except delivery.TemplateError as exc:
        print(f"模板没写进去: {exc}")
        return 1
    preview = out.pop("preview", None)
    _print(out)
    if preview:
        print("\n── 一篇示例稿拼出来是这样 ──\n" + preview)
    return 1 if out.get("error") else 0


_COMPILE_OK = ("up_to_date", "dry_run", "ok")


def _compile_rules(core, sb, args) -> int:
    """compile-rules(2026-10-20, D-121): 规则池里还没编的规则交给 Jev 编译。一个项目一行计数; 有项目没编成退出码 1。

    运维命令, 同 backfill 不要 --user(跑它的人握着 service_role key)。编一批最坏 6 分钟, ``--all`` 会跑很久 ——
    已经编过的(规则 id + sha 都没变)不再发, 所以第二次跑基本是秒回。

    整次运行共用一个 ``seen``(2026-10-20 复审): owner 的 global 规则在他每个项目的池子里, 没有它 ``--force`` 会按项目数
    重编、重付钱, ``--dry-run`` 也按项目数重复计数(真编时后面的项目命中缓存, 两边对不上)。带着它, 同一条规则同一个
    版本一次运行里最多发一次, dry-run 的「要编」合计就是真编要发的条数。

    ``--all`` 遇到读超时就停, 后面的项目不再试、列出来(2026-10-20 第二轮复审): 一个项目里读超时已经不发后面几批了, 跨项目
    原来照发 —— 每个项目各等满 RULE_COMPILE_TIMEOUT_SEC, 每一批都在 Jev 那边编完、计费, 结果没人收。Jev 慢不是哪个项目的事。"""
    from . import store
    if args.all:
        projects = store.projects_with_rules(sb)
    else:
        row = store.project_row(sb, args.project)
        if row is None:
            print(f"项目不存在: {args.project}")
            return 1
        projects = [row]
    if not projects:
        print("没有带规则的项目。")
        return 0
    bad = 0
    tot = {"pool": 0, "cached": 0, "to_compile": 0, "compiled": 0, "errors": 0}
    seen: set[tuple[str, str]] = set()
    not_tried: list[dict] = []
    for n, p in enumerate(projects):
        pid, name = str(p["id"]), p.get("name") or ""
        try:
            rep = core.compile_rules(sb, pid, force=args.force, dry_run=args.dry_run, seen=seen)
        except Exception as exc:                         # noqa: BLE001 — 一个项目坏了不拦后面的
            print(f"✗ {name} ({pid}): {type(exc).__name__}: {exc}"[:300], flush=True)
            bad += 1
            continue
        st = rep.get("status")
        ok = st in _COMPILE_OK
        bad += 0 if ok else 1
        for k in ("pool", "cached", "to_compile", "compiled"):
            tot[k] += int(rep.get(k) or 0)
        tot["errors"] += len(rep.get("errors") or [])
        print(f"{'✓' if ok else '✗'} {name} ({pid}): 池子 {rep.get('pool')} 条 · 已编过 {rep.get('cached')} · "
              f"要编 {rep.get('to_compile')} · 这次编进 {rep.get('compiled')} · 失败 {len(rep.get('errors') or [])} "
              f"[{st}] {rep.get('elapsed_ms')}ms", flush=True)
        if rep.get("same_run"):
            print(f"    · 其中 {rep['same_run']} 条(owner 的 global 规则)这次运行里前面的项目已经编过 / 数过, 不再发")
        if rep.get("verdicts"):
            print("    结论: " + " / ".join(f"{k} {v}" for k, v in sorted(rep["verdicts"].items())))
        if rep.get("detail"):
            print(f"    {rep['detail']}")
        for e in (rep.get("errors") or [])[:5]:
            print(f"    · {e.get('memory_id')}: {e.get('error')}")
        if len(rep.get("errors") or []) > 5:
            print(f"    · …还有 {len(rep['errors']) - 5} 条")
        for b in rep.get("batch_errors") or []:
            print(f"    · 整批 {b.get('rules')} 条没编成 [{b.get('status')}]: {b.get('detail')}")
        if rep.get("not_sent"):
            why = ("Jev 读超时, 那一批多半还在编、照样计费; Jev 恢复之后再跑一次"
                   if rep.get("stopped_by") == "timeout" else "同一个原因会把它们一样挡下")
            print(f"    · 后面 {rep['not_sent']} 条没发({why})")
        if rep.get("not_compilable"):
            print(f"    · {len(rep['not_compilable'])} 条发不了(原文超过 Jev 的上限等), 留在简报里照旧")
        if core.judge_client.JUDGE_TIMEOUT in (st, rep.get("stopped_by")):
            not_tried = projects[n + 1:]
            if not_tried:
                print(f"⏸ Jev 读超时, 后面 {len(not_tried)} 个项目这次没试(超时那批多半还在编、照样计费, 再发只是多付钱; "
                      "Jev 恢复之后再跑一次 compile-rules): "
                      + "、".join(f"{q.get('name') or ''} ({q['id']})" for q in not_tried), flush=True)
            break
    print(f"\n合计 {len(projects)} 个项目: 池子 {tot['pool']} 条 · 已编过 {tot['cached']} · 要编 {tot['to_compile']} · "
          f"这次编进 {tot['compiled']} · 失败 {tot['errors']}" + (" (dry-run, 一条没发)" if args.dry_run else "")
          + (f" · {len(not_tried)} 个项目这次没试(Jev 读超时)" if not_tried else ""))
    return 1 if bad or not_tried else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="deskcore", description="写作台内核 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selftest", help="不连库验查重/发牌/词表")
    sub.add_parser("health", help="回显配置与依赖可用性")

    # 协议正文的主副本在技能文件里(进系统提示, 整场对话都在模型眼前), 源文件是
    # deskcore/protocol.md。改了源文件就跑这个把正文接进 SKILL.md; --check 只比
    # 不写, 给 CI 用。为什么不让 get_protocol 每次下发正文: 见 tools.get_protocol。
    p = sub.add_parser("sync-skill",
                       help="把 protocol.md 接进 skills/…/SKILL.md(不连库)")
    p.add_argument("--check", action="store_true",
                   help="只检查两边是否一致, 不一致退出码 1, 不写文件")

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

    # ⚠️ 这不是从零新建的能力, 也不是给「新规则没向量」兜底的 ——
    # **新规则本来就有向量**: db.upsert_memory 在写入时就算(db.py:1748),
    # 所以走 record_rule 记的规则不会缺。这个命令只管**存量**:
    #   · 2026-08-28 用裸 SQL 灌的 66 条技艺库种子(绕过了 upsert_memory)
    #   · 更早那批建于 embedding 这段代码之前 / 当时没配 GOOGLE_API_KEY 的
    # Streamlit 的记忆管理页也有一个「立即补算(最多 50 条)」按钮调同一个 db
    # 函数(memory.py:2072)。这一版补的是它没有的三件事 —— 翻页 / 停滞检测 /
    # 非零退出码(能脚本化), 外加一个不依赖 Streamlit 还活着的入口。
    # 补几条的话那个按钮更快。(codex review · PR #73)
    p = sub.add_parser(
        "reembed-rules",
        help="给【规则】补向量 —— soft 规则的相关性过滤靠它才有意义")
    p.add_argument("--user", required=True,
                   help="给谁补。规则按 user_id 归属, 一次只补一个人")
    p.add_argument("--batch", type=int, default=50,
                   help="每轮补多少条(默认 50, 与 db 那侧的上限一致)")
    p.add_argument("--max-rounds", type=int, default=40,
                   help="最多转多少轮 —— 停滞保护, 见下面那段")

    p = sub.add_parser("check", help="查重")
    p.add_argument("--project", required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--file", required=True,
                   help='JSON 文件: [{"title": "...", "body": "..."}, ...]')

    # 09-11 起 78% 的稿子没走 commit_drafts, 已经发出去了但指纹库不知道它们 ——
    # 下一批查重看不见, 于是同样的句子被再写一遍。这个命令把它们从飞书表读回来
    # 补进库, **不过闸**(已发生的事实, 拦它没有意义)。认两种表: export_drafts
    # 导出的形状(「内容」+ lineage 列, 带 version_id 的行跳过), 或「标题」「正文」
    # 两列。见 deskcore/ingest.py。
    p = sub.add_parser("ingest", help="把已发未入库的稿子从飞书表补进指纹库(不过闸)")
    p.add_argument("--project", required=True)
    p.add_argument("--user", required=True, help="这批稿子的作者(项目 owner)")
    p.add_argument("--xlsx", required=True)
    p.add_argument("--sheet", default=None)
    p.add_argument("--title-col", default=None)
    p.add_argument("--body-col", default=None)
    p.add_argument("--source", default=None,
                   help="记到 batches.params.file 的出处标签, 默认用文件名")
    p.add_argument("--dry-run", action="store_true", help="只解析、只数, 不写库")

    # TV 对照(migrations/009): TV 的笔记按内容对到写作台的版本, 对不上的补录。
    # 运营不用做任何事 —— 见 core.tv_sync 与 deskcore/tvlink.py 的说明。
    p = sub.add_parser("tv-map", help="TV 项目 ↔ 写作台项目的对照表")
    p.add_argument("action", choices=["list", "add"])
    p.add_argument("--tv-project", default=None, help="TV 的 project_id, 如 SPX_phase1")
    p.add_argument("--project", default=None, help="写作台项目 uuid")
    p.add_argument("--ingest-target", action="store_true",
                   help="对不上的笔记补录进这个写作台项目(每个 TV 项目只能有一个)")
    p.add_argument("--note", default="")
    p = sub.add_parser("tv-sync", help="把 TV 的笔记对到写作台的版本, 对不上的补进指纹库")
    p.add_argument("--tv-project", default=None)
    p.add_argument("--all", action="store_true", help="tv_project_map 里的全部 TV 项目")
    p.add_argument("--since", default=None, help="只看 TV 里这个时间之后进的笔记(ISO)")
    p.add_argument("--rematch", action="store_true",
                   help="已对上的也按内容重新对一遍(我们自己写回 TV 的 lineage 不算证据; ingested 和人定过的不动; "
                        "挪走要 --write-tv, 受 TV_SYNC_RELINK_MAX 限)")
    p.add_argument("--write-tv", action="store_true",
                   help="把对照写回 TV 的 source_autowriter_* 列(只填空的; 先跟 TV 打招呼)")
    p.add_argument("--dry-run", action="store_true", help="全部算、一行不写")
    # 交付版式(2026-10-20): 模板存在 projects.default_params.delivery, score_drafts 存完档按它拼整批交付稿。
    # 运维命令, 同 backfill 不要 --user; 写手那边没有对应的工具 —— 版式是项目定的, 不该在对话里被模型改掉。
    # 字段说明见 deskcore/delivery.py 与 docs/deskcore.md §3.9。
    p = sub.add_parser("delivery-format", help="看 / 设 / 清项目的交付版式模板(写之前先校验)")
    p.add_argument("--project", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--show", action="store_true", help="打印现在的模板和一篇示例稿拼出来的样子")
    g.add_argument("--set", dest="set_file", default=None, help="JSON 文件: 模板对象本身")
    g.add_argument("--clear", action="store_true", help="删掉模板(score_drafts 不再带 delivery)")

    # 规则编译(2026-10-20, D-121): 规则交给 Jev 编成写后检查, 结果按 (规则 id, sha) 缓存进 rule_compiles。
    # open_project / record_rule / check_rules 会在后台自己补编; 这个命令给上线时补一遍、或者 Jev 那边改了编译之后 --force。
    p = sub.add_parser("compile-rules", help="把规则交给 Jev 编成写后检查(check_rules 用), 已编过的不再发")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--project", default=None)
    g.add_argument("--all", action="store_true", help="全部带规则的项目(有项目规则的, 加上 owner 有 global 规则的)")
    p.add_argument("--force", action="store_true", help="已经编过的也重编、覆盖(花钱; 只在 Jev 改了编译之后用)")
    p.add_argument("--dry-run", action="store_true", help="只数要编几条, 一条不发")

    p = sub.add_parser("tv-resolve",
                       help="人看过 tv-sync 报的「分不出」之后落判定(目前只有: 对不上, 按正文补录)")
    p.add_argument("--note", required=True, help="tv_note_links.note_id")
    p.add_argument("--ingest", action="store_true", help="判定为对不上, 把 TV 的正文补进指纹库")

    args = ap.parse_args(argv)
    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "sync-skill":
        return _sync_skill(check=args.check)

    # 以下要连库, 到这一步才 import(让 selftest 不需要任何第三方依赖)
    import config
    import db as db_mod
    from . import core

    # CLI 进程跑完就退出: open 之类的命令触发的后台补编(D-121)会被杀在半路 —— Jev 那边照样编完、照样花钱, 结果却没人
    # 存。CLI 里要编就用 compile-rules(同步跑完)。
    config.RULE_COMPILE_AUTO = False

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
    if args.cmd == "ingest":
        return _ingest(core, sb, args)
    if args.cmd == "tv-map":
        return _tv_map(core, sb, args)
    if args.cmd == "tv-sync":
        return _tv_sync(core, sb, args)
    if args.cmd == "tv-resolve":
        return _tv_resolve(core, sb, args)
    if args.cmd == "delivery-format":
        return _delivery_format(core, sb, args)
    if args.cmd == "compile-rules":
        return _compile_rules(core, sb, args)
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
    elif args.cmd == "reembed-rules":
        # ⚠️ 三件事都必须显式做, 少一件这个命令就会"看起来跑完了"而其实没干活:
        #
        # 1) **状态码不许吞。** db.backfill_memory_embeddings 特意把
        #    "没需要补的"(noop) 和 "没配 GOOGLE_API_KEY"(no_embedding_sdk) /
        #    "pgvector 列没建"(schema_missing) 分开返回 —— 它的 docstring 写着
        #    这几种以前一律塌成 int 0, 点了按钮显示"没有需要补算的记忆", 运维
        #    无从判断为什么没反应。这里按状态分别退出码, 别再塌回去。
        # 2) **要翻页。** 那个函数一轮最多 max_rows 条, 一次调用补不完。
        # 3) **要有停滞检测。** 它对算不出向量的行是 `continue` 跳过的 ——
        #    于是可能 status='ok' 但 updated=0, 而缺向量的行一条没少。
        #    "补不完就一直转"在这里会变成【死循环】: 每轮都查到同一批行、
        #    每轮都补 0 条。所以 updated==0 就停下并如实报, 不假装成功。
        total = 0
        for rnd in range(1, args.max_rounds + 1):
            out = db_mod.backfill_memory_embeddings(
                sb, args.user, max_rows=args.batch)
            st = (out or {}).get("status")
            if st == "noop":
                _print({"status": "done", "updated": total, "rounds": rnd - 1})
                if total == 0:
                    print("\n这个人名下没有缺向量的规则 —— 要么已经补齐, "
                          "要么他名下压根没有规则。")
                return 0
            if st != "ok":
                _print(out)
                print(f"\n⚠️ 补到第 {rnd} 轮停了, 已补 {total} 条。"
                      "上面的 status 是原因, **不是「补完了」**。")
                return 1
            n = int(out.get("updated") or 0)
            if n == 0:
                _print({"status": "stalled", "updated": total, "rounds": rnd})
                print("\n⚠️ 这一轮查到了缺向量的行, 却一条都没补上"
                      "(embed 返回空 / 写回被拒)。**停在这里, 不再空转** —— "
                      "服务端日志里有每行失败的原因(telemetry: "
                      "backfill_memory_row_failed)。")
                return 1
            total += n
            print(f"  第 {rnd} 轮: +{n} 条(累计 {total})", flush=True)
        _print({"status": "max_rounds_reached", "updated": total,
                "rounds": args.max_rounds})
        print(f"\n⚠️ 转满 {args.max_rounds} 轮还没补完, 已补 {total} 条。"
              "**没补完**, 再跑一次接着补(这个命令是幂等的)。")
        return 1
    elif args.cmd == "check":
        with open(args.file, encoding="utf-8") as fh:
            drafts = json.load(fh)
        _print(core.check_drafts(sb, args.project, drafts, user_id=args.user))
    return 0


if __name__ == "__main__":
    sys.exit(main())
