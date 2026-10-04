"""引擎测试——**完整生命周期**与各条不变量在真实流程里的行为。

这里跑的是方案 §3.1 的端到端七步：
register → claim-analyze → define → freeze → claim-dev → start → deliver → verify → accept
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import budgeted, closure_item  # noqa: E402

from bg_coordinator.engine import Params, State, alloc_number, apply, format_id
from bg_coordinator.errors import Code
from bg_coordinator.models import (
    AcceptanceItem,
    AcceptanceType,
    Actor,
    Kind,
    Line,
    Priority,
    Role,
    TaskState,
)


def _pm() -> Actor:
    return Actor(role=Role.PM, name="pm-D", line=Line.D)


def _tl() -> Actor:
    return Actor(role=Role.TECH_LEAD, name="TL-D", line=Line.D)


def _po() -> Actor:
    return Actor(role=Role.PO, name="po", line=None)


def _acceptance() -> list[AcceptanceItem]:
    return [
        AcceptanceItem(type=AcceptanceType.TEST, cmd="pytest -q", desc="全量绿"),
        AcceptanceItem(type=AcceptanceType.NEGATIVE, desc="未注入仍 200 ⇒ 必红"),
        closure_item(),
    ]


def _run(state: State, verb: str, tid: str, actor: Actor, params: Params | None = None, **kw):
    result = apply(state, verb, tid, actor, params=params, **kw)
    assert result.ok, f"{verb} 应成功，却被拒：{result.rejection}"
    return result


def _to_verified(state: State, tmp_path: Path, tid: str = "T-D-1") -> State:
    """把一条目推进到 verified —— 后续测试的公共前置。"""
    ev = tmp_path / "gate.log"
    ev.write_text("2182 passed", encoding="utf-8")

    s = _run(
        state,
        "register",
        tid,
        _po(),
        Params(title="示例需求", line=Line.D, kind=Kind.R, src="user", priority=Priority.P1),
    ).state
    s = _run(s, "claim-analyze", tid, _pm()).state
    s = _run(
        s,
        "define",
        tid,
        _pm(),
        Params(whitelist=["data_access/**"], frozen=[], acceptance=_acceptance()),
    ).state
    s = _run(s, "claim-dev", tid, _tl()).state
    s = _run(s, "start", tid, _tl()).state
    s = _run(
        s,
        "deliver",
        tid,
        _tl(),
        Params(
            commit="abc1234",
            gate_cmd="uv run pytest -q",
            gate_exit=0,
            evidence_path=str(ev),
            changed_files=["data_access/kline/follow.py"],
        ),
    ).state
    return _run(s, "verify", tid, _tl()).state


# ---------------------------------------------------------------------------
# 端到端
# ---------------------------------------------------------------------------


def test_full_lifecycle_reaches_accepted(tmp_path: Path) -> None:
    s = _to_verified(budgeted(State()), tmp_path)
    task = s.tasks["T-D-1"]
    assert task.status == TaskState.VERIFIED
    assert task.kind == Kind.T  # R → T，**共用同一个 id**
    assert task.round == 1

    result = apply(s, "accept", "T-D-1", _pm())
    assert result.ok
    assert result.state.tasks["T-D-1"].status == TaskState.ACCEPTED
    assert result.state.tasks["T-D-1"].acceptor == "pm-D"


def test_every_verb_emits_an_event(tmp_path: Path) -> None:
    """事件是协调器唯一的权威落地物——每个动作都必须留痕。"""
    s = budgeted(State())
    ev = tmp_path / "g.log"
    ev.write_text("ok", encoding="utf-8")
    verbs = ["register", "claim-analyze", "define", "claim-dev", "start", "deliver", "verify"]
    actors = [_po(), _pm(), _pm(), _tl(), _tl(), _tl(), _tl()]
    params: list[Params | None] = [
        Params(title="t", line=Line.D, kind=Kind.R, priority=Priority.P1),
        None,
        Params(whitelist=["a/**"], frozen=[], acceptance=_acceptance()),
        None,
        None,
        Params(commit="c", gate_cmd="pytest", gate_exit=0, evidence_path=str(ev)),
        None,
    ]
    for verb, actor, p in zip(verbs, actors, params, strict=True):
        r = apply(s, verb, "T-D-1", actor, params=p)
        assert r.ok, f"{verb} 失败：{r.rejection}"
        assert r.event is not None
        assert r.event.verb == verb
        s = r.state


def test_event_seq_is_monotonic(tmp_path: Path) -> None:
    """每次动作都递增 seq——事件序号的单调性。"""
    s = budgeted(State())
    seen: list[int] = []
    for verb, actor, params in (
        ("register", _po(), Params(title="t", line=Line.D, kind=Kind.R)),
        ("claim-analyze", _pm(), None),
    ):
        r = apply(s, verb, "T-D-1", actor, params=params)
        assert r.ok and r.event is not None
        seen.append(r.event.seq)
        s = r.state
        assert seen == sorted(seen)


# ---------------------------------------------------------------------------
# 拒绝路径：每条都必须带责任人与建议
# ---------------------------------------------------------------------------


def test_define_without_whitelist_is_rejected() -> None:
    s = _run(budgeted(State()), "register", "T-D-1", _po(), Params(title="t", line=Line.D, kind=Kind.R)).state
    s = _run(s, "claim-analyze", "T-D-1", _pm()).state
    r = apply(s, "define", "T-D-1", _pm(), params=Params(acceptance=_acceptance(), whitelist=[]))
    assert not r.ok
    assert r.rejection is not None
    assert r.rejection.code == Code.E_INCOMPLETE
    assert r.rejection.owner  # 必须有责任人
    assert r.state.tasks["T-D-1"].status == TaskState.ANALYZING  # **状态不变**


def test_claim_dev_requires_priority() -> None:
    """PO 的产出必须有承载——未定优先级不得派工。"""
    s = _run(budgeted(State()), "register", "T-D-1", _po(), Params(title="t", line=Line.D, kind=Kind.R)).state
    s = _run(s, "claim-analyze", "T-D-1", _pm()).state
    s = _run(
        s, "define", "T-D-1", _pm(), Params(whitelist=["a/**"], frozen=[], acceptance=_acceptance())
    ).state
    r = apply(s, "claim-dev", "T-D-1", _tl())
    assert not r.ok
    assert r.rejection is not None and r.rejection.code == Code.E_NOT_READY
    assert "优先级" in r.rejection.message


def test_claim_dev_rejected_when_dep_unfinished() -> None:
    s = _run(
        budgeted(State()), "register", "T-D-1", _po(), Params(title="dep", line=Line.D, kind=Kind.R)
    ).state
    s = _run(
        s,
        "register",
        "T-D-2",
        _po(),
        Params(title="child", line=Line.D, kind=Kind.R, deps=["T-D-1"], priority=Priority.P0),
    ).state
    s = _run(s, "claim-analyze", "T-D-2", _pm()).state
    s = _run(
        s, "define", "T-D-2", _pm(), Params(whitelist=["b/**"], frozen=[], acceptance=_acceptance())
    ).state
    r = apply(s, "claim-dev", "T-D-2", _tl())
    assert not r.ok
    assert r.rejection is not None and r.rejection.code == Code.E_NOT_READY
    assert "T-D-1" in r.rejection.message


def test_deliver_requires_evidence_path_and_gate() -> None:
    s2 = _run(
        budgeted(State()),
        "register",
        "T-D-3",
        _po(),
        Params(title="t", line=Line.D, kind=Kind.R, priority=Priority.P1),
    ).state
    s2 = _run(s2, "claim-analyze", "T-D-3", _pm()).state
    s2 = _run(
        s2, "define", "T-D-3", _pm(), Params(whitelist=["a/**"], frozen=[], acceptance=_acceptance())
    ).state
    s2 = _run(s2, "claim-dev", "T-D-3", _tl()).state
    s2 = _run(s2, "start", "T-D-3", _tl()).state

    r = apply(s2, "deliver", "T-D-3", _tl(), params=Params(commit="", gate_cmd="", gate_exit=0))
    assert not r.ok
    assert r.rejection is not None and r.rejection.code == Code.E_NO_ARTIFACT

    r2 = apply(
        s2,
        "deliver",
        "T-D-3",
        _tl(),
        params=Params(commit="c", gate_cmd="pytest", gate_exit=1, evidence_path="x"),
    )
    assert not r2.ok
    assert r2.rejection is not None and r2.rejection.code == Code.E_GATE_NONZERO

    r3 = apply(
        s2,
        "deliver",
        "T-D-3",
        _tl(),
        params=Params(commit="c", gate_cmd="pytest", gate_exit=0, evidence_path=""),
    )
    assert not r3.ok
    assert r3.rejection is not None and r3.rejection.code == Code.E_NO_EVIDENCE


def test_deliver_rejects_out_of_scope_changes() -> None:
    """越白名单交付即拒——「范围白名单」是可校验的，不是愿望。"""
    ev_path = Path("/tmp/does-not-matter.log")
    s = _run(
        budgeted(State()),
        "register",
        "T-D-4",
        _po(),
        Params(title="t", line=Line.D, kind=Kind.R, priority=Priority.P1),
    ).state
    s = _run(s, "claim-analyze", "T-D-4", _pm()).state
    s = _run(
        s, "define", "T-D-4", _pm(), Params(whitelist=["a/**"], frozen=[], acceptance=_acceptance())
    ).state
    s = _run(s, "claim-dev", "T-D-4", _tl()).state
    s = _run(s, "start", "T-D-4", _tl()).state
    r = apply(
        s,
        "deliver",
        "T-D-4",
        _tl(),
        params=Params(
            commit="c",
            gate_cmd="pytest",
            gate_exit=0,
            evidence_path=str(ev_path),
            changed_files=["a/ok.py", "b/bad.py"],
        ),
    )
    assert not r.ok
    assert r.rejection is not None and r.rejection.code == Code.E_OUT_OF_SCOPE
    assert "b/bad.py" in r.rejection.message


def test_accept_rejects_unmet_acceptance(tmp_path: Path) -> None:
    s = _to_verified(budgeted(State()), tmp_path)
    # 人为把验收项改成引用不存在的证据
    s.tasks["T-D-1"].acceptance = [
        AcceptanceItem(type=AcceptanceType.EVIDENCE, path=str(tmp_path / "gone")),
        closure_item(),  # 闭环项给上，这条测的是"证据缺失"而不是"没闭环"
    ]
    r = apply(s, "accept", "T-D-1", _pm())
    assert not r.ok
    assert r.rejection is not None and r.rejection.code == Code.E_UNMET


def test_cannot_accept_without_verify(tmp_path: Path) -> None:
    s = _to_verified(budgeted(State()), tmp_path)
    s.tasks["T-D-1"].status = TaskState.DELIVERED  # 人为回退到未校验
    r = apply(s, "accept", "T-D-1", _pm())
    assert not r.ok
    assert r.rejection is not None and r.rejection.code == Code.E_BAD_STATE


# ---------------------------------------------------------------------------
# 乐观并发 / 幂等 / 冻结
# ---------------------------------------------------------------------------


def test_expect_ver_conflict_stops_stale_write() -> None:
    """治「陈旧视图」——这是 82 条重试提交的根因。

    用 `start` 做探针：从 claimed 出发，第一次成功、同 `expect_ver` 的第二次必撞冲突。
    """
    s = _run(
        budgeted(State()),
        "register",
        "T-D-1",
        _po(),
        Params(title="t", line=Line.D, kind=Kind.R, priority=Priority.P1),
    ).state
    s = _run(s, "claim-analyze", "T-D-1", _pm()).state
    s = _run(
        s, "define", "T-D-1", _pm(), Params(whitelist=["a/**"], frozen=[], acceptance=_acceptance())
    ).state
    s = _run(s, "claim-dev", "T-D-1", _tl()).state

    ver = s.tasks["T-D-1"].ver
    first = apply(s, "start", "T-D-1", _tl(), expect_ver=ver)
    assert first.ok
    # 用同一个（现已陈旧的）expect_ver 再动一次 ⇒ 必撞乐观并发闸
    stale = apply(first.state, "reverify", "T-D-1", _tl(), expect_ver=ver,
                  params=Params(reason="探针"))
    assert not stale.ok
    assert stale.rejection is not None and stale.rejection.code == Code.E_CONFLICT


def test_rejected_action_still_emits_an_event() -> None:
    """拒绝也要留痕——报告「异常」节的来源。"""
    s = _run(budgeted(State()), "register", "T-D-1", _po(), Params(title="t", line=Line.D, kind=Kind.R)).state
    r = apply(s, "claim-dev", "T-D-1", _tl())
    assert not r.ok
    assert r.event is not None
    assert r.event.result.startswith("rejected:")
    # 该拒绝来自状态机层（registered 不允许 claim-dev）⇒ 责任人字段来自条目而非发起人
    assert "message" in r.event.detail


def test_accepted_is_frozen(tmp_path: Path) -> None:
    s = _to_verified(budgeted(State()), tmp_path)
    s = _run(s, "accept", "T-D-1", _pm()).state
    r = apply(s, "deliver", "T-D-1", _tl(), params=Params(commit="c", gate_cmd="x", gate_exit=0))
    assert not r.ok
    assert r.rejection is not None and r.rejection.code == Code.E_FROZEN


def test_override_requires_reason(tmp_path: Path) -> None:
    s = _to_verified(budgeted(State()), tmp_path)
    r = apply(s, "override", "T-D-1", Actor(role=Role.COMMANDER, name="c"), params=Params(reason=""))
    assert not r.ok
    assert r.rejection is not None and r.rejection.code == Code.E_NO_REASON

    ok = apply(
        s,
        "override",
        "T-D-1",
        Actor(role=Role.COMMANDER, name="c"),
        params=Params(reason="用户直令", target_state=TaskState.ACCEPTED),
    )
    assert ok.ok


# ---------------------------------------------------------------------------
# 返工 / 阻塞
# ---------------------------------------------------------------------------


def test_rework_increments_round_and_keeps_history(tmp_path: Path) -> None:
    """返工：round +1，**失败轮次保留**（溯源需要，不是垃圾）——方案 §4.9.5。"""
    s = _to_verified(budgeted(State()), tmp_path)
    ev2 = tmp_path / "gate2.log"
    ev2.write_text("ok", encoding="utf-8")

    s = _run(s, "reverify", "T-D-1", _tl(), Params(reason="门禁红了，需返工")).state
    assert s.tasks["T-D-1"].status == TaskState.IN_PROGRESS
    s = _run(
        s,
        "deliver",
        "T-D-1",
        _tl(),
        Params(commit="def5678", gate_cmd="pytest", gate_exit=0, evidence_path=str(ev2)),
    ).state
    task = s.tasks["T-D-1"]
    assert task.round == 2
    assert len(task.evidence) == 2  # 两轮都在
    assert task.evidence[0].commit == "abc1234"
    assert task.evidence[1].commit == "def5678"


def test_block_remembers_and_unblock_restores(tmp_path: Path) -> None:
    s = _to_verified(budgeted(State()), tmp_path)
    s = _run(s, "reverify", "T-D-1", _tl(), Params(reason="返工")).state  # → in_progress
    s = _run(s, "block", "T-D-1", _tl(), Params(reason="等外部依赖")).state
    assert s.tasks["T-D-1"].status == TaskState.BLOCKED
    assert s.tasks["T-D-1"].blocked_from == TaskState.IN_PROGRESS

    s = _run(s, "unblock", "T-D-1", _tl()).state
    assert s.tasks["T-D-1"].status == TaskState.IN_PROGRESS
    assert s.tasks["T-D-1"].blocked_from is None


# ---------------------------------------------------------------------------
# OPS / DBA
# ---------------------------------------------------------------------------


def test_ops_completes_own_line() -> None:
    ops = Actor(role=Role.OPS, name="ops", line=Line.OPS)
    s = _run(
        budgeted(State()), "register", "OPS-1", ops,
        Params(title="巡检", line=Line.OPS, kind=Kind.T),
    ).state
    r = apply(s, "complete", "OPS-1", ops, params=Params(reason="已执行留痕"))
    assert r.ok
    assert r.state.tasks["OPS-1"].status == TaskState.ACCEPTED


def test_dba_review_completes() -> None:
    dba = Actor(role=Role.DBA, name="dba", line=Line.D)
    s = _run(
        budgeted(State()), "register", "T-D-5", dba,
        Params(title="迁移评审", line=Line.D, kind=Kind.T),
    ).state
    r = apply(s, "review", "T-D-5", dba, params=Params(reason="评审通过"))
    assert r.ok


# ---------------------------------------------------------------------------
# 发号（取代 registry.md 的整类问题）
# ---------------------------------------------------------------------------


def test_alloc_number_is_monotonic_and_atomic() -> None:
    s = budgeted(State())
    assert alloc_number(s, "T-D") == 1
    assert alloc_number(s, "T-D") == 2
    assert alloc_number(s, "T-D") == 3
    assert alloc_number(s, "T-C") == 1  # 线内独立计数


def test_alloc_number_explicit_seq_wins() -> None:
    s = budgeted(State())
    assert alloc_number(s, "T-D", seq_alloc=100) == 100
    assert alloc_number(s, "T-D") == 101


@pytest.mark.parametrize(
    ("family", "n", "expected"),
    [("T-D", 194, "T-D-194"), ("R-ACL", 67, "R-ACL-67"), ("OPS", 162, "OPS-162"), ("alembic", 34, "0034")],
)
def test_format_id(family: str, n: int, expected: str) -> None:
    assert format_id(family, n) == expected


# ---------------------------------------------------------------------------
# 不可变性：引擎不修改传入状态
# ---------------------------------------------------------------------------


def test_apply_does_not_mutate_input_state() -> None:
    s = _run(budgeted(State()), "register", "T-D-1", _po(), Params(title="t", line=Line.D, kind=Kind.R)).state
    snapshot = s.to_dict()
    apply(s, "claim-analyze", "T-D-1", _pm())
    assert s.to_dict() == snapshot


# ---------------------------------------------------------------------------
# 阻断与返工**必须带原因**——理由就是它们唯一的信息
# ---------------------------------------------------------------------------


def test_block_requires_reason(tmp_path: Path) -> None:
    """阻断不给原因 ⇒ 拒。不许用「（未注明）」兜底。

    理由：阻断是把一件在办的事停下来。没有原因，事后没人知道
    该由谁、在什么条件下解阻——这个阻断动作本身就没有信息。
    """
    s = _to_verified(budgeted(State()), tmp_path)
    r = apply(s, "block", "T-D-1", _tl(), params=Params(reason="   "))
    assert not r.ok
    assert r.rejection is not None and r.rejection.code == Code.E_NO_REASON
    assert r.state.tasks["T-D-1"].status == TaskState.VERIFIED  # **状态未变**


def test_block_with_reason_records_it(tmp_path: Path) -> None:
    s = _to_verified(budgeted(State()), tmp_path)
    r = apply(s, "block", "T-D-1", _tl(), params=Params(reason="等 DBA 评审回执"))
    assert r.ok
    task = r.state.tasks["T-D-1"]
    assert task.status == TaskState.BLOCKED
    assert task.block_reason == "等 DBA 评审回执"  # 原样记录，不加工


def test_reverify_requires_reason(tmp_path: Path) -> None:
    """返工不给原因 ⇒ 拒——与阻断同理：退回也要说清为什么。"""
    s = _to_verified(budgeted(State()), tmp_path)
    r = apply(s, "reverify", "T-D-1", _tl(), params=Params(reason=""))
    assert not r.ok
    assert r.rejection is not None and r.rejection.code == Code.E_NO_REASON
    assert r.state.tasks["T-D-1"].status == TaskState.VERIFIED


def test_reverify_records_reason_in_chain(tmp_path: Path) -> None:
    """返工原因要进**证据链**，不能只留在对话里。"""
    s = _to_verified(budgeted(State()), tmp_path)
    r = apply(s, "reverify", "T-D-1", _tl(), params=Params(reason="缺负例用例"))
    assert r.ok
    assert r.event is not None
    assert r.event.detail["reason"] == "缺负例用例"
