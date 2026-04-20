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
import random
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

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

def _make_user_prompt(
    tactic: str,
    target_audience: str = "",
    key_messages: str = "",
    tone: str = "",
    extra: str = "",
    count: int = 1,
) -> str:
    parts: list[str] = []
    if tactic:
        parts.append(f"战术方向：{tactic}")
    if target_audience:
        parts.append(f"目标人群：{target_audience}")
    if key_messages:
        parts.append(f"核心卖点/关键词：{key_messages}")
    if tone:
        parts.append(f"语气偏好：{tone}")
    if extra:
        parts.append(f"补充说明：{extra}")

    context = "\n".join(parts)
    if context:
        context += "\n\n"

    if count == 1:
        return (
            f"{context}请根据以上系统提示词，生成一篇小红书文案。\n\n"
            "要求：\n"
            "- 标题：15–22字，吸引眼球，可以带数字或疑问句\n"
            "- 正文：300–500字，口语化，有场景感\n"
            "- 关键词：3–5个，用于标签\n\n"
            "以如下 JSON 格式输出（只返回 JSON，不要有其他内容）：\n"
            '{\n  "title": "文案标题",\n  "body": "文案正文（换行用\\n）",\n'
            '  "keywords": ["关键词1", "关键词2", "关键词3"]\n}'
        )
    else:
        return (
            f"{context}请根据以上系统提示词，生成 {count} 篇风格各异的小红书文案。\n\n"
            "要求：\n"
            "- 每篇标题：15–22字，吸引眼球，可以带数字或疑问句\n"
            "- 每篇正文：300–500字，口语化，有场景感\n"
            "- 每篇关键词：3–5个，用于标签\n"
            f"- {count} 篇之间必须角度各异、场景不同、标题句式有差异，不能重复\n\n"
            f"以如下 JSON 数组格式输出（只返回包含 {count} 个元素的 JSON 数组，不要有其他内容）：\n"
            "[\n"
            '  {"title": "文案标题", "body": "文案正文（换行用\\n）", "keywords": ["关键词1", "关键词2"]},\n'
            "  ...\n"
            "]"
        )


# ── Anthropic retry helper ─────────────────────────────────────────────────

def _call_with_retry(call_fn, max_retries: int = 5):
    """
    Call an Anthropic API callable with exponential backoff.

    Retries on:
      - RateLimitError (429)
      - APIStatusError with transient codes: 429, 502, 503, 529
      - APIConnectionError / APITimeoutError (connection dropped, nginx 502)
    """
    delay = 2
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return call_fn()
        except anthropic.RateLimitError as e:
            last_error = e
        except anthropic.APIStatusError as e:
            if e.status_code in (429, 502, 503, 529):
                last_error = e
                if e.status_code == 429:
                    delay = max(delay, 5)
            else:
                raise
        except anthropic.APIConnectionError as e:
            last_error = e
        if attempt < max_retries:
            time.sleep(delay)
            delay = min(delay * 2, 60)
    raise last_error  # type: ignore[misc]


# ── JSON parsing helper ────────────────────────────────────────────────────

def _fix_json_newlines(s: str) -> str:
    """Escape bare newlines/tabs inside JSON string values (common AI output bug)."""
    result: list[str] = []
    in_string = False
    escaped = False
    for ch in s:
        if escaped:
            result.append(ch)
            escaped = False
        elif ch == '\\' and in_string:
            result.append(ch)
            escaped = True
        elif ch == '"':
            result.append(ch)
            in_string = not in_string
        elif in_string and ch == '\n':
            result.append('\\n')
        elif in_string and ch == '\r':
            result.append('\\r')
        elif in_string and ch == '\t':
            result.append('\\t')
        else:
            result.append(ch)
    return ''.join(result)


def _escape_inner_quotes(s: str) -> str:
    """Escape all unescaped ASCII double-quotes within a JSON string value body."""
    out: list[str] = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == '\\' and i + 1 < len(s):
            out.append(c); out.append(s[i + 1]); i += 2
        elif c == '"':
            out.append('\\"'); i += 1
        else:
            out.append(c); i += 1
    return ''.join(out)


def _repair_json_list_field_quotes(s: str) -> str:
    """
    Repair unescaped ASCII double-quotes inside 'title' and 'body' field values
    across *all* elements in a JSON array (uses re.sub for global replacement).
    """
    def _fix(m: re.Match) -> str:
        return m.group(1) + _escape_inner_quotes(m.group(2)) + m.group(3)

    s = re.sub(
        r'("title"\s*:\s*")(.*?)(",\s*"body")',
        _fix, s, flags=re.DOTALL,
    )
    s = re.sub(
        r'("body"\s*:\s*")(.*?)(",\s*"keywords")',
        _fix, s, flags=re.DOTALL,
    )
    return s


