"""
Configuration module for XHS Content Workstation.
Loads settings from environment variables.
"""

import os

# ── Anthropic ──────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")

# Default Claude model for content generation
CLAUDE_MODEL: str = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# ── Google Gemini ──────────────────────────────────────────────────────────
GOOGLE_API_KEY: str = os.environ.get("GOOGLE_API_KEY", "")
GEMINI_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

# ── Supabase ───────────────────────────────────────────────────────────────
SUPABASE_URL: str = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY: str = os.environ.get("SUPABASE_ANON_KEY", "")

# ── Feishu (Lark) Webhook ──────────────────────────────────────────────────
FEISHU_WEBHOOK_URL: str = os.environ.get("FEISHU_WEBHOOK_URL", "")

# ── Memory system thresholds ───────────────────────────────────────────────
# Number of times a feedback pattern must appear before auto-confirming
MEMORY_AUTO_CONFIRM_THRESHOLD: int = int(
    os.environ.get("MEMORY_AUTO_CONFIRM_THRESHOLD", "3")
)

# ── Image handling ─────────────────────────────────────────────────────────
MAX_IMAGE_DIMENSION: int = int(os.environ.get("MAX_IMAGE_DIMENSION", "1568"))
SUPPORTED_IMAGE_FORMATS: list[str] = ["jpg", "jpeg", "png", "webp"]

# ── Generation defaults ────────────────────────────────────────────────────
DEFAULT_GENERATION_COUNT: int = 10
MAX_GENERATION_COUNT: int = 50
MAX_ITERATION_ROUNDS: int = 3  # Maximum context rounds per item

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
