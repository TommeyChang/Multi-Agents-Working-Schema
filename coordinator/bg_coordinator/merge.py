"""合并队列——方案 v2 §6。

**串行化能解决的和不能解决的要分清**：
- 能：git 索引竞态（公约里那套 `diff --stat` 复核补丁的存在理由）；
- 不能：内容冲突。**冲突是语义问题，不是时序问题**——协调器**永不自动解冲突**。

因此：冲突一律**拒绝并转成动作项**给 TL，绝不"智能合并"。
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .engine import Result, State, now_iso
from .errors import Code, Rejection
from .models import Event, MergeRequest, MergeState, Task, TaskState
from .validators import rule_8b_evidence_readable, whitelist_covers


@dataclass
class MergeGates:
    """六条硬闸的判定结果——逐闸可解释，便于报告只报异常。"""

    passed: bool
    rejections: list[Rejection]

    @property
    def first(self) -> Rejection | None:
        return self.rejections[0] if self.rejections else None


def check_gates(
    state: State,
    task: Task,
    changed_files: list[str],
    base_commit: str,
    main_head: str,
    evidence_path_ok: Callable[[str], bool] | None = None,
    migration_check: Callable[[list[str], str], str] | None = None,
) -> MergeGates:
    """七闸（方案 v2 §6.2 ＋ **链位闸**）。

    | # | 闸 | 治什么 |
    |---|---|---|
    | 1 | 条目状态 = delivered／verified | 无实测即合并 |
    | 2 | commit 改动 ⊆ 条目白名单 | 越范围合入 |
    | 3 | 门禁实证存在且可核 | 证伪「台账先于核验」 |
    | 4 | 基座已含 main 最新 | 过期基座合入 |
    | 4.5 | **迁移链位**（注入 `migration_check`） | 号唯一 ≠ 链位唯一：两个各自合法的件合起来是两个 head |
    | 5 | 同一时刻只有一次合并 | （由 dispatch 保证，不在此判） |
    | 6 | 内容冲突即拒 | 语义问题 ≠ 时序问题（由调用方判定后传入） |

    `migration_check(changed_files, base_commit) -> str` 由调用方绑定目标仓
    （判据在 `bg_coordinator/migrations.py`，与提交闸共用）。返回空串 = 通过。
    **不注入即视为"无从判定"**——含迁移件的条目会被拒（宁可不合，也不假装通过）。
    """
    rej: list[Rejection] = []

    # 闸 1
    if task.status not in {TaskState.DELIVERED, TaskState.VERIFIED}:
        rej.append(
            Rejection(
                Code.E_NOT_DELIVERED,
                f"{task.id} 状态为 {task.status}，未交付不得合并",
                id=task.id,
                owner=task.owner,
            )
        )

    # 闸 2
    if task.whitelist:
        ok, out = whitelist_covers(changed_files, task.whitelist)
        if not ok:
            rej.append(
                Rejection(
                    Code.E_OUT_OF_SCOPE,
                    f"{task.id} 改动越出白名单：{out}",
                    id=task.id,
                    owner=task.owner,
                )
            )

    # 闸 3
    ev = task.latest_evidence
    if ev is None:
        rej.append(
            Rejection(Code.E_NO_EVIDENCE, f"{task.id} 无交付证据", id=task.id, owner=task.owner)
        )
    else:
        if ev.gate_exit != 0:
            rej.append(
                Rejection(
                    Code.E_GATE_NONZERO,
                    f"{task.id} 门禁退出码 {ev.gate_exit}",
                    id=task.id,
                    owner=task.owner,
                )
            )
        checker = evidence_path_ok or (lambda p: rule_8b_evidence_readable(p) is None)
        if not checker(ev.evidence_path):
            rej.append(
                Rejection(
                    Code.E_NO_EVIDENCE,
                    f"{task.id} 证据路径不可核：{ev.evidence_path}",
                    id=task.id,
                    owner=task.owner,
                )
            )

    # 闸 4
    if base_commit and main_head and base_commit != main_head:
        rej.append(
            Rejection(
                Code.E_STALE_BASE,
                f"{task.id} 基座过期（{base_commit[:8]} ≠ main {main_head[:8]}）",
                id=task.id,
                owner=task.owner,
                hint="先 rebase／merge main 再入队",
            )
        )

    # 闸 4.5：链位（只在有判据可注入时给出确定结论；否则对含迁移件的条目 fail closed）
    if migration_check is None:
        rej.append(
            Rejection(
                Code.E_INCOMPLETE,
                f"{task.id} 未提供迁移链判据——含迁移件的合入必须能判链位",
                id=task.id,
                owner=task.owner,
                hint="调用方须注入 migration_check（判据见 bg_coordinator/migrations.py）",
            )
        )
    else:
        # 链位要对的是**即将落上去的那个 head**（main head），不是分支自己的基座
        reason = migration_check(changed_files, main_head or base_commit)
        if reason:
            rej.append(
                Rejection(
                    Code.E_NUMBER_TWICE if "撞号" in reason else Code.E_INTEGRITY,
                    f"{task.id} 迁移链不合规：{reason}",
                    id=task.id,
                    owner=task.owner,
                    hint="把父节点接到当前 head 上并重新取号／落物，再入队",
                )
            )

    return MergeGates(passed=not rej, rejections=rej)


def in_flight_merge(state: State) -> MergeRequest | None:
    """闸 5：同一时刻只有一次合并。"""
    for m in state.merges.values():
        if m.state == MergeState.MERGING:
            return m
    return None


def queue(state: State) -> list[MergeRequest]:
    """可观测的队列：FIFO，按请求时间。"""
    return sorted(
        (m for m in state.merges.values() if m.state == MergeState.QUEUED),
        key=lambda m: m.requested_at,
    )


def request_merge(
    state: State,
    task_id: str,
    branch: str,
    commit: str,
    requester: str,
    changed_files: list[str],
    base_commit: str = "",
    main_head: str = "",
    evidence_path_ok: Callable[[str], bool] | None = None,
    migration_check: Callable[[list[str], str], str] | None = None,
    clock: float = 0.0,
) -> Result:
    """显式触发入队。**各闸不过即拒，且拒绝也是一条可追溯事件。**

    `migration_check` 由调用方注入（目标仓不同，迁移图不同）——
    判据在 `bg_coordinator/migrations.py`，与提交闸**同一处口径**。
    """
    new = copy.deepcopy(state)
    ts = now_iso()
    task = new.tasks.get(task_id)
    if task is None:
        return _reject(new, ts, "request_merge", task_id, requester, Code.E_UNKNOWN_ID, "条目不存在")

    gates = check_gates(
        new, task, changed_files, base_commit, main_head, evidence_path_ok, migration_check
    )
    if not gates.passed:
        first = gates.first
        assert first is not None
        return _reject(
            new, ts, "request_merge", task_id, requester, first.code, first.message, first.hint
        )

    merge_id = f"M-{uuid.uuid4().hex[:10]}"
    new.merges[merge_id] = MergeRequest(
        merge_id=merge_id,
        task_id=task_id,
        branch=branch,
        commit=commit,
        requester=requester,
        base_commit=base_commit,
        state=MergeState.QUEUED,
        requested_at=clock,
        changed_files=list(changed_files),
    )
    new.seq += 1
    ev = Event(
        seq=new.seq,
        ts=ts,
        verb="request_merge",
        id=task_id,
        actor=requester,
        before={},
        after={"merge_state": "merge_queued"},
        result="ok",
        detail={"merge_id": merge_id, "branch": branch, "commit": commit},
    )
    return Result(ok=True, state=new, event=ev, detail={"merge_id": merge_id})


def dispatch_next(state: State) -> Result:
    """取队首并置为 merging。**串行闸在此**：已有 merging 则拒绝。"""
    new = copy.deepcopy(state)
    ts = now_iso()
    busy = in_flight_merge(new)
    if busy is not None:
        return _reject(
            new,
            ts,
            "merge_start",
            busy.task_id,
            "coordinator",
            Code.E_BUSY,
            f"已有合并在进行：{busy.merge_id}（{busy.task_id}）",
            "同一时刻只允许一次合并（防 git 索引竞态）",
        )
    q = queue(new)
    if not q:
        return _reject(new, ts, "merge_start", "", "coordinator", Code.E_BAD_STATE, "队列为空")
    head = q[0]
    head.state = MergeState.MERGING
    new.seq += 1
    ev = Event(
        seq=new.seq,
        ts=ts,
        verb="merge_start",
        id=head.task_id,
        actor="coordinator",
        before={"merge_state": "merge_queued"},
        after={"merge_state": "merging"},
        result="ok",
        detail={"merge_id": head.merge_id},
    )
    return Result(ok=True, state=new, event=ev, detail={"merge_id": head.merge_id})


def complete_merge(state: State, merge_id: str, result_commit: str) -> Result:
    """合并成功——记录结果并置终态。**由执行方在真正合完之后回调。**"""
    new = copy.deepcopy(state)
    ts = now_iso()
    m = new.merges.get(merge_id)
    if m is None:
        return _reject(new, ts, "merge_ok", "", "coordinator", Code.E_UNKNOWN_ID, "合并项不存在")
    before = {"merge_state": str(m.state)}
    m.state = MergeState.MERGED
    m.result_commit = result_commit
    task = new.tasks.get(m.task_id)
    if task is not None:
        task.ver += 1
    new.seq += 1
    ev = Event(
        seq=new.seq,
        ts=ts,
        verb="merge_ok",
        id=m.task_id,
        actor="coordinator",
        before=before,
        after={"merge_state": "merged", "result_commit": result_commit},
        result="ok",
        detail={"merge_id": merge_id},
    )
    return Result(ok=True, state=new, event=ev)


def reject_conflict(state: State, merge_id: str, detail: str = "") -> Result:
    """内容冲突——**拒绝并移出队列 ＋ 生成动作项给 TL**（方案 v2 §8.2 M04）。

    协调器**不猜哪里该取哪边**。自动解冲突＝开始"理解代码"＝无人能审计的黑箱。
    """
    new = copy.deepcopy(state)
    ts = now_iso()
    m = new.merges.get(merge_id)
    if m is None:
        return _reject(new, ts, "merge_conflict", "", "coordinator", Code.E_UNKNOWN_ID, "合并项不存在")
    m.state = MergeState.REJECTED
    m.rejection = detail or "内容冲突"
    action = {
        "kind": "resolve_conflict",
        "merge_id": merge_id,
        "task_id": m.task_id,
        "branch": m.branch,
        "assignee_role": "tech-lead",
        "note": "在 worktree 内解冲突后重新入队；禁自动取侧",
        "detail": detail,
    }
    new.seq += 1
    ev = Event(
        seq=new.seq,
        ts=ts,
        verb="merge_conflict",
        id=m.task_id,
        actor="coordinator",
        before={"merge_state": "merging"},
        after={"merge_state": "rejected"},
        result=f"rejected:{Code.E_CONTENT_CONFLICT}",
        detail={"merge_id": merge_id, "action_item": action},
    )
    return Result(
        ok=False,
        state=new,
        event=ev,
        rejection=Rejection(
            Code.E_CONTENT_CONFLICT,
            f"{m.task_id} 内容冲突：{detail}",
            id=m.task_id,
            owner="tech-lead",
            hint="冲突是语义问题——转 TL 在 worktree 解，禁自动取侧",
        ),
        detail={"action_item": action},
    )


def queue_status(state: State) -> dict[str, object]:
    """队列可观测面——**不可观测的队列就是一条新的隐性依赖**（方案 v2 §6.4）。"""
    q = queue(state)
    busy = in_flight_merge(state)
    return {
        "queued": [
            {"merge_id": m.merge_id, "task": m.task_id, "branch": m.branch, "requester": m.requester}
            for m in q
        ],
        "in_flight": busy.merge_id if busy else None,
        "merged": sum(1 for m in state.merges.values() if m.state == MergeState.MERGED),
        "rejected": sum(1 for m in state.merges.values() if m.state == MergeState.REJECTED),
    }


def _reject(
    new: State,
    ts: str,
    verb: str,
    task_id: str,
    actor: str,
    code: Code,
    msg: str,
    hint: str = "",
) -> Result:
    new.seq += 1
    ev = Event(
        seq=new.seq,
        ts=ts,
        verb=verb,
        id=task_id,
        actor=actor,
        before={},
        after={},
        result=f"rejected:{code}",
        detail={"message": msg, "hint": hint},
    )
    return Result(
        ok=False, state=new, event=ev, rejection=Rejection(code, msg, id=task_id, owner=actor, hint=hint)
    )


def evidence_path_readable(path: str) -> bool:
    return rule_8b_evidence_readable(path) is None


def repo_paths_changed(repo: Path, base: str, head: str) -> list[str]:
    """实际改动文件列表——闸 2 的输入。"""
    import subprocess

    out = subprocess.run(  # noqa: S603
        ["git", "diff", "--name-only", f"{base}..{head}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        return []
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
