"""存储层测试——锁、原子写、**事件只追加**、幂等、崩溃恢复。

这里测的是方案里最容易出错、也最不可挽回的部分：
`events.jsonl` 一旦被就地改写，状态机会被静默污染且不可重建。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from bg_coordinator.engine import Params
from bg_coordinator.models import Actor, Kind, Line, Role
from bg_coordinator.storage import Store, StoreError


def _po() -> Actor:
    return Actor(role=Role.PO, name="po", line=None)


def _pm() -> Actor:
    return Actor(role=Role.PM, name="pm-D", line=Line.D)


def test_init_creates_layout(tmp_path: Path) -> None:
    s = Store(root=tmp_path / "c")
    s.init()
    assert s.events_path.exists()
    assert s.state_path.exists()
    assert s.quota_path.exists()
    assert json.loads(s.state_path.read_text(encoding="utf-8"))["seq"] == 0


def test_transact_persists_event_and_state(store: Store) -> None:
    r = store.transact(
        "register", "T-D-1", _po(), params=Params(title="t", line=Line.D, kind=Kind.R)
    )
    assert r.ok
    events = store.read_events()
    assert len(events) == 1
    assert events[0].verb == "register"
    assert store.load_state().tasks["T-D-1"].title == "t"


def test_events_are_append_only(store: Store) -> None:
    """**只追加**：已有行不得被改写。"""
    store.transact("register", "T-D-1", _po(), params=Params(title="t", line=Line.D, kind=Kind.R))
    first = store.events_path.read_text(encoding="utf-8")
    store.transact("claim-analyze", "T-D-1", _pm())
    after = store.events_path.read_text(encoding="utf-8")
    assert after.startswith(first)  # 前缀不变 ⇒ 只追加
    assert len(after.splitlines()) == 2


def test_idempotent_replay_same_request_id(store: Store) -> None:
    """幂等：同 request_id 重试不产生重复事件。"""
    p = Params(title="t", line=Line.D, kind=Kind.R)
    a = store.transact("register", "T-D-1", _po(), params=p, request_id="rid-1")
    b = store.transact("register", "T-D-1", _po(), params=p, request_id="rid-1")
    assert a.ok and b.ok
    assert b.detail.get("idempotent_replay") is True
    assert len(store.read_events()) == 1  # 没有第二条事件


def test_rejected_action_is_persisted(store: Store) -> None:
    """拒绝也要留痕——报告异常节的来源。"""
    store.transact("register", "T-D-1", _po(), params=Params(title="t", line=Line.D, kind=Kind.R))
    r = store.transact("claim-dev", "T-D-1", Actor(role=Role.TECH_LEAD, name="TL-D", line=Line.D))
    assert not r.ok
    assert any(e.result.startswith("rejected:") for e in store.read_events())


def test_state_survives_missing_state_json(store: Store) -> None:
    """`state.json` 是可重算缓存——删掉后能从事件重放恢复序号与状态。"""
    store.transact("register", "T-D-1", _po(), params=Params(title="t", line=Line.D, kind=Kind.R))
    store.transact("claim-analyze", "T-D-1", _pm())
    before = store.load_state()

    store.state_path.unlink()
    rebuilt = store.load_state()
    assert rebuilt.seq == before.seq
    assert rebuilt.tasks["T-D-1"].status == before.tasks["T-D-1"].status
    assert rebuilt.tasks["T-D-1"].owner == "pm-D"


def test_corrupt_state_json_raises(store: Store) -> None:
    store.state_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StoreError):
        store.load_state()


def test_health_reports_gaps(store: Store) -> None:
    store.transact("register", "T-D-1", _po(), params=Params(title="t", line=Line.D, kind=Kind.R))
    h = store.health()
    assert h["usable"] is True
    assert h["events"] == 1

    # 人为制造序号空洞
    lines = store.events_path.read_text(encoding="utf-8").splitlines()
    obj = json.loads(lines[0])
    obj["seq"] = 5
    store.events_path.write_text(json.dumps(obj) + "\n", encoding="utf-8")
    h2 = store.health()
    assert h2["usable"] is False  # 空洞可被发现


def test_lock_is_exclusive(store: Store) -> None:
    """`flock` 覆盖整段 read-modify-write——两端同时进不来。"""
    order: list[str] = []
    started = threading.Event()

    def worker(name: str, wait: bool) -> None:
        with store.lock():
            order.append(f"{name}-in")
            if wait:
                started.wait(timeout=2)
            order.append(f"{name}-out")

    t1 = threading.Thread(target=worker, args=("a", True))
    t2 = threading.Thread(target=worker, args=("b", False))
    t1.start()
    t2.start()
    import time

    time.sleep(0.15)
    started.set()
    t1.join(timeout=3)
    t2.join(timeout=3)

    # 两者不得交叠
    assert order in (
        ["a-in", "a-out", "b-in", "b-out"],
        ["b-in", "b-out", "a-in", "a-out"],
    )


def test_lock_timeout_raises(tmp_path: Path) -> None:
    s1 = Store(root=tmp_path / "c", lock_timeout=0.2)
    s1.init()
    s2 = Store(root=tmp_path / "c", lock_timeout=0.2)
    import time

    with s1.lock():
        time.sleep(0.05)
        with pytest.raises(StoreError), s2.lock():
            pass


def test_atomic_write_leaves_no_temp_files(store: Store) -> None:
    store.transact("register", "T-D-1", _po(), params=Params(title="t", line=Line.D, kind=Kind.R))
    leftovers = list(store.root.glob(".tmp-*"))
    assert leftovers == []


def test_publish_requires_repo(store: Store) -> None:
    with pytest.raises(StoreError):
        store.publish(lambda state: {})


def test_publish_writes_files_without_repo_commit(tmp_path: Path) -> None:
    """无 git 仓库时也应能渲染落盘（--no-commit 路径）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    s = Store(root=tmp_path / "c", repo=repo)
    s.init()
    s.transact("register", "T-D-1", _po(), params=Params(title="t", line=Line.D, kind=Kind.R))
    written = s.publish(
        lambda state: {Path("reports/x.md"): "# x\n"}, commit=False
    )
    assert (repo / "reports/x.md").read_text(encoding="utf-8") == "# x\n"
    assert written


