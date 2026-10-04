"""CLI 测试——**唯一写入口**的端到端行为。

契约：
- 成功 → 退出码 0，**一行摘要**（通过即静默）；
- 失败 → 退出码 1，**一条带责任人的异常**（stderr）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bg_coordinator.cli import main


def _register(
    root: str,
    capsys: pytest.CaptureFixture[str],
    *,
    line: str = "D",
    kind: str | None = None,
    **kw: str,
) -> str:
    """登记并返回**协调器分配的编号**——测试不再自己选号。

    编号由协调器发出，测试必须从返回值里取，这本身就是对「代理不选号」的验证。
    """
    argv = ["--json", "register", "--role", kw.pop("role", "po:po"), "--line", line]
    if kind:
        argv += ["--kind", kind]
    for k, v in kw.items():
        argv += [f"--{k.replace('_', '-')}", v]
    code = _run(root, *argv)
    captured = capsys.readouterr()
    assert code == 0, captured.err
    # 前序命令（如 init）的 stdout 也在同一缓冲区里 —— 只取**最后一行**这个 JSON
    last = [ln for ln in captured.out.splitlines() if ln.strip()][-1]
    payload = json.loads(last)
    return str(payload["detail"]["id"])


@pytest.fixture
def root(tmp_path: Path) -> str:
    return str(tmp_path / "coordinator")


def _run(root: str, *argv: str) -> int:
    return main(["--root", root, *argv])


def test_init_then_health(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(root, "init") == 0
    out = capsys.readouterr().out
    assert "events.jsonl" in out
    assert "只准追加" in out

    assert _run(root, "health") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["usable"] is True


def test_health_without_init_fails(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(root, "health") == 1
    assert "未初始化" in capsys.readouterr().err


def test_register_then_status(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """**编号由协调器分配**——调用方只声明线别与类型。"""
    _run(root, "init")
    capsys.readouterr()
    code = _run(
        root, "--json", "register", "--role", "po:po",
        "--title", "示例", "--line", "D", "--priority", "P1",
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["detail"]["id"].startswith("R-D-")

    assert _run(root, "status") == 0
    assert "条目 1" in capsys.readouterr().out


def test_rejection_goes_to_stderr_with_exit_1(
    root: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """失败即一条带责任人的异常——不是栈回溯。"""
    _run(root, "init")
    _register(root, capsys, role="po:po", line="D", title="t")
    code = _run(root, "claim-dev", "--id", "T-D-1", "--role", "tl:TL-D:D")
    assert code == 1
    err = capsys.readouterr().err
    assert "REJECTED" in err
    assert "E_" in err


def test_duplicate_register_is_rejected(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """重复提交由 **request_id 幂等**识别——编号由协调器发，撞号在结构上已不可能。"""
    _run(root, "init")
    capsys.readouterr()
    argv = (
        "--json", "register", "--role", "po:po", "--title", "t",
        "--line", "D", "--request-id", "dup-1",
    )
    assert _run(root, *argv) == 0
    first = json.loads(capsys.readouterr().out)["detail"]["id"]

    # 同一 request_id 重放 ⇒ 幂等：成功、不发新号
    assert _run(root, *argv) == 0
    capsys.readouterr()

    # 幂等重放不产生新事件：簿记只走了一格
    assert _run(root, "--json", "number") == 0
    assert json.loads(capsys.readouterr().out)["inventory"]["R-D"]["high"] == 1

    # 换个 request_id ⇒ 发新号
    assert _run(root, "--json", "register", "--role", "po:po", "--title", "t2", "--line", "D") == 0
    third = json.loads(capsys.readouterr().out)["detail"]["id"]
    assert third != first


def test_full_lifecycle_via_cli(root: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """端到端七步全走 CLI——证明协调器是**唯一写入口**。"""
    _run(root, "init")
    ev = tmp_path / "gate.log"
    ev.write_text("2182 passed", encoding="utf-8")

    tid = _register(root, capsys, title="示例", priority="P1", kind="T")
    steps: list[tuple[str, ...]] = [
        ("claim-analyze", "--id", tid, "--role", "pm:pm-D:D"),
        (
            "define", "--id", tid, "--role", "pm:pm-D:D",
            "--whitelist", "data_access/**", "--acceptance", "test:uv run pytest -q",
        ),
        ("claim-dev", "--id", tid, "--role", "tl:TL-D:D"),
        ("start", "--id", tid, "--role", "tl:TL-D:D"),
        (
            "deliver", "--id", tid, "--role", "tl:TL-D:D",
            "--commit", "abc1234", "--gate-cmd", "uv run pytest -q", "--gate-exit", "0",
            "--evidence", str(ev), "--changed", "data_access/kline/follow.py",
        ),
        ("verify", "--id", tid, "--role", "tl:TL-D:D"),
        ("accept", "--id", tid, "--role", "pm:pm-D:D"),
    ]
    for step in steps:
        code = _run(root, *step)
        assert code == 0, f"{step[0]} 失败：{capsys.readouterr().err}"
    capsys.readouterr()

    assert _run(root, "status") == 0
    assert "已验收 1" in capsys.readouterr().out


def test_trace_and_audit_and_report(root: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _run(root, "init")
    ev = tmp_path / "g.log"
    ev.write_text("ok", encoding="utf-8")
    tid = _register(root, capsys, title="t", kind="T", priority="P1")
    for step in (
        ("claim-analyze", "--id", tid, "--role", "pm:pm-D:D"),
        (
            "define", "--id", tid, "--role", "pm:pm-D:D",
            "--whitelist", "data_access/**", "--acceptance", "test:pytest",
        ),
        ("claim-dev", "--id", tid, "--role", "tl:TL-D:D"),
        ("start", "--id", tid, "--role", "tl:TL-D:D"),
        (
            "deliver", "--id", tid, "--role", "tl:TL-D:D",
            "--commit", "abc", "--gate-cmd", "pytest", "--gate-exit", "0",
            "--evidence", str(ev), "--changed", "data_access/x.py",
        ),
        ("verify", "--id", tid, "--role", "tl:TL-D:D"),
    ):
        assert _run(root, *step) == 0, capsys.readouterr().err
    capsys.readouterr()

    assert _run(root, "trace", tid) == 0
    assert "证据链" in capsys.readouterr().out

    assert _run(root, "why", "data_access/x.py") == 0
    assert tid in capsys.readouterr().out

    assert _run(root, "audit", "--summary") == 0
    assert "正常" in capsys.readouterr().out

    assert _run(root, "report") == 0
    assert "调度报告" in capsys.readouterr().out


def test_json_output_is_machine_readable(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    _run(root, "init")
    capsys.readouterr()
    code = _run(
        root, "--json", "register", "--role", "po:po", "--title", "t", "--line", "D"
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    # **编号由协调器分配**，并在返回里告知调用方
    assert payload["detail"]["id"].startswith("R-D-")


def test_request_id_is_idempotent(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """幂等：同 request_id 重放不产生新事件、**也不发新号**。"""
    _run(root, "init")
    capsys.readouterr()
    args = (
        "--json", "register", "--role", "po:po", "--title", "t", "--line", "D",
        "--request-id", "rid-9",
    )
    assert _run(root, *args) == 0
    first = json.loads(capsys.readouterr().out)["detail"]["id"]

    assert _run(root, *args) == 0  # 重放：成功且不报重复
    capsys.readouterr()

    # 只登记了一条；簿记只走了一格
    assert _run(root, "--json", "number") == 0
    payload = json.loads(capsys.readouterr().out)
    inv = payload["inventory"]["R-D"]
    assert inv["high"] == 1           # 幂等重放不发新号
    assert inv["materialized"] == 1   # 也不重复记账
    assert inv["unaccounted"] == 0
    assert first == "R-D-1"


def test_expect_ver_blocks_stale_write(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    _run(root, "init")
    tid = _register(root, capsys, role="po:po", line="D", title="t", priority="P1", kind="T")
    for step in (
        ("claim-analyze", "--id", tid, "--role", "pm:pm-D:D"),
        ("define", "--id", tid, "--role", "pm:pm-D:D", "--whitelist", "a/**",
         "--acceptance", "test:pytest"),
        ("claim-dev", "--id", tid, "--role", "tl:TL-D:D"),
    ):
        assert _run(root, *step) == 0, capsys.readouterr().err
    capsys.readouterr()
    # 基座已被上面的动作推进 ⇒ 陈旧的 expect-ver 必被拒
    code = _run(root, "start", "--id", tid, "--role", "tl:TL-D:D", "--expect-ver", "1")
    assert code == 1
    assert "E_CONFLICT" in capsys.readouterr().err


def test_lease_commands(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    import os

    _run(root, "init")
    capsys.readouterr()
    assert _run(
        root, "acquire", "--holder", "w1", "--db-name", "bg_w1", "--pid", str(os.getpid())
    ) == 0
    capsys.readouterr()
    assert _run(root, "leases") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["leases"][0]["db_name"] == "bg_w1"
    assert payload["quota_enabled"] is False


def test_merge_commands(root: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _run(root, "init")
    ev = tmp_path / "g.log"
    ev.write_text("ok", encoding="utf-8")
    tid = _register(root, capsys, title="t", kind="T", priority="P1")
    for step in (
        ("claim-analyze", "--id", tid, "--role", "pm:pm-D:D"),
        (
            "define", "--id", tid, "--role", "pm:pm-D:D",
            "--whitelist", "data_access/**", "--acceptance", "test:pytest",
        ),
        ("claim-dev", "--id", tid, "--role", "tl:TL-D:D"),
        ("start", "--id", tid, "--role", "tl:TL-D:D"),
        (
            "deliver", "--id", tid, "--role", "tl:TL-D:D",
            "--commit", "abc", "--gate-cmd", "pytest", "--gate-exit", "0",
            "--evidence", str(ev), "--changed", "data_access/x.py",
        ),
        ("verify", "--id", tid, "--role", "tl:TL-D:D"),
    ):
        assert _run(root, *step) == 0, capsys.readouterr().err
    capsys.readouterr()

    assert _run(
        root, "merge-request", "--id", tid, "--branch", "dev/TD1", "--commit", "abc",
        "--role", "tl:TL-D:D", "--changed", "data_access/x.py",
    ) == 0
    capsys.readouterr()

    assert _run(root, "merge-status") == 0
    status = json.loads(capsys.readouterr().out)
    assert len(status["queued"]) == 1

    assert _run(root, "merge-next") == 0
    capsys.readouterr()
    mid = status["queued"][0]["merge_id"]
    assert _run(root, "merge-ok", "--merge-id", mid, "--result-commit", "m1") == 0


def test_audit_cli_shows_only_anomalies(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    _run(root, "init")
    capsys.readouterr()
    assert _run(root, "audit") == 0
    assert "（无异常）" in capsys.readouterr().out


def test_priority_command(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    _run(root, "init")
    tid = _register(root, capsys, title="t")
    assert _run(root, "priority", "--id", tid, "--role", "po:po", "--priority", "P0") == 0
    capsys.readouterr()
    assert _run(root, "--json", "show", tid) == 0
    assert json.loads(capsys.readouterr().out)["priority"] == "P0"


def test_bad_verb_is_usage_error(root: str) -> None:
    with pytest.raises(SystemExit):
        main(["--root", root, "no-such-verb"])


# ---------------------------------------------------------------------------
# 取出（show / list）与多值标志——回归锁定
# ---------------------------------------------------------------------------


def _setup_defined(root: str, capsys: pytest.CaptureFixture[str]) -> str:
    """建一个已定稿的条目，**返回协调器分配的编号**。"""
    _run(root, "init")
    tid = _register(root, capsys, role="po:po", line="D", title="K线取数", priority="P1")
    _run(root, "claim-analyze", "--id", tid, "--role", "pm:pm-D:D")
    _run(
        root, "define", "--id", tid, "--role", "pm:pm-D:D",
        "--whitelist", "data_access/**", "--frozen", "main.py",
        "--acceptance", "test:uv run pytest -q",
    )
    capsys.readouterr()
    return tid


def test_show_prints_current_fields(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """`show` 给**当前快照**，与 `trace` 的**事件链**互补。"""
    tid = _setup_defined(root, capsys)
    assert _run(root, "show", tid) == 0
    out = capsys.readouterr().out
    assert tid in out
    assert "defined" in out
    assert "K线取数" in out
    assert "data_access/**" in out
    assert "可认领" in out  # 定稿后认领人已释放


def test_show_json_is_machine_readable(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    tid = _setup_defined(root, capsys)
    assert _run(root, "--json", "show", tid) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["id"] == tid
    assert payload["status"] == "defined"
    assert payload["owner"] == ""
    assert payload["whitelist"] == ["data_access/**"]


def test_show_unknown_id_fails(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    _run(root, "init")
    capsys.readouterr()
    assert _run(root, "show", "T-NOPE") == 1
    assert "未找到" in capsys.readouterr().err


def test_list_filters_and_is_bounded(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    tid = _setup_defined(root, capsys)
    _run(root, "register", "--role", "po:po", "--title", "E线件", "--line", "E")
    capsys.readouterr()

    assert _run(root, "list", "--line", "D") == 0
    out = capsys.readouterr().out
    assert tid in out
    assert "R-E-" not in out

    assert _run(root, "list", "--unclaimed", "--limit", "1") == 0
    out = capsys.readouterr().out
    assert "共 2 条，已显示 1 条" in out


def test_repeatable_flags_accumulate(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """**回归锁定**：多次出现的列表型标志必须累加。

    曾经的缺陷：argparse 的 `nargs="*"` 让最后一次**覆盖**前面几次——
    传 3 条验收标准只存下 1 条，且**静默丢弃**。
    这会让人以为条目的约束齐全，实际只剩最后一条。
    """
    _run(root, "init")
    tid = _register(root, capsys, title="t")
    _run(root, "claim-analyze", "--id", tid, "--role", "pm:pm-D:D")
    code = _run(
        root, "define", "--id", tid, "--role", "pm:pm-D:D",
        "--whitelist", "a/**", "--whitelist", "b/**",
        "--frozen", "x", "--frozen", "y",
        "--acceptance", "test:一", "--acceptance", "negative:二", "--acceptance", "evidence:三",
    )
    assert code == 0, capsys.readouterr().err
    capsys.readouterr()

    assert _run(root, "--json", "show", tid) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["whitelist"] == ["a/**", "b/**"]
    assert payload["frozen"] == ["x", "y"]
    assert len(payload["acceptance"]) == 3
    assert [a["type"] for a in payload["acceptance"]] == ["test", "negative", "evidence"]


def test_quota_command_takes_effect(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """**回归锁定**：`quota.json` 曾被建了却无人读取——设了配额也不生效。

    实测记录：把 `bg_db_max` 设为 1 之后，第二个申请照样通过。
    """
    import os

    _run(root, "init")
    capsys.readouterr()

    assert _run(root, "quota", "--enabled", "true", "--bg-db-max", "1") == 0
    assert "bg_db_max" in capsys.readouterr().out

    pid = str(os.getpid())
    assert _run(root, "acquire", "--holder", "w1", "--db-name", "d1", "--pid", pid) == 0
    capsys.readouterr()

    # 配额 = 1 ⇒ 第二个必须被拒（此前会错误地通过）
    code = _run(root, "acquire", "--holder", "w2", "--db-name", "d2", "--pid", pid)
    assert code == 1
    assert "E_QUOTA_FULL" in capsys.readouterr().err


def test_leases_shows_queue(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    import os

    _run(root, "init")
    _run(root, "quota", "--enabled", "true", "--bg-db-max", "1")
    pid = str(os.getpid())
    _run(root, "acquire", "--holder", "w1", "--db-name", "d1", "--pid", pid)
    _run(root, "acquire", "--holder", "w2", "--db-name", "d2", "--pid", pid)
    capsys.readouterr()

    assert _run(root, "leases") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["quota_enabled"] is True
    assert payload["quota"]["bg_db_max"] == 1
    assert [w["db_name"] for w in payload["waiters"]] == ["d2"]


# ---------------------------------------------------------------------------
# 编号由协调器分配——**代理不选号**（回归锁定）
# ---------------------------------------------------------------------------


def test_register_has_no_id_argument(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """**代理没有挑号的入口**：`register` 不接受 `--id`。"""
    _run(root, "init")
    capsys.readouterr()
    with pytest.raises(SystemExit):
        main([
            "--root", root, "register", "--role", "po:po",
            "--title", "t", "--line", "D", "--id", "T-D-999",
        ])


def test_register_requires_line(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """编号按线分配 ⇒ 没有线别就无法发号（由参数层直接拒绝）。"""
    _run(root, "init")
    capsys.readouterr()
    with pytest.raises(SystemExit):
        main(["--root", root, "register", "--role", "po:po", "--title", "t"])


def test_ids_are_per_family(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """族 = 类型 ＋ 线码：**线内独立计数，族间互不影响**。"""
    _run(root, "init")
    capsys.readouterr()
    d_r = _register(root, capsys, line="D", title="需求")
    d_t = _register(root, capsys, line="D", title="任务", kind="T")
    e_r = _register(root, capsys, line="E", title="E线需求")
    assert (d_r, d_t, e_r) == ("R-D-1", "T-D-1", "R-E-1")


def test_concurrent_registration_never_collides(root: str) -> None:
    """并发登记**零重号**——发号与登记在同一次持锁事务内。"""
    import concurrent.futures

    _run(root, "init")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(
            pool.map(
                lambda i: _run(
                    root, "register", "--role", "po:po",
                    "--title", f"件{i}", "--line", "D",
                ),
                range(12),
            )
        )
    assert all(c == 0 for c in codes)

    from bg_coordinator.storage import Store

    state = Store(root=Path(root)).load_state()
    ids = [t for t in state.tasks if t.startswith("R-D-")]
    assert len(ids) == 12
    assert len(set(ids)) == 12  # 零重号


def test_number_command_reports_gaps(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """`number` 给的是**这笔资源的账**：各族最大号、已分配数、以及有号无物的空洞。"""
    _run(root, "init")
    _register(root, capsys, line="D", title="甲")
    _register(root, capsys, line="D", title="乙", kind="T")
    capsys.readouterr()

    assert _run(root, "--json", "number") == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload["inventory"]) == {"R-D", "T-D"}
    assert payload["inventory"]["R-D"]["high"] == 1
    assert payload["inventory"]["T-D"]["high"] == 1
    assert payload["gaps"] == []  # 账物相符
    assert payload["pending"] == []  # 条目一登记即落物，不留待建
    assert {e["id"] for e in payload["ledger"]} == {"R-D-1", "T-D-1"}


def test_reserve_channel_is_gone(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """**回归锁定**：不许存在"只发号、不建对象"的通道。

    曾经有过 `number --reserve`：发 3 个号却不产生任何对象，
    审计还报"无异常"——号就成了垃圾桶。号是资源，发号必须同时产生对应物。
    """
    _run(root, "init")
    capsys.readouterr()
    with pytest.raises(SystemExit):
        main(["--root", root, "number", "--reserve", "--count", "3"])


def test_out_of_band_number_advance_is_reported(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """号段空洞**必须被审计报出**——静默浪费是资源管理的反面。"""
    from bg_coordinator.storage import Store

    _run(root, "init")
    _register(root, capsys, line="D", title="甲")

    # 人为制造空洞：簿记推进，但账上没有对应物
    st = Store(root=Path(root))
    with st.lock():
        state = st.load_state()
        state.number_book["R-D"] = 5  # 号称发到 5，实际只有一个对象
        st.save_state(state)

    assert _run(root, "audit") == 0
    out = capsys.readouterr().out
    assert "号段空洞" in out
    assert "R-D-" in out




# ---------------------------------------------------------------------------
# 号的资源化：两阶段（占号 → 落物），三类号统一管理
# ---------------------------------------------------------------------------


def test_two_phase_reserve_then_materialize(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """**待建编号有状态**：占号时物还没有，落物后转已落物。

    这是迁移号防撞号的关键：**占号即推进水位**，后来者拿到的是下一个号，
    不会两条线各自以为"最大号是 0036"而都去选 0037。
    """
    _run(root, "init")
    capsys.readouterr()

    assert _run(root, "reserve", "--family", "alembic", "--holder", "dev-A", "--task", "T-A-86") == 0
    first = capsys.readouterr().out.split()[0]

    assert _run(root, "reserve", "--family", "alembic", "--holder", "dev-B", "--task", "T-B-28") == 0
    second = capsys.readouterr().out.split()[0]

    # **水位已推进**：两条线拿到不同的号
    assert first != second

    assert _run(root, "--json", "number") == 0
    payload = json.loads(capsys.readouterr().out)
    inv = payload["inventory"]["alembic"]
    assert inv["pending"] == 2
    assert inv["materialized"] == 0
    assert {p["holder"] for p in payload["pending"]} == {"dev-A", "dev-B"}

    # 落物
    assert _run(
        root, "materialize", "--family", "alembic",
        "--number", first.lstrip("0") or "0", "--id", "0037_migration.py",
    ) == 0
    capsys.readouterr()
    assert _run(root, "--json", "number") == 0
    inv = json.loads(capsys.readouterr().out)["inventory"]["alembic"]
    assert inv["materialized"] == 1
    assert inv["pending"] == 1


def test_pending_allocation_without_task_is_an_anomaly(
    root: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """**无归属的待建号是资源被占着不动**——审计必须报。"""
    _run(root, "init")
    capsys.readouterr()
    _run(root, "reserve", "--family", "alembic", "--holder", "dev-A")
    capsys.readouterr()

    assert _run(root, "audit") == 0
    assert "未绑定来源条目" in capsys.readouterr().out


def test_release_number_requires_reason(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """让号必须给理由——否则与静默丢弃无异。"""
    _run(root, "init")
    capsys.readouterr()
    _run(root, "reserve", "--family", "alembic", "--holder", "dev-A", "--task", "T-A-1")
    capsys.readouterr()

    # 无理由 ⇒ 拒绝
    assert _run(root, "release-number", "--family", "alembic", "--number", "1", "--reason", "") == 1
    capsys.readouterr()

    assert _run(
        root, "release-number", "--family", "alembic", "--number", "1",
        "--reason", "改由补偿式迁移承载",
    ) == 0
    capsys.readouterr()

    assert _run(root, "--json", "number") == 0
    inv = json.loads(capsys.readouterr().out)["inventory"]["alembic"]
    assert inv["released"] == 1
    assert inv["pending"] == 0


def test_released_number_is_not_a_gap(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """**已让号不算空洞**——它有账、有理由。真问题是"无账之号"。"""
    _run(root, "init")
    capsys.readouterr()
    _run(root, "reserve", "--family", "alembic", "--holder", "dev-A", "--task", "T-A-1")
    _run(
        root, "release-number", "--family", "alembic", "--number", "1",
        "--reason", "作废",
    )
    capsys.readouterr()

    assert _run(root, "--json", "number") == 0
    assert json.loads(capsys.readouterr().out)["gaps"] == []


def test_task_families_are_materialized_immediately(
    root: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """三类号统一管理，但**任务与需求一登记即落物**——不留待建。"""
    _run(root, "init")
    capsys.readouterr()
    _register(root, capsys, line="D", title="需求")
    _register(root, capsys, line="D", title="任务", kind="T")
    capsys.readouterr()

    assert _run(root, "--json", "number") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["inventory"]["R-D"]["materialized"] == 1
    assert payload["inventory"]["T-D"]["materialized"] == 1
    assert payload["pending"] == []


# ---------------------------------------------------------------------------
# 开发线可管理 + 需求来源可分类（用户口径：线数由任务定；输入源须可分类）
# ---------------------------------------------------------------------------


def test_dev_lines_are_manageable(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """**线的数量与划分由任务决定**——新线开在协调器里，不开在代码里。"""
    _run(root, "init")
    capsys.readouterr()

    assert _run(root, "--json", "line") == 0
    assert "A" in json.loads(capsys.readouterr().out)  # 内置初始集合

    assert _run(root, "line", "--add", "PAY", "--note", "柜台接入线") == 0
    capsys.readouterr()
    assert _run(root, "--json", "line") == 0
    assert "PAY" in json.loads(capsys.readouterr().out)

    # 新线可用：登记到这条线
    tid = _register(root, capsys, line="PAY", title="柜台接入需求")
    assert tid.startswith("R-PAY-")


def test_unregistered_line_is_rejected(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    _run(root, "init")
    capsys.readouterr()
    assert _run(root, "register", "--role", "po:po", "--title", "x", "--line", "ZZZ") == 1
    err = capsys.readouterr().err
    assert "E_BAD_LINE" in err
    assert "未登记" in err


def test_duplicate_line_add_is_rejected(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    _run(root, "init")
    capsys.readouterr()
    assert _run(root, "line", "--add", "PAY") == 0
    capsys.readouterr()
    assert _run(root, "line", "--add", "PAY") == 1
    assert "已存在" in capsys.readouterr().err


def test_requirement_origin_is_classified(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """**输入源必须可分类**——否则回答不了"这事是谁提的"。"""
    _run(root, "init")
    capsys.readouterr()
    cases = [
        ("commander", "用户原话：要能看 K 线"),
        ("line", "T-D-1 开发中发现"),
        ("dba", "0034 窗口复核发现孤儿授权"),
        ("ops", "巡检 worktree.stale 报出"),
        ("user", "用户直接指令"),
        ("pm", "制定计划时发现口径缺口"),
    ]
    ids = []
    for origin, ref in cases:
        argv = [
            "--json", "register", "--role", "po:po", "--title", f"{origin} 来源",
            "--line", "D", "--origin", origin, "--source-ref", ref,
        ]
        assert _run(root, *argv) == 0, capsys.readouterr().err
        ids.append(json.loads(capsys.readouterr().out)["detail"]["id"])

    for tid, (origin, ref) in zip(ids, cases, strict=True):
        assert _run(root, "--json", "show", tid) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["origin"] == origin
        assert payload["source_ref"] == ref


def test_development_feedback_is_traceable(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """**开发中产生的需求可溯源**：PL／PM 发现问题 → 登记需求 → PO 转化。

    这条链能走通（登记对所有人开放），且**来源可查**——
    "这条需求是从哪来的"必须能回答。
    """
    _run(root, "init")
    tid = _register(root, capsys, line="D", title="原始开发任务", kind="T", priority="P1")

    # 开发中发现缺口 ⇒ 登记新需求，绑定来源条目
    follow = _register(
        root, capsys, line="D", title="开发中发现的口径缺口",
        origin="line", source_ref=f"{tid} 开发中发现",
    )
    assert follow != tid

    assert _run(root, "--json", "show", follow) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["origin"] == "line"
    assert tid in payload["source_ref"]  # 溯源锚：指向产生它的条目


# ---------------------------------------------------------------------------
# TL 评估后分流：**本线自决不过 PO，越线才上 PO**（用户口径）
# ---------------------------------------------------------------------------


def test_intra_line_raise_bypasses_po(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """**本线的事不必经 PO 中转**——TL 有四要素即可直达可开发。

    理由：TL 持有本线完整上下文（代码现状、在办条目、白名单边界），
    PO 中转不增加信息，只增加时延。
    """
    _run(root, "init")
    capsys.readouterr()
    assert _run(
        root, "--json", "raise", "--role", "tl:TL-D:D", "--category", "technical",
        "--scope", "intra_line",
        "--title", "本线口径缺口", "--source-ref", "T-D-1 开发中发现", "--priority", "P1",
        "--whitelist", "data_access/**", "--frozen", "main.py",
        "--acceptance", "test:uv run pytest -q",
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    tid = payload["detail"]["id"]
    assert payload["detail"]["routed_to"] == "tech-lead"  # **没有上 PO**
    assert payload["detail"]["defined"] is True

    # 直接进入可认领清单
    assert _run(root, "ready", "--role-name", "tech-lead") == 0
    assert tid in capsys.readouterr().out

    assert _run(root, "--json", "show", tid) == 0
    task = json.loads(capsys.readouterr().out)
    assert task["scope"] == "intra_line"
    assert task["origin"] == "line"
    assert task["kind"] == "T"  # 已从需求升级为任务
    assert task["status"] == "defined"


def test_intra_line_requires_all_four_elements(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """本线自决 = 你直接把它变成可开发任务 ⇒ **四要素不能缺**。"""
    _run(root, "init")
    capsys.readouterr()
    assert _run(
        root, "raise", "--role", "tl:TL-D:D", "--category", "technical",
        "--scope", "intra_line", "--title", "缺白名单", "--whitelist", "a/**",
    ) == 1
    err = capsys.readouterr().err
    assert "E_INCOMPLETE" in err
    assert "验收标准" in err


def test_cross_line_raise_routes_to_po(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """**越线必须上 PO**——跨线权衡是 PO 的活。"""
    _run(root, "init")
    capsys.readouterr()
    assert _run(
        root, "--json", "raise", "--role", "tl:TL-D:D", "--category", "cross_line",
        "--title", "需要 C 线配合", "--line", "C", "--source-ref", "T-D-1",
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    tid = payload["detail"]["id"]
    assert payload["detail"]["routed_to"] == "po"
    assert "defined" not in payload["detail"]

    assert _run(root, "--json", "show", tid) == 0
    task = json.loads(capsys.readouterr().out)
    assert task["status"] == "registered"  # **停在需求态，等 PO**
    assert task["scope"] == "cross_line"
    assert task["line"] == "C"


def test_intra_line_cannot_land_on_another_line(
    root: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """**本线自决不能落别人的线**——那是越线，须走 cross_line 交 PO。"""
    _run(root, "init")
    capsys.readouterr()
    assert _run(
        root, "raise", "--role", "tl:TL-D:D", "--category", "technical",
        "--scope", "intra_line", "--title", "想落 C 线", "--line", "C",
        "--whitelist", "a/**", "--acceptance", "test:pytest",
    ) == 1
    assert "E_CROSS_LINE" in capsys.readouterr().err


def test_raise_requires_scope_declaration(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """影响面是必填——**它决定去向**，不能默认。"""
    _run(root, "init")
    capsys.readouterr()
    with pytest.raises(SystemExit):
        main([
            "--root", root, "raise", "--role", "tl:TL-D:D", "--title", "x",
        ])


# ---------------------------------------------------------------------------
# 分权：**TL 只裁技术问题**；设计问题归 PM（经 PO）、跨线问题归 PO
# ---------------------------------------------------------------------------


def test_technical_issue_is_tl_decidable(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """技术问题（怎么实现、内部怎么拆、用既有契约的哪种形态）⇒ TL 自决。"""
    _run(root, "init")
    capsys.readouterr()
    assert _run(
        root, "--json", "raise", "--role", "tl:TL-D:D", "--category", "technical",
        "--title", "内部拆法调整", "--whitelist", "data_access/kline/**",
        "--acceptance", "test:uv run pytest -q",
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["detail"]["routed_to"] == "tech-lead"
    assert payload["detail"]["defined"] is True


def test_design_issue_cannot_be_self_determined(
    root: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """**TL 只裁技术问题**——拿设计问题走本线自决即越权，必须被拒。

    设计问题 = 契约／领域模型／口径／标准／阈值，归 PM，经 PO 转化。
    """
    _run(root, "init")
    capsys.readouterr()
    assert _run(
        root, "raise", "--role", "tl:TL-D:D", "--category", "design",
        "--scope", "intra_line", "--title", "改数据面契约",
        "--whitelist", "data_access/ports.py", "--acceptance", "test:pytest",
    ) == 1
    err = capsys.readouterr().err
    assert "E_CATEGORY_ESCALATE" in err
    assert "不归 TL 裁" in err


def test_design_issue_routes_to_po(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """设计问题**自动上 PO**——不声明 scope 也不会落到 TL 手里。"""
    _run(root, "init")
    capsys.readouterr()
    assert _run(
        root, "--json", "raise", "--role", "tl:TL-D:D", "--category", "design",
        "--title", "改数据面契约", "--source-ref", "R-D-1 开发中发现",
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    tid = payload["detail"]["id"]
    assert payload["detail"]["routed_to"] == "po"
    assert "defined" not in payload["detail"]

    assert _run(root, "--json", "show", tid) == 0
    task = json.loads(capsys.readouterr().out)
    assert task["status"] == "registered"
    assert task["category"] == "design"


def test_cross_line_issue_routes_to_po(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    _run(root, "init")
    capsys.readouterr()
    assert _run(
        root, "--json", "raise", "--role", "tl:TL-D:D", "--category", "cross_line",
        "--line", "C", "--title", "需 C 线配合",
    ) == 0
    assert json.loads(capsys.readouterr().out)["detail"]["routed_to"] == "po"


def test_raise_requires_category(root: str, capsys: pytest.CaptureFixture[str]) -> None:
    """**类别决定去向**，不能默认——默认成 TL 会让越权静默发生。"""
    _run(root, "init")
    capsys.readouterr()
    with pytest.raises(SystemExit):
        main(["--root", root, "raise", "--role", "tl:TL-D:D", "--title", "x"])


def test_category_owner_table_is_the_single_authority() -> None:
    """**分权的单一权威**是那张表——判定与文档都引它，不各自为政。"""
    from bg_coordinator.models import CATEGORY_OWNER, DecisionCategory

    assert CATEGORY_OWNER[DecisionCategory.TECHNICAL.value] == "tech-lead"
    assert CATEGORY_OWNER[DecisionCategory.DESIGN.value] == "po"
    assert CATEGORY_OWNER[DecisionCategory.CROSS_LINE.value] == "po"
    # **TL 只裁技术一类**
    assert [k for k, v in CATEGORY_OWNER.items() if v == "tech-lead"] == ["technical"]


# ---------------------------------------------------------------------------
# 文档同步：**不强制修订，但一旦修订必须同批**
# ---------------------------------------------------------------------------


def _deliver_with(
    root: str, capsys: pytest.CaptureFixture[str], tmp_path: Path, changed: list[str]
) -> str:
    ev = tmp_path / "g.log"
    ev.write_text("ok", encoding="utf-8")
    tid = _register(root, capsys, line="D", title="件", kind="T", priority="P1")
    for step in (
        ("claim-analyze", "--id", tid, "--role", "pm:pm-D:D"),
        (
            "define", "--id", tid, "--role", "pm:pm-D:D",
            "--whitelist", "a/**", "--whitelist", "docs/**",
            "--acceptance", "test:pytest",
        ),
        ("claim-dev", "--id", tid, "--role", "tl:TL-D:D"),
        ("start", "--id", tid, "--role", "tl:TL-D:D"),
        (
            "deliver", "--id", tid, "--role", "tl:TL-D:D",
            "--commit", "abc", "--gate-cmd", "pytest", "--gate-exit", "0",
            "--evidence", str(ev), "--changed", *changed,
        ),
    ):
        assert _run(root, *step) == 0, capsys.readouterr().err
    capsys.readouterr()
    return tid


def test_doc_revision_is_not_mandatory(
    root: str, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """**文档修订不是必做动作**——纯内部实现改动，不碰文档也不报异常。"""
    _run(root, "init")
    capsys.readouterr()
    tid = _deliver_with(root, capsys, tmp_path, ["a/impl.py"])
    assert _run(root, "--json", "show", tid) == 0
    assert json.loads(capsys.readouterr().out)["doc_sync"] == []

    assert _run(root, "audit") == 0
    assert "文档" not in capsys.readouterr().out


def test_design_surface_without_doc_is_reported(
    root: str, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """**改了面就必须同批改文档**——触碰设计面而本批无文档 ⇒ 异常。"""
    _run(root, "init")
    capsys.readouterr()
    _deliver_with(root, capsys, tmp_path, ["a/ports.py"])

    assert _run(root, "audit") == 0
    out = capsys.readouterr().out
    assert "触碰设计面" in out
    assert "本批无文档同步" in out


def test_design_surface_with_doc_is_fine(
    root: str, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """成对即可——改了面同时改了文档，不报异常。"""
    _run(root, "init")
    capsys.readouterr()
    tid = _deliver_with(root, capsys, tmp_path, ["a/ports.py", "docs/DATA_ACCESS.md"])

    assert _run(root, "--json", "show", tid) == 0
    assert json.loads(capsys.readouterr().out)["doc_sync"] == ["docs/DATA_ACCESS.md"]

    assert _run(root, "audit") == 0
    assert "触碰设计面" not in capsys.readouterr().out


def test_doc_path_helpers() -> None:
    """同步对的判据本身可测——避免它只是散文。"""
    from bg_coordinator.validators import changed_docs, is_doc_path, touches_design_surface

    assert is_doc_path("docs/DATA_ACCESS.md")
    assert is_doc_path("README.md")
    assert not is_doc_path("data_access/ports.py")

    assert touches_design_surface(["data_access/ports.py"]) == ["data_access/ports.py"]
    assert touches_design_surface(["a/impl.py"]) == []

    # 计划阶段声明要动文档，也算同步对
    # 只记**实际改了**的文档——计划里列了不算已同步
    assert changed_docs(["a/impl.py", "docs/DATA_ACCESS.md"]) == ["docs/DATA_ACCESS.md"]
    assert changed_docs(["a/impl.py"]) == []


# ---------------------------------------------------------------------------
# 回归：**默认值绝不许指向真实主检出**
# ---------------------------------------------------------------------------


def test_no_repo_default_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 `--repo`、没有 `$BG_COORDINATOR_REPO` ⇒ repo 为 None，**不猜任何人**。

    这里曾经硬编码主检出路径。后果是一次「只覆盖 `--root`、忘了 `--repo`」的
    `publish` 把真实 `todo/` **13 个文件、2306 行**换成了空投影——
    **默认值指向别人的工作区，就是一颗静默改主工作区的雷。**
    """
    from bg_coordinator import cli

    monkeypatch.delenv("BG_COORDINATOR_REPO", raising=False)
    assert cli._default_repo() is None


def test_publish_without_repo_writes_nothing(
    root: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有显式仓库时 `publish` **报错退出且不写任何文件**（fail closed）。"""
    # 先摘掉环境变量：否则这个测试自己会往别人声明的仓库里写
    monkeypatch.delenv("BG_COORDINATOR_REPO", raising=False)
    _run(root, "init")
    capsys.readouterr()
    rc = main(["--root", root, "publish", "--no-commit"])
    captured = capsys.readouterr()
    assert rc == 2, "无目标仓库时发布必须失败，不得静默挑一个"
    assert "STORE ERROR" in captured.err
    assert list(tmp_path.rglob("board.md")) == []
    assert list(tmp_path.rglob("调度报告.md")) == []
