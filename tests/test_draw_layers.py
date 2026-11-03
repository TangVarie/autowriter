"""贴型(content layers): 写手说了「写 N 篇 X 贴」才给卡标贴型; 带方向的规则跟着每篇卡的贴型走; 参考帖按贴型优先。

数据全是合成的(项目、台账、Profile、参考帖的 ref)。钉的是:
  · 金标: 没点名贴型时 v2 一批和 c5ca36c 一字不差(候选表、台账 _draw、重发的行、牌、卡面; rng 消耗次序也一样);
  · layer 参数: 字符串 = 全部, dict = 各几篇、其余不标; 别名; 写错了报 layer_invalid、什么都不写; pin 里的「方向」归进
    layer、不当维度; layer 不进 angle_key / 去重;
  · 台账 _draw.ly(P/T/Q/J), 重发带原来那张的 ly, ledger_dims_for 带出来; _draw ≤ 300 字;
  · 卡面那一行(四种贴型各一句, 没贴型不加行);
  · 参考帖: 产品贴 / 精品贴优先产品出场的外部帖、流量贴 / 提问贴优先没出场的, 起量帖优先同一族(TV 有 content_layer 时);
    只是优先、不筛, 记的 p 和真实概率一致。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from deskcore import core
from tests import draw_v2_golden as G

GOLDEN_V2 = json.loads((Path(__file__).parent / "golden" / "draw_v2_c5ca36c.json").read_text(encoding="utf-8"))


# ══════════════════════════════════════════════════════════════════════
# 金标: 没点名贴型的 v2 和 c5ca36c 一字不差
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("entry", GOLDEN_V2["cases"],
                         ids=lambda e: f"{e['case']['kind']}-seed{e['case']['seed']}-n{e['case']['n']}")
def test_v2_without_a_layer_reproduces_the_c5ca36c_draw(entry, monkeypatch):
    monkeypatch.delenv(core.DRAW_POLICY_ENV, raising=False)
    assert G.capture(entry["case"], monkeypatch) == entry["expect"]


def test_the_v2_golden_can_fail(monkeypatch):
    """反证: 换一个 seed, 候选表和牌都不一样 —— 上面那条不是怎么发都绿。"""
    monkeypatch.delenv(core.DRAW_POLICY_ENV, raising=False)
    entry = GOLDEN_V2["cases"][2]
    got = G.capture(dict(entry["case"], seed=entry["case"]["seed"] + 100), monkeypatch)
    assert got["keys_sha256"] != entry["expect"]["keys_sha256"]
    assert got["angles"] != entry["expect"]["angles"]
