"""标定器测试——**判据必须可测，否则拐点全靠眼看**。

标定器只出建议、不写配额；但它的**判据**必须钉死：
拐点找错会把预算定错，而预算错了要么饿着线、要么撑着机器。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "calibrate", Path(__file__).resolve().parents[1] / "tools" / "calibrate.py"
)
assert _SPEC and _SPEC.loader
cal = importlib.util.module_from_spec(_SPEC)
sys.modules["calibrate"] = cal
_SPEC.loader.exec_module(cal)

Rung = cal.Rung
Ladder = cal.LadderResult


def _rung(width: int, wall: float, *, eff: float | None = None, failed: int = 0) -> Rung:
    return Rung(
        width=width,
        jobs=8,
        wall_s=wall,
        cpu_ratio=0.9,
        mem_low_mb=3000,
        ok=8 - failed,
        failed=failed,
        per_job_ms=wall * 1000 / 8,
        throughput=8 / wall,
        speedup=1.0,
        efficiency=eff if eff is not None else 1.0,
    )


# ---------------------------------------------------------------------------
# 拐点判据
# ---------------------------------------------------------------------------


def test_knee_picks_narrowest_near_best() -> None:
    """取**墙钟接近全程最小**的**最窄**档——不是"最后一个还能跑的档"。"""
    lad = Ladder(kind="mysql", serial_s=1.8)
    lad.rungs = [
        _rung(1, 1.87, eff=0.94),
        _rung(2, 1.24, eff=1.42),
        _rung(3, 0.96, eff=1.83),
        _rung(4, 0.80, eff=2.21),
        _rung(6, 0.65, eff=2.69),
        _rung(8, 0.82, eff=2.16),
        _rung(12, 0.71, eff=2.49),
    ]
    assert lad.safe_width == 6, "最好吞吐是 6 路（0.65s），最窄接近它的是 6"


def test_knee_ignores_single_rung_noise() -> None:
    """**单档抖动不能骗过判据**——这正是逐档比较会犯的错。

    实测踩过：宽度 2→3 抖一次就被误判成见顶。
    """
    lad = Ladder(kind="mysql", serial_s=2.0)
    lad.rungs = [
        _rung(1, 2.00, eff=1.0),
        _rung(2, 0.90, eff=2.2),
        _rung(3, 0.95, eff=2.1),  # ← 抖动：比 2 略慢，但不该被判成见顶
        _rung(4, 0.70, eff=2.9),
    ]
    assert lad.safe_width == 4, "抖动之后的 4 明显更好，不该停在 2"


def test_knee_respects_concurrency_gain_gate() -> None:
    """**并发效率低于 1.3 即出局**——那说明并发反而更慢（锁竞争）。"""
    lad = Ladder(kind="mysql", serial_s=1.0)
    lad.rungs = [
        _rung(1, 1.00, eff=1.0),
        _rung(2, 1.10, eff=0.91),  # 慢于串行
        _rung(4, 1.30, eff=0.77),
    ]
    assert lad.safe_width == 1, "没有一档达标 ⇒ 退回串行"


def test_failed_rung_is_excluded() -> None:
    """有失败的档直接出局——**打崩了就不算"能用"**。"""
    lad = Ladder(kind="mysql", serial_s=1.0)
    lad.rungs = [
        _rung(1, 1.00, eff=1.0),
        _rung(2, 0.60, eff=1.7),
        _rung(4, 0.50, eff=2.0, failed=3),
    ]
    assert lad.safe_width == 2


def test_width_one_never_participates() -> None:
    """宽度 1 是串行基准，**不参与判定**——判它自相矛盾。"""
    lad = Ladder(kind="mysql", serial_s=1.0)
    lad.rungs = [_rung(1, 1.00, eff=0.5)]
    assert lad.safe_width == 1
    assert lad.elbow is None, "只有串行基准时没有拐点可说"


def test_elbow_is_the_first_unworthwhile_rung() -> None:
    lad = Ladder(kind="mysql", serial_s=1.8)
    lad.rungs = [
        _rung(1, 1.87, eff=0.94),
        _rung(2, 1.24, eff=1.42),
        _rung(6, 0.65, eff=2.69),
        _rung(8, 0.82, eff=2.16),
    ]
    assert lad.safe_width == 6
    elbow = lad.elbow
    assert elbow is not None and elbow.width == 8


def test_within_tolerance_is_respected() -> None:
    """容差决定"算同一水平"的宽度——它必须真的起作用。"""
    lad = Ladder(kind="mysql", serial_s=1.0)
    lad.rungs = [
        _rung(1, 1.00, eff=1.0),
        _rung(4, 0.60, eff=1.7),
        _rung(8, 0.63, eff=1.6),  # 比最好慢 5%，在 10% 容差内
    ]
    assert lad.safe_width == 4, "容差内取更窄的档"


# ---------------------------------------------------------------------------
# 分配建议
# ---------------------------------------------------------------------------


def test_suggest_follows_workload_share() -> None:
    """**预算跟着工作量走**——线是工作面，工作量天然不均。"""
    loads = [
        cal.LineLoad(line="C", ready=8, in_flight=0, weight=8),
        cal.LineLoad(line="D", ready=2, in_flight=0, weight=2),
        cal.LineLoad(line="E", ready=0, in_flight=0, weight=0),
    ]
    out = cal.suggest(loads, safe_width=6)
    assert out["C"] > out["D"], "工作量大的线应当分到更多预算"
    assert "E" not in out, "没有活的线不进建议"


def test_suggest_gives_at_least_one_to_active_line() -> None:
    """有活的线**至少 1**——否则它永远开不了工。"""
    loads = [
        cal.LineLoad(line="C", ready=100, in_flight=0, weight=100),
        cal.LineLoad(line="D", ready=1, in_flight=0, weight=1),
    ]
    out = cal.suggest(loads, safe_width=2)
    assert out["D"] >= 1


def test_suggest_empty_when_no_work() -> None:
    assert cal.suggest([cal.LineLoad("D", 0, 0, 0)], safe_width=4) == {}


def test_line_load_skips_ops() -> None:
    """OPS 线亲办、不派 dev 子代理——不参与预算分配。"""
    codes = [x.line for x in cal.read_line_load({}, ["A", "OPS"], {"in_progress"})]
    assert "OPS" not in codes


def test_line_load_counts_all_open_items_not_just_defined() -> None:
    """待办口径取**全部未终态**——只数"已定稿"会系统性低估。

    刚登记的需求也是这条线的活；把它算漏了，预算就会给少。
    """
    from bg_coordinator.models import Kind, Task, TaskState

    def t(tid: str, line: str, status: TaskState) -> Task:
        return Task(id=tid, kind=Kind.T, line=line, status=status)

    tasks = {
        "C-1": t("C-1", "C", TaskState.REGISTERED),
        "C-2": t("C-2", "C", TaskState.ANALYZING),
        "C-3": t("C-3", "C", TaskState.DEFINED),
        "C-4": t("C-4", "C", TaskState.IN_PROGRESS),
        "C-5": t("C-5", "C", TaskState.ACCEPTED),  # 终态，不算
    }
    loads = {x.line: x for x in cal.read_line_load(tasks, ["C"], {"in_progress"})}
    assert loads["C"].weight == 4, "四条未终态都该算进权重"
    assert loads["C"].in_flight == 1
    assert loads["C"].ready == 3


# ---------------------------------------------------------------------------
# 多维：**瓶颈不止一个，取最小者**
# ---------------------------------------------------------------------------


def test_every_dimension_launches_in_parallel() -> None:
    """**每个维度都必须并行启动**——串行跑会把并行度误判成 1。

    这是实测踩过的坑：非触库维度曾用串行循环，于是每个维度都得出
    「并行无用」（效率恒≈1），把真实瓶颈掩盖。修好后 CPU 维效率 3.77（4 核理想 4）。
    """
    src = (
        pathlib.Path(__file__).resolve().parents[1] / "tools" / "calibrate.py"
    ).read_text(encoding="utf-8")
    # 跑阶梯的那段必须用 Popen 并行启动，不得出现"逐个 run_one"的串行循环
    ladder_src = src[src.index("def run_ladder") : src.index("def read_line_load")]
    assert "subprocess.Popen(" in ladder_src
    assert "for _ in group:\n                    good, err, cost = run_one" not in ladder_src


def test_unit_commands_cover_all_dimensions() -> None:
    """四维各有可执行单元——缺一个就等于那维没量。"""
    for kind in ("cpu", "mem", "io", "devio", "mysql"):
        cmd = cal._unit_command(kind)
        assert cmd and isinstance(cmd, list)
        assert cmd[0]


def test_devio_is_small_files_not_big_blob() -> None:
    """**开发 IO 是小文件，不是大文件**——用大文件量会得出"磁盘是瓶颈"的假结论。

    依据：本仓 git commit 实测 6~13ms；大文件写＋fsync 约 480ms/件。
    """
    cmd = cal._unit_command("devio")
    joined = " ".join(cmd)
    assert "DEV_FILE_COUNT" in joined or "range(200)" in joined
    assert "fsync" not in joined, "开发形态不该做 fsync 大文件"


def test_default_dims_use_dev_io_not_machine_io() -> None:
    """默认量的是**开发形态**的磁盘，不是机器极限。"""
    src = (
        pathlib.Path(__file__).resolve().parents[1] / "tools" / "calibrate.py"
    ).read_text(encoding="utf-8")
    assert '"cpu", "mem", "devio"' in src, "默认维度应当含 devio 而非 io"


# ---------------------------------------------------------------------------
# 测试维度：**最接近真实的一档负载**
# ---------------------------------------------------------------------------


def test_test_unit_runs_the_repo_fast_channel(tmp_path: Path) -> None:
    """测试单元跑的必须是**目标仓库的快跑通道**。

    真库档按 `docs/TESTING.md §3` 不得并发（并发互踩＝假红），
    所以「测试能开多宽」只对 `-m "not db"` 成立——这条不是保守，是纪律。
    """
    (tmp_path / "tests").mkdir()
    ctx = {"repo": tmp_path, "python": "/usr/bin/python3", "units": cal.TEST_UNITS}
    cmd = cal._unit_command("test", 0, ctx)
    assert cmd[0] == "/usr/bin/python3"
    joined = " ".join(cmd)
    assert "-m pytest" in joined
    assert cal.TEST_MARKER in joined, "必须只跑快跑通道"
    assert str(tmp_path) in joined, "必须跑目标仓库的测试文件"


def test_test_unit_rotates_across_lines() -> None:
    """单元**按线轮转**：不同 job 跑不同测试面，才像多个子代理并行。"""
    assert len(cal.TEST_UNITS) >= 3, "至少三档测试面，轮转才有意义"
    for unit in cal.TEST_UNITS:
        assert unit, "空单元会让整档跑空"
        assert all(f.startswith("tests/") for f in unit)


def test_test_units_all_not_db_marked_source() -> None:
    """测试面清单里的文件必须真实存在——文件缺失会让整件必红，把 W 误判成 1。"""
    repo = (Path(__file__).resolve().parents[1] / ".." / ".." / "futures-broker-gateway").resolve()
    if not (repo / "tests").is_dir():
        pytest.skip("目标仓库不在本机")
    _, why = cal.test_context(repo)
    assert why == "", f"目标仓库里测试面应当可用，却报：{why}"


def test_busy_host_is_flagged() -> None:
    """有别的会话在跑时必须**报不可信**——不能把噪声当拐点。"""
    idle = cal.Machine(
        cpus=4, mem_total_mb=8000, mem_limit_mb=None, load1=0.2, mysql_ok=True
    )
    busy = cal.Machine(
        cpus=4, mem_total_mb=8000, mem_limit_mb=None, load1=4.6, mysql_ok=True
    )
    assert not cal.host_is_busy(idle)
    assert cal.host_is_busy(busy)


def test_invalid_dimension_is_excluded_from_min() -> None:
    """**量不出来的维度不进 min(各维)**——否则一次"本来就红"把 W 压成 1。"""
    src = (
        pathlib.Path(__file__).resolve().parents[1] / "tools" / "calibrate.py"
    ).read_text(encoding="utf-8")
    assert "if not lad.invalid" in src


def test_widths_are_divisors_of_jobs() -> None:
    """宽度必须整除 jobs——否则最后一批不满宽，档与档之间不可比。"""
    ws = cal.widths_for(12, 24)
    assert ws == [1, 2, 3, 4, 6, 12]
    assert all(12 % w == 0 for w in ws)
    # jobs=8 时 3/6 不可比，必须被剔掉
    assert cal.widths_for(8, 24) == [1, 2, 4, 8]


# ---------------------------------------------------------------------------
# 边界：标定器不得因环境失败而崩
# ---------------------------------------------------------------------------


def test_detect_machine_returns_sane_values() -> None:
    m = cal.detect_machine()
    assert m.cpus >= 1
    assert m.mem_total_mb > 0


def test_probe_only_does_not_run_load(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`--probe-only` 只量机器——不做阶梯。"""
    rc = cal.main(["--probe-only"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "核数" in out
    # 必须没有**结果表**（参数说明里的「阶梯」字样不算）
    assert "并发效率" not in out


def test_calibrator_never_writes_quota(tmp_path: Path) -> None:
    """**标定器只出建议，不写配额。**

    写配额是一个决策——哪条线该配几个子代理得由人拍板；
    脚本自作主张写下去，就把那个决策偷偷替人做了。
    """
    src = (Path(__file__).resolve().parents[1] / "tools" / "calibrate.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("set_quota(", "save_quota(", "quota_path.write"):
        assert forbidden not in src, f"标定器不该写配额，却发现 {forbidden}"
