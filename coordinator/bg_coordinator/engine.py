"""协调器核心引擎——**纯函数**：`(state, verb, params) → (state', event)`。

不碰磁盘、不碰 git、不碰数据库。所有副作用（锁、落盘、提交）在 storage.py。
这样状态机与校验规则可以**脱离环境完整测试**——这是本模块唯一的组织原则。
"""

from __future__ import annotations

import copy
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .errors import Code, InflightError, Rejection
from .models import (
    CATEGORY_OWNER,
    DEFAULT_DEV_LINES,
    AcceptanceItem,
    Actor,
    Confirmation,
    DecisionCategory,
    Event,
    Evidence,
    Kind,
    Lease,
    LeaseState,
    Line,
    MergeRequest,
    Origin,
    Priority,
    Role,
    Task,
    TaskState,
)
from .readiness import Quota, evaluate
from .statemachine import check_transition
from .validators import (
    detect_dep_cycle,
    rule_1_unique_id,
    rule_2_ownership,
    rule_3_four_elements,
    rule_4_expect_ver,
    rule_5_dep_ready,
    rule_6_whitelist_exclusive,
    rule_8_evidence_form,
    rule_8b_evidence_readable,
    validate_acceptance,
    whitelist_covers,
)


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Params:
    """动作参数——所有动词共用一个袋子，未用到的字段为 None。

    这样可以避免"每个动词一个签名"带来的 CLI 与引擎不一致。
    """

    title: str = ""
    src: str = ""
    origin: Origin | None = None
    source_ref: str = ""
    scope: str = ""
    category: str = ""
    line: Line | None = None
    kind: Kind | None = None
    deps: list[str] | None = None
    whitelist: list[str] | None = None
    frozen: list[str] | None = None
    acceptance: list[AcceptanceItem] | None = None
    priority: Priority | None = None
    #: 条目级约束（可核的硬要求）；未知约束会被判缺口，不许静默忽略
    constraints: list[str] | None = None

    commit: str = ""
    gate_cmd: str = ""
    gate_exit: int = 0
    evidence_path: str = ""
    verifier: str = ""
    verifier_exit: int | None = None
    changed_files: list[str] | None = None

    target_state: TaskState | None = None
    reason: str = ""
    acceptor: str = ""


@dataclass
class State:
    """协调器的全部活状态——`state.json` 的内容。"""

    seq: int = 0
    tasks: dict[str, Task] = field(default_factory=dict)
    leases: dict[str, Lease] = field(default_factory=dict)
    merges: dict[str, MergeRequest] = field(default_factory=dict)
    quota: Quota = field(default_factory=Quota)
    #: 发号簿：family → 当前最大号。
    number_book: dict[str, int] = field(default_factory=dict)
    #: **分配账**——号是资源，每一笔都要有状态与归属。
    #:
    #: 三种状态：
    #: - `pending`      已占号，**对象待建**（迁移件、后续补充件）；
    #: - `materialized` 对象已存在（条目一登记即落此态）；
    #: - `released`     让号／作废（须给理由）。
    #:
    #: 记录形如 `{family, number, state, id, task, holder, kind, ts, note}`。
    #: **`pending` 不是垃圾**——它是"待建对象"的正常状态；
    #: 但长期 pending 或没有持有人的 pending，就是资源被占着不动。
    allocations: list[dict[str, Any]] = field(default_factory=list)
    #: **开发线集合**——线与线码是**可管理的资源**，不是写死的枚举。
    #:
    #: 「开发线的数量与划分由具体任务决定」（用户口径）：
    #: 新建线经协调器登记即可，不需要改代码。线码启用后不删除——
    #: 历史条目按线码归口，删了会失去归口。
    dev_lines: list[str] = field(default_factory=lambda: list(DEFAULT_DEV_LINES))
    #: 角色声明留痕（`--role` 自报无鉴权的补偿：至少可审计）。
    role_claims: list[dict[str, Any]] = field(default_factory=list)
    #: 缓存锚——本状态对应的**事件日志末尾序号**。
    #: 读缓存时与日志末尾比对；不符即重放。**缓存永远追不上日志时，就说明它不可信。**
    last_seq: int = 0
    #: 资源等待队列——配额满时**排队而不是硬拒**。
    #: 只放**可回收的等待意图**（持有者可死），不放代理身份（子代理生命周期太短，会变僵尸）。
    waiters: list[dict[str, Any]] = field(default_factory=list)
    #: **用户确认**（按需求）：commander 发需求前必须先拿到它。
    #:
    #: 与租约不同，它**进事件面**（`replay` 逐条重建）：它是"用户要什么"的证据，
    #: 不能像运行期状态那样"重建时从旧缓存里捞一把"——那正好会让证据悄悄失真。
    confirmations: dict[str, Confirmation] = field(default_factory=dict)
    #: 确认流水号（`C-<n>`）——号是资源，同一套口径
    confirm_seq: int = 0
    #: **派单累计台账**：派单方 → 累计发出的子代理授权数（含已销账的）。
    #:
    #: 为什么是累计而不是"在办"：用户 2026-10-04 口径是**一个对话累计 ≤10**
    #: （「一个对话开启的子代理总数不得超过 10 个」）——"在办"会随销账归零，
    #: 于是"开一个、销一个、再开"可以无限循环，那不是用户要的闸。
    #: 它进事件面（`replay` 按 `verb=dispatch` 逐条累加），不靠缓存。
    dispatch_tally: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "tasks": {k: v.to_dict() for k, v in self.tasks.items()},
            "leases": {k: v.to_dict() for k, v in self.leases.items()},
            "merges": {k: v.to_dict() for k, v in self.merges.items()},
            "quota": self.quota.to_dict(),
            "number_book": dict(self.number_book),
            "allocations": list(self.allocations),
            "dev_lines": list(self.dev_lines),
            "role_claims": list(self.role_claims),
            "last_seq": self.last_seq,
            "waiters": list(self.waiters),
            "confirmations": {k: v.to_dict() for k, v in self.confirmations.items()},
            "confirm_seq": self.confirm_seq,
            "dispatch_tally": dict(self.dispatch_tally),
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> State:
        return State(
            seq=d.get("seq", 0),
            tasks={k: Task.from_dict(v) for k, v in d.get("tasks", {}).items()},
            leases={k: Lease.from_dict(v) for k, v in d.get("leases", {}).items()},
            merges={k: MergeRequest.from_dict(v) for k, v in d.get("merges", {}).items()},
            quota=Quota.from_dict(d.get("quota", {})),
            number_book=dict(d.get("number_book", {})),
            allocations=list(d.get("allocations", [])),
            dev_lines=list(d.get("dev_lines", DEFAULT_DEV_LINES)),
            role_claims=list(d.get("role_claims", [])),
            last_seq=int(d.get("last_seq", d.get("seq", 0))),
            waiters=list(d.get("waiters", [])),
            confirmations={
                k: Confirmation.from_dict(v) for k, v in d.get("confirmations", {}).items()
            },
            confirm_seq=int(d.get("confirm_seq", 0)),
            dispatch_tally={str(k): int(v) for k, v in d.get("dispatch_tally", {}).items()},
        )


@dataclass
class Result:
    ok: bool
    state: State
    event: Event | None = None
    rejection: Rejection | None = None
    detail: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 发号——取代 registry.md 的"号段空洞／待回填／脱同步"整类问题（方案 v2 §11.2）
# ---------------------------------------------------------------------------

def add_dev_line(state: State, code: str, note: str = "") -> tuple[bool, str]:
    """**登记一条新的开发线**。

    线的数量与划分由任务决定：新线不开在代码里，开在协调器里。
    线码一旦启用不再删除（历史条目按线码分组，删了会失去归口）。
    """
    code = code.strip().upper()
    if not code:
        return False, "线码不能为空"
    if code in state.dev_lines:
        return False, f"线 {code} 已存在"
    state.dev_lines.append(code)
    return True, f"已登记开发线 {code}" + (f"（{note}）" if note else "")


