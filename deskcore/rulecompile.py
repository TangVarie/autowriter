"""deskcore/rulecompile.py — 规则编译结果怎么存、怎么用(纯函数, 2026-10-20, D-121)。

10-20 生产诊断: 规则全部写成文字塞进写手模型的简报(单个项目的软规则平均 400 字), 守不守全凭注意力。改成「写完再查」:
每条规则先由 Jev 的 ``POST /compile_rules`` 编译一次(编译在 Jev 那边, deskcore 仍然零 LLM), 结果按 (规则 id, sha)
缓存进 ``autowriter.rule_compiles``(migrations/015); 写手写完调 ``check_rules``, 用缓存的结果逐篇查:

  questions      编出来的 1–3 道是非题 → 拼成内联项目题库, 单独发一次 ``/judge_draft``(只判这份题库, 不判段);
                 每题对照 ``pass_if`` 读 ``detail.desk_rules[qid]``: 答案和 pass_if 不一样就是没守住。
  code           能用代码查的(形状同 rulecheck: forbidden_word / forbidden_regex / required_phrase / max_len) →
                 交给 rulecheck 跑, 只跑**没有人手设过能跑的 check** 的规则(人设的那份 score_drafts 已经查过, 不查第二遍);
                 只看话题 / 评论的不跑(这两样不随稿子存, 见 UNSTORED_PARTS)。
  not_checkable  流程 / 交付 / 写作方向菜单, 稿子本身查不了 —— 只计数。

结果只作建议(owner 10-20 拍板): hard → 「必须改」, soft → 「建议改」, Jev 拿不准的单列「拿不准」, 都不拦存档。
简报不动: 规则原文照旧进简报, 软规则移出简报要等校准之后再说。

本模块只放不碰库、不出网的部分(sha、发给 Jev 的规则形状、题库拼法、答案怎么读), 由 core 编排; 和 delivery.py /
rulecheck.py 同一个分法, 测试不用起假库。

⚠️ ``rule_sha`` 和 ``project_bank`` 是 Jev ``judge/rules.py`` 的 ``rule_sha`` / ``to_project_bank`` 的移植, 必须逐字节
一致: sha 不一致 → 缓存永远命中不了、每次都重编(白花钱); 题库形状不一致 → Jev 422 或判法变了。
tests/test_rule_compile.py 拿 Jev 那边对合成样例算出来的值钉着。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

from . import rulecheck

logger = logging.getLogger("deskcore.rulecompile")

VERDICTS = ("code", "questions", "not_checkable")
PASS_IF = ("是", "否")
SCOPES = ("title", "body", "full")
# Jev CompileRequest 的字段上限: 超了整批 422(一条坏的拖垮同一批 59 条好的), 所以发之前就挑出来、不发。
MAX_ID_CHARS = 80
MAX_TEXT_CHARS = 4000
MAX_APPLICABILITY_CHARS = 100
QID_RE = re.compile(r"^[0-9a-z]{1,8}_[0-9a-f]{6}_[1-3]_[0-9a-f]{6}$")      # 同 Jev rules.QID_RE
# 同 Jev banks.DEFAULT_AMBIGUITY: 是非题概率落在 0.35–0.65 记歧义。拼题库时原样带上。
DEFAULT_AMBIGUITY = {"choice_top_min": 0.60, "choice_margin_min": 0.20, "noul_band": [0.35, 0.65]}
# 有把握的线: 同 core.SCORE_SURE_P(参考分那边的口径)。Jev 是非题的 p 是所选答案的概率 = max(p_yes, 1 - p_yes)。
SURE_P = 0.7
UNSURE_LABEL = "拿不准"


# ══════════════════════════════════════════════════════════════════════
# 发给 Jev 的规则
# ══════════════════════════════════════════════════════════════════════

def rule_sha(text: str, applicability: str | None = None) -> str:
    """规则原文(UTF-8, 原样不去空白)的 sha256 前 12 位; 带 applicability(去首尾空白后非空)时是 原文 + "\\x00" + applicability。

    与 Jev ``rules.rule_sha`` 同一个算法(见模块头)。规则原文或适用范围一改 sha 就变, 后台重编。"""
    app = (applicability or "").strip()
    raw = text if not app else text + "\x00" + app
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def compile_input(rule: dict) -> tuple[dict | None, str]:
    """库里一条规则 → (发给 /compile_rules 的那一项, sha)。发不了(字段超 Jev 的上限)回 (None, 原因)。

    text 原样发(不 strip): Jev 按收到的原文算 sha, 这边按同一份算, 两边才对得上。"""
    rid = str(rule.get("id") or "")
    text = rule.get("content") or ""
    app = (rule.get("applicability") or "").strip()
    if not rid or len(rid) > MAX_ID_CHARS:
        return None, "id 为空或太长"
    if not text.strip():
        return None, "原文为空"
    if len(text) > MAX_TEXT_CHARS:
        return None, f"原文 {len(text)} 字, 超过 Jev 的上限 {MAX_TEXT_CHARS}"
    if len(app) > MAX_APPLICABILITY_CHARS:
        return None, f"applicability 超过 {MAX_APPLICABILITY_CHARS} 字"
    sev = "hard" if (rule.get("severity") or "soft").lower() == "hard" else "soft"
    item = {"id": rid, "text": text, "severity": sev}
    if app:
        item["applicability"] = app
    return item, rule_sha(text, app)


def usable(result) -> bool:
    """/compile_rules 回的一条能不能存: 有 verdict、没 error。带 error 的下次再编(Jev 那边单条重试过一次了)。"""
    return (isinstance(result, dict) and not result.get("error")
            and result.get("verdict") in VERDICTS)


# ══════════════════════════════════════════════════════════════════════
# 编出来的题 → 内联项目题库
# ══════════════════════════════════════════════════════════════════════

def _question_ok(q) -> bool:
    return (isinstance(q, dict) and isinstance(q.get("qid"), str) and QID_RE.match(q["qid"]) is not None
            and isinstance(q.get("ask"), str) and q["ask"].strip() != ""
            and q.get("pass_if") in PASS_IF and q.get("scope") in SCOPES)


def project_bank(results: list[dict]) -> dict | None:
    """编出来的题 → ``/judge_draft`` 的内联 project_bank(Jev 格式题库 dict); 一道题都没有回 None。

    Jev ``rules.to_project_bank`` 的移植(逐字节一致, 见模块头), 只差一处: 形状不对 / 题号重复的题**跳过并记日志**,
    不抛 —— 那边是编译当场校验, 这里读的是缓存, 一条坏的不该让整份题库发不出去。
      · 每题 noul、criteria 是 / 否、scope 原样; evidence 跟着 pass_if 走: pass_if「否」的题(答「是」= 违反)on_true,
        依据句就是要改的那句; pass_if「是」的题 never(答「否」是缺了什么, 本来就没有一句可指);
      · 不产出 hard_rules: 这些题只作建议(D-121), 调用方不能把它们写进 /judge_draft 的 hard_rules 或 brief。"""
    seen: set[str] = set()
    qs = []
    for r in results:
        if not usable(r) or r.get("verdict") != "questions":
            continue
        for q in r.get("questions") or []:
            if not _question_ok(q):
                logger.warning("rulecompile: 规则 %s 的一道题形状不对, 跳过", r.get("id"))
                continue
            qid = q["qid"]
            if qid in seen:
                logger.warning("rulecompile: 题号 %s 重复(规则 %s), 跳过", qid, r.get("id"))
                continue
            seen.add(qid)
            qs.append({"id": qid, "type": "noul", "scope": q["scope"], "instructions": {"zh": q["ask"]},
                       "criteria": {"true": {"zh": "是"}, "false": {"zh": "否"}},
                       "evidence": "on_true" if q["pass_if"] == "否" else "never",
                       "feeds": f"写作台规则 {r['id']}（sha {r['sha']}）：答「{q['pass_if']}」算守住；只作建议，不进 hard_rules",
                       "on_ambiguous": "只提示"})
    if not qs:
        return None
    version = "rules-" + hashlib.sha256("|".join(sorted(seen)).encode("utf-8")).hexdigest()[:12]
    return {"bank_version": version,
            "ambiguity": {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_AMBIGUITY.items()},
            "questions": qs}


def question_index(pairs: list[tuple[dict, dict]], bank: dict | None) -> dict[str, dict]:
    """题号 → 读答案要的东西 {rule_id, severity, ask, pass_if, note}。只收真的进了 ``bank`` 的题。

    ``pairs`` 是 (库里那条规则, 它的编译结果)。"""
    in_bank = {q["id"] for q in (bank or {}).get("questions") or []}
    out: dict[str, dict] = {}
    for rule, res in pairs:
        if not usable(res) or res.get("verdict") != "questions":
            continue
        sev = "hard" if (rule.get("severity") or "soft").lower() == "hard" else "soft"
        for q in res.get("questions") or []:
            if _question_ok(q) and q["qid"] in in_bank and q["qid"] not in out:
                out[q["qid"]] = {"rule_id": str(rule.get("id")), "severity": sev, "ask": q["ask"].strip(),
                                 "pass_if": q["pass_if"], "note": (q.get("note") or "").strip()}
    return out


# ══════════════════════════════════════════════════════════════════════
# 编出来的 code → rulecheck
# ══════════════════════════════════════════════════════════════════════

def hand_set(rule: dict) -> bool:
    """这条规则有没有人手设过**真能跑的** check(``rule_kind`` 是 rulecheck.CHECK_KINDS 之一, 且 ``rulecheck.spec_of``
    认得出它的 payload; 10-20 第二批 B)。

    设过的不跑编译出来的 code: score_drafts 已经按人设的那份查过, 再查一遍就重复报。
    ``free_text`` / 空 / 别的值都**不算**(2026-10-20 复审): AW 记忆页存硬规则时默认选「自由文本」, 存进库就是
    ``free_text``, 和没设(validator.py 也当没设)分不开 —— 拿它当「有人明说过不用代码查」, AW 页上记的硬规则编出来的
    code 就永远不跑。record_rule 的 {"kind": "free_text"}(撤掉人设的 check)存的是 free_text 加 rulecheck.WITHDRAWN 标记,
    也不算人设的; 那种是写手明说「这条不用代码查」, 编出来的 code 由 code_rules 另外拦下(见 withdrawn)。
    rule_kind 对、payload 却跑不了的**也不算**(2026-10-20 第三轮复审): AW 页存得进去、rulecheck 不收的那几种(validator
    放过、量词形状那道拒掉的正则; max_len 的「开头」老形状)score_drafts 只在 batch_metrics 里记一笔 skipped, 写手什么都
    看不见 —— 原来这里照样让编出来的 code 让路, 这条硬规则两边都没查, check_rules 还说它守住了 / 是流程规则。"""
    return (rule.get("rule_kind") or "").strip() in rulecheck.CHECK_KINDS and rulecheck.spec_of(rule) is not None


def withdrawn(rule: dict) -> bool:
    """写手用 record_rule 的 {"kind": "free_text"} 撤掉了这条的代码检查(rule_kind = free_text 加 rulecheck.WITHDRAWN 标记;
    codex review on aw#115)。撤的是「代码查」: score_drafts 不查, 生成链路(validator)不查, 这里编出来的 code 也不跑;
    Jev 的规则题是判断, 照判。AW 录规则界面写的 free_text 不带标记, 不算撤掉。"""
    if (rule.get("rule_kind") or "").strip() != "free_text":
        return False
    payload = rule.get("rule_payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return False
    return isinstance(payload, dict) and payload.get("code_check") == rulecheck.WITHDRAWN["code_check"]


# 版本行里只存了标题和正文(autowriter.versions 没有话题 / 评论列): 写手单独传的 tags、comments 都不在。check_rules 能
# 拼回来的话题(core._delivered_text)只有正文末尾被摘走的那行 + 模板的固定 / 条件话题 —— 写手传了 tags 的那篇, 交付稿的
# 话题行按写手传的 tags 拼, 这边拼出来的却是末尾那行, 两边对不上; 评论永远是空的。照这样查, 「每篇要带某个话题」会误报
# 必须改, 「话题不超过 N 个」「评论里不许…」又查不准 / 永远查不出来却算查过了。所以这两处从 where 里拿掉, 拿完一处不剩的
# 不跑, 单独计数(2026-10-20 复审; 第三轮复审改了理由: 模板的固定话题现在拼得回来, 拼不回来的是写手传的 tags)。
# score_drafts 的 rule_checks 手里有写手传的 tags / comments 和模板拼出来的话题, 人设的 check 照查。
# ⚠️ required_phrase 例外: 它是「列出的部位合起来一个都没有才算没守」, where 里只要有一处看不见, 那一处可能就有 ——
# 只查剩下的那半会误报。check_rules 查的又是摘掉末尾那行话题的正文(core._delivered_text), 话术只写在话题行里的
# 那篇一定被报成没守住。所以 required_phrase 的 where 里有话题 / 评论就整条不跑, 同样单独计数。
UNSTORED_PARTS = ("tags", "comments")


def _strip_unstored(payload: dict, kind: str | None = None) -> dict | None:
    """编出来的 code payload → 去掉 tags / comments 之后的 payload; 一处不剩回 None(这条 check_rules 查不了)。
    ``kind`` 是 required_phrase 时, where 里有 tags / comments 就回 None(只查剩下的部位会误报, 见上)。

    where 没给(默认标题 + 正文)或形状不认识的原样放过, 由 rulecheck.spec_of 去校(坏的计进 code_refused)。"""
    where = payload.get("where")
    if isinstance(where, str):
        return None if where in UNSTORED_PARTS else payload
    if not isinstance(where, list) or not any(w in UNSTORED_PARTS for w in where):
        return payload
    if kind == "required_phrase":
        return None
    kept = [w for w in where if w not in UNSTORED_PARTS]
    return {**payload, "where": kept} if kept else None


def code_rules(pairs: list[tuple[dict, dict]]) -> tuple[list[dict], list[str]]:
    """(规则, 编译结果) → (喂给 ``rulecheck.check`` 的规则行(rule_kind / rule_payload 形状), 查不了的规则 id)。

    只收编译结果里带 code、且规则没有人设能跑的 check 的(见 hand_set)、写手也没撤掉代码检查的(见 withdrawn)。where 只管话题 / 评论的(required_phrase: 只要沾了话题 /
    评论)进第二个列表(见 UNSTORED_PARTS), 不跑、不算「代码查了」。payload 不在这里校验: rulecheck.spec_of 读的时候会按人设 check 的同一套规矩再校一遍(正则过
    validator.assert_regex_is_safe + 量词形状那道), 不合格的记日志、跳过、计进 skipped —— Jev 那边编译时也查过, 这里是
    第二道, 不会因为 Jev 放过了一个会回溯的正则就把服务进程卡住。"""
    out, unavailable = [], []
    for rule, res in pairs:
        if hand_set(rule) or withdrawn(rule) or not usable(res):
            continue
        code = res.get("code")
        if not isinstance(code, dict) or code.get("kind") not in rulecheck.CHECK_KINDS:
            continue
        payload = _strip_unstored({k: v for k, v in code.items() if k != "kind"}, code["kind"])
        if payload is None:
            unavailable.append(str(rule.get("id")))
            continue
        out.append({"id": rule.get("id"), "severity": rule.get("severity") or "soft",
                    "rule_kind": code["kind"], "rule_payload": payload})
    return out, unavailable


# ══════════════════════════════════════════════════════════════════════
# Jev 的答案 → 必须改 / 建议改 / 拿不准
# ══════════════════════════════════════════════════════════════════════

def _num(p) -> bool:
    return isinstance(p, (int, float)) and not isinstance(p, bool)


def _what(label: str, q: dict, answer: str, p=None) -> str:
    point = q["note"] or q["ask"]
    tail = f"「{q['ask']}」判了「{answer}」" if q["note"] else f"判了「{answer}」"
    if label == UNSURE_LABEL and _num(p):
        tail += f", 把握 {p:.2f}"
    return f"{label}: {point}({tail})"


def read_answers(questions: dict[str, dict], detail) -> dict:
    """``/judge_draft`` 回的 ``detail.desk_rules`` → {"violations", "unsure", "unjudged"}。

    每题对照 pass_if: 答案和 pass_if 不一样 = 没守住。
      · 歧义(Jev 的 ambiguous)或 p < SURE_P: 不管偏向哪边都记「拿不准」, 不算违反也不算守住 —— 0.6 的「守住」和
        0.6 的「没守住」一样不可信;
      · invalid_reason 不为空: 记「没判出来」(no_title / missing / out_of_vocab / evidence_not_found …), 不报违反也不报
        守住, 没有例外 —— 同 Jev README「规则编译 → 送回 /judge_draft」的契约。evidence_not_found 是答了「是」却指不出
        哪一句, TV 账本里那一行 answer 为 NULL(2026-10-20 第二轮复审: 原来这一种照算违反, 硬规则报「必须改」, 写手照着
        一个账本里记成无效的答案去改稿, 还不知道改哪一句);
      · 题在题库里、回包里却没有: 也是没判出来。
    违反的 what 以「必须改」(hard)/「建议改」(soft)开头, evidence 是 Jev 指出的那一句(有才带)。"""
    detail = detail if isinstance(detail, dict) else {}
    violations, unsure, unjudged = [], [], []
    for qid, q in questions.items():
        it = detail.get(qid)
        it = it if isinstance(it, dict) else {}
        ans, p, reason = it.get("answer"), it.get("p"), it.get("invalid_reason")
        if ans not in PASS_IF or reason:
            unjudged.append({"rule_id": q["rule_id"], "reason": reason or "missing"})
            continue
        violated = ans != q["pass_if"]
        if it.get("ambiguous") or not _num(p) or p < SURE_P:
            entry = {"rule_id": q["rule_id"], "severity": q["severity"], "what": _what(UNSURE_LABEL, q, ans, p),
                     "source": "jev"}
            if _num(p):
                entry["p"] = round(float(p), 3)
            unsure.append(entry)
            continue
        if not violated:
            continue
        entry = {"rule_id": q["rule_id"], "severity": q["severity"],
                 "what": _what(rulecheck.SEVERITY_LABEL[q["severity"]], q, ans), "source": "jev",
                 "p": round(float(p), 3)}
        ev = it.get("evidence")
        if isinstance(ev, str) and ev.strip():
            entry["evidence"] = ev.strip()
        violations.append(entry)
    return {"violations": violations, "unsure": unsure, "unjudged": unjudged}


def code_violations(hits: list[dict]) -> list[dict]:
    """rulecheck.check 的命中 → 和 Jev 那边同一个形状(source = code; what 里已经带着命中的词)。"""
    return [{"rule_id": h.get("rule_id"), "severity": h["severity"], "what": h["what"], "source": "code"}
            for h in hits]
