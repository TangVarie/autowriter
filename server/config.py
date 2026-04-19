"""Environment-variable configuration for the Feishu-backed AutoWriter server."""

from __future__ import annotations

import os


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, "") or default


# ── Anthropic ──────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = _env("ANTHROPIC_API_KEY")
ANTHROPIC_BASE_URL: str = _env("ANTHROPIC_BASE_URL")
CLAUDE_MODEL: str = _env("CLAUDE_MODEL", "claude-sonnet-4-6")

# ── Google Gemini ──────────────────────────────────────────────────────────
GOOGLE_API_KEY: str = _env("GOOGLE_API_KEY")
GOOGLE_BASE_URL: str = _env("GOOGLE_BASE_URL")
GEMINI_MODEL: str = _env("GEMINI_MODEL", "gemini-3.1-pro-preview")

# ── Feishu (Lark) open platform ───────────────────────────────────────────
FEISHU_APP_ID: str = _env("FEISHU_APP_ID")
FEISHU_APP_SECRET: str = _env("FEISHU_APP_SECRET")
# The app_token of the Bitable (从多维表格 URL 的 base/xxxxx 段提取)
FEISHU_BITABLE_APP_TOKEN: str = _env("FEISHU_BITABLE_APP_TOKEN")

# Per-table IDs (从每张表格的 URL table=xxxxx 参数提取)
FEISHU_TABLE_PROJECTS: str = _env("FEISHU_TABLE_PROJECTS")
FEISHU_TABLE_BATCHES: str = _env("FEISHU_TABLE_BATCHES")
FEISHU_TABLE_ITEMS: str = _env("FEISHU_TABLE_ITEMS")
FEISHU_TABLE_MEMORIES: str = _env("FEISHU_TABLE_MEMORIES")

# Shared secret that the Feishu automation must include in the X-Autowriter-Token
# header. Prevents random POSTs from triggering generation.
WEBHOOK_SHARED_SECRET: str = _env("WEBHOOK_SHARED_SECRET")

# Default host/port for local uvicorn (Railway injects $PORT)
PORT: int = int(_env("PORT", "8000"))


def validate() -> list[str]:
    """Return the list of missing required keys."""
    required = {
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
        "FEISHU_APP_ID": FEISHU_APP_ID,
        "FEISHU_APP_SECRET": FEISHU_APP_SECRET,
        "FEISHU_BITABLE_APP_TOKEN": FEISHU_BITABLE_APP_TOKEN,
        "FEISHU_TABLE_PROJECTS": FEISHU_TABLE_PROJECTS,
        "FEISHU_TABLE_BATCHES": FEISHU_TABLE_BATCHES,
        "FEISHU_TABLE_ITEMS": FEISHU_TABLE_ITEMS,
    }
    return [k for k, v in required.items() if not v]