def _repair_json_field_quotes(s: str) -> str:
    """
    Repair unescaped ASCII double-quotes inside 'title' and 'body' field values.
    Handles the common Claude output pattern where dialogue / emphasis uses
    plain " chars inside the JSON string, breaking json.loads().
    Field order assumed: title → body → keywords (matches our prompt schema).
    """
    # Fix title field: content between  "title": "..."  and  ","body"
    tm = re.search(r'("title"\s*:\s*")(.*?)(",\s*"body")', s, re.DOTALL)
    if tm:
        s = s[:tm.start(2)] + _escape_inner_quotes(tm.group(2)) + s[tm.end(2):]
    # Fix body field: content between  "body": "..."  and  ","keywords"
    bm = re.search(r'("body"\s*:\s*")(.*?)(",\s*"keywords")', s, re.DOTALL)
    if bm:
        s = s[:bm.start(2)] + _escape_inner_quotes(bm.group(2)) + s[bm.end(2):]
    return s


def _try_parse_dict(s: str, ai_engine: str, raw_text: str) -> Optional["GenerationResult"]:
    """Attempt to parse a string as a JSON dict into a GenerationResult. Returns None on failure."""
    try:
        data = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        try:
            data = json.loads(_fix_json_newlines(s))
        except (json.JSONDecodeError, ValueError):
            # Last resort: repair unescaped quotes inside field values (e.g. dialogue "...")
            try:
                data = json.loads(_repair_json_field_quotes(_fix_json_newlines(s)))
            except (json.JSONDecodeError, ValueError):
                return None
    if not isinstance(data, dict):
        return None
    keywords = data.get("keywords", [])
    if isinstance(keywords, str):
        keywords = [k.strip() for k in re.split(r'[,，、\s]+', keywords) if k.strip()]
    return GenerationResult(
        title=str(data.get("title", "")).strip(),
        body=str(data.get("body", "")).strip(),
        keywords=keywords,
        ai_engine=ai_engine,
        raw_text=raw_text,
    )


def _parse_copy_json(text: str, ai_engine: str) -> GenerationResult:
    """
    Extract and parse JSON from AI output.
    Handles: raw JSON, ```json blocks, partial wrapping text,
    JSON wrapped in outer quotes, bare newlines inside string values.
    """
    # Step 1: strip markdown code fences
    stripped = re.sub(r'```(?:json)?\s*', '', text).replace('```', '').strip()

    # Step 2: extract the {...} block and try to parse it (with and without newline fix)
    for src in (stripped, text):
        start = src.find('{')
        end = src.rfind('}')
        if start == -1 or end <= start:
            # No {} block found — maybe outer quotes wrap everything
            # Strip one level of outer quotes and retry
            for q in ('"', "'"):
                s = src.strip()
                if s.startswith(q) and s.endswith(q) and len(s) > 1:
                    inner = s[1:-1].strip()
                    inner_start = inner.find('{')
                    inner_end = inner.rfind('}')
                    if inner_start != -1 and inner_end > inner_start:
                        src = inner
                        start = inner_start
                        end = inner_end
                        break
            else:
                continue

        json_str = src[start:end + 1]
        result = _try_parse_dict(json_str, ai_engine, text)
        if result is not None:
            return result

    # Step 3: fallback — extract fields from plain Chinese text
    title_match = re.search(r'标题[：:「【]\s*(.+?)[\n」】]', text)
    body_match = re.search(r'正文[：:「【]\s*([\s\S]+?)(?:关键词[：:]|$)', text)
    kw_match = re.search(r'关键词[：:「【]\s*(.+)', text)
    if title_match or body_match:
        keywords: list[str] = []
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


