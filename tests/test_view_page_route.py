"""一批稿子排好版的页面, HTTP 和读库那一层(2026-11-12, deskcore/app.py 的 ``GET /v/{令牌}``, core.view_page_data / view_link)。

钉住:
  1. 不带钥匙能打开的**只有** ``GET /v/<一段>``; 别的动词、别的路径照旧 401;
  2. 令牌不对 / 过期 / 那一批找不到 / 几批不在一个项目 / 总开关关了 → 同一张出错页、同一个状态码; 令牌没验过不碰库;
  3. 响应头(CSP、不收录、不带来源、不缓存、nosniff); 读库出错或太慢 → 503 那一页; 页面有自己的线程额度;
  4. 页面上是每篇现在的那一版(改稿之后刷新就是新的), 顺序照存档时记的; 复制出去的字和 delivery 那一格一样;
  5. 令牌不进任何一行日志(页面那几行、被拒的那一行、uvicorn 的访问日志); /health 只回显开没开。
纯函数那一层在 tests/test_viewpage.py。所有文本都是合成的, 钥匙是测试里现编的。
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

import anyio                                              # noqa: E402
import anyio.to_thread                                    # noqa: E402

import deskcore.app as A                                  # noqa: E402
from deskcore import core, delivery, tools, viewpage      # noqa: E402
from tests.fakes import FakeClient                        # noqa: E402
from tests.test_viewpage import _Doc                      # noqa: E402

UID = "22222222-2222-2222-2222-222222222222"
GOOD = "k-shili-0123456789abcdefFAKE"                      # 现编的
SECRET = "xianbian-de-yemian-miyao-0001"                   # 现编的
BASE = "https://desk.example.com"
PROJ_T = "aaaaaaaa-0000-0000-0000-0000000000a1"            # 有交付模板
PROJ_P = "aaaaaaaa-0000-0000-0000-0000000000a2"            # 没有模板
TABLE = {"layout": "table",
         "columns": [{"name": "方向", "field": "direction"}, {"name": "笔记", "field": "post"},
                     {"name": "评论", "field": "comments"}],
         "tags": {"content": 2, "fixed": ["固定话题"]},
         "comments": {"min": 1, "max": 3, "label_reader": "读者：", "label_author": "作者回复："}}


def _draft(k: int, tag: str = "") -> dict:
    return {"title": f"合成标题{tag}第{k}篇", "body": f"{tag}第{k}篇的第一段。\n{tag}第{k}篇的第二段。",
            "tags": [f"话题{k}甲", f"话题{k}乙"], "direction": f"{tag}第{k}篇的方向",
            "comments": [{"role": "reader", "text": f"{tag}第{k}篇的读者评论"}, {"role": "author", "text": "作者的回复"}]}


class _Db(FakeClient):
    """假库 + 往里放一批稿子的小工具。``reads`` 数页面读了几次库。"""

    def __init__(self):
        super().__init__(rows={"projects": [
            {"id": PROJ_T, "name": "示例项目甲", "default_params": json.dumps({"delivery": TABLE}, ensure_ascii=False)},
            {"id": PROJ_P, "name": "示例项目乙"}], "batches": [], "items": [], "versions": []})

    def add_batch(self, project_id: str, drafts: list[dict], *, created_at: str, record: bool = True) -> dict:
        bid = str(uuid.uuid4())
        order, parts, items = [], {}, []
        for d in drafts:
            iid, vid = str(uuid.uuid4()), str(uuid.uuid4())
            order.append(vid)
            parts[vid] = {k: d[k] for k in core.DELIVERY_FIELDS if k in d}
            self.rows["items"].append({"id": iid, "batch_id": bid, "best_version_id": vid})
            self.rows["versions"].append({"id": vid, "item_id": iid, "title": d["title"], "body": d["body"],
                                          "version_num": 1})
            items.append({"item_id": iid, "version_id": vid})
        params = {"source": "deskcore", **({core.DELIVERY_PARTS_KEY: {"order": order, "parts": parts}} if record else {})}
        self.rows["batches"].append({"id": bid, "project_id": project_id, "created_at": created_at, "params": params})
        return {"id": bid, "items": items}

    def revise(self, batch: dict, at: int, draft: dict, *, record: bool = True) -> str:
        item = batch["items"][at]
        vid = str(uuid.uuid4())
        self.rows["versions"].append({"id": vid, "item_id": item["item_id"], "title": draft["title"],
                                      "body": draft["body"], "version_num": 2})
        next(i for i in self.rows["items"] if i["id"] == item["item_id"])["best_version_id"] = vid
        if record:
            row = next(b for b in self.rows["batches"] if b["id"] == batch["id"])
            row["params"][core.DELIVERY_PARTS_KEY]["parts"][vid] = {k: draft[k] for k in core.DELIVERY_FIELDS if k in draft}
        return vid

    @property
    def reads(self) -> int:
        return len(self.calls)


@pytest.fixture()
def db(monkeypatch):
    deadline = time.monotonic() + 5                        # 上一个用例故意留在后台的读(超时放弃的线程)先等它退
    while A._view_inflight and time.monotonic() < deadline:
        time.sleep(0.02)
    assert A._view_inflight == 0
    d = _Db()
    monkeypatch.setattr(A, "_view_client", lambda: d)
    return d


@pytest.fixture()
def client(monkeypatch, db):
    monkeypatch.setenv("DESKCORE_KEYS", json.dumps({GOOD: {"user_id": UID, "name": "示例写手"}}, ensure_ascii=False))
    monkeypatch.delenv("DESKCORE_API_KEY", raising=False)
    monkeypatch.delenv("DESKCORE_ALLOW_ANONYMOUS", raising=False)
    monkeypatch.setenv("DESKCORE_VIEW_SECRET", SECRET)
    monkeypatch.setenv("DESKCORE_PUBLIC_URL", BASE)
    monkeypatch.delenv("DESKCORE_VIEW_PAGE", raising=False)
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)

    class _Dead:                                           # /health 不连真库
        def table(self, *_a, **_k):
            raise RuntimeError("no db in this test")
    monkeypatch.setattr(A, "_health_probe_client", lambda: _Dead())
    return fastapi_testclient.TestClient(A.app, raise_server_exceptions=False)


def _token(*batches, expires_in: int = 3600) -> str:
    return viewpage.make_token([b["id"] if isinstance(b, dict) else b for b in batches],
                               int(time.time()) + expires_in, viewpage.derive_key(SECRET))


def _server_log(caplog) -> str:
    """服务端记下的日志。httpx 那一行是测试自己这个客户端打的(它当然知道自己请求的地址), 不算。"""
    return "\n".join(rec.getMessage() for rec in caplog.records if rec.name != "httpx")


def _strip_nonce(text: str) -> str:
    return re.sub(r'nonce(="|-)[A-Za-z0-9_-]+', "nonce", text)


# ── 1. 只有 GET /v/<一段> 不带钥匙 ───────────────────────────────────────────────────────────────

def test_the_page_opens_without_a_key(client, db):
    batch = db.add_batch(PROJ_T, [_draft(0), _draft(1)], created_at="2026-11-12T06:30:00+00:00")
    r = client.get(f"/v/{_token(batch)}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "示例项目甲" in r.text and "共 2 篇笔记" in r.text
    assert r.text.index("合成标题第0篇") < r.text.index("合成标题第1篇")
    assert "读者：" in r.text and "第0篇的读者评论" in r.text and "#话题0甲 #话题0乙 #固定话题" in r.text


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD"])
def test_other_verbs_on_the_page_path_still_need_a_key(client, db, method):
    batch = db.add_batch(PROJ_T, [_draft(0)], created_at="2026-11-12T06:30:00+00:00")
    before = db.reads
    r = client.request(method, f"/v/{_token(batch)}")
    assert r.status_code == 401, (method, r.status_code)
    assert db.reads == before


@pytest.mark.parametrize("path", ["/v", "/v/", "/v/a/b", "/v/a/", "/vv/abc", "/V/abc", "/tools", "/skill-versions",
                                  "/tool/list_projects", "/x/v/abc", "/v//abc"])
def test_nothing_else_became_public(client, path):
    assert client.get(path, follow_redirects=False).status_code == 401, path


def test_the_public_path_shape_is_one_segment_under_v_for_get_only():
    class _Req:
        def __init__(self, method, path):
            self.method, self.scope = method, {"path": path}
    assert A._is_view_request(_Req("GET", "/v/abc"))
    for method, path in (("POST", "/v/abc"), ("HEAD", "/v/abc"), ("OPTIONS", "/v/abc"), ("GET", "/v/"), ("GET", "/v"),
                         ("GET", "/v/a/b"), ("GET", "/v/a/"), ("GET", "/mcp/v/abc"), ("GET", "/tool/v/abc"),
                         ("GET", "v/abc"), ("GET", "")):
        assert not A._is_view_request(_Req(method, path)), (method, path)
    assert A._UNAUTHENTICATED_PATHS == frozenset({"/health", "/ready", ""}), "免鉴权的固定路径没有多出来"


def test_a_key_in_the_request_is_neither_needed_nor_counted(client, db, monkeypatch):
    monkeypatch.setattr(A, "_KEY_TRANSPORT", A.collections.Counter())
    batch = db.add_batch(PROJ_T, [_draft(0)], created_at="2026-11-12T06:30:00+00:00")
    assert client.get(f"/v/{_token(batch)}", headers={"X-Deskcore-Key": "k-cuode-0000"}).status_code == 200
    assert sum(A.key_transport_counts().values()) == 0


# ── 2. 一张出错页; 没验过令牌不碰库 ──────────────────────────────────────────────────────────────

def test_every_failure_is_the_same_page_and_status(client, db, monkeypatch):
    batch = db.add_batch(PROJ_T, [_draft(0)], created_at="2026-11-12T06:30:00+00:00")
    other = db.add_batch(PROJ_P, [_draft(1)], created_at="2026-11-12T07:30:00+00:00")
    good = _token(batch)
    cases = {
        "garbage": "/v/not-a-token",
        "odd characters": "/v/abc.def%20ghi",
        "trailing punctuation": f"/v/{good}%E3%80%82",
        "truncated": f"/v/{good[:-3]}",
        "tampered": f"/v/{good[:10]}{'A' if good[10] != 'A' else 'B'}{good[11:]}",
        "expired": f"/v/{_token(batch, expires_in=-5)}",
        "signed with another secret": "/v/" + viewpage.make_token([batch["id"]], int(time.time()) + 3600,
                                                                 viewpage.derive_key("lingyiba-miyao-0000002")),
        "unknown batch": f"/v/{_token(str(uuid.uuid4()))}",
        "one known one unknown": f"/v/{_token(batch, str(uuid.uuid4()))}",
        "two projects": f"/v/{_token(batch, other)}",
        "very long": "/v/" + "A" * 400,
    }
    seen = set()
    for name, path in cases.items():
        r = client.get(path)
        assert r.status_code == 404, (name, r.status_code)
        assert "这个链接打不开了" in r.text and "重新给一个页面链接" in r.text, name
        assert "合成标题" not in r.text and "示例项目" not in r.text, name
        assert "default-src 'none'" in r.headers["content-security-policy"], name
        seen.add(_strip_nonce(r.text))
    monkeypatch.setenv("DESKCORE_VIEW_PAGE", "0")
    r = client.get(f"/v/{good}")
    assert r.status_code == 404
    seen.add(_strip_nonce(r.text))
    assert len(seen) == 1, "哪一种都是同一页, 看不出是哪一种"


def test_a_token_that_does_not_verify_never_reaches_the_database(client, db, monkeypatch):
    batch = db.add_batch(PROJ_T, [_draft(0)], created_at="2026-11-12T06:30:00+00:00")

    def _boom():
        raise AssertionError("the database was touched before the token verified")
    monkeypatch.setattr(A, "_view_client", _boom)
    monkeypatch.setattr(core, "view_page_data", lambda *a, **k: _boom())
    good = _token(batch)
    for path in ("/v/zzz", f"/v/{good[:-1]}", f"/v/{_token(batch, expires_in=-1)}", "/v/" + "A" * 50):
        assert client.get(path).status_code == 404
    assert db.reads == 0
    assert client.get(f"/v/{good}").status_code == 503, "验过了才去读, 读不出来是 503"


def test_no_usable_secret_means_every_link_is_the_error_page(client, db, monkeypatch):
    batch = db.add_batch(PROJ_T, [_draft(0)], created_at="2026-11-12T06:30:00+00:00")
    good = _token(batch)
    monkeypatch.delenv("DESKCORE_VIEW_SECRET")
    monkeypatch.setattr(viewpage, "_server_secret", lambda: "")
    assert client.get(f"/v/{good}").status_code == 404 and db.reads == 0


# ── 3. 响应头, 503, 自己的线程额度 ───────────────────────────────────────────────────────────────

def test_headers_on_the_page_and_the_nonce_matches_the_markup(client, db):
    batch = db.add_batch(PROJ_T, [_draft(0)], created_at="2026-11-12T06:30:00+00:00")
    one, two = client.get(f"/v/{_token(batch)}"), client.get(f"/v/{_token(batch)}")
    csp = one.headers["content-security-policy"]
    nonce = re.search(r"script-src 'nonce-([A-Za-z0-9_-]{16,})'", csp).group(1)
    assert f"style-src 'nonce-{nonce}'" in csp and "'unsafe-inline'" not in csp
    for part in ("default-src 'none'", "base-uri 'none'", "form-action 'none'", "frame-ancestors 'none'"):
        assert part in csp
    assert one.text.count(f'nonce="{nonce}"') == 2, "样式和脚本各带一次"
    assert one.headers["x-robots-tag"] == "noindex, nofollow"
    assert one.headers["referrer-policy"] == "no-referrer"
    assert one.headers["cache-control"] == "private, no-store"
    assert one.headers["x-content-type-options"] == "nosniff"
    assert nonce not in two.headers["content-security-policy"], "每次响应一个新的 nonce"


def test_a_failing_read_is_the_503_page_and_logs_no_error_text(client, db, monkeypatch, caplog):
    batch = db.add_batch(PROJ_T, [_draft(0)], created_at="2026-11-12T06:30:00+00:00")

    def _fail(*_a, **_k):
        raise RuntimeError("Failing row contains (合成标题第0篇, 整行稿子)")
    monkeypatch.setattr(core, "view_page_data", _fail)
    token = _token(batch)
    with caplog.at_level(logging.DEBUG):
        r = client.get(f"/v/{token}")
    assert r.status_code == 503 and "页面暂时打不开" in r.text and "合成标题" not in r.text
    assert "default-src 'none'" in r.headers["content-security-policy"] and r.headers["cache-control"] == "private, no-store"
    logged = _server_log(caplog)
    assert "view page: read failed: RuntimeError" in logged and batch["id"][:8] in logged
    assert "Failing row" not in logged and "合成标题" not in logged and token not in logged and batch["id"] not in logged


def test_a_slow_read_is_cut_off_with_the_503_page(client, db, monkeypatch):
    batch = db.add_batch(PROJ_T, [_draft(0)], created_at="2026-11-12T06:30:00+00:00")
    monkeypatch.setattr(A, "_VIEW_TIMEOUT", 0.2)

    def _slow(*_a, **_k):
        time.sleep(1.0)
        return {}
    monkeypatch.setattr(core, "view_page_data", _slow)
    t0 = time.monotonic()
    r = client.get(f"/v/{_token(batch)}")
    assert r.status_code == 503 and "页面暂时打不开" in r.text
    assert time.monotonic() - t0 < 0.8, "不等那一次读自己结束"


def test_the_page_has_its_own_thread_budget():
    async def _default():
        return anyio.to_thread.current_default_thread_limiter()
    assert isinstance(A._VIEW_LIMITER, anyio.CapacityLimiter) and 1 <= A._VIEW_LIMITER.total_tokens <= 8
    assert A._VIEW_LIMITER is not anyio.run(_default), "不和工具抢默认的线程额度"
    assert A._VIEW_LIMITER is not A._HEALTH_LIMITER, "也不排在 /health 前面"
    assert 0 < A._VIEW_TIMEOUT <= 15


def test_page_reads_leave_the_tools_and_health_untouched_while_they_hang(client, db, monkeypatch):
    """页面的读全卡住时(占满页面自己的额度), /health 和带钥匙的路由照常应答。"""
    import threading
    batch = db.add_batch(PROJ_T, [_draft(0)], created_at="2026-11-12T06:30:00+00:00")
    release = threading.Event()
    monkeypatch.setattr(A, "_VIEW_TIMEOUT", 0.3)
    monkeypatch.setattr(core, "view_page_data", lambda *a, **k: release.wait(5) and {})
    try:
        for _ in range(A._VIEW_LIMITER.total_tokens + 2):
            assert client.get(f"/v/{_token(batch)}").status_code == 503
        assert client.get("/health").status_code == 200
        assert client.get("/tools", headers={"X-Deskcore-Key": GOOD}).status_code == 200
    finally:
        release.set()


def test_reads_that_have_not_finished_are_capped_and_the_slot_comes_back(client, db, monkeypatch):
    """超时放弃的读, 线程还在后台: 攒到上限就不再开新的, 直接 503; 线程退了名额就回来。"""
    import threading
    batch = db.add_batch(PROJ_T, [_draft(0)], created_at="2026-11-12T06:30:00+00:00")
    url = f"/v/{_token(batch)}"
    assert client.get(url).status_code == 200 and A._view_inflight == 0, "正常读完名额就还了"
    release, calls = threading.Event(), []
    monkeypatch.setattr(A, "_VIEW_TIMEOUT", 0.2)
    monkeypatch.setattr(A, "_VIEW_MAX_INFLIGHT", 2)
    monkeypatch.setattr(core, "view_page_data", lambda *a, **k: calls.append(1) or (release.wait(5) and {}))
    try:
        for _ in range(5):
            assert client.get(url).status_code == 503
        assert len(calls) == 2 and A._view_inflight == 2, "第三次起没有再开线程"
    finally:
        release.set()
    deadline = time.monotonic() + 3
    while A._view_inflight and time.monotonic() < deadline:
        time.sleep(0.02)
    assert A._view_inflight == 0


def test_unknown_batch_and_failed_read_give_the_slot_back(client, db, monkeypatch):
    assert client.get(f"/v/{_token(str(uuid.uuid4()))}").status_code == 404 and A._view_inflight == 0
    monkeypatch.setattr(core, "view_page_data", lambda *a, **k: 1 / 0)
    batch = db.add_batch(PROJ_T, [_draft(0)], created_at="2026-11-12T06:30:00+00:00")
    assert client.get(f"/v/{_token(batch)}").status_code == 503 and A._view_inflight == 0


# ── 4. 页面上的内容: 现在的那一版, 记下的顺序, 和 delivery 同一段字 ──────────────────────────────

def test_copy_text_matches_the_delivery_cells(client, db):
    drafts = [_draft(0), _draft(1), _draft(2)]
    batch = db.add_batch(PROJ_T, drafts, created_at="2026-11-12T06:30:00+00:00")
    page = core.view_page_data(db, [batch["id"]], 1_900_000_000)
    rows, _problems = delivery.rows(delivery.validate(TABLE), drafts)
    assert [p["copy_post"] for p in page["posts"]] == [r["post"] for r in rows]
    assert [p["copy_comments"] for p in page["posts"]] == [r["comments"] for r in rows]
    table = delivery.assemble(delivery.validate(TABLE), drafts)["text"]
    for p in page["posts"]:
        assert p["copy_post"].replace("\n", "<br>") in table and p["copy_comments"].replace("\n", "<br>") in table
    assert page["project"] == "示例项目甲" and page["expires_at"] == 1_900_000_000
    assert db.reads == 7, "batch 属于哪个项目、项目行、batches 各一次; items / versions 翻页各两次(末尾那次空页)"


def test_a_revised_post_shows_its_current_version_on_refresh(client, db):
    batch = db.add_batch(PROJ_T, [_draft(0), _draft(1)], created_at="2026-11-12T06:30:00+00:00")
    url = f"/v/{_token(batch)}"
    assert "合成标题第1篇" in client.get(url).text
    db.revise(batch, 1, {**_draft(1, "改过的"), "tags": ["新话题甲", "新话题乙"]})
    after = client.get(url).text
    assert "合成标题改过的第1篇" in after and "合成标题第1篇" not in after
    assert "改过的第1篇的读者评论" in after and "#新话题甲 #新话题乙 #固定话题" in after
    assert after.index("合成标题第0篇") < after.index("合成标题改过的第1篇"), "改过的那篇还在原位"
    assert "共 2 篇笔记" in after


def test_a_version_without_recorded_parts_borrows_the_posts_latest_recorded_ones(db):
    batch = db.add_batch(PROJ_T, [_draft(0)], created_at="2026-11-12T06:30:00+00:00")
    db.revise(batch, 0, {"title": "别处改出来的一版", "body": "别处改的正文。"}, record=False)
    post = core.view_page_data(db, [batch["id"]], 1_900_000_000)["posts"][0]
    assert post["title"] == "别处改出来的一版" and post["direction"] == "第0篇的方向"
    assert [c["text"] for c in post["comments"]] == ["第0篇的读者评论", "作者的回复"]


def test_order_follows_the_recorded_order_not_the_row_order(db):
    batch = db.add_batch(PROJ_P, [_draft(k) for k in range(5)], created_at="2026-11-12T06:30:00+00:00")
    db.rows["items"].sort(key=lambda r: r["id"])
    db.rows["versions"].sort(key=lambda r: r["id"], reverse=True)
    page = core.view_page_data(db, [batch["id"]], 1_900_000_000)
    assert [p["title"] for p in page["posts"]] == [f"合成标题第{k}篇" for k in range(5)]
    assert [p["n"] for p in page["posts"]] == [1, 2, 3, 4, 5]


def test_a_batch_without_recorded_parts_still_renders_in_a_stable_order(client, db):
    batch = db.add_batch(PROJ_P, [_draft(k) for k in range(4)], created_at="2026-11-12T06:30:00+00:00", record=False)
    one = core.view_page_data(db, [batch["id"]], 1_900_000_000)
    two = core.view_page_data(db, [batch["id"]], 1_900_000_000)
    assert sorted(p["title"] for p in one["posts"]) == [f"合成标题第{k}篇" for k in range(4)]
    assert [p["title"] for p in one["posts"]] == [p["title"] for p in two["posts"]]
    assert all(not p["comments"] and not p["direction"] for p in one["posts"])
    r = client.get(f"/v/{_token(batch)}")
    assert r.status_code == 200 and 'class="wrap single"' in r.text


def test_project_without_a_template_copies_marker_title_newline_marker_body(db):
    draft = {"title": "没有模板的一篇", "body": "第一段。\n第二段。\n#话题甲 #话题乙"}
    batch = db.add_batch(PROJ_P, [draft], created_at="2026-11-12T06:30:00+00:00")
    post = core.view_page_data(db, [batch["id"]], 1_900_000_000)["posts"][0]
    assert post["copy_post"] == "【标题】没有模板的一篇\n【正文】第一段。\n第二段。\n#话题甲 #话题乙"
    assert post["paragraphs"] == ["第一段。", "第二段。"] and post["tags"] == ["话题甲", "话题乙"]


def test_several_batches_of_one_project_go_oldest_first(db):
    late = db.add_batch(PROJ_T, [_draft(0, "后")], created_at="2026-11-12T09:00:00+00:00")
    early = db.add_batch(PROJ_T, [_draft(0, "先"), _draft(1, "先")], created_at="2026-11-12T06:30:00+00:00")
    page = core.view_page_data(db, [late["id"], early["id"]], 1_900_000_000)
    assert [p["title"] for p in page["posts"]] == ["合成标题先第0篇", "合成标题先第1篇", "合成标题后第0篇"]
    assert page["created_at"] == "2026-11-12T06:30:00+00:00"


def test_view_page_data_refuses_unknown_and_mixed_batches(db):
    a = db.add_batch(PROJ_T, [_draft(0)], created_at="2026-11-12T06:30:00+00:00")
    b = db.add_batch(PROJ_P, [_draft(1)], created_at="2026-11-12T06:31:00+00:00")
    for ids in ([str(uuid.uuid4())], [a["id"], str(uuid.uuid4())], [a["id"], b["id"]]):
        with pytest.raises(core.ViewNotFound):
            core.view_page_data(db, ids, 1_900_000_000)
    db.rows["projects"] = [p for p in db.rows["projects"] if p["id"] != PROJ_T]
    with pytest.raises(core.ViewNotFound):
        core.view_page_data(db, [a["id"]], 1_900_000_000)


def test_a_broken_template_falls_back_to_the_plain_layout(db):
    batch = db.add_batch(PROJ_T, [_draft(0)], created_at="2026-11-12T06:30:00+00:00")
    db.rows["projects"][0]["default_params"] = json.dumps({"delivery": {"layout": "不认识的版式"}}, ensure_ascii=False)
    post = core.view_page_data(db, [batch["id"]], 1_900_000_000)["posts"][0]
    # 按没有模板排: 写手单独传的话题照样接在正文后面(模板加的固定话题没有了)
    assert post["copy_post"] == "【标题】合成标题第0篇\n【正文】第0篇的第一段。\n第0篇的第二段。\n#话题0甲 #话题0乙"


def test_injection_through_stored_rows_stays_text_end_to_end(client, db):
    evil = '"><script>alert(1)</script><img src=x onerror=alert(2)>'
    draft = {"title": f"标题{evil}", "body": f"正文{evil}", "tags": [f"话题{evil}"], "direction": f"方向{evil}",
             "comments": [{"role": "reader", "text": f"评论{evil}"}]}
    batch = db.add_batch(PROJ_T, [draft], created_at="2026-11-12T06:30:00+00:00")
    db.rows["projects"][0]["name"] = f"项目{evil}"
    r = client.get(f"/v/{_token(batch)}")
    assert r.status_code == 200
    assert r.text.count("<script") == 1 and "<img" not in r.text
    assert "<script>alert(1)" not in r.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
    doc = _Doc(r.text)
    assert len(doc.named("script")) == 1 and not doc.named("img")
    assert not [k for _tag, attrs in doc.tags for k in attrs if k.startswith("on")]


# ── 5. 令牌不进日志; /health 只回显开没开 ────────────────────────────────────────────────────────

def test_no_log_line_carries_the_token_or_the_full_batch_id(client, db, caplog):
    batch = db.add_batch(PROJ_T, [_draft(0)], created_at="2026-11-12T06:30:00+00:00")
    good = _token(batch)
    ghost = _token(str(uuid.uuid4()))
    with caplog.at_level(logging.DEBUG):
        assert client.get(f"/v/{good}").status_code == 200
        assert client.get(f"/v/{ghost}").status_code == 404
        assert client.get(f"/v/{good[:-2]}").status_code == 404
        assert client.post(f"/v/{good}").status_code == 401
    logged = _server_log(caplog)
    for secret in (good, ghost, good[:20], ghost[:20], batch["id"], SECRET):
        assert secret not in logged
    assert "path=/v/-" in logged, "被拒的那一行只说是 /v/ 下面的"
    assert "view page: batch not found" in logged


def test_uvicorn_access_lines_lose_the_token():
    token = "A" * 50
    flt = next(f for f in logging.getLogger("uvicorn.access").filters if isinstance(f, A._HideViewToken))
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                            ("127.0.0.1:5000", "GET", f"/v/{token}", "1.1", 200), None)
    assert flt.filter(rec) is True
    assert rec.getMessage() == '127.0.0.1:5000 - "GET /v/- HTTP/1.1" 200'
    other = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                              ("127.0.0.1:5000", "POST", "/tool/list_projects", "1.1", 200), None)
    assert flt.filter(other) is True and "/tool/list_projects" in other.getMessage()
    odd = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "no args here", None, None)
    assert flt.filter(odd) is True


def test_health_echoes_only_whether_it_is_on(client, db, monkeypatch):
    body = client.get("/health")
    assert body.json()["view_page"] == {"enabled": True}
    assert SECRET not in body.text and "desk.example.com" not in body.text
    monkeypatch.setenv("DESKCORE_VIEW_PAGE", "0")
    assert client.get("/health").json()["view_page"] == {"enabled": False, "reason": "switched_off"}
    monkeypatch.delenv("DESKCORE_VIEW_PAGE")
    monkeypatch.delenv("DESKCORE_PUBLIC_URL")
    assert client.get("/health").json()["view_page"] == {"enabled": False, "reason": "no_public_url"}, \
        "测试里的请求是 http://testserver: 不是 https 也不是本机, 不拿它拼链接"
    assert client.get("/health", headers={"X-Forwarded-Proto": "https"}).json()["view_page"] == {"enabled": True}
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "desk.up.example.app")
    assert client.get("/health").json()["view_page"] == {"enabled": True}
    assert client.get("/ready").json()["view_page"] == {"enabled": True}


# ── 发链接: core.view_link, 这次请求自己的地址 ───────────────────────────────────────────────────

def test_view_link_mints_a_link_that_opens_the_page(client, db):
    batch = db.add_batch(PROJ_T, [_draft(0)], created_at="2026-11-12T06:30:00+00:00")
    link = core.view_link([batch["id"]])
    assert set(link) == {"url", "expires", "note"} and link["url"].startswith(f"{BASE}/v/")
    assert link["note"].endswith(link["url"]) and link["note"].startswith("排好版的页面")
    assert client.get(link["url"][len(BASE):]).status_code == 200


def test_view_link_is_none_when_off_and_never_raises(client, db, monkeypatch, caplog):
    batch = db.add_batch(PROJ_T, [_draft(0)], created_at="2026-11-12T06:30:00+00:00")
    assert core.view_link([]) is None and core.view_link(None) is None and core.view_link(["不是 id"]) is None
    assert core.view_link([str(uuid.uuid4()) for _ in range(5)]) is None, "多于 4 个 batch 不发"
    monkeypatch.setenv("DESKCORE_VIEW_PAGE", "0")
    assert core.view_link([batch["id"]]) is None
    monkeypatch.delenv("DESKCORE_VIEW_PAGE")

    def _boom(_ids, **_k):
        raise RuntimeError(f"{BASE}/v/should-never-be-logged")
    monkeypatch.setattr(viewpage, "mint", _boom)
    with caplog.at_level(logging.DEBUG):
        assert core.view_link([batch["id"]]) is None
    logged = _server_log(caplog)
    assert "RuntimeError" in logged and batch["id"][:8] in logged
    assert "should-never-be-logged" not in logged and batch["id"] not in logged


def test_a_tool_sees_the_request_address_only_when_it_is_trustworthy(client, monkeypatch):
    """没配对外地址时用这次请求自己的: 工具在线程池里也拿得到(REST 和 MCP 两条路), 但只认 https / 本机。"""
    monkeypatch.delenv("DESKCORE_PUBLIC_URL")
    monkeypatch.setitem(tools.TOOLS, "probe_view_base", (lambda: {"base": viewpage.public_base()}, False))
    key = {"X-Deskcore-Key": GOOD}

    def base(headers):
        return client.post("/tool/probe_view_base", json={}, headers={**key, **headers}).json()["result"]["base"]
    assert base({}) is None, "http://testserver"
    assert base({"X-Forwarded-Proto": "https"}) == "https://testserver"
    assert base({"Host": "localhost:8000"}) == "http://localhost:8000"
    assert base({"Host": "desk.example.com", "X-Forwarded-Proto": "https"}) == "https://desk.example.com"
    assert base({"Host": "evil.example.com/x", "X-Forwarded-Proto": "https"}) is None
    monkeypatch.setattr(A, "_ALLOWED_HOSTS", ["desk.example.com", "localhost:*"])
    assert base({"Host": "desk.example.com", "X-Forwarded-Proto": "https"}) == "https://desk.example.com"
    assert base({"Host": "localhost:8000"}) == "http://localhost:8000"
    assert base({"Host": "other.example.com", "X-Forwarded-Proto": "https"}) is None, "不在 Host 白名单里的不拿来拼链接"
    assert viewpage.REQUEST_BASE.get() is None, "请求结束就清掉, 不留到下一个请求"


def test_mcp_tool_calls_see_the_request_address_too(monkeypatch):
    """MCP 那条路上工具跑在 SDK 另起的任务和线程里: 这次请求的地址(和调用者一样)得跟得过去。另起一份 MCP 子应用
    (SDK 的会话管理每份只能起一次, 全局那份留给 tests/test_deskcore_cors.py), 地址照 auth_middleware 的做法放进去。"""
    pytest.importorskip("mcp")
    from starlette.requests import Request
    monkeypatch.delenv("DESKCORE_PUBLIC_URL", raising=False)
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    seen = []
    monkeypatch.setattr(core, "sb", lambda: None)
    monkeypatch.setattr(core, "list_projects", lambda *_a, **_k: seen.append(viewpage.public_base()) or [])
    inner = A._register_mcp().streamable_http_app()

    async def as_request(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        token = viewpage.REQUEST_BASE.set(A._request_base(Request(scope)))
        try:
            await inner(scope, receive, send)
        finally:
            viewpage.REQUEST_BASE.reset(token)
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_projects", "arguments": {}}}
    with fastapi_testclient.TestClient(as_request, raise_server_exceptions=False) as live:
        for extra in ({"Host": "localhost:8000"}, {}, {"X-Forwarded-Proto": "https"}):
            r = live.post("/", headers={**headers, **extra}, json=call)
            assert r.status_code == 200 and not r.json()["result"].get("isError"), r.text
    assert seen == ["http://localhost:8000", None, "https://testserver"]
