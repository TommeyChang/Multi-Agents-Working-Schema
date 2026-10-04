"""工程绑定——**体系管机制，工程管落点**，这条分工要被机器钉住。

两件事：
① 绑定本身能解析、字段齐、缺项报得出（`binding.py` 的判据）；
② **角色文件里不许再出现工程专属路径**——否则"某个工程的巧合"又会长回体系侧。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

COORD = Path(__file__).resolve().parents[1]
MAWS = COORD.parent
sys.path.insert(0, str(COORD))

from bg_coordinator import binding  # noqa: E402
from bg_coordinator.schema import BINDING_FIELDS  # noqa: E402

_GOOD = {
    "project": "demo",
    "version": 1,
    "lines": {"A": {"name": "auth", "workface": ["auth/"]}},
    "gate": {"entry": "tools/gate.py"},
    "migrations": {"dir": "alembic/versions"},
    "protected_assets": ["broker_gateway"],
    "scratch_namespace": "bg_",
    "window": ["停服", "起服"],
    "shared_files": ["todo/**"],
}


def _write(tmp_path: Path, data: dict | None, *, raw: str | None = None) -> Path:
    body = raw if raw is not None else f"# 绑定\n\n```json\n{json.dumps(data, ensure_ascii=False)}\n```\n"
    p = tmp_path / "project.md"
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 解析与校验
# ---------------------------------------------------------------------------


def test_missing_json_block_is_a_clear_error(tmp_path: Path) -> None:
    """没有机读块 ⇒ 明确报错，**不许静默当成空绑定**。"""
    data, err = binding.extract_json("# 只有散文，没有机读块\n")
    assert data is None
    assert "json" in err


def test_broken_json_is_reported(tmp_path: Path) -> None:
    data, err = binding.extract_json("```json\n{不是 JSON}\n```\n")
    assert data is None
    assert "JSON" in err


def test_root_must_be_object() -> None:
    data, err = binding.extract_json("```json\n[1, 2]\n```\n")
    assert data is None
    assert "对象" in err


def test_complete_binding_has_no_gaps() -> None:
    assert binding.validate(_GOOD, BINDING_FIELDS) == []


def test_missing_field_is_reported() -> None:
    data = {k: v for k, v in _GOOD.items() if k != "lines"}
    gaps = binding.validate(data, BINDING_FIELDS)
    assert any("lines" in g for g in gaps)


def test_empty_value_is_a_gap_not_a_pass() -> None:
    """**有字段但空着**是最危险的形态——它看起来像已经声明了。"""
    data = dict(_GOOD, lines={}, protected_assets=[], scratch_namespace="  ")
    gaps = binding.validate(data, BINDING_FIELDS)
    assert len(gaps) == 3
    assert all("须为非空" in g for g in gaps)


def test_wrong_type_is_reported() -> None:
    gaps = binding.validate(dict(_GOOD, version="1"), BINDING_FIELDS)
    assert any("version" in g and "整数" in g for g in gaps)


# ---------------------------------------------------------------------------
# 定位：主干侧优先，体系侧兜底
# ---------------------------------------------------------------------------


def test_trunk_side_wins_over_system_side(tmp_path: Path) -> None:
    """绑定搬进主干后**机制不动**：主干侧优先，体系侧兜底。"""
    repo = tmp_path / "some-project"
    (repo / ".maws").mkdir(parents=True)
    _write(repo / ".maws", _GOOD)
    src, path = binding.locate(repo)
    assert src == binding.SRC_TRUNK
    assert path == repo / ".maws" / "project.md"


def test_system_side_is_the_fallback(tmp_path: Path) -> None:
    repo = tmp_path / "futures-broker-gateway"
    repo.mkdir()
    src, path = binding.locate(repo)
    assert src == binding.SRC_MAWS
    assert path is not None and path.name == "futures-broker-gateway.md"


def test_unbound_project_reports_gap_not_silence(tmp_path: Path) -> None:
    src, path = binding.locate(tmp_path / "never-heard-of-it")
    assert (src, path) == (binding.SRC_NONE, None)
    info = binding.describe(tmp_path / "never-heard-of-it", BINDING_FIELDS)
    assert info["status"] == "未登记"
    assert info["gaps"]


def test_describe_reports_lines(tmp_path: Path) -> None:
    repo = tmp_path / "futures-broker-gateway"
    repo.mkdir()
    info = binding.describe(repo, BINDING_FIELDS)
    assert info["status"] == "完整"
    assert "A" in info["lines"] and "OPS" in info["lines"]


def test_line_of_reads_declaration() -> None:
    assert binding.line_of(_GOOD, "A") == {"name": "auth", "workface": ["auth/"]}
    assert binding.line_of(_GOOD, "Z") is None
    assert binding.line_of(None, "A") is None


# ---------------------------------------------------------------------------
# 体系侧登记必须自洽
# ---------------------------------------------------------------------------


def test_registered_bindings_are_all_complete() -> None:
    """`bindings/` 里登记了的，就必须能解析、字段齐——**对账要报得出坏绑定**。"""
    records = binding.scan_registered(BINDING_FIELDS)
    assert records, "体系侧至少应有一份工程绑定（当前工程）"
    for rec in records:
        assert rec["gaps"] == [], f"{rec['file']} 有缺口：{rec['gaps']}"


def test_reconcile_includes_binding_status() -> None:
    from bg_coordinator.schema import reconcile

    rep = reconcile()
    assert not [d for d in rep.drifts if d.kind == "binding-incomplete"]


def test_future_binding_can_be_incomplete_without_touching_production(tmp_path: Path) -> None:
    """**换一个工程只写一份绑定**：字段齐即通过，角色文件与规则一个字都不用动。"""
    data = dict(_GOOD, project="new-project")
    assert binding.validate(data, BINDING_FIELDS) == []


# ---------------------------------------------------------------------------
# 分工的**结构**不变式：角色文件里不许有工程专属路径
# ---------------------------------------------------------------------------

#: 这些是**某个工程**的落点，出现在角色文件里就是"看起来像规则、实际是巧合"
_PROJECT_TOKENS = (
    "auth/",
    "data_access/",
    "notification/",
    "broker_gateway",
    "dfs/",
    "bg_tmpl",
    "alembic",
    "scripts/ops/",
    "scripts/tools/",
    "deploy/",
    "docs/TESTING",
)


def test_role_docs_carry_no_project_specific_paths() -> None:
    """角色文件只写**换一个工程还成立**的东西。

    判据：*"换一个工程，这句话还成立吗？"* 不成立 ⇒ 它属于绑定，不属于 `agents/`。
    这条闸防的是"工程细节长回体系侧"——那种内容**错得不像错的**。
    """
    offenders: list[str] = []
    for path in sorted((MAWS / "agents").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for token in _PROJECT_TOKENS:
            if token in text:
                offenders.append(f"{path.name}: {token}")
    assert not offenders, "角色文件里出现工程专属落点（应移入 bindings/）：\n  " + "\n  ".join(offenders)


def test_rules_point_at_binding_for_per_project_lists() -> None:
    """规则里凡"逐工程不同"的清单，必须**指向绑定**而不是自己列。"""
    for name in ("SUBAGENT.md", "WORKSPACE.md"):
        text = (MAWS / "rules" / name).read_text(encoding="utf-8")
        assert "工程绑定" in text, f"{name} 应按绑定引用共享面清单"