def _parse_copy_json_list(text: str, count: int, ai_engine: str) -> list[GenerationResult]:
    """
    Parse a JSON array of N copy items from AI output.
    Falls back to single-item parse if the array can't be found.
    """
    stripped = re.sub(r'```(?:json)?\s*', '', text).replace('```', '').strip()

    for src in (stripped, text):
        start = src.find('[')
        end = src.rfind(']')
        if start == -1 or end <= start:
            continue
        json_str = src[start:end + 1]
        data = None
        repaired = _repair_json_list_field_quotes(json_str)
        for candidate in (
            json_str,
            _fix_json_newlines(json_str),
            repaired,
            _fix_json_newlines(repaired),
        ):
            try:
                data = json.loads(candidate)
                break
            except (json.JSONDecodeError, ValueError):
                pass
        if not isinstance(data, list):
            continue

        results: list[GenerationResult] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            keywords = item.get("keywords", [])
            if isinstance(keywords, str):
                keywords = [k.strip() for k in re.split(r'[,，、\s]+', keywords) if k.strip()]
            results.append(GenerationResult(
                title=str(item.get("title", "")).strip(),
                body=str(item.get("body", "")).strip(),
                keywords=keywords,
                ai_engine=ai_engine,
                raw_text=text,
            ))
        if results:
            # Pad if AI returned fewer items than requested
            while len(results) < count:
                results.append(GenerationResult(
                    title="", body="", keywords=[], ai_engine=ai_engine,
                    raw_text=text,
                    error=f"AI仅返回了{len(results)}篇（期望{count}篇）",
                ))
            return results[:count]

    # Fallback: single-item parse
    single = _parse_copy_json(text, ai_engine)
    results = [single]
    while len(results) < count:
        results.append(GenerationResult(
            title="", body="", keywords=[], ai_engine=ai_engine,
            error="无法解析多篇格式，仅返回1篇",
        ))
    return results


# ── Claude engine ──────────────────────────────────────────────────────────

def _extract_text_from_response(response) -> str:
    """Extract the first text block, skipping thinking blocks."""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


class ClaudeEngine:
    def __init__(self) -> None:
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY 未配置")
        client_kwargs: dict = {"api_key": config.ANTHROPIC_API_KEY}
        if config.ANTHROPIC_BASE_URL:
            client_kwargs["base_url"] = config.ANTHROPIC_BASE_URL
        self._client = anthropic.Anthropic(**client_kwargs)

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

    def _make_params(self, model: str, use_thinking: bool = False, count: int = 1) -> dict:
        # Thinking mode is selected via model name (e.g. *-thinking) through the
        # proxy, not via the `thinking` API parameter. The flag is kept in the
        # signature only for call-site compatibility.
        return {"model": model, "max_tokens": max(2048, count * 1500)}

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        images: Optional[list[dict]] = None,
        use_thinking: bool = False,
        model: str = "",
        count: int = 1,
    ) -> list[GenerationResult]:
        model = model or config.CLAUDE_MODEL
        params = self._make_params(model, use_thinking, count)
        def _call():
            with self._client.messages.stream(
                **params,
                system=system_prompt,
                messages=[{"role": "user", "content": self._build_content(user_prompt, images)}],
            ) as stream:
                return stream.get_final_message()
        try:
            response = _call_with_retry(_call)
            text = _extract_text_from_response(response)
            token_usage = {
                "input": response.usage.input_tokens,
                "output": response.usage.output_tokens,
            }
            results = _parse_copy_json_list(text, count, f"claude/{model}") if count > 1 \
                else [_parse_copy_json(text, f"claude/{model}")]
            for r in results:
                r.token_usage = token_usage
            return results
        except anthropic.APIError as e:
            return [GenerationResult(
                title="", body="", keywords=[], ai_engine=f"claude/{model}",
                error=f"Claude API错误: {e}"
            )] * count

    def iterate(
        self,
        messages: list[dict],
        system_prompt: str,
        images: Optional[list[dict]] = None,
        use_thinking: bool = False,
        model: str = "",
    ) -> GenerationResult:
        """Continue a multi-turn conversation for iterative refinement."""
        model = model or config.CLAUDE_MODEL
        messages = [m.copy() for m in messages]
        if images and messages and messages[-1]["role"] == "user":
            last_content = messages[-1]["content"]
            if isinstance(last_content, str):
                messages[-1]["content"] = self._build_content(last_content, images)
        params = self._make_params(model, use_thinking)
        def _call():
            with self._client.messages.stream(
                **params,
                system=system_prompt,
                messages=messages,
            ) as stream:
                return stream.get_final_message()
        try:
            response = _call_with_retry(_call)
            text = _extract_text_from_response(response)
            result = _parse_copy_json(text, f"claude/{model}")
            result.token_usage = {
                "input": response.usage.input_tokens,
                "output": response.usage.output_tokens,
            }
            return result
        except anthropic.APIError as e:
            return GenerationResult(
                title="", body="", keywords=[], ai_engine=f"claude/{model}",
                error=f"Claude迭代错误: {e}"
            )


# ── Gemini engine ──────────────────────────────────────────────────────────

