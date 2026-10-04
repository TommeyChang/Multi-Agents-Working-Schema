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
