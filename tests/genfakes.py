"""把 ``generation_service`` 的编排跑起来所需的全套替身 + **调用录音机**。

⚠️ 这不是"再写一份假 db"。它的用途很具体: **在动那 830 行编排之前, 先把两条
路径当前的行为录下来**(SUP-012 要合并 ``_queue_worker_impl`` 与
``_quick_gen_worker``, 而它们已经漂移过一次)。

判据不是"结果看起来对", 而是**逐次调用的序列**:

  · 调了谁、按什么顺序、关键入参是什么;
  · 最后 status 里留下了什么。

这样合并之后同一份录音必须还能对上 —— 合并有没有偷偷改行为, 不用靠人读 diff。

替身的原则和 tests/fakes.py 一致: **只做真的被用到的那部分**, 缺什么加什么。
每个替身都只回"形状对"的最小值, 不模拟真实语义 —— 真实语义那部分由各自的
单元用例覆盖(_save_batch_results 在 ci.yml 的第二批块里, 查重在 deskcore 那边)。
"""

from __future__ import annotations

import contextlib
import threading


class Recorder:
    """记下每一次外部调用。``calls`` 是 (名字, 关键信息) 的有序列表。"""

    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def log(self, name: str, info=None):
        self.calls.append((name, info))

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]

    def info(self, name: str):
        return [i for n, i in self.calls if n == name]


class _GenResult:
    """generator 返回的对象形状(app 侧只读这几个属性)。"""

    def __init__(self, title, body, engine="claude/x"):
        self.title, self.body, self.ai_engine, self.error = title, body, engine, None
        self.keywords = []
        self.token_usage = {}


def _fake_db(rec: Recorder, *, project: dict, extra_ids=("proj-1",)):
    class _DB:
        WriteReturnedNoRow = RuntimeError

        @staticmethod
        def get_project(client, pid):
            rec.log("db.get_project", pid)
            return dict(project, id=pid)

        @staticmethod
        def list_projects(client, user_id):
            rec.log("db.list_projects", user_id)
            # ⚠️ 要把**用例里出现的每个 project_id** 都回出来。只回一个的话,
            # 跨项目的 plan 在 project_by_id 里查不到项目 —— 那一条会直接失败,
            # 而"缓存有没有按项目分桶"这件事就永远测不到(第一版就是这么错的)。
            return [dict(project, id=pid, name=f"项目{pid}") for pid in extra_ids]

        @staticmethod
        def get_confirmed_memories(client, user_id, project_id=None):
            rec.log("db.get_confirmed_memories", project_id)
            return ([], [])

        @staticmethod
        def list_example_items(client, pid, label, limit=5):
            rec.log("db.list_example_items", (pid, label, limit))
            return []

        @staticmethod
        def get_session_instructions(client, user_id, project_id=None):
            rec.log("db.get_session_instructions", project_id)
            return []

        @staticmethod
        def create_batch(client, user_id, **kw):
            rec.log("db.create_batch", kw.get("tactic"))
            return {"id": "batch-1"}

        @staticmethod
        def get_recent_titles_and_openings(client, pid, **kw):
            rec.log("db.get_recent_titles_and_openings", pid)
            return []

        @staticmethod
        def get_recent_titles_openings_with_embeddings(client, pid, **kw):
            rec.log("db.get_recent_titles_openings_with_embeddings", pid)
            return []

        @staticmethod
        def insert_batch_metrics(client, **kw):
            rec.log("db.insert_batch_metrics", kw.get("batch_id"))

        @staticmethod
        def bulk_create_items(client, *a, **k):
            rec.log("db.bulk_create_items")
            return []

        def __getattr__(self, name):        # 兜底: 没显式实现的一律记一笔并回 None
            def _any(*a, **k):
                rec.log(f"db.{name}")
                return None
            return _any

    return _DB()


