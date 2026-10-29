"""写作台 ↔ TV 的稿子对照 —— 纯匹配逻辑, 不碰库。

背景(2026-09-17): TV(truth_vault.notes)里 5966 条已发笔记, 带写作台 lineage 的
是 0 条。原设计让运营把 export_drafts 导出的六个 ID 列手抄进飞书, 三周零匹配。
两边在同一个库里、内容都是写作台产的, 所以改成**在库里按内容对**:

  1. 正文前 40 字(去标点)相同           → body_exact
  2. 标题(去标点)相同                    → title_exact
  3. 都对不上, 在时间窗内按四字串包含度  → fuzzy(发布稿是从这版改出来的)
  4. 包含度 0.30~0.60, 而且最像的那版比第二像的高出 0.15 以上 → edited(人改得多, 但还是这一篇)
  5. 多个候选分不出                      → ambiguous, 留候选给人看, 不硬猜
  6. 一个都没有                          → unmatched(→ 补录进指纹库)

edited 这一档的依据(2026-10-19 实测, D-120 / W-23): 9-05 以来对不上、被补录的笔记里, 包含度落在
0.30~0.60 的有十几篇, 其中领先第二名 ≥0.15 的(0.53 对 0.09、0.51 对 0.06、0.41 对 0.02、0.32 对
0.17)人一看就是同一篇改出来的; 领先不够的多是同一个故事写了两三版(Hatherine), 分不出是哪一版发的,
进 ambiguous 等周检; 0.30 以下的是共用产品话术的不同稿子。句子对齐、向量都试过/想过: 同故事多版本
时句子对齐也分不开, 而"领先幅度"能分开。

实测口径(2026-09-17 的库):
  · TV 存的是全文: 百健士对上的 143 篇里 115 篇正文逐字相同, 其余只多了话题标签;
  · 发布时间 − 入库时间: 中位数 1~3 天, 九成在 13 天内; 百健士有 101 篇是先发
    后补录(负 4~20 天) —— 所以窗口两头都开;
  · 途鸽的发布稿平均 305 字, 写作台那版 527 字 —— 发之前改得很狠, 精确对不上,
    要靠包含度。

⚠️ 这里的四字串是**完整集合**, 不是 fingerprint.ngram_hashes 的 bottom-k
sketch —— 两个 sketch 之间的包含度要走 sketch_overlap 估计, 而这里两边全文都
在手上, 直接算就是精确值, 没必要绕。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from . import fingerprint as fp

PREFIX_CHARS = 40           # 正文前多少个(去标点后的)字算"开头相同"
MIN_PREFIX = 20             # 正文短于这个数不用开头判 —— 太短的开头到处撞
MIN_TITLE = 4               # 标题短于这个数不用标题判
WINDOW_BEFORE_DAYS = 30     # 发布早于入库多少天内仍算(事后补录的稿子)
WINDOW_AFTER_DAYS = 30      # 入库后多少天内发布仍算
FUZZY_CONTAIN = 0.60        # 发布稿的四字串有六成来自这一版 → 认
FUZZY_MIN_GRAMS = 15        # 与 fp.CONTAIN_MIN_SAMPLE 同口径: 样本太小不判
FUZZY_MARGIN = 0.10         # 最佳与次佳差不到这个数 → ambiguous
EDITED_FLOOR = 0.30         # 包含度到这个数才考虑"改得多的同一篇"
EDITED_MARGIN = 0.15        # 而且要比第二像的那版高出这么多, 否则是同故事多版本, 分不出

_TITLE_RE = re.compile(r"【标题】\s*(.*?)\s*【正文】", re.S)
_BODY_RE = re.compile(r"【正文】\s*(.*)$", re.S)
_TITLE_ONLY_RE = re.compile(r"【标题】\s*(.*)$", re.S)
# 小红书话题: "#秋招" / "#秋招[话题]#" / 连写 "#a#b"。
# ⚠️ 和 validator.topic_tag_matches(数字数、交付稿摘话题、查话题的规则用)两头不一样: 网址里的 #片段、"C#入门" 的 #入门
# 这里也剥(比那边多); 连写的 "#a[话题]#b[话题]#" 这里只剥前一个 —— 中间那个 # 被当成前一个的收尾吃掉, "b[话题]#" 留在
# 文本里、进了改稿差异的内容(比那边少)。这里只是对内容 / 算改稿差异之前去掉标签, 不跟着改: 改了 edit_diff 的输出就变了,
# DIFF_REV 得跟着升。
_TAG_RE = re.compile(r"#[^\s#\[\]]+(?:\[话题\]#?)?")


def parse_raw(raw: str | None) -> tuple[str, str]:
    """TV 的 raw_content(「【标题】… 【正文】…」)→ (标题, 正文)。

    没有标记的整段当正文、标题留空 —— 指纹主要靠正文, 丢正文等于白对。
    """
    text = (raw or "").replace("\r\n", "\n")
    mb = _BODY_RE.search(text)
    if mb:
        mt = _TITLE_RE.search(text)
        return (mt.group(1).strip() if mt else ""), mb.group(1).strip()
    mt = _TITLE_ONLY_RE.search(text)
    if mt:
        # 有【标题】没【正文】(实测 途鸽 127 条里 7 条): 标记后第一行是标题,
        # 其余是正文 —— 整段当标题会让正文为空, 对不上也补不了。
        rest = mt.group(1)
        first, _, tail = rest.partition("\n")
        return first.strip(), tail.strip()
    return "", text.strip()


def strip_hashtags(text: str | None) -> str:
    """去掉话题标签。TV 的正文带标签, 写作台的稿子也可能带(10-20: 写手的稿子末尾常有一行 #话题);
    同项目的稿子都以同一串标签结尾, 不剥的话四字串会互相误撞, 补录进指纹库更是会让整个项目互判重复。"""
    out = _TAG_RE.sub("", text or "")
    lines = [ln.rstrip() for ln in out.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


def prefix_key(body: str) -> str:
    n = fp.normalize(body)
    return n[:PREFIX_CHARS] if len(n) >= MIN_PREFIX else ""


def title_key(title: str) -> str:
    n = fp.normalize(title)
    return n if len(n) >= MIN_TITLE else ""


def grams(text: str, n: int = 4) -> set[str]:
    s = fp.normalize(text)
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _as_dt(v) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=timezone.utc)
    s = str(v).strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        try:
            d = datetime.fromisoformat(s[:10])
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@dataclass
class Version:
    version_id: str
    project_id: str
    title: str
    body: str
    created_at: datetime | None
    item_id: str | None = None

    @classmethod
    def from_row(cls, r: dict) -> "Version":
        return cls(version_id=str(r["version_id"]), project_id=str(r.get("project_id") or ""),
                   title=(r.get("title") or "").strip(), body=(r.get("body") or "").strip(),
                   created_at=_as_dt(r.get("created_at")), item_id=r.get("item_id"))


@dataclass
class Note:
    note_id: str
    title: str
    body: str                # 已剥话题标签
    publish_time: datetime | None
    tier: str | None = None
    tv_version_id: str | None = None   # TV 自己已经带的 lineage(有就不用我们对)

    @classmethod
    def from_row(cls, r: dict) -> "Note":
        title, body = parse_raw(r.get("raw_content"))
        # 老数据 title/body 列可能有值、raw_content 没有标记 —— 列优先
        title = (r.get("title") or "").strip() or title
        body = (r.get("body") or "").strip() or body
        return cls(note_id=str(r["note_id"]), title=title, body=strip_hashtags(body),
                   publish_time=_as_dt(r.get("publish_time")), tier=r.get("tier"),
                   tv_version_id=r.get("source_autowriter_version_id"))


@dataclass
class Match:
    kind: str                       # body_exact / title_exact / fuzzy / edited / ambiguous / unmatched
    version: Version | None = None
    score: float | None = None
    lag_days: int | None = None
    candidates: list[dict] = field(default_factory=list)


# TV 的 publish_time 是飞书的「发布日期」: 北京时间那一天的 0 点, 存成不带时区的时间戳、读进来当 UTC(10-29 查 60 天
# 2542 篇里 2524 篇是 16:00 UTC)。所以两边都换成北京日历日再相减(W-38 复审, 2026-10-29): 按 UTC 日期减, 北京当天写、当天
# 发的稿子算成 −1(发了之后才存的), 改稿差异和「接住」都不认它 —— 10-29 生产库 lag 在 −2..2 的 264 条对照里 26 条是这样。
_CN_TZ = timezone(timedelta(hours=8))


def _cn_day(t: datetime) -> date:
    return t.astimezone(_CN_TZ).date()


def lag_days(note: Note, v: Version) -> int | None:
    """发布那天 − 写作台这一版那天, 都按北京日历日。"""
    if note.publish_time is None or v.created_at is None:
        return None
    return (_cn_day(note.publish_time) - _cn_day(v.created_at)).days


def in_window(lag: int | None) -> bool:
    return lag is None or -WINDOW_BEFORE_DAYS <= lag <= WINDOW_AFTER_DAYS


class VersionIndex:
    """一个 TV 项目对应的全部写作台版本(可能来自多个写作台项目)。"""

    def __init__(self, versions: Iterable[Version]):
        self.versions: list[Version] = list(versions)
        self.by_prefix: dict[str, list[Version]] = {}
        self.by_title: dict[str, list[Version]] = {}
        self._grams: dict[str, set[str]] = {}
        for v in self.versions:
            pk = prefix_key(v.body)
            if pk:
                self.by_prefix.setdefault(pk, []).append(v)
            tk = title_key(v.title)
            if tk:
                self.by_title.setdefault(tk, []).append(v)

    def grams_of(self, v: Version) -> set[str]:
        g = self._grams.get(v.version_id)
        if g is None:
            g = self._grams[v.version_id] = grams(v.body)
        return g

    def containment(self, note_grams: set[str], v: Version) -> float:
        if not note_grams:
            return 0.0
        return len(note_grams & self.grams_of(v)) / len(note_grams)


def _cand(note: Note, v: Version, score: float | None) -> dict:
    return {"version_id": v.version_id, "project_id": v.project_id, "title": v.title[:40],
            "score": None if score is None else round(score, 3), "lag_days": lag_days(note, v)}


def _pick(note: Note, cands: list[Version], index: VersionIndex) -> tuple[Version | None, float | None, list[dict]]:
    """多个候选里挑一个: 先按包含度, 差距够大就认; 再按时间窗内最近; 否则放弃。"""
    ng = grams(note.body)
    scored = sorted(((index.containment(ng, v), v) for v in cands), key=lambda t: -t[0])
    listed = [_cand(note, v, s) for s, v in scored]
    best_s, best_v = scored[0]
    if len(scored) == 1 or best_s - scored[1][0] >= FUZZY_MARGIN:
        return best_v, best_s, listed
    # 包含度分不出(通常是同一篇的两个近似版本): 时间窗内、离发布最近的那一版
    score_of = {v.version_id: s for s, v in scored}
    tied = [v for s, v in scored if best_s - s < FUZZY_MARGIN]
    inside = [(abs(lag_days(note, v)), v) for v in tied
              if lag_days(note, v) is not None and in_window(lag_days(note, v))]
    if inside:
        inside.sort(key=lambda t: t[0])
        if len(inside) == 1 or inside[0][0] != inside[1][0]:
            chosen = inside[0][1]
            return chosen, score_of[chosen.version_id], listed    # 记它自己的分, 不是最高分
    return None, best_s, listed


def match_note(note: Note, index: VersionIndex) -> Match:
    pk = prefix_key(note.body)
    exact = list(index.by_prefix.get(pk, ())) if pk else []
    kind = "body_exact"
    if not exact:
        tk = title_key(note.title)
        exact = list(index.by_title.get(tk, ())) if tk else []
        kind = "title_exact"
    if exact:
        if len(exact) == 1:
            v = exact[0]
            return Match(kind, v, 1.0 if kind == "body_exact" else
                         index.containment(grams(note.body), v), lag_days(note, v))
        v, score, listed = _pick(note, exact, index)
        if v is not None:
            return Match(kind, v, score, lag_days(note, v), listed)
        return Match("ambiguous", None, score, None, listed)

    ng = grams(note.body)
    if len(ng) < FUZZY_MIN_GRAMS:
        return Match("unmatched")
    scored = sorted(((index.containment(ng, v), v) for v in index.versions
                     if in_window(lag_days(note, v))), key=lambda t: -t[0])
    if not scored or scored[0][0] < EDITED_FLOOR:
        return Match("unmatched")
    best_s, best_v = scored[0]
    if best_s < FUZZY_CONTAIN:
        second = scored[1][0] if len(scored) > 1 else 0.0
        listed = [_cand(note, v, s) for s, v in scored[:5] if s >= EDITED_FLOOR - EDITED_MARGIN]
        if best_s - second >= EDITED_MARGIN:
            return Match("edited", best_v, best_s, lag_days(note, best_v), listed)
        return Match("ambiguous", None, best_s, None, listed)
    if len(scored) > 1 and best_s - scored[1][0] < FUZZY_MARGIN:
        listed = [_cand(note, v, s) for s, v in scored[:5] if s >= FUZZY_CONTAIN - FUZZY_MARGIN]
        return Match("ambiguous", None, best_s, None, listed)
    return Match("fuzzy", best_v, best_s, lag_days(note, best_v))


def match_all(notes: Iterable[Note], index: VersionIndex) -> dict[str, Match]:
    return {n.note_id: match_note(n, index) for n in notes}


# ── 查重时记下的底稿(2026-10-29, W-38) ─────────────────────────────────────────────────────────
# 写手没有「存档」这一步(D-120): 模型没调 score_drafts 的稿子, 写作台只在 check_drafts 那一刻见过它(draft_snapshots)。
# 对不上写作台版本的帖, tv-sync 再拿这些底稿对一次, 对上了就把底稿转成真的版本(建 item / version、写指纹、销牌)——
# 那是实打实写进库的, 所以比 match_note 严:
#   · 帖要有发布日期, 底稿要早于帖: 发布那天 − 第一次查重那天(北京日历日)在 0..WINDOW_AFTER_DAYS 天; 晚于发布的不认
#     (DIFF_REV 2 / 判据 C 的"接住"同一个口径, 认了也算不了改稿差异);
#   · 开头 / 标题一样也要包含度到 EDITED_FLOOR(match_note 的精确档是不看包含度的); 改得多的那一档(edited)要到
#     SNAPSHOT_EDITED_FLOOR —— W-34 判"整篇换了"的线是 0.30, 擦着线转正的对照, 运营在飞书里再改几句就会被挪走、补录成副本;
#   · 分不出时, 只在打平的几份是同一篇的前后几稿(互相重合 SNAPSHOT_SAME_DRAFT 以上)才挑一份: 挑和这篇帖两头都最像的
#     (帖在稿里 + 稿在帖里), 一样像取最晚查的; 打平的里面有别的稿子 → ambiguous, 不硬猜;
#   · 存过档的版本也算对手(``main``): 主索引说对不上, 可时间窗里有存档版本离最像的底稿不到一个差距(同一个故事写了两版,
#     一版存了档、一版只查了重) → ambiguous。主索引自己遇到这种也是 ambiguous。
SNAPSHOT_SAME_DRAFT = 0.60
SNAPSHOT_EDITED_FLOOR = 0.45


def snapshot_lag(note: Note, v: Version) -> int | None:
    """发布那天 − 第一次查重那天(北京日历日)。帖没有发布日期的不认(回 None): TV 读到它的时刻可能比发布晚很久
    (新接的表), 拿它当上限会把发出去之后才写的稿认成来源。"""
    return lag_days(note, v)


def _same_draft(a: Version, b: Version, index: VersionIndex, other: VersionIndex | None = None) -> bool:
    ga, gb = index.grams_of(a), (other or index).grams_of(b)
    small = min(len(ga), len(gb))
    return small > 0 and len(ga & gb) / small >= SNAPSHOT_SAME_DRAFT


def match_snapshot(note: Note, snaps: list[Version], index: VersionIndex,
                   main: VersionIndex | None = None) -> Match:
    """这篇帖是不是从哪份底稿发出去的。``snaps`` 每份一个 Version(created_at = 第一次查重的时刻), ``index`` 是它们自己的
    VersionIndex(只用来缓存四字串); ``main`` 是存过档的版本的索引(对手, 见上)。回 body_exact / title_exact / fuzzy /
    edited(带那份底稿)、ambiguous(候选里是底稿的 id)或 unmatched。"""
    ng = grams(note.body)
    if len(ng) < FUZZY_MIN_GRAMS:
        return Match("unmatched")
    eligible = [v for v in snaps if (lag := snapshot_lag(note, v)) is not None and 0 <= lag <= WINDOW_AFTER_DAYS]
    scored = sorted(((index.containment(ng, v), v) for v in eligible), key=lambda t: -t[0])
    if not scored or scored[0][0] < EDITED_FLOOR:
        return Match("unmatched")
    best_s, best_v = scored[0]
    if best_s < FUZZY_CONTAIN and best_s < SNAPSHOT_EDITED_FLOOR:
        return Match("unmatched")
    margin = FUZZY_MARGIN if best_s >= FUZZY_CONTAIN else EDITED_MARGIN
    close = [(s, v) for s, v in scored[1:] if best_s - s < margin]
    listed = [_cand(note, v, s) for s, v in scored[:5] if s >= EDITED_FLOOR - EDITED_MARGIN]
    rivals = []
    if main is not None:
        rivals = sorted(((main.containment(ng, v), v) for v in main.versions if in_window(lag_days(note, v))),
                        key=lambda t: -t[0])
        rivals = [(s, v) for s, v in rivals[:5] if best_s - s < margin]
    if any(not _same_draft(best_v, v, index) for _, v in close) or rivals:
        return Match("ambiguous", None, best_s, None,
                     (listed + [_cand(note, v, s) for s, v in rivals])[:5])

    def back(v: Version) -> float:                      # 稿在帖里: 一稿比另一稿多出来的那段, 帖里没有就分得开
        g = index.grams_of(v)
        return len(g & ng) / len(g) if g else 0.0

    group = [(best_s, best_v)] + [(s, v) for s, v in close if s >= SNAPSHOT_EDITED_FLOOR]
    score, chosen = max(group, key=lambda t: (t[0] + back(t[1]), t[1].created_at))
    pk, tk = prefix_key(note.body), title_key(note.title)
    kind = ("body_exact" if pk and pk == prefix_key(chosen.body) else
            "title_exact" if tk and tk == title_key(chosen.title) else
            "fuzzy" if score >= FUZZY_CONTAIN else "edited")
    return Match(kind, chosen, score, lag_days(note, chosen), listed if close else [])


# ── 对上之后 TV 那边的正文还是不是那一篇(2026-10-27, W-34) ───────────────────────────────────
# 运营在飞书里把一条笔记整篇换成另一篇(note_id 不变), 对照、写回 TV 的血缘和改动差异就一直挂在原来那版上。D-129 那 14 条
# 和原版的重叠 ≤ 0.06; 10-27 生产库 501 条读得到的对照里, 没换过的最低 0.481(那条 edited), 中间 0.078~0.481 一条都没有 ——
# EDITED_FLOOR(0.30) 两边都有很宽的余量。指纹只用来判"改没改过字", 不参与匹配。

TITLE_SAME_FLOOR = 0.5      # 写作台那版只有标题时: 标题的四字串过半还在, 就算还是那一篇(改了一两个字)


def _h8(s: str) -> str:
    return hashlib.sha1(fp.normalize(s).encode("utf-8")).hexdigest()[:8]


def text_fp(note: Note) -> str:
    """TV 这一篇现在的指纹, 16 位: 前 8 位是标题、后 8 位是剥了话题的正文(各自 fp.normalize —— 去标点、空白 —— 后的
    sha1 前 8 位)。只改标点、空格、话题标签的不算改过 —— 运营常这样修, 不该让每条对照每晚重核一遍。标题和正文分开记,
    是为了认出"只改了标题"(``body_unchanged``): 那不是换了一篇(review on aw#128)。"""
    return _h8(note.title) + _h8(note.body)


def body_unchanged(old_fp: str | None, new_fp: str) -> bool:
    """两个 text_fp 的正文那一半一样(只改了标题)。旧格式 / 没有旧指纹的不算。"""
    return bool(old_fp) and len(old_fp) == len(new_fp) == 16 and old_fp[8:] == new_fp[8:]


def overlap(note: Note, v: Version) -> float:
    """笔记现在的四字串有多少在这一版里(与 match_note 的包含度同一口径)。"""
    ng = grams(note.body)
    return len(ng & grams(v.body)) / len(ng) if ng else 0.0


def still_same(note: Note, v: Version) -> bool:
    """改过字之后, 这条笔记还是当初对上 v 的那一篇吗? 只拦整篇换掉的, 改几句、删一段的都算还是。

    · 开头 40 字(去标点)和 v 一样 → 是;
    · v 有正文(四字串够 FUZZY_MIN_GRAMS): 笔记的四字串至少 EDITED_FLOOR 在 v 里 → 是;
    · v 只有标题(HATHERINE 59 条 title_exact 连的是没正文的稿): 标题(去标点)一样, 或标题的四字串至少 TITLE_SAME_FLOOR
      在 v 的标题里(改了一两个字) → 是。
    笔记没正文的不判(调用方先挡掉): 飞书一时读空, 不能当成换了一篇。"""
    pk = prefix_key(note.body)
    if pk and pk == prefix_key(v.body):
        return True
    vg = grams(v.body)
    if len(vg) >= FUZZY_MIN_GRAMS:
        ng = grams(note.body)
        return bool(ng) and len(ng & vg) / len(ng) >= EDITED_FLOOR
    tk = title_key(note.title)
    if not tk:
        return False
    if tk == title_key(v.title):
        return True
    nt = grams(note.title)
    return bool(nt) and len(nt & grams(v.title)) / len(nt) >= TITLE_SAME_FLOOR


# ── 人在发布前改了什么(2026-10-19, D-120 / W-23) ─────────────────────────────────────────────
# 每一对对上的(写作台那版 → 发出去的笔记)算一条差异: 标题改没改、删了哪些句子、加了哪些句子、长度、品牌名
# 提了几次。纯计算, 不碰库; 存库和按项目汇总在 core。哪怕只改了几个字也记 —— 汇总时才看出规律。
_SENT_SPLIT = re.compile(r"[。！？!?；;\n…~～]+")
SENT_MIN_CHARS = 6          # 归一化后短于这个的不算一句(表情、"哈哈哈")
SENT_ALIGN = 0.5            # 一句的四字串有一半以上在另一边出现, 就算"这句还在"
DIFF_KEEP_SENTENCES = 8
DIFF_SENTENCE_CHARS = 80
# 差异的口径版本: 改了 edit_diff 的算法就 +1。tv-sync 把旧口径的行按新口径重算, 摘要只读当前口径的。
# 1 (10-20): 写作台那版也剥话题标签。TV 那版读进来就剥了(Note.from_row), 写手的稿子却以一行 #话题 结尾 ——
#   首晚 440 条差异里 125 条"改过"只差这一行, 品牌次数也跟着少一次, 摘要会把"发布时删了品牌"教给写手。
# 2 (10-20): 写作台那版晚于发布的(lag_days < 0, 发了之后才抄回写作台)不再算差异(core._diffable)。它们的旧行
#   留在旧口径上, 不重算、摘要也不读 —— 不删库, 删数由人在 SQL 编辑器里做。
DIFF_REV = 2


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text or "")
            if len(fp.normalize(s)) >= SENT_MIN_CHARS]


