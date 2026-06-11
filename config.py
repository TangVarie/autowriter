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
# 仅保留 Anthropic 官方当前 GA + 中转站实际支持的模型（截至 2026-06）。
# Retired 已删（Claude 3 全系列、Sonnet/Opus 4.0 一代——后者 2026-04-20 下线）。
# Thinking 模式仍走 -thinking 后缀（中转站约定，非官方 API 参数）；4-6 / 4-7
# 系列中转站未提供 thinking 变体，故不列；4-8 中转站提供 thinking 变体，已列出。
CLAUDE_MODELS: dict[str, str] = {
    # ── Haiku ─────────────────────────────────────────────
    "claude-haiku-4-5-20251001":           "Haiku 4.5（最快/最省）",
    # ── Sonnet ────────────────────────────────────────────
    "claude-sonnet-4-6":                   "Sonnet 4.6",
    # ── Opus ──────────────────────────────────────────────
    "claude-opus-4-6":                     "Opus 4.6",
    "claude-opus-4-7":                     "Opus 4.7",
    "claude-opus-4-8":                     "Opus 4.8（最新，最强）",
    "claude-opus-4-8-thinking":            "Opus 4.8 Thinking（深度推理）",
}

# Default model (can be overridden via env var)
# Sonnet 4.6 是中转站当前支持的 Sonnet 唯一版本(Sonnet 4.5 2026-05 被分组
# 下线),保持 backend utility calls(memory merger / calibration / compliance)
# 默认值与中转站可用 model 同步,避免 worker 调内部辅助调用即 502。
CLAUDE_MODEL: str = _get_secret("CLAUDE_MODEL") or "claude-sonnet-4-6"

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
# R-018: service_role key —— 仅后台 worker (worker.py) 用, 绕 RLS 领取任意
# 用户的 job。Streamlit app 路径绝不使用。从环境/secrets 注入, 不要硬编码;
# 泄露立即在 Supabase Dashboard rotate。app 不配也能正常跑(只有 worker 需要)。
SUPABASE_SERVICE_ROLE_KEY: str = _get_secret("SUPABASE_SERVICE_ROLE_KEY")

# ── Feishu (Lark) Webhook ──────────────────────────────────────────────────
FEISHU_WEBHOOK_URL: str = _get_secret("FEISHU_WEBHOOK_URL")

# ── Flywheel librarian (TV pull 馆员, R-032) ─────────────────────────────────
# 写稿时向 TV 的 LLM 馆员服务借阅匹配的"真实爆款经验"注入 P2(见 docs/15 +
# librarian_client.py)。URL/KEY 由 TV 侧给(部署在 Railway)。
# 留空 = 不接飞轮:写稿照常, 只是少了"真实爆款参照"这一节(纯增强项, 非前置依赖)。
LIBRARIAN_URL: str = _get_secret("LIBRARIAN_URL")       # 例 https://truth-vault-production.up.railway.app
LIBRARIAN_API_KEY: str = _get_secret("LIBRARIAN_API_KEY")
LIBRARIAN_TIMEOUT_SEC: float = float(_get_secret("LIBRARIAN_TIMEOUT_SEC") or "8")

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

