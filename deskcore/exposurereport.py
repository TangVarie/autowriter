"""deskcore/exposurereport.py — 经验卡的曝光读数(``python -m deskcore.cli lesson-exposure``, 2026-11-15, 十三项第 3 项)。

纯函数: 写作台的 batch(带 ``params.lesson_exposure``)、每个 batch 的版本、版本对上 TV 笔记的情况(store.desk_adoption)、
那几篇笔记的 tier → 每张卡 / 每条规律一行计数。只有 id 和数, 没有标题、正文、卡的文字。

读法(docs/deskcore.md §3.33):
  · 这是【曝光】不是【起作用】: 一批稿子写之前 12 小时里, 这个 (项目, 身份) 拿到过这张卡。模型用没用它, 这里看不出来;
  · 一张卡通常和别的卡一起给出去, 一批的结果会同时记到那一批看到的每张卡名下 —— 各行不能相加, 也不能拿两行相减当成
    「这张卡的效果」;
  · 团队共用一个身份时, 12 小时里同一个项目几场拿到的卡并在一起;
  · ``unknown`` 的批次(进程重启过 / 没借过卡)和这个功能之前的批次不在任何一张卡名下。
样本很小(11-12: 38 次借卡), 所以只给计数, 不给比率。
"""

from __future__ import annotations

from . import priors

HIT_TIERS = priors.HIT_TIERS


def _blank() -> dict:
    return {"batches": 0, "batches_published": 0, "posts": 0, "hits": 0}


def report(batches, *, versions: dict | None = None, adoption: dict | None = None,
           notes: dict | None = None, days: int | None = None) -> dict:
    """``batches``: store.report_exposure_batches 的行; ``versions``: {batch_id: [version_id…]};
    ``adoption``: store.desk_adoption 的结果(None = 对照读不到, 发了没有这次不算); ``notes``: {note_id: {"tier"…}}
    (None = TV 笔记读不到, 起量这次不算)。

    回 ``{"days", "batches": {"total", "known", "unknown", "before_feature", "no_cards"},
          "cards": [{"id", "batches", "batches_published", "posts", "hits"}…], "patterns": [同形状…],
          "no_cards": {同四个数}, "outcomes_read": {"published": bool, "hits": bool}}``。
    ``batches_published`` = 看到这张卡的批次里至少发出去一篇的; ``posts`` = 这些批次发出去的篇数(一版对上的一篇笔记算
    一篇); ``hits`` = 其中 tier 是爆 / 大爆的篇数。读不到的那一层的数是 None, 不是 0。"""
    by_version = (adoption or {}).get("by_version") if adoption is not None else None
    have_pub = by_version is not None and versions is not None
    have_hit = have_pub and notes is not None
    counts = {"total": 0, "known": 0, "unknown": 0, "before_feature": 0, "no_cards": 0}
    cards: dict[str, dict] = {}
    patterns: dict[str, dict] = {}
    none = _blank()

    for b in batches or ():
        counts["total"] += 1
        exposure = b.get("exposure")
        if exposure == "absent":
            counts["before_feature"] += 1
            continue
        if not isinstance(exposure, dict):
            counts["unknown"] += 1
            continue
        counts["known"] += 1
        posts = hits = 0
        if have_pub:
            seen_notes = set()
            for vid in versions.get(str(b.get("id"))) or ():
                link = by_version.get(str(vid))
                if link and link.get("note_id") and link["note_id"] not in seen_notes:
                    seen_notes.add(link["note_id"])
            posts = len(seen_notes)
            if have_hit:
                hits = sum(1 for n in seen_notes if str((notes.get(str(n)) or {}).get("tier") or "") in HIT_TIERS)
        card_ids = [str(c) for c in exposure.get("cards") or [] if c]
        pattern_ids = [str(p) for p in exposure.get("patterns") or [] if p]
        targets = [cards.setdefault(c, _blank()) for c in dict.fromkeys(card_ids)]
        targets += [patterns.setdefault(p, _blank()) for p in dict.fromkeys(pattern_ids)]
        if not card_ids:
            counts["no_cards"] += 1
            targets.append(none)
        for row in targets:
            row["batches"] += 1
            row["batches_published"] += int(posts > 0)
            row["posts"] += posts
            row["hits"] += hits

    def _rows(table: dict) -> list[dict]:
        return [{"id": k, **_mask(v)} for k, v in sorted(table.items(), key=lambda kv: (-kv[1]["batches"], kv[0]))]

    def _mask(row: dict) -> dict:
        return {"batches": row["batches"],
                "batches_published": row["batches_published"] if have_pub else None,
                "posts": row["posts"] if have_pub else None,
                "hits": row["hits"] if have_hit else None}

    return {"days": days, "batches": counts, "cards": _rows(cards), "patterns": _rows(patterns),
            "no_cards": _mask(none), "outcomes_read": {"published": bool(have_pub), "hits": bool(have_hit)}}


def _n(v) -> str:
    return "读不到" if v is None else str(v)


def format_report(rep: dict) -> str:
    b = rep["batches"]
    lines = [
        f"经验卡曝光(近 {rep.get('days') or '?'} 天, 只有 id 和计数)",
        f"写作台的批次 {b['total']} 个: 记下了看到哪些卡的 {b['known']} 个(其中一张卡都没拿到的 {b['no_cards']} 个), "
        f"不知道的 {b['unknown']} 个(进程重启过 / 没借过卡), 这个功能之前的 {b['before_feature']} 个",
        "注意: 这是曝光, 不是起作用的证明; 一批的结果同时记在它看到的每张卡名下, 各行不能相加、不能相减。",
        "",
        "经验卡(source_note_id)                         看到的批次  发出去的批次  发出去的篇数  起量篇数",
    ]

    def _line(name: str, r: dict) -> str:
        return (f"{name:<44} {r['batches']:>8}  {_n(r['batches_published']):>10}  {_n(r['posts']):>10}  "
                f"{_n(r['hits']):>8}")
    lines += [_line(r["id"], r) for r in rep["cards"]] or ["(没有)"]
    lines.append(_line("(一张卡都没拿到的批次)", rep["no_cards"]))
    lines += ["", "已验证规律(馆员给的 id; h: 开头的是规律文字的短哈希)"]
    lines += [_line(r["id"], r) for r in rep["patterns"]] or ["(没有)"]
    return "\n".join(lines)
