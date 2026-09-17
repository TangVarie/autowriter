"""CLI 那一层: `ingest` 和 `sync-skill --check` 真的接上了 core。

单测 core.ingest_published 证明不了 argparse 那层把参数传对了 —— 09-17 之前
`backfill` 就有过"CLI 子命令和文档都在, 函数根本没写"的先例(见
core.backfill_fingerprints 的说明)。
"""

from __future__ import annotations

import pytest

from deskcore import cli, core
from tests.fakes import FakeClient

openpyxl = pytest.importorskip("openpyxl")

ME = "11111111-1111-1111-1111-111111111111"
PROJ = "aaaaaaaa-0000-0000-0000-000000000001"


@pytest.fixture
def fake(monkeypatch):
    c = FakeClient(rows={"projects": [
        {"id": PROJ, "name": "途鸽", "brand": "途鸽", "owner_id": ME,
         "calibration_notes": "", "tactics": "[]", "custom_roles": []}]})
    monkeypatch.setattr(core, "sb", lambda: c)
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    return c


def _sheet(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["内容", "_source_autowriter_version_id"])
    ws.append(["标题：已入库的\n\n正文：有 version_id", "dddd0000-0000-0000-0000-00000000000a"])
    ws.append(["标题：没入库的一\n\n正文：" + "正文一。" * 10, None])
    ws.append(["标题：没入库的二\n\n正文：" + "正文二。" * 10, ""])
    p = tmp_path / "途鸽-9月.xlsx"
    wb.save(p)
    return p


def test_ingest_dry_run_parses_and_writes_nothing(fake, tmp_path, capsys):
    p = _sheet(tmp_path)
    rc = cli.main(["ingest", "--project", PROJ, "--user", ME, "--xlsx", str(p), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "解析到 2 条" in out and "跳过 1 条" in out
    assert "--dry-run" in out
    assert not fake.rows.get("items") and not fake.rows.get("draft_fingerprints")


def test_ingest_writes_identity_and_fingerprints_with_the_file_as_source(fake, tmp_path, capsys):
    p = _sheet(tmp_path)
    rc = cli.main(["ingest", "--project", PROJ, "--user", ME, "--xlsx", str(p)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "建身份 2/2" in out and "写指纹 2" in out
    assert fake.rows["batches"][0]["params"] == {"source": "ingest", "file": "途鸽-9月.xlsx"}
    assert all("external_source" not in i for i in fake.rows["items"])
    assert len(fake.rows["draft_fingerprints"]) == 2


def test_ingest_source_flag_overrides_the_filename(fake, tmp_path):
    p = _sheet(tmp_path)
    cli.main(["ingest", "--project", PROJ, "--user", ME, "--xlsx", str(p),
              "--source", "飞书·途鸽·第 3 周"])
    assert fake.rows["batches"][0]["params"]["file"] == "飞书·途鸽·第 3 周"


def test_sync_skill_check_passes_on_a_synced_tree(capsys):
    assert cli.main(["sync-skill", "--check"]) == 0
    assert "一致" in capsys.readouterr().out


def test_sync_skill_check_fails_when_the_skill_is_stale(monkeypatch, tmp_path, capsys):
    from deskcore import tools as T
    stale = tmp_path / "SKILL.md"
    stale.write_text("---\nname: x\n---\n\n# 头\n\n<!-- protocol_version: 000000000000 -->\n\n旧正文\n",
                     encoding="utf-8")
    monkeypatch.setattr(T, "SKILL_PATH", stale)
    assert cli.main(["sync-skill", "--check"]) == 1
    assert "不一致" in capsys.readouterr().out
    # 不带 --check 就真写, 写完再 check 就一致
    assert cli.main(["sync-skill"]) == 0
    assert cli.main(["sync-skill", "--check"]) == 0
    assert "旧正文" not in stale.read_text(encoding="utf-8")