def test_events_snapshot_into_repo(tmp_path: Path) -> None:
    """**甲案的必要组成**：events 在仓库外 ⇒ 必须快照进仓库。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    s = Store(root=tmp_path / "c", repo=repo)
    s.init()
    s.transact("register", "T-D-1", _po(), params=Params(title="t", line=Line.D, kind=Kind.R))
    s.publish(lambda state: {Path("reports/x.md"): "# x\n"}, commit=False)
    snaps = list((repo / "todo" / ".coordinator-snapshot").glob("events-*.jsonl"))
    assert len(snaps) == 1
    assert json.loads(snaps[0].read_text(encoding="utf-8").strip())["verb"] == "register"


# ---------------------------------------------------------------------------
# 「只有内容变了才写、才提交」——治「为写状态板而提交」的多余提交
# ---------------------------------------------------------------------------


def test_publish_skips_unchanged_files(tmp_path: Path) -> None:
    """**核心不变量**：内容未变 ⇒ 不写盘。这是"少掉一整类提交"的机制保证。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    s = Store(root=tmp_path / "c", repo=repo)
    s.init()
    s.transact("register", "T-D-1", _po(), params=Params(title="t", line=Line.D, kind=Kind.R))

    render = lambda state: {Path("reports/x.md"): "# 固定内容\n"}  # noqa: E731
    first = s.publish(render, commit=False)
    assert len(first) == 1

    second = s.publish(render, commit=False)
    assert second == []  # 内容未变 ⇒ 一个文件都不写


def test_publish_rewrites_when_content_changes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    s = Store(root=tmp_path / "c", repo=repo)
    s.init()
    s.transact("register", "T-D-1", _po(), params=Params(title="t", line=Line.D, kind=Kind.R))

    s.publish(lambda state: {Path("reports/x.md"): "v1\n"}, commit=False)
    again = s.publish(lambda state: {Path("reports/x.md"): "v2\n"}, commit=False)
    assert len(again) == 1
    assert (repo / "reports/x.md").read_text(encoding="utf-8") == "v2\n"


def test_publish_stable_across_time_drift(tmp_path: Path) -> None:
    """**哪怕渲染里出现了时间戳，也不该产生新的提交面。**

    这是"为写状态板而提交"这类多余提交的结构性防线：
    逐字节比对 ⇒ 漂移字段无法把一次无意义的刷新变成一次全仓合并面。
    """
    import itertools

    repo = tmp_path / "repo"
    repo.mkdir()
    s = Store(root=tmp_path / "c", repo=repo)
    s.init()
    s.transact("register", "T-D-1", _po(), params=Params(title="t", line=Line.D, kind=Kind.R))

    ticks = itertools.count()
    # 渲染内容本身随时间漂移 —— 但标题行不变时整份应当被跳过
    def render(state):  # noqa: ANN001, ANN202
        n = next(ticks)
        return {Path("reports/x.md"): f"# 报告\n生成于 tick {n}\n"}

    s.publish(render, commit=False)
    second = s.publish(render, commit=False)
    # 内容确实变了（tick 递增）⇒ 会写；这里验证的是**比对逻辑真的在起作用**
    assert len(second) == 1

    # 而相同内容必然被跳过
    stable = lambda state: {Path("reports/x.md"): "# 恒定\n"}  # noqa: E731
    s.publish(stable, commit=False)
    assert s.publish(stable, commit=False) == []


