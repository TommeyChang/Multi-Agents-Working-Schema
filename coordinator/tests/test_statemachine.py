"""状态机测试——**把方案 §8.4 的可判定性检查变成真的断言**。

这些不是"跑一下看看"的测试，而是把设计推导表里的结论钉死：
可达、无死锁、确定性、`accept` 只来自 `verified`。
"""

from __future__ import annotations

import pytest

from bg_coordinator.models import Actor, Kind, Line, Role, Task, TaskState
from bg_coordinator.statemachine import (
    TRANSITIONS,
    VERBS,
    check_transition,
    is_terminal,
    next_state,
    to_legacy_token,
)

ALL_STATES = list(TaskState)


def _actor(role: Role, line: Line | None = Line.D) -> Actor:
    return Actor(role=role, name=f"{role.value}-1", line=line)


def _task(status: TaskState, line: Line = Line.D) -> Task:
    return Task(id="T-D-1", kind=Kind.T, line=line, status=status)


# ---------------------------------------------------------------------------
# 结构
# ---------------------------------------------------------------------------


def test_every_verb_has_a_transition() -> None:
    assert set(VERBS) <= set(TRANSITIONS)


def test_no_transition_is_defined_for_unknown_verb() -> None:
    assert set(TRANSITIONS) <= set(VERBS)


def test_terminal_state_has_no_outgoing_except_override() -> None:
    """终态无出口（除 override）——方案 §8.4。"""
    for verb, tr in TRANSITIONS.items():
        if verb == "override":
            continue
        assert TaskState.ACCEPTED not in tr.sources, f"{verb} 不应允许从终态出发"


def test_accepted_is_the_only_terminal_state() -> None:
    assert [s for s in ALL_STATES if is_terminal(s)] == [TaskState.ACCEPTED]


# ---------------------------------------------------------------------------
# 可达性：自 ∅ 出发能否到达每个状态
# ---------------------------------------------------------------------------


def _reachable_states() -> set[TaskState]:
    reached: set[TaskState] = set()
    frontier: set[TaskState | None] = {None}
    while frontier:
        cur = frontier.pop()
        for verb, tr in TRANSITIONS.items():
            if cur is None:
                if verb != "register":
                    continue
            elif cur not in tr.sources:
                continue
            target = tr.target
            if target is not None and target not in reached:
                reached.add(target)
                frontier.add(target)
    return reached


def test_all_states_are_reachable() -> None:
    """无不可达状态——方案 §8.4。"""
    expected = set(ALL_STATES) - {TaskState.BLOCKED}  # blocked 经 block 动词到达，见下
    assert expected <= _reachable_states()


def test_blocked_is_reachable_from_non_terminal() -> None:
    tr = TRANSITIONS["block"]
    assert TaskState.ACCEPTED not in tr.sources
    assert TaskState.BLOCKED not in tr.sources
    assert tr.target == TaskState.BLOCKED


def test_every_non_terminal_state_has_an_exit() -> None:
    """每个非终态有出口——否则死锁。"""
    for state in ALL_STATES:
        if is_terminal(state):
            continue
        exits = [
            verb
            for verb, tr in TRANSITIONS.items()
            if state in tr.sources and tr.target != state
        ]
        assert exits, f"{state} 无出口（死锁）"


# ---------------------------------------------------------------------------
# accept 只可能来自 verified —— 「形式有效是实质验收的前置」由结构保证
# ---------------------------------------------------------------------------


def test_accept_only_from_verified() -> None:
    assert TRANSITIONS["accept"].sources == frozenset({TaskState.VERIFIED})


def test_accept_rejected_from_delivered() -> None:
    """未校验即验收，结构上不可达。"""
    task = _task(TaskState.DELIVERED)
    rej = check_transition("accept", task, _actor(Role.PM))
    assert rej is not None
    assert rej.code == "E_BAD_STATE"


def test_deliver_rejected_before_in_progress() -> None:
    for state in (TaskState.REGISTERED, TaskState.ANALYZING, TaskState.DEFINED, TaskState.CLAIMED):
        task = _task(state)
        rej = check_transition("deliver", task, _actor(Role.TECH_LEAD))
        assert rej is not None, f"{state} 不应允许 deliver"


# ---------------------------------------------------------------------------
# 确定性：同一 (状态, 动词) 不得有两个出口
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", sorted(TRANSITIONS))
def test_transition_target_is_deterministic(verb: str) -> None:
    """v1 曾在 define 上犯过「同事件两出口」的错——这里钉死。"""
    tr = TRANSITIONS[verb]
    assert tr.target is None or isinstance(tr.target, TaskState)
    # 目标由运行时决定的只有这四个，且都是显式传入或回退语义
    if tr.target is None:
        assert verb in {"unblock", "override", "set-priority"}


