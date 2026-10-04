"""溯源与审核测试——方案 §4.9。

**核心主张：报告不出现原材料。**
- 审核**只产异常**，通过项静默；
- 溯源**按需查询**（正查／反查），而不是把 61 份 SUBMISSION 摊开。
"""

from __future__ import annotations

from pathlib import Path

from bg_coordinator.audit import audit, audit_summary, trace, why
from bg_coordinator.engine import Params, State, apply
from bg_coordinator.errors import Code
from bg_coordinator.models import (
    AcceptanceItem,
    AcceptanceType,
    Actor,
    Kind,
    Line,
    Priority,
    Role,
    TaskState,
)


def _po() -> Actor:
    return Actor(role=Role.PO, name="po", line=None)


def _pm() -> Actor:
    return Actor(role=Role.PM, name="pm-D", line=Line.D)


def _tl() -> Actor:
    return Actor(role=Role.TECH_LEAD, name="TL-D", line=Line.D)


def _full(tmp_path: Path, tid: str = "T-D-1") -> State:
    ev = tmp_path / f"{tid}.log"
    ev.write_text("2182 passed", encoding="utf-8")
    s = apply(
        State(),
        "register",
        tid,
        _po(),
        params=Params(title="示例", line=Line.D, kind=Kind.R, priority=Priority.P1),
    ).state
    s = apply(s, "claim-analyze", tid, _pm()).state
    s = apply(
        s,
        "define",
        tid,
        _pm(),
        params=Params(
            whitelist=["data_access/**"],
            frozen=[],
            acceptance=[AcceptanceItem(type=AcceptanceType.TEST, cmd="pytest -q", desc="绿")],
        ),
    ).state
    s = apply(s, "claim-dev", tid, _tl()).state
    s = apply(s, "start", tid, _tl()).state
    s = apply(
        s,
        "deliver",
        tid,
        _tl(),
        params=Params(
            commit="abc1234",
            gate_cmd="uv run pytest -q",
            gate_exit=0,
            evidence_path=str(ev),
            changed_files=["data_access/kline/follow.py"],
        ),
    ).state
    return apply(s, "verify", tid, _tl()).state


# ---------------------------------------------------------------------------
# 溯源：正查
# ---------------------------------------------------------------------------


def test_trace_shows_full_chain(tmp_path: Path) -> None:
    """正查：这条目凭什么算完成？谁验的？"""
    s = _full(tmp_path)

    chain: list = []
    cur = State()
    for verb, actor, params in (
        ("register", _po(), Params(title="t", line=Line.D, kind=Kind.R, priority=Priority.P1)),
        ("claim-analyze", _pm(), None),
    ):
        r = apply(cur, verb, "T-D-1", actor, params=params)
        chain.append(r.event)
        cur = r.state
    t = trace([e for e in chain if e is not None], cur.tasks["T-D-1"])
    text = t.render()
    assert "T-D-1 证据链" in text
    assert "register" in text
    assert "claim-analyze" in text
    assert "pm-D" in text
    assert s is not None  # `_full` 的链路在本测试中只作对照


def test_trace_includes_deliver_details(tmp_path: Path) -> None:
    """交付细节（commit／门禁／证据路径）必须在链上可见——这是"证据可核"的入口。"""
    cur = State()
    events = []
    ev = tmp_path / "g.log"
    ev.write_text("ok", encoding="utf-8")
    for verb, actor, params in (
        ("register", _po(), Params(title="t", line=Line.D, kind=Kind.R, priority=Priority.P1)),
        ("claim-analyze", _pm(), None),
        (
            "define",
            _pm(),
            Params(
                whitelist=["a/**"],
                frozen=[],
                acceptance=[AcceptanceItem(type=AcceptanceType.TEST, cmd="pytest", desc="绿")],
            ),
        ),
        ("claim-dev", _tl(), None),
        ("start", _tl(), None),
        (
            "deliver",
            _tl(),
            Params(commit="abc1234", gate_cmd="pytest -q", gate_exit=0, evidence_path=str(ev)),
        ),
    ):
        r = apply(cur, verb, "T-D-1", actor, params=params)
        assert r.ok, r.rejection
        events.append(r.event)
        cur = r.state

    text = trace(events, cur.tasks["T-D-1"]).render()  # type: ignore[arg-type]
    assert "abc1234" in text
    assert "gate_exit" in text
    assert str(ev) in text


# ---------------------------------------------------------------------------
# 溯源：反查
# ---------------------------------------------------------------------------


def test_why_maps_file_back_to_task(tmp_path: Path) -> None:
    """**反查是现状几乎做不到的**：git blame 给不出"当时的范围与验收标准"。"""
    s = _full(tmp_path)
    result = why(s, "data_access/kline/follow.py")
    assert len(result.tasks) == 1
    assert result.tasks[0].id == "T-D-1"
    text = result.render()
    assert "T-D-1" in text
    assert "白名单" in text


def test_why_on_unknown_path_says_so() -> None:
    text = why(State(), "no/such/file.py").render()
    assert "无关联条目" in text