def family_of(kind: Kind, line: str | Line) -> str:
    """编号族 = `<类型>-<线码>`（OPS 线例外，无类型前缀）。

    **线内独立计数**：`T-D-1` 与 `R-D-1` 互不影响，各线之间互不影响。
    线码是字符串——线是可管理资源，枚举只提供内置值。
    """
    code = str(line.value) if isinstance(line, Line) else str(line).upper()
    if code == Line.OPS.value:
        return "OPS"
    return f"{kind.value}-{code}"


def alloc_number(
    state: State,
    family: str,
    seq_alloc: int | None = None,
    *,
    obj_id: str = "",
    kind: str = "",
    ts: str = "",
    holder: str = "",
) -> int:
    """原子发号，**并记入分配账**。

    `seq_alloc` 由存储层提供（同一次锁内的全局单调计数），保证并发下不重号；
    未提供时退化为按簿记自增（单进程测试用）。

    **号是资源**：每次发号都在分配账里留一条记录，指向它的对应物。
    只发号不记对象，号段就会出现无法解释的空洞。
    """
    cur = state.number_book.get(family, 0)
    nxt = seq_alloc if seq_alloc is not None else cur + 1
    state.number_book[family] = max(cur, nxt)
    state.allocations.append(
        {
            "family": family,
            "number": state.number_book[family],
            # 用 `is not None` 而不是真值判断：空字符串是"尚无对象"的占位，
            # 但**登记路径**是先占号、拿到 id 后回填，不能因空串被误判为待建。
            "state": "pending" if obj_id is None else "materialized",
            "id": obj_id,
            "task": "",
            "holder": holder,
            "kind": kind,
            "ts": ts,
            "note": "",
        }
    )
    return state.number_book[family]


def alloc_version(family: str, number: int) -> str:
    """编号的**人类可读形态**（`alembic` 族为四位补零）。"""
    return format_id(family, number)


def find_allocation(state: State, family: str, number: int) -> dict[str, Any] | None:
    for e in state.allocations:
        if e.get("family") == family and int(e.get("number", -1)) == number:
            return e
    return None


def reserve_number(
    state: State,
    family: str,
    holder: str,
    ts: str,
    *,
    task: str = "",
    note: str = "",
) -> tuple[int, dict[str, Any]]:
    """**占号**：号发出去，对象待建。

    这是"待建编号"的落点。占号即推进水位——**别的线看到的是占号之后的号**，
    所以两条线不会各自以为"主干最大号是 0015"而都去选 0016。

    `task` 把号与它的来源条目绑定：**号不是孤立的，它服务于某个条目**。

    **串行族（`SERIAL_FAMILIES`）另有一条**：同族**同时只允许一个在飞占号**。
    理由：迁移件的**号顺序就是链位顺序**——两个件同时占号、各自把父节点接到当时的 head 上，
    合起来必然是两个 head。放号时收口，比合并时返工便宜得多。
    """
    if family in SERIAL_FAMILIES:
        flying = inflight_of(state, family)
        if flying:
            holder_names = "、".join(str(e.get("holder") or "?") for e in flying)
            raise InflightError(
                f"{family} 族已有在飞占号（{holder_names}：{flying[0].get('number')}）——"
                "该族必须串行落物：等它 materialize 或让号后再取",
                family=family,
                number=int(flying[0].get("number", -1)),
            )
    n = alloc_number(state, family, ts=ts, holder=holder, kind="pending")
    entry = state.allocations[-1]
    entry.update({"state": "pending", "task": task, "note": note, "id": ""})
    return n, entry


def materialize_number(
    state: State, family: str, number: int, obj_id: str
) -> tuple[bool, str]:
    """**落物**：占的号终于有了对象。返回 (是否成功, 说明)。"""
    entry = find_allocation(state, family, number)
    if entry is None:
        return False, f"{format_id(family, number)} 不在账上——先占号再落物"
    if entry.get("state") == "materialized":
        return False, f"{format_id(family, number)} 已落物（{entry.get('id')}）"
    if entry.get("state") == "released":
        return False, f"{format_id(family, number)} 已让号，不可再用"
    entry.update({"state": "materialized", "id": obj_id})
    return True, f"{format_id(family, number)} 已落物 → {obj_id}"


def release_number(state: State, family: str, number: int, note: str) -> tuple[bool, str]:
    """**让号**：明确弃用，须给理由——留痕后不再计为空洞。"""
    entry = find_allocation(state, family, number)
    if entry is None:
        return False, f"{format_id(family, number)} 不在账上"
    if not note.strip():
        return False, "让号必须给理由（否则与静默丢弃无异）"
    entry.update({"state": "released", "note": note})
    return True, f"{format_id(family, number)} 已让号：{note}"


#: **必须串行落地的族**：号的顺序 == 链位顺序，否则两个各自合法的件会分叉。
#: 只在这里登记一次——判据在 `reserve_number` 里执行。
SERIAL_FAMILIES: frozenset[str] = frozenset({"alembic"})


def inflight_of(state: State, family: str) -> list[dict[str, Any]]:
    """某族**已占号但还没落物**的记录——`pending` 就是这个状态。"""
    return [
        e
        for e in state.allocations
        if str(e.get("family")) == family and str(e.get("state")) == "pending"
    ]


def number_gaps(state: State) -> list[str]:
    """找出**账上没有记录的号**——号段空洞。

    判据：某族簿记已推进到 N，但 N 以下的号在账上查无记录。
    **已让号（released）与待建（pending）都不算空洞**——它们都有账、都有归属。

    真正的问题是**无账之号**：号发出去了，谁拿的、干什么用、对象在哪，一概不知。
    """
    from collections import defaultdict

    by_family: dict[str, set[int]] = defaultdict(set)
    for entry in state.allocations:
        by_family[str(entry.get("family", ""))].add(int(entry.get("number", -1)))

    gaps: list[str] = []
    for family, high in sorted(state.number_book.items()):
        if high <= 0:
            continue
        accounted = by_family.get(family, set())
        if not accounted:
            gaps.append(f"{format_id(family, 1)}..{format_id(family, high)}（簿记推进过，账上无记录）")
            continue
        missing = [n for n in range(1, high + 1) if n not in accounted]
        if missing:
            shown = "、".join(format_id(family, n) for n in missing[:8])
            more = f" 等 {len(missing)} 个" if len(missing) > 8 else ""
            gaps.append(f"{shown}{more}（无账之号）")
    return gaps


def number_inventory(state: State) -> dict[str, dict[str, int]]:
    """按族统计这笔资源的库存：**已落物 / 待建 / 已让号 / 空洞**。"""
    inv: dict[str, dict[str, int]] = {}
    for family, high in sorted(state.number_book.items()):
        rows = [e for e in state.allocations if e.get("family") == family]
        materialized = sum(1 for e in rows if e.get("state") == "materialized")
        pending = sum(1 for e in rows if e.get("state") == "pending")
        released = sum(1 for e in rows if e.get("state") == "released")
        inv[family] = {
            "high": high,
            "materialized": materialized,
            "pending": pending,
            "released": released,
            "unaccounted": max(high - len(rows), 0),
        }
    return inv


def pending_allocations(state: State) -> list[dict[str, Any]]:
    """待建对象的号——**占着号但物还没落**。

    这是"待建的编号"这一状态的可观测面：谁的、为什么、占了多久。
    """
    return [e for e in state.allocations if e.get("state") == "pending"]


