"""
Configuration module for XHS Content Workstation.
Loads settings from environment variables, with fallback to st.secrets
when running on Streamlit Cloud.
"""

import os


def _get_secret(key: str, default: str = "") -> str:
    """Read from env var first, then fall back to st.secrets if available."""
    val = os.environ.get(key, "")
    if not val:
        try:
            import streamlit as st
            val = str(st.secrets.get(key, ""))
        except Exception:
            pass
    return val or default


# ── Anthropic ──────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = _get_secret("ANTHROPIC_API_KEY")
# Optional: set to a proxy base URL (e.g. https://vip.aipro.love) to route
# Claude requests through a third-party API gateway. Leave empty to use the
# official Anthropic endpoint.
ANTHROPIC_BASE_URL: str = _get_secret("ANTHROPIC_BASE_URL")

# Available Claude models: model_id -> display label
# 仅保留 Anthropic 官方当前 GA + 中转站实际支持的模型（截至 2026-05）。
# Retired 已删（Claude 3 全系列、Sonnet/Opus 4.0 一代——后者 2026-04-20 下线）。
# Thinking 模式仍走 -thinking 后缀（中转站约定，非官方 API 参数）；4-6 / 4-7
# 系列中转站未提供 thinking 变体，故不列。
CLAUDE_MODELS: dict[str, str] = {
    # ── Haiku ─────────────────────────────────────────────
    "claude-haiku-4-5-20251001":           "Haiku 4.5（最快/最省）",
    # ── Sonnet ────────────────────────────────────────────
    "claude-sonnet-4-5-20250929":          "Sonnet 4.5",
    "claude-sonnet-4-5-20250929-thinking": "Sonnet 4.5（思考）",
    "claude-sonnet-4-6":                   "Sonnet 4.6",
    # ── Opus ──────────────────────────────────────────────
    "claude-opus-4-1-20250805":            "Opus 4.1",
    "claude-opus-4-1-20250805-thinking":   "Opus 4.1（思考）",
    "claude-opus-4-5-20251101":            "Opus 4.5",
    "claude-opus-4-5-20251101-thinking":   "Opus 4.5（思考）",
    "claude-opus-4-6":                     "Opus 4.6",
    "claude-opus-4-7":                     "Opus 4.7（最强）",
}

# Default model (can be overridden via env var)
# claude-3-sonnet-20240229 was retired from most proxies; default to the
# latest Sonnet GA so backend utility calls (memory merger / calibration /
# compliance) keep working without manual config.
CLAUDE_MODEL: str = _get_secret("CLAUDE_MODEL") or "claude-sonnet-4-5-20250929"

# ── Google Gemini ──────────────────────────────────────────────────────────
GOOGLE_API_KEY: str = _get_secret("GOOGLE_API_KEY")
GOOGLE_BASE_URL: str = _get_secret("GOOGLE_BASE_URL")  # optional proxy, e.g. https://your-relay.com

# Available Gemini models: model_id -> display label
# 3.5 Flash launched GA at Google I/O 2026 (2026-05-19); 2.5 Flash remains
# the cheapest stable option.
GEMINI_MODELS: dict[str, str] = {
    "gemini-3.5-flash":       "Gemini 3.5 Flash（最新，GA）",
    "gemini-3.1-pro-preview": "Gemini 3.1 Pro Preview",
    "gemini-2.5-pro":         "Gemini 2.5 Pro（GA 稳定版）",
    "gemini-2.5-flash":       "Gemini 2.5 Flash（均衡，高吞吐）",
    "gemini-2.5-flash-lite":  "Gemini 2.5 Flash-Lite（最快最省）",
}

# Default model (can be overridden via env var)
GEMINI_MODEL: str = _get_secret("GEMINI_MODEL") or "gemini-2.5-pro"

# ── Supabase ───────────────────────────────────────────────────────────────
SUPABASE_URL: str = _get_secret("SUPABASE_URL")
SUPABASE_ANON_KEY: str = _get_secret("SUPABASE_ANON_KEY")