class GeminiEngine:
    def __init__(self) -> None:
        if not _GEMINI_AVAILABLE:
            raise RuntimeError("google-genai 未安装，请运行 pip install google-genai")
        if not config.GOOGLE_API_KEY:
            raise RuntimeError("GOOGLE_API_KEY 未配置")
        client_kwargs: dict = {"api_key": config.GOOGLE_API_KEY}
        if config.GOOGLE_BASE_URL:
            client_kwargs["http_options"] = {"base_url": config.GOOGLE_BASE_URL}
        self._client = google_genai.Client(**client_kwargs)

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

    def _make_generate_config(self, use_thinking: bool, model: str = "", count: int = 1) -> "genai_types.GenerateContentConfig":
        """
        Build GenerateContentConfig.

        Thinking-only models (Gemini 3.x, Gemini 2.5 Pro): thinking is always
        on; they reject ThinkingConfig(thinking_budget=0). We omit
        ThinkingConfig to let the model run with its built-in default when
        the caller turns the toggle off.
        Gemini 2.5 Flash / Flash-Lite: thinking can be disabled with
        ThinkingConfig(thinking_budget=0).

        When use_thinking=True we always request dynamic budget (-1).
        """
        kwargs: dict = {"max_output_tokens": max(8192, count * 2000)}
        thinking_only = ("gemini-3" in model) or ("2.5-pro" in model)
        try:
            if use_thinking:
                kwargs["thinking_config"] = genai_types.ThinkingConfig(thinking_budget=-1)
            elif not thinking_only:
                # Only flash / flash-lite accept budget=0; pro / 3.x reject it
                kwargs["thinking_config"] = genai_types.ThinkingConfig(thinking_budget=0)
            # thinking_only + use_thinking=False: omit ThinkingConfig entirely
        except AttributeError:
            pass
        return genai_types.GenerateContentConfig(**kwargs)

    def _parse_gemini_response(self, response, model: str) -> GenerationResult:
        text = response.text or ""
        result = _parse_copy_json(text, f"gemini/{model}")
        usage = getattr(response, "usage_metadata", None)
        if usage:
            result.token_usage = {
                "input": getattr(usage, "prompt_token_count", 0),
                "output": getattr(usage, "candidates_token_count", 0),
                "thinking": getattr(usage, "thoughts_token_count", 0),
            }
        return result

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        images: Optional[list[dict]] = None,
        use_thinking: bool = False,
        model: str = "",
        count: int = 1,
    ) -> list[GenerationResult]:
        model = model or config.GEMINI_MODEL
        gen_config = self._make_generate_config(use_thinking, model, count)
        gen_config.system_instruction = system_prompt
        try:
            response = self._client.models.generate_content(
                model=model,
                contents=self._build_parts(user_prompt, images),
                config=gen_config,
            )
            text = response.text or ""
            usage = getattr(response, "usage_metadata", None)
            token_usage = {}
            if usage:
                token_usage = {
                    "input": getattr(usage, "prompt_token_count", 0),
                    "output": getattr(usage, "candidates_token_count", 0),
                    "thinking": getattr(usage, "thoughts_token_count", 0),
                }
            results = _parse_copy_json_list(text, count, f"gemini/{model}") if count > 1 \
                else [_parse_copy_json(text, f"gemini/{model}")]
            for r in results:
                r.token_usage = token_usage
            return results
        except Exception as e:
            return [GenerationResult(
                title="", body="", keywords=[], ai_engine=f"gemini/{model}",
                error=f"Gemini API错误: {e}"
            )] * count

    def iterate(
        self,
        messages: list[dict],
        system_prompt: str,
        images: Optional[list[dict]] = None,
        use_thinking: bool = False,
        model: str = "",
    ) -> GenerationResult:
        """Convert messages history to Gemini multi-turn format and continue."""
        model = model or config.GEMINI_MODEL
        gen_config = self._make_generate_config(use_thinking, model)
        gen_config.system_instruction = system_prompt
        try:
            def _msg_to_str(content) -> str:
                if isinstance(content, list):
                    return " ".join(
                        b.get("text", "") for b in content if b.get("type") == "text"
                    )
                return str(content)

            history = [
                genai_types.Content(
                    role="user" if msg["role"] == "user" else "model",
                    parts=[genai_types.Part.from_text(text=_msg_to_str(msg["content"]))],
                )
                for msg in messages[:-1]
            ]

            last_text = _msg_to_str(messages[-1]["content"])
            import base64
            last_parts = [
                genai_types.Part.from_bytes(
                    data=base64.b64decode(img["data"]),
                    mime_type=img["media_type"],
                )
                for img in (images or [])
            ] + [genai_types.Part.from_text(text=last_text)]

            response = self._client.models.generate_content(
                model=model,
                contents=history + [genai_types.Content(role="user", parts=last_parts)],
                config=gen_config,
            )
            return self._parse_gemini_response(response, model)
        except Exception as e:
            return GenerationResult(
                title="", body="", keywords=[], ai_engine=f"gemini/{model}",
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


AVAILABLE_ENGINES: list[str] = ["claude"] + (
    ["gemini"] if _GEMINI_AVAILABLE and config.GOOGLE_API_KEY else []
)


# ── Batch generation ───────────────────────────────────────────────────────

def _build_dedup_instruction(
    generated_summaries: list[str],
    historical_titles: list[str] | None = None,
) -> str:
    """Build a dedup instruction listing previously generated angles to avoid."""
    lines: list[str] = []

    if historical_titles:
        lines.append("【历史已有文案标题（跨批次），请避免相似角度】")
        for t in historical_titles[-20:]:  # limit to last 20
            lines.append(f"- {t}")

    if generated_summaries:
        lines.append("【本批次已生成的文案，你必须选择完全不同的切入角度、场景和标题风格】")
        # Only keep recent summaries to control prompt size
        for s in generated_summaries[-20:]:
            lines.append(f"- {s}")

    if not lines:
        return ""

    lines.append(
        "\n⚠️ 重要：以上每一篇都是已有内容。"
        "你这次必须用全新的场景、情绪、人物、标题句式来写，"
        "不要重复任何已有的角度、开头方式或叙事结构。"
        "尽量差异化。"
    )
    return "\n".join(lines)


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
    historical_titles: list[str] | None = None,
    use_thinking: bool = False,
    engine_models: dict[str, str] | None = None,
    gemini_use_thinking: bool = False,
) -> list[dict]:
    """
    engine_models: optional per-engine model override, e.g.
        {"claude": "claude-opus-4-6", "gemini": "gemini-3.1-pro-preview"}
    """
    """
    Generate `count` copy items using specified engines.

    For multi-engine mode (len(engines) > 1), each slot gets one version
    per engine. For single-engine mode, each slot gets one version.

    Uses accumulated context to ensure each piece differs from previous ones
    within the batch and optionally from historical titles.

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
        count=count,
    )

    dedup_block = _build_dedup_instruction([], historical_titles)
    if dedup_block:
        user_prompt += "\n\n" + dedup_block

    # One API call per engine, each returning `count` items in a single response.
    # Engines run in parallel via ThreadPoolExecutor.
    def _call_engine(engine_name: str) -> tuple[str, list[GenerationResult]]:
        try:
            engine = get_engine(engine_name)
            model_override = (engine_models or {}).get(engine_name, "")
            thinking_flag = (
                use_thinking if engine_name == "claude"
                else (gemini_use_thinking if engine_name == "gemini" else False)
            )
            items = engine.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                images=images,
                use_thinking=thinking_flag,
                model=model_override,
                count=count,
            )
        except Exception as e:
            items = [GenerationResult(
                title="", body="", keywords=[], ai_engine=engine_name, error=str(e)
            )] * count
        return engine_name, items

    engine_results: dict[str, list[GenerationResult]] = {}
    with ThreadPoolExecutor(max_workers=len(engines)) as executor:
        futures = {executor.submit(_call_engine, eng): eng for eng in engines}
        total = len(engines)
        done = 0
        for future in as_completed(futures):
            eng, items = future.result()
            engine_results[eng] = items
            done += 1
            if progress_callback:
                progress_callback(done / total, f"已完成 {done}/{total} 个引擎…")

    # Assemble: slot i gets one version per engine
    return [
        {"versions": [engine_results[eng][i] for eng in engines], "tactic": tactic}
        for i in range(count)
    ]


# ── Multi-role drafting (三省法 · 中书省) ──────────────────────────────────

CREATIVE_ROLES_POOL: list[dict] = [
    {
        "id": "narrative",
        "name": "叙事角",
        "prompt_suffix": (
            "\n\n【本篇创作角度：叙事】"
            "以一个具体的生活场景或真实故事切入，产品/品牌自然融入叙事，不要开篇就讲卖点。"
            "让读者先被情境带入，再自然意识到这是什么。"
        ),
    },
    {
        "id": "insight",
        "name": "洞察角",
        "prompt_suffix": (
            "\n\n【本篇创作角度：洞察】"
            "从一个反直觉、出乎意料或被大多数人忽视的角度切入，制造认知惊喜或反转。"
            "让读者产生「哦原来是这样」或「这我没想到」的感受。避免常规切入方式。"
        ),
    },
    {
        "id": "empathy",
        "name": "共情角",
        "prompt_suffix": (
            "\n\n【本篇创作角度：共情】"
            "从目标用户当下最真实的情绪或生活状态出发，情绪共鸣先于产品信息。"
            "让读者觉得「这说的就是我」，然后才自然引出产品/品牌。"
        ),
    },
    {
        "id": "contrast",
        "name": "对比角",
        "prompt_suffix": (
            "\n\n【本篇创作角度：对比】"
            "以「之前 vs 之后」「有它 vs 没它」「以为 vs 实际」等对比结构切入，"
            "让改变或差异成为文案的核心张力。对比要具体可感，不要抽象泛泛。"
        ),
    },
    {
        "id": "tips",
        "name": "干货角",
        "prompt_suffix": (
            "\n\n【本篇创作角度：干货】"
            "以实用信息、技巧或方法论为主轴，产品/品牌作为解决方案自然嵌入。"
            "读者应该能带走具体可操作的内容，而不只是情绪感受。"
        ),
    },
    {
        "id": "occasion",
        "name": "场合角",
        "prompt_suffix": (
            "\n\n【本篇创作角度：场合】"
            "锁定一个具体的使用时刻、场合或生活节点（如下班后、周末早晨、聚会前夜），"
            "让产品/品牌成为那个时刻的专属搭档。场合越具体，代入感越强。"
        ),
    },
]

# Default 3 kept for backward compatibility (first three cover the widest angles)
CREATIVE_ROLES = CREATIVE_ROLES_POOL[:3]

_SELECT_SYSTEM = """\
你是一位资深小红书内容编辑，负责从多组草稿中评选最优版本。

