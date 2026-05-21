"""
Export module for XHS Content Workstation.

Features:
  - Generate a formatted Excel (.xlsx) file from approved copy items
  - Generate a formatted Word (.docx) document from approved copy items
  - Push copy to Feishu (Lark) via Webhook
"""

from __future__ import annotations

import io
import json
from datetime import datetime
from typing import Optional

import requests
from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    _OPENPYXL_AVAILABLE = True
except ImportError:
    _OPENPYXL_AVAILABLE = False

import config


# ── 公式注入防护 ───────────────────────────────────────────────────────────
# Excel/WPS/LibreOffice 打开 .xlsx 时，单元格首字符为 ``=`` / ``+`` / ``-`` /
# ``@`` 会被当作公式解析。即便是内部团队也可能误把 CSV/Excel 内容贴去其它
# 系统进而触发 DDE/macro。这里在写入前统一加前缀单引号转义。
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell_value(value):
    """对要写入 Excel 单元格的字符串做公式触发字符的转义。

    非字符串（数字、日期）直接返回；字符串以触发字符开头时前置单引号——
    Excel 会把它当成字面量字符串显示，单引号本身不显示。
    """
    if not isinstance(value, str):
        return value
    if value and value[0] in _FORMULA_TRIGGERS:
        return "'" + value
    return value


# ── Combined single-column Excel ───────────────────────────────────────────

def build_combined_excel(items: list[dict]) -> bytes:
    """
    Build a single-column Excel where every piece of copy occupies one cell.

    Cell format (newline-separated within the cell):
        标题：[title]
        正文：[body]
        #keyword1 #keyword2 #keyword3

    Designed for copy-paste into Feishu / Notion tables or direct posting.
    """
    if not _OPENPYXL_AVAILABLE:
        raise RuntimeError("openpyxl 未安装，请运行 pip install openpyxl")

    wb = Workbook()
    ws = wb.active
    ws.title = "内容"

    cell_align = Alignment(vertical="top", wrap_text=True)

    for row_idx, item in enumerate(items, 1):
        title = (item.get("title") or "").strip()
        body  = (item.get("body")  or "").strip()

        keywords = item.get("keywords", [])
        if isinstance(keywords, str):
            try:
                keywords = json.loads(keywords)
            except Exception:
                keywords = [k.strip() for k in keywords.split(",") if k.strip()]
        kw_str = " ".join(f"#{k}" for k in keywords if k) if keywords else ""

        parts: list[str] = []
        if title:
            parts.append(f"标题：{title}")
        if body:
            parts.append(f"正文：{body}")
        if kw_str:
            parts.append(kw_str)

        cell = ws.cell(row=row_idx, column=1, value=_safe_cell_value("\n".join(parts)))
        cell.alignment = cell_align

    ws.column_dimensions["A"].width = 80

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Legacy multi-column Excel ───────────────────────────────────────────────

