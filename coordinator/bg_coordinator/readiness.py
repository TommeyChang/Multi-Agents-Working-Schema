"""就绪判定与额度闸——方案 v2 §3.3。

这是协调器**最大的单点价值**：把公约里最难执行的那条纪律
（「按互不重叠文件白名单扇出」）从散文变成**可判定函数**。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import Code, Rejection
from .models import Line, Role, Task, TaskState
from .validators import (
    deps_all_terminal,
    deps_missing,
    rule_7_mode,
    whitelist_overlap,
)


@dataclass
class Quota:
    """额度策略——**数值须实测标定**（先观测后启用）。

    **预算按线给，不给统一默认值。** 理由：线是**工作面**，工作量天然不均——
    一个统一数要么饿着重的线、要么撑着轻的线。所以：

    - `per_line`：**逐线预算**（线码 → 同时在办上限）。**没配就是没批**，
      不拿一个全局默认值糊过去——那会把"要不要给这条线加人"这个真问题藏起来；
    - `total_in_flight`：**总量上限**（可选）。线间放开不等于整机资源放开——
      触库并发、CPU、内存都是共享的，这层闸兜的是它们；

    `enabled=False` 时只观测不放行。
    """

    enabled: bool = False
    #: 逐线预算。**缺省即未批**——不是"用默认值"。
    per_line: dict[str, int] = field(default_factory=dict)
    #: 全部线加起来的在办上限；None 表示不设总量闸。
    total_in_flight: int | None = None
    bg_db_max: int = 20
    bg_db_disk_mb: int = 4096
    test_slot: int = 4

    def budget_of(self, line: str) -> int | None:
        """某线的在办上限。**None = 未给这条线批预算。**"""
        if not self.enabled:
            return None
        code = str(getattr(line, "value", line)).upper()
        return self.per_line.get(code)

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "per_line": dict(self.per_line),
            "total_in_flight": self.total_in_flight,
            "bg_db_max": self.bg_db_max,
            "bg_db_disk_mb": self.bg_db_disk_mb,
            "test_slot": self.test_slot,
        }

    @staticmethod
    def from_dict(d: dict[str, object]) -> Quota:
        raw = d.get("per_line") or {}
        per_line = {str(k).upper(): int(v) for k, v in dict(raw).items()}  # type: ignore[arg-type]
        total = d.get("total_in_flight")
        return Quota(
            enabled=bool(d.get("enabled", False)),
            per_line=per_line,
            total_in_flight=int(total) if total is not None else None,  # type: ignore[arg-type]
            bg_db_max=int(d.get("bg_db_max", 20)),  # type: ignore[arg-type]
            bg_db_disk_mb=int(d.get("bg_db_disk_mb", 4096)),  # type: ignore[arg-type]
            test_slot=int(d.get("test_slot", 4)),  # type: ignore[arg-type]
        )

    def set_line(self, line: str, budget: int | None) -> None:
        """给某线批预算；`None` 收回归档（＝未批）。"""
        code = line.strip().upper()
        if budget is None:
            self.per_line.pop(code, None)
        else:
            self.per_line[code] = budget


@dataclass
class Readiness:
    ready: bool
    reasons: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    rejection: Rejection | None = None


def total_in_flight(tasks: dict[str, Task]) -> int:
    """全部线的在办总数——总量闸的输入。"""
    return sum(1 for t in tasks.values() if t.in_flight())


def line_in_flight(tasks: dict[str, Task], line: Line) -> int:
    return sum(1 for t in tasks.values() if t.line == line and t.in_flight())


def evaluate(
    tasks: dict[str, Task],
    task: Task,
    role: Role,
    line: Line,
    quota: Quota | None = None,
) -> Readiness:
    """`ready_for(role, line)` 的实现。

    ```
    ready_for(role, line) =
          status == defined
        ∧ deps 全部终态
        ∧ (role == dev → owner 为空 ∧ whitelist ∩ 在办 whitelist == ∅)
        ∧ (role == pm  → owner 为空 ∧ 未被认领分析)
        ∧ line 在办数 < 线内预算
        ∧ priority 已定
    ```
    """
    q = quota or Quota()
    reasons: list[str] = []

    if task.status != TaskState.DEFINED:
        reasons.append(f"状态为 {task.status}，须为 defined")

    missing = deps_missing(tasks, task.deps)
    if missing:
        reasons.append(f"前置不存在：{missing}")
    pending = deps_all_terminal(tasks, task.deps)
    if pending:
        reasons.append(f"前置未终态：{pending}")

    if role == Role.TECH_LEAD:
        if task.owner:
            reasons.append(f"已被 {task.owner} 认领")
        conflicts: list[str] = []
        for other in tasks.values():
            if other.id == task.id or not other.in_flight():
                continue
            if whitelist_overlap(task.whitelist, other.whitelist):
                conflicts.append(other.id)
        if conflicts:
            reasons.append(f"白名单与在办条目冲突：{conflicts}")
        mode_rej = rule_7_mode(tasks, task)
        if mode_rej:
            reasons.append(mode_rej.message)
        if q.enabled:
            budget = q.budget_of(line)
            used = line_in_flight(tasks, line)
            if budget is None:
                reasons.append(f"{line} 线未批预算——先给它定一个在办上限")
            elif used >= budget:
                reasons.append(f"{line} 线在办 {used} ≥ 预算 {budget}")
            if q.total_in_flight is not None and total_in_flight(tasks) >= q.total_in_flight:
                reasons.append(
                    f"全局在办 {total_in_flight(tasks)} ≥ 总量上限 {q.total_in_flight}"
                )

    if role == Role.PM and task.owner and task.owner != "":
        reasons.append(f"分析已被 {task.owner} 认领")

    if not reasons:
        return Readiness(ready=True)

    return Readiness(
        ready=False,
        reasons=reasons,
        rejection=Rejection(
            Code.E_NOT_READY,
            f"{task.id} 未就绪：" + "；".join(reasons),
            id=task.id,
            owner=task.owner,
        ),
    )


@dataclass
class Fanout:
    """**并行派单建议**——把可认领清单分成"可同时开工"的批次。

    两条判据，缺一不可：

    ① **同批次内两两白名单不重叠**——否则两个子代理会改同一片文件；
    ② **宽度不超过线内预算**（配额启用时）——否则并行度突破了整机预算。

    只看 ① 会让"能并行"变成"全一起上"；只看 ② 会让冲突的活同时开工。
    这是"开发用子代理提高并行度"的机械前提。
    """

    line: str
    batches: list[list[Task]]
    budget: int | None = None
    deferred: list[Task] = field(default_factory=list)

    @property
    def width(self) -> int:
        """最宽的一批能同时开几个子代理。"""
        return max((len(b) for b in self.batches), default=0)

    @property
    def total(self) -> int:
        return sum(len(b) for b in self.batches)


def plan_fanout(
    tasks: dict[str, Task], line: Line, role: Role = Role.TECH_LEAD, quota: Quota | None = None
) -> Fanout:
    """把可认领条目排成**互不重叠的批次**。

    贪心即可：按优先级依次放入**第一个不冲突**的批次。
    同批 = 可同时派子代理；不同批 = 后一批必须等前一批腾出文件。
    """
    from .validators import whitelist_overlap

    q = quota or Quota()
    ready = ready_tasks(tasks, role, line, q)

    # 预算按"在办 ＋ 本批已排"算——否则会排出超过预算的一批
    in_flight = sum(
        1 for t in tasks.values() if str(t.line) == str(line) and t.in_flight()
    )
    budget = None
    if q.enabled:
        allowed = q.budget_of(line)
        budget = None if allowed is None else max(allowed - in_flight, 0)

    batches: list[list[Task]] = []
    deferred: list[Task] = []
    for t in ready:
        if budget is not None and len(batches) == 1 and len(batches[0]) >= budget:
            # 本批已排满预算 ⇒ 其余推迟到下一批，不硬塞
            deferred.append(t)
            continue
        placed = False
        for batch in batches:
            if budget is not None and len(batch) >= budget:
                continue
            if not any(whitelist_overlap(t.whitelist, other.whitelist) for other in batch):
                batch.append(t)
                placed = True
                break
        if not placed:
            batches.append([t])
    return Fanout(line=str(line), batches=batches, budget=budget, deferred=deferred)


def ready_tasks(
    tasks: dict[str, Task], role: Role, line: Line, quota: Quota | None = None
) -> list[Task]:
    """列出某线某角色**当前可认领**的条目——TL 监视器的扫面依据（方案 v2 §7.1）。"""
    out: list[Task] = []
    for t in tasks.values():
        if str(t.line) != str(line):
            continue
        if t.status != TaskState.DEFINED:
            continue
        if evaluate(tasks, t, role, line, quota).ready:
            out.append(t)
    return sorted(out, key=lambda t: (t.priority or "P9", t.id))
