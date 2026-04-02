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
CLAUDE_MODELS: dict[str, str] = {
    "claude-opus-4-6":   "Opus 4.6（最强，深度思考 effort=high）",
    "claude-sonnet-4-6": "Sonnet 4.6（均衡，深度思考 budget_tokens）",
}

# Default model (can be overridden via env var)
CLAUDE_MODEL: str = _get_secret("CLAUDE_MODEL") or "claude-sonnet-4-6"

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
GEMINI_MODEL: str = _get_secret("GEMINI_MODEL") or "gemini-3.1-pro-preview"

# ── Supabase ───────────────────────────────────────────────────────────────
SUPABASE_URL: str = _get_secret("SUPABASE_URL")
SUPABASE_ANON_KEY: str = _get_secret("SUPABASE_ANON_KEY")

# ── Feishu (Lark) Webhook ──────────────────────────────────────────────────
FEISHU_WEBHOOK_URL: str = _get_secret("FEISHU_WEBHOOK_URL")

# ── Memory system thresholds ───────────────────────────────────────────────
MEMORY_AUTO_CONFIRM_THRESHOLD: int = int(
    os.environ.get("MEMORY_AUTO_CONFIRM_THRESHOLD", "3")
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
APP_VERSION: str = "2.0"


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
