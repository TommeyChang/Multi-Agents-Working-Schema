"""逻辑闭环测试——**每个角色都得有入口与出口，每条链都得能走通**。

"闭环"不是形容词。它在这里有三条可执行的判据：

① **状态机闭环**：每个非终态都有出口；终态除 `override` 外无出口；
② **角色链闭环**：每个角色的输入来自谁、输出交给谁，都在体系内；
③ **端到端闭环**：按角色把整条链走一遍——登记 → 分析 → 定稿 → 认领 → 交付 → 校验 → 验收。

任一条断了，体系就有"走不通的路"或"接不上的手"。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from conftest import budgeted, closure_item  # noqa: E402

from bg_coordinator.engine import CONFIRM_TTL_SECONDS, Params, State, apply, grant_confirm
from bg_coordinator.models import (
    CATEGORY_OWNER,
    Actor,
    Kind,
    Line,
    Origin,
    Priority,
    Role,
    TaskState,
)
from bg_coordinator.schema import ROLES, all_verbs, state_verbs
from bg_coordinator.statemachine import TRANSITIONS, is_terminal

# ---------------------------------------------------------------------------
# ① 状态机闭环
# ---------------------------------------------------------------------------


def test_every_non_terminal_state_has_an_exit() -> None:
    """每个非终态都有出口——否则流程会停在那里，谁也推不动。"""
    unreachable = []
    for state in TaskState:
        if is_terminal(state):
            continue
        exits = [v for v, tr in TRANSITIONS.items() if state in tr.sources and tr.target != state]
        if not exits:
            unreachable.append(state.value)
    assert not unreachable, f"这些状态无出口（死锁）：{unreachable}"


def test_terminal_state_has_no_exit_except_override() -> None:
    """终态只留一个逃生口：`override`。"""
    for verb, tr in TRANSITIONS.items():
        if verb == "override":
            continue
        assert TaskState.ACCEPTED not in tr.sources, f"{verb} 不该允许从终态出发"


def test_every_state_is_reachable_from_start() -> None:
    """自空状态出发，每个状态都走得到——没有"设计了却永远到不了"的状态。"""
    reached: set[TaskState] = set()
    frontier: set[TaskState | None] = {None}
    while frontier:
        cur = frontier.pop()
        for verb, tr in TRANSITIONS.items():
            if cur is None:
                if verb not in {"register", "raise"}:
                    continue
            elif cur not in tr.sources:
                continue
            if tr.target is not None and tr.target not in reached:
                reached.add(tr.target)
                frontier.add(tr.target)
    never = {s.value for s in TaskState if s not in reached and s is not TaskState.BLOCKED}
    assert not never, f"这些状态从起点不可达：{never}"


# ---------------------------------------------------------------------------
# ② 角色链闭环
# ---------------------------------------------------------------------------


def test_every_role_has_an_owner_inside_the_system() -> None:
    """每个角色的归属要么是用户，要么是体系内的另一个角色——**不能悬空**。"""
    keys = {r.key for r in ROLES}
    for r in ROLES:
        assert r.reports_to == "user" or r.reports_to in keys, (
            f"{r.key} 归属 {r.reports_to} 不在体系内"
        )


def test_every_role_is_reachable_by_dispatch_or_is_a_root() -> None:
    """每个角色要么被人/上级派到，要么本身就是根（独立会话·归用户）。

    悬空的角色 = 定义了但没人能启用它。
    """
    dispatched = {t for r in ROLES for t in r.dispatch}
    roots = {r.key for r in ROLES if r.reports_to == "user"}
    for r in ROLES:
        assert r.key in dispatched or r.key in roots, f"{r.key} 既无人派，也不是根"


def test_every_role_can_act_on_something() -> None:
    """每个角色至少能执行**一个**状态动词——否则它在流程里没有位置。"""
    for r in ROLES:
        acts = [v for v in state_verbs() if r.key in _verb_roles_canonical(v)]
        assert acts, f"{r.key} 在状态机里没有任何可执行动作"


def _verb_roles_canonical(verb: str) -> set[str]:
    from bg_coordinator.models import canonical_role

    return {canonical_role(x.value) for x in TRANSITIONS[verb].roles}


def test_category_owners_can_all_act() -> None:
    """决定类别指定的裁决者，必须真的能执行相关动作。

    否则会出现"规定由他裁，但他没有那个权限"的空转。
    """
    # technical 归 TL：TL 必须能认领开发，否则"规定由他裁"却没有执行手段
    claim_dev_roles = _verb_roles_canonical("claim-dev")
    assert CATEGORY_OWNER["technical"] == "tech-lead"
    assert "tech-lead" in claim_dev_roles, "technical 归 TL，但 TL 不能认领开发"

    # design / cross_line 归 PO：PO 必须能派 PM（体现在角色表里）
    po = next(r for r in ROLES if r.key == "po")
    assert "pm" in po.dispatch, "design 归 PO，但 PO 派不出 PM 去落设计"


# ---------------------------------------------------------------------------
# ③ 端到端闭环：按角色走完整条链
# ---------------------------------------------------------------------------


def _actor(role: Role, name: str, line: str | None = "D") -> Actor:
    return Actor(role=role, name=name, line=line)


def _run(state: State, verb: str, tid: str, actor: Actor, params: Params | None = None):
    r = apply(state, verb, tid, actor, params=params)
    assert r.ok, f"{verb} 应成功却被拒：{r.rejection}"
    return r.state


def test_full_chain_closes_with_every_role(tmp_path: Path) -> None:
    """**整条链走通一次**——每一步都由它该由的角色发起。

    这是"闭环"最实在的判据：不是看图上连不连，而是**实际走一遍**。
    """
    ev = tmp_path / "gate.log"
    ev.write_text("2182 passed", encoding="utf-8")

    pm = _actor(Role.PM, "pm-D")
    tl = _actor(Role.TECH_LEAD, "TL-D")
    s = budgeted(State())

    # ① 用户确认 → commander 登记（commander 发需求的前置闸：COORDINATION §六·五）
    conf = grant_confirm(
        s, line="D", title="K 线取数", by="user:用户", said="用户原话：要能看 K 线",
        clock=time.time(), ttl=CONFIRM_TTL_SECONDS,
    )
    assert conf.ok, f"confirm 应成功却被拒：{conf.rejection}"
    s = conf.state
    s = _run(
        s, "register", "", _actor(Role.COMMANDER, "c", None),
        Params(title="K 线取数", line=Line.D, kind=Kind.R, priority=Priority.P1,
               origin=Origin.COMMANDER, source_ref="用户原话"),
    )
    tid = next(iter(s.tasks))

    # ② PM 分析并定稿（四要素齐）
    s = _run(s, "claim-analyze", tid, pm)
    s = _run(
        s, "define", tid, pm,
        Params(
            whitelist=["data_access/**"],
            frozen=["main.py"],
            acceptance=[],
            deps=[],
        ),
    ) if False else s  # define 需要结构化验收，见下

    from bg_coordinator.models import AcceptanceItem, AcceptanceType

    s = apply(
        s, "define", tid, pm,
        params=Params(
            whitelist=["data_access/**"],
            frozen=["main.py"],
            acceptance=[
                AcceptanceItem(type=AcceptanceType.TEST, cmd="uv run pytest -q", desc="全绿"),
                AcceptanceItem(type=AcceptanceType.NEGATIVE, desc="未注入仍 200 ⇒ 必红"),
                closure_item(),
            ],
        ),
    ).state
    assert s.tasks[tid].status == TaskState.DEFINED
    assert s.tasks[tid].owner == "", "定稿必须释放认领人，否则 TL 取不到活"

    # ③ TL 认领、派活、交付
    s = _run(s, "claim-dev", tid, tl)
    s = _run(s, "start", tid, tl)
    s = _run(
        s, "deliver", tid, tl,
        Params(
            commit="de1f3a2",
            gate_cmd="uv run pytest -q",
            gate_exit=0,
            evidence_path=str(ev),
            changed_files=["data_access/kline/follow.py"],
        ),
    )

    # ④ 形式校验（TL）——**结构上先于实质验收**
    s = _run(s, "verify", tid, tl)
    assert s.tasks[tid].status == TaskState.VERIFIED

    # ⑤ 实质验收（PM）——✅ 只能由这一步产生
    s = _run(s, "accept", tid, pm)
    assert s.tasks[tid].status == TaskState.ACCEPTED
    assert s.tasks[tid].acceptor == "pm-D"

    # 终态之后，除 override 外一切被拒
    r = apply(s, "deliver", tid, tl, params=Params(commit="x", gate_cmd="y", gate_exit=0))
    assert not r.ok and r.rejection is not None


def test_intra_line_technical_path_closes_without_po(tmp_path: Path) -> None:
    """**本线技术问题不过 PO**：TL 登记即定稿 → 直接认领开发 → 交付 → 校验。

    这是 TL 自决那条路的闭环——链上**没有 PO**。
    """
    ev = tmp_path / "g.log"
    ev.write_text("ok", encoding="utf-8")
    tl = _actor(Role.TECH_LEAD, "TL-D")
    pm = _actor(Role.PM, "pm-D")

    from bg_coordinator.models import AcceptanceItem, AcceptanceType

    r = apply(
        budgeted(State()), "raise", "", tl,
        params=Params(
            title="本线内部拆法调整", line=Line.D, scope="intra_line",
            category="technical", origin=None, source_ref="T-D-1 开发中发现",
            priority=Priority.P1, whitelist=["data_access/kline/**"],
            frozen=[],
            # **本线自决要求四要素齐**——自己决定开工，就等于自己把它变成可开发任务
            acceptance=[
                AcceptanceItem(type=AcceptanceType.TEST, cmd="pytest", desc="绿"),
                closure_item(),
            ],
        ),
    )
    assert r.ok, f"本线自决四要素齐却被拒：{r.rejection}"

    tid = r.detail["id"]
    assert r.detail["routed_to"] == "tech-lead"
    s = r.state
    assert s.tasks[tid].status == TaskState.DEFINED

    s = _run(s, "claim-dev", tid, tl)
    s = _run(s, "start", tid, tl)
    s = _run(
        s, "deliver", tid, tl,
        Params(commit="c", gate_cmd="pytest", gate_exit=0,
               evidence_path=str(ev), changed_files=["data_access/kline/x.py"]),
    )
    s = _run(s, "verify", tid, tl)
    s = _run(s, "accept", tid, pm)
    assert s.tasks[tid].status == TaskState.ACCEPTED


def test_design_path_requires_po(tmp_path: Path) -> None:
    """**设计问题必须经 PO**——TL 自决会被拒，这是分权的闭环。"""
    tl = _actor(Role.TECH_LEAD, "TL-D")
    r = apply(
        budgeted(State()), "raise", "", tl,
        params=Params(
            title="改数据面契约", line=Line.D, scope="intra_line",
            category="design", whitelist=["data_access/ports.py"], acceptance=[],
        ),
    )
    assert not r.ok
    assert r.rejection is not None and r.rejection.code.value == "E_CATEGORY_ESCALATE"


def test_ops_and_dba_have_their_own_closed_paths() -> None:
    """OPS 与 DBA 各有自己的闭环——它们不经过 PO/TL 那条主链。"""
    ops = Actor(role=Role.OPS, name="ops", line="OPS")
    s = apply(
        budgeted(State()), "register", "", ops,
        params=Params(title="巡检", line=Line.OPS, kind=Kind.T),
    ).state
    tid = next(iter(s.tasks))
    r = apply(s, "complete", tid, ops, params=Params(reason="已执行留痕"))
    assert r.ok and r.state.tasks[tid].status == TaskState.ACCEPTED

    dba = Actor(role=Role.DBA, name="dba", line="D")
    s2 = apply(
        budgeted(State()), "register", "", dba,
        params=Params(title="迁移评审", line=Line.D, kind=Kind.T),
    ).state
    tid2 = next(iter(s2.tasks))
    r2 = apply(s2, "review", tid2, dba, params=Params(reason="评审通过"))
    assert r2.ok and r2.state.tasks[tid2].status == TaskState.ACCEPTED


def test_invalid_role_is_rejected_at_the_boundary() -> None:
    """角色名不认识 ⇒ **在入口就问**，不静默兜底到更远的地方才爆。"""
    with pytest.raises(ValueError, match="未知角色"):
        Actor.from_str("bogus:x")


def test_short_alias_resolves_to_canonical() -> None:
    """`tl` 可解析，但**只作为输入别名**——它不产生第二个规范名。"""
    from bg_coordinator.models import canonical_role

    assert canonical_role("tl") == "tech-lead"
    assert canonical_role("tech-lead") == "tech-lead"
    assert Actor.from_str("tl:x").role is Role.TECH_LEAD


def test_verb_groups_cover_the_whole_surface() -> None:
    """四组动词合起来 = 体系对外的全部能力面，无遗漏、无重复。"""
    groups = all_verbs()
    seen: list[str] = []
    for verbs in groups.values():
        seen.extend(verbs)
    assert len(seen) == len(set(seen)), "同一个动词出现在两组里"


# ---------------------------------------------------------------------------
# 并行度：**开发用子代理提高并行度**的机械前提
# ---------------------------------------------------------------------------


def _task(tid: str, wl: list[str], **kw):
    from bg_coordinator.models import Priority, Task

    base: dict = {
        "id": tid, "kind": Kind.T, "line": "D", "status": TaskState.DEFINED,
        "whitelist": wl, "acceptance": [], "priority": Priority.P1,
    }
    base.update(kw)
    return Task(**base)  # type: ignore[arg-type]


def test_fanout_groups_disjoint_whitelists_together() -> None:
    """**同批白名单两两不重叠** ⇒ 可同时派给不同子代理。

    这是"提高并行度"的机械前提——不同批的活同时开，必然两个子代理改同一片文件。
    """
    from bg_coordinator.readiness import Quota, plan_fanout

    tasks = {
        "T-D-1": _task("T-D-1", ["a/**"]),
        "T-D-2": _task("T-D-2", ["b/**"]),
        "T-D-3": _task("T-D-3", ["c/**"]),
    }
    # 默认口径是"未批预算派不了活"，所以**显式批预算**（这也是线上的样子）
    plan = plan_fanout(tasks, "D", quota=Quota(enabled=True, per_line={"D": 3}))
    assert plan.total == 3
    assert plan.width == 3, "三条互不重叠的活应当同批可并行"


def test_fanout_separates_conflicting_whitelists() -> None:
    """白名单重叠 ⇒ **必须分到不同批**，否则两个子代理会改同一片文件。"""
    from bg_coordinator.readiness import Quota, plan_fanout

    tasks = {
        "T-D-1": _task("T-D-1", ["shared/**"]),
        "T-D-2": _task("T-D-2", ["shared/**"]),
        "T-D-3": _task("T-D-3", ["other/**"]),
    }
    plan = plan_fanout(tasks, "D", quota=Quota(enabled=True, per_line={"D": 3}))
    assert plan.total == 3
    assert plan.width == 2, "两条共享文件的活不能同批"
    batch_ids = [[t.id for t in b] for b in plan.batches]
    assert len(batch_ids) == 2
    assert not (
        "T-D-1" in batch_ids[0] and "T-D-2" in batch_ids[0]
    ), "共享白名单的两条被排进了同一批"


def test_fanout_respects_per_line_budget() -> None:
    """**宽度受线内预算约束**——不能排出超过整机预算的一批。"""
    from bg_coordinator.readiness import Quota, plan_fanout

    tasks = {f"T-D-{i}": _task(f"T-D-{i}", [f"m{i}/**"]) for i in range(1, 7)}
    plan = plan_fanout(tasks, "D", quota=Quota(enabled=True, per_line={"D": 3}))
    assert plan.width == 3
    assert plan.budget == 3
    assert len(plan.deferred) == 3, "超出预算的应当暂缓，不硬塞"


def test_fanout_budget_accounts_for_in_flight() -> None:
    """预算要扣掉**已在办**的——否则并行度会突破上限。"""
    from bg_coordinator.readiness import Quota, plan_fanout

    tasks = {f"T-D-{i}": _task(f"T-D-{i}", [f"m{i}/**"]) for i in range(1, 7)}
    tasks["T-D-1"].status = TaskState.IN_PROGRESS
    tasks["T-D-2"].status = TaskState.IN_PROGRESS
    plan = plan_fanout(tasks, "D", quota=Quota(enabled=True, per_line={"D": 3}))
    assert plan.budget == 1, "在办 2 个、预算 3 ⇒ 只剩 1 个额度"
    assert plan.width == 1


def test_fanout_without_budget_dispatches_nothing() -> None:
    """**未批预算 ⇒ 派不了活**（默认即闸）。

    这条钉的是口径本身：旧默认是 `enabled=False`（只观测），于是"能开几个"只剩散文——
    实测没批任何预算 `claim-dev` 照样通过。现在没批就是 0，**缺口暴露给批预算的人**。
    """
    from bg_coordinator.readiness import plan_fanout

    tasks = {
        "T-D-1": _task("T-D-1", ["x/**"]),
        "T-D-2": _task("T-D-2", ["x/**"]),
        "T-D-3": _task("T-D-3", ["y/**"]),
        "T-D-4": _task("T-D-4", ["z/**"]),
    }
    plan = plan_fanout(tasks, "D")
    assert plan.total == 0, "未批预算不得排出可派条目"
    assert plan.width == 0


def test_fanout_only_covers_ready_tasks() -> None:
    """未定稿或被占的条目不进并行计划——**能并行不等于可以开工**。"""
    from bg_coordinator.readiness import Quota, plan_fanout

    tasks = {
        "T-D-1": _task("T-D-1", ["a/**"]),
        "T-D-2": _task("T-D-2", ["b/**"], status=TaskState.REGISTERED),
        "T-D-3": _task("T-D-3", ["c/**"], owner="TL-9"),
    }
    plan = plan_fanout(tasks, "D", quota=Quota(enabled=True, per_line={"D": 3}))
    assert [t.id for b in plan.batches for t in b] == ["T-D-1"]


# ---------------------------------------------------------------------------
# 逐线预算：**线是工作面，工作量天然不均**
# ---------------------------------------------------------------------------


def test_unbatched_line_cannot_dispatch() -> None:
    """**未批预算的线派不了活**——它逼出决策，而不是拿默认值糊过去。

    一个统一默认值会把"要不要给这条线加人"这个真问题藏起来：
    重的线被饿着、轻的线被撑着，而没人需要为此做任何决定。
    """
    from bg_coordinator.readiness import Quota, evaluate

    tasks = {"T-D-1": _task("T-D-1", ["a/**"])}
    q = Quota(enabled=True, per_line={"C": 3})  # 只批了 C 线
    r = evaluate(tasks, tasks["T-D-1"], Role.TECH_LEAD, "D", q)  # 问的是 D 线
    assert not r.ready
    assert any("未批预算" in x for x in r.reasons)


def test_each_line_gets_its_own_budget() -> None:
    """**逐线预算**——工作量不同的线，预算不同。"""
    from bg_coordinator.readiness import Quota

    q = Quota(enabled=True, per_line={"C": 5, "D": 2})
    assert q.budget_of("C") == 5
    assert q.budget_of("D") == 2
    assert q.budget_of("E") is None


def test_budget_is_case_insensitive() -> None:
    from bg_coordinator.readiness import Quota

    q = Quota(enabled=True, per_line={"D": 3})
    assert q.budget_of("d") == 3
    assert q.budget_of(Line.D) == 3


def test_quota_disabled_means_no_budget_at_all() -> None:
    """配额未启用 ⇒ 不受协调器约束（不是"预算等于 0"）。"""
    from bg_coordinator.readiness import Quota

    q = Quota(enabled=False, per_line={"D": 2})
    assert q.budget_of("D") is None


def test_total_in_flight_caps_across_lines() -> None:
    """**线间放开 ≠ 整机资源放开**——触库并发、CPU、内存都是共享的。

    总量上限兜的就是这层：即使每条线都还有额度，合计也不能破顶。
    """
    from bg_coordinator.readiness import Quota, evaluate

    tasks = {
        "T-D-1": _task("T-D-1", ["d/**"], status=TaskState.IN_PROGRESS, owner="TL-1"),
        "T-C-1": _task("T-C-1", ["c/**"], status=TaskState.IN_PROGRESS, owner="TL-2"),
        "T-E-1": _task("T-E-1", ["e/**"]),
    }
    tasks["T-E-1"].line = "E"
    q = Quota(enabled=True, per_line={"E": 5}, total_in_flight=2)
    r = evaluate(tasks, tasks["T-E-1"], Role.TECH_LEAD, "E", q)
    assert not r.ready
    assert any("总量上限" in x for x in r.reasons)


def test_set_and_clear_line_budget() -> None:
    """批预算与收回预算——**收回后视为未批**，不是归零。"""
    from bg_coordinator.readiness import Quota

    q = Quota(enabled=True)
    q.set_line("d", 4)
    assert q.budget_of("D") == 4
    q.set_line("D", None)
    assert q.budget_of("D") is None


def test_quota_roundtrips_through_dict() -> None:
    """配额要能从磁盘读回来——否则设了等于没设。"""
    from bg_coordinator.readiness import Quota

    q = Quota(enabled=True, per_line={"C": 5, "D": 2}, total_in_flight=6)
    back = Quota.from_dict(q.to_dict())
    assert back.enabled is True
    assert back.budget_of("C") == 5
    assert back.budget_of("D") == 2
    assert back.total_in_flight == 6


def test_fanout_budget_follows_the_line() -> None:
    """fanout 的宽度跟着**那条线**的预算走。"""
    from bg_coordinator.readiness import Quota, plan_fanout

    tasks = {f"T-D-{i}": _task(f"T-D-{i}", [f"m{i}/**"]) for i in range(1, 7)}
    q = Quota(enabled=True, per_line={"D": 2, "C": 5})
    plan = plan_fanout(tasks, "D", quota=q)
    assert plan.width == 2, "D 线预算 2 ⇒ 宽度 2"
    assert len(plan.deferred) == 4
