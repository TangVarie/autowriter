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
