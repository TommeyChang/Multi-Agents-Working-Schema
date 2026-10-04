"""`commit_gate.py` 的判据测试——**界内闸必须可测，否则等于没闸**。

测试对象是"改动集对白名单／不动清单"的判定，故不 mock 文件系统：
每个用例都建**真的 git 仓库**（`tmp_path` 下、`git -c user.email=...` 提交）与
**真的协调器状态**（经 `bg_coordinator.cli.main` 驱动 register／claim-analyze／define）。
只 mock 掉"环境"（git 缺失即跳过），不 mock 掉"判据"。

一条纪律单独钉住：**本工具只读**——跑完之后协调器状态根的文件集与 mtime 必须
逐字节不变（`test_run_leaves_state_root_untouched`）。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from bg_coordinator.cli import main as cli_main
from bg_coordinator.models import Kind, Task

_SPEC = importlib.util.spec_from_file_location(
    "commit_gate", Path(__file__).resolve().parents[1] / "tools" / "commit_gate.py"
)
assert _SPEC and _SPEC.loader
cg = importlib.util.module_from_spec(_SPEC)
sys.modules["commit_gate"] = cg
_SPEC.loader.exec_module(cg)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="未安装 git")


# ---------------------------------------------------------------------------
# 夹具：真 git 仓库 ＋ 真协调器状态
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    """跑 git，强制带上身份——不依赖、也不写任何全局配置。"""
    proc = subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test Runner", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"git {' '.join(args)} 失败：{proc.stderr}"
    return proc.stdout


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _stage(repo: Path, rel: str, text: str = "x = 1\n") -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(repo, "add", "--", rel)


def _cli(root: Path, *argv: str) -> tuple[int, str, str]:
    """经协调器唯一写入口造状态；返回 (退出码, stdout, stderr)。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main(["--root", str(root), *argv])
    return code, out.getvalue(), err.getvalue()


def _register(root: Path, *, line: str = "D", title: str = "界内闸样例") -> str:
    code, out, err = _cli(
        root,
        "--json",
        "register",
        "--role",
        "po:po",
        "--title",
        title,
        "--line",
        line,
        "--priority",
        "P1",
    )
    assert code == 0, err
    last = [ln for ln in out.splitlines() if ln.strip()][-1]
    return str(json.loads(last)["detail"]["id"])


def _define(root: Path, tid: str, *, whitelist: list[str], frozen: list[str]) -> None:
    argv = ["define", "--id", tid, "--role", "pm:pm-D:D"]
    for item in whitelist:
        argv += ["--whitelist", item]
    for item in frozen:
        argv += ["--frozen", item]
    argv += ["--acceptance", "test:pytest"]
    code, _, err = _cli(root, *argv)
    assert code == 0, err


def _new_task(root: Path, *, whitelist: list[str], frozen: list[str]) -> str:
    """建一个**已定稿**的条目，返回协调器分配的编号。"""
    assert _cli(root, "init")[0] == 0
    tid = _register(root)
    assert _cli(root, "claim-analyze", "--id", tid, "--role", "pm:pm-D:D")[0] == 0
    _define(root, tid, whitelist=whitelist, frozen=frozen)
    return tid


def _run(root: Path, tid: str, repo: Path, *extra: str) -> int:
    return cg.main(["--task", tid, "--root", str(root), "--repo", str(repo), *extra])


def _tree_snapshot(root: Path) -> dict[str, int]:
    """状态根的「文件集 ＋ mtime」快照——只读判定的证据面。"""
    return {str(p.relative_to(root)): p.stat().st_mtime_ns for p in sorted(root.rglob("*"))}


# ---------------------------------------------------------------------------
# 四条判据
# ---------------------------------------------------------------------------


def test_staged_file_outside_whitelist_blocks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """**越界即拦**：不在白名单里的暂存件 BLOCK，退出码 1。"""
    root = tmp_path / "coordinator"
    tid = _new_task(root, whitelist=["data_access/**"], frozen=["main.py"])
    repo = _init_repo(tmp_path)
    _stage(repo, "auth/dependencies.py")

    code = _run(root, tid, repo, "--staged")
    out = capsys.readouterr().out
    assert code == 1
    assert "BLOCK auth/dependencies.py" in out
    assert "白名单" in out
    assert "拒绝提交" in out


