"""deskcore/delivery.py — 交付版式由代码拼(2026-10-20)。

10-20 生产诊断: 写作台的稿子只有 23~45% 原样发出去, 其余在飞书里被人重写; 其中一类损耗是版式。版式原来写成
规则交给写手那边的模型(WorkBuddy)照着排, 时灵时不灵 —— 同一个项目里攒了好几条互相打架的"交付格式"规则;
老的独立写作台是在代码里拼的(exporter.py / app.py 的复制块), 从没出过这个问题。所以版式回到代码:

  · 模板存在 ``projects.default_params`` 的 ``delivery`` 键里(生产库这一列存的是 JSON **字符串**, 也认 dict),
    运维用 ``python -m deskcore.cli delivery-format`` 设, 写之前先过 ``validate``;
  · ``score_drafts`` 存完档, 用存进去的那几篇按模板拼整批, 写手把 ``delivery.text`` 原样交给用户。

纯函数, 不连库不联网(同 isolation.py)。问题(problems)只报代码能确定判出来的: 话题不够、评论条数不对、评论没提到
该提的词、表格要方向而稿子没带、模型自己又包了一层标记。都只是参考, 不拦存档。

⚠️ 标记只认【标题】/【正文】。tv-sync(tvlink.parse_raw)按这两个标记把发出去的笔记拆回标题和正文, 运营在飞书里
贴的也一直是这个写法(09-15 以来 6 个在跑项目 450 篇全是); 换成「标题：」这种冒号写法, 整段会被当成正文, 对不回底稿。
"""

from __future__ import annotations

import json
import re

import validator

LAYOUTS = ("table", "blocks")
FIELDS = ("direction", "post", "comments")
ROLES = ("reader", "author")
DEFAULT_MARKERS = {"title": "【标题】", "body": "【正文】"}
MAX_CONTENT_TAGS = 10
MAX_COMMENTS = 20
_NAME_MAX = 20

_TOP_KEYS = ("layout", "columns", "markers", "tags", "comments")
_COLUMN_KEYS = ("name", "field")
_TAG_KEYS = ("content", "fixed", "conditional")
_COND_KEYS = ("when_any", "add")
_COMMENT_KEYS = ("min", "max", "label_reader", "label_author", "must_mention_any")

# 小红书话题的几种写法: "#秋招" / "#秋招[话题]#" / 连写 "#a#b"。哪个 # 算话题只在 validator 认一处(validator.HASHTAG /
# topic_tag_matches: 网址里的 #片段、"C#入门" 这种英文字母数字后面的 # 不算), 数字数、摘话题、查话题的规则三处口径一样。
# 摘末尾话题也只拿 topic_tag_matches 切, 不另写正则(codex review on aw#115): 原来那两条 "#[^\s#\[\]]+(?:\[话题\]#?)?" 串到
# 行尾的正则会在 "#?" 上回溯, 把 "#秋招[话题]#" 收尾的 # 当成下一个话题的开头, 后面那半句正文被当成话题摘走; 长串还是平方级的。


def body_tags(body: str) -> list[str]:
    """正文里的 #话题(话题本体的列表)。网址里的 #片段、"学习C#入门" 里的 #入门 都不算(codex review on aw#115): 交付稿的
    话题行里没有它们, 查话题的规则也不该看见 —— 不然只管话题的禁用词会在一句普通的 C# 上报「必须改」。"""
    return split_tags(" ".join(validator.topic_tags(body)))


class TemplateError(ValueError):
    """模板写错了。消息直接给运维看: 说清是哪一项、该怎么改。"""


# ══════════════════════════════════════════════════════════════════════
# 模板: 读 + 校验
# ══════════════════════════════════════════════════════════════════════