def format_id(family: str, n: int) -> str:
    """编号形态：`T-D-194`、`R-ACL-67`、`OPS-162`、`alembic 0034` 的族前缀规则。"""
    if family == "alembic":
        return f"{n:04d}"
    return f"{family}-{n}"


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def apply(  # noqa: C901 - 动词分派本身就是一个 switch，拆开反而更难核对
    state: State,
    verb: str,
    task_id: str,
    actor: Actor,
    params: Params | None = None,
    expect_ver: int | None = None,
    request_id: str = "",
    now_ts: str | None = None,
) -> Result:
    """施加一个动词。**不修改传入的 state**（内部深拷贝）。"""
    p = params or Params()
    ts = now_ts or now_iso()
    rid = request_id or str(uuid.uuid4())

    new = copy.deepcopy(state)
    # 角色声明留痕（补偿 `--role` 无鉴权，方案 v2 §2.3 残留①）
    new.role_claims.append({"ts": ts, "actor": actor.to_dict(), "verb": verb, "id": task_id})
    new.role_claims = new.role_claims[-500:]

    task = new.tasks.get(task_id)

    # --- 第一层：状态机与角色 ---
    rej = check_transition(verb, task, actor, p.target_state, expect_ver)
    if rej:
        return _reject(new, ts, verb, task_id, actor, expect_ver, rid, rej)

    # --- 第二层：不变量 ---
    if verb == "register":
        return _do_register(new, ts, verb, task_id, actor, p, expect_ver, rid)

    if verb == "raise":
        return _do_raise(new, ts, task_id, actor, p, expect_ver, rid)

    assert task is not None  # check_transition 已保证

    rej = rule_4_expect_ver(task, expect_ver)
    if rej:
        return _reject(new, ts, verb, task_id, actor, expect_ver, rid, rej, task=task)

    if verb == "override":
        return _do_override(new, ts, task, actor, p, expect_ver, rid)

    if verb == "set-priority":
        return _do_set_priority(new, ts, task, actor, p, expect_ver, rid)

    if verb == "block":
        return _do_block(new, ts, task, actor, p, expect_ver, rid)

    if verb == "unblock":
        return _do_unblock(new, ts, task, actor, p, expect_ver, rid)

    # 以下动词共享前置校验
    rej = rule_2_ownership(task, actor, verb)
    if rej:
        return _reject(new, ts, verb, task_id, actor, expect_ver, rid, rej, task=task)

    if verb == "define":
        merged = _merge_params(task, p)
        rej = rule_3_four_elements(merged)
        if rej:
            return _reject(new, ts, verb, task_id, actor, expect_ver, rid, rej, task=task)
        if p.constraints:
            from .validators import unknown_constraints

            bad = unknown_constraints([c for c in p.constraints if c])
            if bad:
                # **声明了却没人能核 ⇒ 当场拒**：留着它就是一句安慰
                return _reject(
                    new, ts, verb, task_id, actor, expect_ver, rid,
                    Rejection(
                        Code.E_INCOMPLETE,
                        "；".join(bad),
                        id=task_id,
                        owner=actor.name,
                        hint="只声明有判据的约束；新约束先在 validators.KNOWN_CONSTRAINTS 登记判据",
                    ),
                    task=task,
                )

    if verb == "claim-dev":
        ready = evaluate(new.tasks, task, Role.TECH_LEAD, task.line, new.quota)
        if not ready.ready:
            assert ready.rejection is not None
            return _reject(new, ts, verb, task_id, actor, expect_ver, rid, ready.rejection, task=task)
        rej = rule_5_dep_ready(new.tasks, task, verb)
        if rej:
            return _reject(new, ts, verb, task_id, actor, expect_ver, rid, rej, task=task)

    if verb == "claim-analyze":
        rej, _ = rule_6_whitelist_exclusive(new.tasks, task)
        if task.whitelist and rej:
            return _reject(new, ts, verb, task_id, actor, expect_ver, rid, rej, task=task)

    if verb == "deliver":
        rej = _check_deliver(task, p)
        if rej:
            return _reject(new, ts, verb, task_id, actor, expect_ver, rid, rej, task=task)

    # **返工必须给原因**——与阻断同理：把东西退回去，理由就是它唯一的信息。
    if verb == "reverify" and not p.reason.strip():
        return _reject(
            new,
            ts,
            verb,
            task_id,
            actor,
            expect_ver,
            rid,
            Rejection(
                Code.E_NO_REASON,
                f"{task.id} 返工必须说明原因",
                id=task.id,
                owner=actor.name,
                hint="写清不合格之处：缺什么、错在哪、按什么判据",
            ),
            task=task,
        )

    if verb in {"verify", "accept"}:
        rej = rule_8_evidence_form(task)
        if rej:
            return _reject(new, ts, verb, task_id, actor, expect_ver, rid, rej, task=task)
        ev = task.latest_evidence
        if ev is not None and verb == "verify":
            rej = rule_8b_evidence_readable(ev.evidence_path)
            if rej:
                return _reject(new, ts, verb, task_id, actor, expect_ver, rid, rej, task=task)

    if verb == "accept":
        rej = validate_acceptance(task)
        if rej:
            return _reject(new, ts, verb, task_id, actor, expect_ver, rid, rej, task=task)

    # --- 第三层：真正落地 ---
    before = _snapshot(task)
    _apply_effects(task, verb, actor, p, ts)
    task.ver += 1
    new.seq += 1

    ev = Event(
        seq=new.seq,
        ts=ts,
        verb=verb,
        id=task_id,
        actor=actor.name,
        before=before,
        after=_snapshot(task),
        expect_ver=expect_ver,
        request_id=rid,
        result="ok",
        detail=_detail_for(task, verb, p),
        snapshot=_full_snapshot(task),
    )
    return Result(ok=True, state=new, event=ev)


# ---------------------------------------------------------------------------
# 动词实现
# ---------------------------------------------------------------------------

