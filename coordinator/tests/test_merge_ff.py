"""merge_ff.sh 端到端测试——真起 git 仓，验证「私有 worktree 组树 → 主检出快进」。

为什么不 mock git：脚本的价值全在 git 机制本身（`--no-ff` 组树、`--ff-only` 快进、脏拒绝、
活锁等待）。mock 掉 git 就只能测到参数解析，测不到那套机制是否真的成立。
所有仓都建在 tmp_path 下；身份经 `git -c user.*` 与环境注入，不依赖全局 git 配置，也不需要网络。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "merge_ff.sh"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")

# 隔离全局／系统配置（可能带 gpgsign 等），并显式给出身份——提交不依赖宿主机配置。
_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "tester",
    "GIT_AUTHOR_EMAIL": "tester@example.com",
    "GIT_COMMITTER_NAME": "tester",
    "GIT_COMMITTER_EMAIL": "tester@example.com",
    "GIT_TERMINAL_PROMPT": "0",
}


def _git(repo: Path, *args: str) -> str:
    """在 repo 里跑 git 并返回 stdout；失败即抛（测试要的是「仓处于预期状态」）。"""
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=tester@example.com", "-c", "user.name=tester", *args],
        capture_output=True,
        text=True,
        check=True,
        env=_ENV,
    ).stdout


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """跑被测脚本；stdout／stderr 都收下，退出码由断言判。"""
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, check=False, env=_ENV
    )


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    """主检出 repo（停在 main）＋ 私有 worktree wt（feature 分支领先一个提交）。"""
    main = tmp_path / "repo"
    wt = tmp_path / "wt"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    (main / "base.txt").write_text("base\n", encoding="utf-8")
    _git(main, "add", "base.txt")
    _git(main, "commit", "-q", "-m", "init")
    _git(main, "worktree", "add", "-q", "-b", "feature", str(wt), "main")
    (wt / "feat.txt").write_text("feat\n", encoding="utf-8")
    _git(wt, "add", "feat.txt")
    _git(wt, "commit", "-q", "-m", "feat work")
    return main, wt


def test_happy_path_no_ff_assembly_then_fast_forward(repos: tuple[Path, Path]) -> None:
    """happy path：wt 里 `--no-ff` 组树，main 只快进——main 上必须出现两个亲的合并提交。"""
    main, wt = repos
    before = _git(main, "rev-parse", "HEAD").strip()
    # 起点是单个根提交（无父）——合并前 main 是线性的
    assert len(_git(main, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 1

    res = _run("--worktree", str(wt), "--branch", "feature")
    assert res.returncode == 0, res.stderr

    after = _git(main, "rev-parse", "HEAD").strip()
    assert after != before
    # 三个词 = 提交自身 ＋ 两亲 ⇒ main 上的确是**合并提交**（--no-ff 组树生效）
    assert len(_git(main, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3
    assert _git(main, "rev-parse", "HEAD^2").strip() == _git(main, "rev-parse", "feature").strip()
    # main 的这次前进是快进：旧 HEAD 仍是新 HEAD 的祖先
    _git(main, "merge-base", "--is-ancestor", before, after)


def test_dirty_worktree_refused_exit_3_and_no_commit(repos: tuple[Path, Path]) -> None:
    """脏 worktree 拒绝（禁自动 stash）：退出 3，且**一个提交都不新增**。"""
    main, wt = repos
    before_head = _git(main, "rev-parse", "HEAD").strip()
    before_count = _git(main, "rev-list", "--count", "HEAD").strip()
    (wt / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

    res = _run("--worktree", str(wt), "--branch", "feature")
    assert res.returncode == 3, res.stderr
    assert "不干净" in res.stderr
    assert _git(main, "rev-parse", "HEAD").strip() == before_head
    assert _git(main, "rev-list", "--count", "HEAD").strip() == before_count
    assert (wt / "dirty.txt").exists()  # 脏文件还在：未被 stash 掉


def test_dry_run_writes_nothing(repos: tuple[Path, Path]) -> None:
    """--dry-run：退出 0、打印命令序列，且 refs／HEAD／工作区都零变化。"""
    main, wt = repos
    refs_before = _git(main, "for-each-ref", "--format=%(refname) %(objectname)").strip()
    main_head_before = _git(main, "rev-parse", "HEAD").strip()
    wt_head_before = _git(wt, "rev-parse", "HEAD").strip()

    res = _run("--worktree", str(wt), "--branch", "feature", "--dry-run")
    assert res.returncode == 0, res.stderr
    assert "merge --no-ff feature" in res.stdout
    assert "merge --ff-only" in res.stdout
    assert _git(main, "for-each-ref", "--format=%(refname) %(objectname)").strip() == refs_before
    assert _git(main, "rev-parse", "HEAD").strip() == main_head_before
    assert _git(wt, "rev-parse", "HEAD").strip() == wt_head_before
    assert not (main / ".git" / "MERGE_HEAD").exists()


def test_usage_errors_exit_2(repos: tuple[Path, Path]) -> None:
    """用法／参数错误一律退出 2（含未知参数、缺必填、非法重试次数）。"""
    _main, wt = repos
    assert _run().returncode == 2
    assert _run("--worktree", str(wt)).returncode == 2
    assert _run("--worktree", str(wt), "--branch", "feature", "--bogus").returncode == 2
    assert _run("--worktree", str(wt), "--branch", "feature", "--retries", "0").returncode == 2


def test_missing_branch_exit_3(repos: tuple[Path, Path]) -> None:
    """分支不存在属前置失败 ⇒ 3（不是 1，也不是 2）。"""
    _main, wt = repos
    res = _run("--worktree", str(wt), "--branch", "no-such-branch")
    assert res.returncode == 3, res.stderr


def test_not_a_git_repo_exit_3(tmp_path: Path) -> None:
    """非 git 工作区 ⇒ 3。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _run("--worktree", str(plain), "--branch", "feature").returncode == 3


def test_index_lock_timeout_exit_4(repos: tuple[Path, Path]) -> None:
    """活锁等待超限 ⇒ 4（并发 git 持锁不判致命，超时才失败）。"""
    _main, wt = repos
    lock = Path(_git(wt, "rev-parse", "--git-path", "index.lock").strip())
    if not lock.is_absolute():
        lock = wt / lock
    lock.write_text("", encoding="utf-8")
    try:
        res = _run("--worktree", str(wt), "--branch", "feature", "--lock-wait", "0")
    finally:
        lock.unlink(missing_ok=True)
    assert res.returncode == 4, res.stderr
    assert "index.lock" in res.stderr