def parse_params(raw) -> dict:
    """``projects.default_params`` → dict。

    生产库 80 行全是 JSON 字符串(db.create_project / update_project 用 json.dumps 写), 也认 dict。空的当 {}。
    ⚠️ 字符串解析不了就抛, 不当 {}: CLI 拿 {} 去合并写回, 会把 isolation_terms 这些别的键一起抹掉。
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise TemplateError(f"default_params 不是合法的 JSON({exc})") from exc
    if not isinstance(raw, dict):
        raise TemplateError(f"default_params 应该是 JSON 对象, 现在是 {type(raw).__name__}")
    return raw


def template_of(project: dict) -> dict | None:
    """项目行 → 校验过的模板; 没配返回 None。模板写坏了抛 TemplateError(打分路径记日志后跳过, 不拦存档)。"""
    params = parse_params((project or {}).get("default_params"))
    raw = params.get("delivery")
    if raw is None:
        return None
    return validate(raw)


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _unknown(obj: dict, allowed: tuple, where: str) -> None:
    bad = [k for k in obj if k not in allowed]
    if bad:
        raise TemplateError(f"{where} 里有不认识的键 {bad}; 只认 {' / '.join(allowed)}")


def norm_tag(tag) -> str:
    """一个话题 → 不带 # 的本体。"#秋招" / "秋招" / "#秋招[话题]#" 都是 "秋招"。"""
    t = str(tag or "").replace("[话题]", "").strip()
    return t.strip("#").strip()


def _template_tag(tag, where: str) -> str:
    if not isinstance(tag, str):
        raise TemplateError(f"{where} 应该是字符串")
    t = norm_tag(tag)
    if not t or re.search(r"[\s#\[\]]", t):
        raise TemplateError(f"{where}「{tag}」不是一个话题: 不能为空, 中间不能有空格或 #")
    return t


def _str_list(raw, where: str, *, allow_empty: bool) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        raise TemplateError(f"{where} 应该是非空字符串组成的列表")
    if not raw and not allow_empty:
        raise TemplateError(f"{where} 至少要有一项")
    return [x.strip() for x in raw]


