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


# ── lineage 列: 与 Truth Vault 的跨库契约 ──────────────────────────────────
#
# ⚠️ **这六个列名不是我们定的**, 是 truth-vault 定的 —— 见那边的
# ``docs/11-feishu-table-setup.md``「lineage 元数据列」和
# ``scripts/sync_feishu_notes_to_truth_vault.py`` 里的 ``_LINEAGE_FK_COLS`` /
# ``_LINEAGE_RAW_EXTRA_COLS``。TV 的 sync **按列名**做特殊处理:
#
#   · 两个 UUID 列自动提升进 ``notes.source_autowriter_item_id`` /
#     ``source_autowriter_version_id``（跨 schema FK）—— ``v_model_comparison``
#     就 JOIN 在后者上, 模型胜率全靠它;
#   · 另外四列落 ``notes.raw_extra`` 留痕;
#   · **六个名字对所有项目全局预声明**, 所以带这些列的行不会被 D-021 拦下。
#
# 名字写错的代价**不是丢一列**: 未声明的列会让 D-021 把**整行** quarantine,
# 那条笔记连正文带指标一起进不了库。跨库审计 COR-002 指的就是这个 ——
# 之前这里写的是 ``_source_autowriter_ai_engine`` / ``_source_autowriter_
# version_num``, 两个都不在 TV 认的六个里面。
#
# 收成一张表而不是散在两个 builder 里: 两份就会漂, 而漂了不报错 —— 表现是
# "从某次导出开始, 飞书那边整批笔记悄悄不入库了"。tests/test_lineage_contract.py
# 拿这张表当判据。
#
# 第二个元素 = 从 ``_collect_approved_items`` 产出的行里取哪个 key;
# ``None`` = 不来自稿件本身（``_exported_at`` 是导出这一刻）。
LINEAGE_COLUMNS: tuple[tuple[str, Optional[str]], ...] = (
    ("_source_autowriter_project_id", "project_id"),
    ("_source_autowriter_batch_id",   "batch_id"),
    ("_source_autowriter_item_id",    "item_id"),
    ("_source_autowriter_version_id", "version_id"),
    # ⚠️ 展示用的「AI引擎」列是 .upper() 过的, 这里必须保留原值 —— TV 按它
    #    GROUP BY 出模型胜率, 'CLAUDE' 和 'claude' 会被算成两个引擎。
    ("_ai_engine",                    "ai_engine"),
    # 飞书那边这一列是**日期**类型（docs/11 的表）, 所以给 ISO 8601。
    ("_exported_at",                  None),
)

LINEAGE_HEADERS: tuple[str, ...] = tuple(name for name, _ in LINEAGE_COLUMNS)


def _lineage_values(item: dict, exported_at: str) -> list:
    """按 ``LINEAGE_COLUMNS`` 的顺序取一行的 lineage 值。

    缺的 id 一律写空串而不是跳过 —— 列的**位置**是固定的, 少写一格会让后面
    所有列串位, 而串位之后每个值看起来都还是合法的字符串。
    """
    return [exported_at if key is None else (item.get(key) or "")
            for _name, key in LINEAGE_COLUMNS]


# ── Combined single-column Excel ───────────────────────────────────────────

CONTENT_HEADER = "内容"


