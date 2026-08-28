"""三个月攒下的调校笔记, 必须真的进到写作简报里。

2026-08-28 摸库摸出来的实况: ``projects.calibration_notes`` 这一列里躺着
**27 个项目、约 25,000 字**的积累(最长一个项目 2,796 字 / 43 行), 时间跨度
2026-05 到 2026-08。这是这套系统里最贵的一份资产 —— 规则可以重写, 指纹可以
重算, 这个只能靠人一次次改稿攒出来。

它现在是通的: ``store.project_row`` 用 ``select("*")`` 把整行取回,
``build_writing_brief`` 读 ``project.get("calibration_notes")`` 拼成
「项目共享基线」注入 P1。

⚠️ 但**这条路此前一条测试都没有**, 而它有一个极其安静的断法:
``select("*")`` 哪天被人改成列清单(为性能, 完全合理的改动) ——
``.get("calibration_notes")`` 拿到 None, 笔记从此不再注入, **不报错**,
简报照常返回, `p1` 少了一段没人看得出来。25,000 字就这么消失。

所以这里钉的是【被禁止的形态】: 简报里没有这份笔记。判据故意写得跟实现无关
—— 不管中间怎么重构, 只要笔记文本没出现在 p1 里就红。
"""

from __future__ import annotations

import pytest

from deskcore import core, store
from tests.fakes import FakeClient


ME = "11111111-1111-1111-1111-111111111111"
PID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

# 取自生产库的真实形态: 一行一条、以动词开头的短句。
SHARED_NOTE = (
    "开场不要用问句起手，直接给场景\n"
    "少用感叹号，一段最多一个\n"
    "产品名第一次出现要给全称，之后可以简称"
)
MY_NOTE = "我习惯把结论放在第二段，不放开头"


def _project(**kw):
    row = {"id": PID, "name": "测试项目", "brand": "测试品",
           "owner_id": ME, "system_prompt": "你是文案",
           "calibration_notes": SHARED_NOTE,
           "tactics": "[]", "default_params": "{}"}
    row.update(kw)
    return row


def _client(project=None, notes_rows=None):
    return FakeClient(rows={
        "projects": [project or _project()],
        "memories": [],
        "user_calibration_notes": list(notes_rows or []),
        "items": [], "versions": [], "batches": [],
        "draft_fingerprints": [], "angle_ledger": [], "style_edits": [],
    })


def _brief(sb):
    return core.build_writing_brief(sb, PID, user_id=ME)


# ══════════════════════════════════════════════════════════════════════
# 笔记必须到得了简报
# ══════════════════════════════════════════════════════════════════════

def test_the_project_calibration_notes_actually_reach_p1():
    """这条是本文件存在的唯一理由。断的是**内容出现在 p1 里**, 不是"某个函数
    被调用过" —— 后者会在重构时假报警, 前者在任何实现下都成立。"""
    b = _brief(_client())
    for line in SHARED_NOTE.splitlines():
        assert line in b["p1"], f"调校笔记这一行没进简报: {line!r}"


def test_a_column_list_that_forgets_calibration_notes_is_caught():
    """模拟那个安静的断法: 项目行里没有这一列(相当于 select 改成了列清单)。

    ⚠️ 这不是假想。``project_row`` 现在是 ``select("*")``, 而"把 select(*)
    换成明确列清单"是任何人都可能做的性能优化。做完之后笔记静默消失、简报照常
    返回 —— 没有这条断言就没有任何东西会红。
    """
    row = _project()
    del row["calibration_notes"]                 # 列没被 select 回来
    b = _brief(_client(row))
    assert not b["counts"]["has_shared_calibration"]
    assert "调校笔记" not in b["p1"], (
        "列都没取到, p1 里却出现了调校笔记 —— 这条用例本身构造错了")