def validate(raw) -> dict:
    """校验并补齐默认值。返回规范形状, 再喂一次也照样通过(CLI 的 --show 和打分路径读的都是它)。"""
    if not isinstance(raw, dict):
        raise TemplateError("delivery 应该是 JSON 对象")
    _unknown(raw, _TOP_KEYS, "delivery")

    layout = raw.get("layout")
    if layout not in LAYOUTS:
        raise TemplateError(f"delivery.layout 必须是 {' / '.join(LAYOUTS)}, 现在是 {layout!r}")

    markers = raw.get("markers")
    if markers is None:
        markers = dict(DEFAULT_MARKERS)
    if not isinstance(markers, dict):
        raise TemplateError("delivery.markers 应该是 JSON 对象")
    _unknown(markers, ("title", "body"), "delivery.markers")
    markers = {**DEFAULT_MARKERS, **markers}
    if markers != DEFAULT_MARKERS:
        raise TemplateError(
            "delivery.markers 只能是 {\"title\": \"【标题】\", \"body\": \"【正文】\"}: tv-sync 按这两个标记把发出去的笔记"
            "拆回标题和正文, 换成别的写法(尤其「标题：」这种冒号)整段会被当成正文, 对不回写作台的底稿")

    tags = raw.get("tags")
    if tags is None:
        tags = {}
    if not isinstance(tags, dict):
        raise TemplateError("delivery.tags 应该是 JSON 对象")
    _unknown(tags, _TAG_KEYS, "delivery.tags")
    content = tags.get("content", 0)
    if not _is_int(content) or not 0 <= content <= MAX_CONTENT_TAGS:
        raise TemplateError(f"delivery.tags.content 是写手要给几个话题, 必须是 0~{MAX_CONTENT_TAGS} 的整数")
    fixed_raw = tags.get("fixed", [])
    if not isinstance(fixed_raw, list):
        raise TemplateError("delivery.tags.fixed 应该是列表")
    fixed = [_template_tag(t, "delivery.tags.fixed 里的") for t in fixed_raw]
    cond_raw = tags.get("conditional", [])
    if not isinstance(cond_raw, list):
        raise TemplateError("delivery.tags.conditional 应该是列表")
    conditional = []
    for k, c in enumerate(cond_raw):
        where = f"delivery.tags.conditional[{k}]"
        if not isinstance(c, dict):
            raise TemplateError(f"{where} 应该是 {{\"when_any\": [...], \"add\": \"...\"}}")
        _unknown(c, _COND_KEYS, where)
        conditional.append({"when_any": _str_list(c.get("when_any"), f"{where}.when_any", allow_empty=False),
                            "add": _template_tag(c.get("add"), f"{where}.add")})

    comments = raw.get("comments")
    if comments is not None:
        if not isinstance(comments, dict):
            raise TemplateError("delivery.comments 应该是 null 或 JSON 对象")
        _unknown(comments, _COMMENT_KEYS, "delivery.comments")
        lo, hi = comments.get("min"), comments.get("max")
        if not (_is_int(lo) and _is_int(hi) and 0 <= lo <= hi <= MAX_COMMENTS):
            raise TemplateError(f"delivery.comments 要有 min / max, 整数且 0 ≤ min ≤ max ≤ {MAX_COMMENTS}")
        labels = {}
        for key in ("label_reader", "label_author"):
            v = comments.get(key, "")
            if not isinstance(v, str) or len(v) > _NAME_MAX or "\n" in v or "|" in v:
                raise TemplateError(f"delivery.comments.{key} 是评论前面的标签, 字符串(可以为空), "
                                    f"不超过 {_NAME_MAX} 字, 不能有换行或 |")
            labels[key] = v
        if layout == "blocks" and not all(labels.values()):
            # 一篇一段里评论直接接在话题行后面, 没标签就和正文末尾分不开 —— 运维贴进飞书时整段都成了正文
            raise TemplateError("layout=blocks 时 delivery.comments 的 label_reader 和 label_author 都要非空"
                                "(如 \"读者：\" / \"作者回复：\"): 评论紧跟在话题行后面, 没标签就分不出哪里是正文的结尾")
        comments = {"min": lo, "max": hi, **labels,
                    "must_mention_any": _str_list(comments.get("must_mention_any", []),
                                                  "delivery.comments.must_mention_any", allow_empty=True)}

    columns_raw = raw.get("columns")
    columns: list[dict] = []
    if layout == "blocks":
        if columns_raw:
            raise TemplateError("delivery.columns 只给 table 用; blocks 是一篇一段, 不分列, 删掉 columns")
    else:
        if not isinstance(columns_raw, list) or not columns_raw:
            raise TemplateError("layout=table 要有 columns: [{\"name\": \"列名\", \"field\": \"post\"}, ...], 顺序就是列的顺序")
        seen: set[str] = set()
        for k, col in enumerate(columns_raw):
            where = f"delivery.columns[{k}]"
            if not isinstance(col, dict):
                raise TemplateError(f"{where} 应该是 {{\"name\": ..., \"field\": ...}}")
            _unknown(col, _COLUMN_KEYS, where)
            name, field = col.get("name"), col.get("field")
            if not isinstance(name, str) or not name.strip() or len(name) > _NAME_MAX \
                    or "|" in name or "\n" in name:
                raise TemplateError(f"{where}.name 是表头, 非空、不超过 {_NAME_MAX} 字、不能有 | 或换行")
            if field not in FIELDS:
                raise TemplateError(f"{where}.field 只认 {' / '.join(FIELDS)}, 现在是 {field!r}")
            if field in seen:
                raise TemplateError(f"delivery.columns 里 {field} 出现了两次")
            seen.add(field)
            columns.append({"name": name.strip(), "field": field})
        if "post" not in seen:
            raise TemplateError("delivery.columns 里要有一列 field=post(标题 + 正文 + 话题)")
        if "comments" in seen and comments is None:
            raise TemplateError("表格里有 comments 列, 但 delivery.comments 没配(要几条、标签是什么)")
        if "comments" not in seen and comments is not None:
            # 反过来也不行: 评论照样被要求、被查, 拼出来的表里却没地方放, 写手改完交上去评论静默丢了
            raise TemplateError("配了 delivery.comments, 但表格里没有 field=comments 的列: 评论会被要求和检查, "
                                "却拼不进交付稿。加一列 comments, 或者删掉 delivery.comments")

    return {"layout": layout, "columns": columns, "markers": markers,
            "tags": {"content": content, "fixed": fixed, "conditional": conditional},
            "comments": comments}


