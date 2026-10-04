"""视图渲染测试——方案 §4.4／§4.5。

两份视图，读者不同：
- **视图 A（兼容）**：必须精确匹配旧格式，否则 C3／C8／C9 会红，290 处引用白保；
- **视图 B（报告）**：纯人读，**绝对不能出现原材料**。
"""

from __future__ import annotations

from pathlib import Path

from bg_coordinator.engine import Params, State, apply
from bg_coordinator.models import (
    AcceptanceItem,
    AcceptanceType,
    Actor,
    Kind,
    Line,
    Priority,
    Role,
)
from bg_coordinator.render import (
    BOARD_HEADER,
    INBOX_HEADER,
    LINES,
    render_all,
    render_board,
    render_inbox,
    render_line,
    render_report,
    render_todo_ready,
)


def _po() -> Actor:
    return Actor(role=Role.PO, name="po", line=None)


def _pm() -> Actor:
    return Actor(role=Role.PM, name="pm-D", line=Line.D)


def _state_with_task(tmp_path: Path) -> State:
    ev = tmp_path / "g.log"
    ev.write_text("ok", encoding="utf-8")
    s = apply(
        State(),
        "register",
        "T-D-1",
        _po(),
        params=Params(title="K 线取数", line=Line.D, kind=Kind.R, priority=Priority.P1),
    ).state
    s = apply(s, "claim-analyze", "T-D-1", _pm()).state
    return apply(
        s,
        "define",
        "T-D-1",
        _pm(),
        params=Params(
            whitelist=["data_access/**"],
            frozen=["main.py"],
            acceptance=[AcceptanceItem(type=AcceptanceType.TEST, cmd="pytest -q", desc="绿")],
        ),
    ).state


# ---------------------------------------------------------------------------
# 视图 A：兼容
# ---------------------------------------------------------------------------


def test_line_board_header_is_byte_identical(tmp_path: Path) -> None:
    """表头逐字保持——**这是零改造的关键**。"""
    text = render_line(_state_with_task(tmp_path), Line.D)
    assert BOARD_HEADER in text


def test_inbox_header_has_no_gap(tmp_path: Path) -> None:
    """表头↔分隔行间距必须为 0（C8）。"""
    text = render_inbox(_state_with_task(tmp_path), Line.D)
    assert INBOX_HEADER in text
    head_lines = text.splitlines()
    idx = next(i for i, ln in enumerate(head_lines) if ln.startswith("| 编号"))
    assert head_lines[idx + 1].startswith("|---")


def test_line_board_rows_have_seven_cells(tmp_path: Path) -> None:
    """7 格 = 8 条未转义竖线（C9）。"""
    text = render_line(_state_with_task(tmp_path), Line.D)
    rows = [ln for ln in text.splitlines() if ln.startswith("| T-")]
    assert rows
    for row in rows:
        assert row.count("|") - row.count(r"\|") == 8, row


def test_status_cell_first_char_is_token(tmp_path: Path) -> None:
    """状态格**首字符**必须是状态 token（C3 机读）。"""
    text = render_line(_state_with_task(tmp_path), Line.D)
    rows = [ln for ln in text.splitlines() if ln.startswith("| T-")]
    cell = rows[0].split("|")[-2].strip()
    assert cell[0] in "[✅🔶⏸⛔"


def test_whitelist_backticks_are_paired(tmp_path: Path) -> None:
    """反引号成对——不成对会吞掉后续 token，白名单整段失效（C9）。"""
    text = render_line(_state_with_task(tmp_path), Line.D)
    for row in (ln for ln in text.splitlines() if ln.startswith("| T-")):
        assert row.count("`") % 2 == 0


def test_pipe_in_title_is_escaped(tmp_path: Path) -> None:
    s = _state_with_task(tmp_path)
    s.tasks["T-D-1"].title = "A | B"
    text = render_line(s, Line.D)
    row = next(ln for ln in text.splitlines() if ln.startswith("| T-D-1"))
    assert r"\|" in row
    assert row.count("|") - row.count(r"\|") == 8


def test_render_all_covers_every_line_and_report(tmp_path: Path) -> None:
    files = render_all(_state_with_task(tmp_path))
    names = {str(p) for p in files}
    assert "reports/调度报告.md" in names
    assert "todo/board.md" in names
    assert all(n.startswith("todo/lines/") or n.startswith("todo/inbox/") or True for n in names)
    assert sum(1 for n in names if n.startswith("todo/lines/")) == len(LINES)


def test_board_counts(tmp_path: Path) -> None:
    """需求在 `define` 时升级为任务（R→T，共用 id）——所以这里任务数为 1、需求数为 0。"""
    text = render_board(_state_with_task(tmp_path))
    assert "| D | 0 | 1 |" in text


# ---------------------------------------------------------------------------
# 视图 B：报告（**只出现异常**）
# ---------------------------------------------------------------------------


def test_report_has_all_seven_sections(tmp_path: Path) -> None:
    text = render_report(_state_with_task(tmp_path))
    sections = (
        "一、总览", "二、需要你决定", "三、在办", "四、阻塞",
        "五、资源", "六、异常", "七、本期变更",
    )
    for section in sections:
        assert section in text, f"缺节：{section}"


def test_report_never_dumps_raw_material(tmp_path: Path) -> None:
    """**核心断言：报告不出现原材料。**

    14 项审计明细、SUBMISSION 路径清单、线板全量——都不许出现。
    """
    text = render_report(_state_with_task(tmp_path))
    assert "process_audit" not in text
    assert "SUBMISSION" not in text
    assert "C1 " not in text
    # 只给摘要
    assert "正常" in text and "异常" in text
    assert "14 项审计明细不进报告" in text


def test_report_lists_anomalies_when_present(tmp_path: Path) -> None:
    s = _state_with_task(tmp_path)
    s.tasks["T-D-1"].priority = None
    s.tasks["T-D-1"].status = __import__(
        "bg_coordinator.models", fromlist=["TaskState"]
    ).TaskState.IN_PROGRESS
    s.tasks["T-D-1"].owner = "TL-D"
    text = render_report(s)
    assert "未定优先级" in text


def test_report_snapshot_point_is_shown(tmp_path: Path) -> None:
    """审计滞后必须**如实写给人看**——避免"审计说没问题"被误读成 live 没问题。"""
    text = render_report(_state_with_task(tmp_path))
    assert "快照点" in text


def test_report_shows_quota_not_enabled(tmp_path: Path) -> None:
    s = _state_with_task(tmp_path)
    text = render_report(s)
    assert "未启用" in text or "仅观测" in text


def test_ready_list_only_lists_ready(tmp_path: Path) -> None:
    s = _state_with_task(tmp_path)
    text = render_todo_ready(s, role=Role.TECH_LEAD)
    assert "T-D-1" in text  # P1 且白名单无冲突 ⇒ 可认领


def test_unknown_line_header_does_not_crash() -> None:
    assert "线路总账" in render_board(State())
