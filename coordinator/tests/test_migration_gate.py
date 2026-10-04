"""迁移闸测试——**判据要有齿**：坏图必须被逮住，好图不许误报。

判据是本体系自持的（静态读迁移图与号），所以测试也自持：
在 tmp 里造迁移件，不碰真库、不跑迁移。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "migration_gate", Path(__file__).resolve().parents[1] / "tools" / "migration_gate.py"
)
assert _SPEC and _SPEC.loader
mg = importlib.util.module_from_spec(_SPEC)
sys.modules["migration_gate"] = mg
_SPEC.loader.exec_module(mg)


def _migration(repo: Path, name: str, revision: str, down: str | None) -> None:
    d = repo / "alembic" / "versions"
    d.mkdir(parents=True, exist_ok=True)
    down_lit = "None" if down is None else f'"{down}"'
    (d / name).write_text(
        f'revision = "{revision}"\ndown_revision = {down_lit}\n',
        encoding="utf-8",
    )


def _chain(repo: Path, n: int = 3) -> None:
    prev = None
    for i in range(1, n + 1):
        rev = f"{i:04d}"
        _migration(repo, f"{rev}_step.py", rev, prev)
        prev = rev


def test_healthy_chain_passes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _chain(repo, 3)
    rows = mg.load_migrations(repo)
    assert [r.revision for r in rows] == ["0001", "0002", "0003"]
    assert mg.judge(rows, []) == []
    assert mg.heads_of(rows) == ["0003"]


def test_duplicate_revision_is_blocked(tmp_path: Path) -> None:
    """**撞号**必须被逮住——撞号之后 downgrade／定位全是掷骰子。"""
    repo = tmp_path / "repo"
    _chain(repo, 2)
    _migration(repo, "0099_dup.py", "0002", "0001")
    blocks = mg.judge(mg.load_migrations(repo), [])
    assert any("撞号" in b for b in blocks)


def test_dangling_parent_is_blocked(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _migration(repo, "0001_a.py", "0001", "9999")
    blocks = mg.judge(mg.load_migrations(repo), [])
    assert any("悬空父节点" in b for b in blocks)


def test_two_heads_are_blocked(tmp_path: Path) -> None:
    """两个 head ⇒ 谁也不知道该升到哪个。"""
    repo = tmp_path / "repo"
    _migration(repo, "0001_a.py", "0001", None)
    _migration(repo, "0002_b.py", "0002", "0001")
    _migration(repo, "0003_c.py", "0003", "0001")
    blocks = mg.judge(mg.load_migrations(repo), [])
    assert any("head" in b and "2" in b for b in blocks)


def test_orphan_is_blocked(tmp_path: Path) -> None:
    """孤儿件＝那件永远不会被执行（静默失效），必须报出来。

    构造：一条正常链（0001←0002）＋ 一个自成环的小岛（0003↔0004）。
    head 仍只有一个（0002），所以只有"可达性"这条判据能逮住它。
    """
    repo = tmp_path / "repo"
    _migration(repo, "0001_a.py", "0001", None)
    _migration(repo, "0002_b.py", "0002", "0001")
    _migration(repo, "0003_island.py", "0003", "0004")
    _migration(repo, "0004_island.py", "0004", "0003")
    rows = mg.load_migrations(repo)
    assert mg.heads_of(rows) == ["0002"], "先确认只有一个 head，否则测的不是可达性"
    blocks = mg.judge(rows, [])
    assert any("不可达" in b for b in blocks)


def test_new_migration_must_follow_base_head(tmp_path: Path) -> None:
    """新增件取号必须**大于基线最大号**——插队是事故（尤其是"让号"留下的空洞被复用）。

    基线故意做成有空洞（0001→0003，模拟 0002 曾让号）：新件若填 0002，
    号虽没撞车，**却把已作废的号复活了**——[5] 必须逮住。
    """
    repo = tmp_path / "repo"
    _migration(repo, "0001_a.py", "0001", None)
    _migration(repo, "0003_c.py", "0003", "0001")
    base_rows = mg.load_migrations(repo)
    # 合法新增：号大于基线最大号且父节点等于基线 head
    _migration(repo, "0004_ok.py", "0004", "0003")
    rows = mg.load_migrations(repo)
    assert [b for b in mg.judge(rows, base_rows) if b.startswith("[5]")] == []
    # 插队：填进基线的空洞号
    _migration(repo, "0002_late.py", "0002", "0003")
    blocks = mg.judge(mg.load_migrations(repo), base_rows)
    assert any("插队" in b for b in blocks)


def test_new_migration_from_wrong_parent_is_blocked(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _chain(repo, 2)
    base_rows = mg.load_migrations(repo)
    _migration(repo, "0005_fork.py", "0005", "0001")  # 父节点不是基线 head
    blocks = mg.judge(mg.load_migrations(repo), base_rows)
    assert any("重新分叉" in b for b in blocks)


def test_tuple_down_revision_is_parsed(tmp_path: Path) -> None:
    """合并迁移件（元组父节点）不许被当成"无父"——那是把真 head 认错。"""
    repo = tmp_path / "repo"
    d = repo / "alembic" / "versions"
    d.mkdir(parents=True)
    (d / "0006_merge.py").write_text(
        'revision = "0006"\ndown_revision = ("0004", "0005")\n', encoding="utf-8"
    )
    rows = mg.load_migrations(repo)
    assert rows[0].downs == ("0004", "0005")
    assert mg.heads_of(rows) == ["0006"]


def test_empty_versions_dir_is_blocked(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "alembic" / "versions").mkdir(parents=True)
    assert mg.judge([], []) != []


def test_missing_versions_dir_is_a_clean_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = mg.main(["--target", str(repo), "--json"])
    assert rc == 1  # 没有迁移件 ⇒ BLOCK（闸的意义就是"没图不许合"）
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