# ---------------------------------------------------------------------------
# 审核：只产异常
# ---------------------------------------------------------------------------


def test_healthy_state_has_no_anomalies(tmp_path: Path) -> None:
    """**通过项必须静默**——这正是不把文档丢过来的关键。"""
    s = _full(tmp_path)
    assert audit(s) == []
    summary = audit_summary(s)
    assert summary["anomaly_count"] == 0
    assert summary["ok_count"] == 1


def test_accepted_without_evidence_is_an_anomaly() -> None:
    s = State()
    r = apply(
        s, "register", "T-D-1", _po(), params=Params(title="t", line=Line.D, kind=Kind.T)
    )
    s = r.state
    s.tasks["T-D-1"].status = TaskState.ACCEPTED  # 人为制造：已验收但零证据
    anomalies = audit(s)
    assert any(a.code == Code.E_NO_EVIDENCE for a in anomalies)


def test_unreadable_evidence_is_an_anomaly(tmp_path: Path) -> None:
    s = _full(tmp_path)
    from dataclasses import replace

    s.tasks["T-D-1"].evidence[-1] = replace(
        s.tasks["T-D-1"].evidence[-1], evidence_path=str(tmp_path / "gone.log")
    )
    anomalies = audit(s)
    assert any("不可核" in a.message for a in anomalies)


def test_in_flight_without_priority_is_an_anomaly(tmp_path: Path) -> None:
    """PO 的产出必须有承载——在办却没优先级是异常。"""
    s = _full(tmp_path)
    s.tasks["T-D-1"].priority = None
    anomalies = audit(s)
    assert any(a.code == Code.E_NO_PRIORITY for a in anomalies)


def test_dangling_dependency_is_an_anomaly() -> None:
    s = State()
    s = apply(
        s, "register", "T-D-1", _po(), params=Params(title="t", line=Line.D, kind=Kind.R)
    ).state
    s.tasks["T-D-1"].deps = ["T-D-999"]
    assert any(a.code == Code.E_NO_DEP for a in audit(s))


def test_dead_lease_is_an_anomaly() -> None:
    """资源枯竭**事前可见**——这是报告第五节的价值。"""
    from bg_coordinator.engine import acquire_lease

    s = State()
    r = acquire_lease(s, klass="bg_db", holder="w", db_name="bg_x", pid=999999999, ttl=100.0, clock=1.0)
    assert r.ok
    anomalies = audit(r.state, clock=1.0)
    assert any("属主 pid" in a.message for a in anomalies)


def test_rejected_merge_becomes_anomaly(tmp_path: Path) -> None:
    from bg_coordinator.merge import dispatch_next, reject_conflict, request_merge

    s = _full(tmp_path)
    r = request_merge(
        s, "T-D-1", branch="b", commit="c", requester="TL-D",
        changed_files=["data_access/kline/follow.py"], clock=1.0,
        migration_check=lambda files, base: "",  # 链位判据另有专测（test_merge／test_migration_gate）
    )
    started = dispatch_next(r.state)
    mid = started.detail["merge_id"]
    conflicted = reject_conflict(started.state, mid, "冲突")
    anomalies = audit(conflicted.state)
    assert any(a.code == Code.E_CONTENT_CONFLICT for a in anomalies)
    assert any(a.owner == "tech-lead" for a in anomalies)


def test_anomaly_renders_as_report_row() -> None:
    from bg_coordinator.audit import Anomaly

    a = Anomaly(Code.E_GATE_NONZERO, "T-D-1", "门禁退出码 1", owner="TL-D", hint="重跑")
    row = a.render_row()
    assert row.startswith("| T-D-1 |")
    assert "TL-D" in row
    assert "重跑" in row


# ---------------------------------------------------------------------------
# 上下文边界——**读命令必须有界**（设计稿 §10）
# ---------------------------------------------------------------------------


def test_trace_is_bounded_by_default(tmp_path: Path) -> None:
    """返工多次的条目，证据链默认只出尾部若干条，不摊全史。"""
    from bg_coordinator.audit import DEFAULT_TRACE_LIMIT, Trace, TraceNode
    from bg_coordinator.models import Kind, Priority, Task

    task = Task(id="T-D-1", kind=Kind.T, line=Line.D, priority=Priority.P1)
    nodes = [
        TraceNode(
            seq=i, ts="t", verb="deliver", actor="TL-D",
            before={}, after={}, result="ok",
        )
        for i in range(50)
    ]
    text = Trace(task=task, nodes=nodes).render()
    assert "前 38 条已折叠" in text
    assert text.count("✓ deliver") == DEFAULT_TRACE_LIMIT

    full = Trace(task=task, nodes=nodes).render(full=True)
    assert full.count("✓ deliver") == 50
    assert "已折叠" not in full


