"""tvlink.match_note_by_item(2026-11-26): 同一个 item 的几版先并成一版, 再按 match_note 的档和线对。

tv-sync 主索引只有 item 的 best; 写手在对话里改几轮、发出去的是中间存过的一版时就对不上。旧版不能直接塞进主索引: 兄弟版本
几乎一样, 每一档都要领先一个差距才认, 它们互相领先不了 —— 本来对得上的会变成 ambiguous。这里钉住:
  1. 每个 item 只有一版的索引上, 它和 match_note 一模一样(随机生成的几百个索引逐个比整个 Match; 现有的匹配用例换成它也照过);
  2. 兄弟版本不再互相分不出, 挑的是和帖最像的那一版; 不同 item 的稿分不出照样 ambiguous;
  3. 并的时候的先后: 帖在稿里 → 稿在帖里 → 标题和帖一样 → 开头和帖一样 → 发布之前存的 → 存得晚的 → 排在前面的;
  4. ``skip`` 里的版本和不在索引里一模一样(tv-sync 拿它去掉发布之后才存的另一版); 并的时候不再把每个候选的全文归一化一遍。
文字全是编的(test_tv_edits 的线性同余句子: 句子之间没有共同的四字串)。
"""

from __future__ import annotations

import inspect
import random
from datetime import datetime, timedelta, timezone

import pytest

from deskcore import tvlink as L
from tests.test_tv_edits import FRESH, SENTS, _sentence

T0 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _iv(vid, item, body, *, title="存档的标题", days=0):
    return L.Version(version_id=vid, project_id="p1", title=title, body=body,
                     created_at=T0 + timedelta(days=days), item_id=item)


def _n(body, *, title="发布标题", days=2):
    return L.Note(note_id="n1", title=title, body=body, publish_time=T0 + timedelta(days=days))


def _b(*sents) -> str:
    return "。".join(sents) + "。"


X = _b(*SENTS[:10])                     # 第一版
X_MORE = _b(*SENTS[:10], FRESH[19])     # 第二版: 只在末尾多了一句(帖在两版里一样多)
REWRITE = _b(*FRESH[5:15])              # 定稿: 整篇重写了


def _family():
    """i1: v1(第一版) → v1b(多一句, 同一天) → v2(定稿, 重写)。"""
    return [_iv("v2", "i1", REWRITE, title="重写之后的标题", days=1),
            _iv("v1", "i1", X), _iv("v1b", "i1", X_MORE)]


# ── 1. 每个 item 一版时和 match_note 一模一样 ─────────────────────────────────────────────────────

_TITLES = ("秋招投了六十份", "群面被刷三次", "绩点二点八也拿到了终面", "露营忘带防潮垫")
_POOL = [_sentence(1000 + i) for i in range(300)]


def _random_case(rng: random.Random):
    """一个每个 item 只有一版的索引, 和一篇从其中一版改出来(或者别处写)的帖。专门凑: 同标题(F04)、借开头(F02)、
    正文一样的两个 item(按时间挑 / 分不出)、改得多(edited)、太短、时间窗外。"""
    stories = []
    for i in range(rng.randint(1, 6)):
        if stories and rng.random() < 0.3:
            base = rng.choice(stories)
            k = rng.randint(2, len(base))
            story = base[:k] + rng.sample(_POOL, rng.randint(0, 5))    # 和另一篇共用一截
        else:
            story = rng.sample(_POOL, rng.randint(5, 12))
        stories.append(story)
    versions = []
    for i, story in enumerate(stories):
        item = None if rng.random() < 0.2 else f"i{i}"
        title = rng.choice(_TITLES) if rng.random() < 0.7 else f"独一无二的标题{i}"
        versions.append(_iv(f"v{i}", item, _b(*story), title=title, days=rng.randint(-35, 35)))
        if rng.random() < 0.15:                                     # 正文一样的另一篇稿(别的 item)
            versions.append(_iv(f"v{i}dup", f"dup{i}", _b(*story), title=title, days=rng.randint(-3, 3)))
    rng.shuffle(versions)
    src = rng.choice(stories)
    mode = rng.choice(("same", "keep_tail", "fresh_front", "borrow_open", "outside", "short"))
    fresh = rng.sample(_POOL, 8)
    body = {"same": src,
            "keep_tail": src[:rng.randint(1, len(src))] + fresh[:rng.randint(0, 3)],
            "fresh_front": fresh[:rng.randint(1, 8)] + src[:rng.randint(1, len(src))],
            "borrow_open": src[:2] + fresh,
            "outside": fresh,
            "short": [src[0][:rng.randint(3, 15)]]}[mode]
    src_title = next(v.title for v in versions if v.body == _b(*src))
    title = src_title if rng.random() < 0.5 else rng.choice(_TITLES + ("别处起的标题",))
    note = L.Note(note_id="n", title=title, body=_b(*body), publish_time=T0 + timedelta(days=rng.randint(-40, 40)))
    return L.VersionIndex(versions), note


