"""门禁入口测试——**证据四件套必须是一次跑出来的**。

本体系自己出门禁证据（命令／退出码／摘要／落盘路径）。
这里只钉两件事：
① 范围真的决定了跑什么（受影响面／域／全量／静态）；
② **红就是红**——腿红 ⇒ 退出码非 0，且落盘日志里留着原始输出。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "gate", Path(__file__).resolve().parents[1] / "tools" / "gate.py"
)
assert _SPEC and _SPEC.loader
gate = importlib.util.module_from_spec(_SPEC)
sys.modules["gate"] = gate
_SPEC.loader.exec_module(gate)


def _fake_target(tmp_path: Path, *, red: bool = False) -> Path:
    """造一个最小可跑的目标仓：ruff 配置 ＋ 一个域下的测试文件。

    测试文件放 `tests/auth/`：本体系的域映射认 `auth/`，这样"改动面 → 影响面"
    这条链才真的被走到（放一个不映射任何域的目录会退化成"只跑静态腿"）。
    """
    repo = tmp_path / "repo"
    (repo / "tests" / "auth").mkdir(parents=True)
    # 不加 `addopts`：本体系自己传 `-q`，两边都加会变成 `-qq` 把摘要行也吞掉
    (repo / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")
    body = "def test_ok():\n    assert 1 + 1 == 2\n"
    if red:
        body += "\n\ndef test_red():\n    assert 1 == 2\n"
    (repo / "tests" / "auth" / "test_demo.py").write_text(body, encoding="utf-8")
    return repo


def _args(**kw):
    """构造 argparse 形状的入参——只带 build_plan 关心的字段。"""
    base = {
        "scope": "affected",
        "changed": [],
        "base": "",
        "domain": [],
        "static_tests": [],
        "no_ruff": False,
        "no_complexity": False,
    }
    base.update(kw)
    return type("A", (), base)()


def _declare_complexity(tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    """给目标仓挂一份（声明／不声明复杂度闸的）体系侧绑定。"""
    import bg_coordinator.binding as b

    side = tmp_path / "bindings"
    side.mkdir(exist_ok=True)
    gate_conf: dict = {"entry": "tools/gate.py"}
    if on:
        gate_conf["complexity"] = {
            "tool": "radon",
            "base": "origin/main",
            "paths": ["auth"],
            "floor": "C",
        }
    payload = {"project": repo.name, "version": 1, "gate": gate_conf}
    (side / f"{repo.name}.md").write_text(
        "# 绑定\n\n```json\n" + json.dumps(payload) + "\n```\n", encoding="utf-8"
    )
    monkeypatch.setattr(b, "bindings_dir", lambda: side)


def test_complexity_leg_added_only_when_declared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**声明了才加腿**（工程落点归工程）：没认过的闸不该被塞进去。"""
    repo = _fake_target(tmp_path)
    _declare_complexity(tmp_path, repo, monkeypatch, on=False)
    legs, _ = gate.build_plan(_args(scope="static"), repo, sys.executable)
    assert not any("complexity.py" in " ".join(c) for _, c in legs)

    _declare_complexity(tmp_path, repo, monkeypatch, on=True)
    legs, _ = gate.build_plan(_args(scope="static"), repo, sys.executable)
    cmds = [" ".join(c) for _, c in legs]
    assert any("complexity.py" in c for c in cmds), cmds
    assert any("--target" in c for c in cmds)


def test_complexity_leg_has_explicit_escape_hatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """逃生口是**显式**的：`--no-complexity` 一给，证据里就少一条腿（评审看得见）。"""
    repo = _fake_target(tmp_path)
    _declare_complexity(tmp_path, repo, monkeypatch, on=True)
    legs, _ = gate.build_plan(_args(scope="static", no_complexity=True), repo, sys.executable)
    assert not any("complexity.py" in " ".join(c) for _, c in legs)