# ── 失败位补量(undercount top-up, R-033)──────────────────────────────────
# 多样性硬约束明确允许模型"宁可少出一条也不要硬出重复项"(_make_user_prompt /
# _build_dedup_instruction), 所以"要 10 篇只回 8 篇"是 prompt 授权的正常行为;
# 个别槽位 JSON 损坏同理。开启时, 每次引擎调用结束后如有失败位, 把已产出标题
# 加入避重清单后**恰好按缺口数**再调一次(每次调用最多补 1 刀, 不递归)。
# 成本: 仅在出现缺口时多一次小调用; 仍补不满时保留原错误如实上报。
# 整调用级 API 失败不补(retry middleware 已重试过)。
ENABLE_UNDERCOUNT_TOPUP: bool = (
    os.environ.get("ENABLE_UNDERCOUNT_TOPUP", "1") not in ("0", "false", "False")
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
# **Pricing keys ≠ selectable models**：``CLAUDE_MODELS`` 是 UI 下拉里
# 可选的"当前还能用的 model"列表（只有 4 个）；本表是历史成本计算用，
# 包含**已下线但 DB 里仍有 batch 记录的 retired model 价档**——历史回看
# 面板算成本时需要按当时价精确路由，不然估算偏差很大（如 opus-4-1 实际
# $27 vs 4.5+ $9，3 倍价差，被错误 fallback 到 4.7 价档会让历史成本
# 低估 67%）。
#
# Anthropic 的 input/cache_read/cache_create 三个 token 计数**互斥**:
#   总输入 token = input_tokens + cache_creation_input_tokens + cache_read_input_tokens
# Gemini 的 cached_content_token_count 是 prompt_token_count 的**子集**:
#   非缓存输入 = prompt_token_count - cached_content_token_count
# `estimate_cost_usd` 按 engine 类型分别处理这两种语义。
#
# 价格匹配采用"最长 model-id 前缀"策略（``get_pricing``），所以 dated
# 全 id 与 alias 都能正确路由（``claude-sonnet-4-6`` 直接命中 Sonnet 4.6
# 那档）。
#
# Gemini 价格暂仍按 Google 官方公开价；中转站 Gemini 实际价待用户提供后再调。
MODEL_PRICING: dict[str, dict[str, float]] = {
    # ── Claude 当前可选（CLAUDE_MODELS 里有）──────────────────────────
    "claude-haiku-4-5":   {"input": 1.80, "output": 9.00,   "cache_write": 2.25,   "cache_read": 0.18},
    "claude-sonnet-4-6":  {"input": 5.40, "output": 27.00,  "cache_write": 6.75,   "cache_read": 0.54},
    "claude-opus-4-6":    {"input": 9.00, "output": 45.00,  "cache_write": 11.25,  "cache_read": 0.90},
    "claude-opus-4-7":    {"input": 9.00, "output": 45.00,  "cache_write": 11.25,  "cache_read": 0.90},
    # opus-4-8 与 4-5/4-6/4-7 同档（中转站 Opus 统一价）。``claude-opus-4-8-thinking``
    # 经 get_pricing 最长前缀匹配命中本档，无需单列；若中转站对 4-8 单独定价，改这里。
    "claude-opus-4-8":    {"input": 9.00, "output": 45.00,  "cache_write": 11.25,  "cache_read": 0.90},
    # ── Claude 已下线但 DB 历史 batch 仍引用（仅用于成本回算）──────────
    # 2026-05 中转站新分组下线; 用户的历史 batch 大量用这些 model id
    "claude-sonnet-4-5":  {"input": 5.40, "output": 27.00,  "cache_write": 6.75,   "cache_read": 0.54},
    "claude-opus-4-1":    {"input": 27.00, "output": 135.00, "cache_write": 33.75, "cache_read": 2.70},
    "claude-opus-4-5":    {"input": 9.00, "output": 45.00,  "cache_write": 11.25,  "cache_read": 0.90},
    # ── Gemini（官方价，待中转站价格更新）──────────────────────────────
    "gemini-pro":         {"input": 1.25, "output": 10.00,  "cache_write": 0.0,    "cache_read": 0.31},
    "gemini-flash":       {"input": 0.30, "output": 2.50,   "cache_write": 0.0,    "cache_read": 0.075},
    "gemini-flash-lite":  {"input": 0.10, "output": 0.40,   "cache_write": 0.0,    "cache_read": 0.025},
}


def get_pricing(model_id: str) -> dict[str, float]:
    """Resolve a model id to its pricing dict via **longest-prefix match**.

    ``claude-sonnet-4-6`` → ``claude-sonnet-4-6`` 那一档；
    ``claude-opus-4-7``   → ``claude-opus-4-7`` 那一档。
    遍历所有 key 按长度倒序，先匹到长 key 就返回——保留长度倒序逻辑是
    为了将来如果加回 family-level fallback（如 ``claude-opus``）也不会
    让前缀短的优先抢走精确的 model 价档。

    Gemini 走 substring 兜底（flash-lite / flash / pro 三档命名稳定）。
    完全未知 / 已下线的 model id fallback 到最贵的 claude-opus-4-7，
    宁可高估也不 silently miss（用户跑历史 batch 用了已 retire 的 model
    时也能给出合理数字而不是 KeyError）。
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
    # 3. 未知：回退到最贵的（高估好过 silently miss）。Opus 4.7 是当前最贵档
    #    （Opus 4.1 已下线后,4.5/4.6/4.7 同价、Opus 4.7 是 alias 也最稳）。
    return MODEL_PRICING["claude-opus-4-7"]


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


# ── Model context windows (Phase 2: session window management) ────────────
# 每个 model 的 input + output 总 token 上限。session 累积 running_input_tokens
# 接近这个值时(默认 80%)前端面板给软警告;API 真返回 context_length_exceeded
# 才硬切到新 session(中转站给的实际余量经常比官方大,先看软警告再硬撞)。
#
# Claude Sonnet 4.6 官方支持 1M context beta header (anthropic-beta:
# context-1m-2025-08), 但默认走 200K 档。这里取 200K 保守值——开 1M
# 需要请求时带 beta header,目前 ClaudeEngine 没传,保持一致。
#
# Gemini 2.5 Pro 1M、Gemini Flash 1M; 中转站没显式说明窗口,按官方默认。
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-haiku-4-5":   200_000,
    "claude-sonnet-4-6":  200_000,
    "claude-opus-4-6":    200_000,
    "claude-opus-4-7":    200_000,
    "claude-opus-4-8":    200_000,
    "gemini-pro":         1_000_000,
    "gemini-flash":       1_000_000,
    "gemini-flash-lite":  1_000_000,
}

# 历史已下线 model 的窗口(给老数据回算用,跟 MODEL_PRICING 的 retired 价档
# 同步保留)
MODEL_CONTEXT_WINDOWS.update({
    "claude-sonnet-4-5":  200_000,
    "claude-opus-4-1":    200_000,
    "claude-opus-4-5":    200_000,
})


def get_context_window(model_id: str) -> int:
    """Resolve model id to context window via longest-prefix match.

    跟 ``get_pricing`` 同套路: ``claude/`` 前缀剥离 + 最长 key 优先 +
    Gemini 子串兜底 + unknown fallback 200K(保守)。
    """
    mid = (model_id or "").lower()
    if mid.startswith("claude/"):
        mid = mid[len("claude/"):]
    claude_keys = [k for k in MODEL_CONTEXT_WINDOWS if k.startswith("claude-")]
    for key in sorted(claude_keys, key=len, reverse=True):
        if mid.startswith(key):
            return MODEL_CONTEXT_WINDOWS[key]
    if "flash-lite" in mid:
        return MODEL_CONTEXT_WINDOWS["gemini-flash-lite"]
    if "flash" in mid:
        return MODEL_CONTEXT_WINDOWS["gemini-flash"]
    if "gemini" in mid:
        return MODEL_CONTEXT_WINDOWS["gemini-pro"]
    return 200_000


# Phase 2.3：session 当前 prefix 占用达到 window_limit 的这个比例时自动封窗
# （seal → 下一批路由到新 session，新 session 懒同步最近 50 条 approved 历史，
# 避重自动接续 + dedup_block 照常注入，所以无需额外"摘要注入"）。
# 取 0.8 留 20% 余量给输出 / thinking / 每批浮动的 memories·dedup_block——
# 实际撞窗口的硬边界仍靠 API 返回 context_length_exceeded 兜底。
SESSION_SEAL_THRESHOLD: float = float(
    os.environ.get("SESSION_SEAL_THRESHOLD", "0.8")
)


def compute_base_prompt_hash(base_prompt: str) -> str:
    """sha256 hex digest of the normalized base_prompt — Phase 2 session
    routing 用的 hash 维度。

    只对 ``project.system_prompt`` (即"项目人格"那段最稳定的内容) 做 hash,
    不含 memories / calibration / examples——那些每批可能变,如果让它们
    参与 hash, session 几乎每批都得切, cache 复用就失效了。

    用户每批产生新记忆或调校笔记是常态; 那些变化在分层 system prompt
    里只让 P0/P1 层失效 cache, 不影响 session 路由身份。
    """
    import hashlib
    return hashlib.sha256((base_prompt or "").strip().encode("utf-8")).hexdigest()


# ── App ────────────────────────────────────────────────────────────────────
APP_TITLE: str = "小红书内容自动化工作台"
APP_VERSION: str = "2.17.0-phase2.3"


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
