"""deskcore — 写作台内核 (TV D-041 / R-034)。

autowriter 的 Streamlit 界面停用后, 把它值钱的四个机制(分层硬约束 / 调校笔记
自动萃取 / 正负例池 / 语义查重)做成 MCP 工具, 挂到 WorkBuddy / Claude Code /
CodeBuddy。数据仍是本仓的 autowriter schema, 一行不迁。

⚠️ R-042: 本包是 headless 进程(uvicorn), 不是 Streamlit 脚本上下文。必须在
import db 之前把 AW_DISABLE_ST_CACHE 打开, 否则 db.py 的缓存 shim 会走真
st.cache_data —— 跨进程缓存无法被 app 的 .clear() 失效, 用户在 UI 改完记忆
后 deskcore 会拿 30-60s 的旧数据(worker.py:56 同款处理)。
"""

import os as _os

_os.environ.setdefault("AW_DISABLE_ST_CACHE", "1")
