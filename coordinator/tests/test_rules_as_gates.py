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
    from bg_coordinator.models import Evidence, MergeRequest, MergeState, Task, TaskState

    s = budgeted(State())
    s.tasks["T-D-1"] = Task(
        id="T-D-1", kind=Kind.T, line="D", status=TaskState.ACCEPTED, owner="TL-D",
        evidence=[Evidence(round=1, commit="abc123", gate_cmd="pytest -q", gate_exit=0,
                           evidence_path="x.log")],
    )
    hits = [a for a in audit(s) if a.code == Code.E_ACCEPTED_UNMERGED]
    assert [a.id for a in hits] == ["T-D-1"], "验收未合入没被点名"

    # **不假红**：复核/口径类条目（没有 commit ⇒ 没有可合入的东西）不该被点
    s.tasks["T-D-2"] = Task(
        id="T-D-2", kind=Kind.T, line="D", status=TaskState.ACCEPTED, owner="TL-D",
        evidence=[Evidence(round=1, commit="none", gate_cmd="probe", gate_exit=0,
                           evidence_path="x.log")],
    )
    got = [a.id for a in audit(s) if a.code == Code.E_ACCEPTED_UNMERGED]
    assert got == ["T-D-1"], f"无代码交付物的条目被误点（假红）：{got}"

    # 有合入记录即销
    s.merges["M-1"] = MergeRequest(merge_id="M-1", task_id="T-D-1", branch="dev/x",
                                   commit="abc", requester="tech-lead:TL-D:D",
                                   state=MergeState.MERGED)
    assert not [a for a in audit(s) if a.code == Code.E_ACCEPTED_UNMERGED]


def test_terminal_task_holding_grant_is_flagged() -> None:
    """终态条目不得持有在手凭证（2026-10-06 用户定：收窄版——只点终态）。"""
    import time as _time

    from bg_coordinator.audit import audit
    from bg_coordinator.engine import State, grant_dispatch
    from bg_coordinator.errors import Code
    from bg_coordinator.models import Kind, Task, TaskState

    s = budgeted(State())
    s.tasks["T-D-1"] = Task(id="T-D-1", kind=Kind.T, line="D", status=TaskState.ACCEPTED,
                            owner="TL-D")
    r = grant_dispatch(s, holder="tech-lead:TL-D:D", task_id="T-D-1", line="D",
                       clock=_time.time(), ttl=3600)
    assert r.ok, r.rejection
    hits = [a for a in audit(r.state) if a.code == Code.E_GRANT_AFTER_DONE]
    assert hits and hits[0].id == r.state.leases[next(iter(r.state.leases))].lease_id

    # 收窄的另一半：非终态（在办）持证 ⇒ 不点
    r.state.tasks["T-D-1"].status = TaskState.IN_PROGRESS
    assert not [a for a in audit(r.state) if a.code == Code.E_GRANT_AFTER_DONE]


def test_complete_requires_note_and_released_grants() -> None:
    """自主闭环的最小判据（2026-10-06 用户定）：裸收口不算闭环。"""
    import time as _time

    from bg_coordinator.engine import State, apply, grant_dispatch
    from bg_coordinator.errors import Code
    from bg_coordinator.models import Actor, Kind, Line, Role, Task, TaskState

    def _ops() -> Actor:
        return Actor(role=Role.OPS, name="ops", line=Line.OPS)

    s = budgeted(State())
    s.tasks["OPS-1"] = Task(id="OPS-1", kind=Kind.T, line="OPS",
                            status=TaskState.IN_PROGRESS, owner="ops")
    # ① 没 note ⇒ 拒
    r = apply(s, "complete", "OPS-1", _ops())
    assert not r.ok and r.rejection.code == Code.E_INCOMPLETE and "note" in r.rejection.message

    # ② 有 note 但凭证在手 ⇒ 拒（先销账）
    g = grant_dispatch(s, holder="ops:ops", task_id="OPS-1", line="OPS",
                       clock=_time.time(), ttl=3600)
    assert g.ok, g.rejection
    from bg_coordinator.engine import Params as _P

    r2 = apply(g.state, "complete", "OPS-1", _ops(), params=_P(reason="已执行留痕"))
    assert not r2.ok and r2.rejection.code == Code.E_GRANT_NOT_RELEASED
    assert "release" in r2.rejection.hint

    # ③ note 齐 ＋ 凭证已释放 ⇒ 过
    from bg_coordinator.engine import release_lease

    lid = next(iter(g.state.leases))
    g2 = release_lease(g.state, lid, "ops:ops", clock=_time.time())
    assert g2.ok, g2.rejection
    r3 = apply(g2.state, "complete", "OPS-1", _ops(), params=_P(reason="已执行留痕"))
    assert r3.ok, r3.rejection


def test_idle_grant_is_flagged_but_fresh_or_terminal_is_not() -> None:
    """空转嫌疑（2026-10-06 用户定）：凭证在手超 2h、任务仍在办 ⇒ 点名排查。"""
    import time as _time

    from bg_coordinator.audit import IDLE_GRANT_SECONDS, audit
    from bg_coordinator.engine import State, grant_dispatch
    from bg_coordinator.errors import Code
    from bg_coordinator.models import Kind, Task, TaskState

    now = _time.time()
    s = budgeted(State())
    s.tasks["T-D-1"] = Task(id="T-D-1", kind=Kind.T, line="D",
                            status=TaskState.IN_PROGRESS, owner="TL-D")
    g = grant_dispatch(s, holder="tech-lead:TL-D:D", task_id="T-D-1", line="D",
                       clock=now - IDLE_GRANT_SECONDS - 60, ttl=86400)
    assert g.ok, g.rejection
    hits = [a for a in audit(g.state, clock=now) if a.code == Code.E_SUBAGENT_IDLE]
    assert hits, "超时未进展没被点名"

    # 刚领的 ⇒ 不点
    assert not [a for a in audit(g.state, clock=now - IDLE_GRANT_SECONDS + 60)
                if a.code == Code.E_SUBAGENT_IDLE]
    # 终态持证由 E_GRANT_AFTER_DONE 管，空转不重复点
    g.state.tasks["T-D-1"].status = TaskState.ACCEPTED
    assert not [a for a in audit(g.state, clock=now) if a.code == Code.E_SUBAGENT_IDLE]