def wants_tags(template: dict) -> bool:
    t = template.get("tags") or {}
    return bool(t.get("content") or t.get("fixed") or t.get("conditional"))


# ══════════════════════════════════════════════════════════════════════
# 拼
# ══════════════════════════════════════════════════════════════════════

def split_tags(raw) -> list[str]:
    """写手传的 tags(列表或一串) → 话题本体的列表。"#a #b" 塞在一项里、带不带 #、带 [话题] 都认。

    一项是整个列表 json.dumps 出来的一串('["a", "b"]', 模型常这么传, 默认分隔带空格)也认: 去掉空白后以 [ 开头、解得开是
    列表的, 里面的字符串照常再切(# 和 [话题] 照摘)。原来按空白切成 '["a",' 和 '"b"]', 交付稿里是 #["a", #"b"]、一句不报,
    话题数还算够(codex review on aw#115)。摘话题、交付稿的话题行、查话题的规则都从这里拿, 一处改三处一样。
    一长串 [ 的 json.loads 抛的是 RecursionError(3.11 上 1000 层就到), 同解不开的一样照字符串切 —— 抛出去的话 core 那边
    整批的交付稿和代码查规则都没了(codex review on aw#115)。"""
    if raw is None:
        return []
    items = [raw] if isinstance(raw, str) else (list(raw) if isinstance(raw, (list, tuple)) else [])
    out: list[str] = []
    for it in items:
        if not isinstance(it, str):
            continue
        if it.strip().startswith("["):
            try:
                got = json.loads(it)
            except (ValueError, RecursionError):
                got = None
            if isinstance(got, list):
                out += split_tags(got)
                continue
        out += re.findall(r"[^\s#]+", it.replace("[话题]", " "))
    return out


def lift_trailing_tags(body: str) -> tuple[str, list[str]]:
    """正文末尾的话题摘出来。10-20 查生产库: 写手的稿子约六分之一末尾带一行 #话题; 模板也拼一行的话交付稿里就是两行。

    末尾的整行话题之外, 最后一句后面直接跟着的一串话题("冲鸭！#秋招 #求职")也摘: 不摘的话交付稿照样是两份话题。
    正文中间的 #话题 不动; 网址里的 #片段(https://example.com/#faq)、"C#入门" 不是话题(同 validator.topic_tag_matches),
    也不动, 它们后面隔开跟着的话题照摘。"最后祝大家#秋招[话题]#顺利上岸" 这种话题后面还有正文的也不动: 收尾的那个 # 后面
    是正文, 不是又一个话题(codex review on aw#115, 原来后半句被当成话题摘走, 传了 tags 的话交付稿里就没了)。"""
    lines = (body or "").replace("\r\n", "\n").rstrip().split("\n")
    lifted: list[str] = []
    while lines:
        if not lines[-1].strip():
            lines.pop()
            continue
        cut, tags = _trailing_tag_run(lines[-1])
        if not tags:
            break
        lifted = tags + lifted
        head = lines[-1][:cut].rstrip()
        if head.strip():                    # 最后一句后面跟着的一串: 摘完这一句留下, 不再往上看
            lines[-1] = head
            break
        lines.pop()                         # 整行都是话题
    return "\n".join(lines).rstrip(), lifted


def _trailing_tag_run(line: str) -> tuple[int, list[str]]:
    """这一行末尾那一串话题: (从哪儿起, 话题本体)。从行尾往回接 validator.topic_tag_matches 认下的话题, 两个之间只能隔空白;
    隔着正文、网址里的 #片段、"C#入门" 就停。没有是 (-1, [])。"""
    end, cut, run = len(line.rstrip()), -1, []
    for m in reversed(validator.topic_tag_matches(line)):
        if line[m.end():end].strip():
            break
        cut, end = m.start(), m.start()
        run.append(m.group(0))
    return cut, split_tags(" ".join(reversed(run)))


def _dedupe(tags: list[str]) -> list[str]:
    seen, out = set(), []
    for t in tags:
        key = t.casefold()
        if t and key not in seen:
            seen.add(key)
            out.append(t)
    return out


