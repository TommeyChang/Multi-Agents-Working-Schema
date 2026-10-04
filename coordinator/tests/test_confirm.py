"""**commander 发需求必须得到用户显式确认**——闸的回归。

为什么要有它（而不是只写在文档里）：commander 是**用户接口**，同时又是**需求的
形式化者**——没有独立留痕时，"用户要的"与"commander 认为用户要的"在状态里
长得一模一样，事后分不开。所以这条规矩落成一条可核记录：

    内容绑定（digest）· 一次性（used_by）· 会过期（expires_at）· 必带用户原话（said）

控制例成对出现：**该拦的必拦、不该拦的必不拦**（非 commander 的来源、
非需求类条目、`raise` 那条链一律不受影响）。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from conftest import budgeted  # noqa: E402

from bg_coordinator.audit import audit
from bg_coordinator.engine import (
    CONFIRM_TTL_SECONDS,
    Params,
    State,
    apply,
    grant_confirm,
    live_confirmations,
    need_digest,
)
from bg_coordinator.errors import Code
from bg_coordinator.models import Actor, Kind, Line, Origin, Role
from bg_coordinator.storage import Store


def _cmdr() -> Actor:
    return Actor(role=Role.COMMANDER, name="cmdr", line=None)


def _po() -> Actor:
    return Actor(role=Role.PO, name="po", line=None)


def _register(state: State, actor: Actor, **kw):
    params = Params(
        title=kw.pop("title", "接入 X"),
        line=kw.pop("line", Line.D),
        kind=kw.pop("kind", Kind.R),
        origin=kw.pop("origin", Origin.COMMANDER),
        **kw,
    )
    return apply(state, "register", "", actor, params)


def _confirm(state: State, *, title: str = "接入 X", line: str = "D", said: str = "把 X 接进来",
             ttl: float = CONFIRM_TTL_SECONDS, by: str = "commander:cmdr"):
    return grant_confirm(
        state, line=line, title=title, by=by, said=said, clock=time.time(), ttl=ttl
    )


# ---------------------------------------------------------------------------
# 该拦的
# ---------------------------------------------------------------------------


def test_commander_requirement_without_confirmation_is_rejected() -> None:
    """没有用户确认 ⇒ 拒，并**给出下一步**（不让 commander 猜该找谁）。"""
    res = _register(State(), _cmdr())
    assert not res.ok
    assert res.rejection.code == Code.E_NO_USER_CONFIRM
    assert "显式确认" in res.rejection.message
    assert "coord confirm" in res.rejection.hint, "拒绝必须带可执行的下一步"


def test_concrete_confirmation_lets_it_through_and_leaves_trace() -> None:
    """确认在手 ⇒ 放行，且**确认人与用户原话落在条目上**（可核、可读）。"""
    state = State()
    conf = _confirm(state)
    assert conf.ok, conf.rejection
    state = conf.state

    res = _register(state, _cmdr())
    assert res.ok, res.rejection
    task = res.state.tasks["R-D-1"]
    assert task.confirmed_by == "commander:cmdr"
    assert task.user_said == "把 X 接进来"
    # 确认被消费：同一条确认不能再发第二条需求
    used = res.state.confirmations[conf.detail["id"]]
    assert used.used_by == "R-D-1"
    assert not used.live(time.time())


def test_confirmation_is_single_use() -> None:
    """**用完即销**：一条确认只够发一条需求（防"确认一次、发十条"）。"""
    state = _confirm(State()).state
    first = _register(state, _cmdr())
    assert first.ok, first.rejection
    second = _register(first.state, _cmdr())
    assert not second.ok
    assert second.rejection.code == Code.E_NO_USER_CONFIRM


def test_confirmation_is_content_bound() -> None:
    """**内容绑定**：确认了「接入 X」不能拿去发「接入 Y」。"""
    state = _confirm(State(), title="接入 X").state
    res = _register(state, _cmdr(), title="接入 Y")
    assert not res.ok
    assert res.rejection.code == Code.E_NO_USER_CONFIRM
    assert "内容与本条不符" in res.rejection.message, res.rejection.message


def test_expired_confirmation_does_not_count() -> None:
    """**过期的"是"不算数**：TTL 到点即作废（负 TTL = 立刻过期，便于确定性复现）。"""
    state = _confirm(State(), ttl=-1.0).state
    assert live_confirmations(state, now=time.time()) == []
    res = _register(state, _cmdr())
    assert not res.ok and res.rejection.code == Code.E_NO_USER_CONFIRM


def test_commander_actor_is_gated_even_without_origin() -> None:
    """**防漏**：actor 是 commander 而 `--origin` 空着，也归 `commander 发需求`。"""
    res = _register(State(), _cmdr(), origin=None)
    assert not res.ok and res.rejection.code == Code.E_NO_USER_CONFIRM


def test_confirm_without_user_words_is_rejected() -> None:
    """**没有原话就没有证据面** ⇒ 拒绝（空话术不算确认）。"""
    res = _confirm(State(), said="   ")
    assert not res.ok and res.rejection.code == Code.E_NO_USER_SAID


def test_duplicate_confirmation_is_rejected() -> None:
    """同一条内容已有在手确认 ⇒ 拒（不然"哪条被消费了"就说不清）。"""
    state = _confirm(State()).state
    again = _confirm(state)
    assert not again.ok and again.rejection.code == Code.E_CONFIRM_INVALID


# ---------------------------------------------------------------------------
# 不该拦的
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("origin", [Origin.PM, Origin.LINE, Origin.DBA, Origin.OPS])
def test_other_origins_are_not_gated(origin: Origin) -> None:
    """闸只管 commander 的需求——其它来源（开发中发现／DBA／OPS／PM）照旧登记。"""
    res = _register(State(), _po(), origin=origin)
    assert res.ok, res.rejection


def test_task_kind_is_not_gated() -> None:
    """需求（R）才要用户点头；任务（T）不受此闸。"""
    res = _register(State(), _cmdr(), kind=Kind.T)
    assert res.ok, res.rejection


def test_raise_chain_is_unaffected() -> None:
    """`raise`（TL 评估后登记）不走这条闸——它自己那一套来源与转化规则没变。"""
    from bg_coordinator.engine import format_id

    state = State()
    res = apply(
        state,
        "raise",
        "",
        Actor(role=Role.TECH_LEAD, name="TL-D", line=Line.D),
        Params(
            title="跨线的事", src="发现", scope="cross_line", category="cross_line",
            line=Line.D, kind=Kind.R,
        ),
    )
    assert res.ok, res.rejection
    assert res.state.tasks[format_id("R-D", 1)].confirmed_by == ""


# ---------------------------------------------------------------------------
# 事件面：确认要能被重建（配额踩过的坑）
# ---------------------------------------------------------------------------


def test_confirmation_survives_replay(tmp_path: Path) -> None:
    """**重建后确认还在**，且"已消费"不会被还原成"在手"。

    这条钉的是本体系踩过的坑：配额当年只进缓存不进事件流，重建即丢，
    于是"设了配额"是假的。用户确认是证据面，比配额更不能丢。
    """
    store = Store(root=tmp_path / "coord", repo=None)
    store.init()
    with store.lock():
        state = store.load_state()
        conf = _confirm(state)
        store.append_event(conf.event)
        store.save_state(conf.state)
        reg = _register(conf.state, _cmdr())
        store.append_event(reg.event)
        store.save_state(reg.state)

    rebuilt = store.replay()
    assert conf.detail["id"] in rebuilt.confirmations, "重放把用户确认弄丢了"
    assert rebuilt.confirmations[conf.detail["id"]].used_by == reg.detail["id"]
    assert rebuilt.tasks["R-D-1"].user_said == "把 X 接进来"
    assert store.verify_cache(rebuilt) == []
    assert rebuilt.confirm_seq == 1, "流水号必须推回原值——否则重建会重号"


def test_verify_cache_catches_missing_confirmation(tmp_path: Path) -> None:
    """缓存里少了确认 ⇒ 校验必须报出来（缓存不是权威投影）。"""
    store = Store(root=tmp_path / "coord", repo=None)
    store.init()
    with store.lock():
        state = store.load_state()
        conf = _confirm(state)
        store.append_event(conf.event)
        store.save_state(conf.state)
    cached = store.load_state(strict=False)
    cached.confirmations = {}
    diffs = store.verify_cache(cached)
    assert any("用户确认" in d for d in diffs), diffs


# ---------------------------------------------------------------------------
# 审计：闸管"新发的"，审计管"存量的"
# ---------------------------------------------------------------------------


def test_audit_reports_requirement_without_confirmation() -> None:
    """存量 commander 需求没有确认留痕 ⇒ 审计报 `E_NO_USER_CONFIRM`。

    闸管的是"新发的"；闸上线之前登记的、以及以别的 origin 绕进来的存量，
    只能靠审计看见——这正是这条检查存在的理由。
    """
    from bg_coordinator.models import Task, TaskState

    state = budgeted(State())
    state.tasks["R-D-9"] = Task(
        id="R-D-9", kind=Kind.R, line="D", status=TaskState.REGISTERED,
        title="历史需求", origin=Origin.COMMANDER.value,
    )
    assert Code.E_NO_USER_CONFIRM in [a.code for a in audit(state)]

    # 补上留痕即消警
    state.tasks["R-D-9"].confirmed_by = "commander:cmdr"
    state.tasks["R-D-9"].user_said = "用户说过"
    assert Code.E_NO_USER_CONFIRM not in [a.code for a in audit(state)]

    # 非 commander 来源的需求不受这条审计管
    state.tasks["R-D-9"].origin = Origin.PM.value
    state.tasks["R-D-9"].confirmed_by = ""
    assert Code.E_NO_USER_CONFIRM not in [a.code for a in audit(state)]


def test_digest_binds_line_and_title() -> None:
    """指纹必须同时含线别与标题：换个线发同名的需求，不是同一条。"""
    assert need_digest("D", "接入 X") != need_digest("A", "接入 X")
    assert need_digest("D", "接入  X") == need_digest("D", "接入 X"), "空白归一化"