def test_frozen_file_blocks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """**不动清单独立生效**：即便文件在白名单内（实为笔误），frozen 仍拦。"""
    root = tmp_path / "coordinator"
    tid = _new_task(root, whitelist=["data_access/**", "main.py"], frozen=["main.py"])
    repo = _init_repo(tmp_path)
    _stage(repo, "main.py")

    code = _run(root, tid, repo)
    out = capsys.readouterr().out
    assert code == 1
    assert "BLOCK main.py" in out
    assert "不动清单" in out


def test_in_whitelist_change_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """界内改动**零发现**——WARN 也没有，退出码 0。"""
    root = tmp_path / "coordinator"
    tid = _new_task(root, whitelist=["data_access/**"], frozen=["main.py"])
    repo = _init_repo(tmp_path)
    _stage(repo, "data_access/kline/follow.py")

    code = _run(root, tid, repo)
    out = capsys.readouterr().out
    assert code == 0
    assert not any(ln.startswith(("BLOCK ", "WARN ")) for ln in out.splitlines())
    assert "放行" in out


def test_design_surface_without_doc_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """**改设计面必须同批改文档**——只告警不阻断（退出码仍 0）。"""
    root = tmp_path / "coordinator"
    tid = _new_task(root, whitelist=["data_access/**"], frozen=[])
    repo = _init_repo(tmp_path)
    _stage(repo, "data_access/ports.py")  # ports.py ⇒ touches_design_surface

    code = _run(root, tid, repo)
    out = capsys.readouterr().out
    assert code == 0
    assert "WARN data_access/ports.py" in out
    assert "设计面" in out
    assert not any(ln.startswith("BLOCK ") for ln in out.splitlines())


def test_design_surface_with_doc_does_not_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """**成对修订即静默**：同批带了 docs/ ⇒ 不再告警。"""
    root = tmp_path / "coordinator"
    tid = _new_task(root, whitelist=["data_access/**", "docs/**"], frozen=[])
    repo = _init_repo(tmp_path)
    _stage(repo, "data_access/contract.py")
    _stage(repo, "docs/CONTRACT.md")

    code = _run(root, tid, repo)
    out = capsys.readouterr().out
    assert code == 0
    assert not any(ln.startswith("WARN ") for ln in out.splitlines())


def test_empty_whitelist_warns_but_does_not_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """**无白名单不成条目**：判不了就留痕（WARN），不把"没登记"误判成"全越界"。"""
    root = tmp_path / "coordinator"
    assert _cli(root, "init")[0] == 0
    tid = _register(root, title="未定稿")  # 只 register，whitelist 为空
    repo = _init_repo(tmp_path)
    _stage(repo, "anything.py")

    code = _run(root, tid, repo)
    out = capsys.readouterr().out
    assert code == 0
    assert "白名单为空" in out
    assert not any(ln.startswith("BLOCK ") for ln in out.splitlines())


# ---------------------------------------------------------------------------
# 用法／环境错误（退出码 2，错误只走 stderr）
# ---------------------------------------------------------------------------


def test_unknown_task_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "coordinator"
    _new_task(root, whitelist=["data_access/**"], frozen=[])
    repo = _init_repo(tmp_path)
    _stage(repo, "data_access/kline/follow.py")

    code = _run(root, "T-D-9999", repo)
    captured = capsys.readouterr()
    assert code == 2
    assert "不存在" in captured.err
    assert captured.out == ""  # 错误路径 stdout 必须干净


def test_not_a_git_repo_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "coordinator"
    tid = _new_task(root, whitelist=["data_access/**"], frozen=[])
    plain = tmp_path / "plain"
    plain.mkdir()

    code = _run(root, tid, plain)
    captured = capsys.readouterr()
    assert code == 2
    assert "git 仓库" in captured.err
    assert captured.out == ""


def test_staged_and_range_conflict_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "coordinator"
    tid = _new_task(root, whitelist=["data_access/**"], frozen=[])
    repo = _init_repo(tmp_path)

    code = _run(root, tid, repo, "--staged", "--range", "HEAD~1..HEAD")
    captured = capsys.readouterr()
    assert code == 2
    assert "互斥" in captured.err


def test_missing_root_and_env_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """**绝不猜默认根**：既无 `--root` 又无环境变量 ⇒ 拒绝运行。"""
    monkeypatch.delenv("BG_COORDINATOR_ROOT", raising=False)
    repo = _init_repo(tmp_path)

    code = cg.main(["--task", "T-D-1", "--repo", str(repo)])
    captured = capsys.readouterr()
    assert code == 2
    assert "BG_COORDINATOR_ROOT" in captured.err
    assert captured.out == ""


