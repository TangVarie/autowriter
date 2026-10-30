"""写作台 skill 版本探针 —— 服务端核对这次调用报上来的 skill 版本, 是旧版就在工具返回值里带上升级提醒。

为什么在服务端做: 不管哪一版 skill, 写稿都要调 deskcore 的工具; 而 skill 文件拷到写手机器上之后, 改了没人知道。
skill 自己核版本 (get_protocol) 靠模型愿不愿意调, 更老的 skill 连 protocol_version 那一行都没有。服务端每次都在,
所以由它来核:

  · 入口工具报版本: ``open_project(skill_version=…)`` / ``comments_for(skill_version=…)`` / ``get_protocol(local_version=…)``
    带 skill 里 protocol_version
    那一行的 12 位值。工具说明随服务端更新 (MCP tools/list 现取), 所以已经装好的 skill 也会照新说明传 ——
    **不改协议、不改 skill 文件**, 写手不用为这个再导一次。
  · **提醒只看这一次调用报上来的版本** (2026-10-30 发布收尾): 最新 → 不提醒; 旧版 → 返回值带 ``skill_update``;
    没报 (或报的是「无」「none」这类占位) → 只记一笔给计数, 不提醒。其它工具不带提醒。
  · ``skill_update.message`` 末尾两行是升级入口 (仓库里 skill 的地址 + WorkBuddy 的「更新技能/bywood-writing-desk」),
    模型照抄给写手。

原来按写手 (key 映射到的 user_id) 记最近一次报的版本, 没报就沿用 12 小时内的记录、没有就提醒「没核到版本, 请升级」,
其它工具再按记录抽查 (30 分钟一次)。可写作台现在是**一个团队共用一个身份** (一个 user_id): 一个人手上的旧 skill
让所有人都收到升级提醒; 进程一重启记录清零, 没带版本的 open_project 又给每场开头报一句「没核到版本」。按人记在这种
用法下既认不出是谁, 也说不准 —— 所以不再按身份推断, 只认这一次调用自己报的。

不拦任何调用: 旧版 skill 照样能写 (get_protocol 在版本对不上时会把新正文发下去), 提醒只是让写手尽快升级。
计数状态只在进程内存 (deskcore 单进程, railway.json 的 uvicorn 没开 --workers)。计数走要 key 的 /skill-versions,
不放 /health —— /health 不鉴权, 按 app.py 的规矩不许放业务数据。
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
HEALTH_WINDOW_S = 24 * 3600

# 模型在 skill 里找不到 protocol_version 那一行时, 不传空而是传一个「没有」—— 和没报同一个意思, 不算一个版本。
_PLACEHOLDERS = frozenset({"无", "没有", "空", "未知", "none", "null", "n/a", "na", "unknown", "-"})

_lock = threading.Lock()
# (身份, 报的版本 / None=没报) → 最近一次的时刻。只给 /skill-versions 计数用, 不决定提醒。按 (身份, 版本) 而不是按身份记:
# 一个团队共用一个身份时, 按身份只能留下最后报的那一个版本, 「还有人在用旧版」就看不见了。状态不存, 计数时拿当前版本现算 ——
# 协议换了版, 之前报「最新」的那一笔随之算旧版。
_seen: dict[tuple[str, Optional[str]], float] = {}


def normalize(raw) -> Optional[str]:
    """宽松取 12 位十六进制 (模型可能把整行 "protocol_version: xxx" 或 HTML 注释原样传进来)。
    空 / None / 「无」这类占位 → None (没报); 报了但认不出 12 位十六进制 (截断、乱填) → 原值前 12 个字符, 按旧版算 ——
    与 get_protocol 的 up_to_date 判定同一个口径 (那边也判 False)。"""
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip()
    if text.lower() in _PLACEHOLDERS:
        return None
    m = re.search(r"[0-9a-f]{12}", text.lower())
    return m.group(0) if m else text[:12]


def _key(user_id) -> str:
    return str(user_id or "anonymous")


def _status(local: Optional[str], current: str) -> str:
    return UNKNOWN if local is None else (UP_TO_DATE if local == current else OUTDATED)


def report(user_id, raw_version, current: str, *, now: Optional[float] = None) -> tuple[str, Optional[str]]:
    """入口工具调: 记下这次报的版本 (给计数), 回 (这次的状态, 版本)。状态只看这一次报的: 没报就是 unknown,
    不沿用之前的记录 —— 同一个身份背后可能是好几个写手。"""
    now = time.time() if now is None else now
    local = normalize(raw_version)
    with _lock:
        _seen[(_key(user_id), local)] = now
        for k in [k for k, ts in _seen.items() if now - ts >= HEALTH_WINDOW_S]:
            del _seen[k]          # 过了计数窗口的不留, 免得乱填的版本串越攒越多
    return _status(local, current), local


def notice(local: Optional[str], current: str) -> dict:
    """旧版的升级提醒。只在这次报上来的是旧版时给 (见 probe)。"""
    return {
        "status": OUTDATED,
        "local": local,
        "current": current,
        "message": (f"写作台 skill 不是最新版（你手上的是 {local}，最新是 {current}），请先升级：\n"
                    f"{UPDATE_URL}\n{UPDATE_COMMAND}"),
        "action": "把 message 原样（三行都要）放在给用户回复的最前面，然后照常继续；不要改写链接和「更新技能」那一行。",
    }


def probe(user_id, raw_version, current: str, *, now: Optional[float] = None) -> Optional[dict]:
    """入口工具 (open_project / comments_for / get_protocol) 调: 记一笔, 这次报的是旧版才回提醒 (``skill_update``),
    最新 / 没报都回 None。"""
    status, local = report(user_id, raw_version, current, now=now)
    return notice(local, current) if status == OUTDATED else None


def health(current: str, *, now: Optional[float] = None) -> dict:
    """/skill-versions 用: 24 小时内报上来的版本各是什么状态 (只给计数, 不给是谁)。

    ``writers_24h`` 数的是 (身份, 版本) 的组合: 每个身份报过的每个不同版本各算一个, 没报版本的调用每个身份算一个 unknown。
    一个团队共用一个身份时, 它读作「这 24 小时里有几种版本在用」, 不是人数。"""
    now = time.time() if now is None else now
    counts = {UP_TO_DATE: 0, OUTDATED: 0, UNKNOWN: 0}
    old_versions: dict[str, int] = {}
    with _lock:
        for (_user, local), ts in _seen.items():
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
