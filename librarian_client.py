"""
TV 飞轮馆员客户端 (R-032) —— 通道2 pull 模型的消费侧。

背景
────
通道2 已从 push(TV 预先把爆款塞进 ``autowriter.items``)改为 pull:TV 当
图书馆,autowriter 写稿时按本次 brief 来"借阅"匹配的真实爆款经验,注入
system prompt 的 P2 会话层(不缓存)。TV 侧的馆员服务已上线 Railway,本模块
只负责发 brief、收 selected,其余(结果缓存 / Anthropic prompt caching /
走中转站)都在馆员服务内部,autowriter 不关心。

契约(见 docs/15 §0)
────────────────────
    POST {LIBRARIAN_URL}/librarian   header X-Librarian-Key: <key>
    body  = brief(见 ``build_brief``)
    → 200 {"selected": [ {hook_type, why_it_worked, borrow_what,
                          why_relevant, excerpt, tier, structure,
                          transferable_tactic, source_note_id}, ...0-5 条 ]}
    空库 / 馆员内部出错 → {"selected": []}(不是 500)。

降级语义(重要)
──────────────
飞轮是**增强项,不是写稿前置依赖**。未配 URL/KEY、超时、网络错、4xx/5xx、
返回非预期结构 —— 一律 fail-open 成 ``[]``,绝不阻塞或拖慢写稿。调用方拿到
``[]`` 就当"这次没飞轮料",照常用 owner 自有正例写稿。

实现说明
────────
用同步 ``requests``(不是 httpx):仓库已直接依赖 requests(exporter.py 的
飞书 webhook 就用它),httpx 只是 supabase 的传递依赖。generate_batch 本就在
worker daemon 线程里同步跑,同步 HTTP 即可。
"""

from __future__ import annotations

from typing import Optional

import requests

import config
import telemetry
from logger_utils import mask_secrets


def build_brief(
    project: dict,
    *,
    tactic: str = "",
    key_messages: str = "",
    target_audience: str = "",
    tone: str = "",
    extra_instructions: str = "",
    draft_topic: str = "",
) -> dict:
    """组装借阅 brief:项目级稳定字段(馆员会 prompt-cache 这部分)+ 本批 delta。

    字段集对齐 docs/15 §0 契约(R-032 回执:delta 加了 ``key_messages``,馆员
    按同卖点优先匹配)。``project`` 缺某列时对应值为 ``None``,馆员服务按缺失
    处理(不会 500)。``draft_topic`` 可选——核心卖点走 ``key_messages`` 自己的
    槽,与"选题"语义不同,aw 暂无真选题字段时不带这一键。
    """
    brief = {
        "consumer": "autowriter",
        "project_id": project.get("id"),
        # —— 项目级稳定字段 ——
        "brand": project.get("brand"),
        "project_name": project.get("name"),
        "system_prompt": project.get("system_prompt"),
        "system_prompt_tone": project.get("system_prompt_tone"),
        "system_prompt_exec": project.get("system_prompt_exec"),
        "tactics": project.get("tactics"),
        "calibration_notes": project.get("calibration_notes"),
        # —— 本次 batch 的 delta ——
        "tactic": tactic,
        "key_messages": key_messages,
        "target_audience": target_audience,
        "tone": tone,
        "extra_instructions": extra_instructions,
    }
    if draft_topic:
        brief["draft_topic"] = draft_topic
    return brief


def fetch_flywheel_lessons(brief: dict) -> list[dict]:
    """向 TV 馆员借阅经验卡。

    任何异常 / 超时 / 未配 → 返回 ``[]``(绝不抛、绝不阻塞写稿)。返回 list 内
    元素形状见模块 docstring 的契约;非 list 响应也归一成 ``[]``,由下游
    ``memory.build_layered_system_prompt`` 再按 dict 逐条防御。
    """
    if not config.LIBRARIAN_URL or not config.LIBRARIAN_API_KEY:
        return []                              # 没接飞轮, 静默跳过
    try:
        resp = requests.post(
            f"{config.LIBRARIAN_URL.rstrip('/')}/librarian",
            headers={"X-Librarian-Key": config.LIBRARIAN_API_KEY},
            json=brief,
            timeout=config.LIBRARIAN_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        selected = resp.json().get("selected")
        return selected if isinstance(selected, list) else []
    except Exception as exc:
        # 超时 / 网络 / 4xx / 5xx / 解析全吞 —— 飞轮是增强项不是前置依赖,
        # 失败就当没有, 用 owner 自有正例照常写。mask_secrets 防 URL/key 入日志。
        telemetry.log_event(
            "flywheel_librarian_unavailable",
            project_id=str(brief.get("project_id") or ""),
            error=mask_secrets(str(exc))[:300],
        )
        return []