def tag_list(template: dict, title: str, body: str, content_tags: list[str]) -> tuple[list[str], int]:
    """(最终话题, 写手给的有效话题数)。顺序: 写手的(截到 content 个) → 固定的 → 命中条件的; 大小写不敏感去重。

    写手给的里面和固定 / 条件话题重名的先剔掉, 不占写手的名额 —— 否则固定话题会被挤到最前面, 而固定话题的
    位置本来就是项目定死的(有的项目有"话题顺序"的硬规则)。
    """
    cfg = template["tags"]
    text = f"{title}\n{body}".casefold()
    extra = list(cfg["fixed"]) + [c["add"] for c in cfg["conditional"]
                                  if any(w.casefold() in text for w in c["when_any"])]
    taken = {t.casefold() for t in extra}
    mine = [t for t in _dedupe([norm_tag(t) for t in content_tags]) if t.casefold() not in taken]
    mine = mine[:cfg["content"]]
    return _dedupe(mine + extra), len(mine)


def post_tags(template: dict, title: str, body: str, raw_tags) -> tuple[str, list[str], int]:
    """模板管话题时的一篇: (摘掉末尾话题之后的正文, 拼进话题行的话题, 写手给的有效话题数)。

    单独传了 ``tags`` 就以它为准, 没传才用正文末尾摘下来的那几个。assemble 和代码查规则(交付出去的话题)共用这一处。
    ⚠️ 传了空列表也算传了(codex review on aw#115): 写手把话题从 tags 里删光, 就是这篇不带写手的话题, 不能再退回正文末尾
    那几个。没传、传 null、传空字符串(模型常拿 "" 填缺省)才算没传。末尾那行照样摘掉, 不留在正文里。"""
    body, lifted = lift_trailing_tags(body)
    supplied = raw_tags is not None and not (isinstance(raw_tags, str) and not raw_tags.strip())
    tags, n_mine = tag_list(template, title, body, split_tags(raw_tags) if supplied else lifted)
    return body, tags, n_mine


def delivered_post(template: dict, draft: dict) -> tuple[str, list[str]] | None:
    """这篇交付出去的 (正文, 全部话题)。模板不管话题时返回 None(调用方照原样查)。

    正文是摘掉末尾话题之后的那一份(同 assemble); 话题是模板拼的话题行 + 正文中间留下的 #话题。rulecheck 两样都用它:
    固定话题、条件话题是模板加的, 只看写手传的 tags 会和 delivery.text 对不上; 正文末尾那行话题被摘进了话题行, 拿原稿
    查正文规则的话, 只在话题行里的词会被报成「正文里有」, 必须带的话术也会靠被摘走的那行混过去(codex review on aw#115)。"""
    tpl = validate(template)
    if not wants_tags(tpl):
        return None
    d = draft if isinstance(draft, dict) else {}
    title = str(d.get("title") or "").strip()
    body = str(d.get("body") or "").replace("\r\n", "\n").strip()
    body, tags, _n = post_tags(tpl, title, body, d.get("tags"))
    return body, _dedupe(tags + body_tags(body))


def comment_entries(raw) -> list | None:
    """写手传的 comments 的外壳 → 一条一项的列表。没传 / null / 空字符串是 []; 外壳本身认不出(数字、true 这类)回 None。

    单独一条字符串或一条 {role, text} 也认成一条(codex review on aw#115): 列表里的每一项本来就认这两种写法, 模型只写一条
    时常常不包列表。原来非列表一律当 [] —— 项目 min 是 0 的话一句不报, 写手明明给了的评论就从交付稿里消失了。
    字符串是一整串 JSON 的(模型常把嵌套的列表序列化了再传), 解得开、解出来是列表 / 对象的, 照解出来的列表 / 一条对象认;
    不然整段 JSON 带着「读者：」进交付稿, 一句不报。解出来的每一项照常由 comment_list 认: 没有 text 的对象({role, content})、
    外面再包一层的 {"comments": [...]}、列表里夹着的 null / 数字都报成那一条形状不对、不拼 —— 原来只解评论列表和一条 {text},
    别的形状整段 JSON 照样当一条读者评论交出去、还算够条数(codex review on aw#115)。不往里多解一层。解不开的([捂脸R]求链接
    这类; 一长串 [ 抛的 RecursionError 也算解不开)照旧是一条字符串。"""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return []
    if isinstance(raw, str) and raw.strip()[0] in "[{":
        try:
            got = json.loads(raw)
        except (ValueError, RecursionError):
            got = None
        if isinstance(got, list):
            return got
        if isinstance(got, dict):
            return [got]
    if isinstance(raw, (str, dict)):
        return [raw]
    return list(raw) if isinstance(raw, (list, tuple)) else None


