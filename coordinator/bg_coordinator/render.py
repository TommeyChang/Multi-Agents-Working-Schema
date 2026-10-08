"""视图渲染——markdown 从"双向账本"降为**单向渲染**（方案 v2 §4.3／§4.4／§4.5）。

两份视图，读者不同、约束不同：

| | 视图 A（兼容） | 视图 B（报告） |
|---|---|---|
| 读者 | 现有工具（**机读**） | **人** |
| 约束 | 必须精确兼容旧格式（7 格、token 首字符） | **自由** |

**关键**：视图 A 存在的唯一理由是让现有 290 处路径引用**一行都不用改**。
等读者都切到协调器 API，就可以简化它（方案 v2 §11 阶段六）。
"""

from __future__ import annotations

from pathlib import Path

from .audit import Anomaly, audit_summary
from .engine import State
from .models import CATEGORY_OWNER, LeaseState, Line, MergeState, Role, TaskState
from .readiness import ready_tasks
from .statemachine import to_legacy_token

LINES: tuple[Line, ...] = (Line.A, Line.B, Line.C, Line.D, Line.E, Line.ACL)

#: 每条线在视图 A 里的文件名（**保持现状不变**——这是零改造的关键）。
LINE_FILES: dict[Line, str] = {
    Line.A: "todo/lines/auth.md",
    Line.B: "todo/lines/domain.md",
    Line.C: "todo/lines/data-access.md",
    Line.D: "todo/lines/infra.md",
    Line.E: "todo/lines/ohs.md",
    Line.ACL: "todo/lines/acl.md",
}
INBOX_FILES: dict[Line, str] = {
    Line.A: "todo/inbox/A.md",
    Line.B: "todo/inbox/B.md",
    Line.C: "todo/inbox/C.md",
    Line.D: "todo/inbox/D.md",
    Line.E: "todo/inbox/E.md",
    Line.ACL: "todo/inbox/ACL.md",
}

#: 视图 A 的表头——**逐字保持现状**，否则 C8／C9 会红。
BOARD_HEADER = (
    "| 条目号 | 内容 | 修改范围白名单 | 不动清单 | 前置 | 验收标准 | 状态/认领人 |\n"
    "|---|---|---|---|---|---|---|\n"
)
INBOX_HEADER = "| 编号 | 需求 | 来源 | 日期 | 状态 | 处置 |\n|---|---|---|---|---|---|\n"


def render_all(state: State, clock: float = 0.0) -> dict[Path, str]:
    """协调器发布时调用的总入口——产出全部视图文件。"""
    files: dict[Path, str] = {}
    files.update(render_compat(state))
    files[Path("reports/调度报告.md")] = render_report(state, clock)
    files[Path("todo/board.md")] = render_board(state)
    return files


# ---------------------------------------------------------------------------
# 视图 A：兼容视图
# ---------------------------------------------------------------------------


def _escape_cell(text: str) -> str:
    """转义竖线——C9 的「未转义竖线才是分隔符」。"""
    return text.replace("|", r"\|").replace("\n", " ")


def render_line(state: State, line: Line) -> str:
    """线板表——**7 格、白名单反引号成对、状态格首字符是 token**（C3／C8／C9）。"""
    tasks = [t for t in state.tasks.values() if t.line == line]
    tasks.sort(key=lambda t: t.id)
    out = [f"# {line.value} 线 — 执行明细板（协调器投影，请勿直改）\n", BOARD_HEADER]
    for t in tasks:
        wl = "、".join(f"`{w}`" for w in t.whitelist) or "—"
        fz = "、".join(f"`{f}`" for f in t.frozen) or "—"
        dep = "、".join(t.deps) or "—"
        acc = f"{len(t.acceptance)} 条" if t.acceptance else "—"
        token = to_legacy_token(t.status)
        who = f"{token} {t.owner}".strip()
        out.append(
            f"| {t.id} | {_escape_cell(t.title)} | {wl} | {fz} | {dep} | {acc} | {who} |\n"
        )
    return "".join(out)


def render_inbox(state: State, line: Line) -> str:
    """inbox 表——表头↔分隔行**间距必须为 0**（C8）。"""
    tasks = [t for t in state.tasks.values() if t.line == line and t.kind.value == "R"]
    tasks.sort(key=lambda t: t.id)
    out = [f"# {line.value} 线 — 需求登记（协调器投影，请勿直改）\n", INBOX_HEADER]
    for t in tasks:
        token = to_legacy_token(t.status)
        out.append(
            f"| {t.id} | {_escape_cell(t.title)} | {_escape_cell(t.src)} | — | {token} | — |\n"
        )
    return "".join(out)


