"""协议正文只管流程；工具字段级的说明放工具 docstring（2026-10-16）。

protocol.md 的每一个字都盖进 protocol_version，改一行就要所有写手重新导入 skill。工具 docstring 是 MCP tools/list
每次连接现发的，部署即生效，和系统提示一样一直在模型眼前。所以「返回值哪个字段什么意思、什么算通过」这类会随工具
变的口径写在 docstring 里，协议只说什么时候调哪个工具、哪些话不能说。"""
from pathlib import Path

from deskcore import tools as T

PROTOCOL = Path(T.__file__).with_name("protocol.md").read_text(encoding="utf-8")

# 判稿 / 评论工具的返回字段和评论位参数：出现在协议里就意味着工具一改口径就得逼写手重导
FIELD_LEVEL = ("hard_fails", "para_stats", "judge_status", "needs_review", "hints", "value_ok", "may_name_brand",
               "min_detail", "must_echo")


def test_protocol_does_not_spell_out_judge_or_comment_fields():
    leaked = [w for w in FIELD_LEVEL if w in PROTOCOL]
    assert not leaked, f"这些是工具字段级的口径，写进 tools.py 的 docstring，不要写进 protocol.md：{leaked}"


def test_protocol_defers_to_tool_descriptions():
    assert "以各工具自己的说明为准" in PROTOCOL


def test_comment_tool_descriptions_carry_the_reading_rules():
    entry, score = T.comments_for.__doc__, T.score_comments.__doc__
    for word in ("link", "resolved", "slots", "comment_rules", "hard_rules", "existing_comments", "score_comments"):
        assert word in entry, f"comments_for 的说明里缺 {word}"
    for word in ("slot", "judge_status", "passed", "needs_review", "fix", "hints", "max_named", "一轮", "不要循环"):
        assert word in score, f"score_comments 的说明里缺 {word}"


def test_judge_tool_descriptions_carry_the_reading_rules():
    doc = T.judge_draft.__doc__
    for word in ("hard_fails", "plan", "judge_status", "不要循环", "不是闸"):
        assert word in doc, f"judge_draft 的说明里缺 {word}"
