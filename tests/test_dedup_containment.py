"""审计 COR-014 · 正文四字串那一路的两个问题。

问题一 **估计有偏**: ``ngram_hashes`` 给每篇各自取最小的 200 个 hash
(bottom-k sketch)。两篇长度差得多时它们的第 200 小值差着量级, 直接对两个
sketch 求交并比会系统性偏低。

问题二 **指标选错**: 就算估计改准了, "短稿逐字抄长稿"这个形状下 Jaccard 的
**真值**本来就小(短稿整篇塞进长稿 ⇒ J = |A|/|B|), 够不着 0.35 的硬闸线。

这两件事必须分开验 —— 只验第一件的话, 修完"估计"会以为收工了, 而真正的漏检
一条没少。下面第 2 组就是钉这一点的。

阈值都从**零分布**定, 不是拍的; 合成对照的完整数字在
docs/audit-2026-08-23-full.md §0.5 与 deskcore/fingerprint.py 的注释里。
"""

from __future__ import annotations

import random

import pytest

from deskcore import core
from deskcore import fingerprint as fp

POOL = ("的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会"
        "可主发年动同工也能下过子说产种面而方后多定行学法所民得经十三之进着等部"
        "度家电力里如水化高自二理起小物现实加量都两体制机当使点从业本去把性好应"
        "开它合还因由其些然前外天政四日那社义事平形相全表间样与关各重新线内数正")


def _doc(rng: random.Random, n_sent: int) -> str:
    """造一段长度可控的中文。内容随机 —— 逐字抄袭是真的逐字, 不是"话题相近"。"""
    return "".join(
        f"这一段说的是{''.join(rng.choice(POOL) for _ in range(18))}。"
        for _ in range(n_sent))


def _prefix(text: str, n_sent: int) -> str:
    """取前 n_sent 句 —— 逐字子串。"""
    return "。".join(text.split("。")[:n_sent]) + "。"


def _all_grams(text: str, n: int = 4) -> set[str]:
    """不截断的完整四字串集合 —— 地面真值, 只在测试里用。"""
    norm = fp.normalize(text)
    return {fp.sha16(norm[i:i + n]) for i in range(len(norm) - n + 1)}


# ══════════════════════════════════════════════════════════════════════
# 0 · normalize 的口径 —— 存量指纹全靠它, 动它等于让历史指纹作废
# ══════════════════════════════════════════════════════════════════════

def test_punct_pattern_is_byte_for_byte_what_it_always_was():
    """``_PUNCT_RE`` 原来是两个字符串隐式拼接(后半截不是 raw), 现在改写成一个
    完整的 raw 串把 DeprecationWarning 消掉 —— 但**编译结果必须一模一样**。

    为什么要钉到这个程度: ``normalize`` 是库里几千条存量指纹的计算口径。
    它变一个字符, 新稿算出来的四字串就和历史对不上, 查重当场变弱, 而且
    **不报错**。修一个 lint 警告顺手改宽了字符类, 是最容易发生的那种事故。
    """
    assert fp._PUNCT_RE.pattern == (
        "[\\s，。！？、；：''《》（）()\\[\\]…—~·,.!?;:'\"\\-_/\\|+*#@$%^&]+")


