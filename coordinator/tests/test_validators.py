"""校验规则测试——八条不变量 + 白名单函数 + 依赖图。

「白名单互不重叠」与「回标必须带实证」是本方案**从散文变成可判定函数**的两条，
所以它们各有独立测试。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bg_coordinator.errors import Code
from bg_coordinator.models import (
    AcceptanceItem,
    AcceptanceType,
    Actor,
    Evidence,
    Kind,
    Line,
    Role,
    Task,
    TaskState,
)
from bg_coordinator.validators import (
    deps_all_terminal,
    deps_missing,
    detect_dep_cycle,
    rule_1_unique_id,
    rule_2_ownership,
    rule_3_four_elements,
    rule_4_expect_ver,
    rule_6_whitelist_exclusive,
    rule_8_evidence_form,
    rule_8b_evidence_readable,
    validate_acceptance,
    whitelist_covers,
    whitelist_overlap,
)


def _task(tid: str = "T-D-1", status: TaskState = TaskState.DEFINED, **kw) -> Task:
    base: dict = {"id": tid, "kind": Kind.T, "line": Line.D, "status": status}
    base.update(kw)
    return Task(**base)


def _dev() -> Actor:
    return Actor(role=Role.DEV, name="dev-1", line=Line.D)


# ---------------------------------------------------------------------------
# 白名单匹配与互不重叠
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "whitelist", "expected"),
    [
        ("data_access/kline/follow.py", ["data_access"], True),
        ("data_access/kline/follow.py", ["data_access/**"], True),
        ("data_access/kline/follow.py", ["data_access/*.py"], False),
        ("data_access/kline/follow.py", ["data_access/kline/*.py"], True),
        ("main.py", ["main.py"], True),
        ("main.py", ["broker_gateway/**"], False),
        ("tests/a/b.py", ["tests/**"], True),
    ],
)
def test_whitelist_covers(path: str, whitelist: list[str], expected: bool) -> None:
    assert whitelist_covers([path], whitelist)[0] is expected


def test_whitelist_covers_reports_out_of_scope_files() -> None:
    ok, out = whitelist_covers(["a/x.py", "b/y.py"], ["a/**"])
    assert ok is False
    assert out == ["b/y.py"]


@pytest.mark.parametrize(
    ("a", "b", "expect_overlap"),
    [
        (["data_access/**"], ["data_access/kline/**"], True),
        (["data_access/kline/**"], ["broker_gateway/**"], False),
        (["main.py"], ["main.py"], True),
        (["a/b.py"], ["a/**"], True),
    ],
)
def test_whitelist_overlap(a: list[str], b: list[str], expect_overlap: bool) -> None:
    """这是「可并行扇出」的机械判据——方案 §3.3。"""
    assert whitelist_overlap(a, b) is expect_overlap


def test_whitelist_exclusive_detects_in_flight_conflict() -> None:
    a = _task("T-D-1", TaskState.IN_PROGRESS, whitelist=["data_access/**"])
    b = _task("T-D-2", TaskState.DEFINED, whitelist=["data_access/kline/**"])
    rej, conflicts = rule_6_whitelist_exclusive({"T-D-1": a, "T-D-2": b}, b)
    assert rej is not None and rej.code == Code.E_WL_CONFLICT
    assert conflicts == ["T-D-1"]


def test_whitelist_exclusive_ignores_non_in_flight() -> None:
    a = _task("T-D-1", TaskState.REGISTERED, whitelist=["data_access/**"])
    b = _task("T-D-2", TaskState.DEFINED, whitelist=["data_access/**"])
    rej, conflicts = rule_6_whitelist_exclusive({"T-D-1": a, "T-D-2": b}, b)
    assert rej is None and conflicts == []


# ---------------------------------------------------------------------------
# 依赖图
# ---------------------------------------------------------------------------


def test_dep_cycle_detected() -> None:
    a = _task("T-D-1", deps=["T-D-2"])
    b = _task("T-D-2", deps=["T-D-3"])
    c = _task("T-D-3", deps=[])
    tasks = {"T-D-1": a, "T-D-2": b, "T-D-3": c}
    cycle = detect_dep_cycle(tasks, "T-D-3", ["T-D-1"])
    assert cycle is not None
    assert cycle[0] == cycle[-1]


def test_dep_cycle_absent() -> None:
    tasks = {"T-D-1": _task("T-D-1", deps=[])}
    assert detect_dep_cycle(tasks, "T-D-2", ["T-D-1"]) is None


def test_self_dependency_is_a_cycle() -> None:
    assert detect_dep_cycle({}, "T-D-1", ["T-D-1"]) is not None


def test_deps_helpers() -> None:
    done = _task("T-D-1", TaskState.ACCEPTED)
    doing = _task("T-D-2", TaskState.IN_PROGRESS)
    tasks = {"T-D-1": done, "T-D-2": doing}
    assert deps_all_terminal(tasks, ["T-D-1"]) == []
    assert deps_all_terminal(tasks, ["T-D-1", "T-D-2"]) == ["T-D-2"]
    assert deps_missing(tasks, ["T-D-9"]) == ["T-D-9"]


# ---------------------------------------------------------------------------
# 八条不变量
# ---------------------------------------------------------------------------


def test_rule_1_duplicate_id() -> None:
    tasks = {"T-D-1": _task()}
    rej = rule_1_unique_id(tasks, "T-D-1")
    assert rej is not None and rej.code == Code.E_DUP_ID
    assert rule_1_unique_id(tasks, "T-D-2") is None


def test_rule_2_ownership_conflict() -> None:
    task = _task(owner="tl-other")
    rej = rule_2_ownership(task, _dev(), "claim-dev")
    assert rej is not None and rej.code == Code.E_OWNED
    assert task.owner == "tl-other"  # 不覆盖他人


def test_rule_2_not_owner_cannot_deliver() -> None:
    task = _task(status=TaskState.IN_PROGRESS, owner="tl-1")
    rej = rule_2_ownership(task, Actor(role=Role.TECH_LEAD, name="tl-2", line=Line.D), "deliver")
    assert rej is not None and rej.code == Code.E_NOT_OWNER


def test_rule_2_frozen_task() -> None:
    """终态条目：认领人本人来改，撞的是**冻结闸**。"""
    tl = Actor(role=Role.TECH_LEAD, name="TL-D", line=Line.D)
    task = _task(status=TaskState.ACCEPTED, owner="TL-D")
    assert rule_2_ownership(task, tl, "deliver").code == Code.E_FROZEN  # type: ignore[union-attr]


def test_rule_2_owner_checked_before_freeze() -> None:
    """非认领人先撞**所有权闸**——先判越权，再判冻结。"""
    tl2 = Actor(role=Role.TECH_LEAD, name="TL-OTHER", line=Line.D)
    task = _task(status=TaskState.ACCEPTED, owner="TL-D")
    assert rule_2_ownership(task, tl2, "deliver").code == Code.E_NOT_OWNER  # type: ignore[union-attr]


def test_rule_3_four_elements_all_required() -> None:
    """无白名单不成条目——方案 §4.3。"""
    rej = rule_3_four_elements(_task())
    assert rej is not None and rej.code == Code.E_INCOMPLETE
    assert "修改范围白名单" in rej.message

    ok = _task(
        whitelist=["a/**"],
        frozen=[],
        acceptance=[AcceptanceItem(type=AcceptanceType.MANUAL, desc="x")],
    )
    assert rule_3_four_elements(ok) is None


def test_rule_3_acceptance_required() -> None:
    t = _task(whitelist=["a/**"], frozen=[])
    rej = rule_3_four_elements(t)
    assert rej is not None and "验收标准" in rej.message


def test_rule_4_expect_ver_conflict() -> None:
    task = _task(ver=5)
    assert rule_4_expect_ver(task, 5) is None
    assert rule_4_expect_ver(task, None) is None
    rej = rule_4_expect_ver(task, 4)
    assert rej is not None and rej.code == Code.E_CONFLICT


def test_rule_8_evidence_form() -> None:
    task = _task()
    assert rule_8_evidence_form(task).code == Code.E_NO_EVIDENCE  # type: ignore[union-attr]

    task.evidence.append(
        Evidence(round=1, commit="abc", gate_cmd="pytest -q", gate_exit=1, evidence_path="x.log")
    )
    rej = rule_8_evidence_form(task)
    assert rej is not None and rej.code == Code.E_GATE_NONZERO

    task.evidence[0] = Evidence(
        round=1, commit="abc", gate_cmd="pytest -q", gate_exit=0, evidence_path="x.log"
    )
    assert rule_8_evidence_form(task) is None


def test_rule_8b_checks_filesystem(tmp_path: Path) -> None:
    """证据路径**存在且可读**是机械可判的；这不是「内容正确」的证明。"""
    missing = tmp_path / "nope.log"
    assert rule_8b_evidence_readable(str(missing)).code == Code.E_NO_EVIDENCE  # type: ignore[union-attr]

    real = tmp_path / "gate.log"
    real.write_text("2182 passed", encoding="utf-8")
    assert rule_8b_evidence_readable(str(real)) is None
    assert rule_8b_evidence_readable("").code == Code.E_NO_EVIDENCE  # type: ignore[union-attr]


def test_validate_acceptance_reports_missing_evidence(tmp_path: Path) -> None:
    t = _task(
        TaskState.VERIFIED,
        acceptance=[
            AcceptanceItem(type=AcceptanceType.EVIDENCE, path=str(tmp_path / "gone")),
            AcceptanceItem(type=AcceptanceType.CLOSURE, desc="入口动作 → 可观测反应"),
        ],
    )
    rej = validate_acceptance(t)
    assert rej is not None and rej.code == Code.E_UNMET


def test_validate_acceptance_ok() -> None:
    t = _task(
        TaskState.VERIFIED,
        acceptance=[
            AcceptanceItem(type=AcceptanceType.TEST, cmd="pytest -q"),
            AcceptanceItem(type=AcceptanceType.NEGATIVE, desc="未注入仍 200 ⇒ 必红"),
            AcceptanceItem(type=AcceptanceType.CLOSURE, desc="入口动作 → 可观测反应"),
        ],
    )
    assert validate_acceptance(t) is None


def test_validate_acceptance_requires_items() -> None:
    assert validate_acceptance(_task(TaskState.VERIFIED)).code == Code.E_UNMET  # type: ignore[union-attr]