def render_board(state: State) -> str:
    """总账——各线计数一览。"""
    out = ["# 线路总账（协调器投影，请勿直改）\n\n", "| 线 | 需求 | 任务 | 已验收 | 在办 | 阻塞 |\n"]
    out.append("|---|---|---|---|---|---|\n")
    for line in LINES:
        ts = [t for t in state.tasks.values() if t.line == line]
        reqs = sum(1 for t in ts if t.kind.value == "R")
        tasks = sum(1 for t in ts if t.kind.value == "T")
        acc = sum(1 for t in ts if t.status == TaskState.ACCEPTED)
        flying = sum(1 for t in ts if t.in_flight())
        blocked = sum(1 for t in ts if t.status == TaskState.BLOCKED)
        out.append(f"| {line.value} | {reqs} | {tasks} | {acc} | {flying} | {blocked} |\n")
    return "".join(out)


def render_compat(state: State) -> dict[Path, str]:
    files: dict[Path, str] = {}
    for line in LINES:
        files[Path(LINE_FILES[line])] = render_line(state, line)
        files[Path(INBOX_FILES[line])] = render_inbox(state, line)
    return files


# ---------------------------------------------------------------------------
# 视图 B：人读报告（**报告只出现异常**）
# ---------------------------------------------------------------------------

#: 角色键 → 报告里的显示名（报告是人读的，不摆内部键）
_ROLE_LABELS: dict[str, str] = {
    "tech-lead": "TL",
    "po": "PO",
    "pm": "PM",
    "commander": "commander",
    "ops": "OPS",
    "dba": "DBA",
    "dev": "dev",
}


def _role_label(key: str) -> str:
    return _ROLE_LABELS.get(key, key or "—")


_PHASE_COLUMNS: tuple[tuple[str, tuple[TaskState, ...]], ...] = (
    ("未处理", (TaskState.REGISTERED,)),
    ("分析中", (TaskState.ANALYZING,)),
    ("已定稿", (TaskState.DEFINED,)),
    ("在办", (TaskState.CLAIMED, TaskState.IN_PROGRESS)),
    ("待验收", (TaskState.DELIVERED, TaskState.VERIFIED)),
    ("已验收", (TaskState.ACCEPTED,)),
    ("阻塞", (TaskState.BLOCKED,)),
)


