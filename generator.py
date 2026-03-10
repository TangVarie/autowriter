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

import threading

import anthropic
import config

# Global semaphore to stay under the API provider's concurrent-request limit.
# Set to 4 to leave headroom below the provider's cap of 5.
_API_SEMAPHORE = threading.Semaphore(4)

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
    parts: list[str] = []
    if tactic:
        parts.append(f"战术方向：{tactic}")
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


# ── Anthropic retry helper ─────────────────────────────────────────────────

def _call_with_retry(call_fn, max_retries: int = 5):
    """
    Call an Anthropic API callable with exponential backoff.

    Acquires the global _API_SEMAPHORE before each attempt so that the total
    number of in-flight requests never exceeds the semaphore limit (4), staying
    safely below the provider's concurrent-request cap (5).

    Retries on:
      - RateLimitError (429)
      - APIStatusError with transient codes: 429, 502, 503, 529
      - APIConnectionError / APITimeoutError (connection dropped, nginx 502)
    """
    delay = 2
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        with _API_SEMAPHORE:
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


# ── Claude engine ──────────────────────────────────────────────────────────

def _extract_text_from_response(response) -> str:
    """Extract the first text block, skipping thinking blocks."""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


def _claude_thinking_params(model: str) -> dict:
    """
    Build the extended-thinking parameters for a Claude model.

    - claude-opus-4-6 dropped budget_tokens in favour of the "effort" parameter.
    - All other current models (Sonnet 4.6, Haiku 4.5) still use budget_tokens.
    """
    if "opus-4-6" in model:
        return {
            "max_tokens": 32000,
            "thinking": {"type": "enabled", "effort": "high"},
        }
    else:
        return {
            "max_tokens": 16000,
            "thinking": {"type": "enabled", "budget_tokens": 8000},
        }


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

    def _make_params(self, model: str, use_thinking: bool) -> dict:
        if use_thinking:
            return {"model": model, **_claude_thinking_params(model)}
        return {"model": model, "max_tokens": 2048}

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        images: Optional[list[dict]] = None,
        use_thinking: bool = False,
        model: str = "",
    ) -> GenerationResult:
        model = model or config.CLAUDE_MODEL
        params = self._make_params(model, use_thinking)
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
            result = _parse_copy_json(text, f"claude/{model}")
            result.token_usage = {
                "input": response.usage.input_tokens,
                "output": response.usage.output_tokens,
            }
            return result
        except anthropic.APIError as e:
            return GenerationResult(
                title="", body="", keywords=[], ai_engine=f"claude/{model}",
                error=f"Claude API错误: {e}"
            )

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

    def _make_generate_config(self, use_thinking: bool, model: str = "") -> "genai_types.GenerateContentConfig":
        """
        Build GenerateContentConfig.

        Gemini 3.x series (e.g. gemini-3.1-*): thinking is on by default and
        cannot be disabled with budget=0 — omit ThinkingConfig to use the
        model's own default (thinking on).
        Gemini 2.5 series: thinking opt-in via ThinkingConfig(thinking_budget>0).

        When use_thinking=True we explicitly request dynamic budget (-1).
        When use_thinking=False we only set budget=0 for models that support it
        (Gemini 2.5). For Gemini 3.x we leave ThinkingConfig out entirely.
        """
        kwargs: dict = {"max_output_tokens": 8192}
        is_gemini3 = "gemini-3" in model
        try:
            if use_thinking:
                kwargs["thinking_config"] = genai_types.ThinkingConfig(thinking_budget=-1)
            elif not is_gemini3:
                # Only pass budget=0 for 2.5 series; 3.x rejects this value
                kwargs["thinking_config"] = genai_types.ThinkingConfig(thinking_budget=0)
            # For Gemini 3.x with use_thinking=False: omit ThinkingConfig,
            # let the model run with its built-in default (thinking enabled)
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
    ) -> GenerationResult:
        model = model or config.GEMINI_MODEL
        gen_config = self._make_generate_config(use_thinking, model)
        gen_config.system_instruction = system_prompt
        try:
            response = self._client.models.generate_content(
                model=model,
                contents=self._build_parts(user_prompt, images),
                config=gen_config,
            )
            return self._parse_gemini_response(response, model)
        except Exception as e:
            return GenerationResult(
                title="", body="", keywords=[], ai_engine=f"gemini/{model}",
                error=f"Gemini API错误: {e}"
            )

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
            history = []
            for msg in messages[:-1]:
                role = "user" if msg["role"] == "user" else "model"
                content = msg["content"]
                if isinstance(content, list):
                    content = " ".join(
                        b.get("text", "") for b in content if b.get("type") == "text"
                    )
                history.append({"role": role, "parts": [content]})

            last_msg = messages[-1]
            last_text = last_msg["content"]
            if isinstance(last_text, list):
                last_text = " ".join(
                    b.get("text", "") for b in last_text if b.get("type") == "text"
                )

            response = self._client.models.generate_content(
                model=model,
                contents=history + [{"role": "user", "parts": self._build_parts(last_text, images)}],
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
    base_user_prompt = _make_user_prompt(
        tactic=tactic,
        target_audience=target_audience,
        key_messages=key_messages,
        tone=tone,
        extra=extra_instructions,
    )

    results: list[dict] = []
    total = count * len(engines)
    done = 0

    # Accumulate summaries of what we've generated so far for dedup
    generated_summaries: list[str] = []

    for i in range(count):
        # Build dedup-enhanced prompt
        dedup_block = _build_dedup_instruction(generated_summaries, historical_titles)
        if dedup_block:
            user_prompt = base_user_prompt + "\n\n" + dedup_block
        else:
            user_prompt = base_user_prompt

        slot_versions: list[GenerationResult] = []
        for engine_name in engines:
            try:
                engine = get_engine(engine_name)
                model_override = (engine_models or {}).get(engine_name, "")
                thinking_flag = (
                    use_thinking if engine_name == "claude"
                    else (gemini_use_thinking if engine_name == "gemini" else False)
                )
                result = engine.generate(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    images=images,
                    use_thinking=thinking_flag,
                    model=model_override,
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
            time.sleep(0.05)

        # Record what we generated for dedup in subsequent pieces
        for v in slot_versions:
            if v.success and v.title:
                # title + first ~30 chars of body as angle summary
                body_preview = v.body[:50].split("\n")[0] if v.body else ""
                generated_summaries.append(f"「{v.title}」— {body_preview}")

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
