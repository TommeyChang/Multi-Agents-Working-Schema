"""合并队列测试——方案 §6。

核心主张：**串行化能解决索引竞态，不能解决内容冲突**。
所以冲突一律拒绝并转成动作项，协调器永不自动解冲突。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from conftest import budgeted  # noqa: E402

from bg_coordinator.engine import Params, State, apply
from bg_coordinator.errors import Code
from bg_coordinator.merge import (
    check_gates,
    complete_merge,
    dispatch_next,
    in_flight_merge,
    queue,
    queue_status,
    reject_conflict,
    request_merge,
)
from bg_coordinator.models import (
    AcceptanceItem,
    AcceptanceType,
    Actor,
    Kind,
    Line,
    MergeState,
    Priority,
    Role,
    TaskState,
)


def _po() -> Actor:
    return Actor(role=Role.PO, name="po", line=None)


def _pm() -> Actor:
    return Actor(role=Role.PM, name="pm-D", line=Line.D)


def _tl() -> Actor:
    return Actor(role=Role.TECH_LEAD, name="TL-D", line=Line.D)


def _delivered(
    tmp_path: Path,
    tid: str = "T-D-1",
    changed: list[str] | None = None,
    prior: State | None = None,
    whitelist: list[str] | None = None,
) -> State:
    """把一条目推进到 delivered。**任何一步被拒即停并抛错**——否则会静默留在中间态。"""
    ev = tmp_path / f"{tid}.log"
    ev.write_text("2182 passed", encoding="utf-8")
    wl = whitelist if whitelist is not None else ["data_access/**"]
    # 实际改动默认落在白名单内（`dir/**` → `dir/probe.py`）
    if changed is None:
        probe = wl[0].rstrip("/").replace("/**", "") if wl else "data_access"
        changed = [f"{probe}/probe.py"]
    s = prior if prior is not None else budgeted(State())
    steps: list[tuple[str, Actor, Params | None]] = [
        (
            "register",
            _po(),
            Params(title="t", line=Line.D, kind=Kind.R, priority=Priority.P1),
        ),
        ("claim-analyze", _pm(), None),
        (
            "define",
            _pm(),
            Params(
                whitelist=wl,
                frozen=[],
                acceptance=[AcceptanceItem(type=AcceptanceType.TEST, cmd="pytest -q", desc="绿")],
            ),
        ),
        ("claim-dev", _tl(), None),
        ("start", _tl(), None),
        (
            "deliver",
            _tl(),
            Params(
                commit="abc1234",
                gate_cmd="uv run pytest -q",
                gate_exit=0,
                evidence_path=str(ev),
                changed_files=changed,
            ),
        ),
    ]
    for verb, actor, params in steps:
        r = apply(s, verb, tid, actor, params=params)
        assert r.ok, f"{tid} 的 {verb} 被拒：{r.rejection}"
        s = r.state
    return s


# ---------------------------------------------------------------------------
# 各闸
# ---------------------------------------------------------------------------


def _req(s, *a, **kw):
    """入队便捷入口——**注入一个"链位通过"的桩**。

    链位判据本身（分叉／插队／撞号）由 `tests/test_migration_gate.py` 逐条测；
    这里测的是其余各闸，用一个恒过的桩把它们与迁移判据解耦。
    真实装配在 CLI（`cli._migration_checker`），它从 git 读两张图。
    """
    kw.setdefault("migration_check", lambda files, base: "")
    return request_merge(s, *a, **kw)


def test_request_merge_ok(tmp_path: Path) -> None:
    s = _delivered(tmp_path)
    r = _req(
        s, "T-D-1", branch="dev/TD1", commit="abc1234", requester="TL-D",
        changed_files=["data_access/kline/follow.py"], clock=1.0,
    )
    assert r.ok
    assert len(queue(r.state)) == 1


def test_gate_1_rejects_undelivered(tmp_path: Path) -> None:
    """无实测即合并——闸 1。"""
    s = _delivered(tmp_path)
    s.tasks["T-D-1"].status = TaskState.IN_PROGRESS
    r = _req(
        s, "T-D-1", branch="b", commit="c", requester="TL-D", changed_files=[], clock=1.0
    )
    assert not r.ok
    assert r.rejection is not None and r.rejection.code == Code.E_NOT_DELIVERED


def test_gate_2_rejects_out_of_scope(tmp_path: Path) -> None:
    """越范围合入——闸 2（条目白名单是 `data_access/**`，却改了 `broker_gateway/**`）。"""
    s = _delivered(tmp_path, changed=["data_access/ok.py"])
    r = _req(
        s, "T-D-1", branch="b", commit="c", requester="TL-D",
        changed_files=["data_access/ok.py", "broker_gateway/other.py"], clock=1.0,
    )
    assert not r.ok
    assert r.rejection is not None and r.rejection.code == Code.E_OUT_OF_SCOPE


def test_gate_3_rejects_unreadable_evidence(tmp_path: Path) -> None:
    """证伪「台账先于核验」——闸 3。"""
    s = _delivered(tmp_path)
    old = s.tasks["T-D-1"].evidence[-1]
    s.tasks["T-D-1"].evidence[-1] = replace(old, evidence_path=str(tmp_path / "gone.log"))
    r = _req(
        s, "T-D-1", branch="b", commit="c", requester="TL-D",
        changed_files=["data_access/kline/follow.py"], clock=1.0,
    )
    assert not r.ok
    assert r.rejection is not None and r.rejection.code == Code.E_NO_EVIDENCE


def test_gate_3_rejects_nonzero_gate(tmp_path: Path) -> None:
    s = _delivered(tmp_path)
    s.tasks["T-D-1"].evidence[-1] = replace(s.tasks["T-D-1"].evidence[-1], gate_exit=1)
    gates = check_gates(s, s.tasks["T-D-1"], ["data_access/kline/follow.py"], "", "")
    assert not gates.passed
    assert gates.first is not None and gates.first.code == Code.E_GATE_NONZERO


def test_gate_4_rejects_stale_base(tmp_path: Path) -> None:
    """过期基座合入——闸 4。"""
    s = _delivered(tmp_path)
    r = _req(
        s, "T-D-1", branch="b", commit="c", requester="TL-D",
        changed_files=["data_access/kline/follow.py"], base_commit="old123", main_head="new456",
        clock=1.0,
    )
    assert not r.ok
    assert r.rejection is not None and r.rejection.code == Code.E_STALE_BASE


def test_gate_5_serializes_merges(tmp_path: Path) -> None:
    """同一时刻只有一次合并——**这条取代了现在那套 `diff --stat` 复核补丁**。"""
    s = _delivered(tmp_path, "T-D-1", whitelist=["data_access/**"])
    s = _delivered(tmp_path, "T-D-2", prior=s, whitelist=["broker_gateway/**"])
    assert s.tasks["T-D-1"].status == TaskState.DELIVERED
    assert s.tasks["T-D-2"].status == TaskState.DELIVERED
    a = _req(
        s, "T-D-1", branch="b1", commit="c1", requester="TL-D",
        changed_files=["data_access/kline/follow.py"], clock=1.0,
    )
    b = _req(
        a.state, "T-D-2", branch="b2", commit="c2", requester="TL-D",
        changed_files=["broker_gateway/composition.py"], clock=2.0,
    )
    assert b.ok
    first = dispatch_next(b.state)
    assert first.ok
    assert in_flight_merge(first.state) is not None

    second = dispatch_next(first.state)
    assert not second.ok
    assert second.rejection is not None and second.rejection.code == Code.E_BUSY


def test_dispatch_is_fifo(tmp_path: Path) -> None:
    s = _delivered(tmp_path, "T-D-1", whitelist=["data_access/**"])
    s = _delivered(tmp_path, "T-D-2", prior=s, whitelist=["broker_gateway/**"])
    assert s.tasks["T-D-1"].status == TaskState.DELIVERED
    assert s.tasks["T-D-2"].status == TaskState.DELIVERED
    a = _req(
        s, "T-D-1", branch="b1", commit="c1", requester="TL-D",
        changed_files=["data_access/kline/follow.py"], clock=1.0,
    )
    b = _req(
        a.state, "T-D-2", branch="b2", commit="c2", requester="TL-D",
        changed_files=["broker_gateway/composition.py"], clock=2.0,
    )
    head = queue(b.state)[0]
    assert head.task_id == "T-D-1"  # 先请求的排前面


# ---------------------------------------------------------------------------
# 冲突：永不自动解
# ---------------------------------------------------------------------------


def test_conflict_is_rejected_and_becomes_action_item(tmp_path: Path) -> None:
    """**冲突是语义问题，不是时序问题**——拒绝并转动作项给 TL。"""
    s = _delivered(tmp_path)
    r = _req(
        s, "T-D-1", branch="b", commit="c", requester="TL-D",
        changed_files=["data_access/kline/follow.py"], clock=1.0,
    )
    started = dispatch_next(r.state)
    mid = started.detail["merge_id"]

    conflicted = reject_conflict(started.state, mid, "composition.py 冲突")
    assert not conflicted.ok
    assert conflicted.rejection is not None
    assert conflicted.rejection.code == Code.E_CONTENT_CONFLICT
    assert conflicted.rejection.owner == "tech-lead"

    action = conflicted.detail["action_item"]
    assert action["kind"] == "resolve_conflict"
    assert action["assignee_role"] == "tech-lead"
    assert "禁自动取侧" in action["note"]
    assert conflicted.state.merges[mid].state == MergeState.REJECTED


def test_complete_merge_marks_terminal(tmp_path: Path) -> None:
    s = _delivered(tmp_path)
    r = _req(
        s, "T-D-1", branch="b", commit="c", requester="TL-D",
        changed_files=["data_access/kline/follow.py"], clock=1.0,
    )
    started = dispatch_next(r.state)
    mid = started.detail["merge_id"]
    done = complete_merge(started.state, mid, "merge-commit-1")
    assert done.ok
    assert done.state.merges[mid].state == MergeState.MERGED
    assert done.state.merges[mid].result_commit == "merge-commit-1"


def test_queue_status_is_observable(tmp_path: Path) -> None:
    """**不可观测的队列就是一条新的隐性依赖**。"""
    s = _delivered(tmp_path)
    r = _req(
        s, "T-D-1", branch="b", commit="c", requester="TL-D",
        changed_files=["data_access/kline/follow.py"], clock=1.0,
    )
    st = queue_status(r.state)
    assert len(st["queued"]) == 1  # type: ignore[arg-type]
    assert st["in_flight"] is None


def test_dispatch_on_empty_queue_is_rejected() -> None:
    r = dispatch_next(budgeted(State()))
    assert not r.ok
    assert r.rejection is not None and r.rejection.code == Code.E_BAD_STATE


def test_queue_item_does_not_hold_task_ownership(tmp_path: Path) -> None:
    """**`claim` 与 `request_merge` 必须解耦**——防等待环（方案 §6.4）。"""
    s = _delivered(tmp_path)
    r = _req(
        s, "T-D-1", branch="b", commit="c", requester="TL-D",
        changed_files=["data_access/kline/follow.py"], clock=1.0,
    )
    # 入队不改变条目的认领人，也不把它标为终态
    assert r.state.tasks["T-D-1"].owner == "TL-D"
    assert r.state.tasks["T-D-1"].status == TaskState.DELIVERED


# ---------------------------------------------------------------------------
# 闸 4.5：迁移链位（**放号只保证号唯一，保证不了链位唯一**）
# ---------------------------------------------------------------------------


def _delivered_migration(tmp_path: Path):
    """造一个**白名单允许迁移件**的已交付条目——这样闸 2 不会先把它拦掉。"""
    s = _delivered(tmp_path)
    s.tasks["T-D-1"].whitelist = ["alembic/versions/**"]
    return s


def test_chain_gate_blocks_forked_migration(tmp_path: Path) -> None:
    """判据说"分叉"就拒——这就是"两个各自合法的迁移件合起来是两个 head"的收口点。"""
    s = _delivered_migration(tmp_path)
    r = _req(
        s, "T-D-1", branch="dev/TD1", commit="b" * 8, requester="TL-D",
        changed_files=["alembic/versions/0037_x.py"],
        migration_check=lambda files, base: "[3] 有 2 个 head：['0037', '0038']——链位分叉",
    )
    assert not r.ok
    assert r.event is not None
    assert "E_INTEGRITY" in r.event.result, r.event.result


def test_chain_gate_passes_when_judgement_is_clean(tmp_path: Path) -> None:
    """负例锚点：判据说通过就必须能入队——否则这条闸是噪声。"""
    s = _delivered_migration(tmp_path)
    r = _req(
        s, "T-D-1", branch="dev/TD1", commit="b" * 8, requester="TL-D",
        changed_files=["alembic/versions/0037_x.py"],
        migration_check=lambda files, base: "",
    )
    assert r.ok, r.event.result if r.event else ""


def test_chain_gate_fails_closed_without_judgement(tmp_path: Path) -> None:
    """**没有判据就拒**——宁可不合，也不假装通过。

    注入点只有一个（CLI 的 `_migration_checker`）；若哪天漏了，故障方向是"合不进去"
    （看得见、可修），而不是"分叉合进去了"（看不见、要回滚）。
    """
    s = _delivered_migration(tmp_path)
    r = request_merge(
        s, "T-D-1", branch="dev/TD1", commit="b" * 8, requester="TL-D",
        changed_files=["alembic/versions/0037_x.py"],
    )
    assert not r.ok
    assert r.event is not None
    assert "E_INCOMPLETE" in r.event.result, r.event.result