def render_report(state: State, clock: float = 0.0) -> str:
    """**报告是人读的——所以它只递"异常与需要决定的事"**（方案 v2 §4.5／§4.9.4）。"""
    summary = audit_summary(state, clock=clock)
    anomalies: list[Anomaly] = summary["anomalies"]  # type: ignore[assignment]

    out: list[str] = ["# 调度报告\n\n"]
    out.append(f"> 生成自协调器投影 · 快照点 `seq={state.seq}`\n")
    out.append("> **本报告只列异常与待决**；明细不进报告——那叫把文档又丢过来。\n\n")

    # 一、总览
    out.append("## 一、总览\n\n")
    head = "| 线 | " + " | ".join(name for name, _ in _PHASE_COLUMNS) + " |\n"
    out.append(head)
    out.append("|---" * (len(_PHASE_COLUMNS) + 1) + "|\n")
    for line in LINES:
        ts = [t for t in state.tasks.values() if t.line == line]
        cells = [str(sum(1 for t in ts if t.status in states)) for _, states in _PHASE_COLUMNS]
        out.append(f"| {line.value} | " + " | ".join(cells) + " |\n")

    # 二、需要你决定（人工闸）
    out.append("\n## 二、需要你决定（人工闸）\n\n")
    gates: list[str] = []
    # **待接单**：已登记、无人认领的还在"未处理"格里——只有一行计数是不够的。
    # 尤其 `raise --category cross_line/design`：它把决定权交给 PO（类别决定去向），
    # 但条目停在 registered，PO 的可认领清单（只列 defined）**永远看不到它**。
    # 少了这一段，跨线登记就掉进一个没有可见面的缝里——静默等。
    intake: list[str] = []
    for t in sorted(state.tasks.values(), key=lambda x: x.id):
        if t.status == TaskState.BLOCKED:
            gates.append(f"| {t.id} | 阻塞：{t.block_reason} | — | 解阻或废弃 |")
        elif t.status == TaskState.DEFINED and t.priority is None:
            gates.append(f"| {t.id} | 未定优先级，无法派工 | PO | 裁定 P0/P1/P2 |")
        elif t.status == TaskState.VERIFIED and not t.acceptor:
            gates.append(f"| {t.id} | 已通过形式校验，待实质验收 | PM | 逐条比对验收标准 |")
        elif t.status == TaskState.REGISTERED and not t.owner:
            if t.scope == "cross_line":
                who = CATEGORY_OWNER.get(t.category, "po")
                intake.append(
                    f"| {t.id} | 跨线登记未受理（类别 {t.category or '—'}）"
                    f" | {_role_label(who)} | 转 PM 分析，或否决 |"
                )
            else:
                intake.append(f"| {t.id} | 已登记、无人认领 | PO | 归一后派 PM 分析 |")
    INTAKE_SHOWN = 10
    if gates or intake:
        out.append("| 条目 | 卡在哪 | 等谁 | 建议 |\n|---|---|---|---|\n")
        out.extend(g + "\n" for g in gates)
        out.extend(g + "\n" for g in intake[:INTAKE_SHOWN])
        if len(intake) > INTAKE_SHOWN:
            out.append(f"| … | 另有 {len(intake) - INTAKE_SHOWN} 条已登记未接单 | — | `coord list` |\n")
    else:
        out.append("（无）\n")

    # 三、在办
    out.append("\n## 三、在办\n\n")
    flying = [t for t in state.tasks.values() if t.in_flight()]
    if flying:
        out.append("| 条目 | 线 | 认领人 | 状态 | 轮次 | 标题 |\n|---|---|---|---|---|---|\n")
        for t in sorted(flying, key=lambda x: (x.line, x.id)):
            title = (t.title or "").replace("|", "／")[:30]
            out.append(f"| {t.id} | {t.line} | {t.owner or '—'} | {t.status} | {t.round} | {title} |\n")
    else:
        out.append("（无）\n")

    # 四、阻塞与风险
    out.append("\n## 四、阻塞与风险\n\n")
    risks: list[str] = []
    for t in state.tasks.values():
        if t.status == TaskState.BLOCKED:
            risks.append(f"- **{t.id}**：{t.block_reason}")
    from .validators import deps_all_terminal

    for t in state.tasks.values():
        pending = deps_all_terminal(state.tasks, t.deps)
        if pending and t.in_flight():
            risks.append(f"- **{t.id}**：前置未终态 {pending}（已在办）")
    out.extend(r + "\n" for r in risks)
    if not risks:
        out.append("（无）\n")

    # 五、资源
    out.append("\n## 五、资源\n\n")
    out.append("| 资源 | 用量 | 配额 | 可回收 | 说明 |\n|---|---|---|---|---|\n")
    active = [ls for ls in state.leases.values() if ls.state == LeaseState.ACTIVE]
    recl = [ls for ls in state.leases.values() if ls.state == LeaseState.RECLAIMABLE]
    out.append(
        f"| scratch 库 | {len(active)} | {state.quota.bg_db_max if state.quota.enabled else '（观测态）'} "
        f"| {len(recl)} | {'配额闸已启用（默认即闸）' if state.quota.enabled else '观测态：不拦，仅记录'} |\n"
    )
    q = [m for m in state.merges.values() if m.state == MergeState.QUEUED]
    merging = [m for m in state.merges.values() if m.state == MergeState.MERGING]
    out.append(
        f"| 合并队列 | {len(q)} 待合 | — | — | 进行中 {merging[0].merge_id if merging else '无'} |\n"
    )

    # 六、异常（**审核只在这里出现**）
    out.append("\n## 六、异常（审核只在这里出现）\n\n")
    if anomalies:
        out.append("| 条目 | 异常 | 责任人 | 建议动作 |\n|---|---|---|---|\n")
        out.extend(a.render_row() + "\n" for a in anomalies)
    else:
        out.append("（无异常）\n")
    out.append(
        f"\n> 本期审核：**正常 {summary['ok_count']} 条 / 异常 {summary['anomaly_count']} 条**"
        f"（快照点 `seq={state.seq}`）\n"
    )
    out.append("> 14 项审计明细不进报告——协调器已消化为上方异常；要明细请问协调器（`coord audit`）。\n")

    # 七、本期变更
    out.append("\n## 七、本期变更\n\n")
    out.append(
        f"- 条目总数 {len(state.tasks)} · 事件序号 {state.seq} · "
        f"租约 {len(state.leases)} · 合并 {len(state.merges)}\n"
    )
    out.append("- 变更明细见 `coord trace <id>`；**本报告不重复事件流**。\n")

    return "".join(out)


def render_todo_ready(state: State, role: Role = Role.TECH_LEAD) -> str:
    """给 TL／监视器用的「现在可认领」清单——`delta_since` 的落地形态。

    **配额取 `state.quota`**：这里曾经硬编码 `Quota()`，于是清单永远不反映真实预算——
    配额闸一变（默认由观测改为"未批预算派不了活"），清单却照旧，视图与闸各说各话。
    """
    out: list[str] = []
    unbudgeted: list[str] = []
    for line in LINES:
        if state.quota.enabled and state.quota.budget_of(line) is None:
            unbudgeted.append(line.value)
        ready = ready_tasks(state.tasks, role, line, state.quota)
        if ready:
            out.append(f"{line.value}: " + "、".join(f"{t.id}({t.priority})" for t in ready))
    if out:
        return "\n".join(out)
    if unbudgeted:
        # **不许给一个空清单就完事**：空是因为"没预算"，必须说出来
        return (
            "（无可认领条目——其中 "
            + "、".join(unbudgeted)
            + " 线**未批预算**，按口径不派活：`coord quota --line <线> --budget <n>`）"
        )
    return "（无可认领条目）"


def human_summary(state: State) -> str:
    """终端一行摘要。"""
    n = len(state.tasks)
    flying = sum(1 for t in state.tasks.values() if t.in_flight())
    acc = sum(1 for t in state.tasks.values() if t.status == TaskState.ACCEPTED)
    return f"条目 {n} · 在办 {flying} · 已验收 {acc} · seq {state.seq}"
