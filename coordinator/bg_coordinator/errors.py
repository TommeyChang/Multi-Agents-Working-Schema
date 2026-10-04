"""拒绝码与异常——协调器对外唯一的失败表达。

设计要求（方案 v2 §9）：**通过即静默，失败即带责任人的一条异常**。
因此所有拒绝都必须是**结构化、可枚举**的（拒绝码），不允许抛裸异常或返回自由文本。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Code(StrEnum):
    """拒绝码——与方案 v2 §8 状态机表逐条对应。"""

    # --- 通用 ---
    OK = "OK"
    E_UNKNOWN_VERB = "E_UNKNOWN_VERB"
    E_UNKNOWN_ID = "E_UNKNOWN_ID"
    E_CONFLICT = "E_CONFLICT"  # expect_ver 不符（乐观并发）
    E_NO_REASON = "E_NO_REASON"

    # --- register ---
    E_NO_ID = "E_NO_ID"  # 调用方不该自己发号
    E_NUMBER_GAP = "E_NUMBER_GAP"  # 无账之号：号出去了，账上没有
    E_NUMBER_PENDING = "E_NUMBER_PENDING"  # 待建号无归属
    E_NUMBER_TWICE = "E_NUMBER_TWICE"  # 同号双占：同族同号在账上出现两次
    E_NUMBER_INFLIGHT = "E_NUMBER_INFLIGHT"  # 同族已有在飞的占号：迁移件必须串行落地
    E_UNAUTHORIZED_DISPATCH = "E_UNAUTHORIZED_DISPATCH"  # 子代理授权：无凭证／超上限／越权派单
    E_DOC_UNSYNCED = "E_DOC_UNSYNCED"  # 设计面动了但文档未同批
    E_DUP_ID = "E_DUP_ID"
    E_BAD_LINE = "E_BAD_LINE"
    E_NO_DEP = "E_NO_DEP"
    E_DEP_CYCLE = "E_DEP_CYCLE"

    # --- 角色 / 所有权 ---
    E_NOT_PM = "E_NOT_PM"
    E_NOT_TL = "E_NOT_TL"
    E_NOT_DEV = "E_NOT_DEV"
    E_NOT_OPS = "E_NOT_OPS"
    E_NOT_DBA = "E_NOT_DBA"
    E_NOT_COMMANDER = "E_NOT_COMMANDER"
    E_CROSS_LINE = "E_CROSS_LINE"
    E_OWNED = "E_OWNED"
    E_NOT_OWNER = "E_NOT_OWNER"
    E_FORBIDDEN_WRITE = "E_FORBIDDEN_WRITE"

    # --- define ---
    E_INCOMPLETE = "E_INCOMPLETE"
    E_CATEGORY_ESCALATE = "E_CATEGORY_ESCALATE"  # 该类别不归本角色裁

    # --- claim / 就绪 ---
    E_NOT_READY = "E_NOT_READY"
    E_WL_CONFLICT = "E_WL_CONFLICT"
    E_BUDGET = "E_BUDGET"
    E_NO_PRIORITY = "E_NO_PRIORITY"

    # --- deliver / verify ---
    E_NO_ARTIFACT = "E_NO_ARTIFACT"
    E_NO_EVIDENCE = "E_NO_EVIDENCE"
    E_FORM_INVALID = "E_FORM_INVALID"
    E_GATE_NONZERO = "E_GATE_NONZERO"

    # --- accept / 终态 ---
    E_UNMET = "E_UNMET"
    E_FROZEN = "E_FROZEN"
    E_BAD_STATE = "E_BAD_STATE"
    E_STILL_BLOCKED = "E_STILL_BLOCKED"

    # --- 资源 ---
    E_QUOTA_FULL = "E_QUOTA_FULL"
    E_NOT_HOLDER = "E_NOT_HOLDER"
    E_IN_USE = "E_IN_USE"
    E_MDL = "E_MDL"
    E_STILL_LOCKED = "E_STILL_LOCKED"

    # --- 合并 ---
    E_NOT_DELIVERED = "E_NOT_DELIVERED"
    E_OUT_OF_SCOPE = "E_OUT_OF_SCOPE"
    E_STALE_BASE = "E_STALE_BASE"
    E_CONTENT_CONFLICT = "E_CONTENT_CONFLICT"
    E_INTEGRITY = "E_INTEGRITY"
    E_BUSY = "E_BUSY"


@dataclass(frozen=True)
class Rejection:
    """一次被拒绝的动作——报告里「异常」节的唯一来源。"""

    code: Code
    message: str
    id: str | None = None
    owner: str | None = None  # 责任人（角色或代理 id）
    hint: str = ""

    def __str__(self) -> str:
        parts = [f"[{self.code}]"]
        if self.id:
            parts.append(f"{self.id}:")
        parts.append(self.message)
        if self.hint:
            parts.append(f"（{self.hint}）")
        return " ".join(parts)


class InflightError(Exception):
    """同族已有在飞占号——**串行族的放号闸**。

    做成异常而不是返回码，是因为 `reserve_number` 的既有签名返回 `(号, 记录)`，
    加一个错误返回位会破坏所有调用点；而这条拒绝**必须**在放号那一刻生效
    （晚一步就是两个件同时存在）。
    """

    def __init__(self, message: str, *, family: str = "", number: int = -1) -> None:
        super().__init__(message)
        self.family = family
        self.number = number


class CoordinatorError(Exception):
    """非业务性失败（存储损坏、锁不可得等）——不参与报告异常节。"""

    def __init__(self, rejection: Rejection) -> None:
        super().__init__(str(rejection))
        self.rejection = rejection