你会收到若干组草稿，每组包含来自不同创作角度和/或不同 AI 模型的多个版本。

对每一组，选出最适合发布的一篇。评选标准（按优先级）：
1. 开头吸引力：前两行能否让人停下来继续读
2. 内容真实感：读起来像真人在说话，而不是广告稿
3. 品牌融入自然度：产品信息不突兀，不破坏阅读流
4. 读者代入感：目标用户能否在文中找到自己

多样性参考（软性）：如果多组草稿中某几篇开头方式或整体结构高度相似，在质量相近时可以倾向选更有差异的那篇——但质量明显更高的草稿应当优先入选，不必为了差异化而放弃更好的内容。

给出1-2句评审意见，说明选择理由及潜在改进点。

以 JSON 数组格式回复，顺序与输入完全一致：
[{"best_index": <整数>, "notes": "评审意见"}, ...]
只返回 JSON 数组，不要任何其他文字。"""


def _select_best_drafts_batch(
    brief: str,
    all_slot_drafts: list[list[GenerationResult]],
    draft_labels: list[str],
) -> list[tuple[int, str]]:
    """
    One Claude call evaluates all slots at once.
    draft_labels: human-readable label per draft, e.g. ["叙事角·Claude", "洞察角·Gemini", ...]
    Returns [(best_index, notes), ...] in slot order.
    """
    slot_blocks: list[str] = []
    for i, slot_drafts in enumerate(all_slot_drafts, 1):
        lines = [f"第{i}组："]
        for j, draft in enumerate(slot_drafts):
            label = draft_labels[j] if j < len(draft_labels) else f"草稿{j+1}"
            body_preview = (draft.body or "")[:200].split("\n")[0]
            lines.append(f"  草稿{j+1}（{label}）：标题：{draft.title}  正文节选：{body_preview}")
        slot_blocks.append("\n".join(lines))

    user_content = f"创作任务简报：\n{brief}\n\n" + "\n\n".join(slot_blocks)

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=512,
        system=_SELECT_SYSTEM,
        messages=[{"role": "user", "content": user_content}],
    )
    raw = resp.content[0].text.strip()
    try:
        data = json.loads(re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip())
        if isinstance(data, list):
            return [
                (int(item.get("best_index", 0)), str(item.get("notes", "")))
                for item in data
            ]
    except Exception:
        pass
    return [(0, "") for _ in all_slot_drafts]



# ── 尚书省 · 六部精炼 ──────────────────────────────────────────────────────

_REFINE_SYSTEM_SUFFIX = """