def build_combined_excel(items: list[dict],
                         exported_at: Optional[str] = None) -> bytes:
    """
    Build a copy-paste Excel: one cell per piece of copy, plus lineage columns.

    A 列每格的格式（格内换行）:
        标题：[title]
        正文：[body]
        #keyword1 #keyword2 #keyword3

    B–G 列是 ``LINEAGE_COLUMNS``，第 1 行是表头。

    ── 2026-08-25：lineage 从「隐藏的无名 B 列」改成「命名的可见列」 ──────
    原来 B 列写的是一个 lineage JSON 包，整列 ``hidden=True``。那个设计在飞书
    这一侧**结构上就走不通**，两条独立的原因:

      · **没有表头行**（原来 ``enumerate(items, 1)`` 从第 1 行就开始写数据）。
        飞书按**列名**匹配字段，无名列进不去；TV 的 sync 也是按名认那六列的。
      · 隐藏列**只有"整表导入"才跟着走**。运营的实际动作是选中可见列复制、
        粘进飞书表 —— 一粘，隐藏列就没了。truth-vault 的
        ``docs/11-feishu-table-setup.md`` 结尾把这条单独列为待解决的坑。

    于是这套 lineage 从上线起就没有真的到过飞书:
    ``truth_vault.v_model_comparison`` JOIN 的是
    ``notes.source_autowriter_version_id``，那一列一直是空的，view 长期查出来
    是空集 —— 而且不报错。

    改成可见的命名列之后，运营整片选中粘贴就把六列带过去了。代价是**粘贴时会
    多带一行表头**，这是有意的取舍: 飞书要靠表头认列，而认不出列的后果（整行
    被 D-021 quarantine）比多粘一行严重得多。

    ``exported_at`` 只为测试可复现留的口子，正常调用不传。
    """
    if not _OPENPYXL_AVAILABLE:
        raise RuntimeError("openpyxl 未安装，请运行 pip install openpyxl")

    exported_at = exported_at or datetime.now().isoformat(timespec="seconds")

    wb = Workbook()
    ws = wb.active
    ws.title = "内容"

    cell_align = Alignment(vertical="top", wrap_text=True)

    # 第 1 行: 表头。飞书靠它认列 —— 少了这一行，后面写什么都没用。
    for col_idx, header in enumerate((CONTENT_HEADER, *LINEAGE_HEADERS), 1):
        ws.cell(row=1, column=col_idx,
                value=_safe_cell_value(header)).font = Font(bold=True)

    for row_idx, item in enumerate(items, 2):
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

        # lineage: 每个 id 一列, 名字由 TV 定。全空的稿子照样写空格子 ——
        # 列的位置必须对所有行一致, 否则粘进飞书会串位。
        for offset, value in enumerate(_lineage_values(item, exported_at), 2):
            lc = ws.cell(row=row_idx, column=offset, value=_safe_cell_value(value))
            lc.alignment = cell_align

    ws.column_dimensions["A"].width = 80
    for offset in range(len(LINEAGE_COLUMNS)):
        # 2=B, 3=C … openpyxl 的列字母换算走 get_column_letter, 这里列数固定
        # 且很小, 直接用 chr 更省一个 import。
        ws.column_dimensions[chr(ord("B") + offset)].width = 38

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
    Columns: 序号, 标题, 正文, 关键词, AI引擎, 版本 + LINEAGE_COLUMNS
    Optimized for copy-paste into Feishu spreadsheet.

    ⚠️ **当前没有任何调用方** —— 导出中心走的是 ``build_combined_excel``。
    保留是因为分列形态（标题/正文/关键词各一列）对某些飞书表更合适，真要启用
    时不必重写。但也正因为没人调，它是最容易和 TV 契约漂开的地方: 跨库审计
    COR-002 钉的两个错列名就在这个函数里，从写下那天起就没被任何一次真实导出
    暴露过。所以 lineage 一律走 ``LINEAGE_COLUMNS``，不在这里另写一份。
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
    # 末尾 6 列是 lineage（名字由 TV 定，见 LINEAGE_COLUMNS）。AI 引擎 / 版本
    # 在前 6 列已经有展示用的格式 (.upper() / "v1")，lineage 列保留原值便于
    # 程序消费。
    #
    # 2026-08-25（跨库审计 COR-002）: 这里原来最后两列写的是
    # ``_source_autowriter_ai_engine`` / ``_source_autowriter_version_num``，
    # 两个名字 TV 都不认。后者尤其不是改个名的事 —— TV 那一格要的是
    # ``_exported_at``（**导出时刻**，飞书日期列），和「第几版」是两码事。
    headers = ["序号", "标题", "正文", "关键词", "AI引擎", "版本",
               *LINEAGE_HEADERS]
    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment
        cell.border = thin_border

    # Data rows
    body_alignment = Alignment(vertical="top", wrap_text=True)
    exported_at = generated_at or datetime.now().isoformat(timespec="seconds")
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
            *_lineage_values(item, exported_at),
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
    # lineage 列 G 起。**不隐藏** —— 隐藏列只有"整表导入"才跟着走, 复制可见列
    # 粘进飞书就丢了(同 build_combined_excel 的说明)。
    for offset in range(len(LINEAGE_COLUMNS)):
        ws.column_dimensions[chr(ord("G") + offset)].width = 30

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