def comment_list(raw) -> tuple[list[dict], list[str]]:
    """写手传的 comments → [{role, text}], 外加形状不对的那几条的问题描述。空文本的不算一条。

    text 没有、或者不是字符串的(写成 {role, content}、给了列表 / 数字 / dict)算这一条形状不对: 报出来, 不拼(codex review
    on aw#115)。原来没有 text 的一句不报就没了; 是列表的拿 str() 拼成 Python 的 ['…'] 进交付稿, 而查规则只认字符串、看不见
    它。rulecheck 查评论也只从这里拿, 交付稿里有哪几条评论, 规则就查哪几条。"""
    out, bad = [], []
    entries = comment_entries(raw)
    if entries is None:
        return [], [f"comments 应该是列表(每条 {{\"role\": \"reader\", \"text\": \"...\"}}), 现在是 "
                    f"{type(raw).__name__}, 这篇的评论没拼进去"]
    for k, c in enumerate(entries):
        if isinstance(c, str):
            c = {"role": "reader", "text": c}
        if not isinstance(c, dict) or not isinstance(c.get("text"), str):
            bad.append(f"第 {k + 1} 条评论应该是 {{\"role\": \"reader\", \"text\": \"...\"}}")
            continue
        text = c["text"].strip()
        if not text:
            continue
        role = c.get("role") or "reader"
        if role not in ROLES:
            bad.append(f"第 {k + 1} 条评论的 role 是「{role}」, 只认 reader / author")
            role = "reader"
        out.append({"role": role, "text": text})
    return out, bad


def _cell(text: str) -> str:
    return text.replace("\r\n", "\n").replace("|", "\\|").replace("\n", "<br>")


