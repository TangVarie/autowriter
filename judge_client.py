"""
入库判定客户端(judge) —— 与 ``librarian_client`` 同一形状的薄客户端。

背景
────
JevforCoentent 仓 docs/00 #4(2026-09-23 拍板): 写作台的入库判定挂在
``commit_drafts`` 内部, **advisory, 超时 8 秒即跳过, 答案进账本, 影子期只记不拦**。
账本是 TV 的 ``truth_vault.note_feature_answers``(``subject_type='aw_version'``,
``subject_id = autowriter.versions.id``), 由 judge 服务自己写(``write=true``);
写作台这边只发稿子、收一个结局。

deskcore「一次 LLM 调用都没有」这条不破: 判定是 HTTP 调 judge 拿**事实**, 模型调用
(Jev)在 judge 服务里, 与借卡调 TV 馆员是同一种关系。本模块不 import 任何模型 SDK。

覆盖面(docs/deskcore.md §3.8): **只判走 commit_drafts 入库的稿子**。在外面写、发布后由
tv-sync 补录(``core._ingest_published_unlocked``)的那些已经是公开笔记, 由 TV 的事后特征
抽取(docs/31 位置 ①)覆盖(前提是笔记在 TV 里), 补录路径不调本模块。

契约(JevforCoentent ``judge/api.py``)
──────────────────────────────────────
    POST {JUDGE_URL}/judge_draft   header X-Judge-Key: <JUDGE_API_KEY>
    body = build_draft_request(...)
    → 200 {"passed": bool, "hard_fails": [[bank, qid, answer, p, evidence], ...],
           "plan": [...], "calls": int, "written": int | null,
           "policy": {"published": false, "project": "...", "dropped_banks": [...],
                      "dropped_hard_rules": [...]}, ...}
    → 403 且 detail 以 "policy:" 开头 = 数据出境口径拒绝(处方药项目的未发布稿, docs/00 #7)
    → 422 请求不对 · 401 / 503 鉴权或服务端配置 · 502 Jev 调用失败

结局(``judge_status``)
───────────────────────
    ok               判完了(passed 是判定结论, 影子期不拿它拦任何东西)
    not_configured   没配 JUDGE_URL / JUDGE_API_KEY(或调用方判出这次不该发, 见 core)
    timeout          超时(连接或读)。judge 那边可能仍会判完并写账本, 只是我们没等到
    policy_blocked   judge 按数据出境口径拒绝。**不重试** —— 重试也是同一个结果
    bad_request      422: 请求形状不对。**不重试**, 是写作台这边的 bug
    unavailable      401 / 503 / 404 / 连不上: 部署或配置问题, 要人去修
    jev_failed       502: judge 调 Jev 失败
    error            其它一切(别的状态码、200 但结构不对、意料之外的异常)

降级语义(重要)
──────────────
判定是**增强项**, 不是入库的前置条件。任何失败一律变成上面某个结局交回, **绝不抛、
绝不重试**(预算只有 8 秒; policy_blocked / bad_request 重试也是同一个结果)。调用方
(``core.commit_drafts``)自己再包一层 try, 且在项目写锁与恢复指纹的那个大 try 之外调。

实现说明
────────
同步 ``requests``, 与 librarian_client 同理。超时给成 ``(连接, 读)`` 二元组: 单个数字在
requests 里是"连接 N 秒 + 每次读 N 秒", 最坏是两倍; 整批判定的**共同截止**由调用方用线程池
的 ``wait(timeout=...)`` 兜住, 这里只保证单个请求本身有界。
"""

from __future__ import annotations

import time
from typing import Any, Optional

import requests

import config
import telemetry
from logger_utils import mask_secrets

JUDGE_OK = "ok"
JUDGE_NOT_CONFIGURED = "not_configured"
JUDGE_TIMEOUT = "timeout"
JUDGE_POLICY_BLOCKED = "policy_blocked"
JUDGE_BAD_REQUEST = "bad_request"
JUDGE_UNAVAILABLE = "unavailable"
JUDGE_JEV_FAILED = "jev_failed"
JUDGE_ERROR = "error"

STATUSES = (JUDGE_OK, JUDGE_NOT_CONFIGURED, JUDGE_TIMEOUT, JUDGE_POLICY_BLOCKED,
            JUDGE_BAD_REQUEST, JUDGE_UNAVAILABLE, JUDGE_JEV_FAILED, JUDGE_ERROR)

# 账本主键里的 subject_type。TV notes_v1_13 的 CHECK 从一开始就留了这一档(D-072)。
SUBJECT_TYPE = "aw_version"