def test_known_gap_curly_quotes_and_backslash_are_not_stripped():
    """把**当前的缺口**钉成可执行的事实, 而不是留一句注释。

    中文弯引号 “ ” 不在字符类里(原来那半截非 raw 串把 ASCII 双引号吃掉了,
    弯引号则从来就没写进去)。后果很具体: 同一篇稿子换个引号样式, Jaccard
    掉到 0.35 硬闸线以下, 开头指纹也对不上 —— 是一条能绕过查重的路子。

    **这次刻意不修**: 改 normalize 会让库里存量指纹的口径与新稿不一致, 过渡期
    查重反而更弱, 而 backfill 按 version_id 幂等跳过、不会重算已有行。要改必须
    配一次全量重算。这条用例存在的意义是: 修的时候它会红, 逼人一起处理重算。
    """
    a = "他说“这个真的好用”，回购了三次。"
    b = '他说"这个真的好用"，回购了三次。'      # 只有引号形态不同
    assert fp.normalize(a) != fp.normalize(b), (
        "弯引号已经被 normalize 掉了 —— 说明有人改了字符类。"
        "那是对的方向, 但必须同时安排存量指纹重算, 否则查重在过渡期变弱。"
        "改完请把这条用例改成断言相等。")
    assert fp.jaccard(set(fp.ngram_hashes(a * 20)),
                      set(fp.ngram_hashes(b * 20))) < fp.NGRAM_JACCARD_HARD
    assert fp.normalize("a\\b") == "a\\b", "反斜杠现在不在字符类里"


# ══════════════════════════════════════════════════════════════════════
# 1 · 估计式: 只在两个 sketch 都覆盖的区间上算
# ══════════════════════════════════════════════════════════════════════

def test_identical_docs_are_exactly_one():
    body = _doc(random.Random(1), 40)
    s = set(fp.ngram_hashes(body))
    j, c, sample = fp.sketch_overlap(s, s)
    assert j == 1.0 and c == 1.0, (j, c)
    assert sample == len(s)


def test_union_estimator_beats_naive_on_length_disparity():
    """长短稿之间, 新估计式必须比"直接对两个 sketch 求交并比"更接近真值。

    这条不是"新的更好看"这种模糊断言 —— 它逐例比较**与地面真值的偏差**,
    并要求新的严格不差、且至少有一例明显更好。
    """
    rng = random.Random(20260824)
    improved = 0
    for n_short, n_long in [(12, 60), (12, 120), (12, 240), (30, 300)]:
        long_body = _doc(rng, n_long)
        short_body = _prefix(long_body, n_short)
        truth = fp.jaccard(_all_grams(short_body), _all_grams(long_body))

        sa = set(fp.ngram_hashes(short_body))
        sb = set(fp.ngram_hashes(long_body))
        naive = fp.jaccard(sa, sb)            # 旧做法: 直接对两个 sketch 算
        new, _, _ = fp.sketch_overlap(sa, sb)

        assert abs(new - truth) <= abs(naive - truth) + 1e-9, (
            f"{n_short}句⊂{n_long}句: 新估计 {new:.4f} 比旧的 {naive:.4f} "
            f"更偏离真值 {truth:.4f}")
        # 旧做法【总是偏低】—— 它把短稿里落在长稿 sketch 之外的 hash 当成
        # "长稿没有"。方向性是这条 bug 的本质, 不只是"不够准"。
        assert naive <= truth + 1e-9, f"旧做法居然偏高了: {naive:.4f} > {truth:.4f}"
        if abs(naive - truth) - abs(new - truth) > 0.01:
            improved += 1
    assert improved >= 2, "新估计式在长短稿上没有可见改善, 断言可能没在验东西"


def test_cap_is_large_enough_for_the_common_shape():
    """cap 决定包含度这一路够不够得着样本量下限 —— 这不是"调大一点更好"的事。

    实测: cap=200 时, 12 句 ⊂ 120 句(约 280 字草稿 vs 2800 字历史, 最常见的
    形状)的样本量 p05 只有 12, **低于** CONTAIN_MIN_SAMPLE=15 —— 也就是有 5%
    以上的概率这一路直接不发言, 抓不抓得到全看运气。cap=400 之后 p05 在 30 以上。

    这条用 p05 而不是中位数: 中位数在 cap=200 下也有 21, 看着够用 —— 只看中位数
    会得出"不用改"的错误结论。
    """
    rng = random.Random(4242)
    samples = []
    for _ in range(40):
        long_body = _doc(rng, 120)
        short_body = _prefix(long_body, 12)
        _, _, m = fp.sketch_overlap(set(fp.ngram_hashes(short_body)),
                                    set(fp.ngram_hashes(long_body)))
        samples.append(m)
    samples.sort()
    p05 = samples[1]
    assert p05 >= fp.CONTAIN_MIN_SAMPLE, (
        f"12句⊂120句 的样本量 p05={p05} 低于下限 {fp.CONTAIN_MIN_SAMPLE} —— "
        f"包含度这一路在最常见的形状上会随机哑火(现在 cap={fp.NGRAM_CAP})")


