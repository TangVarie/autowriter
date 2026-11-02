"""scripts/build_angle_priors.py — 重建发牌 v2 的先验快照 deskcore/vendor/angle_priors_snapshot.json(只读, 不写任何库)。

快照是 TV 读不到时 ``priors.get_profile`` 用的全局表: 每个 TV 项目按 内容形式 × 情绪杠杆 / 内容形式 × 问句与否 的干净
标签计数, 加上发帖配比、品类和品牌、构建时间。**只有计数** —— 没有标题、正文、笔记 id(仓库是公开的; 写盘前
``priors.snapshot_problems`` 再查一遍白名单)。每次部署重建一次。

用法(仓库根目录下):
  python scripts/build_angle_priors.py
      生产: 用 service client 读 TV(truth_vault.notes / note_feature_answers / projects, 全是 SELECT),
      过 priors.build_global —— 和线上缓存走的是同一个函数。
  python scripts/build_angle_priors.py --from-export cells.json mix.json
      离线: 没有库凭据时, 用 scripts/sql/angle_priors_export.sql 两段只读 SQL 导出的聚合拼(标签计数按同一套口径在
      SQL 里算; 导出文件本身只有计数, 也别提交)。
  --out PATH 写到别处; --check 只打印摘要不写盘。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from deskcore import priors, vocab  # noqa: E402

DEFAULT_OUT = priors.SNAPSHOT_PATH
_TIERS = {0: "train", 1: "gap", 2: "test"}


def _idx(values, i):
    try:
        i = int(i)
    except (TypeError, ValueError):
        return None
    return values[i] if 0 <= i < len(values) else None


def parse_cells(rows) -> dict[str, list[dict]]:
    """标签格导出(angle_priors_export.sql 第一段) → {TV 项目: [{bucket, content_format, emotional_lever, q, y, n, r}]}。
    每格是「阶段 × 形式 × 杠杆 × 问句与否 × 起量与否」的篇数 n 和中位秩之和 r; bucket 是项目开张后第几段
    (0: 前 10 天, 1: 第 10–13 天, 2: 第 14 天起), 回测用。"""
    out: dict[str, list[dict]] = {}
    for r in rows:
        cells = []
        for c in str(r.get("c") or "").split(";"):
            if not c:
                continue
            parts = c.split(",")
            if len(parts) != 7:
                # 少一段多半是导出里哪一列成了 NULL(concat_ws 会把它整个丢掉), 猜不出丢的是哪一列, 只能整份不认
                raise ValueError(f"cells export: project {r.get('p')!r} has a cell with {len(parts)} fields, "
                                 f"expected 7 (bucket,format,lever,question,y,n,rank_sum): {c!r}")
            b, fi, li, qi, y, n, rs = parts
            cells.append({"bucket": _TIERS.get(int(b), "test"),
                          "content_format": _idx(vocab.CONTENT_FORMATS, fi),
                          "emotional_lever": _idx(vocab.EMOTIONAL_LEVERS, li),
                          "q": {"1": priors.Q, "0": priors.NQ}.get(qi),
                          "y": int(y), "n": int(n), "r": float(rs)})
        out[str(r["p"])] = cells
    return out


_MIX_DIM = {"f": ("content_format", vocab.CONTENT_FORMATS), "l": ("emotional_lever", vocab.EMOTIONAL_LEVERS),
            "a": ("human_truth_archetype", vocab.HUMAN_TRUTH_ARCHETYPES),
            "i": ("emotional_intensity", vocab.EMOTIONAL_INTENSITIES),
            "t": ("trend_dependencies", vocab.TREND_DEPENDENCIES)}


def parse_mix(m: str) -> dict[str, dict]:
    """配比导出(第二段)的一个项目: "A"= 它自己最后 180 天, "B" = 现在往回 180 天。"""
    out = {"A": priors._new_mix(), "B": priors._new_mix()}
    for tok in str(m or "").split(";"):
        if not tok:
            continue
        key, val = tok.split(":")
        win, d, v = key[0], key[1], key[2:]
        acc = out[win]
        x = float(val)
        if d == "n":
            acc["n"] = x
        elif d == "q":
            acc["title_q"][priors.Q if v == "Q" else priors.NQ] = x
        else:
            dim, values = _MIX_DIM[d]
            name = _idx(values, v)
            if name:
                acc[dim][name] = x
    return out


def from_export(cells_rows, mix_rows) -> dict:
    cells = parse_cells(cells_rows)
    built = None
    gt: dict = {"schema": priors.SNAPSHOT_SCHEMA, "projects": {}}
    for r in mix_rows:
        pid = str(r["p"])
        first, last = str(r.get("span") or "/").split("/")
        e: dict = {"category": r.get("c") or None, "brand": priors.brand_of(pid), "first": first, "last": last}
        lbl = None
        for c in cells.get(pid, []):
            lbl = lbl or priors._new_label()
            priors.label_add(lbl, fmt=c["content_format"], lever=c["emotional_lever"], qclass=c["q"], y=c["y"],
                             rank=c["r"], n=c["n"])
            if c["r"] > 0:
                lbl["rk"] = True
        if lbl:
            e["label"] = lbl
        mixes = parse_mix(r.get("m"))
        e["mix"] = mixes["A"]
        if mixes["B"]["n"] > 0:
            e["mix_recent"] = mixes["B"]
        gt["projects"][pid] = e
        b = r.get("built")
        if b:
            built = str(b)
    t = priors._ts(str(built).replace(" ", "T")) if built else None
    gt["built_at"] = (t or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    gt["source"] = "sql-export"
    return priors._round_tree(gt)


def summarize(gt: dict) -> str:
    lines = [f"built_at {gt.get('built_at')} · {len(gt['projects'])} TV projects"]
    for pid, e in sorted(gt["projects"].items()):
        lbl = e.get("label") or {}
        lines.append(f"  {pid:<18} {e.get('category') or '-':<6} labelled {lbl.get('n', 0):>4} / successes "
                     f"{lbl.get('y', 0):>3} · posts(last 180d of own data) {int((e.get('mix') or {}).get('n', 0)):>4}")
    tot_n = sum((e.get("label") or {}).get("n", 0) for e in gt["projects"].values())
    tot_y = sum((e.get("label") or {}).get("y", 0) for e in gt["projects"].values())
    lines.append(f"  total labelled {tot_n} · successes {tot_y}")
    return "\n".join(lines)


def write(gt: dict, out: str) -> int:
    probs = priors.snapshot_problems(gt)
    if probs:
        print("refusing to write the snapshot:\n  " + "\n  ".join(probs[:20]), file=sys.stderr)
        return 1
    data = json.dumps(gt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    if len(data.encode("utf-8")) > priors.SNAPSHOT_MAX_BYTES:
        print(f"snapshot is {len(data.encode('utf-8'))} bytes (> {priors.SNAPSHOT_MAX_BYTES}); not written",
              file=sys.stderr)
        return 1
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(data)
    print(f"wrote {out} ({len(data.encode('utf-8'))} bytes)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="rebuild deskcore/vendor/angle_priors_snapshot.json (read-only)")
    ap.add_argument("--from-export", nargs=2, metavar=("CELLS_JSON", "MIX_JSON"), default=None)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true", help="print the summary only; write nothing")
    args = ap.parse_args(argv)
    if args.from_export:
        with open(args.from_export[0], encoding="utf-8") as fh:
            cells_rows = json.load(fh)
        with open(args.from_export[1], encoding="utf-8") as fh:
            mix_rows = json.load(fh)
        gt = from_export(cells_rows, mix_rows)
    else:
        from deskcore import core
        gt = priors.fetch_global(core.sb())
        if gt is None:
            print("truth_vault unreadable (see the WARN above); snapshot not rebuilt", file=sys.stderr)
            return 1
        gt["source"] = "live"
    print(summarize(gt))
    return 0 if args.check else write(gt, args.out)


if __name__ == "__main__":
    sys.exit(main())
