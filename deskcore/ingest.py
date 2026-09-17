"""把【已经发出去、但没走 commit_drafts】的稿子从飞书表读回来 —— 只管解析。

── 为什么需要它(2026-09-17)────────────────────────────────────────────
09-11 起写作台写出去的稿子有 78% 没入库(见 tools.get_protocol 的说明)。它们
已经发在小红书上、也粘在飞书表里, 但指纹库对它们一无所知 —— 下一批查重时
它们等于从没存在过, 于是同样的句子会被再写一遍并顺利过闸。补救只有一条路:
把那些稿子从飞书表读回来, 补进指纹库。

这个模块**只做解析**, 不碰库: 输入一个 xlsx, 输出每行的标题 / 正文, 以及哪些
行要跳过。入库那一步在 core.ingest_published, 走的是回填(不过闸) —— 已经发出去
的稿子无论跟谁重复都必须记下来, 拦它没有意义, 拦了下一批照样撞它。

── 认两种表 ─────────────────────────────────────────────────────────────
  1. export_drafts 导出的形状: 一列「内容」(单元格里 "标题：…\\n正文：…"),
     后面跟 `_source_autowriter_*` 六列 lineage。**带 version_id 的行跳过** ——
     那些是真走过 commit 的, 已经有指纹。
  2. 运营自己的表: 「标题」「正文」两列(列名可以指定)。

列名手抄在这里而不从 exporter 读: exporter 引 openpyxl 之外还引一堆写表的东西,
而这里只想认几个名字。tests/test_deskcore_ingest.py 盯着两边一致。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

CONTENT_HEADER = "内容"
VERSION_ID_HEADER = "_source_autowriter_version_id"
DEFAULT_TITLE_COL = "标题"
DEFAULT_BODY_COL = "正文"

_TITLE_RE = re.compile(r"^\s*标题[:：]\s*(.*)$")
_BODY_RE = re.compile(r"^\s*正文[:：]\s*")


@dataclass
class IngestRow:
    row: int                      # 表里的行号(1-based, 表头是第 1 行)
    title: str
    body: str
    lineage_version_id: str | None = None


def parse_content_cell(text: str | None) -> tuple[str, str]:
    """导出格式的「内容」单元格 → (标题, 正文)。

    格式是 exporter 写的: 第一行 "标题：xxx", 空一行, "正文：" 开头到底。
    两种手改都要认(指纹主要靠正文, 丢正文等于白补):
      · 没有 "标题：" 前缀 → 整格当正文、标题留空;
      · 有 "标题：" 但 "正文：" 前缀被删了 → 标题后第一个非空行起全是正文。
    """
    if not text:
        return "", ""
    lines = str(text).replace("\r\n", "\n").split("\n")
    title = ""
    body_lines: list[str] = []
    in_body = False
    for ln in lines:
        if not in_body and not title:
            m = _TITLE_RE.match(ln)
            if m:
                title = m.group(1).strip()
                continue
        if not in_body and _BODY_RE.match(ln):
            in_body = True
            body_lines.append(_BODY_RE.sub("", ln, count=1))
            continue
        if in_body:
            body_lines.append(ln)
        elif ln.strip():
            # 到这里说明这一行既不是标题也没有"正文："前缀:
            #   · 还没有标题 → 整格没前缀, 全部当正文;
            #   · 已有标题   → "正文："被人删了, 这一行就是正文的开头。
            in_body = True
            body_lines.append(ln)
    return title, "\n".join(body_lines).strip()


def read_published_xlsx(path: str | Path, *, sheet: str | None = None,
                        title_col: str | None = None,
                        body_col: str | None = None) -> dict:
    """读一张已发稿子的表。返回::

        {"rows": [IngestRow, ...],           # 要入库的
         "skipped_with_lineage": int,        # 带 version_id 的, 已在库里
         "skipped_empty": int,               # 标题正文都空
         "shape": "export" | "columns",
         "headers": [...]}

    ``title_col`` / ``body_col`` 任一给了就走两列模式(缺的那个用默认列名);
    都没给就找「内容」列; 都找不到抛 ValueError 并把表头列出来。
    """
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        return _read(wb, path, sheet=sheet, title_col=title_col, body_col=body_col)
    finally:
        wb.close()          # read_only 模式下不 close 会一直占着文件句柄


def _read(wb, path, *, sheet, title_col, body_col) -> dict:
    ws = wb[sheet] if sheet else wb.active
    it = ws.iter_rows(values_only=True)
    try:
        header = [str(h).strip() if h is not None else "" for h in next(it)]
    except StopIteration:
        raise ValueError(f"{path}: 空表, 连表头都没有") from None
    col = {h: i for i, h in enumerate(header) if h}

    if title_col or body_col:
        tcol, bcol = title_col or DEFAULT_TITLE_COL, body_col or DEFAULT_BODY_COL
        missing = [c for c in (tcol, bcol) if c not in col]
        if missing:
            raise ValueError(f"{path}: 表头里没有 {missing}; 实际表头: {header}")
        shape = "columns"
    elif CONTENT_HEADER in col:
        shape = "export"
    elif DEFAULT_TITLE_COL in col and DEFAULT_BODY_COL in col:
        tcol, bcol = DEFAULT_TITLE_COL, DEFAULT_BODY_COL
        shape = "columns"
    else:
        raise ValueError(
            f"{path}: 认不出表的形状 —— 既没有「{CONTENT_HEADER}」列, 也没有"
            f"「{DEFAULT_TITLE_COL}」+「{DEFAULT_BODY_COL}」; 实际表头: {header}。"
            "用 --title-col / --body-col 指定。")

    vid_idx = col.get(VERSION_ID_HEADER)
    rows: list[IngestRow] = []
    skipped_lineage = skipped_empty = 0
    for n, values in enumerate(it, start=2):
        values = list(values) + [None] * (len(header) - len(values))
        vid = (str(values[vid_idx]).strip() if vid_idx is not None
               and values[vid_idx] not in (None, "") else None)
        if vid:
            skipped_lineage += 1
            continue
        if shape == "export":
            title, body = parse_content_cell(values[col[CONTENT_HEADER]])
        else:
            title = str(values[col[tcol]] or "").strip()
            body = str(values[col[bcol]] or "").strip()
        if not title and not body:
            skipped_empty += 1
            continue
        rows.append(IngestRow(row=n, title=title, body=body, lineage_version_id=None))
    return {"rows": rows, "skipped_with_lineage": skipped_lineage,
            "skipped_empty": skipped_empty, "shape": shape, "headers": header}
