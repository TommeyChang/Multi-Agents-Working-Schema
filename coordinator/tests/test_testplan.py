"""测试分级：判据要能算、能升、**判不了时不许当罪名**（用户 2026-10-05 口径）。

口径：开发过程的测试分多级、不能全量跑；哪一级必须齐由"改动文件 → 域"算出来。
"""

from __future__ import annotations

import sys
from pathlib import Path

COORD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(COORD))

from bg_coordinator import testplan as tp  # noqa: E402

DOMAINS = {
    "auth": ["auth", "broker_gateway/auth"],
    "data": ["data_access"],
    "nof": ["notification"],
}
PUBLIC = ("main.py", "dfs")


def test_single_domain_needs_affected_only() -> None:
    need, why = tp.required_scope(["auth/login.py"], DOMAINS, PUBLIC)
    assert need == "affected" and "auth" in why[0]


def test_cross_domain_escalates_to_domain() -> None:
    need, why = tp.required_scope(["auth/a.py", "data_access/b.py"], DOMAINS, PUBLIC)
    assert need == "domain" and "跨 2 个域" in why[0]


def test_public_surface_escalates_to_full() -> None:
    need, why = tp.required_scope(["auth/a.py", "main.py"], DOMAINS, PUBLIC)
    assert need == "full" and "公共面" in why[0]


def test_unknown_mapping_is_not_a_charge() -> None:
    """映射不明 ⇒ `None`（**判不了就不拦**），但理由要说清缺什么。"""
    need, why = tp.required_scope(["brand_new/x.py"], DOMAINS, PUBLIC)
    assert need is None and "映射不明" in why[0]
    assert tp.meets(None, "static"), "判不了时必须放行——否则是假红"


def test_no_binding_domains_is_reported_not_guessed() -> None:
    need, why = tp.required_scope(["auth/a.py"], {}, PUBLIC)
    assert need is None and "testplan.domains" in why[0]


def test_meets_compares_by_strictness() -> None:
    assert tp.meets("affected", "affected")
    assert tp.meets("affected", "domain") and tp.meets("affected", "full")
    assert not tp.meets("domain", "affected")
    assert not tp.meets("full", "domain")
    assert not tp.meets("affected", ""), "没申报级别不能算达标"


def test_longest_prefix_wins() -> None:
    """嵌套域按**最长前缀**归属（`auth` 与 `broker_gateway/auth` 同时命中时取后者）。"""
    d = {"outer": ["broker_gateway"], "inner": ["broker_gateway/auth"]}
    need, _why = tp.required_scope(["broker_gateway/auth/x.py"], d, ())
    assert need == "affected"
    hits = tp._domain_of("broker_gateway/auth/x.py", d)
    assert hits == "inner"


def test_load_reads_binding_block() -> None:
    domains, public, budget = tp.load(
        {"domains": {"auth": ["auth"]}, "public": ["main.py"], "budget": {"affected": 180}}
    )
    assert domains == {"auth": ["auth"]} and public == ("main.py",) and budget == {"affected": 180}


def test_audit_flags_delivery_without_scope() -> None:
    """**负例**：没申报级别的交付要被点名（分级口径的第一步是"看得见"）。"""
    import sys as _sys
    from pathlib import Path as _P

    _sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
    from conftest import budgeted  # noqa: E402

    from bg_coordinator.audit import audit
    from bg_coordinator.engine import State
    from bg_coordinator.errors import Code
    from bg_coordinator.models import Evidence, Kind, Task, TaskState

    s = budgeted(State())
    s.tasks["T-D-1"] = Task(
        id="T-D-1", kind=Kind.T, line="D", status=TaskState.DELIVERED,
        evidence=[
            Evidence(round=1, commit="abc", gate_cmd="pytest -q", gate_exit=0,
                     evidence_path=".evidence/x.txt")
        ],
    )
    hits = [a for a in audit(s) if a.code == Code.E_SCOPE_MISSING]
    assert [a.id for a in hits] == ["T-D-1"]

    old = s.tasks["T-D-1"].evidence[0]
    import dataclasses as _dc

    s.tasks["T-D-1"].evidence = [_dc.replace(old, scope="affected")]  # 证据不可变，换新条
    assert not [a for a in audit(s) if a.code == Code.E_SCOPE_MISSING]