def test_mixed_cap_comparison_is_still_correct():
    """新 sketch(cap=400) 比旧 sketch(cap=200) 必须仍然对 —— 存量指纹不用重算。

    t = min(两边最大值) 会让精度退回**旧的那一边**, 也就是与今天持平, 不会更差。
    这条钉住"不需要 re-backfill"这个承诺。
    """
    rng = random.Random(77)
    long_body = _doc(rng, 120)
    short_body = _prefix(long_body, 12)
    new_short = set(fp.ngram_hashes(short_body, cap=400))
    old_long = set(fp.ngram_hashes(long_body, cap=200))     # 库里存量的形态
    j, contain, sample = fp.sketch_overlap(new_short, old_long)
    assert contain >= fp.NGRAM_CONTAIN_HARD, (contain, sample)
    assert j < fp.NGRAM_JACCARD_HARD


def test_sketch_overlap_degrades_cleanly_on_empty():
    assert fp.sketch_overlap(set(), set()) == (0.0, 0.0, 0)
    assert fp.sketch_overlap({"a" * 16}, set()) == (0.0, 0.0, 0)


def test_disjoint_hash_ranges_do_not_divide_by_zero():
    """两个 sketch 的 hash 区间完全不重叠 —— 子域里一边是空的。

    人造但会真的发生(一篇极短、hash 全落在高位)。要求返回 0 而不是抛。
    """
    lo = {f"0000{i:012x}" for i in range(5)}
    hi = {f"ffff{i:012x}" for i in range(5)}
    j, c, sample = fp.sketch_overlap(lo, hi)
    assert (j, c, sample) == (0.0, 0.0, 0)


# ══════════════════════════════════════════════════════════════════════
# 2 · 指标: Jaccard 结构上抓不到短稿照搬长稿
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("n_short,n_long", [(12, 60), (12, 120), (30, 120), (30, 300)])
def test_containment_catches_what_jaccard_structurally_cannot(n_short, n_long):
    """同一组输入上: 包含度 reject, 而 Jaccard **连真值都够不着硬闸线**。

    第二个断言是这条用例的重点 —— 它证明漏检不是"算得不准", 而是**指标选错**。
    如果哪天有人把包含度删掉、说"把 Jaccard 算准就行了", 这条会红。
    """
    rng = random.Random(n_short * 1000 + n_long)
    long_body = _doc(rng, n_long)
    short_body = _prefix(long_body, n_short)

    sa, sb = set(fp.ngram_hashes(short_body)), set(fp.ngram_hashes(long_body))
    j, contain, sample = fp.sketch_overlap(sa, sb)

    assert sample >= fp.CONTAIN_MIN_SAMPLE, f"样本量 {sample} 不够, 这一路不该发言"
    assert contain >= fp.NGRAM_CONTAIN_HARD, f"逐字抄袭漏了: 包含度={contain:.4f}"

    truth_j = fp.jaccard(_all_grams(short_body), _all_grams(long_body))
    assert truth_j < fp.NGRAM_JACCARD_HARD, (
        f"Jaccard 的**真值** {truth_j:.4f} 够着了 {fp.NGRAM_JACCARD_HARD} —— "
        "那这个形状根本不需要包含度, 整条 finding 的前提就错了")

    assert fp.deciding_signals(0.0, False, j, contain, sample)[0] == "reject"