def build_excel_document(
    items: list[dict],
    project_name: str,
    brand: str,
    tactic: str,
    generated_at: Optional[str] = None,
) -> bytes:
    """
    Build an Excel file from approved copy items.
    Columns: 序号, 标题, 正文, 关键词, AI引擎, 版本
    Optimized for copy-paste into Feishu spreadsheet.
    """
    if not _OPENPYXL_AVAILABLE:
        raise RuntimeError("openpyxl 未安装，请运行 pip install openpyxl")

    wb = Workbook()
    ws = wb.active
    ws.title = "稿件内容"

    # Header style
    header_font = Font(bold=True, size=12, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin_border = Border(
        left=Side(style="thin"),
        right=Side(style="thin"),
        top=Side(style="thin"),
        bottom=Side(style="thin"),
    )

    # Headers
    # 2026-05-21: 末尾 6 列是隐藏 lineage，飞书整表导入会带过去，TV ingest
    # 用 _source_autowriter_* 反向归因到具体 item / version。AI 引擎 / 版本
    # 在前 6 列已经有展示用的格式 (.upper() / "v1")，lineage 列保留原值便于
    # 程序消费。
    headers = [
        "序号", "标题", "正文", "关键词", "AI引擎", "版本",
        "_source_autowriter_project_id",
        "_source_autowriter_batch_id",
        "_source_autowriter_item_id",
        "_source_autowriter_version_id",
        "_source_autowriter_ai_engine",
        "_source_autowriter_version_num",
    ]
    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment
        cell.border = thin_border

    # Data rows
    body_alignment = Alignment(vertical="top", wrap_text=True)
    for row_idx, item in enumerate(items, 2):
        keywords = item.get("keywords", [])
        if isinstance(keywords, list):
            kw_str = " ".join(f"#{k}" for k in keywords)
        else:
            kw_str = str(keywords)

        row_data = [
            row_idx - 1,
            item.get("title", ""),
            item.get("body", ""),
            kw_str,
            item.get("ai_engine", "").upper(),
            f"v{item.get('version_num', 1)}",
            item.get("project_id", ""),
            item.get("batch_id", ""),
            item.get("item_id", ""),
            item.get("version_id", ""),
            item.get("ai_engine", ""),     # 不带 .upper()，保留原值供 TV ingest
            item.get("version_num", 1),
        ]
        for col_idx, value in enumerate(row_data, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=_safe_cell_value(value))
            cell.alignment = body_alignment
            cell.border = thin_border

    # Column widths
    ws.column_dimensions["A"].width = 6   # 序号
    ws.column_dimensions["B"].width = 30  # 标题
    ws.column_dimensions["C"].width = 80  # 正文
    ws.column_dimensions["D"].width = 25  # 关键词
    ws.column_dimensions["E"].width = 10  # AI引擎
    ws.column_dimensions["F"].width = 8   # 版本
    # 隐藏 lineage 列：G–L 设宽 30，hidden=True
    for col_letter in ("G", "H", "I", "J", "K", "L"):
        ws.column_dimensions[col_letter].hidden = True
        ws.column_dimensions[col_letter].width = 30

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Word Export ────────────────────────────────────────────────────────────

def _add_heading(doc: Document, text: str, level: int = 1) -> None:
    p = doc.add_heading(text, level=level)
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT


def _add_body_paragraph(doc: Document, text: str) -> None:
    for line in text.split("\n"):
        para = doc.add_paragraph(line)
        para.style.font.size = Pt(11)


def build_word_document(
    items: list[dict],
    project_name: str,
    brand: str,
    tactic: str,
    generated_at: Optional[str] = None,
) -> bytes:
    """
    Build a Word document from a list of approved copy items.

    Each item dict should have keys: title, body, keywords, ai_engine, version_num
    Returns raw .docx bytes.
    """
    doc = Document()

    # ── Cover page ──────────────────────────────────────────────────────
    title_para = doc.add_paragraph()
    title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title_para.add_run(f"{brand} 小红书内容稿件")
    run.bold = True
    run.font.size = Pt(24)

    subtitle_para = doc.add_paragraph()
    subtitle_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    date_str = generated_at or datetime.now().strftime("%Y年%m月%d日")
    subtitle_para.add_run(
        f"项目：{project_name}  |  战术方向：{tactic}  |  生成日期：{date_str}"
    ).font.size = Pt(12)

    count_para = doc.add_paragraph()
    count_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    count_para.add_run(f"共 {len(items)} 篇通过稿件").font.size = Pt(12)

    doc.add_page_break()

    # ── Table of Contents placeholder ───────────────────────────────────
    _add_heading(doc, "目录", level=1)
    for idx, item in enumerate(items, 1):
        toc_entry = doc.add_paragraph(style="List Number")
        toc_entry.add_run(item.get("title", f"第{idx}篇"))

    doc.add_page_break()

    # ── Copy pages ────────────────────────────────────────────────────
    for idx, item in enumerate(items, 1):
        _add_heading(doc, f"第{idx}篇", level=1)

        # Title
        title_p = doc.add_paragraph()
        title_run = title_p.add_run(item.get("title", ""))
        title_run.bold = True
        title_run.font.size = Pt(16)
        title_p.alignment = WD_ALIGN_PARAGRAPH.LEFT

        # Character count hint
        title_len = len(item.get("title", ""))
        meta_p = doc.add_paragraph()
        meta_p.add_run(
            f"标题字数：{title_len}字  |  AI来源：{item.get('ai_engine','').upper()}  "
            f"|  版本：v{item.get('version_num', 1)}"
        ).font.size = Pt(9)

        # Separator
        doc.add_paragraph("─" * 40)

        # Body
        _add_body_paragraph(doc, item.get("body", ""))

        # Keywords
        keywords = item.get("keywords", [])
        if keywords:
            kw_para = doc.add_paragraph()
            kw_para.add_run("关键词：").bold = True
            kw_para.add_run("  ".join(f"#{k}" for k in keywords))

        # 2026-05-21: lineage footnote for TV reverse-attribution.
        # 6pt 灰字，肉眼几乎看不见但 TV ingest 可解析。
        item_uuid = item.get("item_id") or ""
        version_uuid = item.get("version_id") or ""
        if item_uuid or version_uuid:
            footnote = doc.add_paragraph()
            run = footnote.add_run(
                f"source_autowriter_item_id={item_uuid} "
                f"version_id={version_uuid}"
            )
            run.font.size = Pt(6)
            run.font.color.rgb = RGBColor(0xAA, 0xAA, 0xAA)

        doc.add_page_break()

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ── Feishu (Lark) Webhook Push ─────────────────────────────────────────────

def push_to_feishu(
    items: list[dict],
    project_name: str,
    brand: str,
    tactic: str,
    webhook_url: Optional[str] = None,
) -> bool:
    """
    Push approved copy items to a Feishu group via Webhook.
    Returns True on success.

    Uses a rich-text card format.
    """
    url = webhook_url or config.FEISHU_WEBHOOK_URL
    if not url:
        return False

    date_str = datetime.now().strftime("%Y年%m月%d日")
    header_text = f"📝 {brand} 小红书稿件 | {tactic} | {date_str} | 共{len(items)}篇"

    # Build content lines
    lines: list[str] = []
    for idx, item in enumerate(items, 1):
        lines.append(f"**{idx}. {item.get('title', '')}**")
        body = item.get("body", "")
        # Trim body preview to 150 chars
        preview = body[:150] + ("…" if len(body) > 150 else "")
        lines.append(preview)
        keywords = item.get("keywords", [])
        if keywords:
            lines.append("　".join(f"#{k}" for k in keywords))
        lines.append("---")

    content = "\n".join(lines)

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": header_text},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": content,
                }
            ],
        },
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        return result.get("StatusCode") == 0 or result.get("code") == 0
    except Exception:
        return False


def push_items_as_text(
    items: list[dict],
    project_name: str,
    brand: str,
    tactic: str,
    webhook_url: Optional[str] = None,
) -> bool:
    """
    Alternative: push as plain text message (simpler, for basic Feishu bots).
    """
    url = webhook_url or config.FEISHU_WEBHOOK_URL
    if not url:
        return False

    date_str = datetime.now().strftime("%Y-%m-%d")
    lines = [f"【{brand}·{tactic}】小红书稿件 {date_str}\n"]

    for idx, item in enumerate(items, 1):
        lines.append(f"▌{idx}. {item.get('title', '')}")
        lines.append(item.get("body", ""))
        kws = item.get("keywords", [])
        if kws:
            lines.append(" ".join(f"#{k}" for k in kws))
        lines.append("")

    payload = {
        "msg_type": "text",
        "content": {"text": "\n".join(lines)},
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception:
        return False
