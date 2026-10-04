"""资源租约测试——方案 §5.3。

**租约与任务所有权语义相反**：任务所有权必须显式释放（否则活没人干），
租约必须能 TTL 自动回收（否则进程一崩就永久泄漏）。
这两条不许混用同一套语义。
"""

from __future__ import annotations

import os

from bg_coordinator.engine import (
    State,
    acquire_lease,
    mark_reclaimed,
    pid_alive,
    reclaimable_leases,
    release_lease,
)
from bg_coordinator.errors import Code
from bg_coordinator.models import LeaseState
from bg_coordinator.readiness import Quota


def _acquire(state: State, holder: str = "w1", pid: int | None = None, clock: float = 1000.0, **kw):
    return acquire_lease(
        state,
        klass=kw.pop("klass", "bg_db"),
        holder=holder,
        db_name=kw.pop("db_name", f"bg_{holder}"),
        pid=pid if pid is not None else os.getpid(),
        ttl=kw.pop("ttl", 1800.0),
        clock=clock,
        **kw,
    )


def test_acquire_and_release_roundtrip() -> None:
    r = _acquire(State())
    assert r.ok
    lease_id = r.detail["lease_id"]
    assert r.state.leases[lease_id].state == LeaseState.ACTIVE

    rel = release_lease(r.state, lease_id, "w1")
    assert rel.ok
    assert rel.state.leases[lease_id].state == LeaseState.RELEASED


def test_release_by_other_holder_is_rejected() -> None:
    r = _acquire(State())
    lease_id = r.detail["lease_id"]
    bad = release_lease(r.state, lease_id, "w2")
    assert not bad.ok
    assert bad.rejection is not None and bad.rejection.code == Code.E_NOT_HOLDER


def test_dead_holder_becomes_reclaimable() -> None:
    """活性判定复用库名内嵌 pid——**不需要心跳**。"""
    r = _acquire(State(), pid=999999999)  # 几乎不可能存在的 pid
    assert r.ok
    reclaimable = reclaimable_leases(r.state, clock=1000.0)
    assert len(reclaimable) == 1
    assert reclaimable[0].db_name == "bg_w1"


def test_live_holder_is_not_reclaimable() -> None:
    r = _acquire(State(), pid=os.getpid())
    assert r.ok
    assert reclaimable_leases(r.state, clock=1000.0) == []


def test_ttl_expiry_marks_reclaimable_even_if_pid_alive() -> None:
    """pid 存活但 TTL 过期 ⇒ 可回收（TTL 是 pid 复用的兜底）。"""
    r = _acquire(State(), pid=os.getpid(), ttl=10.0, clock=1000.0)
    assert r.ok
    assert reclaimable_leases(r.state, clock=2000.0) != []


def test_quota_full_is_rejected_when_enabled() -> None:
    s = State(quota=Quota(enabled=True, bg_db_max=2))
    a = _acquire(s, "w1", db_name="d1")
    b = _acquire(a.state, "w2", db_name="d2")
    c = _acquire(b.state, "w3", db_name="d3")
    assert c.ok is False
    assert c.rejection is not None and c.rejection.code == Code.E_QUOTA_FULL


def test_quota_disabled_only_observes() -> None:
    """标定期：只观测不放行（方案 §5.5）。"""
    s = State(quota=Quota(enabled=False, bg_db_max=1))
    a = _acquire(s, "w1", db_name="d1")
    b = _acquire(a.state, "w2", db_name="d2")
    assert b.ok  # 未启用配额 ⇒ 不拒


def test_quota_sweeps_dead_lease_before_rejecting() -> None:
    """**即时回收是关键**——否则枯竭只是换个地方发生。"""
    s = State(quota=Quota(enabled=True, bg_db_max=2))
    a = _acquire(s, "dead", pid=999999999, db_name="d1")
    b = _acquire(a.state, "w2", db_name="d2")
    assert b.ok
    # 第三笔：配额 2 满，但其中一笔属主已死 ⇒ 应先回收再放行
    c = _acquire(b.state, "w3", db_name="d3")
    assert c.ok, f"应先回收死租约再放行，实际被拒：{c.rejection}"
    states = {ls.db_name: ls.state for ls in c.state.leases.values()}
    assert states["d1"] == LeaseState.RECLAIMABLE


def test_reclaim_success_closes_lease() -> None:
    r = _acquire(State())
    lease_id = r.detail["lease_id"]
    done = mark_reclaimed(r.state, lease_id)
    assert done.ok
    assert done.state.leases[lease_id].state == LeaseState.RECLAIMED


def test_reclaim_mdl_failure_goes_to_tracking() -> None:
    """MDL 占用 ⇒ 进 `reclaim_tracking`——**持锁会话不关，谁都删不掉**（方案 §5.1 第③层）。"""
    r = _acquire(State())
    lease_id = r.detail["lease_id"]
    bad = mark_reclaimed(r.state, lease_id, mdl_failed=True)
    assert bad.ok is False
    assert bad.rejection is not None and bad.rejection.code == Code.E_MDL
    assert bad.state.leases[lease_id].state == LeaseState.RECLAIM_TRACKING


