"""把两条**文字规则**变成闸：闭环路径验收（2026-10-01）与累计派单上限（2026-10-04）。

用户口径：「规则性的，最好用工具来解决，才能够真正量化，单纯靠文字，约束力太小了。」
这两条原先都只有文字：一条是 `COORDINATION.md §五` 的散文，一条是 `SUBAGENT.md §二`
写着"累计"而工具算的是"在手"——**文字与工具不一致本身就是欠账**。
"""

from __future__ import annotations

import time
from pathlib import Path

from conftest import budgeted, closure_item  # noqa: E402

from bg_coordinator.audit import audit
from bg_coordinator.engine import State, apply, grant_dispatch
from bg_coordinator.errors import Code
from bg_coordinator.models import (
    AcceptanceItem,
    AcceptanceType,
    Actor,
    Kind,
    LeaseState,
    Line,
    Priority,
    Role,
    TaskState,
)
from bg_coordinator.readiness import Quota
from bg_coordinator.storage import Store
from bg_coordinator.validators import NO_CLOSURE, rule_closure_path, validate_acceptance


def _pm() -> Actor:
    return Actor(role=Role.PM, name="pm-D", line=Line.D)


def _tl() -> Actor:
    return Actor(role=Role.TECH_LEAD, name="TL-D", line=Line.D)