def _do_register(
    new: State,
    ts: str,
    verb: str,
    task_id: str,
    actor: Actor,
    p: Params,
    expect_ver: int | None,
    rid: str,
) -> Result:
    rej = rule_1_unique_id(new.tasks, task_id)
    if rej:
        return _reject(new, ts, verb, task_id, actor, expect_ver, rid, rej)

    line = p.line or actor.line
    if line is None:
        return _reject(
            new,
            ts,
            verb,
            task_id,
            actor,
            expect_ver,
            rid,
            Rejection(Code.E_BAD_LINE, "未指定线别", id=task_id, owner=actor.name),
        )
    line_code = str(line.value) if isinstance(line, Line) else str(line).upper()
    if line_code not in new.dev_lines:
        return _reject(
            new,
            ts,
            verb,
            task_id,
            actor,
            expect_ver,
            rid,
            Rejection(
                Code.E_BAD_LINE,
                f"线 {line_code} 未登记（现有：{'、'.join(new.dev_lines)}）",
                id=task_id,
                owner=actor.name,
                hint="新建开发线需先经协调器登记——线的数量与划分由任务决定",
            ),
        )

    deps = list(p.deps or [])
    from .validators import deps_missing

    missing = deps_missing(new.tasks, deps)
    if missing:
        return _reject(
            new,
            ts,
            verb,
            task_id,
            actor,
            expect_ver,
            rid,
            Rejection(Code.E_NO_DEP, f"前置不存在：{missing}", id=task_id, owner=actor.name),
        )

    cycle = detect_dep_cycle(new.tasks, task_id, deps)
    if cycle:
        return _reject(
            new,
            ts,
            verb,
            task_id,
            actor,
            expect_ver,
            rid,
            Rejection(
                Code.E_DEP_CYCLE,
                f"依赖成环：{' → '.join(cycle)}",
                id=task_id,
                owner=actor.name,
            ),
        )

    # **用户显式确认**：commander 发需求必须先拿到用户的确认（R 才判）。
    #
    # 闸放在**发号之前**：号是资源——没经确认的需求不该占号，也不该进流程。
    # 判据是 `models.Confirmation`：内容指纹相符、未消费、未过期；发号即消费。
    # 为什么不能只写文档：commander 既是用户接口、又是需求的形式化者，
    # 没有独立留痕时"用户要的"与"它认为用户要的"在状态里长得一模一样。
    confirmed: Confirmation | None = None
    kind_hint = p.kind or Kind.R
    if kind_hint == Kind.R:
        digest = need_digest(line_code, p.title)
        now = time.time()
        live = live_confirmations(new, digest=digest, now=now)
        by_commander = actor.role == Role.COMMANDER or p.origin == Origin.COMMANDER
        if by_commander and not live:
            same_line = live_confirmations(new, line=line_code, now=now)
            why = (
                f"线 {line_code} 上有 {len(same_line)} 条在手确认，但**内容与本条不符**"
                f"（最近一条：{same_line[0].id}「{same_line[0].title}」）"
                if same_line
                else f"线 {line_code} 上没有任何在手确认"
            )
            return _reject(
                new, ts, verb, task_id, actor, expect_ver, rid,
                Rejection(
                    Code.E_NO_USER_CONFIRM,
                    f"commander 发需求必须得到用户**显式确认**：{why}（本条指纹 {digest}）",
                    id=task_id,
                    owner=actor.name,
                    hint=(
                        "先问用户拿到原话；确认时标题必须与本条**逐字一致**："
                        f'coord confirm --line {line_code} --title "{p.title}" '
                        f'--role {actor.role}:{actor.name} --said "<用户原话>"'
                    ),
                ),
            )
        if live:
            confirmed = live[0]

    # **编号由协调器分配**：调用方只声明线别与类型，不发号。
    # 发号与登记在**同一次持锁事务**内完成 ⇒ 撞号在结构上不可能。
    if not task_id:
        family = family_of(kind_hint, line)
        # 先占号位，拿到 id 后回填对应物——**同一事务内完成，不留悬空号**
        n = alloc_number(new, family, ts=ts, holder=actor.name, kind=str(kind_hint.value))
        task_id = format_id(family, n)
        # 对象在同一事务内产生 ⇒ 直接落物，不留 pending
        entry = new.allocations[-1]
        entry.update({"id": task_id, "state": "materialized", "kind": kind_hint.value})
    kind = p.kind or (Kind.R if task_id.startswith("R-") else Kind.T)
    task = Task(
        id=task_id,
        kind=kind,
        line=line,
        status=TaskState.REGISTERED,
        title=p.title,
        src=p.src,
        origin=str(p.origin.value) if p.origin else "",
        source_ref=p.source_ref,
        deps=deps,
        priority=p.priority,
        ver=1,
    )
    if confirmed is not None:
        # **用完即销**：一条确认只够发一条需求（防"确认一次、发十条"）
        confirmed.used_by = task_id
        task.confirmed_by = confirmed.by
        task.user_said = confirmed.said
    new.tasks[task_id] = task
    new.seq += 1

    detail: dict[str, Any] = {"line": str(line), "kind": str(kind), "allocated": task_id}
    if confirmed is not None:
        # 事件面记下"消费了哪条确认"——`replay` 据此把那一条标成已销
        detail.update(
            {
                "confirmation_used": confirmed.id,
                "confirmed_by": confirmed.by,
                "user_said": confirmed.said,
            }
        )
    ev = Event(
        seq=new.seq,
        ts=ts,
        verb=verb,
        id=task_id,
        actor=actor.name,
        before={},
        after=_snapshot(task),
        expect_ver=expect_ver,
        request_id=rid,
        result="ok",
        detail=detail,
        snapshot=_full_snapshot(task),
    )
    return Result(ok=True, state=new, event=ev, detail={"id": task_id})


def _do_raise(
    new: State,
    ts: str,
    task_id: str,
    actor: Actor,
    p: Params,
    expect_ver: int | None,
    rid: str,
) -> Result:
    """**TL 评估后的登记**：影响面决定去向。

    - `intra_line`：**本线自决**——四要素齐即直达「已定稿」，**不经 PO**；
    - `cross_line`：越出本线 ⇒ 落需求态，等 PO 做跨线归一与优先级。

    为什么本线可以不过 PO：TL 持有本线的完整上下文（代码现状、在办条目、白名单边界），
    PO 中转不会增加信息，只会增加时延。**跨线才需要 PO**，因为那要跨线权衡。
    """
    line = p.line or actor.line
    if line is None:
        return _reject(
            new, ts, "raise", "", actor, expect_ver, rid,
            Rejection(Code.E_BAD_LINE, "未指定线别", owner=actor.name),
        )
    line_code = str(line.value) if isinstance(line, Line) else str(line).upper()
    if line_code not in new.dev_lines:
        return _reject(
            new, ts, "raise", "", actor, expect_ver, rid,
            Rejection(
                Code.E_BAD_LINE,
                f"线 {line_code} 未登记",
                owner=actor.name,
                hint="新建开发线需先经协调器登记",
            ),
        )
    if p.scope == "intra_line" and line_code != (actor.line or ""):
        return _reject(
            new, ts, "raise", "", actor, expect_ver, rid,
            Rejection(
                Code.E_CROSS_LINE,
                f"本线自决只能落自己的线（{actor.line}），不能落 {line_code}",
                owner=actor.name,
                hint="越线的事项按 cross_line 提，交 PO 归一",
            ),
        )
    if actor.line is None:
        return _reject(
            new, ts, "raise", "", actor, expect_ver, rid,
            Rejection(
                Code.E_BAD_LINE,
                "登记影响面必须先声明自己所属的线",
                owner=actor.name,
                hint="角色写法：role:name:LINE",
            ),
        )

    # **类别决定去向**——这是分权的执行点。
    if p.category not in CATEGORY_OWNER:
        return _reject(
            new, ts, "raise", "", actor, expect_ver, rid,
            Rejection(
                Code.E_INCOMPLETE,
                "raise 必须声明决定类别（technical / design / cross_line）",
                owner=actor.name,
                hint="技术问题 TL 可自决；设计问题与跨线问题一律上 PO",
            ),
        )

    from .models import canonical_role as _canon

    owner_of_decision = _canon(CATEGORY_OWNER[p.category])
    intra = owner_of_decision == "tech-lead"

    # **TL 只裁技术问题**：拿设计问题走本线自决 = 越权，直接拒。
    if p.scope == "intra_line" and not intra:
        label = "设计问题" if p.category == DecisionCategory.DESIGN.value else "跨线问题"
        return _reject(
            new, ts, "raise", "", actor, expect_ver, rid,
            Rejection(
                Code.E_CATEGORY_ESCALATE,
                f"{label}不归 TL 裁（类别 {p.category} → 归 {owner_of_decision}）",
                owner=actor.name,
                hint="TL 只裁技术问题；设计走 PM、跨线走 PO，两者均经 PO 转化",
            ),
        )
    if p.scope == "cross_line" and p.category == DecisionCategory.TECHNICAL.value:
        # 线内技术问题却声明跨线：不算错，但按跨线走——影响面优先
        intra = False

    p_scope = "intra_line" if intra else "cross_line"

    family = family_of(Kind.R, line_code)
    n = alloc_number(new, family, ts=ts, holder=actor.name, kind=Kind.R.value)
    new_id = format_id(family, n)
    new.allocations[-1].update({"id": new_id, "state": "materialized", "task": p.source_ref})

    task = Task(
        id=new_id,
        kind=Kind.R,
        line=line_code,
        status=TaskState.REGISTERED,
        title=p.title,
        origin=str(p.origin.value) if p.origin else Origin.LINE.value,
        source_ref=p.source_ref,
        scope=p_scope,
        category=p.category,
        deps=list(p.deps or []),
        priority=p.priority,
        ver=1,
    )

    detail: dict[str, Any] = {
        "id": new_id,
        "scope": p_scope,
        "category": p.category,
        "routed_to": owner_of_decision,
    }
    if intra:
        # 本线自决：四要素齐则直达已定稿——**不经 PO**
        task.whitelist = list(p.whitelist or [])
        task.frozen = list(p.frozen or [])
        task.acceptance = list(p.acceptance or [])
        task.definer = actor.name
        task.kind = Kind.T
        task.status = TaskState.DEFINED
        task.owner = ""
        missing = []
        if not task.whitelist:
            missing.append("白名单")
        if not task.acceptance:
            missing.append("验收标准")
        if missing:
            return _reject(
                new, ts, "raise", "", actor, expect_ver, rid,
                Rejection(
                    Code.E_INCOMPLETE,
                    f"本线自决须同时给出：{'、'.join(missing)}",
                    id=new_id,
                    owner=actor.name,
                    hint="本线自决意味着你直接把它变成可开发的任务——四要素不能缺",
                ),
            )
        detail["defined"] = True

    new.tasks[new_id] = task
    new.seq += 1
    ev = Event(
        seq=new.seq,
        ts=ts,
        verb="raise",
        id=new_id,
        actor=actor.name,
        before={},
        after=_snapshot(task),
        expect_ver=expect_ver,
        request_id=rid,
        result="ok",
        detail=detail,
        snapshot=_full_snapshot(task),
    )
    return Result(ok=True, state=new, event=ev, detail=detail)


