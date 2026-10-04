#!/usr/bin/env python3
"""逐线预算标定器——**用真实负载量出并行度拐点，再按各线占比分配**。

**瓶颈不止一个。** 并行度被这几类资源各卡一遍，取**最小者**：

| 维度 | 量法 | 典型病征 |
|---|---|---|
| `db` | 并发**建库→全链迁移→删库**（`db_ladder.py`，真负载） | 元数据锁竞争，单件耗时随并发飙升 |
| `test` | 并发跑**真实受影响面测试**（快跑通道） | 测试是最重的一档负载，常是真正的瓶颈 |
| `cpu` | 并发跑 GIL-free 计算 | 核数用满后墙钟不再下降 |
| `mem` | 并发分配大块内存 | 可用内存掉到阈值以下 |
| `io` | 并发读写大文件 | 磁盘吞吐饱和（**机器极限，不代表开发形态**——默认不量） |

**只看数据库会高估上限**——你可能开得起 6 个 dev 子代理连库，
但它们同时跑测试时被 CPU 或内存先卡住。所以 W = min(各维度安全宽度)。

`test` 维度是**唯一非合成**的一档：它跑的就是 dev 子代理交付前该跑的
「受影响面测试」，所以它量出来的宽度最接近真实可派宽度。
它只跑**快跑通道**（`-m "not db"`）——真库档按 `docs/TESTING.md §3`
**不得与其它会话并发**（并发互踩＝假红），
所以「测试能开多宽」这个问题**只对快跑通道成立**，真库档是纪律上的串行。

为什么需要它：预算不能靠猜。**线是工作面，工作量天然不均**；
给统一默认值要么饿着重的线、要么撑着轻的线，更糟的是它把
「要不要给这条线加人」这个真问题藏起来。所以：

```
① 量机器   阶梯并发跑**真实触库负载** → 找效率拐点 → 得安全宽度 W
② 量各线   读协调器状态 → 各线的待办量 → 得占比
③ 分配     预算 = W × 该线占比（向下取整，至少 1）
```

**本脚本只出建议，不写配额。** 写配额是一个决策，得由人来做——
那正是「决策缺口必须暴露」这条的具体执行。

用法：
    python3 tools/calibrate.py                    # 全跑（阶梯 + 分配建议）
    python3 tools/calibrate.py --probe-only       # 只量机器，不做阶梯
    python3 tools/calibrate.py --dims test cpu    # 只量指定维度
    python3 tools/calibrate.py --max-width 8      # 阶梯上限
    python3 tools/calibrate.py --json             # 机读输出
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

_COORDINATOR_ROOT = Path(__file__).resolve().parents[1]
if str(_COORDINATOR_ROOT) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_ROOT))

from bg_coordinator.target import target_repo  # noqa: E402

# ---------------------------------------------------------------------------
# 机器底数
# ---------------------------------------------------------------------------


@dataclass
class Machine:
    cpus: int
    mem_total_mb: int
    mem_limit_mb: int | None
    load1: float
    mysql_ok: bool


def _read_first(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def detect_machine() -> Machine:
    cpus = os.cpu_count() or 1

    mem_total = 0
    for line in _read_first("/proc/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            mem_total = int(line.split()[1]) // 1024
            break

    limit: int | None = None
    for cand in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        raw = _read_first(cand)
        if raw and raw.isdigit():
            val = int(raw) // (1024 * 1024)
            # cgroup v2 用 "max" 表示不限；极大值也视为不限
            if 0 < val < 1_000_000:
                limit = val
            break

    load1 = 0.0
    raw_load = _read_first("/proc/loadavg")
    if raw_load:
        load1 = float(raw_load.split()[0])

    # 触库单元的可用性**以单元自身为准**（它要的是 pymysql ＋ alembic，
    # 不是 `mysql` 命令行）。这里只做一个便宜的"有没有库"的前置判断，
    # 真正的失败由串行基准暴露（那一维会被标成"量不出来"并给出原因）。
    mysql_ok = Path("/var/run/mysqld/mysqld.sock").exists()
    return Machine(
        cpus=cpus,
        mem_total_mb=mem_total,
        mem_limit_mb=limit,
        load1=load1,
        mysql_ok=mysql_ok,
    )


# ---------------------------------------------------------------------------
# CPU 采样
# ---------------------------------------------------------------------------


def cpu_snapshot() -> tuple[int, int]:
    """返回 (busy, total) 累计 jiffies。"""
    line = ""
    for ln in _read_first("/proc/stat").splitlines():
        if ln.startswith("cpu "):
            line = ln
            break
    if not line:
        return (0, 0)
    parts = [int(x) for x in line.split()[1:]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
    total = sum(parts)
    return (total - idle, total)


def mem_available_mb() -> int:
    for line in _read_first("/proc/meminfo").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return 0


# ---------------------------------------------------------------------------
# 负载：各维度的**单元命令**
# ---------------------------------------------------------------------------

#: 触库单元由 `db_ladder.py` 提供（建库 ＋ 全链迁移 ＋ 删库）。
#:
#: **这里曾经自己拼 `CREATE TABLE`**——那是合成负载：它量不到迁移链的长度，
#: 也量不到 DDL 元数据锁的等待。实测差一个数量级（合成 ~0.4s／件，
#: 真实全链 ~4.2s／件），于是 `db` 维被系统性看轻、W 被高估。
#: 现在单元只有一个来源，改口径改那一处。
DB_LADDER = Path(__file__).resolve().parent / "db_ladder.py"


# ---------------------------------------------------------------------------
# 测试维度：可用性探测
# ---------------------------------------------------------------------------


def test_python(repo: Path) -> str:
    """目标仓库的解释器：优先它自己的 venv（测试依赖都在那儿）。"""
    venv = repo / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def test_context(repo: Path) -> tuple[dict | None, str]:
    """测试维度的 ctx 与不可用原因。

    不可用就**明说原因**，不静默跳过——"量不了"和"量出来很宽"是两件事，
    把前者当后者就是把决策缺口悄悄填上一个默认值。
    """
    if not (repo / "tests").is_dir():
        return None, f"未找到测试目录：{repo / 'tests'}"
    missing = [f for unit in TEST_UNITS for f in unit if not (repo / f).exists()]
    if len(missing) == len({f for unit in TEST_UNITS for f in unit}):
        return None, f"测试面清单全部不存在（首个：{missing[0]}）"
    # 只保留文件齐全的单元：少一个文件会让整件必红，进而把 W 误判成 1
    units = tuple(u for u in TEST_UNITS if all((repo / f).exists() for f in u))
    return {"repo": repo, "python": test_python(repo), "units": units}, ""


def probe_test_units(ctx: dict) -> tuple[tuple[tuple[str, ...], ...], dict[str, str]]:
    """**先串行试一遍**，把本机本来就是红的测试面剔出去。

    为什么必须剔：这一维量的是"机器能同时跑几份测试"，
    **测试红不红是测试自己的事，不是机器上限**。留着它，
    每次并发都会让整档失败 → `safe_width` 被拉到 1 → 把测试的锅算到机器头上。

    剔掉的面**要报出来**（返回 dropped），不能静默吞掉。
    """
    kept: list[tuple[str, ...]] = []
    dropped: dict[str, str] = {}
    for i, unit in enumerate(ctx["units"]):
        ok, err, _ = run_one("test", i, ctx)
        if ok:
            kept.append(unit)
        else:
            dropped[unit[0]] = err or "非零退出"
    return tuple(kept), dropped


#: 内存单元分配量（MB）——要够大才量得到压力
MEM_UNIT_MB = 512
#: 磁盘单元读写量（MB）——`io` 维度的**机器极限**模式用；必须 fsync
IO_UNIT_MB = 64
#: 开发实际的小文件数量——`devio` 维度用。
#: 依据：本仓 git commit 实测 6~13ms，**开发不是大文件 IO**；
#: 用 64MB 写去量并行度会得出"磁盘是瓶颈"的**假结论**（实测它把 W 压到 1）。
DEV_FILE_COUNT = 200
#: CPU 单元的目标忙时。**必须显著大于进程启动开销**（实测 Python 冷启动约 100ms
#: 量级），否则量到的是"启动成本"而不是"并行收益"——那样每一档效率都会≈1。
CPU_UNIT_S = 0.6

#: 测试阶段的**工作单元**：每件 = 一个 dev 子代理该跑的**受影响面**快跑通道。
#: 单元**按线轮转**（job id 取模），模拟"不同线的子代理跑不同的测试面"。
#: 选档标准：全部 `not db`、无外部依赖、单件 1~4s（够长，量得到并行；够短，阶梯跑得起）。
#: 仓库里换测试面时改这里即可；文件缺失的单元会被自动跳过。
TEST_UNITS: tuple[tuple[str, ...], ...] = (
    ("tests/auth/test_domain_purity.py", "tests/auth/test_absolute_session_limit.py"),
    ("tests/data_access/test_analytics.py", "tests/data_access/test_contract_pull_throttle.py"),
    ("tests/broker_gateway/test_import_direction_gate.py", "tests/broker_gateway/test_td127_throttle.py"),
    ("tests/notification/test_template_codes.py", "tests/notification/test_route_config.py"),
    ("tests/infra/test_alloc_number.py",),
)

#: 测试单元只跑快跑通道。**真库档不得并发**（`docs/TESTING.md §3`：并发互踩＝假红），
#: 所以这条不是"保守"，是纪律。
TEST_MARKER = "not db"


def _unit_command(kind: str, idx: int = 0, ctx: dict | None = None) -> list[str]:
    """把一次工作单元变成一条**可并行启动**的命令行。

    统一成命令行是有意的：**只有并行启动才量得到并行**——
    早前的版本对非触库维度用了串行循环，量出来的是串行耗时，
    于是每个维度都被误判成"并行无用"（实测效率恒≈1）。

    `idx` 用于**测试维度**轮转测试面（不同线的子代理跑不同的面）；
    `ctx` 传仓库根与解释器（测试维度要的是**目标仓库**的 venv，不是本脚本的）。
    """
    if kind == "test":
        ctx = ctx or {}
        repo = Path(ctx["repo"])
        py = ctx.get("python") or sys.executable
        units = ctx.get("units") or TEST_UNITS
        unit = units[idx % len(units)]
        return [
            py,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-m",
            TEST_MARKER,
            *[str(repo / f) for f in unit],
        ]
    if kind == "cpu":
        code = (
            "import hashlib,time\n"
            "t=time.perf_counter(); h=hashlib.sha256()\n"
            "buf=bytes(range(256))*4096\n"
            f"while time.perf_counter()-t < {CPU_UNIT_S}:\n"
            "    h.update(buf)\n"
        )
        return [sys.executable, "-c", code]
    if kind == "mem":
        return [
            sys.executable,
            "-c",
            f"buf=bytearray({MEM_UNIT_MB}*1024*1024)\n"
            "for i in range(0,len(buf),4096): buf[i]=1\n"
            "import time; time.sleep(0.3)\n",
        ]
    if kind == "devio":
        # **贴近开发实际**：大量小文件的建/写/读/删——源码、__pycache__、
        # pytest 的 tmp 文件、日志，都是这个形态。git commit 实测 6~13ms。
        return [
            sys.executable,
            "-c",
            f"import os,tempfile,pathlib\n"
            f"d=pathlib.Path(tempfile.mkdtemp())\n"
            f"for i in range({DEV_FILE_COUNT}):\n"
            f"    q=d/f'f{{i}}.py'; q.write_text('x'*200)\n"
            f"    q.read_text(); q.stat()\n"
            f"for q in d.iterdir(): q.unlink()\n"
            f"d.rmdir()\n",
        ]
    if kind == "io":
        # **机器极限**：大文件写＋fsync。用来量磁盘本身，不代表开发的 IO 形态。
        return [
            sys.executable,
            "-c",
            f"import os,tempfile,pathlib\n"
            f"d=tempfile.mkdtemp(); p=pathlib.Path(d)/'b'\n"
            f"c=b'x'*(1024*1024)\n"
            f"f=p.open('wb')\n"
            f"[f.write(c) for _ in range({IO_UNIT_MB})]\n"
            f"f.flush(); os.fsync(f.fileno()); f.close()\n"
            f"p.read_bytes(); p.unlink(); os.rmdir(d)\n",
        ]
    # 触库：交给 db_ladder（建库 ＋ 全链迁移 ＋ 删库），**不在这里拼 SQL**
    ctx = ctx or {}
    repo = Path(ctx["repo"]) if ctx and ctx.get("repo") else target_repo(None)
    py = ctx.get("python") or sys.executable
    return [py, str(DB_LADDER), "--unit", "--repo", str(repo)]


def run_one(kind: str, idx: int = 0, ctx: dict | None = None) -> tuple[bool, str, float]:
    """跑一个工作单元，返回 (成功, 错误, 耗时秒)。

    **串行基准与并发档跑的是同一条命令**——早前基准走进程内的 `synthetic_unit`、
    档走命令行，两者只是"手工保持等价"：任何一处漂移都会让加速比的底数变成假的。
    现在只有一条路径，漂移在结构上不可能。
    """
    t0 = time.perf_counter()
    ok, err = _run_command(_unit_command(kind, idx, ctx), ctx)
    return ok, err, time.perf_counter() - t0


def _run_command(cmd: list[str], ctx: dict | None) -> tuple[bool, str]:
    """跑一条子进程命令；**cwd 必须是目标仓库**（pytest 的 rootdir/conftest 依赖它）。"""
    cwd = (ctx or {}).get("repo")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=600,
            check=False,
        )
    except OSError as exc:
        return False, str(exc)[:160]
    except subprocess.TimeoutExpired:
        return False, "timeout"
    if proc.returncode == 0:
        return True, ""
    tail = (proc.stderr or "").strip().splitlines()
    return False, (tail[-1][:160] if tail else f"exit {proc.returncode}")


# ---------------------------------------------------------------------------
# 阶梯
# ---------------------------------------------------------------------------


@dataclass
class Rung:
    width: int
    jobs: int
    wall_s: float
    cpu_ratio: float
    mem_low_mb: int
    ok: int
    failed: int
    per_job_ms: float
    throughput: float
    speedup: float
    #: **并发效率 = 实测串行墙钟 ÷ 本档墙钟**。
    #: `> 1` 并发确实更快；`< 1` **并发反而更慢**（锁竞争，触库路径的典型病征）。
    #: 底数是**实测**串行墙钟——不是 `jobs × 单件`（单件本身会因竞争而膨胀）。
    efficiency: float


@dataclass
class LadderResult:
    kind: str
    rungs: list[Rung] = field(default_factory=list)
    #: **并发效率的底数**——串行跑完一轮、各**成功件实测耗时之和**。
    #: 不用"jobs × 单件"推：单件耗时会随竞争膨胀，推出来的底数是假的。
    serial_s: float = 0.0
    #: 基线阶段的实测墙钟（含每件进程启动、含重试）——只作对照显示
    serial_wall_s: float = 0.0
    serial_ok: int = 0
    #: 基线里重试过的件数。**机器上常有别的会话在跑**，单次抖动不该判死一维。
    serial_retries: int = 0
    #: 非空 = 这一维**量不出来**（串行基准就失败），不得参与 `min(各维)`。
    #: 典型情形：测试面本来就是红的、单元文件不存在、触库不可用。
    #: 若不排除，一次"本来就红"会把 W 误判成 1——那是把测试的锅算到机器头上。
    invalid: str = ""

    #: 并发**相对串行**的最小增益：低于它说明并发已经不值当（大概率更慢）
    MIN_GAIN = 1.3
    #: 容差：墙钟在"全程最好"的这个比例以内，就算同一个水平（吸收单档抖动）
    WITHIN = 0.10

    def _scored(self) -> list[Rung]:
        """参与判定的档：跳过宽度 1（它就是串行基准，判它自相矛盾）。"""
        return [r for r in self.rungs if r.width > 1]

    @property
    def safe_width(self) -> int:
        """**安全宽度**：接近全程最好吞吐的**最窄档**。

        三条判据，按顺序生效：

        ① **并发相对串行必须有增益**（`efficiency ≥ 1.3`）——
           低于它说明并发反而更慢，那是锁竞争（触库路径的典型病征）；
        ② **排除不合法的档**（有失败即出局）；
        ③ 在剩下的档里取**墙钟最接近全程最小值**的**最窄**那个——
           `within` 容差默认 10%，用来吸收单档抖动。

        **为什么用"最窄的接近最好"而不是"逐档比较"**：
        逐档比较会被**单档噪声**骗——某一档恰好慢一点，就被误判成见顶。
        实测就踩过这个坑：宽度 2→3 抖了一次（0.68→0.65 本已变好，
        但另一轮是 0.59→0.48→0.47→0.48 判到 4 后又被 6 的抖动截断）。
        取全局最小再看谁接近它，判据对噪声不敏感。
        """
        valid = [
            r for r in self._scored() if not r.failed and r.efficiency >= self.MIN_GAIN
        ]
        if not valid:
            return 1
        best_wall = min(r.wall_s for r in valid)
        cutoff = best_wall * (1 + self.WITHIN)
        for r in sorted(valid, key=lambda x: x.width):
            if r.wall_s <= cutoff:
                return r.width
        return valid[0].width

    @property
    def elbow(self) -> Rung | None:
        """**第一个"不划算"的档**——安全宽度的下一档。"""
        safe = self.safe_width
        for r in self._scored():
            if r.width > safe:
                return r
        return None


def _chunk(seq: list[int], n: int) -> list[list[int]]:
    return [seq[i : i + n] for i in range(0, len(seq), n)]


#: 候选宽度台阶
CANDIDATE_WIDTHS: tuple[int, ...] = (1, 2, 3, 4, 6, 8, 12, 16, 24)


def widths_for(jobs: int, max_width: int) -> list[int]:
    """可用宽度 = **jobs 的因数**（且 ≤ 上限）。

    为什么必须整除：`_chunk` 把 jobs 件**分批**跑，最后一批不满宽时，
    这一档的墙钟里混进了"半宽运行"的时间，与别的档**不可比**。
    实测踩过：jobs=8 时宽度 3 的分批是 3+3+2，宽度 3 那档因此被系统性看慢。

    所以宁可少给几档（比如 jobs=12 才有 1/2/3/4/6/12），也不给不可比的档。
    """
    ws = [w for w in CANDIDATE_WIDTHS if w <= max_width and jobs % w == 0]
    return ws or [1]


def host_is_busy(m: Machine) -> bool:
    """机器上是否已有别的工作——**有负载时标出来的宽度不可信**。

    依据：本机实测，别的会话在跑 broker_gateway 测试时 load1≈4.6（4 核），
    同一维的墙钟在 8.3s~13.8s 之间乱跳，拐点直接被噪声推到最后。
    """
    return m.load1 > max(m.cpus * 0.8, 1.0)


def run_ladder(
    kind: str,
    widths: list[int],
    jobs_per_width: int,
    timeout_s: float,
    ctx: dict | None = None,
) -> LadderResult:
    """逐档跑：宽度 w 下同时跑 jobs_per_width 个工作单元。

    单档**先测基线**（宽度 1）再测宽度 w，才能算加速比。
    """
    res = LadderResult(kind=kind)

    # **先老实跑一遍串行**，作为并发效率的底数。
    t0 = time.perf_counter()
    serial_ok = 0
    serial_unit_s = 0.0
    retries = 0
    for i in range(jobs_per_width):
        good, _, dt = run_one(kind, i, ctx)
        if not good:
            # 机器上常有别的会话在跑测试——单次抖动不该判死一整维
            retries += 1
            good, _, dt = run_one(kind, i, ctx)
        if good:
            serial_ok += 1
            serial_unit_s += dt
    baseline_wall = time.perf_counter() - t0
    res.serial_s = round(serial_unit_s, 3)
    res.serial_wall_s = round(baseline_wall, 3)
    res.serial_ok = serial_ok
    res.serial_retries = retries
    if serial_ok < jobs_per_width:
        # 连重试都过不了 ⇒ 这一维量的是"本来就坏"，不是机器上限
        res.invalid = f"串行基准 {serial_ok}/{jobs_per_width} 件失败（已重试）"
        return res
    baseline_s = serial_unit_s

    for width in widths:
        ids = list(range(jobs_per_width))
        chunks = _chunk(ids, width)
        ok = failed = 0
        first_err = ""
        dur: list[float] = []

        busy0, tot0 = cpu_snapshot()
        mem_low = mem_available_mb()
        t0 = time.perf_counter()

        for group in chunks:
            procs: list[tuple[subprocess.Popen, float]] = []
            for idx in group:
                cmd = _unit_command(kind, idx, ctx)
                procs.append(
                    (
                        subprocess.Popen(
                            cmd,
                            cwd=str(ctx["repo"]) if ctx and ctx.get("repo") else None,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE,
                            text=True,
                        ),
                        time.perf_counter(),
                    )
                )
            for proc, started in procs:
                try:
                    _, err = proc.communicate(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    _, err = proc.communicate()
                    failed += 1
                    first_err = first_err or "timeout"
                    continue
                dur.append(time.perf_counter() - started)
                if proc.returncode == 0:
                    ok += 1
                else:
                    failed += 1
                    first_err = first_err or (err or "").strip()[:160]

        wall = time.perf_counter() - t0
        busy1, tot1 = cpu_snapshot()
        cpu_ratio = (busy1 - busy0) / (tot1 - tot0) if tot1 > tot0 else 0.0
        mem_low = min(mem_low, mem_available_mb())

        per_job_ms = statistics.mean(dur) * 1000 if dur else 0.0
        throughput = ok / wall if wall > 0 else 0.0
        speedup = (baseline_s / wall) if (baseline_s and wall > 0) else 1.0
        res.rungs.append(
            Rung(
                width=width,
                jobs=jobs_per_width,
                wall_s=round(wall, 3),
                cpu_ratio=round(cpu_ratio, 3),
                mem_low_mb=mem_low,
                ok=ok,
                failed=failed,
                per_job_ms=round(per_job_ms, 1),
                throughput=round(throughput, 3),
                speedup=round(speedup, 3),
                # 效率的底数是**实测串行墙钟**（见下 baseline_s）
                efficiency=round(speedup, 3),
            )
        )
        if failed and width > 1:
            # 已经打崩，再往上没意义
            break
    return res


# ---------------------------------------------------------------------------
# 各线占比
# ---------------------------------------------------------------------------


@dataclass
class LineLoad:
    line: str
    ready: int
    in_flight: int
    weight: int

    @property
    def share(self) -> float:  # 由外部归一
        return float(self.weight)


#: 终态——不再需要人力的状态
TERMINAL_STATUSES = {"accepted"}


def read_line_load(
    tasks: dict, dev_lines: list[str], in_flight_states: set[str]
) -> list[LineLoad]:
    """统计各线负载。

    **权重 = 未终结的活 ＋ 在办**。

    待办口径取**全部未终态**（已登记／分析中／已定稿），不只是"已定稿"——
    刚登记的需求也是这条线的活，只数已定稿会系统性低估。
    在办另计，因为它是**已占用**的资源，与"还有多少要做"是两件事。
    """
    out: list[LineLoad] = []
    for code in dev_lines:
        if code == "OPS":
            continue  # OPS 线亲办、不派 dev 子代理
        open_items = sum(
            1
            for t in tasks.values()
            if str(t.line) == code and str(t.status) not in TERMINAL_STATUSES
        )
        flying = sum(
            1
            for t in tasks.values()
            if str(t.line) == code
            and str(t.status) in in_flight_states
            and str(t.status) not in TERMINAL_STATUSES
        )
        pending = max(open_items - flying, 0)
        out.append(
            LineLoad(line=code, ready=pending, in_flight=flying, weight=open_items)
        )
    return out


def suggest(loads: list[LineLoad], safe_width: int) -> dict[str, int]:
    """**按占比分配**：预算 = 安全宽度 × 该线占比，向下取整，有活的线至少 1。

    线是工作面、工作量不均 ⇒ 预算必须跟着工作量走，不能一刀切。
    """
    active = [x for x in loads if x.weight > 0]
    if not active:
        return {}
    total = sum(x.weight for x in active)
    out: dict[str, int] = {}
    for x in active:
        raw = safe_width * x.weight / total
        out[x.line] = max(1, int(raw))
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="逐线预算标定器（只出建议，不写配额）")
    ap.add_argument("--max-width", type=int, default=0, help="阶梯上限；0 = 机器核数")
    ap.add_argument("--jobs", type=int, default=12, help="每档跑多少个工作单元（宽度取其因数）")
    ap.add_argument("--timeout", type=float, default=120.0, help="单个工作单元超时秒")
    ap.add_argument("--probe-only", action="store_true", help="只量机器，不做阶梯")
    ap.add_argument(
        "--dims", nargs="+", default=None,
        choices=["db", "test", "cpu", "mem", "io", "devio"],
        help="只量这些维度（默认全量；test 是最接近真实的一档）",
    )
    ap.add_argument("--no-db", action="store_true", help="不触库，用退化负载验证阶梯机制")
    ap.add_argument(
        "--repo", default=None,
        help="目标仓库根（test 维度跑它的测试；相对本脚本所在目录解析）",
    )
    ap.add_argument("--coordinator-root", default=os.environ.get("BG_COORDINATOR_ROOT", ""))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    repo = target_repo(args.repo)
    test_ctx, test_why = test_context(repo)
    dropped_units: dict[str, str] = {}
    if test_ctx and not args.probe_only:
        # 只留**本机本来就是绿的**测试面：红是测试的问题，不是机器上限
        kept, dropped_units = probe_test_units(test_ctx)
        if not kept:
            test_ctx, test_why = None, f"测试面全部为红（{len(dropped_units)} 件）"
        else:
            test_ctx["units"] = kept

    m = detect_machine()
    max_width = args.max_width or m.cpus * 2
    widths = widths_for(args.jobs, max_width)

    # 要量哪些维度：`--dims` 显式给；否则按可用性全量（db 需 MySQL、test 需测试面）
    dims = list(args.dims or [])
    if not dims:
        # 默认量**贴近实际**的维度：磁盘用 `devio`（小文件），不是 `io`（大文件）
        dims = ["cpu", "mem", "devio"]
        if m.mysql_ok and not args.no_db:
            dims.append("db")
        if test_ctx:
            dims.append("test")
    if "test" in dims and not test_ctx:
        print(f"测试维度不可用：{test_why}", file=sys.stderr)
        return 2
    if "db" in dims and not m.mysql_ok and not args.no_db:
        print("db 维度不可用：MySQL 连不上", file=sys.stderr)
        return 2

    report: dict = {
        "within": LadderResult.WITHIN,
        "machine": asdict(m),
        "dims": dims,
        "widths": widths,
        "jobs": args.jobs,
        "repo": str(repo),
        # 有别的会话在跑时标出来的宽度不可信——**要报出来，不能假装准确**
        "busy": host_is_busy(m),
    }
    if not test_ctx:
        report["test_dim_unavailable"] = test_why
    elif dropped_units:
        report["test_units_dropped"] = dropped_units

    if not args.probe_only:
        # 触库单元的 ctx：**用目标仓的 venv**（pymysql／alembic 都在那儿）
        db_ctx = {"repo": repo, "python": test_python(repo)}
        ladders: dict[str, LadderResult] = {}
        for dim in dims:
            kind = "mysql" if dim == "db" else dim
            ctx = test_ctx if dim == "test" else (db_ctx if dim == "db" else None)
            ladders[dim] = run_ladder(kind, widths, args.jobs, args.timeout, ctx)

        report["ladders"] = {
            d: {
                "serial_s": lad.serial_s,
                "serial_wall_s": lad.serial_wall_s,
                "serial_ok": lad.serial_ok,
                "serial_retries": lad.serial_retries,
                "safe_width": lad.safe_width,
                "invalid": lad.invalid,
                "rungs": [asdict(r) for r in lad.rungs],
            }
            for d, lad in ladders.items()
        }

        # **W = 各维度安全宽度的最小者**——最紧的那个瓶颈定上限。
        # **量不出来的维度不参与**：串行就红的维度量的是"本来就坏"，不是机器上限。
        report["invalid_dims"] = {d: lad.invalid for d, lad in ladders.items() if lad.invalid}
        per_dim = {d: lad.safe_width for d, lad in ladders.items() if not lad.invalid}
        report["per_dim"] = per_dim
        report["bottleneck"] = min(per_dim, key=lambda d: per_dim[d]) if per_dim else None
        report["safe_width"] = min(per_dim.values()) if per_dim else 1

        # 各线占比 → 建议预算
        loads: list[LineLoad] = []
        if args.coordinator_root:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            try:
                from bg_coordinator.storage import Store  # noqa: PLC0415

                st = Store(root=Path(args.coordinator_root))
                state = st.load_state(strict=False)
                loads = read_line_load(
                    state.tasks,
                    list(state.dev_lines),
                    {"claimed", "in_progress", "delivered", "verified"},
                )
            except Exception as exc:  # noqa: BLE001 - 标定器不应因读状态失败而崩
                report["state_error"] = str(exc)
        report["lines"] = [asdict(x) for x in loads]
        report["suggested"] = suggest(loads, report["safe_width"])

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    _render(report, m)
    return 0


def _render(report: dict, m: Machine) -> None:
    print("═══ 逐线预算标定 ═══")
    print()
    print("机器")
    cap = f"{m.mem_total_mb} MB" + (f"（cgroup 上限 {m.mem_limit_mb} MB）" if m.mem_limit_mb else "")
    print(f"  核数    {m.cpus}")
    print(f"  内存    {cap}")
    print(f"  负载    {m.load1}（1 分钟）")
    print(f"  MySQL   {'可用' if m.mysql_ok else '不可用'}")

    if "ladders" not in report:
        print()
        print("（--probe-only：未做阶梯）")
        return

    labels = {
        "db": "触库（建库＋全链迁移＋删库）",
        "test": "测试（真实受影响面·快跑通道）",
        "cpu": "CPU",
        "mem": "内存",
        "devio": "磁盘（开发形态·小文件）",
        "io": "磁盘（机器极限·大文件）",
    }
    print()
    print("**瓶颈不止一个。** 每个维度各量一遍，取最小者为上限。")
    print(f"  阶梯宽度 {report.get('widths')}（jobs={report.get('jobs')} 的因数——只有整除才可跨档比较）")
    if report.get("busy"):
        print()
        print("⚠⚠ **机器上已有别的负载**（load1 相对核数偏高）——本次阶梯的宽度**不可信**：")
        print("    并发墙钟会被别的会话推着乱跳，拐点常被噪声推到最后一档。")
        print("    请在**没有其它会话跑测试**时重标（真库档尤其不得并发）。")
    dropped = report.get("test_units_dropped") or {}
    if dropped:
        print()
        print("⚠ 这些测试面**本机就是红的**，已从「测试」维剔除（红是测试的问题，不是机器上限）：")
        for f, why in dropped.items():
            print(f"    {f} —— {why}")
    for dim, data in report["ladders"].items():
        print()
        print(f"── {labels.get(dim, dim)} ── 串行底数 {data['serial_s']:.2f}s")
        if data.get("invalid"):
            print(f"  ⚠ **这一维量不出来**：{data['invalid']}")
            print("    不计入 min(各维)——它量的是「本来就坏」，不是机器上限。")
            continue
        if data.get("serial_retries"):
            print(
                f"  （基线重试 {data['serial_retries']} 件——机器上还有别的会话在跑；"
                f"基线墙钟 {data['serial_wall_s']:.2f}s）"
            )
        if dim == "test":
            print(f"  （口径：`pytest -m \"{TEST_MARKER}\"`。真库档不得并发，故不在本维）")
        print(
            f"  {'宽度':>4} {'墙钟s':>7} {'每件ms':>8} {'并发效率':>8} "
            f"{'CPU':>6} {'可用内存MB':>10} {'成功':>5} {'失败':>5}"
        )
        safe = data["safe_width"]
        for r in data["rungs"]:
            mark = "  ← 该维安全宽度" if r["width"] == safe else ""
            print(
                f"  {r['width']:>4} {r['wall_s']:>7.2f} {r['per_job_ms']:>8.1f} "
                f"{r['efficiency']:>8.2f} {r['cpu_ratio']:>6.2f} "
                f"{r['mem_low_mb']:>10} {r['ok']:>5} {r['failed']:>5}{mark}"
            )
        print(f"  该维安全宽度 = {safe}")

    per_dim = report.get("per_dim") or {}
    widths = report.get("widths") or []
    print()
    print("各维安全宽度")
    for dim, w in sorted(per_dim.items(), key=lambda kv: kv[1]):
        tag = "  ← **瓶颈**" if dim == report.get("bottleneck") else ""
        print(f"  {labels.get(dim, dim):<24} {w}{tag}")
    for dim, why in (report.get("invalid_dims") or {}).items():
        print(f"  {labels.get(dim, dim):<24} 量不出（{why}）")
    if report.get("test_dim_unavailable"):
        print(f"  {labels['test']:<24} 未纳入（{report['test_dim_unavailable']}）")

    w = report["safe_width"]
    print()
    bn = report.get("bottleneck")
    if bn:
        print(f"**W = min(各维) = {w}**（{labels.get(bn, bn)} 先到顶）")
    else:
        print("**无一维量得出来**——W 无依据，不得据此写配额。")
    if widths and w >= max(widths):
        print("  ⚠ **在测试上限触顶**——未见拐点，说明还能更宽。")
        print(f"    想找真拐点请加大上限：--max-width {max(widths) * 2}")

    lines = report.get("lines") or []
    if lines:
        print()
        print("各线负载（权重 = 未终结的活 ＋ 在办）")
        print(f"  {'线':<6} {'待办':>5} {'在办':>5} {'权重':>5}")
        for x in lines:
            print(f"  {x['line']:<6} {x['ready']:>5} {x['in_flight']:>5} {x['weight']:>5}")

    suggested = report.get("suggested") or {}
    print()
    if suggested and report.get("busy"):
        print("⚠ **机器当时有别的负载**：下面的数字只作形状参考，**别照着写配额**，")
        print("  等空闲时重标一次再定。")
        print()
    if suggested:
        print(f"建议预算（W={w} × 该线占比，向下取整，有活的线至少 1）")
        for code, b in sorted(suggested.items()):
            print(f"  coord quota --line {code} --budget {b}")
        print()
        print("  另建议设总量上限兜共享资源：")
        print(f"  coord quota --total {max(w, 1)}")
    else:
        print("未读到协调器状态（用 --coordinator-root 指定），只给机器结论")

    print()
    print("**本脚本只出建议，不写配额。**")
    print("写配额是一个决策——线是工作面、工作量不均，")
    print("哪条线该配几个子代理，得由人拍板；脚本只把依据摆出来。")


if __name__ == "__main__":
    sys.exit(main())