def test_on_an_index_with_one_version_per_item_it_is_exactly_match_note():
    rng = random.Random(20261126)
    kinds: dict[str, int] = {}
    for _ in range(800):
        idx, note = _random_case(rng)
        want = L.match_note(note, idx)
        assert L.match_note_by_item(note, idx) == want, (note, [v.version_id for v in idx.versions])
        kinds[want.kind] = kinds.get(want.kind, 0) + 1
    # 每一档都真走到了(不然这条用例什么都没证明)
    assert set(kinds) == {"body_exact", "title_exact", "fuzzy", "edited", "ambiguous", "unmatched"}, kinds
    assert min(kinds.values()) >= 10, kinds


def _matcher_tests():
    """现有的匹配用例里只调 match_note / match_all、不碰库的那些。"""
    from tests import test_tv_edits, test_tv_link_miss_score, test_tvlink, test_tvlink_prefix_tail
    out = []
    for mod in (test_tvlink, test_tv_edits, test_tvlink_prefix_tail, test_tv_link_miss_score):
        for name, fn in sorted(vars(mod).items()):
            if not name.startswith("test_") or not inspect.isfunction(fn) or inspect.signature(fn).parameters:
                continue
            src = inspect.getsource(fn)
            if ("match_note(" in src or "match_all(" in src) and "core." not in src and "_client(" not in src:
                out.append(pytest.param(fn, id=f"{mod.__name__.rsplit('.', 1)[-1]}::{name}"))
    return out


_MATCHER_TESTS = _matcher_tests()


def test_the_existing_matcher_cases_are_found():
    assert len(_MATCHER_TESTS) >= 25


@pytest.mark.parametrize("fn", _MATCHER_TESTS)
def test_existing_matcher_cases_hold_for_the_by_item_matcher(fn, monkeypatch):
    # 那些用例的索引每个 item 一版(各自的 item_id / 没有 item_id): 换成按 item 并的匹配器, 断言照样成立
    monkeypatch.setattr(L, "match_note", L.match_note_by_item)
    fn()


# ── 2. 兄弟版本不再互相分不出 ───────────────────────────────────────────────────────────────────

def test_the_best_alone_does_not_match_and_flat_siblings_are_ambiguous():
    """前提(为什么要另写一个匹配器): 只有定稿 → 对不上; 旧版直接塞进去 → 两版分不出。"""
    assert L.match_note(_n(X), L.VersionIndex(_family()[:1])).kind == "unmatched"
    m = L.match_note(_n(X), L.VersionIndex(_family()))
    assert m.kind == "ambiguous" and {c["version_id"] for c in m.candidates} == {"v1", "v1b"}


def test_siblings_resolve_to_the_version_the_post_came_from():
    m = L.match_note_by_item(_n(X), L.VersionIndex(_family()))
    assert (m.kind, m.version.version_id, m.score, m.lag_days) == ("body_exact", "v1", 1.0, 2)
    # 帖是第二版(多了那一句): 帖在第二版里多, 挑第二版
    m = L.match_note_by_item(_n(X_MORE), L.VersionIndex(_family()))
    assert (m.kind, m.version.version_id) == ("body_exact", "v1b")