def _do_override(
    new: State, ts: str, task: Task, actor: Actor, p: Params, expect_ver: int | None, rid: str
) -> Result:
    if not p.reason.strip():
        return _reject(
            new,
            ts,
            "override",
            task.id,
            actor,
            expect_ver,
            rid,
            Rejection(
                Code.E_NO_REASON,
                "override 必须给 reason",
                id=task.id,
                owner=actor.name,
                hint="强制转换是逃生口，必须留痕可审计",
            ),
            task=task,
        )
    before = _snapshot(task)
    if p.target_state is not None:
        task.status = p.target_state
    if p.whitelist is not None:
        task.whitelist = p.whitelist
    if p.frozen is not None:
        task.frozen = p.frozen
    if p.acceptance is not None:
        task.acceptance = p.acceptance
    if p.priority is not None:
        task.priority = p.priority
    task.block_reason = p.reason
    task.ver += 1
    new.seq += 1
    ev = Event(
        seq=new.seq,
        ts=ts,
        verb="override",
        id=task.id,
        actor=actor.name,
        before=before,
        after=_snapshot(task),
        expect_ver=expect_ver,
        request_id=rid,
        result="ok",
        detail={"reason": p.reason},
        snapshot=_full_snapshot(task),
    )
    return Result(ok=True, state=new, event=ev)


def _do_set_priority(
    new: State, ts: str, task: Task, actor: Actor, p: Params, expect_ver: int | None, rid: str
) -> Result:
    if p.priority is None:
        return _reject(
            new,
            ts,
            "set-priority",
            task.id,
            actor,
            expect_ver,
            rid,
            Rejection(Code.E_INCOMPLETE, "未给优先级", id=task.id, owner=actor.name),
            task=task,
        )
    before = _snapshot(task)
    old = task.priority
    task.priority = p.priority
    task.ver += 1
    new.seq += 1
    ev = Event(
        seq=new.seq,
        ts=ts,
        verb="set-priority",
        id=task.id,
        actor=actor.name,
        before=before,
        after=_snapshot(task),
        expect_ver=expect_ver,
        request_id=rid,
        result="ok",
        detail={"from": str(old) if old else None, "to": str(p.priority), "reason": p.reason},
        snapshot=_full_snapshot(task),
    )
    return Result(ok=True, state=new, event=ev)


def _do_block(
    new: State, ts: str, task: Task, actor: Actor, p: Params, expect_ver: int | None, rid: str
) -> Result:
    """阻断。**必须给原因**——阻断是把一件在办的事停下来，原因就是它的全部信息。

    不许用「（未注明）」兜底：那会让阻断变成一件没有解释的动作，
    事后没人知道该由谁、在什么条件下解阻。
    """
    if not p.reason.strip():
        return _reject(
            new,
            ts,
            "block",
            task.id,
            actor,
            expect_ver,
            rid,
            Rejection(
                Code.E_NO_REASON,
                f"{task.id} 阻断必须说明原因",
                id=task.id,
                owner=actor.name,
                hint="写清阻塞点：等什么、被谁卡住、什么条件下可解",
            ),
            task=task,
        )
    before = _snapshot(task)
    task.blocked_from = task.status
    task.status = TaskState.BLOCKED
    task.block_reason = p.reason.strip()
    task.ver += 1
    new.seq += 1
    ev = Event(
        seq=new.seq,
        ts=ts,
        verb="block",
        id=task.id,
        actor=actor.name,
        before=before,
        after=_snapshot(task),
        expect_ver=expect_ver,
        request_id=rid,
        result="ok",
        detail={"from": str(before["status"]), "reason": task.block_reason},
        snapshot=_full_snapshot(task),
    )
    return Result(ok=True, state=new, event=ev)


def _do_unblock(
    new: State, ts: str, task: Task, actor: Actor, p: Params, expect_ver: int | None, rid: str
) -> Result:
    if task.blocked_from is None:
        return _reject(
            new,
            ts,
            "unblock",
            task.id,
            actor,
            expect_ver,
            rid,
            Rejection(
                Code.E_STILL_BLOCKED,
                "无可回退状态（blocked_from 为空）",
                id=task.id,
                owner=actor.name,
            ),
            task=task,
        )
    before = _snapshot(task)
    # T10：回退到**进入前的状态**——这是状态机推导表里明确的一条。
    task.status = task.blocked_from
    task.blocked_from = None
    task.block_reason = ""
    task.ver += 1
    new.seq += 1
    ev = Event(
        seq=new.seq,
        ts=ts,
        verb="unblock",
        id=task.id,
        actor=actor.name,
        before=before,
        after=_snapshot(task),
        expect_ver=expect_ver,
        request_id=rid,
        result="ok",
        detail={"to": str(task.status)},
        snapshot=_full_snapshot(task),
    )
    return Result(ok=True, state=new, event=ev)


def _merge_params(task: Task, p: Params) -> Task:
    """把 params 合进 task 的**副本**，用于 define 前的四要素校验。"""
    t = copy.deepcopy(task)
    if p.whitelist is not None:
        t.whitelist = p.whitelist
    if p.frozen is not None:
        t.frozen = p.frozen
    if p.acceptance is not None:
        t.acceptance = p.acceptance
    if p.deps is not None:
        t.deps = p.deps
    if p.title:
        t.title = p.title
    return t


def _check_deliver(task: Task, p: Params) -> Rejection | None:
    """交付校验——「回标必须带实证」的可校验条件（方案 v2 §4.3）。"""
    if not p.commit:
        return Rejection(
            Code.E_NO_ARTIFACT,
            f"{task.id} 未给 commit",
            id=task.id,
            owner=task.owner,
        )
    if not p.gate_cmd:
        return Rejection(
            Code.E_NO_EVIDENCE,
            f"{task.id} 未给门禁命令",
            id=task.id,
            owner=task.owner,
            hint="回报须含：命令 ＋ 退出码 ＋ 摘要数字 ＋ 落盘路径",
        )
    if p.gate_exit != 0:
        return Rejection(
            Code.E_GATE_NONZERO,
            f"{task.id} 门禁退出码 {p.gate_exit} ≠ 0",
            id=task.id,
            owner=task.owner,
            hint="禁把红说成既有红；须给 main／HEAD^1 的同口径对照实证",
        )
    if not p.evidence_path:
        return Rejection(
            Code.E_NO_EVIDENCE,
            f"{task.id} 未给原始输出落盘路径",
            id=task.id,
            owner=task.owner,
        )
    # 白名单边界：交付的实际改动必须落在白名单内
    changed = list(p.changed_files or [])
    if changed and task.whitelist:
        ok, out = whitelist_covers(changed, task.whitelist)
        if not ok:
            return Rejection(
                Code.E_OUT_OF_SCOPE,
                f"{task.id} 改动越出白名单：{out}",
                id=task.id,
                owner=task.owner,
                hint="越范围合入须先改条目范围（走新条目）",
            )
    return None


