"""核心数据模型——存储格式即数据本身（方案 v2 §4.3）。

**不再有"把结构化字段编码进 markdown 行"这件事**：状态、依赖、白名单、验收
都是类型化字段。markdown 只是单向渲染（见 render.py）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Role(StrEnum):
    COMMANDER = "commander"
    PO = "po"
    PM = "pm"
    TECH_LEAD = "tech-lead"
    DEV = "dev"
    OPS = "ops"
    DBA = "dba"


#: 内置线码——**只是初始集合，不是封闭集合**。
#:
#: 「开发线的数量与划分由具体任务决定」（用户口径），所以线码是**可管理的资源**：
#: 新建线不靠改代码，靠协调器登记。`Line` 保留为内置值，供类型提示与默认值；
#: 真正的合法集合由协调器状态里的 `dev_lines` 决定。
class Line(StrEnum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    E = "E"
    ACL = "ACL"
    OPS = "OPS"


#: **角色名的唯一权威就是 `Role` 枚举值本身**——不另立一张对照表。
#:
#: 「一个概念只用一个名字」：文档、目录、派单记录、Schema 全用同一个名字。
#: 早期版本让枚举用 `tl`、文档用 `tech-lead`，对账立刻暴露对不上号——
#: 所以这里**不给两套名字留位置**，连别名都不设。
#: 解析别名——**只有通行缩写**，不是第二套名字。
#:
#: `tl` 是 tech-lead 的通用缩写，人自然会这么写；允许它**只影响输入解析**。
#: 枚举、Schema、文档一律只有 `tech-lead` 一个规范名——别名不参与任何判定。
ROLE_INPUT_ALIASES: dict[str, str] = {"tl": Role.TECH_LEAD.value}


def canonical_role(raw: str) -> str:
    """规范化角色名。**非法名直接抛错**，不做静默兜底。

    静默返回原样会让"角色没登记"这类错在更远的地方才爆；
    在这里就问，错就落在离原因最近的地方。
    """
    key = raw.strip().lower()
    key = ROLE_INPUT_ALIASES.get(key, key)
    valid = {r.value for r in Role}
    if key not in valid:
        raise ValueError(f"未知角色 {raw!r}；合法角色：{sorted(valid)}")
    return key


#: 需求／问题的来源。**输入源必须可分类**——否则无法回答"这事是谁提的"。
class Origin(StrEnum):
    USER = "user"              # 用户直接指令
    COMMANDER = "commander"    # commander 澄清与调研后转化的形式化需求
    LINE = "line"              # 开发线在开发中发现
    DBA = "dba"                # DBA 反馈
    OPS = "ops"                # OPS 反馈
    PM = "pm"                  # 线 PM 在制定计划时发现


#: **决定类别**——它决定事项归谁裁。
class DecisionCategory(StrEnum):
    #: 技术问题：怎么实现、内部怎么拆、用本线既有契约的哪种形态 ⇒ **TL 可自决**
    TECHNICAL = "technical"
    #: 设计问题：契约／领域模型／口径／标准／阈值 ⇒ **归 PM，经 PO 转化**
    DESIGN = "design"
    #: 跨线问题：影响面越出本线 ⇒ **归 PO 做跨线权衡**
    CROSS_LINE = "cross_line"


#: 类别 → 决策权归属。**这是分权的单一权威**，判定与文档都引它。
CATEGORY_OWNER: dict[str, str] = {
    DecisionCategory.TECHNICAL.value: "tech-lead",
    DecisionCategory.DESIGN.value: "po",
    DecisionCategory.CROSS_LINE.value: "po",
}

#: 内置初始开发线（含 OPS）。新建线经协调器登记后加入。
DEFAULT_DEV_LINES: tuple[str, ...] = ("A", "B", "C", "D", "E", "ACL", "OPS")


class Kind(StrEnum):
    R = "R"  # 需求
    T = "T"  # 任务


class Priority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class TaskState(StrEnum):
    """条目状态机（方案 v2 §8.1）。

    注意 `verified` 是为了让「形式有效是实质验收的前置」**由结构保证**而新增的一态。
    """

    REGISTERED = "registered"
    ANALYZING = "analyzing"
    DEFINED = "defined"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    DELIVERED = "delivered"
    VERIFIED = "verified"
    ACCEPTED = "accepted"
    BLOCKED = "blocked"


#: 终态——除 override 外无出口。
TERMINAL_STATES: frozenset[TaskState] = frozenset({TaskState.ACCEPTED})


class AcceptanceType(StrEnum):
    TEST = "test"
    NEGATIVE = "negative"
    EVIDENCE = "evidence"
    MANUAL = "manual"
    #: **闭环路径**：一条「入口动作 → 系统可观测反应」的端到端断言。
    #: 为什么它是独立类型而不是 `test` 的一种：只交模块单测的条目**看着全绿却没闭环**——
    #: 形状上必须能把"这条路走通了"与"单元都过了"分开，否则判据无从下手。
    CLOSURE = "closure"


@dataclass(frozen=True)
class AcceptanceItem:
    """验收项——**结构化断言**，不是散文。

    这让「回标必须带实证」从一条纪律变成可校验条件（方案 v2 §4.3）。
    """

    type: AcceptanceType
    desc: str = ""
    cmd: str = ""
    expect_exit: int = 0
    path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": str(self.type),
            "desc": self.desc,
            "cmd": self.cmd,
            "expect_exit": self.expect_exit,
            "path": self.path,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> AcceptanceItem:
        return AcceptanceItem(
            type=AcceptanceType(d["type"]),
            desc=d.get("desc", ""),
            cmd=d.get("cmd", ""),
            expect_exit=d.get("expect_exit", 0),
            path=d.get("path", ""),
        )


@dataclass(frozen=True)
class Evidence:
    """交付证据——一次 deliver 产生一份。**不可变。**

    返工（reverify → in_progress → deliver）会产生**新的一轮**证据，
    旧轮次**保留**（它们是溯源的一部分，不是垃圾——方案 v2 §4.9.5）。
    """

    round: int
    commit: str
    gate_cmd: str
    gate_exit: int
    evidence_path: str
    verifier: str = ""
    verifier_exit: int | None = None
    ts: str = ""
    #: **申报的测试级别**（`static`／`affected`／`domain`／`full`）。
    #: 为什么要有它：分级口径（2026-10-05 用户定）必须有处可核——
    #: 交付时报一级、合并前按**同一级**复跑，"范围对不上"才拦得住。
    scope: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Evidence:
        return Evidence(
            round=d["round"],
            commit=d["commit"],
            gate_cmd=d["gate_cmd"],
            gate_exit=d["gate_exit"],
            evidence_path=d["evidence_path"],
            verifier=d.get("verifier", ""),
            verifier_exit=d.get("verifier_exit"),
            ts=d.get("ts", ""),
            scope=d.get("scope", ""),
        )


@dataclass
class Task:
    """一个条目（需求或任务）。R 在 `define` 时升级为 T，**共用同一个 id**。

    共用 id 是有意的：结构上不可能出现「需求翻牌了但任务不见了」（方案 v2 §3.3）。
    """

    id: str
    kind: Kind
    #: 线码——**字符串，不是枚举**。线是可管理的资源，枚举只提供内置值。
    line: str
    status: TaskState = TaskState.REGISTERED
    title: str = ""
    src: str = ""
    #: 来源分类（谁提的）。
    origin: str = ""
    #: 来源引用——这条需求是从哪来的（条目号／问题号／用户原话摘要）。
    source_ref: str = ""
    #: **用户确认留痕**——记录那次确认的会话（`role:name`）与**用户原话**。
    #:
    #: 为什么是两个显式字段，而不是塞进 `source_ref` 的自由文本：
    #: 「commander 发需求必须得到用户显式确认」这条要能被**机械核**（审计报缺口），
    #: 自由文本核不了——那正是"看起来声明了"的缺口形态。
    confirmed_by: str = ""
    user_said: str = ""
    #: 影响面：`intra_line`（本线自决）｜`cross_line`（越出本线，须上 PO）。
    #:
    #: TL 有本线的完全上下文，**本线的事不必经 PO 中转**；
    #: 越线的才需要 PO 做跨线归一与优先级。
    scope: str = ""
    #: **文档同步对**——这批改动包含的文档路径。
    #:
    #: 文档修订**不是必做动作**；但一旦修订，就必须与它所描述的面**同批**。
    #: 这里记的是"这批里有文档"，供审计判断同步是否成对。
    doc_sync: list[str] = field(default_factory=list)
    #: **决定类别**——技术／设计／跨线。它决定归谁裁：
    #: 只有 `technical` 归 TL；`design` 与 `cross_line` 一律上 PO。
    category: str = ""
    #: **条目级约束**——把"本条的硬要求"变成可核的声明（而不是文档里的一句话）。
    #: 例：`zero_migration`（本条判定零迁移；确需迁移须另立 dba 评审＋ops 窗口条目）。
    #: 未知约束**报缺口**（声明了却没人能核 ⇒ 暴露，不许静默算过）。
    constraints: list[str] = field(default_factory=list)
    #: **DBA 迁移要求**（2026-10-06 用户定：动库的开发，DBA 先行）：
    #: 含迁移件的条目定稿时必须挂一份——表结构/索引/锁影响/回填与降级/窗口建议；
    #: 小迁移可以是一句口径，大迁移（大表 DDL、回填）必须完整。
    migration_req: str = ""

    deps: list[str] = field(default_factory=list)
    owner: str = ""
    definer: str = ""
    acceptor: str = ""

    whitelist: list[str] = field(default_factory=list)
    frozen: list[str] = field(default_factory=list)
    acceptance: list[AcceptanceItem] = field(default_factory=list)

    priority: Priority | None = None
    evidence: list[Evidence] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)

    round: int = 0
    ver: int = 1
    #: blocked 时记录进入前的状态，unblock 回退用（方案 v2 §8.1 T10）。
    blocked_from: TaskState | None = None
    block_reason: str = ""

    #: 冻结快照——`freeze` 时把范围与验收固化，此后改动须开新条目（纪律 9／11）。
    frozen_snapshot: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": str(self.kind),
            "line": self.line,
            "status": str(self.status),
            "title": self.title,
            "src": self.src,
            "origin": self.origin,
            "source_ref": self.source_ref,
            "confirmed_by": self.confirmed_by,
            "user_said": self.user_said,
            "scope": self.scope,
            "doc_sync": list(self.doc_sync),
            "constraints": list(self.constraints),
            "migration_req": self.migration_req,
            "category": self.category,
            "deps": list(self.deps),
            "owner": self.owner,
            "definer": self.definer,
            "acceptor": self.acceptor,
            "whitelist": list(self.whitelist),
            "frozen": list(self.frozen),
            "acceptance": [a.to_dict() for a in self.acceptance],
            "priority": str(self.priority) if self.priority else None,
            "evidence": [e.to_dict() for e in self.evidence],
            "changed_files": list(self.changed_files),
            "round": self.round,
            "ver": self.ver,
            "blocked_from": str(self.blocked_from) if self.blocked_from else None,
            "block_reason": self.block_reason,
            "frozen_snapshot": self.frozen_snapshot,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Task:
        bf = d.get("blocked_from")
        pr = d.get("priority")
        return Task(
            id=d["id"],
            kind=Kind(d["kind"]),
            line=str(d["line"]),
            status=TaskState(d["status"]),
            title=d.get("title", ""),
            src=d.get("src", ""),
            origin=d.get("origin", ""),
            source_ref=d.get("source_ref", ""),
            confirmed_by=d.get("confirmed_by", ""),
            user_said=d.get("user_said", ""),
            scope=d.get("scope", ""),
            doc_sync=list(d.get("doc_sync", [])),
            constraints=list(d.get("constraints", [])),
            migration_req=d.get("migration_req", ""),
            category=d.get("category", ""),
            deps=list(d.get("deps", [])),
            owner=d.get("owner", ""),
            definer=d.get("definer", ""),
            acceptor=d.get("acceptor", ""),
            whitelist=list(d.get("whitelist", [])),
            frozen=list(d.get("frozen", [])),
            acceptance=[AcceptanceItem.from_dict(a) for a in d.get("acceptance", [])],
            priority=Priority(pr) if pr else None,
            evidence=[Evidence.from_dict(e) for e in d.get("evidence", [])],
            changed_files=list(d.get("changed_files", [])),
            round=d.get("round", 0),
            ver=d.get("ver", 1),
            blocked_from=TaskState(bf) if bf else None,
            block_reason=d.get("block_reason", ""),
            frozen_snapshot=d.get("frozen_snapshot"),
        )

    @property
    def latest_evidence(self) -> Evidence | None:
        if not self.evidence:
            return None
        return max(self.evidence, key=lambda e: e.round)

    def in_flight(self) -> bool:
        """是否占着线内在办名额（方案 v2 §3.3 的 `line 在办数 < 预算`）。"""
        return self.status in {
            TaskState.CLAIMED,
            TaskState.IN_PROGRESS,
            TaskState.DELIVERED,
            TaskState.VERIFIED,
        }


class LeaseState(StrEnum):
    ACTIVE = "active"
    RECLAIMABLE = "reclaimable"
    RECLAIM_TRACKING = "reclaim_tracking"
    RECLAIMED = "reclaimed"
    RELEASED = "released"


@dataclass
class Lease:
    """资源租约——**语义与任务所有权相反**：可 TTL 自动回收（方案 v2 §5.3）。

    任务所有权必须显式释放（否则活没人干）；租约必须能自动回收（否则进程一崩就永久泄漏）。
    """

    lease_id: str
    klass: str  # bg_db | test_slot | disk | **subagent**（派单授权）
    holder: str
    #: 授权类租约绑定的**条目**——按条目授权：一次派单一条，销账时对得上
    task: str = ""
    db_name: str = ""
    pid: int = 0
    n: int = 1
    bytes_: int = 0
    created_at: float = 0.0
    ttl: float = 0.0
    state: LeaseState = LeaseState.ACTIVE
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "klass": self.klass,
            "holder": self.holder,
            "task": self.task,
            "db_name": self.db_name,
            "pid": self.pid,
            "n": self.n,
            "bytes": self.bytes_,
            "created_at": self.created_at,
            "ttl": self.ttl,
            "state": str(self.state),
            "reason": self.reason,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Lease:
        return Lease(
            lease_id=d["lease_id"],
            klass=d["klass"],
            holder=d.get("holder", ""),
            task=d.get("task", ""),
            db_name=d.get("db_name", ""),
            pid=d.get("pid", 0),
            n=d.get("n", 1),
            bytes_=d.get("bytes", 0),
            created_at=d.get("created_at", 0.0),
            ttl=d.get("ttl", 0.0),
            state=LeaseState(d.get("state", "active")),
            reason=d.get("reason", ""),
        )

    def expired(self, now: float) -> bool:
        return self.ttl > 0 and (now - self.created_at) > self.ttl


@dataclass
class Confirmation:
    """**用户对某一条需求的显式确认**——内容绑定、一次性、会过期。

    为什么要有这条独立记录，而不是"commander 说用户同意了"：

    commander 是**用户接口**，同时又是**需求的形式化者**——没有独立留痕时，
    "用户要的"与"commander 认为用户要的"在状态里长得一模一样，事后分不开。
    这条记录把两者掰开，且每一条都可核：

    - **内容绑定**（`digest` = 线别＋标题的指纹）：确认了 A 就不能拿去发 B；
    - **一次性**（`used_by`）：发号那一刻消费掉，不能反复用；
    - **会过期**（`expires_at`）：上个月的"是"不算今天的数；
    - **必须带用户原话**（`said`）：这是这条记录唯一的证据面。

    **已知残留（与 `Actor` 同级）**：`by`（记录人自报）无鉴权——协调器把它连同原话
    记进事件流以便审计：伪造的话，用户读到"用户原话：……"一眼就知道不是自己说的。
    """

    id: str
    line: str
    title: str
    digest: str
    by: str
    said: str
    ts: str
    expires_at: float
    #: 被哪一条 `register` 消费掉（用完即销；空 = 还在手）
    used_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "line": self.line,
            "title": self.title,
            "digest": self.digest,
            "by": self.by,
            "said": self.said,
            "ts": self.ts,
            "expires_at": self.expires_at,
            "used_by": self.used_by,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Confirmation:
        return Confirmation(
            id=d["id"],
            line=str(d.get("line", "")),
            title=d.get("title", ""),
            digest=d.get("digest", ""),
            by=d.get("by", ""),
            said=d.get("said", ""),
            ts=d.get("ts", ""),
            expires_at=float(d.get("expires_at", 0.0)),
            used_by=d.get("used_by", ""),
        )

    def live(self, now: float) -> bool:
        """在手 = **没被消费** 且 **没过期**。"""
        return not self.used_by and self.expires_at > now


class MergeState(StrEnum):
    QUEUED = "merge_queued"
    MERGING = "merging"
    MERGED = "merged"
    REJECTED = "rejected"  # 冲突即拒，移出队列并生成动作项


@dataclass
class MergeRequest:
    merge_id: str
    task_id: str
    branch: str
    commit: str
    requester: str
    base_commit: str = ""
    state: MergeState = MergeState.QUEUED
    requested_at: float = 0.0
    result_commit: str = ""
    rejection: str = ""
    changed_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "merge_id": self.merge_id,
            "task_id": self.task_id,
            "branch": self.branch,
            "commit": self.commit,
            "requester": self.requester,
            "base_commit": self.base_commit,
            "state": str(self.state),
            "requested_at": self.requested_at,
            "result_commit": self.result_commit,
            "rejection": self.rejection,
            "changed_files": list(self.changed_files),
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> MergeRequest:
        return MergeRequest(
            merge_id=d["merge_id"],
            task_id=d["task_id"],
            branch=d["branch"],
            commit=d["commit"],
            requester=d.get("requester", ""),
            base_commit=d.get("base_commit", ""),
            state=MergeState(d.get("state", "merge_queued")),
            requested_at=d.get("requested_at", 0.0),
            result_commit=d.get("result_commit", ""),
            rejection=d.get("rejection", ""),
            changed_files=list(d.get("changed_files", [])),
        )


@dataclass(frozen=True)
class Event:
    """事件——协调器**唯一的权威落地物**，append-only，永不改写。

    **就地编辑 = 事故**：状态机会被静默污染且不可重建（方案 v2 §3.4）。

    `before`／`after` 是**语义摘要**（给人看、给报告用）；
    `snapshot` 是**完整条目快照**（给重放用）。两者刻意分开：
    摘要稳定可读，快照保证 `state.json` 丢失后能逐字重建。
    """

    seq: int
    ts: str
    verb: str
    id: str
    actor: str
    before: dict[str, Any]
    after: dict[str, Any]
    expect_ver: int | None = None
    request_id: str = ""
    result: str = "ok"
    detail: dict[str, Any] = field(default_factory=dict)
    snapshot: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "verb": self.verb,
            "id": self.id,
            "actor": self.actor,
            "before": self.before,
            "after": self.after,
            "expect_ver": self.expect_ver,
            "request_id": self.request_id,
            "result": self.result,
            "detail": self.detail,
            "snapshot": self.snapshot,
        }


@dataclass
class Actor:
    """动作发起者。

    **已知残留**：`role` 目前是自报的，无鉴权（方案 v2 待决项 8）。
    协调器把它**记入事件流**以便审计，但不做身份校验。
    """

    role: Role
    name: str
    line: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"role": str(self.role), "name": self.name, "line": self.line}

    @staticmethod
    def from_str(s: str) -> Actor:
        """从 `role:name[:line]` 解析。

        角色名先**规范化**：`tl` 与 `tech-lead` 是同一个角色，
        不允许两套写法各自流传（否则 Schema 与文档会对不上号）。
        """
        parts = s.split(":")
        role = Role(canonical_role(parts[0]))
        name = parts[1] if len(parts) > 1 else role.value
        line = parts[2].upper() if len(parts) > 2 else None
        return Actor(role=role, name=name, line=line)