def test_pid_alive_for_current_process() -> None:
    assert pid_alive(os.getpid()) is True
    assert pid_alive(999999999) is False
    assert pid_alive(0) is False


def test_protected_names_are_never_reclaimed() -> None:
    """生产／开发库永不回收——判据单源在 validators。"""
    from bg_coordinator.validators import PROTECTED_DB_NAMES

    assert "broker_gateway" in PROTECTED_DB_NAMES
    assert "broker_gateway_dev" in PROTECTED_DB_NAMES


# ---------------------------------------------------------------------------
# 等待队列：满则**排队**，不是硬拒；释放后**按序补位**
# ---------------------------------------------------------------------------


def _enable(s: State, n: int) -> State:
    from bg_coordinator.readiness import Quota

    s.quota = Quota(enabled=True, bg_db_max=n)
    return s


def test_quota_full_enqueues_with_position() -> None:
    """满 ⇒ 排队并回报队位——**让申请者知道等多久，而不是盲目重试**。"""
    s = _enable(State(), 1)
    a = _acquire(s, "w1", db_name="d1")
    b = _acquire(a.state, "w2", db_name="d2")
    assert b.ok is False
    assert b.rejection is not None and b.rejection.code == Code.E_QUOTA_FULL
    assert b.detail.get("queued") is True
    assert b.detail.get("position") == 1
    assert len(b.state.waiters) == 1


def test_repeat_acquire_keeps_position() -> None:
    """已在队里 ⇒ **保持原位**，不重复入队。"""
    s = _enable(State(), 1)
    a = _acquire(s, "w1", db_name="d1")
    b = _acquire(a.state, "w2", db_name="d2")
    c = _acquire(b.state, "w2", db_name="d2")
    assert c.detail.get("position") == 1
    assert len(c.state.waiters) == 1  # 没有变成两条


def test_release_advances_queue_in_order() -> None:
    """**分水岭**：释放后空位由协调器按序补位，不靠申请者反复试探。"""
    s = _enable(State(), 2)
    a = _acquire(s, "w1", db_name="d1")
    b = _acquire(a.state, "w2", db_name="d2")
    c = _acquire(b.state, "w3", db_name="d3")
    d = _acquire(c.state, "w4", db_name="d4")
    assert [w["db_name"] for w in d.state.waiters] == ["d3", "d4"]

    lease_w1 = next(
        ls.lease_id for ls in d.state.leases.values() if ls.holder == "w1"
    )
    rel = release_lease(d.state, lease_w1, "w1", clock=1000.0)
    assert rel.ok

    active = [ls.db_name for ls in rel.state.leases.values() if ls.state == LeaseState.ACTIVE]
    assert "d3" in active  # 队首补位
    assert "d1" not in active
    assert [w["db_name"] for w in rel.state.waiters] == ["d4"]  # 队列前进一格


def test_advance_stops_when_still_full() -> None:
    """空位不足 ⇒ 只补到配额为止，其余保持排队。"""
    s = _enable(State(), 2)
    a = _acquire(s, "w1", db_name="d1")
    b = _acquire(a.state, "w2", db_name="d2")
    c = _acquire(b.state, "w3", db_name="d3")
    d = _acquire(c.state, "w4", db_name="d4")

    # 释放一个 ⇒ 补一个，还剩一个排队
    lease_w1 = next(ls.lease_id for ls in d.state.leases.values() if ls.holder == "w1")
    rel = release_lease(d.state, lease_w1, "w1", clock=1000.0)
    active = [ls for ls in rel.state.leases.values() if ls.state == LeaseState.ACTIVE]
    assert len(active) == 2  # 补满即止，不超配额
    assert len(rel.state.waiters) == 1


def test_expired_waiter_is_dropped() -> None:
    """**等待意图也会过期**——申请者死了，位置不能永久占着。"""
    from bg_coordinator.engine import _drop_expired_waiters

    s = _enable(State(), 1)
    a = _acquire(s, "w1", db_name="d1", ttl=10.0, clock=1000.0)
    b = _acquire(a.state, "w2", db_name="d2", ttl=10.0, clock=1000.0)
    assert len(b.state.waiters) == 1

    dropped = _drop_expired_waiters(b.state, clock=1000.0 + 400.0)
    assert dropped == ["bg_db:w2"]
    assert b.state.waiters == []


def test_queue_positions_are_visible() -> None:
    """**不可观测的队列就是一条新的隐性依赖**。"""
    from bg_coordinator.engine import queue_positions

    s = _enable(State(), 1)
    a = _acquire(s, "w1", db_name="d1")
    b = _acquire(a.state, "w2", db_name="d2")
    c = _acquire(b.state, "w3", db_name="d3")
    rows = queue_positions(c.state)
    assert [r["position"] for r in rows] == [1, 2]
    assert [r["db_name"] for r in rows] == ["d2", "d3"]