def _apply_effects(task: Task, verb: str, actor: Actor, p: Params, ts: str) -> None:
    """把已通过校验的动作落到 task 上（**只改状态与字段，不再校验**）。"""
    if verb == "claim-analyze":
        task.owner = actor.name
        task.status = TaskState.ANALYZING

    elif verb == "define":
        if p.title:
            task.title = p.title
        if p.whitelist is not None:
            task.whitelist = p.whitelist
        if p.frozen is not None:
            task.frozen = p.frozen
        if p.acceptance is not None:
            task.acceptance = p.acceptance
        if p.constraints is not None:
            task.constraints = [c for c in p.constraints if c]
        if p.deps is not None:
            task.deps = p.deps
        task.definer = actor.name
        task.kind = Kind.T  # R → T，**共用同一个 id**
        task.status = TaskState.DEFINED
        # **交接面**：定稿即释放分析所有权，否则 TL 永远认领不到（owner 占着）。
        task.owner = ""

    elif verb == "freeze":
        task.frozen_snapshot = {
            "whitelist": list(task.whitelist),
            "frozen": list(task.frozen),
            "acceptance": [a.to_dict() for a in task.acceptance],
            "deps": list(task.deps),
            "frozen_at": ts,
        }

    elif verb == "claim-dev":
        task.owner = actor.name
        task.status = TaskState.CLAIMED

    elif verb == "start":
        task.status = TaskState.IN_PROGRESS

    elif verb == "deliver":
        task.round += 1
        task.evidence.append(
            Evidence(
                round=task.round,
                commit=p.commit,
                gate_cmd=p.gate_cmd,
                gate_exit=p.gate_exit,
                evidence_path=p.evidence_path,
                verifier=p.verifier,
                verifier_exit=p.verifier_exit,
                ts=ts,
            )
        )
        task.changed_files = list(p.changed_files or [])
        # 记录这批改动里的文档（口径不强制改文档，但改了就必须成对）
        from .validators import changed_docs

        task.doc_sync = changed_docs(task.changed_files)
        task.status = TaskState.DELIVERED

    elif verb == "verify":
        task.status = TaskState.VERIFIED

    elif verb == "reverify":
        # 返工：round 已由下一轮 deliver 递增；**失败轮次保留**（溯源需要）
        task.status = TaskState.IN_PROGRESS

    elif verb == "accept":
        task.acceptor = actor.name
        task.status = TaskState.ACCEPTED

    elif verb == "complete" or verb == "review":
        task.status = TaskState.ACCEPTED


def _detail_for(task: Task, verb: str, p: Params) -> dict[str, Any]:
    if verb == "deliver":
        return {
            "round": task.round,
            "commit": p.commit,
            "gate_exit": p.gate_exit,
            "evidence_path": p.evidence_path,
        }
    if verb == "reverify":
        return {"round": task.round, "reason": p.reason.strip()}
    if verb in {"complete", "review"}:
        return {"note": p.reason}
    return {}


def _snapshot(task: Task) -> dict[str, Any]:
    """语义摘要——给人看、给报告用（稳定可读）。"""
    return {
        "status": str(task.status),
        "owner": task.owner,
        "ver": task.ver,
        "round": task.round,
        "priority": str(task.priority) if task.priority else None,
    }


def _full_snapshot(task: Task) -> dict[str, Any]:
    """完整快照——给重放用，保证 `state.json` 丢失后能逐字重建。"""
    return task.to_dict()


def _reject(
    new: State,
    ts: str,
    verb: str,
    task_id: str,
    actor: Actor,
    expect_ver: int | None,
    rid: str,
    rej: Rejection,
    task: Task | None = None,
) -> Result:
    """拒绝也要留痕——报告「异常」节的来源（方案 v2 §4.9.2）。"""
    new.seq += 1
    ev = Event(
        seq=new.seq,
        ts=ts,
        verb=verb,
        id=task_id,
        actor=actor.name,
        before=_snapshot(task) if task else {},
        after=_snapshot(task) if task else {},
        expect_ver=expect_ver,
        request_id=rid,
        result=f"rejected:{rej.code}",
        detail={"message": rej.message, "hint": rej.hint, "owner": rej.owner},
    )
    return Result(ok=False, state=new, event=ev, rejection=rej)


# ---------------------------------------------------------------------------
# 资源租约（方案 v2 §5.3）
# ---------------------------------------------------------------------------

def acquire_lease(
    state: State,
    klass: str,
    holder: str,
    db_name: str,
    pid: int,
    ttl: float,
    clock: float,
    n: int = 1,
    bytes_: int = 0,
) -> Result:
    """申请租约。

    三步，**顺序不可换**：

    1. **先即时回收**——把属主已死或过期的租约标为可回收。
       否则"配额满"会成为永久状态，枯竭只是换个地方发生；
    2. **再判配额**：有空位即发放；
    3. **满则排队**——返回**队位**，而不是让申请者盲目重试。

    已在本队列中的申请者**保持原位**，不重复入队。
    """
    new = copy.deepcopy(state)
    ts = now_iso()
    _sweep_reclaimable(new, clock)
    _drop_expired_waiters(new, clock)

    active = [ls for ls in new.leases.values() if ls.state == LeaseState.ACTIVE]
    full = new.quota.enabled and _quota_full(new.quota, klass, active)

    if not full:
        return _grant(new, ts, klass, holder, db_name, pid, ttl, clock, n, bytes_)

    # 满 ⇒ 排队。已在队里则保持原位，只回报当前队位。
    existing = next(
        (w for w in new.waiters if w.get("holder") == holder and w.get("klass") == klass),
        None,
    )
    if existing is None:
        new.waiters.append(
            {
                "klass": klass,
                "holder": holder,
                "db_name": db_name,
                "pid": pid,
                "ttl": ttl,
                "n": n,
                "bytes": bytes_,
                "enqueued_at": clock,
                "position": len(new.waiters) + 1,
                # 等待意图也会过期：申请者死了就没人来取，位置不能永久占着。
                # 这与租约本身要能 TTL 回收是同一个道理。
                "expires_at": clock + max(ttl, 300.0),
            }
        )
        pos = new.waiters[-1]["position"]
    else:
        existing.update({"db_name": db_name, "pid": pid, "ttl": ttl, "n": n, "bytes": bytes_})
        pos = existing["position"]

    new.seq += 1
    ev = Event(
        seq=new.seq,
        ts=ts,
        verb="acquire_queued",
        id=f"{klass}:{holder}",
        actor=holder,
        before={},
        after={"position": pos},
        result=f"rejected:{Code.E_QUOTA_FULL}",
        detail={
            "queue_length": len(new.waiters),
            "active": len(active),
            "quota": _quota_of(new.quota, klass),
        },
    )
    return Result(
        ok=False,
        state=new,
        event=ev,
        rejection=Rejection(
            Code.E_QUOTA_FULL,
            f"{klass} 配额满（{len(active)}/{_quota_of(new.quota, klass)}），已排队第 {pos} 位",
            id=f"{klass}:{holder}",
            owner=holder,
            hint="释放或回收后按序发放；用 leases 查队位，勿盲目重试",
        ),
        detail={"queued": True, "position": pos, "queue_length": len(new.waiters)},
    )


def _grant(
    new: State,
    ts: str,
    klass: str,
    holder: str,
    db_name: str,
    pid: int,
    ttl: float,
    clock: float,
    n: int = 1,
    bytes_: int = 0,
) -> Result:
    """真正发放租约，并把该申请者从队列中移除。"""
    new.waiters = [
        w for w in new.waiters if not (w.get("holder") == holder and w.get("klass") == klass)
    ]
    new.waiters = [dict(w, position=i) for i, w in enumerate(new.waiters, 1)]

    lease_id = f"L-{uuid.uuid4().hex[:12]}"
    new.leases[lease_id] = Lease(
        lease_id=lease_id,
        klass=klass,
        holder=holder,
        db_name=db_name,
        pid=pid,
        n=n,
        bytes_=bytes_,
        created_at=clock,
        ttl=ttl,
        state=LeaseState.ACTIVE,
    )
    new.seq += 1
    ev = Event(
        seq=new.seq,
        ts=ts,
        verb="acquire_slot",
        id=lease_id,
        actor=holder,
        before={},
        after={"state": "active", "db_name": db_name, "pid": pid},
        result="ok",
        detail={"class": klass, "ttl": ttl},
    )
    return Result(ok=True, state=new, event=ev, detail={"lease_id": lease_id, "db_name": db_name})


