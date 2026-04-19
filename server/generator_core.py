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

from dataclasses import asdict
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
) -> list[dict[str, Any]]:
    """Run a single generation round and return a flat list of result dicts.

    generate_batch returns ``count`` slots, each holding one version per engine.
    We flatten that into ``count * len(engines)`` rows so each result becomes
    one record in the Bitable Items table.
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
        images=None,
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
