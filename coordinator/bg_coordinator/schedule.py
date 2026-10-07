"""**调度器**——从状态直接推出"每个角色下一步该做什么"。

用户口径（2026-10-06）：**调度器取代文档对账**——不再让角色读视图自己对账
（那是把状态读成散文再靠人脑还原），而是由协调器的状态直接给出动作清单。

**边界**：调度器只给**下一步动作**与**放行它的判据**；它不替你执行
（确认闸、验收、合并六道闸仍各自独立），也不越过任何一条已有闸。
视图（`status`/`report`/`trace`）留作**证据与追溯**，不再是"找活"的入口。
"""

from __future__ import annotations

from dataclasses import dataclass

from .engine import State, active_grants
from .models import TERMINAL_STATES, MergeState, Role, Task, TaskState
from .readiness import ready_tasks

_NON_TERMINAL = frozenset(s for s in TaskState if s not in TERMINAL_STATES)


@dataclass
class Action:
    verb: str
    task_id: str
    reason: str  # 为什么轮到它（判据，可追溯）


def _by_priority(tasks: list[Task]) -> list[Task]:
    return sorted(tasks, key=lambda t: (t.priority or "P9", t.id))


def plan_role(state: State, role: Role, line: str, now: float = 0.0) -> list[Action]:
    """某角色在某线的下一步动作清单（**只算，不动**）。"""
    out: list[Action] = []
    tasks = [t for t in state.tasks.values() if str(t.line) == line]
    grants = {ls.task for ls in active_grants(state)}

    if role is Role.PM:
        for t in _by_priority(tasks):
            if t.status == TaskState.REGISTERED and not t.owner:
                if t.origin == "commander" and not t.confirmed_by:
                    continue  # 确认闸：没确认的需求不进任何人的计划
                out.append(Action("claim-analyze", t.id, "已登记未认领"))
            elif t.status == TaskState.ANALYZING and t.owner.startswith("pm"):
                out.append(Action("define", t.id, "分析中（四要素待落）"))
            elif t.status == TaskState.VERIFIED:
                out.append(Action("accept", t.id, "已验证待实质验收"))
    elif role is Role.TECH_LEAD:
        for t in ready_tasks(state.tasks, role, line, state.quota):
            out.append(Action("claim-dev", t.id, "就绪（优先级/依赖/白名单/预算四项已过）"))
        for t in _by_priority(tasks):
            if t.status == TaskState.CLAIMED:
                if t.id not in grants:
                    out.append(Action("dispatch", t.id, "已认领，尚无在手凭证"))
                else:
                    out.append(Action("start", t.id, "凭证在手"))
        running = [m for m in state.merges.values() if m.state == MergeState.MERGING]
        queued = [m for m in state.merges.values() if m.state == MergeState.QUEUED]
        if queued and not running:
            out.append(Action("merge-next", queued[0].merge_id, "合并队列有队首且无在办合并"))
            held = any(
                ls.klass == "test-domain" and ls.state.value == "active"
                for ls in state.leases.values()
            )
            if not held:
                out.append(Action("acquire", "test-domain",
                                  "合并后域级复跑 ⇒ 先领测试档（池上限 quota.slot_max，满则排队）"))
    elif role is Role.PO:
        # PO 的两个真职责：**定优先级**（不定优先级 ⇒ 永远不就绪，谁也接不走）
        # 与**阻塞仲裁**（block/unblock）。这两件不派出去，口子就一直开着。
        for t in _by_priority(tasks):
            if t.status == TaskState.BLOCKED:
                out.append(Action("unblock", t.id, "阻塞中，待仲裁或解除"))
            elif t.status in _NON_TERMINAL and not (t.priority or "").strip():
                out.append(Action("priority", t.id,
                                  "未定优先级——不定就永远不就绪（就绪判据会挡住所有人）"))
    elif role is Role.OPS:
        # **OPS 线自主闭环**：认领与交付都是 ops 自己（`complete` 收口，见状态机）。
        # 2026-10-06 新派单权让 ops 也能把手上工作**并发**派给 dev。
        for t in _by_priority(tasks):
            if t.status == TaskState.REGISTERED and not t.owner:
                out.append(Action("dispatch", t.id,
                                  "自主闭环线：派 dev 并发执行（10-06 新权），或自办"))
    elif role is Role.DBA:
        # **评审即入库**（`review` 收口）：白名单触及迁移目录的已交付条目待评审；
        # 2026-10-06 新派单权同样适用（dba 可派 dev）。
        mig_dir = _migrations_dir()
        for t in _by_priority(tasks):
            if t.status == TaskState.REGISTERED and not t.owner:
                out.append(Action("dispatch", t.id,
                                  "可派 dev 执行（10-06 新权），或自办"))
            elif mig_dir and t.status in (TaskState.DELIVERED, TaskState.VERIFIED) and any(
                w.lstrip("./").startswith(mig_dir) for w in (t.whitelist or [])
            ):
                out.append(Action("review", t.id,
                                  f"白名单触及 {mig_dir}——评审即入库（review）"))
            elif mig_dir and t.status == TaskState.DEFINED and not t.migration_req and any(
                w.lstrip("./").startswith(mig_dir) for w in (t.whitelist or [])
            ):
                out.append(Action("review", t.id,
                                  "已定稿含迁移件但**缺 DBA 迁移要求**（存量）——先补要求再评审"))
    elif role is Role.COMMANDER:
        from .models import LeaseState

        for ls in state.leases.values():
            if ls.state == LeaseState.ACTIVE and now and ls.expired(now):
                out.append(Action("reclaim", ls.lease_id, "凭证已过期，回收销账"))
        for m in state.merges.values():
            if m.state == MergeState.REJECTED:
                out.append(Action("merge-conflict", m.merge_id, "冲突待转 TL 动作项"))
    return out


def _migrations_dir() -> str:
    """绑定 §迁移 的 `dir`（评审信号来自绑定，不写死）。"""
    try:
        from .binding import load as load_binding
        from .testplan import locate_testplan

        path = locate_testplan(None)
        data, _err = load_binding(path) if path else ({}, "")
        mig = (data or {}).get("migrations") or {}
        return str(mig.get("dir") or "").strip("/")
    except Exception:  # noqa: BLE001 —— 取不到 ⇒ 评审信号静默不触发（不造假）
        return ""


def plan(state: State, line: str, now: float = 0.0) -> dict[str, list[Action]]:
    """全角色计划——`coord schedule` 的数据面。"""
    return {
        role.value: plan_role(state, role, line, now)
        for role in (Role.PO, Role.PM, Role.TECH_LEAD, Role.COMMANDER, Role.OPS, Role.DBA)
    }