def test_a_different_item_with_the_same_text_is_still_ambiguous_one_candidate_per_item():
    idx = L.VersionIndex(_family() + [_iv("v3", "i3", X)])
    m = L.match_note_by_item(_n(X), idx)
    assert m.kind == "ambiguous" and m.version is None
    assert [c["version_id"] for c in m.candidates] == ["v1", "v3"], "一个 item 一个候选"


@pytest.mark.parametrize("keep,new,kind", [(7, 3, "fuzzy"), (5, 6, "edited")])
def test_siblings_in_the_content_tiers(keep, new, kind):
    note = _n(_b(*FRESH[:new], *SENTS[:keep]))               # 开头换了: 走内容那一路
    assert L.match_note(note, L.VersionIndex(_family())).kind == "ambiguous"
    m = L.match_note_by_item(note, L.VersionIndex(_family()))
    assert m.kind == kind and m.version.version_id == "v1"
    assert m.score == pytest.approx(L.overlap(note, _iv("v1", "i1", X)))
    # 别的 item 也一样像: 照样分不出, 候选一个 item 一个
    m = L.match_note_by_item(note, L.VersionIndex(_family() + [_iv("v3", "i3", X)]))
    assert m.kind == "ambiguous" and {c["version_id"] for c in m.candidates} == {"v1", "v3"}


def test_siblings_in_the_title_tier():
    fam = [_iv("v2", "i1", REWRITE, title="重写之后的标题", days=1),
           _iv("v1", "i1", X, title="同一个标题"), _iv("v1b", "i1", X_MORE, title="同一个标题")]
    note = _n(_b(*FRESH[:2], *SENTS[:8]), title="同一个标题")
    assert L.match_note(note, L.VersionIndex(fam)).kind == "ambiguous"
    m = L.match_note_by_item(note, L.VersionIndex(fam))
    assert (m.kind, m.version.version_id) == ("title_exact", "v1")


def test_title_only_siblings_stay_a_weak_title_hold_with_one_candidate():
    fam = [_iv("v1", "i1", X, title="同一个标题"), _iv("v1b", "i1", X_MORE, title="同一个标题")]
    note = _n(_b(*FRESH[:10]), title="同一个标题")             # 标题一样、正文毫不相干
    flat = L.match_note(note, L.VersionIndex(fam))
    assert L.weak_title_only(flat) and len(flat.candidates) == 2
    m = L.match_note_by_item(note, L.VersionIndex(fam))
    assert L.weak_title_only(m) and len(m.candidates) == 1 and m.score == flat.score


def test_an_unrelated_post_keeps_the_highest_overlap_of_every_version():
    note = _n(_b(*_POOL[:3], SENTS[3], *_POOL[3:9]))            # 借了一句
    idx = L.VersionIndex(_family())
    m = L.match_note_by_item(note, idx)
    assert m.kind == "unmatched" and m.score == L.match_note(note, idx).score > 0


# ── 3. 并的时候留哪一版 ─────────────────────────────────────────────────────────────────────────

def test_same_text_prefers_the_version_saved_before_publication():
    fam = [_iv("v1", "i1", X, days=0), _iv("v1late", "i1", X, days=5)]     # 发了(第 2 天)之后又存了一次
    assert L.match_note_by_item(_n(X), L.VersionIndex(fam)).version.version_id == "v1"
    assert L.match_note_by_item(_n(X), L.VersionIndex(fam[::-1])).version.version_id == "v1"


def test_same_text_both_before_publication_prefers_the_later_save():
    fam = [_iv("v1", "i1", X, days=0), _iv("v1again", "i1", X, days=1)]
    m = L.match_note_by_item(_n(X), L.VersionIndex(fam))
    assert m.version.version_id == "v1again" and m.lag_days == 1


def test_a_title_like_the_posts_breaks_a_tie_in_text():
    fam = [_iv("v1", "i1", X, title="改之前的标题", days=1), _iv("v1t", "i1", X, title="发布标题", days=0)]
    assert L.match_note_by_item(_n(X), L.VersionIndex(fam)).version.version_id == "v1t"