def assemble(template: dict, drafts: list[dict], *, indices: list[int] | None = None) -> dict:
    """按模板把一批稿子拼成交付稿。返回 ``{"format", "text", "problems": [{"index", "what"}]}``。

    ``indices``: 每篇在调用方那一批里的下标(score_drafts 只拼存进去的那几篇, problems 要按写手传的位置报)。
    """
    tpl = validate(template)
    markers, ccfg = tpl["markers"], tpl["comments"]
    use_tags = wants_tags(tpl)
    idx = list(indices) if indices is not None else list(range(len(drafts)))
    problems: list[dict] = []
    rows: list[dict] = []
    for pos, d in enumerate(drafts):
        i = idx[pos]
        d = d if isinstance(d, dict) else {}
        title = str(d.get("title") or "").strip()
        body = str(d.get("body") or "").replace("\r\n", "\n").strip()
        for label, text in (("标题", title), ("正文", body)):
            hit = [m for m in markers.values() if m in text]
            if hit:
                problems.append({"index": i, "what": f"{label}里已经有「{'」「'.join(hit)}」; 标记由写作台加, "
                                                     f"稿子里删掉"})

        post = [markers["title"] + title]
        if use_tags:
            body, tags, n_mine = post_tags(tpl, title, body, d.get("tags"))
            post.append(markers["body"] + body)
            need = tpl["tags"]["content"]
            if n_mine < need:
                problems.append({"index": i, "what": f"话题标签 {n_mine} 个, 这个项目要 {need} 个; 在 tags 里补齐"})
            if tags:
                post.append(" ".join("#" + t for t in tags))
        else:
            post.append(markers["body"] + body)

        lines: list[str] = []
        if ccfg is not None:
            comments, bad = comment_list(d.get("comments"))
            problems += [{"index": i, "what": w} for w in bad]
            lo, hi = ccfg["min"], ccfg["max"]
            if not lo <= len(comments) <= hi:
                want = f"{lo}" if lo == hi else f"{lo}~{hi}"
                problems.append({"index": i, "what": f"评论 {len(comments)} 条, 这个项目要 {want} 条; 在 comments 里改"})
            terms = ccfg["must_mention_any"]
            for k, c in enumerate(comments):
                if terms and not any(t.casefold() in c["text"].casefold() for t in terms):
                    problems.append({"index": i, "what": f"第 {k + 1} 条评论没提到「{'」「'.join(terms)}」里任何一个"})
                label = ccfg["label_reader"] if c["role"] == "reader" else ccfg["label_author"]
                lines.append(label + c["text"])

        direction = str(d.get("direction") or "").strip()
        rows.append({"post": "\n".join(post), "comments": "\n".join(lines), "direction": direction})
        for col in tpl["columns"]:
            if col["field"] == "direction" and not direction:
                problems.append({"index": i, "what": f"表格有「{col['name']}」列, 这篇没带 direction"})

    if tpl["layout"] == "table":
        head = "| " + " | ".join(c["name"] for c in tpl["columns"]) + " |"
        sep = "| " + " | ".join("---" for _ in tpl["columns"]) + " |"
        body_rows = ["| " + " | ".join(_cell(r[c["field"]]) for c in tpl["columns"]) + " |" for r in rows]
        text = "\n".join([head, sep] + body_rows)
    else:
        text = "\n\n".join("\n".join(filter(None, [r["post"], r["comments"]])) for r in rows)
    return {"format": tpl["layout"], "text": text, "problems": problems}


def requirements(template: dict) -> dict:
    """简报里的 ``delivery_requirements``: 写手动笔前就该知道每篇要带什么(话题几个、评论几条、要不要方向)。

    不给的话第一次 score_drafts 几乎必然带回一串 problems, 整批再存一次(judge 再跑一遍)。只摘写手要做的部分,
    标记、话题顺序、固定话题这些写作台自己加的不在里面。"""
    tpl = validate(template)
    t, c = tpl["tags"], tpl["comments"]
    need_direction = any(col["field"] == "direction" for col in tpl["columns"])
    asks: list[str] = []
    if t["content"]:
        extra = "、".join("#" + x for x in t["fixed"])
        asks.append(f"tags 给 {t['content']} 个话题" + (f"(固定话题 {extra} 写作台会加, 不用给)" if extra else ""))
    if c is not None:
        want = f"{c['min']}" if c["min"] == c["max"] else f"{c['min']}~{c['max']}"
        asks.append(f"comments 给 {want} 条" + (f", 每条都要提到「{'」「'.join(c['must_mention_any'])}」里的一个"
                                                if c["must_mention_any"] else ""))
    if need_direction:
        asks.append("direction 写一句这篇的切入角度(不是 产品向 / 流量向)")
    task = ("这个项目的交付稿由写作台按模板拼。"
            + (f"score_drafts 时每篇 draft 里带上: {'; '.join(asks)}。" if asks else "")
            + "body 只放正文, 不放【标题】【正文】标记、不放话题行。")
    return {"layout": tpl["layout"], "tags": t["content"], "fixed_tags": list(t["fixed"]),
            "comments": (None if c is None else {"min": c["min"], "max": c["max"],
                                                 "must_mention_any": list(c["must_mention_any"])}),
            "direction": need_direction, "task": task}


# CLI 的 --show 用: 拿一篇合成的示例稿拼一次, 让运维设完就看得见长什么样。
SAMPLE_DRAFT = {"title": "示例标题", "body": "示例正文第一段。\n示例正文第二段。", "tags": ["示例话题一", "示例话题二"],
                "comments": [{"role": "reader", "text": "示例读者评论"}, {"role": "author", "text": "示例作者回复"}],
                "direction": "示例方向"}


def preview(template: dict) -> dict:
    return assemble(template, [SAMPLE_DRAFT])
