"""
Multi-AI generation engine for XHS Content Workstation.

Supports:
  - Mode A: single engine batch generation
  - Mode B: multi-engine parallel comparison (Claude + Gemini)
  - Mode C: cross-iteration (generate with one engine, iterate with another)

Each engine implements a common interface:
    generate(system_prompt, user_prompt, images) -> GenerationResult
    iterate(messages, system_prompt, images)     -> GenerationResult
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import anthropic
import config

# Gemini import (graceful fallback if not installed)
try:
    from google import genai as google_genai
    from google.genai import types as genai_types
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False


# ── Data structures ────────────────────────────────────────────────────────

@dataclass
class GenerationResult:
    title: str
    body: str
    keywords: list[str]
    ai_engine: str
    token_usage: dict = field(default_factory=dict)
    raw_text: str = ""
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.error is None and bool(self.title)


# ── Prompt templates ───────────────────────────────────────────────────────

_BASE_USER_PROMPT = """请根据以上系统提示词，生成一篇小红书文案。

要求：
- 标题：15–22字，吸引眼球，可以带数字或疑问句
- 正文：300–500字，口语化，有场景感
- 关键词：3–5个，用于标签

以如下 JSON 格式输出（只返回 JSON，不要有其他内容）：
{{
  "title": "文案标题",
  "body": "文案正文（换行用\\n）",
  "keywords": ["关键词1", "关键词2", "关键词3"]
}}

