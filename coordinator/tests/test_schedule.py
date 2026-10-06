"""调度器：计划必须由状态推出，且**遵守每一条已有闸**（用户 2026-10-06 口径）。

调度器取代文档对账——角色不再读视图自己找活，而是调度器从状态给出
"谁下一步做什么 ＋ 放行它的判据"。它**只算不动**，且不许越过确认闸／就绪判据／额度。
"""

from __future__ import annotations

import time
from pathlib import Path

from conftest import budgeted  # noqa: E402

from bg_coordinator.engine import Params, State, apply
from bg_coordinator.models import (
    Actor,
    Kind,
    Line,
    AcceptanceItem,
    AcceptanceType,
    MergeRequest,
    MergeState,
    Origin,
    Priority,
    Role,
    TaskState,
)
from bg_coordinator.readiness import Quota
from bg_coordinator.schedule import plan_role


def _pm() -> Actor:
    return Actor(role=Role.PM, name="pm-D", line=Line.D)


def _tl() -> Actor:
    return Actor(role=Role.TECH_LEAD, name="TL-D", line=Line.D)


def _to(state: State, tid: str, upto: str) -> State:
    """把条目推到指定状态（走真动词，不造假状态）。"""
    s = state
    s = apply(s, "claim-analyze", tid, _pm()).state
    if upto == "analyzing":
        return s
    s = apply(s, "define", tid, _pm(), params=Params(whitelist=["a/**"], frozen=[], priority=Priority.P1,
                acceptance=[AcceptanceItem(type=AcceptanceType.CLOSURE, desc="入口 → 可观测")])).state
    if upto == "defined":
        return s
    s.quota = Quota(enabled=True, per_line={"D": 3})
    s = apply(s, "claim-dev", tid, _tl()).state
    return s


def _reg(state: State, title: str, origin: Origin = Origin.LINE) -> tuple[State, str]:
    r = apply(state, "register", "", Actor(role=Role.PO, name="po", line=Line.D),
              params=Params(title=title, kind=Kind.R, line=Line.D, priority=Priority.P1,
                            origin=origin))
    assert r.ok, r.rejection
    return r.state, r.detail["id"]


def test_pm_plan_covers_intake_analysis_and_acceptance() -> None:
    s = budgeted(State())
    s, t1 = _reg(s, "待认领")
    s, t2 = _reg(s, "分析中")
    s, _t3 = _reg(s, "已定稿")
    s = _to(s, t2, "analyzing")
    s, t3 = _reg(s, "x3")
    s = _to(s, t3, "defined")
    s = apply(s, "claim-dev", t3, _tl()).state
    s = apply(s, "start", t3, _tl()).state
    s = apply(s, "deliver", t3, _tl(), params=Params(commit="abc", gate_cmd="x",
              gate_exit=0, evidence_path=str(_mk_evidence()))).state
    s = apply(s, "verify", t3, _tl()).state
    verbs = {(a.verb, a.task_id) for a in plan_role(s, Role.PM, "D")}
    assert ("claim-analyze", t1) in verbs
    assert ("define", t2) in verbs
    assert ("accept", t3) in verbs


def test_unconfirmed_commander_requirement_is_never_planned() -> None:
    """**确认闸在调度器里同样成立**：没确认的需求不进任何人的计划。"""
    s = budgeted(State())
    from bg_coordinator.engine import grant_confirm

    r = grant_confirm(s, line="D", title="x", by="commander:cmd",
                      said="用户原话", clock=time.time())
    assert r.ok
    s2, t = _reg(r.state, "x", origin=Origin.COMMANDER)
    assert t in {a.task_id for a in plan_role(s2, Role.PM, "D")}, "已确认的要进计划"
    # 未确认的：直接造（绕过闸，只为测调度器的过滤）
    from bg_coordinator.models import Task

    s2.tasks["R-D-99"] = Task(id="R-D-99", kind=Kind.R, line="D", title="未确认",
                              origin="commander")
    assert not [a for a in plan_role(s2, Role.PM, "D") if a.task_id == "R-D-99"]


def test_tl_plan_respects_readiness_and_grants(tmp_path: Path) -> None:
    s = budgeted(State())
    s.quota = Quota(enabled=True, per_line={"D": 2})
    s, t1 = _reg(s, "就绪")
    s = _to(s, t1, "defined")
    verbs = [a.verb for a in plan_role(s, Role.TECH_LEAD, "D")]
    assert "claim-dev" in verbs

    s = apply(s, "claim-dev", t1, _tl()).state
    acts = {a.verb for a in plan_role(s, Role.TECH_LEAD, "D")}
    assert "dispatch" in acts, "已认领且无凭证 ⇒ 调度器该让它先领凭证"

    from bg_coordinator.engine import grant_dispatch

    r = grant_dispatch(s, holder="tech-lead:TL-D:D", task_id=t1, line="D",
                       clock=time.time(), ttl=3600)
    assert r.ok, r.rejection
    acts = {a.verb for a in plan_role(r.state, Role.TECH_LEAD, "D")}
    assert "dispatch" not in acts and "start" in acts


def test_commander_plan_surfaces_expired_leases_and_conflicts() -> None:
    s = budgeted(State())
    s.merges["M-1"] = MergeRequest(merge_id="M-1", task_id="T-D-1", branch="b",
                                   commit="c", requester="tech-lead:TL-D:D",
                                   state=MergeState.REJECTED)
    acts = {a.verb for a in plan_role(s, Role.COMMANDER, "D", now=time.time())}
    assert "merge-conflict" in acts


def _mk_evidence():
    import tempfile

    f = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False)
    f.write("green\n")
    f.close()
    return Path(f.name)