# 连接超时单独压短: Railway 同区连接是毫秒级, 连 3 秒还没连上就是连不上了,
# 别让它吃掉读的预算。
_CONNECT_TIMEOUT_CAP_SEC = 3.0

_DETAIL_MAX = 300
# 错误体原文先留长一些再交给 result(): 脱敏要在**截断之前**做。原来这里先截到 300 再脱敏,
# key 恰好跨在第 300 个字上时只剩半截, 值级脱敏认不出半截, 那半截就原样留在 detail 里。
_RAW_DETAIL_MAX = 4 * _DETAIL_MAX


def configured() -> bool:
    """URL 和 key 都配了才算接上。``/health`` 的回显也走这个函数(同源, 见 deskcore.md §4.2)。"""
    return bool(config.JUDGE_URL and config.JUDGE_API_KEY)


def timeout_sec() -> float:
    return float(config.JUDGE_TIMEOUT_SEC)


def build_draft_request(*, version_id: str, title: str, body: str, project: str,
                        category: Optional[str] = None) -> dict:
    """组一篇稿子的 /judge_draft 请求体。

    · ``subject_id`` = ``versions.id``: 账本主键含它, 必须是这篇自己的 id(judge 对
      ``write=true`` + 默认的 ``"draft"`` 会 422, 就是怕所有稿子写进同一个主键)。
    · ``project`` 必传: 未发布稿的数据出境口径按项目走(docs/00 #7), judge 据它决定只跑
      通用层 + 平台层, 还是整篇拒绝(处方药)。``category`` 知道才传(TV 的受控词表)。
    · ``judge_paras = "never"``: 不判段, 一篇的调用次数压到最少, 才放得进 8 秒。
    · 不带 ``brief``: 写作台的规则是自由文本, 编不成闭集题(那要模型); 而且未清关项目的
      项目层本来就会被 judge 按出境口径丢掉。项目题库留给写手侧的 MCP 工具。
    · ``return_rows = false``: 账本由 judge 自己写, 不把几十行账本拖回来。
    """
    payload: dict[str, Any] = {
        "subject_id": str(version_id),
        "subject_type": SUBJECT_TYPE,
        "project": project,
        "title": title or "",
        "body": body or "",
        "judge_paras": "never",
        "write": True,
        "run_tag": "primary",
        "return_rows": False,
    }
    if category:
        payload["category"] = category
    return payload


def result(status: str, *, detail: str = "", http_status: Optional[int] = None,
           elapsed_ms: int = 0, **extra) -> dict:
    """一次判定的结局, 固定形状。调用方(core)也用它造"没发出去"的那几种。"""
    out = {"judge_status": status, "http_status": http_status,
           "elapsed_ms": int(elapsed_ms), "detail": mask_secrets(detail or "")[:_DETAIL_MAX],
           "passed": None, "hard_fails": [], "calls": None, "written": None,
           "policy": None, "plan_size": 0}
    out.update(extra)
    return out


def _error_detail(resp) -> str:
    """FastAPI 的错误体是 ``{"detail": "..."}``; 拿不到就退回原文。

    只截到 ``_RAW_DETAIL_MAX``(防一整页 HTML), 脱敏与截到 ``_DETAIL_MAX`` 都在 ``result()`` 里、
    按先脱敏后截断的顺序做。
    """
    try:
        body = resp.json()
    except Exception:                                  # noqa: BLE001
        return (getattr(resp, "text", "") or "")[:_RAW_DETAIL_MAX]
    if isinstance(body, dict) and "detail" in body:
        d = body["detail"]
        return (d if isinstance(d, str) else str(d))[:_RAW_DETAIL_MAX]
    return str(body)[:_RAW_DETAIL_MAX]


def _status_for_http_error(code: int, detail: str) -> str:
    if code == 403:
        # 403 只有出境口径这一种来路; 不是 "policy:" 开头的 403 不认 —— 那多半是中间
        # 某一层(代理 / 网关)回的, 当成 policy_blocked 会让人以为是合规挡的, 永不重试。
        return JUDGE_POLICY_BLOCKED if detail.strip().startswith("policy:") else JUDGE_ERROR
    if code in (400, 422):
        return JUDGE_BAD_REQUEST
    if code in (401, 404, 503):
        # 401 key 不对 / 503 judge 没配 JUDGE_API_KEY 或 Supabase / 404 JUDGE_URL 指错
        # 或服务版本太老(没有 /judge_draft)—— 三个都是"要人去改部署", 归一档。
        return JUDGE_UNAVAILABLE
    if code == 502:
        return JUDGE_JEV_FAILED
    return JUDGE_ERROR