def _fake_mem(rec: Recorder):
    class _Mem:
        @staticmethod
        def prepare_soft_context(text):
            rec.log("mem.prepare_soft_context", (text or "")[:40])
            return {"mode": "ok"}

        @staticmethod
        def filter_soft_by_relevance(mems, text, report_sink=None, context=None):
            # ⚠️ **必须把 text 原样记下来**。第一版只记了 {n, has_context},
            # 于是两条路径"拿什么文本做相关性过滤"的差异完全测不到 —— 而那
            # 恰好是真实存在的一处漂移(queue 的 context 里没有 image_prompt)。
            # 录音机记得太粗, 得到的绿是假的。
            rec.log("mem.filter_soft_by_relevance",
                    {"n": len(mems or []), "has_context": context is not None,
                     "text": text})
            return list(mems or [])

        @staticmethod
        def render_flywheel_block(lessons):
            rec.log("mem.render_flywheel_block", len(lessons or []))
            return ""

        @staticmethod
        def build_layered_system_prompt(**kw):
            rec.log("mem.build_layered_system_prompt",
                    {k: (v if isinstance(v, (str, int, bool, type(None))) else
                         f"<{type(v).__name__}:{len(v) if hasattr(v, '__len__') else '?'}>")
                     for k, v in sorted(kw.items())})
            return "SYSTEM"

        @staticmethod
        def ingest_user_instruction(client, user_id, text, **kw):
            rec.log("mem.ingest_user_instruction", (text or "")[:30])

    return _Mem()


def _fake_gen(rec: Recorder, *, n=2):
    class _Gen:
        @staticmethod
        def generate_batch(**kw):
            rec.log("gen.generate_batch", {
                "count": kw.get("count"), "engines": list(kw.get("engines") or []),
                "has_prior": bool(kw.get("engine_prior_messages")),
                # extra_instructions 里拼了 image_prompt, 是两条路径最容易漂的
                # 地方之一 —— 原样记下来。
                "extra": kw.get("extra_instructions"),
                "tactic": kw.get("tactic"),
                "audience": kw.get("target_audience"),
                "images": bool(kw.get("images")),
            })
            cb = kw.get("progress_callback")
            if cb:
                cb(0.5, "half")
            return [_GenResult(f"标题{i}", f"正文{i}" * 30) for i in range(n)]

        @staticmethod
        def generate_batch_multi_role(**kw):
            rec.log("gen.generate_batch_multi_role", {"count": kw.get("count")})
            return [_GenResult(f"多角色{i}", f"正文{i}" * 30) for i in range(n)]

    return _Gen()


def _fake_librarian(rec: Recorder):
    class _Lib:
        @staticmethod
        def build_brief(project, **kw):
            rec.log("lib.build_brief", dict(kw))
            return {}

        @staticmethod
        def fetch_flywheel_lessons(brief):
            rec.log("lib.fetch_flywheel_lessons")
            return []

    return _Lib()


def _fake_proj(rec: Recorder):
    class _Proj:
        @staticmethod
        def _parse_json_field(raw, default):
            return default

        @staticmethod
        def get_tactic_prompt_suffix(project, tactic):
            rec.log("proj.get_tactic_prompt_suffix", tactic)
            return ""

    return _Proj()


def _fake_dedup(rec: Recorder, *, available=False):
    class _Dedup:
        @staticmethod
        def embeddings_available():
            rec.log("dedup.embeddings_available")
            return available

        @staticmethod
        def embed_texts(texts):
            rec.log("dedup.embed_texts", len(texts or []))
            return None

    return _Dedup()


def _fake_validator(rec: Recorder):
    class _V:
        @staticmethod
        def filter_hard(mems):
            rec.log("validator.filter_hard", len(mems or []))
            return []

    return _V()


PROJECT = {
    "id": "proj-1", "name": "测试项目", "system_prompt": "基线",
    "calibration_notes": "", "tactics": "[]", "custom_roles": [],
    "queue_strategy": "default",
}

PLAN = {
    "project_id": "proj-1", "project_name": "测试项目", "tactic": "战术A",
    "engines": ["claude"], "engine_models": {}, "count": 2,
    "target_audience": "上班族", "key_messages": "省时", "tone": "轻松",
    "extra_instructions": "", "image_prompt": "", "images": [],
    "use_thinking": False, "gemini_use_thinking": False,
    "use_multi_role": False, "n_roles": 3,
}


