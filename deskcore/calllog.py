"""deskcore/calllog.py — 每次工具调用留一行只有数的记录(2026-11-11, docs/deskcore.md §3.8「每次工具调用留一行」)。

为什么: 原来只有 score_drafts 在 ``batch_metrics`` 里留一行。开项目、查重、查规则各用了多久、怎么收场的, 写评论那一路
(comments_for / score_comments)判了几条、哪一样检查打回了几条, 库里一个数都没有, 只能去翻只留一周左右的网关日志。

怎么做: app.py 里 REST 和 MCP 本来就给每次工具调用计时的那两处, 回包定下来之后把这一次交给 ``submit``。交接只是往一个
有上限的队列里放一下: 满了就丢, 绝不等。后台一个线程把它摘成一行只有数的记录(``build``), 写进
``autowriter.batch_metrics``(``meta.mode = 'tool_call'``, ``batch_id`` 空着, 不加迁移)。写不进去只记一行限频的日志:
回包这时早就发出去了, 不受影响。进程退出时尽力把队列里剩下的写完(``flush``), 写不完就算了。

⚠️ 这一行只留数、固定的几个词(工具名、结局、信号名这类)和 8 位的代号。稿子、评论、规则、回包里的任何一段文字都不进:
``build`` 只从固定的位置取数(长度、布尔、固定的词), 最后 ``_clean`` 再筛一遍 —— 不是整数 / 布尔 / 认得的词的一律丢掉。
队列里等着的那一项握着这次调用的参数和回包(只在进程内存里, 写完就放), 进库的只有摘出来的那一行。

存档之后在后台跑的 check_rules(改稿复查、规则补查)不是工具调用: 它们直接调 core, 不经过 app 的那两处, 不留这一行。

core 里知道、回包里没有的数(一组评论每样检查打回几条、帖子是按链接还是按正文对上的)用 ``note`` 记一笔: app 在工具跑的
那个线程里装了一个收数的字典(``collecting``), 没装(CLI、用例直接调 core)时 ``note`` 什么都不做。

``DESKCORE_TOOL_CALL_LOG=0`` 关掉(默认开): 关掉之后 ``submit`` 直接回 False, 一行不写。
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import queue
import re
import threading
import time

logger = logging.getLogger("deskcore.calllog")

MODE = "tool_call"
VERSION = 1                               # 这一行的形状改了就加一, 读的时候好分开
ENV_SWITCH = "DESKCORE_TOOL_CALL_LOG"
# 不留痕的工具: get_protocol 每场开头核一次版本, 不是写作的一步(/health、/ready、/tools、/skill-versions 不是工具, 本来就不经过这里)
SKIP_TOOLS = frozenset({"get_protocol"})
COMMENT_TOOLS = frozenset({"comments_for", "score_comments"})
QUEUE_MAX = 200                           # 队列里最多等这么多次调用; 满了新的直接丢(一天也就几十到几百次调用)
FLUSH_SEC = 2.0                           # 进程退出时最多等这么久把剩下的写完
FAIL_LOG_EVERY_SEC = 300.0                # 写不进去 / 队列满: 这么久最多记一行日志

OUTCOMES = ("ok", "error_result", "refused", "failed", "interrupted")
_SIGNALS = frozenset({"opening", "title", "ngram", "contain"})
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# 认得的词: 只有 ASCII 的字母、数字、下划线、点(异常类型名带模块), 后面可以跟一个错误码。中文、空格、标点一律不是
_WORD = re.compile(r"[A-Za-z0-9_.]{1,80}(?: code=[A-Za-z0-9_]{1,16})?")
_MAX_KEYS = 40


def enabled() -> bool:
    """开关(默认开)。每次现读: 用例和本地调试改了环境变量马上生效。"""
    return (os.environ.get(ENV_SWITCH) or "1").strip().lower() not in ("0", "false", "off", "no")


# ── core 那边记一笔 ────────────────────────────────────────────────────────────────────────────────
_notes: contextvars.ContextVar = contextvars.ContextVar("deskcore_call_notes", default=None)


def collecting(sink, fn, /, *args, **kwargs):
    """在工具跑的这个线程里装上 ``sink``(一个字典)再调 ``fn``: 这期间 ``note`` 记的数都进它。原样回 ``fn`` 的结果、
    原样抛 ``fn`` 的异常。"""
    token = _notes.set(sink)
    try:
        return fn(*args, **kwargs)
    finally:
        _notes.reset(token)


def active() -> bool:
    """这会儿有没有人在收 ``note`` 记的数(要先算一遍才有的数, 没人收就不用算)。"""
    return _notes.get() is not None


def note(**fields) -> None:
    """记几个数给这一次调用的那一行。没人在收就什么都不做; 绝不抛。只许放数、布尔、固定的词和 8 位的代号。"""
    sink = _notes.get()
    if isinstance(sink, dict):
        sink.update(fields)


# ── 摘成一行 ──────────────────────────────────────────────────────────────────────────────────────
def _count(v) -> int:
    return len(v) if isinstance(v, (list, tuple, dict)) else 0


def _int(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) < 1e12 else None


def _uuid(v) -> str | None:
    s = v.strip().lower() if isinstance(v, str) else ""
    return s if _UUID.fullmatch(s) else None


def _items(args: dict):
    """这次调用带了几条: 稿子 / 评论 / 要查的版本的条数, 发牌是要几张。别的工具没有这个数。"""
    for key in ("drafts", "comments", "version_ids"):
        if isinstance(args.get(key), list):
            return len(args[key])
    return _int(args.get("n"))


def _reply_chars(result) -> int | None:
    """回包有多长(紧凑 JSON 的字符数, 中文一个字算一个)。量不出来就是 None。"""
    try:
        return len(json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str))
    except Exception:                                   # noqa: BLE001
        return None


def _tally(into: dict, key) -> None:
    if isinstance(key, str):
        into[key] = into.get(key, 0) + 1


def _check_block(res: dict) -> dict:
    """check_drafts: 过 / 可疑 / 拒各几篇, 可疑和拒的是哪个信号定的(一篇可以有几个), 不是撞车的拒(gate)各几篇。"""
    summary = res.get("summary") if isinstance(res.get("summary"), dict) else {}
    block: dict = {k: _int(summary.get(k)) for k in ("total", "pass", "warn", "reject")}
    decided: dict = {}
    gates: dict = {}
    for row in res.get("results") if isinstance(res.get("results"), list) else []:
        if not isinstance(row, dict) or row.get("status") not in ("warn", "reject"):
            continue
        signals = row.get("decided_by")
        for sig in signals if isinstance(signals, list) else []:
            _tally(decided.setdefault(row["status"], {}), sig if sig in _SIGNALS else "other")
        if row.get("gate"):
            _tally(gates, row["gate"])
    if decided:
        block["decided_by"] = decided
    if gates:
        block["gates"] = gates
    if summary.get("semantic_degraded"):
        block["semantic_degraded"] = True
    return block


# check_rules 回包里的数(2026-11-11 那版回包: 拿不准 / 没判出来的每篇只有条数, 偏向没守住的按规则汇成 unsure_rules)
_RULES_SUMMARY = ("versions", "must_fix", "suggest", "unsure", "unjudged", "evidence_not_found", "not_finished",
                  "empty_body", "code_unchecked", "more")
_RULES_POOL = ("pool", "by_jev", "by_code", "uncompiled", "not_checkable", "not_compilable", "partial",
               "code_unavailable", "code_withdrawn", "code_refused", "code_timed_out", "by_score_drafts",
               "questions_unchecked", "ledger_write_errors")
_RULES_STATUSES = frozenset({"checked", "nothing_to_check", "no_versions", "error"})


def _rules_block(res: dict) -> dict:
    """check_rules: 这次怎么收场的、查了几版、必须改 / 建议改 / 拿不准 / 没判出来各几条(整次的合计, 同回包的 ``summary``),
    列出来的 ``unsure_rules`` 几条、其中硬约束几条, 规则池里各有几条是怎么查的 / 为什么没查(同回包的 ``rules``, 只取数)。
    规则 id、规则原文、Jev 指出的句子一样不取。"""
    status = res.get("status")
    block: dict = {"status": status if status in _RULES_STATUSES else "other"}
    summary = res.get("summary") if isinstance(res.get("summary"), dict) else {}
    for key in _RULES_SUMMARY:
        if _int(summary.get(key)) is not None:
            block[key] = _int(summary[key])
    rows = [v for v in res.get("versions") if isinstance(v, dict)] if isinstance(res.get("versions"), list) else []
    if rows:
        block["flagged"] = sum(1 for v in rows if v.get("violations"))          # 有要动手的那几篇
        block["judged"] = sum(1 for v in rows if v.get("judge_status") == "ok")  # 规则题判回来了的那几篇
    lean = [e for e in res.get("unsure_rules") if isinstance(e, dict)] if isinstance(res.get("unsure_rules"), list) else []
    if lean:
        block["unsure_rules"] = len(lean)
        block["unsure_rules_hard"] = sum(1 for e in lean if e.get("severity") == "hard")
    for key, value in (("excluded", res.get("excluded")), ("remaining", res.get("remaining_version_ids"))):
        if _count(value):
            block[key] = _count(value)
    pool = res.get("rules") if isinstance(res.get("rules"), dict) else {}
    pool = {key: _int(pool[key]) for key in _RULES_POOL if _int(pool.get(key)) is not None}
    if pool:
        block["pool"] = pool
    return block


_COMMENT_NOTES = ("source", "post8")                                            # core 记的词
_SCORE_NOTES = ("earlier", "no_slot", "existing")                               # core 记的数
_SCORE_CHECKS = ("brand_over", "repeats", "endings_over", "short_by", "neutral_by")


def _comment_block(tool: str, res: dict, notes: dict) -> dict:
    """写评论那两个工具: 帖子对上没有、怎么对上的; score_comments 另记这一组几条、每样检查打回几条、过了几条、要人核实
    几条、判定的结局、那一格拼了几行。"""
    # 帖子根本没查成的(评论线程占满 / 查帖子超时: 回包里没有 resolved, core 也没记 source)不记成对上了 —— 那一行
    # 没有 resolved 这个键, 靠 judge_status = timeout 认(2026-11-12 复审 TC-1)
    block: dict = {}
    if "resolved" in res or notes.get("source") is not None:
        block["resolved"] = res.get("resolved") is not False
    for key in _COMMENT_NOTES:
        if notes.get(key) is not None:
            block[key] = notes[key]
    if res.get("resolved") is False:
        block["need"] = _count(res.get("need"))
        return block
    if tool == "comments_for":
        block.update(existing=_count(res.get("existing_comments")), comment_rules=_count(res.get("comment_rules")),
                     hard_rules=_count(res.get("hard_rules")), has_set="comment_set" in res)
        return block
    block["n"] = _int(notes.get("n"))
    for key in _SCORE_NOTES:
        if notes.get(key) is not None:
            block[key] = _int(notes[key])
    block["judge_status"] = res.get("judge_status")
    if isinstance(res.get("passed"), bool):
        block["passed"] = res["passed"]                                         # 整组过没过
    summary = res.get("summary") if isinstance(res.get("summary"), dict) else {}
    if summary:
        block.update(passed_n=_int(summary.get("passed")), needs_review=_int(summary.get("needs_review")))
    rows = res.get("comments") if isinstance(res.get("comments"), list) else res.get("style_issues")
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    checks: dict = {"fix": sum(1 for r in rows if r.get("fix")), "style_fix": sum(1 for r in rows if r.get("style_fix")),
                    "thread_fix": _count(res.get("thread_fix")), "thread_hints": _count(res.get("thread_hints"))}
    if isinstance(notes.get("style"), dict):
        checks["style"] = notes["style"]                                        # 写法底线每样打回几条
    for key in _SCORE_CHECKS:
        if notes.get(key) is not None:
            checks[key] = _int(notes[key])
    block["checks"] = checks
    delivery = res.get("delivery_column")
    if isinstance(delivery, dict):
        block["delivery_lines"] = _int(delivery.get("lines"))
    return block


def _clean(value, depth: int = 0):
    """最后一道筛: 只留布尔、整数、认得的词(``_WORD``)和装着它们的字典; 别的(中文、带空格或标点的串、列表、小数)一律丢掉。
    ``build`` 本来就不取文字, 这一道是防以后有人往里加字段时带进来。"""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value if abs(value) < 1e12 else None
    if isinstance(value, str):
        return value if _WORD.fullmatch(value) else None
    if isinstance(value, dict) and depth < 4:
        out = {}
        for k, v in list(value.items())[:_MAX_KEYS]:
            v = _clean(v, depth + 1)
            if isinstance(k, str) and _WORD.fullmatch(k) and v is not None and v != {}:
                out[k] = v
        return out
    return None


def build(job: dict) -> dict:
    """一次调用 → 要写进 ``batch_metrics`` 的那一行(``project_id / user_id / phase_ms / counters / meta``)。

    ``project_id`` 那一列只在调用成了的时候填(评论工具用服务端对上的那个项目): 没成的调用带来的 project_id 可能是抄错的、
    别人的, 填进去外键会让整行写不进去 —— 那种只在 ``meta.project8`` 里留前 8 位。评论工具的帖子没对上 / 没查成时回的也是
    一个普通的 200, 带来的 project_id 同样没过归属校验: 那一列只认回包里的 project_id(2026-11-12 复审 TC-1)。"""
    args = job.get("args") if isinstance(job.get("args"), dict) else {}
    result = job.get("result")
    res = result if isinstance(result, dict) else {}
    notes = job.get("sink") if isinstance(job.get("sink"), dict) else {}
    tool = str(job.get("tool") or "")
    outcome = job.get("outcome")
    if outcome not in OUTCOMES:
        # 没抛、但回的是「出错了」的形状(_safe 的降级 / check_rules 读不出编译结果), 同 app._log_error_result 的判据
        outcome = "error_result" if (res.get("error") or res.get("status") == "error") else "ok"
    elapsed = _int(job.get("elapsed_ms")) or 0
    meta: dict = {"v": VERSION, "tool": tool, "channel": job.get("channel"), "elapsed_ms": elapsed, "outcome": outcome}
    if job.get("error_class"):
        meta["error_class"] = job["error_class"]
    items = _items(args)
    if items is not None:
        meta["items"] = items
    if result is not None:
        meta["reply_chars"] = _reply_chars(result)
    matched = _uuid(res.get("project_id"))
    project_id = matched or _uuid(args.get("project_id"))
    column = project_id if outcome == "ok" and (matched or tool not in COMMENT_TOOLS) else None
    if project_id and column is None:
        meta["project8"] = project_id[:8]
    if result is not None:
        if tool == "check_drafts":
            meta["check"] = _check_block(res)
        elif tool == "check_rules":
            meta["rules"] = _rules_block(res)
        elif tool in COMMENT_TOOLS:
            meta["comments"] = _comment_block(tool, res, notes)
    if notes.get("skill") is not None:
        meta["skill"] = notes["skill"]                  # 入口工具这次报上来的 skill 版本: up_to_date / outdated / unknown
    if isinstance(notes.get("view"), bool):
        meta["view"] = notes["view"]                    # 存档时发没发排好版页面的链接(2026-11-12); 只这一个布尔, 链接不进来
    return {"project_id": column, "user_id": job.get("user_id"), "phase_ms": {"total": elapsed}, "counters": {},
            "meta": _clean(meta)}


# ── 后台写 ────────────────────────────────────────────────────────────────────────────────────────
def _store(row: dict) -> None:
    """把一行写进库。单拎出来是为了用例能换掉它; core / store 在这里才 import(core 自己 import 本模块)。"""
    from . import core, store
    store.insert_tool_call_metrics(core.sb(), **row)


class _Writer:
    """有上限的队列 + 一个后台线程(第一次有东西要写时才起, daemon)。"""

    def __init__(self, maxsize: int = QUEUE_MAX):
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_log = {"dropped": 0.0, "failed": 0.0}
        self.counts = {"written": 0, "dropped": 0, "failed": 0}

    def put(self, job: dict) -> bool:
        try:
            self._q.put_nowait(job)
        except queue.Full:
            self._bump("dropped", "队列满了(后台写得比调用慢), 这次调用没留痕")
            return False
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="tool-call-log", daemon=True)
                self._thread.start()
        return True

    def _bump(self, what: str, why: str) -> None:
        with self._lock:
            self.counts[what] += 1
            n, now = self.counts[what], time.monotonic()
            quiet = self._last_log[what] and now - self._last_log[what] < FAIL_LOG_EVERY_SEC
            if not quiet:
                self._last_log[what] = now
        if not quiet:
            logger.warning("工具调用留痕: %s(进程启动以来第 %d 次; %d 秒内不再重复记)", why, n, int(FAIL_LOG_EVERY_SEC))

    def _run(self) -> None:
        while True:
            job = self._q.get()
            try:
                _store(build(job))
                with self._lock:
                    self.counts["written"] += 1
            except Exception as exc:                    # noqa: BLE001 — 留痕写不进去不是任何人的事, 只记个数
                # 只记异常类型和错误码: 驱动的报错原文里带整行内容
                code = getattr(exc, "code", None)
                self._bump("failed", f"没写进库 ({type(exc).__name__}{f' code={code}' if code else ''})")
            finally:
                job = None                              # 这次调用的参数和回包不留到下一次(队列空着时线程一直等在 get 上)
                self._q.task_done()

    def flush(self, timeout: float = FLUSH_SEC) -> bool:
        """等队列里的都写完(或写失败), 最多等 ``timeout`` 秒。回是不是都处理完了。"""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._q.all_tasks_done:
            while self._q.unfinished_tasks:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._q.all_tasks_done.wait(left)
        return True

    def pending(self) -> int:
        return self._q.qsize()


_writer = _Writer()


def submit(*, tool: str, channel: str, user_id, args, sink, elapsed_ms: int, result=None,
           outcome: str | None = None, error_class: str | None = None) -> bool:
    """app 在回包定下来之后调: 把这一次调用交给后台去记。**不等、不抛**; 回有没有放进队列。

    不记的: 开关关着、``SKIP_TOOLS`` 里的工具、认不出调用者的(``batch_metrics.user_id`` 是非空的 uuid 列, 不编一个填进去)。"""
    try:
        user = _uuid(user_id)
        if tool in SKIP_TOOLS or user is None or not enabled():
            return False
        return _writer.put({"tool": tool, "channel": channel, "user_id": user, "args": args, "sink": sink,
                            "elapsed_ms": elapsed_ms, "result": result, "outcome": outcome, "error_class": error_class})
    except Exception:                                   # noqa: BLE001 — 留痕绝不能改变工具的行为
        return False


def flush(timeout: float = FLUSH_SEC) -> bool:
    """进程退出时(app 的 lifespan 收尾)和用例里调: 尽力把剩下的写完。**不抛**。"""
    try:
        return _writer.flush(timeout)
    except Exception:                                   # noqa: BLE001
        return False


def stats() -> dict:
    """进程启动以来写了 / 丢了 / 没写进去几行, 队列里还等着几项。只有数。"""
    return {**_writer.counts, "pending": _writer.pending(), "queue_max": _writer._q.maxsize}


def reset(maxsize: int = QUEUE_MAX) -> None:
    """测试用: 换一个新的队列和计数(原来那个线程空了就一直等着, 不碍事)。"""
    global _writer
    _writer = _Writer(maxsize)