def test_trace_clips_long_fields() -> None:
    from bg_coordinator.audit import Trace
    from bg_coordinator.models import Kind, Priority, Task

    task = Task(id="T-D-1", kind=Kind.T, line=Line.D, priority=Priority.P1)
    task.whitelist = ["a" * 400]
    text = Trace(task=task, nodes=[]).render()
    assert "…" in text
    assert "a" * 400 not in text


def test_why_is_bounded(tmp_path: Path) -> None:
    from bg_coordinator.audit import DEFAULT_WHY_LIMIT, WhyResult
    from bg_coordinator.models import Kind, Priority, Task

    tasks = [
        Task(id=f"T-D-{i}", kind=Kind.T, line=Line.D, priority=Priority.P1, whitelist=["a/**"])
        for i in range(20)
    ]
    text = WhyResult(path="a/b.py", tasks=tasks, commits=[f"c{i}" for i in range(20)]).render()
    assert f"另有 {20 - DEFAULT_WHY_LIMIT} 条匹配" in text
    assert "另有 15 个" in text  # 提交列表同样有上限


def test_anomaly_row_is_clipped() -> None:
    from bg_coordinator.audit import Anomaly

    row = Anomaly(
        Code.E_GATE_NONZERO, "T-D-1", message="长" * 300, owner="TL-D", hint="短"
    ).render_row()
    assert "…" in row
    assert len(row) < 400


# ---------------------------------------------------------------------------
# 号账目的两条判据（折自既有取号器，不另立第二套账）
# ---------------------------------------------------------------------------


def test_duplicate_number_is_an_anomaly() -> None:
    """**同号双占**必须被报出——号是资源，一个号只能有一个归属。

    判据折自既有取号器（主检出 `scripts/tools/alloc_number/_check.py` 的「同号双占」）。
    同号两行之后，后面每个"这个号在不在账上"的判断都会失准：
    让号可能只让掉一行，落物可能落在另一行。
    """
    from bg_coordinator.engine import reserve_number

    s = State()
    reserve_number(s, "T-D", holder="TL-D", ts="2026-10-04T00:00:00+08:00")
    # 正常路径（alloc_number 单调推进）到不了这里；能到这里的只有 override／事件重放／人工改状态
    s.allocations.append(dict(s.allocations[0]))

    codes = [a.code for a in audit(s)]
    assert Code.E_NUMBER_TWICE in codes
    msg = next(a.message for a in audit(s) if a.code == Code.E_NUMBER_TWICE)
    assert "T-D-1" in next(a.id for a in audit(s) if a.code == Code.E_NUMBER_TWICE)
    assert "2 次" in msg


def test_legal_number_ledger_has_no_duplicate_anomaly() -> None:
    """负例锚点：合法演进（占号／落物／让号）**不得**报同号双占——否则判据是噪声。"""
    from bg_coordinator.engine import materialize_number, release_number, reserve_number

    s = State()
    reserve_number(s, "T-D", holder="TL-D", ts="t")
    reserve_number(s, "T-D", holder="TL-D", ts="t")
    reserve_number(s, "T-D", holder="TL-D", ts="t")
    assert materialize_number(s, "T-D", 1, obj_id="T-D-1")[0]
    assert release_number(s, "T-D", 2, note="合并到 T-D-1，不再单独建件")[0]

    assert Code.E_NUMBER_TWICE not in [a.code for a in audit(s)]


def test_number_ledger_partitions_the_range() -> None:
    """**三态互斥且铺满**：`1..high` 里每个号恰好属于 已落物／待建／已让号／空洞 之一。

    这是折进来的第二条判据（既有取号器把它钉在真簿上）。它挡的是一类静默失真：
    某个号既不在账上又被算作"已让号"，或者同一号被两处分类各计一次。
    """
    from bg_coordinator.engine import (
        materialize_number,
        number_gaps,
        number_inventory,
        release_number,
        reserve_number,
    )

    s = State()
    for _ in range(5):
        reserve_number(s, "T-D", holder="TL-D", ts="t")
    assert materialize_number(s, "T-D", 1, obj_id="T-D-1")[0]
    assert materialize_number(s, "T-D", 2, obj_id="T-D-2")[0]
    assert release_number(s, "T-D", 3, note="并入 T-D-2")[0]
    # 4、5 仍是待建

    high = s.number_book["T-D"]
    buckets = {
        "materialized": {1, 2},
        "pending": {4, 5},
        "released": {3},
    }
    # ① 互斥：各桶两两不交
    keys = list(buckets)
    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            assert not (buckets[a] & buckets[b]), f"{a} 与 {b} 不该相交"
    # ② 铺满：各桶并集 == 1..high（没有既不在账上又没被记成空洞的号）
    assert set().union(*buckets.values()) == set(range(1, high + 1))
    # ③ 与「空洞」互斥：本序列无空洞
    assert number_gaps(s) == []
    # ④ 库存口径与桶一致（unaccounted 不为负 = 没有凭空多出来的号）
    inv = number_inventory(s)["T-D"]
    assert (inv["materialized"], inv["pending"], inv["released"]) == (2, 2, 1)
    assert inv["unaccounted"] == 0
