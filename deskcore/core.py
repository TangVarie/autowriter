"""deskcore/core.py — 写作台内核的纯逻辑层。

不 import FastAPI / MCP SDK —— cli.py 能直接调、能 selftest, 与 app.py 共用同一
份逻辑(librarian 那边 core/CLI/HTTP 三个 adapter 是同一个形状)。

【复用】本仓已有的东西, 不重写:
    memory.build_layered_system_prompt   五层 + hard/soft 分级
    db.get_service_client / get_project / set_item_example_label / upsert_memory
    dedup.embed_texts / cosine_similarity / embeddings_available
    librarian_client.build_brief / fetch_flywheel_lessons
    judge_client.judge_draft             (入库判定: HTTP 调 judge 拿事实, 与借卡同一种
                                          关系 —— deskcore 自己仍然零 LLM 调用)
    logger_utils.mask_secrets            (判定那块的 detail / error 回给调用方、落库前脱敏)
    db._parse_pgvector                   (R-034: PostgREST 把 pgvector 序列化成
                                          字符串, 不归一的话 cosine 静默返回 0.0)

【新增】的只有四样(其余全是薄封装):
    1. 发牌 draw_angles + 跨批次台账
    2. 查重硬闸 check_drafts + 持久全量指纹库
    3. 正例按【相关性】选取, 取代 recency top-5(断掉趋同回路)
    4. 个人调校笔记分层(项目共享基线 + 个人叠加)。蒸馏本身【交给调用方模型做】,
       服务端只出材料和口径 —— 见 build_distillation_task / save_my_style。
"""

from __future__ import annotations

import base64
import hashlib
import json
import concurrent.futures as cf
import contextlib
import dataclasses
import logging
import os
import random
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional

import config
import db
import dedup
# ⚠️ 新增的耦合(export_drafts 用它): exporter 顶层 import 了 docx / openpyxl。
#    两个都在 requirements.lock 里, 而 deskcore 的部署方式就是
#    `pip install -r requirements.lock -r deskcore/requirements.txt`
#    (见 deskcore/requirements.txt 头两行), 所以这条是成立的。
#    真要给 deskcore 做瘦镜像的话, 这一行会让**整个服务起不来**而不是只坏一个
#    工具 —— 那时候把它改成函数内延迟 import。
import exporter
import judge_client
import librarian_client
import memory
from logger_utils import mask_secrets

from . import delivery, rulecheck, rulecompile
from . import fingerprint as fp
from . import store, structure, tvlink, vocab

logger = logging.getLogger("deskcore.core")

MAX_POSITIVE = 5
MAX_NEGATIVE = 3

# 简报里回显"抽了没写"的回看天数(D-071)。7 天 = 一个工作周,
# 与 TV 夜跑那盏灯 check_angle_ledger_leak.py 的默认窗口同口径。
ANGLE_DEBT_DAYS = 7
# 只数这个时刻之后抽的牌(2026-10-30 发布收尾)。W-38(查重时就记下稿子, tv-sync 拿飞书的帖对它)是 2026-10-29 08:26 UTC
# 上线的; 在那之前只有 score_drafts 会记稿子, 写手没有存档这一步(D-120), 于是那之前抽的牌大多已经写完、发出去了,
# 只是对不回来、销不了账。拿它们当「抽了没写」报, 每场开头都是一串其实写过的坐标。简报的 angle_debt、存档后的
# angle_debt、archive_pending 都按这个下限数; 一周之后它自然落到 7 天窗口外面, 不用再改。
ANGLE_DEBT_SINCE = "2026-10-29T08:26:00+00:00"

# 交付前没存档的牌(W-29, 2026-10-23)。10-20~10-22 途鸽五轮 73 张、百健士一轮 24 张牌全没存档: 每一轮 open_project /
# draw_angles / 1~3 次 check_drafts 都回了 200, 然后调用方就不再调写作台了, score_drafts 一次都没到库。和 W-06 同一个
# 形状(交付时模型没调存档), D-071 定过不设硬闸(不拦发牌、不拦交付), 所以把"存了档再给用户看"这句放进离交付最近的几个
# 返回值里: draw_angles / check_drafts / record_rule(见 archive_pending)。简报的 angle_debt 管不到这里: 两轮里
# open_project 和 draw_angles 是一起发出去的(06:32:32 已经发完牌, 06:32:43 简报才查账), 提醒到的时候牌早抽完了。
# 窗口 1 天: 只管这一场还在写的牌; 更早挂着的由简报的 angle_debt(7 天)管。
ARCHIVE_PENDING_DAYS = 1
# 和 protocol.md「5. score_drafts」那段同一个口径: 查重通过 → 交给用户之前、同一轮里存档; 改的是已经存过的那篇 →
# score_drafts 带 replaces_version_id(它先摘掉旧版再查重)。要先过一遍 check_drafts 也行(2026-10-30 发布收尾): 同样
# 带上 replaces_version_id, 它就不拿这篇和它自己存档的旧版比(见 _without_own_versions)。原来 check_drafts 不认这个
# 字段, 改稿一过 check_drafts 就被自己的旧版拦下(「正文开头与历史稿完全一致」), 这里才叫模型跳过它 —— 模型照协议先查一遍
# 的那几次, 写手听到的是「改过的稿子和历史稿重复」。整篇推翻、换角度重写的才当新稿, 走 check_drafts → score_drafts。
ARCHIVE_PENDING_NEXT_STEP = (
    "查重通过的稿子, 交给用户之前就在同一轮里调 score_drafts 存档(每篇带 angle_key, 不等用户表态); "
    "按反馈改的是已经存过的那篇, 就调 score_drafts 带 replaces_version_id(它会先摘掉旧版再查重; 要先过 check_drafts "
    "的话那篇同样带上 replaces_version_id, 就不会拿它和自己的旧版比); 整篇推翻、换角度重写的当新稿, 先过 check_drafts "
    "再存; 都存完再给用户看"
    "(存档是你这边的事, 别跟用户提「存档」、也别让用户做什么: 用户写完贴进飞书就行, 查重时带了 angle_key 的稿子"
    "发出去以后写作台会自己从飞书对回来)。")
def _supersede_snapshots(client, project_id: str, drafts: list[dict], vids: dict[int, str]) -> None:
    """commit_drafts 存成版本的那几篇, 同样文字的查重底稿记成已转正(W-38 复审)。附带的一步, 失败只记 WARN。"""
    try:
        by_fp = {_draft_text_fp(drafts[i].get("title") or "", drafts[i].get("body") or ""): vid
                 for i, vid in vids.items() if tvlink.strip_hashtags((drafts[i].get("body") or "").strip())}
        if by_fp:
            store.supersede_snapshots(client, project_id, by_fp)
    except Exception:                                   # noqa: BLE001 — 底稿留着也无害(主索引先对)
        logger.warning("commit_drafts: 把存了档的底稿记成已转正失败 (project=%s)", project_id, exc_info=True)


# W-38(2026-10-29): 查重时带过 angle_key 的牌不算「抽了没写」—— 稿子已经写出来、查过重(draft_snapshots), 发出去以后
# tv-sync 从飞书对回来销账。再把它们报成欠账, 模型会按同一张牌再写一篇。只有简报的 angle_debt 按这个口径数(新开一场对话时
# 列出来让模型「按它们写」的那份); 入库后的 angle_debt 和 archive_pending 不排除 —— 那两处是在叫模型给手上的稿子存档、
# 带上分给它的 key, 刚查过的牌正该在里面(W-38 复审)。只排除最近一次查重没被打回的(store.snapshotted_angle_keys), 也只排除
# 那次查重之前抽的: 同一张牌之后又抽到的那一次还没写, 照旧算欠着(codex on aw#132)。
def _checked_angle_keys(client, project_id: str, user_id: str | None, days: int) -> dict[str, str]:
    return {k: at for k, at in store.snapshotted_angle_keys(client, project_id, user_id, days).items()
            if fp.looks_like_angle_key(k)}


def _angle_debt_note(days: int, n: int, last_drawn_at, shown: int, more: int) -> str:
    """简报里 angle_debt_note 那句话(2026-10-30 发布收尾)。说给模型听的: 这个项目发牌分到、还没有稿子的坐标, 用户这次
    要写的对得上就优先按它们写。不叫模型把数字转告用户、不问「接着写还是放弃」(那不是写手该拍板的事), 不说「你抽了」
    (一个团队共用一个身份), 也不提存档(写手没有这一步, D-120; 存档的事 draw_angles / check_drafts 的 archive_pending 会说)。"""
    return (
        f"这个项目近 {days} 天发牌分到的坐标里, 有 {n} 个还没有对应的稿子(最近一次发牌 {last_drawn_at}; "
        "查重时带过 angle_key 的已经写出来了, 不在这里面)。用户这次要写的对得上(比如要这个项目的一批稿、没点具体题目), "
        "就优先按这些坐标写, 不够再调 draw_angles 补齐; 用户点了具体题目或角度, 就照用户的写, 这些坐标放着就行。"
        + (f"坐标在 angle_debt.angles 里(列了 {shown} 个" + (f", 还有 {more} 个没列" if more > 0 else "")
           + "), 按哪个写, 查重时就带上它的 angle_key。" if shown else "")
        + "这段是给你挑坐标用的, 不用转告用户, 也不用问用户怎么处理。")

# 存档带的 angle_key 对不上台账(W-31, 2026-10-27)。10-26 sportsix 一场 20 张牌, 5 批存了 19 篇, 每篇都带了 key, 可没有
# 一条在台账里(7~8 位带下划线的编号、53 位的长串), 牌一张没销。score_drafts 的回包现在会说清(angle_key_warning), 但之后
# draw_angles / check_drafts / record_rule 的 archive_pending 只会说「还有 N 张牌没存档」—— 模型明明存过了, 只会当提醒
# 有误。所以存档时对不上的记一笔, archive_pending 一并带上(angle_key_mismatch)。记在进程内存里, 同 _borrowed_arms 的
# 取舍(deskcore 单进程): 重启丢了只少这一段提醒(存档那次回包已经说过), 不为它让 archive_pending 多查一次库。
# 窗口和 archive_pending 一样 1 天, 每篇对不上的各按自己存档的时刻过期(codex review on aw#130: 原来整段跟着最近一笔续命,
# 0、23、46 小时各一篇的, 46 小时时三篇都还在, 旧 key 还占着 keys 的位置); 只记 (项目, 写手) 自己的, 别人的不给看。
ANGLE_KEY_SHOW_CHARS = 24       # 回包里 key 只露前这么多字(模型编的长串可能是一句描述)
ANGLE_KEY_MISS_SHOW = 10        # 提醒里最多列几个对不上的 key
_ANGLE_KEY_MISS_TTL = timedelta(days=ARCHIVE_PENDING_DAYS)
_angle_key_misses: dict[tuple[str, str], list[dict]] = {}   # (项目, 写手) → 每篇一笔 {"at", "problem", "angle_key"}
_angle_key_misses_lock = threading.Lock()
# 这个项目台账里没有的三种(都算进 unknown_angle_keys): 形状不对 = 编的 / 缩写的 / 抄错的; 形状对 = 别的项目的牌, 或者
# 哪个台账都没有(W-31 复审: 10-27 查生产库近 60 天 48 条对不上的, 15 条是 20 位十六进制, 不能一律说「自己编的」)
ANGLE_KEY_ABSENT = (store.ANGLE_KEY_UNKNOWN, store.ANGLE_KEY_OTHER_PROJECT, store.ANGLE_KEY_NOT_IN_LEDGER)

# 判定阈值与 angle_key 住在 fingerprint.py —— 那个模块只用标准库, 让 selftest
# 能在不装 supabase/anthropic 的裸环境里跑(本模块顶层 import db, 拖整条依赖链)。
angle_key = fp.angle_key
verdict = fp.verdict


def sb():
    return store.client()


# ══════════════════════════════════════════════════════════════════════
# 归属校验(审计 COR-015)
# ══════════════════════════════════════════════════════════════════════

class ProjectNotFound(ValueError):
    """project_id 在库里没有这一行。多半是 id 抄错, 不是权限问题。

    与 PermissionError 分开是有意的: 「不存在」和「不是你的」对调用方是两种
    完全不同的下一步(改 id / 换项目), 混成一种会让人反复试同一个错 id。
    """


class ProjectIsolationRejected(ValueError):
    """Actionable cross-project rule rejection; REST maps this to 400."""


def assert_project_access(client, project_id: str, *,
                          user_id: str | None) -> dict:
    """**整套项目归属校验的唯一实现**。通过则返回项目整行。

    审计 COR-015: 在此之前 deskcore 对 project_id 【没有任何归属校验】——
    ``check_drafts`` / ``borrow_lessons`` / ``list_projects`` 连调用者是谁都不问,
    其余工具虽然拿到了 user_id 却只用它读个人层, 从不核对项目归谁。于是任一
    持有效 key 的调用方传入他人 project_id 就能:

      · 读他人项目的全部成稿标题(``check_drafts`` 的 ``collided_with`` 会回显);
      · 往他人项目写指纹、写角度台账、写团队共享的 hard 规则。

    ``deskcore/store.py`` 原来明写着"不按 owner 过滤"是设计选择(项目规则团队
    共享)。本条指出的不是那个选择本身错, 而是它的代价: 整套隔离就只剩 key 这
    一层, 而 key 这一层有 ROB-003(配错就全开)。产品决策已定 —— **按
    ``projects.owner_id`` 隔离**, 读写两侧都校验。

    ── 为什么收敛成一个函数 ──────────────────────────────────────────
    归属口径是**产品决策**, 会变(今天按 owner, 明天可能按团队成员表)。散在十
    一个工具里就意味着改口径要改十一处, 而漏掉的那处不会报错, 只会继续放行。
    所以: 判据只写在这里, 将来换模型**只改这个函数体** —— 加一张
    ``project_members`` 表就是把下面那个 ``!=`` 换成一次成员查询, 调用方一行不动。

    ── 为什么 user_id 缺失是拒绝而不是放行 ────────────────────────────
    与 ROB-003 同一口径: 身份识别不出来时, "放行"意味着一个配置疏忽就等于把
    全部租户的项目数据开放出去。匿名 dev 模式(``DESKCORE_ALLOW_ANONYMOUS=1``)
    要能用就得同时配 ``DESKCORE_DEFAULT_USER_ID`` —— 那本来就是它该配的东西
    (identity.py 的 ``resolve`` 就是这么回的), 否则个人层(正负例/调校笔记)一样
    读不出东西。
    """
    if not project_id or not str(project_id).strip():
        raise ProjectNotFound("project_id 不能为空")
    if not user_id:
        raise PermissionError(
            "无法识别调用者身份, 拒绝访问项目数据 —— deskcore 持 service_role "
            "绕过 RLS, 认不出人就等于对所有租户开放。服务端要配 DESKCORE_KEYS "
            "(推荐)或 DESKCORE_API_KEY + DESKCORE_DEFAULT_USER_ID; 本地匿名 dev "
            "模式也要配 DESKCORE_DEFAULT_USER_ID。")

    project = store.project_row(client, project_id)
    if project is None:
        raise ProjectNotFound(f"project not found: {project_id}")

    # ↓↓↓ 换归属模型时【只改这三行】↓↓↓
    owner = project.get("owner_id")
    if str(owner or "") != str(user_id):
        raise PermissionError(
            f"project {project_id} 不属于当前调用者, 拒绝访问。"
            "deskcore 按 projects.owner_id 隔离 —— 项目规则在同一 owner 的项目"
            "之间共享, 跨 owner 不共享。project_id 是不是传错了?")
    # ↑↑↑ 换归属模型时【只改这三行】↑↑↑
    return project


# ══════════════════════════════════════════════════════════════════════
# 方向档(产品向 / 流量向)
# ══════════════════════════════════════════════════════════════════════
#
# 同一条技艺在两种方向上经常是【相反】的要求。三路独立质疑在 37 簇里反复
# 撞到同一件事:"结尾截断留白"和"结尾完整收尾"、"去品牌标签"和"正文嵌品牌
# 名"、"编号分点"和"叙事时间线" —— 每一对的项目集几乎互补, 一边全是产品/
# 体验向, 另一边全是流量/叙事向。也就是说它们不是互相矛盾的两条规则, 是
# 同一条规则的两个条件分支。没有这个维度, 技艺库必然自相打架。
#
# ⚠️ 判据【只认这两个词】, 不做泛化猜测。``applicability`` 是自由文本, 库里
# 已经有 "正文" / "改简历部分" 这类**部位**标注在用 —— 那些不是方向, 必须原样
# 放行。把闸门做成"有 applicability 就按它过滤"会把这些老行全部静默挡掉,
# 而调用方看到的一切都正常。这正是本仓库反复出现的那类事故。
DIRECTION_TAGS = ("产品向", "流量向")


def detect_direction(project_name: str, tactic: str = "") -> str:
    """从项目名 / 战术名判出这次写的是哪个方向。判不出来返回 ``""``。

    判不出来时上层**放行全部规则** —— 保守方向必须是"多注入", 不是"少注入":
    漏注入一条写手明明设过的规则是无声的, 多注入一条他至少看得见。
    """
    text = f"{project_name or ''} {tactic or ''}"
    hit = [t for t in DIRECTION_TAGS if t[:2] in text]   # "产品" / "流量"
    # 两个方向词同时出现(比如"产品直出-流量版")= 判不出来, 放行。
    return hit[0] if len(hit) == 1 else ""


def filter_by_applicability(rules: list[dict], direction: str) -> tuple[list[dict], list[dict]]:
    """按方向档筛 soft 规则。返回 ``(留下的, 挡掉的)``。

    规则的 ``applicability`` 里带方向词的才受这道闸管; 空的、或者写的是别的
    东西(部位标注之类)一律放行。``direction`` 为空时全部放行。
    """
    if not direction:
        return list(rules), []
    keep, dropped = [], []
    for r in rules:
        tag = (r.get("applicability") or "").strip()
        tagged = [t for t in DIRECTION_TAGS if t in tag]
        if tagged and direction not in tagged:
            dropped.append(r)
        else:
            keep.append(r)
    return keep, dropped


# ══════════════════════════════════════════════════════════════════════
# 写作简报
# ══════════════════════════════════════════════════════════════════════

def _brief_text(brief: dict) -> str:
    parts = [brief.get(k) or "" for k in
             ("tactic", "draft_topic", "key_messages", "target_audience",
              "tone", "extra_instructions")]
    return "\n".join(p for p in parts if p).strip()


def _daily_rotation_seed(project_id, user_id, *, purpose: str, now: datetime | None = None) -> int:
    """按天轮换的种子: (用途, 项目, 写手, 北京日期) 的 sha256, 同 flywheel_arm 的取法。用途当前缀(positives /
    patterns), 免得几件事和分臂共用同一个哈希 —— 各轮各的, 不该绑在一起。"""
    day = (now or datetime.now(timezone.utc)).astimezone(_CN_TZ).date().isoformat()
    return int(hashlib.sha256(f"{purpose}|{project_id}|{user_id or ''}|{day}".encode("utf-8")).hexdigest()[:16], 16)


def select_positive_examples(candidates: list[dict], brief: dict,
                             limit: int = MAX_POSITIVE, *, project_id=None, user_id=None,
                             now: datetime | None = None) -> tuple[list[dict], str]:
    """按相关性 + 多样性挑正例, 取代 db.list_example_items 的 created_at DESC 取 5。

    为什么改: recency top-5 构成【趋同回路】—— 模型模仿最近 5 条 → 新稿被标
    positive → 窗口滚动 → 语感越收越窄。而监控这件事的 TV
    check_positive_saturation.py 只统计 external_source='truth_vault' 的行,
    那列生产库里全 NULL, 所以它从上线起永远打印"没有正例" —— 这个回路
    从来没被任何人看见过(TV D-041 / R-034 已修那个盲点)。

    候选先按 (项目, 写手, 北京日期) 定种子洗牌 —— 同一个人同一天开几场看到的是同一批, 换一天换一批。
    排不出相关性时(没有 brief 文本 / embedding 不可用 / 候选一条向量都没有)就按洗好的顺序取, 不退回 recency;
    排得出时有向量的按相关性排在前面, 没向量的照洗好的顺序接在后面 —— 也轮换, 不是"最近那几条"。
    返回 (picked, mode), mode ∈ relevance / rotating / empty。
    """
    if not candidates:
        return [], "empty"

    # ⚠️ 原来这里先算 brief 的向量、再给每条候选打分, 没向量的记 -1.0 —— 而写作台的版本【从来不算向量】
    # (10-20 查生产库: 09-01 以来 1,011 个写作台版本, 0 个有 embedding)。于是每条都是 -1.0, 稳定排序原样
    # 交回最近的 N 条, mode 却报 "relevance"; 每一场拿到的都是同几条最新正例, 正是这个函数要断的趋同回路。
    # 现在一条向量都没有就不去算 brief 的向量(还省一次 embedding 调用), 直接轮换。
    # ⚠️ 洗牌放在打分【之前】(10-20 review): 只有少数几条有向量(网页端那条生成路径会写 versions.embedding,
    # 用户在网页端标过的正例就带着)时, 其余的仍然都是 -1.0; 先洗再稳定排序, 这一截才跟着按天轮换,
    # 不然空位每天都落回最新的那几条。
    ranked = list(candidates)
    random.Random(_daily_rotation_seed(project_id, user_id, purpose="positives", now=now)).shuffle(ranked)
    mode = "rotating"
    text = _brief_text(brief)
    if text and any(c.get("embedding") for c in ranked) and dedup.embeddings_available():
        vecs = dedup.embed_texts([text])
        if vecs:
            bvec = vecs[0]
            scored = []
            for c in ranked:
                emb = c.get("embedding")
                # 没算过向量的排后面但【不丢弃】—— 否则新项目(向量还没回填)
                # 会一条正例都取不到。
                scored.append((dedup.cosine_similarity(bvec, emb) if emb else -1.0, c))
            scored.sort(key=lambda t: t[0], reverse=True)
            ranked, mode = [c for _s, c in scored], "relevance"

    # 多样性限额在 fingerprint.cap_by_shape —— 纯逻辑放无依赖模块, 让 selftest
    # 能在裸环境里验它(这一条是趋同回路的最后一道闸, 值得单独回归)。
    return fp.cap_by_shape(ranked, limit), mode


# ── 简报里的"最近的标题"(2026-10-20): 见 build_writing_brief 末尾那段 ──────────────────────
RECENT_TITLES_PUBLISHED_DAYS = 30
RECENT_TITLES_DRAFTED_DAYS = 14
RECENT_TITLES_MAX = 20
# ⚠️ 别写成"标题句式要和它们不一样"(10-20 review): 标题句式是 draw_angles 发的硬坐标, 只有 8 种, 近 20 条标题
# 几乎总把它们占全, 两句话就没法同时做到。这里只管具体说法 / 人设 / 场景, 并写明谁优先。
RECENT_TITLES_TASK = (
    "published 是这个项目最近发出去的标题, drafted 是最近在写作台写过的标题。它们只用来避开, 不要模仿, "
    "也不用念给用户听。这一批新写的稿子: 标题别和它们撞主干词、撞具体说法(标题句式照 draw_angles 分到的写, "
    "在同一种句式里换说法), 开头别复述这些标题; 人设和核心场景尽量和它们错开, 同一批里也尽量别两篇用同一个人设或"
    "同一个场景。优先级: p0 / stable 定下的人设、draw_angles 分到的坐标和切入角度、用户点名要写的题目或要改的稿, "
    "都在这一栏之上; 这一栏只管新写的稿, 改稿不受它约束。")


# ── 已验证规律怎么用(2026-10-20) ──────────────────────────────────────────────
# 10-20 生产诊断: 85% 的写手日简报里都带着同样 4 条已验证规律, 其中一条是"整篇围绕一个求助问题来组织"。模型把
# 4 条全用在每一篇上 —— 批批同一副骨架。规律是"这样写表现好过", 不是"每篇都要这样写"。清单本身原样转交
# (馆员给的, 写作台不改), 只在旁边说清楚用法, 并按条数给一个具体的轮换法: 光说"轮换"模型会自己解释成"每篇都沾一点"。
#
# 轮换的起点按天换(10-20 review): 固定从第 1 条起的话, 每一批的第 1 篇都用第 1 条, 同一副骨架还是批批出现(只是
# 落在固定的那一篇上); 而 3 篇一批、4 条规律时轮不到"不用", 和"有几篇一条都不用"自相矛盾。所以 k 条规律加一个
# "不用"排成一圈, 起点用 (项目, 写手, 北京日期) 的哈希定 —— open_project 和 borrow_lessons 同一天给的是同一句;
# 再补一条: 两篇以上的批次没轮到"不用"的, 最后一篇不用。
def pattern_rotation(k: int, seed: int = 0) -> list[int | None]:
    """第 1..k+1 篇各用第几条规律(None = 一条都不用), 之后从头再来。"""
    cycle: list[int | None] = [*range(1, k + 1), None]
    start = seed % len(cycle)
    return cycle[start:] + cycle[:start]


def validated_patterns_usage(k: int, seed: int = 0) -> str:
    rota = pattern_rotation(k, seed)
    if k > 1:
        steps = ", ".join(f"第 {i} 篇" + (f"用第 {p} 条" if p else "一条都不用") for i, p in enumerate(rota, 1))
        rule = (f"默认按篇号轮换: {steps}, 然后从头再来; 一批两篇以上、按这个轮法没轮到\"一条都不用\"的, "
                "把最后一篇改成一条都不用")
    else:
        rule = ("默认隔一篇用一次: " + ("单数篇用它, 双数篇不用" if rota[0] else "单数篇不用, 双数篇用它"))
    return ("validated_patterns 是表现好过的写法, 供挑选, 不是模板。每篇最多用其中一条, 这一批里轮换着用, "
            f"也要有几篇一条都不用。{rule}。某条跟那一篇不贴题就换成不用; 和 draw_angles 分到的坐标(内容形式、"
            "标题句式)或 p0 冲突时, 以坐标和 p0 为准。")


def _patterns_usage_for(project_id, user_id, k: int) -> str:
    return validated_patterns_usage(k, _daily_rotation_seed(project_id, user_id, purpose="patterns"))


def build_writing_brief(client, project_id: str, *, user_id: str | None = None,
                        brief: dict | None = None) -> dict:
    """一次拿全写作上下文 —— 治「一个项目 5 个提示词要点 5 次」。

    分层【直接复用 memory.build_layered_system_prompt】, 只在调用前把
    calibration_notes 拼成「项目共享基线 + 我的个人叠加」两段 —— 这是 deskcore
    唯一改动的语义(隔离口径: 项目规则团队共享 + 个人风格私有)。
    """
    brief = brief or {}
    # 归属校验 + 取项目行一次搞定 —— 这里本来就要 db.get_project, 所以 COR-015
    # 的校验在这条路径上【不多发一次查询】。
    project = assert_project_access(client, project_id, user_id=user_id)

    hard, soft = store.shared_memories(client, project_id, user_id)
    pool = store.labeled_examples(client, project_id, "positive", user_id)
    positives, pos_mode = select_positive_examples(pool, brief, project_id=project_id, user_id=user_id)
    negatives = store.labeled_examples(client, project_id, "negative", user_id)[:MAX_NEGATIVE]

    shared_calib = (project.get("calibration_notes") or "").strip()
    my_calib, _ = store.get_user_calibration(client, project_id, user_id) if user_id else ("", None)
    calib_parts = []
    if shared_calib:
        calib_parts.append(f"[项目共享基线]\n{shared_calib}")
    if my_calib:
        calib_parts.append("[我的个人风格 —— 从我手动改稿里提炼; 与项目基线冲突时以本节为准]\n"
                           + my_calib)
    calibration = "\n\n".join(calib_parts)

    # 战术后缀: app.py 的队列/快速生成两条路径都调 get_tactic_prompt_suffix 并
    # 把它作为独立一层注入(app.py:1066 / 3131 / 4857)。deskcore 不带的话, 项目
    # 配好的战术专属写作指令会【静默丢失】—— 传了 tactic 名却只影响正例排序。
    # (codex review P1)
    import projects as proj_module   # 纯函数; CI 的 import 图冒烟已覆盖该模块
    tactic_name = (brief.get("tactic") or "").strip()
    tactic_suffix = ""
    if tactic_name:
        tactic_suffix = proj_module.get_tactic_prompt_suffix(project, tactic_name) or ""

    # ⚠️ tactics 必须解码再返回。db.create_project 和战术设置页都是用
    # json.dumps 存的, PostgREST 原样带回一个【JSON 字符串】而不是 list ——
    # 直接透传的话, MCP 调用方拿到的 tactics 是一坨字符串, 想枚举有哪些战术
    # 只能自己猜着 json.loads。用 projects 里那个同款解析器
    # (get_tactic_prompt_suffix 内部就是它), 口径保持一处。(codex review)
    tactics = proj_module._parse_json_field(project.get("tactics"), []) or []

    # ── soft 规则: 相关性过滤 + 封顶 ────────────────────────────────
    # hard 全量保留(合规, 不能因为"跟本次不相关"就丢)。soft 要过一遍
    # autowriter 生产路径同样的两道(app.py:1096/3150 + db.get_confirmed_memories
    # 的 cap_per_scope), 否则 P1 会被历史偏好堆爆:
    #   · deskcore 的规则是【团队共享】的 —— 池子比 autowriter 单人视角大得多,
    #     这一层不做, 二十条互相打架的旧偏好会一起进 P1, 模型只能写出四不像;
    #   · 排序用 db._rank_memories_for_injection: 7 天内的新规则永远不被老的
    #     高频规则挤掉("我刚说过 → 立刻生效"), 这是工作台"规则不忘"的一部分。
    # (codex review round-5 P2)
    soft_ctx = " ".join(filter(None, [
        brief.get("tactic", ""), brief.get("draft_topic", ""),
        brief.get("key_messages", ""), brief.get("target_audience", ""),
        brief.get("tone", ""), brief.get("extra_instructions", ""),
    ])).strip()
    soft_report: dict = {}
    soft_all = len(soft)
    # ── 方向档闸(产品向 / 流量向) ──────────────────────────────────
    # 排在相关性过滤【前面】。相关性靠 embedding, 而库里绝大多数规则还没有
    # 向量 —— filter_soft_by_relevance 对无向量的一律放行。也就是说今天相关性
    # 这一层几乎拦不住东西, 方向闸是真正在起作用的那道。
    #
    # ⚠️ 顺序是 方向闸 → 相关性 → 排序封顶, 三者都在 cap 之前。别把相关性挪到
    # cap 后面: 那样 cap 先按"新 + 高频"挑满 12 条, 相关性再从里面删, 最后注入
    # 的会【少于 12 条】, 而池子里本来还有相关的排在第 13 位没能补上。
    direction = detect_direction(project.get("name") or "",
                                 brief.get("tactic") or "")
    soft, dropped_by_direction = filter_by_applicability(soft, direction)
    if soft_ctx:
        soft = memory.filter_soft_by_relevance(soft, soft_ctx, report_sink=soft_report)
    soft_cap = int(getattr(config, "MAX_INJECTED_MEMORIES_PER_SCOPE", 12) or 12)
    soft_by_scope = {"global": [], "project": []}
    for m in soft:
        soft_by_scope["global" if m.get("scope") == "global" else "project"].append(m)
    soft = (db._rank_memories_for_injection(soft_by_scope["global"], soft_cap)
            + db._rank_memories_for_injection(soft_by_scope["project"], soft_cap))

    layers = memory.build_layered_system_prompt(
        base_prompt=project.get("system_prompt") or "",
        tactic_suffix=tactic_suffix,
        global_memories=[m for m in soft + hard if m.get("scope") == "global"],
        project_memories=[m for m in soft + hard if m.get("scope") != "global"],
        calibration_notes=calibration,
        positive_examples=positives,
        negative_examples=negatives,
    )

    out = {
        "project_id": project_id,
        "project_name": project.get("name") or "",
        "brand": project.get("brand") or "",
        "stable": layers.get("stable", ""),
        "tactic_layer": layers.get("tactic", ""),
        "p0": layers.get("p0", ""),
        "p1": layers.get("p1", ""),
        "tactics": tactics,
        "counts": {
            "hard_rules": len(hard),
            "soft_rules": len(soft),
            # 说清楚"注入了几条 / 池子里共几条", 否则用户定过的偏好没生效时
            # 完全看不出是被滤掉了还是根本没存进去。
            "soft_rules_pool": soft_all,
            "soft_filter_mode": soft_report.get("soft_filter_mode", "off"),
            "soft_rules_cap_per_scope": soft_cap,
            # 方向闸必须可见。被方向挡掉的规则和"根本没存进去"在写手眼里
            # 长得一模一样, 不回显就没法自查 —— 这套东西的全部意义就是让写手
            # 自己看得见、自己能改。
            "direction": direction or "未判定",
            "soft_dropped_by_direction": len(dropped_by_direction),
            "positive_examples": len(positives),
            "positive_pool": len(pool),
            "negative_examples": len(negatives),
            "has_shared_calibration": bool(shared_calib),
            "has_personal_calibration": bool(my_calib),
            "tactic_suffix_applied": bool(tactic_suffix),
        },
        "positive_selection_mode": pos_mode,
    }

    # ── 飞轮经验卡: 随简报一起借, 不再等模型自己想起来 ──────────────────
    # 2026-09-19 查生产库: 9 月 1-16 日写作台 commit 了 71 批稿子, 馆员缓存里
    # 同期只有 3 次借阅 —— 协议里 open_project / draw_angles 是"必做",
    # borrow_lessons 是"想要真实爆款参照时调", 于是通道 2 (TV → 写作台) 在
    # 每一场对话里都取决于模型愿不愿意多调一个可选工具, 而它 95% 的时候不愿意。
    # 馆员本身是通的(那 3 次各借到 4-5 张卡)。修法不是再劝一遍模型, 而是把
    # 借阅并进必做的那一步: 简报本来就是拿项目行 + 本次 delta 拼的, brief 也
    # 是同一份材料, 多一次 fail-open 的 HTTP 调用(≤ LIBRARIAN_TIMEOUT_SEC)。
    #
    # ⚠️ 这一段的失败**不能**影响简报: P0 已经拿到了, 卡借不到只是少点参考。
    # fetch_flywheel_lessons 本身绝不抛, 但 build_brief / 其它意外仍兜一层 ——
    # 兜住之后照样留痕(status.state=error + WARN 日志), 不吞成看似成功。
    borrowed = _borrow_for_brief_bounded(project, brief, user_id=user_id)
    out["lessons"] = borrowed["lessons"]
    if borrowed.get("validated_patterns"):
        out["validated_patterns"] = borrowed["validated_patterns"]
        out["validated_patterns_usage"] = _patterns_usage_for(project.get("id") or project_id, user_id,
                                                              len(borrowed["validated_patterns"]))
    out["flywheel_arm"] = borrowed.get("flywheel_arm")   # 入库时原样传给 commit_drafts (对照组圈人用)
    out["lessons_status"] = {k: borrowed[k] for k in
                             ("status", "count", "elapsed_ms", "detail")}
    out["counts"]["lessons"] = len(borrowed["lessons"])

    # ── 上一场停在哪: 发了牌还没写的角度 ──────────────────────────────────
    # 2026-09-20 查生产库(TV D-071): 7 天发出去 214 个角度, 只有 20 个销了账;
    # 194 个里 138 个所在的会话连一篇稿都没入库 —— 会话停在发牌之后, 而谁都不知道。
    # deskcore 的 /health 早就在报这个比例, 但那是运维视角、没人天天看; 模型每场都看得见
    # 的地方只有简报。所以把这个项目发了牌还没写的坐标摆在每场对话开头, 让模型优先拿它们写。
    # 2026-10-30 发布收尾: 原来那句是叫模型把这个数说给用户、问「接着写完还是放弃」—— 可那时数出来的大多是 W-38 之前
    # 写完发出去、只是对不回来的牌(见 ANGLE_DEBT_SINCE), 写手每场开头都被问一遍自己做不了主的事。现在只数下限之后的,
    # 话也改成说给模型听的: 用户这次要写的对得上就优先按它们写, 不转告、不问用户。一个团队共用一个身份(同一个 user_id),
    # 所以说「这个项目」, 不说「你抽了」。
    # ⚠️ 和借卡同一条纪律: 这一段的失败绝不能影响简报, 兜住但留痕。
    try:
        debt = store.angle_debt(client, project_id, user_id, ANGLE_DEBT_DAYS, since=ANGLE_DEBT_SINCE,
                                exclude_keys=_checked_angle_keys(client, project_id, user_id, ANGLE_DEBT_DAYS))
    except Exception:
        logger.exception("angle debt lookup failed (project=%s)", project_id)
        debt = {"unconsumed": 0, "since_days": ANGLE_DEBT_DAYS, "last_drawn_at": None}
    out["counts"]["angle_debt"] = debt["unconsumed"]
    if debt["unconsumed"]:
        out["angle_debt"] = debt
        shown = debt.get("angles") or []
        # 必须把坐标本身给出去: 新开一场对话时, 上一次 draw_angles 的返回早就不在
        # 上下文里了, 只报个数等于让模型"写完你不知道是哪几个的角度" —— 它只能忽略,
        # 或者再抽一批, 恰好是这条提醒要防的事(codex review on #86)。
        more = debt["unconsumed"] - len(shown)
        out["angle_debt_note"] = _angle_debt_note(debt["since_days"], debt["unconsumed"], debt["last_drawn_at"],
                                                  len(shown), more)

    # ── 最近发出去 / 写过的标题: 这一批要避开的东西(2026-10-20) ──────────────────────
    # 10-20 生产诊断: 稿子批批长得像、篇幅短, 飞书里大半被人重写。模型不知道这个项目上周发了什么、前天写了什么,
    # 每一场都从同一份简报出发, 自然写出同一种标题句式、同一个人设。只给标题(每条 ≤40 字、合计 ≤20 条), 不给正文。
    # ⚠️ 和借卡、角度债同一条纪律: 查不到就不带这一栏, 简报照给。
    try:
        recent = store.recent_titles(client, project_id, published_days=RECENT_TITLES_PUBLISHED_DAYS,
                                     drafted_days=RECENT_TITLES_DRAFTED_DAYS, limit=RECENT_TITLES_MAX)
    except Exception:
        logger.exception("recent titles lookup failed (project=%s)", project_id)
        recent = None
    if recent and (recent.get("published") or recent.get("drafted")):
        out["recent_titles"] = {"published": recent["published"], "drafted": recent["drafted"],
                                "task": RECENT_TITLES_TASK}

    # ── 最近的稿子在哪几处都一样(2026-10-20, 问哪几题、怎么算见 deskcore/structure.py) ─────────────────
    # recent_titles 管标题撞词, 管不到骨架: 10-20 查生产库, 有个项目近百篇稿子 86% 第一句是具体事件、82% 标题是问句、
    # 95% 写了已经发生的坏结果, 而它已发笔记里这三样分别只占 45% / 43% / 35%。Jev 给每篇打分时答过的特征题就在
    # TV 账本里, 读回来数一数, 扎堆的几处摆给模型, 让这一批有一半换个写法。只是提醒, 不拦任何东西。
    # ⚠️ 同一条纪律: 账本读不到(Exposed schemas 没有 truth_vault / 本地库)、项目没接 judge、篇数不够, 都是不带这一栏。
    try:
        shape = structure.recent_structure(store.recent_structure_answers(
            client, project_id, questions=structure.ASKED, bank_version=structure.BANK_VERSION,
            limit=structure.MAX_DRAFTS))
    except Exception:
        logger.exception("recent structure lookup failed (project=%s)", project_id)
        shape = None
    if shape:
        out["recent_structure"] = shape

    # ── 交付模板要写手带什么(2026-10-20, deskcore/delivery.py) ──────────────────────────────────────
    # 模板在项目行里(上面已经取了), 不多查。不放进简报的话, 写手第一次 score_drafts 才知道要几个话题、几条评论、
    # 要不要方向, 整批带着 problems 回来再存一次(judge 也再跑一遍)。模板坏了不带这一栏, 简报照给。
    try:
        tpl = delivery.template_of(project)
    except Exception as exc:                            # noqa: BLE001 — 增强项, 绝不冒泡
        logger.warning("open_project: 项目 %s 的交付模板用不了, 简报不带 delivery_requirements(%s)", project_id, exc)
        tpl = None
    if tpl is not None:
        out["delivery_requirements"] = delivery.requirements(tpl)

    # ── 规则编译(2026-10-20, D-121): 有没编的规则就在后台补编, 简报一秒都不等它(编一次最坏 6 分钟) ──────────
    # 要不要编由后台线程自己查(按 (规则 id, sha) 比 rule_compiles), 这里不多查一次库; 冷却之内重复打开不起新线程。
    _kick_rule_compile(client, project_id, reason="open_project")
    return out


# ══════════════════════════════════════════════════════════════════════
# 发牌
# ══════════════════════════════════════════════════════════════════════


# ── 一批之内的散布(2026-10-20) ─────────────────────────────────────────────────
# 原来是洗牌后的笛卡尔积直接取前 n 个没避重的 —— 跨批次不重复有台账管, 同一批之内什么都不管: 一批 5 篇里两篇
# 撞同一种内容形式、同一种标题句式是常事(各 8 个值, 5 篇的内容形式全不撞只有约 1/5, 形式和句式都不撞不到 5%)。
# 10-20 生产诊断: 稿子批批长得像, 而同一批里体裁和标题句式撞车就是"长得像"最直接的一层。
#
# 做法是贪心重排, 不换抽法: 按洗好的顺序往下找, 优先内容形式和标题句式都还没用过的, 其次有一个没用过的, 再次
# 随便; 同档里情绪杠杆没用过的优先。某一维 8 个值都用过一轮就清空重来(第 9、10 篇各开新一轮, 所以 n=10 时
# 同一种形式最多两篇)。仍然随机(先后来自洗牌), 同 seed 结果不变。
#
# ⚠️ RPC 路径上这只是【排序】: 被避重挡掉的那几个由 RPC 跳过, 后面补上的那个是照"前面那个也用了"排的,
# 所以近期用得多的项目偶尔会多撞一个。不为此多读一次台账 —— 选择必须留在 RPC 的事务里(并发发牌不撞车靠它)。
# 重排的长度和每一步往下看的候选数都封顶(codex review on aw#112): draw_angles 的 n 没有上限, 一次要几百上千个时
# 不封顶是 O(n × 候选数) 的纯 Python 扫描, 能把服务卡住。一批实际是 5~20 篇, 前 240 个、每步看 600 个足够错开。
_SPREAD_HEAD_MAX = 240
_SPREAD_SCAN = 600
_SPREAD_DIMS = (("content_format", len(vocab.CONTENT_FORMATS)),
                ("title_structure", len(vocab.TITLE_STRUCTURES)),
                ("emotional_lever", len(vocab.EMOTIONAL_LEVERS)))


def _spread_order(candidates: list[dict], n: int) -> list[dict]:
    """把前 ``3n + 24`` 个按"一批之内尽量不撞"重排, 后面的原序接上。只排前面一段: RPC 只会取到前 n 个加上
    被避重跳过的那几个, 全量贪心是 O(候选²), 白花。"""
    rest = list(candidates)
    head = min(len(rest), 3 * max(n, 0) + 24, _SPREAD_HEAD_MAX)
    used = {dim: set() for dim, _ in _SPREAD_DIMS}
    out: list[dict] = []
    while rest and len(out) < head:
        best_i, best = 0, -1
        for i, c in enumerate(rest[:_SPREAD_SCAN]):
            d = c["dims"]
            score = (2 * (d.get("content_format") not in used["content_format"])
                     + 2 * (d.get("title_structure") not in used["title_structure"])
                     + (d.get("emotional_lever") not in used["emotional_lever"]))
            if score > best:
                best_i, best = i, score
                if score == 5:
                    break
        c = rest.pop(best_i)
        out.append(c)
        for dim, size in _SPREAD_DIMS:
            used[dim].add(c["dims"].get(dim))
            if len(used[dim]) >= size:
                used[dim].clear()
    return out + rest


def draw_angles(client, project_id: str, n: int, *, avoid_days: int = 30,
                user_id: str | None = None, seed: int | None = None,
                perpetual_bias: bool = False) -> dict:
    """发 n 张互不重复、且避开台账的创作坐标, 写入台账。

    这是 generator._assign_slot_coordinates(:1301) 的复活版。当年移除
    (generator.py:1576-1584)的理由是通用角度池跟项目自己的 role 设定打架 ——
    LLM 会锁定更具体的平台标签、把项目的 role 降级成"风格提示"。修法照注释里
    留的那条路: **切入角度优先用项目自己的 custom_roles**, 没配才用通用池。
    """
    # 归属校验放在 n<=0 的早返回【之前】: 每个项目级入口都以这一行开头, 才能在
    # CI 里用一条断言把"有没有漏掉某个工具"验死。代价是 n=0 时多一次查询,
    # 而 n=0 本来就是退化调用。(审计 COR-015)
    project = assert_project_access(client, project_id, user_id=user_id)
    if n <= 0:
        return {"angles": [], "requested": 0, "delivered": 0}

    custom = project.get("custom_roles") or []
    if isinstance(custom, str):
        import json
        try:
            custom = json.loads(custom)
        except (ValueError, TypeError):
            custom = []
    angles_pool = ([{"id": r.get("id", ""), "name": r.get("name", ""),
                     "brief": (r.get("prompt_suffix") or "").strip()}
                    for r in custom] if custom else vocab.default_angles())
    pool_source = "project.custom_roles" if custom else "generator.CREATIVE_ROLES_POOL"

    rng = random.Random(seed) if seed is not None else random.Random()

    space = [(lev, arc, fmt, st)
             for lev in vocab.EMOTIONAL_LEVERS
             for arc in vocab.HUMAN_TRUTH_ARCHETYPES
             for fmt in vocab.CONTENT_FORMATS
             for st in vocab.TITLE_STRUCTURES]
    rng.shuffle(space)

    # 过量供给候选给 RPC —— 它在事务里逐个查避重集、取前 n 个可用的。
    # 20 倍冗余(下限 500)足以覆盖"近期用掉很多"的项目。
    cand_cap = min(len(space), max(n * 20, 500))
    candidates = []
    for lev, arc, fmt, st in space[:cand_cap]:
        dims = {"emotional_lever": lev, "human_truth_archetype": arc,
                "content_format": fmt, "title_structure": st}
        candidates.append({"angle_key": angle_key(dims), "dims": dims})
    # 一批之内错开体裁和标题句式(10-20): RPC 按候选的先后取前 n 个可用的, 所以把洗好的候选重排成"前面几个尽量
    # 互不同形", 选择本身还是 RPC 在事务里做。见 _spread_order。
    candidates = _spread_order(candidates, n)

    reserved = store.reserve_angles(client, project_id, candidates, n,
                                    user_id, avoid_days)
    atomic = reserved is not None
    if not atomic:
        # RPC 不在(迁移没跑)。降级到旧的"读-挑-插"三步, 并明确告知不是原子的。
        logger.warning("draw_angles: 本次发牌不是原子的(deskcore_reserve_angles RPC 不存在, migrations/001 "
                       "可能没跑, project=%s)", project_id)
        avoid = store.recent_angle_keys(client, project_id, avoid_days)
        # 先去掉避重的再重排一次: 这条路上知道哪些被挡, 错开得比 RPC 那条路还准
        reserved = _spread_order([c for c in candidates if c["angle_key"] not in avoid], n)[:n]
        store.record_draw(client, project_id, reserved, user_id)

    tilt = rng.choice(vocab.WORD_TILTS)  # 词感是"今天的心情", 全批一致

    # ⚠️ 角度池要洗牌, 不能每次都从 0 号开始取。
    # 原来是 angles_pool[len(picked) % len(angles_pool)] —— 每次发牌都从头数,
    # 于是【每批要的篇数少于角度数时, 后面的角度永远轮不到】: 6 个角度、每次
    # 只发 3 篇, 那就永远只用前 3 个, 另外 3 个一次都不会出现。其他维度都随机
    # 了, 唯独这一维退化成常量, 而这正是"跨批次别老写同样切入"要治的东西。
    # 用本次请求的 rng 洗牌: seed 相同则结果可复现(draw 支持 seed 参数)。
    # (codex review)
    angle_order = list(angles_pool)
    rng.shuffle(angle_order)

    picked: list[dict] = []
    for r in reserved:
        dims = r["dims"]
        lev = dims.get("emotional_lever", "")
        angle = angle_order[len(picked) % len(angle_order)]
        trends = ([vocab.TREND_EXCLUSIVE] if perpetual_bias
                  else vocab.normalize_trends([rng.choice(vocab.TREND_DEPENDENCIES)]))
        picked.append({
            "slot": len(picked) + 1,
            "angle_key": r["angle_key"],
            "dims": dims,
            "emotional_valence": vocab.valence_of(lev),
            "emotional_intensity": rng.choice(vocab.EMOTIONAL_INTENSITIES),
            "trend_dependencies": trends,
            "word_tilt": tilt,
            "angle_name": angle.get("name") or angle.get("id") or "",
            "angle_brief": angle.get("brief") or "",
            "boundary_rule": vocab.boundary_rules_for(lev),
        })

    if len(picked) < n:
        logger.warning("angle space exhausted (project=%s): asked %d got %d",
                       project_id, n, len(picked))

    return {
        "angles": picked,
        "requested": n,
        "delivered": len(picked),
        "prompt_block": render_angles_block(picked),
        "angle_pool_source": pool_source,
        "atomic_reservation": atomic,
        "combination_space": vocab.combination_space(),
        "note": ("发到的组合少于请求数, 说明近期用掉太多; 可以调小 avoid_days 或分批写。"
                 if len(picked) < n else ""),
        # 念给写手的只说后果; RPC / 迁移号在上面 atomic 判定那里记日志
        "warning": ("" if atomic else
                    "这次发牌没能锁住角度: 队友同时发牌时可能抽到同一组。已记下, 不用你做什么。"),
    }


def render_angles_block(angles: list[dict]) -> str:
    """渲染成可直接贴进 prompt 的硬约束块。

    照 generator._build_slot_coordinates_block(:1350) 的形状, 但多带 essence
    维度和边界判据 —— 光给标签名模型会混(docs/05 §3 花 40 行讲焦虑 vs 恐惧
    怎么分是有原因的)。
    """
    if not angles:
        return ""
    lines = ["【本批次每篇的创作坐标（必须按编号对应，不得互换）】"]
    for a in angles:
        d = a["dims"]
        lines.append(
            f"第{a['slot']}篇：情绪杠杆={d['emotional_lever']}"
            f"({a['emotional_valence']}/{a['emotional_intensity']})"
            f" · 人性原型={d['human_truth_archetype']}"
            f" · 内容形式={d['content_format']}"
            f" · 标题句式={d['title_structure']}"
            f" · 切入角度={a['angle_name']}")
        # ⚠️ 时效依赖必须渲进来。它是发牌时抽的(perpetual_bias=True 时强制抽
        # 排他值「通用」), 但这个块才是文档让人贴进 prompt 的东西 —— 不渲染
        # 的话模型根本收不到, perpetual_bias 这个开关等于没接线, 除非调用方
        # 自己去翻原始 angles。(codex review)
        trends = a.get("trend_dependencies") or []
        if trends:
            note = ("（排他值：全篇不得出现任何时事、节日、平台事件、流行词等"
                    "时效元素）" if vocab.TREND_EXCLUSIVE in trends else "")
            lines.append(f"    ↳ 时效依赖：{'、'.join(trends)}{note}")
        if a.get("boundary_rule"):
            lines.append(f"    ↳ 判据：{a['boundary_rule']}")
        if a.get("angle_brief"):
            lines.append(f"    ↳ 角度：{a['angle_brief']}")
    lines.append(f"\n全批词感倾向：{angles[0].get('word_tilt', '')}")
    lines.append("以上坐标为硬约束。写完后每一篇要能说出自己用的是哪一组，说不出就是没按坐标写。")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# 查重硬闸
# ══════════════════════════════════════════════════════════════════════


class HistHit(NamedTuple):
    """一篇草稿与历史比对的**逐信号**最佳命中。

    每一路各记各的 —— 各路的最佳命中可能来自不同的历史稿, 混在一起报会让人
    对着一条根本没引发拒绝的稿子去改(codex review 记过这一条)。
    ``*_title`` 为 None 表示这一路没有命中(不是"命中了一条空标题")。
    """
    sim: float
    sim_title: str | None
    jac: float
    j_title: str | None
    open_exact: bool
    open_title: str | None
    contain: float                 # 审计 COR-014
    contain_title: str | None
    contain_sample: int            # 有效样本量, 低于阈值时这一路不发言


def _history_probe(client, project_id: str, o_hashes: list[str],
                   grams: list[set], new_vecs, *, own: dict[int, list[dict]] | None = None):
    """返回 ``(probe, hist_size, hist_with_vec, hist_untitled, hist_truncated)``。

    ``probe(i)`` → ``HistHit`` —— 第 i 篇草稿与【历史】比对的四路最佳命中。
    ``hist_untitled`` 是空标题、也没有向量的历史条数: 它们永远算不出标题向量, 不算"缺"
    (见 store.fingerprint_stats 的说明), 由 _gate 从缺向量的数里减掉。

    两条实现共用这一个形状:

      A. 下推路径(审计 SUP-002 / ROB-004 / ROB-011, 需要 migrations/004)
         整个比对在库里做完, 只回每篇一行结果。原来是把 4000 行 × 768 维拉进
         Python 逐对算 —— 百 MB 传输 + 三千万次乘加 + 占满线程池。

      B. Python 路径(migrations/004 没跑时)
         与下推前完全一致, 包括 4000 条的上限和 history_truncated 的警告。

    ⚠️ 只有"RPC 不存在"才降级到 B。查重是 deskcore 唯一不 fail-open 的路径,
    其它异常(权限 / 库故障)一律冒泡。

    ``own``: 改稿那几篇不比的行(它替换的旧 item 的指纹, 见 _gate)。A 路径库里排除不了, 交给 _without_own_versions
    在 Python 里补; B 路径本来就把历史拉回来了, 直接跳过那几行。
    """
    # ⚠️ ``embed_texts`` 是**逐位对齐**的, 但某一位可能是 None —— 那条稿子没有
    # 可嵌入的标题(空标题)。所以判据必须是"这一位有没有向量", 不能只判
    # "这一批有没有向量、下标越没越界"。
    #
    # 2026-08-26 这里真崩过一次: 判据写成 `new_vecs and i < len(new_vecs)`,
    # 于是空标题那一位的 None 掉进 `for x in None`, 整个 check_drafts 回 500。
    # 起因是同一天把 embed_texts 从"整批全有或全无"改成了逐位可空(那个改动本身
    # 是对的 —— 一条空标题不该拖垮整批), 而**这个调用点没跟上新契约**。
    # 当时的回归夹具是 `lambda ts: [[0.1]] * len(ts)`, 永远不产生 None, 所以
    # 测试套一片绿 —— 假件比真实情况"整齐", 测出来的绿就是假的。
    def _vec_literal(i: int) -> Optional[str]:
        v = new_vecs[i] if new_vecs and i < len(new_vecs) else None
        return f"[{','.join(repr(float(x)) for x in v)}]" if v else None

    payload = [
        {
            "opening_hash": o_hashes[i] or None,
            "ngram_hashes": sorted(grams[i]),
            # 这一条算不出向量时传 null, 库里那一路直接跳过。
            "title_embedding": _vec_literal(i),
            # ⚠️ 必须带上模型名: 库里那一路要用它把**别的模型产的**历史向量
            # 排除掉。跨模型算余弦出来的数是垃圾且【不报错】—— 不带的话
            # migrations/008 之后的函数会退回"比全部非空向量"的老行为, 而
            # 老行为正是 #65 P1 说的那个洞。(codex review · #65 P1)
            #
            # 没有向量的那条不写模型名 —— 写了也没意义, 而且会让"这条比过没有"
            # 从回执里看不出来。
            "embedding_model": (dedup.EMBEDDING_MODEL
                                if _vec_literal(i) else None),
        }
        for i in range(len(o_hashes))
    ]
    # 阈值只在 fingerprint.py 里定义一处 —— SQL 里的 DEFAULT 只是兜底。
    rows = store.check_drafts_sql(client, project_id, payload,   # 故意让异常冒泡
                                  contain_min_sample=fp.CONTAIN_MIN_SAMPLE)
    if rows is not None:
        by_idx = {int(r.get("idx", -1)): r for r in rows}
        # ⚠️ 回执必须每篇一行。少一行就意味着那一篇【根本没比过】, 而下面
        # 取不到时会退化成全 0 信号 → 直接判 pass。查重是硬闸, 这种"静默放行"
        # 正是它最不能有的失败模式, 宁可整个调用报错。
        missing = [i for i in range(len(payload)) if i not in by_idx]
        if missing:
            raise RuntimeError(
                f"deskcore_check_drafts 回执缺 {len(missing)}/{len(payload)} 篇"
                f"(idx={missing[:5]}) —— 拒绝按'没撞车'放行")
        # 口径是"有【本模型】向量的条数" —— 见 store.fingerprint_stats 的说明。
        total, with_vec, untitled = store.fingerprint_stats(client, project_id,
                                                            dedup.EMBEDDING_MODEL)
        # 老版 RPC(只跑过 migrations/004)回不出包含度三列。写手那边只听到「这一路没查成」(见 _gate),
        # 到底是哪个迁移没跑只在这里说一次。
        if rows and any("c_sample" not in r for r in rows):
            logger.warning("deskcore_check_drafts 回执没有包含度三列(project=%s): 库里只跑了 migrations/004、"
                           "没跑 005, 短稿照搬长稿这一路在 check 这一关不发言 —— 跑 migrations/005 修好",
                           project_id)

        def _probe(i: int) -> HistHit:
            r = by_idx.get(i) or {}
            # NUMERIC 在 PostgREST 上可能回数字也可能回字符串, float() 两种都吃。
            # open_exact 不看它自己的布尔值而是看 open_title 有没有 ——
            # 万一布尔被序列化成字符串, `bool("false")` 是 True, 而
            # opening_exact 是**单独就判死**的最强信号, 没有任何东西兜得住
            # 这个误伤。RPC 里那一列本来就是 `open_title IS NOT NULL` 算出来的,
            # 这么取口径完全一致, 只是不依赖布尔的传输形态。
            #
            # ⚠️ best_c / c_title / c_sample 是 migrations/005 加的。老版本 RPC
            # (只跑过 004)回不出这三列 —— 取不到就当**这一路没跑**(sample=0,
            # 于是 deciding_signals 里 contain_ok 为 False, 包含度不发言),
            # 而不是当成"包含度为 0 = 没撞车"。两者的差别在下面的
            # contain_pushdown_missing 里报给调用方。(审计 COR-014)
            return HistHit(
                float(r.get("best_sim") or 0.0),
                r.get("sim_title"),
                float(r.get("best_j") or 0.0),
                r.get("j_title"),
                r.get("open_title") is not None,
                r.get("open_title"),
                float(r.get("best_c") or 0.0),
                r.get("c_title"),
                int(r.get("c_sample") or 0),
            )

        # 下推路径比的是【全量】—— 库里没有 4000 条那个上限, 所以永不截断。
        if own:
            return (_without_own_versions(client, project_id, _probe, own, o_hashes, grams, new_vecs),
                    total, with_vec, untitled, False)
        return _probe, total, with_vec, untitled, False

    logger.warning(
        "deskcore_check_drafts RPC 不存在(migrations/004 没跑) —— 本次退回 "
        "Python 逐对比对: 大项目会慢数十秒并占满线程池, 见审计 SUP-002/ROB-004。")
    _rpc_missing_telemetry("deskcore_check_drafts", project_id)

    history, truncated = store.fingerprints(client, project_id)
    hist_grams = [set(h.get("ngram_hashes") or []) for h in history]
    hist_open = {h.get("opening_hash"): h for h in history if h.get("opening_hash")}
    # ⚠️ 只把**本模型**产的向量算进比对。跨模型算余弦出来的数是垃圾, 而且
    # 【不报错】—— 混着比的结果是硬闸看起来跑了、结论却是噪声。
    # ``embedding_model`` 为 NULL 的行同样排除: 那是"来路不明", 不是"本模型"。
    # 排除掉的行仍然计入 total, 于是 with_vec < total → semantic_degraded 为
    # true 并把原因报出去。**宁可说自己没比全, 也不能拿垃圾数当比过了。**
    # (codex review · #65 P1; 与库里那一路 migrations/008 的过滤同一口径)
    hist_vecs = [h.get("title_embedding")
                 if h.get("embedding_model") == dedup.EMBEDDING_MODEL else None
                 for h in history]
    with_vec = sum(1 for v in hist_vecs if v)
    # 与 store.fingerprint_stats 同一口径: 空标题且没向量的行不算缺。
    untitled = sum(1 for h in history
                   if not (h.get("title") or "") and not h.get("title_embedding"))

    def _probe_py(i: int) -> HistHit:
        best_sim, sim_hit = 0.0, None
        if new_vecs and i < len(new_vecs):
            for hi, emb in enumerate(hist_vecs):
                if not emb:
                    continue
                s = dedup.cosine_similarity(new_vecs[i], emb)
                if s > best_sim:
                    best_sim, sim_hit = s, history[hi]
        best_j, j_hit = 0.0, None
        best_c, c_hit, c_sample = 0.0, None, 0
        for hi, hg in enumerate(hist_grams):
            # ⚠️ 两边都是 bottom-k **sketch**, 不是完整 gram 集 —— 必须走
            # sketch_overlap, 直接 fp.jaccard 会系统性偏低(审计 COR-014)。
            j, c, s = fp.sketch_overlap(grams[i], hg)
            if j > best_j:
                best_j, j_hit = j, history[hi]
            # 包含度**单独记它自己的最佳命中**: 抄袭源和"用词最像的那篇"经常
            # 不是同一条, 归错了人会对着无关的稿子改。
            #
            # ⚠️ 样本量的判据必须在**取最大之前**。原来是先按 c 取冠军、事后
            # 才看冠军的样本量够不够 —— 一条毫不相关、只跟本稿撞上一个低位
            # hash 的历史稿能拿到 c=1.0 / 样本量=1, 压过真正的抄袭源
            # (c=0.9 / 样本量>=15); 冠军随后因样本量不足被丢掉, 而真命中
            # 根本没进过决赛, 于是照搬长稿的稿子**两道闸都放行**。
            # SQL 侧(migrations/005 的 cbest 与 commit 的 LOOP)同一口径。
            if s >= fp.CONTAIN_MIN_SAMPLE and c > best_c:
                best_c, c_hit, c_sample = c, history[hi], s
        open_hit = hist_open.get(o_hashes[i]) if o_hashes[i] else None
        return HistHit(
            best_sim, (sim_hit or {}).get("title", "") if sim_hit else None,
            best_j, (j_hit or {}).get("title", "") if j_hit else None,
            open_hit is not None,
            (open_hit or {}).get("title", "") if open_hit else None,
            best_c, (c_hit or {}).get("title", "") if c_hit else None,
            c_sample,
        )

    if own:
        # 改稿那几篇: 历史里去掉它旧 item 的那几行再比(按指纹行的 id); 别的稿子照旧走 _probe_py
        rest = _sketch_rows(history)
        skip = {i: {str(r.get("id")) for r in rows} for i, rows in own.items()}

        def _probe_py_own(i: int) -> HistHit:
            if i not in skip:
                return _probe_py(i)
            return _best_hits([x for x in rest if str(x[0].get("id")) not in skip[i]],
                              o_hashes[i], grams[i], _vec_at(new_vecs, i))
        return _probe_py_own, len(history), with_vec, untitled, truncated
    return _probe_py, len(history), with_vec, untitled, truncated


def _vec_at(new_vecs, i: int):
    """第 i 篇的标题向量; 这一批没算向量、或者这一位是 None(空标题)都回 None。"""
    return new_vecs[i] if new_vecs and i < len(new_vecs) and new_vecs[i] else None


def _sketch_rows(rows: list[dict]) -> list[tuple[dict, set, list | None]]:
    """历史行 → ``(行, 四字串 sketch 的集合, 本模型的标题向量或 None)``, 给 _best_hits 逐行比。别的模型产的向量、
    来路不明的(embedding_model 为空)一律当没有, 同 _probe_py 的口径。"""
    return [(r, set(r.get("ngram_hashes") or []),
             r.get("title_embedding") if r.get("embedding_model") == dedup.EMBEDDING_MODEL else None)
            for r in rows]


def _best_hits(rows: list[tuple[dict, set, list | None]], o_hash: str, grams_i: set, vec) -> HistHit:
    """一篇草稿和给定的这几行逐路比, 四路各取最佳命中。比法与 _probe_py 一字不差: sketch 走 sketch_overlap, 包含度的样本量
    先过闸再比大小, 开头哈希为空不算撞车。只给改稿排除旧版那条路用(_probe_py 照旧是 Python 路径的主体)。"""
    best_sim, sim_hit = 0.0, None
    best_j, j_hit = 0.0, None
    best_c, c_hit, c_sample = 0.0, None, 0
    open_hit = None
    for r, hg, emb in rows:
        if vec and emb:
            s = dedup.cosine_similarity(vec, emb)
            if s > best_sim:
                best_sim, sim_hit = s, r
        if hg:
            j, c, s = fp.sketch_overlap(grams_i, hg)
            if j > best_j:
                best_j, j_hit = j, r
            if s >= fp.CONTAIN_MIN_SAMPLE and c > best_c:
                best_c, c_hit, c_sample = c, r, s
        if o_hash and open_hit is None and r.get("opening_hash") == o_hash:
            open_hit = r

    def _t(r):
        return (r.get("title") or "") if r is not None else None
    return HistHit(best_sim, _t(sim_hit), best_j, _t(j_hit), open_hit is not None, _t(open_hit),
                   best_c, _t(c_hit), c_sample)


# ── 改稿不和自己的旧版比(2026-10-30 发布收尾) ─────────────────────────────────────
# check_drafts 带 replaces_version_id 的那篇, 旧 item 全部版本的指纹行不算它的对手。score_drafts 是先把那几行摘下来再查;
# check_drafts 是只读的, 不能摘, 而库里那一路(deskcore_check_drafts)每一路只回最像的那一条的标题、也没法点名排除哪几行
# —— 不加迁移的做法是在 Python 里补:
#   1. 拿旧版那几行和这篇逐路比一次(几行而已)。库里回的最佳命中比得过它们(更像), 或者标题和它们都对不上, 那就是别的
#      稿子, 照用 —— 这时"其余历史里最像的"就是它;
#   2. 比不过的(命中的就是旧版, 或者分不清): 这一路在去掉旧版之后的历史上重比。开头精确撞车按开头哈希点查(取旧版行数 + 1
#      行就够分清有没有别的稿子); 标题语义 / 四字串把项目指纹拉回来、去掉旧版那几行再比, 只拉要比的列, 一次查重只拉一遍。
# 拿不准时一律当成"命中的是旧版"去重比: 重比算的就是去掉旧版之后的真值, 多比一次只是慢, 不会错。改稿时标题、正文多半和
# 旧版很像, 所以第 2 步在改稿时几乎总要走 —— 那是这条路的代价(同 Python 路径, 只给改稿的那几篇), 新稿一行都不多拉。
SIM_TIE_EPS = 1e-3          # 库里(pgvector)和这里(double)算同一对余弦能差出来的量; 差在这以内的当打平
SKETCH_TIE_EPS = 1e-9       # sketch 的两个比值两边都是整数相除, 只差浮点误差


def _without_own_versions(client, project_id: str, probe, own: dict[int, list[dict]],
                          o_hashes: list[str], grams: list[set], new_vecs):
    """库里那一路的 ``probe`` → 改稿那几篇不算旧版之后的 ``probe``(见上面那段说明)。出错照样上抛: 查重是硬闸。"""
    fixed: dict[int, HistHit] = {}
    redo: dict[int, set[str]] = {}
    own_ids: dict[int, set[str]] = {}
    for i, rows in own.items():
        h = fixed[i] = probe(i)
        own_ids[i] = {str(r.get("id")) for r in rows}
        if not rows:
            continue                                    # 旧版一行指纹都没有: 库里回的命中不可能是它
        mine = _best_hits(_sketch_rows(rows), o_hashes[i], grams[i], _vec_at(new_vecs, i))
        names = {r.get("title") or "" for r in rows}
        sig = set()
        if h.open_exact and mine.open_exact:
            sig.add("opening")
        if h.sim_title is not None and h.sim_title in names and h.sim <= mine.sim + SIM_TIE_EPS:
            sig.add("title")
        if h.j_title is not None and h.j_title in names and h.jac <= mine.jac + SKETCH_TIE_EPS:
            sig.add("ngram")
        if h.contain_title is not None and h.contain_title in names and h.contain <= mine.contain + SKETCH_TIE_EPS:
            sig.add("contain")
        # 库里回的值本来就 ≤ 0 的那一路: 去掉旧版之后也只会是 0, 不用重比, 把指着旧版的那个标题摘掉就行(不然通过的稿子
        # 会把它自己的旧版报成「最接近的参照」)
        zero = {"title": h.sim <= 0, "ngram": h.jac <= 0, "contain": h.contain <= 0}
        patch = {**({"sim": 0.0, "sim_title": None} if "title" in sig and zero["title"] else {}),
                 **({"jac": 0.0, "j_title": None} if "ngram" in sig and zero["ngram"] else {}),
                 **({"contain": 0.0, "contain_title": None, "contain_sample": 0}
                    if "contain" in sig and zero["contain"] else {})}
        if patch:
            fixed[i] = fixed[i]._replace(**patch)
        sig = {s for s in sig if not zero.get(s)}
        if sig:
            redo[i] = sig
    for i, sig in redo.items():
        if "opening" in sig:
            hits = store.fingerprints_with_opening(client, project_id, o_hashes[i], limit=len(own_ids[i]) + 1)
            other = next((r for r in hits if str(r.get("id")) not in own_ids[i]), None)
            fixed[i] = fixed[i]._replace(open_exact=other is not None,
                                         open_title=(other.get("title") or "") if other is not None else None)
    heavy = {s for sig in redo.values() for s in sig} - {"opening"}
    if heavy:
        history, truncated = store.fingerprints(client, project_id, vectors="title" in heavy)
        if truncated:
            logger.warning("check_drafts: 改稿排除旧版时项目 %s 的历史指纹超过 %d 条, 重比只覆盖了最近的这些",
                           project_id, len(history))
        prepped = _sketch_rows(history)
        for i, sig in redo.items():
            if not sig & heavy:
                continue
            m = _best_hits([x for x in prepped if str(x[0].get("id")) not in own_ids[i]],
                           "", grams[i], _vec_at(new_vecs, i) if "title" in sig else None)
            patch: dict = {}
            if "title" in sig:
                patch.update(sim=m.sim, sim_title=m.sim_title)
            if "ngram" in sig:
                patch.update(jac=m.jac, j_title=m.j_title)
            if "contain" in sig:
                patch.update(contain=m.contain, contain_title=m.contain_title, contain_sample=m.contain_sample)
            fixed[i] = fixed[i]._replace(**patch)
        logger.info("check_drafts: 改稿 %d 篇排除了旧版重比(project=%s, 信号=%s, 历史 %d 行)",
                    len(redo), project_id, sorted(heavy), len(history))
    return lambda i: fixed[i] if i in fixed else probe(i)


def _rpc_missing_telemetry(name: str, project_id: str) -> None:
    """把"迁移没跑"埋一行点。静默降级最怕的就是没人知道它降级了。"""
    try:
        import telemetry
        telemetry.log_event("deskcore_rpc_missing", rpc=name,
                            project_id=project_id[:8],
                            hint="跑 migrations/004_deskcore_check_pushdown.sql")
    except Exception:
        pass


def check_drafts(client, project_id: str, drafts: list[dict],
                 *, user_id: str | None = None, record_snapshot: bool = False) -> dict:
    """比对全量历史 + 本批内互比。

    ⚠️ deskcore 唯一【不 fail-open】的路径。其它读类工具出错返回可用结构不阻塞
    写稿, 但查重挂了必须抛 —— 静默放行就是重演 config.py 那个
    ENABLE_DEDUP_REGEN 默认 "0"、查重跑了但不拦的老问题。

    ⚠️ 这是审计 COR-015 里【读侧最要命的那个】: 返回值的 ``collided_with`` 会回显
    撞车对象的**标题**。没有归属校验时, 拿一篇随便什么稿子去撞别人的项目, 就是
    一个可以反复调用的历史标题读取接口。所以 user_id 现在是必需的。

    ── 2026-10-30: 改稿带 ``replaces_version_id`` ──────────────────────────
    改的是已经存过的那篇: 每篇可以带 ``replaces_version_id``(同 score_drafts 的字段名、同一套核对, _lookup_replacements)。
    核对通过的, 旧 item 全部版本的指纹行不算它的对手(_without_own_versions), 别的稿子照常比; 这一行带回
    ``replaces_version_id``。核对不过的(不是 UUID / 不是本项目的版本 / 和 version_id 同时带 / 同一篇替换两次)判 reject,
    ``gate="replace_lookup"``, 和 score_drafts 那边拒的是同一种。**仍然只读**: 不摘指纹, 摘指纹是 score_drafts 存档时的事。
    """
    assert_project_access(client, project_id, user_id=user_id)
    if not drafts:
        return {"results": [], "summary": {"total": 0, "pass": 0, "warn": 0, "reject": 0}}

    titles = [(d.get("title") or "").strip() for d in drafts]
    bodies = [d.get("body") or "" for d in drafts]
    new_vecs = dedup.embed_texts(titles) if dedup.embeddings_available() else None
    # 没有改稿的批次一次多余的查询都不加(下推路径的 CI 守着"不拉指纹行")
    replace, bad_ref = ({}, {})
    if any(isinstance(d, dict) and d.get("replaces_version_id") for d in drafts):
        replace, bad_ref = _lookup_replacements(client, project_id, drafts)
    out = _gate(client, project_id, titles, bodies, new_vecs,
                own=_own_fingerprints(client, project_id, replace))
    if replace or bad_ref:
        _mark_replacements(out, replace, bad_ref)
    if record_snapshot:
        # 只有写手那边的 AI 走工具查重才记(tools.check_drafts); 运维的 `cli check` 试跑不记 —— 不然试跑的稿子挂在写手名下,
        # 发出去以后会被当成写作台写的转正(W-38 复审)。
        # 改稿那篇照样记一行, 但记不下它替换的是哪一版: draft_snapshots 没有这样的列(version_id 是转正后的那一版), 这次
        # 不加列。存档时(score_drafts 带 replaces_version_id)同样文字的底稿记成转正到新的那一版(_supersede_snapshots);
        # 没存档就发出去的, tv-sync 先拿主索引对旧版, 对不上才轮到这份底稿。
        _record_snapshots(client, project_id, user_id, drafts, out)
    return out


def _mark_replacements(out: dict, replace: dict, bad_ref: dict) -> None:
    """check_drafts 的回执里标出改稿那几篇(2026-10-30)。核对通过的带回 ``replaces_version_id``; 核对不过的改判 reject,
    ``gate="replace_lookup"`` —— 和 score_drafts 那边拒的是同一种, 稿子本身不用重写, 改对 id 再查。summary 的三个数跟着重算。"""
    for r in out.get("results") or []:
        i = r.get("index")
        if i in replace:
            r["replaces_version_id"] = replace[i]["old_version_id"]
        elif i in bad_ref:
            r.pop("collided_note", None)
            r.update(status="reject", collided_with="", collided_scope="", decided_by=[], gate="replace_lookup",
                     reason=(f"{bad_ref[i]}。这篇没按改稿查: 改的是已经存过的那篇, 就把 replaces_version_id 改成 "
                             "score_drafts 返回的 version_ids 里对应的那个再查一次, 稿子不用重写; 不是改稿就去掉这个字段"))
    s = out.get("summary") or {}
    for k in ("pass", "warn", "reject"):
        s[k] = sum(1 for r in out.get("results") or [] if r.get("status") == k)


# ── 查重时记下底稿(W-38, 2026-10-29) ────────────────────────────────────────────
# 写手没有「存档」这一步(D-120): 稿子写完贴进飞书, 写作台自己按内容把飞书的帖对回来。可写作台的稿只在模型调了
# score_drafts 时才进库; 10-28 途鸽那轮 10 张牌, 查重查了、score_drafts 没调, 发出去的帖就只能被补录成副本(牌不销、
# 没有改稿差异)。check_drafts 是每篇必过的硬闸, 在这里把稿子记下来(draft_snapshots, 不进指纹库 —— 不挡后面的稿子),
# tv-sync 对不上写作台版本的帖再拿它对一次(_promote_snapshots)。
# ⚠️ 记底稿绝不能影响查重的判定: 任何错都只记 WARN(018 没跑也是)。
SNAPSHOT_MAX_AGE_DAYS = 45          # tv-sync 只拿近 45 天的底稿对(发布晚于查重最多 tvlink.WINDOW_AFTER_DAYS 天, 留余量)
SNAPSHOT_ANGLE_KEY_MAX = 64         # 模型带来的 angle_key 原样存, 只截断(销牌时再按台账核)


def _draft_text_fp(title: str, body: str) -> str:
    """稿子的文字指纹, 和 TV 那边 tv_text_fp 同一把尺子(tvlink.text_fp: 标题 + 剥了话题的正文, 去标点空白)。"""
    return tvlink.text_fp(tvlink.Note(note_id="", title=(title or "").strip(),
                                      body=tvlink.strip_hashtags((body or "").strip()), publish_time=None))


def _record_snapshots(client, project_id: str, user_id: str | None, drafts: list[dict], gate: dict) -> int:
    """这次查重的每篇(有正文的)记一行底稿, 一次 insert。回记了几行; 失败回 0、只记 WARN。"""
    try:
        status = {r.get("index"): r.get("status") for r in (gate.get("results") or [])}
        try:
            arm, arm_source, _ = batch_arm(project_id, user_id)
        except Exception:                               # noqa: BLE001 — 臂只是附带记一下
            arm, arm_source = None, None
        check_id = str(uuid.uuid4())
        rows = []
        for i, d in enumerate(drafts):
            title = (d.get("title") or "").strip()
            body = (d.get("body") or "").strip()
            if not tvlink.strip_hashtags(body):
                continue                                # 没正文的对不上任何帖(W-30 / W-36), 不记
            key = d.get("angle_key")
            key = key.strip()[:SNAPSHOT_ANGLE_KEY_MAX] if isinstance(key, str) and key.strip() else None
            rows.append({"project_id": project_id, "user_id": user_id, "check_id": check_id,
                         "title": title, "body": body, "angle_key": key,
                         "text_fp": _draft_text_fp(title, body),
                         "status": status.get(i), "flywheel_arm": arm, "flywheel_arm_source": arm_source})
        return store.insert_draft_snapshots(client, rows)
    except Exception:                                   # noqa: BLE001 — 查重的判定已经算好了, 记不上只少一份底稿
        logger.warning("check_drafts: 记底稿失败 (project=%s), 判定照常返回; 这一批发出去以后认不回写作台, 除非存了档",
                       project_id, exc_info=True)
        return 0


def _gate(client, project_id: str, titles: list[str], bodies: list[str],
          new_vecs, *, own: dict[int, list[dict]] | None = None) -> dict:
    """check_drafts 的判定主体: 与历史比对 + 本批内互比 + 逐条 verdict。

    从 check_drafts 里抽出来是为了让 **commit_drafts 也跑同一套**(2026-09-17)。
    在此之前入库只靠 RPC 的两路确定性信号(开头精确 / 四字串), 标题语义
    (TITLE_SIM_HARD)和本批内互比只在 check_drafts 里 —— 于是一个跳过 check
    直接 commit 的模型能把同题重写的稿子整批灌进库: 途鸽 09-10 一天里四个标题
    各入库两次, 两两 Jaccard 只有 0.33, RPC 全放行。闸不该依赖模型记得先调它。

    不做归属校验、不算向量: 两个调用方各自做完再进来, 向量按 ``titles`` 逐位
    对齐(某一位可以是 None —— 空标题算不出向量)。

    ``own``(2026-10-30): ``{第几篇: 它替换的旧 item 的指纹行}``, 只有 check_drafts 的改稿那几篇有 —— 这些行不算那一篇
    的对手(见 _without_own_versions)。commit_drafts 不传: 它在进闸之前已经把旧版的指纹摘下来了。
    """
    o_hashes = [fp.opening_hash(b) for b in bodies]
    grams = [set(fp.ngram_hashes(b)) for b in bodies]


    # ── 与历史比对: 优先下推到库里(审计 SUP-002 / ROB-004 / ROB-011)──────
    # ``hist`` 是一个「按 i 取四路最佳命中」的可调用对象, 两条路径共用同一个
    # 形状, 下面拼 verdict 的代码因此完全不用分叉。
    hist, hist_size, hist_with_vec, hist_untitled, hist_truncated = _history_probe(
        client, project_id, o_hashes, grams, new_vecs, own=own)

    # ⚠️ 降级判定不能只看"这批能不能算向量"(codex review P1)。
    # 历史行的 title_embedding 可能是 NULL —— 当初 commit 时 embedding 服务不可用
    # 就会这样, 而且没有回填路径。那种情况下 new_vecs 非空、看起来正常, 但每一条
    # 历史都在下面被跳过, 标题语义这一路【实际没跑】,
    # 却报 semantic_degraded=false 让调用方以为全套硬闸都过了。
    #
    # ⚠️ 但空标题的历史行【不算缺】(2026-10-30)。它们永远算不出标题向量, 也没有补的出口 —— 原来算进缺的那一边,
    # 一个项目只要补录过一条空标题的帖, 之后每一批都报 degraded, 写手每次都听一遍「没跑全」, 而那一路其实对每一条
    # 有标题的历史都比过了。有标题、没向量的照样算缺。(口径见 store.fingerprint_stats)
    # 几个数来自分开的几次 count, 中间有人删行时可能对不齐, 钳到 0, 别报出一个负数。
    hist_missing_vec = max(0, hist_size - hist_with_vec - hist_untitled)
    hist_titled = max(0, hist_size - hist_untitled)
    semantic_ran = bool(new_vecs) and (hist_with_vec > 0 or hist_titled == 0)
    degraded = not semantic_ran or hist_missing_vec > 0

    results = []
    contain_ran = False          # 有没有任何一篇真的跑过包含度这一路
    for i in range(len(titles)):
        h = hist(i)
        best_sim, sim_hit, best_j, j_hit, exact, open_hit = (
            h.sim, h.sim_title, h.jac, h.j_title, h.open_exact, h.open_title)
        best_c, c_hit, c_sample = h.contain, h.contain_title, h.contain_sample

        # ⚠️ 每个信号的最佳命中【各记各的】, 且各自记清楚是本批内还是历史。
        # 原来共用一个 intra 变量, 只要任一信号的最佳命中来自本批内就无条件
        # 认定"撞的是本批内那篇" —— 但真正让 verdict 判死的可能是另一个信号、
        # 而那个信号的命中在历史里。于是报出去的 collided_with 是一条根本没
        # 引发拒绝的稿子, 人对着它改, 改完还是过不了。(codex review)
        # hit = (标题, 归属)  归属 ∈ {"本批内", "历史"}
        sim_best = (sim_hit or "", "历史") if sim_hit is not None else None
        j_best = (j_hit or "", "历史") if j_hit is not None else None
        open_best = (open_hit or "", "历史") if open_hit is not None else None
        c_best = (c_hit or "", "历史") if c_hit is not None else None

        for k in range(i):   # 本批内互比: 同批两篇撞车同样要拦
            # o_hashes[i] 为空 = 这篇没有正文开头。空 == 空【不算撞车】——
            # 否则两篇 title-only 的稿子会被判成开头完全一致(见
            # fp.opening_hash 的说明)。
            if o_hashes[i] and o_hashes[i] == o_hashes[k]:
                exact, open_best = True, (titles[k], "本批内")
                break
            # 同上: 本批内两篇也都是 sketch, 而且同一批里长短稿并存很常见
            # (一篇 300 字的短图文 + 一篇 1500 字的长测评)。(审计 COR-014)
            jj, cc, ss = fp.sketch_overlap(grams[i], grams[k])
            if jj > best_j:
                best_j, j_best = jj, (titles[k], "本批内")
            # 样本量先过闸再比大小 —— 与上面对历史稿那一路同一条理由。
            if ss >= fp.CONTAIN_MIN_SAMPLE and cc > best_c:
                best_c, c_best, c_sample = cc, (titles[k], "本批内"), ss
            if new_vecs:
                s = dedup.cosine_similarity(new_vecs[i], new_vecs[k])
                if s > best_sim:
                    best_sim, sim_best = s, (titles[k], "本批内")

        contain_ran = contain_ran or c_sample >= fp.CONTAIN_MIN_SAMPLE
        status, reason, which = fp.deciding_signals(
            best_sim, exact, best_j, best_c, c_sample)
        by_signal = {"opening": open_best, "title": sim_best,
                     "ngram": j_best, "contain": c_best}
        # 按 verdict 实际依据的信号取命中; 两个弱信号并列时取第一个(它排在
        # reason 的最前面, 与用户读到的解释对得上)。pass 时没有依据信号,
        # 退回"最强的那个"只为让人知道最接近的是什么, 并注明是参考。
        hit = next((by_signal[s] for s in which if by_signal.get(s)), None)
        informational = False
        if hit is None:
            hit = open_best or sim_best or j_best or c_best
            informational = hit is not None
        collided, scope = hit if hit else ("", "")
        row = {
            "index": i, "title": titles[i], "status": status, "reason": reason,
            "collided_with": collided,
            "collided_scope": scope,
            "decided_by": which,
            "signals": {"title_similarity": round(best_sim, 4),
                        "opening_exact_match": exact,
                        "body_ngram_jaccard": round(best_j, 4),
                        # 审计 COR-014。sample 一并报出去 —— 它低于
                        # CONTAIN_MIN_SAMPLE 时 containment 这个数字**没有参考
                        # 价值**, 不报的话看的人会拿一个纯噪声当结论。
                        "body_ngram_containment": round(best_c, 4),
                        "containment_sample": c_sample},
        }
        if informational:
            row["collided_note"] = ("这条判定为通过, collided_with 只是最接近的"
                                    "参照, 不是拦下它的原因。")
        results.append(row)

    summary = {
        "total": len(results),
        "pass": sum(1 for r in results if r["status"] == "pass"),
        "warn": sum(1 for r in results if r["status"] == "warn"),
        "reject": sum(1 for r in results if r["status"] == "reject"),
        "history_size": hist_size,
        "history_with_embedding": hist_with_vec,
        "history_missing_embedding": hist_missing_vec,
        # 空标题、没向量的历史条数: 不算在 history_missing_embedding 里(见上面降级判定那段)
        "history_untitled": hist_untitled,
        "history_truncated": hist_truncated,
        "semantic_degraded": degraded,
        "containment_checked": contain_ran,
    }
    # ── 下面几句是**原样念给写手的**(2026-10-30 发布收尾)────────────────────────
    # 只说发生了什么、交付照不照常; 命令、迁移号、环境变量、表名一律进 logger.warning, 不进返回值 ——
    # 原来这里写着 `python -m deskcore.cli backfill` / migrations/004 之类, 模型照念, 写手每批听一遍看不懂、
    # 也做不了的工程指令。写手手里没有任何一步能补这些, 不能说成是他们要做什么。
    if hist_size and not contain_ran:
        # 审计 COR-014: 包含度这一路一次都没真的发言。静默不发言正是这条 finding 本身的形态(有个信号没跑,
        # 而返回值看起来一切正常), 所以照样说; 老版 RPC 回不出包含度那种情形 _history_probe 已经记了日志。
        summary["containment_skipped_warning"] = (
            "「短稿整段照搬长稿」这一路这次没查成(多半是草稿和历史稿长短差太多, 能比的字数不够), "
            "另外三路照常查了; 整段照搬一篇长稿的重复这次可能漏掉。已记下, 交付照常, 不用你做什么。")
    if hist_truncated:
        # history_size 报的是【实际比过的条数】, 但项目的历史比这更多。
        # 不说出来的话, "比对全量历史" 就成了一句假话。
        # 下推路径不会走到这里(库里是全量比的, hist_truncated 恒为 False),
        # 只有 migrations/004 没跑、退回 Python 路径时才可能命中。
        logger.warning("check_drafts: 项目 %s 的历史指纹超过 %d 条, 只比了最近的这些 —— 退回了 Python 路径"
                       "(migrations/004 没跑?), 长期解法是把比对下推到数据库(docs/deskcore.md §7)",
                       project_id, hist_size)
        summary["history_truncated_warning"] = (
            f"这个项目的历史稿超过 {hist_size} 篇, 这次只和最近的 {hist_size} 篇比了, 更早的没参与查重。"
            "已记下, 交付照常, 不用你做什么。")
    # 指纹库是空的但项目其实有历史 = 没回填。硬闸背后什么都没有, 必须说出来,
    # 不能让调用方以为"比对了全量历史然后没撞车"。
    if not hist_size:
        logger.warning("check_drafts: 项目 %s 的指纹库一条历史都没有 —— 不是新项目的话说明还没回填, "
                       "跑 `python -m deskcore.cli backfill --project %s`", project_id, project_id)
        summary["empty_history_warning"] = (
            "查重库里这个项目还没有历史稿, 这次只在本批内部比了(新项目本来就是这样)。"
            "已记下, 交付照常, 不用你做什么。")

    if degraded:
        # 原因只进日志: 写手那边一句话就够, 而且一个字都不该是要他们去做的事。
        why = []
        if not new_vecs:
            why.append("本批标题算不出向量(GOOGLE_API_KEY 未配或 embedding 调用失败)")
        if hist_missing_vec:
            # 补已经在指纹库里的行是 reembed 的活, 不是 backfill(见 reembed_fingerprints 的说明)
            why.append(f"{hist_missing_vec}/{hist_titled} 条有标题的历史没有本模型的 title_embedding"
                       f"(跑 `python -m deskcore.cli reembed --project {project_id}` 可补)")
        logger.warning("semantic dedup degraded (project=%s): new_vecs=%s history_with_vec=%d/%d "
                       "(另有 %d 条空标题, 不算缺): %s",
                       project_id, bool(new_vecs), hist_with_vec, hist_size, hist_untitled,
                       "; ".join(why))
        summary["degraded_note"] = "这次标题语义查重没跑全, 已记下; 交付照常, 不用你做什么。"
    return {"results": results, "summary": summary}


# (这里原来有个 _placeholder_version_id: 没有真实 version_id 时按 angle_key 的
#  sha256 造一个确定性假 UUID, 只为把 angle_ledger.consumed_version_id 置成非
#  NULL。commit_drafts 现在给每条没带 id 的稿子都造真 id 并建出 versions 行,
#  那个分支再也走不到 —— 删掉而不是留着, 留着的是一段永远不执行的代码加一句
#  "没有真实 version_id 时"的注释, 下一个读的人会以为这种情况还存在。)


# deskcore_commit_fingerprints 成功那一支返回的 status。**改这个字面量等于改
# 一份跨语言契约**: SQL 侧写什么、这里数什么, 必须是同一个词。
#
# 之所以要给它一个名字: migrations/005 曾把 SQL 侧从 'inserted' 改成 'written',
# 而这边照旧只数 'inserted' —— 后果不是报错, 是每次成功入库都报 written=0、
# 角度台账一条都不销账(于是同一个坐标可以被无限次抽到)。全程没有任何异常。
# 现在 tests/sql_parity_check.py 拿这个常量去比对 SQL 的真实返回值。
COMMIT_STATUS_INSERTED = "inserted"


def _prepare_replacements(client, project_id: str, drafts: list[dict]) -> tuple[dict, dict, dict]:
    """改稿替换: 找出带 ``replaces_version_id`` 的稿子, 把旧 item 的指纹摘下来。

    回 ``(replace, rejected, stash)``:
      replace  {index: {"item_id", "old_version_id", "status", "next_version_num"}}
      rejected {index: 原因}  —— 不是 UUID / 同时带了 version_id / 版本不在本项目 /
                               同一版被替换两次
      stash    {index: [指纹整行, …]} —— 已经从库里删掉、待会儿可能要放回的

    ⚠️ 调用方必须持有 _project_write_lock: 摘掉指纹到原子写入之间不在一个事务里。
    删之前把**全部**要摘的行先读齐, 删到一半失败就把已删的放回再上抛 —— 否则前面
    几条的指纹就此消失, 而外层 try 还没拿到 stash(codex #84 P1)。
    """
    replace, rejected = _lookup_replacements(client, project_id, drafts)
    if not replace:
        return replace, rejected, {}
    versions = store.version_ids_of_items(client, [r["item_id"] for r in replace.values()])
    # 先把要摘的全部读齐, 再删 —— 读齐之前一行都不动。
    to_pull: dict[int, tuple[list[str], list[dict]]] = {}
    for i, r in replace.items():
        vs = versions.get(r["item_id"]) or []
        r["next_version_num"] = max([v["version_num"] for v in vs] + [0]) + 1
        ids = [v["id"] for v in vs] or [r["old_version_id"]]
        to_pull[i] = (ids, store.fingerprint_rows_for_versions(client, project_id, ids))
    stash: dict[int, list[dict]] = {}
    for i, (ids, rows) in to_pull.items():
        try:
            store.delete_fingerprints_for_versions(client, project_id, ids)
        except Exception:
            _restore_stash(client, stash, list(stash))
            raise
        stash[i] = rows
    return replace, rejected, stash


def _lookup_replacements(client, project_id: str, drafts: list[dict]) -> tuple[dict, dict]:
    """核 ``replaces_version_id``, 只读。回 ``(replace, rejected)``:
      replace  {index: {"item_id", "old_version_id", "status"}}
      rejected {index: 原因}  —— 不是 UUID / 同时带了 version_id / 版本不在本项目 / 同一版被替换两次

    score_drafts(_prepare_replacements, 核完再摘指纹)和 check_drafts(核完只在比对时把旧版排除在外)共用这一处
    (2026-10-30): 同一个 id 两边的判断一个字不差, 不会查重时放过、存档时又拒。
    """
    want = {i: str(d.get("replaces_version_id")).strip()
            for i, d in enumerate(drafts) if isinstance(d, dict) and d.get("replaces_version_id")}
    if not want:
        return {}, {}
    rejected: dict[int, str] = {}
    lookup: dict[int, str] = {}
    for i, vid in want.items():
        if not _is_uuid(vid):
            # uuid 列的 IN 谓词在解析阶段就会拒掉整条查询, 一个打错的 id 会连累同批
            # 所有好的(codex #84 P2)。
            rejected[i] = f"replaces_version_id={vid!r} 不是合法的 UUID"
        elif drafts[i].get("version_id"):
            # 带 version_id 的稿子是"库里本来就有这一版"(UI 生成), 不会造新 id ——
            # 和替换互斥, 否则新指纹写进去之后才发现没有 id 可挂(codex #84 P2)。
            rejected[i] = "不能同时带 version_id 和 replaces_version_id: 替换稿由入库分配新 id"
        else:
            lookup[i] = vid
    if not lookup:
        return {}, rejected
    known = store.items_for_versions(client, project_id, list(lookup.values()))
    replace: dict[int, dict] = {}
    seen_items: dict[str, int] = {}
    for i, vid in lookup.items():
        hit = known.get(vid)
        if not hit:
            rejected[i] = (f"replaces_version_id={vid} 不是本项目里的版本 —— 只能替换"
                           "本项目 score_drafts 返回过的 version_id")
            continue
        if hit["item_id"] in seen_items:
            rejected[i] = (f"replaces_version_id={vid} 与第 {seen_items[hit['item_id']]} 条"
                           "替换的是同一篇稿子, 一次只能替换一次")
            continue
        seen_items[hit["item_id"]] = i
        replace[i] = {"item_id": hit["item_id"], "old_version_id": vid,
                      "status": hit.get("status")}
    return replace, rejected


def _own_fingerprints(client, project_id: str, replace: dict) -> dict[int, list[dict]]:
    """check_drafts 的改稿那几篇: 旧 item **全部版本**在指纹库里的行(和 _prepare_replacements 摘的是同一批: 发出去的帖记在
    那一版下的(W-40)也在里面), 只读。``{index: [行, …]}``, 向量列解析成 list(同 store.fingerprints)。"""
    if not replace:
        return {}
    versions = store.version_ids_of_items(client, [r["item_id"] for r in replace.values()])
    ids_of = {i: [v["id"] for v in versions.get(r["item_id"]) or []] or [r["old_version_id"]]
              for i, r in replace.items()}
    rows = store.fingerprint_rows_for_versions(client, project_id, [v for ids in ids_of.values() for v in ids])
    by_version: dict[str, list[dict]] = {}
    for row in rows:
        row = {**row, "title_embedding": db._parse_pgvector(row.get("title_embedding"))}
        by_version.setdefault(str(row.get("version_id")), []).append(row)
    return {i: [row for v in ids for row in by_version.get(str(v), [])] for i, ids in ids_of.items()}

def _empty_body_reason(draft: dict) -> str:
    """正文空的那篇怎么说(W-30)。只列字段名, 不带内容。"""
    keys = ", ".join(sorted(str(k) for k in draft)) or "(空)"
    return (f"正文是空的, 没存档、角度没销账。正文要放在这篇 draft 的 body 字段里(这次收到的字段: {keys}), "
            "补上之后原样带着 angle_key / replaces_version_id 再调一次 score_drafts")


def _clip_key(key: str) -> str:
    return key if len(key) <= ANGLE_KEY_SHOW_CHARS else key[:ANGLE_KEY_SHOW_CHARS] + "…"


def _angle_key_problems(client, project_id: str, missed: dict[int, str], *,
                        user_id=None) -> tuple[list[dict], bool]:
    """送去销账却没销成的那几篇里, 哪几篇是 key 本身的问题(W-31)。

    回 ``(问题, 查清了没有)``; 问题是 ``[{"index", "angle_key"(只露前 ANGLE_KEY_SHOW_CHARS 字), "length", "problem"}]``,
    problem 是 ``store.ANGLE_KEY_USED``(已经销给了别的稿子)或这个项目台账里没有的三种(ANGLE_KEY_ABSENT):
      · ``unknown``: 形状就不是发牌发的那种(20 位十六进制) —— 编的、缩写的、抄错的;
      · ``other_project``: 形状对, 是调用者自己在别的项目抽的牌(条目多带 ``project_id``) —— 稿子多半存错了项目;
      · ``not_in_ledger``: 形状对, 哪个台账都没有(或者没查清别的项目) —— 多半是发牌时台账没写进去。
    后两种是 W-31 复审补的: 形状对的 key 说成「自己编的」, 写手会去改一个本来没错的东西。台账里还有没销账的行却没销成的
    (并发 / 出错)不算 key 的问题, 不在里面。**不抛**: 指纹这时已经写进去了, 同 angle_debt 那段的纪律。"""
    if not missed:
        return [], True
    try:
        states = store.angle_key_states(client, project_id, list(missed.values()))
    except Exception:                                   # noqa: BLE001 — 提醒, 绝不拖垮入库
        logger.exception("commit_drafts: 查 angle_key 状态失败(project=%s) —— 只笼统报没销成", project_id)
        states = None
    if states is None:
        return [], False
    # 本项目台账里没有、形状却对的, 再查一次是不是调用者自己在别的项目抽的(只有这种时候才多这一次查询)
    shaped = sorted({k for k in missed.values()
                     if states.get(k) == store.ANGLE_KEY_UNKNOWN and fp.looks_like_angle_key(k)})
    elsewhere: dict[str, str] = {}
    if shaped:
        try:
            elsewhere = store.angle_keys_drawn_elsewhere(client, project_id, user_id, shaped) or {}
        except Exception:                               # noqa: BLE001 — 同上; 查不清就按「台账里没有」说
            logger.exception("commit_drafts: 查 angle_key 是不是别的项目的牌失败(project=%s)", project_id)
    out = []
    for i, key in sorted(missed.items()):
        st = states.get(key)
        if st == store.ANGLE_KEY_UNKNOWN and fp.looks_like_angle_key(key):
            st = store.ANGLE_KEY_OTHER_PROJECT if key in elsewhere else store.ANGLE_KEY_NOT_IN_LEDGER
        if st in ANGLE_KEY_ABSENT or st == store.ANGLE_KEY_USED:
            p = {"index": i, "angle_key": _clip_key(key), "length": len(key), "problem": st}
            if st == store.ANGLE_KEY_OTHER_PROJECT:
                p["project_id"] = elsewhere[key]
            out.append(p)
    if out:
        by = {}
        for p in out:
            by[p["problem"]] = by.get(p["problem"], 0) + 1
        logger.warning("commit_drafts: %d 篇带的 angle_key 对不上台账 (project=%s problems=%s lengths=%s)",
                       len(out), project_id, by, sorted({p["length"] for p in out}))
    return out, True


def _restore_stash(client, stash: dict[int, list[dict]], indices) -> None:
    for i in indices:
        rows = stash.get(i)
        if rows:
            try:
                store.restore_fingerprint_rows(client, rows)
            except Exception:                        # noqa: BLE001
                logger.exception("commit_drafts: 替换失败后放回旧指纹失败(index=%d) —— "
                                 "跑 backfill 可补", i)


# ══════════════════════════════════════════════════════════════════════
# 入库判定(judge) —— commit_drafts 在项目写锁之外调(2026-09-24, docs/deskcore.md §3.8)
# ══════════════════════════════════════════════════════════════════════

# 不拦(JevforCoentent docs/00 #4; docs/28 §7.3: 最低档也只是 revise, TV 不拦发布)。
# 2026-10-19 起(D-120, 盖过 D-119 J-07 的"留在影子期"): 存档后的判定**给写手看** —— 每篇一个参考分 + 最多
# SCORE_MAX_FIXES 条修改建议。仍然**不拦**: 不改 written / rejected, 判不成存档照样成功。答案照旧由 judge 写进
# TV 账本。这句话原样回给调用方模型, 它决定模型拿分数做什么: 说给用户听、按建议改一轮, 不循环、不重试。
SCORE_NOTE = ("参考分: 不拦交付, 也不改 written / rejected。score 满分 100, 只看篇级硬约束(平台合规题 + 特征题库里的"
              "合规题), 文笔好坏不在里面。fixes 是最该看的几条(sure=false 的是 Jev 拿不准的, 看一眼就行, 不一定要改)。"
              "把每篇的分数和 fixes 照实告诉用户; 用户要改, 按 fixes 改完带 replaces_version_id 再调一次 score_drafts, "
              "改一轮就够, 不要为了分数反复重写。judge_status 不是 ok 就说这篇这次没打成分, 不用重试。")
JUDGE_SHADOW_NOTE = SCORE_NOTE          # 旧名, 老测试 / 老调用方还在 import

# 参考分怎么算(写给人看的口径, 协议里同一句): 满分 100; Jev 有把握(p ≥ SCORE_SURE_P)的硬伤每条 -25,
# 拿不准的每条 -8, 有硬约束没判出答案的每条 -10; 正文太短判不了的不给分(score=None)。
# 线上 10-19 实查: 影子期判出的 7 条硬伤 p 全在 0.54~0.69 —— 一条都不算"有把握", 所以拿不准的必须单独一档,
# 不然一个 0.55 的疑似就能把一篇好稿打到 75。
SCORE_SURE_P = 0.7
SCORE_MAX_FIXES = 3
_SCORE_PENALTY_SURE = 25
_SCORE_PENALTY_UNSURE = 8
_SCORE_PENALTY_UNJUDGED = 10

# 数据出境口径里唯一"未发布稿整篇不发"的品类(docs/00 #7)。一个写作台项目对着几个
# TV 项目、品类又不一致时, 只要其中有它就按它报 —— 出境这件事上宁严勿松。
_RX_CATEGORY = "处方药"


def _ms_since(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


# commit 段里各步的名字(W-21, 10-30): 按执行顺序, 「other」是 commit 段减去这些之后剩的(拼回包、算提示语、放锁)。
COMMIT_PARTS = ("access", "replace_prep", "embed", "gate", "fingerprints", "restore", "replace_versions", "arm",
                "mint", "consume", "snapshots", "key_check", "angle_debt", "other")


def _lap(parts: dict, key: str, since: float) -> float:
    """把 since 到现在的毫秒数累加进 ``parts[key]``, 回现在, 下一步从这里接着量(W-21)。

    一段接一段地量、中间不留缝, 各步加起来 + other 就是 phase_ms.commit; 哪一步慢一眼看得出, 不用再猜。"""
    now = time.monotonic()
    parts[key] = parts.get(key, 0) + int((now - since) * 1000)
    return now


def _judge_targets(drafts: list[dict], titles: list[str], bodies: list[str],
                   inserted_idx: set[int], replace: dict, replaced_ok: dict,
                   minted_ids: dict, minted_ok) -> tuple[list[dict], list[dict]]:
    """这次入库里**哪几篇要判**、各自的 subject_id。返回 ``(targets, skipped)``。

    subject_id 必须是一条**真实存在**的 ``versions.id``: 账本按它 JOIN 回写作台,
    指到不存在的行就等于白判(而且判的钱照花)。所以:

      · 调用方自带 ``version_id`` 的(UI 生成的稿, 库里本来就有那一版)用它自己的。
        ⚠️ 这些**不在** ``out["version_ids"]`` 里(那里只报这次新建的身份), 照
        version_ids 判会把它们整批漏掉 —— 设计审查 2026-09-23 §5 指出的就是这个。
        但这个 id 是调用方说的, 入库这条路径从没核过它是不是本项目的一版: 这里只标上
        ``caller_supplied``, 发之前由 ``_judge_owned_only`` 按本项目查一遍, 查不到的不发;
      · 新稿只判 ``minted_ok`` 里的: ``identity_error`` 时没建成的那几条只有指纹、
        没有 versions 行, **跳过**;
      · 改稿替换只判真挂上了旧 item 的(``replaced_ok``), 挂失败的同理跳过;
      · 正文为空的跳过: 只有标题判不出东西(fq 全部 text_too_short), 平台题却可能对着
        一个标题给出硬伤(设计审查 §6 · loop.judge_draft 空正文那条)。

    只看入库成功的(``inserted_idx``): 被拒的没有交付, 不该在账本里留答案。
    这里不查库 —— 要查的(归属、项目号、品类)都在 ``_judge_committed`` 的截止之内查。
    """
    minted_ok = set(minted_ok)
    targets: list[dict] = []
    skipped: list[dict] = []
    for i in sorted(inserted_idx):
        d = drafts[i]
        caller_supplied = False
        if i in replace:
            if i not in replaced_ok:
                skipped.append({"index": i, "reason": "replace_failed",
                                "detail": "新指纹入了库但没挂到旧稿上, 这一版没有 versions 行"})
                continue
            vid = minted_ids[i]
        elif d.get("version_id"):
            vid = str(d["version_id"])
            caller_supplied = True
        elif minted_ids.get(i) in minted_ok:
            vid = minted_ids[i]
        else:
            skipped.append({"index": i, "reason": "identity_error",
                            "detail": "指纹入了库, 但 items/versions 没建成 —— 账本的 "
                                      "subject_id 会指向不存在的行"})
            continue
        if not (bodies[i] or "").strip():
            skipped.append({"index": i, "version_id": vid, "reason": "empty_body",
                            "detail": "只有标题, 判不出东西"})
            continue
        targets.append({"index": i, "version_id": vid, "caller_supplied": caller_supplied,
                        "title": titles[i], "body": bodies[i]})
    return targets, skipped


# 调用方自带的 version_id 在本项目里查不到(不存在 / 别的项目的 / 不是 UUID)时 skipped 的 reason。
JUDGE_SKIP_NOT_IN_PROJECT = "version_not_in_project"


def _judge_owned_only(client, project_id: str, targets: list[dict]
                      ) -> tuple[list[dict], list[dict], list[tuple[dict, dict]]]:
    """调用方自带的 version_id, 发之前先按**本项目**核一遍(codex review P1 on #92)。
    返回 ``(send, skipped, failed)``: 要发的 / 不发且记进 skipped 的 / 查库失败没发的
    (``(target, 结局)``)。

    judge 收到 ``write=true`` 就按 subject_id 把答案写进 TV 账本。而 commit_drafts 对调用方
    带来的 version_id 一向照单全收(写进指纹、拿去销账), 从没核过。判定这里再照单全收:

      · 过期的 / 抄错的 UUID → 账本里多出一组挂在不存在的 ``aw_version`` 上的答案(孤儿行),
        JOIN 回写作台永远 JOIN 不上, 判的钱白花;
      · **别的项目**的 version_id → 这篇稿子的判定记到了那个项目的那一版名下, 张冠李戴 ——
        那一版自己的答案也跟着被搅乱(账本主键含 subject_id)。

    所以走 ``store.items_for_versions``: ``_prepare_replacements`` 核 ``replaces_version_id``
    用的同一个按项目钉死归属的查询(``items!inner → batches!inner`` 再 ``.eq`` project_id,
    别的项目的 id 根本查不出来)。查不到的记 ``skipped``(``reason = version_not_in_project``),
    不发。本次新建的 id(新稿、替换稿)是这次调用自己在本项目里建的, 不用查。

      · 不是合法 UUID 的不送进查询(同 ``_is_uuid``: 一个坏值会让 PG 在解析阶段拒掉整条
        IN 查询, 连累同批的好 id), 直接记 version_not_in_project;
      · 发出去的是库里的规范写法(小写带连字符): 调用方写成大写或不带连字符 PG 照认,
        账本按文本 JOIN 就对不上了;
      · 归属查询本身失败: 这几篇**不发**(不知道是不是本项目的, 就不能拿它写账本), 记
        ``error``; 本次新建的照发 —— 一次查询故障不该连累它们。
    """
    theirs = [t for t in targets if t.get("caller_supplied")]
    if not theirs:
        return list(targets), [], []
    canon = {t["index"]: str(uuid.UUID(str(t["version_id"])))
             for t in theirs if _is_uuid(t["version_id"])}
    known: dict[str, dict] = {}
    lookup_error = None
    if canon:
        try:
            known = store.items_for_versions(client, project_id, sorted(set(canon.values())))
        except Exception as exc:                        # noqa: BLE001 — 查不了就不发, 不抛
            logger.exception("commit_drafts: 核对自带 version_id 的归属失败(project=%s) "
                             "—— 这几篇不判, 入库不受影响", project_id)
            lookup_error = f"{type(exc).__name__}: {exc}"
    send: list[dict] = []
    skipped: list[dict] = []
    failed: list[tuple[dict, dict]] = []
    for t in targets:
        if not t.get("caller_supplied"):
            send.append(t)
            continue
        vid = canon.get(t["index"])
        if vid is None:
            skipped.append({"index": t["index"], "version_id": t["version_id"],
                            "reason": JUDGE_SKIP_NOT_IN_PROJECT,
                            "detail": "调用方带的 version_id 不是合法的 UUID, 不会是本项目的版本"})
        elif lookup_error:
            failed.append((t, judge_client.result(
                judge_client.JUDGE_ERROR,
                detail=f"核对自带 version_id 是不是本项目的版本时查库失败({lookup_error}), 这篇没发")))
        elif vid in known:
            send.append({**t, "version_id": vid})
        else:
            skipped.append({"index": t["index"], "version_id": t["version_id"],
                            "reason": JUDGE_SKIP_NOT_IN_PROJECT,
                            "detail": ("调用方带的 version_id 不是本项目里的版本(不存在, 或属于别的"
                                       "项目) —— 发出去 judge 会把答案写到不存在或别人的版本名下")})
    return send, skipped, failed


def _judge_project_context(client, project_id: str) -> dict:
    """judge 要的项目号与品类: 写作台项目 → ``tv_project_map`` → TV 项目号 →
    ``truth_vault.projects.category``。

    返回 ``{"project", "category", "status", "detail"}``; ``project`` 为 None 时这次
    **不发**, ``status`` / ``detail`` 说明原因。

    为什么不拿写作台的 UUID 凑一个 project: judge 的出境口径(docs/00 #7)按 TV 的项目号
    认项目, UUID 它认不出来 —— 认不出来的项目它只能按「未清关」跑通用层 + 平台层, 而那
    恰好漏掉了「处方药项目的未发布稿不跑」: 一个没接 tv-map 的处方药项目, 稿子会照样发给
    Jev。所以没有映射就不发, 记 ``not_configured``, 补救是 ``tv-map add``(与 tv-sync
    同一张表, 接上它这两件事一起好)。

    多个 TV 项目时取补录目标那一行(``ingest_target``), 没有就按项目号排第一个; 品类
    只要有一个是处方药就报处方药(见 ``_RX_CATEGORY``)。品类读不到就只发项目号。

    ``detail`` 会原样进 score_drafts / judge_draft / check_rules 的返回值, 是念给写手的那一句, 所以只说"这次判不了、
    不影响什么"; 补救命令和异常原文进日志和 ``ops_detail``(只给 CLI 的 compile-rules 打印, 2026-10-30 发布收尾)。

    ⚠️ 这两次查库会卡: 只许在 ``_judge_committed`` 的工作线程里、共同截止之内调。
    """
    try:
        maps = [m for m in store.tv_projects_of(client, project_id) if m.get("tv_project_id")]
    except Exception as exc:                            # noqa: BLE001 — 009 没跑 / 库故障
        ops = f"查 tv_project_map 失败({type(exc).__name__}: {exc})"
        logger.warning("judge: %s (project=%s), 这次一篇都不发", ops, project_id)
        return {"project": None, "category": None, "status": judge_client.JUDGE_ERROR,
                "detail": "这次没查到项目对照, 判不了; 已记下, 不影响存档和交付, 不用重试。",
                "ops_detail": ops}
    if not maps:
        ops = ("这个项目没接 tv_project_map, 判不出 TV 项目号与品类 —— 数据出境口径(docs/00 #7)下不发。"
               f"补救: python -m deskcore.cli tv-map add --tv-project <TV 项目号> --project {project_id}")
        logger.warning("judge: %s", ops)
        return {"project": None, "category": None,
                "status": judge_client.JUDGE_NOT_CONFIGURED,
                "detail": "这个项目还没接上判定服务, 这次判不了; 已记下, 不影响存档和交付, 不用你做什么。",
                "ops_detail": ops}
    maps.sort(key=lambda m: (not m.get("ingest_target"), str(m["tv_project_id"])))
    project = str(maps[0]["tv_project_id"])
    cats = store.tv_project_categories(client, [m["tv_project_id"] for m in maps])
    category = _RX_CATEGORY if _RX_CATEGORY in cats.values() else cats.get(project)
    return {"project": project, "category": category, "status": None, "detail": ""}


_JUDGE_ROW_KEYS = ("judge_status", "passed", "hard_fails", "calls", "written",
                   "policy", "elapsed_ms", "http_status", "detail")

_JUDGE_BLOCK_TEXT_MAX = 300


def _is_sure(p) -> bool:
    return isinstance(p, (int, float)) and not isinstance(p, bool) and p >= SCORE_SURE_P


def draft_score(res: dict) -> dict:
    """一篇的参考分与修改建议(口径见 SCORE_SURE_P 那段)。只在 judge_status=ok 时有意义; 其余回 {}。

    返回 ``{"score", "fixes", "more_fixes"?, "unjudged"?, "why"?}``。fixes 来自 judge 的修改单(只说哪一句犯了哪条,
    不给改法): 有把握的排前面, 同档按概率从高到低, 最多 SCORE_MAX_FIXES 条; 剩下的只报条数。
    """
    if res.get("judge_status") != judge_client.JUDGE_OK:
        return {}
    if res.get("invalid_reason"):
        return {"score": None, "fixes": [], "why": "正文太短, 判不了"}
    fails = [h for h in (res.get("hard_fails") or []) if isinstance(h, (list, tuple)) and len(h) >= 4]
    sure = sum(1 for h in fails if _is_sure(h[3]))
    unsure = len(fails) - sure
    unjudged = int(res.get("unjudged") or 0)
    score = max(0, 100 - _SCORE_PENALTY_SURE * sure - _SCORE_PENALTY_UNSURE * unsure
                - _SCORE_PENALTY_UNJUDGED * unjudged)
    plan = [e for e in (res.get("plan") or []) if isinstance(e, dict) and e.get("instruction")]
    plan.sort(key=lambda e: (not _is_sure(e.get("p")), -(e.get("p") or 0)))
    fixes = [{"sure": _is_sure(e.get("p")), "instruction": e["instruction"]} for e in plan[:SCORE_MAX_FIXES]]
    out: dict = {"score": score, "fixes": fixes}
    # 没列出的条数按 judge 回的总数算, 不按 judge_client 截过之后留下的(codex review on aw#107)
    total = max(len(plan), int(res.get("plan_size") or 0))
    if total > len(fixes):
        out["more_fixes"] = total - len(fixes)
    if unjudged:
        out["unjudged"] = unjudged
    return out


def _judge_row(target: dict, res: dict) -> dict:
    """一篇一行。空字段不带: 这块东西每次存档都回给调用方模型, 没接 judge 的部署
    一批 20 篇就是 20 行一模一样的空壳 —— 返回值越长, 越容易把协议挤出模型的注意力
    (09-11 那次入库率掉到 22% 就是这个形状, 见 docs/deskcore.md §4.3)。
    参考分(``score`` / ``fixes``)排在最前面: 那是写手要看的; 后面几个键是给指标留的。"""
    row = {"index": target["index"], "version_id": target["version_id"],
           "judge_status": res.get("judge_status")}
    row.update(draft_score(res))
    row.update({k: res[k] for k in _JUDGE_ROW_KEYS
                if k != "judge_status" and res.get(k) not in (None, "", [], {})
                and not (k == "elapsed_ms" and not res.get(k))})   # 0 = 根本没发, 不是"很快"
    return row


def _judge_block(rows: list[dict], skipped: list[dict], *, project: str | None = None,
                 category: str | None = None, detail: str | None = None,
                 error: str | None = None) -> dict:
    """``detail``: 整批一个都没发出去时的原因(没配 / 没接 tv-map / 查库超时), 只说一次。

    ``detail`` / ``error`` 里是异常原文, 一路进返回值和 batch_metrics: 先脱敏、再截断
    (反过来的话, 跨在截断点上的 key 只剩半截, 值级脱敏认不出来)。每篇那一行的 detail
    由 ``judge_client.result`` 按同一个顺序处理过了。
    """
    summary: dict[str, int] = {}
    for r in rows:
        summary[r["judge_status"]] = summary.get(r["judge_status"], 0) + 1
    block = {"visible": True, "note": SCORE_NOTE, "project": project,
             "category": category, "summary": summary, "results": rows,
             "skipped": skipped}
    if detail:
        block["detail"] = mask_secrets(detail)[:_JUDGE_BLOCK_TEXT_MAX]
    if error:
        block["error"] = mask_secrets(error)[:_JUDGE_BLOCK_TEXT_MAX]
    return block


def _judge_committed(client, project_id: str, targets: list[dict],
                     skipped: list[dict], budget: Optional[float] = None) -> dict:
    """把 targets 并行发给 judge, **整批共用一个截止时间**。逐篇兜底, 不抛。

    共同截止(``JUDGE_TIMEOUT_SEC``, 默认 8 秒)而不是每篇各 8 秒: 串行的话 10 篇就是
    80 秒, 远过 MCP 客户端实测能容忍的 ~22 秒(config.py LIBRARIAN_TIMEOUT_SEC 那段)。
    到点没回的记 ``timeout``; 还在排队没开跑的直接取消, 不会再发出去; 已经在飞的那几个
    不等, 由 requests 自己的超时收尾(连接 ≤ 3 秒 + 读 ≤ 截止), 线程随后自然退出。
    ⚠️ 记成 timeout 的那篇, judge 那边可能照样判完并写了账本 —— timeout 只说明写作台
    这边没等到, 不说明账本里没有。

    ── 截止从发之前的查库就开始算(codex review P2 on #92)────────────────
    发之前要查三样: ``tv_project_map``(TV 项目号)、``truth_vault.projects``(品类, 跨
    schema)、调用方自带 version_id 的归属(``_judge_owned_only``)。原来前两样在截止**之外**、
    在调用线程里同步查 —— 而 PostgREST 客户端的默认超时是 120 秒: 库那边一卡, 一次**已经
    入库成功**的 commit 就挂着远远过了 8 秒, MCP 客户端 ~22 秒放弃, 调用方看到的是失败,
    回头一重试就撞上自己刚写进去的指纹。现在三样都在工作线程里查、算进同一个截止:
    到点没查完 → 整批记 ``timeout``、一篇都不发, commit 照常返回; 查完了 → 判定只拿剩下的
    时间。卡住的那个线程由 PostgREST 自己的超时收尾, 它回来之后也不会再发任何东西 ——
    发请求只在本函数里、截止之内。

    ── ``budget``: 调用方按整次 commit 的回包目标算好的截止(J-08)──────────────
    不给就是 ``JUDGE_TIMEOUT_SEC``。commit_drafts 给的是 min(JUDGE_TIMEOUT_SEC, 回包目标 − 入库
    已用 − 跨项目检查), 入库慢的那批判定就少等; 不到 ``_JUDGE_MIN_BUDGET_SEC`` 一篇不发、一次库
    不查, 整批记 ``timeout``。
    """
    if not targets:
        return _judge_block([], skipped)
    if not judge_client.configured():
        # 没配就一个请求都不发、一次库都不查 —— 没接 judge 的部署与改动前完全一样。
        nc = judge_client.result(judge_client.JUDGE_NOT_CONFIGURED)
        # 没配是部署层面的事(judge_status=not_configured 已经落进 batch_metrics), 写手那边只说没打成分
        return _judge_block([_judge_row(t, nc) for t in targets], skipped,
                            detail="这个写作台还没接判定服务, 这次没打分; 不影响存档和交付, 不用你做什么。")

    budget = judge_client.timeout_sec() if budget is None else budget
    if budget < _JUDGE_MIN_BUDGET_SEC:
        to = judge_client.result(judge_client.JUDGE_TIMEOUT)
        return _judge_block(
            [_judge_row(t, to) for t in targets], skipped,
            detail=(f"入库本身已经用掉了大部分回包时间(整次 commit 的目标 "
                    f"{config.COMMIT_RESPONSE_BUDGET_SEC:g}s), 留给判定的不到 "
                    f"{_JUDGE_MIN_BUDGET_SEC:g}s, 一篇都没发 —— 入库不受影响"))
    started = time.monotonic()
    deadline = started + budget

    def _left() -> float:
        return max(0.0, deadline - time.monotonic())

    def _prepare():
        ctx = _judge_project_context(client, project_id)
        if ctx["project"] is None:
            return ctx, [], [], []                     # 反正一篇都不发, 归属也不用查了
        return (ctx, *_judge_owned_only(client, project_id, targets))

    ex = cf.ThreadPoolExecutor(
        max_workers=max(1, min(len(targets), int(config.JUDGE_MAX_WORKERS))),
        thread_name_prefix="judge")
    send: list[dict] = []
    futs: list[cf.Future] = []
    done: set = set()
    try:
        try:
            ctx, send, not_ours, failed = ex.submit(_prepare).result(timeout=_left())
        except cf.TimeoutError:
            to = judge_client.result(judge_client.JUDGE_TIMEOUT)
            logger.warning("judge: 发之前的查库(tv_project_map / TV 品类 / 自带 version_id 的归属)在整批判定的"
                           "共同截止 %.1fs 内没查完, 一篇都没发 (project=%s)", budget, project_id)
            return _judge_block(
                [_judge_row(t, to) for t in targets], skipped,
                detail=(f"发判定之前查库超时({budget:g}s), 这次一篇都没发、没打分; 不影响存档和交付, 不用重试。"))
        if ctx["project"] is None:
            res = judge_client.result(ctx["status"])
            return _judge_block([_judge_row(t, res) for t in targets], skipped,
                                detail=ctx["detail"])
        skipped = sorted(skipped + not_ours, key=lambda s: s["index"])
        rows = [_judge_row(t, r) for t, r in failed]
        left = _left()
        if send and left > 0:
            payloads = [judge_client.build_draft_request(
                version_id=t["version_id"], title=t["title"], body=t["body"],
                project=ctx["project"], category=ctx["category"]) for t in send]
            futs = [ex.submit(judge_client.judge_draft, p, timeout=left) for p in payloads]
            done, _pending = cf.wait(futs, timeout=_left())
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    for k, t in enumerate(send):
        f = futs[k] if k < len(futs) else None
        if f is not None and f in done:
            try:
                res = f.result()
            except Exception as exc:                    # noqa: BLE001 — judge_draft 本不抛, 兜底
                res = judge_client.result(judge_client.JUDGE_ERROR,
                                          detail=f"{type(exc).__name__}: {exc}")
        elif f is None:
            res = judge_client.result(
                judge_client.JUDGE_TIMEOUT,
                detail=f"查库用完了整批判定的共同截止 {budget:g}s, 没来得及发")
        else:
            res = judge_client.result(
                judge_client.JUDGE_TIMEOUT, elapsed_ms=_ms_since(started),
                detail=f"整批判定的共同截止 {budget:g}s 到了还没回来(或排队没轮到)")
        rows.append(_judge_row(t, res))
    rows.sort(key=lambda r: r["index"])
    return _judge_block(rows, skipped, project=ctx["project"], category=ctx["category"])


# ── 写手侧的判稿入口(2026-10-09)──────────────────────────────────────────────
# 以前写手要在自己机器上起 judge 仓的 judge.mcp_server(stdio, 本地 checkout + 一套 JUDGE_* env + 一把发到写手
# 机器的 key); 现在并进 deskcore: 写手只有 deskcore 这一个 HTTP MCP、一把 deskcore key, 判定由 deskcore 用服务端的
# 管理 key 转发给 judge。write / return_rows 钉死 false(judge_client.build_tool_request), 账本行(含暗题)永远不到
# 写手手里; 项目号 / 品类按 tv_project_map 定(与存档时的判定同一条路 _judge_project_context, 数据出境口径
# docs/00 #7 由 judge 执行并回显在 policy)。仍然一次 LLM 调用都没有。
JUDGE_TOOL_NOTE = ("这是写后的深一层参考判定(score_drafts 存档时已经自动打过分), 不拦交付、不落账本。hard_fails / plan 拿去改稿, 改完最多再判一轮; "
                   "judge_status 不是 ok 时如实告诉用户判定这次没拿到, 查重和入库照常走。")


def _judge_tool_call(client, project_id: str, title: str, body: str, *, user_id, judge_paras: str,
                     banks, brief, hard_rules, target, validated) -> dict:
    """归属校验在两个公开函数里各自直接调(tests/test_deskcore_ownership 按 AST 认函数体里的那一次调用)。"""
    base = {"project": None, "category": None, "response": None, "http_status": None, "elapsed_ms": 0}
    if judge_paras not in judge_client.JUDGE_PARAS:
        return {**base, "judge_status": judge_client.JUDGE_BAD_REQUEST,
                "detail": f"judge_paras 只能是 {' / '.join(judge_client.JUDGE_PARAS)}"}
    if not (body or "").strip():
        return {**base, "judge_status": judge_client.JUDGE_BAD_REQUEST, "detail": "body 不能为空"}
    if not judge_client.configured():
        return {**base, "judge_status": judge_client.JUDGE_NOT_CONFIGURED,
                "detail": "这个写作台还没接判定服务, 这次判不了; 不用重试, 不用你做什么。"}
    # 项目号 / 品类的两次查库也算进同一个墙钟截止 (codex review on #95, P2): PostgREST 客户端默认 120 秒超时,
    # 库一卡这个同步工具就远超 MCP 客户端 ~22 秒的容忍, 还不会报 timeout。做法同 _judge_committed: 查库在工作线程里、
    # 到点就记 timeout 一个请求都不发; 查完了判定只拿剩下的时间。
    budget = judge_client.tool_timeout_sec()
    started = time.monotonic()
    ex = cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="judge-tool")
    try:
        try:
            ctx = ex.submit(_judge_project_context, client, project_id).result(timeout=budget)
        except cf.TimeoutError:
            logger.warning("judge_draft: 发之前的查库(tv_project_map / TV 品类)在 %.1fs 内没查完, 没发判定 "
                           "(project=%s)", budget, project_id)
            return {**base, "judge_status": judge_client.JUDGE_TIMEOUT, "elapsed_ms": _ms_since(started),
                    "detail": f"发判定之前查库超时({budget:g}s), 这次没判; 不用重试。"}
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    if ctx["project"] is None:
        return {**base, "judge_status": ctx["status"], "detail": ctx["detail"], "elapsed_ms": _ms_since(started)}
    left = budget - (time.monotonic() - started)
    if left <= 0.5:
        return {**base, "judge_status": judge_client.JUDGE_TIMEOUT, "elapsed_ms": _ms_since(started),
                "detail": f"查库用完了 {budget:g}s 的预算, 没来得及发判定"}
    payload = judge_client.build_tool_request(
        title=title, body=body, project=ctx["project"], category=ctx["category"], judge_paras=judge_paras,
        banks=banks, brief=brief, hard_rules=hard_rules, target=target, validated=validated)
    res = judge_client.judge_draft_full(payload, timeout=left)
    return {**res, "project": ctx["project"], "category": ctx["category"], "elapsed_ms": _ms_since(started)}


def _judge_tool_head(res: dict) -> dict:
    out = {"judge_status": res["judge_status"], "project": res.get("project"), "category": res.get("category"),
           "note": JUDGE_TOOL_NOTE, "elapsed_ms": res.get("elapsed_ms", 0)}
    if res["judge_status"] != judge_client.JUDGE_OK:
        out["detail"] = res.get("detail")
        if res.get("http_status") is not None:
            out["http_status"] = res["http_status"]
    return out


def judge_draft(client, project_id: str, title: str, body: str, *, user_id: str | None = None,
                judge_paras: str = "on_fail", banks: list | None = None, brief: dict | None = None,
                hard_rules: dict | None = None, target: dict | None = None,
                validated: list | None = None) -> dict:
    """判一篇稿子, 回 judge 抹掉暗题的视图(profile / hard_fails / unjudged / plan / recorded / para_stats / policy / banks /
    calls / usage)。judge_status != ok 时只回 judge_status / detail, 不抛。"""
    assert_project_access(client, project_id, user_id=user_id)
    res = _judge_tool_call(client, project_id, title, body, user_id=user_id, judge_paras=judge_paras, banks=banks,
                           brief=brief, hard_rules=hard_rules, target=target, validated=validated)
    out = _judge_tool_head(res)
    if res["judge_status"] != judge_client.JUDGE_OK:
        return out
    resp = res.get("response")
    if not isinstance(resp, dict):
        out.update(judge_status=judge_client.JUDGE_ERROR, detail="judge 回了 200 但响应体不是对象")
        return out
    resp.pop("ledger_rows", None)
    return {**resp, **out}


def repair_plan_for(client, project_id: str, title: str, body: str, *, user_id: str | None = None,
                    banks: list | None = None, brief: dict | None = None, hard_rules: dict | None = None,
                    target: dict | None = None, validated: list | None = None) -> dict:
    """同一次 /judge_draft, 只拿修改单: plan / recorded / passed / invalid_reason / policy / calls。"""
    assert_project_access(client, project_id, user_id=user_id)
    res = _judge_tool_call(client, project_id, title, body, user_id=user_id, judge_paras="on_fail", banks=banks,
                           brief=brief, hard_rules=hard_rules, target=target, validated=validated)
    out = _judge_tool_head(res)
    if res["judge_status"] != judge_client.JUDGE_OK:
        return out
    resp = res.get("response")
    if not isinstance(resp, dict) or "plan" not in resp:
        logger.warning("repair_plan_for: judge 的 /judge_draft 响应里没有 plan(project=%s) —— 服务端版本太旧, "
                       "或 JUDGE_URL 不是 judge 服务?", project_id)
        out.update(judge_status=judge_client.JUDGE_ERROR,
                   detail="判定服务这次没给出改稿方案, 已记下; 不用重试, 不用你做什么。")
        return out
    out.update(plan=resp["plan"], recorded=resp.get("recorded", []), passed=resp.get("passed"),
               invalid_reason=resp.get("invalid_reason"), policy=resp.get("policy"), calls=resp.get("calls"))
    return out


def list_banks() -> dict:
    """judge 上可用的题库清单(名字、版本、层、公开的题目 id; 暗题只报个数)。"""
    res = judge_client.list_banks()
    out = {"judge_status": res["judge_status"], "elapsed_ms": res.get("elapsed_ms", 0)}
    if res["judge_status"] != judge_client.JUDGE_OK:
        out["detail"] = res.get("detail")
        if res.get("http_status") is not None:
            out["http_status"] = res["http_status"]
        return out
    out["banks"] = res.get("response") if isinstance(res.get("response"), list) else []
    return out


_REJECT_SCOPE_CODES = {"本批内": "in_batch", "历史": "history"}
EMPTY_BODY_FIELDS_MAX = 20


def _rejection_meta(drafts: list[dict], rejected: list[dict]) -> dict:
    """被拒的按原因计数, 给 batch_metrics.meta(W-31, 10-27)。10-26 08:09 sportsix 一次 20 篇全拒(written 0),
    回包里有 rejected, 可 batch_metrics 只记了个总数, 事后查不出是闸前撞车、本批内互撞还是正文空。

    只留数和字段名, 不留标题、正文、撞上的是哪篇:
      · ``rejected_by``: gate → 篇数(pre_commit / atomic_recheck / replace_lookup / empty_body);
      · ``rejected_signals``: 依据的信号 → 篇数(opening / title / ngram / contain; 一篇可以有几个);
      · ``rejected_scope``: 撞的是本批内(in_batch)还是历史(history);
      · ``empty_body_fields``: 正文空的那几篇带了哪些字段名(W-30: 多半是正文放错了字段)。
    没有被拒的回 {}(meta 不多出这几个键)。"""
    if not rejected:
        return {}
    by: dict[str, int] = {}
    signals: dict[str, int] = {}
    scope: dict[str, int] = {}
    fields: set[str] = set()
    for r in rejected:
        gate = str(r.get("gate") or "unknown")
        by[gate] = by.get(gate, 0) + 1
        decided = r.get("decided_by") or []
        for sig in ([decided] if isinstance(decided, str) else decided):
            signals[str(sig)] = signals.get(str(sig), 0) + 1
        if r.get("collided_scope"):
            code = _REJECT_SCOPE_CODES.get(r["collided_scope"], "other")
            scope[code] = scope.get(code, 0) + 1
        i = r.get("index")
        if gate == "empty_body" and isinstance(i, int) and 0 <= i < len(drafts) and isinstance(drafts[i], dict):
            fields.update(str(k)[:40] for k in drafts[i])
    meta: dict = {"rejected_by": by}
    if signals:
        meta["rejected_signals"] = signals
    if scope:
        meta["rejected_scope"] = scope
    if fields:
        meta["empty_body_fields"] = sorted(fields)[:EMPTY_BODY_FIELDS_MAX]
    return meta


def _record_commit_metrics(client, project_id: str, user_id, drafts: list[dict],
                           out: dict, *, flywheel: dict | None = None, checks: dict | None = None,
                           commit_parts: dict | None = None) -> None:
    """耗时与判定结局落一行 ``batch_metrics`` + 一行结构化日志。**不抛**。

    为什么要落库而不只打日志: 「commit_drafts 约 1~2 秒」「一批 10 篇并行也是一两秒」
    都没有样本撑着, 8 秒的截止要拿分位数重定(docs/deskcore.md §3.8 有查法)。Railway
    的日志留不久、也查不了分位数。判定的结局同理: 影子期要回答的就是"判了多少、超时多少、
    被出境口径挡了多少", 答案要在库里。
    """
    j = out.get("judge") or {}
    try:
        counters = {"drafts": len(drafts), "written": int(out.get("written") or 0),
                    "rejected": len(out.get("rejected") or []),
                    "judge_skipped": len(j.get("skipped") or []),
                    # 角度销账(W-31): 没带 key / 带了台账里没有的 key / 带了已经销给别的稿子的 key
                    "unattributed": int(out.get("unattributed") or 0),
                    "unknown_angle_keys": int(out.get("unknown_angle_keys") or 0),
                    "used_angle_keys": int(out.get("used_angle_keys") or 0)}
        for k, v in (j.get("summary") or {}).items():
            counters[f"judge_{k}"] = v
        meta = {
            "mode": "deskcore_commit",
            "batch_id": out.get("batch_id"),
            "judge_project": j.get("project"),
            "judge_category": j.get("category"),
            # 整批没发出去(没配 / 没接 tv-map)或判定这一步整体失败时的原因
            "judge_detail": ((j.get("error") or j.get("detail") or "")[:300] or None),
            # 只留判定的结局, 不留题目细节 —— 答案本身在 TV 账本里, 这里要回答的是
            # "发出去了没有、怎么收场的"。
            "judge": [{"version_id": r.get("version_id"), "status": r.get("judge_status"),
                       "passed": r.get("passed"), "score": r.get("score"),
                       "fixes": len(r.get("fixes") or []) + int(r.get("more_fixes") or 0),
                       "hard_fails": [h[1] for h in (r.get("hard_fails") or [])
                                      if isinstance(h, (list, tuple)) and len(h) > 1],
                       "calls": r.get("calls"), "written": r.get("written"),
                       "elapsed_ms": r.get("elapsed_ms"),
                       "detail": (r.get("detail") or "")[:200] or None}
                      for r in (j.get("results") or [])],
            "judge_skipped": [{"index": s.get("index"), "reason": s.get("reason")}
                              for s in (j.get("skipped") or [])],
            "project_isolation": out.get("project_isolation") or {},
            # commit 段按步拆开的毫秒数(W-21, COMMIT_PARTS 的顺序), 加起来 = phase_ms.commit。不放 phase_ms: 那里各段加起来 = total
            "commit_parts_ms": {k: int(commit_parts[k]) for k in COMMIT_PARTS if k in (commit_parts or {})},
            # 飞轮分臂 (docs/28 §7.4): 这次的臂、改稿挂到的旧 item、没能在旧 batch 上标 mixed 的 item ——
            # batch.params 之外的第二份记录, 分析对照组时据此剔除跨臂改过的批次
            **(flywheel or {}),
            # 交付版式与代码查规则(10-20): 拼了没有、几条问题, 查了几条规则、命中几次 —— 只留数, 不留稿子
            **(checks or {}),
            # 被拒的按原因计数(W-31: 10-26 08:09 sportsix 20 篇全拒、written 0, 事后查不出为什么)
            **_rejection_meta(drafts, out.get("rejected") or []),
        }
        if out.get("angle_key_mismatch"):
            # 哪几篇的 key 对不上、错在哪、多长 —— 不留 key 本身(模型编的长串可能是一句描述)
            meta["angle_key_mismatch"] = [{"index": p.get("index"), "problem": p.get("problem"),
                                           "length": p.get("length")} for p in out["angle_key_mismatch"]]
        store.insert_commit_metrics(client, project_id=project_id, user_id=str(user_id),
                                    phase_ms=out.get("phase_ms") or {},
                                    counters=counters, meta=meta)
    except Exception:                                   # noqa: BLE001 — 埋点不能拖垮入库
        logger.exception("commit_drafts: 指标落库失败(project=%s) —— 入库不受影响",
                         project_id)
    try:
        import telemetry
        telemetry.log_event("deskcore_commit", project_id=project_id,
                            drafts=len(drafts), written=out.get("written"),
                            elapsed_ms=out.get("elapsed_ms"),
                            phase_ms=out.get("phase_ms"),
                            commit_parts_ms=commit_parts or {},
                            judge=j.get("summary"), judge_skipped=len(j.get("skipped") or []),
                            project_isolation_status=(out.get("project_isolation") or {}).get("status"),
                            project_isolation_flags=len((out.get("project_isolation") or {}).get("flags") or []),
                            rule_check_hits=((checks or {}).get("rule_checks") or {}).get("hits"))
    except Exception:                                   # noqa: BLE001
        pass


COMMENT_TOOL_NOTE = ("评论只做参考判定，不入稿件库。judge_status 不是 ok 时不能声称检查通过；"
                     "needs_review 的背书或缺答需人工核实，不编造亲历。")

# Running I/O cannot be cancelled by Future.cancel(). Bound surviving work across requests.
_ISOLATION_POOL = cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="project-isolation")
_ISOLATION_SLOTS = threading.BoundedSemaphore(2)
_COMMENT_POOL = cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="comment-tool")
_COMMENT_SLOTS = threading.BoundedSemaphore(4)


def _bounded_submit(pool, slots, fn, *args):
    if not slots.acquire(blocking=False):
        return None
    try:
        future = pool.submit(fn, *args)
    except BaseException:
        slots.release()
        raise
    future.add_done_callback(lambda _: slots.release())
    return future


def _vector_scores(vecs: dict[int, list], vs: list) -> dict[int, float]:
    """每篇稿子对一个项目近期标题样本的最高余弦; 只比同维度的。成批走矩阵(dedup.similarity_matrix),
    走不了(没 numpy / 维度参差)退回逐对 —— 一批 20 篇 × 40 条逐对纯 Python 就要几百毫秒(10-19 实测超时)。"""
    out: dict[int, float] = {}
    dims = {len(v) for v in vecs.values()}
    if len(dims) == 1:
        idx = list(vecs)
        same = [v for v in vs if len(v) == next(iter(dims))]
        sims = dedup.similarity_matrix([vecs[i] for i in idx], same) if same else None
        if sims is not None:
            return {i: float(sims[k].max()) for k, i in enumerate(idx)}
    for i, vec in vecs.items():
        same = [v for v in vs if len(v) == len(vec)]
        if same:
            out[i] = max(dedup.cosine_similarity(vec, v) for v in same)
    return out


def _cross_project_shadow(client, project_id, user_id, drafts, vectors, exclude_versions,
                          partial: dict | None = None, deadline: float | None = None):
    """品牌词标记先算完放进 ``partial``: 外层超时也能把它带回去(10-14 / 10-19 五批超时, 以前连标记一起丢)。
    向量部分(拉样本 + 比)守 ``deadline``, 到点就带着已比完的返回, 不拖到外层把整次检查掐掉。"""
    from .isolation import foreign_markers
    deadline = time.monotonic() + _ISOLATION_TIMEOUT_SEC - 0.1 if deadline is None else deadline
    projects = store.isolation_projects(client, owner_id=user_id)
    target = next((p for p in projects if str(p["id"]) == str(project_id)), None)
    if target is None:
        return {"status": "unavailable", "note": "当前项目标记未查到，无从判断是否串项目"}
    flags = [{"index": i, "markers": foreign_markers((d.get("title") or "") + "\n" + (d.get("body") or ""),
                                                      target, projects)} for i, d in enumerate(drafts)]
    if partial is not None:
        partial["markers"] = [dict(r) for r in flags]
    vecs = {i: vectors[i] for i in range(len(flags)) if vectors and i < len(vectors) and vectors[i]}
    scores: dict = {}
    partial_vectors = False
    if vecs:
        fetch_until = min(time.monotonic() + 1.0, deadline - _ISOLATION_COMPARE_RESERVE_SEC)
        ordered = [target] + [p for p in projects if p["id"] != target["id"]]
        for n, p in enumerate(ordered):
            if time.monotonic() >= fetch_until:
                partial_vectors = True
                break
            rows = store.isolation_fingerprints(client, p["id"], dedup.EMBEDDING_MODEL)
            vs = [r["title_embedding"] for r in rows
                  if r.get("version_id") not in exclude_versions and r.get("title_embedding")
                  and r.get("embedding_model") == dedup.EMBEDDING_MODEL]
            if vs:
                scores[p["id"]] = _vector_scores(vecs, vs)
            if time.monotonic() >= deadline:
                partial_vectors = n + 1 < len(ordered)
                break
    for i, row in enumerate(flags):
        per = {pid: s[i] for pid, s in scores.items() if i in s}
        own = per.get(target["id"])
        others = [(score, pid) for pid, score in per.items() if pid != target["id"]]
        if own is not None and others:
            score, pid = max(others)
            if score >= 0.72 and score - own >= 0.08:
                row["title_vector"] = {"other_project_id": pid, "other_similarity": score,
                                       "own_similarity": own, "model": dedup.EMBEDDING_MODEL}
    out = {"status": "shadow", "flags": [r for r in flags if r["markers"] or r.get("title_vector")],
           "vector_projects_sampled": len(scores), "projects_total": len(projects),
           "note": "跨项目信号只记录、不拦入库。向量仅比同模型的近期标题样本，不能当成正文归属或完整扫描。"}
    if partial_vectors:
        out["vector_partial"] = True
    return out


# commit_drafts 留给跨项目检查的上界; 判定的预算要先把它扣出来(J-08, COMMIT_RESPONSE_BUDGET_SEC)。
_ISOLATION_TIMEOUT_SEC = 1.5
# 向量样本拉到截止前这么多秒就停, 留给比对和回包。
_ISOLATION_COMPARE_RESERVE_SEC = 0.25
# 判定预算低于这个数就不发: 和 JUDGE_TIMEOUT_SEC 的下界同一个数, 再少连查库都查不完。
_JUDGE_MIN_BUDGET_SEC = 1.0


def _cross_project_shadow_bounded(client, project_id, user_id, drafts, vectors, exclude_versions):
    partial: dict = {}
    try:
        future = _bounded_submit(_ISOLATION_POOL, _ISOLATION_SLOTS, _cross_project_shadow,
                                 client, project_id, user_id, drafts, vectors, exclude_versions,
                                 partial, time.monotonic() + _ISOLATION_TIMEOUT_SEC - 0.1)
        if future is None:
            return {"status": "busy", "flags": [], "note": "跨项目检查线程占满，无从判断；入库结果不变"}
        return future.result(timeout=_ISOLATION_TIMEOUT_SEC)
    except cf.TimeoutError:
        future.cancel()  # A running call retains its slot until the done callback.
        markers = partial.get("markers")
        if markers is not None:
            return {"status": "timeout", "flags": [r for r in markers if r["markers"]], "markers_checked": True,
                    "note": "品牌词标记已查完；标题向量没比完，不能声称已完整排除串项目；入库结果不变"}
        return {"status": "timeout", "flags": [], "note": "跨项目检查超时，无从判断；入库结果不变"}
    except Exception as exc:
        logger.warning("cross-project shadow failed: %s", type(exc).__name__)
        return {"status": "unavailable", "flags": [], "note": "跨项目检查不可用；入库结果不变"}


def _comment_tool_call(client, project_id: str, path: str, payload: dict, *,
                       started: float | None = None) -> dict:
    """``started``: 调用方已经花掉的时间也算进同一个截止(score_comments 先查帖子再判, codex review on aw#107)。"""
    started = time.monotonic() if started is None else started
    budget = judge_client.tool_timeout_sec()
    head = {"project": None, "category": None, "elapsed_ms": 0, "note": COMMENT_TOOL_NOTE}
    if not judge_client.configured():
        return {**head, "judge_status": judge_client.JUDGE_NOT_CONFIGURED, "detail": "评论判定服务未配置"}

    def run():
        ctx = _judge_project_context(client, project_id)
        mapped = {**head, "project": ctx["project"], "category": ctx["category"]}
        if ctx["project"] is None:
            return {**mapped, "judge_status": ctx["status"], "detail": ctx["detail"], "elapsed_ms": _ms_since(started)}
        left = budget - (time.monotonic() - started)
        if left <= 0.5:
            return {**mapped, "judge_status": judge_client.JUDGE_TIMEOUT,
                    "elapsed_ms": _ms_since(started), "detail": "项目查询耗尽判定预算，未发送评论判定"}
        request = {**payload, "project": ctx["project"], "category": ctx["category"]}
        res = judge_client.judge_comment_tool(path, request, timeout=left)
        out = {**mapped, "judge_status": res["judge_status"], "elapsed_ms": _ms_since(started)}
        if res["judge_status"] != judge_client.JUDGE_OK:
            return {**out, "detail": res.get("detail"), "http_status": res.get("http_status")}
        return {**(res.get("response") or {}), **out}

    try:
        future = _bounded_submit(_COMMENT_POOL, _COMMENT_SLOTS, run)
        if future is None:
            return {**head, "judge_status": judge_client.JUDGE_TIMEOUT,
                    "elapsed_ms": _ms_since(started), "detail": "评论判定线程占满，未发送本次判定"}
        return future.result(timeout=max(0.001, budget - (time.monotonic() - started)))
    except cf.TimeoutError:
        future.cancel()
        return {**head, "judge_status": judge_client.JUDGE_TIMEOUT,
                "elapsed_ms": _ms_since(started), "detail": "评论判定超过总截止，未取得完整结果"}
    except Exception as exc:
        return {**head, "judge_status": judge_client.JUDGE_ERROR, "elapsed_ms": _ms_since(started),
                "detail": judge_client.mask_secrets(str(exc))[:300]}


def comment_repair_plan_for(client, project_id: str, post_title: str, post_body: str, text: str, slot: dict, *,
                            user_id: str | None = None, kind: str = "product", points: list | None = None,
                            reply_to_text: str = "") -> dict:
    """单条评论按目标画像判定并给修改单；项目归属由写作台确定。"""
    assert_project_access(client, project_id, user_id=user_id)
    return _comment_tool_call(client, project_id, "/comment_repair_plan_for",
                              {"post_title": post_title, "post_body": post_body, "text": text,
                               "slot": slot, "kind": kind, "points": points, "reply_to_text": reply_to_text})


def judge_thread(client, project_id: str, post_title: str, post_body: str, comments: list, *,
                 user_id: str | None = None, kind: str = "product", points: list | None = None,
                 max_named: int = 1) -> dict:
    """整组评论判定；不写稿件库或评论账本。"""
    assert_project_access(client, project_id, user_id=user_id)
    return _comment_tool_call(client, project_id, "/judge_thread",
                              {"post_title": post_title, "post_body": post_body, "comments": comments,
                               "kind": kind, "points": points, "max_named": max_named})


# ── 按链接写评论(2026-10-19, D-120) ─────────────────────────────────────────────────────────────
# 以前写评论要先 open_project, 再自己把帖子正文和评论位要求拼进 comment_repair_plan_for / judge_thread。现在
# 写手在任何对话里贴一条发布链接(或贴帖子正文 + 说是哪个项目)就行: comments_for 从 TV 把帖子、已有评论、本项目
# 管评论的规则和默认评论位一次拿齐; 模型写完调 score_comments, 整组一次判定, 带每条的修改建议。
#
# 默认评论位与 Jev 的 judge/comments.py::default_slots 同一组(提问 ×2 · 补充经验 ×1 · 贴主答疑 ×1, 不放背书位):
# 字段名和闭集取值照抄那边 —— 写错一个取值 judge 会 422(slot.* 不在题库闭集里)。
DEFAULT_COMMENT_SLOTS = (
    {"id": "q1", "speech_act": ["提问"], "role": "读者位", "may_name_brand": False, "must_echo": False,
     "value_ok": ["可行动信息", "判断依据", "无"], "min_detail": "无", "intent": "其他",
     "note": "问一件帖子没写清的事"},
    {"id": "q2", "speech_act": ["提问"], "role": "读者位", "may_name_brand": False, "must_echo": False,
     "value_ok": ["可行动信息", "判断依据", "无"], "min_detail": "无", "intent": "其他",
     "note": "问另一件帖子没写清的事, 别和 q1 问同一件"},
    {"id": "exp", "speech_act": ["补充经验"], "role": "读者位", "may_name_brand": False, "must_echo": True,
     "value_ok": ["可行动信息", "判断依据"], "min_detail": "模糊", "intent": "补充信息",
     "note": "补一段和帖子相关的情境或做法, 不对品牌下结论, 不编造用过某产品的亲历"},
    {"id": "reply", "speech_act": ["回复答疑"], "role": "贴主", "reply_to": "q1", "may_name_brand": True,
     "must_echo": False, "value_ok": ["可行动信息"], "min_detail": "无", "intent": "补充信息",
     "note": "贴主回答 q1, 给能用的信息"},
)


class CommentInputError(ValueError):
    """score_comments 的参数不对(没有 text、slot id 认不出 / 重复、超过条数)。tools 层转成 InvalidInput → 400。"""


COMMENT_RULE_PREFIX = "评论区"
COMMENT_KINDS = ("product", "agency", "shop")      # judge 那边的 Literal, 写错会 422
MAX_SCORED_COMMENTS = 20                            # judge ThreadRequest.comments 的上限
_EXISTING_COMMENT_CHARS = 200
_POST_BODY_CHARS = 6000            # judge 那边只看前 600 字; 给模型看的留长一些, 但别把超长帖整篇灌进对话

COMMENT_SCORE_NOTE = ("评论检查: 只判不写库。passed=true 且 needs_review=false 才算这条过; fix 是必须改的(主要行为、点名品牌、"
                      "接住帖子、细节四项), hints 只是参考, 不改不算没过。没过的按 fix 改一轮再调一次 score_comments, 不要循环。"
                      "needs_review=true 交人工核实, 不能改成伪造的亲历。judge_status 不是 ok 就说这次没判成, 不能说通过。")


def _tv_post_text(note: dict) -> tuple[str, str]:
    title, body = (note.get("title") or "").strip(), (note.get("body") or "").strip()
    if not body:
        t2, body = tvlink.parse_raw(note.get("raw_content"))
        title = title or t2
    return title, body


TV_VANISHED_FLAG = "_vanished"
TV_VANISHED_NOTE = ("TV 里这条链接只剩飞书里已删 / 挪去别的表的旧记录: 项目是按那行旧记录对的, 已有评论停在它消失前。"
                    "帖子要是挪去了别的项目, 带那个项目的 project_id 和帖子正文再调一次。")
_TV_VANISHED_WHY = ("TV 里这条链接只剩飞书里已删 / 挪去别的表的旧记录, 对不到你的项目(帖子可能挪了表、新表还没同步)。"
                    "把帖子的标题和正文贴过来, 并告诉我是哪个项目")


def _note_from_link(client, link: str) -> tuple[dict | None, str | None]:
    """发布链接 → TV 的笔记(多条同链接取最近发布的那条)。回 ``(笔记, 认不出时的原因)``; 查库异常上抛。

    飞书里已删 / 挪走的行(TV 的 v_notes_vanished)让位给活的那行: 运营挪表之后同一条链接在 TV 里有两行, 发布时间
    一样, 排序只剩 note_id, 旧项目的那行会排在前面(BJS_phase1_… < BJS_phase2_…, I-22)。只剩已删的行时照样回它,
    但带上 ``TV_VANISHED_FLAG``, 由调用方说清楚它是旧记录。视图读不到时按原来的办法取, 不因为它拦评论。"""
    key = tvlink.link_key(link)
    if key is None:
        return None, "认不出这条链接(只认小红书 / xhslink / 抖音 / t.cn 的发布链接)"
    rows = store.tv_notes_by_link_token(client, key[1],
                                        accept=lambda r: tvlink.link_key(r.get("publish_url")) == key)
    if not rows:
        return None, ("TV 里还没有这条链接(笔记每天同步一次; 短链和长链也互相认不出)。"
                      "把帖子的标题和正文贴过来, 并告诉我是哪个项目")
    rows.sort(key=lambda r: str(r.get("publish_time") or ""), reverse=True)
    try:
        gone = store.tv_vanished_note_ids(client, [r.get("note_id") for r in rows])
    except Exception:                                   # noqa: BLE001 — 同 016 视图不在时的退路: 照旧取
        logger.exception("comments_for: 查 v_notes_vanished 失败, 按发布时间取最近的那条")
        gone = set()
    live = [r for r in rows if str(r.get("note_id")) not in gone]
    if live:
        return live[0], None
    return {**rows[0], TV_VANISHED_FLAG: True}, None


def _project_for_note(client, note: dict, project_id: str | None, user_id) -> tuple[dict | None, list[dict]]:
    """笔记所在的 TV 项目 → 调用者能访问的写作台项目: ``(项目, [])``, 或者定不下来时 ``(None, 可选的项目)``。

    一个 TV 项目可以对好几个写作台项目(WTG 15 个方向): 先看这条笔记夜里对上的是哪一个(tv_note_links), 对不上
    又有不止一个归调用者的, 让调用者选, 不替他猜 —— 猜错了评论规则和硬规则都是别的方向的(codex review on aw#107)。
    不归调用者的一律 PermissionError(不回帖子内容, 也不回 TV 项目号 —— 拿着一把 key 挨个试链接, 不该能问出别人的
    项目号)。"""
    tv_pid = str(note.get("project_id") or "")
    targets = store.tv_targets_of(client, tv_pid) if tv_pid else []
    ids = [str(t["project_id"]) for t in sorted(targets, key=lambda t: not t.get("ingest_target"))
           if t.get("project_id")]
    if project_id:
        if str(project_id) not in ids:
            raise PermissionError(
                f"这条链接的笔记不属于你给的项目 {project_id}。"
                "是不是链接贴错了, 或者项目选错了? 帖子不在 TV 里对得上的话, 把正文贴过来、不带链接也行。")
        return assert_project_access(client, project_id, user_id=user_id), []
    if len(ids) > 1 and note.get("note_id"):
        try:
            row = store.tv_link(client, str(note["note_id"])) or {}
        except Exception:                               # noqa: BLE001 — 查不到就让用户选, 不替他猜
            logger.exception("comments_for: 查这条笔记的对照失败 (note=%s)", note.get("note_id"))
            row = {}
        linked = str(row.get("project_id") or "") if row.get("match_kind") in _TV_BACKFILL_KINDS else ""
        if linked in ids:
            # 只认真对上的那几档: ingested / unmatched 那一行的 project_id 是这个 TV 项目的补录目标(按项目给的默认),
            # 不是这条笔记写的方向
            try:
                return assert_project_access(client, linked, user_id=user_id), []
            except (PermissionError, ProjectNotFound):
                pass
    mine = []
    for pid in ids:
        try:
            mine.append(assert_project_access(client, pid, user_id=user_id))
        except (PermissionError, ProjectNotFound):
            continue
    if len(mine) == 1:
        return mine[0], []
    if mine:
        return None, [{"id": p["id"], "name": p.get("name")} for p in mine]
    raise PermissionError(
        "这条链接的笔记没有对到任何归你的写作台项目上(还没接上, 或者那个项目不归你)。"
        "告诉我要写的是哪个项目, 并把帖子正文贴过来。")


def _resolve_post(client, *, user_id, link: str, project_id: str, post_title: str,
                  post_body: str) -> tuple[dict | None, str, str, dict | None, dict | None]:
    """评论要接的那篇帖子 → ``(项目, 标题, 正文, TV 笔记或 None, 缺东西时的回包或 None)``。

    有链接先按链接找 TV(找到了按笔记反查项目、过归属闸); 找不到、或没给链接, 就用贴过来的正文 + project_id
    (同样过闸)。两样都没有时回一份 ``resolved=false`` 的回包, 不报错 —— 那是正常的下一步。
    用户贴了正文时以贴的为准(发布后可能改过), 链接只用来定项目和取已有评论。
    """
    link, project_id = (link or "").strip(), (project_id or "").strip()
    post_title, post_body = (post_title or "").strip(), (post_body or "").strip()
    note, why = (None, None)
    if link:
        note, why = _note_from_link(client, link)
    project = choices = None
    if note is not None:
        try:
            project, choices = _project_for_note(client, note, project_id or None, user_id)
        except PermissionError:
            if not note.get(TV_VANISHED_FLAG):
                raise
            # 只剩飞书里已删 / 挪走的旧行: 它的 TV 项目不一定是帖子现在所在的项目(I-22: 成人藻油从 BJS_phase1 挪去新表,
            # 写手给的是成人项目), 不拿它挡人 —— 当作链接没找到, 走贴正文那条路(那条路照样过归属闸, 旧行的内容不回)。
            note, why = None, _TV_VANISHED_WHY
    if note is not None:
        if project is None:
            return None, "", "", None, {
                "resolved": False, "need": ["project_id"], "choices": choices,
                "note": "这条笔记所在的 TV 项目对着你的好几个写作台项目, 夜里也还没对上是哪一个。问用户写的是哪个"
                        "(choices 里选), 再带 project_id 调一次。"}
        title, body = _tv_post_text(note)
        if post_body:
            title, body = post_title or title, post_body
        if not body:
            # TV 里有这条, 但正文是空的(飞书那行只有链接): 光凭标题写评论、判评论都没意义, 让用户贴正文
            # (codex review on aw#107)。项目已经对上了, 回包里带上, 下一次带 project_id 调就行。
            return None, "", "", None, {
                "resolved": False, "need": ["帖子的正文"], "project_id": project["id"],
                "note": f"TV 里有这条笔记, 但没有正文。请用户把帖子正文贴过来, 再带 post_body 和 "
                        f"project_id={project['id']} 调一次。"}
        return project, title, body, note, None
    if post_body and project_id:
        project = assert_project_access(client, project_id, user_id=user_id)
        return project, post_title, post_body, None, None
    need = []
    if not post_body:
        need.append("帖子的标题和正文")
    if not project_id:
        need.append("是哪个项目(list_projects 里的 project_id)")
    return None, "", "", None, {
        "resolved": False, "need": need,
        "note": (why + "。" if why else "") + "请用户补上: " + "、".join(need) + "。补齐后再调一次。"}


def _comment_rules(client, project_id: str, user_id) -> tuple[list[str], list[str]]:
    """``(评论规则, 硬约束)``: content 以「评论区」开头的(任何 severity)+ 全部 hard。合规硬约束对评论一样管用。"""
    hard, soft = store.shared_memories(client, project_id, user_id)
    text = lambda m: (m.get("content") or "").strip()
    comment = [text(m) for m in hard + soft if text(m).startswith(COMMENT_RULE_PREFIX)]
    hard_only = [text(m) for m in hard if not text(m).startswith(COMMENT_RULE_PREFIX)]
    return comment, hard_only


def comments_for(client, *, user_id: str | None, link: str = "", project_id: str = "",
                 post_title: str = "", post_body: str = "") -> dict:
    """写评论之前一次拿齐: 帖子(按链接从 TV 找, 找不到就用贴过来的正文)、已有评论、本项目管评论的规则、默认评论位。

    找不到帖子又没贴正文时**不报错**, 回 ``resolved=false`` + 一句该让用户补什么 —— 那是正常的下一步, 不是故障。
    帖子所在的项目不归调用者时报 PermissionError, 不回帖子内容。归属闸在 ``_resolve_post`` 里, 两条路都过。
    """
    project, title, body, note, missing = _resolve_post(
        client, user_id=user_id, link=link, project_id=project_id, post_title=post_title, post_body=post_body)
    if missing is not None:
        return missing
    source = "tv" if note is not None else "pasted"
    comment_rules, hard_rules = _comment_rules(client, project["id"], user_id)
    existing = []
    rows: list[dict] = []
    if note is not None:
        rows = store.tv_note_comments(client, str(note["note_id"]))
        rows.sort(key=lambda r: (r.get("comment_order") is None, r.get("comment_order") or 0))
        existing = [{"order": r.get("comment_order"), "role": r.get("comment_role"),
                     "is_reply": bool(r.get("parent_comment_id")),
                     "text": (r.get("content") or "").strip()[:_EXISTING_COMMENT_CHARS]}
                    for r in rows if (r.get("content") or "").strip()]
    out = {"resolved": True, "source": source, "project_id": project["id"],
           "project_name": project.get("name"),
           "post": {"title": title, "body": body[:_POST_BODY_CHARS]},
           "existing_comments": existing,
           "comment_rules": comment_rules, "hard_rules": hard_rules,
           "slots": [dict(s) for s in DEFAULT_COMMENT_SLOTS],
           "next_step": ("按 slots 每位写一条(贴主答疑那条回 q1), 守 hard_rules 和 comment_rules, 别和 existing_comments "
                         "里已经有的问题重复; 写完把整组交给 score_comments(帖子参数和这次一样: link, 或 post_title / post_body + "
                         "project_id; 每条评论带 slot 的 id)。")}
    if note is not None:
        out["note_id"] = note.get("note_id")
        out["tier"] = note.get("tier")
        if note.get(TV_VANISHED_FLAG):
            out["tv_note_vanished"] = True
            out["tv_note_vanished_note"] = TV_VANISHED_NOTE
        total = note.get("comments_count")
        if len(rows) >= store.TV_COMMENTS_MAX or (isinstance(total, int) and total > len(existing)):
            # 只取了前 TV_COMMENTS_MAX 条(codex review on aw#107): 说出来, 别让模型以为这就是全部
            out["existing_comments_note"] = (
                f"这条帖子有 {total if isinstance(total, int) else '更多'} 条评论, 这里只列了按顺序的前 "
                f"{len(existing)} 条; 避开重复时以这些为准, 后面的没看到。")
    return out


def _slot_of(value, by_id: dict) -> dict | None:
    """slot 写 id(默认评论位) 或整个对象(自定义)。自定义的必须带非空的 id: 下面按 id 接贴主答疑、查重复,
    缺了会 KeyError 成 500(codex review on aw#107)。"""
    if isinstance(value, dict):
        return dict(value) if isinstance(value.get("id"), str) and value["id"].strip() else None
    if isinstance(value, str) and value in by_id:
        return dict(by_id[value])
    return None


def _thread_lines(entries) -> list[str]:
    """整组的修改单条目是 ``{"slot", "why"}``(judge/comments.py::thread_repair_plan): 折成「位 q1: …」一句。"""
    out = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        why = e.get("why") or e.get("instruction")
        if isinstance(why, str) and why.strip():
            out.append(f"位 {e['slot']}: {why}" if e.get("slot") else why)
    return out


_COMMENT_FIX_CHARS = 400            # judge 的单条修改单带整段题干, 一条能上千字; 一组 4 条 × 3 条修改单会把协议挤出注意力


def _instructions(entries, limit: int) -> list[str]:
    return [e["instruction"][:_COMMENT_FIX_CHARS] for e in (entries or [])
            if isinstance(e, dict) and isinstance(e.get("instruction"), str)][:limit]


def _comment_payload(cands: list[dict]) -> list[dict]:
    """送给 /judge_thread 的 comments: 贴主答疑那条没给 reply_to_text 时, 接上它 reply_to 的那一位的文字。"""
    text_by_slot = {c["slot"]["id"]: c["text"] for c in cands if c["slot"]}
    out = []
    for c in cands:
        slot = c["slot"]
        reply_to_text = c["reply_to_text"] or (text_by_slot.get(slot.get("reply_to"), "") if slot else "")
        item = {"text": c["text"], "role": (slot or {}).get("role", "读者位"), "reply_to_text": reply_to_text}
        if slot:
            item["slot"] = slot
        out.append(item)
    return out


def score_comments(client, *, user_id: str | None, comments: list, link: str = "", project_id: str = "",
                   post_title: str = "", post_body: str = "", kind: str = "product",
                   max_named: int = 1) -> dict:
    """一组候选评论整体判一次(judge /judge_thread), 回每条过没过和修改建议。只判, 不写任何库。

    ``comments`` 每条 ``{"slot": "q1" | {评论位}, "text": "...", "reply_to_text"?}``: slot 写 comments_for 给的 id 就行;
    贴主答疑那条不带 reply_to_text 时, 自动用它 reply_to 的那一位的文字。帖子同 comments_for: 给 link 从 TV 取,
    或给 post_body + project_id。
    """
    if not isinstance(comments, list) or not comments:
        raise CommentInputError("comments 不能为空: 每条 {slot, text}")
    if kind not in COMMENT_KINDS:
        raise CommentInputError(f"kind 只能是 {' / '.join(COMMENT_KINDS)}")
    if len(comments) > MAX_SCORED_COMMENTS:
        raise CommentInputError(f"一次最多判 {MAX_SCORED_COMMENTS} 条评论, 这次给了 {len(comments)} 条")
    by_id = {s["id"]: s for s in DEFAULT_COMMENT_SLOTS}
    cands = []
    for i, c in enumerate(comments):
        if not isinstance(c, dict) or not isinstance(c.get("text"), str) or not c["text"].strip():
            raise CommentInputError(f"第 {i + 1} 条评论没有 text(要是一段文字)")
        if c.get("reply_to_text") is not None and not isinstance(c["reply_to_text"], str):
            raise CommentInputError(f"第 {i + 1} 条评论的 reply_to_text 要是一段文字")
        slot = _slot_of(c.get("slot"), by_id)
        if c.get("slot") not in (None, "") and slot is None:
            raise CommentInputError(f"第 {i + 1} 条评论的 slot {c.get('slot')!r} 不是 comments_for 给的评论位 id"
                                    f"({' / '.join(by_id)}); 自定义评论位要传整个对象, 而且要带 id")
        cands.append({"text": c["text"].strip(), "slot": slot, "reply_to_text": (c.get("reply_to_text") or "").strip()})
    ids = [c["slot"]["id"] for c in cands if c["slot"] and c["slot"].get("id")]
    if len(ids) != len(set(ids)):
        raise CommentInputError("同一个评论位只能有一条(slot id 重复了): 想多写几条同类的, 分几次判, 或者给它们不同的 id")
    # 查帖子(链接 → TV → 项目 → 归属)也算进同一个截止: PostgREST 默认等 120 秒, 库一卡就远超 MCP 客户端 ~22 秒的
    # 容忍(codex review on aw#107; 同 _judge_tool_call / codex #95 的做法)。参数检查在这之前, 不碰库。
    started, budget = time.monotonic(), judge_client.tool_timeout_sec()
    fut = _bounded_submit(_COMMENT_POOL, _COMMENT_SLOTS, lambda: _resolve_post(
        client, user_id=user_id, link=link, project_id=project_id, post_title=post_title, post_body=post_body))
    if fut is None:
        return {"judge_status": judge_client.JUDGE_TIMEOUT, "note": COMMENT_SCORE_NOTE,
                "detail": "评论判定线程占满, 这次没判"}
    try:
        project, title, body, _note, missing = fut.result(timeout=budget)
    except cf.TimeoutError:
        fut.cancel()
        return {"judge_status": judge_client.JUDGE_TIMEOUT, "note": COMMENT_SCORE_NOTE,
                "detail": f"查帖子(链接 / 项目 / 归属)在 {budget:g}s 内没查完, 这次没判"}
    if missing is not None:
        return missing
    payload_comments = _comment_payload(cands)
    res = _comment_tool_call(client, project["id"], "/judge_thread",
                             {"post_title": title, "post_body": body[:_POST_BODY_CHARS], "comments": payload_comments,
                              "kind": kind, "points": None, "max_named": max_named}, started=started)
    head = {"judge_status": res.get("judge_status"), "project_id": project["id"],
            "note": COMMENT_SCORE_NOTE}
    if _note is not None and _note.get(TV_VANISHED_FLAG):
        head["tv_note_vanished"] = True
        head["tv_note_vanished_note"] = TV_VANISHED_NOTE
    if res.get("judge_status") != judge_client.JUDGE_OK:
        return {**head, "detail": res.get("detail")}
    per_comment = res.get("comments") or []
    if len(per_comment) != len(cands):
        # 少回了几条: zip 会悄悄丢掉没回的那几条, 摘要就成了"这几条评论不存在"(codex review on aw#107)
        return {**head, "judge_status": judge_client.JUDGE_ERROR,
                "detail": f"判定服务回了 {len(per_comment)} 条单条结果, 送去的是 {len(cands)} 条 —— 版本对不上?"
                          "这次不算判过"}
    rows = []
    for i, (c, v) in enumerate(zip(cands, per_comment)):
        v = v if isinstance(v, dict) else {}
        row = {"index": i, "slot": (c["slot"] or {}).get("id"), "passed": bool(v.get("passed")),
               "needs_review": bool(v.get("needs_review"))}
        fix = _instructions(v.get("plan"), SCORE_MAX_FIXES)
        hints = _instructions(v.get("hints"), 2)
        if fix:
            row["fix"] = fix
        if hints:
            row["hints"] = hints
        rows.append(row)
    passed = sum(1 for r in rows if r["passed"])
    thread_fix = _thread_lines(res.get("plan"))
    thread_hints = _thread_lines(res.get("hints"))
    out = {**head, "passed": bool(res.get("passed")), "needs_review": bool(res.get("needs_review")),
           "summary": {"comments": len(rows), "passed": passed,
                       "needs_review": sum(1 for r in rows if r["needs_review"])},
           "comments": rows}
    if thread_fix:
        out["thread_fix"] = thread_fix[:SCORE_MAX_FIXES]
    if thread_hints:
        out["thread_hints"] = thread_hints[:2]
    return out


# 交付稿永远整段原样交(2026-10-30 发布收尾)。原来改稿重存时 delivery 只拼改过的那几篇, 这里叫模型「用它们按顺序替换原来
# 那几篇、表头不要再贴一遍」—— 和同一句里的「原样交给用户」打架, 模型自己拼表格时行错位、表头贴两遍, 写手拿到的是两张半截
# 的表。现在改稿重存时服务端把原来那一整批拼回来(_whole_batch_delivery), 只有拼不回来时才只给这次的几篇(scope=this_call,
# 另外补一句 DELIVERY_PARTIAL_NEXT_STEP)。
DELIVERY_NEXT_STEP = ("这个项目有交付模板: delivery.text 是按模板拼好的交付稿; 改过重存的那一次, 它是原来那一整批(没改的几篇照"
                      "它们现在的样子, 改过的换在原位, 顺序不变)。delivery.problems 里有的先改(改过的那几篇带 replaces_version_id "
                      "再调一次, 新返回的 delivery.text 就是改好之后的整批), 然后把 delivery.text 原样整段交给用户: 不要自己拼接、"
                      "替换其中几行或几段, 不要重排版式, 不要再加标记。")
DELIVERY_PARTIAL_NEXT_STEP = ("这一次 delivery.text 只有这次存进去的几篇(delivery.scope 是 this_call: 原来那一批的其余几篇这次"
                              "没能一起拼出来): 照样原样交给用户, 跟用户说这是改过的那几篇, 其余几篇和上一次交给用户的一样。")
RULE_CHECKS_NEXT_STEP = ("rule_checks 是代码按项目规则逐篇查出来的: 标「必须改」的改完再交(带 replaces_version_id 再调一次), "
                         "「建议改」的告诉用户。只是参考, 存档已经成功了。")
RULE_CHECKS_MORE_NEXT_STEP = ("rule_checks 只列了一部分(必须改的全列了, 没列的都是建议改), 还有 rule_checks_more 条没列: "
                              "如实告诉用户还有这么多条没列出来。")
RULE_CHECKS_ERROR_NEXT_STEP = ("rule_checks_status=error: 这次代码查规则没跑成(存档不受影响), 不能跟用户说规则都用代码查过了。")
UNCHECKED_LABEL = {"hard": "硬约束·没查完", "soft": "建议·没查完"}
RULE_CHECKS_UNCHECKED_NEXT_STEP = ("rule_checks_unchecked 里的规则这次代码没查完(正则没在时限内跑完、限时的引擎跑不了它, "
                                   "或者存的 check 用不了; 存档不受影响): 从 from_index 那篇起算没查 —— 不是查出了违规, 也不能跟用户说这几条守住了; "
                                   "它之前查出来、列在 rule_checks 里的命中照常算。severity 是 hard 的(「硬约束·没查完」)照规则原文"
                                   "(rule)把 from_index 起的那几篇自己逐篇核一遍。")


# ── 改稿重存时把原来那一整批拼回来(2026-10-30 发布收尾) ─────────────────────────────────────
# 话题、评论、方向不进 versions(那里只有标题、正文), 原来服务端拼不回上一次的稿子: 改稿重存的那一次只拼改过的几篇, 叫模型
# 自己按顺序替换原来那几行 —— 和同一句里的「原样交给用户」打架。现在 score_drafts 存档时把这三样连同这一批的顺序记进 batch 的
# params(jsonb, 键 DELIVERY_PARTS_KEY, 不加迁移): ``{"order": [每篇第一版的 version_id, 按写手传的顺序], "parts": {version_id:
# {tags / comments / direction 里写手传了的那几样}}}``; 改稿挂上旧 item 时, 新的那一版的三样并进旧 batch 记的那一份。只在项目
# 配了交付模板时记(没模板就没有交付稿可拼)。这之前存的 batch、补录的、底稿转正的都没有这一份: 改它们的稿子时照旧只拼改过的几篇,
# 回包里 delivery.scope = this_call、scope_fallback 说为什么。
DELIVERY_PARTS_KEY = "delivery_parts"
DELIVERY_FIELDS = ("tags", "comments", "direction")
DELIVERY_SCOPE_WHOLE = "whole_batch"
DELIVERY_SCOPE_THIS_CALL = "this_call"


def _delivery_fields(d: dict) -> dict:
    """一篇稿子交付时要、又不进 versions 的那几样。写手传了哪样记哪样(传了 ``tags: []`` 和没传拼出来不一样, 见
    delivery.assemble)。过一遍 JSON: 记不下的值按字符串记 —— 宁可那一格拼得不对, 也不能让建 batch 那一次写失败。"""
    picked = {k: d[k] for k in DELIVERY_FIELDS if isinstance(d, dict) and k in d}
    return json.loads(json.dumps(picked, ensure_ascii=False, default=str))


def _wants_delivery_parts(project: dict) -> bool:
    """项目配了能用的交付模板才记交付用的那几样。模板坏了不记: 拼交付稿那一步自己会记日志。"""
    try:
        return delivery.template_of(project) is not None
    except Exception:                                   # noqa: BLE001
        return False


def _record_revised_parts(client, project_id: str, revised: dict[int, str], drafts: list[dict],
                          minted_ids: dict[int, str]) -> None:
    """改稿挂上旧 item 的那几篇: 新的那一版的话题 / 评论 / 方向并进旧 batch 记的那一份(调用方持项目写锁, 同一 batch 不会
    并发改)。旧 batch 没记过的不补: 没有当初的顺序, 补了也拼不回整批。**不抛**: 记不上只是下次改同一批时退回只拼改过的几篇。"""
    try:
        of_item = store.batch_ids_of_items(client, sorted(set(revised.values())))
        by_batch: dict[str, dict] = {}
        for i, item_id in revised.items():
            if of_item.get(item_id):
                by_batch.setdefault(of_item[item_id], {})[minted_ids[i]] = _delivery_fields(drafts[i])
        params = store.batch_params(client, list(by_batch))
        for bid, parts in by_batch.items():
            rec = (params.get(bid) or {}).get(DELIVERY_PARTS_KEY)
            if isinstance(rec, dict) and isinstance(rec.get("parts"), dict):
                store.merge_batch_params(client, bid, {DELIVERY_PARTS_KEY: {**rec, "parts": {**rec["parts"], **parts}}})
    except Exception:                                   # noqa: BLE001 — 增强项, 存档已经成了
        logger.warning("commit_drafts: 改稿的话题 / 评论 / 方向没记进原来那一批(project=%s), 下次改这一批时只拼改过的几篇",
                       project_id, exc_info=True)


def _whole_batch_delivery(client, project_id: str, tpl: dict, drafts: list[dict], indices: list[int],
                          revised: dict[int, str]) -> tuple[dict | None, str | None, dict]:
    """改稿重存时拼原来那一整批。改稿挂的每个旧 batch 一段(按 batch 建的先后; 一个表格模板就是一张表), 段里按当初的顺序:
    没改的几篇用它们现在的那一版(标题正文从 versions 取, 话题 / 评论 / 方向从 batch 记的那一份取), 这次改过的换在原位。
    这次另有新稿的, 拼成最后一段(它们是新的一批)。``problems`` 只报这次传的那几篇(``index`` 是写手这次传的位置): 没改的
    几篇上一次交付时已经报过, 这一次也改不了它们。

    回 ``(delivery, None, meta)``; 拼不回来回 ``(None, 原因, {})``, 原因是机器字段 ``scope_fallback`` 的值:
      · ``batch_unknown``: 旧 item 找不到所在的 batch, 或者那个 batch 不在本项目;
      · ``batch_not_recorded``: 旧 batch 没记交付用的那几样(这之前存的、补录的、底稿转正的), 或者某一篇现在那一版没记上;
      · ``batch_changed``: 旧 batch 里的稿子和记下的顺序对不上。
    查库出错上抛, 调用方退回只拼这次的几篇(``lookup_failed``)。"""
    items = sorted(set(revised.values()))
    of_item = store.batch_ids_of_items(client, items)
    if any(it not in of_item for it in items):
        return None, "batch_unknown", {}
    first_at: dict[str, int] = {}                       # batch → 这次传的稿子里最先挂到它上面的下标(同一时刻建的按它排)
    for i in sorted(revised):
        first_at.setdefault(of_item[revised[i]], i)
    info = store.batch_drafts(client, project_id, list(first_at))
    if set(info) != set(first_at):
        return None, "batch_unknown", {}
    at_item = {item_id: i for i, item_id in revised.items()}
    groups: list[tuple[list[dict], list[int | None]]] = []
    untouched = 0
    for bid in sorted(first_at, key=lambda b: (str(info[b].get("created_at") or ""), first_at[b])):
        rec = info[bid]["params"].get(DELIVERY_PARTS_KEY)
        if not (isinstance(rec, dict) and isinstance(rec.get("order"), list) and isinstance(rec.get("parts"), dict)):
            return None, "batch_not_recorded", {}
        by_version = {v: it for it in info[bid]["items"] for v in it["version_ids"]}
        placed, seen = [], set()
        for vid in rec["order"]:
            it = by_version.get(str(vid))
            if it is not None and it["item_id"] not in seen:     # 当初没建成的跳过
                seen.add(it["item_id"])
                placed.append(it)
        if len(seen) != len(info[bid]["items"]):
            return None, "batch_changed", {}
        g_drafts: list[dict] = []
        g_idx: list[int | None] = []
        for it in placed:
            i = at_item.get(it["item_id"])
            if i is not None:
                g_drafts.append(drafts[i])
                g_idx.append(i)
                continue
            cur = it.get("current")
            part = rec["parts"].get(cur["id"]) if cur else None
            if not isinstance(part, dict):
                return None, "batch_not_recorded", {}
            g_drafts.append({**part, "title": cur["title"], "body": cur["body"]})
            g_idx.append(None)
            untouched += 1
        groups.append((g_drafts, g_idx))
    batches = len(groups)
    fresh = [i for i in indices if i not in revised]
    if fresh:
        groups.append(([drafts[i] for i in fresh], list(fresh)))
    texts: list[str] = []
    problems: list[dict] = []
    fmt, n = tpl.get("layout"), 0
    for g_drafts, g_idx in groups:
        res = delivery.assemble(tpl, g_drafts, indices=g_idx)
        fmt = res["format"]
        texts.append(res["text"])
        n += len(g_drafts)
        problems += [p for p in res["problems"] if p.get("index") is not None]
    problems.sort(key=lambda p: p["index"])             # 稳定排序: 同一篇的几条保持原来的先后
    return ({"format": fmt, "text": "\n\n".join(texts), "problems": problems, "scope": DELIVERY_SCOPE_WHOLE,
             "batches": batches, "untouched": untouched},
            None, {"drafts": n, "batches": batches, "untouched": untouched})


def _as_delivered(tpl: dict | None, drafts: list[dict]) -> tuple[list[dict], list[list[str]] | None]:
    """查规则看的那几篇 → (稿子, 每篇交付出去的话题; 没有模板时是 None)。score_drafts 的 rule_checks 和
    check_rules(_delivered_text: 代码查的、发给 Jev 的)都只从这里拿, 两边看见的标题、正文一字不差; 话题只在写手没传
    ``tags`` 时一样(check_rules 读的版本行里没存写手传的 tags, 见 _delivered_text)。

    有模板时每篇换成 ``delivery.delivered_draft`` 交付出去的那一份, 和 delivery.text 对得上: 模板管话题的, 正文摘掉末尾话题,
    话题是它拼的那一行(写手的 + 模板的固定 / 条件话题 + 正文中间留下的 #话题); 不管话题的, ``tags`` 字段不算、只认正文里的
    #话题; ``comments`` 是 null 的评论不算(codex review on aw#115)。没有模板原样, 话题由 rulecheck._parts 照 split_tags +
    body_tags 拼。哪个 # 算话题(validator.topic_tag_matches)、哪几条评论算数(delivery.comment_list)也都只在 delivery /
    validator 认一处, 这里不另写。"""
    if tpl is None:
        return list(drafts), None
    views = [delivery.delivered_draft(tpl, d) for d in drafts]
    return [view for view, _tags in views], [tags for _view, tags in views]


def _delivery_and_rule_checks(client, project: dict, project_id: str, user_id, drafts: list[dict],
                              indices: list[int], out: dict, *, tactic: str = "",
                              revised: dict[int, str] | None = None) -> dict:
    """存完档之后的两样纯代码活(2026-10-20), 结果挂进 ``out``, 返回给 batch_metrics 的那一小块。**不抛**。

    1. 交付版式: 项目配了 ``default_params.delivery`` 就按模板把存进去的那几篇拼好(``out["delivery"]``)。
       模板坏了只记日志、不带这一块 —— 打分照常(运维用 CLI 设模板时已经校验过, 走到这里多半是有人手改了库)。
       ``revised``(2026-10-30): 这次改稿重存、真存进去了的那几篇 ``{第几篇: 旧 item}``。有它就拼原来那一整批
       (_whole_batch_delivery, ``delivery.scope = "whole_batch"``); 拼不回来就照旧只拼这次的几篇, ``scope = "this_call"``,
       ``scope_fallback`` 说为什么。没有改稿的批次回包一个字不变(不带 scope)。
    2. 代码查规则: 和简报同一个池子里带 check 的那几条, soft 的过同一道方向闸, 逐篇查(``out["rule_checks"]``,
       有命中才带)。查不成就带 ``rule_checks_status = "error"``: 不带的话和"查过了、没命中"长得一模一样。正则规则限时
       (rulecheck.check), 没在时限内查完的、存的 check 用不了的都列进 ``out["rule_checks_unchecked"]``, 理由同上: 不列就成了
       "守住了"。

    两样都只是参考: 入库到这里已经成功了, 这一步任何失败都不许把它变成失败(同判定那一段的纪律)。
    """
    meta: dict = {}
    picked = [drafts[i] for i in indices]
    tpl = None
    try:
        tpl = delivery.template_of(project)
    except Exception as exc:                            # noqa: BLE001 — 增强项, 绝不冒泡
        logger.warning("commit_drafts: 项目 %s 的交付模板用不了, 这次不拼(%s)", project_id, exc)
        meta["delivery"] = {"error": str(exc)[:200]}
    if tpl is not None and picked:
        try:
            res, why, extra = None, None, {}
            if revised:
                try:
                    res, why, extra = _whole_batch_delivery(client, project_id, tpl, drafts, indices, revised)
                except Exception:                       # noqa: BLE001 — 拼不回整批就只拼这次的, 存档不受影响
                    logger.warning("commit_drafts: 改稿重存时拼回原来那一批失败(project=%s), 这次只拼存进去的几篇",
                                   project_id, exc_info=True)
                    res, why, extra = None, "lookup_failed", {}
            if res is None:
                res = delivery.assemble(tpl, picked, indices=indices)
                extra = {"drafts": len(picked)}
                if revised:
                    logger.warning("commit_drafts: 改稿重存时没拼回原来那一批(%s, project=%s), 这次只拼存进去的几篇",
                                   why, project_id)
                    res.update(scope=DELIVERY_SCOPE_THIS_CALL, scope_fallback=why)
                    extra["scope_fallback"] = why
            out["delivery"] = res
            meta["delivery"] = {"layout": res["format"], "drafts": extra.pop("drafts"), "problems": len(res["problems"])}
            if revised:
                meta["delivery"].update(scope=res.get("scope"), **extra)
        except Exception as exc:                        # noqa: BLE001
            logger.exception("commit_drafts: 拼交付稿失败(project=%s) —— 存档不受影响", project_id)
            meta["delivery"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    if picked:
        t0 = time.monotonic()
        try:
            hard, soft = store.shared_memories(client, project_id, user_id, rule_kinds=rulecheck.CHECK_KINDS)
            # 方向闸: 同简报(build_writing_brief)和 my_rules 的「方向不符」。标了另一个方向的 soft 规则简报里没有, 这里
            # 再报「建议改」就是在叫模型写它没被要求、甚至相反的东西(产品向的"带上产品名"报到流量向项目上)。
            # hard 不过这道闸, 同简报。判据也同简报: 项目名 + 这场 open_project 传的 tactic(写手原样传回来, codex review
            # on aw#115) —— 只按项目名判的话, 项目名不带方向、靠 tactic 判出方向的那场, 简报挡掉的规则这里又全报一遍。
            soft, _dropped = filter_by_applicability(soft, detect_direction(project.get("name") or "", tactic or ""))
            # 有交付模板时, 查的是交付出去的那一份, 免得和 delivery.text 对不上(delivery.delivered_draft): 模板管话题的, 话题含
            # 固定 / 条件话题、正文是摘掉末尾那行话题之后的; 不管话题的, tags 字段不算、只认正文里的 #话题; comments 是 null 的,
            # 评论不算(codex review on aw#115: 拿原稿查的话, 不交付的话题 / 评论里的词照报违规, 必须带的话术也能靠它们混过去)。
            # check_rules 也走 _as_delivered 这一处, 两边看见的是同一份标题、正文(话题见 _delivered_text)
            checked, final_tags = _as_delivered(tpl, picked)
            # 模板要求每篇都带的部位(评论 min ≥ 1、要话题): 空着的话「必须带的话术」照没带报, 不能靠不交那一块混过去(codex review
            # on aw#115)。没模板、模板不要求的部位, 空着照旧不查(评论常常是后面另走评论那一步写的)
            expects = delivery.expected_parts(tpl) if tpl is not None else ()
            hits, stats = rulecheck.check(hard + soft, checked, indices=indices, tags=final_tags, expects=expects)
            if hits:
                shown, more = rulecheck.top_hits(hits)
                out["rule_checks"] = shown
                if more:
                    out["rule_checks_more"] = more
            if stats.get("timed_out") or stats.get("skipped_ids"):
                rows = {r.get("id"): r for r in hard + soft}
                sev = {rid: ("hard" if (r.get("severity") or "soft").lower() == "hard" else "soft") for rid, r in rows.items()}
                # 开头不用「必须改」/「建议改」(codex review on aw#115): 那两个词在 rule_checks 里是"这篇查出了违规", 写手的模型扫到
                # 「必须改」会跟用户说稿子违规了 —— 这里什么都没查出来, 只是没查完。轻重只放在 severity 里, 开头另起一个说法。
                # 带上规则原文(独立复审 on aw#115 第六轮): 简报里只有原文没有 id, 只给 rule_id 写手对不上是哪一条, 没法照原文核
                # 从哪篇起没查完(独立复审 on aw#115 第六轮): 前面查出来的命中是真的, 不说清楚的话同一条既是「必须改」又是"算没查"
                found = {h["rule_id"] for h in hits}
                start = stats.get("timed_out_from") or {}
                out["rule_checks_unchecked"] = [
                    {"rule_id": rid, "severity": sev.get(rid, "soft"), "rule": (rows.get(rid) or {}).get("content") or "",
                     "from_index": start.get(rid),
                     "what": f"{UNCHECKED_LABEL[sev.get(rid, 'soft')]}: 这条规则的正则从 index={start.get(rid)} 那篇起没查完"
                             "(超时或者限时的引擎跑不了), 那几篇算没查, 不是查出了违规"
                             + ("; 之前查出来、列在 rule_checks 里的照改" if rid in found else "")}
                    for rid in stats.get("timed_out") or ()]
                # 存的 check 用不了的(写坏了, 或者是这里不查的老写法; rulecheck 记日志、跳过): 原来只在 batch_metrics 里记个数,
                # 回包和「查过了、没命中」一模一样, 一条硬规则就这样每批都没查(codex review on aw#115)。整批都没查, 照没查完报
                out["rule_checks_unchecked"] += [
                    {"rule_id": rid, "severity": sev.get(rid, "soft"), "rule": (rows.get(rid) or {}).get("content") or "",
                     "from_index": indices[0],
                     "what": f"{UNCHECKED_LABEL[sev.get(rid, 'soft')]}: 这条规则存的代码检查用不了(写坏了, 或者是这里不查的"
                             "老写法), 这次整批都没查, 不是查出了违规"}
                    for rid in stats.get("skipped_ids") or ()]
            meta["rule_checks"] = {**stats, "hits": len(hits),
                                   "hard": sum(1 for h in hits if h["severity"] == "hard"),
                                   "elapsed_ms": _ms_since(t0)}
        except Exception as exc:                        # noqa: BLE001
            logger.exception("commit_drafts: 代码查规则失败(project=%s) —— 存档不受影响", project_id)
            meta["rule_checks"] = {"error": f"{type(exc).__name__}: {exc}"[:200], "elapsed_ms": _ms_since(t0)}
            out["rule_checks_status"] = "error"

    tips = (([DELIVERY_NEXT_STEP] if "delivery" in out else [])
            + ([DELIVERY_PARTIAL_NEXT_STEP] if (out.get("delivery") or {}).get("scope") == DELIVERY_SCOPE_THIS_CALL else [])
            + ([RULE_CHECKS_NEXT_STEP] if "rule_checks" in out else [])
            + ([RULE_CHECKS_MORE_NEXT_STEP] if "rule_checks_more" in out else [])
            + ([RULE_CHECKS_UNCHECKED_NEXT_STEP] if "rule_checks_unchecked" in out else [])
            + ([RULE_CHECKS_ERROR_NEXT_STEP] if out.get("rule_checks_status") == "error" else []))
    if tips and out.get("next_step"):
        out["next_step"] = out["next_step"] + " " + " ".join(tips)
    return meta


def commit_drafts(client, project_id: str, drafts: list[dict],
                  *, user_id: str | None = None, flywheel_arm: str | None = None, tactic: str = "") -> dict:
    """定稿入库: 写指纹(同一事务内重查) + 给坐标销账。

    只收真正定稿的 —— 指纹库脏了(把废稿也记进去)会让后续正常选题被误杀。

    ⚠️ 入库【会再查一次重】。check_drafts 和本调用之间可能有别人先 commit 了
    撞车的稿子(两人各自 check 时看到的是同一份旧指纹集), 那条竞态窗口只能在
    写入的同一个事务里关掉。被判撞车的条目【不入库】, 在返回值的 rejected 里
    列出来, 调用方要让用户重写。

    ── 2026-08-25: 入库的稿子现在**有身份** ────────────────────────────
    真的入了库的那几条会同时建出 ``batches`` / ``items`` / ``versions`` 行,
    返回值带 ``batch_id`` 和 ``version_ids``。在此之前写作台的稿子只有指纹、
    没有 version_id, 于是:

      · 回填(走 items × versions)看不到它们, 欠费期间缺的向量补不回来;
      · 导出的 lineage 没有 version_id 可写, 而 Truth Vault 的
        ``v_model_comparison`` 正是 JOIN 在 ``autowriter.versions.id`` 上 ——
        "写作台写的稿子发出去爆没爆"在数据上根本问不出来。

    ⚠️ 建出来的 item 是 ``pending``、**不盖任何决策戳**。理由见
    ``store.DESKCORE_ITEM_STATUS`` —— 标 approved 会让每条定稿变成一条伪造的
    人工评价灌进 TV 的评估模型。

    ── 2026-09-18: 交付即入库 + 改稿替换 ─────────────────────────────
    协议改成"交付的同一轮里入库, 不等用户说定稿"(近一周 214 个角度只有 20 条
    走到这里, 漏的全是那道人工门)。配套的是改稿: 用户让改某一条时, 新稿会撞上
    它自己旧版的指纹。带 ``replaces_version_id`` 的稿子走替换: 先把旧 item 全部
    版本的指纹摘下来(留副本), 新稿走**同一套**闸和原子写入; 入了库就在旧 item 上
    加一版(version_num+1, best_version_id 指过去); 被拒或半途异常就把指纹原样
    放回。不销角度(旧版已经销过)、不计 unattributed。

    ── 2026-09-24: 入库判定(judge) ───────────────────────────────────────
    真的建成了 versions 行的那几篇, 入库完成、**项目写锁放掉之后**逐篇发给 judge
    (``judge_client``), 结局挂在返回值的 ``judge`` 上, 答案由 judge 写进 TV 账本。
    不拦: ``written`` / ``rejected`` / ``version_ids`` 的口径一个字不变,
    判定失败、超时、没配, 入库照样成功。返回值另带 ``elapsed_ms`` / ``phase_ms``,
    同一份数落进 ``batch_metrics``(meta.mode='deskcore_commit')攒样本。
    覆盖面与位置的理由见 docs/deskcore.md §3.8。

    ── 2026-10-19: 存档 + 参考分(D-120) ─────────────────────────────────
    写手不再"调 commit_drafts": 模型写完自动调 ``score_drafts``(本函数的新入口名), 存档与打分一次做完。
    判定结局不再是影子 —— ``judge.results`` 每篇带 ``score`` / ``fixes``(``draft_score``), 给写手看,
    仍然不拦。人审(review_drafts)和导出(export_drafts)两个工具撤了: 定稿由写手贴进飞书, 发布后
    tv-sync 按内容对回来。

    ``tactic``(2026-10-20): 这场 open_project 传的 tactic, 只给代码查规则的方向闸用(同 build_writing_brief 的判据)。

    ── 2026-10-27: 存档时核对 key 和臂(W-31) ─────────────────────────────
    带了 angle_key 却没销成的, 按 key 再查一次台账, 分出「这个项目台账里没有」(``unknown_angle_keys``; 形状不对的、别的
    项目的牌、哪个台账都没有的, 见 _angle_key_problems)和「已经销给了别的稿子」(``used_angle_keys``), 和 ``unattributed``
    分开数, 哪几篇、哪一种在 ``angle_key_mismatch``, 怎么办在 ``angle_key_warning``; 同一笔记进进程内存, 下一次
    archive_pending 也带上。模型传回的 flywheel_arm 对不上服务端发的时以服务端为准(``flywheel_arm_corrected``), 重启过
    核对不了的另标(``flywheel_arm_unverified``), 见 batch_arm。被拒的按原因计数进 batch_metrics.meta(``_rejection_meta``)。
    docs/deskcore.md §3.12。
    """
    started = time.monotonic()
    # W-21(10-30): commit 段(入库段)按步计时, 落进 batch_metrics.meta.commit_parts_ms, 不进 phase_ms(那里各段加起来等于
    # total, 测试和查法都靠这一条)。从 started 起一段接一段量, 等锁那段不算(单列在 lock_wait)。
    commit_parts: dict[str, int] = {}
    # 审计 COR-015。项目整行留着: 交付模板(default_params.delivery)就在这一行里, 不再多查一次(10-20)
    project = assert_project_access(client, project_id, user_id=user_id)
    if not drafts:
        return {"written": 0, "consumed_angles": 0, "rejected": []}
    _lap(commit_parts, "access", started)
    arm_meta: dict = {}      # 这次入库的臂 + 改稿挂到哪些旧 item, 进 batch_metrics.meta (分析对照组的第二份记录)

    # 全程持项目写锁(codex #84 P1): 替换要先摘旧指纹再查重写入, 两步之间别的 commit /
    # 补录不能插进来。没有替换的普通 commit 也拿 —— 它们正是会在那个窗口里把与旧稿
    # 相同的文字塞进来的一方, 只锁一边等于没锁。
    holder = f"commit:{uuid.uuid4().hex[:8]}"
    lock_asked = time.monotonic()
    with _project_write_lock(client, project_id, holder):
        lock_wait_ms = _ms_since(lock_asked)
        lap = time.monotonic()
        # 正文空的替换稿不摘旧指纹(codex review on aw#126): 它反正要被拒(W-30, 见下), 先摘了的话同批里和旧版
        # 重复的另一篇查重时看不见旧版, 照样入库。
        empty_idx = {i for i, d in enumerate(drafts) if not str(d.get("body") or "").strip()}
        replace, replace_rejected, stash = _prepare_replacements(
            client, project_id, [{**d, "replaces_version_id": None} if i in empty_idx else d
                                 for i, d in enumerate(drafts)])
        lap = _lap(commit_parts, "replace_prep", lap)
        # 整段包在 try 里(而不是拆成子函数): ci.yml 用 inspect.getsource 守着"commit 走 _gate"
        # 和"commit 记 embedding_model"这两条, 拆出去它们就看不见了。
        try:
            titles = [(d.get("title") or "").strip() for d in drafts]
            # ⚠️ embeddings_available() 判的是【客户端对象能不能建起来】(有 key + SDK
            # 装了), 不是调用能不能成功, 而且那个 client 是进程级单例、启动时就缓存了。
            # 所以 key 欠费/被封/配额用尽之后它照样返回 True, 真正失败的是下面这次
            # embed_texts —— 它 catch 住异常返回 None。
            # 这两者必须分开记: configured 为真而 vecs 为空 = 【本该有向量却没拿到】,
            # 那是故障不是"没配"。不区分的话, 欠费那几天入库的稿子会安静地只有确定性
            # 指纹, 查重从此对那批内容有个洞, 不报错、也没人知道。
            #
            # (这里原来还写着"而且补不回来 —— backfill 走 items×versions, WorkBuddy
            #  写的稿子根本不在 autowriter.versions 里"。下面建身份那一步之后不再成立:
            #  这些稿子现在有真的 versions 行, backfill 扫得到。)
            embed_configured = dedup.embeddings_available()
            vecs = dedup.embed_texts(titles) if embed_configured else None
            embed_failed = embed_configured and not vecs
            bodies = [d.get("body") or "" for d in drafts]
            lap = _lap(commit_parts, "embed", lap)

            # ── 入库自带闸(2026-09-17)────────────────────────────────────────
            # 先跑一遍和 check_drafts **同一套**判定, 判 reject 的不进 RPC。理由见
            # _gate 的说明: 闸不该依赖模型记得先调 check_drafts。这里多一次历史比对
            # (一次 RPC), 换来的是"跳过 check 直接 commit"这条路从此进不了重复的稿子。
            # 出错照样上抛 —— 和 check_drafts 一样, 查重挂了不能当作通过。
            # 正文空的不存档(W-30, 10-26): 10-21 / 10-22 Hatherine 两批 15 篇只有标题、body 是空的, 照样写了指纹、
            # 销了角度、建了 versions —— 判定按 empty_body 跳过, 指纹没有开头哈希和四字串, 查重看不见, 发出去也对不回来。
            # 多半是模型把正文放进了别的字段: 拒掉, 说清收到了哪些字段, 让它当场补上重调。替换稿的旧指纹上面就没摘。
            empty_rejected = {i: _empty_body_reason(drafts[i]) for i in sorted(empty_idx)}
            # 空正文那几篇也不进闸(codex review on aw#126): 本批内互比会拿标题向量把后面同标题的完整稿判成撞上它,
            # 结果两篇都没存。闸只跑其余几篇, 结果下标翻回调用方的; 全是空正文时照旧整批跑(结果反正都被拒掉)。
            gate_idx = [i for i in range(len(drafts)) if i not in empty_rejected] or list(range(len(drafts)))
            gate = _gate(client, project_id, [titles[i] for i in gate_idx], [bodies[i] for i in gate_idx],
                         [vecs[i] if i < len(vecs) else None for i in gate_idx] if vecs else vecs)
            pre_rejected = {}
            for r in gate["results"]:
                i = gate_idx[r["index"]]
                if r.get("status") == "reject" and i not in replace_rejected and i not in empty_rejected:
                    pre_rejected[i] = {**r, "index": i}
            survivors = [i for i in range(len(drafts))
                         if i not in pre_rejected and i not in replace_rejected and i not in empty_rejected]
            lap = _lap(commit_parts, "gate", lap)

            # ── 先把 version_id 定下来 ──────────────────────────────────────────
            # 顺序是有讲究的: **先造 id 并写进指纹, 事后才建 versions 行**。
            #
            # 反过来(先建 versions 再写指纹)的话, 被查重判撞车的那几条会留下没有指纹的
            # 孤儿 item —— 它们会出现在审核页、进正例池、被回填扫到, 而对应的稿子其实
            # 根本没有交付。
            #
            # 现在这个顺序的失败模式是另一头: 指纹写进去了、身份没建成, 于是指纹的
            # version_id 指向一个不存在的行。那只是**退回到今天的状态**(lineage 断掉),
            # 查重一点不受影响 —— 而且下面会明说。两个方向的坏, 这个可逆。
            #
            # 调用方已经带了 version_id 的(稿子是 UI 生成的, 库里本来就有那一版)照旧用
            # 它自己的, 不重新造、也不会再建一遍身份。
            minted_ids: dict[int, str] = {}
            for i, d in enumerate(drafts):
                if not d.get("version_id"):
                    minted_ids[i] = str(uuid.uuid4())

            rows = []
            for i in survivors:
                d = drafts[i]
                body = bodies[i]
                emb = vecs[i] if vecs and i < len(vecs) else None
                rows.append({
                    "version_id": d.get("version_id") or minted_ids.get(i),
                    "title": titles[i],
                    "opening": fp.opening_of(body),
                    # RPC 侧按 text 转 vector, 这里给 pgvector 的字面量形式
                    "title_embedding": ("[" + ",".join(repr(float(x)) for x in emb) + "]") if emb else None,
                    "embedding_model": dedup.EMBEDDING_MODEL if emb else None,
                    "opening_hash": fp.opening_hash(body),
                    "ngram_hashes": fp.ngram_hashes(body),
                    "angle_key": d.get("angle_key"),
                })

            # 闸前就判死的那几条: 说清是哪一路信号、撞的是本批内还是历史。
            rejected: list[dict] = [
                {"index": i, "title": titles[i],
                 "collided_with": r.get("collided_with") or "",
                 "collided_scope": r.get("collided_scope") or "",
                 "decided_by": r.get("decided_by"),
                 "reason": r.get("reason") or "入库前判定与已有稿件重复",
                 "gate": "pre_commit"}
                for i, r in sorted(pre_rejected.items())]
            rejected += [{"index": i, "title": titles[i], "collided_with": "", "collided_scope": "",
                          "decided_by": None, "reason": why, "gate": "replace_lookup"}
                         for i, why in sorted(replace_rejected.items())]
            rejected += [{"index": i, "title": titles[i], "collided_with": "", "collided_scope": "",
                          "decided_by": None, "reason": why, "gate": "empty_body"}
                         for i, why in sorted(empty_rejected.items())]
            rejected.sort(key=lambda r: r["index"])
            # ⚠️ RPC 回的 idx 是**幸存者列表里的下标**, 不是调用方列表的。下面凡是把
            #    idx 翻回调用方视角的地方都要过 survivors[...]。
            if survivors:
                outcome = store.commit_fingerprints_atomic(
                    client, project_id, rows, user_id, fp.NGRAM_JACCARD_HARD,
                    # 阈值只在 fingerprint.py 里定义一处 —— SQL 里的 DEFAULT 只是兜底,
                    # 真正生效的是这里传下去的值。两边写死两份就迟早对不上。(审计 COR-014)
                    contain_hard=fp.NGRAM_CONTAIN_HARD,
                    contain_min_sample=fp.CONTAIN_MIN_SAMPLE)
            else:
                outcome = []          # 全被闸前拦下, 没东西可写, 也别去碰 RPC

            atomic = outcome is not None
            rpc_anomalies = 0
            if atomic:
                written = sum(1 for o in outcome
                              if o.get("status") == COMMIT_STATUS_INSERTED)
                # ⚠️ idx 越界的回执**不许抛**: 指纹这时已经写进去了, 抛出去调用方会重试,
                #    重试会撞上自己刚写的指纹 —— 一次故障变成一句"你的稿子重复了"。
                #    记日志、数出来、报给调用方, 但不中断。(code review 2026-09-17)
                valid = []
                for o in outcome:
                    idx = o.get("idx")
                    if isinstance(idx, int) and 0 <= idx < len(survivors):
                        valid.append(o)
                    else:
                        rpc_anomalies += 1
                        logger.error("commit_drafts: RPC 回执 idx=%r 越界(幸存者 %d 条, "
                                     "project=%s) —— 跳过这一行", idx, len(survivors), project_id)
                outcome = valid
                for o in outcome:
                    if o.get("status") == "rejected":
                        orig = survivors[o["idx"]]
                        rejected.append({
                            "index": orig,
                            "title": titles[orig],
                            "collided_with": o.get("collided_with") or "",
                            "collided_scope": "历史",
                            "reason": o.get("detail") or "与库中已有稿件重复",
                            "gate": "atomic_recheck",
                        })
                rejected.sort(key=lambda r: r["index"])
            else:
                # RPC 不在: 降级直插, 并明确标出这次没有关掉竞态窗口。
                payload = []
                for k, r in enumerate(rows):
                    orig = survivors[k]
                    payload.append({
                        "project_id": project_id, "user_id": user_id,
                        "version_id": r["version_id"], "title": r["title"],
                        "opening": r["opening"],
                        "title_embedding": vecs[orig] if vecs and orig < len(vecs) else None,
                        "embedding_model": r["embedding_model"],
                        "opening_hash": r["opening_hash"],
                        "ngram_hashes": r["ngram_hashes"], "angle_key": r["angle_key"],
                    })
                written = store.write_fingerprints(client, payload)
            lap = _lap(commit_parts, "fingerprints", lap)

            # 只给真的入了库的坐标销账 —— 被拒的那条角度还没产出成稿, 不该占坑。
            inserted_idx = ({survivors[o["idx"]] for o in outcome
                             if o.get("status") == COMMIT_STATUS_INSERTED}
                            if atomic else set(survivors))

            # ── 给真的入了库的那几条建身份 ──────────────────────────────────────
            # 只建 inserted 的: 被判撞车的那几条没有交付, 不该在 items 里留一行。
            # mint_draft_identity 不抛: 指纹已经进库了, 这次 commit 的**主要目的**(让这些
            # 稿子参与以后的查重)已经达成。把整个调用报成失败会让调用方去重试, 而重试会被
            # 自己刚写进去的指纹判成撞车 —— 一次故障变成一句"你的稿子重复了", 现场完全对
            # 不上。它半途失败时会把**已经建成的那部分**连同 error 一起回来。
            # 替换的稿子没入库(闸前被拒 / 原子重查撞车): 旧指纹放回去, 旧稿照旧参与查重。
            _restore_stash(client, stash, [i for i in replace if i not in inserted_idx])
            lap = _lap(commit_parts, "restore", lap)

            # 替换成功的: 不建新 item, 在旧 item 上加一版。半途失败与建身份同一口径 ——
            # 指纹已进库, 主要目的达成, 不抛, 说清哪几条没接上。
            replaced_ok: dict[int, str] = {}
            replace_error: str | None = None
            for i in sorted(inserted_idx):
                if i not in replace:
                    continue
                ctx = replace[i]
                try:
                    store.add_version_to_item(
                        client, ctx["item_id"], version_id=minted_ids[i],
                        version_num=ctx["next_version_num"], title=titles[i], body=bodies[i],
                        keywords=drafts[i].get("keywords") or [])
                    store.point_item_at_version(
                        client, ctx["item_id"], minted_ids[i],
                        reset_decision=(ctx.get("status") or "pending") != "pending")
                    replaced_ok[i] = ctx["item_id"]
                except Exception as exc:                    # noqa: BLE001
                    logger.exception("commit_drafts: 替换旧版失败(index=%d, item=%s)", i, ctx["item_id"])
                    replace_error = replace_error or f"{type(exc).__name__}: {exc}"
            # 改稿重存时要把原来那一整批拼回来(2026-10-30): 新的那一版的话题 / 评论 / 方向并进旧 batch 记的那一份。在锁里做
            # (读-并-写), 不抛
            keep_parts = _wants_delivery_parts(project)
            if keep_parts and replaced_ok:
                _record_revised_parts(client, project_id, replaced_ok, drafts, minted_ids)
            lap = _lap(commit_parts, "replace_versions", lap)

            minted = {"batch_id": None, "versions": {}, "error": None}
            to_mint = [
                {"version_id": minted_ids[i], "title": titles[i],
                 "body": drafts[i].get("body") or "",
                 "keywords": drafts[i].get("keywords") or []}
                for i in sorted(inserted_idx) if i in minted_ids and i not in replace
            ]
            # 这批落在哪个飞轮臂 (docs/28 §7.4): 这场写作借卡时定下的那个, 对照组靠它圈出来
            arm, arm_source, arm_check = batch_arm(project_id, user_id, from_brief=flywheel_arm)
            # 模型传回的臂认得出、却没被采用 = 服务端没给过这场这个臂 (corrected), 或者重启过核对不了 (unverified):
            # 都记服务端最近一次借卡的臂 (W-31, 见 batch_arm)。传回的那个留一份(batch.params + batch_metrics.meta),
            # 核对不了的另标 flywheel_arm_unverified, 分析对照组时剔掉; 回包里也说一句。改稿挂的旧 batch 同样标上
            # (codex review on aw#130: 旧 batch 正好是记下的那个臂时比不出 mixed, 可改稿可能是在另一个臂下写的)。
            # 没传臂、按入库时重算的 (recomputed) 也是猜的: 新 batch 靠 flywheel_arm_source 剔, 旧 batch 的来源是它
            # 自己的, 一样标 unverified。
            arm_reported = flywheel_arm if arm_check else None
            arm_flags = {**({"flywheel_arm_reported": arm_reported} if arm_reported else {}),
                         **({"flywheel_arm_unverified": True} if arm_check == "unverified" else {})}
            unmarked = _mark_mixed_arm_batches(
                client, list(replaced_ok.values()), arm,
                unverified=arm_check == "unverified" or arm_source == "recomputed") if replaced_ok else []
            arm_meta = {"flywheel_arm": arm, "flywheel_arm_source": arm_source, **arm_flags,
                        "revised_item_ids": sorted(set(replaced_ok.values())),
                        "arm_mixed_unmarked_item_ids": unmarked}
            lap = _lap(commit_parts, "arm", lap)
            if to_mint:
                # 有交付模板的项目, 这一批的顺序和每篇的话题 / 评论 / 方向跟着 batch 一起记下(DELIVERY_PARTS_KEY): 以后改其中
                # 几篇时服务端能把整批拼回来。和建 batch 是同一次写, 不多一次请求
                parts = ({DELIVERY_PARTS_KEY: {"order": [e["version_id"] for e in to_mint],
                                               "parts": {minted_ids[i]: _delivery_fields(drafts[i])
                                                         for i in sorted(inserted_idx)
                                                         if i in minted_ids and i not in replace}}}
                         if keep_parts else {})
                minted = store.mint_draft_identity(
                    client, project_id, str(user_id), "", to_mint,
                    batch_params={"source": "deskcore", "flywheel_arm": arm, "flywheel_arm_source": arm_source,
                                  **arm_flags, **parts})
            identity_error = minted.get("error")
            lap = _lap(commit_parts, "mint", lap)

            consumed = 0
            attempted = 0
            unminted_angles = 0      # 身份没建成、所以**没销账**的角度(见下面那段)
            missed_keys: dict[int, str] = {}     # 送去销账却没销成的: 下标 → key(W-31, 下面分清是不是 key 本身错了)
            for i, d in enumerate(drafts):
                if i not in inserted_idx or i in replace:   # 替换: 旧版入库时已经销过
                    continue
                key = d.get("angle_key")
                if not key:
                    continue
                # 台账销账用**真的** version_id。以前这里塞的是一个按 angle_key 哈希出来的
                # 假 UUID(见上面删掉的 _placeholder_version_id) —— consumed_version_id 非
                # NULL 就够避重用了, 但那个 id 指不到任何一行, 没法回答"这个角度产出的那篇
                # 后来怎么样了"。
                vid = d.get("version_id") or minted_ids[i]
                # 身份没建成的那几条(mint 半途失败)**不销账**(TV 审计 2026-10-08 B-20)。
                # 销了的话 consumed_version_id 指向一个不存在的 versions 行 —— 和当年的
                # 假 UUID 一模一样: 台账说"这个角度产出了一篇", 而那一篇谁也找不到, 回填 /
                # 导出 / v_model_comparison 全 JOIN 不到。留着不销, 这个坐标下一批还能被
                # 抽到、重写一次 —— 这次的指纹已经入库, 同题重写会被闸挡下来并说清楚, 那是
                # 看得见的; 悬空的 id 是看不见的。调用方自带 version_id 的不受影响。
                if not d.get("version_id") and vid not in (minted.get("versions") or {}):
                    unminted_angles += 1
                    continue
                attempted += 1
                # user_id 传下去: 同一坐标占坑过期后会被重新发牌, 台账里可能有两条
                # 未消耗行, 销账要销自己那一条(codex review on #86)。
                if store.consume_angle(client, project_id, key, vid, user_id=user_id):
                    consumed += 1
                else:
                    missed_keys[i] = str(key)
            lap = _lap(commit_parts, "consume", lap)

            # W-38: 存了档的稿子, 同样文字的查重底稿记成已转正 —— 它有版本了, 发出去以后主索引对得上, 底稿不用再等飞书。
            # 改稿替换真挂上了旧 item 的也算(codex on aw#132): 底稿留着, 这一版之后又被替换掉、主索引对不上它的时候, 会被另转一份
            _supersede_snapshots(client, project_id, drafts,
                                 {i: minted_ids[i] for i in sorted(inserted_idx)
                                  if i in replaced_ok
                                  or (i not in replace and minted_ids.get(i) in (minted.get("versions") or {}))})
            lap = _lap(commit_parts, "snapshots", lap)

            # 带了 key 却没销成的, 分清是不是 key 本身的问题(W-31, 10-26 sportsix: 19 篇带的 key 一条都不在台账里,
            # 回包只说「销账少了」, 写手那边的 AI 不知道 key 是错的, 20 张牌一天后回了池子)。只在有没销成的时候多查一次。
            key_problems, keys_classified = _angle_key_problems(client, project_id, missed_keys, user_id=user_id)
            key_problem_n: dict[str, int] = {}
            for kp in key_problems:
                key_problem_n[kp["problem"]] = key_problem_n.get(kp["problem"], 0) + 1
            unknown_keys = sum(key_problem_n.get(c, 0) for c in ANGLE_KEY_ABSENT)
            used_keys = key_problem_n.get(store.ANGLE_KEY_USED, 0)
            _note_angle_key_misses(project_id, user_id, key_problems)    # 下一次 archive_pending 也带上
            lap = _lap(commit_parts, "key_check", lap)

            # 入了库却没带 angle_key 的: 台账没法给它们销账, 同一个故事下一批还可能被
            # 抽到 —— 而那种重复(同题重写)指纹闸抓不到。途鸽 09-10 的 66 条里 49 条是
            # 这么进来的, 当天四个标题各入库两次。RPC 对空 angle_key 一声不吭地照收,
            # 所以只能在这里数出来说给调用方听。
            unattributed = sum(1 for i in inserted_idx
                               if i not in replace and not drafts[i].get("angle_key"))
            minted_ok = [minted_ids[i] for i in sorted(inserted_idx)
                         if i not in replace and minted_ids.get(i) in minted.get("versions", {})]
            out = {"written": written, "consumed_angles": consumed,
                   "unattributed": unattributed,
                   # 带了 angle_key、台账却对不上的(W-31): 这个项目的台账里没有这个 key(编的 / 别的项目的牌 / 台账
                   # 没写进去, 见 _angle_key_problems) / 这个 key 已经销给了别的稿子。和 unattributed(没带 key)分开数,
                   # 哪几篇、哪一种在 angle_key_mismatch 里
                   "unknown_angle_keys": unknown_keys, "used_angle_keys": used_keys,
                   "embedded": bool(vecs), "rejected": rejected,
                   "atomic_recheck": atomic,
                   "gate_summary": gate["summary"],
                   "embedding_model": dedup.EMBEDDING_MODEL if vecs else None,
                   # 这次建出来的身份。导出要用 version_id, 所以直接回给调用方 ——
                   # 不然它得再查一次才知道自己刚提交的稿子叫什么。
                   #
                   # ⚠️ 只报**真的建出了行**的那些(拿 minted["versions"] 过一遍),
                   #    不是"我本来打算用这些 id"。建失败时报出去的 id 会让调用方拿它
                   #    去导出, 而 TV 那边 JOIN 不到任何东西 —— 那比不报更坏。
                   "batch_id": minted.get("batch_id"),
                   # 按调用方的下标顺序: 协议让调用方按位置留下 version_id 以便日后改稿,
                   # 替换稿和新稿混在一批时不能把替换的都排到后面(codex #84 P2)。
                   "version_ids": [minted_ids[i] for i in sorted(inserted_idx)
                                   if (i in replaced_ok) or (minted_ids.get(i) in minted_ok)],
                   # 改稿替换的: 同一个 item 升了一版。version_ids 里也有新 id(导出用)。
                   "replaced": [{"index": i, "item_id": replaced_ok[i],
                                 "old_version_id": replace[i]["old_version_id"],
                                 "version_id": minted_ids[i]} for i in sorted(replaced_ok)]}
            if arm_meta.get("arm_mixed_unmarked_item_ids"):
                # 机器可读: 这几条改稿的旧 batch 没能标上 flywheel_arm_mixed / flywheel_arm_unverified, 分析对照组时要剔掉
                # (同一份也进 batch_metrics)
                out["flywheel_arm_warning"] = {"code": "arm_mixed_unmarked", "arm": arm_meta["flywheel_arm"],
                                               "item_ids": arm_meta["arm_mixed_unmarked_item_ids"]}
            if arm_check == "corrected":
                out["flywheel_arm_corrected"] = {
                    "reported": arm_reported, "recorded": arm,
                    "note": (f"你传回的 flywheel_arm 是 {arm_reported}, 但这场借卡时服务端分的是 {arm}"
                             + ("(没有给已验证规律)" if arm == "explore" else "")
                             + f", 这批按 {arm} 记。以后原样传回 open_project / borrow_lessons 返回里的 flywheel_arm, "
                               "不要自己填。")}
            elif arm_check == "unverified":
                # 重启过: 传回的可能是对的, 不能说「服务端分的是 X」(W-31 复审)。只说明这批怎么记的, 不叫模型改什么
                out["flywheel_arm_unverified"] = {
                    "reported": arm_reported, "recorded": arm,
                    "note": (f"服务端中途重启过, 核对不了这场借卡时分的是不是你传回的 {arm_reported}; 这批先按服务端重启后"
                             f"最近一次借卡的 {arm} 记, 标了 flywheel_arm_unverified, 分析对照组时会剔掉。你这边不用改, "
                             "照旧原样传回 open_project / borrow_lessons 返回里的 flywheel_arm。")}
            if key_problems:
                out["angle_key_mismatch"] = key_problems
            # ── 下面这几句 *_warning 是念给写手的(2026-10-30 发布收尾): 只说发生了什么、要不要重来; 异常原文、表名、
            #    补救命令一律进日志, 写手手里没有任何一步能补这些。
            if replace_error:
                missed = [i for i in sorted(inserted_idx) if i in replace and i not in replaced_ok]
                logger.warning("commit_drafts: %d 条替换稿的新指纹入了库, 但没能挂到旧稿上(%s), 下标 %s; 旧稿的 "
                               "best_version_id 还指着旧版 (project=%s)", len(missed), replace_error, missed, project_id)
                out["replace_warning"] = (
                    f"{len(missed)} 条改稿存进去了、查重照常, 但没能挂到旧稿上(第 {missed} 条)。"
                    "已记下, 不用重存, 不用你做什么。")
            if identity_error:
                # 说清楚**丢的是哪几条**: 查重没事, 断的是"发出去之后能不能归因回来"。
                # ⚠️ 报的是**实际没建成的条数**, 不是 len(to_mint) —— 半途失败时前面几条
                #    是真建成了的, 说"全都没建成"会让人去重做已经做完的事。
                done = len(minted_ok)
                logger.warning("commit_drafts: %d 条指纹都入了库, 只有 %d 条建成了 items/versions (batch=%s, "
                               "project=%s): %s —— 没建成的发出去之后 tv-sync 对不回这一版、只会当新稿补录; "
                               "带角度没销账的 %d 条", len(to_mint), done, out.get("batch_id"), project_id,
                               identity_error, unminted_angles)
                out["identity_warning"] = (
                    f"{len(to_mint)} 条稿子都存进了查重库, 但只有 {done} 条建成了底稿。查重不受影响; "
                    f"没建成的那 {len(to_mint) - done} 条不在 version_ids 里, 发出去以后写作台认不回这一版"
                    "(参考分、改动对照接不上)。"
                    + (f"已建成的那 {done} 条(batch {out['batch_id']})不受影响。" if done else "")
                    + (f"没建成的里 {unminted_angles} 条带着角度, 这几张牌**没销账**, 下一批可能还会抽到。"
                       if unminted_angles else "")
                    + "已记下, 不用重写, 不用你做什么。")
            if embed_failed and written:
                # 配了 embedding 却没拿到向量 = 故障(欠费/配额/网络), 不是"没配"。
                # 必须当场说, 让人决定是先修 key 再 commit, 还是接受这批只有确定性指纹。
                # (2026-08-25 起这几行**补得回来**了 —— 稿子有了 versions 行, backfill
                #  和 reembed 都扫得到。reembed 更直接, 指路只进日志: 那是工程侧的事, 写手做不了。)
                logger.warning("commit_drafts: 配了 embedding 但本次取向量失败, %d 条没有标题向量入库 (project=%s) "
                               "—— 常见原因是 key 欠费/配额用尽/网络不通; 修好之后跑 "
                               "`python -m deskcore.cli reembed --project %s` 补齐", written, project_id, project_id)
                out["embedding_warning"] = (
                    f"这次存档时标题向量没算出来, {written} 条暂时只参与开头和正文重合的查重"
                    "(同角度换说法的标题比不出来)。已记下, 交付照常, 不用你做什么。")
            other_missed = attempted - consumed - len(key_problems)
            if attempted and other_missed > 0:
                # consume_angle 现在会在"台账里根本没这一行"时返回 False(见 store 里的
                # 说明)。差额必须说出来 —— 这些坐标下一批还会被抽到, 悄悄少算等于
                # 避重失效了却没人知道。key 本身对不上的那几篇另报(angle_key_warning, W-31), 这里只剩其余的:
                # 台账里有这一行、这次没销成(查询 / 写台账出错, 或者被并发抢先), 或者没查清是哪一种。
                out["angle_ledger_warning"] = (
                    f"{other_missed}/{attempted} 个坐标没能在台账上销账("
                    + ("台账里有这一行, 是查台账或写台账时出了错、或者被并发抢先" if keys_classified
                       else "没查清是 key 对不上台账、还是写台账时出了错")
                    + ")。这些坐标下一批可能被重复抽到; 已记下, 不用你做什么。")
            if rpc_anomalies:
                out["rpc_anomalies"] = rpc_anomalies
                out["rpc_warning"] = (
                    f"存档回执里有 {rpc_anomalies} 行对不上号, 已跳过并记下: 这几条可能已经存进去了, "
                    "但底稿 / 销账没做。别重试 —— 重试会撞上刚存进去的这几条。")
            if unattributed or key_problems:
                # 只报个数没用 —— 这正是简报那段 angle_debt 踩过的:
                # 新开一场对话时上一次 draw_angles 的返回早就不在上下文里, 只说
                # "你有 N 条没带坐标"等于让模型去补一个它不知道是什么的东西,
                # 它只能忽略、或者再抽一批(codex review on #86)。所以把【这个人
                # 当前还挂着的坐标】一并回去。
                #
                # 2026-09-21 实查生产库: Hatherine痘痘贴 9-14/9-15 抽了 24 张牌,
                # 9-21 04:01 另一场会话一次 commit 了 7 篇、一个坐标没带 —— 那场
                # 会话当天【一次 draw_angles 都没调】, 手里根本没有坐标可带。简报
                # 里的 angle_debt 那会儿已经上线了, 但"看到这个数该怎么办"写在
                # SKILL.md 里, 得靠客户端重新导入 skill 才生效。这一段【不依赖
                # skill】: 交闸这一刻服务端自己把坐标摆出来。
                #
                # ⚠️ 必须在销账【之后】查 —— 这一批刚销掉的不该再报成欠账。
                # ⚠️ 失败绝不许冒泡。指纹这时已经写进去了, 抛出去调用方会重试,
                #    重试会撞上自己刚写的指纹 —— 一句提醒把一次成功的入库变成
                #    "你的稿子重复了"。同 rpc_anomalies / identity_error 的纪律。
                # W-38 上线之前抽的不算(ANGLE_DEBT_SINCE, 同简报): 那些多半写完发出去了, 叫模型「下一批按它们写」会再写一遍。
                lap = _lap(commit_parts, "other", lap)       # 拼回包、提示语(纯代码)
                try:
                    debt = store.angle_debt(client, project_id, user_id,
                                            ANGLE_DEBT_DAYS, since=ANGLE_DEBT_SINCE)
                except Exception:
                    logger.exception("angle debt lookup after commit failed "
                                     "(project=%s)", project_id)
                    debt = None
                lap = _lap(commit_parts, "angle_debt", lap)
                tail = key_tail = ""
                if debt and debt["unconsumed"]:
                    out["angle_debt"] = debt
                    shown = debt.get("angles") or []
                    more = debt["unconsumed"] - len(shown)
                    listed = (f"(angle_debt.angles 里列了 {len(shown)} 个"
                              + (f", 还有 {more} 个没列" if more > 0 else "")
                              + ")" if shown else "")
                    tail = (
                        f"你在这个项目近 {debt['since_days']} 天还有 "
                        f"{debt['unconsumed']} 个抽了没写的坐标" + listed
                        + " —— 下一批按它们写, 别再抽一批新的。")
                    # key 对不上的那几篇其实写了、存了: 它们的牌还挂在这里面, 叫模型「按它们写」会把同一个角度再写一遍
                    key_tail = (f"你在这个项目近 {debt['since_days']} 天还挂着 {debt['unconsumed']} 个没销账的坐标"
                                + listed + ": 这一场还没存的稿子, 存档时从里面找到分给它的那个 key 原样带上; "
                                "已经存了的那几篇别按这些坐标再写一遍。")
                if unattributed:
                    # 同一批里还有 key 对不上的: 挂着的牌里有几张其实刚写完存了, 这里再说「下一批按它们写」就和
                    # angle_key_warning 打架、把同一个角度写两遍(W-31 复审)。那种时候挂着的牌只在 angle_key_warning 里说
                    out["unattributed_warning"] = (
                        f"{unattributed}/{written} 条入库的稿子没带 angle_key, 台账无法给它们"
                        "销账 —— 这些角度下一批还会被抽到, 同一个故事会被讲第二遍, 而那种"
                        "重复查重闸抓不到。成批写的稿子每条都要带 draw_angles 分给它的 "
                        "angle_key。" + ("" if key_problems else tail))
                if key_problems:
                    # W-31: 只说「销账少了几个」模型不知道是自己的 key 错了(10-26 sportsix 5 批都这样存下去)。
                    # 说清是哪几篇、错在哪、下一篇怎么带; 已经存了的不用重存(没有改 key 的路, 重存只会多一版)。
                    # 「自己编的、缩写的或抄错了」只对形状不对的说: 形状对的不是编的(W-31 复审)
                    n = key_problem_n
                    parts = []
                    if n.get(store.ANGLE_KEY_UNKNOWN):
                        parts.append(f"{n[store.ANGLE_KEY_UNKNOWN]} 篇带的 angle_key 在这个项目的台账里根本没有(draw_angles "
                                     f"发的 key 都是 {fp.ANGLE_KEY_HEX_LEN} 位十六进制串, 这几个是自己编的、缩写的或抄错了)")
                    if n.get(store.ANGLE_KEY_OTHER_PROJECT):
                        parts.append(f"{n[store.ANGLE_KEY_OTHER_PROJECT]} 篇带的 angle_key 是你在别的项目抽的牌(哪个项目见 "
                                     "angle_key_mismatch 里的 project_id): 稿子是不是存错了项目? 跟用户确认 —— 那几张牌在"
                                     "那个项目里还挂着")
                    if n.get(store.ANGLE_KEY_NOT_IN_LEDGER):
                        parts.append(f"{n[store.ANGLE_KEY_NOT_IN_LEDGER]} 篇带的 angle_key 形状是对的({fp.ANGLE_KEY_HEX_LEN} "
                                     "位十六进制), 但台账里没有这一行: 多半是发牌时台账没写进去, 不是你编的或抄错的")
                    if used_keys:
                        parts.append(f"{used_keys} 篇带的 angle_key 已经销给了别的稿子(一个 key 只能给一篇, 同一批里两篇"
                                     "带了同一个 key 也是这样)")
                    out["angle_key_warning"] = (
                        "; ".join(parts) + f"。这 {len(key_problems)} 篇已经存档、查重照常, 但角度没销账(哪几篇见 "
                        "angle_key_mismatch), 同一个角度之后还可能被抽到; 已经存了的这几篇不用重存。"
                        + ("接下来存档时每篇的 angle_key 照 draw_angles 返回里分给它的那个原样复制, 不要自己编号、缩写或改写。"
                           if n.get(store.ANGLE_KEY_UNKNOWN) or used_keys else "")
                        + key_tail)
            if rejected:
                pre = sum(1 for r in rejected if r.get("gate") == "pre_commit")
                race = sum(1 for r in rejected if r.get("gate") == "atomic_recheck")
                bad_ref = sum(1 for r in rejected if r.get("gate") == "replace_lookup")
                parts = []
                if pre:
                    parts.append(f"{pre} 条在入库前的判定里就被拦下(和 check_drafts 同一套"
                                 "闸 —— 说明这几条没过 check, 或者 check 之后又改回去了)")
                if race:
                    parts.append(f"{race} 条在写入时被拦下(你 check 之后、commit 之前有人"
                                 "先提交了撞车的稿子)")
                tail = ""
                if pre or race:
                    tail = ("。这几条没有入库, 要重写后重新走 check_drafts, "
                            "不能当作已交付")
                if bad_ref:
                    # 这不是撞车, 是 replaces_version_id 给错了: 让人重写只会再错一次
                    # (codex #84 P2)。
                    parts.append(f"{bad_ref} 条的 replaces_version_id 对不上(不是 UUID / 不是本"
                                 "项目的版本 / 和 version_id 同时带了 / 同一篇替换两次) —— 稿子"
                                 "本身没问题, 改对 id 再提交一次, 不用重写")
                out["note"] = "; ".join(parts) + tail + "。"
            if not atomic:
                logger.warning("commit_drafts: 本次入库没有做原子重查(deskcore_commit_fingerprints RPC 不存在, "
                               "migrations/001 可能没跑, project=%s)", project_id)
                out["warning"] = ("这次存档没做并发重查: 队友同时存档时, 可能有撞车的稿子一起存进去。"
                                  "已记下, 不用你做什么。")
            # 存档之后的下一步写进返回值(D-120: 人审和导出两个工具已经撤了, 模型最容易犯的错变成"让用户导出 / 等用户
            # 说定稿")。只在真存进去了时说(codex review on #94, P2 同一个理由): written=0 时没有版本可说。
            if out.get("written"):
                # check_rules(2026-10-20, D-121)是存档之后唯一还要调的一步: 按编译好的规则逐篇查, 只作建议。
                # 只查新稿(2026-10-20 复审): 带 replaces_version_id 改过重存的, 协议 5a 和 check_rules 自己都说「改一轮就够,
                # 不用再调」—— 这里再叫它调一次, 最新的那句话就成了「再查」, 多花一轮 Jev 的钱, 还会改 → 查 → 改地转下去。
                replaced_new = {r["version_id"] for r in out["replaced"]}
                out["check_rules_version_ids"] = [v for v in out["version_ids"] if v not in replaced_new]
                # 底稿没建成的新稿(identity_warning)没有版本行, check_rules 查不了(独立复审 on aw E: 原来 version_ids 一空就说
                # 「都是改过重存的」, 新稿一篇都没查、写手还被告知不用查)
                unminted = len(to_mint) - len(minted_ok)
                unminted_step = (f"另有 {unminted} 篇新稿底稿没建成(见 identity_warning), check_rules 查不了它们, "
                                 "不能说这几篇守住了规则。" if unminted else "")
                if not out["check_rules_version_ids"] and unminted:
                    rules_step = unminted_step
                elif not out["check_rules_version_ids"]:
                    rules_step = ("这几篇都是改过重存的(带 replaces_version_id), 不用再调 check_rules。")
                elif replaced_new:
                    rules_step = ("然后调一次 check_rules, 只传 check_rules_version_ids(这批里的新稿)按项目规则逐篇查, "
                                  "只作建议; 带 replaces_version_id 改过重存的那几篇不用再查。")
                else:
                    rules_step = ("然后调一次 check_rules(传 check_rules_version_ids, 就是这批的 version_ids)按项目规则"
                                  "逐篇查, 只作建议。")
                if out["check_rules_version_ids"] and unminted:
                    rules_step += unminted_step                      # 建成了的照查, 没建成的那几篇说清没查
                if out["check_rules_version_ids"] and (tactic or "").strip():
                    # 方向闸同一个判据(项目名 + tactic): 不带的话 check_rules 只按项目名判, 简报挡掉的规则又查一遍
                    rules_step += "调 check_rules 时 tactic 也照这次传给 score_drafts 的原样带上。"
                if len(out["check_rules_version_ids"]) > CHECK_RULES_MAX_VERSIONS:
                    # 一批不限篇数, check_rules 一次只查这么多(2026-10-20 第二轮复审: 原来超了整次报错, 写手照协议说没查成)
                    rules_step += (f"这批新稿 {len(out['check_rules_version_ids'])} 篇, check_rules 一次查 "
                                   f"{CHECK_RULES_MAX_VERSIONS} 篇: 照样整串传, 没查到的它放在返回的 remaining_version_ids "
                                   "里, 照它的 next_step 接着调。")
                out["next_step"] = ("这批已存档。把 judge.results 里每篇的 score 和 fixes 照实告诉用户(不拦交付)。"
                                    + rules_step +
                                    "用户要改哪篇: 改完带 replaces_version_id 再调 score_drafts。定稿由用户自己贴进飞书, "
                                    "发出去之后系统按内容自动对回来 —— 不用导出。")
            # ⚠️ 这里原来是 `return out`。改成落到 with 块外面去, 是为了让下面的入库判定
            #    【不在】项目写锁里、也【不在】这个 try 里(设计审查 2026-09-23 §5):
            #    · 在 try 里: 判定一抛, 下面的 except 会把已替换稿的旧指纹无条件放回 ——
            #      新旧两版指纹并存, 下一版判自己撞车; 再 raise 出去, 调用方重试又撞上
            #      自己刚写的指纹。一次增强项的故障变成一句"你的稿子重复了"。
            #    · 在锁里: 就算自己包了 try, 多等的 8 秒也全算进同项目其它 commit / 补录
            #      的 90 秒等锁预算。
        except Exception:
            # 指纹已经摘了、稿子却没入库: 放回去, 否则旧稿从此不参与查重(而且没人知道)。
            _restore_stash(client, stash, list(stash))
            raise
    # 各段可加: lock_wait + commit + delivery_rules + judge + project_isolation ≈ total
    commit_ms = _ms_since(started) - lock_wait_ms
    # commit 段剩下的(拼回包、提示语、放锁)记进 other, 各步加起来 = commit(W-21)。毫秒取整各自截断, 差几毫秒算进 other
    commit_parts["other"] = max(0, commit_parts.get("other", 0)
                                + commit_ms - sum(commit_parts.values()))

    # ── 交付版式 + 代码查规则(2026-10-20): 锁外、恢复指纹的 try 外, 同下面的判定 ──────────────────
    # 都是纯代码(模板在项目行里, 规则多一次很小的查询), 放在判定前面: 判定的截止按"已用时间"算, 这一步多花的
    # 几十毫秒从判定里扣, 不会把整次回包推过客户端的容忍。只拼 / 只查存进去了的那几篇, 被闸拒掉的要重写。
    # 单记一段 delivery_rules(codex review on aw#115): 规则那次查询是翻页读的, 库慢的时候不是几十毫秒; 不单记的话
    # 这段时间哪一段都不算, 判定被扣掉的那截预算也查不出是谁花的。
    post_started = time.monotonic()
    # 改稿重存、真存进去了的那几篇 → 它们挂的旧 item: 有它们就把原来那一整批拼回来(挂旧 item 半途失败的那篇也算, 写手要的是
    # 改过的这一版换在原位)
    post_meta = _delivery_and_rule_checks(client, project, project_id, user_id, drafts, sorted(inserted_idx), out,
                                          tactic=tactic,
                                          revised={i: replace[i]["item_id"] for i in sorted(inserted_idx) if i in replace})
    delivery_rules_ms = _ms_since(post_started)

    # ── 入库判定(参考分): 锁已经放了, 恢复指纹的那个 try 也出来了 ─────────────
    # 到这里入库已经成功, 下面任何一步都不许把它变成失败: 判定自己不抛
    # (_judge_committed 内部逐篇兜底), 外面再兜一层, 兜到的也只记进 out["judge"]。
    judge_started = time.monotonic()
    # J-08: 判定只拿整次回包目标里剩下的(先扣掉后面跨项目检查的上界)。入库段实测已到 12 秒,
    # 判定再拿满 8 秒就贴着客户端 ~22 秒的容忍 —— 见 config.COMMIT_RESPONSE_BUDGET_SEC。
    judge_budget = min(judge_client.timeout_sec(),
                       config.COMMIT_RESPONSE_BUDGET_SEC - (judge_started - started)
                       - _ISOLATION_TIMEOUT_SEC)
    try:
        targets, skipped = _judge_targets(drafts, titles, bodies, inserted_idx, replace,
                                          replaced_ok, minted_ids, minted_ok)
        out["judge"] = _judge_committed(client, project_id, targets, skipped, budget=judge_budget)
    except Exception as exc:                            # noqa: BLE001 — 增强项, 绝不冒泡
        logger.exception("commit_drafts: 入库判定整体失败(project=%s) —— 入库不受影响",
                         project_id)
        out["judge"] = _judge_block(
            [], [], error=f"判定这一步整体失败({type(exc).__name__}: {exc}), 入库不受影响")
    judge_ms = _ms_since(judge_started)

    # 「commit_drafts 现在约 1~2 秒」在仓库里没有依据(设计审查 §5)—— 先量, 攒样本再定
    # judge 的 8 秒。lock_wait 单列: 补录正在跑时入库要等锁(最长 INGEST_LOCK_WAIT_SEC),
    # 不拆开的话一次等锁就把分位数带歪。
    isolation_started = time.monotonic()
    excluded = {drafts[i].get("version_id") or minted_ids.get(i) for i in inserted_idx}
    out["project_isolation"] = _cross_project_shadow_bounded(client, project_id, user_id, drafts, vecs, excluded)
    out["elapsed_ms"] = _ms_since(started)
    out["phase_ms"] = {"lock_wait": lock_wait_ms, "commit": commit_ms, "delivery_rules": delivery_rules_ms,
                       "judge": judge_ms, "project_isolation": _ms_since(isolation_started), "total": out["elapsed_ms"]}
    _record_commit_metrics(client, project_id, user_id, drafts, out, flywheel=arm_meta, checks=post_meta,
                           commit_parts=commit_parts)
    return out


# 人审只认这两个结论。**刻意不含 pending** —— 迭代后重置回待审是
# `DecisionSource.SYSTEM`(见 app.py 那处注释), 既不是审稿也不是检测, 让它从
# 人审入口进来就等于伪造一条人工决策。
HUMAN_DECISIONS = ("approved", "needs_revision")

MAX_REVIEW_DRAFTS = 200

# review_drafts 的 user_words: 用户给结论时的原话, strip 后 1 ~ 200 字(上限与
# migrations/012 的 CHECK 同一个数, 在 db.DECISION_NOTE_MAX_CHARS)。
USER_WORDS_MAX_CHARS = db.DECISION_NOTE_MAX_CHARS


def _clean_user_words(user_words) -> tuple[str | None, str | None]:
    """``(原话, 错误)``: 只有一个非 None。空 / 不是字符串 / 超长都是错。"""
    if not isinstance(user_words, str) or not user_words.strip():
        return None, ("user_words 必填, 且必须是用户自己给出结论时的原话"
                      "(比如「这批可以发」「第 3 条重写」)。用户没表态就不要调这个工具, "
                      "也不要替他编一句。")
    words = user_words.strip()
    if len(words) > USER_WORDS_MAX_CHARS:
        return None, (f"user_words 超过 {USER_WORDS_MAX_CHARS} 字({len(words)}) —— "
                      "只放用户给结论的那一两句原话, 不要把整段对话塞进来。")
    return words, None


def _seconds_since(version_created_at) -> int | None:
    """决策时刻距【被审那一版】建出来的秒数; 时间缺失或解析不了就 None, 不硬算。
    不按批次算 (codex review on #94, P2): 替换稿是往原 item 上加新版本, batch_id 不变, 按批次算会把
    "入库 10 秒就自审"量成几天, 信号就没了。versions.created_at 每个新版本都有。"""
    created = db.parse_ts(version_created_at)
    if created is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - created).total_seconds()))


def _is_uuid(value: str) -> bool:
    """格式合法的 UUID 才敢送进 ``.in_()``。

    ``versions.id`` 是 uuid 列。PostgreSQL 在**解析**阶段就会拒掉整条查询
    (`invalid input syntax for type uuid`), 不是跳过那一个值 —— 所以一个打错的
    id 会连累同一批里所有好的。
    """
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def review_drafts(client, project_id: str, decisions: list[dict],
                  user_words: str | None = None,
                  user_id: str | None = None) -> dict:
    """给已定稿的稿子盖一枚**经代理转述的**人工审核决策。

    ── 2026-10-09 审计 A-01: 来源改记 ``human_via_agent``, 并留下原话 ─────
    生产库里 ``decision_source='human'`` 的 32 行全部是这个函数写的, 而且全落在
    模型自己的工具链里(commit 后 10~27 秒就"审完"), 库里分不出「模型自己点了」和
    「用户说了全过、模型照实记」。所以从此:

      · 这里**只写** ``db.DecisionSource.HUMAN_VIA_AGENT``, 永远不写 ``human`` ——
        那个值留给 Streamlit 里真的点了按钮的路(app.py);
      · ``user_words`` **必填**: 用户给结论时的原话, strip 后 1 ~ 200 字, 落到
        ``items.decision_note``。缺了 / 空的 / 超长 → 整批不写, 回 ``error``;
      · ``decided_within_s`` 服务端算(距这批稿子 ``batches.created_at`` 的秒数),
        几秒内审完一批就是模型替用户点的信号。batch 时间缺失存 NULL, 不失败。

    这不是服务端能判「用户真的说过」的闸 —— 它判不了。它做的是把证据留下来,
    让人和下游**能分开看**。

    ── 为什么非有不可(2026-09-16 评测 AW-01)─────────────────────────────

    ── 为什么非有不可(2026-09-16 评测 AW-01)─────────────────────────────
    ``commit_drafts`` 建的 item 是 ``pending`` 且不盖任何决策戳 —— 那是对的,
    理由见 ``store.DESKCORE_ITEM_STATUS`` 上面那整段: 定稿不等于审核, 把每次
    commit 写成 approved 会让「机器判定被当人工反馈」这个已经修过一次的 bug
    从新入口重犯一次, 而且灌进去的是**清一色正例**。

    但在此之前, 写作台这条路**根本没有**下一步: 只用它写稿、定稿、导出的团队
    永远产不出一条人工审核决定, 于是 TV 那边按
    ``status in ('approved','needs_revision')`` 捞行时一条也捞不到。缺的不是
    「把 pending 改成 approved」, 是**一个真的有人点过的动作**。

    这个函数就是那个动作, 三条纪律:

      1. **reviewer 恒为调用者**, 签名里没有 reviewer 参数。审计 COR-004 治的
         正是"把 owner 当 reviewer", 留个口子等于把它请回来。
      2. **只认 approved / needs_revision**, 见 ``HUMAN_DECISIONS``。
      3. **打回和通过同等公民**。一个只能点通过的审核入口产出的仍然是清一色
         正例, 与不做无异。

    ⚠️ 不设 ``best_version_id``。「选为最佳」在 UI 里是**独立于审稿**的动作
    (app.py 那处注释: 走 update_item_status 会给它盖上一枚人工决策戳, 把出处
    数据自己污染掉)。写作台的 item 每条只有一个版本, 代表版本自然落在它身上。

    返回 ``{"reviewed", "results"}``; 每条 result 带 ``outcome``:
      · ``recorded``   —— 决策已落库。附 ``previous_status`` 与
                          ``previously_decided_by`` —— 后者是**上一个决定的来源**
                          (``db.DecisionSource`` 的五个值), 只有 ``human`` /
                          ``human_via_agent`` 才代表之前真有人审过; 机器打回的
                          (``auto_hard_rule`` / ``auto_dedup``)和系统置位
                          (``system``)都不是
      · ``not_found``  —— 这个 version_id 不在本项目里(或根本不存在)
      · ``invalid``    —— 入参不合法: decision 不是那两个值之一, 或 version_id
                          不是合法 UUID(``detail`` 里说是哪一种)
      · ``duplicate``  —— 同一条稿子在本批里已经被决定过, **这条没生效**
      · ``failed``     —— 写库失败, ``detail`` 里是原因

    **部分失败照样回报已经成功的那几条**, 理由同 ``store.mint_draft_identity``:
    行已经改了而调用方以为没改, 比报一条失败更难查。
    """
    # user_words 先于一切校验: 不合法就一行都不碰, 也不去查库。
    words, words_error = _clean_user_words(user_words)
    if words_error:
        return {"reviewed": 0, "results": [], "error": words_error,
                "hint": "等用户读完稿子、亲口给出结论, 再把他的原话放进 user_words 调一次。"}
    assert_project_access(client, project_id, user_id=user_id)   # 审计 COR-015
    if not decisions:
        return {"reviewed": 0, "results": []}
    if len(decisions) > MAX_REVIEW_DRAFTS:
        raise ValueError(
            f"一次最多审 {MAX_REVIEW_DRAFTS} 条, 收到 {len(decisions)} 条。"
            "分批调用 —— 单次太大时部分失败的定位成本会陡增。")

    wanted = [str(d.get("version_id") or "") for d in decisions]
    # ⚠️ 只把**格式合法**的 id 送进 .in_()。``versions.id`` 是 uuid 列, 混进一个
    # 手抖打错的值, PostgreSQL 会用
    # `invalid input syntax for type uuid` 拒掉**整条查询** —— 于是同一批里
    # 二十条好的也一起挂, 而这个函数的契约明写着部分失败要逐条报。
    # (codex review P2; 在真 PG 16.13 上复现过)
    known = store.items_for_versions(
        client, project_id, [v for v in wanted if _is_uuid(v)])

    results: list[dict] = []
    reviewed = 0
    # item_id → 本批里**先**决定它的那个 version_id。同一条稿子被点两次时,
    # 静默让最后一次生效是最糟的选择: 两条都报 recorded, 而第二条的
    # previous_status 取自更新前的快照、已经是错的, 把覆盖藏了起来。
    decided_items: dict[str, str] = {}
    for d in decisions:
        vid = str(d.get("version_id") or "")
        decision = str(d.get("decision") or "")
        row = {"version_id": vid, "decision": decision}

        if decision not in HUMAN_DECISIONS:
            results.append({**row, "outcome": "invalid",
                            "detail": f"decision 只能是 {' / '.join(HUMAN_DECISIONS)}"})
            continue
        if not _is_uuid(vid):
            results.append({**row, "outcome": "invalid",
                            "detail": "version_id 不是合法 UUID"})
            continue
        hit = known.get(vid)
        if hit is None:
            results.append({**row, "outcome": "not_found",
                            "detail": "这个 version_id 不在本项目里"})
            continue
        prior = decided_items.get(hit["item_id"])
        if prior is not None:
            # 拒绝而不是"最后一条生效": 同一条稿子在一批里被点两次是调用方的
            # 错, 悄悄取最后一条会让它永远发现不了。
            results.append({**row, "outcome": "duplicate",
                            "item_id": hit["item_id"],
                            "detail": f"同一条稿子本批里已经被 {prior} 决定过了, "
                                      f"这条没生效 —— 想改判就单独再调一次"})
            continue

        try:
            # ⚠️ 永远是 HUMAN_VIA_AGENT, 不是 HUMAN —— 这条路上没有人点过按钮,
            #    只有模型转述。tests/test_review_provenance.py 用 AST 守着 deskcore/
            #    里不再出现写 'human' 的代码。
            db.update_item_status(client, hit["item_id"], decision,
                                  source=db.DecisionSource.HUMAN_VIA_AGENT,
                                  reviewer_id=user_id,
                                  decision_note=words,
                                  decided_within_s=_seconds_since(
                                      hit.get("version_created_at")))
        except Exception as exc:                 # noqa: BLE001
            logger.exception("review_drafts 写库失败 (version=%s)", vid)
            results.append({**row, "outcome": "failed",
                            "item_id": hit["item_id"],
                            "detail": f"{type(exc).__name__}: {exc}"[:300]})
            continue

        reviewed += 1
        decided_items[hit["item_id"]] = vid
        results.append({**row, "outcome": "recorded",
                        "item_id": hit["item_id"],
                        "previous_status": hit.get("status"),
                        "previously_decided_by": hit.get("decision_source")})

    return {"reviewed": reviewed, "results": results}


MAX_EXPORT_DRAFTS = 200


def export_drafts(client, project_id: str, *, batch_id: str | None = None,
                  version_ids: list[str] | None = None,
                  user_id: str | None = None) -> dict:
    """把定稿导成一个可以粘进飞书表的 Excel。

    ── 这一步在闭环里的位置 ────────────────────────────────────────────
    写作台 → 飞书表 → Truth Vault → (指标回流) → 写作台。第一段一直是**手工**
    的: 运营把稿子粘进飞书表。粘的时候如果不带 lineage, TV 就只知道"有这么一条
    笔记", 不知道它是谁写的哪一版 —— ``v_model_comparison`` 那个 view 就是这么
    长期查出空集的。

    所以这里导出的表, 除了内容列还带 ``exporter.LINEAGE_COLUMNS`` 那六个**命名
    可见列**(列名由 TV 定)。运营整片选中粘贴, lineage 就跟着过去了。

    ⚠️ 飞书表那边要**先建好这六列**, 否则粘过去是六列无处安放的数据。列名和
       字段类型见 truth-vault 的 ``docs/11-feishu-table-setup.md``。

    ── 为什么返回 base64 而不是文件路径 ────────────────────────────────
    deskcore 是个远端服务, 调用方读不到它的文件系统。给一个下载 URL 就得配一套
    单独的签名/鉴权 —— 而本仓的审计史上一半的坑都是"半套鉴权"。base64 让调用方
    自己落盘, 不新增任何鉴权面。

    正文全文本来就在调用方手里(稿子是它写的), 所以真正的额外开销只有 xlsx 的
    封装。``preview`` 只回标题和 id, 够核对导的是不是那一批, 不重复正文。
    """
    assert_project_access(client, project_id, user_id=user_id)
    if not batch_id and not version_ids:
        raise ValueError(
            "要导哪些稿子? 给 batch_id(commit_drafts 的返回值里有)或者 version_ids。"
            "不给的话只能靠猜, 而猜错了导出的是别的批次 —— 那会把错的 lineage "
            "粘进飞书表, 比导不出来更难查。")

    items = store.drafts_for_export(
        client, project_id, batch_id=batch_id, version_ids=version_ids,
        limit=MAX_EXPORT_DRAFTS)
    if not items:
        # 键的形状和成功那一支保持一致 —— 调用方不该为了空结果写第二套解析。
        return {"count": 0, "columns": [], "filename": None, "xlsx_base64": None,
                "preview": [], "missing_version_ids": sorted(version_ids or []),
                "truncated": False, "exported_at": None,
                "note": ("没找到可导的稿子。要么 batch_id / version_ids 不属于这个"
                         "项目, 要么那一批还没 commit_drafts —— 只有入了库的稿子"
                         "才有身份可导。")}

    # ── 少导了就必须说 ──────────────────────────────────────────────────
    # 本仓的审计里"静默截断"是反复出现的一类(COR-006 / ROB-011 都是它), 而导出
    # 这条路上它尤其阴: 少导几条 = 那几篇稿子发出去之后归因不回来, 而返回值看起来
    # 完全正常, 没有任何人会发现。所以两种少法都点名:
    #
    #   · 点名要了某些 version 却没找到 —— id 打错、不属于这个项目、或者还没 commit;
    #   · 命中数顶到上限 —— 后面可能还有, 这一次没导全。
    missing = sorted(set(version_ids or []) - {r["version_id"] for r in items})
    truncated = len(items) >= MAX_EXPORT_DRAFTS

    exported_at = datetime.now().isoformat(timespec="seconds")
    blob = exporter.build_combined_excel(items, exported_at=exported_at)
    stamp = exported_at.replace("-", "").replace(":", "").replace("T", "_")[:13]

    notes = ["把 xlsx_base64 解码写成 filename 那个文件, 打开、整片选中、粘进飞书表。"
             "⚠️ 飞书表要先有 columns 里的那几列, 列名逐字相同 —— 对不上的列会让 "
             "Truth Vault 把整行 quarantine。"]
    if missing:
        notes.append(
            f"⚠️ 点名的 {len(missing)} 个 version_id 没找到, **不在这个表里**: "
            f"{missing[:10]}{' …' if len(missing) > 10 else ''}。"
            "多半是 id 打错、不属于这个项目、或者那几条还没 commit_drafts。")
    if truncated:
        notes.append(
            f"⚠️ 命中数顶到上限 {MAX_EXPORT_DRAFTS} 条, 后面可能还有没导出来的。"
            "分批用 version_ids 点名导, 别把这一次当全量。")

    return {
        "count": len(items),
        "missing_version_ids": missing,
        "truncated": truncated,
        # 明着回一份列名: 运营要照着它在飞书表里建列, 而且对不上会整行被
        # quarantine(TV 的 D-021)。让它出现在返回值里, 不必去翻文档。
        "columns": [exporter.CONTENT_HEADER, *exporter.LINEAGE_HEADERS],
        "filename": f"deskcore_{stamp}.xlsx",
        "xlsx_base64": base64.b64encode(blob).decode("ascii"),
        "preview": [{"title": r["title"], "version_id": r["version_id"]}
                    for r in items],
        "exported_at": exported_at,
        "note": " ".join(notes),
    }


def reembed_fingerprints(client, project_id: str, *, chunk: int = 50,
                         progress=None) -> dict:
    """给指纹库里缺标题向量的行补上向量。

    ⚠️ 【故意没有归属校验】(审计 COR-015)。它和 backfill 一样不是 MCP 工具 ——
    只有 CLI 能调, 而跑 CLI 的人手里握着 service_role key(等价于直连库)。在这儿
    加一道 owner 校验挡不住任何人, 只会挡住"帮同事补一下向量"这类正当运维,
    还会给人一种"运维路径也隔离了"的错觉。隔离边界在 MCP/REST 那一面。

    **和 backfill 是两件事, 别混。** backfill 把 autowriter.versions 里的历史
    成稿【搬进】指纹库; reembed 修的是【已经在指纹库里、但当时没取到向量】的行。
    后者 backfill 够不着 —— 它扫的是 items × versions, 而 WorkBuddy 写的稿子
    version_id 为空、不在那张表里。

    典型触发场景两个:

      · embedding 的 key 欠费/配额用尽那几天照常 commit 了稿子, 它们只有确定性
        指纹。补上 key 之后跑这个。
      · **换了 embedding 模型**(2026-08-26 就换过一次)。老模型产的向量不能跟新
        向量比 —— 跨模型算余弦出来的数是垃圾且不报错 —— 所以它们被比对排除,
        必须用新模型重算才能重新参与。这一路是 codex review · #65 P1 补的:
        原来只扫 ``title_embedding IS NULL``, 于是换模型之后老行既进不了比对、
        又永远不会被重算, 卡在一个**没有出口**的状态里。
    """
    if not dedup.embeddings_available():
        return {"error": "embedding 不可用(GOOGLE_API_KEY 未配或 SDK 缺失), 无法补向量",
                "fixed": 0, "pending": None}

    rows = store.fingerprints_needing_vectors(client, project_id,
                                              dedup.EMBEDDING_MODEL)
    if not rows:
        return {"pending": 0, "fixed": 0, "failed": 0,
                "note": "没有需要重算向量的行, 不用补。"}

    # 分别报出来, 因为它们意味着不同的事: 缺向量是"当时没算成", 换模型是"算过
    # 但那一批已经作废"。混成一个数字, 看的人没法判断这次该不该意外。
    absent = sum(1 for r in rows if r.get("_expect") is store.VECTOR_ABSENT)
    stale = len(rows) - absent

    fixed = failed = 0
    for start in range(0, len(rows), chunk):
        part = rows[start:start + chunk]
        vecs = dedup.embed_texts([r.get("title") or "" for r in part])
        if vecs is None:
            failed += len(part)
            logger.warning("reembed: embed_texts failed for chunk at %d", start)
            continue
        for r, v in zip(part, vecs):
            # expect 是读到这行时它的状态 —— set_fingerprint_vector 拿它做 CAS,
            # 免得用更旧的批次盖掉别的进程刚刷好的结果。
            if v and store.set_fingerprint_vector(
                    client, r["id"], v, dedup.EMBEDDING_MODEL,
                    expect=r.get("_expect", store.VECTOR_ABSENT)):
                fixed += 1
            else:
                failed += 1
        if progress:
            progress(min(start + chunk, len(rows)), len(rows))

    out = {"pending": len(rows), "fixed": fixed, "failed": failed,
           "missing_vector": absent, "stale_model": stale,
           "embedding_model": dedup.EMBEDDING_MODEL}
    if stale:
        out["stale_model_note"] = (
            f"{stale} 条是【换模型作废】的旧向量(不是缺向量) —— 它们在重算完成"
            f"之前不参与标题语义比对。当前模型: {dedup.EMBEDDING_MODEL}")
    if failed:
        out["note"] = (f"{failed} 条没补上(embedding 调用失败或行已被改动)。"
                       "可以再跑一次, 已补好的不会重复处理。")
    return out


def recompute_fingerprints(client, project_id: str, *, progress=None) -> dict:
    """按**当前的** ``normalize`` 口径重算确定性指纹。审计 COR-014 的后续。

    ⚠️ 什么时候需要跑: 只在 ``fingerprint.normalize`` 的口径变了之后。存量指纹的
    ``opening_hash`` / ``ngram_hashes`` 是用**当时**的口径算的 —— 口径一变, 新稿
    算出来的四字串就和历史对不上, 查重在过渡期反而更弱, 而且**不报错**。
    ``backfill`` 补不了这个: 它按 ``version_id`` 幂等跳过, 只管"没有的行", 不重算
    已有的行。

    ── 能重算到什么程度 ──────────────────────────────────────────────────
    指纹表**不存正文**(只有 ``title`` 和 ``opening`` 前 25 字), 所以:

      · ``opening_hash`` —— **每一行都能重算**。它本来就是
        ``sha16(normalize(opening))``, 而 ``opening`` 原样存着。这一路是
        "单独就判死"的最强信号, 能全修回来是关键。
      · ``ngram_hashes`` —— 只有 ``version_id`` 非空的行能重算(正文在
        ``autowriter.versions`` 里)。WorkBuddy 经 ``commit_drafts`` 写进来的行
        ``version_id`` 是空的, **正文已经不存在了**, 这一路修不回来。

    返回值里的 ``ngram_unrecoverable`` 就是修不回来的行数。**它不为 0 就要告诉
    用户**: 那些行的四字串仍然是旧口径, 与新稿比对会偏低。想彻底修只能把那些
    稿子重新 commit 一遍。

    幂等: 重复跑是同一个结果(纯函数重算), 只是白写一遍。

    ⚠️ W-38(2026-10-29): 查重底稿转正的版本, 发出去的帖和底稿不一样时有两行指纹(底稿一行、帖的文字一行, 同一个
    ``version_id``)。这里按版本正文(底稿)重算, 帖那一行的四字串会变成底稿的 —— 重算之后那几篇改过的帖, 新稿照着发出去的
    文字写时查重会弱一点(开头那一路不受影响)。
    """
    total = recomputed = unrecoverable = 0
    for page in store.fingerprint_pages(client, project_id):
        need_body = [r for r in page if r.get("version_id")]
        bodies = (store.version_bodies(client, [r["version_id"] for r in need_body])
                  if need_body else {})
        for row in page:
            total += 1
            new_open = fp.sha16(fp.normalize(row.get("opening") or "")) \
                if (row.get("opening") or "").strip() else ""
            vid = row.get("version_id")
            body = bodies.get(str(vid)) if vid else None
            if body:
                new_grams = fp.ngram_hashes(body)
            else:
                new_grams = None
                unrecoverable += 1
            store.update_fingerprint_hashes(
                client, row["id"], opening_hash=new_open, ngram_hashes=new_grams)
            recomputed += 1
        if progress:
            progress(recomputed, total)

    out = {"scanned": total, "rewritten": recomputed,
           "ngram_unrecoverable": unrecoverable}
    if unrecoverable:
        out["warning"] = (
            f"{unrecoverable}/{total} 行的正文已经不在库里(version_id 为空, "
            "多半是 WorkBuddy 经 commit_drafts 写进来的), 四字串那一路**没能重算** "
            "—— 它们仍是旧的规范化口径, 与新稿比对会偏低。开头指纹已全部修好。"
            "要彻底修只能把那些稿子重新 commit 一遍。")
    return out


def backfill_fingerprints(client, project_id: str, *, with_embeddings: bool = True,
                          chunk: int = 50, progress=None) -> dict:
    """把项目的历史成稿补进指纹库。**部署后每个项目必跑一次。**

    ⚠️ 【故意没有归属校验】—— 理由同 reembed_fingerprints, 见那边。

    为什么必须有: migrations/001 建的是【空表】, 而 check_drafts 只读这张表、
    只有 commit_drafts 会往里写。不回填的话, 上线第一天号称"比对全量历史"的
    硬闸手里一条历史都没有 —— 几年的老稿子对它全是新的, 重复原样放行。
    (codex review: 这条先被指出过一次, 我加了 CLI 子命令和文档却【没写这个
    函数】, 于是 `deskcore.cli backfill` 每次都 AttributeError —— 等于回填这件
    事从头到尾没存在过, 而文档和 PR 都写着它已经有了。)

    幂等: 已经有指纹的 version_id 跳过, 重跑安全。

    embedding 的取法有讲究: `versions.embedding` 存的就是**标题**向量
    (app.py:520 只对 title 取向量), 与 draft_fingerprints.title_embedding 同义,
    所以历史行有向量就【直接复用】, 不重新调 API —— 几千条重算既慢又费钱。
    只有缺向量的才补算, 且 embedding 不可用时照常写入(向量留空), 确定性信号
    不受影响。
    """
    # 审计 ROB-011: 逐页消费, 不再把整个项目的历史成稿(全文 + 每条 768 个
    # Python float)同时留在内存。5000 条那一档原来是三四百 MB 峰值 —— 容器
    # OOM 重启, 而这是"部署后每个项目必跑一次"的动作。
    #
    # done_ids 仍然一次取全: 它只是一串 UUID(几 MB 顶天), 而它是【幂等性本身
    # 所依赖的那个集合】—— 分页判重会让跨页的重复漏过去(审计 COR-005 修的
    # 就是它被静默截断的那一版)。
    done_ids = store.existing_fingerprint_version_ids(client, project_id)
    can_embed = with_embeddings and dedup.embeddings_available()
    total = already = written = reused = computed = missing = 0
    todo_seen = processed = 0
    pending: list[dict] = []

    def _flush() -> None:
        nonlocal pending, written, reused, computed, missing, processed
        if not pending:
            return
        part, pending = pending, []
        processed += len(part)
        # ⚠️ **能算就全部重算, 不复用 versions.embedding。**(codex review · #65 P1)
        #
        # 原来是"历史行有向量就直接复用, 只给缺的补算", 理由是省钱省时间。那个
        # 理由建立在一个**再也不成立**的假设上: `versions.embedding` 与当前模型
        # 同源。那张表【没有模型标记】, 所以它的来路永远无法证明 —— 2026-08-26
        # 换掉 text-embedding-004 之后, 复用就等于把老模型的向量贴上新模型的标签
        # 写进指纹库, 而跨模型算余弦出来的数是垃圾且【不报错】。
        #
        # 现在的口径: 能调 embedding 就一律用**当前模型**现算(几千条一批, 成本
        # 可以忽略); 调不动才退回复用历史向量, 且那时 embedding_model 记 NULL ——
        # 来路不明就如实说不知道, 于是比对时被排除、reembed 能找到它重算。
        # **宁可少比, 不能拿垃圾数当比过了。**
        need = (list(range(len(part))) if can_embed
                else [i for i, r in enumerate(part) if not r.get("embedding")])
        fresh: list[list[float]] | None = None
        if can_embed and need:
            fresh = dedup.embed_texts([part[i]["title"] for i in need])
            if fresh is None:
                logger.warning("backfill: embed_texts failed for a chunk of %d; "
                               "falling back to legacy vectors (model unknown)",
                               len(need))

        payload = []
        for i, r in enumerate(part):
            # attested = 这个向量确实是【当前模型】产的。只有现算的才算数。
            vec, attested = None, False
            if fresh is not None and i in need:
                pos = need.index(i)
                vec = fresh[pos] if pos < len(fresh) else None
                if vec:
                    computed += 1
                    attested = True
            if not vec and r.get("embedding"):
                # 退路: 现算不成(或整批 embedding 不可用)才复用历史向量。
                vec, attested = r["embedding"], False
                reused += 1
            if not vec:
                missing += 1
            body = r.get("body") or ""
            payload.append({
                "project_id": project_id,
                "user_id": r.get("user_id"),
                "version_id": r.get("version_id"),
                "title": r.get("title") or "",
                "opening": fp.opening_of(body),
                "title_embedding": vec or None,
                # ⚠️ 只有【现算】的才敢写模型名。复用 versions.embedding 时记
                # NULL —— 那张表没有模型标记, 来路无法证明, 而贴一个错标签的
                # 后果是查重安静地拿垃圾数当结论。NULL 的行会被比对排除, 并且
                # 能被 reembed 找到重算, 是个有出口的状态。(codex review · #65 P1)
                "embedding_model": dedup.EMBEDDING_MODEL if attested else None,
                "opening_hash": fp.opening_hash(body),
                "ngram_hashes": fp.ngram_hashes(body),
                "angle_key": None,   # 历史稿不是发牌产出的, 没有坐标
            })
        written += store.write_fingerprints(client, payload)

    for page in store.legacy_version_pages(client, project_id):
        total += len(page)
        for r in page:
            if r.get("version_id") in done_ids:
                already += 1
                continue
            pending.append(r)
            todo_seen += 1
            if len(pending) >= chunk:
                _flush()
                if progress:
                    # 流式下没有事先算好的分母 —— 报的是【已处理 / 已发现待回填】,
                    # 分母会随着翻页往上走。CLI 那边的文案已经跟着改了。
                    progress(processed, todo_seen)
    _flush()
    if progress and todo_seen:
        progress(processed, todo_seen)

    if not total:
        return {"total": 0, "already": 0, "written": 0, "embedded": 0,
                "reused_embeddings": 0, "missing_embeddings": 0}
    if not todo_seen:
        return {"total": total, "already": already, "written": 0, "embedded": 0,
                "reused_embeddings": 0, "missing_embeddings": 0}

    out = {
        "total": total, "already": already, "written": written,
        "embedded": reused + computed,
        "reused_embeddings": reused, "computed_embeddings": computed,
        "missing_embeddings": missing,
    }
    if missing:
        out["note"] = (
            f"{missing}/{written} 条没有标题向量"
            + ("(GOOGLE_API_KEY 未配或 embedding 调用失败)。" if not can_embed or computed == 0
               else "。")
            + "这些历史稿只参与确定性查重(开头精确 + 四字串重合), "
              "同角度换说法的标题比不出来。配好 embedding 后跑 "
              "`deskcore.cli reembed --project <id>` 给它们补上 —— 重跑 backfill "
              "不会重复写入, 但补向量是 reembed 的活。")
    if reused:
        # 复用的那些没有模型标记, 所以**不参与**标题语义比对。不说出来的话
        # "embedded: N" 看着像全都能比, 而实际能比的只有 computed 那部分 ——
        # 又一次"写着已经有了, 实际没有"。(codex review · #65 P1)
        out["unattested_embeddings"] = reused
        out["unattested_note"] = (
            f"{reused} 条复用了 versions.embedding 的历史向量。那张表【没有模型"
            f"标记】, 来路无法证明, 所以按 embedding_model=NULL 写入 —— 它们"
            f"**不参与**标题语义比对。跑 `deskcore.cli reembed --project <id>` "
            f"用当前模型({dedup.EMBEDDING_MODEL})重算之后才会生效。")
    return out


# ══════════════════════════════════════════════════════════════════════
# 部署自检 —— 这个库到底跑到第几个迁移了
# ══════════════════════════════════════════════════════════════════════
#
# ── 为什么非有这一段不可 ──────────────────────────────────────────────
#
# `migrations/README.md` 写着「五个迁移都设计成不跑也不会坏」。那句话是真的,
# 代价却是**没跑这件事在运行期完全看不见**: 004/005 缺席只埋一行 telemetry 就
# 退回慢路径, 短稿照搬长稿从此抓不到; 006 缺席则是另一种 —— 现有工作台每一次
# 「通过 / 打回」都会撞上 PostgREST 的 `column ... does not exist`。
#
# 这正是本仓反复栽的那个形状(runbook §5「文档与实际不一致」整节都是它):
# 文字写着已经有了, 实际没有, 而且不报错。2026-08-26 实测生产库
# (kduysqedr) 的结果是 **只跑了 001** —— 而 runbook §0 当时写的是
# 「schema 也上了生产」。查一次库就知道, 但在这之前没有任何一条命令能查。
#
# ── 三条实现纪律 ──────────────────────────────────────────────────────
#
# 1. **判据必须与真正消费它的代码同源。** 这里一律走 `store.rpc_missing`,
#    不另写一套字符串匹配。deskcore.md §4.2 记过反例: /health 自己读一遍 env,
#    于是回显着一个从未被调用过的模型名, 配错依然当场看不见。
#
# 2. **探测一律只读。** 写类 RPC 用「空 `_rows`」调 —— 那时函数体的 FOR 循环
#    一次都不进(见 migrations/005 的函数体), 只取一次事务级 advisory lock,
#    不写任何行。CAS 那个用 nil UUID + 不可能匹配的 witness, UPDATE 命中 0 行。
#
# 3. **探不到的要说"探不到", 不许猜。** 003 建的是唯一索引, PostgREST 看不见
#    索引 —— 所以它报 `unprobeable` 并把该跑的 SQL 一起交出来, 而不是按
#    "别的都在, 它八成也在" 蒙一个 applied。蒙对了没人受益, 蒙错了正好复现
#    这一整段想根治的那个失败形态。

# 匹配不到任何真实行的 UUID。探测只读的前提之一 —— 用它当 project_id/user_id,
# 就算某个探测函数真的会写, 它也没有行可写。
_PROBE_NIL_UUID = "00000000-0000-0000-0000-000000000000"

# 一定不等于任何一份 calibration_notes 的 md5(md5('') 是 d41d8c...)。
_PROBE_IMPOSSIBLE_MD5 = "0" * 32

# 001 建的四张表, 以及探它们用的那一列。四张表都有 ``project_id``(它们全都
# 挂在项目下), 这不是巧合而是设计 —— 但**别把它当理所当然**: 见下面探测处的
# 注释, 用 ``id`` 探的第一版在 user_calibration_notes 上就是错的。
_MIGRATION_001_TABLES = ("angle_ledger", "draft_fingerprints",
                         "user_calibration_notes", "style_edits")
_MIGRATION_001_PROBE_COLUMN = "project_id"

# backfill 自己那条路的上限(``store.legacy_version_pages`` 的默认 limit)。
# backfill_gap 刻意**不**用它扫描, 只用它判"这个项目大到 backfill 追不平了"。
_BACKFILL_DEFAULT_CAP = 5000

# 唯一一个"不跑就当场坏"的迁移 —— 其余几个缺席都是降级 + 留痕。所以它在补救
# 清单里必须排最前面, 与 runbook 的 `006 → 002 → 003 → 004 → 005` 一致。
MIGRATION_RUN_FIRST = "006_item_decision_provenance.sql"

# 缺 GRANT 的唯一补救出口。denied 状态**一律**指向这里, 不管报 denied 的那条
# 探测挂在哪个迁移名下 —— 见 migration_state 末尾对这条口径的完整说明。
MIGRATION_GRANTS = "007_deskcore_table_grants.sql"

MIGRATION_UNPROBEABLE_SQL = (
    "select indexname from pg_indexes where schemaname='autowriter' "
    "and indexname='versions_item_version_uniq';"
)

# 008 换的是**函数体**, 签名和返回列一个字没动 —— 从 PostgREST 这一面看,
# 跑没跑过完全一样。所以只能交出这句 SQL, 不许蒙。
MIGRATION_EMBEDDING_ISOLATION = "008_embedding_model_isolation.sql"

# 009 建两张表 + 两个跨 schema 的 RPC。表探列名取 tv_project_id(两张都有)。
MIGRATION_TV_LINKS = "009_tv_links.sql"
_MIGRATION_009_TABLES = ("tv_project_map", "tv_note_links")   # ingest_locks 单独探(列名不同)
_MIGRATION_009_PROBE_COLUMN = "tv_project_id"
# 010 开的是 RLS, PostgREST 探不到(service_role 绕 RLS, 开没开读起来一样)。
MIGRATION_TV_LINKS_RLS = "010_tv_links_rls.sql"
MIGRATION_010_PROBE_SQL = (
    "select relname, relrowsecurity from pg_class c join pg_namespace n on n.oid=c.relnamespace "
    "where n.nspname='autowriter' and relname in ('tv_project_map','tv_note_links','ingest_locks');"
)
# 011 建一个视图(发牌台账 → 版本 → TV 笔记 → tier)。PostgREST 能直接读视图, 探得到。
MIGRATION_ANGLE_OUTCOMES = "011_angle_outcomes_view.sql"
_MIGRATION_011_VIEW = "v_angle_outcomes"
# 012 给 items 加 decision_note / decided_within_s 两列, 并让 decision_source 的 CHECK
# 多认 human_via_agent(审计 A-01)。列探得到; CHECK 从 PostgREST 探不到(要写才知道),
# 用列当代理 —— 两者在同一个事务里, 不存在只跑了一半的中间态。
MIGRATION_REVIEW_PROVENANCE = "012_review_via_agent_provenance.sql"
# 013 建 tv_edit_diffs / tv_edit_digest_acks 两张表, 并让 tv_note_links 的 match_kind 多认 'edited'(D-120)。
# 表探得到; CHECK 同 012 用表当代理(同一个事务)。
MIGRATION_TV_EDITS = "013_tv_edits.sql"
# 014 给 tv_edit_diffs 加 diff_rev(差异的口径版本, 话题标签不算正文之后旧行要重算)。
MIGRATION_TV_EDIT_REV = "014_tv_edit_diff_rev.sql"
# 015 建 rule_compiles(规则编译结果的缓存, D-121)。表探得到。
MIGRATION_RULE_COMPILES = "015_rule_compiles.sql"
# 016 换 deskcore_tv_notes 的函数体(不返回 TV v_notes_vanished 里的篇), 签名和返回列没动 —— 同 008, 探不到。
# 第二列是那张视图在不在: 视图不在时 016 照旧全给, 函数体对了也不起作用。
MIGRATION_TV_NOTES_VANISHED = "016_tv_notes_skip_vanished.sql"
# 017 给 tv_note_links 加 tv_text_fp(W-34: TV 正文的指纹), 建 deskcore_tv_clear_lineage(撤回写作台自己写进 TV 的血缘)。
# 列和 RPC 都探得到(RPC 用空 _links, 一行不碰)。
MIGRATION_TV_TEXT_FP = "017_tv_link_text_fp.sql"
# 018 建 draft_snapshots(W-38: 查重时记下的底稿, tv-sync 拿飞书的帖来对)。表探得到。
MIGRATION_DRAFT_SNAPSHOTS = "018_draft_snapshots.sql"
MIGRATION_016_PROBE_SQL = (
    "select p.prosrc like '%v_notes_vanished%' as skips_vanished, "
    "to_regclass('truth_vault.v_notes_vanished') is not null as view_present "
    "from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
    "where n.nspname='autowriter' and p.proname='deskcore_tv_notes';"
)
MIGRATION_008_PROBE_SQL = (
    "select prosrc like '%f.embedding_model = _model%' as has_model_filter "
    "from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
    "where n.nspname='autowriter' and p.proname='deskcore_check_drafts';"
)


def _probe_ok(fn, *, predicate=None) -> tuple[str, str]:
    """跑一个只读探测。返回 (state, note)。

    判据说"这个对象不存在"→ ``missing``; 其余异常 → ``error``(**不是**
    missing) —— 权限、参数、库故障被当成"迁移没跑"正是 store.py 里那一大段
    注释在防的事。

    ⚠️ **两种判据, 按探的是什么分**(codex review · #63, 实测确认):

      · **RPC** 探测用 ``store.rpc_missing``(默认) —— 与运行期降级用的**同一个**
        函数。同源是这里的重点: 自检说"RPC 在", 运行期就不该判它不在。
      · **表 / 列** 探测用 ``store.schema_object_missing`` —— 因为
        ``rpc_missing`` 对缺表返 False: PostgREST 回的是 ``PGRST205
        Could not find the table ... in the schema cache``, 三个条件一个都不沾。
        照旧用它的话, 一个**真的没跑 001** 的库会被报成 ``error`` 并写着
        "不是「没跑迁移」" —— 正好说反, 而这个命令存在的全部意义就是别说反。
        表/列这一路没有运行期对应物(运行期不该因为缺表就降级), 所以宽一档不
        破坏同源那条纪律。

    ⚠️ **"没授权"在两种判据之前先被拎出来, 报成 ``denied``。**
    ``42501 permission denied for table`` 说的是"表建出来了, 但 GRANT 没发",
    它既不是 missing(对象在)也不该混进 error(它有确定的补救 SQL: 007)。
    不单独分一档的话, 一个**只缺 GRANT** 的库会让 001 那几条表探测全报
    ``error`` 并写着"不是「没跑迁移」", 而同一份输出里 007 报 missing ——
    自检工具给出互相矛盾的结论, 比它不存在更坏(与 codex #63 那条同一个道理)。
    """
    check = predicate or store.rpc_missing
    try:
        fn()
    except Exception as exc:                       # noqa: BLE001 — 探测就是要看异常
        if store.table_permission_denied(exc):
            return "denied", f"{type(exc).__name__}: {exc}"[:200]
        if check(exc):
            return "missing", f"{type(exc).__name__}: {exc}"[:200]
        return "error", (f"探测本身失败(不是「没跑迁移」): "
                         f"{type(exc).__name__}: {exc}"[:200])
    return "applied", "ok"


def _probe_signature(client, name: str, wide: dict, narrow: dict,
                     wide_tag: str, narrow_tag: str) -> tuple[str, str]:
    """先按新签名调, 报"找不到"再按旧签名调 —— 与 store.py 的两处同一套路。

    区分"函数完全不存在"和"函数在、但还是旧签名"是这里的全部意义:
    PostgREST 对**参数对不上**的报错文本里也带 ``does not exist``, 只看一次
    调用会把"库停在 004"误报成"001 都没跑"(审计 COR-014 记过这个坑)。
    """
    state, note = _probe_ok(lambda: client.rpc(name, wide).execute())
    if state != "missing":
        return (wide_tag if state == "applied" else state), note
    state, note = _probe_ok(lambda: client.rpc(name, narrow).execute())
    if state == "applied":
        return narrow_tag, "函数在, 但还是旧签名"
    return state, note


def migration_state(client) -> dict:
    """逐个探测: 这个库跑到第几个迁移了, 缺的那些各自会怎样。

    只读。返回 ``{"ok": bool, "checks": [...], "missing": [...],
    "unprobeable": [...]}``; ``checks`` 每项带 ``migration`` / ``state`` /
    ``impact``, 让人不必翻文档就知道缺了要紧不要紧。

    ``ok`` 的口径是**没有一项 missing 或 error**。``unprobeable`` 不算不 ok
    —— 探不到不等于没跑, 把它算进去会让这条命令永远报红, 而永远报红的检查
    等于没有检查。
    """
    checks: list[dict] = []

    def _add(migration: str, what: str, state: str, note: str, impact: str) -> None:
        checks.append({"migration": migration, "probe": what,
                       "state": state, "note": note, "impact": impact})

    # ── 001: 四张表 + items.updated_at ──
    #
    # ⚠️ 取的列是 ``project_id`` 而不是 ``id``。``user_calibration_notes`` 的
    # 主键是 ``(project_id, user_id)`` —— **它没有 id 列**。探一个不存在的列,
    # PostgREST 报的是 `column "id" does not exist`, 而那句话正好被
    # ``rpc_missing`` 认成"迁移没跑" —— 于是在一个 001 明明跑过的库上报 missing,
    # 把人打发去重跑一遍迁移。这个探测器存在的全部意义就是不出这种错。
    #
    # 第一版就是 ``.select("id")``, 而假件不校验列名, 所以测试给了个假的绿 ——
    # 与审计 §0.5 那条"录音机记得不够细, 得到的绿是假的"同一件事。现在
    # ``tests/test_migration_doctor.py`` 会去 001 的 SQL 里核对这个列真的存在。
    for table in _MIGRATION_001_TABLES:
        state, note = _probe_ok(
            lambda t=table: client.table(t)
            .select(_MIGRATION_001_PROBE_COLUMN).limit(1).execute(),
            predicate=store.schema_object_missing)
        _add("001_deskcore.sql", f"表 {table}", state, note,
             "deskcore 整个不可用")
    state, note = _probe_ok(
        lambda: client.table("items").select("id,updated_at").limit(1).execute(),
        predicate=store.schema_object_missing)
    _add("001_deskcore.sql", "items.updated_at", state, note,
         "TV 的 sync_autowriter_decisions_to_prepublish 会降级回只按 created_at, "
         "迟到的人工决策重新开始漏收")

    # 001 建的还有这个 RPC —— 漏探它的代价是**静默的**: 表都在, doctor 报
    # "001 到位", 而 store.reserve_angles 每次都判 RPC 不存在、降级成非原子发牌,
    # 于是两个人同时发牌能拿到同一组角度坐标, 两边都报成功。部分/手工部署或
    # schema 漂移下这是会发生的。(codex review · #63)
    #
    # ``_want: 0`` 是可证明的 no-op: 函数体第一句就是 `IF _want <= 0 THEN
    # RETURN; END IF;`, 连 advisory lock 都还没取。
    state, note = _probe_ok(lambda: client.rpc("deskcore_reserve_angles", {
        "_project_id": _PROBE_NIL_UUID,
        "_candidates": [],
        "_drawn_by": _PROBE_NIL_UUID,
        "_want": 0,
        "_avoid_days": 30,
    }).execute())
    _add("001_deskcore.sql", "deskcore_reserve_angles", state, note,
         "发牌退回非原子路径 —— 两人同时发牌可能拿到同一组角度坐标, "
         "而两边都报成功(台账事后也看不出来)")

    # ── 002: CAS RPC。nil 项目 + 不可能匹配的 witness → UPDATE 命中 0 行 ──
    state, note = _probe_ok(lambda: client.rpc("update_calibration_notes_cas", {
        "_project_id": _PROBE_NIL_UUID,
        "_expected_md5": _PROBE_IMPOSSIBLE_MD5,
        "_notes": "",
    }).execute())
    _add("002_calibration_cas.sql", "update_calibration_notes_cas", state, note,
         "长笔记(>4000 字级)的自动学习静默停摆 —— 四条学习路径全部不再写回, "
         "而且不报错")

    # ── 003: 唯一索引。PostgREST 看不见索引, 只能交出 SQL ──
    _add("003_versions_unique_num.sql", "versions_item_version_uniq (唯一索引)",
         "unprobeable",
         "PostgREST 读不到 pg_indexes; 用 SQL Editor 跑: " + MIGRATION_UNPROBEABLE_SQL,
         "少了数据库层对重复 version_num 的保护(应用层重试本身不依赖它)")

    # ── 004: 指纹计数 RPC ──
    state, note = _probe_ok(lambda: client.rpc(
        "deskcore_fingerprint_counts",
        {"_project_ids": [_PROBE_NIL_UUID]}).execute())
    _add("004_deskcore_check_pushdown.sql", "deskcore_fingerprint_counts",
         state, note, "list_projects 退回逐项目 count(2N+1 次查询)")

    # ── 004/005: check 的两版签名 ──
    state, note = _probe_signature(
        client, "deskcore_check_drafts",
        wide={"_project_id": _PROBE_NIL_UUID, "_rows": [],
              "_contain_min_sample": fp.CONTAIN_MIN_SAMPLE},
        narrow={"_project_id": _PROBE_NIL_UUID, "_rows": []},
        wide_tag="applied", narrow_tag="old_signature")
    _add("005_deskcore_containment.sql", "deskcore_check_drafts(3 参)",
         state, note,
         "缺席→查重退回 Python 逐对比对(慢, 回到 4000 条上限); "
         "旧签名→包含度那一路不发言, **短稿整段照搬长稿在 check 这一关拦不住**")

    # ── 001/005: commit 的两版签名。空 _rows 时函数体的 FOR 一次都不进 ──
    state, note = _probe_signature(
        client, "deskcore_commit_fingerprints",
        wide={"_project_id": _PROBE_NIL_UUID, "_rows": [],
              "_user_id": _PROBE_NIL_UUID,
              "_ngram_hard": fp.NGRAM_JACCARD_HARD,
              "_contain_hard": fp.NGRAM_CONTAIN_HARD,
              "_contain_min_sample": fp.CONTAIN_MIN_SAMPLE},
        narrow={"_project_id": _PROBE_NIL_UUID, "_rows": [],
                "_user_id": _PROBE_NIL_UUID,
                "_ngram_hard": fp.NGRAM_JACCARD_HARD},
        wide_tag="applied", narrow_tag="old_signature")
    _add("005_deskcore_containment.sql", "deskcore_commit_fingerprints(6 参)",
         state, note,
         "缺席→定稿退回直插, check/commit 之间的竞态窗口重新打开; "
         "旧签名→竞态仍关着, 但写入侧不做包含度重查")

    # ── 006: 决策出处三列。⚠️ 这一条不跑是**硬失败**, 不是降级 ──
    state, note = _probe_ok(lambda: client.table("items")
                            .select("id,decision_source,reviewer_id,decided_at")
                            .limit(1).execute(),
                            predicate=store.schema_object_missing)
    _add("006_item_decision_provenance.sql", "items 的决策出处三列", state, note,
         "⚠️ 硬失败: db.update_item_status 无条件写这三列, 缺列会让现有工作台的"
         "「通过 / 打回」、硬规则自动标记、查重自动标记**全部报错**。"
         "这个迁移与其它几个不同, **不是可选的**")

    # ── 007: 四张表的表级 GRANT。⚠️ 也是**硬失败** ──
    #
    # 探的是"读得到吗", 不是"表在吗" —— 上面 001 那几条已经回答了后者, 而
    # 2026-08-26 首次真部署证明这两件事**不是一回事**: 表全在, doctor 全绿,
    # /health 全绿, 而 deskcore 除 list_projects 外每个工具都挂在
    # `42501 permission denied for table draft_fingerprints`。
    #
    # 为什么 001 那几条探测挡不住这个: 它们跑在同一个 service_role 上、报的是
    # 同一个 42501 —— 现在由 _probe_ok 统一识别成 denied, 所以两边给的是同一个
    # 结论, 补救指向同一个文件。这里再单列一条, 是为了让"缺 GRANT"在
    # missing 清单里有个**编号**可跑, 而不是只留一句 note 让人自己想办法。
    #
    # 用 draft_fingerprints 当代表: 四张表在 001 / 007 里是同一条 GRANT 语句
    # 发的, 不存在只授权了其中一张的中间态。
    #
    # ⚠️ **四个权限都要探, 不能只探 SELECT。**(codex review · #65)
    # 只探读的话, 一个"读得到但写不进"的库(手工补授权补漏了 / 后来被人收回过)
    # 会报全绿, 而定稿入库、发牌、存个人笔记照旧在 42501 上挂 —— 正好是这条
    # 检查存在的理由的反面。007 承诺四个权限, 就得验四个。
    #
    # 怎么在【不写一行】的前提下探写权限(与本节第 2 条纪律一致)。
    #
    # ⚠️ 关键在于三个探测都必须是**逻辑上不可能生效**的, 而不是"大概不会命中"。
    # 这是对着生产库跑的命令, "nil UUID 应该没有对应行"这种概率论不够格 ——
    # 真有那么一行的话, UPDATE 会清空它的标题、DELETE 会把它删掉。
    #
    #   · INSERT —— payload 是 ``{"project_id": None}``, 而这一列是 NOT NULL。
    #                权限检查在约束检查【之前】, 所以没权限报 42501, 有权限报
    #                23502(非空约束)。**无论库里有什么数据, 它都插不进去。**
    #   · UPDATE / DELETE —— 过滤是 ``id = X AND id <> X``, 对任何一行都是假。
    #                有权限就是成功的空操作, 没权限才报 42501。
    #
    # tests 里有两条断言守着: 一条钉住这三个形态(哪天谁把矛盾条件改成普通
    # 过滤, 这个自检命令就成了删库命令); 一条让假件真的模拟 NOT NULL ——
    # 探测的安全性靠的是那条约束, 假件不模拟它, 测试给的绿就是假的。
    _grant_probes = (
        ("SELECT", lambda t: t.select(_MIGRATION_001_PROBE_COLUMN).limit(1)),
        ("INSERT", lambda t: t.insert({"project_id": None})),
        ("UPDATE", lambda t: t.update({"title": ""})
                              .eq("id", _PROBE_NIL_UUID)
                              .neq("id", _PROBE_NIL_UUID)),
        ("DELETE", lambda t: t.delete()
                              .eq("id", _PROBE_NIL_UUID)
                              .neq("id", _PROBE_NIL_UUID)),
    )
    for priv, build in _grant_probes:
        state, note = _probe_ok(
            lambda b=build: b(client.table("draft_fingerprints")).execute(),
            predicate=store.schema_object_missing)
        if priv == "INSERT" and state in ("error", "missing"):
            # 有权限时 INSERT 必然挂在非空约束上 —— 那**正是通过**, 不是故障。
            # (也认 23503: 万一哪天这一列不再是 NOT NULL, 外键仍然会拦下。)
            if any(k in note.lower() for k in
                   ("23502", "23503", "not-null", "not null", "foreign key")):
                state, note = "applied", "ok(约束拦下, 说明 INSERT 权限是有的)"
        if state == "missing":
            # 表本身还不在 = 001 都没跑, 上面那几条已经在喊了。这里再喊一遍
            # "007 也缺"只会让人以为要跑两个 —— 而 001 里已经含着同一条 GRANT。
            state, note = "unprobeable", ("001 的四张表还不在, 先跑 001 "
                                          "(它里面已经含着这条 GRANT)")
        _add(MIGRATION_GRANTS,
             f"draft_fingerprints 的 {priv} 权限", state, note,
             "⚠️ 硬失败: service_role 绕过 RLS 但**不绕过表级 GRANT**。缺了它, "
             "deskcore 除 list_projects 外每个工具都在 42501 permission denied 上挂, "
             "而 /health 仍然全绿(它探的是连得上, 不是访问得了)")

    # ── 008: 标题语义比对按 embedding 模型隔离 ──
    #
    # PostgREST 看不见函数体, 所以"这一版有没有那句过滤"从这一面探不到 ——
    # 与 003 的唯一索引同类, 如实报 unprobeable 并把该跑的 SQL 交出来。
    #
    # ⚠️ **别用"函数在不在"冒充这条。** 008 是 CREATE OR REPLACE, 签名和返回列
    # 一个字都没动 —— 跑没跑过, 从调用侧看**完全一样**, 直到某天有人换了模型
    # 才发现比对一直是混着算的。蒙一个 applied 正好复现这套东西要根治的形态。
    _add(MIGRATION_EMBEDDING_ISOLATION,
         "deskcore_check_drafts 按模型过滤(函数体)",
         "unprobeable",
         "PostgREST 读不到函数体; 用 SQL Editor 跑: " + MIGRATION_008_PROBE_SQL,
         "缺席→标题语义那一路把【别的模型产的】历史向量也算进来。跨模型的余弦"
         "是噪声: 既会放过真重复, 也会误杀无关稿, 而 semantic_degraded 照报 "
         "false —— 失灵的同时还说自己跑过了")

    # ── 009: TV 对照的两张表 + 读 TV 的 RPC。只读探测: 表 select 1 行, RPC 用一个
    #    不存在的 TV 项目名(返回空集; 库里没有 truth_vault 时函数自己返回空)。
    for table in _MIGRATION_009_TABLES:
        state, note = _probe_ok(
            lambda t=table: client.table(t)
            .select(_MIGRATION_009_PROBE_COLUMN).limit(1).execute(),
            predicate=store.schema_object_missing)
        _add(MIGRATION_TV_LINKS, f"表 {table}", state, note,
             "tv-sync 整个不可用: TV 的笔记认不回写作台的版本, 已发未入库的稿子"
             "补不进指纹库, 「爆没爆」回不到写作台")
    state, note = _probe_ok(lambda: client.rpc("deskcore_tv_notes", {
        "_tv_project_id": "__doctor_probe__", "_since": None,
        "_after": None, "_limit": 1}).execute())
    _add(MIGRATION_TV_LINKS, "deskcore_tv_notes", state, note,
         "读不到 TV 的笔记 —— tv-sync 一条都对不了")
    # ── 016: 同一个 RPC 换了函数体(飞书里已删的篇不返回)。调用侧看跑没跑过完全一样 —— 与 008 同类, 如实报
    #    unprobeable 并交出 SQL。上面那条 applied 只说明 009 的签名在, 不说明 016 跑过。
    _add(MIGRATION_TV_NOTES_VANISHED,
         "deskcore_tv_notes 跳过飞书里已删的篇(函数体)",
         "unprobeable",
         "PostgREST 读不到函数体; 用 SQL Editor 跑(两列都该是 true): " + MIGRATION_016_PROBE_SQL,
         "缺席→飞书里已删的篇(TV 的 v_notes_vanished)tv-sync 每晚照样读回来重对: 项目有补录目标时, 其中对不上"
         "的会被当成新稿补录成副本、进那个项目的指纹库; 没有补录目标时每晚重写一遍 unmatched。不报错 —— "
         "所以在它跑过之前, 别把有已删篇的 TV 项目的补录目标打开(10-26: BJS_phase1)")
    # 回填 RPC: 空 _links → UPDATE 一行都不碰(codex #81: 只探读的那一半, 部署了一半
    # 的库会被报成 applied, 而 --write-tv 到运行时才炸)。
    state, note = _probe_ok(lambda: client.rpc("deskcore_tv_backfill_lineage",
                                               {"_links": []}).execute())
    _add(MIGRATION_TV_LINKS, "deskcore_tv_backfill_lineage", state, note,
         "tv-sync --write-tv 到运行时才失败: 对照写不回 TV 的 source_autowriter_*")
    # 补录锁: 表 + 放锁 RPC(nil 项目 + 探针专用 holder: 那一行逻辑上不可能存在,
    # DELETE 命中 0 行)。拿锁 RPC 不探 —— 探一次就真的会写一行。
    state, note = _probe_ok(
        lambda: client.table("ingest_locks").select("project_id").limit(1).execute(),
        predicate=store.schema_object_missing)
    _add(MIGRATION_TV_LINKS, "表 ingest_locks", state, note,
         "补录退回进程内锁: 服务进程与 tv-sync 进程重叠时同一批稿子会建两份身份")
    state, note = _probe_ok(lambda: client.rpc("deskcore_ingest_unlock", {
        "_project_id": _PROBE_NIL_UUID, "_holder": "__doctor_probe__"}).execute())
    _add(MIGRATION_TV_LINKS, "deskcore_ingest_unlock", state, note,
         "同上: 跨进程互斥失效(deskcore_ingest_lock 与它同一个迁移, 不单独探)")

    # ── 010: 009 那三张表开 RLS。PostgREST 这一面探不到 relrowsecurity(service_role
    #    绕 RLS, 开没开读起来一样), 与 003 / 008 同类: 如实报 unprobeable 并交出
    #    该跑的 SQL。漏跑不影响任何功能, 只是 Supabase advisor 会一直报。
    _add(MIGRATION_TV_LINKS_RLS,
         "tv_project_map / tv_note_links / ingest_locks 的 RLS",
         "unprobeable",
         "PostgREST 读不到 pg_class.relrowsecurity; 用 SQL Editor 跑: " + MIGRATION_010_PROBE_SQL,
         "功能不坏(anon 对这三张表没有表级 GRANT, service_role 绕 RLS); 差的是 "
         "Supabase advisor 一直报 rls_disabled, 以及以后谁给 anon 发了 GRANT 会一下子"
         "把整张表露出去")

    # ── 011: 发牌台账接上结果的视图。只读: select 1 行。
    #
    # ⚠️ 没授权要报 missing 并指向 011, **不走** denied → 007 那条聚合: 视图的 GRANT
    # 在 011 自己里面, 007 一行视图权限都不管, 指去跑 007 修不好它。PG 对视图报的是
    # "permission denied for view", table_permission_denied 不认它 —— 所以这里自己认。
    def _view_absent_or_ungranted(exc: Exception) -> bool:
        return (store.schema_object_missing(exc)
                or "permission denied for view" in str(exc).lower())
    state, note = _probe_ok(
        lambda: client.table(_MIGRATION_011_VIEW).select("ledger_id").limit(1).execute(),
        predicate=_view_absent_or_ungranted)
    if state == "denied" or (state == "missing" and "permission denied" in note.lower()):
        state, note = "missing", ("视图在, 但 service_role 没有 SELECT —— 重跑 011"
                                  "(它自己带 GRANT, 不是 007)。" + note)
    _add(MIGRATION_ANGLE_OUTCOMES, f"视图 {_MIGRATION_011_VIEW}", state, note,
         "发牌加权看不到结果(哪些坐标出了爆文); 不影响任何现有功能。⚠️ 视图不在也可能"
         "是跑 011 时库里还没有 truth_vault.notes —— 那时它只打一行 NOTICE 跳过, TV 落库"
         "之后重跑 011 即可")

    # ── 012: 人审出处补两列 + CHECK 多认 human_via_agent(审计 A-01) ──
    state, note = _probe_ok(lambda: client.table("items")
                            .select("id,decision_note,decided_within_s")
                            .limit(1).execute(),
                            predicate=store.schema_object_missing)
    _add(MIGRATION_REVIEW_PROVENANCE, "items 的 decision_note / decided_within_s 两列",
         state, note,
         "⚠️ 硬失败, 但只打 review_drafts: 它无条件写 human_via_agent + 这两列, 缺了"
         "每条都报 failed(db.update_item_status 会翻译成人话)。Streamlit 的通过 / 打回"
         "仍写 human + 三列, 不受影响")

    # ── 013: 人在发布前改了什么 + 'edited' 一档(D-120 / W-23) ──
    state, note = _probe_ok(lambda: (client.table("tv_edit_diffs").select("note_id, edited_sentences").limit(1).execute(),
                                     client.table("tv_edit_digest_acks").select("project_id").limit(1).execute()),
                            predicate=store.schema_object_missing)
    _add(MIGRATION_TV_EDITS, "表 tv_edit_diffs(含 edited_sentences 列) / tv_edit_digest_acks", state, note,
         "⚠️ 硬失败, 但只打两处: tv-sync 写 match_kind='edited' 的对照会被旧 CHECK 拒(整块 upsert 失败, "
         "那一块的对照当晚写不进去); 改动差异和每周摘要没有(tv-sync 对照照写、退出码 1; open_project 不带摘要)")

    # ── 014: 改动差异的口径版本 ──
    state, note = _probe_ok(lambda: client.table("tv_edit_diffs").select("note_id, diff_rev").limit(1).execute(),
                            predicate=store.schema_object_missing)
    _add(MIGRATION_TV_EDIT_REV, "tv_edit_diffs 的 diff_rev 列", state, note,
         "改动差异写不进去、每周摘要读不出来(tv-sync 对照照写、退出码 1; open_project 不带摘要)")

    # ── 017: 对照记 TV 正文的指纹 + 撤回自己写进 TV 的旧血缘(W-34) ──
    state, note = _probe_ok(lambda: client.table("tv_note_links").select("note_id, tv_text_fp").limit(1).execute(),
                            predicate=store.schema_object_missing)
    _add(MIGRATION_TV_TEXT_FP, "tv_note_links 的 tv_text_fp 列", state, note,
         "⚠️ 硬失败: tv-sync 读对照选不到这一列, 每个 TV 项目都失败、退出码 1、一行不写(不写错数据)。先跑 017 再部署代码")
    state, note = _probe_ok(lambda: client.rpc("deskcore_tv_clear_lineage", {"_links": []}).execute())
    _add(MIGRATION_TV_TEXT_FP, "deskcore_tv_clear_lineage", state, note,
         "tv-sync --write-tv 撤回不了 TV 里的旧血缘: TV 正文整篇换了的对照每晚报错、不挪(退出码 1), 差异先隔离")

    # ── 015: 规则编译结果的缓存(D-121) ──
    state, note = _probe_ok(lambda: client.table("rule_compiles").select("memory_id, sha, result").limit(1).execute(),
                            predicate=store.schema_object_missing)
    if state == "denied":
        state, note = "missing", "表在, 但 service_role 缺权限 —— 重跑 015(它自己带 GRANT, 不是 007)。" + note
    _add(MIGRATION_RULE_COMPILES, "表 rule_compiles", state, note,
         "可降级: compile-rules 和后台补编读不了缓存就报 error、一条都不发(不花编译的钱); check_rules 读不到编译结果"
         "就回 status=error、记日志(写手那边是这次规则没查成, 不是 500), 简报和存档不受影响")

    # ── 018: 查重时记下的底稿(W-38) ──
    state, note = _probe_ok(lambda: client.table("draft_snapshots")
                            .select("id, project_id, text_fp, promoted_at").limit(1).execute(),
                            predicate=store.schema_object_missing)
    if state == "denied":
        state, note = "missing", "表在, 但 service_role 缺权限 —— 重跑 018(它自己带 GRANT, 不是 007)。" + note
    _add(MIGRATION_DRAFT_SNAPSHOTS, "表 draft_snapshots", state, note,
         "可降级(但该先跑 018 再部署): check_drafts 照常判, 只是不记底稿(记 WARN); tv-sync 跳过底稿那一遍、照旧补录 —— "
         "模型没调 score_drafts 的稿子发出去以后认不回写作台(当外面写的补录成副本, 牌不销、没有改稿差异)")

    # ⚠️ ``error`` 不进 ``missing``(codex review · #63)。原来它进 —— 于是一次
    # 权限/连通性故障会让 doctor 打印"还缺这些迁移, 按编号顺序跑", 把人指去跑
    # 一遍根本不缺的 SQL, 而同一份输出里那条 note 明明写着"不是「没跑迁移」"。
    # 一个自检工具给出**互相矛盾的**结论, 比它不存在更坏。
    #
    # 两者都判红(见 ok), 但补救方式完全不同, 所以必须分开报。
    #
    # ``denied`` 同理再分一档: 它既不是"没跑迁移"(对象在), 也不是"探测本身坏了"
    # (它有确定的补救 SQL)。但它**进 missing** —— 因为对跑这条命令的人来说,
    # 该做的事就是去跑 migrations/007, 与其它缺席迁移的动作完全一样。
    #
    # ⚠️ **denied 一律记到 007 名下, 不管这条探测挂在哪个迁移上。**(codex #65)
    # 缺 GRANT 时 001 那四条表探测也会报 denied —— 按"探测挂在哪个迁移就算哪个"
    # 聚合的话, missing 里会同时出现 001 和 007, 而 doctor 紧接着打印"还缺这些
    # 迁移, 按这个顺序跑"。于是人对着一个**表和函数都好好在那儿**的库重跑一遍
    # 建表 SQL(幂等, 什么都不会变), 然后继续 42501。
    #
    # 那些 denied 的探测行**照常显示**(state 就是 denied, 一眼看出是哪几张表),
    # 只是不进【该跑什么】那份清单 —— 清单要回答的是动作, 不是现象。
    # 这跟上面 error 不进 missing 是同一条纪律: 一个自检工具给出互相矛盾的
    # 结论, 比它不存在更坏。
    denied = sorted({c["migration"] for c in checks if c["state"] == "denied"})
    missing = sorted(
        {c["migration"] for c in checks
         if c["state"] in ("missing", "old_signature")}
        | ({MIGRATION_GRANTS} if denied else set()))
    errors = sorted({c["migration"] for c in checks if c["state"] == "error"})
    return {
        "ok": not missing and not errors,
        "checks": checks,
        "missing": missing,
        "denied": denied,
        "errors": errors,
        "unprobeable": sorted({c["migration"] for c in checks
                               if c["state"] == "unprobeable"}),
    }


# ── 补录的互斥(codex review #81 P1) ─────────────────────────────────────
# ingest_published 的幂等靠"先读库里有没有指纹、再写"; 两个调用重叠时都过完那
# 一读再各写一份。调用方有两种进程: 服务进程(WorkBuddy 工具)和 CLI/cron
# (tv-sync), 进程内的锁管不到对方, 所以主锁是库里的一行(migrations/009 的
# ingest_locks, 带 TTL, 持有方崩了也能被接管); 进程内锁只是省得同进程的并发
# 请求都去轮询库。锁 RPC 没部署(doctor 会报)时退回进程内锁并记 warning ——
# 那是降级, 不是"拿到了"。
INGEST_LOCK_TTL_SEC = 600          # 一批 ≤ 50 条远用不了; tv-sync 一个项目几百条也够
INGEST_LOCK_WAIT_SEC = 90          # 等不到就报 IngestBusy, 让调用方稍后再来
INGEST_LOCK_POLL_SEC = 2.0
INGEST_EMBED_CHUNK = 100           # embed_content 单次上限; 也限住指纹请求体
_ingest_proc_locks: dict[str, threading.Lock] = {}
_ingest_proc_guard = threading.Lock()


class IngestBusy(RuntimeError):
    """另一个补录正在这个项目上跑(锁被别的进程持有, 等了 INGEST_LOCK_WAIT_SEC 还没放)。
    REST 层映成 409: 不是坏了, 稍后重来即可。"""


def _ingest_proc_lock(project_id: str) -> threading.Lock:
    with _ingest_proc_guard:
        return _ingest_proc_locks.setdefault(str(project_id), threading.Lock())


def _acquire_ingest_lock(client, project_id: str, holder: str) -> bool | None:
    """轮询拿库锁。True 拿到; None 锁 RPC 没部署(降级); 等超时抛 IngestBusy。"""
    deadline = time.monotonic() + INGEST_LOCK_WAIT_SEC
    while True:
        got = store.try_ingest_lock(client, project_id, holder, INGEST_LOCK_TTL_SEC)
        if got is None:
            logger.warning("ingest lock RPC 没部署(migrations/009), 本次只用进程内锁 "
                           "(project=%s)", project_id)
            return None
        if got:
            return True
        if time.monotonic() >= deadline:
            raise IngestBusy(f"项目 {project_id} 上另一个写操作(补录或入库)正在跑(锁被别的"
                             f"进程持有), 等了 {INGEST_LOCK_WAIT_SEC}s 没放 —— 稍后再试")
        time.sleep(INGEST_LOCK_POLL_SEC)


@contextlib.contextmanager
def _project_write_lock(client, project_id: str, holder: str):
    """项目级写锁: 进程内锁 + 009 的库锁(RPC 没部署就只有进程内那把)。

    2026-09-18 起 commit_drafts 也拿它(codex #84 P1): 改稿替换要"先摘旧指纹、再查重
    写入", 这两步不在一个事务里 —— 中间那一瞬别的 commit 能把与旧稿相同的文字塞进来。
    补录和入库都过这把锁, 那个窗口就关上了。代价是每次 commit 多两次 RPC, 且补录
    正在跑时入库要等(最多 INGEST_LOCK_WAIT_SEC, 之后 IngestBusy → 409, 重试即可)。
    """
    with _ingest_proc_lock(project_id):
        got = _acquire_ingest_lock(client, project_id, holder)
        try:
            yield got
        finally:
            if got:
                try:
                    store.ingest_unlock(client, project_id, holder)
                except Exception:                    # noqa: BLE001
                    logger.exception("project write lock release failed (project=%s, "
                                     "holder=%s); 锁会在 TTL 后自动过期", project_id, holder)


def ingest_published(client, project_id: str, entries: list[dict], *,
                     user_id: str, source: str, dry_run: bool = False) -> dict:
    """``_ingest_published_unlocked`` 的加锁形态 —— 调用方一律走这个。

    dry_run 不写库, 不拿锁(拿了只会让真在跑的补录多等)。
    """
    # 先过归属闸再去拿锁: 拿不到锁要等 90s, 等完再告诉人"不是你的项目"没有道理。
    assert_project_access(client, project_id, user_id=user_id)   # 审计 COR-015
    if dry_run:
        return _ingest_published_unlocked(client, project_id, entries, user_id=user_id,
                                          source=source, dry_run=True)
    holder = f"{source}:{uuid.uuid4().hex[:8]}"
    with _project_write_lock(client, project_id, holder):
        return _ingest_published_unlocked(client, project_id, entries, user_id=user_id,
                                          source=source, dry_run=False)


def _ingest_published_unlocked(client, project_id: str, entries: list[dict], *,
                               user_id: str, source: str, dry_run: bool = False) -> dict:
    """把【已经发出去、但没走 commit_drafts】的稿子补进库: 身份 + 指纹, **不过闸**。

    ── 为什么不过闸 ────────────────────────────────────────────────────
    这些稿子已经发在小红书上了。它跟库里谁重复都改变不了这个事实, 拦它没有
    任何意义 —— 拦了, 下一批照样撞上它。回填的逻辑(backfill_fingerprints)是
    一样的: 已发生的历史原样入库。

    ── 为什么要建身份 ──────────────────────────────────────────────────
    只写指纹也能挡重复, 但 items/versions 行是导出 lineage 和 TV 归因的凭据;
    这些稿子既然发出去了, 之后指标回流时 TV 会按 version_id 找它们。
    出处记在 ``batches.params``({"source": "ingest", "file": 表名}), 让人以后
    分得清这批是补进来的, 不是写作台当场写的。**不碰 items.external_source**:
    那列是 TV 同步的标记('truth_vault'), TV 按它认自己的行, 借用会搅乱对接。

    ── 幂等(code review 2026-09-17)────────────────────────────────────
    重跑必须安全: 运营看到一条警告就会再跑一遍。所以入库前先按正文开头哈希
    (``opening_hash``)查一遍指纹库, **已经有指纹的行跳过**; 同一张表里正文相同
    的行也只收第一条。两种跳过都数出来报。

    但"身份建了、指纹没写"那种半途失败**不是靠重跑修**: 重跑看不到它的指纹,
    会再建一份身份。修法是 ``backfill --project`` —— 它按 version_id 幂等, 会把
    有身份没指纹的行补上。返回里 ``fingerprinted < minted`` 时就该走那条路。

    ── 为什么这里【不】调入库判定(judge, 2026-09-24 定的覆盖面)──────────
    commit_drafts 入库后会把稿子发给 judge(docs/deskcore.md §3.8), 这条补录路径刻意
    不发。D-071 的实查是写手没散场、只是稿子在外面写, 发布后由 tv-sync 倒灌回来 ——
    走到这里的**全是已经发布的笔记**: 它们在 TV 的 ``truth_vault.notes`` 里, 由 TV 的
    事后特征抽取(docs/31 位置 ①, 同一把 fq 尺子, ``subject_type='note'``)覆盖。再以
    ``aw_version`` 判一遍是同一篇文字在账本里记两份、两种主体, 而"写前拦一下"对已发布的
    稿子又没有意义。所以入库判定的覆盖面是「走写作台交付的稿」, 在外面写的那部分交给 TV。
    ⚠️ 前提是那条笔记**在 TV 里**: 从飞书表 ``ingest --xlsx`` 补进来、而那张表没接 TV 同步
    的(Hatherine 就是), 两边都判不到 —— 补救是把表接进 TV, 不是在这里补一刀判定。

    ``dry_run`` 只数不写(但会查已有指纹, 所以 dry-run 的数字就是真跑会写的数)。
    """
    assert_project_access(client, project_id, user_id=user_id)   # 审计 COR-015
    cleaned = [{"title": (e.get("title") or "").strip(),
                "body": (e.get("body") or "").strip(), "index": i}
               for i, e in enumerate(entries)]
    cleaned = [e for e in cleaned if e["title"] or e["body"]]
    # 表内去重 + 库内去重, 都按【开头哈希 + 整篇四字串 sketch】—— 只按开头会把
    # 同一个模板开头的不同稿子全判成重复(codex review; 见 store 里那段说明)。
    # 没有正文(title-only)的行算不出任何一样, 只能照收 —— 它们本来也拦不住谁。
    dup_in_sheet = already = 0
    seen: set[tuple] = set()
    keys = [(fp.opening_hash(e["body"]), tuple(sorted(fp.ngram_hashes(e["body"]))))
            for e in cleaned]
    known = store.existing_fingerprint_sketches(
        client, project_id, [oh for oh, _ in keys if oh])
    keep: list[dict] = []
    for e, (oh, sk) in zip(cleaned, keys):
        if oh and (oh, sk) in seen:
            dup_in_sheet += 1
            continue
        if oh and sk in known.get(oh, ()):
            already += 1
            continue
        if oh:
            seen.add((oh, sk))
        keep.append(e)
    cleaned = keep
    out = {"received": len(entries), "to_write": len(cleaned),
           "skipped_already_fingerprinted": already,
           "skipped_duplicate_in_sheet": dup_in_sheet,
           "minted": 0, "fingerprinted": 0, "batch_id": None,
           "identity_error": None, "fingerprint_error": None,
           "embedded": False, "dry_run": dry_run, "written": []}
    if dry_run or not cleaned:
        return out
    to_mint = [{"version_id": str(uuid.uuid4()), "title": e["title"],
                "body": e["body"], "keywords": [], "index": e["index"]} for e in cleaned]
    minted = store.mint_draft_identity(client, project_id, str(user_id), "",
                                       to_mint,
                                       batch_params={"source": "ingest",
                                                     "file": source})
    done = [m for m in to_mint if m["version_id"] in minted.get("versions", {})]
    out.update(minted=len(done), batch_id=minted.get("batch_id"),
               identity_error=minted.get("error"),
               # 调用方传进来的第几条 → 建成的 version_id(tv_sync 记对照要用)
               written=[{"index": m["index"], "version_id": m["version_id"]} for m in done])
    if not done:
        return out
    # 按 INGEST_EMBED_CHUNK 条一批: embed_content 单次最多 100 条, 指纹行带 400 个
    # 四字串 hash + 768 维向量, 几百行一个请求体会顶到网关上限。tv-sync 一次
    # 补几百条是常态(2026-09-17 dry-run: sportsix 461 / 雷诺考特 357 / 西屋 301)。
    embedded_any = False
    can_embed = dedup.embeddings_available()
    for start in range(0, len(done), INGEST_EMBED_CHUNK):
        chunk = done[start:start + INGEST_EMBED_CHUNK]
        vecs = dedup.embed_texts([m["title"] for m in chunk]) if can_embed else None
        payload = []
        for i, m in enumerate(chunk):
            vec = vecs[i] if vecs and i < len(vecs) and vecs[i] else None
            payload.append({
                "project_id": project_id, "user_id": user_id,
                "version_id": m["version_id"], "title": m["title"],
                "opening": fp.opening_of(m["body"]),
                "title_embedding": vec,
                "embedding_model": dedup.EMBEDDING_MODEL if vec else None,
                "opening_hash": fp.opening_hash(m["body"]),
                "ngram_hashes": fp.ngram_hashes(m["body"]),
                "angle_key": None,       # 补进来的稿子不是发牌产出的, 没有坐标
            })
        # ⚠️ 身份已经建好了。这一步再抛出去, 调用方(CLI)就到不了"跑 backfill"那句,
        #    而一次自然的重跑看不到这几条的指纹, 会再建一份身份(codex review)。
        #    所以吞掉、写进返回值, 让 CLI 把正确的补救路径说出来。
        try:
            out["fingerprinted"] += store.write_fingerprints(client, payload)
        except Exception as exc:  # noqa: BLE001
            logger.exception("ingest: 身份建成 %d 条后写指纹失败 (project=%s, 已写 %d)",
                             len(done), project_id, out["fingerprinted"])
            out["fingerprint_error"] = f"{type(exc).__name__}: {exc}"[:300]
            break
        embedded_any = embedded_any or bool(vecs)
    out["embedded"] = embedded_any
    return out


# ── 「发了角度没入库」: 流程漏斗的泄漏 ──────────────────────────────────
# 09-11 起 78% 的角度发出去了、稿子没入库, 这个数字在库里躺了一周没人看 ——
# 服务本身一直是 ok 的, 而它是使用层的故障。所以 doctor 和 /health 都报它。
LEAK_WINDOW_DAYS = 7
LEAK_MIN_DRAWN = 20        # 样本太小不报警: 一个人试写 5 条没入库不是事故
LEAK_ALERT_PCT = 50


def pipeline_leak(client, days: int = LEAK_WINDOW_DAYS) -> dict:
    """近 ``days`` 天: 发了多少角度、多少条真的入库销了账, 按项目分。

    ``ok`` 为 False 的判据: 发了至少 LEAK_MIN_DRAWN 个角度, 且超过 LEAK_ALERT_PCT
    没入库。健康的日子(09-10)是 18%; 塌了的日子(09-14/15)是 100% / 88%。
    这不是服务健康 —— /health 的顶层 ok 不看它; 它说的是"流程在漏"。
    """
    per = store.angle_leak(client, days)
    # W-22: 同一窗口里从飞书同步回来的补录副本数。只作【原因标签】, 不算交付、不改 ok ——
    # 副本没有 angle_key, 对不上任何一张牌; 算交付会让灯在它要抓的失败上变绿(没销账的
    # 坐标会被重抽, 指纹查重抓不到同一个故事换个角度; codex review on TV #189)。
    try:
        copies = store.ingest_copy_counts(client, [p["project_id"] for p in per],
                                          since=store.iso_ago(days), strict=True)
    except Exception:  # noqa: BLE001 —— 标签拿不到不影响灯本身
        logger.exception("pipeline_leak: ingest copy counts failed")
        copies = None
    drawn = sum(p["drawn"] for p in per)
    consumed = sum(p["consumed"] for p in per)

    def _breach(d: int, c: int) -> bool:
        return d >= LEAK_MIN_DRAWN and round(100 * (d - c) / d) > LEAK_ALERT_PCT

    for p in per:
        p["leak_pct"] = (round(100 * (p["drawn"] - p["consumed"]) / p["drawn"])
                         if p["drawn"] else 0)
        p["ok"] = not _breach(p["drawn"], p["consumed"])
        p["synced_back"] = None if copies is None else copies.get(str(p["project_id"]), 0)
        if p["ok"]:
            p["likely_cause"] = None
        elif copies is None:
            p["likely_cause"] = "unknown"
        elif p["synced_back"]:
            p["likely_cause"] = "delivered_without_commit"   # 稿子进了飞书、没走 commit: 坐标没销账, 可能被重抽
        else:
            p["likely_cause"] = "not_delivered"              # 既没入库, 飞书里也没同步回来
    pct = round(100 * (drawn - consumed) / drawn) if drawn else 0
    # ⚠️ 逐项目也判, 不只看总量: 一个 30/30 全漏的项目会被另一个 100/100 全入库
    #    的大项目摊成 23%、判成健康 —— 而那个小项目的指纹一条都没进(codex review)。
    over = [p for p in per if not p["ok"]]
    ok = not _breach(drawn, consumed) and not over
    return {
        "window_days": days, "drawn": drawn, "consumed": consumed,
        "leak_pct": pct, "ok": ok,
        "projects_over_threshold": len(over),
        # 副本数读失败时给 None 而不是 0: /health 只露这个总数, 0 会把"查不到"说成"一个都没有"(codex review on #103)
        "projects_delivered_without_commit": (None if copies is None else
                                              sum(1 for p in over if p.get("likely_cause") == "delivered_without_commit")),
        "projects": per,
        "note": ("发出去的角度里还没销账的比例(销账 = 模型调了 score_drafts, 或者 W-38 起: 查重时记下的底稿发出去以后被"
                 "tv-sync 从飞书对回来)。只作参考: 没写出来、写了没发的牌本来就不销, 这个数高不等于漏了稿, 更不是写手的事 ——"
                 "写手没有存档这一步。要看发出去的帖对没对回写作台, 看 TV 夜跑的角度台账灯(判据 C)。超过 "
                 f"{LEAK_ALERT_PCT}% 时 likely_cause="
                 "delivered_without_commit 是同期有补录副本(发出去的帖没对回写作台)"
                 + ("" if ok else
                    f" —— 现在就超了({len(over)} 个项目单独超线)" if over
                    else " —— 现在就超了")),
    }


def backfill_gap(client, project_id: str) -> dict:
    """这个项目的指纹回填还差多少 —— 用**与 backfill 完全同一套口径**算。

    ⚠️ 故意不做归属校验, 理由同 backfill_fingerprints / reembed_fingerprints:
    这是运维命令, 跑它的人手里握着 service_role key。

    ── 为什么不是一句 SQL ──────────────────────────────────────────────
    runbook 里原本给的是一段手写 SQL(``items × batches × versions`` 三表 join)。
    那是把回填的口径**抄了第二份**: backfill 只取 items × versions 里"有版本的
    item", 而抄出来的那份一旦和 `store.legacy_version_pages` 漂开, 验收标准
    就会在"主力项目还差几百条"的时候报绿 —— 而验收标准报绿正是本仓最怕的那
    一类失败。这里直接调回填自己用的那两个函数, 两者不可能漂。

    ⚠️ **扫全量, 不带上限**(``limit=None``)。``legacy_version_pages`` 默认
    ``limit=5000`` —— 沿用它的话, 一个 5000 条以上的项目在最新那 5000 条补齐之后
    就会被报成"还差 0 / done"，而更老的历史稿从来没进过查重基线。**一个只看了
    前 5000 条的验收标准报出来的绿是假的**, 而验收标准报假绿正是本仓最怕的那类
    失败(runbook §5 整节)。(codex review · #63)

    回填本身**仍然**带 5000 上限, 那是它的已知边界 —— 所以 ``eligible`` 超过
    上限时这里会带一条 ``backfill_capped_warning``: 那种项目靠 backfill 追不平,
    需要先把 cap 调高。诚实地说出来, 好过让两个数永远对不上而没人知道为什么。

    只读: 只数数, 不写指纹。
    """
    done = store.existing_fingerprint_version_ids(client, project_id)
    eligible: set[str] = set()
    ingest_copies = 0
    for page in store.legacy_version_pages(client, project_id, limit=None):
        for row in page:
            vid = row.get("version_id")
            if vid:
                eligible.add(str(vid))
                if row.get("ingest"):
                    ingest_copies += 1

    todo = eligible - done
    out = {
        "project_id": project_id,
        "eligible": len(eligible),         # backfill 口径下"应有"的条数(含补录副本: 它们也要指纹)
        # 其中从 TV 补录进来的副本(TV 审计 B-19)。eligible 里它们占大头时, "这个项目有 N 条待回填"
        # 读起来像写作台写了 N 篇 —— 不是。backfill 自己的 5000 上限也会被副本先吃掉。
        "ingest_copies": ingest_copies,
        "backfilled": len(eligible & done),
        "todo": len(todo),
        # ⚠️ 真的去数一次行数。这里原来写的是 len(done) —— 而 done 来自
        # existing_fingerprint_version_ids, 它过滤掉了 version_id 为空的行、
        # 还去了重。也就是说它**恰恰数不到**写作台经 commit_drafts 写进来的
        # 那批(那批的 version_id 就是空的), 而 CLI 的说明文字还专门写着
        # "含 commit_drafts 写进来的" —— 正好说反。(codex review · #63)
        "fingerprints_total": store.fingerprint_row_count(client, project_id),
    }
    out["done"] = not todo
    if todo:
        out["next"] = (f"python -m deskcore.cli backfill --project {project_id}")
    if len(eligible) > _BACKFILL_DEFAULT_CAP:
        out["backfill_capped_warning"] = (
            f"这个项目有 {len(eligible)} 条待回填, 超过 backfill 自己的上限 "
            f"{_BACKFILL_DEFAULT_CAP} —— 跑一次追不平, 得先把 "
            f"store.legacy_version_pages 的 limit 调高")
    return out


# delivery_format 的「只看, 不写」: 不能拿 None 当这个记号 —— --set 的文件里写的是 JSON null 时 template 也是 None, 会被当成
# --show, 一个字不写还退出码 0(codex review on aw#115)。null 要走校验、报「delivery 应该是 JSON 对象」; 删模板用 clear。
SHOW_ONLY = object()


def delivery_format(client, project_id: str, *, template=SHOW_ONLY, clear: bool = False) -> dict:
    """运维命令 ``cli delivery-format``: 看 / 设 / 清一个项目的交付模板(``default_params.delivery``, 2026-10-20)。

    不收 user: 同 backfill, 跑它的人握着 service_role key(docs/deskcore.md §2.2.1)。写手那边没有这个工具 ——
    版式是项目定的, 不该由写手的模型在对话里改。
    写之前先校验(delivery.validate), 存规范形状; 写回时保留 default_params 里别的键(isolation_terms 等), 仍存成
    JSON 字符串(生产库 80 行都是这个形状)。default_params 本身解析不了就拒绝写: 拿 {} 去合并会把别的键一起抹掉。
    ``template`` 不传(SHOW_ONLY)是只看; 传了什么都要过校验, 传 None 也一样(抛 TemplateError, 不写)。
    """
    project = store.project_row(client, project_id)
    if project is None:
        raise ProjectNotFound(f"project not found: {project_id}")
    params = delivery.parse_params(project.get("default_params"))
    out = {"project_id": project_id, "project_name": project.get("name") or ""}
    if template is not SHOW_ONLY or clear:
        if clear:
            params = {k: v for k, v in params.items() if k != "delivery"}
        else:
            params = {**params, "delivery": delivery.validate(template)}
        store.set_project_default_params(client, project_id, json.dumps(params, ensure_ascii=False))
        out["written"] = True
        project = store.project_row(client, project_id) or {}
    try:
        tpl = delivery.template_of(project)
    except delivery.TemplateError as exc:
        out.update(delivery=None, error=str(exc))
        return out
    out["delivery"] = tpl
    if tpl is not None:
        out["preview"] = delivery.preview(tpl)["text"]
    return out


# ══════════════════════════════════════════════════════════════════════
# 反馈学习
# ══════════════════════════════════════════════════════════════════════

# ⚠️ 这里【故意没有】resolve_model / 任何 LLM 调用。
#
# deskcore 曾经自己拿 Anthropic 客户端做调校笔记蒸馏, 那违反了本方案自己的
# 原则 ——「推理归 WorkBuddy, MCP 只做轻量数据操作」。蒸馏现在由调用方模型做
# (见 build_distillation_task / save_my_style), 服务端只出材料和口径。
#
# 收益不只是"少一个依赖": deskcore 不再需要 ANTHROPIC_API_KEY / DESKCORE_MODEL,
# 少一个中转站故障点, 也不再有"回显的模型和实际调用的模型不同源"这类问题 ——
# 那个坑上一轮刚踩过(/health 读 env, core 读一个 config 里根本不存在的属性,
# 于是回显着一个从未被调用过的模型名)。没有模型可配, 就没有配错的余地。
#
# ⚠️ 加新功能时别顺手把 LLM 调用加回来。要模型干活就把材料和指令交出去,
# 让调用方的模型做 —— 它本来就跑在一个有模型的环境里。



# ── 人在发布前改了什么 → 每周一份摘要(2026-10-19, D-120 / W-23) ────────────────────────────────
# tv-sync 每晚给对上的稿子算改动差异(tv_edit_diffs)。open_project 带摘要(ack 之后一周内不再带, 没 ack 就每次打开都带), 由写手的模型归纳成规则建议、
# 问过用户再 record_rule —— 和 record_edit 的蒸馏同一个分工: 提炼是文本活, 交给本来就在跑的模型, deskcore 不调 LLM。
EDIT_DIGEST_EVERY_DAYS = 7
EDIT_DIGEST_MIN_CHANGED = 3       # 改过的少于这几篇不出摘要: 一两篇看不出规律, 只会诱导模型硬编
EDIT_DIGEST_EXAMPLES = 6
EDIT_DIGEST_TASK = (
    "先把 message 原样告诉用户(放在这一轮回复最前面), 再照下面做完, 然后才开始写稿。"
    "这是本项目最近发出去的稿子在发布前被人改过的地方(写作台那版 → 发出去那版)。看有没有反复出现、像是有意为之的改法"
    "(比如总删掉某类句子、总把某种说法换成另一种、总把标题改短、总少提或多提品牌), 归纳成最多 3 条能写成规则的话, "
    "念给用户听, 问要不要记下。"
    "用户说要的, 用 record_rule 记(soft, scope=project)。只出现一两次的、说不清为什么改的, 不要硬编成规则; "
    "一条都看不出来就直接跟用户说没看出规律。"
    "不管记没记, 最后调 ack_edit_digest, through 原样传回, rules_recorded 填记了几条; 之后一周内不会再出现。")


EDIT_DIGEST_MESSAGE = ("开写之前说一句: 这个项目最近发出去的稿子里有 {changed} 篇在发布前被人改过。"
                       "我先看看有没有反复出现的改法, 有的话列给你, 你看哪几条要记成以后都照着写的规则(都不要也行)。")


def _avg(xs) -> int | None:
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(sum(xs) / len(xs)) if xs else None


# ── 发出去的帖写作台没接住(desk_gap, 2026-10-19 TV O-04) ────────────────────────
# 途鸽 9-16 之后写作台一次调用都没收到, 10-12 那周发出去 22 篇全是补录副本; 同一位写手别的项目照常在写作台里写。
# owner 10-19 确认 WorkBuddy 的写作台连接是全局的(新开对话就能列项目), 那一波是 skill 没被调起来、没开项目就写了。
# 漏接的那些对话里写作台没被调用, 在那里提醒不了 —— 所以在调到写作台的对话里(open_project / list_projects)
# 告诉运营: 你名下哪个项目最近发出去的帖写作台没接住, 下次写它先说「打开它」。口径同 TV 角度台账
# 那盏灯的判据 C: 近 7 天发出去的帖里补录副本 >= 10 篇、对上 <= 2 篇(对上只认写作台那版早于发布的), 这个项目
# 在写作台里发过牌(不限时间: 断得越久越该说, codex review on TV#205)。
#
# 2026-10-30 发布收尾: list_projects / open_project 不再带它。一个团队共用一个身份, 每个写手每场开头都听到一段「写作台
# 提醒」, 点名的是别人的项目, 还推断「写的时候没调起写作台」—— 可对不回来的大多是 W-38 之前写的(那时查重不记稿子,
# 见 ANGLE_DEBT_SINCE), 不是谁没在写作台里写; 正在写作台里写的人还被叫「这次就在写作台里写」。看漏接走 TV 角度台账
# 那盏灯(判据 C, 同口径)。这里的计算留着(desk_gaps), 只是不再进回包; 现在没有别的调用方。
DESK_GAP_DAYS = 7
DESK_GAP_MIN_UNCAUGHT = 10
DESK_GAP_MAX_CAUGHT = 2
DESK_GAP_CACHE_SEC = 600          # 一场对话开头 list_projects + open_project 会连着问两次, 数一天才变一次
# 窗口按 truth_vault.notes.publish_time 切(只取 id), 不按对照表的 matched_at: 新接一张表、或者 tv-sync 停了几天
# 再补, 一批旧笔记会同时拿到很新的 matched_at, 被说成"最近 7 天发的"(codex review on aw#109)。
_desk_gap_cache: dict[str, tuple[float, list]] = {}
DESK_GAP_TASK = "把 message 原样告诉用户, 同一场对话里说过就不再说; 然后照常继续, 不用等用户回答。"


def _desk_gap_items(client, user_id: str) -> list[dict]:
    # 按 user_id 缓存: store.client() 每次都新建一个 client, 拿它当键永远命中不了(codex review on aw#109)
    now = time.monotonic()
    hit = _desk_gap_cache.get(user_id)
    if hit and now - hit[0] < DESK_GAP_CACHE_SEC:
        return hit[1]
    mine = {p["id"]: p.get("name") or p["id"] for p in store.list_all_projects(client, owner_id=user_id)}
    groups: dict[str, list[str]] = {}
    target: dict[str, str] = {}
    for m in store.tv_project_map(client):
        if m["project_id"] in mine:
            groups.setdefault(m["tv_project_id"], []).append(m["project_id"])
            if m.get("ingest_target"):
                target[m["tv_project_id"]] = m["project_id"]
    items: list[dict] = []
    if groups:
        wall = _utcnow()
        # 上界 = 现在: 排期还没发的(publish_time 在未来)不算
        recent = store.tv_published_note_ids(client, list(groups),
                                             (wall - timedelta(days=DESK_GAP_DAYS)).isoformat(), wall.isoformat())
        counts: dict[str, list[int]] = {}
        for r in store.tv_link_kinds_for_notes(client, recent):
            # 只数对到【调用者自己】项目上的: 一个 TV 项目可能映射到好几个人的写作台项目(codex review on aw#109)
            if r.get("project_id") not in mine or r.get("tv_project_id") not in groups:
                continue
            c = counts.setdefault(r["tv_project_id"], [0, 0])
            if r.get("match_kind") == "ingested":
                c[0] += 1
            elif r.get("match_kind") in _TV_BACKFILL_KINDS and (r.get("lag_days") is None or r["lag_days"] >= 0):
                c[1] += 1               # 写作台那版晚于发布 = 发出去之后抄回去的, 不算接住
        for tv, pids in sorted(groups.items()):
            ing, caught = counts.get(tv, (0, 0))
            if ing < DESK_GAP_MIN_UNCAUGHT or caught > DESK_GAP_MAX_CAUGHT:
                continue
            if not store.any_draw(client, pids):
                continue        # 从没在写作台里写过的项目: 本来就不在这写, 不唠叨
            name = mine.get(target.get(tv, "")) or min(mine[p] for p in pids)
            items.append({"tv_project_id": tv, "project": name, "project_ids": sorted(pids),
                          "published": ing + caught, "caught": caught})
    for k in [k for k, (t, _) in _desk_gap_cache.items() if now - t >= DESK_GAP_CACHE_SEC]:
        _desk_gap_cache.pop(k, None)                 # 过期的顺手清掉, 进程里不越攒越多
    _desk_gap_cache[user_id] = (now, items)
    return items


def desk_gaps(client, *, user_id: str | None, current_project_id: str | None = None) -> dict | None:
    """调用者名下, 最近发出去的帖写作台没接住的项目 + 一段原话。没有就 None。只读, 只看调用者自己的项目。
    2026-10-30 起不进任何工具的回包(见上面 DESK_GAP_* 那段), 只留给排查时手动调。"""
    if not user_id:
        return None
    items = _desk_gap_items(client, user_id)
    if not items:
        return None
    lines = []
    for it in items:
        name, n, caught = it["project"], it["published"], it["caught"]
        # W-38(10-29, owner): 写手没有「存档」这一步, 写作台自己按内容从飞书对回来 —— 这里不叫写手打分 / 存档。
        # 写手能做的只有一件: 在连着写作台的对话里写(查重那一步写作台会记下稿子, 发出去以后对得回来)。
        if current_project_id and current_project_id in it["project_ids"]:
            lines.append(f"「{name}」最近 {DESK_GAP_DAYS} 天发出去 {n} 篇, 写作台只对上 {caught} 篇。这次就在写作台里写, "
                         f"写完照常贴进飞书, 发出去以后写作台会自己对回来。")
        else:
            lines.append(f"「{name}」最近 {DESK_GAP_DAYS} 天发出去 {n} 篇, 写作台只对上 {caught} 篇: 对不上的那些没有查重, "
                         f"也进不了改稿复盘。下次写{name}请在连着写作台的对话里写, 开头先说「打开{name}」; "
                         f"写完照常贴进飞书就行, 写作台会自己对回来。")
    return {"projects": [{k: v for k, v in it.items() if k != "project_ids"} for it in items],
            "message": "写作台提醒:\n" + "\n".join(lines), "task": DESK_GAP_TASK}


def archive_pending(client, project_id: str, *, user_id: str | None) -> dict | None:
    """调用者自己在这个项目近 ARCHIVE_PENDING_DAYS 天抽了、还没存档销账的牌(W-29)。有就回
    ``{"cards": 张数, "oldest_drawn_at": 最早那张的发牌时间, "next_step": ARCHIVE_PENDING_NEXT_STEP}``, 没有回 None。

    张数含这一次刚发的牌: 挂在 draw_angles 返回上时, 它就是在说"这一批写完要存档"; 比这一批更早的还挂着,
    oldest_drawn_at 就早于这次发牌。同一次发牌的牌 drawn_at 都一样。

    复用简报 angle_debt 那一条查询(store.angle_debt, 只取最早的一行 + 精确计数): 每次调用只多一次查询, 走
    (project_id, drawn_at) 那个索引。只数 drawn_by = 调用者的行, 别人的牌、别的项目的牌都不算。
    不过归属闸: 只被已过闸的 draw_angles / check_drafts / record_rule 在它们成功之后调(tools._archive_pending)。
    查询失败时 store.angle_debt 自己留痕、回 0, 这里就回 None; 工具层再兜一层, 怎么都不会把工具结果弄坏。

    W-31(10-27): 调用者这一天里存档时带的 angle_key 对不上台账(没有这个 key / 已经销给别的稿子)的, 多带一个
    ``angle_key_mismatch``: ``{"drafts", "unknown", "used", "by_problem", "keys"(最多 ANGLE_KEY_MISS_SHOW 个, 截断),
    "last_at", "note"}``。``unknown`` 同回包的 unknown_angle_keys(这个项目台账里没有的三种), ``by_problem`` 分开数,
    ``note`` 按出现的那几种拼(_angle_key_mismatch_note)。从进程内存读(commit_drafts 记的), 不多查库; 没有就不带。
    """
    if not user_id:
        return None
    # W-38: 这里【不】排除查重时带过的牌 —— 它挂在 check_drafts 的回包上, 刚查过的那几张正是该存档的(存档才有参考分、
    # 规则检查和交付模板); 没存上也不丢(发出去以后 tv-sync 从底稿对回来), 所以 next_step 只叫模型存, 不叫它跟用户说。
    # W-38 上线之前抽的不算(ANGLE_DEBT_SINCE, 同简报): 那些多半写完发出去了, 只是当时查重没记稿子、对不回来。
    debt = store.angle_debt(client, project_id, user_id, ARCHIVE_PENDING_DAYS, sample=1, oldest_first=True,
                            since=ANGLE_DEBT_SINCE)
    cards = int(debt.get("unconsumed") or 0)
    if cards <= 0:
        return None
    block = {"cards": cards, "oldest_drawn_at": debt.get("oldest_drawn_at"),
             "next_step": ARCHIVE_PENDING_NEXT_STEP}
    # W-31: 这一天里存档带的 key 对不上台账的, 一并说(进程内存, 不多查库)。只在还挂着牌时带 —— 那些牌就是没销掉的
    miss = _recent_angle_key_misses(project_id, user_id)
    if miss:
        block["angle_key_mismatch"] = miss
    return block


def _note_angle_key_misses(project_id, user_id, problems: list[dict]) -> None:
    """存档时 key 对不上台账的那几篇记一笔(W-31), 给之后的 archive_pending 用。每篇带自己的时刻, 记之前先剪掉过了
    窗口的(_unexpired_angle_key_misses), 不是最近一笔还在就整段留着。不抛。"""
    if not problems or not user_id:
        return
    try:
        now = _utcnow()
        k = (str(project_id), str(user_id))
        with _angle_key_misses_lock:
            _angle_key_misses[k] = _unexpired_angle_key_misses(k, now) + [
                {"at": now, "problem": p["problem"], "angle_key": p["angle_key"]} for p in problems]
    except Exception:                                   # noqa: BLE001 — 提醒, 绝不拖垮入库
        logger.warning("commit_drafts: 记 angle_key 对不上的那一笔失败 (project=%s)", project_id, exc_info=True)


def _unexpired_angle_key_misses(k: tuple[str, str], now: datetime) -> list[dict]:
    """(项目, 写手) 记下的对不上的那几篇里, 还在 _ANGLE_KEY_MISS_TTL 窗口里的(每篇按自己的时刻算)。调用方持锁。"""
    return [m for m in _angle_key_misses.get(k) or [] if now - m["at"] <= _ANGLE_KEY_MISS_TTL]


def _recent_angle_key_misses(project_id, user_id) -> dict | None:
    """调用者在这个项目近 ARCHIVE_PENDING_DAYS 天存档时 key 对不上台账的篇数和其中几个 key(W-31); 没有回 None。
    只数还在窗口里的那几篇, keys 也只从它们里挑; 全过期了就把这一笔清掉。"""
    now = _utcnow()
    k = (str(project_id), str(user_id))
    with _angle_key_misses_lock:
        kept = _unexpired_angle_key_misses(k, now)
        if kept:
            _angle_key_misses[k] = kept
        else:
            _angle_key_misses.pop(k, None)              # 过期的顺手清掉, 进程里不越攒越多
    if not kept:
        return None
    n: dict[str, int] = {}
    keys: list[str] = []
    for m in kept:
        n[m["problem"]] = n.get(m["problem"], 0) + 1
        if m["angle_key"] not in keys and len(keys) < ANGLE_KEY_MISS_SHOW:
            keys.append(m["angle_key"])
    unknown = sum(n.get(c, 0) for c in ANGLE_KEY_ABSENT)
    used = n.get(store.ANGLE_KEY_USED, 0)
    return {"drafts": unknown + used, "unknown": unknown, "used": used, "by_problem": n,
            "keys": keys, "last_at": max(m["at"] for m in kept).isoformat(),
            "note": _angle_key_mismatch_note(n)}


def _angle_key_mismatch_note(n: dict[str, int]) -> str:
    """archive_pending 里 angle_key_mismatch 那句话, 按出现的问题拼(W-31 复审)。

    「牌其实写完存了、别按它们再写」只对 unknown / used 说: 那两种是这个项目的牌写了却带错了 key。别的项目的牌、
    台账里没有这一行的, 这个项目挂着的牌跟那几篇无关。「不要自己编号、缩写或改写」只对形状不对的(unknown)说。"""
    unknown, used = n.get(store.ANGLE_KEY_UNKNOWN, 0), n.get(store.ANGLE_KEY_USED, 0)
    other, missing = n.get(store.ANGLE_KEY_OTHER_PROJECT, 0), n.get(store.ANGLE_KEY_NOT_IN_LEDGER, 0)
    parts = []
    if unknown or used:
        parts.append(
            f"这些牌里有的其实已经写完存档了: 有 {unknown + used} 篇存档时带的 angle_key 在这个项目的台账里对不上"
            "(keys 里有), 所以没销账。已经存了的不用重存, 也别按这些牌再写一遍"
            + (f"; 之后每篇存档时的 angle_key 照 draw_angles 返回里分给它的那个原样复制({fp.ANGLE_KEY_HEX_LEN} 位十六进制), "
               "不要自己编号、缩写或改写" if unknown else "") + "。")
    if other:
        parts.append(f"有 {other} 篇存进这个项目时带的是你在别的项目抽的牌: 稿子是不是存错了项目? 跟用户确认。")
    if missing:
        parts.append(f"有 {missing} 篇带的 angle_key 形状是对的({fp.ANGLE_KEY_HEX_LEN} 位十六进制), 但台账里没有这一行: "
                     "多半是发牌时台账没写进去, 不是你编的或抄错的。")
    return "".join(parts)


def edit_digest(client, project_id: str, *, user_id: str | None = None,
                now: datetime | None = None) -> dict | None:
    """这个项目该不该给一份"人改了什么"的摘要: 上次看过不到一周、或者新改过的不到 3 篇 → None。"""
    assert_project_access(client, project_id, user_id=user_id)
    now = now or _utcnow()
    ack = store.tv_edit_digest_ack(client, project_id)
    if ack:
        acked_at = tvlink._as_dt(ack.get("acked_at"))
        if acked_at and now - acked_at < timedelta(days=EDIT_DIGEST_EVERY_DAYS):
            return None
    got = store.tv_edit_diffs_since(client, project_id, (ack or {}).get("acked_through"), min_rev=tvlink.DIFF_REV)
    changed = [r for r in got["rows"] if r.get("changed")]
    if len(changed) < EDIT_DIGEST_MIN_CHANGED:
        return None
    # through = 这次真看到的最后一行(库里按时间从旧往新排好的); 后面还有的(more)下次接着看, 不会被跨过去
    through = str(changed[-1].get("computed_at") or "")
    examples = []
    for r in reversed(changed[-EDIT_DIGEST_EXAMPLES:]):         # 例子给最近的几篇
        ex: dict = {"title": r.get("published_title") or r.get("draft_title")}
        if r.get("title_changed"):
            ex["title_before"] = r.get("draft_title")
        if r.get("removed_sentences"):
            ex["removed"] = list(r["removed_sentences"])[:3]
        if r.get("added_sentences"):
            ex["added"] = list(r["added_sentences"])[:3]
        if r.get("edited_sentences"):
            ex["rewritten"] = [{"before": a, "after": b} for a, b in list(r["edited_sentences"])[:3]]
        examples.append(ex)
    out = {"through": through, "since": (ack or {}).get("acked_through"),
           "pairs": got["pairs"], "changed": len(changed),
           "title_changed": sum(1 for r in changed if r.get("title_changed")),
           "rewritten_sentences": sum(int(r.get("edited_count") or 0) for r in changed),
           "chars": {"draft_avg": _avg(r.get("draft_chars") for r in changed),
                     "published_avg": _avg(r.get("published_chars") for r in changed)},
           "examples": examples, "task": EDIT_DIGEST_TASK,
           # W-33(10-30): 10-21～10-26 摘要给了 8 次只 ack 过 1 次, 模型拿到就直接去发牌了 —— 加一句原样转给用户的话
           # (同 desk_gap / skill_update), 至少让写手看得见; ack 仍在用户看过、做了决定之后(先 ack 会让漏看的差异再也进不了摘要)
           "message": EDIT_DIGEST_MESSAGE.format(changed=len(changed))}
    if got["more"]:
        out["more_after_through"] = True        # 一次没看完: 看完这份、ack 之后, 下一份从 through 接着往后
    bd, bp = _avg(r.get("brand_draft") for r in changed), _avg(r.get("brand_published") for r in changed)
    if bd is not None or bp is not None:
        out["brand_mentions"] = {"draft_avg": bd, "published_avg": bp}
    return out


def ack_edit_digest(client, project_id: str, through: str, *, user_id: str | None = None,
                    rules_recorded: int = 0) -> dict:
    """记下这份摘要看过了(through 之前的差异不再进摘要), 一周内不再给。"""
    assert_project_access(client, project_id, user_id=user_id)
    t = tvlink._as_dt(through)
    if t is None:
        raise ValueError("through 要原样传回 edit_digest 里的那个时间")
    if t > _utcnow() + timedelta(hours=1):
        # 水位只往前走, 传错一个将来的时间就永远收不到摘要了 —— 摘要给的 through 不会比现在晚
        raise ValueError("through 比现在还晚, 不是 edit_digest 给的那个时间; 原样传回")
    if not isinstance(rules_recorded, int) or isinstance(rules_recorded, bool) or not 0 <= rules_recorded <= 20:
        raise ValueError("rules_recorded 是这次记了几条规则(0~20 的整数)")
    kept = store.tv_edit_digest_ack_upsert(client, project_id, acked_through=t.isoformat(),
                                           acked_by=str(user_id) if user_id else None, rules_recorded=rules_recorded)
    return {"acked": True, "through": kept, "rules_recorded": rules_recorded,
            "next_after_days": EDIT_DIGEST_EVERY_DAYS}


def record_rule(client, project_id: str, content: str, *, severity: str = "soft",
                scope: str = "project", user_id: str | None = None,
                applicability: str = "", check: dict | None = None) -> dict:
    """沉淀一条规则(团队共享)。severity='hard' 的下次进 P0。

    复用 db.upsert_memory —— 它带并发安全(keyed lock + CAS 重试)和
    rule_kind/rule_payload 的向后兼容 strip, 别绕过它自己 insert。
    force_confirmed=True: 这条路径是用户明确说「以后都这样」才走的, 不需要
    频次阈值(那是给自动抽取的候选留人工复核用的)。

    ``check``(2026-10-20): 词表 / 某个词能不能出现在哪 / 字数范围这类规则, 再带一份代码能执行的形状, 存进
    ``rule_kind`` / ``rule_payload``, score_drafts 存档时逐篇查(deskcore/rulecheck.py)。**先校验再写**: check 写错
    整条不落库、报 CheckError(工具层映成 400), 免得规则记上了而检查是坏的、谁也不知道。
    """
    # 审计 COR-015。scope='global' 时 project_id 其实不参与写入, 但照样校验 ——
    # 规则是"每个项目级入口都以这一行开头", 留例外就等于留一个以后会被忘掉的
    # 缺口, 而这里的代价只是一次主键查询。
    assert_project_access(client, project_id, user_id=user_id)
    content = (content or "").strip()
    if not content:
        raise ValueError("rule content must not be empty")
    severity = (severity or "soft").lower()
    if severity not in ("hard", "soft"):
        raise ValueError("severity must be 'hard' or 'soft'")
    if scope not in ("project", "global"):
        raise ValueError("scope must be 'project' or 'global'")
    rule_check = rulecheck.normalize_check(check) if check is not None else None

    from .isolation import foreign_markers
    projects = store.isolation_projects(client, owner_id=user_id)
    target = next((p for p in projects if str(p["id"]) == str(project_id)), None)
    if target is None:
        raise RuntimeError("项目标记未查到，未写规则")
    # 必须带的话术也过同一道(10-20): 它会变成"必须改: 正文里没有「X」"回给写手, X 是别的项目的品牌名时等于叫模型往这个
    # 项目里写别家的名字。禁用词 / 正则不过: 它们只会让稿子少写什么, 禁掉别家的名字恰恰是常见用法。
    guarded = content
    if rule_check is not None and rule_check[0] == "required_phrase":
        guarded = content + "\n" + "\n".join(rule_check[1]["any"])
    conflicts = foreign_markers(guarded, target, projects, global_scope=scope == "global")
    if conflicts:
        terms = sorted({t for c in conflicts for t in c["terms"]})
        raise ProjectIsolationRejected("规则含项目专属词「" + "、".join(terms) + "」，可能串项目，未写入。请核对项目；项目专属规则不要记为全局规则。")

    row = db.upsert_memory(
        client, user_id=user_id, scope=scope, content=content,
        source_feedback="deskcore",
        project_id=project_id if scope == "project" else None,
        force_confirmed=True, severity=severity,
        # 用户是【明确指定】了 hard/soft 才走到这里的, 所以命中已存在的规则时
        # 要把 severity 也写回去。db.upsert_memory 默认不写(自动抽取传的
        # severity 是猜的, 会把手设的 hard 降回 soft), 这条路径必须显式打开 ——
        # 否则"把这条改成硬约束"会返回成功但库里还是 soft, 继续待在 P1。
        # (codex review round-5 P1)
        update_severity=True,
    )
    # 以【库里实际的值】为准回报, 别回报入参 —— 回报入参正是上面那个 bug 之所以
    # 看不见的原因: 库里没改, 返回值却说改了。
    stored = (row or {}).get("severity") or severity
    out = {"memory_id": (row or {}).get("id"), "severity": stored,
           "scope": scope, "content": content}
    if stored != severity:
        out["warning"] = (f"请求 severity={severity}, 但库里这条现在是 {stored} —— "
                          "以库里的为准, 请把这个差异告诉用户")

    # applicability 得单独写一次: db.upsert_memory 不认这个字段(它是 autowriter
    # 那边的老签名), 而它正是方向档的载体。
    # ⚠️ 写失败必须**上抛**, 不能吞。吞掉的话这条规则会以"无方向"落库 → 之后
    # 每一个方向的项目都注入它, 而调用方收到的是一个成功返回。
    applicability = (applicability or "").strip()
    if applicability and out["memory_id"]:
        bad = [t for t in DIRECTION_TAGS if t in applicability]
        if applicability not in DIRECTION_TAGS and bad:
            raise ValueError(
                f"applicability={applicability!r} 里带了方向词但不等于 "
                f"{list(DIRECTION_TAGS)} 之一。方向档是精确匹配的, "
                "半个词会让这条规则在两个方向上都被挡掉。")
        saved = store.update_memory_fields(
            client, out["memory_id"], {"applicability": applicability})
        out["applicability"] = (saved or {}).get("applicability")

    # check 同理单独写(db.upsert_memory 只在新建时写这两列), 写失败同样上抛 —— 吞掉的话用户以为这条会被代码查,
    # 其实没有。回报库里的值, 不回报入参(同 severity 那条的教训)。
    if rule_check is not None and out["memory_id"]:
        kind, payload = rule_check
        saved = store.set_memory_check(client, out["memory_id"], kind, payload)
        stored_kind = (saved or {}).get("rule_kind")
        out["check"] = ({"kind": stored_kind, **((saved or {}).get("rule_payload") or {})}
                        if stored_kind in rulecheck.CHECK_KINDS else None)
        if stored_kind != kind:
            out["check_warning"] = (f"请求 check.kind={kind}, 但库里这条现在是 {stored_kind!r} —— "
                                    "以库里的为准, 请把这个差异告诉用户")

    # 新记的(或改了原文 / 方向的)规则在后台编译(D-121), 不等冷却; 这个调用不等它。
    _kick_rule_compile(client, project_id, reason="record_rule", bypass_cooldown=True)
    return out


# ══════════════════════════════════════════════════════════════════════
# 规则台账: 让写手自己看见、自己升降、自己停用
# ══════════════════════════════════════════════════════════════════════
#
# 这三个操作以前只有直连数据库才能做, 也就是只有运维能做。那意味着技艺库
# 的进化速度被"写手找运维"这个环节卡死, 而真正知道一条规则好不好用的只有
# 天天写的那个人。把控制权交到工具层, 这套东西才谈得上自己长。

_RULE_ACTIONS = ("mute", "unmute", "retire", "promote", "set_direction")


def my_rules(client, project_id: str, *, user_id: str) -> dict:
    """我这个项目上**全部**规则的台账 —— 含试用档和被我关掉的。

    与简报的口径差别是故意的: 简报只给"现在生效的", 台账要给"库里有的",
    否则写手看不见自己还有什么待裁决、什么被自己静音了。
    """
    project = assert_project_access(client, project_id, user_id=user_id)
    rows = store.rules_ledger(client, user_id=user_id, project_id=project_id)
    direction = detect_direction(project.get("name") or "")

    out = []
    for r in rows:
        muted = db.is_memory_muted_now(r.get("muted_until"))
        status = (r.get("status") or "candidate").lower()
        tag = (r.get("applicability") or "").strip()
        blocked = bool(direction) and bool([t for t in DIRECTION_TAGS if t in tag]) \
            and direction not in tag
        if status != "confirmed":
            state = "试用"          # 不进简报, 等升档
        elif muted:
            state = "已停用"
        elif blocked:
            state = "方向不符"      # 在别的方向上生效, 这个项目上不注入
        else:
            state = "生效中"
        out.append({
            "memory_id": r.get("id"),
            "content": r.get("content") or "",
            "state": state,
            "severity": (r.get("severity") or "soft").lower(),
            "scope": r.get("scope"),
            "direction": tag or "通用",
            "votes": r.get("frequency") or 1,
            "muted_until": r.get("muted_until"),
            "created_at": r.get("created_at"),
        })
        # 存档时会被代码逐篇查的那几条带上它的 check(10-20): 写手问"这条怎么没拦住"时看得见查的是什么
        spec = rulecheck.spec_of(r)
        if spec is not None:
            out[-1]["check"] = spec
    order = {"生效中": 0, "方向不符": 1, "试用": 2, "已停用": 3}
    out.sort(key=lambda x: (order.get(x["state"], 9), -int(x["votes"] or 1)))
    counts: dict = {}
    for r in out:
        counts[r["state"]] = counts.get(r["state"], 0) + 1

    # 缺向量的条数要露出来。没向量的规则不会报错、照样注入, 只是**不参与
    # 相关性筛选** —— 于是"这个项目跟这条规则根本不相干, 它却还是进了简报"
    # 这件事从外面完全看不出来。这是这套东西唯一一个还需要跑一次运维命令
    # (cli reembed-rules)才能补上的洞, 不显示出来就没人会想起去跑。
    # None = 查不出来(不是 0)。查不出来就什么都不显示, 别把"不知道"渲染成
    # 一个看起来正常的数字。
    missing = store.count_rules_missing_embedding(
        client, user_id=user_id, project_id=project_id)
    if missing:
        counts["缺向量"] = missing

    res = {
        "project_id": project_id,
        "project_name": project.get("name") or "",
        "project_direction": direction or "未判定",
        "rules": out,
        "counts": counts,
        "actions": ("改这些用 set_rule_state: mute(停用一段时间) / unmute(恢复) / "
                    "retire(降回试用档, 不再进简报) / promote(试用→生效) / "
                    "set_direction(设成 产品向 / 流量向 / 通用)"),
    }
    if missing:
        res["note_missing_embedding"] = (
            f"有 {missing} 条规则没有向量。它们照常注入, 但**不参与相关性筛选** —— "
            "也就是跟本次要写的东西不相干时也会进简报。"
            # ⚠️ 这句话是直接送到模型眼前的, 它会盖过 SKILL.md 里的指引。
            # 原来写的是"运维跑一次 cli reembed-rules" —— 那正好把用户推回
            # 运维环节, 而这一轮做 reembed_my_rules 就是为了消灭那个环节。
            # (codex review · PR #74)
            "**调 reembed_my_rules 就能补**(一次 50 条, 按返回的 remaining 重复调"
            "直到归零), 不用找别人。"
            "走 record_rule 新记的规则会在写入时自动算(db.upsert_memory), "
            "所以这个数只会往下走, 不会自己涨。")
    return res


def reembed_my_rules(client, *, user_id: str, batch: int = 50) -> dict:
    """给**我自己**缺向量的规则补算, 一次一批。

    为什么做成工具而不是只留 CLI: 补向量需要 Supabase 的 service_role key 和
    GOOGLE_API_KEY, 两样都只在服务端有 —— 做成 CLI 就意味着每次都要有人进
    Railway 的 shell, 也就是"又得找运维"。这条路径只碰调用者自己名下的行
    (``db.backfill_memory_embeddings`` 里那句 ``.eq("user_id", user_id)``),
    所以它跟其它工具是同一套归属口径, 不是新开的管理面。

    **一次只补一批**: 补算要逐条调 embedding API, 一次几百条会把工具调用拖到
    超时。返回 ``remaining`` 让调用方自己决定要不要再来一次 —— 与 Streamlit
    那个「立即补算(最多 50 条)」按钮同一个节奏。
    """
    if not user_id:
        raise PermissionError("无法识别调用者身份, 拒绝补算")
    batch = max(1, min(int(batch or 50), 50))

    out = db.backfill_memory_embeddings(client, user_id, max_rows=batch)
    status = (out or {}).get("status")
    updated = int((out or {}).get("updated") or 0)

    # ⚠️ 失败状态必须**抛**, 不能返回一个带 error 字段的字典。
    # MCP 会把"返回了字典"当成调用成功, 于是调用方模型看到的是一次成功调用 +
    # 一个 remaining 数字, 完全可能当作"补完了"往下走。SKILL.md 里也写着这个
    # 工具"出错一律直接报错" —— 返回字典会让代码和文档对不上。
    # (codex review · PR #74 · P1)
    # 报错文本是念给写手的(2026-10-30 发布收尾): 环境变量、列名、迁移只进日志, 写手那边只说"这次补不了、不是没东西补"。
    if status == "no_embedding_sdk":
        logger.warning("reembed_my_rules: 服务端没配 GOOGLE_API_KEY(或 embedding SDK 缺失), 补算功能不可用 (user=%s)",
                       user_id)
        raise RuntimeError(
            "这次补不了: 服务端暂时算不了向量(不是「没有需要补的」)。已记下, 规则照常生效, "
            "不用重试, 不用你做什么。")
    if status == "schema_missing":
        logger.warning("reembed_my_rules: memories.embedding 列不存在, 需要先跑 pgvector 迁移: %s",
                       (out or {}).get("hint") or "")
        raise RuntimeError(
            "这次补不了: 服务端还不支持规则向量(不是「没有需要补的」)。已记下, 规则照常生效, "
            "不用重试, 不用你做什么。")
    if status == "query_failed":
        raise RuntimeError(f"补算查询失败: {(out or {}).get('error')}")

    # ⚠️ 剩余条数**必须重新查库**, 不能拿"上次查到多少减去补了多少"去算 ——
    # backfill 对算不出向量的行是跳过的, 那种行会永远留在待补集合里。用减法
    # 会得出一个一直在降、最后归零的假数字, 而库里其实一条没少。
    #
    # ⚠️ 而且必须用**补算口径**(这个人名下所有 scope), 不是台账口径
    # (global + 单个项目)。backfill 只按 user_id 过滤, 台账口径会漏掉他在别的
    # 项目里的项目规则 → 第一批之后就报 remaining: 0 让人收工。
    # (codex review · PR #74 · P1)
    remaining = store.count_own_rules_missing_embedding(client, user_id=user_id)

    res = {"updated": updated, "remaining": remaining, "status": status}
    if status == "noop":
        res["note"] = "没有缺向量的规则了 —— 已经补齐。"
    elif updated == 0 and remaining:
        # 查到了却一条没补上 = 再调一次还是同一批, 明说别空转。
        # 每行失败的原因在日志里(telemetry: backfill_memory_row_failed), 不念给写手
        logger.warning("reembed_my_rules: 查到了缺向量的行却一条都没补上(还剩 %d 条, user=%s) —— 每行原因见 "
                       "telemetry backfill_memory_row_failed", remaining, user_id)
        res["warning"] = (f"这一批查到了缺向量的规则却一条都没补上(还剩 {remaining} 条)。"
                          "**不要接着调** —— 再调还是同一批。已记下, 规则照常生效, 不用你做什么。")
    elif remaining:
        res["next"] = f"还剩 {remaining} 条, 再调一次这个工具接着补。"
    return res


def set_rule_state(client, memory_id: str, action: str, *,
                   user_id: str, days: int = 90, direction: str = "") -> dict:
    """写手自己改一条规则的档位。**不改内容** —— 改内容是 record_rule 的事。

    归属校验分两路, 因为这两种规则的归属定义本来就不同:
      · scope='global' —— 私人技艺库, 判 ``user_id`` 相等;
      · scope='project' —— 团队共享, 判项目归属(assert_project_access)。
    少判任何一路都等于让持 key 的人改别人的规则。
    """
    action = (action or "").strip().lower()
    if action not in _RULE_ACTIONS:
        raise ValueError(f"action 必须是 {list(_RULE_ACTIONS)} 之一, 收到 {action!r}")

    row = store.memory_row(client, memory_id)
    if row is None:
        raise ProjectNotFound(f"规则不存在: {memory_id}")
    if not db._is_rule_memory(row):
        raise ValueError(f"{memory_id} 不是一条写作规则(memory_type="
                         f"{row.get('memory_type')!r}), 拒绝改动")

    if (row.get("scope") or "") == "global":
        if str(row.get("user_id") or "") != str(user_id):
            raise PermissionError(
                "这条是别人的个人技艺库里的规则, 不能改。global 规则是私有的。")
    else:
        assert_project_access(client, row.get("project_id"), user_id=user_id)

    if action == "mute":
        days = int(days or 90)
        if days < 1:
            raise ValueError("mute 的天数至少是 1")
        until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        fields = {"muted_until": until}
    elif action == "unmute":
        fields = {"muted_until": None}
    elif action == "retire":
        # 降回试用档 = 不再进简报, 但**不删**。删了就没法回头看"这条当初为什么
        # 被记下来", 而技艺库的价值恰恰在那段演化史里。
        fields = {"status": "candidate"}
    elif action == "promote":
        fields = {"status": "confirmed"}
    else:                                   # set_direction
        direction = (direction or "").strip()
        if direction in ("", "通用"):
            fields = {"applicability": None}
        elif direction in DIRECTION_TAGS:
            fields = {"applicability": direction}
        else:
            raise ValueError(f"direction 只能是 {list(DIRECTION_TAGS)} 或 '通用', "
                             f"收到 {direction!r}")

    saved = store.update_memory_fields(client, memory_id, fields)
    if (saved
            and (saved.get("status") or "").lower() == "confirmed"
            and (action in ("promote", "unmute") or (action == "set_direction"
                 and (saved.get("applicability") or "").strip() != (row.get("applicability") or "").strip()))):
        compile_project = row.get("project_id")
        if row.get("scope") == "global":
            # 全局缓存跨项目共用; 选择 owner 自己已接 TV 的项目, 避免第一批先漏查。
            compile_project = None
            try:
                for project in store.list_all_projects(client, owner_id=row["user_id"]):
                    if any(m.get("tv_project_id") for m in store.tv_projects_of(client, project["id"])):
                        compile_project = project["id"]
                        break
            except Exception:                      # 后台增强失败不能把已保存的修改报成失败
                logger.warning("global 规则激活后查编译项目失败(memory=%s)", memory_id, exc_info=True)
        if compile_project:
            _kick_rule_compile(client, compile_project, reason="set_rule_state", bypass_cooldown=True)
    return {
        "memory_id": memory_id,
        "content": (row.get("content") or "")[:120],
        "action": action,
        # 一律以库里的值回报。回报入参 = record_rule 那条 severity bug 的复刻。
        "now": {
            "status": (saved or {}).get("status"),
            "muted_until": (saved or {}).get("muted_until"),
            "applicability": (saved or {}).get("applicability") or "通用",
        },
    }


# ══════════════════════════════════════════════════════════════════════
# 规则编译 + 写后查规则(2026-10-20, D-121)
# ══════════════════════════════════════════════════════════════════════
#
# 10-20 生产诊断: 规则全部写成文字塞进简报, 守不守全凭模型注意力。改成「写完再查」: 规则由 Jev 编译一次(POST
# /compile_rules, 模型调用在 Jev 那边, 这里仍然零 LLM), 结果按 (规则 id, sha) 缓存进 rule_compiles(migrations/015);
# 写手写完调 check_rules, 用缓存逐篇查, 只作建议: hard → 必须改, soft → 建议改, 不拦存档(owner 10-20 拍板, D-121)。
# 简报不动 —— 软规则移出简报要等校准之后。哪些是纯函数见 deskcore/rulecompile.py。
#
# 编译最坏要 6 分钟(Jev README「规则编译」), 所以**写手的请求永远不等它**: open_project / record_rule / check_rules
# 发现可能有没编的规则, 就在后台起一个线程去编(每个项目同时最多一个、有冷却), CLI 的 compile-rules 是运维入口。

RULE_COMPILE_COOLDOWN_SEC = 300          # 同一个项目两次后台编译至少隔这么久(record_rule 不受它限: 刚记的规则要尽快编)
RULE_COMPILE_HOLD_SEC = 3600             # 整批被挡下(出境口径 / 没接 tv-map / judge 不可用)之后, 后台这么久不再试
RULE_COMPILE_TIMEOUT_HOLD_SEC = 1800     # 一批读超时之后, 后台这么久不再试(Jev 慢成这样多半还在编、还在计费, 见 compile_rules)
RULE_COMPILE_RETRY_AFTER_SEC = 6 * 3600  # 同一条规则的同一个版本编失败之后, 后台这么久之内不再编(每编一次都花钱)
# Jev 生成端的 HTTP 429 / 5xx(Jev 自己重试过一次还是这样, 回「生成端调用失败：GeneratorHTTPError: HTTP 529 …」): 上游没出
# 输出、没计费, 多半是部署后一阵过载。原来和花了钱的失败一样扣 6 小时, 一次几秒的过载让整个池子半天没检查(独立复审 on aw E)。
# 这种只扣这么久。504 / 524 是网关超时, 上游可能已经生成、计费了, 照旧 6 小时; 拒答、截断、输出不合格、读超时同样照旧。
RULE_COMPILE_TRANSIENT_RETRY_SEC = 1800
_FREE_GENERATOR_FAILURE = re.compile(r"生成端调用失败：GeneratorHTTPError: HTTP (?:429|5(?!04|24)\d\d)\b")
RULE_COMPILE_MAX_ROUNDS = 3              # 后台线程一次最多连编几轮(编的时候又记了规则, 会再来一轮)
# Jev 回了 200, 但 model 是 mock(JUDGE_MOCK=1 的测试 / 本地起的 Jev, 按关键词给假结果): 这批一条不存(2026-10-20 第三轮
# 复审)。缓存键是 (规则 id, sha), 不带 model —— 存进去的话, 以后 JUDGE_URL 指回真的 Jev, 这些假题、假代码照样一直命中缓存,
# 只有 --force 换得掉。Jev 自己的账本也拒 mock 的答案(write=true 在 mock 下 422), 这边对编译结果同一个口径。
RULE_COMPILE_MOCK = "jev_mock"
_RULE_COMPILE_HOLD_STATUSES = frozenset({
    judge_client.JUDGE_POLICY_BLOCKED, judge_client.JUDGE_NOT_CONFIGURED,
    judge_client.JUDGE_UNAVAILABLE, judge_client.JUDGE_BAD_REQUEST, RULE_COMPILE_MOCK,
})
# 遇到就不再发后面几批的: 整批被挡的那几种(后面一样会被挡) + 读超时(2026-10-20 复审: Jev 的 /compile_rules 没有整体
# 截止, 调用方断开之后照样编完、照样计费; 读超时过了 Jev 最坏的 ~361 秒说明它慢下来了, 再发只是多付几批没人收的钱)
_RULE_COMPILE_STOP_STATUSES = _RULE_COMPILE_HOLD_STATUSES | {judge_client.JUDGE_TIMEOUT}
_RULE_COMPILE_LOCK = threading.Lock()
# 整个进程同时只编一个项目: 部署后第一轮各项目的 open_project 会同时触发, 一起压到 Jev 上; 而且同一个 owner 的 global
# 规则在他每个项目的池子里 —— 排着队编, 后一个项目读缓存时前一个已经存下了, **这个进程里**不会把同一条 global 规则编两遍。
# ⚠️ 只管本进程: CLI 的 compile-rules 是另一个进程, 和服务进程的后台补编不共用这把锁(runbook §1.11: 上线补编时先关
# RULE_COMPILE_AUTO)。CLI 的 --all 自己带 seen 去重(见 compile_rules)。
# 线程在这里排队(后台, 没人等它), 不占写手的请求。
_RULE_COMPILE_SLOTS = threading.BoundedSemaphore(1)
_RULE_COMPILE_STATE: dict[str, dict] = {}                    # project_id → {running, again, last, hold_until, last_status, hold_status}
_RULE_COMPILE_FAILED: dict[tuple[str, str], float] = {}      # (memory_id, sha) → 上次编失败的时刻(monotonic)
# 进程级的「Jev 慢下来了」(2026-10-20 第二轮复审): 任何一次编译读超时, RULE_COMPILE_TIMEOUT_HOLD_SEC 之内后台不给**任何**项目
# 发(record_rule 也不例外)。只记在超时的那个项目上的话, 排着队等槽位的别的项目一拿到槽位就发, 一个接一个读超时 —— 每一批都在
# Jev 那边编完、计费, 结果没人收。Jev 慢不是哪个项目、哪条规则的事。
_RULE_COMPILE_JEV: dict[str, float] = {"slow_until": 0.0}    # monotonic; 改它要握着 _RULE_COMPILE_LOCK
# 发出去还没回来的那一批: project_id → {"rules", "since"(monotonic)}; 改它要握着 _RULE_COMPILE_LOCK。进程退出时拿它报哪几批
# 被打断了(log_rule_compiles_cut_off; 2026-10-20 第三轮复审)
_RULE_COMPILE_INFLIGHT: dict[str, dict] = {}
RULE_COMPILE_JEV_SLOW = "jev_slow"                            # 停发期里 _kick_rule_compile 的返回 / 后台线程这一轮的结局
_RULE_COMPILE_ERROR_MAX = 200


def _jev_slow_now() -> bool:
    """这会儿是不是还在「Jev 慢下来了」的停发期里。调用方不能握着 _RULE_COMPILE_LOCK(它不可重入)。"""
    with _RULE_COMPILE_LOCK:
        return time.monotonic() < _RULE_COMPILE_JEV["slow_until"]


def _compile_error(text) -> str:
    return mask_secrets(str(text or ""))[:_RULE_COMPILE_ERROR_MAX]


def _mock_model(model) -> bool:
    """/compile_rules 回包的 model 是不是 mock(见 RULE_COMPILE_MOCK)。"""
    return isinstance(model, str) and model.strip().lower().startswith("mock")


def log_rule_compiles_cut_off() -> list[str]:
    """进程要退了(app 的 lifespan 收尾时调): 还有一批编译在路上的项目记 WARNING, 返回这些项目号。**不抛**。

    后台补编是 daemon 线程, 进程一退(Railway 重新部署 —— 改一个变量就重新部署)就被杀在半路: Jev 那批照样编完、照样计费,
    结果没存, 进程内的「编失败 6 小时不重编」也跟着清零, 新进程的 open_project / record_rule 会把同一批再发一遍(2026-10-20
    第三轮复审: 原来一行日志都没有)。排着队还没发出去的只记 INFO —— 没花钱, 新进程会照常补编。"""
    try:
        now = time.monotonic()
        with _RULE_COMPILE_LOCK:
            inflight = {pid: dict(v) for pid, v in _RULE_COMPILE_INFLIGHT.items()}
            queued = sorted(pid for pid, st in _RULE_COMPILE_STATE.items()
                            if st.get("running") and pid not in inflight)
        for pid, v in sorted(inflight.items()):
            logger.warning("后台补编被进程退出打断(project=%s): 一批 %s 条已经发给 Jev %.0f 秒, Jev 那批照样计费, 结果没存; "
                           "新进程里这个项目下一次 open_project / record_rule 会重编(再付一次)",
                           pid, v.get("rules"), now - float(v.get("since") or now))
        if queued:
            logger.info("进程退出时还有 %d 个项目的后台补编在排队、没发(没花钱, 新进程会照常补编): %s",
                        len(queued), ", ".join(queued))
        return sorted(inflight)
    except Exception:                                        # noqa: BLE001 — 退出路径上, 绝不冒泡
        logger.warning("进程退出时没查成后台补编的状态", exc_info=True)
        return []


def _failed_at(error) -> float:
    """记进 _RULE_COMPILE_FAILED 的时刻。Jev 生成端没计费的 429 / 5xx(见 _FREE_GENERATOR_FAILURE)往前记, 让它
    RULE_COMPILE_TRANSIENT_RETRY_SEC 之后就过期; 别的照实记(6 小时)。读的地方(skip_recent_failures、_uncompiled_reason)
    都按「现在 - 这个时刻 < RULE_COMPILE_RETRY_AFTER_SEC」算, 不用另改(独立复审 on aw E)。"""
    now = time.monotonic()
    if _FREE_GENERATOR_FAILURE.search(str(error or "")):
        return now - (RULE_COMPILE_RETRY_AFTER_SEC - RULE_COMPILE_TRANSIENT_RETRY_SEC)
    return now


def compile_rules(client, project_id: str, *, user_id: str | None = None, force: bool = False,
                  dry_run: bool = False, skip_recent_failures: bool = False,
                  seen: set[tuple[str, str]] | None = None) -> dict:
    """把这个项目规则池里还没编过的规则发给 Jev 编译, 结果写进 rule_compiles。**Jev 那边怎么失败都不抛**, 回一份报告。

    池子和简报 / 代码查规则同一个: ``store.shared_memories``(confirmed、没静音、本项目 + owner 自己的 global), hard 和
    soft 都编, 方向闸不在这里过(global 规则跨项目共用, 方向是查的时候按项目定的)。每条按 rulecompile.rule_sha 算 sha,
    (规则 id, sha) 已经有一行的跳过(``force`` 时全部重编、覆盖)。超过 Jev 字段上限的不发, 记在 ``not_compilable``。

    ``user_id`` 给了就过归属闸(同其它入口); 不给是运维 / 后台路径(CLI compile-rules、open_project 和 record_rule 过了闸
    之后起的后台线程), 按项目 owner 取池子。``skip_recent_failures``: 后台用, 近 6 小时编失败过的同一版本这次不编(Jev 生成端
    没计费的 429 / 5xx 只 30 分钟, 见 _failed_at)。
    ``dry_run``: 只数要编几条, 不发; 没配 JUDGE_* / 没接 tv-map 的照样报出来(和真编同一个结局, 2026-10-20 复审)。
    ``seen``(CLI ``--all`` 用, 2026-10-20 复审): 这次运行里已经编进库(dry-run 时: 已经数过)的 (规则 id, sha), 在里面的
    当缓存命中, ``force`` 也不重发 —— owner 的 global 规则在他每个项目的池子里, 不带它 ``--all --force`` 会按项目数重编、
    重付钱, ``--all --dry-run`` 也会按项目数重复计数。这次写进库的 / dry-run 数到的会加进去; 读超时的那一批也加(Jev 多半
    还在编、照样计费, 同一次运行里不再发)。

    报告 ``status``: up_to_date(没有要编的)/ dry_run / ok / partial(一部分没编出来)/ error(一条都没写进去)/
    not_configured / policy_blocked / timeout / unavailable / jev_failed / bad_request(judge_client 的结局, 整批没成)/
    jev_mock(Jev 回的 model 是 mock: 假结果, 一条不存, 不按规则记失败, 见 RULE_COMPILE_MOCK)。连接超时(请求没发出去)是
    unavailable, 不是 timeout: 没花钱, 不停整个进程的后台补编(judge_client.compile_rules)。
    被挡下或读超时之后后面几批不发(``not_sent`` 条, ``stopped_by`` 是哪个结局)。读超时还会让本进程的后台补编对所有项目
    停 RULE_COMPILE_TIMEOUT_HOLD_SEC(``_RULE_COMPILE_JEV``); 本函数自己不看这个停发期 —— 运维的 CLI 要能在 Jev 恢复之后直接重跑。
    """
    t0 = time.monotonic()
    out: dict = {"project_id": project_id, "status": None, "pool": 0, "cached": 0, "to_compile": 0, "compiled": 0,
                 "verdicts": {}, "errors": [], "not_compilable": [], "batches": 0, "calls": 0, "elapsed_ms": 0}

    def _done(status: str, detail: str | None = None) -> dict:
        out["status"] = status
        if detail:
            out["detail"] = _compile_error(detail)
        out["elapsed_ms"] = _ms_since(t0)
        return out

    if user_id:
        project = assert_project_access(client, project_id, user_id=user_id)
    else:
        project = store.project_row(client, project_id)
        if project is None:
            raise ProjectNotFound(f"project not found: {project_id}")
    out["project_name"] = project.get("name") or ""
    owner = str(project.get("owner_id") or "") or None
    try:
        hard, soft = store.shared_memories(client, project_id, owner, vectors=False)
    except Exception as exc:                                 # noqa: BLE001 — 报告里说清, 不抛
        logger.exception("compile_rules: 读规则池失败(project=%s)", project_id)
        return _done("error", f"读规则池失败: {type(exc).__name__}: {exc}")
    items: dict[str, tuple[dict, str]] = {}
    for r in hard + soft:
        item, sha = rulecompile.compile_input(r)
        if item is None:
            out["not_compilable"].append({"memory_id": r.get("id"), "reason": sha})
        else:
            items.setdefault(item["id"], (item, sha))
    out["pool"] = len(hard) + len(soft)
    try:
        cached = store.rule_compiles_for(client, list(items))
    except Exception as exc:                                 # noqa: BLE001
        logger.exception("compile_rules: 读 rule_compiles 失败(project=%s)", project_id)
        return _done("error", f"读 rule_compiles 失败(migrations/015 跑了吗?): {type(exc).__name__}: {exc}")
    todo = [(it, sha) for mid, (it, sha) in items.items() if force or (mid, sha) not in cached]
    if seen:
        same_run = sum(1 for it, sha in todo if (it["id"], sha) in seen)
        if same_run:
            out["same_run"] = same_run                       # 这次运行里别的项目已经编过(owner 的 global 规则)
            todo = [(it, sha) for it, sha in todo if (it["id"], sha) not in seen]
    out["cached"] = len(items) - len(todo)
    if skip_recent_failures and not force:
        now = time.monotonic()
        with _RULE_COMPILE_LOCK:
            fresh = {k for k, t in _RULE_COMPILE_FAILED.items() if now - t < RULE_COMPILE_RETRY_AFTER_SEC}
        held = [(it, sha) for it, sha in todo if (it["id"], sha) in fresh]
        if held:
            out["held_after_failure"] = len(held)
            todo = [(it, sha) for it, sha in todo if (it["id"], sha) not in fresh]
    out["to_compile"] = len(todo)
    if not todo:
        return _done("up_to_date")
    if not judge_client.configured():
        return _done(judge_client.JUDGE_NOT_CONFIGURED, "JUDGE_URL / JUDGE_API_KEY 未配置, 规则编不了")
    ctx = _judge_project_context(client, project_id)
    if ctx["project"] is None:
        # 同存档判定的数据出境口径(docs/00 #7): 认不出 TV 项目号就不发规则原文。这里是 CLI / 后台, 报带补救命令的那句
        return _done(ctx["status"], ctx.get("ops_detail") or ctx["detail"])
    out["tv_project"], out["category"] = ctx["project"], ctx["category"]
    if dry_run:
        # 放在上面两道之后: dry-run 是真编之前看数的那一步, 真编会被挡的项目在这里就要报出来(2026-10-20 复审)
        if seen is not None:
            seen.update((it["id"], sha) for it, sha in todo)
        return _done("dry_run")

    def _failed(mid: str, sha: str, error: str) -> None:
        out["errors"].append({"memory_id": mid, "error": _compile_error(error)})
        with _RULE_COMPILE_LOCK:
            _RULE_COMPILE_FAILED[(mid, sha)] = _failed_at(error)

    batch_status = None
    size = max(1, int(config.RULE_COMPILE_BATCH))
    for i in range(0, len(todo), size):
        part = todo[i:i + size]
        payload: dict = {"project": ctx["project"], "rules": [it for it, _ in part]}
        if ctx["category"]:
            payload["category"] = ctx["category"]
        # 发之前记一行: 进程在这一批回来之前退出(重新部署)的话, 日志里看得出丢了哪一批(Jev 那边照样编完、计费)
        logger.info("compile_rules: 发一批 %d 条去 Jev 编译(project=%s, 第 %d 批, 共 %d 条要编)",
                    len(part), project_id, out["batches"] + 1, len(todo))
        with _RULE_COMPILE_LOCK:
            _RULE_COMPILE_INFLIGHT[str(project_id)] = {"rules": len(part), "since": time.monotonic()}
        try:
            res = judge_client.compile_rules(payload)
        finally:
            with _RULE_COMPILE_LOCK:
                _RULE_COMPILE_INFLIGHT.pop(str(project_id), None)
        out["batches"] += 1
        if res["judge_status"] == judge_client.JUDGE_OK and _mock_model(res["response"].get("model")):
            res = {"judge_status": RULE_COMPILE_MOCK,
                   "detail": f"Jev 回的 model 是 {res['response'].get('model')}(JUDGE_MOCK 的假结果), 一条不存; "
                             "JUDGE_URL 指的多半是测试 / 本地的 Jev"}
        if res["judge_status"] != judge_client.JUDGE_OK:
            batch_status = res["judge_status"]
            out.setdefault("batch_errors", []).append(
                {"status": batch_status, "rules": len(part), "detail": _compile_error(res.get("detail"))})
            if batch_status not in _RULE_COMPILE_HOLD_STATUSES:
                # 只有超时 / Jev 失败 / 出错这几种可能是规则本身的事, 才按规则记失败。整批被挡(出境口径 / 没接 tv-map /
                # judge 不可用 / 请求形状)是项目的事, 后台的项目级 hold 已经管了; 按规则记的话, owner 的 global 规则在
                # 别的项目(比如他不是处方药的项目)里也会被扣 6 小时不编(2026-10-20 复审)。502 是 Jev 生成端过载(没计费)的
                # 只扣 RULE_COMPILE_TRANSIENT_RETRY_SEC(见 _failed_at; 独立复审 on aw E)
                now = _failed_at(res.get("detail"))
                with _RULE_COMPILE_LOCK:
                    for it, sha in part:
                        _RULE_COMPILE_FAILED[(it["id"], sha)] = now
            if batch_status in _RULE_COMPILE_STOP_STATUSES:
                out["not_sent"] = len(todo) - i - len(part)     # 后面几批一样会被挡 / 一样慢, 不发
                out["stopped_by"] = batch_status
                if batch_status == judge_client.JUDGE_TIMEOUT:
                    logger.warning("compile_rules: 一批 %d 条读超时(project=%s), 后面 %d 条不发; 超时的那批 Jev 多半"
                                   "还在编、照样计费, 结果没人收。Jev 恢复之后再跑一次 compile-rules",
                                   len(part), project_id, out["not_sent"])
                    # 整个进程的后台补编一起停(见 _RULE_COMPILE_JEV); 同一次运行(CLI --all 的 seen)里这一批不再发
                    with _RULE_COMPILE_LOCK:
                        _RULE_COMPILE_JEV["slow_until"] = max(_RULE_COMPILE_JEV["slow_until"],
                                                              time.monotonic() + RULE_COMPILE_TIMEOUT_HOLD_SEC)
                    if seen is not None:
                        seen.update((it["id"], sha) for it, sha in part)
                break
            continue
        resp = res["response"]
        model = resp.get("model") if isinstance(resp.get("model"), str) else None
        if isinstance(resp.get("calls"), int):
            out["calls"] += resp["calls"]
        expected = {it["id"]: sha for it, sha in part}
        got: set[str] = set()
        rows = []
        for r in resp["rules"]:
            mid = r.get("id")
            if mid not in expected or mid in got:
                continue
            got.add(mid)
            if r.get("sha") != expected[mid]:
                # 两边算法漂了: 存下去也永远按这边的 sha 命中不了, 每次都会重编 —— 不存, 报出来
                _failed(mid, expected[mid], f"Jev 回的 sha {r.get('sha')} 和这边算的 {expected[mid]} 对不上")
                continue
            if not rulecompile.usable(r):
                _failed(mid, expected[mid], r.get("error") or "没编出结果")
                continue
            rows.append({"memory_id": mid, "sha": expected[mid], "result": r, "model": model,
                         "compiled_at": store.iso_now()})
        for mid in expected.keys() - got:
            _failed(mid, expected[mid], "Jev 的回包里没有这条")
        try:
            try:
                store.rule_compiles_upsert(client, rows)
            except Exception as exc:                         # noqa: BLE001
                # 一批最多 60 行是一条 INSERT … ON CONFLICT: 编的那几分钟里有一条规则被删了(AW 页上硬删), 外键让整条
                # 语句失败, 另外几十条已经付过钱的结果一起丢、下次还要再付一遍。先看哪几条还在, 只写还在的(2026-10-20 复审)
                alive = store.existing_memory_ids(client, [r["memory_id"] for r in rows])
                gone = [r for r in rows if r["memory_id"] not in alive]
                if not gone:
                    raise
                logger.warning("compile_rules: %d 条规则在编译期间被删, 其余 %d 条照存(project=%s): %s",
                               len(gone), len(rows) - len(gone), project_id, exc)
                for r in gone:                               # 规则没了, 不记 _RULE_COMPILE_FAILED
                    out["errors"].append({"memory_id": r["memory_id"], "error": "规则编译期间被删, 结果没存"})
                rows = [r for r in rows if r["memory_id"] in alive]
                store.rule_compiles_upsert(client, rows)
        except Exception as exc:                             # noqa: BLE001 — 写不进去就停: 再编下一批也是白花钱
            logger.exception("compile_rules: 写 rule_compiles 失败(project=%s)", project_id)
            out["write_error"] = _compile_error(f"{type(exc).__name__}: {exc}")
            now = time.monotonic()
            with _RULE_COMPILE_LOCK:                         # 后台路径不要马上又付一遍钱去编这几条
                for r in rows:
                    _RULE_COMPILE_FAILED[(r["memory_id"], r["sha"])] = now
            break
        out["compiled"] += len(rows)
        for r in rows:
            out["verdicts"][r["result"]["verdict"]] = out["verdicts"].get(r["result"]["verdict"], 0) + 1
        if seen is not None:
            seen.update((r["memory_id"], r["sha"]) for r in rows)
    if out.get("write_error"):
        return _done("error", "编出来了但写不进 rule_compiles(migrations/015 跑了吗?): " + out["write_error"])
    if out["compiled"] == 0 and batch_status:
        return _done(batch_status, out["batch_errors"][-1]["detail"])
    if out["errors"] or out.get("batch_errors"):
        return _done("partial" if out["compiled"] else "error")
    return _done("ok")


def _kick_rule_compile(client, project_id: str, *, reason: str, bypass_cooldown: bool = False) -> str:
    """有没编的规则就在后台补编, **调用方不等**。返回这次做了什么(给日志、测试和 check_rules 的措辞看): started /
    queued / running / cooldown / held / jev_slow / off / error。绝不抛 —— 它挂在 open_project / record_rule / check_rules
    的尾巴上, 是增强项。

    每个项目同时最多一个线程(进程内锁); 线程跑完之后 RULE_COMPILE_COOLDOWN_SEC 之内再触发不起新线程, 免得每次
    open_project 都去查一遍。``bypass_cooldown``(record_rule 用): 刚记下的规则要尽快编, 不等冷却; 正在编的话让那个线程
    跑完再来一轮(它读池子的时候这条规则可能还没写进去)。不管从哪儿触发, 编的都是整个池子里还没编的, 已经编过的不花钱。
    本进程里任何一次编译读超时之后的停发期里(``_RULE_COMPILE_JEV``)一律不起, 回 jev_slow —— ``bypass_cooldown`` 也一样。
    """
    try:
        if not (config.RULE_COMPILE_AUTO and judge_client.configured()):
            return "off"
        pid = str(project_id)
        now = time.monotonic()
        with _RULE_COMPILE_LOCK:
            if now < _RULE_COMPILE_JEV["slow_until"]:
                return RULE_COMPILE_JEV_SLOW
            st = _RULE_COMPILE_STATE.setdefault(pid, {"running": False, "again": False, "last": None,
                                                     "hold_until": 0.0, "last_status": None})
            if st["running"]:
                if bypass_cooldown:
                    st["again"] = True
                    return "queued"
                return "running"
            if not bypass_cooldown:
                if now < st["hold_until"]:
                    return "held"
                if st["last"] is not None and now - st["last"] < RULE_COMPILE_COOLDOWN_SEC:
                    return "cooldown"
            st.update(running=True, again=False, last=now)
        try:
            threading.Thread(target=_rule_compile_worker, args=(client, pid, reason),
                             name=f"rule-compile-{pid[:8]}", daemon=True).start()
        except Exception:
            with _RULE_COMPILE_LOCK:
                st["running"] = False
            raise
        return "started"
    except Exception:                                        # noqa: BLE001 — 增强项, 绝不冒泡
        logger.warning("后台补编没起来(project=%s, reason=%s)", project_id, reason, exc_info=True)
        return "error"


def _rule_compile_worker(client, project_id: str, reason: str) -> None:
    """后台线程本体: 编到没有新触发为止(最多 RULE_COMPILE_MAX_ROUNDS 轮), 每轮先排 _RULE_COMPILE_SLOTS 的队。任何异常只记日志。

    整批被挡下(或停在某种 hold 结局上)停 RULE_COMPILE_HOLD_SEC, 读超时停 RULE_COMPILE_TIMEOUT_HOLD_SEC; 这一轮的结局记进
    ``last_status``, check_rules 拿它说清「为什么还没有编译结果」。拿到槽位之后先看进程级的停发期(``_RULE_COMPILE_JEV``):
    排在前面的项目刚读超时的话这一轮不发, 记 jev_slow(2026-10-20 第二轮复审: 原来 hold 只记在超时的那个项目上, 排着队的
    项目拿到槽位就发, 一个接一个读超时)。"""
    rounds = 0
    try:
        while True:
            rounds += 1
            hold = 0.0
            status = None
            try:
                with _RULE_COMPILE_SLOTS:
                    rep = None if _jev_slow_now() else compile_rules(client, project_id, skip_recent_failures=True)
                if rep is None:
                    status = RULE_COMPILE_JEV_SLOW
                    logger.info("rule compile (project=%s, reason=%s, round=%d): 本进程刚有一批编译读超时, 停发期内"
                                "这一轮不发", project_id, reason, rounds)
                else:
                    status = rep.get("status")
                    stop = rep.get("stopped_by")
                    if status in _RULE_COMPILE_HOLD_STATUSES or stop in _RULE_COMPILE_HOLD_STATUSES:
                        hold = RULE_COMPILE_HOLD_SEC
                        status = status if status in _RULE_COMPILE_HOLD_STATUSES else stop
                    elif judge_client.JUDGE_TIMEOUT in (status, stop):
                        hold = RULE_COMPILE_TIMEOUT_HOLD_SEC
                        status = judge_client.JUDGE_TIMEOUT
                    logger.info("rule compile (project=%s, reason=%s, round=%d): status=%s pool=%s cached=%s "
                                "compiled=%s errors=%s elapsed_ms=%s", project_id, reason, rounds, rep.get("status"),
                                rep.get("pool"), rep.get("cached"), rep.get("compiled"), len(rep.get("errors") or []),
                                rep.get("elapsed_ms"))
            except Exception:                                # noqa: BLE001
                status = "error"
                logger.exception("后台补编失败(project=%s, reason=%s)", project_id, reason)
            with _RULE_COMPILE_LOCK:
                st = _RULE_COMPILE_STATE.setdefault(project_id, {"running": True, "again": False, "last": None,
                                                                 "hold_until": 0.0, "last_status": None})
                st["last_status"] = status
                if hold:
                    st["hold_until"] = time.monotonic() + hold
                    st["hold_status"] = status
                elif status in ("ok", "partial"):
                    # 这一轮真编成了(record_rule 不等 hold): Jev 好了, 项目的 hold 清掉(独立复审 on aw E: 原来只在 hold 时设、
                    # 从不清, Jev 恢复后改过的规则照样被扣到 hold 到期, check_rules 还说「上次整批没编成(ok)」)。
                    # up_to_date / error 不算: 没发、或者是库的事, 说明不了被挡的那件事好了
                    st["hold_until"] = 0.0
                if st["again"] and rounds < RULE_COMPILE_MAX_ROUNDS and status != RULE_COMPILE_JEV_SLOW:
                    st["again"] = False
                    continue
                st.update(running=False, again=False, last=time.monotonic())
                return
    finally:
        with _RULE_COMPILE_LOCK:
            st = _RULE_COMPILE_STATE.get(project_id)
            if st is not None and st.get("running"):
                st.update(running=False, last=time.monotonic())


CHECK_RULES_MAX_VERSIONS = 20
CHECK_RULES_WORKERS = 4
# 每篇列几条: 必须改全列, 建议改补到一共这么多条为止, 其余报条数(返回值太长会把协议挤出注意力)。必须改不截, 同
# rulecheck.top_hits(codex review on aw#115): 截掉的只剩个数, 写手不知道是哪条, 会当没问题交出去
CHECK_RULES_MAX_LISTED = 12
CHECK_RULES_MAX_UNSURE = 5
_CHECK_RULES_TITLE_CHARS = 40
_CHECK_RULES_UNCOVERED_CHARS = 80
CHECK_RULES_NOTE = ("规则检查只是建议, 不拦交付, 存档已经成功了。必须改 = 硬约束没守住(照着改, 改完用 score_drafts 带 "
                    "replaces_version_id 再存一次); 建议改 = 软规则, 告诉用户, 改不改由用户定; 拿不准 = Jev 判不准, 提一句"
                    "就行, 不用改。judge_status 不是 ok 的那几篇是没判完, 不能说它们守住了规则; judge_status 是 ok 的篇里, "
                    "counts.unjudged 数到的那几道规则题也没判出来, 同样不能说守住了。")


# 规则编译结果读不出来(多半是 migrations/015 没跑)时的 next_step。不拦交付、不让重试: 同一个库再调一次还是读不出来
CHECK_RULES_UNREADABLE_STEP = ("这次规则检查没查成, 已记下; 不能说规则守住了, 但存档已经成功, 交付照常, "
                               "不用重试, 不用你做什么。")


class RuleCheckInputError(ValueError):
    """check_rules 的参数不对(不是列表 / 空的 / 有空值)。工具层映成 400。"""


def _check_rules_ids(version_ids) -> tuple[list[str], list[str]]:
    """(这次查的, 留到下一次的)。去重之后前 CHECK_RULES_MAX_VERSIONS 个这次查, 其余原样还回去(``remaining_version_ids``)。

    2026-10-20 第二轮复审: 原来超过上限直接报错。score_drafts 一批不限篇数(22 篇的批次出现过), 协议 5a 让写手把
    check_rules_version_ids 原样传、报错就说「这次规则没查成」—— 超过 20 篇的批次整批都没查。上限还在(一次的篇数都挤在
    同一个 JUDGE_DRAFT_TOOL_TIMEOUT_SEC 的截止里), 只是不再拒, 改成分次。"""
    if not isinstance(version_ids, list) or not version_ids:
        raise RuleCheckInputError("version_ids 要是非空列表: score_drafts 返回的 check_rules_version_ids 原样传")
    if any(not isinstance(v, str) or not v.strip() for v in version_ids):
        raise RuleCheckInputError("version_ids 里每一项都要是非空字符串")
    ids = list(dict.fromkeys(v.strip() for v in version_ids))
    return ids[:CHECK_RULES_MAX_VERSIONS], ids[CHECK_RULES_MAX_VERSIONS:]


def _check_rules_remaining_step(rest: list[str]) -> str:
    return (f" 还有 {len(rest)} 篇这次没查(一次最多查 {CHECK_RULES_MAX_VERSIONS} 篇): 接着再调一次 check_rules, version_ids "
            "传这次返回的 remaining_version_ids(tactic 照这次带), 几次的结果合起来告诉用户。")


_KICK_COMPILING = frozenset({"started", "queued", "running"})


def _ledger_write_failed(resp: dict) -> bool:
    """check_rules 发的是 write=true: 回包的 ``written`` 不是整数、或者带着 ``write_error``, 就是答案没(全)落进 TV 账本
    (Jev ``_write_or_report``: 账本写失败照样回 200, 判出来的答案照样给)。写手这边的结论不受影响, 少的是以后拿来校准
    的那几行 —— 原来一个字都不看, 影子期要攒的数据次次丢了也没人知道(2026-10-20 第三轮复审)。"""
    written = resp.get("written")
    return bool(resp.get("write_error")) or not isinstance(written, int) or isinstance(written, bool)


def _uncompiled_reason(client, project_id: str, missing: list[tuple[str, str]], kick: str) -> tuple[str, str, int]:
    """没有编译结果的那几条规则, 为什么没有: (原因码, 给写手的一句, 其中上次编失败的条数)。

    10-20 复审: 原来一律说「还在后台编译」, 可没接 tv-map / 没配 Jev / 后台补编关着 / 一编就失败的规则**永远**编不出来,
    写手每批都照着说一遍「在编」。所以先认得出来的说清楚, 只有真的起了线程 / 正在编的才说「后台在编」。
    tv-map 多查一次库(只在有没编的规则时), 不靠后台线程的上一轮结局: 部署后第一次调就要说对。"""
    now = time.monotonic()
    with _RULE_COMPILE_LOCK:
        failed = sum(1 for k in missing
                     if k in _RULE_COMPILE_FAILED and now - _RULE_COMPILE_FAILED[k] < RULE_COMPILE_RETRY_AFTER_SEC)
        st = _RULE_COMPILE_STATE.get(str(project_id)) or {}
        # held 时说的是**起 hold 的那一轮**的结局: 之后又有一轮(record_rule 不等 hold)把 last_status 盖成别的, 照它说就成了
        # 「上次整批没编成(ok)」(独立复审 on aw E)
        last = (st.get("hold_status") or st.get("last_status")) if kick == "held" else st.get("last_status")
        slow = now < _RULE_COMPILE_JEV["slow_until"]
    # 第二个值原样进 check_rules 的 next_step(念给写手的, 2026-10-30 发布收尾): 只说"为什么这次查不了、不用做什么",
    # 要配什么、跑什么命令只进日志 —— 原来写着「要运维配 JUDGE_URL」「要运维 tv-map add」, 写手每批都听一遍。
    if not judge_client.configured():
        logger.warning("check_rules: 规则没有编译结果、也编不了 —— Jev 没配置, 要配 JUDGE_URL / JUDGE_API_KEY "
                       "(project=%s)", project_id)
        return "judge_not_configured", "Jev 没配置, 规则编不了, 这次查不了, 已记下", failed
    try:
        mapped = any(m.get("tv_project_id") for m in store.tv_projects_of(client, project_id))
    except Exception:                                        # noqa: BLE001 — 查不了就不下这个结论
        logger.warning("check_rules: 查 tv_project_map 失败(project=%s)", project_id, exc_info=True)
        mapped = True
    if not mapped:
        logger.warning("check_rules: 项目 %s 没接 tv_project_map, 规则编不了 —— 补救: python -m deskcore.cli tv-map add "
                       "--tv-project <TV 项目号> --project %s", project_id, project_id)
        return "no_tv_map", "这个项目还没接上规则编译, 这次查不了, 已记下", failed
    if kick == "off":
        logger.warning("check_rules: 项目 %s 有没编的规则, 但后台补编关着(RULE_COMPILE_AUTO) —— 跑 "
                       "python -m deskcore.cli compile-rules --project %s", project_id, project_id)
        return "auto_off", "后台补编关着, 规则还没编好, 这次查不了, 已记下", failed
    if kick == "held" and last == judge_client.JUDGE_POLICY_BLOCKED:
        return "policy_blocked", "规则发去编译时被出境口径挡下, 编不了", failed
    if kick == "held" and last == RULE_COMPILE_MOCK:
        logger.warning("check_rules: 规则编译接到的是测试用的 Jev(model 是 mock, 结果没存) —— 查 JUDGE_URL "
                       "(project=%s)", project_id)
        return RULE_COMPILE_MOCK, "规则编译这次没接到正式的编译服务, 编出来的没存, 已记下", failed
    # 本进程里任何一个项目的编译读超时之后, 停发期内哪个项目都不发(_RULE_COMPILE_JEV), 不只是超时的那个
    if kick == RULE_COMPILE_JEV_SLOW or slow or (kick == "held" and last == judge_client.JUDGE_TIMEOUT):
        return RULE_COMPILE_JEV_SLOW, "最近一次编译 Jev 超时, 过一阵后台再试", failed
    if kick == "held":
        if not last:
            logger.warning("check_rules: 项目 %s 上次整批没编成, 结局没记下来(见编译线程的日志)", project_id)
        return "held", f"上次整批没编成({last or '原因已记下'}), 过一阵后台再试", failed
    if missing and failed == len(missing):
        return "compile_failed", "上次编失败了, 稍后后台自动重试", failed
    tail = f"; 其中 {failed} 条上次编失败了, 稍后自动重试" if failed else ""
    if kick in _KICK_COMPILING:
        return "compiling", "后台在编" + tail, failed
    return "pending", "稍后后台会再编" + tail, failed


def _never_checked_tip(not_compilable: int, code_refused: int) -> str:
    """编不了(原文超过 Jev 的字段上限之类, 永远不发)/ 编出来的代码检查不安全或形状不对(永远不跑)的规则: 一句话。"""
    n = not_compilable + code_refused
    if not n:
        return ""
    why = "; ".join(([f"{not_compilable} 条编不了: 原文超过 Jev 的上限之类"] if not_compilable else [])
                    + ([f"{code_refused} 条编出来的检查不安全或形状不对, 没跑"] if code_refused else []))
    return f"有 {n} 条规则这次查不了({why}), 不能说它们守住了。"


def _withdrawn_tip(n: int) -> str:
    """写手撤掉了代码检查、Jev 又只编成了代码检查(没有规则题)的规则: 这次什么都没查它(独立复审 on aw E)。"""
    return f"有 {n} 条规则撤掉了代码检查, Jev 又只编成了代码检查(没有规则题), 这次没查, 不能说它们守住了。"


def _questions_unchecked_tip(n: int) -> str:
    return (f"有 {n} 条规则的缓存题目形状不对或题号重复, 部分题目没查(见 rules.questions_unchecked_rules), "
            "不能说整条规则守住了; 已记下, 不用你做什么。")


def _check_rules_next_step(summary: dict, uncompiled: int, uncompiled_why: str = "",
                           code_unavailable: int = 0, not_compilable: int = 0, code_refused: int = 0,
                           partial: int = 0, code_withdrawn: int = 0, questions_unchecked: int = 0) -> str:
    tips = []
    if summary.get("must_fix"):
        tips.append("「必须改」的照 what / evidence 改完, 那几篇用 score_drafts 带 replaces_version_id 再存一次(会重新打分), "
                    "改一轮就够, 不用再调 check_rules。")
    if summary.get("suggest"):
        tips.append("「建议改」的告诉用户, 改不改由用户定。")
    if summary.get("unsure"):
        tips.append("「拿不准」的提一句就行, 不用改。")
    if summary.get("more"):
        # 每篇只列一部分(必须改全列, 建议改补到 12 条, 拿不准最多 5 条), 原来截掉的只剩每篇的 more, next_step 一字不提,
        # 写手照列出来的说完就算了(同 score_drafts 的 RULE_CHECKS_MORE_NEXT_STEP; 独立复审 on aw E)
        tips.append(f"还有 {summary['more']} 条建议改 / 拿不准没列出来(必须改的全列了; 每篇没列的条数在 more 里): "
                    "如实告诉用户还有这么多条没列。")
    if summary.get("not_finished"):
        tips.append(f"有 {summary['not_finished']} 篇的规则题这次没判完, 不能说它们守住了规则; 不用重试。")
    if summary.get("empty_body"):
        # 空正文那篇不发 Jev, 标题上的题、「正文要带…」的题都没判: 不算没判完, 但也不能算守住了(独立复审 on aw E)
        tips.append(f"有 {summary['empty_body']} 篇正文是空的, 规则题没判, 不能说它们守住了规则。")
    if summary.get("unjudged"):
        tips.append(f"另有 {summary['unjudged']} 道规则题这次没判出来(Jev 回包缺题或判不了), 那几条不能说守住了; 不用重试。")
    if uncompiled:
        tips.append(f"还有 {uncompiled} 条规则还没有编译结果({uncompiled_why}), 这次没查; 不用等, 下一批再说。")
    if code_unavailable:
        tips.append(f"有 {code_unavailable} 条规则要看话题 / 评论, 这两样不随稿子存, check_rules 查不了, 不能说它们守住了。")
    if not_compilable or code_refused:
        tips.append(_never_checked_tip(not_compilable, code_refused))
    if partial:
        tips.append(f"有 {partial} 条规则只查了一部分(Jev 编译时没全装进去, 或者有一部分要看话题 / 评论; 没查的那部分写在 "
                    "rules.partial_rules 的 uncovered 里), 不能说它们整条守住了。")
    if code_withdrawn:
        tips.append(_withdrawn_tip(code_withdrawn))
    if questions_unchecked:
        tips.append(_questions_unchecked_tip(questions_unchecked))
    if summary.get("code_unchecked"):
        # 「同一篇里已经列在 violations 的照改」: 正则先在标题上命中、再在正文上超时时, 同一条同一篇既是「必须改」又在
        # code_unchecked 里, 只说「不是违规」写手会当成连那条命中一起撤了(同 score_drafts 的 rule_checks_unchecked; 独立复审 on aw E)
        tips.append(f"有 {summary['code_unchecked']} 处(篇 × 条)代码检查这次没查完(正则没在时限内跑完, 或者限时的引擎跑不了它; "
                    "见每篇的 code_unchecked): 没查完的那部分不是违规, 也不能说那几条守住了; 同一篇里已经列在 violations 的照改; "
                    "severity 是 hard 的照规则原文(rule)自己核; 不用重试。")
    if not tips:
        # 只有上面每一项都是 0 才说这句(2026-10-20 复审: 没判出来的题、没编的规则、查不了的话题规则都不算守住;
        # 第二轮: 编不了的、编出来的检查不安全没跑的也不算; 第三轮: 只查了一部分的(coverage=partial)也不算;
        # 独立复审 on aw E: 撤掉代码检查又没有题的、空正文没判题的、where 里话题 / 评论那半没查的也不算)
        tips.append("这次查的规则都守住了。")
    return " ".join(tips) + " 都只是建议, 不拦交付。"


def _delivered_text(template: dict | None, version: dict) -> dict:
    """库里的一版 → ``{"title", "body", "tags"}``。``title`` / ``body`` 是 delivery.assemble 拼进 delivery.text 的那一份
    (表格的单元格转义除外), 一字不差; ``tags`` **只是下限**, 见下。

    版本行存的是写手传的原稿(带着末尾那行 #话题 的也原样存)。标题正文先照 assemble 去首尾空白、换行统一成 \\n, 再过
    ``_as_delivered``(score_drafts 的 rule_checks 同一处): 模板管话题时正文摘掉末尾话题(``delivery.delivered_post``);
    拿原稿去判, 只在话题行里的词会被判成「正文里有」, 必须带的话术也能靠被摘走的那行混过去。
    没有模板时正文就是原稿, ``tags`` 是 None(rulecheck._parts 照正文里的 #话题 拼, 同 score_drafts); 模板不管话题时正文也是
    原稿, ``tags`` 是正文里的 #话题(delivery.delivered_draft)。
    ``tags``(模板管话题时)是拿版本行能拼出来的那一行: 末尾被摘走的话题 + 模板的固定 / 条件话题。写手单独传的 ``tags``
    没存(2026-10-20 第三轮复审): 写手传了 tags 的那篇, 交付稿的话题行按写手传的拼, 和这里**对不上**; 只有写手没传 tags 时
    才和 delivery.text 的话题行一样。评论版本行里没有, rulecheck._parts 走 delivery.comment_list 拿到的永远是空的。所以编出来的
    code 里只看话题 / 评论的照旧不跑(rulecompile.UNSTORED_PARTS), 这里的 ``tags`` 实际上没有检查会读。"""
    title = str(version.get("title") or "").strip()
    body = str(version.get("body") or "").replace("\r\n", "\n").strip()
    (post,), tags = _as_delivered(template, [{"title": title, "body": body}])
    return {"title": title, "body": post["body"], "tags": tags[0] if tags is not None else None}


def check_rules(client, project_id: str, version_ids: list[str], *, user_id: str | None = None,
                tactic: str = "") -> dict:
    """写完存过档之后, 按编译好的规则逐篇查(D-121)。只作建议, 不拦任何东西, 不改库里的稿子。

    1. 归属: 项目过 assert_project_access; version_ids 只认本项目的(store.versions_in_project, 同存档判定核自带
       version_id 的那条 inner join), 别的项目的 / 不存在的 / 不是 UUID 的记进 ``excluded``, 不查、不发;
       查的是交付出去的那一份(``_delivered_text``: 模板管话题时正文摘掉末尾那行话题), Jev 判的和代码查的都是它;
    2. 池子: 同简报 —— shared_memories(本项目 + 调用者自己的 global), soft 过方向闸(filter_by_applicability, 按项目名
       + 写手原样传回来的 ``tactic`` 判, 同简报和 score_drafts 的代码查规则); 每条按 sha 找 rule_compiles 里的编译结果,
       找不到的数进 ``uncompiled_rules`` 并说清为什么没有(_uncompiled_reason), 触发后台补编;
    3. 规则题: 编出来的 questions 拼成内联项目题库, 每篇单独发一次 /judge_draft(banks=[]、不判段、write=true、
       run_tag=shadow-rules), 最多 4 路并行, 整次共用 JUDGE_DRAFT_TOOL_TIMEOUT_SEC(默认 18 秒)的截止 —— 到点没回来的
       记「没判完」, 绝不算守住; 回来了但一道题都没判出来的(回包没有 desk_rules / 每题都带 invalid_reason)同样记没判完,
       判出了一部分的, 没判出来的题数进 ``counts.unjudged`` / ``summary.unjudged``(2026-10-20 复审);
    4. 代码查: 编出来的 code 交给 rulecheck, 只跑没有人手设过 check 的规则(人设的 score_drafts 已经查过); 只看话题 /
       评论的不跑(版本行里没存这两样), 数进 ``rules.code_unavailable``; 不安全 / 形状不对没跑的数进 ``rules.code_refused``,
       不算进 ``rules.by_code``。编不了的(原文超过 Jev 的字段上限之类)数进 ``rules.not_compilable``。这三种和没编译结果的
       一样, next_step 不会说它们守住了(2026-10-20 第二轮复审)。人设过 check 的数进 ``rules.by_score_drafts``; 写手撤掉了
       代码检查、又没有规则题的数进 ``rules.code_withdrawn``(这次没查, 不说守住了); where 里混着话题 / 评论、只跑了标题 /
       正文那一半的数进 ``rules.partial``(独立复审 on aw E)。

    ``version_ids`` 一次查前 CHECK_RULES_MAX_VERSIONS 个(去重之后), 多出来的原样放进 ``remaining_version_ids``、next_step
    让再调一次(2026-10-20 第二轮复审: score_drafts 一批不限篇数, 原来超过上限整次报错, 写手照协议说规则没查成)。
    """
    started = time.monotonic()
    project = assert_project_access(client, project_id, user_id=user_id)
    ids, rest = _check_rules_ids(version_ids)
    canon = {raw: str(uuid.UUID(raw)) for raw in ids if _is_uuid(raw)}
    found = store.versions_in_project(client, project_id, sorted(set(canon.values()))) if canon else {}
    excluded, targets, seen = [], [], set()
    for raw in ids:
        vid = canon.get(raw)
        if vid is None or vid not in found:
            excluded.append({"version_id": raw, "reason": JUDGE_SKIP_NOT_IN_PROJECT})
        elif vid not in seen:
            seen.add(vid)
            targets.append((vid, found[vid]))
    # Jev 判的、代码查的都是交付出去的那一份(同 score_drafts 的 rule_checks, delivery.delivered_post)。模板坏了(有人手改了库)
    # 同 score_drafts: 记日志, 照原稿查, 不拦
    template = None
    if targets:
        try:
            template = delivery.template_of(project)
        except Exception as exc:                             # noqa: BLE001 — 增强项, 绝不冒泡
            logger.warning("check_rules: 项目 %s 的交付模板用不了, 照原稿查(%s)", project_id, exc)
    targets = [(vid, _delivered_text(template, v)) for vid, v in targets]

    hard, soft = store.shared_memories(client, project_id, user_id, vectors=False)
    # 方向闸的判据同简报和 score_drafts: 项目名 + 这场 open_project 传的 tactic(写手原样传回来)。只按项目名判的话,
    # 项目名不带方向、靠 tactic 判出方向的那场, 简报挡掉的规则这里又当成题发给 Jev
    soft, _dropped = filter_by_applicability(soft, detect_direction(project.get("name") or "", tactic or ""))
    pool = hard + soft
    shas: dict[str, tuple[dict, str]] = {}
    not_compilable = 0
    for r in pool:
        item, sha = rulecompile.compile_input(r)
        if item is None:
            not_compilable += 1
        else:
            shas.setdefault(item["id"], (r, sha))
    try:
        compiled = store.rule_compiles_for(client, list(shas)) if shas else {}
    except Exception:                                        # noqa: BLE001 — 见 CHECK_RULES_UNREADABLE_STEP
        # migrations/015 没跑(或库从 015 之前的基线恢复)时 PostgREST 回 PGRST205。原来直接抛: REST 是 500(模型读成「待会儿
        # 重试」), MCP 是一个工具错误, 服务端 deskcore 一行日志都没有 —— 每次存档的 next_step 都让写手来调, 次次这样
        # (2026-10-20 第三轮复审)。改成回一份 status=error 的结果: 写手照样知道规则没查、不能说守住了, 服务端有日志。
        logger.exception("check_rules: 读 rule_compiles 失败(project=%s), 这次规则没查 —— migrations/015 跑了吗?", project_id)
        out = {"project_id": project_id, "status": "error", "note": CHECK_RULES_NOTE, "versions": [],
               "rules": {"pool": len(pool)}, "reason": "rule_compiles_unreadable",
               "next_step": CHECK_RULES_UNREADABLE_STEP}
        if excluded:
            out["excluded"] = excluded
        return out
    pairs, missing = [], []
    for mid, (r, sha) in shas.items():
        res = (compiled.get((mid, sha)) or {}).get("result")
        if isinstance(res, str):
            try:
                res = json.loads(res)
            except ValueError:
                res = None
        if rulecompile.usable(res):
            pairs.append((r, res))
        else:
            missing.append((mid, sha))
    uncompiled = len(missing)

    skipped_questions: set[str] = set()
    bank = rulecompile.project_bank([res for _, res in pairs], skipped=skipped_questions)
    questions = rulecompile.question_index(pairs, bank)
    code, code_unavailable = rulecompile.code_rules(pairs)
    # payload 在这里按人设 check 的同一套规矩校一遍(rulecheck.spec_of: 正则过 validator.assert_regex_is_safe + 量词形状那道),
    # 不合格的记日志、不跑, 数进 rules.code_refused; 先挑出来是为了知道**哪几条**真跑了(下面 partial 只数真查了的)
    runnable = [r for r in code if rulecheck.spec_of(r) is not None]
    judged = {q["rule_id"] for q in questions.values()}
    # code_rules 跳过的两种(独立复审 on aw E: 原来哪个桶都不进, rules 里池子 1 条、各项全是 0; nothing_to_check 时还把它们
    # 说成「流程、交付、写作方向」那种查不了): 人设了能跑的 check 的(score_drafts 存档时已经按人设的查过)、写手撤掉了
    # 代码检查的(Jev 只编成了 code、没有规则题时这次什么都没查它, 不能说它守住了)。
    skipped_code = [(r, res) for r, res in pairs if str(r.get("id")) not in judged and isinstance(res.get("code"), dict)]
    by_score_drafts = sum(1 for r in pool if rulecompile.hand_set(r))
    code_withdrawn = sum(1 for r, _ in skipped_code if rulecompile.withdrawn(r))
    rules_info = {"pool": len(pool),
                  "by_jev": len(judged),
                  "by_code": len(code),
                  "not_checkable": sum(1 for _, res in pairs if res.get("verdict") == "not_checkable"),
                  "uncompiled": uncompiled}
    if not_compilable:
        rules_info["not_compilable"] = not_compilable
    if by_score_drafts:
        rules_info["by_score_drafts"] = by_score_drafts
    if code_withdrawn:
        rules_info["code_withdrawn"] = code_withdrawn
    if skipped_questions:
        rules_info["questions_unchecked"] = len(skipped_questions)
        rules_info["questions_unchecked_rules"] = sorted(skipped_questions)
        # 写手那边只说「部分题目没查、已记下」(_questions_unchecked_tip), 要修的是编译结果, 记在这里
        logger.warning("check_rules: 项目 %s 有 %d 条规则的缓存题目形状不对或题号重复, 部分题目没查 —— 要修编译结果"
                       "(compile-rules --force): %s", project_id, len(skipped_questions), sorted(skipped_questions)[:10])
    # Jev 编译时标了 coverage=partial 的(一条规则的要求没全装进题 / 代码, 没装进去的写在 uncovered): 这次查了的也只是
    # 一部分, 题全守住 / 代码没命中也不能说整条守住了(2026-10-20 第三轮复审: 原来 coverage / uncovered 这边一处都不读,
    # 「标题不超过 20 字, 正文要带某句话术」只编出了标题那一半, 标题短就报「这次查的规则都守住了」)。只数这次真查了的。
    # 编出来的 where 里混着话题 / 评论、只跑了标题 / 正文那一半的(code_rules 的 stripped)也算(独立复审 on aw E)
    used = judged | {str(r.get("id")) for r in runnable}
    stripped = {str(r.get("id")): r["stripped"] for r in runnable if r.get("stripped")}
    partial = [(str(r.get("id")), res) for r, res in pairs
               if str(r.get("id")) in used and (res.get("coverage") == "partial" or str(r.get("id")) in stripped)]
    if partial:
        def _uncovered(rid: str, res: dict) -> str:
            bits = [str(res.get("uncovered") or "").strip()] if res.get("coverage") == "partial" else []
            if rid in stripped:
                bits.append("、".join(rulecheck.PART_CN.get(w, w) for w in stripped[rid]) + "那部分没查(不随稿子存)")
            return "; ".join(b for b in bits if b)[:_CHECK_RULES_UNCOVERED_CHARS]
        rules_info["partial"] = len(partial)
        rules_info["partial_rules"] = [{"rule_id": rid, "uncovered": _uncovered(rid, res)}
                                       for rid, res in partial[:CHECK_RULES_MAX_UNSURE]]
    if code_unavailable:
        rules_info["code_unavailable"] = len(code_unavailable)      # 只看话题 / 评论, 版本行里没有, 没跑
    uncompiled_why = ""
    if uncompiled:
        kick = _kick_rule_compile(client, project_id, reason="check_rules")
        reason, uncompiled_why, failed = _uncompiled_reason(client, project_id, missing, kick)
        rules_info["uncompiled_reason"] = reason
        if failed:
            rules_info["compile_failed"] = failed
    out: dict = {"project_id": project_id, "note": CHECK_RULES_NOTE, "rules": rules_info,
                 "uncompiled_rules": uncompiled}
    if excluded:
        out["excluded"] = excluded

    if not questions and not code:
        out["status"] = "nothing_to_check"
        out["versions"] = []
        parts = []
        if uncompiled:
            parts.append(f"这个项目的规则还没有编译结果({uncompiled} 条, {uncompiled_why}), 这次一条都查不了。")
        if code_unavailable:
            parts.append(f"有 {len(code_unavailable)} 条规则要看话题 / 评论, 这两样不随稿子存, check_rules 查不了, "
                         "不能说它们守住了。")
        if not_compilable:
            parts.append(_never_checked_tip(not_compilable, 0))
        if code_withdrawn:
            parts.append(_withdrawn_tip(code_withdrawn))
        if skipped_questions:
            parts.append(_questions_unchecked_tip(len(skipped_questions)))
        if by_score_drafts:
            # 人设了 check 的: 不是查不了, 是 score_drafts 存档时已经查过; 那边报的「必须改」照改, 不能说「不用做什么」
            # (独立复审 on aw E)
            parts.append(f"有 {by_score_drafts} 条规则人手设过代码检查, score_drafts 存档时已经按它查过(那次 rule_checks 里的"
                         "「必须改」照改), 这里不重复查。")
        if parts:
            out["next_step"] = " ".join(parts) + (" 不用等, 照常交付, 下一批写完再调 check_rules。" if uncompiled
                                                  else " 照常交付。")
        elif not pool:
            out["next_step"] = "这个项目没有可查的规则, 不用做什么, 照常交付。"
        elif rules_info["not_checkable"] == len(pairs):
            # 只在每条编译结果都是 not_checkable 时这么说(独立复审 on aw E: 原来池子不空就这么说, 人设了 check 的、撤掉了
            # 代码检查的也被说成流程规则)
            out["next_step"] = "这个项目的规则都是查不了的那种(流程、交付、写作方向), 不用做什么, 照常交付。"
        else:
            # 剩下的是编译结果用不上的(题的形状全不对、code 的 kind 不认识): 没查, 不能说守住了
            logger.warning("check_rules: 项目 %s 的编译结果都用不上(题的形状全不对 / code 的 kind 不认识), 一条都没查",
                           project_id)
            out["next_step"] = "这次规则检查没查全(编译结果用不上), 已记下; 不能说它们守住了。交付照常, 不用你做什么。"
        return out
    if rest:
        out["remaining_version_ids"] = rest
    if not targets:
        out["status"] = "no_versions"
        out["versions"] = []
        out["next_step"] = ("version_ids 里没有这个项目的稿子(excluded 里写着原因): 传 score_drafts 这次返回的 "
                            "check_rules_version_ids。查不了不影响交付。")
        if rest:
            out["next_step"] += _check_rules_remaining_step(rest)
        return out

    # ── 代码查: 编出来的 code, 只给没人手设过 check 的规则 ──
    by_index: dict[int, list[dict]] = {}
    code_stats = {"rules": 0, "skipped": 0}
    if runnable:
        # 话题同 score_drafts 的 rule_checks: 模板管话题时传交付出去的那一行, 不然 None(rulecheck 照正文拼)
        hits, code_stats = rulecheck.check(runnable, [{"title": v["title"], "body": v["body"]} for _, v in targets],
                                           tags=[v["tags"] for _, v in targets])
        for h in hits:
            by_index.setdefault(h["index"], []).append(h)
    # 正则没在时限内查完的(rulecheck.check 的 timed_out_from: 从第几篇起): 那几篇这条算没查, 不能算进「守住了」
    # (validator.search_user_regex 限时之后才有这一种; 同 score_drafts 的 rule_checks_unchecked)
    code_timed = code_stats.get("timed_out_from") or {}
    if code_timed:
        rules_info["code_timed_out"] = len(code_timed)
    rule_rows = {str(r.get("id")): r for r in pool}
    if len(code) - len(runnable):
        rules_info["code_refused"] = len(code) - len(runnable)  # 正则不安全 / 形状不对, 没跑
    # 交给代码查的条数: 不安全 / 形状不对没跑的不算; 正则没在时限内查完的算在里面(同 score_drafts meta 的 rule_checks.rules),
    # 另数在 code_timed_out(独立复审 on aw E: 文档原来说成「真跑了的」, 限时引擎一篇都没搜成的那条也在里面)
    rules_info["by_code"] = code_stats.get("rules", 0)

    # ── 规则题: 每篇单独发一次 /judge_draft, 整次共用一个截止 ──
    jev: dict[str, dict] = {}
    jev_detail = None
    if questions:
        budget = judge_client.tool_timeout_sec()

        def _left() -> float:
            return max(0.0, budget - (time.monotonic() - started))

        if not judge_client.configured():
            jev_detail = "这个写作台还没接判定服务, 规则题这次判不了。"
            jev = {vid: {"judge_status": judge_client.JUDGE_NOT_CONFIGURED} for vid, _ in targets}
        else:
            ex = cf.ThreadPoolExecutor(max_workers=CHECK_RULES_WORKERS, thread_name_prefix="check-rules")
            futs: dict = {}
            done: set = set()
            try:
                try:
                    ctx = ex.submit(_judge_project_context, client, project_id).result(timeout=_left())
                except cf.TimeoutError:
                    logger.warning("check_rules: 发规则题之前的查库(tv_project_map / TV 品类)在 %.1fs 内没查完, "
                                   "没发 (project=%s)", budget, project_id)
                    ctx = {"project": None, "category": None, "status": judge_client.JUDGE_TIMEOUT,
                           "detail": f"发规则题之前查库超时({budget:g}s), 这次没发。"}
                if ctx["project"] is None:
                    jev_detail = ctx["detail"]
                    jev = {vid: {"judge_status": ctx["status"]} for vid, _ in targets}
                elif _left() <= 0.5:
                    jev_detail = f"查库用完了 {budget:g}s 的预算, 规则题没来得及发"
                    jev = {vid: {"judge_status": judge_client.JUDGE_TIMEOUT} for vid, _ in targets}
                else:
                    def _judge_before_deadline(payload):
                        left = _left()
                        if left <= 0:
                            return {"judge_status": judge_client.JUDGE_TIMEOUT}
                        return judge_client.judge_draft_full(payload, timeout=left)

                    for vid, v in targets:
                        if not (v["body"] or "").strip():
                            jev[vid] = {"judge_status": "empty_body"}
                            continue
                        payload = judge_client.build_rules_request(
                            version_id=vid, title=v["title"], body=v["body"], project=ctx["project"],
                            category=ctx["category"], project_bank=bank)
                        futs[ex.submit(_judge_before_deadline, payload)] = vid
                    done, _pending = cf.wait(list(futs), timeout=_left())
            finally:
                ex.shutdown(wait=False, cancel_futures=True)
            for f, vid in futs.items():
                if f not in done:
                    jev[vid] = {"judge_status": judge_client.JUDGE_TIMEOUT}
                    continue
                try:
                    res = f.result()
                except Exception as exc:                     # noqa: BLE001 — judge_draft_full 本不抛, 兜底
                    res = {"judge_status": judge_client.JUDGE_ERROR, "detail": f"{type(exc).__name__}: {exc}"}
                jev[vid] = res

    versions = []
    ledger_errors: list[str] = []
    summary = {"versions": len(targets), "must_fix": 0, "suggest": 0, "unsure": 0, "not_finished": 0, "unjudged": 0}
    for i, (vid, v) in enumerate(targets):
        row: dict = {"version_id": vid, "title": (v["title"] or "")[:_CHECK_RULES_TITLE_CHARS]}
        found_v = rulecompile.code_violations(by_index.get(i, []))
        unsure: list[dict] = []
        unjudged: list[dict] = []
        if questions:
            res = jev.get(vid) or {"judge_status": judge_client.JUDGE_TIMEOUT}
            status = res.get("judge_status")
            row["judge_status"] = status
            resp = res.get("response") if status == judge_client.JUDGE_OK else None
            if isinstance(resp, dict) and _ledger_write_failed(resp):
                ledger_errors.append(vid)
                logger.warning("check_rules: 规则题的答案没落进 TV 账本(project=%s, version=%s, written=%r): %s",
                               project_id, vid, resp.get("written"),
                               mask_secrets(str(resp.get("write_error") or "回包里没有 written"))[:_JUDGE_BLOCK_TEXT_MAX])
            if isinstance(resp, dict) and resp.get("invalid_reason"):
                row["judge_status"] = "not_judged"
                row["judge_note"] = f"规则题没判: {resp['invalid_reason']}(多半是正文太短)"
                summary["not_finished"] += 1
            elif isinstance(resp, dict):
                raw = resp.get("detail")
                detail = raw.get(judge_client.RULES_BANK_NAME) if isinstance(raw, dict) else None
                got = rulecompile.read_answers(questions, detail)
                found_v += got["violations"]
                unsure = got["unsure"]
                unjudged = got["unjudged"]
                summary["unjudged"] += len(unjudged)
                if unjudged and len(unjudged) == len(questions):
                    # 回来了, 却一道题都没判出来(回包里没有 desk_rules / 每题都带 invalid_reason): 和没判完一样, 不能当
                    # 守住了(2026-10-20 复审: 原来这里只记个数, summary 不看它, next_step 照说「都守住了」)
                    row["judge_status"] = "not_judged"
                    row["judge_note"] = ("规则题一道都没判出来(" + (
                        "回包里没有规则题的答案" if not isinstance(detail, dict) else
                        "Jev 标了判不了: " + "、".join(sorted({u["reason"] for u in unjudged}))
                    ) + "), 不能当作守住了规则")
                    summary["not_finished"] += 1
                if unjudged:
                    row["unjudged"] = unjudged[:CHECK_RULES_MAX_UNSURE]
            else:
                row["judge_note"] = ("没判完: 这一篇的规则题这次没在时限内判完, 不能当作守住了规则"
                                     if status == judge_client.JUDGE_TIMEOUT else
                                     "这一篇正文是空的, 规则题没判" if status == "empty_body" else
                                     f"规则题这次没判成({status}), 不能当作守住了规则")
                if status != "empty_body":
                    summary["not_finished"] += 1
                else:
                    # 空正文不算没判完(本来就没东西可判), 可标题上的题、「正文要带…」的题都没问, 也不能算守住了: 单独计数,
                    # next_step 不再说「都守住了」(独立复审 on aw E)
                    summary["empty_body"] = summary.get("empty_body", 0) + 1
                if res.get("detail"):
                    row["judge_detail"] = mask_secrets(str(res["detail"]))[:_JUDGE_BLOCK_TEXT_MAX]
        code_unchecked = [{"rule_id": rid, "severity": "hard" if str((rule_rows.get(str(rid)) or {}).get("severity") or "")
                           .lower() == "hard" else "soft", "rule": (rule_rows.get(str(rid)) or {}).get("content") or ""}
                          for rid, at in code_timed.items() if i >= at]
        if code_unchecked:
            row["code_unchecked"] = code_unchecked
            summary["code_unchecked"] = summary.get("code_unchecked", 0) + len(code_unchecked)
        found_v.sort(key=lambda e: e["severity"] != "hard")
        must = sum(1 for e in found_v if e["severity"] == "hard")
        row["violations"] = found_v[:max(must, CHECK_RULES_MAX_LISTED)]      # 必须改在前, 全列
        row["unsure"] = unsure[:CHECK_RULES_MAX_UNSURE]
        more = len(found_v) - len(row["violations"]) + len(unsure) - len(row["unsure"])
        if more:
            row["more"] = more
            summary["more"] = summary.get("more", 0) + more             # next_step 要说还有这么多条没列(独立复审 on aw E)
        row["counts"] = {"must_fix": must, "suggest": len(found_v) - must, "unsure": len(unsure),
                         "unjudged": len(unjudged)}
        summary["must_fix"] += must
        summary["suggest"] += len(found_v) - must
        summary["unsure"] += len(unsure)
        versions.append(row)
    if ledger_errors:
        # 只给运维看(写手的 next_step 不提): 判是判出来了, 少的是以后校准用的那几行账本
        rules_info["ledger_write_errors"] = len(ledger_errors)
        try:
            import telemetry
            telemetry.log_event("deskcore_check_rules_ledger", project_id=project_id, versions=len(targets),
                                ledger_write_errors=len(ledger_errors), version_ids=ledger_errors[:CHECK_RULES_MAX_VERSIONS])
        except Exception:                                    # noqa: BLE001 — 埋点不能拖垮查规则
            pass
    out["status"] = "checked"
    out["versions"] = versions
    out["summary"] = summary
    if jev_detail:
        out["judge_detail"] = mask_secrets(str(jev_detail))[:_JUDGE_BLOCK_TEXT_MAX]
    out["next_step"] = _check_rules_next_step(summary, uncompiled, uncompiled_why, len(code_unavailable),
                                              not_compilable, rules_info.get("code_refused", 0), len(partial),
                                              code_withdrawn, len(skipped_questions))
    if rest:
        out["next_step"] += _check_rules_remaining_step(rest)
    out["elapsed_ms"] = _ms_since(started)
    return out


_CALIB_SYSTEM = """\
你在维护一份「个人写作调校笔记」。输入是同一个人对 AI 初稿做的手动精修 —— \
左边是 AI 写的，右边是这个人改成的样子。

你的任务：从这些改动里提炼出【这个人的语感偏好】，写成可执行的短句。

铁律：
1. 只写从改动里【看得出来】的偏好。看不出来就不写，宁可少写。
2. 不要复述具体内容（"把郁可唯改成了别的明星"没有价值）；要写模式\
（"倾向去掉明星名，用泛指的场景代替"）。
3. 每条一行，以动词开头，可以直接当写作指令用。
4. 合并同类项。新观察和已有笔记说的是一件事，就改写已有那条让它更准，\
不要并列两条。
5. 与已有笔记冲突时以新观察为准（人的偏好会变）。
6. 总长度控制在 20 行以内。超了就合并最弱的几条。

只输出笔记正文，不要解释，不要 markdown 标题。"""


def record_edit(client, project_id: str, *, user_id: str,
                ai_title: str = "", ai_body: str = "",
                my_title: str = "", my_body: str = "",
                note: str | None = None, distill: bool = True) -> dict:
    """喂一条手动精修 diff —— 【信号 A, 最高权重】, "裂变"的入口。

    口径照 memory.generate_calibration_notes(:1154-1210): 只收人真的动手改了的
    对子。没改就通过的稿子【不算教学材料】—— memory.py:1191-1194 明确拒绝从
    那里学, 理由是模型会从偶然选择里编造风格规则。
    """
    assert_project_access(client, project_id, user_id=user_id)   # 审计 COR-015
    if not (my_title or my_body):
        raise ValueError("my_title/my_body must not both be empty")
    if ((ai_title or "").strip() == (my_title or "").strip()
            and (ai_body or "").strip() == (my_body or "").strip()):
        return {"stored": False,
                "reason": "AI 版与手改版完全一致, 不是有效信号, 未入库"}

    store.add_style_edit(client, project_id, user_id,
                         ai_title=ai_title or "", ai_body=ai_body or "",
                         my_title=my_title or "", my_body=my_body or "", note=note)
    out: dict = {"stored": True}
    if not distill:
        out["next_step"] = ("本次没有要蒸馏(distill=false)。diff 已入库, "
                            "下次蒸馏时会连它一起算。")
        return out

    task = build_distillation_task(client, project_id, user_id=user_id)
    if task is None:                      # 理论上不会 —— 上面刚存了一条
        out["next_step"] = "没有可蒸馏的材料。"
        return out
    out["distillation_task"] = task
    out["next_step"] = (
        "⚠️ 这一步【还没做完】。请按 distillation_task.instruction 的口径, "
        "拿 existing_notes 和 edits 提炼出【更新后的完整笔记】, 然后调用 "
        "save_my_style 写回去, 并把 distillation_task.edit_ids 原样传回。"
        "不写回去的话这次精修等于白喂 —— diff 存下来了, 但文风不会变。")
    return out


def build_distillation_task(client, project_id: str, *, user_id: str,
                            max_edits: int = 8) -> dict | None:
    """把"该蒸馏什么"打包成一份【交给调用方模型做】的任务。返回 None 表示没料。

    ⚠️ 这里【不调 LLM】。deskcore 原来自己拿 Anthropic 客户端蒸馏, 那违反了本
    方案自己的原则 ——「推理归 WorkBuddy, MCP 只做轻量数据操作」。搬走之后
    deskcore 不再需要 ANTHROPIC_API_KEY / DESKCORE_MODEL, 也少一个中转站故障点;
    而写作台本来就跑在一个有模型的环境里, 让它做这点文本提炼是顺手的事。

    返回的三样东西调用方模型全都要用上:
      existing_notes —— 已有笔记, 要在它基础上改写而不是另起一份
      edits          —— 本次要吸收的手动精修对子
      instruction    —— 提炼口径(原 _CALIB_SYSTEM), 口径留在服务端是刻意的:
                        它是这套东西的一部分, 不该由每个平台各自发挥。
    """
    edits = store.recent_style_edits(client, project_id, user_id, max_edits)
    if not edits:
        return None
    existing, _ = store.get_user_calibration(client, project_id, user_id)

    blocks = []
    for e in edits:
        b = ("AI 原版：\n"
             f"  标题：{e.get('ai_title','')}\n"
             f"  正文：{(e.get('ai_body') or '')[:400]}\n"
             "手改版：\n"
             f"  标题：{e.get('my_title','')}\n"
             f"  正文：{(e.get('my_body') or '')[:400]}")
        if e.get("note"):
            b += f"\n  本人备注：{e['note']}"
        blocks.append(b)

    total_pending = store.count_pending_distillation(client, project_id, user_id)
    out = {
        "instruction": _CALIB_SYSTEM,
        "existing_notes": existing,
        "edits": "\n\n---\n".join(blocks),
        "edit_count": len(edits),
        # ⚠️ 这批的确切 id。save_my_style 只销这几条的账 —— 见
        # store.mark_edits_distilled 的说明(全量销账会吃掉快照外的行)。
        "edit_ids": [e["id"] for e in edits if e.get("id")],
    }
    if total_pending > len(edits):
        out["more_pending"] = total_pending - len(edits)
        out["note"] = (
            f"这个人还有 {total_pending - len(edits)} 条精修没进本次快照"
            f"(一次最多取 {max_edits} 条)。写回之后【再调一次 record_edit 或看 "
            "my_style】会拿到下一批, 别以为一次就吸收完了。")
    return out


def save_my_style(client, project_id: str, notes: str, *, user_id: str,
                  edit_ids: list[str] | None = None) -> dict:
    """把调用方模型蒸馏好的笔记写回, 并把【这一批】diff 标记为已吸收。

    ⚠️ 只有【真的写回来】才算完成一次学习。record_edit 只是把 diff 存下来 +
    把任务交出去; 中间断掉的话 diff 还在库里(下次还会拿到), 但笔记不会变 ——
    也就是"喂了稿子却没变得更像我"。my_style 的 pending_distillation 就是给
    这个断点用的可见性。

    ⚠️ edit_ids 必须原样回传 record_edit 给的那一份。不传 = 只改笔记、不销任何
    账 —— 这正是"用户说这条笔记不对, 直接改一下"那种用法应有的行为: 手动改写
    笔记【不等于】吸收了那些待处理的精修, 顺手把它们标掉会让它们静默消失。
    (codex review #56 P1)
    """
    assert_project_access(client, project_id, user_id=user_id)   # 审计 COR-015
    notes = (notes or "").strip()
    if not notes:
        raise ValueError("notes 不能为空 —— 空笔记会把已有的个人风格清掉")
    store.save_user_calibration(client, project_id, user_id, notes)
    marked = store.mark_edits_distilled(client, project_id, user_id, edit_ids or [])
    out = {"saved": True, "notes": notes, "lines": len(notes.splitlines()),
           "edits_absorbed": marked,
           "pending_distillation": store.count_pending_distillation(
               client, project_id, user_id)}
    if edit_ids and marked < len(edit_ids):
        out["warning"] = (
            f"传了 {len(edit_ids)} 个 edit_id 但只销掉 {marked} 条 —— "
            "多半是其中几条已经被别处吸收过了。笔记已保存。")
    if not edit_ids:
        out["note"] = ("没传 edit_ids, 所以只更新了笔记、没有销账。"
                       "如果这是在吸收精修而不是手动改写笔记, "
                       "要把 record_edit 返回的 edit_ids 原样传进来。")
    if out["pending_distillation"]:
        out["still_pending"] = (
            f"还有 {out['pending_distillation']} 条精修没被吸收 —— "
            "看 my_style 的 pending_distillation_task 接着做。")
    return out


def label_example(client, item_id: str, label: str | None,
                  *, user_id: str | None = None) -> dict:
    """标正/负例。复用 db.set_item_example_label —— 它顺带清三处缓存。

    ⚠️ 【必须校验归属】。items.example_label 是【个人】风格资产(私有层), 而
    deskcore 持 service_role 绕 RLS —— 没有这道校验的话, 任何拿到别人 item
    UUID 的调用方都能改别人的正负例池, 进而污染那个人的写作风格。
    RLS 在 Streamlit 路径下挡住了这件事, 到了 service_role 路径就得自己挡。

    ⚠️ 负例【只取人工标注】。TV D-040 讲得很清楚: 「赢」需要真的好, 「输」有
    太多无辜理由(撞流量墙 / 账号限流 / 时机), 从数据反推负例会把被埋没的
    好内容也标成垃圾, 污染负面特征库。
    """
    if label not in ("positive", "negative", None):
        raise ValueError("label must be 'positive', 'negative' or None")
    if not user_id:
        raise PermissionError(
            "label_example 需要调用者身份 —— 正负例是个人资产, 不能匿名改。"
            "服务端要配 DESKCORE_KEYS 或 DESKCORE_DEFAULT_USER_ID。")

    owner = store.item_owner(client, item_id)
    if owner is None:
        raise ValueError(f"item not found: {item_id}")
    if str(owner) != str(user_id):
        raise PermissionError(
            f"item {item_id} 属于别人, 不能改它的 example_label。"
            "正负例是个人风格资产(私有层), 项目规则才是团队共享的。")

    db.set_item_example_label(client, item_id, label)
    return {"item_id": item_id, "example_label": label}


def my_style(client, project_id: str, *, user_id: str) -> dict:
    """我在这个项目上的风格资产。"""
    # 校验 + 取行一次搞定(原来这里就要 get_project)。审计 COR-015
    project = assert_project_access(client, project_id, user_id=user_id)
    shared = (project or {}).get("calibration_notes") or ""
    mine, updated = store.get_user_calibration(client, project_id, user_id)
    pending = store.count_pending_distillation(client, project_id, user_id)
    out = {
        "project_id": project_id,
        "shared_calibration": shared.strip(),
        "my_calibration": mine,
        "my_calibration_updated_at": updated,
        "edits_fed": store.count_style_edits(client, project_id, user_id),
        "my_positive_examples": len(store.labeled_examples(client, project_id, "positive", user_id)),
        "my_negative_examples": len(store.labeled_examples(client, project_id, "negative", user_id)),
        # ⚠️ 蒸馏搬到调用方模型之后, record_edit 和 save_my_style 是两步。
        # 两步之间断掉 = diff 存了但笔记没更新, 也就是"喂了稿子却没变得更像我",
        # 而且【完全无声】。这个计数就是那个断点的可见性: 大于 0 说明有精修
        # 还没被吸收进笔记。
        "pending_distillation": pending,
    }
    # ⚠️ 光报个数字不够 —— 断点最典型的形态就是【会话没了】(超时、换了个
    # session), 那时 record_edit 那次的返回值也一起丢了。只给数字的话, 调用方
    # 拿不回材料和口径, 我在 SKILL.md 里写的"重新提炼一次写回"根本做不到,
    # 除非让用户把同一条精修再喂一遍(那会造重复行)。
    # 所以 pending > 0 时把任务原样带上, 恢复就是自明的。
    # (codex review #56 P2)
    if pending:
        task = build_distillation_task(client, project_id, user_id=user_id)
        if task:
            out["pending_distillation_task"] = task
            out["next_step"] = (
                "有精修还没被吸收进笔记。按 pending_distillation_task.instruction "
                "提炼出更新后的完整笔记, 调 save_my_style 写回, "
                "并把 task 里的 edit_ids 原样传回去。")
    return out


# ══════════════════════════════════════════════════════════════════════
# 飞轮经验卡(转调 TV 馆员)
# ══════════════════════════════════════════════════════════════════════

# ── 飞轮分臂 (TV docs/28 §7.4, D-116) ──────────────────────────────────────
# 写作台 15% 的批次随机不注入闸二已验证规律, 作对照组: 飞轮只喂自己验证过的东西就只会反复学已经在做的事,
# 规律什么时候失效只有留着没照做的那部分看得出来。随机在【批次】级做: 按 (项目, 写手, 北京日期) 哈希定臂,
# 借卡时定下、入库时把借卡那次的臂记进 batches.params.flywheel_arm (见下面 batch_arm)。
# 确定性哈希而不是 random: 重启、重试、两条入口 (open_project / borrow_lessons) 同一天结果一致。
FLYWHEEL_EXPLORE_PCT = 15
FLYWHEEL_ARMS = ("patterns", "explore")
_CN_TZ = timezone(timedelta(hours=8))


def flywheel_arm(project_id, user_id, *, now: datetime | None = None) -> str:
    """'explore' (不注入规律) | 'patterns'。"""
    day = (now or datetime.now(timezone.utc)).astimezone(_CN_TZ).date().isoformat()
    h = int(hashlib.sha256(f"{project_id}|{user_id or ''}|{day}".encode("utf-8")).hexdigest()[:8], 16)
    return "explore" if h % 100 < FLYWHEEL_EXPLORE_PCT else "patterns"


# 入库记的是【这场写作借卡时】的臂, 不是入库这一刻重算的 (codex review on #104): 一次写作跨了北京 0 点, 两天的
# 哈希可能不同, 重算会把拿到规律的批次记成对照组。三层, 先到先用, 来源记进 batch 的 flywheel_arm_source:
#   · "brief"      —— open_project / borrow_lessons 把臂随简报交给模型, 模型入库时原样传回 (commit_drafts 的
#                     flywheel_arm)。只有它分得清同一写手同一项目同时开着的两场 (各拿各的简报);
#   · "borrow"     —— 模型没传: 用这个 (项目, 写手) 最近一次借卡时记下的臂 (进程内存; deskcore 单进程,
#                     railway.json 的 uvicorn 没开 --workers);
#   · "recomputed" —— 重启过 / 36 小时内没借过卡: 按入库时重算。分析对照组时可以只看前两种。改稿不建新 batch,
#                     挂的旧 batch 来源还是它自己的, 所以另标 flywheel_arm_unverified (见 _mark_mixed_arm_batches)。
#
# ⚠️ 模型传回的臂要和服务端对得上 (W-31, 2026-10-27)。10-26 sportsix 那场借卡是 explore (TV 缓存键 `:explore`,
# 没给规律), 5 批里 4 批模型传回的却是 patterns, 照单记成了 patterns —— 对照组被污染, 分析时只能手工剔。所以进程里
# 记的是这个 (项目, 写手) 近 36 小时【每个臂最后一次】发出去的时刻, 不只最近一次 (_reported_arm_check):
#   · 进程里什么都没有 (重启后没借过 / 36 小时没借过) → 无从核对, 照旧采用传回的, 来源 brief;
#   · 传回的是最近一次发的臂, 或者 12 小时 (_ARM_REPORT_TTL) 内发过 → 采用 (同时开着两场、跨北京 0 点各拿一个臂的,
#     两个都在, 各记各的, codex review on #104 不退)。不放宽到 36 小时: 同一个写手昨天开过一场别的臂, 今天这场
#     传错就核不出来 (W-31 复审查生产库: 近 30 天 21 个 (项目, 写手, 北京日) 里 4 个前一天 36 小时内有一场另一个臂的);
#   · 进程起来不到 12 小时、传回的臂正好是起来之前那一段某个北京日的臂 → 可能是重启前那场拿到的, 核对不了: 记服务端
#     最近一次发的臂、来源 borrow, 标 flywheel_arm_unverified (分析对照组时剔掉; 改稿挂的旧 batch 也标, 见
#     _mark_mixed_arm_batches), 回包带 flywheel_arm_unverified, 不说「服务端分的是 X」(W-31 复审: 部署重启落在一场
#     写作中间时, 那样说是错的);
#   · 其余 → 服务端没给过这场这个臂, 模型手里不可能有它对应的简报: 以服务端为准, 记最近一次发的臂、来源 borrow,
#     传回的那个另存 flywheel_arm_reported, 回包带 flywheel_arm_corrected。
# borrow_lessons 的 reuse 走同一道 (核对不了的也沿用): 服务端没发过的臂不沿用, 否则一句传错的臂先被借卡记下来,
# 入库时就核不出来了。没选「标 mixed」: 被纠正的那批成稿只受过一种处理 (服务端发的那个臂), 剔掉反而白丢样本。
# 剩下核不准的: ① 一场写作开了 12 小时以上、中间同一个写手又借到另一个臂 —— 那一场后面的批次会被纠正成新的臂;
# ② 前一晚 (12 小时内) 那一场的臂, 第二天一早新开的一场传错成它 —— 照收。docs/deskcore.md §3.12。
_ARM_TTL = timedelta(hours=36)
_ARM_REPORT_TTL = timedelta(hours=12)
_borrowed_arms: dict[tuple[str, str], dict[str, datetime]] = {}     # (项目, 写手) → {臂: 最后一次发出的时刻}
_borrowed_arms_lock = threading.Lock()
_ARMS_SINCE = datetime.now(timezone.utc)    # 进程起来的时刻: 这之前发出去的臂 _borrowed_arms 里没有 (重启丢了)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _issued_arms(key: tuple[str, str], now: datetime) -> dict[str, datetime]:
    """这个 (项目, 写手) 近 _ARM_TTL 内服务端发过的臂 → 最后一次发出的时刻。调用方持锁。"""
    return {a: t for a, t in (_borrowed_arms.get(key) or {}).items() if now - t <= _ARM_TTL}


def _arm_maybe_issued_before_restart(project_id, user_id, arm: str, now: datetime) -> bool:
    """进程起来还不到 _ARM_REPORT_TTL 时, 起来之前那一段发过什么臂 _borrowed_arms 里没有。``arm`` 正好是那一段里
    某个北京日哈希出来的臂 → 可能是重启前那场拿到的, 核对不了 (W-31 复审)。纯函数, 不读写库。"""
    start, end = now - _ARM_REPORT_TTL, min(_ARMS_SINCE, now)
    if end <= start:
        return False
    day, last = start.astimezone(_CN_TZ).date(), end.astimezone(_CN_TZ).date()
    while day <= last:
        if flywheel_arm(project_id, user_id, now=datetime(day.year, day.month, day.day, 12, tzinfo=_CN_TZ)) == arm:
            return True
        day += timedelta(days=1)
    return False


def _reported_arm_check(project_id, user_id, reported: str, issued: dict[str, datetime], now: datetime) -> str:
    """模型传回的臂 (已认得出) 对不对得上服务端发过的 (见上面那段): ``"ok"`` 采用 | ``"unverified"`` 核对不了 |
    ``"corrected"`` 服务端没给过这场这个臂。``issued`` = _issued_arms 的结果。"""
    if not issued:
        return "ok"
    latest = max(issued, key=issued.get)
    if reported == latest or (reported in issued and now - issued[reported] <= _ARM_REPORT_TTL):
        return "ok"
    if _arm_maybe_issued_before_restart(project_id, user_id, reported, now):
        return "unverified"
    return "corrected"


def _borrow_arm(project_id, user_id, *, reuse: str | None = None) -> str:
    """借卡时定臂, 并记下来给入库用。同一 (项目, 写手) 每个臂留最后一次发出的时刻。
    ``reuse`` = 这场写作已经拿到的臂 (模型把 open_project 给的原样传回): 认得出、而且对得上服务端发过的 (或者核对
    不了, 见 _reported_arm_check) 就沿用, 不重新分 —— 同一场里先借后借跨了北京 0 点, 重新分会让手里已有规律的会话
    被记成探索臂 (codex review on #104)。服务端没给过这场的不沿用 (W-31), 按当天重新分。"""
    now = _utcnow()
    key = (str(project_id), str(user_id or ""))
    with _borrowed_arms_lock:
        issued = _issued_arms(key, now)
        if reuse in FLYWHEEL_ARMS and _reported_arm_check(project_id, user_id, reuse, issued, now) != "corrected":
            arm = reuse
        else:
            arm = flywheel_arm(project_id, user_id, now=now)
        issued[arm] = now
        _borrowed_arms[key] = issued
    return arm


def batch_arm(project_id, user_id, *, from_brief: str | None = None) -> tuple[str, str, str | None]:
    """入库时这批记哪个臂: (臂, 'brief' | 'borrow' | 'recomputed', 传回的臂为什么没采用)。``from_brief`` = 模型传回的
    简报里的臂。第三项 (W-31, 见上面那段): None = 采用了 / 没传; ``"corrected"`` = 服务端没给过这场这个臂, 以服务端
    最近一次发的为准; ``"unverified"`` = 重启过核对不了, 也记服务端最近一次发的, 调用方标 flywheel_arm_unverified。"""
    now = _utcnow()
    with _borrowed_arms_lock:
        issued = _issued_arms((str(project_id), str(user_id or "")), now)
    if from_brief in FLYWHEEL_ARMS:
        check = _reported_arm_check(project_id, user_id, from_brief, issued, now)
        if check == "ok":
            return from_brief, "brief", None
        return max(issued, key=issued.get), "borrow", check     # 不是 ok 时 issued 一定非空
    if issued:
        return max(issued, key=issued.get), "borrow", None
    return flywheel_arm(project_id, user_id, now=now), "recomputed", None


def _mark_mixed_arm_batches(client, item_ids: list[str], arm: str, *, unverified: bool = False) -> list[str]:
    """改稿挂在旧 item 上 (旧 batch), 不建新 batch。改稿这场的臂和旧 batch 记的不一样 → 这批的成稿受过两种处理,
    在 batch 上标 flywheel_arm_mixed, 分析对照组时剔掉 (codex review on #104)。旧 batch 没记臂 (分臂之前的) 不在
    实验里, 不碰。

    ``unverified`` = 改稿的臂核对不了: 传回的臂核对不了 (batch_arm 的 ``"unverified"``, 重启过), ``arm`` 只是服务端
    重启后最近一次发的, 改稿真正的臂可能是传回的那个; 或者没传、按入库时重算 (``"recomputed"``), ``arm`` 是猜的。
    拿 ``arm`` 比同臂, 不等于改稿没换过处理 —— 旧 batch 也标 flywheel_arm_unverified, 分析对照组时一样剔掉
    (codex review on aw#130)。和 ``arm`` 不同的照旧另标 mixed。

    不影响入库 (失败重试一次, 再失败就放过), 但**不只记日志**: 返回没能确认标好的 item_id, 调用方把它们写进
    commit 的返回值 (flywheel_arm_warning) 和 batch_metrics.meta —— 分析时那一份也能把这几批剔掉。"""
    last_exc: Exception | None = None
    for _ in range(2):
        try:
            for batch_id, params in store.batch_params_for_items(client, item_ids).items():
                if params.get("flywheel_arm") not in FLYWHEEL_ARMS:
                    continue
                patch = {**({"flywheel_arm_mixed": True} if params["flywheel_arm"] != arm else {}),
                         **({"flywheel_arm_unverified": True} if unverified else {})}
                patch = {k: v for k, v in patch.items() if not params.get(k)}
                if patch:
                    store.merge_batch_params(client, batch_id, patch)
            return []
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    logger.warning("commit_drafts: 改稿跨臂 / 臂核对不了的 batch 没标上 flywheel_arm_mixed / flywheel_arm_unverified "
                   "(items=%s): %s", item_ids, last_exc)
    return list(item_ids)


def borrow_lessons(client, project_id: str, *, user_id: str | None = None,
                   flywheel_arm: str | None = None, **delta) -> dict:
    """复用 librarian_client 的 build_brief + fetch_flywheel_lessons(R-032)。

    那边已经处理好 fail-open(超时/非 200/未配 → [], 绝不阻塞写稿)和 brief 的
    字段集对齐(docs/15 §0 契约)。这里只做项目查询 + 转发。

    ⚠️ 借来的经验卡本身是公司公共资产, 但**发给馆员的 brief 是拿项目行拼的**
    (品牌 / 定位 / 战术), 所以入口照样要校验归属 —— 否则它就成了一个"用别人的
    project_id 就能读出那个项目怎么定位"的接口。(审计 COR-015)
    """
    project = assert_project_access(client, project_id, user_id=user_id)
    arm = _borrow_arm(project.get("id"), user_id, reuse=flywheel_arm)   # 这场已经有臂就沿用
    out = _borrow_for_brief(project, delta, user_id=user_id, arm=arm)
    if out.get("validated_patterns"):       # 用法同简报那份(10-20): 每篇最多一条、轮换、有几篇不用
        out["validated_patterns_usage"] = _patterns_usage_for(project.get("id") or project_id, user_id,
                                                              len(out["validated_patterns"]))
    return out


# open_project 随简报带卡时的上限, 与常规生成路径 render_flywheel_block 同一
# 口径(memory.FLYWHEEL_CARD_CAP): 馆员契约上限也是 5, 这里只是钉死"两条入口
# 给模型看的卡数一致", 免得哪天馆员放宽了, 简报被十几张卡撑爆。
LESSON_CAP = int(getattr(memory, "FLYWHEEL_CARD_CAP", 5) or 5)

# 借阅结局里"要人管"的那三种 —— 留痕用。empty 是正常结局, 不记 WARN。
_BORROW_NEEDS_ATTENTION = frozenset({
    librarian_client.BORROW_NOT_CONFIGURED,
    librarian_client.BORROW_TIMEOUT,
    librarian_client.BORROW_ERROR,
})


def _borrow_for_brief(project: dict, delta: dict | None, *, user_id: str | None = None,
                      arm: str | None = None) -> dict:
    """borrow_lessons 与 build_writing_brief 共用的借阅体。**绝不抛**。

    输入是已经过归属校验的项目行 + 本次 delta(tactic / draft_topic /
    key_messages / target_audience / tone; 多余的键会被丢掉, 因为
    librarian_client.build_brief 只认契约里那几个)。

    ⚠️ 空列表有**五种**来路, 对调用方的含义完全不同(2026-09-16 评测 AW-05):
    没匹配上是正常的, 没配 key 是部署漏了, 超时是 TV 那边慢了。只回一个
    count=0 的话, 模型只能猜, 而它多半会猜成"这个项目没有可借的经验"。
    所以 status 永远带, 且三种"要人管"的结局各记一条 WARN —— 这是 TV docs/27
    要求的"fail-open 要留痕": 服务日志里 grep 得到通道 2 到底为什么黑。
    """
    delta = delta or {}
    allowed = ("tactic", "draft_topic", "key_messages",
               "target_audience", "tone", "extra_instructions")
    kwargs = {k: (delta.get(k) or "") for k in allowed if delta.get(k)}
    st: dict = {}
    arm = arm or _borrow_arm(project.get("id"), user_id)
    try:
        brief = librarian_client.build_brief(project, **kwargs)
        brief["consumer"] = "deskcore"
        # 显式带 explore 才参与分臂 (TV 那边不带这个键的老消费方不下发规律)
        brief["explore"] = arm == "explore"
        selected = librarian_client.fetch_flywheel_lessons(brief, status=st)
    except Exception as exc:  # noqa: BLE001 — 增强项, 失败不能拖垮简报
        logger.exception("flywheel borrow crashed (project_id=%s)", project.get("id"))
        st = {"state": librarian_client.BORROW_ERROR, "count": 0,
              "elapsed_ms": 0, "detail": f"{type(exc).__name__}: {exc}"[:300]}
        selected = []
    if not isinstance(selected, list):
        selected = []
    lessons = [L for L in selected if isinstance(L, dict)][:LESSON_CAP]
    state = st.get("state") or librarian_client.BORROW_ERROR
    if state in _BORROW_NEEDS_ATTENTION:
        logger.warning("flywheel borrow %s (project_id=%s elapsed_ms=%s): %s",
                       state, project.get("id"), st.get("elapsed_ms"),
                       st.get("detail") or "")
    return {"lessons": lessons, "count": len(lessons),
            "status": state,
            "elapsed_ms": st.get("elapsed_ms"),
            "detail": st.get("detail") or "",
            "validated_patterns": list(st.get("patterns") or []),
            "flywheel_arm": arm}


def _borrow_for_brief_bounded(project: dict, delta: dict | None,
                              budget: float | None = None, *, user_id: str | None = None) -> dict:
    """``open_project`` 用的借阅体: 和 ``_borrow_for_brief`` 一样绝不抛, 但**只等
    ``OPEN_PROJECT_BORROW_SEC``**(默认 10 秒), 到点先把简报交出去。

    为什么要有这一层(TV 2026-10-08 审计 A-04): 借阅原来直接在 open_project 的同步路径上
    等满 ``LIBRARIAN_TIMEOUT_SEC``(60 秒), 而 MCP 客户端只容忍 ~22 秒 —— 超过的不是"少几张
    卡", 是**整份简报连 P0 硬约束一起丢**, 协议让模型停笔。TV 侧实测 09-22 冷借 13 次里
    12 次超过 22 秒。馆员选卡要跑一次 LLM, 把 60 秒改小不行(test_librarian_timeout 钉着);
    把等待从必经路径上拆出来才行。

    到点之后借阅线程不取消: TV 那边会把这次选卡跑完并写缓存, 模型稍后调 borrow_lessons
    (同参数命中缓存即回)就拿得到。``detail`` 里写清这一点, 模型读得到。
    """
    budget = float(config.OPEN_PROJECT_BORROW_SEC if budget is None else budget)
    # 臂在这里先定: 借阅超时 / 线程起不来, 简报也要带着臂交出去 (入库时原样传回)
    arm = _borrow_arm(project.get("id"), user_id)
    try:
        ex = cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="borrow")
        fut = ex.submit(_borrow_for_brief, project, delta, user_id=user_id, arm=arm)
    except Exception as exc:  # noqa: BLE001
        # 线程池建不起来 / submit 失败 (RuntimeError: can't start new thread, 解释器关闭中…): 这一层的
        # 承诺和 _borrow_for_brief 一样是"绝不抛" —— 抛出去 open_project 整份简报连 P0 硬约束一起丢
        # (codex review on #93)。按借阅出错回, 简报照交。
        logger.warning("flywheel borrow could not start (project_id=%s): %s: %s",
                       project.get("id"), type(exc).__name__, exc)
        return {"lessons": [], "count": 0,
                "status": librarian_client.BORROW_ERROR,
                "elapsed_ms": 0,
                "detail": f"借阅线程起不来: {type(exc).__name__}: {exc}"[:300],
                "validated_patterns": [], "flywheel_arm": arm}
    try:
        try:
            return fut.result(timeout=budget)
        except cf.TimeoutError:
            logger.warning("flywheel borrow exceeded open_project budget %.1fs (project_id=%s); "
                           "brief returned without cards, borrow thread left running",
                           budget, project.get("id"))
            return {"lessons": [], "count": 0,
                    "status": librarian_client.BORROW_TIMEOUT,
                    "elapsed_ms": int(budget * 1000),
                    "validated_patterns": [], "flywheel_arm": arm,
                    "detail": (f"open_project 的借阅预算 {budget:g}s 内馆员没回(选卡要跑一次 LLM, "
                               "冷路径常要 30-80s)。TV 那边会继续选完并写缓存 —— 想要卡的话稍后"
                               "调 borrow_lessons, 同样的选题/战术参数命中缓存即回")}
    finally:
        ex.shutdown(wait=False)


def create_project(client, name: str, *, brand: str = "",
                   user_id: str | None = None) -> dict:
    """新建一个项目。owner **恒为调用者**, 不接受 owner 参数。

    为什么必须有这个工具(2026-08-27): 在此之前 deskcore 的十二个工具里没有
    "新建项目"这个动作 —— 建项目的代码只在停用中的 Streamlit 工作台里。
    于是现场卡成死结: 文案想给新品牌开项目, 模型如实回答"我没有这个工具";
    想跳过项目直接写, skill 又强制要求先 open_project 取硬约束。**新品牌
    完全进不来**, 而这一层在任何文档里都没写。

    ⚠️ 签名里【没有】owner_id, 这是安全属性不是疏忽。归属恒等于
    ``user_id``(即这把 key 映射到的人)。加一个 owner 参数就等于允许调用方
    往别人名下建项目 —— 今天刚踩过同类的坑: 一把标着"Ziao"的 key 实际指向
    同事的账号, 拿它写稿会把稿子记到别人的历史库里。归属只能由服务端定。

    撞名保护分两层, 都不是可选的:

    · **同名 → 不建**, 把已有项目的 id 还回去(``created=False``)。建重了的
      代价不是多一行: 两个同名项目 = 两套互不可见的历史库, 查重从此对这个
      方向失效, 而且**不报错**。宁可返回已有的。
    · **同品牌 → 照建, 但把兄弟项目列出来**(``siblings``)。"途鸽"已经有
      D-1..D-7 七个子项目, 再建一个叫"途鸽"的顶层项目多半是误操作, 但也可能
      是真的要开新方向 —— 这个只有人能判断, 所以给信息不拦。
    """
    if not user_id:
        raise PermissionError(
            "无法识别调用者身份, 不能建项目 —— 项目归属按 owner 隔离。"
            "服务端要配 DESKCORE_KEYS 或 DESKCORE_DEFAULT_USER_ID。")

    name = (name or "").strip()
    if not name:
        raise ValueError("project name must not be empty")
    brand = (brand or "").strip()

    dupes = store.projects_with_exact_name(client, owner_id=user_id, name=name)
    if dupes:
        hit = dupes[0]
        return {
            "project_id": hit["id"],
            "name": hit.get("name") or name,
            "brand": hit.get("brand") or "",
            "created": False,
            "note": (f"你名下已经有一个叫「{name}」的项目, 没有新建 —— "
                     "直接用这个 project_id。建同名的第二个会让历史稿分裂成"
                     "两套互不可见的库, 查重从此对这个方向失效。"
                     "确实要另开方向的话, 换一个能区分的名字。"),
        }

    row = db.create_project(client, user_id=user_id, name=name, brand=brand)
    pid = (row or {}).get("id")
    if not pid:
        # 不静默返回半个结果: 调用方拿不到 id 就没法接着 open_project, 而
        # "建成功了但没 id"会让人以为项目在库里, 下一步才炸且看不出根因。
        raise RuntimeError(
            f"建项目之后没拿到 id(name={name!r}) —— PostgREST 没回插入行, "
            "多半是 service client 的 returning 行为变了")

    siblings = [{"project_id": p["id"], "name": p.get("name") or ""}
                for p in store.projects_with_brand(client, owner_id=user_id,
                                                   brand=brand)
                if p["id"] != pid] if brand else []

    out = {"project_id": pid, "name": name, "brand": brand, "created": True,
           "siblings": siblings}
    if siblings:
        out["siblings_note"] = (
            f"品牌「{brand}」名下已经有 {len(siblings)} 个项目。新建的这个是"
            "独立的一套历史库, 跟它们【不互相查重】。如果本意是在已有方向下"
            "继续写, 应该用那个项目而不是这个新的 —— 告诉用户, 让他确认。")
    return out


def list_projects(client, *, user_id: str | None = None) -> list[dict]:
    """**我的**项目清单 + 每个项目手上有多少料。

    审计 COR-015: 这个清单原来返回**全库所有项目**(名称/品牌/owner_id/规则条数/
    指纹数), 而且是三个 needs_user=False 的工具之一 —— 也就是任一持有效 key 的
    调用方都能把整个库的项目台账拉出来, 顺带拿到一批可以喂给其它工具的
    project_id。现在按 owner 过滤, 判据收在 ``assert_project_access`` 的同一处口径。

    审计 SUP-004: 原来每个项目发 2 次查询(规则一次 + 指纹 count 一次), 40 个
    项目 = 81 次往返 —— 而这是模型最常调的第一个工具, 每次开工都要等它。
    更亏的是规则那次走的是 shared_memories, 它为了给相关性过滤准备数据会把每条
    规则的 **768 维 embedding** 一起拉回来, 而这里只用了两个 len()。

    现在是【3 次固定查询】: 项目清单 + 规则批量计数 + 指纹批量计数, 与项目
    个数无关(加 owner 过滤不改变这个性质 —— 它只是第一次查询多一个 .eq)。
    指纹计数走 migrations/004 的 RPC(PostgREST 不会 GROUP BY);
    RPC 没部署时退回逐项目 count —— 慢, 但清单仍然是对的。
    """
    if not user_id:
        raise PermissionError(
            "无法识别调用者身份, 不能列项目 —— 清单按 owner 隔离。"
            "服务端要配 DESKCORE_KEYS 或 DESKCORE_DEFAULT_USER_ID。")
    projects = store.list_all_projects(client, owner_id=user_id)
    if not projects:
        return []
    pids = [p["id"] for p in projects]

    rule_counts = store.rule_counts_bulk(client, pids)
    fp_counts = store.fingerprint_counts(client, pids)
    if fp_counts is None:
        # 迁移没跑: 退回 N 次 count。单个项目查失败按 0 计, 与旧行为一致 ——
        # 计数只是清单上的提示, 不该让整个 list_projects 挂掉。
        _rpc_missing_telemetry("deskcore_fingerprint_counts", pids[0])
        fp_counts = {}
        for pid in pids:
            try:
                fp_counts[pid] = (client.table("draft_fingerprints")
                                    .select("id", count="exact")
                                    .eq("project_id", pid).limit(1)
                                    .execute()).count or 0
            except Exception:
                fp_counts[pid] = 0

    # 补录副本数(TV 审计 B-19): 两次固定查询, 不随项目数增长(见 store.ingest_copy_counts)。
    ingest_counts = store.ingest_copy_counts(client, pids)

    out = []
    for p in projects:
        pid = p["id"]
        hard_n, soft_n = rule_counts.get(pid, (0, 0))
        out.append({"project_id": pid, "name": p.get("name") or "",
                    "brand": p.get("brand") or "", "owner_id": p.get("owner_id"),
                    "hard_rules": hard_n, "soft_rules": soft_n,
                    "fingerprint_count": int(fp_counts.get(pid, 0) or 0),
                    # 补录进来的 item 数 —— 独立的一个数, 不是 fingerprint_count 的子集 (补录半途失败的
                    # 副本有 item 没指纹, codex review on #93), 两者别相减。它的用处是让模型别把
                    # "1,800 条积累"读成"写过 1,800 篇"。
                    "ingest_copies": int(ingest_counts.get(pid, 0) or 0)})
    return out


# ══════════════════════════════════════════════════════════════════════
# 写作台 ↔ TV 的稿子对照 + 补录 (migrations/009, deskcore/tvlink.py)
# ══════════════════════════════════════════════════════════════════════

def _tv_owner(client, project_id: str) -> str:
    row = store.project_row(client, project_id)
    if row is None:
        raise ProjectNotFound(f"project not found: {project_id}")
    owner = row.get("owner_id")
    # 系统任务, 以项目 owner 的身份跑 —— 补录进去的稿子归 owner, 与运营在
    # WorkBuddy 里自己 commit 的一样。这里过一遍归属闸是为了让"每个项目级入口
    # 都调过 assert_project_access"这条不变量成立, 不是因为会拒(owner 必过)。
    assert_project_access(client, project_id, user_id=owner)
    return str(owner)


TV_SYNC_INGEST_CHUNK = 100    # 一次 ingest_published 补几条; 与 tools 的 50 上限无关


def _resolve_any_version(client, project_of: dict, version_id: str):
    """TV 自己带的 version_id 可能指向某个 item 的**旧版**(不是 best), 对照索引里
    只有 best 那一版。按对照表里的每个写作台项目再问一次库(items_for_versions
    只认本项目的 version, 别的项目的查不出来)。回 (project_id, item_id) 或 None。"""
    for pid in project_of:
        try:
            hit = store.items_for_versions(client, pid, [version_id]).get(version_id)
        except Exception:                            # noqa: BLE001
            logger.exception("tv_sync: 按 version_id 反查 item 失败 (project=%s)", pid)
            hit = None
        if hit:
            return pid, hit.get("item_id")
    return None


def tv_resolve(client, note_id: str, *, ingest: bool) -> dict:
    """人看过一条 ``ambiguous`` 之后的判定: 目前只有一种 —— "对不上, 按正文补录"。

    tv_sync 判不出的留 candidates 给人看、不硬猜(两版同标题的稿子, 正文都和笔记
    不像 —— 2026-09-18 途鸽那条, 两版都是评论稿, 真正的正文写作台里没有)。人看完
    说"对不上", 就该和 unmatched 一样把正文补进指纹库, 否则这篇发出去的稿子查重
    永远看不见它。判定结果写回 tv_note_links: match_kind → ingested, 带 version_id;
    原来的 candidates 保留, 前面加一条"人工判定"。之后每天的 tv_sync 把它当已对上
    跳过; --write-tv 对 ingested 不回写(见 _backfillable)。

    "指定某一版"这个判定**故意没有**: match_kind 的 CHECK 里没有 manual 一档,
    硬记成 body_exact 是撒谎。真需要时加迁移, 不在这儿蒙。
    """
    if not ingest:
        raise ValueError("tv_resolve 目前只有 --ingest 一种判定")
    link = store.tv_link(client, note_id)
    if link is None:
        raise ValueError(f"tv_note_links 里没有 {note_id!r} —— 先跑 tv-sync")
    if link.get("version_id"):
        raise ValueError(f"{note_id} 已经对上了({link['match_kind']} → "
                         f"{link['version_id']}), 没什么可判的")
    tv_project_id = link["tv_project_id"]
    maps = [m for m in store.tv_project_map(client) if m["tv_project_id"] == tv_project_id]
    target = next((m for m in maps if m.get("ingest_target")), None)
    if target is None:
        # W-10 (D-119): 没有补录目标是有意的(RIO ×5 这类只判不入库), 不是配错 —— 说明白, 不抛错。
        return {"note_id": note_id, "resolved": False, "ingest": None,
                "note": (f"{tv_project_id} 只判不入库(映射里没有补录目标, D-119): 对不上的就留 unmatched, "
                         "不用处理。真要补进指纹库, 先 tv-map add … --ingest-target 指一个写作台项目")}
    row = next((r for r in store.tv_notes(client, tv_project_id)
                if str(r.get("note_id")) == note_id), None)
    if row is None:
        raise ValueError(f"TV 里没有 {note_id!r}(被删了?)")
    note = tvlink.Note.from_row(row)
    if not note.body:
        raise ValueError(f"{note_id} 在 TV 里没有正文, 补不了")

    owner = _tv_owner(client, target["project_id"])
    report = ingest_published(client, target["project_id"],
                              [{"title": note.title, "body": note.body}],
                              user_id=owner, source=f"tv:{tv_project_id}")
    written = report.get("written") or []
    version_id = written[0]["version_id"] if written else None
    if not version_id:
        # 指纹库已有同正文(skipped_already_fingerprinted)或半途失败: 都不能把对照
        # 记成 ingested, 如实回报, 对照不动。
        return {"note_id": note_id, "resolved": False, "ingest": report,
                "note": ("没建成版本: " + (report.get("identity_error")
                                          or report.get("fingerprint_error")
                                          or "指纹库里已有同正文的稿子(对照没动)"))}
    item_id = None
    try:
        item_id = (store.items_for_versions(client, target["project_id"], [version_id])
                   .get(version_id) or {}).get("item_id")
    except Exception:                        # noqa: BLE001
        logger.exception("tv_resolve: 取 item_id 失败, 对照先不带 item_id")
    candidates = [{"note": "人工判定: 对不上, 按正文补录(tv-resolve --ingest)"}]
    candidates += list(link.get("candidates") or [])
    store.tv_links_upsert(client, [{
        "note_id": note_id, "tv_project_id": tv_project_id,
        "project_id": target["project_id"], "version_id": version_id, "item_id": item_id,
        "match_kind": "ingested", "score": None, "lag_days": None,
        "candidates": candidates, "updated_at": store.iso_now()}])
    return {"note_id": note_id, "resolved": True, "version_id": version_id,
            "item_id": item_id, "project_id": target["project_id"], "ingest": report,
            "note": f"{note_id} 按对不上补录: 版本 {version_id[:8]}, 对照改成 ingested"
                    + ("" if report.get("fingerprinted") else
                       " —— ⚠️ 指纹没写上, 跑 backfill --project " + target["project_id"])}


# 哪些对照可以写回 TV。ingested 排除: 版本是从这条笔记复制来的, 不是它的来源。
_TV_BACKFILL_KINDS = frozenset({"body_exact", "title_exact", "fuzzy", "edited", "tv_lineage"})
# 每晚给"以前对上、还没算过改动差异"的笔记补算多少条(013 上线后第一晚要补几百条, 之后每晚只有新对上的)
TV_SYNC_DIFF_BACKFILL = 500
# W-34(10-27): 按内容判的对照。tv_lineage 是 TV 自己说的、ingested 是从这条笔记复制的 —— 这两种 TV 正文换了也不重对。
_CONTENT_KINDS = frozenset({"body_exact", "title_exact", "fuzzy", "edited"})
# 每个 TV 项目每晚最多挪几条"整篇换了"的对照; 超了**一条都不挪**、差异先隔离、退出码 1, 等人看。BJS 那次是 14 条一起换
# (运营把一张表挪作他用), 若自动挪, 它们会被补录进儿童藻油项目, 而 D-129 要的是成人藻油 —— 成批的换由人定。
# 环境变量 TV_SYNC_RELINK_MAX 覆盖(人看过清单后手动放行; 0 = 只报不挪)。
TV_SYNC_RELINK_MAX = 5
# W-37(10-28): 飞书里删了的篇缺席多久才把它的差异降回旧口径。TV 每天完整同步一次: 真删了的, 第二晚起缺口就过一天半;
# 两次同步重叠 / 某次写库写一半, 活着的篇只会短暂进视图(不到一天), 不降 —— 降了它回来时 computed_at 是旧的(同一版不推),
# 中间若有人确认过摘要, 它就再也进不了摘要。
TV_VANISHED_GRACE = timedelta(hours=36)
# 人定过的对照不动: candidates 里带 D-xxx 决策号或"人工判定"的备注。不认 W-xx —— W-34 自己写的备注不能把链接钉死。
_PIN_RE = re.compile(r"(?<![A-Za-z])D-\d{3}(?!\d)|人工判定")


def _relink_max() -> int:
    raw = (os.environ.get("TV_SYNC_RELINK_MAX") or "").strip()
    if not raw:
        return TV_SYNC_RELINK_MAX
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("TV_SYNC_RELINK_MAX=%r 不是整数, 按默认 %d", raw, TV_SYNC_RELINK_MAX)
        return TV_SYNC_RELINK_MAX


def _pinned(link: dict) -> bool:
    return link.get("match_kind") == "ingested" or any(
        isinstance(e, dict) and _PIN_RE.search(str(e.get("note") or "")) for e in (link.get("candidates") or []))


def _tv_text_state(prev: dict, n: tvlink.Note, fp_now: str, known: dict, linked_text: dict) -> str:
    """一条有版本的对照, TV 那边的正文现在怎样(W-34)。调用方已挡掉指纹没变的和没正文的。
    unverifiable: 连着的那一版取不到 —— 不判、不记指纹, 明晚再核(不能在取不到的那晚把"整篇换了"漏掉);
    replaced: 整篇换了(tvlink.still_same 不认); seed: 017 之前的对照第一次记指纹; edited: 改过字, 还是那一篇。
    只改了标题(指纹的正文那一半没变)直接算 edited: 写作台那版只有标题时, 改一两个字会被 still_same 判成换了一篇(review on aw#128)。"""
    if tvlink.body_unchanged(prev.get("tv_text_fp"), fp_now):
        return "edited"
    v = known.get(str(prev["version_id"])) or linked_text.get(str(prev["version_id"]))
    if v is None:
        return "unverifiable"
    if not tvlink.still_same(n, v):
        return "replaced"
    return "seed" if prev.get("tv_text_fp") is None else "edited"


def _linked_versions_outside_index(client, existing: dict, notes: list, fp_now: dict, known: dict,
                                   *, rematch: bool = False) -> dict:
    """指纹变了(或 --rematch)、连着的那一版又不在对照索引里(不是 best 的旧版 / 别处的版本)的, 按项目去库里取标题和正文,
    好判换没换。取不到的就没有 —— 那条记 unverifiable、明晚再核, 不当成"没换"。10-27 生产库 0 条, 留着兜底。"""
    want: dict[str, set] = {}
    for n in notes:
        l = existing.get(n.note_id)
        if (l and l.get("version_id") and l.get("project_id")
                and (rematch or l.get("tv_text_fp") != fp_now[n.note_id])
                and not _pinned(l) and str(l["version_id"]) not in known):
            want.setdefault(str(l["project_id"]), set()).add(str(l["version_id"]))
    out: dict[str, tvlink.Version] = {}
    for pid, ids in want.items():
        try:
            got = store.versions_in_project(client, pid, sorted(ids))
        except Exception:                               # noqa: BLE001
            logger.exception("tv_sync: 取对照索引外的旧版失败 (project=%s), 这几条明晚再核", pid)
            continue
        for vid, r in got.items():
            out[vid] = tvlink.Version(version_id=vid, project_id=pid, title=(r.get("title") or "").strip(),
                                      body=(r.get("body") or "").strip(), created_at=None)
    return out


def _diff_stamp(link: dict, diffed: dict[str, dict]) -> dict:
    """差异行的 computed_at: 新算的、或者换了一版的, 记现在(进下一份摘要); 同一版重算(--rematch / 口径过时)不带这一列,
    upsert 就留着原来的时间 —— 不然一次 rematch 会把早就看过的整批差异推过水位、每周 200 篇地重放。
    "同一版"按差异行自己记的 version_id 比, 不按旧对照: 对照已经改过、差异没跟上的(那晚写差异失败), 换的那版要进摘要。"""
    old = diffed.get(link["note_id"])
    if old is not None and str(old.get("version_id") or "") == str(link.get("version_id") or ""):
        return {}
    return {"computed_at": store.iso_now()}


def _diffable(link: dict) -> bool:
    """这一对能不能算"人在发布前改了什么"。⚠️ 写作台那版晚于发布的(lag_days < 0)不算(10-20): 首晚 440 条差异里
    212 条是发出去 6~7 天后才存进写作台的那版 —— 是把发出去的帖抄回来, 不是人改写作台的稿, 拿它们教写手就教反了。
    desk_gap 和 TV 的判据 C 早就这样判"接住"(lag_days ≥ 0 或没有)。"""
    return bool(link.get("version_id")) and link.get("match_kind") in _TV_BACKFILL_KINDS and (
        link.get("lag_days") is None or link["lag_days"] >= 0)


def _tv_edit_diffs(client, tv_project_id: str, links: list[dict], existing: dict[str, dict],
                   notes_by_id: dict, known: dict, *, dry_run: bool,
                   text_changed: frozenset | set = frozenset(), blocked: frozenset | set = frozenset()) -> dict:
    """对上的每一对算一条"人在发布前改了什么"(tvlink.edit_diff), 写 tv_edit_diffs(D-120 / W-23)。

    本次新对上的全算; 以前对上、还没有差异行或口径过时(diff_rev < tvlink.DIFF_REV)的补算(每晚最多
    TV_SYNC_DIFF_BACKFILL 条; 同一版重算不推 computed_at, 见 _diff_stamp)。只算真对上的
    (_TV_BACKFILL_KINDS, 不含 ingested —— 那一版就是从这条笔记复制的, 没有改动可言); 那一版不在对照索引里
    (tv_lineage 指到别处的版本)就跳过。**不抛**: 这是附加信息, 算不了也不能让对照本身失败。

    W-34(10-27): ``text_changed`` 是 TV 改过字、还是同一篇的 —— 差异按现在的正文重算(同一版, 不推 computed_at);
    ``blocked`` 是 TV 整篇换了、这晚没挪成的(超上限 / TV 的血缘没清掉 / tv_lineage) —— 差异隔离(降回旧口径),
    不算、不进摘要, 等对照挪好了再按新的那版算。"""
    try:
        diffed = store.tv_edit_diff_revs(client, tv_project_id) if not dry_run else {}
    except Exception as exc:                            # noqa: BLE001 — 013 没跑 / 库错: 这晚不算
        # 记日志、带回 error; cli 的 tv-sync 据它退出非零(codex review on aw#107), 对照照写
        logger.exception("tv_sync: 读已算过的改动差异失败 (tv=%s) —— 这晚不算差异", tv_project_id)
        return {"written": 0, "error": f"{type(exc).__name__}: {exc}"[:200]}
    # 每条笔记看它**现在**的对照(codex review on aw#113): 本次处理过的(新对上 / --rematch 重对)以本次为准,
    # 旧对照不再进补算 —— 不然重对到"发布后才存"的那版时, 被替掉的旧对照照样按当前口径算一遍、进摘要。
    current = {**existing, **{l["note_id"]: l for l in links}}
    # lag 是空的、但两边时间现在都有了的, 补上并写回对照(codex review on aw#113): 以前的对照有的没记 lag
    # (那时笔记还没有发布时间, 或者是老代码对上的), _diffable 把空当"不知道、放行", 抄回来的那版就混进差异。
    # W-38 复审(10-29): lag 以前按 UTC 日期减, 而 TV 的 publish_time 是北京那天的 0 点 —— 北京当天写、当天发的记成了 −1
    # (当成发了之后才存的, 不算差异、不算接住)。tvlink.lag_days 改成北京日历日之后, 存着 < 0、按现在的算法 ≥ 0 的也改回来;
    # 别的(差一天但同号的)不动, 判据只看正负。
    lag_fill: dict[str, int] = {}
    for nid, l in current.items():
        stored = l.get("lag_days")
        if (stored is None or stored < 0) and l.get("version_id") and l.get("match_kind") in _TV_BACKFILL_KINDS:
            n, v = notes_by_id.get(nid), known.get(str(l["version_id"]))
            lag = tvlink.lag_days(n, v) if n is not None and v is not None else None
            if lag is not None and (stored is None or lag >= 0):
                lag_fill[nid] = lag
                current[nid] = dict(l, lag_days=lag)
    # 要(重)算的: 还没有差异行、口径过时、或差异行记的那版和现在的对照不是同一版(重对之后那晚写差异失败了)
    stale = {nid for nid, d in diffed.items()
             if d["rev"] < tvlink.DIFF_REV or (nid in current and current[nid].get("version_id")
                                               and str(d.get("version_id") or "") != str(current[nid]["version_id"]))}
    stale |= set(text_changed) & set(diffed)            # TV 改过字(W-34): 同一版也按现在的正文重算

    def _ok(nid: str) -> bool:
        return _diffable(current[nid]) and nid not in blocked and _draft_has_body(current[nid])

    def _draft_has_body(link: dict) -> bool:
        # 写作台那版只有标题(W-36, 10-27): 没有正文可比, 算出来是"整篇都是新加的" —— HATHERINE 59 条 title_exact 连的就是
        # 这种稿, 13 条差异按当前口径进了摘要。这种对照不算差异, 已算过的降回旧口径。那版不在索引里的照旧(这里看不到)。
        v = known.get(str(link.get("version_id") or ""))
        return v is None or bool(fp.normalize(tvlink.strip_hashtags(v.body)))

    def _has_body(nid: str) -> bool:
        # 这晚 TV 读到的正文是空的(只剩标题 / 只剩话题): 不拿空正文算差异 —— 会算成"整篇删了"(review on aw#128)。
        # 只管算不算, 不管降不降: 已有的差异照旧, 有正文了再算。
        n = notes_by_id.get(nid)
        return n is not None and bool(n.body)

    seen = {l["note_id"] for l in links}
    fresh = {nid: current[nid] for nid in seen if _ok(nid) and _has_body(nid)}
    # 先筛掉算不了的(笔记不在这次读到的范围 / 那一版不在对照索引里), 再截到每晚上限: 不然一堆永远算不了的
    # 老对照占满名额, 后面能算的一直轮不到。TV 改过字的排最前(W-34): 那几条的旧差异是错的。
    eligible = sorted((dict(r, note_id=nid) for nid, r in current.items()
                       if nid not in seen and (nid not in diffed or nid in stale) and _ok(nid)
                       and _has_body(nid) and str(r["version_id"]) in known),
                      key=lambda r: r["note_id"] not in text_changed)
    backlog = eligible[:TV_SYNC_DIFF_BACKFILL]
    # 已经按当前口径算过、但对照现在不该算了的(重对到抄回的那版 / 补上的 lag < 0 / W-34 整篇换了还没挪成的): 降回旧口径,
    # 摘要就读不到它; 不删行(删数据由人在 SQL 编辑器里做)。
    demote = sorted(nid for nid, d in diffed.items()
                    if d["rev"] >= tvlink.DIFF_REV and nid in current and not _ok(nid))
    # 飞书里删了 / 挪走的篇(TV 的 v_notes_vanished, W-37, 10-28): 016 之后这晚读不到它们, 差异既不重算、上面也降不到,
    # 一直留在摘要里 —— 10-28 HATHERINE 一篇已删的帖子还是待读摘要里的一条"人改了什么"。缺席满 TV_VANISHED_GRACE 的,
    # 按当前口径算过的降回旧口径。只查这晚没读到的那几条(`--since` 窗口外的也没读到, 但不在视图里, 不会被降);
    # 查不了就这晚不降、报出来。降了之后那篇又读到了, 按"口径过时"重算(那一版还在对照索引里才算得了)。
    unread = sorted(nid for nid, d in diffed.items()
                    if d["rev"] >= tvlink.DIFF_REV and nid not in notes_by_id and nid not in demote)
    vanished: set[str] = set()
    vanished_error = None
    if unread:
        try:
            vanished = store.tv_long_vanished_note_ids(client, unread, min_gap=TV_VANISHED_GRACE)
        except Exception as exc:                        # noqa: BLE001
            logger.exception("tv_sync: 查 v_notes_vanished 失败 (tv=%s) —— 飞书删了的篇这晚不降", tv_project_id)
            vanished_error = f"{type(exc).__name__}: {exc}"[:200]
        demote = sorted(set(demote) | vanished)
    brands: dict[str, str] = {}
    rows = []
    for l in list(fresh.values()) + backlog:
        n, v = notes_by_id.get(l["note_id"]), known.get(str(l["version_id"]))
        if n is None or v is None:
            continue
        if v.project_id not in brands:
            try:
                brands[v.project_id] = (store.project_row(client, v.project_id) or {}).get("brand") or ""
            except Exception:                           # noqa: BLE001
                brands[v.project_id] = ""
        d = tvlink.edit_diff(v.title, v.body, n.title, n.body, brand=brands[v.project_id])
        # project_id = 那一版所在的写作台项目(写稿的那个), 不是 TV 项目的补录目标(W-39, 10-30): 一个 TV 项目对着几个写作台
        # 项目时, 改法回到写这篇稿的那个项目的摘要里; 按 TV 项目归会把 8～10 个方向压成一个、或同一条改法在几个项目里各出一次
        rows.append({"note_id": n.note_id, "tv_project_id": tv_project_id, "project_id": v.project_id,
                     "version_id": v.version_id, "match_kind": l["match_kind"],
                     "publish_time": n.publish_time.isoformat() if n.publish_time else None,
                     "draft_title": v.title[:120], "diff_rev": tvlink.DIFF_REV, **{k: d[k] for k in (
                         "changed", "title_changed", "published_title", "draft_chars", "published_chars",
                         "removed_count", "added_count", "edited_count", "removed_sentences", "added_sentences",
                         "edited_sentences", "brand_draft", "brand_published")},
                     **_diff_stamp(l, diffed)})
    out = {"computed": len(rows), "changed": sum(1 for r in rows if r["changed"]),
           "backfilled": sum(1 for r in rows if r["note_id"] not in fresh),
           "recomputed": sum(1 for r in rows if r["note_id"] in stale), "written": 0,
           "lag_filled": 0, "demoted": 0,
           "text_recomputed": sum(1 for r in rows if r["note_id"] in text_changed),
           "quarantined": sum(1 for nid in demote if nid in blocked),
           "vanished": len(vanished),
           # 改过字、该重算却被每晚上限挤掉的: tv_sync 这晚不给它们记指纹, 明晚再算
           "text_left": sorted({r["note_id"] for r in eligible[TV_SYNC_DIFF_BACKFILL:]} & set(text_changed))}
    if vanished_error:
        out["vanished_error"] = vanished_error
    if dry_run:
        out.update(lag_filled=len(lag_fill), demoted=len(demote))      # dry-run 报"会做多少"
        return out
    # 三笔各写各的、各记各的错: 先降口径(这一步才让该藏的行出摘要), 再补 lag, 最后写差异 —— 一笔失败不拖住另两笔
    # (降口径和补 lag 只看内存里的 current, 不依赖差异写没写成)。
    for key, err_key, fn, arg, what in (
            ("demoted", "demote_error", store.tv_edit_diffs_demote, demote, "差异降回旧口径"),
            ("lag_filled", "lag_error", store.tv_links_set_lag, lag_fill, "对照补 lag"),
            ("written", "error", store.tv_edit_diffs_upsert, rows, "改动差异写库")):
        if not arg:
            continue
        try:
            out[key] = fn(client, arg)
        except Exception as exc:                        # noqa: BLE001
            logger.exception("tv_sync: %s失败 (tv=%s)", what, tv_project_id)
            out[key] = getattr(exc, "done", 0)          # 分块的: 失败前已提交的那几块照实报
            out[err_key] = f"{type(exc).__name__}: {exc}"[:200]
    return out


def _backfillable(link: dict) -> bool:
    return bool(link.get("version_id")) and link.get("match_kind") in _TV_BACKFILL_KINDS


# ── 查重底稿转正(W-38, 2026-10-29) ────────────────────────────────────────────────────────
# 底稿按 (写作台项目, text_fp) 归成一篇; 转成版本时的 version_id 由这两样定出来(uuid5), 同一篇哪晚转都是同一个 id ——
# 一晚建了版本、写指纹 / 销牌半途断了, 第二晚认得出是它, 只补没做完的, 不再建一份。
_SNAPSHOT_NS = uuid.UUID("5d0a2f6e-38c1-4b7a-9e2d-0c8f1a6b3e38")
SNAPSHOT_NOTE = "W-38: 写作台查重时记下的底稿(没存过档), 按内容对上, 转成了版本"
SNAPSHOT_RETAIN_DAYS = 60           # 底稿留多久(存的是稿子全文; tv-sync 只用近 SNAPSHOT_MAX_AGE_DAYS 天的)


def prune_snapshots(client) -> int | None:
    """底稿留够 SNAPSHOT_RETAIN_DAYS 天就删(存的是稿子全文, 多半没发出去; tv-sync 只用近 SNAPSHOT_MAX_AGE_DAYS 天的)。
    tv-sync --all 跑完调一次。回删了几行; 018 没跑回 None。别的错误上抛(CLI 记退出码 1)。"""
    try:
        return store.delete_old_snapshots(client, before=store.iso_ago(SNAPSHOT_RETAIN_DAYS))
    except Exception as exc:                            # noqa: BLE001
        if store.snapshots_table_missing(exc):
            return None
        raise


def _snapshot_version_id(project_id: str, text_fp: str) -> str:
    return str(uuid.uuid5(_SNAPSHOT_NS, f"w38:{project_id}:{text_fp}"))


def _snapshot_groups(rows: list[dict]) -> dict[str, dict]:
    """底稿行 → 按 (项目, text_fp) 归成的一篇, 键是转正后的 version_id。同一篇查了几次: 时刻、check_id、飞轮臂取第一次的
    (那时这篇就已经写出来了); 原文取最晚那次的(text_fp 一样, 只差标点空白 / 话题); angle_key 取最晚带了的那次, 写手跟着
    它走(销牌按发牌的人), 销牌只认那一次查重之前抽的(``key_checked_at``: 先没带 key 查过一次、抽了牌再带着 key 查的,
    按第一次的时刻就销不到那张牌, codex on aw#132), 都没带就取最晚那次的写手。``ok``: 至少有一次查重没被打回(pass / warn) —— 只被打回过的(撞了
    库里的稿, 多半是照抄发出去的帖)不转正(W-38 复审)。"""
    groups: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: (str(r.get("checked_at") or ""), str(r.get("id") or ""))):
        if not (r.get("project_id") and r.get("text_fp")):
            continue
        vid = _snapshot_version_id(str(r["project_id"]), str(r["text_fp"]))
        g = groups.get(vid)
        if g is None:
            g = groups[vid] = {"version_id": vid, "project_id": str(r["project_id"]), "ids": [],
                               "first_checked_at": r.get("checked_at"), "check_id": r.get("check_id"),
                               "flywheel_arm": r.get("flywheel_arm"),
                               "flywheel_arm_source": r.get("flywheel_arm_source"),
                               "angle_key": None, "key_user": None, "key_checked_at": None, "user_id": None,
                               "ok": False, "notes": []}
        g["ids"].append(str(r["id"]))
        g["title"], g["body"] = r.get("title") or "", r.get("body") or ""
        g["user_id"] = r.get("user_id") or g["user_id"]
        g["ok"] = g["ok"] or r.get("status") in ("pass", "warn", None)
        if r.get("angle_key"):
            g["angle_key"], g["key_user"] = str(r["angle_key"]), r.get("user_id") or g["key_user"]
            g["key_checked_at"] = r.get("checked_at")
    for g in groups.values():
        g["writer"] = g.pop("key_user") or g["user_id"]
    return groups


def _snapshot_fp_payload(project_id: str, user_id: str, rows: list[dict]) -> list[dict]:
    """转正版本的指纹行, 同 _ingest_published_unlocked 的形状(不过闸 —— 帖已经发出去了), 但带着底稿的 angle_key。
    ``rows`` 每行 {version_id, title, body, writer, angle_key}: 底稿那一版一行; 发出去的帖跟底稿不一样的(改过的), 帖的
    原文也记一行在同一个 version_id 下 —— 以前补录会把发出去的文字记进指纹库, 转正了也不能少(W-38 复审)。"""
    out: list[dict] = []
    can_embed = dedup.embeddings_available()
    for start in range(0, len(rows), INGEST_EMBED_CHUNK):
        chunk = rows[start:start + INGEST_EMBED_CHUNK]
        vecs = dedup.embed_texts([g["title"] for g in chunk]) if can_embed else None
        for i, g in enumerate(chunk):
            vec = vecs[i] if vecs and i < len(vecs) and vecs[i] else None
            out.append({"project_id": project_id, "user_id": g.get("writer") or user_id,
                        "version_id": g["version_id"], "title": g["title"],
                        "opening": fp.opening_of(g["body"]), "title_embedding": vec,
                        "embedding_model": dedup.EMBEDDING_MODEL if vec else None,
                        "opening_hash": fp.opening_hash(g["body"]), "ngram_hashes": fp.ngram_hashes(g["body"]),
                        "angle_key": g.get("angle_key")})
    return out


def _fp_key(body: str) -> tuple:
    """指纹行按什么算同一行: 开头哈希 + 四字串 sketch(同 existing_fingerprint_sketches / 补录的去重)。"""
    return (fp.opening_hash(body) or "", tuple(sorted(fp.ngram_hashes(body))))


# W-40(10-30): 哪几种对照的帖, 发出去的文字要记在连着的那一版下。ingested 不用(版本就是从这篇帖抄进来的, 文字本来就在库里)。
# body_exact 也要: 它只是正文前 40 字一样, 后面改过的照样有。
_PUBLISHED_FP_KINDS = frozenset({"body_exact", "title_exact", "fuzzy", "edited", "tv_lineage"})


def _fingerprint_published(client, pairs: list[tuple], *, known: dict, owners: dict, holder: str,
                           dry_run: bool = False) -> dict:
    """W-40: 帖对上了写作台的哪一版, 发出去的文字就记一行指纹在那一版下 —— 查重比的是写过的稿, 写手拿去改过再发的那一版
    原来不在库里, 下一篇照着发出去的帖写, 查重认不出(补录的帖、W-38 转正的帖早就是这样记的, 只有对上存档版本的漏了)。

    ``pairs``: ``[(note, project_id, version_id)]``, 只放今晚核过、按现在的正文还连着这一版的。按文字幂等, 正文和标题一起算
    (正文去标点空白后比四字串 sketch, 标题去标点空白后比): 那一版下已经有正文一样、标题也一样的行(底稿 / 存档那一行, 或者前一晚记过的帖)不再记; 帖和那一版只差话题标签的也算一样
    (帖读进来就剥了话题)。只改了标题的照样记一行(查重单看标题也会判死, 运营改过的标题不在库里就拦不住照着它起的标题)。
    行的时刻、写手、angle_key 跟那一版最早那行走 —— 是那一版的另一种写法, 不是今晚新写的稿(TV 的发帖对账按时间窗数稿子,
    记成今晚就多数一篇)。那一版一行指纹都没有的(改稿时 replaces_version_id 把整个 item 的指纹撤了重写、老的没指纹的版本、
    W-38 转了一半的), 时刻取那一版自己建的时刻、写手记项目 owner, 照样记 —— 帖还在外面, 不能因为那一版的行没了就漏掉。
    拿项目写锁(同补录 / 转正), 锁里重读一遍再写, 按 INGEST_EMBED_CHUNK 分批插(同补录)。
    回 ``{"checked", "to_write", "written", "no_rows", "error"}``(``no_rows``: 那一版没有指纹行、按它自己的时刻记的篇数);
    ``dry_run`` 只数不写。"""
    out = {"checked": len(pairs), "to_write": 0, "written": 0, "no_rows": 0, "error": None}
    by_pid: dict[str, list[tuple]] = {}
    for n, pid, vid in pairs:
        by_pid.setdefault(str(pid), []).append((n, str(vid)))

    def _text(body: str) -> tuple:
        # 正文按四字串 sketch 比(去标点空白之后的全文), 不带开头哈希: 开头哈希取的是原文前 25 字再规范化, 标点 / 空格一改窗口就挪,
        # 只改了标点的帖会被当成新文字再记一行
        return tuple(sorted(fp.ngram_hashes(body)))

    def _rows(items: list[tuple], meta: dict, versions: dict, times: dict) -> tuple[list[dict], int]:
        rows, no_rows, seen = [], 0, {}
        for n, vid in items:
            k = _text(n.body)
            if not k:
                continue
            m = meta.get(vid) or {"pairs": set(), "created_at": times.get(vid), "user_id": None, "angle_key": None}
            have = seen.get(vid)
            if have is None:
                have = seen[vid] = {(hk[1], fp.normalize(ht)) for hk, ht in m["pairs"]}
                v = versions.get(vid)
                if v is not None:
                    # 只差话题标签的帖算已经在库里, 前提是那一版自己的文字(标题 + 带话题的正文)真有一行 —— 那一版的行被撤光了的,
                    # 和它一模一样的帖也得记(codex on aw#133)
                    vt, vk = fp.normalize(v.title), _text(v.body)
                    if (vk, vt) in have:
                        have.add((_text(tvlink.strip_hashtags(v.body)), vt))
            t = fp.normalize(n.title)
            # 帖没标题的只比正文(不为一个空标题另记一行)
            if any(hk == k and (not t or ht == t) for hk, ht in have):
                continue
            have.add((k, t))
            no_rows += vid not in meta
            rows.append({"version_id": vid, "title": n.title, "body": n.body, "writer": m.get("user_id"),
                         "angle_key": m.get("angle_key"), "created_at": m.get("created_at")})
        return rows, no_rows

    for pid, items in by_pid.items():
        try:
            vids = sorted({vid for _, vid in items})
            meta = store.fingerprint_meta(client, pid, vids)
            versions = {vid: known[vid] for vid in vids if vid in known}
            outside = sorted(set(vids) - set(known))
            if outside:
                # 索引外的旧版(不是 best 的那版): 取正文和标题只为了认出"和那一版一样 / 只差话题标签"的
                versions.update({vid: tvlink.Version(version_id=vid, project_id=pid, title=(r.get("title") or "").strip(),
                                                     body=(r.get("body") or "").strip(), created_at=None)
                                 for vid, r in store.versions_in_project(client, pid, outside).items()})
            times: dict = {}

            def _fill_times(meta: dict) -> None:
                # 没有指纹行的版本按它自己建的时刻记; 锁里重读之后才没了行的(刚被改稿撤掉)也要补上(codex on aw#133)
                bare = sorted(set(vids) - set(meta) - set(times))
                if not bare:
                    return
                times.update({vid: versions[vid].created_at.isoformat() for vid in bare
                              if vid in versions and versions[vid].created_at})
                missing = [vid for vid in bare if vid not in times]
                if missing:
                    times.update(store.version_times(client, missing))

            _fill_times(meta)
            todo, no_rows = _rows(items, meta, versions, times)
            if not todo:
                continue
            if dry_run:
                out["to_write"] += len(todo)
                out["no_rows"] += no_rows
                continue
            with _project_write_lock(client, pid, holder):
                # 锁里重读: 锁外读完到这里, 别的 tv-sync / 转正可能刚把同样的文字记上
                meta = store.fingerprint_meta(client, pid, vids)
                _fill_times(meta)
                todo, no_rows = _rows(items, meta, versions, times)
                out["to_write"] += len(todo)
                out["no_rows"] += no_rows
                payload = _snapshot_fp_payload(pid, owners.get(pid), todo)
                for p, r in zip(payload, todo):
                    p["created_at"] = r["created_at"] or store.iso_now()
                for start in range(0, len(payload), INGEST_EMBED_CHUNK):
                    out["written"] += store.write_fingerprints(client, payload[start:start + INGEST_EMBED_CHUNK])
        except Exception as exc:                        # noqa: BLE001 — 对照已经写了; 没记上的明晚按文字再补
            logger.exception("tv_sync: 帖的文字记进指纹库失败 (project=%s)", pid)
            out["error"] = out["error"] or f"{type(exc).__name__}: {exc}"[:300]
    return out


def _promote_snapshot_groups(client, project_id: str, groups: list[dict], *, owner: str, holder: str) -> dict:
    """把这几篇底稿转成写作台的版本(W-38), 和存过档的一样: 建 batch / item / version(时刻 = 第一次查重)、写指纹(不过闸)、
    销牌(只销写手自己在查重之前抽的那张)、底稿行记上转正。同一晚同一次查重(check_id)的几篇一个 batch(一轮的帖分几晚发,
    就分几个 batch, params.check_id 一样)。拿项目写锁(同 commit / 补录)。

    回 ``{"items": {version_id: item_id}, "minted", "already", "fingerprinted", "consumed", "marked", "error",
    "superseded"}``: ``items`` 里有的才是真建成的版本(可以连对照); ``error`` 非空时其余的这晚没转成、明晚再来;
    ``superseded``: 拿到锁时底稿已经被存档记走了的, 这晚不转; ``fingerprinted_meanwhile``: 拿到锁时帖的文字已经进了
    指纹库的(刚被补录), 这晚不转。版本建成之后的几步(指纹 / 销牌 / 标记)断了不撤版本: 之后
    tv_sync 认出它已经有版本(同一个 id), 只补没做完的。``wait_for_post`` 的(补的这晚帖还没连上)只补指纹 / 销牌, 不记转正。"""
    out = {"items": {}, "minted": 0, "already": 0, "fingerprinted": 0, "consumed": 0, "marked": 0, "error": None,
           "superseded": [], "fingerprinted_meanwhile": []}
    with _project_write_lock(client, project_id, holder):
        # 拿到锁再看一眼底稿还是不是没转正(codex on aw#132): tv-sync 读底稿到这里之间, commit_drafts 可能刚把同样文字的稿子
        # 存了档、把底稿记到了那一版(_supersede_snapshots, 同一把锁) —— 再按底稿建一份, 同一篇就有了两个版本, 牌销给了存档
        # 那一版、对照却连着这一版。这几篇这晚不转, 明晚主索引对存了档的那一版。
        state = store.snapshot_states(client, [i for g in groups for i in g["ids"]])
        out["superseded"] = sorted({g["version_id"] for g in groups
                                    if any(state.get(i) not in (None, g["version_id"]) for i in g["ids"])})
        groups = [g for g in groups if g["version_id"] not in out["superseded"]]
        vids = [g["version_id"] for g in groups]
        if not groups:
            return out
        have = store.items_for_versions(client, project_id, vids)
        # 要新建版本的, 拿到锁再核一次帖的文字是不是已经在指纹库里了(codex on aw#132): 锁外核过之后、拿到锁之前, 别的补录
        # 可能刚把同一篇帖记成了副本 —— 再转正就是同一篇两个版本。这几篇这晚不转, 明晚照锁外那一关走(补录那条路)。
        # 已经有版本的(补齐)不核: 帖的文字可能正是前一晚转正时记在它自己名下的。
        fresh = [g for g in groups if g["version_id"] not in have]
        posted = [(g, fp.opening_hash(b), tuple(sorted(fp.ngram_hashes(b)))) for g in fresh for _, b in g.get("notes") or ()]
        if posted:
            seen = store.existing_fingerprint_sketches(client, project_id, [h for _, h, _ in posted if h])
            out["fingerprinted_meanwhile"] = sorted({g["version_id"] for g, h, sk in posted if h and sk in seen.get(h, ())})
            groups = [g for g in groups if g["version_id"] not in out["fingerprinted_meanwhile"]]
            if not groups:
                return out
        out["items"].update({vid: (hit or {}).get("item_id") for vid, hit in have.items()})
        out["already"] = len(have)
        by_check: dict[str, list[dict]] = {}
        for g in groups:
            if g["version_id"] not in have:
                by_check.setdefault(str(g.get("check_id") or g["version_id"]), []).append(g)
        for check_id, gs in by_check.items():
            first = gs[0]
            params = {"source": "deskcore", "via": "snapshot", "check_id": check_id}
            if first.get("flywheel_arm") in FLYWHEEL_ARMS:
                params.update(flywheel_arm=first["flywheel_arm"],
                              flywheel_arm_source=first.get("flywheel_arm_source") or "recomputed")
            minted = store.mint_draft_identity(
                client, project_id, str(first.get("writer") or owner), "",
                [{"version_id": g["version_id"], "title": g["title"], "body": g["body"], "keywords": [],
                  "created_at": g.get("first_checked_at")} for g in gs],
                batch_params=params)
            out["items"].update(minted.get("versions") or {})
            out["minted"] += len(minted.get("versions") or {})
            if minted.get("error") and not out["error"]:
                out["error"] = f"建版本: {minted['error']}"[:300]
        done = [g for g in groups if g["version_id"] in out["items"]]
        if not done:
            return out
        try:
            work = [g for g in done if not g.get("mark_only")]
            # 按文字补, 不按版本: 前几晚写了底稿那一行、发出去的帖那一行还没写的(帖那时还没连上), 这晚照样补上
            have_fp = store.fingerprint_keys(client, project_id, [g["version_id"] for g in work])
            rows: list[dict] = []
            for g in work:
                seen = set(have_fp.get(g["version_id"], ()))
                if _fp_key(g["body"]) not in seen:
                    rows.append(g)
                # 帖和底稿只差话题标签的不另记(帖读进来就剥了话题, 底稿原样存)
                seen |= {_fp_key(g["body"]), _fp_key(tvlink.strip_hashtags(g["body"]))}
                for title, body in g.get("notes") or ():
                    k = _fp_key(body)
                    if k[0] and k not in seen:
                        seen.add(k)
                        rows.append({**g, "title": title, "body": body})
            if rows:
                out["fingerprinted"] = store.write_fingerprints(client, _snapshot_fp_payload(project_id, owner, rows))
            consumed = store.angle_consumed_by(client, project_id, [g["version_id"] for g in work])
            for g in work:
                # B-20: 版本真建成了才销; 已经销给它的(前一晚半途断了)不再销第二张
                if g.get("angle_key") and g["version_id"] not in consumed:
                    if store.consume_own_angle(client, project_id, g["angle_key"], g["version_id"],
                                               user_id=g.get("writer"),
                                               drawn_before=g.get("key_checked_at") or g.get("first_checked_at")):
                        out["consumed"] += 1
            for g in done:
                if g.get("wait_for_post"):
                    continue                            # 帖还没连上: 先不记转正, 等连着它的那个 TV 项目补上帖的文字
                out["marked"] += store.mark_snapshots_promoted(client, g["ids"], version_id=g["version_id"],
                                                               note_id=g.get("note_id"))
        except Exception as exc:                        # noqa: BLE001 — 版本已在, 对照照连; 明晚补齐
            logger.exception("tv_sync: 底稿转正后写指纹 / 销牌 / 标记失败 (project=%s)", project_id)
            out["error"] = out["error"] or f"转正后续: {type(exc).__name__}: {exc}"[:300]
    return out


def tv_sync(client, tv_project_id: str, *, dry_run: bool = False,
            write_tv: bool = False, since: str | None = None,
            rematch: bool = False) -> dict:
    """把一个 TV 项目的笔记对到写作台的版本; 对不上的补录进指纹库。

    ── 为什么是它, 不是让运营抄 ID ──────────────────────────────────
    2026-09-17: TV 5966 条笔记里带写作台 lineage 的是 0 条, 原设计让运营把
    export_drafts 的六个 ID 列手抄进飞书, 三周零匹配。两边在同一个库里, 内容
    都是写作台产的, 按内容对就行 —— 运营只管写和发。

    ── 流程 ────────────────────────────────────────────────────────────
      1. tv_project_map 告诉我们这个 TV 项目对应哪些写作台项目(WTG 有 15 个
         方向)、其中哪一个是补录目标(ingest_target)。
      2. 拉 TV 的笔记(RPC 跨 schema) + 各写作台项目的定稿版本。
      3. tvlink.match_note 逐条判: body_exact / title_exact / fuzzy / ambiguous /
         unmatched。TV 自己已经带 source_autowriter_version_id 的直接记 tv_lineage。
      4. unmatched → ingest_published(剥掉话题标签, source="tv:<项目>", 不过闸,
         按全文幂等), 建成的 version_id 记回对照。
      5. 对照写进 tv_note_links(upsert, 按 note_id)。已经对上、TV 正文指纹没变的
         笔记跳过 —— 每天跑一次, 增量。指纹变了的(W-34, 10-27)核一下还是不是
         那一篇: 是就只记指纹、差异按现在的正文重算; 整篇换了就按内容重对, 挪之前
         先撤回 TV 里我们自己写的旧血缘(只在 --write-tv 下; 每晚每个 TV 项目最多
         TV_SYNC_RELINK_MAX 条, 超了一条不挪)。ingested 和人定过的不动。
      6. ``write_tv`` 才把对照写回 TV 的 source_autowriter_* 两列(只填 NULL 的
         行; 那是 TV 的列, 默认不碰)。**只回写真对上的**(body_exact / title_exact
         / fuzzy / tv_lineage); ``ingested`` 的不写 —— 那些版本是从这条笔记复制
         来的, 写回去因果倒置(见 _backfillable)。

    ``dry_run``: 全部算、一行不写(对照不写、指纹不写、TV 不写), 报表照出。
    """
    maps = [m for m in store.tv_project_map(client) if m["tv_project_id"] == tv_project_id]
    if not maps:
        raise ValueError(f"tv_project_map 里没有 {tv_project_id!r} —— 先跑 "
                         "`python -m deskcore.cli tv-map add --tv-project … --project … "
                         "--ingest-target`")
    target = next((m for m in maps if m.get("ingest_target")), None)
    owners = {m["project_id"]: _tv_owner(client, m["project_id"]) for m in maps}

    versions = [tvlink.Version.from_row(v) for m in maps
                for v in store.versions_for_linking(client, m["project_id"])]
    index = tvlink.VersionIndex(versions)
    known = {v.version_id: v for v in versions}
    project_of = {m["project_id"]: m for m in maps}
    notes = [tvlink.Note.from_row(r) for r in store.tv_notes(client, tv_project_id, since=since)]
    existing = store.tv_links(client, tv_project_id)
    notes_by_id = {n.note_id: n for n in notes}
    fp_now = {n.note_id: tvlink.text_fp(n) for n in notes}
    linked_text = _linked_versions_outside_index(client, existing, notes, fp_now, known, rematch=rematch)
    relink_max = _relink_max()

    now = store.iso_now()
    counts: dict[str, int] = {k: 0 for k in (
        "already_linked", "tv_lineage", "body_exact", "title_exact", "fuzzy", "edited",
        "ambiguous", "unmatched", "no_body")}
    links: list[dict] = []
    to_ingest: list[tvlink.Note] = []
    ambiguous_samples: list[dict] = []
    # W-34: 已对上的对照, TV 正文变了没有。这些数不进 counts(counts 加起来仍等于笔记数), 单独报。
    drift: dict = {"seeded": 0, "text_changed": 0, "kept_after_rematch": 0, "replaced": 0, "relinked": 0,
                   "deferred": 0, "blocked": 0, "unverifiable": 0, "no_body_kept": 0, "tv_cleared": 0,
                   "clear_missed": 0, "fp_written": 0, "fp_held": 0, "over_cap": 0, "relink_max": relink_max,
                   "clear_error": None, "fp_error": None, "samples": []}
    fp_set: dict[str, str] = {}          # 对照不重写、只记指纹的
    text_changed: set[str] = set()       # 改过字、还是那一篇: 差异按现在的正文重算
    blocked: set[str] = set()            # 整篇换了、这晚没挪成: 差异隔离
    w34_note: dict[str, list[dict]] = {}  # 挪走的对照, 新对照的 candidates 前面记一句为什么挪
    moved: list[tuple] = []              # (笔记, 新判定, 旧对照, 旧版本是不是我们自己写回 TV 的)

    def _link(n: tvlink.Note, kind: str, *, project_id=None, version_id=None,
              item_id=None, score=None, lag=None, candidates=None) -> dict:
        # tv_text_fp: 这一行是按现在的正文判的(W-34)。synced_to_tv_at 清空: 这一版还没写回过 TV(挪到新版本之后要再回填)。
        # 每行都带同样的键 —— PostgREST 批量写按键的并集、没带的补 NULL(假件 tests/fakes.py 同样如此)。
        return {"note_id": n.note_id, "tv_project_id": tv_project_id,
                "project_id": project_id, "version_id": version_id, "item_id": item_id,
                "match_kind": kind, "score": score, "lag_days": lag,
                "candidates": candidates or None, "updated_at": now,
                "tv_text_fp": fp_now[n.note_id], "synced_to_tv_at": None}

    def _decide(n: tvlink.Note, *, trust_tv: bool = True) -> tvlink.Match:
        """只判, 不写。tv_lineage / no_body 也包成 Match。``trust_tv=False``: TV 带的 lineage 是我们自己写回去的旧版,
        不当证据, 按内容对(否则 --rematch 和 W-34 的重对都会被它拽回旧版)。"""
        if trust_tv and n.tv_version_id:
            return tvlink.Match("tv_lineage")
        if not n.body:
            return tvlink.Match("no_body")
        return tvlink.match_note(n, index)

    def _apply(n: tvlink.Note, m: tvlink.Match, extra: list[dict] | None = None) -> None:
        """判定的副作用(计数、对照、补录名单)。``extra``: 挪走的对照前面记的那句(W-34)。"""
        extra = list(extra or [])
        if m.kind == "tv_lineage":
            # TV 自己带的 lineage。只在那个 version 真在我们库里时才写 version_id ——
            # tv_note_links.version_id 有外键, 一个陈旧/别处的 id 会让整个 upsert
            # 分块失败; 认不出的记在 candidates 里给人看, 不写外键列。
            counts["tv_lineage"] += 1
            tvid = str(n.tv_version_id)
            v = known.get(tvid)
            hit = ((v.project_id, v.item_id) if v else _resolve_any_version(client, project_of, tvid))
            links.append(_link(n, "tv_lineage",
                               project_id=hit[0] if hit else None,
                               version_id=tvid if hit else None,
                               item_id=hit[1] if hit else None,
                               # 那一版在对照索引里就照样算 lag(codex review on aw#113): 不算的话是 None, _diffable
                               # 当成"不知道、放行", 发了之后才存进写作台的那版就混进改动差异。索引外的旧版
                               # (_resolve_any_version 反查到的)这里拿不到时间, 但差异本来也只对索引里的版本算。
                               lag=tvlink.lag_days(n, v) if v else None,
                               candidates=extra + ([] if hit else [{"tv_version_id": tvid,
                                                                    "note": "TV 带的 version_id 不在对照的写作台项目里"}])))
            return
        if m.kind == "no_body":
            # 没正文: 对不了, 也不能补 —— 空正文建出来的身份没有开头哈希和四字串,
            # 对查重是隐形的, 却会被报成"已补录"(codex #81 P2)。记下来给人看。
            counts["no_body"] += 1
            links.append(_link(n, "unmatched", candidates=extra + [{"note": "TV 里没有正文, 无法对照也无法补录"}]))
            return
        counts[m.kind] += 1
        if m.version is not None:
            links.append(_link(n, m.kind, project_id=m.version.project_id,
                               version_id=m.version.version_id, item_id=m.version.item_id,
                               score=m.score, lag=m.lag_days, candidates=extra + list(m.candidates or [])))
        elif m.kind == "ambiguous":
            links.append(_link(n, "ambiguous", score=m.score, candidates=extra + list(m.candidates or [])))
            if len(ambiguous_samples) < 10:
                ambiguous_samples.append({"note_id": n.note_id, "title": n.title[:40],
                                          "candidates": m.candidates[:3]})
        else:
            to_ingest.append(n)                         # 补录 / W-10 那里再带上 w34_note

    def _sample(n: tvlink.Note, prev: dict, m: tvlink.Match | None, why: str, *, move: bool = False) -> None:
        # 挪的(和超上限没挪的)一条不漏: 放行之前人要看全清单(review on aw#128); 其余只是提示, 最多 20 条
        if not move and len(drift["samples"]) >= 20:
            return
        old_v = known.get(str(prev["version_id"])) or linked_text.get(str(prev["version_id"]))
        drift["samples"].append({
            "note_id": n.note_id, "old_kind": prev.get("match_kind"), "old_version": str(prev["version_id"]),
            "overlap": round(tvlink.overlap(n, old_v), 3) if old_v is not None else None,
            "new_kind": m.kind if m is not None else None,
            "new_version": str(m.version.version_id) if m is not None and m.version is not None else None,
            "why": why})

    for n in notes:
        prev = existing.get(n.note_id)
        if not (prev and prev.get("version_id")):
            _apply(n, _decide(n))
            continue
        # ── 已经对上过的(W-34, 10-27) ──
        # ingested 的对照是终态, --rematch 也不重对(codex #83 P1): 它的版本就是从这条笔记复制出来的, 重对只能对上它
        # 自己 —— 记成 body_exact 之后 _backfillable 放行, --write-tv 就把本要排除的因果倒置 lineage 全写回 TV 了。
        # 人定过的(candidates 里有 D-xxx / 人工判定)同样不动。
        if _pinned(prev):
            counts["already_linked"] += 1
            continue
        if not rematch and prev.get("tv_text_fp") == fp_now[n.note_id]:
            counts["already_linked"] += 1             # 上次核过, 字没变: 绝大多数对照每晚走这里
            continue
        if not n.body:
            # 飞书一时读空 / 正文被清了: 不能当成"整篇换了", 对照和指纹都不动, 有正文了再核(--rematch 也一样)。
            counts["already_linked"] += 1
            drift["no_body_kept"] += 1
            continue
        old = str(prev["version_id"])
        old_v = known.get(old) or linked_text.get(old)
        # tv_lineage 而 TV 还指着这一版: 那是 TV 自己说的来源(不是我们写回的), 正文整篇换了也不挪, 差异隔离、每晚报给人看;
        # TV 改了 lineage(清掉 / 指到别的版)之后才按 TV / 内容走 —— --rematch 也一样(review on aw#128)。
        tv_says_old = prev.get("match_kind") not in _CONTENT_KINDS and str(n.tv_version_id or "") == old
        st = ("same" if rematch and prev.get("tv_text_fp") == fp_now[n.note_id] else
              _tv_text_state(prev, n, fp_now[n.note_id], known, linked_text))
        if st == "unverifiable":
            counts["already_linked"] += 1
            drift["unverifiable"] += 1
            continue
        if st == "replaced":
            drift["replaced"] += 1
            if tv_says_old:
                counts["already_linked"] += 1             # 不记指纹: 每晚都报, 直到人处理
                blocked.add(n.note_id)
                drift["blocked"] += 1
                _sample(n, prev, None, "TV 带的 lineage, 正文整篇换了: 没挪, 差异已隔离, 要人看")
                continue
            ov = tvlink.overlap(n, old_v) if old_v is not None else None
            w34_note[n.note_id] = [{"note": f"W-34 {now[:10]}: TV 正文整篇换了(原来 {prev.get('match_kind')} "
                                            f"{old[:8]}, 重叠 {0 if ov is None else round(ov, 3)}), 按内容重对"}]
        elif not rematch:
            # seed / edited: 还是那一篇。只记指纹; 差异按现在的正文重算 —— seed 也算: 017 之前改过字的, 旧差异是按改之前
            # 的正文算的, 这回不重算, 记了指纹就再也不会重算了(review on aw#128)。同一版, 不推 computed_at。
            counts["already_linked"] += 1
            fp_set[n.note_id] = fp_now[n.note_id]
            drift["seeded" if st == "seed" else "text_changed"] += 1
            text_changed.add(n.note_id)
            continue
        elif old not in known:
            # --rematch, 连着的那版已不是 item 的 best(不在对照索引里): 匹配器看不见它, 重对只会对到别处或对不上 ——
            # 按内容核得上就留着(以前 --rematch 靠 TV 的 lineage 留住它, review on aw#128); 取不到就不动; 核不上才往下重对。
            if old_v is None:
                counts["already_linked"] += 1
                drift["unverifiable"] += 1
                continue
            if st != "same" or tvlink.still_same(n, old_v):
                drift["kept_after_rematch"] += 1
                counts["already_linked"] += 1
                fp_set[n.note_id] = fp_now[n.note_id]
                text_changed.add(n.note_id)
                continue
        # 整篇换了, 或 --rematch。旧版本是我们自己写回 TV 的(TV 的 lineage 正是它)就不当证据, 按内容对 ——
        # 这也修了 --rematch 的回环: 以前 519 条写回过的对照一重对就走 tv_lineage 分支、对回同一版。
        own = prev.get("match_kind") in _CONTENT_KINDS and str(n.tv_version_id or "") == old
        m = _decide(n, trust_tv=not own)
        mv = (str(m.version.version_id) if m.version is not None else
              str(n.tv_version_id) if m.kind == "tv_lineage" and n.tv_version_id else None)
        if mv == old or (m.kind == "ambiguous" and old in {str(c.get("version_id")) for c in (m.candidates or [])}):
            # 还对回原来那版(或原来那版在分不出的候选里): 对照留着。同一版照常写一遍(--rematch 的老行为), 带上指纹;
            # 分不出的不写 —— 不能因为重对一次就把已经对上的版本丢掉。
            drift["kept_after_rematch"] += 1
            w34_note.pop(n.note_id, None)
            if mv == old:
                _apply(n, m)
                links[-1]["tv_text_fp"] = prev.get("tv_text_fp")     # 指纹等差异算好了再记(见下面 fp_set)
            else:
                counts["already_linked"] += 1
            fp_set[n.note_id] = fp_now[n.note_id]
            text_changed.add(n.note_id)
            continue
        moved.append((n, m, prev, own))

    # 挪对照(W-34)。先撤回 TV 里我们自己写的旧血缘, 撤成了才挪: 先挪的话, 挪成 unmatched 的那条明晚又被 TV 的旧
    # lineage(_decide 的 tv_lineage 分支)拽回旧版。撤回只在 --write-tv 下做(与回填同一个开关); 没开的这晚只报不挪。
    if moved:
        need = [(n, prev) for n, _m, prev, own in moved if own]
        cleared: set[str] = set()
        if len(moved) > relink_max:
            drift["over_cap"] = len(moved)
        elif need and write_tv and dry_run:
            cleared = {n.note_id for n, _ in need}      # dry-run: 报"会怎样"
        elif need and write_tv:
            try:
                # 先把这几条对照标成"没回填过"(review on aw#128): 撤回一提交, TV 就是 NULL 了; 之后挪对照那一步要是没落地
                # (写库失败 / 进程被杀 / RPC 提交了但客户端超时), 第二天正文又改回来的话, 指纹不变、对照不重写, TV 就一直空着。
                # 标过之后, 不管哪一步断, 回填都会把对照现在那一版补回去(只填 NULL, 安全)。
                store.tv_links_mark_unsynced(client, [n.note_id for n, _ in need])
                cleared = store.tv_clear_lineage(client, [
                    {"note_id": n.note_id, "version_id": prev["version_id"], "item_id": prev.get("item_id")}
                    for n, prev in need])
            except Exception as exc:                    # noqa: BLE001
                logger.exception("tv_sync: 撤回 TV 旧血缘失败 (tv=%s), 这晚一条都不挪", tv_project_id)
                drift["clear_error"] = f"{type(exc).__name__}: {exc}"[:200]
            drift["tv_cleared"] = len(cleared)
        for n, m, prev, own in moved:
            go = not drift["over_cap"] and (not own or n.note_id in cleared)
            if go:
                drift["relinked"] += 1
                _sample(n, prev, m, "整篇换了, 已按内容重对" if not rematch else "--rematch 重对到别的判定", move=True)
                _apply(n, m, w34_note.get(n.note_id))
                continue
            counts["already_linked"] += 1
            blocked.add(n.note_id)
            if drift["over_cap"]:
                why = f"超过每晚上限 {relink_max}, 没挪"
            elif own and write_tv and not drift["clear_error"]:
                drift["clear_missed"] += 1
                why = "TV 里的血缘已经不是我们写的那一对, 没挪: 要人看"
            else:
                drift["deferred"] += 1
                why = ("撤回 TV 旧血缘失败, 没挪, 明晚再试" if drift["clear_error"] else
                       "没开 --write-tv, 撤回不了 TV 的旧血缘, 没挪")
            _sample(n, prev, m, why, move=True)

    # ── W-38: 对不上写作台版本的帖, 再拿查重时记下的底稿对一次(补录之前 —— 补录是终态, 补成副本就再也不重对) ──
    snap: dict = {"pending": 0, "matched_notes": 0, "promoted": 0, "minted": 0, "repaired": 0, "fingerprinted": 0,
                  "angles_consumed": 0, "ambiguous": 0, "already_fingerprinted": 0, "held": 0, "failed_notes": 0,
                  "waiting_post": 0, "superseded": 0, "undated": 0, "same_night": 0, "held_rival": 0,
                  "applied": True, "error": None, "samples": []}
    pids = [m["project_id"] for m in maps]
    try:
        snap_rows = store.pending_draft_snapshots(client, pids, since=store.iso_ago(SNAPSHOT_MAX_AGE_DAYS))
    except Exception as exc:                            # noqa: BLE001
        logger.exception("tv_sync: 读查重底稿失败 (tv=%s)", tv_project_id)
        snap["error"] = f"读底稿: {type(exc).__name__}: {exc}"[:300]
        snap_rows = []
        if not store.snapshots_misconfigured(exc):
            # 一时的故障: 这晚一条都不补录(补录是终态, 本该对上底稿的帖补成副本就再也认不回来), 记 unmatched 明晚再对。
            # 库的配置问题(缺权限 / 缺列)不扣 —— 扣了会每晚都扣、补录一直停着; 照旧补录, 退出码 1 让人去修。
            for n in to_ingest:
                snap["held"] += 1
                links.append(_link(n, "unmatched", candidates=w34_note.get(n.note_id, []) + [
                    {"note": "W-38: 这晚读不出查重底稿, 没补录, 明晚再对"}]))
            to_ingest = []
    if snap_rows is None:
        snap["applied"] = False                         # 018 没跑: 没有底稿, 照旧补录
        snap_rows = []
    groups = _snapshot_groups(snap_rows)
    snap["pending"] = len(groups)
    # 前一晚建了版本、后几步断了的(同一个 id 已经在对照索引里): 补齐指纹 / 销牌 / 标记。dry-run 不补。
    # 第二晚这篇帖已经连在那一版上了(对照里有), 帖的文字从那里拿 —— 发出去的文字那一行指纹、底稿记的 note_id 都要补。
    # 对照算到这一晚的: 前一晚断在写对照之前的话, 这篇帖是今晚主索引才连上那一版的(在 links 里, 不在 existing 里, codex
    # on aw#132)。只被打回过的(版本建成之后同样文字又查了一次, 撞了它自己)只记成已转正, 不写指纹、不销牌。
    # 今晚 W-34 挪走的(进了补录名单)和整篇换了没挪成的(blocked)不算: 存着的对照还指着那一版, 可帖已经不是那一篇了
    gone = {n.note_id for n in to_ingest} | blocked
    linked_to: dict[str, list[str]] = {}
    for nid, l in {**existing, **{l["note_id"]: l for l in links}}.items():
        if l.get("version_id") and nid not in gone:
            linked_to.setdefault(str(l["version_id"]), []).append(nid)
    repair: dict[str, list[dict]] = {}
    for vid, g in groups.items():
        if vid in known:
            on = sorted(nid for nid in linked_to.get(vid, ()) if nid in notes_by_id)
            g["notes"] = [(notes_by_id[nid].title, notes_by_id[nid].body) for nid in on]
            g["note_id"] = on[0] if on else None
            g["mark_only"] = not g["ok"]
            # 这个 TV 项目里没有连着它的帖(帖在接同一个写作台项目的另一个 TV 项目里, 或者还没连上): 先补底稿那一行指纹、
            # 销牌, 不记转正 —— 记了就再也补不上帖的文字(codex on aw#132)。连着它的那个 TV 项目那一晚补齐、记转正。
            g["wait_for_post"] = g["ok"] and not on
            repair.setdefault(g["project_id"], []).append(g)
    if repair and not dry_run:
        for pid, gs in repair.items():
            try:
                rep = _promote_snapshot_groups(client, pid, gs, owner=owners[pid],
                                               holder=f"tv-sync-snapshot:{tv_project_id}:{uuid.uuid4().hex[:8]}")
            except Exception as exc:                    # noqa: BLE001
                logger.exception("tv_sync: 补齐转正一半的底稿失败 (project=%s)", pid)
                snap["error"] = snap["error"] or f"补齐: {type(exc).__name__}: {exc}"[:300]
                continue
            snap["repaired"] += sum(1 for g in gs if g["version_id"] in rep["items"] and not g["wait_for_post"])
            snap["waiting_post"] += sum(1 for g in gs if g["version_id"] in rep["items"] and g["wait_for_post"])
            snap["superseded"] += len(rep["superseded"])
            snap["fingerprinted"] += rep["fingerprinted"]
            snap["angles_consumed"] += rep["consumed"]
            snap["error"] = snap["error"] or rep["error"]
    live = [g for vid, g in groups.items() if vid not in known and g["ok"]]
    if to_ingest and live:
        svers = [tvlink.Version(version_id=g["version_id"], project_id=g["project_id"], title=g["title"].strip(),
                                body=tvlink.strip_hashtags(g["body"]), created_at=tvlink._as_dt(g["first_checked_at"]))
                 for g in live]
        sindex = tvlink.VersionIndex(svers)
        by_vid = {g["version_id"]: g for g in live}
        hits: dict[str, list[tuple]] = {}
        held: set[str] = set()                          # 帖还没有发布日期、这晚等着的底稿
        rest: list[tvlink.Note] = []
        for n in to_ingest:
            if n.publish_time is None:
                # 飞书里还没填发布日期就被 TV 读进来了(10-29 生产库近 14 天 413 篇里 17 篇): 底稿要按发布日期定先后, 这晚
                # 定不了 —— 按内容对得上底稿的先不补录(补录是终态, 日期填上以后就再也认不回), 记 unmatched 等日期; 对不上
                # 的照旧补录。等的上限同分不出的: 底稿过了 SNAPSHOT_MAX_AGE_DAYS 就不再参与(复审 on aw#132)
                probe = tvlink.match_snapshot(dataclasses.replace(n, publish_time=tvlink._as_dt(now)), svers, sindex, index,
                                              max_lag=None)
                if probe.kind != "unmatched":
                    snap["undated"] += 1
                    held.update([probe.version.version_id] if probe.version is not None else
                                [str(c.get("version_id")) for c in probe.candidates or () if c.get("version_id") in by_vid])
                    links.append(_link(n, "unmatched", candidates=w34_note.get(n.note_id, []) + [
                        {"note": "W-38: 按内容对得上查重底稿, 帖还没有发布日期, 没补录, 填了日期再对"}]))
                else:
                    rest.append(n)
                continue
            m = tvlink.match_snapshot(n, svers, sindex, index)
            if m.kind == "ambiguous":
                # 分不出是哪一份底稿(或和存过档的版本分不出): 不补录(补成副本就再也认不回), 记 ambiguous 每晚重判,
                # 同主索引的分不出一样等人看(tv-resolve); 底稿过了 SNAPSHOT_MAX_AGE_DAYS 就不再参与, 那时照常补录
                snap["ambiguous"] += 1
                counts["unmatched"] -= 1
                counts["ambiguous"] += 1
                links.append(_link(n, "ambiguous", score=m.score, candidates=w34_note.get(n.note_id, []) + [
                    {"note": "W-38: 和几份查重底稿(或存过档的版本)分不出是哪一篇, 没补录"}] + list(m.candidates or [])))
                if len(ambiguous_samples) < 10:
                    ambiguous_samples.append({"note_id": n.note_id, "title": n.title[:40], "candidates": m.candidates[:3]})
                continue
            if m.version is None:
                rest.append(n)                          # 对不上: 照旧补录(或 W-10 只记 unmatched)
                continue
            hits.setdefault(m.version.version_id, []).append((n, m))
        # 同一篇改写给另一个号发的(开头 / 标题都换了, 包含度不到底稿那一档的线): 明晚主索引会把它连上今晚转正的那一版, 今晚
        # 补录成副本就再也连不上了。所以今晚就按主索引的规矩对一次 —— 对手里带上存过档的版本(只拿今晚这几版比, 会把主索引
        # 判成分不出的硬连上); 对上的跟着这一版一起转正(帖的文字一起记指纹); 分不出的不补录, 明晚主索引重判(codex / 复审
        # on aw#132)。转正只认上面按底稿对上的那几篇: 指纹库那一关把它们都拦下了的, 跟着来的也回去补录。
        # 这晚等发布日期的那几份底稿也一样对一次: 对上的(同一篇的改写版)也等着, 不补录 —— 不然它补成副本, 等原帖填了日期、
        # 底稿转正了也连不回来(复审 on aw#132)
        extra: set[str] = set()
        if (hits or held) and rest:
            tindex = tvlink.VersionIndex([sv for sv in svers if sv.version_id in hits or sv.version_id in held])
            full: tvlink.VersionIndex | None = None
            still: list[tvlink.Note] = []
            for n in rest:
                m = tvlink.match_note(n, tindex) if n.body else tvlink.Match("unmatched")
                if m.version is None and m.kind != "ambiguous":
                    still.append(n)
                    continue
                full = full or tvlink.VersionIndex.merged(index, tindex)
                m = tvlink.match_note(n, full)
                if m.version is not None and m.version.version_id in hits:
                    hits[m.version.version_id].append((n, m))
                    extra.add(n.note_id)
                elif m.version is not None and m.version.version_id in held:
                    snap["undated"] += 1
                    links.append(_link(n, "unmatched", candidates=w34_note.get(n.note_id, []) + [
                        {"note": "W-38: 和一篇还没填发布日期、对得上查重底稿的帖是同一篇, 没补录, 那篇填了日期再对"}]))
                elif m.version is None and m.kind != "ambiguous":
                    still.append(n)
                else:
                    snap["held_rival"] += 1
                    links.append(_link(n, "unmatched", candidates=w34_note.get(n.note_id, []) + [
                        {"note": "W-38: 和今晚转正的底稿、存过档的版本分不出, 没补录, 明晚再对"}] + list(m.candidates or [])))
            rest = still
        # 帖的文字已经在指纹库里了(多半是以前补录的副本, 补录每晚跳过它、记 unmatched): 不拿底稿把一篇早就公开的帖转成写作台
        # 的稿(W-38 复审) —— 照旧走补录那条路(它会跳过、记 unmatched)
        guard_error = None
        if hits:
            hit_notes = [n for pairs in hits.values() for n, _ in pairs]
            oh = [fp.opening_hash(n.body) for n in hit_notes]
            known_fp: dict[str, set] = {}
            try:
                for pid in pids:
                    for h, sks in store.existing_fingerprint_sketches(client, pid, [h for h in oh if h]).items():
                        known_fp.setdefault(h, set()).update(sks)
            except Exception as exc:                    # noqa: BLE001 — 核不了就这晚不转正、也不补录, 明晚再对
                logger.exception("tv_sync: 转正前查指纹库失败 (tv=%s)", tv_project_id)
                guard_error = f"{type(exc).__name__}: {exc}"[:200]
                snap["error"] = snap["error"] or f"转正前查指纹库: {guard_error}"
            for vid in (list(hits) if guard_error is None else ()):
                keep = []
                for n, m in hits[vid]:
                    h = fp.opening_hash(n.body)
                    if h and tuple(sorted(fp.ngram_hashes(n.body))) in known_fp.get(h, ()):
                        snap["already_fingerprinted"] += 1
                        rest.append(n)
                    else:
                        keep.append((n, m))
                if any(n.note_id not in extra for n, _ in keep):
                    hits[vid] = keep
                else:
                    rest.extend(n for n, _ in keep)
                    del hits[vid]
        by_project: dict[str, list[dict]] = {}
        for vid, pairs in hits.items():
            g = by_vid[vid]
            g["note_id"] = pairs[0][0].note_id
            g["notes"] = [(n.title, n.body) for n, _ in pairs]
            by_project.setdefault(g["project_id"], []).append(g)
        made: dict[str, str | None] = {}               # 建成的 version_id → item_id
        failed: dict[str, str] = {}                    # 没建成的 version_id → 原因
        for pid, gs in by_project.items():
            if guard_error is not None:
                failed.update({g["version_id"]: f"查指纹库失败: {guard_error}" for g in gs})
                continue
            if dry_run:
                made.update({g["version_id"]: None for g in gs})
                continue
            try:
                res = _promote_snapshot_groups(client, pid, gs, owner=owners[pid],
                                               holder=f"tv-sync-snapshot:{tv_project_id}:{uuid.uuid4().hex[:8]}")
            except Exception as exc:                    # noqa: BLE001 — IngestBusy / 库错: 这几篇明晚再来
                logger.exception("tv_sync: 底稿转正失败 (project=%s)", pid)
                why = f"{type(exc).__name__}: {exc}"[:200]
                snap["error"] = snap["error"] or f"转正: {why}"
                failed.update({g["version_id"]: why for g in gs})
                continue
            made.update(res["items"])
            snap["minted"] += res["minted"]
            snap["fingerprinted"] += res["fingerprinted"]
            snap["angles_consumed"] += res["consumed"]
            snap["error"] = snap["error"] or res["error"]
            snap["superseded"] += len(res["superseded"])
            failed.update({g["version_id"]: ("这份底稿刚存了档, 明晚按存的那一版对" if g["version_id"] in res["superseded"]
                                             else "帖的文字刚被补录进指纹库, 明晚再对"
                                             if g["version_id"] in res["fingerprinted_meanwhile"]
                                             else res["error"] or "没建成")
                           for g in gs if g["version_id"] not in res["items"]})
        for vid, pairs in hits.items():
            g = by_vid[vid]
            if vid in made:
                snap["promoted"] += 1
                known[vid] = tvlink.Version(version_id=vid, project_id=g["project_id"], title=g["title"].strip(),
                                            body=g["body"].strip(), created_at=tvlink._as_dt(g["first_checked_at"]),
                                            item_id=made[vid])
            for n, m in pairs:
                if vid not in made:
                    # 没建成: 记 unmatched 不补录(补成副本就再也认不回这份底稿), 明晚再对
                    snap["failed_notes"] += 1
                    links.append(_link(n, "unmatched", candidates=w34_note.get(n.note_id, []) + [
                        {"note": f"W-38: 对上了查重底稿, 但这晚没转成版本({failed.get(vid, '')[:80]}), 没补录, 明晚再试"}]))
                    continue
                snap["matched_notes"] += 1
                snap["same_night"] += n.note_id in extra
                counts["unmatched"] -= 1                # _apply 记过一次 unmatched; counts 加起来仍等于笔记数
                counts[m.kind] += 1
                links.append(_link(n, m.kind, project_id=g["project_id"], version_id=vid, item_id=made[vid],
                                   score=m.score, lag=m.lag_days,
                                   candidates=w34_note.get(n.note_id, []) + [{"note": SNAPSHOT_NOTE}]
                                   + list(m.candidates or [])))
                if len(snap["samples"]) < 10:
                    snap["samples"].append({"note_id": n.note_id, "kind": m.kind, "score": round(m.score or 0, 3),
                                            "lag_days": m.lag_days, "version_id": vid,
                                            "angle_key": bool(g.get("angle_key"))})
        to_ingest = rest

    ingest: dict | None = None
    unmatched_kept = 0                   # 没有补录目标、上次就是同样的 unmatched, 这次没重写(W-10)
    dup_of: dict[int, int] = {}          # to_ingest 下标 → 正文相同的第一条的下标
    if to_ingest and target is not None:
        # 先在整批里按 ingest 同一把尺子(开头哈希 + 四字串 sketch)去重, 再分块:
        # 否则跨块的重复在 dry-run 里被数两次、真跑时第二份被判"已有指纹", 两边
        # 的 to_write 对不上(codex #82)。重复的那条最后链到第一条建成的版本。
        seen_key: dict[tuple, int] = {}
        uniq: list[int] = []
        for i, n in enumerate(to_ingest):
            key = (fp.opening_hash(n.body), tuple(sorted(fp.ngram_hashes(n.body))))
            first = seen_key.get(key) if key[0] else None
            if first is None:
                if key[0]:
                    seen_key[key] = i
                uniq.append(i)
            else:
                dup_of[i] = first
        # 几百条分几次调: 每次一把锁(TTL 内做得完)、请求体有上限、半途失败只影响
        # 一批。各批的计数合并成一份 ingest 报表, written 的下标换算回整体下标。
        ingest = {"received": len(to_ingest), "to_write": 0,
                  "skipped_already_fingerprinted": 0,
                  "skipped_duplicate_in_sheet": len(dup_of), "minted": 0, "fingerprinted": 0,
                  "batch_id": None, "batch_ids": [], "identity_error": None,
                  "fingerprint_error": None, "chunk_error": None, "embedded": False,
                  "dry_run": dry_run, "written": [], "skipped_after_failure": 0}
        for start in range(0, len(uniq), TV_SYNC_INGEST_CHUNK):
            idxs = uniq[start:start + TV_SYNC_INGEST_CHUNK]
            entries = [{"title": to_ingest[i].title, "body": to_ingest[i].body} for i in idxs]
            try:
                part = ingest_published(client, target["project_id"], entries,
                                        user_id=owners[target["project_id"]],
                                        source=f"tv:{tv_project_id}", dry_run=dry_run)
            except Exception as exc:                    # noqa: BLE001
                # IngestBusy(锁没等到) / 库错: 这一块一条都没处理, 后面的块也别碰 ——
                # 但**前面块的对照和这次真对上的笔记照样要写进 tv_note_links**, 否则一次
                # 锁冲突就让整个 TV 项目当晚一行对照都没有、明天从头再来(TV 审计 A-02)。
                # 没处理到的记成 unmatched + 原因, 明晚自然重来(unmatched 没 version_id,
                # 不算 already_linked)。
                logger.exception("tv_sync: 补录第 %d 块失败 (tv=%s), 后面的块不再补",
                                 start // TV_SYNC_INGEST_CHUNK + 1, tv_project_id)
                ingest["chunk_error"] = f"{type(exc).__name__}: {exc}"[:300]
                ingest["skipped_after_failure"] = len(uniq) - start
                break
            for k in ("to_write", "skipped_already_fingerprinted",
                      "skipped_duplicate_in_sheet", "minted", "fingerprinted"):
                ingest[k] += part.get(k, 0)
            ingest["embedded"] = ingest["embedded"] or bool(part.get("embedded"))
            if part.get("batch_id"):
                ingest["batch_ids"].append(part["batch_id"])
                ingest["batch_id"] = ingest["batch_id"] or part["batch_id"]
            for key in ("identity_error", "fingerprint_error"):
                if part.get(key) and not ingest[key]:
                    ingest[key] = part[key]
            ingest["written"].extend({"index": idxs[w["index"]], "version_id": w["version_id"]}
                                     for w in part.get("written", []))
            if part.get("fingerprint_error"):
                # 指纹写失败后别再往下补(先 backfill); 没处理到的要数出来并说清:
                # backfill 之后还得再跑一次 tv-sync 补它们(codex #82 P1)。
                ingest["skipped_after_failure"] = len(uniq) - (start + len(idxs))
                break
        by_index = {w["index"]: w["version_id"] for w in ingest.get("written", [])}
        for i, first in dup_of.items():
            if first in by_index:
                by_index[i] = by_index[first]
        item_of: dict[str, dict] = {}
        if by_index and not dry_run:
            try:
                item_of = store.items_for_versions(client, target["project_id"],
                                                   list(by_index.values()))
            except Exception:                        # noqa: BLE001
                logger.exception("tv_sync: 取补录稿子的 item_id 失败, 对照先不带 item_id")
        for i, n in enumerate(to_ingest):
            vid = by_index.get(i)
            why = None
            if not vid and ingest.get("fingerprint_error"):
                why = [{"note": "补录半途指纹写失败, 这条还没处理: 先 backfill, 再跑一次 tv-sync"}]
            elif not vid and ingest.get("chunk_error"):
                why = [{"note": "补录分块失败(" + ingest["chunk_error"][:80] + "), 这条还没处理: 下次 tv-sync 会再补"}]
            links.append(_link(n, "ingested" if vid else "unmatched",
                               project_id=target["project_id"], version_id=vid,
                               item_id=(item_of.get(vid) or {}).get("item_id") if vid else None,
                               candidates=w34_note.get(n.note_id, []) + (why or [])))
    else:
        # W-10 (D-119): 没有补录目标的 TV 项目(RIO ×5)只判不入库, 对不上的留 unmatched。上次就记成
        # 同样的 unmatched(没版本、没备注)的不再每晚重写一遍 —— RIO 782 行每晚只是刷一次 updated_at。
        # 照样重新对一次(写作台存了新版本还能对上); 上次带备注的(比如"没正文")照写, 把旧备注换掉。
        # W-34 挪过来的(上次是有版本的对照)不会走捷径: 它的旧对照有 version_id。
        for n in to_ingest:
            prev = existing.get(n.note_id)
            if (prev and prev.get("match_kind") == "unmatched" and not prev.get("version_id")
                    and not prev.get("candidates")):
                unmatched_kept += 1
                continue
            links.append(_link(n, "unmatched", candidates=w34_note.get(n.note_id)))

    written_links = 0
    backfilled = 0
    if not dry_run:
        written_links = store.tv_links_upsert(client, links)
        if write_tv:
            # ingested 不回写(TV 2026-09-18 核对时的条件): 那些版本是我们从这条
            # 笔记复制进写作台的, 写回去等于说"这条笔记来源于版本 X"而 X 来源于
            # 这条笔记 —— 因果倒置, TV 的模型对比视图会多出上千行没信息量的
            # deskcore。它们留在 tv_note_links 里就够了(查重靠的是指纹, 不靠 TV
            # 那两列)。
            fresh = {l["note_id"] for l in links}
            pending = [l for l in links if _backfillable(l)]
            # 整篇换了、这晚没挪成的(blocked)不回填: 那一版已知不是它的来源(review on aw#128)
            pending += [dict(r, note_id=nid) for nid, r in existing.items()
                        if _backfillable(r) and not r.get("synced_to_tv_at")
                        and nid not in fresh and nid not in blocked]
            backfilled = store.tv_backfill_lineage(client, pending)
            if pending:
                store.tv_mark_synced(client, [l["note_id"] for l in pending])

    edit_diffs = _tv_edit_diffs(client, tv_project_id, links, existing, notes_by_id, known, dry_run=dry_run,
                                text_changed=text_changed, blocked=blocked)
    fp_ok: set = set(fp_set) if dry_run else set()   # 正文指纹记上了的(W-40 只认这些改过字的)
    if fp_set and not dry_run:
        # 指纹最后记, 而且只给差异已经按现在的正文算好了的(review on aw#128): 指纹一记, 明晚就不再当"改过字"; 差异这晚没算成
        # (写库失败 / 读不出 / 被每晚上限挤掉)的先不记, 明晚再核、再算。前面回填抛错中断的, 一条都不记, 同理。
        hold = set(edit_diffs.get("text_left") or ())
        if edit_diffs.get("error"):
            hold |= text_changed
        todo = {nid: f for nid, f in fp_set.items() if nid not in hold}
        drift["fp_held"] = len(fp_set) - len(todo)
        try:
            drift["fp_written"] = store.tv_links_set_fp(client, todo)
            fp_ok = set(todo)
        except Exception as exc:                        # noqa: BLE001 — 明晚再核一遍, 结论一样(退出码 1 让人知道)
            logger.exception("tv_sync: 记 TV 正文指纹失败 (tv=%s)", tv_project_id)
            drift["fp_written"] = getattr(exc, "done", 0)
            drift["fp_error"] = f"{type(exc).__name__}: {exc}"[:200]
    # W-40: 对上了写作台版本的帖, 发出去的文字也记进指纹库。只认按现在的正文核过的对照: 今晚写的对照(带着现在的指纹)、
    # 今晚记上指纹的(改过字、还是那一篇)、之前核过且字没变的; 整篇换了没挪成的(blocked)、补录 / 底稿那条路的不在这里。
    final = {**existing, **{l["note_id"]: l for l in links}}
    pairs = []
    for nid, l in final.items():
        n = notes_by_id.get(nid)
        if (n is None or not n.body or nid in blocked or not l.get("version_id") or l.get("project_id") not in owners
                or l.get("match_kind") not in _PUBLISHED_FP_KINDS):
            continue
        if l.get("tv_text_fp") == fp_now[nid] or nid in fp_ok:
            pairs.append((n, str(l["project_id"]), str(l["version_id"])))
    published_fp = _fingerprint_published(client, pairs, known=known, owners=owners, dry_run=dry_run,
                                          holder=f"tv-sync-published:{tv_project_id}:{uuid.uuid4().hex[:8]}")
    matched = counts["body_exact"] + counts["title_exact"] + counts["fuzzy"] + counts["edited"]
    return {
        "tv_project_id": tv_project_id,
        "desk_projects": [m["project_id"] for m in maps],
        "ingest_target": target["project_id"] if target else None,
        "versions": len(versions), "notes": len(notes),
        "counts": counts, "matched": matched,
        "ingest": ingest, "links_written": written_links, "unmatched_kept": unmatched_kept,
        "tv_backfilled": backfilled, "write_tv": write_tv, "dry_run": dry_run,
        "ambiguous_samples": ambiguous_samples,
        "edit_diffs": edit_diffs,
        "drift": drift,
        "snapshots": snap,
        "published_fp": published_fp,
        "note": (f"{len(notes)} 条笔记: 已对上 {counts['already_linked']}, 本次对上 {matched}"
                 f"(开头 {counts['body_exact']} / 标题 {counts['title_exact']} / 模糊 "
                 f"{counts['fuzzy']} / 改过 {counts['edited']}"
                 + (f"; 其中对上查重底稿 {snap['matched_notes']}" if snap["matched_notes"] else "")
                 + f"), 分不出 {counts['ambiguous']}, 对不上 {counts['unmatched']}"
                 + (f", 没正文 {counts['no_body']}" if counts["no_body"] else "")
                 + (f"; 正文整篇换了 {drift['replaced']}(挪了 {drift['relinked']})" if drift["replaced"] else "")
                 + (f" → 补录 {ingest['minted']}/{ingest['to_write']}"
                    if ingest and not dry_run else
                    f" → 会补录 {ingest['to_write']}" if ingest else
                    (" → 没有补录目标, 只记 unmatched"
                     + (f"(其中 {unmatched_kept} 条上次就是 unmatched, 没重写)" if unmatched_kept else ""))
                    if (to_ingest and target is None) else
                    " → 没有要补录的")),
    }