# ── Feishu (Lark) Webhook ──────────────────────────────────────────────────
FEISHU_WEBHOOK_URL: str = _get_secret("FEISHU_WEBHOOK_URL")

# ── Memory system thresholds ───────────────────────────────────────────────
MEMORY_AUTO_CONFIRM_THRESHOLD: int = int(
    os.environ.get("MEMORY_AUTO_CONFIRM_THRESHOLD", "3")
)

# When True (default), user-typed feedback / extra_instructions are routed
# through an AI classifier that decides per input whether it's a merge
# candidate for an existing rule, a brand-new rule, a taste observation
# (→ calibration notes), or a one-off session instruction (24h TTL).
# Turn off to fall back to the legacy "write-straight-to-session-memory"
# behavior introduced in 2.4.0.
ENABLE_MEMORY_MERGE: bool = (
    os.environ.get("ENABLE_MEMORY_MERGE", "1") not in ("0", "false", "False")
)

# Maximum number of ``severity='soft'`` rule-memories injected per scope
# (global / project).  Hard rules (compliance / brand lines) are NOT
# subject to this cap — they always feed the P0 tier in full.
#
# Lowered from 40 → 12 in 2026-05: at 40 the system prompt routinely
# carried 80+ "必须执行" lines (40 global + 40 project + calibration +
# few-shots + session), which drowned model attention and caused the
# user-reported "hard requirements not being respected" — there were so
# many same-priority "rules" the model couldn't tell which ones were
# non-negotiable.
MAX_INJECTED_MEMORIES_PER_SCOPE: int = int(
    os.environ.get("MAX_INJECTED_MEMORIES_PER_SCOPE", "12")
)

# ── 去重自动重生硬闸门（Stage B1+B2）─────────────────────────────────────
# 当文本或语义去重命中时，是否自动让单条文案重新生成？
#   ENABLE_DEDUP_REGEN=true  → 命中后调一次 1-item 生成调用避开；
#   失败次数超过 DEDUP_REGEN_MAX_RETRIES → 标记 item.status='needs_revision'
# 默认 OFF：自动重生会增加 token 成本和耗时；先用埋点观察基线，
# 再决定是否开启。
ENABLE_DEDUP_REGEN: bool = (
    os.environ.get("ENABLE_DEDUP_REGEN", "0") not in ("0", "false", "False")
)
DEDUP_REGEN_MAX_RETRIES: int = int(
    os.environ.get("DEDUP_REGEN_MAX_RETRIES", "2")
)
# 触发自动重生的语义相似度阈值（cos similarity）；与 dedup.HARD_DUPLICATE_THRESHOLD
# 同步默认 0.92，但允许通过环境变量收紧到 0.88 等更激进的值。
DEDUP_SEMANTIC_THRESHOLD: float = float(
    os.environ.get("DEDUP_SEMANTIC_THRESHOLD", "0.92")
)

# ── Compliance recheck ────────────────────────────────────────────────────
# When True, generate_batch runs a second Claude call after generation to
# verify each version respected the active project memories and session
# instructions. Violations are tagged in token_usage["compliance_violation"]
# for UI surfacing — versions are NOT automatically regenerated.
#
# 历史上这里还有一个 ``COMPLIANCE_AUTO_REGEN`` flag 暗示"打开后自动重生违规
# 版本"，但 ``_apply_compliance_recheck`` 内部从未实装重生逻辑，开关存在但
# 不生效。已删除以免运维误以为打开就能用——硬规则违规的重生路径走
# ``_run_semantic_dedup_pass`` 的 ``regen_ctx`` 管线（按相似度阈值触发）。
ENABLE_COMPLIANCE_CHECK: bool = (
    os.environ.get("ENABLE_COMPLIANCE_CHECK", "1") not in ("0", "false", "False")
)

# ── Image handling ─────────────────────────────────────────────────────────
MAX_IMAGE_DIMENSION: int = int(os.environ.get("MAX_IMAGE_DIMENSION", "1568"))
SUPPORTED_IMAGE_FORMATS: list[str] = ["jpg", "jpeg", "png", "webp"]