{extra_instructions}"""


def _make_user_prompt(
    tactic: str,
    target_audience: str = "",
    key_messages: str = "",
    tone: str = "",
    extra: str = "",
) -> str:
    parts: list[str] = [f"战术方向：{tactic}"]
    if target_audience:
        parts.append(f"目标人群：{target_audience}")
    if key_messages:
        parts.append(f"核心卖点/关键词：{key_messages}")
    if tone:
        parts.append(f"语气偏好：{tone}")

    extra_text = "\n".join(parts)
    if extra:
        extra_text += f"\n补充说明：{extra}"

    return _BASE_USER_PROMPT.format(extra_instructions=extra_text)


# ── JSON parsing helper ────────────────────────────────────────────────────

def _parse_copy_json(text: str, ai_engine: str) -> GenerationResult:
    """
    Extract and parse JSON from AI output.
    Handles: raw JSON, ```json blocks, partial wrapping text.
    """
    # Step 1: strip markdown code fences
    stripped = re.sub(r'```(?:json)?\s*', '', text).replace('```', '').strip()

    # Step 2: try each candidate source for a JSON object
    for src in (stripped, text):
        start = src.find('{')
        end = src.rfind('}')
        if start != -1 and end > start:
            try:
                data = json.loads(src[start:end + 1])
                keywords = data.get("keywords", [])
                if isinstance(keywords, str):
                    keywords = [k.strip() for k in re.split(r'[,，、\s]+', keywords) if k.strip()]
                return GenerationResult(
                    title=str(data.get("title", "")).strip(),
                    body=str(data.get("body", "")).strip(),
                    keywords=keywords,
                    ai_engine=ai_engine,
                    raw_text=text,
                )
            except (json.JSONDecodeError, ValueError):
                continue

    # Step 3: fallback — extract fields from plain Chinese text
    title_match = re.search(r'标题[：:「【]\s*(.+?)[\n」】]', text)
    body_match = re.search(r'正文[：:「【]\s*([\s\S]+?)(?:关键词[：:]|$)', text)
    kw_match = re.search(r'关键词[：:「【]\s*(.+)', text)
    if title_match or body_match:
        keywords = []
        if kw_match:
            kw_raw = kw_match.group(1).strip()
            keywords = [k.lstrip('#').strip() for k in re.split(r'[,，、\s#]+', kw_raw) if k.strip()]
        return GenerationResult(
            title=(title_match.group(1).strip() if title_match else ""),
            body=(body_match.group(1).strip() if body_match else text),
            keywords=keywords,
            ai_engine=ai_engine,
            raw_text=text,
            error="非JSON格式，已尝试提取字段",
        )

    # Step 4: complete fallback
    return GenerationResult(
        title="（解析失败）",
        body=text,
        keywords=[],
        ai_engine=ai_engine,
        raw_text=text,
        error="JSON解析失败，已返回原始文本",
    )


# ── Claude engine ──────────────────────────────────────────────────────────

class ClaudeEngine:
    def __init__(self) -> None:
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY 未配置")
        self._client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    def _build_content(
        self, text: str, images: Optional[list[dict]] = None
    ) -> list[dict]:
        """Build message content block, prepending images if provided."""
        content: list[dict] = []
        for img in (images or []):
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img["media_type"],
                    "data": img["data"],
                },
            })
        content.append({"type": "text", "text": text})
        return content

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        images: Optional[list[dict]] = None,
    ) -> GenerationResult:
        try:
            response = self._client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=2048,
                system=system_prompt,
                messages=[
                    {
                        "role": "user",
                        "content": self._build_content(user_prompt, images),
                    }
                ],
            )
            text = response.content[0].text
            result = _parse_copy_json(text, "claude")
            result.token_usage = {
                "input": response.usage.input_tokens,
                "output": response.usage.output_tokens,
            }
            return result
        except anthropic.APIError as e:
            return GenerationResult(
                title="", body="", keywords=[], ai_engine="claude",
                error=f"Claude API错误: {e}"
            )

    def iterate(
        self,
        messages: list[dict],
        system_prompt: str,
        images: Optional[list[dict]] = None,
    ) -> GenerationResult:
        """Continue a multi-turn conversation for iterative refinement."""
        # Append images to last user message if provided
        if images and messages and messages[-1]["role"] == "user":
            last_content = messages[-1]["content"]
            if isinstance(last_content, str):
                messages[-1]["content"] = self._build_content(last_content, images)
        try:
            response = self._client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=2048,
                system=system_prompt,
                messages=messages,
            )
            text = response.content[0].text
            result = _parse_copy_json(text, "claude")
            result.token_usage = {
                "input": response.usage.input_tokens,
                "output": response.usage.output_tokens,
            }
            return result
        except anthropic.APIError as e:
            return GenerationResult(
                title="", body="", keywords=[], ai_engine="claude",
                error=f"Claude迭代错误: {e}"
            )


# ── Gemini engine ──────────────────────────────────────────────────────────

class GeminiEngine:
    def __init__(self) -> None:
        if not _GEMINI_AVAILABLE:
            raise RuntimeError("google-genai 未安装，请运行 pip install google-genai")
        if not config.GOOGLE_API_KEY:
            raise RuntimeError("GOOGLE_API_KEY 未配置")
        self._client = google_genai.Client(api_key=config.GOOGLE_API_KEY)

    def _build_parts(
        self, text: str, images: Optional[list[dict]] = None
    ) -> list:
        parts = []
        for img in (images or []):
            import base64
            raw = base64.b64decode(img["data"])
            parts.append(
                genai_types.Part.from_bytes(data=raw, mime_type=img["media_type"])
            )
        parts.append(text)
        return parts

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        images: Optional[list[dict]] = None,
    ) -> GenerationResult:
        try:
            response = self._client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=self._build_parts(user_prompt, images),
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    max_output_tokens=2048,
                ),
            )
            text = response.text or ""
            result = _parse_copy_json(text, "gemini")
            return result
        except Exception as e:
            return GenerationResult(
                title="", body="", keywords=[], ai_engine="gemini",
                error=f"Gemini API错误: {e}"
            )

    def iterate(
        self,
        messages: list[dict],
        system_prompt: str,
        images: Optional[list[dict]] = None,
    ) -> GenerationResult:
        """Convert messages history to Gemini multi-turn format and continue."""
        try:
            # Build Gemini conversation history
            history = []
            for msg in messages[:-1]:
                role = "user" if msg["role"] == "user" else "model"
                content = msg["content"]
                if isinstance(content, list):
                    # Extract text from content blocks
                    content = " ".join(
                        b.get("text", "") for b in content if b.get("type") == "text"
                    )
                history.append({"role": role, "parts": [content]})

            # Last message is the current user turn
            last_msg = messages[-1]
            last_text = last_msg["content"]
            if isinstance(last_text, list):
                last_text = " ".join(
                    b.get("text", "") for b in last_text if b.get("type") == "text"
                )

            response = self._client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=history + [{"role": "user", "parts": self._build_parts(last_text, images)}],
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    max_output_tokens=2048,
                ),
            )
            text = response.text or ""
            result = _parse_copy_json(text, "gemini")
            return result
        except Exception as e:
            return GenerationResult(
                title="", body="", keywords=[], ai_engine="gemini",
                error=f"Gemini迭代错误: {e}"
            )


# ── Engine registry ────────────────────────────────────────────────────────

_ENGINE_CACHE: dict[str, object] = {}


def get_engine(engine_name: str):
    """Return a cached engine instance, creating it on first use."""
    if engine_name not in _ENGINE_CACHE:
        if engine_name == "claude":
            _ENGINE_CACHE["claude"] = ClaudeEngine()
        elif engine_name == "gemini":
            _ENGINE_CACHE["gemini"] = GeminiEngine()
        else:
            raise ValueError(f"未知引擎：{engine_name}")
    return _ENGINE_CACHE[engine_name]


AVAILABLE_ENGINES: list[str] = ["claude"] + (["gemini"] if _GEMINI_AVAILABLE else [])


# ── Batch generation ───────────────────────────────────────────────────────

def generate_batch(
    system_prompt: str,
    tactic: str,
    count: int,
    engines: list[str],
    target_audience: str = "",
    key_messages: str = "",
    tone: str = "",
    extra_instructions: str = "",
    images: Optional[list[dict]] = None,
    progress_callback=None,
) -> list[dict]:
    """
    Generate `count` copy items using specified engines.

    For multi-engine mode (len(engines) > 1), each slot gets one version
    per engine. For single-engine mode, each slot gets one version.

    Returns a list of dicts, each with:
        {
          "versions": [GenerationResult, ...],  # one per engine
          "tactic": str,
        }
    """
    user_prompt = _make_user_prompt(
        tactic=tactic,
        target_audience=target_audience,
        key_messages=key_messages,
        tone=tone,
        extra=extra_instructions,
    )

    results: list[dict] = []
    total = count * len(engines)
    done = 0

    for i in range(count):
        slot_versions: list[GenerationResult] = []
        for engine_name in engines:
            try:
                engine = get_engine(engine_name)
                result = engine.generate(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    images=images,
                )
            except Exception as e:
                result = GenerationResult(
                    title="", body="", keywords=[], ai_engine=engine_name,
                    error=str(e)
                )
            slot_versions.append(result)
            done += 1
            if progress_callback:
                progress_callback(done / total, f"正在生成第 {i+1}/{count} 篇…")
            # Small delay to avoid rate limiting
            time.sleep(0.2)

        results.append({"versions": slot_versions, "tactic": tactic})

    return results


# ── Iterative refinement ───────────────────────────────────────────────────

def build_iteration_messages(
    original_user_prompt: str,
    versions: list[dict],
    feedback: str,
) -> list[dict]:
    """
    Build the messages array for an iterative refinement call.

    versions is a list of {"version_num": int, "body": str, "title": str}
    sorted ascending by version_num.
    """
    messages: list[dict] = []

    for v in sorted(versions, key=lambda x: x.get("version_num", 0)):
        if v.get("feedback"):
            # User gave feedback before this version
            messages.append({"role": "user", "content": v["feedback"]})
        else:
            # First version: use the original generation prompt
            messages.append({"role": "user", "content": original_user_prompt})
        # AI response for this version
        assistant_text = json.dumps(
            {
                "title": v.get("title", ""),
                "body": v.get("body", ""),
                "keywords": v.get("keywords", []),
            },
            ensure_ascii=False,
        )
        messages.append({"role": "assistant", "content": assistant_text})

    # New user turn with the current feedback
    messages.append({"role": "user", "content": feedback})

    # Trim if context is getting too long (keep last 2 rounds + new feedback)
    MAX_ROUNDS = 3  # each round = 2 messages
    if len(messages) > MAX_ROUNDS * 2 + 1:
        # Keep the first user message (original prompt) + last N rounds
        messages = messages[:1] + messages[-(MAX_ROUNDS * 2):]

    return messages


def iterate_copy(
    system_prompt: str,
    original_user_prompt: str,
    version_history: list[dict],
    feedback: str,
    engine_name: str = "claude",
    images: Optional[list[dict]] = None,
) -> GenerationResult:
    """Refine a copy item based on feedback."""
    messages = build_iteration_messages(
        original_user_prompt, version_history, feedback
    )
    try:
        engine = get_engine(engine_name)
        return engine.iterate(messages, system_prompt=system_prompt, images=images)
    except Exception as e:
        return GenerationResult(
            title="", body="", keywords=[], ai_engine=engine_name,
            error=str(e)
        )


# ── User prompt reconstruction ─────────────────────────────────────────────

def reconstruct_user_prompt(batch_params: dict, tactic: str) -> str:
    """Re-build the original user prompt from stored batch params."""
    params = batch_params if isinstance(batch_params, dict) else {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except Exception:
            params = {}
    return _make_user_prompt(
        tactic=tactic,
        target_audience=params.get("target_audience", ""),
        key_messages=params.get("key_messages", ""),
        tone=params.get("tone", ""),
        extra=params.get("extra_instructions", ""),
    )