def _normalize_ok(payload: Any) -> Optional[dict]:
    """200 的响应体 → 结局里要留的那几个字段。结构不对回 None(算 error, 不算 ok)。

    ⚠️ 与馆员那条同一个道理(codex review P2 on librarian_client): 200 但结构不对**不是**
    成功。把它记成 ok, 这一类故障就永远没人看了。
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("passed"), bool):
        return None
    fails = []
    for h in payload.get("hard_fails") or []:
        # [bank, qid, answer, p, evidence] —— 证据句不带回来: 它是稿子里的原句, 账本里
        # 有; 影子期也不希望写手模型对着它改稿(见 core 的 JUDGE_SHADOW_NOTE)。
        if isinstance(h, (list, tuple)) and len(h) >= 4:
            fails.append([h[0], h[1], h[2], h[3]])
    policy = payload.get("policy")
    if isinstance(policy, dict):
        policy = {k: policy.get(k) for k in
                  ("published", "project", "dropped_banks", "dropped_hard_rules")
                  if k in policy}
    else:
        policy = None
    calls = payload.get("calls")
    written = payload.get("written")
    return {"passed": payload["passed"], "hard_fails": fails,
            "calls": calls if isinstance(calls, int) else None,
            "written": written if isinstance(written, int) else None,
            "policy": policy,
            "plan_size": len(payload.get("plan") or []) if isinstance(payload.get("plan"), list) else 0}


def judge_draft(payload: dict, *, timeout: Optional[float] = None) -> dict:
    """判一篇。**永不抛**, 返回 ``result(...)`` 的固定形状。

    ``timeout`` 不给就用 ``config.JUDGE_TIMEOUT_SEC``(默认 8 秒)。
    """
    sid = str((payload or {}).get("subject_id") or "")
    if not configured():
        return result(JUDGE_NOT_CONFIGURED, detail="JUDGE_URL / JUDGE_API_KEY 未配置")

    t = float(timeout if timeout is not None else timeout_sec())
    started = time.monotonic()

    def _ms() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        resp = requests.post(
            f"{config.JUDGE_URL.rstrip('/')}/judge_draft",
            headers={"X-Judge-Key": config.JUDGE_API_KEY},
            json=payload,
            timeout=(min(_CONNECT_TIMEOUT_CAP_SEC, t), t),
        )
    except requests.Timeout as exc:
        # ⚠️ 必须排在 ConnectionError 前面: ConnectTimeout 同时是两者的子类。
        out = result(JUDGE_TIMEOUT, detail=f"{t:g}s 内没回: {exc}", elapsed_ms=_ms())
        _log(sid, out)
        return out
    except requests.ConnectionError as exc:
        out = result(JUDGE_UNAVAILABLE, detail=f"连不上 judge: {exc}", elapsed_ms=_ms())
        _log(sid, out)
        return out
    except Exception as exc:                           # noqa: BLE001 — 增强项, 一律不抛
        out = result(JUDGE_ERROR, detail=f"{type(exc).__name__}: {exc}", elapsed_ms=_ms())
        _log(sid, out)
        return out

    code = getattr(resp, "status_code", None)
    try:
        if code != 200:
            detail = _error_detail(resp)
            out = result(_status_for_http_error(int(code or 0), detail),
                         detail=f"HTTP {code}: {detail}", http_status=code, elapsed_ms=_ms())
        else:
            norm = _normalize_ok(resp.json())
            if norm is None:
                out = result(JUDGE_ERROR, http_status=code, elapsed_ms=_ms(),
                             detail="judge 回了 200 但响应体不是约定的形状(缺 passed)")
            else:
                out = result(JUDGE_OK, http_status=code, elapsed_ms=_ms(), **norm)
    except Exception as exc:                           # noqa: BLE001 — 解析失败也不抛
        out = result(JUDGE_ERROR, http_status=code, elapsed_ms=_ms(),
                     detail=f"解析响应失败 {type(exc).__name__}: {exc}")
    _log(sid, out)
    return out


def _log(subject_id: str, out: dict) -> None:
    """每篇一行结构化日志(同馆员: 每种结局一个可 grep 的状态)。"""
    telemetry.log_event(
        "judge_draft_result",
        subject_id=subject_id, state=out["judge_status"],
        http_status=out.get("http_status"), elapsed_ms=out.get("elapsed_ms"),
        passed=out.get("passed"), error=out.get("detail") or None,
    )