def _actor_of(holder: str) -> Actor:
    """把 `role:name[:line]` 解析成 Actor——拒绝事件要带责任人。"""
    try:
        return Actor.from_str(holder)
    except Exception:  # noqa: BLE001 - 解析不了就当匿名，拒绝照样留痕
        return Actor(role=Role.COMMANDER, name=holder, line=None)


#: 子代理授权的租约类别——**授权就是一条租约**：有 id、有持有者、有上限、有到期、要销账。
SUBAGENT_KLASS = "subagent"

#: 用户确认的默认有效期（12 小时）。
#:
#: 为什么**必须会过期**：确认是"用户此刻要什么"的证据，不是一张永久通行证——
#: 上个月的"是"拿来发今天的需求，等于没有确认。
CONFIRM_TTL_SECONDS = 12 * 3600.0


def need_digest(line: str, title: str) -> str:
    """需求的内容指纹：**线别 ＋ 标题**（归一化空白后）。

    用它把"确认的那条"与"要发的那条"绑成同一件事——
    确认了 A 却拿去发 B，是这条闸最容易漏的形态。
    """
    norm = " ".join(f"{line}::{title}".split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def live_confirmations(state: State, *, digest: str = "", line: str = "", now: float = 0.0):
    """在手（未消费、未过期）的确认；给了 `digest` 就只留内容相符的那些。"""
    out = []
    for c in state.confirmations.values():
        if not c.live(now):
            continue
        if digest and c.digest != digest:
            continue
        if line and c.line != line:
            continue
        out.append(c)
    return sorted(out, key=lambda c: c.id)


def grant_confirm(
    state: State,
    *,
    line: str,
    title: str,
    by: str,
    said: str,
    clock: float,
    ttl: float = CONFIRM_TTL_SECONDS,
    request_id: str = "",
) -> Result:
    """**记下用户的一次显式确认**（按需求）。

    「commander 发需求必须得到用户显式确认」这句话，如果只写在文档里，
    拦不住任何东西——commander 自己既是用户接口、又是需求的形式化者，
    没有独立留痕时，"用户要的"和"它认为用户要的"在状态里长得一模一样。

    所以这里把它变成一条可核的记录：**内容绑定、一次性、会过期、必须带用户原话**，
    发号那一刻消费掉。见 `models.Confirmation`。
    """
    new = copy.deepcopy(state)
    ts = now_iso()
    rid = request_id or str(uuid.uuid4())
    line_code = str(line).upper().strip()
    title = " ".join((title or "").split())
    said = (said or "").strip()

    if not line_code or not title:
        return _reject(
            new, ts, "confirm", "", _actor_of(by), None, rid,
            Rejection(
                Code.E_INCOMPLETE,
                "confirm 必须给 --line 与 --title（确认绑定的是**那一条**需求）",
                owner=by,
                hint="确认的是内容，不是「我同意了」这句话本身——标题要与 register 逐字一致",
            ),
        )
    if not said:
        return _reject(
            new, ts, "confirm", "", _actor_of(by), None, rid,
            Rejection(
                Code.E_NO_USER_SAID,
                "confirm 必须带 --said（**用户原话**）——它是这条记录唯一的证据面",
                owner=by,
                hint=(
                    '例：coord confirm --line D --title "接入 X" '
                    '--role commander:cmdr --said "把 X 接进来，先做只读"'
                ),
            ),
        )

    digest = need_digest(line_code, title)
    dup = live_confirmations(new, digest=digest, now=clock)
    if dup:
        return _reject(
            new, ts, "confirm", "", _actor_of(by), None, rid,
            Rejection(
                Code.E_CONFIRM_INVALID,
                f"这一条已经有在手确认 {dup[0].id}（未消费、未过期）——不必重复确认",
                owner=by,
                hint=f"直接 register 即可（发号时消费 {dup[0].id}）",
            ),
        )

    new.confirm_seq += 1
    conf = Confirmation(
        id=f"C-{new.confirm_seq}",
        line=line_code,
        title=title,
        digest=digest,
        by=by,
        said=said,
        ts=ts,
        expires_at=clock + ttl,
    )
    new.confirmations[conf.id] = conf
    new.seq += 1
    ev = Event(
        seq=new.seq,
        ts=ts,
        verb="confirm",
        id=conf.id,
        actor=by,
        before={},
        after=conf.to_dict(),
        expect_ver=None,
        request_id=rid,
        result="ok",
        detail={"confirmation": conf.to_dict()},
    )
    return Result(
        ok=True,
        state=new,
        event=ev,
        detail={"id": conf.id, "digest": digest, "title": title, "expires_at": conf.expires_at},
    )



def active_grants(state: State, holder: str = "") -> list[Lease]:
    """在手的子代理授权（可按派单方过滤）——**这就是"开了几个子代理"的可数事实**。"""
    return [
        ls
        for ls in state.leases.values()
        if ls.klass == SUBAGENT_KLASS
        and ls.state == LeaseState.ACTIVE
        and (not holder or ls.holder == holder)
    ]


def grant_dispatch(
    state: State,
    holder: str,
    task_id: str,
    line: str,
    clock: float,
    ttl: float = 3600.0,
) -> Result:
    """**派单授权**（按条目）：给"开一个子代理"发一张凭证。

    为什么它必须是协调器里的记录，而不是文档里的一句话：

    - **可数**：`active_grants(state, holder)` 就是"这个人现在手上几个子代理"，
      不用问任何人、也不靠自报；
    - **有界**：超过 `quota.subagent_max_per_dispatcher` 即拒——
      这条以前只写在 `rules/SUBAGENT.md`（"累计 ≤10"），散文拦不住任何东西；
    - **会过期**：TTL 到了自动回收（复用租约的 `_sweep_reclaimable`），
      避免"授权挂着但人早走了"；
    - **可对账**：在办条目若没有在手授权，审计报 `E_UNAUTHORIZED_DISPATCH`。

    **按条目授权**：一次派单一条（与"一个子代理一条目"同粒度），销账时对得上。
    """
    new = copy.deepcopy(state)
    ts = now_iso()
    _sweep_reclaimable(new, clock)
    _drop_expired_waiters(new, clock)

    if not new.quota.enabled:
        # 观测态：不拦，但也不假装授权成功——凭证照样发，方便先观测后收紧
        pass
    held = active_grants(new, holder)
    tally = new.dispatch_tally.get(holder, 0)
    limit = new.quota.subagent_max_per_dispatcher
    if tally >= limit:
        return _reject(
            new,
            ts,
            "dispatch",
            task_id,
            _actor_of(holder),
            None,
            "",
            Rejection(
                Code.E_UNAUTHORIZED_DISPATCH,
                f"{holder} 的**累计**派单已达上限 {limit}（累计 {tally}，在手 {len(held)}）"
                "——累计口径：销账不重置",
                id=task_id,
                owner=holder,
                hint=(
                    "复用已有子代理，或由派单方自办；确需更多 ⇒ 显式调高 quota 的 "
                    "subagent_max_per_dispatcher（那是一次有人负责的决定）"
                ),
            ),
        )
    if any(ls.task == task_id for ls in active_grants(new)):
        return _reject(
            new,
            ts,
            "dispatch",
            task_id,
            _actor_of(holder),
            None,
            "",
            Rejection(
                Code.E_UNAUTHORIZED_DISPATCH,
                f"条目 {task_id} 已有一张在手授权（按条目授权：一次派单一张）",
                id=task_id,
                owner=holder,
                hint="同一条目复用一个子代理即可——复用不新开（见 SUBAGENT.md §七）",
            ),
        )

    lease_id = f"L-{uuid.uuid4().hex[:12]}"
    # 累计台账：发出去就 +1，**销账不回落**（用户口径：累计 ≤10）
    new.dispatch_tally[holder] = tally + 1
    new.leases[lease_id] = Lease(
        lease_id=lease_id,
        klass=SUBAGENT_KLASS,
        holder=holder,
        task=task_id,
        created_at=clock,
        ttl=ttl,
        state=LeaseState.ACTIVE,
    )
    new.seq += 1
    ev = Event(
        seq=new.seq,
        ts=ts,
        verb="dispatch",
        id=lease_id,
        actor=holder,
        before={},
        after={"state": "active", "task": task_id, "line": line},
        result="ok",
        detail={"lease_id": lease_id, "task": task_id, "ttl": ttl, "line": line},
    )
    return Result(ok=True, state=new, event=ev, detail={"lease_id": lease_id, "task": task_id})


def release_lease(
    state: State, lease_id: str, holder: str, clock: float | None = None
) -> Result:
    new = copy.deepcopy(state)
    ts = now_iso()
    lease = new.leases.get(lease_id)
    if lease is None:
        return _lease_reject(new, ts, holder, Code.E_UNKNOWN_ID, f"租约不存在：{lease_id}")
    if lease.holder != holder:
        return _lease_reject(
            new,
            ts,
            holder,
            Code.E_NOT_HOLDER,
            f"租约 {lease_id} 属 {lease.holder}，非 {holder}",
        )
    lease.state = LeaseState.RELEASED
    # 时钟必须可注入：内部取墙钟会让"注入时钟"的测试与生产行为不一致
    # （实证：测试用注入时钟入队，释放时读到真实墙钟 ⇒ 等待意图被误判过期而丢弃）。
    if clock is None:
        import time as _time

        clock = _time.time()
    advanced = advance_queue(new, clock)
    new.seq += 1
    ev = Event(
        seq=new.seq,
        ts=ts,
        verb="release_slot",
        id=lease_id,
        actor=holder,
        before={"state": "active"},
        after={"state": "released"},
        result="ok",
        detail={"advanced": advanced},
    )
    return Result(ok=True, state=new, event=ev)


def reclaimable_leases(state: State, clock: float, alive: Any = None) -> list[Lease]:
    """列出可回收租约。`alive(pid) -> bool` 默认走 `kill -0`。

    **活性判定复用库名内嵌 pid**——不需要心跳（方案 v2 §5.3）。
    """
    if alive is None:
        alive = pid_alive
    out: list[Lease] = []
    for ls in state.leases.values():
        if ls.state not in {LeaseState.ACTIVE, LeaseState.RECLAIMABLE}:
            continue
        if not alive(ls.pid) or ls.expired(clock):
            out.append(ls)
    return out


def pid_alive(pid: int) -> bool:
    """`kill -0 <pid>` —— 现有回收器最稳的判据，直接沿用。

    **已知残留**：pid 复用会误判存活（方案 v2 §7.3）；
    缓解靠 TTL 兜底，或加 `/proc/<pid>/stat` 的 starttime 比对。
    """
    if pid <= 0:
        return False
    try:
        import os

        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _sweep_reclaimable(state: State, clock: float) -> list[str]:
    """把已死／过期的 active 租约标为 reclaimable。返回本次新标的 id。

    **没有 pid 就不拿 pid 判死**（`pid <= 0` ⇒ 只看 TTL）。
    依据：判据缺失时的失败方向必须一致地偏"少回收、不误杀"——
    这条闸曾经把**刚发的子代理授权**当成死租约扫掉（授权没有 pid，
    而 `pid_alive(0)` 为假），于是"同条目只发一张"在第二次调用时形同虚设。
    """
    marked: list[str] = []
    for ls in state.leases.values():
        if ls.state != LeaseState.ACTIVE:
            continue
        if ls.pid > 0 and not pid_alive(ls.pid):
            ls.state = LeaseState.RECLAIMABLE
            ls.reason = "holder pid 已死"
            marked.append(ls.lease_id)
        elif ls.expired(clock):
            ls.state = LeaseState.RECLAIMABLE
            ls.reason = "TTL 过期"
            marked.append(ls.lease_id)
    return marked


def mark_reclaimed(state: State, lease_id: str, mdl_failed: bool = False) -> Result:
    """**执行层回调**——由 dba 回收器在真正 DROP 之后调用。

    协调器**自己不做 DROP**（方案 v2 §5.4）：它不直接接触真实数据库。
    """
    new = copy.deepcopy(state)
    ts = now_iso()
    lease = new.leases.get(lease_id)
    if lease is None:
        return _lease_reject(new, ts, "dba", Code.E_UNKNOWN_ID, f"租约不存在：{lease_id}")
    if mdl_failed:
        lease.state = LeaseState.RECLAIM_TRACKING
        lease.reason = "MDL 占用——持锁会话须先关"
        code = Code.E_MDL
    else:
        lease.state = LeaseState.RECLAIMED
        lease.reason = "已回收"
        code = Code.OK
    new.seq += 1
    ev = Event(
        seq=new.seq,
        ts=ts,
        verb="reclaim",
        id=lease_id,
        actor="dba",
        before={"state": "reclaimable"},
        after={"state": str(lease.state)},
        result="ok" if code == Code.OK else f"rejected:{code}",
        detail={"reason": lease.reason},
    )
    if code != Code.OK:
        return Result(
            ok=False,
            state=new,
            event=ev,
            rejection=Rejection(code, lease.reason, id=lease_id, owner="dba"),
        )
    return Result(ok=True, state=new, event=ev)


def _quota_of(quota: Quota, klass: str) -> int:
    return quota.bg_db_max if klass == "bg_db" else quota.test_slot


def _quota_full(quota: Quota, klass: str, active: list[Lease]) -> bool:
    return len(active) >= _quota_of(quota, klass)


def _drop_expired_waiters(state: State, clock: float) -> list[str]:
    """丢弃过期的等待意图。返回被丢弃者的描述。

    **等待意图也会过期**：申请者若已死，位置不能永久占着——
    这与租约本身要能 TTL 回收是同一个道理。
    """
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    for w in state.waiters:
        if float(w.get("expires_at", 0)) and clock > float(w["expires_at"]):
            dropped.append(f"{w.get('klass')}:{w.get('holder')}")
            continue
        kept.append(w)
    state.waiters = kept
    for i, w in enumerate(state.waiters, 1):
        w["position"] = i
    return dropped


def advance_queue(state: State, clock: float) -> list[str]:
    """释放/回收之后**按序补位**：有空位就把队首转成租约。

    这是「排队」与「硬拒 + 重试」的分水岭——空位由协调器主动分配，
    不靠申请者反复试探。
    """
    granted: list[str] = []
    _drop_expired_waiters(state, clock)
    while state.waiters:
        active = [ls for ls in state.leases.values() if ls.state == LeaseState.ACTIVE]
        head = state.waiters[0]
        klass = str(head.get("klass", ""))
        if state.quota.enabled and _quota_full(state.quota, klass, active):
            break
        state.waiters.pop(0)
        state.waiters = [dict(w, position=i) for i, w in enumerate(state.waiters, 1)]
        granted.append(klass)
        lease_id = f"L-{uuid.uuid4().hex[:12]}"
        state.leases[lease_id] = Lease(
            lease_id=lease_id,
            klass=klass,
            holder=str(head.get("holder", "")),
            db_name=str(head.get("db_name", "")),
            pid=int(head.get("pid", 0)),
            n=int(head.get("n", 1)),
            bytes_=int(head.get("bytes", 0)),
            created_at=clock,
            ttl=float(head.get("ttl", 0.0)),
            state=LeaseState.ACTIVE,
        )
    return granted


def queue_positions(state: State) -> list[dict[str, Any]]:
    """队列可见面——**不可观测的队列就是一条新的隐性依赖**。"""
    return [
        {
            "position": w.get("position"),
            "class": w.get("klass"),
            "holder": w.get("holder"),
            "db_name": w.get("db_name"),
        }
        for w in state.waiters
    ]


def _lease_reject(new: State, ts: str, actor: str, code: Code, msg: str) -> Result:
    new.seq += 1
    ev = Event(
        seq=new.seq,
        ts=ts,
        verb="lease",
        id="",
        actor=actor,
        before={},
        after={},
        result=f"rejected:{code}",
        detail={"message": msg},
    )
    return Result(ok=False, state=new, event=ev, rejection=Rejection(code, msg, owner=actor))