def test_containment_does_not_fire_on_unrelated_docs():
    """误伤检查。误杀比漏检更难被发现 —— 用户只会觉得"这系统老让我重写"。"""
    rng = random.Random(99)
    for n_a, n_b in [(12, 120), (30, 120), (60, 60), (120, 120)]:
        a = set(fp.ngram_hashes(_doc(rng, n_a)))
        b = set(fp.ngram_hashes(_doc(rng, n_b)))
        j, contain, sample = fp.sketch_overlap(a, b)
        status, _, _ = fp.deciding_signals(0.0, False, j, contain, sample)
        assert status == "pass", (
            f"无关的 {n_a}句 vs {n_b}句 被判 {status}: "
            f"J={j:.3f} 包含={contain:.3f} 样本={sample}")


def test_tiny_sample_never_rejects_even_at_containment_one():
    """样本量不够时**完全不发言** —— 哪怕包含度是 1.0。

    实测: 100 字草稿 vs 6000 字历史时子域只剩 4 个元素, 完全无关的两篇也能
    撞出包含度 1.0。这一路在这里刻意 fail-open, 理由写在 CONTAIN_MIN_SAMPLE
    的注释里 —— 与本仓其它地方的 fail-closed 口径相反, 是有意的。
    """
    small = fp.CONTAIN_MIN_SAMPLE - 1
    assert fp.deciding_signals(0.0, False, 0.0, 1.0, small)[0] == "pass"
    assert fp.deciding_signals(0.0, False, 0.0, 1.0, 0)[0] == "pass"
    # 刚够到线就要发言, 免得阈值写成了 > 而不是 >=
    assert fp.deciding_signals(
        0.0, False, 0.0, 1.0, fp.CONTAIN_MIN_SAMPLE)[0] == "reject"


def test_containment_is_attributed_to_its_own_signal_name():
    """判定依据要报 ``contain``, 不能混进 ``ngram``。

    调用方靠 decided_by 把"撞的是哪一条"归到对的稿子上 —— 抄袭源和"用词最像
    的那篇"经常不是同一条。
    """
    status, reason, which = fp.deciding_signals(0.0, False, 0.05, 0.95, 100)
    assert status == "reject" and which == ["contain"], (status, which)
    assert "照搬" in reason or "%" in reason, reason


def test_old_three_signal_callers_are_unaffected():
    """只喂三路的老调用方行为不变 —— 默认参数等于"这一路没发言"。"""
    for sim, exact, j in [(0.0, False, 0.0), (0.95, False, 0.0),
                          (0.0, True, 0.0), (0.0, False, 0.5),
                          (0.86, False, 0.25)]:
        assert (fp.deciding_signals(sim, exact, j)
                == fp.deciding_signals(sim, exact, j, 0.0, 0))


# ══════════════════════════════════════════════════════════════════════
# 3 · 接进 check_drafts: 两条路径 + 老 RPC 的降级
# ══════════════════════════════════════════════════════════════════════

ME = "11111111-1111-1111-1111-111111111111"
PID = "aaaaaaaa-0000-0000-0000-000000000001"


def _sb(fingerprint_rows, rpc_impl=None):
    from tests.fakes import FakeClient
    return FakeClient(
        rows={"projects": [{"id": PID, "owner_id": ME}],
              "draft_fingerprints": fingerprint_rows},
        rpc_impl=rpc_impl or {})


def test_python_path_rejects_short_copy_of_long_history():
    """migrations/005 没跑时的 Python 路径也要拦下来 —— 两条路径结论必须一致。"""
    rng = random.Random(5)
    long_body = _doc(rng, 120)
    short_body = _prefix(long_body, 12)

    sb = _sb([{"id": "f1", "project_id": PID, "title": "去年那篇长测评",
               "opening_hash": "deadbeefdeadbeef",
               "ngram_hashes": sorted(fp.ngram_hashes(long_body)),
               "title_embedding": None}])
    out = core.check_drafts(sb, PID, [{"title": "全新的标题", "body": short_body}],
                            user_id=ME)
    r = out["results"][0]
    assert r["status"] == "reject", r
    assert r["decided_by"] == ["contain"], r
    assert r["collided_with"] == "去年那篇长测评", r
    assert r["signals"]["containment_sample"] >= fp.CONTAIN_MIN_SAMPLE, r["signals"]
    assert out["summary"]["containment_checked"] is True