def _kept(sentence: str, other_grams: set[str]) -> bool:
    g = grams(sentence)
    return bool(g) and len(g & other_grams) / len(g) >= SENT_ALIGN


def _edited_pairs(draft_sents: list[str], published_sents: list[str], removed: list[str]) -> list[list[str]]:
    """还在、但里面改了字的句子: 稿子里每一句没被删的, 找发出去那边四字串重合最多的一句, 归一化后不一样就记
    ``[原句, 改后]``。长句里改几个字两边都还过半, 进不了删 / 加 —— 这些小改正是要看的(codex review on aw#107)。"""
    gone = set(removed)
    exact = {fp.normalize(p) for p in published_sents}
    pg = [(p, grams(p)) for p in published_sents]
    out, seen = [], set()
    for s in draft_sents:
        ns = fp.normalize(s)
        if s in gone or ns in exact or not pg:
            continue
        g = grams(s)
        best, overlap = max(((p, len(g & q)) for p, q in pg), key=lambda t: t[1])
        if overlap and (s, best) not in seen:
            seen.add((s, best))
            out.append([s, best])
    return out


def edit_diff(draft_title: str, draft_body: str, published_title: str, published_body: str,
              *, brand: str = "") -> dict:
    """写作台那版 → 发出去的那版: 改了什么。

    句子按标点切; 一句在另一边能找到一半以上的四字串就算"还在", 否则算删掉 / 新加的; 还在但字不一样的记成
    改写对(原句 → 改后)。只回前几句(每句截断), 计数是全量的。``brand`` 给了就数两边各提了几次(不分大小写)。
    ``changed`` 看的是归一化后的整篇: 只调了句子顺序、或者改动零碎到对不上哪一句, 也算改过。
    话题标签两边都先剥掉(``strip_hashtags``, 见 DIFF_REV 的说明), 品牌只数正文里提的。"""
    draft_body, published_body = strip_hashtags(draft_body), strip_hashtags(published_body)
    dg, pg = grams(draft_body), grams(published_body)
    dsents, psents = sentences(draft_body), sentences(published_body)
    removed = [s for s in dsents if not _kept(s, pg)]
    added = [s for s in psents if not _kept(s, dg)]
    edited = _edited_pairs(dsents, psents, removed)
    out = {
        "title_changed": fp.normalize(draft_title) != fp.normalize(published_title),
        "draft_title": (draft_title or "")[:120], "published_title": (published_title or "")[:120],
        "draft_chars": len(fp.normalize(draft_body)), "published_chars": len(fp.normalize(published_body)),
        "removed_count": len(removed), "added_count": len(added), "edited_count": len(edited),
        "removed_sentences": [s[:DIFF_SENTENCE_CHARS] for s in removed[:DIFF_KEEP_SENTENCES]],
        "added_sentences": [s[:DIFF_SENTENCE_CHARS] for s in added[:DIFF_KEEP_SENTENCES]],
        "edited_sentences": [[a[:DIFF_SENTENCE_CHARS], b[:DIFF_SENTENCE_CHARS]]
                             for a, b in edited[:DIFF_KEEP_SENTENCES]],
    }
    b = (brand or "").strip().lower()
    out["brand_draft"] = (draft_body or "").lower().count(b) if b else None
    out["brand_published"] = (published_body or "").lower().count(b) if b else None
    out["changed"] = bool(out["title_changed"] or removed or added or edited
                          or fp.normalize(draft_body) != fp.normalize(published_body))
    return out


