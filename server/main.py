"""FastAPI app that Feishu Bitable automations can POST to.

Endpoint surface:
  GET  /healthz           — liveness probe (for Railway / uptime pingers)
  POST /generate          — trigger a batch (body: {"batch_record_id": "..."})
  POST /iterate           — re-draft one item using its feedback
                           (body: {"item_record_id": "..."})

Both mutating endpoints return 202 immediately and run the actual LLM call
in a BackgroundTask, so Feishu automation never hits the webhook timeout.
Status + errors are written back to the relevant row in Bitable.

Auth: if WEBHOOK_SHARED_SECRET is set, incoming requests must carry it in
the X-Autowriter-Token header. In Feishu automation's "发送 HTTP 请求" step,
add that header manually.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import config
from .feishu_bitable import (
    BitableClient,
    attachment_tokens_of,
    encode_attachments_as_images,
    link_ids_of,
    multi_select_of,
    single_select_of,
    text_of,
)
from .generator_core import run_generation, run_iteration
from .prompt_builder import build_system_prompt, lookup_tactic_suffix, parse_examples_text

log = logging.getLogger("autowriter.server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="AutoWriter Feishu Backend")
_client = BitableClient()


# ── Request models ──────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    batch_record_id: str = Field(..., description="Record ID in the Batches table")


class IterateRequest(BaseModel):
    item_record_id: str = Field(..., description="Record ID in the Items table")


# ── Routes ──────────────────────────────────────────────────────────────

@app.get("/healthz")
def healthz() -> dict[str, Any]:
    missing = config.validate()
    return {"ok": not missing, "missing_env": missing}


def _check_auth(token: str | None) -> None:
    if config.WEBHOOK_SHARED_SECRET and token != config.WEBHOOK_SHARED_SECRET:
        raise HTTPException(status_code=401, detail="bad token")
    missing = config.validate()
    if missing:
        raise HTTPException(status_code=500, detail=f"missing env: {missing}")


@app.post("/generate", status_code=202)
def generate(
    req: GenerateRequest,
    background: BackgroundTasks,
    x_autowriter_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_auth(x_autowriter_token)
    background.add_task(_run_batch, req.batch_record_id)
    return {"accepted": True, "batch_record_id": req.batch_record_id}


@app.post("/iterate", status_code=202)
def iterate(
    req: IterateRequest,
    background: BackgroundTasks,
    x_autowriter_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_auth(x_autowriter_token)
    background.add_task(_run_iterate, req.item_record_id)
    return {"accepted": True, "item_record_id": req.item_record_id}


# ── Shared field / status constants ─────────────────────────────────────

_BATCH_STATUS = "状态"
_BATCH_ERROR = "错误信息"
_ITEM_STATUS = "状态"
_ITEM_ERROR = "错误信息"


# ── Batch orchestration ────────────────────────────────────────────────

def _mark_batch(record_id: str, status: str, error: str | None = None) -> None:
    fields: dict[str, Any] = {_BATCH_STATUS: status}
    if error is not None:
        fields[_BATCH_ERROR] = error[:2000]
    try:
        _client.update_record(config.FEISHU_TABLE_BATCHES, record_id, fields)
    except Exception as e:
        log.exception("failed to mark batch %s as %s: %s", record_id, status, e)


def _mark_item(record_id: str, fields: dict[str, Any]) -> None:
    try:
        _client.update_record(config.FEISHU_TABLE_ITEMS, record_id, fields)
    except Exception as e:
        log.exception("failed to update item %s: %s", record_id, e)


def _run_batch(batch_record_id: str) -> None:
    """Full generation pipeline for one Batches row."""
    try:
        _mark_batch(batch_record_id, "生成中", error="")

        batch_row = _client.get_record(config.FEISHU_TABLE_BATCHES, batch_record_id)
        batch_fields = batch_row.get("fields", {}) or {}

        project_id, project_fields = _load_project_from_batch(batch_fields)
        tactic = single_select_of(batch_fields.get("tactic")) or text_of(batch_fields.get("tactic"))

        full_system_prompt = _compose_system_prompt(
            project_id=project_id,
            project_fields=project_fields,
            tactic=tactic,
        )

        claude_model, gemini_model, claude_thinking, gemini_thinking = _engine_overrides(batch_fields)

        raw_count = batch_fields.get("数量") or 1
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            count = 1
        count = max(1, min(count, 50))

        engines = _parse_engines(batch_fields.get("引擎"))

        images = _project_reference_images(project_fields)
        historical_titles = _recent_project_titles(project_id)

        target_audience = text_of(batch_fields.get("目标人群"))
        key_messages = text_of(batch_fields.get("核心卖点"))
        tone = text_of(batch_fields.get("语气"))
        extra = text_of(batch_fields.get("补充说明"))

        log.info(
            "batch %s: project=%s tactic=%s count=%d engines=%s images=%d history=%d",
            batch_record_id, project_id, tactic, count, engines,
            len(images), len(historical_titles),
        )

        results = run_generation(
            system_prompt=full_system_prompt,
            tactic=tactic,
            count=count,
            engines=engines,
            target_audience=target_audience,
            key_messages=key_messages,
            tone=tone,
            extra_instructions=extra,
            claude_model=claude_model,
            gemini_model=gemini_model,
            claude_thinking=claude_thinking,
            gemini_thinking=gemini_thinking,
            historical_titles=historical_titles,
            images=images or None,
        )

        records = _results_to_item_records(
            results=results,
            batch_record_id=batch_record_id,
            project_id=project_id,
        )
        if records:
            _client.batch_create(config.FEISHU_TABLE_ITEMS, records)

        failed = [r for r in results if r.get("error") or not r.get("title")]
        if failed and len(failed) == len(results):
            _mark_batch(
                batch_record_id, "失败",
                error=f"全部生成失败。示例：{failed[0].get('error') or '无标题输出'}",
            )
        else:
            note = f"{len(failed)}/{len(results)} 条失败" if failed else ""
            _mark_batch(batch_record_id, "完成", error=note)

    except Exception as e:  # noqa: BLE001
        log.exception("batch %s failed: %s", batch_record_id, e)
        _mark_batch(
            batch_record_id, "失败",
            error=f"{type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}",
        )


# ── Single-item iteration ──────────────────────────────────────────────

def _run_iterate(item_record_id: str) -> None:
    try:
        _mark_item(item_record_id, {_ITEM_STATUS: "生成中", _ITEM_ERROR: ""})

        item_row = _client.get_record(config.FEISHU_TABLE_ITEMS, item_record_id)
        item_fields = item_row.get("fields", {}) or {}

        feedback = text_of(item_fields.get("反馈")).strip()
        if not feedback:
            raise ValueError("反馈字段为空，无法迭代")

        prior_title = text_of(item_fields.get("标题"))
        prior_body = text_of(item_fields.get("正文"))
        prior_keywords_raw = text_of(item_fields.get("关键词"))
        prior_keywords = [k.strip() for k in prior_keywords_raw.replace("，", ",").split(",") if k.strip()]

        project_ids = link_ids_of(item_fields.get("项目"))
        if not project_ids:
            raise ValueError("Item 未关联项目")
        project_id = project_ids[0]
        project_row = _client.get_record(config.FEISHU_TABLE_PROJECTS, project_id)
        project_fields = project_row.get("fields", {}) or {}

        # Find the owning batch to re-read tactic + model/thinking overrides
        batch_fields: dict[str, Any] = {}
        batch_ids = link_ids_of(item_fields.get("批次"))
        if batch_ids:
            try:
                batch_row = _client.get_record(config.FEISHU_TABLE_BATCHES, batch_ids[0])
                batch_fields = batch_row.get("fields", {}) or {}
            except Exception as e:
                log.warning("failed to read batch for item %s: %s", item_record_id, e)

        tactic = (
            single_select_of(batch_fields.get("tactic"))
            or text_of(batch_fields.get("tactic"))
        )
        full_system_prompt = _compose_system_prompt(
            project_id=project_id,
            project_fields=project_fields,
            tactic=tactic,
        )

        claude_model, gemini_model, claude_thinking, gemini_thinking = _engine_overrides(batch_fields)

        # Determine which engine to iterate with — prefer the original engine
        engine = (
            text_of(item_fields.get("引擎")).split("/")[0].strip().lower()
            or "claude"
        )
        if engine not in ("claude", "gemini"):
            engine = "claude"

        images = _project_reference_images(project_fields)

        log.info(
            "iterate %s: project=%s engine=%s feedback_len=%d",
            item_record_id, project_id, engine, len(feedback),
        )

        result = run_iteration(
            system_prompt=full_system_prompt,
            prior_title=prior_title,
            prior_body=prior_body,
            prior_keywords=prior_keywords,
            feedback=feedback,
            engine=engine,
            claude_model=claude_model,
            gemini_model=gemini_model,
            claude_thinking=claude_thinking,
            gemini_thinking=gemini_thinking,
            images=images or None,
        )

        prior_version_num = item_fields.get("版本") or 1
        try:
            next_version = int(prior_version_num) + 1
        except (TypeError, ValueError):
            next_version = 2

        update_fields: dict[str, Any] = {
            "标题": result.get("title") or prior_title,
            "正文": result.get("body") or prior_body,
            "关键词": ", ".join(result.get("keywords") or prior_keywords),
            "引擎": result.get("ai_engine", engine),
            "版本": next_version,
            _ITEM_STATUS: "待审核",
            "反馈": "",  # clear so the same feedback isn't applied twice
            _ITEM_ERROR: result.get("error") or "",
        }
        _mark_item(item_record_id, update_fields)

    except Exception as e:  # noqa: BLE001
        log.exception("iterate %s failed: %s", item_record_id, e)
        _mark_item(item_record_id, {
            _ITEM_STATUS: "需修改",
            _ITEM_ERROR: f"{type(e).__name__}: {e}\n{traceback.format_exc()[-400:]}",
        })


# ── Shared helpers ─────────────────────────────────────────────────────

def _load_project_from_batch(batch_fields: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    project_ids = link_ids_of(batch_fields.get("项目"))
    if not project_ids:
        raise ValueError("批次未关联项目")
    project_id = project_ids[0]
    project_row = _client.get_record(config.FEISHU_TABLE_PROJECTS, project_id)
    project_fields = project_row.get("fields", {}) or {}
    if not text_of(project_fields.get("system_prompt")).strip():
        raise ValueError("项目 system_prompt 为空")
    return project_id, project_fields


def _compose_system_prompt(
    *,
    project_id: str,
    project_fields: dict[str, Any],
    tactic: str,
) -> str:
    system_prompt = text_of(project_fields.get("system_prompt"))
    calibration_notes = text_of(project_fields.get("调教笔记"))
    tactics_config = text_of(project_fields.get("战术配置"))
    tactic_suffix = lookup_tactic_suffix(tactic, tactics_config)

    manual_positive = parse_examples_text(text_of(project_fields.get("正面示例")))
    manual_negative = parse_examples_text(text_of(project_fields.get("负面示例")))
    auto_positive, auto_negative = _examples_from_items(project_id)

    # Manual examples take priority (come first), auto-harvested fill remaining slots.
    positive_examples = (manual_positive + auto_positive)[:5]
    negative_examples = (manual_negative + auto_negative)[:3]

    memories = _load_memories(project_id) if config.FEISHU_TABLE_MEMORIES else ([], [])
    global_memories, project_memories = memories

    return build_system_prompt(
        base_prompt=system_prompt,
        global_memories=global_memories,
        project_memories=project_memories,
        tactic_suffix=tactic_suffix,
        calibration_notes=calibration_notes,
        positive_examples=positive_examples,
        negative_examples=negative_examples,
    )


def _engine_overrides(
    batch_fields: dict[str, Any],
) -> tuple[str, str, bool, bool]:
    """Read per-batch model + thinking overrides. All fields are optional."""
    claude_model = single_select_of(batch_fields.get("Claude模型")) or text_of(batch_fields.get("Claude模型"))
    gemini_model = single_select_of(batch_fields.get("Gemini模型")) or text_of(batch_fields.get("Gemini模型"))
    claude_thinking = bool(batch_fields.get("Claude深度思考"))
    gemini_thinking = bool(batch_fields.get("Gemini深度思考"))
    return claude_model, gemini_model, claude_thinking, gemini_thinking


def _parse_engines(raw: Any) -> list[str]:
    engines_raw = multi_select_of(raw) or ["claude"]
    engines = [e.lower().split("/")[0] for e in engines_raw]
    engines = [e for e in engines if e in ("claude", "gemini")]
    return engines or ["claude"]


def _project_reference_images(project_fields: dict[str, Any]) -> list[dict]:
    attachments = attachment_tokens_of(project_fields.get("参考图片"))
    if not attachments:
        return []
    try:
        return encode_attachments_as_images(_client, attachments, max_images=4)
    except Exception as e:
        log.warning("encode reference images failed: %s", e)
        return []


def _recent_project_titles(project_id: str, limit: int = 30) -> list[str]:
    """Pull the last ``limit`` item titles for this project as dedup context.

    Bitable FQL filters on linked-record fields require matching the link's
    *display* value, not the record_id — we don't know the primary field
    up-front, so we fetch one page of items and filter client-side by
    link_ids. This keeps the call simple; for larger tables, add a formula
    field that exposes the linked project's name and filter on that.
    """
    try:
        rows = _client.list_records(config.FEISHU_TABLE_ITEMS, page_size=100)
    except Exception as e:
        log.warning("recent titles fetch failed: %s", e)
        return []

    titles: list[str] = []
    for row in rows:
        f = row.get("fields", {}) or {}
        if project_id not in link_ids_of(f.get("项目")):
            continue
        title = text_of(f.get("标题"))
        if title:
            titles.append(title)
        if len(titles) >= limit:
            break
    return titles


def _examples_from_items(
    project_id: str,
    *,
    pos_limit: int = 3,
    neg_limit: int = 2,
) -> tuple[list[dict], list[dict]]:
    """Harvest approved / marked items from the Items table as live examples.

    Positive: 示例标记 = "正例"  OR  状态 = "已通过"
    Negative: 示例标记 = "负例"
    Manual 正面示例 / 负面示例 in the project still take priority; these fill gaps.
    """
    try:
        rows = _client.list_records(config.FEISHU_TABLE_ITEMS, page_size=100)
    except Exception as e:
        log.warning("examples fetch failed: %s", e)
        return [], []

    pos: list[dict] = []
    neg: list[dict] = []
    for row in rows:
        f = row.get("fields", {}) or {}
        if project_id not in link_ids_of(f.get("项目")):
            continue
        title = text_of(f.get("标题"))
        body = text_of(f.get("正文"))
        if not title or not body:
            continue
        label = single_select_of(f.get("示例标记"))
        status = single_select_of(f.get("状态"))
        if label == "正例" or (label == "" and status == "已通过"):
            pos.append({"title": title, "body": body})
        elif label == "负例":
            neg.append({"title": title, "body": body})
        if len(pos) >= pos_limit and len(neg) >= neg_limit:
            break
    return pos[:pos_limit], neg[:neg_limit]


def _load_memories(project_id: str) -> tuple[list[dict], list[dict]]:
    """Return (global_memories, project_memories) as lists of {'content': str}."""
    try:
        all_enabled = _client.list_records(
            config.FEISHU_TABLE_MEMORIES,
            filter_expr='CurrentValue.[启用] = TRUE()',
        )
    except Exception as e:
        log.warning("list memories failed: %s", e)
        return [], []

    global_memories: list[dict] = []
    project_memories: list[dict] = []
    for row in all_enabled:
        f = row.get("fields", {}) or {}
        content = text_of(f.get("内容"))
        if not content.strip():
            continue
        scope = single_select_of(f.get("范围"))
        linked = link_ids_of(f.get("项目"))
        if scope == "全局" or (scope == "" and not linked):
            global_memories.append({"content": content})
        elif project_id in linked:
            project_memories.append({"content": content})
    return global_memories, project_memories


def _results_to_item_records(
    *,
    results: list[dict[str, Any]],
    batch_record_id: str,
    project_id: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for r in results:
        fields: dict[str, Any] = {
            "标题": r.get("title", ""),
            "正文": r.get("body", ""),
            "关键词": ", ".join(r.get("keywords") or []),
            "引擎": r.get("ai_engine", ""),
            "版本": 1,
            "状态": "待审核",
            "批次": [batch_record_id],
            "项目": [project_id],
        }
        if r.get("error"):
            fields[_ITEM_ERROR] = str(r["error"])
        records.append({"fields": fields})
    return records
