"""W-59: 写作台的「已有评论」只读运营写的评论(TV v1.27 的 source 为空), 没有这一列时照旧。合成数据。"""
import pytest

from deskcore import store


class _Q:
    def __init__(self, sb):
        self.sb, self.only_operator = sb, False

    def schema(self, _):
        return self

    def table(self, _):
        return self

    def select(self, _):
        return self

    def eq(self, *_):
        return self

    def is_(self, key, value):
        assert (key, value) == ("source", "null")
        self.only_operator = True
        return self

    def order(self, _):
        return self

    def limit(self, _):
        return self

    def execute(self):
        self.sb.calls.append(self.only_operator)
        if self.only_operator and self.sb.error:
            raise self.sb.error
        rows = [r for r in self.sb.rows if not (self.only_operator and r.get("source"))]
        return type("R", (), {"data": [{k: v for k, v in r.items() if k != "source"} for r in rows]})()


class _SB:
    def __init__(self, rows, error=None):
        self.rows, self.error, self.calls = rows, error, []

    def schema(self, name):
        return _Q(self).schema(name)


ROWS = [{"comment_id": "c1", "content": "运营写的一条", "source": None},
        {"comment_id": "c2", "content": "读者留的一条", "source": "linkcheck_digest"}]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(store, "_TV_COMMENT_SOURCE_MISSING_AT", 0.0)


def test_only_operator_rows_are_read_when_the_column_exists():
    sb = _SB(ROWS)
    assert [r["comment_id"] for r in store.tv_note_comments(sb, "n1")] == ["c1"]
    assert sb.calls == [True]


def test_missing_column_falls_back_and_is_not_retried_at_once():
    sb = _SB(ROWS[:1], RuntimeError('{"code": "42703", "message": "column comments.source does not exist"}'))
    assert [r["comment_id"] for r in store.tv_note_comments(sb, "n1")] == ["c1"]
    assert sb.calls == [True, False]
    store.tv_note_comments(sb, "n1")
    assert sb.calls == [True, False, False]


def test_it_tries_again_after_the_wait(monkeypatch):
    sb = _SB(ROWS[:1], RuntimeError("42703 column comments.source does not exist"))
    store.tv_note_comments(sb, "n1")
    sb.error, sb.rows = None, ROWS
    monkeypatch.setattr(store, "_TV_COMMENT_SOURCE_MISSING_AT",
                        store._TV_COMMENT_SOURCE_MISSING_AT - store._TV_COMMENT_SOURCE_RETRY_S - 1)
    assert [r["comment_id"] for r in store.tv_note_comments(sb, "n1")] == ["c1"]


def test_other_errors_still_raise():
    with pytest.raises(RuntimeError):
        store.tv_note_comments(_SB(ROWS, RuntimeError("db down")), "n1")
