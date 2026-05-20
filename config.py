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
# Thinking mode is selected via model name (-thinking suffix); proxy does not
# accept the `thinking` API parameter.
CLAUDE_MODELS: dict[str, str] = {
    # ── Sonnet series ──
    "claude-3-sonnet-20240229":            "Sonnet 3（默认）",
    "claude-3-5-sonnet-20240620":          "Sonnet 3.5（首版）",
    "claude-3-5-sonnet-20241022":          "Sonnet 3.5 v2",
    "claude-3-7-sonnet-20250219":          "Sonnet 3.7",
    "claude-3-7-sonnet-20250219-thinking": "Sonnet 3.7（思考）",
    "claude-sonnet-4-20250514":            "Sonnet 4",
    "claude-sonnet-4-20250514-thinking":   "Sonnet 4（思考）",
    "claude-sonnet-4-5-20250929":          "Sonnet 4.5",
    "claude-sonnet-4-5-20250929-thinking": "Sonnet 4.5（思考）",
    "claude-sonnet-4-6":                   "Sonnet 4.6",
    # ── Opus series ──
    "claude-3-opus-20240229":              "Opus 3",
    "claude-opus-4-20250514":              "Opus 4",
    "claude-opus-4-20250514-thinking":     "Opus 4（思考）",
    "claude-opus-4-1-20250805":            "Opus 4.1",
    "claude-opus-4-1-20250805-thinking":   "Opus 4.1（思考）",
    "claude-opus-4-5-20251101":            "Opus 4.5",
    "claude-opus-4-5-20251101-thinking":   "Opus 4.5（思考）",
    "claude-opus-4-6":                     "Opus 4.6",
    "claude-opus-4-6-thinking":            "Opus 4.6（思考）",
    "claude-opus-4-7":                     "Opus 4.7",
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
# All three are GA stable as of 2025; 2.5 Flash is best price/performance
GEMINI_MODELS: dict[str, str] = {
    "gemini-3.1-pro-preview": "Gemini 3.1 Pro Preview（最新）",
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
# for UI surfacing — versions are NOT automatically regenerated unless
# COMPLIANCE_AUTO_REGEN is also enabled (default off to control cost).
ENABLE_COMPLIANCE_CHECK: bool = (
    os.environ.get("ENABLE_COMPLIANCE_CHECK", "1") not in ("0", "false", "False")
)
COMPLIANCE_AUTO_REGEN: bool = (
    os.environ.get("COMPLIANCE_AUTO_REGEN", "0") not in ("0", "false", "False")
)

# ── Image handling ─────────────────────────────────────────────────────────
MAX_IMAGE_DIMENSION: int = int(os.environ.get("MAX_IMAGE_DIMENSION", "1568"))
SUPPORTED_IMAGE_FORMATS: list[str] = ["jpg", "jpeg", "png", "webp"]

# ── Generation defaults ────────────────────────────────────────────────────
DEFAULT_GENERATION_COUNT: int = 10
MAX_GENERATION_COUNT: int = 50
MAX_ITERATION_ROUNDS: int = 3

# ── App ────────────────────────────────────────────────────────────────────
APP_TITLE: str = "小红书内容自动化工作台"
APP_VERSION: str = "2.10.1-studio"


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