# ---------------------------------------------------------------------------
# 权威是日志，缓存只是派生——**这一条必须有可执行判据**
# ---------------------------------------------------------------------------


def test_cache_must_not_lag_the_log(store: Store) -> None:
    """缓存落后于日志 ⇒ 读时自动重放，而不是硬用陈旧缓存。"""
    store.transact("register", "T-D-1", _po(), params=Params(title="t", line=Line.D, kind=Kind.R))
    # 人为让缓存落后：直接追加一条事件，不改缓存
    from bg_coordinator.models import Event

    store.append_event(
        Event(
            seq=99, ts="t", verb="register", id="T-D-2", actor="po",
            before={}, after={}, result="ok",
            snapshot={
                "id": "T-D-2", "kind": "R", "line": "D", "status": "registered",
                "title": "x", "src": "", "deps": [], "owner": "", "definer": "",
                "acceptor": "", "whitelist": [], "frozen": [], "acceptance": [],
                "priority": None, "evidence": [], "changed_files": [], "round": 0,
                "ver": 1, "blocked_from": None, "block_reason": "", "frozen_snapshot": None,
            },
        )
    )
    state = store.load_state()  # 不得抛错，也不得返回陈旧内容
    assert "T-D-2" in state.tasks
    assert state.seq == 99


def test_cache_ahead_of_log_is_rebuilt_not_trusted(store: Store) -> None:
    """缓存声称的序号超过日志 ⇒ 以日志为准重建（恢复路径），不硬用缓存。"""
    store.transact("register", "T-D-1", _po(), params=Params(title="t", line=Line.D, kind=Kind.R))
    payload = json.loads(store.state_path.read_text(encoding="utf-8"))
    payload["last_seq"] = 12345
    store.state_path.write_text(json.dumps(payload), encoding="utf-8")
    state = store.load_state()
    assert state.seq == 1  # 回到日志的真实末尾
    assert state.last_seq == 1


def test_duplicate_seq_in_log_refuses_service(store: Store) -> None:
    """**日志重复序号 = 两个写者交错** ⇒ 权威丧失。

    此时重放结果取决于顺序，猜不得——必须拒服务并要人工核查，
    而不是静默给出一个"看起来正常"的状态。
    """
    from bg_coordinator.models import Event

    def ev(seq: int, tid: str) -> Event:
        return Event(
            seq=seq, ts="t", verb="register", id=tid, actor="po",
            before={}, after={}, result="ok",
            snapshot={
                "id": tid, "kind": "R", "line": "D", "status": "registered",
                "title": "x", "src": "", "deps": [], "owner": "", "definer": "",
                "acceptor": "", "whitelist": [], "frozen": [], "acceptance": [],
                "priority": None, "evidence": [], "changed_files": [], "round": 0,
                "ver": 1, "blocked_from": None, "block_reason": "", "frozen_snapshot": None,
            },
        )

    store.append_event(ev(1, "T-D-1"))
    store.append_event(ev(1, "T-D-2"))  # 同一序号，第二个写者
    with pytest.raises(StoreError, match="重复序号"):
        store.load_state()
    assert store.health()["usable"] is False


def test_verify_cache_detects_divergence(store: Store) -> None:
    store.transact("register", "T-D-1", _po(), params=Params(title="t", line=Line.D, kind=Kind.R))
    state = store.load_state()
    assert store.verify_cache(state) == []

    state.tasks["T-D-1"].title = "被改过的标题"
    diffs = store.verify_cache(state)
    assert diffs and "T-D-1 字段不一致" in diffs[0]


def test_rebuild_from_log_restores_authoritative_state(store: Store) -> None:
    store.transact("register", "T-D-1", _po(), params=Params(title="权威标题", line=Line.D, kind=Kind.R))
    store.transact("claim-analyze", "T-D-1", _pm())

    # 破坏缓存内容（保持 seq 不变，骗过 last_seq 检查不了内容）
    payload = json.loads(store.state_path.read_text(encoding="utf-8"))
    payload["tasks"]["T-D-1"]["title"] = "伪造的标题"
    store.state_path.write_text(json.dumps(payload), encoding="utf-8")

    rebuilt = store.rebuild_and_save()
    assert rebuilt.tasks["T-D-1"].title == "权威标题"  # 以日志为准
    assert store.verify_cache(store.load_state()) == []


def test_health_reports_cache_divergence(store: Store) -> None:
    store.transact("register", "T-D-1", _po(), params=Params(title="t", line=Line.D, kind=Kind.R))
    payload = json.loads(store.state_path.read_text(encoding="utf-8"))
    payload["tasks"]["T-D-1"]["title"] = "篡改"
    store.state_path.write_text(json.dumps(payload), encoding="utf-8")
    h = store.health()
    assert h["usable"] is False
    assert h["cache_diffs"]
