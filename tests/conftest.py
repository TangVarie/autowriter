"""pytest 的共同前置(审计 SUP-024)。

⚠️ 为什么全仓在此之前一个测试文件都没有, 而 CI 里却有十几个 python heredoc:
那些块是随着一次次 review 长出来的, 每个都自带一份假件和一套 env 设置。
它们仍然有效、也仍然在跑 —— 本目录**不是**去替换它们, 而是给后面那批
结构性改造(把生成编排抽成 generation_service)先立一个能长期加用例的地基。
详见 docs/audit-2026-08-23-full.md §7.1。

两件事必须在 import 任何业务模块【之前】做完:

  1. ``AW_DISABLE_ST_CACHE=1`` —— 让 db.py 的缓存 shim 退化成透传
     (db.py:37-39 已支持)。不设的话 st.cache_data 会在无 ScriptRunContext 的
     环境里刷一堆警告, 而且缓存会让"改了数据再读一次"这类断言失真。
  2. Supabase 的三个 env —— config.py 是**模块级**读取的, 缺了会在 import
     期就抛。给占位值即可, 测试全走假件, 不连网。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 仓库根目录进 sys.path —— 业务模块是平铺在根下的(db.py / app.py / ...),
# 没有包结构, 直接 pytest 时不会自动可见。
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("AW_DISABLE_ST_CACHE", "1")
os.environ.setdefault("SUPABASE_URL", "https://placeholder.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "placeholder-anon-key")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "placeholder-service-key")

# 备用中转站(D-148)的开关一律清掉: 开发机 env 里配了备用站的话, config 会在 import 期读到,
# 所有走 clients.get_anthropic_client 的老用例就悄悄换成了"自动切换"那条路。
# 要测它的用例(tests/test_anthropic_relay_failover.py)自己在 config 上设。
for _relay_var in ("ANTHROPIC_BASE_URL_BACKUP", "ANTHROPIC_API_KEY_BACKUP", "RELAY_FORCE",
                   "RELAY_PRIMARY_TIMEOUT_S", "RELAY_SLOW_S", "RELAY_BREAKER_FAILS", "RELAY_BREAKER_COOLDOWN_S"):
    os.environ.pop(_relay_var, None)


def pytest_configure(config):
    # 慢用例(几秒级的模拟闸, angle cards v2 §5.6): 默认照跑, `-m "not slow"` 可以跳过
    config.addinivalue_line("markers", "slow: 几秒级的模拟 / 回测用例(默认照跑, -m 'not slow' 跳过)")


# ── 发牌先验的进程缓存(deskcore/priors.py, angle cards v2 §4) ──────────────────────────────────────
# 生产里先验在两个后台线程里建(open_project 只投递不等, draw_angles 至多等 2.5 秒)。用例里换成「投递就地跑完」的池子:
# 走假件的构建几毫秒就完, 就地跑让每条用例对假件发了哪些请求是确定的(后台线程跑的话, 简报回来之后它还在往同一个假件
# 上发请求, 数请求的断言会时红时绿)。每条用例从空缓存开始, 别的用例建好的先验不串进来。
# 要测「冷缓存 / 慢 TV 不拖慢 open_project」的用例自己调 priors.reset_cache() 换回真的线程池(tests/test_priors.py、
# tests/test_project_now.py)。
import pytest  # noqa: E402


class _InlinePool:
    def submit(self, fn, fut):
        if not fut.set_running_or_notify_cancel():
            return
        try:
            fut.set_result(fn())
        except BaseException as exc:                    # noqa: BLE001 — 交给 Future, 同真池子
            fut.set_exception(exc)

    def wait_idle(self, timeout: float = 10.0) -> bool:
        return True


@pytest.fixture(autouse=True)
def _angle_priors_cache():
    from deskcore import priors
    priors.reset_cache()
    priors._POOL[0] = _InlinePool()
    yield
    priors.reset_cache()


# ── 改稿的后台规则复查(AU-F07, deskcore/core.py 的 _revision_rule_recheck) ──────────────────────────────────
# 生产里存完档在一个后台线程里跑 check_rules。用例里默认关掉: 后台线程会在用例跑完之后还往同一个假件上发请求(数请求的
# 断言时红时绿), 还会去碰别的用例换掉的 store / judge_client。要测它的用例自己把池子换成就地跑的(tests/test_revision_recheck.py)。
@pytest.fixture(autouse=True)
def _revision_recheck_off():
    from deskcore import core
    saved = core._REVISION_RECHECK_POOL[0]
    core._REVISION_RECHECK_POOL[0] = None
    yield
    core._REVISION_RECHECK_POOL[0] = saved