def test_migration_entry_requires_dba_req_at_define() -> None:
    """**DBA 先行**（2026-10-06 用户定）：含迁移件的条目，定稿时必须挂 DBA 迁移要求。"""
    from bg_coordinator.engine import Params, State, apply
    from bg_coordinator.errors import Code
    from bg_coordinator.models import AcceptanceItem, AcceptanceType, Actor, Kind, Line, Role, Task
    from bg_coordinator.validators import rule_migration_req

    def _pm() -> Actor:
        return Actor(role=Role.PM, name="pm-D", line=Line.D)

    # 判据本体
    t = Task(id="T-D-1", kind=Kind.T, line="D")
    assert rule_migration_req(t, needs=True) is not None
    assert rule_migration_req(t, needs=False) is None
    t.migration_req = "加索引 idx_x（锁评估：online）"
    assert rule_migration_req(t, needs=True) is None

    # define 路径（真动词）
    s = budgeted(State())
    from bg_coordinator.models import TaskState as _TS

    s.tasks["R-D-1"] = Task(id="R-D-1", kind=Kind.R, line="D", owner="pm-D",
                            status=_TS.ANALYZING)
    p = Params(whitelist=["alembic/versions/**"], frozen=[],
               acceptance=[AcceptanceItem(type=AcceptanceType.CLOSURE, desc="入口 → 可观测")],
               needs_migration_req=True)
    r = apply(s, "define", "R-D-1", _pm(), params=p)
    assert not r.ok and r.rejection.code == Code.E_INCOMPLETE
    assert "DBA 迁移要求" in r.rejection.message

    p.migration_req = "加索引 idx_x（锁评估：online）"
    r2 = apply(r.state, "define", "R-D-1", _pm(), params=p)
    assert r2.ok, r2.rejection
    assert r2.state.tasks["R-D-1"].migration_req.startswith("加索引")


def test_test_tier_slot_caps_full_and_domain() -> None:
    """测试档（2026-10-06 用户定）：full 同时只能一个、domain 两个——满则排队。"""
    import time as _time

    from bg_coordinator.engine import State, acquire_lease
    from bg_coordinator.readiness import Quota

    now = _time.time()
    s = State()
    s.quota = Quota(enabled=True)
    r1 = acquire_lease(s, klass="test-full", holder="a", db_name="full", pid=1,
                       ttl=600, clock=now)
    assert r1.ok, r1.rejection
    # 第二张 full ⇒ 满（上限 1）⇒ 排队
    r2 = acquire_lease(r1.state, klass="test-full", holder="b", db_name="full",
                       pid=2, ttl=600, clock=now)
    assert not r2.ok and "排队" in (r2.rejection.message + str(r2.detail))

    d1 = acquire_lease(r1.state, klass="test-domain", holder="a", db_name="domain",
                       pid=1, ttl=600, clock=now)
    d2 = acquire_lease(d1.state, klass="test-domain", holder="b", db_name="domain",
                       pid=2, ttl=600, clock=now)
    assert d1.ok and d2.ok
    d3 = acquire_lease(d2.state, klass="test-domain", holder="c", db_name="domain",
                       pid=3, ttl=600, clock=now)
    assert not d3.ok, "第三张 domain 没排队（上限 2 失效）"


def test_closure_cmd_must_pass_at_accept(tmp_path) -> None:
    """带 cmd 的闭环必须可执行（TDD 兜底）：验收时真跑，退出码不符 ⇒ 拒。"""
    from bg_coordinator.engine import Params, State, apply
    from bg_coordinator.errors import Code
    from bg_coordinator.models import (
        AcceptanceItem, AcceptanceType, Actor, Evidence, Kind, Line, Role, Task, TaskState,
    )

    def _pm() -> Actor:
        return Actor(role=Role.PM, name="pm-D", line=Line.D)

    def _mk(cmd: str) -> State:
        s = budgeted(State())
        s.tasks["T-D-1"] = Task(
            id="T-D-1", kind=Kind.T, line="D", status=TaskState.VERIFIED, definer="pm-D",
            acceptance=[AcceptanceItem(type=AcceptanceType.CLOSURE, desc="入口 → 可观测", cmd=cmd)],
            evidence=[Evidence(round=1, commit="abc", gate_cmd="pytest -q", gate_exit=0,
                               evidence_path=str(tmp_path / "g.log"))],
        )
        (tmp_path / "g.log").write_text("green\n", encoding="utf-8")
        return s

    # 闭环命令失败 ⇒ 拒
    r = apply(_mk("exit 1"), "accept", "T-D-1", _pm(), params=Params(repo_path=str(tmp_path)))
    assert not r.ok and r.rejection.code == Code.E_UNMET and "闭环断言未通过" in r.rejection.message

    # 闭环命令通过 ⇒ 过
    r2 = apply(_mk("exit 0"), "accept", "T-D-1", _pm(), params=Params(repo_path=str(tmp_path)))
    assert r2.ok, r2.rejection

    # 不给仓 ⇒ 不跑（判不了不当罪名）
    r3 = apply(_mk("exit 1"), "accept", "T-D-1", _pm(), params=Params())
    assert r3.ok, r3.rejection
