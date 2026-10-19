"""写作台 skill 版本探针 —— 服务端抽查写手手上的 skill 是不是最新版, 不是就在工具返回值里带上升级提醒。

为什么在服务端做: 不管哪一版 skill, 写稿都要调 deskcore 的工具; 而 skill 文件拷到写手机器上之后, 改了没人知道。
skill 自己核版本 (get_protocol) 靠模型愿不愿意调, 更老的 skill 连 protocol_version 那一行都没有。服务端每次都在,
所以由它来抽查:

  · 入口工具报版本: ``open_project(skill_version=…)`` / ``comments_for(skill_version=…)`` / ``get_protocol(local_version=…)``
    带 skill 里 protocol_version
    那一行的 12 位值。工具说明随服务端更新 (MCP tools/list 现取), 所以已经装好的 skill 也会照新说明传 ——
    **不改协议、不改 skill 文件**, 写手不用为这个再导一次。
  · 每个写手 (按 key 映射到的 user_id) 记最近一次报的版本。没报 → 先看 12 小时内有没有报过; 没有就记「没核到」。
  · 入口工具: 不是最新 / 没核到 → 返回值带 ``skill_update``。
  · 其它工具 (抽查): 这个写手记录在案是旧版 / 没核到 → 返回值也带 ``skill_update``, 但同一个人 30 分钟最多一次,
    免得刷屏。进程重启后还没报过版本的人不提醒 —— 否则每次部署都会给正在写稿的人误报一轮。
  · ``skill_update.message`` 末尾两行是升级入口 (仓库里 skill 的地址 + WorkBuddy 的「更新技能/bywood-writing-desk」),
    模型照抄给写手。

不拦任何调用: 旧版 skill 照样能写 (get_protocol 在版本对不上时会把新正文发下去), 提醒只是让写手尽快升级。
状态只在进程内存 (deskcore 单进程, railway.json 的 uvicorn 没开 --workers)。计数走要 key 的 /skill-versions,
不放 /health —— /health 不鉴权, 按 app.py 的规矩不许放业务数据。

记录按写手 (user_id) 而不是按机器: stateless_http 下没有会话 id。同一个人两台机器一新一旧, 记录会随最近一次
报的版本来回变, 新版那台也可能捎上一条「你手上的是旧版」—— 让那台也升级就好。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Optional

UPDATE_URL = ("https://github.com/TangVarie/autowriter/tree/claude/xhs-content-workstation-BSEuH/"
              "skills/bywood-writing-desk")
UPDATE_COMMAND = "更新技能/bywood-writing-desk"

UP_TO_DATE, OUTDATED, UNKNOWN = "up_to_date", "outdated", "unknown"
ENTRY_TOOLS = frozenset({"open_project", "get_protocol", "comments_for"})   # comments_for: 写评论不用先 open_project
RECORD_TTL_S = 12 * 3600       # 入口工具没带版本时, 12 小时内报过的那次算数
REMIND_EVERY_S = 30 * 60       # 抽查提醒: 同一个人 30 分钟最多一次
HEALTH_WINDOW_S = 24 * 3600

_lock = threading.Lock()
_seen: dict[str, tuple[Optional[str], float]] = {}   # user → (报的版本, 时刻)。状态不存, 每次拿当前版本现算 ——
                                                       # 协议换了版, 之前报「最新」的人随之变成旧版
_reminded: dict[str, float] = {}                          # user → 上次在非入口工具上提醒的时刻


def normalize(raw) -> Optional[str]:
    """宽松取 12 位十六进制 (模型可能把整行 "protocol_version: xxx" 或 HTML 注释原样传进来)。
    空 / None → None (没报); 报了但认不出 12 位十六进制 (截断、乱填) → 原值前 12 个字符, 按旧版算 ——
    与 get_protocol 的 up_to_date 判定同一个口径 (那边也判 False)。"""
    if raw is None or not str(raw).strip():
        return None
    m = re.search(r"[0-9a-f]{12}", str(raw).lower())
    return m.group(0) if m else str(raw).strip()[:12]


def _key(user_id) -> str:
    return str(user_id or "anonymous")


def _status(local: Optional[str], current: str) -> str:
    return UNKNOWN if local is None else (UP_TO_DATE if local == current else OUTDATED)


def report(user_id, raw_version, current: str, *, now: Optional[float] = None) -> tuple[str, Optional[str]]:
    """入口工具调: 记下这个写手报的版本, 回 (状态, 版本)。没带版本时沿用 12 小时内的记录, 没有就是 unknown。"""
    now = time.time() if now is None else now
    local = normalize(raw_version)
    k = _key(user_id)
    with _lock:
        ts = now
        if local is None:
            prev = _seen.get(k)
            if prev and now - prev[1] < RECORD_TTL_S and prev[0]:
                # 沿用旧记录, 时间也沿用 —— 否则一直不带版本的调用会让一条旧记录永不过期
                local, ts = prev[0], prev[1]
        status = _status(local, current)
        _seen[k] = (local, ts)
        if status != UP_TO_DATE:
            _reminded[k] = now     # 入口工具这次已经提醒过了, 抽查从现在起算 30 分钟, 不紧跟着再来一条
    return status, local


def notice(status: str, local: Optional[str], current: str) -> dict:
    if status == OUTDATED:
        lead = f"写作台 skill 不是最新版（你手上的是 {local}，最新是 {current}），请先升级："
    else:
        lead = "没核到写作台 skill 的版本（skill 里应该有 protocol_version 这一行，没有就是旧版），请升级："
    return {
        "status": status,
        "local": local,
        "current": current,
        "message": f"{lead}\n{UPDATE_URL}\n{UPDATE_COMMAND}",
        "action": "把 message 原样（三行都要）放在给用户回复的最前面，然后照常继续；不要改写链接和「更新技能」那一行。",
    }


def annotate(tool_name: str, user_id, result, current: str, *, now: Optional[float] = None):
    """非入口工具的抽查: 这个写手记录在案是旧版 / 没核到, 且 30 分钟内没提醒过 → 返回值带 skill_update。
    入口工具自己处理 (见 tools.open_project / get_protocol), 这里不重复加。只动 dict 返回值。"""
    if tool_name in ENTRY_TOOLS or not isinstance(result, dict) or "skill_update" in result:
        return result
    now = time.time() if now is None else now
    k = _key(user_id)
    with _lock:
        rec = _seen.get(k)
        if not rec or now - rec[1] >= RECORD_TTL_S:
            return result
        local = rec[0]
        status = _status(local, current)
        if status == UP_TO_DATE or now - _reminded.get(k, 0.0) < REMIND_EVERY_S:
            return result
        _reminded[k] = now
    result["skill_update"] = notice(status, local, current)
    return result


def health(current: str, *, now: Optional[float] = None) -> dict:
    """/health 用: 24 小时内报过版本的写手各是什么状态 (只给计数, 不给是谁)。"""
    now = time.time() if now is None else now
    counts = {UP_TO_DATE: 0, OUTDATED: 0, UNKNOWN: 0}
    old_versions: dict[str, int] = {}
    with _lock:
        for local, ts in _seen.values():
            if now - ts >= HEALTH_WINDOW_S:
                continue
            status = _status(local, current)
            counts[status] += 1
            if status == OUTDATED and local:
                old_versions[local] = old_versions.get(local, 0) + 1
    return {"current": current, "writers_24h": counts, "outdated_versions": old_versions}


def reset() -> None:
    """测试用。"""
    with _lock:
        _seen.clear()
        _reminded.clear()