def test_old_004_rpc_is_treated_as_not_run_not_as_zero():
    """只跑过 004 的库回不出包含度三列 —— 必须报"这一路没跑", 不能当成"没撞车"。

    这正是本条 finding 自己的形态: 一个信号静默没跑, 而返回值看起来一切正常。
    """
    rng = random.Random(6)
    long_body = _doc(rng, 120)
    short_body = _prefix(long_body, 12)

    # 老 RPC 的回执: 只有 004 那七列
    old_row = {"idx": 0, "best_sim": 0.0, "sim_title": None,
               "best_j": 0.08, "j_title": "去年那篇长测评",
               "open_exact": False, "open_title": None}
    sb = _sb([{"id": "f1", "project_id": PID, "title": "去年那篇长测评",
               "opening_hash": "deadbeefdeadbeef",
               "ngram_hashes": sorted(fp.ngram_hashes(long_body)),
               "title_embedding": None}],
             rpc_impl={"deskcore_check_drafts": lambda a: [old_row]})
    out = core.check_drafts(sb, PID, [{"title": "全新的标题", "body": short_body}],
                            user_id=ME)
    assert out["summary"]["containment_checked"] is False
    assert "containment_skipped_warning" in out["summary"], out["summary"]
    assert "005" in out["summary"]["containment_skipped_warning"]
    # 而且【不能】因此把这一路当成 0 分就判 pass 却什么都不说
    assert out["results"][0]["signals"]["containment_sample"] == 0


def test_new_005_rpc_drives_the_verdict():
    """新 RPC 回了包含度 → 结论由它驱动, 归因也是它。"""
    new_row = {"idx": 0, "best_sim": 0.0, "sim_title": None,
               "best_j": 0.08, "j_title": "另一篇用词像的",
               "open_exact": False, "open_title": None,
               "best_c": 0.97, "c_title": "被照搬的那篇长稿", "c_sample": 120}
    sb = _sb([], rpc_impl={"deskcore_check_drafts": lambda a: [new_row]})
    out = core.check_drafts(sb, PID, [{"title": "t", "body": "正文" * 200}],
                            user_id=ME)
    r = out["results"][0]
    assert r["status"] == "reject" and r["decided_by"] == ["contain"], r
    # 归因必须是包含度那一路的命中, 不是 Jaccard 那一路的
    assert r["collided_with"] == "被照搬的那篇长稿", r
    assert out["summary"]["containment_checked"] is True


# ══════════════════════════════════════════════════════════════════════
# 4 · 两份 SQL 必须同步(migrations/005 与 db.py::CREATE_TABLES_SQL)
# ══════════════════════════════════════════════════════════════════════

def _sql_sources():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    mig = (root / "migrations" / "005_deskcore_containment.sql").read_text("utf-8")
    import db
    return {"migrations/005": mig, "db.py::CREATE_TABLES_SQL": db.CREATE_TABLES_SQL}


def test_both_sql_copies_return_the_containment_columns():
    """加函数要两边都改 —— migrations/README.md 的规矩, 这里机械执行一遍。

    只改一边的后果是**静默**的: fresh install 走 CREATE_TABLES_SQL, 存量库走
    migrations, 两边行为不一样而没有任何地方会报错。
    """
    for name, sql in _sql_sources().items():
        for col in ("best_c", "c_title", "c_sample"):
            assert col in sql, f"{name} 里没有 {col} —— 两份 SQL 不同步了"


