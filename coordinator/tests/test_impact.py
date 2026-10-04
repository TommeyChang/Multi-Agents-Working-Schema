"""影响面估计器测试——**按影响面测，不全量测**的判据必须可测。

关键性质有两条，都得钉住：
① **细粒度确实省**（否则工具没意义）；
② **必须保守兜底**（前两档没命中就退回整域，宁多跑不可漏跑）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "impact_scope", Path(__file__).resolve().parents[1] / "tools" / "impact_scope.py"
)
assert _SPEC and _SPEC.loader
imp = importlib.util.module_from_spec(_SPEC)
sys.modules["impact_scope"] = imp
_SPEC.loader.exec_module(imp)

TestFile = imp.TestFile


def _tf(rel: str, domain: str, tests: int, imports: set[str] | None = None) -> TestFile:
    return TestFile(
        path=Path(rel), rel=rel, domain=domain, tests=tests, imports=imports or set()
    )


# ---------------------------------------------------------------------------
# 域映射：与 gate.sh 同口径
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "dom"),
    [
        ("auth/ohs/dependencies.py", "auth"),
        ("data_access/kline/follow.py", "data_access"),
        ("notification/dispatcher.py", "notification"),
        ("broker_gateway/composition.py", "broker_gateway"),
        ("dfs/tqsdk.py", "infra"),
        ("scripts/tools/gate.sh", "infra"),
        ("tests/auth/test_x.py", "auth"),
        ("docs/DATA_ACCESS.md", None),
        ("todo/board.md", None),
    ],
)
def test_domain_mapping(path: str, dom: str | None) -> None:
    assert imp.domain_of(path) == dom


def test_shared_files_map_to_all_domains() -> None:
    """共享面改动 ⇒ **全部域**——它们被所有域依赖，只跑一个域必漏。"""
    for path in ("main.py", "settings.py", "logging_config.py", "pyproject.toml"):
        assert imp.domain_of(path) == "*"


def test_conftest_maps_to_all_domains() -> None:
    assert imp.domain_of("tests/conftest.py") == "*"


# ---------------------------------------------------------------------------
# 三档：直连 → 同名 → 退回整域
# ---------------------------------------------------------------------------


def test_direct_import_wins(tmp_path: Path) -> None:
    """**直连最优**：测试 import 了该模块 ⇒ 就是它。"""
    repo = tmp_path
    (repo / "data_access" / "kline").mkdir(parents=True)
    (repo / "data_access" / "kline" / "follow.py").write_text("x = 1\n", encoding="utf-8")
    tests = [
        _tf("tests/data_access/test_other.py", "data_access", 100, {"data_access.kline.follow"}),
        _tf("tests/data_access/test_many.py", "data_access", 700, {"something.else"}),
    ]
    got = imp.estimate(repo, "data_access/kline/follow.py", tests)
    assert got.scope == "direct"
    assert [t.rel for t in got.tests] == ["tests/data_access/test_other.py"]
    assert got.case_count == 100


def test_name_match_when_no_direct_import(tmp_path: Path) -> None:
    """没有直连 ⇒ 退到**同名**。"""
    repo = tmp_path
    (repo / "data_access" / "kline").mkdir(parents=True)
    (repo / "data_access" / "kline" / "follow.py").write_text("x = 1\n", encoding="utf-8")
    tests = [
        _tf("tests/data_access/test_tc81_follow_loop.py", "data_access", 12, {"unrelated"}),
        _tf("tests/data_access/test_zzz.py", "data_access", 700, {"unrelated"}),
    ]
    got = imp.estimate(repo, "data_access/kline/follow.py", tests)
    assert got.scope == "name"
    assert got.case_count == 12


def test_fallback_to_whole_domain_when_nothing_matches(tmp_path: Path) -> None:
    """**兜底必须保守**：前两档都没命中 ⇒ 退回整域。

    宁可多跑，不可漏跑——静默少跑是这类工具最危险的失败形态。
    """
    repo = tmp_path
    (repo / "broker_gateway").mkdir(parents=True)
    (repo / "broker_gateway" / "zzz_unknown_name.py").write_text("x = 1\n", encoding="utf-8")
    tests = [
        _tf("tests/broker_gateway/test_a.py", "broker_gateway", 100, {"unrelated"}),
        _tf("tests/broker_gateway/test_b.py", "broker_gateway", 200, {"unrelated"}),
    ]
    got = imp.estimate(repo, "broker_gateway/zzz_unknown_name.py", tests)
    assert got.scope == "domain-fallback"
    assert got.case_count == 300, "退回整域就必须全跑，不得少跑"


def test_all_domains_for_shared_file(tmp_path: Path) -> None:
    repo = tmp_path
    tests = [
        _tf("tests/auth/test_a.py", "auth", 10, set()),
        _tf("tests/infra/test_b.py", "infra", 20, set()),
    ]
    got = imp.estimate(repo, "main.py", tests)
    assert got.scope == "all-domains"
    assert got.case_count == 30


def test_doc_change_runs_no_tests(tmp_path: Path) -> None:
    """文档面不映射到测试域 ⇒ **零测试**，只跑静态闸。"""
    got = imp.estimate(tmp_path, "docs/DATA_ACCESS.md", [])
    assert got.scope == "none"
    assert got.case_count == 0


# ---------------------------------------------------------------------------
# 细粒度确实省——否则工具没有意义
# ---------------------------------------------------------------------------


def test_name_granularity_is_much_cheaper_than_domain(tmp_path: Path) -> None:
    """**域粒度是瓶颈的根因**：改一个文件不该跑整个域。

    实测：`data_access/kline/follow.py` 域粒度 785 例，名字粒度 34 例。
    """
    repo = tmp_path
    (repo / "data_access" / "kline").mkdir(parents=True)
    (repo / "data_access" / "kline" / "follow.py").write_text("x = 1\n", encoding="utf-8")
    tests = [_tf(f"tests/data_access/test_{i}.py", "data_access", 50, set()) for i in range(20)]
    tests.append(_tf("tests/data_access/test_follow_loop.py", "data_access", 20, set()))

    got = imp.estimate(repo, "data_access/kline/follow.py", tests)
    domain_total = sum(t.tests for t in tests)
    assert got.case_count == 20
    assert got.case_count < domain_total / 10, "细粒度应当比域粒度便宜一个数量级"


# ---------------------------------------------------------------------------
# 扫描：静态 AST，必须可靠
# ---------------------------------------------------------------------------


def test_scan_counts_tests_and_imports(tmp_path: Path) -> None:
    d = tmp_path / "tests" / "demo"
    d.mkdir(parents=True)
    (d / "test_x.py").write_text(
        "import os\nfrom data_access.kline import follow\n\n"
        "def test_a():\n    pass\n\ndef test_b():\n    pass\n",
        encoding="utf-8",
    )
    got = imp.scan_tests(tmp_path)
    assert len(got) == 1
    assert got[0].tests == 2
    assert "data_access" in got[0].imports
    assert "os" in got[0].imports


def test_scan_ignores_import_in_strings(tmp_path: Path) -> None:
    """AST 比正则可靠：字符串里的 `import` 字样不该被当成 import。"""
    d = tmp_path / "tests" / "demo"
    d.mkdir(parents=True)
    (d / "test_y.py").write_text(
        'DOC = "import fake_module"\n\ndef test_a():\n    pass\n', encoding="utf-8"
    )
    got = imp.scan_tests(tmp_path)
    assert "fake_module" not in got[0].imports


def test_scan_survives_syntax_error(tmp_path: Path) -> None:
    """坏文件不该让整个扫描崩——它是估计工具，不是门禁。"""
    d = tmp_path / "tests" / "demo"
    d.mkdir(parents=True)
    (d / "test_bad.py").write_text("def broken(:\n", encoding="utf-8")
    got = imp.scan_tests(tmp_path)
    assert len(got) == 1


# ---------------------------------------------------------------------------
# 诚实性：必须自曝"这是估计"
# ---------------------------------------------------------------------------


def test_report_states_estimate_caveat(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """**必须自曝这不是保证**——静态反演看不到动态 import 与间接依赖。"""
    d = tmp_path / "tests" / "auth"
    d.mkdir(parents=True)
    (d / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    rc = imp.main(["--repo", str(tmp_path), "--changed", "auth/x.py"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "不保证零漏测" in out
    assert "gate.sh --full" in out


def test_missing_repo_is_a_clean_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = imp.main(["--repo", str(tmp_path / "nope"), "--changed", "a.py"])
    assert rc == 2
    assert "未找到测试目录" in capsys.readouterr().err
