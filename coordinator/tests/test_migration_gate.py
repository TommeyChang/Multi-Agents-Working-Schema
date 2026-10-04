"""迁移闸测试——**判据要有齿**：坏图必须被逮住，好图不许误报。

判据本身在 `bg_coordinator/migrations.py`（系统模块）——**三个收口点共用同一处口径**：
放号（协调器号段）、提交（`tools/commit_gate.py`）、合并（`merge.py` 的链位闸）。
这里既测判据，也测外壳（`tools/migration_gate.py`）能跑。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="未安装 git")

_SPEC = importlib.util.spec_from_file_location(
    "migration_gate", Path(__file__).resolve().parents[1] / "tools" / "migration_gate.py"
)
assert _SPEC and _SPEC.loader
mg = importlib.util.module_from_spec(_SPEC)
sys.modules["migration_gate"] = mg
_SPEC.loader.exec_module(mg)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bg_coordinator import migrations as mig  # noqa: E402

#: 判据在系统模块；`mg` 只是外壳——测试直接测判据，外壳另有一条
judge = mig.judge
heads_of = mig.heads_of


def load_migrations(repo: Path) -> list[mig.Migration]:
    """测试用的便捷包装：迁移目录固定为约定值（正文只关心图，不关心目录）。"""
    return mig.load_migrations(repo, "alembic/versions")


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
    rows = load_migrations(repo)
    assert [r.revision for r in rows] == ["0001", "0002", "0003"]
    assert judge(rows, []) == []
    assert heads_of(rows) == ["0003"]


def test_duplicate_revision_is_blocked(tmp_path: Path) -> None:
    """**撞号**必须被逮住——撞号之后 downgrade／定位全是掷骰子。"""
    repo = tmp_path / "repo"
    _chain(repo, 2)
    _migration(repo, "0099_dup.py", "0002", "0001")
    blocks = judge(load_migrations(repo), [])
    assert any("撞号" in b for b in blocks)


def test_dangling_parent_is_blocked(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _migration(repo, "0001_a.py", "0001", "9999")
    blocks = judge(load_migrations(repo), [])
    assert any("悬空父节点" in b for b in blocks)


def test_two_heads_are_blocked(tmp_path: Path) -> None:
    """两个 head ⇒ 谁也不知道该升到哪个。"""
    repo = tmp_path / "repo"
    _migration(repo, "0001_a.py", "0001", None)
    _migration(repo, "0002_b.py", "0002", "0001")
    _migration(repo, "0003_c.py", "0003", "0001")
    blocks = judge(load_migrations(repo), [])
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
    rows = load_migrations(repo)
    assert heads_of(rows) == ["0002"], "先确认只有一个 head，否则测的不是可达性"
    blocks = judge(rows, [])
    assert any("不可达" in b for b in blocks)


def test_new_migration_must_follow_base_head(tmp_path: Path) -> None:
    """新增件取号必须**大于基线最大号**——插队是事故（尤其是"让号"留下的空洞被复用）。

    基线故意做成有空洞（0001→0003，模拟 0002 曾让号）：新件若填 0002，
    号虽没撞车，**却把已作废的号复活了**——[5] 必须逮住。
    """
    repo = tmp_path / "repo"
    _migration(repo, "0001_a.py", "0001", None)
    _migration(repo, "0003_c.py", "0003", "0001")
    base_rows = load_migrations(repo)
    # 合法新增：号大于基线最大号且父节点等于基线 head
    _migration(repo, "0004_ok.py", "0004", "0003")
    rows = load_migrations(repo)
    assert [b for b in judge(rows, base_rows) if b.startswith("[5]")] == []
    # 插队：填进基线的空洞号
    _migration(repo, "0002_late.py", "0002", "0003")
    blocks = judge(load_migrations(repo), base_rows)
    assert any("插队" in b for b in blocks)


def test_new_migration_from_wrong_parent_is_blocked(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _chain(repo, 2)
    base_rows = load_migrations(repo)
    _migration(repo, "0005_fork.py", "0005", "0001")  # 父节点不是基线 head
    blocks = judge(load_migrations(repo), base_rows)
    assert any("重新分叉" in b for b in blocks)


def test_tuple_down_revision_is_parsed(tmp_path: Path) -> None:
    """合并迁移件（元组父节点）不许被当成"无父"——那是把真 head 认错。"""
    repo = tmp_path / "repo"
    d = repo / "alembic" / "versions"
    d.mkdir(parents=True)
    (d / "0006_merge.py").write_text(
        'revision = "0006"\ndown_revision = ("0004", "0005")\n', encoding="utf-8"
    )
    rows = load_migrations(repo)
    assert rows[0].downs == ("0004", "0005")
    assert heads_of(rows) == ["0006"]


def test_empty_versions_dir_is_blocked(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "alembic" / "versions").mkdir(parents=True)
    assert judge([], []) != []


def test_missing_versions_dir_is_a_clean_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = mg.main(["--target", str(repo), "--json"])
    assert rc == 1  # 没有迁移件 ⇒ BLOCK（闸的意义就是"没图不许合"）
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False


# ---------------------------------------------------------------------------
# [6] 已落库件不得就地改写／删除——**真 git 仓**上的控制例
#
# 为什么用真仓：判据比的是 git 对象事实（基线树／分叉点树／工作区三份），
# 拿假 dict 去喂它测的就不是它。控制例成对出现：**该红的必红、不该红的必不红**——
# 后者更重要：这条闸的假红形态（把"落后"当"改写"）在主仓那边打过两轮补丁。
# ---------------------------------------------------------------------------

VD = "alembic/versions"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=Test", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"git {' '.join(args)} 失败：{proc.stderr}"
    return proc.stdout


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _put(repo: Path, name: str, rev: str, down: str | None, *, body: str = "    pass\n") -> None:
    d = repo / VD
    d.mkdir(parents=True, exist_ok=True)
    down_lit = "None" if down is None else f'"{down}"'
    (d / name).write_text(
        f'revision = "{rev}"\ndown_revision = {down_lit}\n\n\ndef upgrade():\n{body}',
        encoding="utf-8",
    )


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


def _gate(repo: Path, base: str) -> tuple[list[str], list[str]]:
    """照三个收口点的接法跑一遍：判据在系统模块，外壳只接线。"""
    rows = mig.load_migrations(repo, VD)
    base_rows, err = mig.load_from_git(repo, base, VD)
    assert not err, err
    landed = mig.load_landed(repo, base, "HEAD", VD) if base_rows else None
    return mig.judge(rows, base_rows, VD, landed=landed), mig.landed_notes(rows, base_rows, landed)


def _blocks(repo: Path, base: str) -> list[str]:
    return [b for b in _gate(repo, base)[0] if "[6]" in b]


def _base_commit(repo: Path, revs: list[tuple[str, str | None]]) -> None:
    for rev, down in revs:
        _put(repo, f"{rev}_step.py", rev, down)
    _commit(repo, "base chain")
    _git(repo, "tag", "base")


def test_landed_rewrite_is_blocked_before_commit(tmp_path: Path) -> None:
    """**就地改写**：已落库件的内容变了（还没提交也一样）⇒ BLOCK。"""
    repo = _git_repo(tmp_path)
    _base_commit(repo, [("0001", None)])
    _put(repo, "0001_step.py", "0001", None, body="    op.add_column('t', 'x')\n")
    blocks = _blocks(repo, "base")
    assert any("就地改写" in b and "0001_step.py" in b for b in blocks), blocks


def test_rewrite_in_earlier_commit_is_still_blocked(tmp_path: Path) -> None:
    """判据比的是**状态**：本分支早先提交里的改写也报（区间判据只看得见最后一次提交）。"""
    repo = _git_repo(tmp_path)
    _base_commit(repo, [("0001", None)])
    _put(repo, "0001_step.py", "0001", None, body="    op.drop_column('t', 'x')\n")
    _commit(repo, "偷偷就地改写")
    assert _blocks(repo, "base"), "早先提交里的改写没被报出来"


def test_reparenting_a_landed_file_is_blocked(tmp_path: Path) -> None:
    """只改 `down_revision`（不动 DDL）也必须拦——主仓按 DDL 标记扫的判据看不见这一类。"""
    repo = _git_repo(tmp_path)
    _base_commit(repo, [("0001", None), ("0002", "0001")])
    _put(repo, "0002_step.py", "0002", None)  # 把已落库件的父节点摘掉
    assert _blocks(repo, "base"), "改已落库件的父节点没被拦住"


def test_deleting_a_landed_file_is_blocked(tmp_path: Path) -> None:
    """**删／改名历史件**也从同一个判据出来（链上别人的父节点会悬空）。"""
    repo = _git_repo(tmp_path)
    _base_commit(repo, [("0001", None), ("0002", "0001")])
    (repo / VD / "0001_step.py").unlink()
    blocks = _blocks(repo, "base")
    assert any("没了" in b and "0001_step.py" in b for b in blocks), blocks


def test_behind_branch_is_not_blocked(tmp_path: Path) -> None:
    """**落后不是改写**：基线新增的件本分支还没有 ⇒ 不许报（这是误杀的主形态）。"""
    repo = _git_repo(tmp_path)
    _base_commit(repo, [("0001", None)])
    _git(repo, "branch", "feat")
    _put(repo, "0002_step.py", "0002", "0001")  # main 单侧前进
    _commit(repo, "main: 落地 0002")
    _git(repo, "checkout", "-q", "feat")  # 本分支停在分叉点
    assert _blocks(repo, "main") == []


def test_main_side_rewrite_does_not_false_red_a_behind_branch(tmp_path: Path) -> None:
    """**基线自己动了这件、本分支手里还是分叉点那份** ⇒ 是"落后"，不是本分支改写。

    这是三份对照（分叉点／基线／工作区）存在的唯一理由：两两比都会在这里误杀。
    """
    repo = _git_repo(tmp_path)
    _base_commit(repo, [("0001", None)])
    _git(repo, "branch", "feat")
    _put(repo, "0001_step.py", "0001", None, body="    op.add_column('t', 'x')\n")
    _commit(repo, "main: 有人就地改了 0001")  # 违规发生在基线侧
    _git(repo, "checkout", "-q", "feat")
    assert _blocks(repo, "main") == [], "把基线的改动算到本分支头上了"


def test_rewrite_then_merge_main_is_still_blocked(tmp_path: Path) -> None:
    """先改写、再 `merge main` ⇒ 仍须红。

    主仓 T-D-157 实证过：只加"内容面"豁免会在这里留**漏报洞**——本分支的改写
    在合并后仍与基线不同，判据必须看得见它。
    """
    repo = _git_repo(tmp_path)
    _base_commit(repo, [("0001", None)])
    _git(repo, "branch", "feat")
    _put(repo, "0002_step.py", "0002", "0001")
    _commit(repo, "main: 落地 0002")
    _git(repo, "checkout", "-q", "feat")
    _put(repo, "0001_step.py", "0001", None, body="    op.add_column('t', 'x')\n")
    _commit(repo, "feat: 就地改写 0001")
    _git(repo, "merge", "-q", "--no-edit", "main")
    assert _blocks(repo, "main"), "改写后并入 main，改写被漏掉了"


def test_comment_only_correction_is_exempt_but_noted(tmp_path: Path) -> None:
    """**允许的例外**：剥掉 docstring 后 AST 等价（注释／docstring／排版级）⇒ 放行 ＋ 留痕。"""
    repo = _git_repo(tmp_path)
    _base_commit(repo, [("0001", None)])
    d = repo / VD / "0001_step.py"
    d.write_text(
        d.read_text(encoding="utf-8").replace(
            "def upgrade():\n    pass\n",
            "# 更正：这条注释早就陈旧了\n"
            "def upgrade():\n"
            '    """docstring 陈旧，一并更正（无语义改动）。"""\n'
            "    pass\n",
        ),
        encoding="utf-8",
    )
    blocks, notes = _gate(repo, "base")
    assert [b for b in blocks if "[6]" in b] == [], blocks
    assert any("例外" in n for n in notes), notes


def test_syntax_broken_landed_file_is_blocked(tmp_path: Path) -> None:
    """语法坏了 ⇒ 证不出"无语义改动" ⇒ BLOCK（fail closed，不给假绿留门）。"""
    repo = _git_repo(tmp_path)
    _base_commit(repo, [("0001", None)])
    _put(repo, "0001_step.py", "0001", None, body="    op.add_column('t', 'x'\n")  # 少个括号
    assert _blocks(repo, "base"), "坏文件被当成「改不动语义」放行了"


def test_unrelated_history_is_note_not_block(tmp_path: Path) -> None:
    """分叉点不可判（无共同祖先）⇒ **只提示不拦**：判不了归属就不许拿它当罪名。"""
    repo = _git_repo(tmp_path)
    _base_commit(repo, [("0001", None)])
    _git(repo, "checkout", "-q", "--orphan", "other")
    _git(repo, "rm", "-rq", "--cached", ".")
    _put(repo, "0001_step.py", "0001", None, body="    op.add_column('t', 'x')\n")
    _commit(repo, "orphan: 自成一史")
    blocks, notes = _gate(repo, "base")
    assert [b for b in blocks if "[6]" in b] == [], blocks
    assert any("分叉点不可判" in n for n in notes), notes


def test_unlanded_new_file_may_be_reworked(tmp_path: Path) -> None:
    """未落库的新件随便返工——[6] 只管已落库面（合入前唯一窗口，主仓 R-D-90 的教训）。"""
    repo = _git_repo(tmp_path)
    _base_commit(repo, [("0001", None)])
    _put(repo, "0002_step.py", "0002", "0001")
    _put(repo, "0002_step.py", "0002", "0001", body="    op.add_column('t', 'x')\n")
    assert _blocks(repo, "base") == []


def test_cli_blocks_rewrite_and_reports_note_channel(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """外壳面：`--base` 跑真仓 ⇒ 退出 1，且 `--json` 里有 [6] 与 `notes` 两个通道。"""
    repo = _git_repo(tmp_path)
    _base_commit(repo, [("0001", None)])
    _put(repo, "0001_step.py", "0001", None, body="    op.add_column('t', 'x')\n")
    rc = mg.main(["--target", str(repo), "--base", "base", "--json"])
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 1
    assert any("[6]" in b for b in payload["blocked"]), payload
    assert payload["notes"] == []


def test_cli_is_green_on_behind_branch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """外壳面：落后分支跑真仓 ⇒ 退出 0（**误报防线**——假红比没闸更坏）。"""
    repo = _git_repo(tmp_path)
    _base_commit(repo, [("0001", None)])
    _git(repo, "branch", "feat")
    _put(repo, "0002_step.py", "0002", "0001")
    _commit(repo, "main: 落地 0002")
    _git(repo, "checkout", "-q", "feat")
    rc = mg.main(["--target", str(repo), "--base", "main", "--json"])
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0, payload
    assert payload["blocked"] == []


def test_merge_point_checker_blocks_landed_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第三个收口点（合并）也要接线：`cli._migration_checker` 从 **git 树**判。

    合并时主检出停在 main、分支件不在工作区，故这里的"工作区"其实是**分支提交树**——
    对照系里的分叉点也得按那次提交算（`load_landed(repo, base, commit, …)`）。
    """
    import bg_coordinator.binding as b
    from bg_coordinator import cli as cli_mod

    repo = _git_repo(tmp_path)
    _base_commit(repo, [("0001", None)])
    _git(repo, "branch", "feat")
    _git(repo, "checkout", "-q", "feat")
    _put(repo, "0001_step.py", "0001", None, body="    op.add_column('t', 'x')\n")
    _commit(repo, "feat: 就地改写 0001")
    commit = _git(repo, "rev-parse", "HEAD").strip()

    side = tmp_path / "bindings"
    side.mkdir()
    (side / f"{repo.name}.md").write_text(
        "# 绑定\n\n```json\n" + json.dumps({"migrations": {"dir": VD, "base": "main"}}) + "\n```\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(b, "bindings_dir", lambda: side)

    check = cli_mod._migration_checker(repo, commit)
    assert check(["data_access/x.py"], "main") == "", "本批没有迁移件时不该下判"
    reason = check([f"{VD}/0001_step.py"], "main")
    assert "就地改写" in reason, reason


def test_real_repo_has_no_landed_rewrite() -> None:
    """对真目标仓跑 [6]：**现状不许有命中**（0 命中 ＝ 规则此刻被遵守）。"""
    target = Path(__file__).resolve().parents[3] / "futures-broker-gateway"
    if not (target / VD).is_dir():
        pytest.skip("目标仓不在本机")
    rows = mig.load_migrations(target, VD)
    base_rows, err = mig.load_from_git(target, "origin/main", VD)
    if err:
        pytest.skip(f"基线不可用：{err}")
    landed = mig.load_landed(target, "origin/main", "HEAD", VD)
    blocks = mig.judge(rows, base_rows, VD, landed=landed)
    assert [b for b in blocks if "[6]" in b] == [], "真仓出现已落库件被就地改写"