# ── 发布链接 → 查得到的键(2026-10-19, comments_for 按链接找帖子) ─────────────────────────────
# TV 的 publish_url 是运营从飞书带过来的, 长短两种都有(10-19 实查 6168 条: 长链接 2302, xhslink 短链 3464, 其余
# 抖音 / t.cn)。写手贴过来的可能是另一种, 所以两边都折成"平台 + 那一段 id"再比, 不比整串 URL(查询参数每次分享都不同)。
# 短链和长链之间互相认不出(要跟一次跳转, 写作台不往外发请求), 那种情况 comments_for 会让写手把正文贴过来。
_LINK_PATTERNS = (
    # re.I: 主机名不分大小写(WWW.XIAOHONGSHU.COM 也认, codex review on aw#107)。取出来的 id 原样保留 ——
    # 字符类本来就两种大小写都收, re.I 不改捕获到的文字, 短链 id 的大小写照旧区分。
    ("xhs", re.compile(r"xiaohongshu\.com/(?:explore|discovery/item)/([0-9a-fA-F]{24})", re.I)),
    ("xhslink", re.compile(r"xhslink\.(?:com|cn)/(?:[a-zA-Z]/)?([A-Za-z0-9_-]{4,32})", re.I)),
    ("dy", re.compile(r"v\.douyin\.com/([A-Za-z0-9_-]{4,32})", re.I)),
    ("dyv", re.compile(r"douyin\.com/(?:video|note)/([0-9]{8,32})", re.I)),
    ("tcn", re.compile(r"t\.cn/([A-Za-z0-9]{4,32})", re.I)),
)


def link_key(url: str | None) -> tuple[str, str] | None:
    """发布链接 → ``("平台:id", id)``; 认不出的回 None。

    第二个值是拿去库里 ``ilike`` 粗筛的那一段: 只含字母数字和 ``_`` ``-``(上面的正则保证; 抖音短链里有这两个),
    不会把 PostgREST 的过滤语法(逗号、括号、百分号)带进查询。粗筛之后必须再用本函数比一次键 —— ilike 不分大小写
    (而且 ``_`` 在 like 里是通配), 短链的 id 分大小写。
    小红书的 24 位 id 是十六进制, 统一成小写。
    """
    text = (url or "").strip()
    if not text:
        return None
    for kind, rx in _LINK_PATTERNS:
        m = rx.search(text)
        if m:
            token = m.group(1).lower() if kind == "xhs" else m.group(1)
            return f"{kind}:{token}", token
    return None
