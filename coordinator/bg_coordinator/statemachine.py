"""状态机——**纯函数**，不碰磁盘、不碰 git（方案 v2 §8 推导表的直译）。

设计约束：
- 同一 `(状态, 事件)` **必须确定性**（v1 曾在 `define` 上犯过"同事件两出口"的错）；
- `accept` **只可能来自 `verified`** ⇒ 「形式有效是实质验收的前置」由结构保证，不靠纪律；
- 终态无出口（除 `override`）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import Code, Rejection
from .models import Actor, Kind, Priority, Role, Task, TaskState

#: 动词全集。
VERBS: tuple[str, ...] = (
    "register",
    "raise",
    "claim-analyze",
    "define",
    "freeze",
    "claim-dev",
    "start",
    "deliver",
    "verify",
    "reverify",
    "accept",
    "block",
    "unblock",
    "complete",
    "review",
    "override",
    "set-priority",
)


@dataclass(frozen=True)
class Transition:
    """一条合法转换。

    `sources` 为空 ⇒ 仅用于 `register`（从无到有）。
    """

    verb: str
    sources: frozenset[TaskState]
    target: TaskState | None  # None ⇒ 目标由运行时决定（block/unblock/override）
    roles: frozenset[Role]
    same_line: bool = False
    note: str = ""


_ANY = frozenset(TaskState)
_NON_TERMINAL = frozenset(
    s
    for s in TaskState
    if s not in {TaskState.ACCEPTED}
)

TRANSITIONS: dict[str, Transition] = {
    "register": Transition("register", frozenset(), TaskState.REGISTERED, frozenset(Role)),
    "raise": Transition("raise", frozenset(), TaskState.REGISTERED, frozenset(Role)),
    "claim-analyze": Transition(
        "claim-analyze",
        frozenset({TaskState.REGISTERED, TaskState.BLOCKED}),
        TaskState.ANALYZING,
        frozenset({Role.PM}),
        same_line=True,
    ),
    "define": Transition(
        "define",
        frozenset({TaskState.ANALYZING}),
        TaskState.DEFINED,
        frozenset({Role.PM}),
        same_line=True,
        note="R→T；四要素齐（缺项即拒，状态不变）",
    ),
    "freeze": Transition(
        "freeze",
        frozenset({TaskState.DEFINED}),
        TaskState.DEFINED,
        frozenset({Role.PM, Role.TECH_LEAD, Role.COMMANDER}),
        same_line=True,
    ),
    "claim-dev": Transition(
        "claim-dev",
        frozenset({TaskState.DEFINED}),
        TaskState.CLAIMED,
        frozenset({Role.TECH_LEAD}),
        same_line=True,
    ),
    "start": Transition(
        "start",
        frozenset({TaskState.CLAIMED}),
        TaskState.IN_PROGRESS,
        frozenset({Role.TECH_LEAD}),
        same_line=True,
    ),
    "deliver": Transition(
        "deliver",
        frozenset({TaskState.IN_PROGRESS}),
        TaskState.DELIVERED,
        frozenset({Role.TECH_LEAD}),
        same_line=True,
    ),
    "verify": Transition(
        "verify",
        frozenset({TaskState.DELIVERED}),
        TaskState.VERIFIED,
        frozenset({Role.TECH_LEAD, Role.COMMANDER}),
        same_line=True,
    ),
    "reverify": Transition(
        "reverify",
        frozenset({TaskState.DELIVERED, TaskState.VERIFIED}),
        TaskState.IN_PROGRESS,
        frozenset({Role.TECH_LEAD}),
        same_line=True,
        note="返工：round +1，失败轮次保留",
    ),
    "accept": Transition(
        "accept",
        frozenset({TaskState.VERIFIED}),  # ← 只能来自 verified
        TaskState.ACCEPTED,
        frozenset({Role.PM}),
        same_line=True,
    ),
    "block": Transition(
        "block",
        _NON_TERMINAL - {TaskState.BLOCKED},
        TaskState.BLOCKED,
        frozenset({Role.TECH_LEAD, Role.PM, Role.PO, Role.COMMANDER}),
    ),
    "unblock": Transition(
        "unblock",
        frozenset({TaskState.BLOCKED}),
        None,
        frozenset({Role.TECH_LEAD, Role.PM, Role.PO, Role.COMMANDER}),
    ),
    "complete": Transition(
        "complete",
        _NON_TERMINAL,
        TaskState.ACCEPTED,
        frozenset({Role.OPS}),
        same_line=True,
        note="OPS 线自主闭环",
    ),
    "review": Transition(
        "review",
        _NON_TERMINAL,
        TaskState.ACCEPTED,
        frozenset({Role.DBA}),
        same_line=True,
        note="评审即入库",
    ),
    "override": Transition(
        "override",
        _ANY,
        None,
        frozenset({Role.COMMANDER}),
        note="唯一强制转换；reason 必填、留痕",
    ),
    "set-priority": Transition(
        "set-priority",
        _NON_TERMINAL,
        None,
        frozenset({Role.PO, Role.COMMANDER}),
    ),
}


def check_transition(
    verb: str,
    task: Task | None,
    actor: Actor,
    target: TaskState | None = None,
    expect_ver: int | None = None,
) -> Rejection | None:
    """第一层：**乐观并发 → 角色 → 状态**的合法性。

    返回 `None` ⇒ 通过；返回 `Rejection` ⇒ 拒绝。
    **不做**业务校验（白名单、证据、依赖）——那些归 validators.py。

    **顺序有意为之**：
    1. **乐观并发最先**——客户端拿到陈旧快照时，最该告诉他的就是"你看到的是旧的"，
       而不是把他引向"状态不允许"这条**看起来像设计问题、实则是读取过时**的岔路；
    2. **角色先于状态**——否则「无权角色对终态条目动手」会报 `E_BAD_STATE`／`E_FROZEN`
       而非 `E_FORBIDDEN_WRITE`，掩盖真正的越权问题。
    """
    tr = TRANSITIONS.get(verb)
    if tr is None:
        return Rejection(Code.E_UNKNOWN_VERB, f"未知动词 {verb!r}")

    if actor.role not in tr.roles:
        return Rejection(
            Code.E_FORBIDDEN_WRITE,
            f"角色 {actor.role} 不得执行 {verb}",
            id=task.id if task else None,
            owner=actor.name,
            hint=f"允许角色：{sorted(r.value for r in tr.roles)}",
        )

    if verb in {"register", "raise"}:
        return None

    if task is None:
        return Rejection(Code.E_UNKNOWN_ID, "条目不存在")

    if expect_ver is not None and task.ver != expect_ver:
        from .errors import Rejection as _R

        return _R(
            Code.E_CONFLICT,
            f"{task.id} 已被他人改动（期望 ver={expect_ver}，实际 ver={task.ver}）",
            id=task.id,
            owner=task.owner,
            hint="重新读取当前快照后再试",
        )

    if task.status not in tr.sources:
        if task.status in {TaskState.ACCEPTED} and verb != "override":
            return Rejection(
                Code.E_FROZEN,
                f"{task.id} 已终态（{task.status}），不可再变更",
                id=task.id,
                owner=task.owner,
                hint="交付即冻结，新缺口请开新条目",
            )
        return Rejection(
            Code.E_BAD_STATE,
            f"{task.id} 当前状态 {task.status} 不允许 {verb}",
            id=task.id,
            owner=task.owner,
            hint=f"该动词允许来源状态：{sorted(s.value for s in tr.sources)}",
        )

    if tr.same_line and actor.line is not None and str(actor.line) != str(task.line):
        return Rejection(
            Code.E_CROSS_LINE,
            f"{actor.name}（{actor.line} 线）不得处置 {task.line} 线的 {task.id}",
            id=task.id,
            owner=actor.name,
            hint="跨线须移交目标线 inbox，不直接动手",
        )
    return None


def next_state(verb: str, task: Task | None, target: TaskState | None = None) -> TaskState | None:
    """计算目标状态（纯函数）。

    `block`/`unblock`/`override`/`set-priority` 的目标由运行时决定：
    - `block`   → BLOCKED，且调用方须先写 `blocked_from`
    - `unblock` → 回退到 `task.blocked_from`
    - `override`/`set-priority` → 显式传入 `target`
    """
    if verb == "unblock":
        return task.blocked_from if task else None
    if verb in {"override", "set-priority"}:
        return target
    tr = TRANSITIONS[verb]
    return tr.target


def is_terminal(state: TaskState) -> bool:
    return state == TaskState.ACCEPTED


def map_kind_after_define(task: Task) -> Kind:
    """`define` 使 R 升级为 T——**共用同一个 id**（方案 v2 §3.3）。"""
    return Kind.T


# ---------------------------------------------------------------------------
# 旧 token 映射（兼容视图 A 用；方案 v2 §4.4 的过渡期损失）
# ---------------------------------------------------------------------------

TOKEN_MAP: dict[TaskState, str] = {
    TaskState.REGISTERED: "[未处理]",
    TaskState.ANALYZING: "[处理中]",
    TaskState.DEFINED: "[处理中]",
    TaskState.CLAIMED: "[处理中]",
    TaskState.IN_PROGRESS: "[处理中]",
    TaskState.DELIVERED: "[处理中]",
    TaskState.VERIFIED: "[处理中]",
    TaskState.ACCEPTED: "✅",
    TaskState.BLOCKED: "⏸",
}
"""新状态 → 旧 token。

**过渡期接受信息损失**：`已定稿`／`已交付`／`已验证` 都落在 `[处理中]`。
判据是「协调器在办／待处置」：`已定稿` 正等 TL 认领，属待处置，故归 `[处理中]` 而非 `[未处理]`
——这与现状一致，不是回归（方案 v2 §4.4）。
"""


def to_legacy_token(state: TaskState) -> str:
    return TOKEN_MAP[state]


def parse_priority(raw: str | None) -> Priority | None:
    return Priority(raw) if raw else None


def parse_line(raw: str) -> str:
    """线码一律规范化为大写字符串——线是可管理资源，不是封闭枚举。"""
    return raw.strip().upper()