def test_pick_python_prefers_target_venv_then_self(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    (repo / ".venv" / "bin").mkdir(parents=True)
    venv_py = repo / ".venv" / "bin" / "python"
    venv_py.write_text("", encoding="utf-8")
    assert gate.pick_python(repo) == str(venv_py)
    (repo / ".venv").rename(repo / ".venv-x")
    assert gate.pick_python(repo) == sys.executable
    assert gate.pick_python(repo, "/custom/python") == "/custom/python"
    # 相对解释器路径要落成绝对（否则在目标仓 cwd 里跑会 127），
    # 但**不得解析符号链接**——venv 的 bin/python 就是符号链接，解析掉会丢 site-packages。
    import os

    assert gate.pick_python(repo, "relative/py") == os.path.abspath("relative/py")
    assert gate.pick_python(repo, "python3") == "python3"


def test_affected_scope_uses_impact_scope(tmp_path: Path) -> None:
    """受影响面由**本体系的 impact_scope** 算——不是外部脚本，也不是全量。"""
    repo = _fake_target(tmp_path)
    args = _args(changed=["auth/x.py"])
    legs, note = gate.build_plan(args, repo, sys.executable)
    names = [n for n, _ in legs]
    assert "ruff" in names
    assert any("受影响面" in n or "全域" in n for n in names)
    assert "影响面" in note or "全域" in note


def test_static_scope_runs_only_ruff_and_given_legs(tmp_path: Path) -> None:
    repo = _fake_target(tmp_path)
    args = _args(scope="static", static_tests=["tests/auth/test_demo.py"])
    legs, _ = gate.build_plan(args, repo, sys.executable)
    assert [n for n, _ in legs] == ["ruff", "静态腿 tests/auth/test_demo.py"]


def test_full_scope_has_no_fast_marker(tmp_path: Path) -> None:
    """发布前口径**不得**带快跑通道标记——带了就是把真库档悄悄排除在外。"""
    repo = _fake_target(tmp_path)
    args = _args(scope="full")
    legs, note = gate.build_plan(args, repo, sys.executable)
    cmds = [" ".join(c) for _, c in legs]
    assert all(gate.FAST_MARKER not in c for c in cmds)
    assert any("tests/" in c for c in cmds)
    assert "全量" in note


def test_domain_scope_rejects_missing_domain(tmp_path: Path) -> None:
    repo = _fake_target(tmp_path)
    args = _args(scope="domain", domain=["nope"], no_ruff=True)
    with pytest.raises(SystemExit):
        gate.build_plan(args, repo, sys.executable)


def test_gate_runs_green_and_writes_evidence(tmp_path: Path) -> None:
    """真跑一遍：全绿 ⇒ 退出 0，证据落盘（原始输出在文件里，不在回报里）。"""
    repo = _fake_target(tmp_path)
    log = tmp_path / "logs"
    rc = gate.main(
        ["--target", str(repo), "--python", sys.executable, "--scope", "full", "--log-dir", str(log)]
    )
    assert rc == 0
    files = list(log.glob("gate-full-*.log"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "=== [gate] ruff ===" in text
    assert "1 passed" in text


def test_gate_red_is_red_and_evidence_keeps_raw_output(tmp_path: Path) -> None:
    """**红就是红**：腿红 ⇒ 退出 1，且日志里留着失败原文（证据可核）。"""
    repo = _fake_target(tmp_path, red=True)
    log = tmp_path / "logs"
    rc = gate.main(
        ["--target", str(repo), "--python", sys.executable, "--scope", "full", "--log-dir", str(log)]
    )
    assert rc == 1
    text = next(log.glob("gate-full-*.log")).read_text(encoding="utf-8")
    assert "failed" in text
    assert "退出码 1" in text


def test_gate_json_is_machine_readable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _fake_target(tmp_path)
    rc = gate.main(
        [
            "--target", str(repo), "--python", sys.executable, "--scope", "full",
            "--log-dir", str(tmp_path / "logs"), "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["scope"] == "full"
    assert payload["exit"] == 0
    assert payload["log"].endswith(".log")
    assert {leg["name"] for leg in payload["legs"]} >= {"ruff"}


def test_gate_rejects_non_repo_target(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = gate.main(["--target", str(tmp_path / "nope")])
    assert rc == 2
    assert "目标仓" in capsys.readouterr().err


def test_affected_without_changes_is_a_clean_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """算不出改动面就不许瞎跑——**范围不明时宁可报错，也不许静默全量或静默空跑**。"""
    repo = _fake_target(tmp_path)
    rc = gate.main(["--target", str(repo), "--scope", "affected"])
    assert rc == 2
    assert "改动面" in capsys.readouterr().err


def test_gate_script_runs_as_plain_python3(tmp_path: Path) -> None:
    """**自持**：以脚本方式直跑也要能 import 到包（不靠 PYTHONPATH、不靠 venv）。"""
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "tools" / "gate.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "门禁入口" in proc.stdout
