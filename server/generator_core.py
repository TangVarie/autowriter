"""Thin wrapper around the top-level ``generator`` module.

The server only needs the single-pass generation path (三省六部 is dropped
per the plan). Everything else — Claude / Gemini clients, JSON parsing,
retry logic — is reused directly from ``generator.py`` so improvements
to parsing / retry only have to be made in one place.

Note: importing ``generator`` triggers ``import config`` at the top level.
That config module has a ``try: import streamlit`` fallback wrapped in a
bare ``except Exception``, so running without streamlit installed is safe.
"""

from __future__ import annotations

from typing import Any

import generator as _gen


def result_to_dict(r: _gen.GenerationResult) -> dict[str, Any]:
    return {
        "title": r.title,
        "body": r.body,
        "keywords": list(r.keywords or []),
        "ai_engine": r.ai_engine,
        "token_usage": dict(r.token_usage or {}),
        "error": r.error,
        "success": r.success,
    }


def run_generation(
    *,
    system_prompt: str,
    tactic: str,
    count: int,
    engines: list[str],
    target_audience: str = "",
    key_messages: str = "",
    tone: str = "",
    extra_instructions: str = "",
    claude_model: str = "",
    gemini_model: str = "",
    claude_thinking: bool = False,
    gemini_thinking: bool = False,
    historical_titles: list[str] | None = None,
    images: list[dict] | None = None,
) -> list[dict[str, Any]]:
    """Run a single generation round and return a flat list of result dicts.

    ``generate_batch`` returns ``count`` slots, each holding one version per
    engine. We flatten that into ``count * len(engines)`` rows so each result
    becomes one record in the Bitable Items table.

    ``images``: list of {"media_type": str, "data": base64-str} — passed to
    both Claude and Gemini as vision input (applied to every call in this
    batch; set once per project/batch).
    """
    engine_models: dict[str, str] = {}
    if claude_model:
        engine_models["claude"] = claude_model
    if gemini_model:
        engine_models["gemini"] = gemini_model

    slots = _gen.generate_batch(
        system_prompt=system_prompt,
        tactic=tactic,
        count=count,
        engines=engines,
        target_audience=target_audience,
        key_messages=key_messages,
        tone=tone,
        extra_instructions=extra_instructions,
        images=images,
        historical_titles=historical_titles or [],
        use_thinking=claude_thinking,
        engine_models=engine_models or None,
        gemini_use_thinking=gemini_thinking,
    )

    flat: list[dict[str, Any]] = []
    for slot in slots:
        for version in slot.get("versions", []):
            flat.append(result_to_dict(version))
    return flat


def run_multi_role(
    *,
    system_prompt: str,
    tactic: str,
    count: int,
    engines: list[str],
    target_audience: str = "",
    key_messages: str = "",
    tone: str = "",
    extra_instructions: str = "",
    claude_model: str = "",
    gemini_model: str = "",
    claude_thinking: bool = False,
    gemini_thinking: bool = False,
    historical_titles: list[str] | None = None,
    images: list[dict] | None = None,
    custom_roles: list[dict] | None = None,
    n_roles: int = 3,
) -> list[dict[str, Any]]:
    """三省六部 pipeline — multi-role × multi-engine parallel drafting,
    AI selection of the best per slot, then 六部 structured refinement.

    Returns a flat list of ``count`` result dicts (one per slot after
    selection + refinement) augmented with ``ai_review_notes`` from the
    evaluator. See ``generator.generate_batch_multi_role`` for the
    underlying implementation.
    """
    engine_models: dict[str, str] = {}
    if claude_model:
        engine_models["claude"] = claude_model
    if gemini_model:
        engine_models["gemini"] = gemini_model

    slots = _gen.generate_batch_multi_role(
        system_prompt=system_prompt,
        tactic=tactic,
        target_audience=target_audience,
        key_messages=key_messages,
        tone=tone,
        extra_instructions=extra_instructions,
        count=count,
        images=images,
        historical_titles=historical_titles or [],
        engines=engines,
        engine_models=engine_models or None,
        use_thinking=claude_thinking,
        gemini_use_thinking=gemini_thinking,
        custom_roles=custom_roles,
        n_roles=n_roles,
    )

    results: list[dict[str, Any]] = []
    for slot in slots:
        versions = slot.get("versions", [])
        if not versions:
            continue
        winner = versions[0]
        entry = result_to_dict(winner)
        entry["ai_review_notes"] = slot.get("ai_review_notes", "") or ""
        results.append(entry)
    return results


def run_iteration(
    *,
    system_prompt: str,
    prior_title: str,
    prior_body: str,
    prior_keywords: list[str],
    feedback: str,
    engine: str = "claude",
    claude_model: str = "",
    gemini_model: str = "",
    claude_thinking: bool = False,
    gemini_thinking: bool = False,
    images: list[dict] | None = None,
) -> dict[str, Any]:
    """Re-draft a single item using the prior version + user feedback.

    Builds a two-turn conversation: assistant's prior JSON output, then the
    user's revision request. Calls ``engine.iterate`` and returns a result
    dict in the same shape as ``run_generation`` entries.
    """
    import json as _json

    prior_json = _json.dumps(
        {"title": prior_title, "body": prior_body, "keywords": prior_keywords or []},
        ensure_ascii=False,
    )
    feedback_msg = (
        f"请根据以下反馈重写这篇文案（保持 JSON 格式输出，只返回 JSON）：\n\n"
        f"{feedback.strip()}\n\n"
        "重写要求：\n"
        "- 保留系统提示词中的所有记忆与调教要求\n"
        "- 针对反馈做实质性改动，不要小修小补\n"
        "- 仍然输出 {\"title\":..., \"body\":..., \"keywords\":[...]} 这个 JSON"
    )
    messages = [
        {"role": "user", "content": "请生成一篇小红书文案。"},
        {"role": "assistant", "content": prior_json},
        {"role": "user", "content": feedback_msg},
    ]

    engine_key = engine.lower().split("/")[0]
    if engine_key not in ("claude", "gemini"):
        engine_key = "claude"
    eng = _gen.get_engine(engine_key)
    model_override = claude_model if engine_key == "claude" else gemini_model
    thinking = claude_thinking if engine_key == "claude" else gemini_thinking

    result = eng.iterate(
        messages=messages,
        system_prompt=system_prompt,
        images=images,
        use_thinking=thinking,
        model=model_override,
    )
    return result_to_dict(result)