def _task(state: TaskState = TaskState.VERIFIED, **kw):
    from bg_coordinator.models import Task

    base = {
        "id": "T-D-1",
        "kind": Kind.T,
        "line": "D",
        "status": state,
        "acceptance": [AcceptanceItem(type=AcceptanceType.TEST, cmd="pytest -q", desc="绿")],
        "definer": "pm-D",
    }
    base.update(kw)
    return Task(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ① 闭环路径：口头规则 → 可核判据
# ---------------------------------------------------------------------------


def test_functional_task_without_closure_path_is_rejected() -> None:
    """**只交模块单测 ⇒ 不能验收**——这是这条闸的全部理由。"""
    rej = rule_closure_path(_task())
    assert rej is not None and rej.code == Code.E_NO_CLOSURE_PATH
    assert "闭环" in rej.message
    assert "closure:" in rej.hint, "拒绝必须给出可执行的补法"


def test_closure_path_lets_it_through() -> None:
    assert rule_closure_path(_task(acceptance=[closure_item()])) is None


def test_non_task_kind_is_not_gated() -> None:
    """需求（R）还没到验收内容那一步——闸只管功能条目（T）。"""
    assert rule_closure_path(_task(kind=Kind.R)) is None


def test_declared_exemption_is_honoured_and_still_visible() -> None:
    """纯文档／口径条目可显式豁免——但**豁免要能被数出来**，不是消失。"""
    t = _task(constraints=[NO_CLOSURE])
    assert rule_closure_path(t) is None

    state = budgeted(State())
    state.tasks["T-D-1"] = t
    codes = [(a.code, a.id) for a in audit(state)]
    assert (Code.OK, "闭环路径豁免") in codes, "豁免没被点名"


def test_closure_item_without_description_is_rejected() -> None:
    """闭环项必须写成「入口动作 → 可观测反应」——空描述等于没写。"""
    rej = validate_acceptance(_task(acceptance=[AcceptanceItem(type=AcceptanceType.CLOSURE)]))
    assert rej is not None and rej.code == Code.E_UNMET
    assert "闭环路径缺描述" in rej.message


def test_audit_reports_stock_without_closure_path() -> None:
    """闸拦"走到验收"的；**审计**管存量——还没走到验收的也要露出来。"""
    state = budgeted(State())
    state.tasks["T-D-1"] = _task(TaskState.DEFINED)
    state.tasks["T-D-2"] = _task(TaskState.DEFINED, id="T-D-2", acceptance=[closure_item()])
    hits = [a for a in audit(state) if a.code == Code.E_NO_CLOSURE_PATH]
    assert [a.id for a in hits] == ["T-D-1"], "存量缺口没报（或多报了）"


def test_accept_verb_blocks_without_closure_path(tmp_path: Path) -> None:
    """端到端：`accept` 那一刻被拦（闸在实质验收处，不在 define 处）。"""
    from bg_coordinator.models import Evidence, Task

    s = budgeted(State())
    s.tasks["T-D-1"] = Task(
        id="T-D-1", kind=Kind.T, line="D", status=TaskState.VERIFIED, priority=Priority.P1,
        acceptance=[AcceptanceItem(type=AcceptanceType.TEST, cmd="pytest -q", desc="绿")],
        definer="pm-D",
        evidence=[Evidence(
            round=1, commit="abc", gate_cmd="pytest -q", gate_exit=0,
            evidence_path=str(tmp_path),
        )],
    )
    r = apply(s, "accept", "T-D-1", _pm())
    assert not r.ok and r.rejection.code == Code.E_NO_CLOSURE_PATH

    s.tasks["T-D-1"].acceptance = [*s.tasks["T-D-1"].acceptance, closure_item()]
    assert apply(s, "accept", "T-D-1", _pm()).ok


# ---------------------------------------------------------------------------
# ② 累计派单上限：文字说累计、工具算在办 —— 现已一致
# ---------------------------------------------------------------------------


def _dispatch(state: State, holder: str, task: str):
    return grant_dispatch(
        state, holder=holder, task_id=task, line="D", clock=time.time(), ttl=60.0
    )


def test_dispatch_limit_counts_cumulative_not_in_hand() -> None:
    """**销账不重置**：开一个、销一个、再开 ⇒ 累计仍然在涨，到顶就拒。"""
    state = State()
    state.quota = Quota(enabled=True, subagent_max_per_dispatcher=2)
    r1 = _dispatch(state, "tech-lead:TL", "T-D-1")
    assert r1.ok, r1.rejection
    r2 = _dispatch(r1.state, "tech-lead:TL", "T-D-2")
    assert r2.ok, r2.rejection

    # 把两张都销账（在手归零），再派第三张 —— 旧口径会放行，累计口径必须拒
    s = r2.state
    for ls in s.leases.values():
        ls.state = LeaseState.RELEASED
    r3 = _dispatch(s, "tech-lead:TL", "T-D-3")
    assert not r3.ok
    assert r3.rejection.code == Code.E_UNAUTHORIZED_DISPATCH
    assert "累计" in r3.rejection.message and "销账不重置" in r3.rejection.message


def test_dispatch_tally_is_per_dispatcher() -> None:
    """台账按派单方分开——A 用完额度不影响 B。"""
    state = State()
    state.quota = Quota(enabled=True, subagent_max_per_dispatcher=1)
    r1 = _dispatch(state, "tech-lead:TL", "T-D-1")
    assert r1.ok
    assert not _dispatch(r1.state, "tech-lead:TL", "T-D-2").ok
    assert _dispatch(r1.state, "dev:dev-D", "T-D-3").ok


def test_dispatch_tally_survives_replay(tmp_path: Path) -> None:
    """累计台账**进事件面**：重放可还原，缓存落后也不丢（配额踩过同一个坑）。"""
    store = Store(root=tmp_path / "coord", repo=None)
    store.init()
    with store.lock():
        state = store.load_state()
        state.quota = Quota(enabled=True, subagent_max_per_dispatcher=5)
        r = _dispatch(state, "tech-lead:TL", "T-D-1")
        assert r.ok, r.rejection
        store.append_event(r.event)
        store.save_state(r.state)

    rebuilt = store.replay()
    assert rebuilt.dispatch_tally == {"tech-lead:TL": 1}
    assert store.verify_cache(rebuilt) == []


def test_audit_flags_accepted_but_never_merged() -> None:
    """**漏洞回归**：验收了却没合并 ⇒ 用户以为做完了，代码其实不在主干。"""
    from bg_coordinator.audit import audit
    from bg_coordinator.engine import State
    from bg_coordinator.errors import Code
    from bg_coordinator.models import MergeRequest, MergeState, Task, TaskState

    s = budgeted(State())
    s.tasks["T-D-1"] = Task(id="T-D-1", kind=Kind.T, line="D", status=TaskState.ACCEPTED,
                            owner="TL-D")
    hits = [a for a in audit(s) if a.code == Code.E_ACCEPTED_UNMERGED]
    assert [a.id for a in hits] == ["T-D-1"], "验收未合入没被点名"

    # 有合入记录即销
    s.merges["M-1"] = MergeRequest(merge_id="M-1", task_id="T-D-1", branch="dev/x",
                                   commit="abc", requester="tech-lead:TL-D:D",
                                   state=MergeState.MERGED)
    assert not [a for a in audit(s) if a.code == Code.E_ACCEPTED_UNMERGED]