@contextlib.contextmanager
def patched(monkeypatch, *, embeddings=False, n_results=2,
            project_ids=("proj-1",)):
    """把 generation_service 的外部依赖全换成替身, 交出 Recorder。

    ⚠️ 换的是 **generation_service 模块上的属性**, 不是那些模块本身 ——
    别的测试用例(以及 deskcore)拿到的仍然是真货。
    """
    import generation_service as gs

    rec = Recorder()
    monkeypatch.setattr(gs, "db", _fake_db(rec, project=PROJECT,
                                       extra_ids=project_ids))
    monkeypatch.setattr(gs, "mem_module", _fake_mem(rec))
    monkeypatch.setattr(gs, "gen_module", _fake_gen(rec, n=n_results))
    monkeypatch.setattr(gs, "librarian_client", _fake_librarian(rec))
    monkeypatch.setattr(gs, "proj_module", _fake_proj(rec))
    monkeypatch.setattr(gs, "dedup_module", _fake_dedup(rec, available=embeddings))
    monkeypatch.setattr(gs, "validator", _fake_validator(rec))

    # 这四个是编排内部的步骤, 各自已有专门的用例覆盖(ci.yml 第二批 / deskcore)。
    # 这里只记一笔 —— 录音关心的是"编排按什么顺序调了它们", 不是它们内部怎么做。
    # ⚠️ error_prefix 两条路径的**传法不一样**: queue 走位置参数(第 5 个),
    # quick 走关键字。两种都要抓得到, 否则录音里会出现一个假的 None 差异。
    # (这个不一致本身也是 SUP-012 要收掉的东西之一。)
    def _prefix(a, k, pos):
        return k.get("error_prefix", a[pos] if len(a) > pos else None)

    # ⚠️ 这几个内部步骤的**入参也要记**, 不能只记 error_prefix。合并的时候最容易
    # 出的错就是"某个变量传串了"(比如把 quick 的临时向量池传成 queue 的共享池),
    # 而那种错只有把入参记下来才看得见 —— 光看调用顺序是绿的。
    def _sbr(*a, **k):
        rec.log("_save_batch_results",
                {"prefix": _prefix(a, k, 4), "batch_id": a[1] if len(a) > 1 else None,
                 "n_results": len(a[3]) if len(a) > 3 else None})
        return ([{"id": "i1"}],
                [{"id": "v1", "title": "标题0", "ai_engine": "claude/x"}],
                ["标题0"])

    def _dedup_pass(*a, **k):
        # 位置签名: (db_client, inserted_versions, version_rows, pool, project_id, ...)
        pool = a[3] if len(a) > 3 else k.get("queue_embeddings")
        rec.log("_run_semantic_dedup_pass", {
            "prefix": _prefix(a, k, 5),
            "project_id": a[4] if len(a) > 4 else None,
            "pool_id": id(pool),                       # 同一个池 = 跨批共享
            "pool_keys": sorted(pool) if isinstance(pool, dict) else None,
            "has_regen": bool(k.get("regen_ctx")),
            "n_versions": len(a[1]) if len(a) > 1 else None,
        })

    def _hard_check(*a, **k):
        rec.log("_run_hard_constraint_check",
                {"prefix": _prefix(a, k, 3),
                 "n_rules": len(a[2]) if len(a) > 2 else None,
                 "n_versions": len(a[1]) if len(a) > 1 else None})

    monkeypatch.setattr(gs, "_save_batch_results", _sbr)
    monkeypatch.setattr(gs, "_run_semantic_dedup_pass", _dedup_pass)
    monkeypatch.setattr(gs, "_run_hard_constraint_check", _hard_check)
    monkeypatch.setattr(gs, "_resolve_engine_sessions",
                        lambda *a, **k: (rec.log("_resolve_engine_sessions"), ({}, {}))[1])
    monkeypatch.setattr(gs, "_commit_session_tokens",
                        lambda *a, **k: rec.log("_commit_session_tokens"))
    monkeypatch.setattr(gs, "_update_session_occupancy",
                        lambda *a, **k: (rec.log("_update_session_occupancy"), [])[1])
    yield rec


def fresh_status() -> dict:
    return {
        "running": True, "done": False, "total": 0, "current": 0,
        "message": "准备中…", "completed": [], "errors": [],
        "warnings": [], "embedding_missing": [],
        "phase": "starting", "_lock": threading.Lock(),
    }
