"""写作台 ↔ TV 的稿子对照 —— 纯匹配逻辑, 不碰库。

背景(2026-09-17): TV(truth_vault.notes)里 5966 条已发笔记, 带写作台 lineage 的
是 0 条。原设计让运营把 export_drafts 导出的六个 ID 列手抄进飞书, 三周零匹配。
两边在同一个库里、内容都是写作台产的, 所以改成**在库里按内容对**:

  1. 正文前 40 字(去标点)相同           → body_exact
  2. 标题(去标点)相同                    → title_exact
  3. 都对不上, 在时间窗内按四字串包含度  → fuzzy(发布稿是从这版改出来的)
  4. 多个候选分不出                      → ambiguous, 留候选给人看, 不硬猜
  5. 一个都没有                          → unmatched(→ 补录进指纹库)

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

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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

_TITLE_RE = re.compile(r"【标题】\s*(.*?)\s*【正文】", re.S)
_BODY_RE = re.compile(r"【正文】\s*(.*)$", re.S)
_TITLE_ONLY_RE = re.compile(r"【标题】\s*(.*)$", re.S)
# 小红书话题: "#秋招" / "#秋招[话题]#" / 连写 "#a#b"。
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
    """去掉话题标签。写作台的正文不带标签, TV 的带; 同项目的稿子都以同一串标签
    结尾, 不剥的话四字串会互相误撞, 补录进指纹库更是会让整个项目互判重复。"""
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
    kind: str                       # body_exact / title_exact / fuzzy / ambiguous / unmatched
    version: Version | None = None
    score: float | None = None
    lag_days: int | None = None
    candidates: list[dict] = field(default_factory=list)


def lag_days(note: Note, v: Version) -> int | None:
    if note.publish_time is None or v.created_at is None:
        return None
    return (note.publish_time.date() - v.created_at.date()).days


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
    tied = [v for s, v in scored if best_s - s < FUZZY_MARGIN]
    inside = [(abs(lag_days(note, v)), v) for v in tied
              if lag_days(note, v) is not None and in_window(lag_days(note, v))]
    if inside:
        inside.sort(key=lambda t: t[0])
        if len(inside) == 1 or inside[0][0] != inside[1][0]:
            return inside[0][1], best_s, listed
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
    if not scored or scored[0][0] < FUZZY_CONTAIN:
        return Match("unmatched")
    best_s, best_v = scored[0]
    if len(scored) > 1 and best_s - scored[1][0] < FUZZY_MARGIN:
        listed = [_cand(note, v, s) for s, v in scored[:5] if s >= FUZZY_CONTAIN - FUZZY_MARGIN]
        return Match("ambiguous", None, best_s, None, listed)
    return Match("fuzzy", best_v, best_s, lag_days(note, best_v))


def match_all(notes: Iterable[Note], index: VersionIndex) -> dict[str, Match]:
    return {n.note_id: match_note(n, index) for n in notes}