def test_neither_sql_copy_still_uses_the_unrestricted_jaccard():
    """钉住【被禁止的形态】: 直接对两个完整数组求交并比。

    ⚠️ 断言写成"不许出现旧写法"而不是"必须出现新写法" —— 后者会被一次正当的
    重构(改个 CTE 名、换个等价写法)误伤。第三批在这上面栽过一次, 见
    docs/audit-2026-08-23-full.md §0.4 末尾。
    """
    forbidden = "UNION SELECT unnest(f.ngram_hashes)"
    for name, sql in _sql_sources().items():
        assert forbidden not in sql, (
            f"{name} 还在用未受限的交并比 —— 那是 bottom-k sketch 上的有偏估计, "
            "审计 COR-014 换掉的就是它")
        # 受限子域的判据: 必须出现 t = LEAST(两边最大值) 这个构造
        assert "LEAST(ng_max" in sql, f"{name} 没有 t = min(两个 sketch 的最大值)"


def test_commit_rpc_falls_back_to_the_old_4_arg_signature():
    """migrations/005 把 commit RPC 从 4 参改成 6 参。只跑到 004 的库上会怎样?

    PostgREST 对"参数对不上"的报错文本里带 ``does not exist`` —— 正好被
    ``rpc_missing()`` 认成"函数压根不存在"。不处理的话, **整条原子写入路径静默
    退化成直插**: check/commit 之间的竞态窗口重新打开, 而日志只会说
    "migrations/001 还没跑?"(完全误导)。分两步升级的库上这一定会发生。

    要求: 先试 6 参, 找不到就回退 4 参, 两次都找不到才算真没跑迁移。
    """
    from deskcore import store
    seen = []

    class _Old:
        def rpc(self, name, args):
            seen.append(sorted(args))

            class _Q:
                @staticmethod
                def execute():
                    if "_contain_hard" in args:
                        raise RuntimeError(
                            "PGRST202 Could not find the function "
                            "autowriter.deskcore_commit_fingerprints(...) "
                            "in the schema cache")
                    return type("R", (), {"data": [{"idx": 0, "status": "written"}]})()
            return _Q()

    out = store.commit_fingerprints_atomic(
        _Old(), "p1", [{"title": "t"}], "u1", 0.35,
        contain_hard=0.6, contain_min_sample=15)
    assert out == [{"idx": 0, "status": "written"}], out
    assert len(seen) == 2, f"应该先试 6 参再回退 4 参, 实际发了 {len(seen)} 次"
    assert "_contain_hard" in seen[0] and "_contain_hard" not in seen[1]


def test_commit_rpc_returns_none_only_when_both_signatures_are_missing():
    from deskcore import store

    class _Gone:
        def rpc(self, name, args):
            class _Q:
                @staticmethod
                def execute():
                    raise RuntimeError("PGRST202 Could not find the function")
            return _Q()

    assert store.commit_fingerprints_atomic(
        _Gone(), "p1", [{"title": "t"}], "u1", 0.35,
        contain_hard=0.6, contain_min_sample=15) is None


def test_commit_rpc_never_swallows_a_real_error():
    """权限错 / 库故障必须原样上抛 —— 不能被当成"迁移没跑"降级成直插。"""
    from deskcore import store

    class _Boom:
        def rpc(self, name, args):
            class _Q:
                @staticmethod
                def execute():
                    raise RuntimeError("permission denied for function")
            return _Q()

    with pytest.raises(RuntimeError, match="permission denied"):
        store.commit_fingerprints_atomic(
            _Boom(), "p1", [{"title": "t"}], "u1", 0.35,
            contain_hard=0.6, contain_min_sample=15)


def test_intra_batch_short_copy_is_caught_too():
    """本批内互比也要走同一套 —— 同一批里长短稿并存很常见。"""
    rng = random.Random(8)
    long_body = _doc(rng, 120)
    short_body = _prefix(long_body, 12)
    sb = _sb([])
    out = core.check_drafts(sb, PID, [
        {"title": "长的那篇", "body": long_body},
        {"title": "短的那篇", "body": short_body},
    ], user_id=ME)
    r = out["results"][1]
    assert r["status"] == "reject", r
    assert r["collided_scope"] == "本批内", r
    assert r["collided_with"] == "长的那篇", r
