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

import time
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


# 一次借阅的**结局**。四种失败长得一模一样(都是空列表), 但对运营的含义完全
# 不同: "这次没匹配上"是正常的, "没配 key"是部署漏了, "超时"是 TV 那边慢了。
# 混成一个空列表之后, "经验到底有没有被用上"这件事就再也统计不出来了
# (2026-09-16 评测 AW-05)。
BORROW_BORROWED = "borrowed"            # 借到了
BORROW_EMPTY = "empty"                  # 通了, 但这次没有匹配的卡
BORROW_NOT_CONFIGURED = "not_configured"  # 压根没接飞轮
BORROW_TIMEOUT = "timeout"              # 超时
BORROW_ERROR = "error"                  # 网络 / 4xx / 5xx / 解析失败


def fetch_flywheel_lessons(brief: dict, *,
                           status: dict | None = None) -> list[dict]:
    """向 TV 馆员借阅经验卡。

    任何异常 / 超时 / 未配 / 响应结构不对 → 返回 ``[]``(绝不抛、绝不阻塞写稿)。
    返回 list 内元素形状见模块 docstring 的契约,由下游
    ``memory.build_layered_system_prompt`` 再按 dict 逐条防御。
    **拿不到 ``selected`` 这个 list 算 ``error`` 不算 ``empty``** —— 契约里空库也
    要回 ``{"selected": []}``, 所以结构不对是故障。

    ⚠️ **空列表不等于"没匹配上"。** 传一个 dict 给 ``status``, 调用完它会被填成
    ``{"state", "count", "elapsed_ms", "detail"}`` —— ``state`` 是上面那五个之一。
    不传就只有 telemetry 记着(每种结局一个独立事件名), 调用方看不见区别。

    返回类型刻意**没变**: 借阅是增强项, 让它的失败去改生成路径的函数签名不值当。
    """
    st = status if status is not None else {}
    st.update({"state": BORROW_ERROR, "count": 0,
               "elapsed_ms": 0, "detail": ""})
    pid = str(brief.get("project_id") or "")

    if not config.LIBRARIAN_URL or not config.LIBRARIAN_API_KEY:
        st["state"] = BORROW_NOT_CONFIGURED
        st["detail"] = "LIBRARIAN_URL / LIBRARIAN_API_KEY 未配置"
        # ⚠️ 这条以前是**完全静默**的 return —— 于是"这个部署根本没接飞轮"和
        # "接了但这次没匹配"在数据上分不开, 而前者是要人去补配置的。
        telemetry.log_event("flywheel_librarian_not_configured", project_id=pid)
        return []

    started = time.monotonic()
    try:
        resp = requests.post(
            f"{config.LIBRARIAN_URL.rstrip('/')}/librarian",
            headers={"X-Librarian-Key": config.LIBRARIAN_API_KEY},
            json=brief,
            timeout=config.LIBRARIAN_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        # 超时 / 网络 / 4xx / 5xx / 解析全吞 —— 飞轮是增强项不是前置依赖,
        # 失败就当没有, 用 owner 自有正例照常写。mask_secrets 防 URL/key 入日志。
        timed_out = isinstance(exc, requests.Timeout)
        st["state"] = BORROW_TIMEOUT if timed_out else BORROW_ERROR
        st["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        st["detail"] = mask_secrets(str(exc))[:300]
        telemetry.log_event(
            "flywheel_librarian_unavailable",
            project_id=pid,
            state=st["state"],
            elapsed_ms=st["elapsed_ms"],
            error=st["detail"],
        )
        return []

    st["elapsed_ms"] = int((time.monotonic() - started) * 1000)

    # ⚠️ 200 但结构不对**不是** empty。契约(模块 docstring)写着空库也要回
    # {"selected": []}, 所以拿不到那个 list 就是故障: 馆员换了契约、回了错误页、
    # 或者中转站塞了别的东西。而 empty 恰恰是"不需要任何人管"的那一类结局 ——
    # 把故障归进去, 它就永远没人看了, 这正是 AW-05 要治的病
    # (codex review P2)。
    if not isinstance(payload, dict):
        bad = f"响应体不是 JSON 对象, 是 {type(payload).__name__}"
    elif "selected" not in payload:
        bad = (f"响应体里没有 selected 键(实际键: {sorted(payload)[:6]})"
               " —— 契约要求空库也回 []")
    elif not isinstance(payload["selected"], list):
        bad = f"selected 不是 list, 是 {type(payload['selected']).__name__}"
    else:
        bad = ""

    if bad:
        st["state"] = BORROW_ERROR
        st["detail"] = mask_secrets(f"馆员回了 200 但{bad}")[:300]
        telemetry.log_event(
            "flywheel_librarian_bad_payload",
            project_id=pid, state=st["state"],
            elapsed_ms=st["elapsed_ms"], error=st["detail"],
        )
        return []

    lessons = payload["selected"]
    st["count"] = len(lessons)
    st["state"] = BORROW_BORROWED if lessons else BORROW_EMPTY
    telemetry.log_event(
        "flywheel_librarian_result",
        project_id=pid, state=st["state"],
        count=st["count"], elapsed_ms=st["elapsed_ms"],
    )
    return lessons
