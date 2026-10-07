"""MAWS — Multi-Agents Working Schema。

**本文件是这套协作体系的机器可读定义。** 散在 `Multi-Agents-Working-Schema/*.md` 里的规则是给人读的；
这里是给程序读的，因此**可以对账**：Schema 说有什么，实现就必须有什么。

对账不是学术——它治的是一类具体的事故：**文档写了角色/动词，代码却没实现**
（或反过来：代码里有条路，文档里没人知道）。这类漂移在纯文档体系里无法发现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import Code
from .models import CATEGORY_OWNER, DEFAULT_DEV_LINES, Role, canonical_role
from .statemachine import TRANSITIONS

SCHEMA_VERSION = 1

#: 规则文档（相对体系根）。Schema 里声明的职责与它们**必须对得上**。
RULE_DOCS: tuple[str, ...] = (
    "AGENT.md",
    "rules/COORDINATION.md",
    "rules/WORKSPACE.md",
    "rules/SUBAGENT.md",
    "rules/RISKS.md",
    # 查阅面分册：动作面主文件里留锚指向它们（拆出是为了控固定上下文规模）
    "rules/COORDINATION-details.md",
    "rules/WORKSPACE-details.md",
)

#: 角色文档**可解析头部**：`> maws: role=<key> form=<form> reports-to=<who> dispatch=<a,b>`
#:
#: 存在理由：只查"文件在不在"不够——文档写"归 PO"，Schema 写"归用户"，
#: 这种不一致人工审不出来。有了可解析头部，**逐字段比对**就成了机械判定。
DOC_HEADER_PREFIX = "> maws:"


@dataclass(frozen=True)
class DocHeader:
    role: str
    form: str
    reports_to: str
    dispatch: tuple[str, ...]


def parse_doc_header(text: str) -> DocHeader | None:
    """从角色文档里取头部。**缺失或不成形都返回 None**——由调用方报为漂移。"""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(DOC_HEADER_PREFIX):
            continue
        fields: dict[str, str] = {}
        for token in line[len(DOC_HEADER_PREFIX) :].split():
            if "=" not in token:
                continue
            k, _, v = token.partition("=")
            fields[k.strip()] = v.strip()
        if not {"role", "form", "reports-to"} <= set(fields):
            return None
        # `-` 是"无派单权"的占位——必须解析回空元组，否则每份无派单权的文档都被报漂移
        raw_dispatch = fields.get("dispatch", "-").strip()
        dispatch = () if raw_dispatch in {"", "-"} else tuple(
            x for x in raw_dispatch.split(",") if x
        )
        return DocHeader(
            role=fields["role"],
            form=fields["form"],
            reports_to=fields["reports-to"],
            dispatch=dispatch,
        )
    return None


def render_doc_header(spec: RoleSpec) -> str:
    """按 Schema 生成头部行——**文档的头由 Schema 派生，不手写**。"""
    dispatch = ",".join(spec.dispatch) if spec.dispatch else "-"
    return (
        f"{DOC_HEADER_PREFIX} role={spec.key} form={spec.form} "
        f"reports-to={spec.reports_to} dispatch={dispatch}"
    )


#: 体系根相对本模块的位置。
def maws_root() -> Path:
    """MAWS 根目录。本文件在 `<体系根>/coordinator/bg_coordinator/` 下，往上三层。"""
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleSpec:
    """一个角色的形态与权责。

    `form` 与 `reports_to` 一起决定**它能不能派子代理**：
    子代理不能再开子代理，所以「派单」这件事只可能发生在独立会话。
    """

    key: str
    form: str  # independent（独立会话）｜ subagent（子代理）
    reports_to: str
    dispatch: tuple[str, ...]  # 它能派谁
    brief: str
    workface: tuple[str, ...]

    @property
    def can_dispatch(self) -> bool:
        return bool(self.dispatch)


#: 七角色。**这是角色的单一权威**——`Multi-Agents-Working-Schema/agents/*.md` 是它的人读展开。
ROLES: tuple[RoleSpec, ...] = (
    RoleSpec(
        key=Role.COMMANDER.value,
        form="independent",
        reports_to="user",
        # 2026-10-06 用户定：commander 也可以开子代理——但**仍不直开 dev**（10-04 口径保留），
        # 边固定为 commander→po（pm 与 dev 无派单权）。
        dispatch=("po",),
        brief="用户接口 ＋ git 机制层 ＋ 最终仲裁",
        workface=("git worktree/分支/合并/推送", "主检出守护", "仲裁", "override"),
    ),
    RoleSpec(
        key=Role.PO.value,
        form="independent",
        reports_to="user",
        dispatch=("pm",),
        brief="跨线优先级与范围仲裁；不做需求分拣、不派 dev",
        workface=("跨线优先级", "范围与资源冲突", "积压盘点", "PM 名册"),
    ),
    RoleSpec(
        key=Role.PM.value,
        form="subagent",
        reports_to=Role.PO.value,
        dispatch=(),
        brief="本线需求 → 条目定义；保留实质验收权",
        workface=("需求分析", "条目定义（四要素）", "实质验收", "标准撰写"),
    ),
    RoleSpec(
        key="tech-lead",
        form="independent",
        reports_to="user",
        dispatch=("dev",),
        brief="本线唯一执行入口：认领、派 dev、独立重跑门禁、发起合并；不碰验收",
        workface=("已定稿条目认领", "派 dev", "门禁复核", "合并发起", "冲突处理"),
    ),
    RoleSpec(
        key=Role.DEV.value,
        form="subagent",
        reports_to="tech-lead",
        # 2026-10-06 用户定：**开子代理仅限 PO 与 TL**（仅限 MAWS，不影响生产）——
        # 撤销 10-04 的「dev 可再开 dev」例外；dev 只做实现，不再派单。
        dispatch=(),
        brief="实现 ＋ 测试 ＋ 门禁；永不 merge/push、不改任务数据",
        workface=("本线工作面（见工程绑定 §线别与工作面）",),
    ),
    RoleSpec(
        key=Role.OPS.value,
        form="independent",
        reports_to="user",
        dispatch=("dev",),
        brief="部署、迁移窗口执行、巡检、容量；完全权限但只在本面",
        workface=("部署面（见工程绑定 §线别与工作面）", "服务器与后台任务", "迁移执行窗口"),
    ),
    RoleSpec(
        key=Role.DBA.value,
        form="independent",
        reports_to="user",
        dispatch=("dev",),
        brief="迁移评审闸 ＋ 实例运维；评审即入库",
        workface=("迁移件评审", "备份/恢复/容量/慢查询/授权", "资源回收执行层"),
    ),
)


# ---------------------------------------------------------------------------
# 动词
# ---------------------------------------------------------------------------


#: **工程绑定的必需字段**——单一权威。`binding.py` 只按这份清单校验，不另立一份。
#:
#: 体系管机制、工程管落点：**换一个工程还成立**的写进 `agents/`／`rules/`；
#: 换一个工程就不成立的（线别、工作面、门禁、资产名、窗口段序）写进绑定。
BINDING_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("project", "str", "工程名（与目标仓目录同名）"),
    ("version", "int", "绑定格式版本"),
    ("lines", "dict", "线别 → {name, workface[]}：本工程有哪几条线、各线工作面"),
    ("gate", "dict", "门禁：入口／快跑标记／域映射／共享面／静态腿／发布口径／真库并发纪律"),
    ("migrations", "dict", "迁移目录、工具、命名、已知坑"),
    ("tests", "dict", "测试口径：真库标记／真库夹具／基座／**自称与事实**的硬要求"),
    ("protected_assets", "list", "受保护资产（回收器永不触碰）"),
    ("scratch_namespace", "str", "scratch 命名空间前缀"),
    ("window", "list", "部署窗口段序"),
    ("shared_files", "list", "共享面（改动须串行或单点改）"),
)

#: 只读动词：不改状态，因此**不需要角色闸**（谁都能查）。
READ_VERBS: tuple[str, ...] = (
    "ready",
    "schedule",  # 调度器：只算不动的计划面
    "trace",
    "why",
    "audit",
    "report",
    "status",
    "health",
    "number",
    "line",
    "leases",
    "quota",
    "verify-state",
    "bind",
    "grants",
    "confirms",
)

#: 资源动词：不经状态机，走租约自己的生命周期。
RESOURCE_VERBS: tuple[str, ...] = (
    "acquire",
    "release",
    "reclaim",
    "reserve",
    "materialize",
    "release-number",
    "dispatch",
    # 用户确认：commander 发需求前必须先拿到它（凭证面，不进状态机）
    "confirm",
)

#: 合并队列动词。
MERGE_VERBS: tuple[str, ...] = (
    "merge-request",
    "merge-next",
    "merge-ok",
    "merge-conflict",
    "merge-status",
)


def state_verbs() -> tuple[str, ...]:
    """走状态机的动词——**从转换表派生，不另立一份**。

    这正是 Schema 的价值：动词清单不是手写的，它跟着实现走。
    """
    return tuple(sorted(TRANSITIONS))


def all_verbs() -> dict[str, tuple[str, ...]]:
    return {
        "state": state_verbs(),
        "resource": RESOURCE_VERBS,
        "merge": MERGE_VERBS,
        "read": READ_VERBS,
    }


def verb_roles(verb: str) -> tuple[str, ...]:
    """某动词允许的角色——从转换表取，**与运行时判定同源**。"""
    tr = TRANSITIONS.get(verb)
    if tr is None:
        return ()
    return tuple(sorted(canonical_role(r.value) for r in tr.roles))


# ---------------------------------------------------------------------------
# 编号资源
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FamilySpec:
    """编号族。**号是资源**——每个族都要说清它服务于什么、谁发。"""

    family: str
    serves: str
    issued_by: str
    two_phase: bool
    note: str = ""


FAMILIES: tuple[FamilySpec, ...] = (
    FamilySpec("R-<线>", "需求", "coordinator", False, "登记即落物"),
    FamilySpec("T-<线>", "开发任务", "coordinator", False, "定稿即落物"),
    FamilySpec("OPS", "运维条目", "coordinator", False, "无类型前缀"),
    FamilySpec("alembic", "数据库迁移件", "coordinator", True, "**两阶段**：先占号，写完结件后落物"),
)


# ---------------------------------------------------------------------------
# 与实现对账
# ---------------------------------------------------------------------------


@dataclass
class Drift:
    """一处 Schema 与实现的不一致。"""

    kind: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.kind}] {self.detail}"


@dataclass
class SchemaReport:
    version: int
    roles: tuple[RoleSpec, ...]
    verbs: dict[str, tuple[str, ...]]
    families: tuple[FamilySpec, ...]
    categories: dict[str, str]
    dev_lines: tuple[str, ...]
    drifts: list[Drift] = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        return not self.drifts

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "roles": [
                {
                    "key": r.key,
                    "form": r.form,
                    "reports_to": r.reports_to,
                    "dispatch": list(r.dispatch),
                    "brief": r.brief,
                }
                for r in self.roles
            ],
            "verbs": {k: list(v) for k, v in self.verbs.items()},
            "families": [
                {
                    "family": f.family,
                    "serves": f.serves,
                    "issued_by": f.issued_by,
                    "two_phase": f.two_phase,
                }
                for f in self.families
            ],
            "categories": dict(self.categories),
            "dev_lines": list(self.dev_lines),
            "drifts": [str(d) for d in self.drifts],
            "consistent": self.consistent,
        }


def reconcile() -> SchemaReport:
    """**对账**：Schema 声明的，实现里必须真有。

    检查四类：
    ① 每个状态动词都有转换表条目（否则文档写了、代码没有）；
    ② 每个动词的允许角色与 Schema 角色集相容；
    ③ 决策类别表覆盖到每个角色；
    ④ 子代理角色**没有派单权**（子代理不能开子代理）。
    """
    drifts: list[Drift] = []
    role_keys = {r.key for r in ROLES}

    # ① 转换表与动词清单一致
    for verb in state_verbs():
        if verb not in TRANSITIONS:
            drifts.append(Drift("verb-missing", f"{verb} 在 Schema 却不在转换表"))

    # ② 动词允许的角色必须都在 Schema 里
    for verb in state_verbs():
        for role in verb_roles(verb):
            if role not in role_keys:
                drifts.append(Drift("role-unknown", f"{verb} 允许未登记的角色 {role}"))

    # ③ **子代理一律不得自开**（2026-10-06 用户定：开子代理仅限 PO 与 TL，
    #    仅限 MAWS，不影响生产）——10-04 的「dev 可再开 dev」例外已撤销。
    #    额度／复用／空转三处控制照旧在，但它们管的是"开多少"，不是"谁能开"。
    for r in ROLES:
        if r.form == "subagent" and r.can_dispatch:
            drifts.append(Drift("subagent-dispatch", f"{r.key} 是子代理却声明了派单权"))
    #    有派单权的只能是这两位，且边固定：PO→pm，TL→dev
    _DISPATCH_RIGHTS = {
        Role.COMMANDER.value: {Role.PO.value},
        Role.PO.value: {Role.PM.value},
        "tech-lead": {Role.DEV.value},
        Role.OPS.value: {Role.DEV.value},
        Role.DBA.value: {Role.DEV.value},
    }
    for r in ROLES:
        if not r.can_dispatch:
            continue
        allowed = _DISPATCH_RIGHTS.get(r.key)
        if allowed is None:
            drifts.append(Drift("dispatch-rights",
                             f"{r.key} 有派单权——仅限独立角色（10-06：pm／dev 除外）"))
        elif set(r.dispatch) != allowed:
            drifts.append(Drift("dispatch-rights", f"{r.key} 的派单边只许是 {sorted(allowed)}：{r.dispatch}"))

    #    另一端同一口径：**commander 不得直开 dev**（10-04 用户定） ⇒ 必须有角色能派 dev
    if not any(Role.DEV.value in r.dispatch for r in ROLES):
        drifts.append(Drift("dev-unreachable", "没有任何角色能派 dev——实现体就没人能开了"))

    # ⑤ **体系侧登记的绑定必须自洽**——登记了就要能解析、字段齐、不空值。
    #    绑定坏掉不该等到某个工程派单时才炸。
    from .binding import scan_registered

    for rec in scan_registered(BINDING_FIELDS):
        for gap in rec["gaps"]:
            drifts.append(Drift("binding-incomplete", f"bindings/{rec['file']}：{gap}"))

    # ④ 决策类别必须都有人接
    for cat, owner in CATEGORY_OWNER.items():
        owner = canonical_role(owner)
        if owner not in role_keys:
            drifts.append(Drift("category-owner", f"类别 {cat} 的裁决者 {owner} 未登记"))

    # ⑤ 文档 ↔ Schema
    root = maws_root()
    for doc in RULE_DOCS:
        if not (root / doc).exists():
            drifts.append(Drift("doc-missing", f"规则文档缺失：{doc}"))

    agents_dir = root / "agents"
    for r in ROLES:
        doc = agents_dir / f"{r.key}.md"
        if not doc.exists():
            drifts.append(Drift("role-doc-missing", f"角色 {r.key} 无对应文件 agents/{r.key}.md"))
            continue

        header = parse_doc_header(doc.read_text(encoding="utf-8"))
        if header is None:
            drifts.append(
                Drift(
                    "role-doc-header",
                    f"agents/{r.key}.md 缺可解析头部；应写：{render_doc_header(r)}",
                )
            )
            continue

        expected = {
            "role": r.key,
            "form": r.form,
            "reports-to": r.reports_to,
            "dispatch": r.dispatch,
        }
        actual = {
            "role": header.role,
            "form": header.form,
            "reports-to": header.reports_to,
            "dispatch": header.dispatch,
        }
        for field_name, want in expected.items():
            got = actual[field_name]
            if got != want:
                drifts.append(
                    Drift(
                        "role-doc-drift",
                        f"agents/{r.key}.md 的 {field_name} 写的是 {got!r}，Schema 是 {want!r}",
                    )
                )

    if agents_dir.is_dir():
        known = {r.key for r in ROLES}
        for extra in sorted(p.name for p in agents_dir.glob("*.md")):
            if extra[:-3] not in known:
                drifts.append(
                    Drift("role-doc-extra", f"agents/{extra} 无对应角色登记（Schema 里没有 {extra[:-3]}）")
                )

    # ⑥ 编码里引用的拒绝码必须真实存在
    code_names = {c.value for c in Code}
    for needed in ("E_DUP_ID", "E_FORBIDDEN_WRITE", "E_NO_REASON", "E_QUOTA_FULL"):
        if needed not in code_names:
            drifts.append(Drift("code-missing", f"拒绝码 {needed} 未定义"))

    return SchemaReport(
        version=SCHEMA_VERSION,
        roles=ROLES,
        verbs=all_verbs(),
        families=FAMILIES,
        categories=dict(CATEGORY_OWNER),
        dev_lines=DEFAULT_DEV_LINES,
        drifts=drifts,
    )


def render_schema() -> str:
    """人读形态——用于 `coord maws`。"""
    rep = reconcile()
    lines = [f"MAWS v{rep.version}（Multi-Agents Working Schema）", ""]

    lines.append("角色")
    for r in rep.roles:
        shape = "独立会话" if r.form == "independent" else "子代理"
        disp = "、".join(r.dispatch) if r.dispatch else "—"
        lines.append(f"  {r.key:<10} {shape:<6} 归 {r.reports_to:<9} 派 {disp:<5} {r.brief}")

    lines.append("")
    lines.append("工程绑定（必需字段——体系管机制，工程管落点）")
    for name, kind, desc in BINDING_FIELDS:
        lines.append(f"  {name:<18} {kind:<5} {desc}")

    lines.append("")
    lines.append("动词")
    for group, verbs in rep.verbs.items():
        lines.append(f"  {group:<9} {len(verbs):>2} 个  " + "、".join(verbs))

    lines.append("")
    lines.append("编号族（号是资源）")
    for f in rep.families:
        phase = "两阶段" if f.two_phase else "一步"
        lines.append(f"  {f.family:<10} {phase:<6} 服务 {f.serves:<8} 发放 {f.issued_by}")

    lines.append("")
    lines.append("决定类别（决定归谁裁）")
    for cat, owner in rep.categories.items():
        lines.append(f"  {cat:<11} → {owner}")

    lines.append("")
    if rep.consistent:
        lines.append("对账：**Schema 与实现一致**")
    else:
        lines.append("对账：**发现不一致**")
        for d in rep.drifts:
            lines.append(f"  {d}")
    return "\n".join(lines)