# ── Generation defaults ────────────────────────────────────────────────────
DEFAULT_GENERATION_COUNT: int = 10
MAX_GENERATION_COUNT: int = 50
MAX_ITERATION_ROUNDS: int = 3

# ── Model pricing (per 1M tokens, USD) ────────────────────────────────────
# Claude 部分用**中转站实际计费**（截至 2026-05）——不是 Anthropic 官方价。
# 不同 Opus 版本价差 3 倍（Opus 4.1 是 Opus 4.5+ 的 3 倍），所以必须按
# model id 精细分档，不能用"family 一锅"。
#
# Anthropic 的 input/cache_read/cache_create 三个 token 计数**互斥**:
#   总输入 token = input_tokens + cache_creation_input_tokens + cache_read_input_tokens
# Gemini 的 cached_content_token_count 是 prompt_token_count 的**子集**:
#   非缓存输入 = prompt_token_count - cached_content_token_count
# `estimate_cost_usd` 按 engine 类型分别处理这两种语义。
#
# 价格匹配采用"最长 model-id 前缀"策略（``get_pricing``），所以 dated
# 全 id 与 alias 都能正确路由（``claude-sonnet-4-5-20250929-thinking``
# 会按 ``claude-sonnet-4-5`` 匹配到 Sonnet 4.5 那档）。
#
# Gemini 价格暂仍按 Google 官方公开价；中转站 Gemini 实际价待用户提供后再调。
MODEL_PRICING: dict[str, dict[str, float]] = {
    # ── Claude（中转站价）─────────────────────────────────────────────
    "claude-haiku-4-5":   {"input": 1.80, "output": 9.00,   "cache_write": 2.25,   "cache_read": 0.18},
    "claude-sonnet-4-5":  {"input": 5.40, "output": 27.00,  "cache_write": 6.75,   "cache_read": 0.54},
    "claude-sonnet-4-6":  {"input": 5.40, "output": 27.00,  "cache_write": 6.75,   "cache_read": 0.54},
    "claude-opus-4-1":    {"input": 27.00, "output": 135.00, "cache_write": 33.75, "cache_read": 2.70},
    "claude-opus-4-5":    {"input": 9.00, "output": 45.00,  "cache_write": 11.25,  "cache_read": 0.90},
    "claude-opus-4-6":    {"input": 9.00, "output": 45.00,  "cache_write": 11.25,  "cache_read": 0.90},
    "claude-opus-4-7":    {"input": 9.00, "output": 45.00,  "cache_write": 11.25,  "cache_read": 0.90},
    # ── Gemini（官方价，待中转站价格更新）──────────────────────────────
    "gemini-pro":         {"input": 1.25, "output": 10.00,  "cache_write": 0.0,    "cache_read": 0.31},
    "gemini-flash":       {"input": 0.30, "output": 2.50,   "cache_write": 0.0,    "cache_read": 0.075},
    "gemini-flash-lite":  {"input": 0.10, "output": 0.40,   "cache_write": 0.0,    "cache_read": 0.025},
}


def get_pricing(model_id: str) -> dict[str, float]:
    """Resolve a model id to its pricing dict via **longest-prefix match**.

    ``claude-sonnet-4-5-20250929-thinking`` → ``claude-sonnet-4-5`` 那一档；
    ``claude-opus-4-7``                     → ``claude-opus-4-7`` 那一档。
    遍历所有 key 按长度倒序，先匹到长 key 就返回——比 "opus" in mid 的
    fuzzy 子串匹配精准 3 倍以上（Opus 4.1 vs 4.5+ 价差 3×，搞错就盘子
    成本估算偏差 200%）。

    Gemini 走 substring 兜底（flash-lite / flash / pro 三档命名稳定）。
    完全未知的 model id fallback 到最贵的 claude-opus-4-1，宁可高估也不
    silently miss。
    """
    mid = (model_id or "").lower()
    # 1. Claude: 按完整 model-id 前缀最长匹配（移除 ``claude/`` 路由前缀如有）
    if mid.startswith("claude/"):
        mid_stripped = mid[len("claude/"):]
    else:
        mid_stripped = mid
    # 按 key 长度倒序（让 ``claude-opus-4-5`` 先于 ``claude-opus`` 匹中,如果哪天
    # 加回 family-level fallback 也不会被前缀短的优先抢走）
    claude_keys = [k for k in MODEL_PRICING if k.startswith("claude-")]
    for key in sorted(claude_keys, key=len, reverse=True):
        if mid_stripped.startswith(key):
            return MODEL_PRICING[key]
    # 2. Gemini: 命名稳定，子串匹配够用
    if "flash-lite" in mid:
        return MODEL_PRICING["gemini-flash-lite"]
    if "flash" in mid:
        return MODEL_PRICING["gemini-flash"]
    if "gemini" in mid:
        return MODEL_PRICING["gemini-pro"]
    # 3. 未知：回退到最贵的（高估好过 silently miss）
    return MODEL_PRICING["claude-opus-4-1"]