def test_define_has_exactly_one_target() -> None:
    assert TRANSITIONS["define"].target == TaskState.DEFINED


# ---------------------------------------------------------------------------
# 角色与线路边界
# ---------------------------------------------------------------------------


#: 每个动词的**合法来源状态**——角色测试必须从合法状态出发，
#: 否则测到的是状态错误而不是角色错误。
_VERB_SOURCE: dict[str, TaskState] = {
    "claim-analyze": TaskState.REGISTERED,
    "define": TaskState.ANALYZING,
    "freeze": TaskState.DEFINED,
    "claim-dev": TaskState.DEFINED,
    "start": TaskState.CLAIMED,
    "deliver": TaskState.IN_PROGRESS,
    "verify": TaskState.DELIVERED,
    "reverify": TaskState.DELIVERED,
    "accept": TaskState.VERIFIED,
    "complete": TaskState.REGISTERED,
    "review": TaskState.REGISTERED,
    "override": TaskState.ACCEPTED,
}


@pytest.mark.parametrize(
    ("verb", "role", "allowed"),
    [
        ("claim-analyze", Role.PM, True),
        ("claim-analyze", Role.DEV, False),
        ("claim-dev", Role.TECH_LEAD, True),
        ("claim-dev", Role.PM, False),
        ("deliver", Role.TECH_LEAD, True),
        ("deliver", Role.DEV, False),
        ("accept", Role.PM, True),
        ("accept", Role.TECH_LEAD, False),
        ("complete", Role.OPS, True),
        ("complete", Role.DEV, False),
        ("review", Role.DBA, True),
        ("override", Role.COMMANDER, True),
        ("override", Role.TECH_LEAD, False),
    ],
)
def test_role_gate(verb: str, role: Role, allowed: bool) -> None:
    task = _task(_VERB_SOURCE[verb])
    rej = check_transition(verb, task, _actor(role))
    if allowed:
        assert rej is None, f"{verb} 应允许 {role}：{rej}"
    else:
        assert rej is not None and rej.code == "E_FORBIDDEN_WRITE"


def test_cross_line_is_rejected() -> None:
    task = _task(TaskState.DEFINED, line=Line.D)
    rej = check_transition("claim-dev", task, _actor(Role.TECH_LEAD, line=Line.C))
    assert rej is not None and rej.code == "E_CROSS_LINE"


def test_frozen_task_rejects_further_changes() -> None:
    task = _task(TaskState.ACCEPTED)
    rej = check_transition("deliver", task, _actor(Role.TECH_LEAD))
    assert rej is not None and rej.code == "E_FROZEN"


def test_unknown_verb_and_missing_task() -> None:
    assert check_transition("nonsense", None, _actor(Role.TECH_LEAD)).code == "E_UNKNOWN_VERB"  # type: ignore[union-attr]
    assert check_transition("deliver", None, _actor(Role.TECH_LEAD)).code == "E_UNKNOWN_ID"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# 目标状态计算
# ---------------------------------------------------------------------------


def test_unblock_returns_to_pre_block_state() -> None:
    task = _task(TaskState.BLOCKED)
    task.blocked_from = TaskState.IN_PROGRESS
    assert next_state("unblock", task) == TaskState.IN_PROGRESS


def test_override_and_priority_use_explicit_target() -> None:
    assert next_state("override", _task(TaskState.ACCEPTED), TaskState.IN_PROGRESS) == TaskState.IN_PROGRESS
    assert next_state("set-priority", _task(TaskState.DEFINED), None) is None


# ---------------------------------------------------------------------------
# 旧 token 映射（过渡期信息损失，非回归）
# ---------------------------------------------------------------------------


def test_legacy_token_mapping_covers_all_states() -> None:
    for state in ALL_STATES:
        assert to_legacy_token(state)


def test_legacy_mapping_documented_loss() -> None:
    """已定稿／已交付／已验证 都落在 [处理中]——这是**已知的过渡期损失**。"""
    assert to_legacy_token(TaskState.DEFINED) == "[处理中]"  # 正等 TL 认领 ⇒ 待处置
    assert to_legacy_token(TaskState.DELIVERED) == "[处理中]"
    assert to_legacy_token(TaskState.VERIFIED) == "[处理中]"
    assert to_legacy_token(TaskState.REGISTERED) == "[未处理]"
    assert to_legacy_token(TaskState.ACCEPTED) == "✅"