def test_root_falls_back_to_env(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "coordinator"
    tid = _new_task(root, whitelist=["data_access/**"], frozen=[])
    repo = _init_repo(tmp_path)
    _stage(repo, "data_access/kline/follow.py")
    monkeypatch.setenv("BG_COORDINATOR_ROOT", str(root))

    code = cg.main(["--task", tid, "--repo", str(repo), "--staged"])
    capsys.readouterr()
    assert code == 0


# ---------------------------------------------------------------------------
# --range 与 --json
# ---------------------------------------------------------------------------


def test_range_diff_is_judged(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`--range` 判的是区间改动集，不是暂存区。"""
    root = tmp_path / "coordinator"
    tid = _new_task(root, whitelist=["data_access/**"], frozen=[])
    repo = _init_repo(tmp_path)
    _stage(repo, "data_access/base.py")
    _git(repo, "commit", "-q", "-m", "base")
    _stage(repo, "auth/evil.py")
    _git(repo, "commit", "-q", "-m", "evil")

    code = _run(root, tid, repo, "--range", "HEAD~1..HEAD")
    out = capsys.readouterr().out
    assert code == 1
    assert "BLOCK auth/evil.py" in out


def test_json_payload_shape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`--json` 的键固定，且 `ok` 与 BLOCK 一致。"""
    root = tmp_path / "coordinator"
    tid = _new_task(root, whitelist=["data_access/**"], frozen=["main.py"])
    repo = _init_repo(tmp_path)
    _stage(repo, "auth/evil.py")

    code = _run(root, tid, repo, "--json")
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert set(payload) == {"task", "blocked", "warns", "files", "ok"}
    assert payload["task"] == tid
    assert payload["ok"] is False
    assert payload["files"] == ["auth/evil.py"]
    assert payload["blocked"][0]["path"] == "auth/evil.py"
    assert payload["blocked"][0]["reason"]


def test_json_error_payload_on_unknown_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """错误路径带 `--json` 时 stdout 出 `ok=false`；错误文案仍在 stderr。"""
    root = tmp_path / "coordinator"
    _new_task(root, whitelist=["data_access/**"], frozen=[])
    repo = _init_repo(tmp_path)

    code = _run(root, "T-D-404", repo, "--json")
    captured = capsys.readouterr()
    assert code == 2
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["task"] == "T-D-404"
    assert payload["blocked"] == []
    assert captured.err


# ---------------------------------------------------------------------------
# 只读性：**跑完什么都不许变**
# ---------------------------------------------------------------------------


def test_run_leaves_state_root_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """过与不过两条路径都不得写盘、不得改 mtime——本工具是只读闸。"""
    root = tmp_path / "coordinator"
    tid = _new_task(root, whitelist=["data_access/**"], frozen=["main.py"])
    repo = _init_repo(tmp_path)

    _stage(repo, "data_access/kline/follow.py")
    before = _tree_snapshot(root)
    assert _run(root, tid, repo) == 0
    assert _tree_snapshot(root) == before

    _stage(repo, "auth/evil.py")
    assert _run(root, tid, repo) == 1
    capsys.readouterr()
    assert _tree_snapshot(root) == before


# ---------------------------------------------------------------------------
# 纯函数判据（不经 CLI，直接钉住优先级与边界）
# ---------------------------------------------------------------------------


def test_judge_frozen_beats_whitelist() -> None:
    task = Task(
        id="T-D-1",
        kind=Kind.T,
        line="D",
        whitelist=["main.py", "a/**"],
        frozen=["main.py"],
    )
    verdict = cg.judge("T-D-1", task, ["main.py"])
    assert [f.path for f in verdict.blocked] == ["main.py"]
    assert "不动清单" in verdict.blocked[0].reason


def test_judge_empty_whitelist_still_checks_frozen() -> None:
    """白名单为空只免掉"覆盖性"判定，不动清单**照判**。"""
    task = Task(id="T-D-1", kind=Kind.T, line="D", whitelist=[], frozen=["main.py"])
    verdict = cg.judge("T-D-1", task, ["main.py", "other.py"])
    assert [f.path for f in verdict.blocked] == ["main.py"]
    assert [w.path for w in verdict.warns] == ["T-D-1"]


def test_judge_deduplicates_and_sorts_files() -> None:
    task = Task(id="T-D-1", kind=Kind.T, line="D", whitelist=["a/**"], frozen=[])
    verdict = cg.judge("T-D-1", task, ["a/b.py", "a/a.py", "a/b.py"])
    assert verdict.files == ["a/a.py", "a/b.py"]


# ---------------------------------------------------------------------------
# 迁移腿：**提交时就收口**（放号只保证号唯一，保证不了链位唯一）
# ---------------------------------------------------------------------------


def _bind_migrations(tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, vdir: str) -> None:
    """给这个临时仓挂一份"只声明迁移目录"的绑定。

    绑定解析顺序是**主干侧优先 → 体系侧兜底**；这里用体系侧那份（把 `bindings_dir`
    指到 tmp），从而不碰任何真实文件。
    """
    import bg_coordinator.binding as b

    side = tmp_path / "bindings"
    side.mkdir(exist_ok=True)
    (side / f"{repo.name}.md").write_text(
        "# 绑定\n\n```json\n"
        + json.dumps(
            {
                "project": repo.name,
                "version": 1,
                "lines": {"D": {"name": "d", "workface": ["data_access/"]}},
                "gate": {"entry": "tools/gate.py"},
                "migrations": {"dir": vdir, "base": "base"},
                "tests": {"db_marker": "db", "db_fixtures": ["db_session"]},
                "protected_assets": ["x"],
                "scratch_namespace": "bg_",
                "window": ["起服"],
                "shared_files": ["todo/**"],
            },
            ensure_ascii=False,
        )
        + "\n```\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(b, "bindings_dir", lambda: side)


def _base_chain(repo: Path, vdir: str, revisions: list[tuple[str, str | None]]) -> None:
    """在基线上造一条链，并打上 `base` 标签（绑定里声明的基线名）。"""
    for rev, down in revisions:
        down_lit = "None" if down is None else f'"{down}"'
        _stage(repo, f"{vdir}/{rev}_m.py", f'revision = "{rev}"\ndown_revision = {down_lit}\n')
    _git(repo, "commit", "-q", "-m", "base chain")
    _git(repo, "tag", "base")


def test_migration_leg_blocks_forked_chain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """**分叉必拦**：分支新增件的父节点不是基线 head ⇒ 提交时就拒。

    这正是 DBA 冲突的形态：两个件各自合法，合起来是两个 head。
    """
    repo = _init_repo(tmp_path)
    vdir = "alembic/versions"
    _base_chain(repo, vdir, [("0001", None), ("0002", "0001")])
    _stage(repo, f"{vdir}/0003_new.py", 'revision = "0003"\ndown_revision = "0001"\n')  # 接到旧 head
    root = tmp_path / "coord"
    tid = _new_task(root, whitelist=["alembic/**"], frozen=[])
    _bind_migrations(tmp_path, repo, monkeypatch, vdir)

    rc = _run(root, tid, repo)
    out = capsys.readouterr()
    assert rc == 1, out
    assert "迁移链" in out.out, out.out
    assert "分叉" in out.out or "父节点" in out.out, out.out


def test_migration_leg_passes_clean_chain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """负例锚点：干净链不许误报——否则这条腿会被当噪声绕过。"""
    repo = _init_repo(tmp_path)
    vdir = "alembic/versions"
    _base_chain(repo, vdir, [("0001", None), ("0002", "0001")])
    _stage(repo, f"{vdir}/0003_new.py", 'revision = "0003"\ndown_revision = "0002"\n')
    root = tmp_path / "coord"
    tid = _new_task(root, whitelist=["alembic/**"], frozen=[])
    _bind_migrations(tmp_path, repo, monkeypatch, vdir)

    rc = _run(root, tid, repo)
    out = capsys.readouterr()
    assert rc == 0, out
    assert "迁移链" not in out.out


def test_migration_leg_not_applicable_without_declaration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """绑定没声明迁移目录 ⇒ 该工程没有"迁移件"这类改动 ⇒ 不适用（不拦也不假装判过）。"""
    repo = _init_repo(tmp_path)
    _stage(repo, "alembic/versions/0001_x.py", 'revision = "0001"\ndown_revision = None\n')
    root = tmp_path / "coord"
    tid = _new_task(root, whitelist=["alembic/**"], frozen=[])
    import bg_coordinator.binding as b

    monkeypatch.setattr(b, "bindings_dir", lambda: tmp_path / "no-such-bindings")

    rc = _run(root, tid, repo)
    assert rc == 0, capsys.readouterr()


def test_migration_leg_blocks_landed_rewrite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """**已落库件被就地改写 ⇒ 提交时就拒**（判据 [6]，与合并闸同源）。

    为什么必须前移到提交：这件一旦进了分支，评审、窗口、推送都排在后面——
    等合并才发现，改的是"已执行过的历史"，那批 DDL 永远不会生效。
    """
    repo = _init_repo(tmp_path)
    vdir = "alembic/versions"
    _base_chain(repo, vdir, [("0001", None), ("0002", "0001")])
    _stage(  # 已落库的 0001：改写正文（基线那份只有 pass）
        repo,
        f"{vdir}/0001_m.py",
        'revision = "0001"\ndown_revision = None\n\n\ndef upgrade():\n    op.add_column("t", "x")\n',
    )
    root = tmp_path / "coord"
    tid = _new_task(root, whitelist=["alembic/**"], frozen=[])
    _bind_migrations(tmp_path, repo, monkeypatch, vdir)

    rc = _run(root, tid, repo)
    out = capsys.readouterr()
    assert rc == 1, out
    assert "就地改写" in out.out, out.out
    assert "另开下一 revision" in out.out, "没给出正当路径"


def test_migration_leg_warns_on_comment_only_correction(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """**允许的例外**：剥掉 docstring 后 AST 等价 ⇒ 放行（WARN 留痕，不拦）。

    基线件得先有本体（函数 ＋ docstring），否则"加了个函数"是**真**语义改动，
    例外判据本来就不该放行——本例测的是注释与 docstring 级的更正。
    """
    repo = _init_repo(tmp_path)
    vdir = "alembic/versions"
    body = 'revision = "0001"\ndown_revision = None\n\n\ndef upgrade():\n    pass\n'
    _stage(repo, f"{vdir}/0001_m.py", body)
    _git(repo, "commit", "-q", "-m", "base chain")
    _git(repo, "tag", "base")
    _stage(
        repo,
        f"{vdir}/0001_m.py",
        "# 更正：陈旧注释\n"
        + body.replace(
            "def upgrade():\n    pass\n",
            'def upgrade():\n    """更正后的 docstring。"""\n    pass\n',
        ),
    )
    root = tmp_path / "coord"
    tid = _new_task(root, whitelist=["alembic/**"], frozen=[])
    _bind_migrations(tmp_path, repo, monkeypatch, vdir)

    rc = _run(root, tid, repo)
    out = capsys.readouterr()
    assert rc == 0, out
    assert "WARN" in out.out and "例外" in out.out, out.out


# ---------------------------------------------------------------------------
# 条目级约束：**把"本条的硬要求"核成事实**
# ---------------------------------------------------------------------------


def test_zero_migration_constraint_blocks_migration_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """声明"零迁移"却动了迁移件 ⇒ BLOCK，并给出**该走的正当路径**。

    这条原本写在条目描述里（"本条判定零迁移，若确需迁移另立 dba 评审＋ops 窗口条目"）——
    是个承诺，靠人记；现在声明进协调器，提交那一刻核。
    """
    repo = _init_repo(tmp_path)
    vdir = "alembic/versions"
    _base_chain(repo, vdir, [("0001", None)])
    _stage(repo, f"{vdir}/0002_new.py", 'revision = "0002"\ndown_revision = "0001"\n')
    root = tmp_path / "coord"
    _bind_migrations(tmp_path, repo, monkeypatch, vdir)

    # **带约束地**定稿（经协调器动词，不手改状态文件）
    assert _cli(root, "init")[0] == 0
    tid = _register(root)
    assert _cli(root, "claim-analyze", "--id", tid, "--role", "pm:pm-D:D")[0] == 0
    assert _cli(
        root, "define", "--id", tid, "--role", "pm:pm-D:D",
        "--whitelist", "alembic/**", "--acceptance", "test:t",
        "--constraint", "zero_migration",
    )[0] == 0

    rc = _run(root, tid, repo)
    out = capsys.readouterr()
    assert rc == 1, out
    assert "零迁移" in out.out
    assert "另立条目" in out.out and "dba 评审" in out.out


def test_unknown_constraint_is_rejected_at_define(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """**没有判据的约束等于没声明** ⇒ 定稿时当场拒。"""
    root = tmp_path / "coord"
    assert _cli(root, "init")[0] == 0
    tid = _register(root)
    assert _cli(root, "claim-analyze", "--id", tid, "--role", "pm:pm-D:D")[0] == 0
    code, _out, err = _cli(
        root, "define", "--id", tid, "--role", "pm:pm-D:D",
        "--whitelist", "a/**", "--acceptance", "test:t", "--constraint", "别乱改",
    )
    capsys.readouterr()
    assert code == 1
    assert "未知约束" in err, err
