"""新项目建出来之后, 提醒负责人把它接上判定服务。

为什么有这一段(2026-11-16): 判定(打分 / 合规 / 规则检查 / 评论整组判定)按 TV 的项目号认项目, 写作台项目要在
``tv_project_map`` 里有一行才算接上(core._judge_project_context)。写手在对话里用 ``create_project`` 建出来的新项目
**没有这一行**, 而接上它要一个 TV 项目号, 只有负责人给得出来。于是新项目的稿子照常能写、能存、能交, 判定那几步
安安静静地不跑 —— 模型每次会说一句「还没接上判定服务」, 写手未必知道这句话意味着要去找负责人, 负责人更是无从知道
多了一个项目。11-16 查库: 团队名下 40 个项目里 22 个没接, 其中当天新建的一个就是这么来的。

两条路一起走:

  · **告诉写手**(``writer_note``): 进 ``create_project`` 的返回值, 模型原样说给写手。只说后果和该找谁, 不带命令 ——
    同 ``_judge_project_context`` 的 detail: 补救命令只进负责人那一侧。
  · **推给负责人**(``notify_new_project``): 配了 ``DESKCORE_OWNER_WEBHOOK_URL`` 就往那个飞书机器人发一条, 里面有
    项目名、品牌、project_id 和要跑的那条命令。没配就不发。单独一个配置项而不是复用 ``FEISHU_WEBHOOK_URL``:
    那个是给稿件推送用的群, 不能因为加了这个功能就多出一类消息。

推送是同步的、有很短的上限: 写手那句话要看推没推成(推成了才能说「已经通知负责人」), 而建项目一天没几次, 多等两三秒
不碍事。推失败不影响建项目 —— 项目已经在库里了, 这里只是少一条提醒, 写手那句话会改成请他转告。
"""

from __future__ import annotations

import logging

import requests

import config
from logger_utils import mask_secrets

logger = logging.getLogger("deskcore.ownernotice")

# (连接, 读): 加起来就是建项目最多多等的时间。飞书机器人正常几百毫秒回。
_TIMEOUT = (2.0, 3.0)

# 机器人开了「自定义关键词」安全设置的话, 关键词填这个就行: 每条消息开头都带它。
KEYWORD = "写作台"


def webhook_url() -> str:
    return (getattr(config, "DESKCORE_OWNER_WEBHOOK_URL", "") or "").strip()


def owner_text(name: str, brand: str, project_id: str) -> str:
    """发给负责人的那一条。命令照抄就能跑, 只差 TV 项目号。"""
    return (
        f"【{KEYWORD}】新建了一个项目, 还没接判定服务\n"
        f"项目: {name}\n"
        f"品牌: {brand or '(没填)'}\n"
        f"project_id: {project_id}\n"
        "接上之前, 这个项目的稿子照常能写、查重、存档, 但打分、合规检查、规则检查和评论整组判定都不跑。\n"
        "接上的命令(把 <TV项目号> 换成 TV 那边的项目号; 处方药项目接不接, 未发布稿都不会送判):\n"
        f"python -m deskcore.cli tv-map add --tv-project <TV项目号> --project {project_id}"
    )


def notify_new_project(name: str, brand: str, project_id: str) -> bool:
    """推一条给负责人。推成了回 True; 没配、推失败都回 False, **不抛**。"""
    url = webhook_url()
    if not url:
        return False
    try:
        resp = requests.post(url, json={"msg_type": "text",
                                        "content": {"text": owner_text(name, brand, project_id)}},
                             timeout=_TIMEOUT)
    except Exception as exc:                            # noqa: BLE001 — 提醒发不出去不能拖垮建项目
        logger.warning("新项目提醒没发出去(project=%s): %s", project_id, mask_secrets(f"{type(exc).__name__}: {exc}"))
        return False
    if resp.status_code != 200:
        logger.warning("新项目提醒被拒(project=%s): HTTP %s", project_id, resp.status_code)
        return False
    # 飞书机器人 HTTP 200 也可能是失败(关键词不对、签名校验没过): 成功是 code / StatusCode 为 0。
    try:
        body = resp.json()
    except ValueError:
        logger.warning("新项目提醒的回包不是 JSON(project=%s), 当没发成", project_id)
        return False
    code = body.get("code", body.get("StatusCode")) if isinstance(body, dict) else None
    if code != 0:
        logger.warning("新项目提醒没被接受(project=%s): %s", project_id, mask_secrets(str(body)[:200]))
        return False
    return True


def writer_note(name: str, notified: bool) -> dict:
    """进 ``create_project`` 返回值的那一块。``message`` 是念给写手的原话。"""
    base = (f"「{name}」是新建的项目, 还没接上判定服务: 稿子照常能写, 查重和存档照常, "
            "但打分、合规检查和规则检查暂时不跑。")
    tail = ("已经通知负责人了, 接上之后自动生效, 不用你做什么。" if notified
            else "请把这个项目名发给负责人, 让他接上; 接上之后自动生效。")
    return {"connected": False, "owner_notified": bool(notified), "message": base + tail}