def estimate_cost_usd(model_id: str, usage: dict) -> float:
    """Compute the USD cost of a single LLM call from its ``token_usage`` dict.

    Engine identified by model id substring. The ``usage`` dict can contain
    any subset of these keys (missing = 0):
      - ``input``         (Anthropic: non-cached input; Gemini: full prompt incl. cached)
      - ``output``        output tokens
      - ``cache_read``    tokens served from cache
      - ``cache_create``  tokens written into cache (Anthropic only; Gemini implicit cache has no write fee)
      - ``thinking``      thinking tokens (Gemini 2.5+ / Claude thinking models — billed as output)
    """
    if not isinstance(usage, dict):
        return 0.0
    p = get_pricing(model_id)
    mid = (model_id or "").lower()
    is_gemini = "gemini" in mid

    if is_gemini:
        # Gemini: cached_content_token_count is a SUBSET of prompt_token_count.
        # Avoid double-counting by subtracting from input before applying base rate.
        cache_read = int(usage.get("cache_read") or 0)
        regular_input = max(0, int(usage.get("input") or 0) - cache_read)
        cost = (
            regular_input          * p["input"]
            + cache_read           * p["cache_read"]
            + int(usage.get("output") or 0)   * p["output"]
            + int(usage.get("thinking") or 0) * p["output"]
        )
    else:
        # Anthropic: input / cache_read / cache_create are mutually exclusive.
        cost = (
            int(usage.get("input") or 0)         * p["input"]
            + int(usage.get("cache_read") or 0)  * p["cache_read"]
            + int(usage.get("cache_create") or 0) * p["cache_write"]
            + int(usage.get("output") or 0)      * p["output"]
            + int(usage.get("thinking") or 0)    * p["output"]
        )
    return cost / 1_000_000.0


def estimate_cache_savings_usd(by_model: dict) -> float:
    """估算"如果不开 cache、按 input 全价计费"会比当前多花多少 USD。

    ``by_model`` 形如 ``{"claude/claude-opus-4-7": {"cache_read": N, ...}, ...}``，
    通常来自 ``BatchMetrics.meta["token_totals"]["by_model"]``。
    """
    if not isinstance(by_model, dict):
        return 0.0
    saved = 0.0
    for model_full, usage in by_model.items():
        if not isinstance(usage, dict):
            continue
        p = get_pricing(model_full)
        cache_read = int(usage.get("cache_read") or 0)
        # cache_read 命中部分本来要按 input rate 收，现在按 cache_read rate 收，
        # 差额就是省下来的钱。
        saved += cache_read * (p["input"] - p["cache_read"]) / 1_000_000.0
    return saved


# ── App ────────────────────────────────────────────────────────────────────
APP_TITLE: str = "小红书内容自动化工作台"
APP_VERSION: str = "2.14.0-studio"


def validate_config() -> list[str]:
    """Return a list of missing required config keys."""
    missing = []
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if not SUPABASE_URL:
        missing.append("SUPABASE_URL")
    if not SUPABASE_ANON_KEY:
        missing.append("SUPABASE_ANON_KEY")
    return missing
