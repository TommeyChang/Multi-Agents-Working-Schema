"""工程绑定——**体系管机制，工程管落点**，这条分工要被机器钉住。

两件事：
① 绑定本身能解析、字段齐、缺项报得出（`binding.py` 的判据）；
② **角色文件里不许再出现工程专属路径**——否则"某个工程的巧合"又会长回体系侧。
"""

from __future__ import annotations

import json
import re
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
    "migrations": {"dir": "alembic/versions", "base": "origin/main"},
    "tests": {"db_marker": "db", "db_fixtures": ["db_session"]},
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


def test_missing_migration_subfield_is_a_gap() -> None:
    """`migrations` 里**闸真正读**的子字段缺了 ⇒ 报缺口。

    真事：本工程的绑定人读部分写着「基线：`origin/main`」，机读块却**没有** `base`
    ——形状上"字段齐了"（`migrations` 是非空对象），提交闸取基线却拿不到。
    这类缺口的共同点是：**看起来声明了，读它的代码一无所获**。
    """
    assert binding.validate(_GOOD, BINDING_FIELDS) == []
    for sub in ("dir", "base"):
        data = dict(_GOOD, migrations={k: v for k, v in _GOOD["migrations"].items() if k != sub})
        gaps = binding.validate(data, BINDING_FIELDS)
        assert any(f"migrations.{sub}" in g for g in gaps), (sub, gaps)
    blank = dict(_GOOD, migrations={"dir": "  ", "base": ""})
    assert len(binding.validate(blank, BINDING_FIELDS)) == 2


def test_bindings_readme_lists_exactly_the_required_fields() -> None:
    """`bindings/README.md` 的字段表必须与 `BINDING_FIELDS`**同集合**。

    为什么钉这条：那张表先前写着"九个必需字段"，比 `BINDING_FIELDS` 少一个 `tests`
    ——文档少一个字段，读者就会以为可以不填。**权威只有一处**，文档只能复述它，
    所以复述得对不对要机器来核（先前只是"人读部分说是九个"这类静默漂移）。
    """
    rows = (MAWS / "bindings" / "README.md").read_text(encoding="utf-8").splitlines()
    listed = {
        m.group(1) for ln in rows if (m := re.match(r"\| `([a-z_]+)` \|", ln)) is not None
    } & {name for name, _kind, _desc in BINDING_FIELDS}
    assert listed == {name for name, _kind, _desc in BINDING_FIELDS}


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


def test_role_docs_carry_no_hardcoded_local_endpoints() -> None:
    """角色文件里不许出现**写死的本机地址＋端口**——具体端口是工程的落点。

    实例：`agents/ops.md` 曾写着 `http://127.0.0.1:18080/health`。
    这与"路径清单"是同一类问题：看起来像规则，实际是某一工程的巧合。
    """
    import re

    pattern = re.compile(r"127\.0\.0\.1:\d+")
    offenders: list[str] = []
    for path in sorted((MAWS / "agents").glob("*.md")):
        for m in pattern.finditer(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.name}: {m.group(0)}")
    assert not offenders, "角色文件里出现写死的本机端口（应移入 bindings/）：\n  " + "\n  ".join(offenders)


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
                offenders.append(f"{path.relative_to(MAWS)}: {token}")
    assert not offenders, "角色文件里出现工程专属落点（应移入 bindings/）：\n  " + "\n  ".join(offenders)


#: 规范面的**全体**（AGENT ＋ rules ＋ agents）都不许出现这些——它们是某一工程或某一工作区的落点
_NORMATIVE_TOKENS = (
    "/root/",  # 工作区绝对路径
    "uv run",  # 某工程的工具链
    "alembic",  # 某工程的迁移工具
    "broker_gateway",
    "data_access/",
    "auth/",
    "notification/",
    "18080",  # 某工程的端口
)


def test_normative_surface_carries_no_project_or_workspace_landing() -> None:
    """**规范面只写「换一个工程还成立」的东西**——工作区路径、工具链、迁移工具、
    域路径、端口，一律属于绑定。

    这条闸防的是同一类失败反复发生：工程细节长回体系侧，而它**错得不像错的**
    （看起来像规则，实际是某一工程的巧合）。本轮陆续收掉了 agents/ 的七处、
    rules/WORKSPACE.md 的六处、COORDINATION 与 SUBAGENT 各一处——所以钉住。
    """
    surfaces = [
        MAWS / "AGENT.md",
        *(MAWS / "rules").glob("*.md"),
        *(MAWS / "agents").glob("*.md"),
        # **技能也是规范面**（agent 直接读它做事）——2026-10-05 纳入扫描
        *(MAWS / ".dsh" / "skills").glob("*/SKILL.md"),
    ]
    offenders: list[str] = []
    for path in sorted(surfaces):
        text = path.read_text(encoding="utf-8")
        for token in _NORMATIVE_TOKENS:
            if token in text:
                offenders.append(f"{path.name}: {token}")
    assert not offenders, "规范面出现工程/工作区落点（应移入 bindings/）：\n  " + "\n  ".join(offenders)


def test_rules_point_at_binding_for_per_project_lists() -> None:
    """规则里凡"逐工程不同"的清单，必须**指向绑定**而不是自己列。"""
    for name in ("SUBAGENT.md", "WORKSPACE.md"):
        text = (MAWS / "rules" / name).read_text(encoding="utf-8")
        assert "工程绑定" in text, f"{name} 应按绑定引用共享面清单"


# ---------------------------------------------------------------------------
# 并存副本：**不许有第二真相源**
# ---------------------------------------------------------------------------


def test_shadowed_copy_is_reported_even_when_identical(tmp_path: Path) -> None:
    """两处并存 ⇒ 报出来（哪怕此刻一致）——被忽略的那份**不会停止变化**。"""
    repo = tmp_path / "demo"
    (repo / ".maws").mkdir(parents=True)
    _write(repo / ".maws", dict(_GOOD, project="demo"))
    # 体系侧同名副本（用 monkeypatch 把体系根指到 tmp）
    side = tmp_path / "sys" / "bindings"
    side.mkdir(parents=True)
    _write(side, dict(_GOOD, project="demo"))
    (side / "project.md").rename(side / "demo.md")
    import bg_coordinator.binding as b

    orig = b.bindings_dir
    b.bindings_dir = lambda: side
    try:
        info = b.describe(repo, BINDING_FIELDS)
    finally:
        b.bindings_dir = orig
    assert info["source"] == b.SRC_TRUNK
    assert len(info["shadowed"]) == 1
    assert info["shadowed"][0]["diverged"] is False
    assert any("冗余副本" in g for g in info["gaps"])


def test_diverged_shadow_is_a_gap(tmp_path: Path) -> None:
    """两份**内容不一致** ⇒ 缺口（退出码 1）——这是静默漂移的入口。"""
    repo = tmp_path / "demo"
    (repo / ".maws").mkdir(parents=True)
    _write(repo / ".maws", dict(_GOOD, project="demo"))
    side = tmp_path / "sys" / "bindings"
    side.mkdir(parents=True)
    _write(side, dict(_GOOD, project="demo", scratch_namespace="old_"))
    (side / "project.md").rename(side / "demo.md")
    import bg_coordinator.binding as b

    orig = b.bindings_dir
    b.bindings_dir = lambda: side
    try:
        info = b.describe(repo, BINDING_FIELDS)
    finally:
        b.bindings_dir = orig
    assert info["shadowed"][0]["diverged"] is True
    assert any("不一致" in g for g in info["gaps"])
