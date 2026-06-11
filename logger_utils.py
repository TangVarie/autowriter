"""
日志 / 错误展示的 secret 脱敏。

R-023 (2026-05-22 audit): 异常 stacktrace、telemetry 事件、UI 上展示的
traceback 都可能带 API key / Supabase bearer token / JWT payload。生产部署
后日志一旦外泄(运维看日志 / 上传第三方平台 / 截图发群), secret 直接泄露。

本模块提供 ``mask_secrets``: 在写日志 / 渲染错误前过一道, 把已知形态的
secret 替换成 ``***REDACTED***``。模式集与 truth-vault ``scripts/_common.py``
和 sanshengliubu ``pipeline/logger_utils.py`` shadow-aligned, 三仓保持一致,
新增一类 secret 时三处同步更新。

零依赖(只用 re), 任何模块都能 import, 不会引入循环依赖。
"""

from __future__ import annotations

import logging
import re

_REDACTED = "***REDACTED***"

# 顺序有意义: 更具体的前缀(sk-ant- / sk-proj-)必须排在泛化的 sk- 之前,
# 否则泛化模式先把前半截吃掉留下尾巴。各模式都要求 20+ 字符正文, 避免把
# 普通短串(如 "sk-1" 这种示例)误伤。
_SECRET_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),                                       # Anthropic
    re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"),                                      # OpenAI project key
    re.compile(r"sb_secret_[A-Za-z0-9]{20,}"),                                       # Supabase service role (2024+)
    re.compile(r"sbp_[A-Za-z0-9]{20,}"),                                             # Supabase personal access token
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT (header.payload.sig)
    re.compile(r"AIza[A-Za-z0-9_\-]{20,}"),                                          # Google API key
    re.compile(r"sk-[A-Za-z0-9]{40,}"),                                              # 泛化 OpenAI-style (放最后)
)


# R-040: 值级脱敏。LIBRARIAN_API_KEY(TV 侧自定义格式)、飞书 webhook token
# 段等没有可识别前缀, 形态正则抓不住 —— 一旦异常文本带上它们, 上面的模式集
# 全部漏网。这里按"已知 secret 的具体值"做替换: 从 config 懒加载一次(config
# 仅依赖 os, 无循环导入; 失败静默, 本模块保持可独立 import)。
_VALUE_SECRETS: tuple[str, ...] | None = None


def _load_value_secrets() -> tuple[str, ...]:
    global _VALUE_SECRETS
    if _VALUE_SECRETS is not None:
        return _VALUE_SECRETS
    vals: list[str] = []
    try:
        import config as _cfg
        for name in (
            "LIBRARIAN_API_KEY",
            "SUPABASE_SERVICE_ROLE_KEY",
            "ANTHROPIC_API_KEY",
            "GOOGLE_API_KEY",
        ):
            v = getattr(_cfg, name, "") or ""
            # ≥8 字符才入表: 避免把过短占位值(如 "1"/"test")当 secret 误伤正文
            if isinstance(v, str) and len(v) >= 8:
                vals.append(v)
        wh = getattr(_cfg, "FEISHU_WEBHOOK_URL", "") or ""
        # 飞书 webhook 的鉴权就在 URL 路径里 —— 只脱 token 段, 保留域名便于排障
        if "/hook/" in wh:
            token = wh.split("/hook/", 1)[1].strip("/")
            if len(token) >= 8:
                vals.append(token)
    except Exception:
        pass
    # 长值优先替换, 防止短值是长值子串时把长值劈成两半留尾巴
    _VALUE_SECRETS = tuple(sorted(set(vals), key=len, reverse=True))
    return _VALUE_SECRETS


def mask_secrets(s):
    """把字符串里已知形态/已知值的 secret 替换成 ``***REDACTED***``。

    非字符串原样返回(调用方常传 ``str(exc)`` 但偶尔传别的类型, 不强转避免
    把 None / dict 变成 "None" 噪声)。脱敏失败时返回原值——脱敏本身不能
    把日志/错误展示搞崩(那比泄露更影响排障)。
    """
    if not isinstance(s, str) or not s:
        return s
    try:
        for pat in _SECRET_PATTERNS:
            s = pat.sub(_REDACTED, s)
        for val in _load_value_secrets():
            if val in s:
                s = s.replace(val, _REDACTED)
    except Exception:
        return s
    return s


class SecretMaskingFormatter(logging.Formatter):
    """logging.Formatter 子类, 在最终格式化后跑一遍 ``mask_secrets``。

    本项目主要走 telemetry stdout JSON + Streamlit st.code 两条路径(见
    telemetry.py / app.py), 标准 logging 用得少; 这个 formatter 给将来接
    标准 logging handler 时直接复用(用法见模块 docstring 的兄弟仓)。
    """

    def format(self, record: logging.LogRecord) -> str:
        return mask_secrets(super().format(record))
