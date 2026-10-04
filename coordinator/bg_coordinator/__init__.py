"""任务协调器——多角色调度的协议层与存储层。

设计文档见同仓 `DESIGN.md`；协作规则见 `Multi-Agents-Working-Schema/`。

三条核心原则：
1. **协调器只执行动词，不做决策**——判定规则住在配置与校验里，协调器只做校验与串行化；
2. **"现在是什么"归协调器，"曾经发生过什么"归 commit**；
3. **markdown 是单向渲染**，不是双向账本。
"""

from __future__ import annotations

from .engine import Params, Result, State, alloc_number, apply, format_id
from .errors import Code, Rejection
from .models import (
    AcceptanceItem,
    AcceptanceType,
    Actor,
    Event,
    Evidence,
    Kind,
    Lease,
    LeaseState,
    Line,
    MergeRequest,
    MergeState,
    Priority,
    Role,
    Task,
    TaskState,
)
from .readiness import Quota, Readiness, evaluate, ready_tasks
from .schema import ROLES, SCHEMA_VERSION, reconcile, render_schema
from .statemachine import TRANSITIONS, VERBS, check_transition, is_terminal, to_legacy_token
from .storage import Store, StoreError

__version__ = "0.1.0"

__all__ = [
    "AcceptanceItem",
    "AcceptanceType",
    "Actor",
    "Code",
    "Event",
    "Evidence",
    "Kind",
    "Lease",
    "LeaseState",
    "Line",
    "MergeRequest",
    "MergeState",
    "Params",
    "Priority",
    "Quota",
    "Readiness",
    "Rejection",
    "Result",
    "ROLES",
    "Role",
    "SCHEMA_VERSION",
    "State",
    "Store",
    "StoreError",
    "TRANSITIONS",
    "Task",
    "TaskState",
    "VERBS",
    "__version__",
    "alloc_number",
    "apply",
    "check_transition",
    "evaluate",
    "format_id",
    "is_terminal",
    "ready_tasks",
    "reconcile",
    "render_schema",
    "to_legacy_token",
]
