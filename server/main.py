"""FastAPI app that Feishu Bitable automations can POST to.

Endpoint surface:
  GET  /healthz          — liveness probe (for Railway / uptime pingers)
  POST /generate         — triggers a batch (body: {"batch_record_id": "..."})

The /generate handler returns 202 immediately and runs the actual generation
in a BackgroundTask, so Feishu automation does not hit the webhook timeout.
Status + errors are written back to the Batches row in Bitable.

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
    link_ids_of,
    multi_select_of,
    single_select_of,
    text_of,
)
from .generator_core import run_generation
from .prompt_builder import build_system_prompt, parse_examples_text

log = logging.getLogger("autowriter.server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="AutoWriter Feishu Backend")
_client = BitableClient()


# ── Request models ──────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    batch_record_id: str = Field(..., description="Record ID of the row in the Batches table")


# ── Routes ──────────────────────────────────────────────────────────────

@app.get("/healthz")
def healthz() -> dict[str, Any]:
    missing = config.validate()
    return {"ok": not missing, "missing_env": missing}


@app.post("/generate", status_code=202)
def generate(
    req: GenerateRequest,
    background: BackgroundTasks,
    x_autowriter_token: str | None = Header(default=None),
) -> dict[str, Any]:
    if config.WEBHOOK_SHARED_SECRET:
        if x_autowriter_token != config.WEBHOOK_SHARED_SECRET:
            raise HTTPException(status_code=401, detail="bad token")

    missing = config.validate()
    if missing:
        raise HTTPException(status_code=500, detail=f"missing env: {missing}")

    background.add_task(_run_batch, req.batch_record_id)
    return {"accepted": True, "batch_record_id": req.batch_record_id}


# ── Orchestration ──────────────────────────────────────────────────────

_STATUS_FIELD = "状态"
_ERROR_FIELD = "错误信息"


def _mark_batch(record_id: str, status: str, error: str | None = None) -> None:
    fields: dict[str, Any] = {_STATUS_FIELD: status}
    if error is not None:
        fields[_ERROR_FIELD] = error[:2000]  # Bitable text field cap is generous; keep room
    try:
        _client.update_record(config.FEISHU_TABLE_BATCHES, record_id, fields)
    except Exception as e:
        log.exception("failed to mark batch %s as %s: %s", record_id, status, e)


def _run_batch(batch_record_id: str) -> None:
    """Full generation pipeline for one Batches row."""
    try:
        _mark_batch(batch_record_id, "生成中", error="")
        batch_row = _client.get_record(config.FEISHU_TABLE_BATCHES, batch_record_id)
        batch_fields = batch_row.get("fields", {}) or {}

        project_ids = link_ids_of(batch_fields.get("项目"))
        if not project_ids:
            raise ValueError("批次未关联项目")
        project_id = project_ids[0]

        project_row = _client.get_record(config.FEISHU_TABLE_PROJECTS, project_id)
        project_fields = project_row.get("fields", {}) or {}

        system_prompt = text_of(project_fields.get("system_prompt"))
        if not system_prompt.strip():
            raise ValueError("项目 system_prompt 为空")

        positive_examples = parse_examples_text(text_of(project_fields.get("正面示例")))
        negative_examples = parse_examples_text(text_of(project_fields.get("负面示例")))

        # Memory filter: scope=全局 enabled=TRUE  ∪  项目 = this_project AND enabled=TRUE
        memories = _load_memories(project_id) if config.FEISHU_TABLE_MEMORIES else ([], [])
        global_memories, project_memories = memories

        full_system_prompt = build_system_prompt(
            base_prompt=system_prompt,
            global_memories=global_memories,
            project_memories=project_memories,
            positive_examples=positive_examples,
            negative_examples=negative_examples,
        )

        tactic = single_select_of(batch_fields.get("tactic")) or text_of(batch_fields.get("tactic"))
        raw_count = batch_fields.get("数量") or 1
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            count = 1
        count = max(1, min(count, 50))

        engines_raw = multi_select_of(batch_fields.get("引擎")) or ["claude"]
        engines = [e.lower().split("/")[0] for e in engines_raw]
        engines = [e for e in engines if e in ("claude", "gemini")]
        if not engines:
            engines = ["claude"]

        target_audience = text_of(batch_fields.get("目标人群"))
        key_messages = text_of(batch_fields.get("核心卖点"))
        tone = text_of(batch_fields.get("语气"))
        extra = text_of(batch_fields.get("补充说明"))

        log.info(
            "batch %s: project=%s tactic=%s count=%d engines=%s",
            batch_record_id, project_id, tactic, count, engines,
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
                batch_record_id,
                "失败",
                error=f"全部生成失败。示例：{failed[0].get('error') or '无标题输出'}",
            )
        else:
            note = ""
            if failed:
                note = f"{len(failed)}/{len(results)} 条失败"
            _mark_batch(batch_record_id, "完成", error=note)

    except Exception as e:  # noqa: BLE001
        log.exception("batch %s failed: %s", batch_record_id, e)
        _mark_batch(batch_record_id, "失败", error=f"{type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}")


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
            "状态": "待审核",
            "批次": [batch_record_id],
            "项目": [project_id],
        }
        if r.get("error"):
            fields["错误信息"] = str(r["error"])
        records.append({"fields": fields})
    return records
