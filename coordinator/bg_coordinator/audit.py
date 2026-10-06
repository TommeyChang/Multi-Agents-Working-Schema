"""溯源与审核——方案 v2 §4.9。

**反模式（必须消除）**：把 14 项审计的原始数字、61 份 SUBMISSION 路径、1054 行线板
抄进报告给人看——那还是"把所有文档又丢过来"，只是换了个容器。

**正确形态**：
- 审核**在动作提交点按条目做**，**通过即静默**，失败才产一条带责任人的异常；
- 溯源按需查询（正查 / 反查），而不是把全量摊开。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .engine import State, pid_alive
from .errors import Code
from .models import Event, Kind, LeaseState, Origin, Task, TaskState
from .validators import deps_all_terminal, deps_missing, rule_8b_evidence_readable, whitelist_covers

# ---------------------------------------------------------------------------
# 上下文边界（设计稿 §10「读命令必须有界」）
# ---------------------------------------------------------------------------

#: 正查默认只出尾部这么多条事件。
DEFAULT_TRACE_LIMIT = 12
#: 反查默认最多列这么多条匹配。
DEFAULT_WHY_LIMIT = 5
#: 单字段显示宽度——超长即裁剪，防止一条长文案独占上下文。
DEFAULT_FIELD_WIDTH = 120


def _clip(text: str, width: int = DEFAULT_FIELD_WIDTH) -> str:
    """裁剪长字段。**宁可显示省略号，也不让一条长文案吃掉上下文。**"""
    text = " ".join(str(text).split())
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"

# ---------------------------------------------------------------------------
# 溯源
# ---------------------------------------------------------------------------


@dataclass
class TraceNode:
    """一条证据链上的节点。"""

    seq: int
    ts: str
    verb: str
    actor: str
    before: dict[str, object]
    after: dict[str, object]
    result: str
    detail: dict[str, object] = field(default_factory=dict)


@dataclass
class Trace:
    """一个条目的完整证据链——**正查**（方案 v2 §4.9.1）。"""

    task: Task
    nodes: list[TraceNode]

    def render(self, limit: int = DEFAULT_TRACE_LIMIT, full: bool = False) -> str:
        """渲染证据链。

        **默认只出尾部 limit 条**——证据链是给人看结论的，不是给人看全史的。
        条目返工多次时，全史会线性膨胀；**读命令必须有界**（设计稿 §10）。
        传 `full=True` 才出全部。
        """
        t = self.task
        lines = [
            f"{t.id} 证据链（{t.line} 线 · {t.kind} · 当前 {t.status} · ver {t.ver}）",
            f"  白名单 {_clip('、'.join(t.whitelist) or '（空）')}",
            f"  验收 {len(t.acceptance)} 条 · 证据 {len(t.evidence)} 轮 · 优先级 {t.priority or '（未定）'}",
        ]
        nodes = self.nodes if full else self.nodes[-limit:]
        hidden = len(self.nodes) - len(nodes)
        if hidden > 0:
            lines.append(f"  …（前 {hidden} 条已折叠；要看全部加 --full）")
        lines.append("")
        for n in nodes:
            mark = "✓" if n.result == "ok" else "✗"
            lines.append(
                f"  {mark} {n.verb:<14} {n.actor:<12} {n.ts}  "
                f"{n.before.get('status', ''):>12} → {n.after.get('status', ''):<12}"
                + (f"  {n.result}" if n.result != "ok" else "")
            )
            if n.detail:
                for k in ("commit", "gate_exit", "evidence_path", "merge_id", "reason"):
                    if k in n.detail and n.detail[k] not in (None, ""):
                        lines.append(f"      {k}: {_clip(str(n.detail[k]))}")
        return "\n".join(lines)


def trace(events: list[Event], task: Task) -> Trace:
    """正查：这条目凭什么算完成？谁验的？"""
    nodes = [
        TraceNode(
            seq=e.seq,
            ts=e.ts,
            verb=e.verb,
            actor=e.actor,
            before=e.before,
            after=e.after,
            result=e.result,
            detail=e.detail,
        )
        for e in events
        if e.id == task.id
    ]
    return Trace(task=task, nodes=nodes)


@dataclass
class WhyResult:
    """**反查**——这个文件／commit 为什么是这个样？谁授权的？范围是什么？

    现状**几乎做不到**：`git blame` 只给出 commit，**给不出"当时的范围与验收标准"**。
    """

    path: str
    tasks: list[Task]
    commits: list[str]

    def render(self, limit: int = DEFAULT_WHY_LIMIT) -> str:
        """渲染反查结果。**匹配条目数与提交列表都有上限**——反查是定位用的，不是清单。"""
        if not self.tasks:
            return f"{_clip(self.path)}\n  （无关联条目——可能是未经协调器的改动，值得关注）"
        lines = [_clip(self.path)]
        for t in self.tasks[:limit]:
            lines.append(
                f"  ← {t.id}（{t.line} · {t.status} · 优先级 {t.priority or '未定'}）"
                f"{_clip(t.title)}"
            )
            lines.append(f"      白名单：{_clip('、'.join(t.whitelist) or '（空）')}")
            lines.append(f"      验收：{len(t.acceptance)} 条")
            for ev in t.evidence[-2:]:  # 只出最近两轮，早轮次不占上下文
                lines.append(f"      轮 {ev.round}：commit {ev.commit} · 门禁 exit={ev.gate_exit}")
        if len(self.tasks) > limit:
            lines.append(f"  …（另有 {len(self.tasks) - limit} 条匹配，未列）")
        if self.commits:
            shown = self.commits[:limit]
            tail = f"（另有 {len(self.commits) - limit} 个）" if len(self.commits) > limit else ""
            lines.append(f"  相关提交：{', '.join(shown)}{tail}")
        return "\n".join(lines)


def why(state: State, path: str, events: list[Event] | None = None) -> WhyResult:
    """反查：文件 → 条目（经白名单匹配 ＋ 交付时的 changed_files）。"""
    hits: list[Task] = []
    commits: list[str] = []
    for t in state.tasks.values():
        matched = False
        hit_changed = bool(t.changed_files) and any(
            f == path or f.endswith(path) for f in t.changed_files
        )
        hit_whitelist = bool(t.whitelist) and whitelist_covers([path], t.whitelist)[0]
        if hit_changed or hit_whitelist:
            matched = True
        if matched:
            hits.append(t)
            commits.extend(ev.commit for ev in t.evidence if ev.commit)
    if events:
        for e in events:
            if e.detail.get("evidence_path", "").endswith(path) and e.id not in {
                h.id for h in hits
            }:
                t = state.tasks.get(e.id)
                if t:
                    hits.append(t)
    return WhyResult(path=path, tasks=hits, commits=commits)


# ---------------------------------------------------------------------------
# 审核——只产异常
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Anomaly:
    """一条异常——报告「异常」节的唯一来源。

    **通过项必须静默**：它们不产生 `Anomaly`，因此永远不进报告。
    """

    code: Code
    id: str
    message: str
    owner: str = ""
    hint: str = ""

    def render_row(self) -> str:
        return (
            f"| {self.id} | {_clip(self.message, 80)} | "
            f"{self.owner or '—'} | {_clip(self.hint, 60) or '—'} |"
        )


def audit(state: State, events: list[Event] | None = None, clock: float = 0.0) -> list[Anomaly]:
    """按条目审核，**只返回异常**。

    这是 `process_audit` 的"按条目筛异常"改造版（方案 v2 §4.9.2）：
    全局 14 项检查不再原样输出，而是被消化成这里的异常。
    """
    out: list[Anomaly] = []

    for t in sorted(state.tasks.values(), key=lambda x: x.id):
        # 1. 已终态但缺证据
        if t.status == TaskState.ACCEPTED and not t.evidence and t.kind.value == "T":
            out.append(
                Anomaly(
                    Code.E_NO_EVIDENCE,
                    t.id,
                    "已验收但无交付证据",
                    owner=t.acceptor or t.owner,
                    hint="终态必须有可核证据",
                )
            )

        # 2. 交付／已验证但证据路径不可核
        if t.status in {TaskState.DELIVERED, TaskState.VERIFIED, TaskState.ACCEPTED}:
            ev = t.latest_evidence
            if ev is not None:
                rej = rule_8b_evidence_readable(ev.evidence_path)
                if rej:
                    out.append(
                        Anomaly(
                            Code.E_NO_EVIDENCE,
                            t.id,
                            f"证据路径不可核：{ev.evidence_path}",
                            owner=t.owner,
                            hint="重跑门禁并重新 deliver",
                        )
                    )
                if ev.gate_exit != 0:
                    out.append(
                        Anomaly(
                            Code.E_GATE_NONZERO,
                            t.id,
                            f"门禁退出码 {ev.gate_exit}",
                            owner=t.owner,
                            hint="禁把红说成既有红",
                        )
                    )

        # 3. 前置未终态却在推进
        if t.status in {TaskState.CLAIMED, TaskState.IN_PROGRESS}:
            pending = deps_all_terminal(state.tasks, t.deps)
            if pending:
                out.append(
                    Anomaly(
                        Code.E_NOT_READY,
                        t.id,
                        f"在办但前置未终态：{pending}",
                        owner=t.owner,
                        hint="前置未终态不得开工",
                    )
                )

        # 4. 在办但无优先级
        if t.in_flight() and t.priority is None:
            out.append(
                Anomaly(
                    Code.E_NO_PRIORITY,
                    t.id,
                    "在办但未定优先级",
                    owner=t.owner,
                    hint="优先级由 PO 裁定",
                )
            )

        # 5. 依赖引用不存在（悬空）
        missing = deps_missing(state.tasks, t.deps)
        if missing:
            out.append(
                Anomaly(Code.E_NO_DEP, t.id, f"前置引用不存在：{missing}", owner=t.owner)
            )

        # 6. 白名单为空但已定稿
        if t.status.value in {"defined", "claimed", "in_progress"} and not t.whitelist:
            out.append(
                Anomaly(Code.E_INCOMPLETE, t.id, "已定稿但白名单为空", owner=t.definer or t.owner)
            )

    # 7. 租约异常（资源枯竭的**事前可见**——方案 v2 §5.3）
    holders: dict[str, int] = {}
    for ls in state.leases.values():
        if ls.state == LeaseState.ACTIVE:
            holders[ls.holder] = holders.get(ls.holder, 0) + 1
            # **无 pid 的租约不拿 pid 判死**（如子代理授权）：只看 TTL。
            # 与 `_sweep_reclaimable` 同一取向——判据缺失时偏"少回收、不误杀"。
            if ls.pid > 0 and not pid_alive(ls.pid):
                out.append(
                    Anomaly(
                        Code.E_IN_USE,
                        ls.lease_id,
                        f"租约属主 pid {ls.pid} 已死（{ls.db_name}）",
                        owner="dba",
                        hint="可回收——回收执行归 dba",
                    )
                )
            elif ls.expired(clock):
                out.append(
                    Anomaly(
                        Code.E_IN_USE,
                        ls.lease_id,
                        f"租约 TTL 过期（{ls.db_name}）",
                        owner=ls.holder,
                        hint="可回收",
                    )
                )

    # 8. **待建对象占号**——号已占，物未落
    #
    #    `pending` 本身是正常状态（先取号后建件）。但**没有归属的待建**
    #    就是资源被占着不动：谁占的、为什么占、什么时候落，一概不知。
    from .engine import pending_allocations

    for alloc in pending_allocations(state):
        fam = str(alloc.get("family", ""))
        num = int(alloc.get("number", 0))
        holder = alloc.get("holder") or ""
        task = alloc.get("task") or ""
        if not holder and not task:
            out.append(
                Anomaly(
                    Code.E_NUMBER_PENDING,
                    f"{fam}-{num}",
                    "待建号无归属（既无持有人，也无来源条目）",
                    owner="pm",
                    hint="补持有人与来源，或让号",
                )
            )
        elif not task:
            out.append(
                Anomaly(
                    Code.E_NUMBER_PENDING,
                    f"{fam}-{num}",
                    f"待建号未绑定来源条目（持有 {holder}）",
                    owner=holder,
                    hint="绑定到具体条目，便于追溯为何占号",
                )
            )

    # 9. **文档同步**——不强制修订文档，但**一旦修订必须同批**
    #
    #    误读提醒：旧口径写「文档与代码必须同步更新」，容易被读成"必须改文档"。
    #    正确含义是**成对**：改了面就必须同批改文档，反之亦然。
    from .validators import touches_design_surface

    for t in sorted(state.tasks.values(), key=lambda x: x.id):
        if t.status not in {
            TaskState.DELIVERED,
            TaskState.VERIFIED,
            TaskState.ACCEPTED,
        }:
            continue
        design_files = touches_design_surface(t.changed_files)
        if design_files and not t.doc_sync:
            out.append(
                Anomaly(
                    Code.E_DOC_UNSYNCED,
                    t.id,
                    f"触碰设计面（{design_files[:2]}）但本批无文档同步",
                    owner=t.owner or t.definer,
                    hint="文档修订非必做；但**改了面就同批改文档**——否则或补文档，或说明这次不动口径",
                )
            )

    # 10. **号段空洞**——有号无物
    #
    #    号是资源：每个号都该有对应物。没有对应物的号就是被浪费掉的资源，
    #    而且是**静默浪费**——不查账就发现不了。
    from .engine import number_gaps

    for gap in number_gaps(state):
        out.append(
            Anomaly(
                Code.E_NUMBER_GAP,
                "编号",
                f"号段空洞：{gap}",
                owner="pm",
                hint="说明去向：让号／作废／补建对象",
            )
        )

    # 11. **同号双占**——同族同号在账上出现两次
    #
    #    判据取自既有取号器（主检出 `scripts/tools/alloc_number/_check.py` 的「同号双占」）。
    #    本体系**不另立第二套账**，只把这条判据折进来：
    #    **号是资源，一个号只能有一个归属**。同号两行之后，后面每个"这个号在不在账上"
    #    的判断都会失准——让号可能只让掉一行，落物可能落在另一行。
    #
    #    正常路径（`alloc_number` 单调推进）到不了这里；能到这里的只有 `override`、
    #    事件重放或人工改状态——所以它是**账本完整性**的哨兵，不是日常噪声。
    from .engine import format_id

    counts: dict[tuple[str, int], int] = {}
    for entry in state.allocations:
        key = (str(entry.get("family", "")), int(entry.get("number", -1)))
        counts[key] = counts.get(key, 0) + 1
    for (family, number), count in sorted(counts.items()):
        if count > 1:
            out.append(
                Anomaly(
                    Code.E_NUMBER_TWICE,
                    format_id(family, number),
                    f"同号双占：账上出现 {count} 次",
                    owner="pm",
                    hint="查明来源后清理重复行；让号／落物前先确认只对应一行",
                )
            )

    # 11.5 **串行族的在飞占号超过一个**——放号闸只挡"以后"，账上的存量要报出来
    #
    #     存量可能来自：规则生效前取的号、`override` 绕过的、事件重放出来的。
    #     迁移件的号顺序就是链位顺序，两个在飞必然分叉——报给 dba 收口。
    from .engine import SERIAL_FAMILIES, inflight_of

    for fam in sorted(SERIAL_FAMILIES):
        flying = inflight_of(state, fam)
        if len(flying) > 1:
            nums = [int(e.get("number", -1)) for e in flying]
            out.append(
                Anomaly(
                    Code.E_NUMBER_INFLIGHT,
                    f"{fam} 族",
                    f"在飞占号 {len(flying)} 个：{nums}——该族必须串行落物（号顺序＝链位顺序）",
                    owner="dba",
                    hint="只留一个在飞：其余 materialize 或让号（须给理由）",
                )
            )

    # 11.8 **在办但无子代理授权**——"开了几个"不能只靠自报
    #
    #     在办意味着**有人在做**；而按本体系的口径，做的人要么是独立会话，
    #     要么是**领了授权的子代理**。没凭证的在办 = 越权派单或漏领凭证，
    #     两种都该被看见（子代理不得自开，见 SUBAGENT.md §一）。
    from .engine import active_grants

    granted_tasks = {ls.task for ls in active_grants(state) if ls.task}
    for t in sorted(state.tasks.values(), key=lambda x: x.id):
        if t.in_flight() and t.id not in granted_tasks:
            out.append(
                Anomaly(
                    Code.E_UNAUTHORIZED_DISPATCH,
                    t.id,
                    "在办但没有在手的子代理授权",
                    owner=t.owner,
                    hint="先领凭证：coord dispatch --role <派单方> --task <条目号>",
                )
            )

    # 11.9 **commander 的需求没有用户确认留痕**——闸只管"新发的"，这条管"存量的"。
    #
    #      闸在 register 那一刻拦；但确认是**独立一次动作**，所以还要能回答
    #      "手上这些需求里，哪些其实没有用户点头"——存量、旁路、以及
    #      闸上线之前登记的条目，都只能靠这条审计看见。
    for t in sorted(state.tasks.values(), key=lambda x: x.id):
        if t.kind != Kind.R or t.origin != Origin.COMMANDER.value or t.confirmed_by:
            continue
        out.append(
            Anomaly(
                Code.E_NO_USER_CONFIRM,
                t.id,
                "commander 的需求没有用户显式确认留痕（commander 发需求必须先取得用户确认）",
                owner="commander",
                hint=(
                    "补留痕：先请用户确认，再 coord confirm --line <线> "
                    f'--title "{t.title}" --role <会话> --said "<用户原话>"；'
                    "（以别的 origin 绕闸的写入在事件流里 actor 可见，可人工核）"
                ),
            )
        )

    # 11.10 **功能条目没有闭环路径**——闸在 accept 那一刻；这条管"还没走到验收的"。
    #
    #      为什么要有：`accept` 时的闸只拦得住"走到验收"的条目，
    #      而"只交模块单测"的条目往往**根本走不到**（或者被人工放行）。
    #      存量与该报的，在这里逐条露出来。
    from .models import AcceptanceType
    from .validators import NO_CLOSURE

    for t in sorted(state.tasks.values(), key=lambda x: x.id):
        if t.kind != Kind.T or NO_CLOSURE in (t.constraints or []):
            continue
        if any(i.type == AcceptanceType.CLOSURE for i in t.acceptance):
            continue
        if not t.acceptance:
            continue  # 还没定稿的条目不算缺口（define 会拦四要素）
        out.append(
            Anomaly(
                Code.E_NO_CLOSURE_PATH,
                t.id,
                "功能条目的验收里没有闭环路径（只有模块单测＝未闭环）",
                owner=t.definer or t.owner or "pm",
                hint=(
                    "补一条 closure 验收项；确属纯文档／纯口径 ⇒ 声明 constraint "
                    f"`{NO_CLOSURE}`（审计会逐条点名，豁免可见）"
                ),
            )
        )
    #      豁免不是"消失"：声明了就要能被数出来
    exempt = sorted(t.id for t in state.tasks.values() if NO_CLOSURE in (t.constraints or []))
    if exempt:
        out.append(
            Anomaly(
                Code.OK,
                "闭环路径豁免",
                f"{len(exempt)} 条声明了 `{NO_CLOSURE}`：{'、'.join(exempt)}",
                owner="pm",
                hint="豁免要逐条能被看见；若某条其实需要闭环，撤掉声明",
            )
        )

    # 11.11 **交付没申报测试级别**——分级口径（2026-10-05）的第一步只是"看得见"。
    #
    #       为什么不直接拦：分级要先知道"该跑哪一级"，那要绑定里的域映射；
    #       映射没填时拦就是假红。所以先报存量，等映射齐了再收紧成 BLOCK。
    for t in sorted(state.tasks.values(), key=lambda x: x.id):
        ev = t.latest_evidence
        if ev is None or ev.scope:
            continue
        out.append(
            Anomaly(
                Code.E_SCOPE_MISSING,
                t.id,
                f"交付证据没申报测试级别（第 {ev.round} 轮）",
                owner=t.owner or "tech-lead",
                hint="`deliver` 时给 `--test-scope static|affected|domain|full`；"
                     "合并前要与交付同一级复跑",
            )
        )

    # 11.12 **已验收却从未合入**——验收是"决定"，合并是"动作"；
    #       只决定不动作，代码就永远不在主干上（用户以为做完了）。
    from .models import MergeState
    from .models import TaskState as _TS

    merged_tasks = {
        m.task_id for m in state.merges.values() if m.state == MergeState.MERGED
    }
    for t in sorted(state.tasks.values(), key=lambda x: x.id):
        if t.status != _TS.ACCEPTED or t.id in merged_tasks:
            continue
        # **失败方向**：只有"真交付了代码"（证据里有 commit）的条目才有"合入"一说；
        # 复核/口径类条目不改码 ⇒ 没有 commit ⇒ 这条不点它（否则全是假红）。
        ev = t.latest_evidence
        if ev is None or not ev.commit or ev.commit.lower() in ("none", "-"):
            continue
        if True:
            out.append(
                Anomaly(
                    Code.E_ACCEPTED_UNMERGED,
                    t.id,
                    "已验收但没有任何一条合入记录——代码不在主干上",
                    owner=t.owner or "tech-lead",
                    hint="走 `merge-request`（六道闸）→ `merge-next` → `merge-ok` 把它合上",
                )
            )

    # 12. 合并队列里的冲突拒绝项 → 转 TL 动作项
    from .models import MergeState

    for m in state.merges.values():
        if m.state == MergeState.REJECTED:
            out.append(
                Anomaly(
                    Code.E_CONTENT_CONFLICT,
                    m.task_id,
                    f"合并被拒：{m.rejection}",
                    owner="tech-lead",
                    hint="在 worktree 解冲突后重新入队；禁自动取侧",
                )
            )

    return out


def audit_summary(state: State, events: list[Event] | None = None, clock: float = 0.0) -> dict[str, object]:
    """报告用的**摘要**——不是明细（明细请问协调器）。"""
    anomalies = audit(state, events, clock)
    total = len(state.tasks)
    return {
        "total": total,
        "anomalies": anomalies,
        "ok_count": max(total - len({a.id for a in anomalies}), 0),
        "anomaly_count": len(anomalies),
    }


def evidence_dir(repo: Path) -> Path:
    return repo / "worktrees"