def test_project_row_still_brings_the_column_back():
    """守住上一条依赖的前提: 取项目行时必须把 calibration_notes 带回来。

    直接验行为而不是验 ``select("*")`` 这个写法 —— 换成显式列清单只要包含
    这一列, 照样应该绿。
    """
    row = store.project_row(_client(), PID)
    assert "calibration_notes" in row, (
        "project_row 没把 calibration_notes 取回来 —— "
        "build_writing_brief 的 .get() 会静默拿到 None, 25000 字的积累就此失联")
    assert row["calibration_notes"] == SHARED_NOTE


def test_counts_report_whether_calibration_is_present():
    """简报要如实报告有没有这层, 否则"笔记没生效"从外面完全看不出来。"""
    assert _brief(_client())["counts"]["has_shared_calibration"] is True
    assert _brief(_client(_project(calibration_notes="")))[
        "counts"]["has_shared_calibration"] is False


@pytest.mark.parametrize("blank", ["", "   ", "\n\n", None])
def test_blank_calibration_does_not_emit_an_empty_section(blank):
    """空笔记不该在 p1 里留一个空的「调校笔记」小节 —— 那会让模型以为这层
    存在但没内容, 而不是这层压根没配。"""
    b = _brief(_client(_project(calibration_notes=blank)))
    assert "调校笔记" not in b["p1"]
    assert b["counts"]["has_shared_calibration"] is False


# ══════════════════════════════════════════════════════════════════════
# 共享层与个人层是两段, 不许互相顶掉
# ══════════════════════════════════════════════════════════════════════

def test_personal_notes_are_added_not_substituted():
    """个人叠加不许把项目共享基线顶掉。

    生产实况: 共享层 27 个项目有数据, 个人层 0 条。将来个人层开始有数据时,
    如果实现写成了"有个人笔记就只用个人的", 那 25,000 字会在有人喂第一条个人
    笔记的那天悄悄消失。
    """
    sb = _client(notes_rows=[{"project_id": PID, "user_id": ME,
                              "notes": MY_NOTE, "updated_at": None}])
    b = _brief(sb)
    assert MY_NOTE in b["p1"], "个人叠加没进去"
    for line in SHARED_NOTE.splitlines():
        assert line in b["p1"], f"个人笔记把项目共享基线顶掉了: {line!r}"
    assert b["counts"]["has_shared_calibration"] is True
    assert b["counts"]["has_personal_calibration"] is True


def test_the_two_layers_are_separated_so_the_model_can_tell_them_apart():
    """两段必须分开、各带标签、共享在前。

    混成一坨的话模型分不清"这个品的规矩"和"这个人的习惯", 而这两件事的适用
    范围完全不同 —— 前者换个人照样成立, 后者换个项目才成立。

    ⚠️ 断言刻意**不写死标签的措辞**。第一版写了"个人叠加"就当场红了 ——
    实际文案是"我的个人风格 —— 从我手动改稿里提炼"。钉措辞的用例会在任何
    一次文案调整时假报警, 而这里真正要守的是【两段没有糊在一起、顺序没颠倒】。
    """
    sb = _client(notes_rows=[{"project_id": PID, "user_id": ME,
                              "notes": MY_NOTE, "updated_at": None}])
    p1 = _brief(sb)["p1"]

    first_shared = p1.index(SHARED_NOTE.splitlines()[0])
    at_mine = p1.index(MY_NOTE)
    assert first_shared < at_mine, "个人笔记跑到项目共享基线前面去了"

    between = p1[first_shared:at_mine]
    assert "[" in between and "]" in between, (
        f"两段之间没有任何小节标题, 糊成一坨了:\n{between!r}")


def test_the_shared_baseline_does_not_depend_on_who_is_asking():
    """共享基线是**项目的**资产, 不是个人的。取用它不该跟调用者身份绑定 ——
    否则将来做团队共享时, 换个人打开同一个项目会看到不一样的基线。"""
    row = store.project_row(_client(), PID)
    assert (row.get("calibration_notes") or "").strip() == SHARED_NOTE