---【尚书省六部精炼指令】---
你已进入六部精炼阶段。结合上方的创作任务背景，对草稿依次执行六部审查并输出精炼后的版本：

• 礼部（格式规范）：段落间空行，末尾话题标签每个以单个#开头（如 #话题名），不要##，可加1-3个；字数服从内容本身的需要，可以是两三句的短文案也可以是长篇，不做硬性限制
• 工部（结构优化）：开头2-3行钩子必须够强，段落节奏流畅，收尾自然
• 户部（价值密度）：删除套话和无效铺垫，每段都有实质内容
• 吏部（受众校准 + 记忆合规）：用目标受众真实说话的方式，不用品牌发布腔；逐条核查 System Prompt 中的项目记忆和调校笔记，确认内容没有违反任何已知约束和审美偏好，如有违反则修正
• 兵部（角度差异）：若开头方式过于常见，做一次微调使其更独特
• 刑部（平台合规）：符合小红书平台基本内容准则，不涉及明显违法内容或虚假信息；品牌层面的禁忌词、敏感表述等合规要求以 System Prompt 中的规则为准，此处不做额外限制

核心要求：保留草稿的创意内核和角色风格，只做必要精炼，不改变内容方向。

输出精炼后的版本（JSON）：
{"title": "...", "body": "...", "keywords": [...]}
只返回 JSON，不要任何前缀或说明。"""


def _refine_drafts_batch(
    system_prompt: str,
    brief: str,
    drafts: list[GenerationResult],
    model: str = "",
) -> list[GenerationResult]:
    """
    Run 六部 structured refinement on all winning drafts in parallel.
    Uses the project system_prompt as brand context + _REFINE_SYSTEM_SUFFIX as instructions.
    Falls back to original draft on any failure.
    """
    if not drafts:
        return drafts

    refine_system = system_prompt.strip() + _REFINE_SYSTEM_SUFFIX

    def _refine_one(idx: int, draft: GenerationResult) -> tuple[int, GenerationResult]:
        user_content = (
            f"创作任务简报：\n{brief}\n\n"
            f"待精炼草稿：\n"
            f"标题：{draft.title}\n\n"
            f"正文：{draft.body}\n\n"
            f"关键词：{json.dumps(draft.keywords or [], ensure_ascii=False)}"
        )
        try:
            client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
            resp = client.messages.create(
                model=model or config.CLAUDE_MODEL,
                max_tokens=2048,
                system=refine_system,
                messages=[{"role": "user", "content": user_content}],
            )
            raw = resp.content[0].text.strip()
            cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
            refined = _try_parse_dict(cleaned, draft.ai_engine, raw)
            if refined and (refined.title or refined.body):
                return idx, GenerationResult(
                    title=refined.title or draft.title,
                    body=refined.body or draft.body,
                    keywords=refined.keywords or draft.keywords,
                    ai_engine=draft.ai_engine,
                    raw_text=raw,
                    token_usage={
                        "input_tokens": resp.usage.input_tokens,
                        "output_tokens": resp.usage.output_tokens,
                    },
                )
        except Exception:
            pass
        return idx, draft  # fallback to original

    result_list: list[GenerationResult] = list(drafts)  # pre-fill with originals
    with ThreadPoolExecutor(max_workers=len(drafts)) as executor:
        futures = {executor.submit(_refine_one, i, d): i for i, d in enumerate(drafts)}
        for future in as_completed(futures):
            i, refined = future.result()
            result_list[i] = refined
    return result_list


def generate_batch_multi_role(
    system_prompt: str,
    tactic: str = "",
    target_audience: str = "",
    key_messages: str = "",
    tone: str = "",
    extra_instructions: str = "",
    count: int = 5,
    images: list | None = None,
    progress_callback=None,
    historical_titles: list[str] | None = None,
    engines: list[str] | None = None,
    engine_models: dict[str, str] | None = None,
    use_thinking: bool = False,
    gemini_use_thinking: bool = False,
    custom_roles: list[dict] | None = None,
    n_roles: int = 3,
) -> list[dict]:
    """
    Generate `count` items using multi-role × multi-engine parallel drafting (三省法).

    All combinations of (role × engine) run in parallel — e.g. 3 roles × 2 engines
    = 6 concurrent API calls, same wall-clock time as a single call.
    One lightweight Claude selection call then picks the best draft per slot.

    custom_roles: if set, used as the role pool instead of CREATIVE_ROLES_POOL
    n_roles: how many roles to randomly sample from the pool each run (default 3)
    engines: list of engine names to use, default ["claude"]
    engine_models: per-engine model override, e.g. {"claude": "claude-opus-4-6"}
    """
    pool = custom_roles if custom_roles else CREATIVE_ROLES_POOL
    k = min(n_roles, len(pool))
    roles = random.sample(pool, k) if k < len(pool) else list(pool)
    _engines = engines or ["claude"]
    _models = engine_models or {}

    base_prompt = _make_user_prompt(
        tactic=tactic,
        target_audience=target_audience,
        key_messages=key_messages,
        tone=tone,
        extra=extra_instructions,
        count=count,
    )
    dedup_block = _build_dedup_instruction([], historical_titles)
    if dedup_block:
        base_prompt += "\n\n" + dedup_block

    tasks = [(role, eng) for role in roles for eng in _engines]
    n_tasks = len(tasks)
    if progress_callback:
        progress_callback(0.05, f"并行起草中（{len(roles)}角色 × {len(_engines)}引擎 = {n_tasks}路）…")

    # Step 1: All (role × engine) combinations run in parallel
    def _call_task(role: dict, eng_name: str) -> tuple[str, str, list[GenerationResult]]:
        engine = get_engine(eng_name)
        thinking = use_thinking if eng_name == "claude" else (gemini_use_thinking if eng_name == "gemini" else False)
        items = engine.generate(
            system_prompt=system_prompt,
            user_prompt=base_prompt + role["prompt_suffix"],
            images=images,
            use_thinking=thinking,
            model=_models.get(eng_name, ""),
            count=count,
        )
        return role["id"], eng_name, items

    # key: (role_id, eng_name) → list[GenerationResult]
    task_results: dict[tuple[str, str], list[GenerationResult]] = {}
    with ThreadPoolExecutor(max_workers=n_tasks) as executor:
        futures = {executor.submit(_call_task, role, eng): (role["id"], eng) for role, eng in tasks}
        for future in as_completed(futures):
            role_id, eng_name, items = future.result()
            task_results[(role_id, eng_name)] = items

    if progress_callback:
        progress_callback(0.85, "AI 编辑评选最优版本…")

    # Assemble per-slot draft lists and labels (order: roles first, engines second)
    draft_labels = [
        f"{role['name']}·{eng.capitalize()}"
        for role in roles for eng in _engines
    ]
    all_slot_drafts = [
        [task_results[(role["id"], eng)][i] for role in roles for eng in _engines]
        for i in range(count)
    ]

    brief = _make_user_prompt(
        tactic=tactic, target_audience=target_audience,
        key_messages=key_messages, tone=tone, extra=extra_instructions, count=1,
    )
    selections = _select_best_drafts_batch(brief, all_slot_drafts, draft_labels)

    if progress_callback:
        progress_callback(0.90, f"尚书省六部精炼中（{count}篇并行）…")

    # Step 3: 六部精炼 — parallel refinement of all winning drafts
    winning_drafts = [
        all_slot_drafts[i][best_idx]
        for i, (best_idx, _) in enumerate(selections)
    ]
    refined_drafts = _refine_drafts_batch(
        system_prompt=system_prompt,
        brief=brief,
        drafts=winning_drafts,
        model=_models.get("claude", ""),
    )

    if progress_callback:
        progress_callback(1.0, "完成")

    results = []
    for i, (best_idx, notes) in enumerate(selections):
        winner_label = draft_labels[best_idx] if best_idx < len(draft_labels) else "?"
        results.append({
            "versions": [refined_drafts[i]],
            "tactic": tactic,
            "ai_review_notes": f"【{winner_label}胜出·六部精炼】{notes}".strip(),
        })
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

    Ensures strict user/assistant alternation for API compatibility.
    """
    messages: list[dict] = []

    sorted_versions = sorted(versions, key=lambda x: x.get("version_num", 0))

    for i, v in enumerate(sorted_versions):
        # Determine user message for this version
        if i == 0:
            # First version always uses original prompt
            user_content = original_user_prompt
        elif v.get("feedback"):
            user_content = v["feedback"]
        else:
            # Subsequent version without feedback — skip to avoid
            # consecutive assistant messages
            continue

        # Only add user message if it won't create consecutive same-role messages
        if not messages or messages[-1]["role"] != "user":
            messages.append({"role": "user", "content": user_content})
        else:
            # Merge with previous user message to avoid consecutive user turns
            messages[-1]["content"] += "\n\n" + user_content

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

    # Trim if context is getting too long (keep first round + last N rounds + new feedback)
    MAX_ROUNDS = 3  # each round = 2 messages (user + assistant)
    if len(messages) > MAX_ROUNDS * 2 + 1:
        # Keep first user+assistant pair + last rounds + final user message
        messages = messages[:2] + messages[-(MAX_ROUNDS * 2 - 1):]

    return messages


def iterate_copy(
    system_prompt: str,
    original_user_prompt: str,
    version_history: list[dict],
    feedback: str,
    engine_name: str = "claude",
    images: Optional[list[dict]] = None,
    use_thinking: bool = False,
    model: str = "",
) -> GenerationResult:
    """Refine a copy item based on feedback."""
    messages = build_iteration_messages(
        original_user_prompt, version_history, feedback
    )
    try:
        engine = get_engine(engine_name)
        return engine.iterate(
            messages,
            system_prompt=system_prompt,
            images=images,
            use_thinking=(use_thinking and engine_name == "claude"),
            model=model,
        )
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