def test_a_full_tie_keeps_the_one_listed_first():
    """tv-sync 合索引时定稿那一版排前面: 分不出就留定稿(第二遍不认, 保守的那一边)。"""
    a, b = _iv("best", "i1", X), _iv("old", "i1", X)
    assert L.match_note_by_item(_n(X), L.VersionIndex([a, b])).version.version_id == "best"
    assert L.match_note_by_item(_n(X), L.VersionIndex([b, a])).version.version_id == "old"


# ── 4. skip: 当那几版不在 ─────────────────────────────────────────────────────────────────────────

def _with_siblings(rng: random.Random, idx: L.VersionIndex) -> list[L.Version]:
    """给一部分 item 加兄弟版本: 末尾多一句 / 少一句 / 换一句, 存的日子前后错开。"""
    out = list(idx.versions)
    for v in idx.versions:
        if v.item_id is None or rng.random() < 0.5:
            continue
        sents = [x for x in v.body.split("。") if x]
        for k in range(rng.randint(1, 2)):
            alt = rng.choice((sents + [rng.choice(_POOL)], sents[:-1] or sents, sents[:-1] + [rng.choice(_POOL)]))
            out.append(L.Version(version_id=f"{v.version_id}s{k}", project_id="p1",
                                 title=v.title if rng.random() < 0.7 else "兄弟版本的标题", body=_b(*alt),
                                 created_at=v.created_at + timedelta(days=rng.randint(-4, 4)), item_id=v.item_id))
    rng.shuffle(out)
    return out


def test_skipped_versions_behave_exactly_as_if_they_were_not_in_the_index():
    rng = random.Random(20261127)
    hit = 0
    for _ in range(500):
        idx, note = _random_case(rng)
        versions = _with_siblings(rng, idx)
        skip = frozenset(v.version_id for v in versions if rng.random() < 0.3)
        want = L.match_note_by_item(note, L.VersionIndex([v for v in versions if v.version_id not in skip]))
        assert L.match_note_by_item(note, L.VersionIndex(versions), skip=skip) == want, (note, sorted(skip))
        hit += want.version is not None
    assert hit >= 100, hit


def test_a_copy_back_sibling_hides_the_version_saved_before_publication_unless_skipped():
    """i1: v1 发布前存的, v1cb 是发了以后把帖(多一句)抄回来存的, v2 定稿重写了。不去掉 v1cb, 并的时候它更像帖, 挑的是它
    (lag < 0, tv-sync 不认, 整个 item 就对不上); 去掉之后挑 v1。"""
    pub = X_MORE
    fam = [_iv("v2", "i1", REWRITE, title="重写之后的标题", days=5), _iv("v1", "i1", X, days=0),
           _iv("v1cb", "i1", pub, days=3)]
    m = L.match_note_by_item(_n(pub), L.VersionIndex(fam))
    assert (m.version.version_id, m.lag_days) == ("v1cb", -1)
    m = L.match_note_by_item(_n(pub), L.VersionIndex(fam), skip=frozenset({"v1cb"}))
    assert (m.kind, m.version.version_id, m.lag_days) == ("body_exact", "v1", 2)


def test_collapsing_reads_the_keys_the_index_already_has(monkeypatch):
    """打平的几版比标题 / 开头时用建索引时记下的键(VersionIndex.keys_of), 不再把每一版的全文归一化一遍(复审 11-26: 第二遍
    原来慢了二三十倍, 几乎都花在这里)。帖自己的开头 / 标题算一次。"""
    fam = [_iv(f"v{k}", "i1", X, days=k) for k in range(4)]            # 一模一样的四版: 全靠后面几样定
    idx = L.VersionIndex(fam)
    calls = []
    real = L.prefix_key
    monkeypatch.setattr(L, "prefix_key", lambda text: calls.append(text) or real(text))
    note = _n(X, days=5)
    (kept,) = L._collapse(note, L.grams(note.body), fam, idx)
    assert kept.version_id == "v3" and calls == [note.body]
    assert idx.keys_of(fam[0]) == (real(X), L.title_key("存档的标题"))

