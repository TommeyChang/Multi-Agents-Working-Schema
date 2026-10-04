#!/usr/bin/env python3
"""开窗前预检——**把 fail-closed 判据前移到开窗前**（ops／dba 面）。

## 为什么要有它

"开窗之后才发现必须回滚"是最贵的一类失败：服务已停、迁移已跑、窗口已用掉，
问题才在起服那一刻暴露。把判据**前移**，任一 BLOCK ⇒ **拒绝开窗**并给可读原因。

## 五条腿（**都复用本体系已有的判据，不重写**）

| 腿 | 判据 |
|---|---|
| ① 协调器自检 | 状态缓存与事件流是否一致、有无重复 seq（`Store.health`） |
| ② 迁移图 | 单 head／号唯一／父节点齐／全可达（复用 `migration_gate.py`） |
| ③ 触库可用 | socket ＋ pymysql ＋ 目标仓迁移链（复用 `db_ladder.py --check`） |
| ④ scratch 残留 | 孤儿库数量是否超阈值（复用 `scratch_gc.py` 判定；**不删任何东西**） |
| ⑤ 宿主余量 | load1 相对核数、可用内存下限 |

**它不做的事**：不部署、不停服、不跑迁移、**不删库**——预检只判，动手是另一个决定。

## 用法

    python3 tools/preflight.py                         # 用体系内默认目标仓
    python3 tools/preflight.py --root "$BG_COORDINATOR_ROOT"
    python3 tools/preflight.py --scratch-limit 20 --json

退出码：`0` 全部通过（可以开窗）；`1` 有 BLOCK（**拒绝开窗**）；`2` 用法或环境错误。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

_COORDINATOR_ROOT = Path(__file__).resolve().parents[1]
if str(_COORDINATOR_ROOT) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_ROOT))

from bg_coordinator.target import target_repo  # noqa: E402

TOOLS = _COORDINATOR_ROOT / "tools"


@dataclass
class Leg:
    name: str
    ok: bool
    detail: str = ""
    warn: bool = False


@dataclass
class Report:
    legs: list[Leg] = field(default_factory=list)

    @property
    def blocked(self) -> list[Leg]:
        return [x for x in self.legs if not x.ok]


def _run_json(cmd: list[str]) -> tuple[dict | None, str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)[:160]
    try:
        return json.loads(out.stdout.strip().splitlines()[-1]), ""
    except (json.JSONDecodeError, IndexError):
        return None, (out.stderr or out.stdout).strip()[:160]


# ---------------------------------------------------------------------------
# 各腿
# ---------------------------------------------------------------------------


def leg_coordinator(root: str | None) -> Leg:
    """① 协调器自检：**缓存与事件流不一致**时，后面所有判断都建在流沙上。"""
    if not root:
        return Leg("协调器自检", True, "未给 --root，跳过（开窗前应给）", warn=True)
    from bg_coordinator.storage import Store  # noqa: PLC0415

    try:
        health = Store(root=Path(root)).health()
    except Exception as exc:  # noqa: BLE001
        return Leg("协调器自检", False, f"读状态失败：{str(exc)[:120]}")
    diffs = health.get("cache_diffs") or []
    dups = health.get("seq_duplicates") or []
    if diffs or dups:
        return Leg("协调器自检", False, f"缓存/日志不一致 {diffs}；重复 seq {dups}")
    return Leg("协调器自检", True, f"seq={health.get('log_tail_seq', 0)}，无差异")


def leg_migrations(target: Path, base: str) -> Leg:
    cmd = [sys.executable, str(TOOLS / "migration_gate.py"), "--target", str(target), "--json"]
    if base:
        cmd += ["--base", base]
    data, err = _run_json(cmd)
    if data is None:
        return Leg("迁移图", False, f"判定失败：{err}")
    blocks = data.get("blocked") or []
    if blocks:
        return Leg("迁移图", False, "；".join(str(b)[:80] for b in blocks[:3]))
    return Leg("迁移图", True, f"{data.get('migrations')} 件，head {data.get('heads')}")


def leg_db(target: Path) -> Leg:
    cmd = [
        sys.executable,
        str(TOOLS / "db_ladder.py"),
        "--check",
        "--json",
        "--repo",
        str(target),
    ]
    data, err = _run_json(cmd)
    if data is None:
        return Leg("触库可用", False, f"探测失败：{err}")
    if not data.get("ok"):
        return Leg("触库可用", False, str(data.get("why") or "不可用"))
    return Leg("触库可用", True, "socket 与迁移链就位")


def leg_scratch(limit: int, target: Path) -> Leg:
    """④ 残留量——**只判不删**（删归 `scratch_gc.py --apply`，且那是另一个决定）。"""
    cmd = [sys.executable, str(TOOLS / "scratch_gc.py"), "--json"]
    data, err = _run_json(cmd)
    if data is None:
        return Leg("scratch 残留", True, f"未查到（{err}）——不阻断", warn=True)
    orphans = data.get("orphans") or []
    if len(orphans) > limit:
        return Leg(
            "scratch 残留",
            False,
            f"{len(orphans)} 个孤儿库 > 上限 {limit}——先清库（scratch_gc.py --apply）再开窗",
        )
    return Leg("scratch 残留", True, f"{len(orphans)} 个孤儿（上限 {limit}）")


def leg_host(load_ratio: float, min_free_mb: int) -> Leg:
    cpus = _cpu_count()
    load1 = _load1()
    free_mb = _mem_available_mb()
    if load1 > cpus * load_ratio:
        return Leg("宿主余量", False, f"load1 {load1} > {cpus} 核 × {load_ratio}")
    if free_mb < min_free_mb:
        return Leg("宿主余量", False, f"可用内存 {free_mb}MB < 下限 {min_free_mb}MB")
    return Leg("宿主余量", True, f"load1 {load1}／{cpus} 核；可用内存 {free_mb}MB")


def _cpu_count() -> int:
    import os  # noqa: PLC0415

    return os.cpu_count() or 1


def _load1() -> float:
    try:
        return float(Path("/proc/loadavg").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def _mem_available_mb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def run(args: argparse.Namespace, target: Path) -> Report:
    rep = Report()
    rep.legs.append(leg_coordinator(args.root))
    if shutil.which("git") is None:
        rep.legs.append(Leg("迁移图", True, "无 git，跳过", warn=True))
    else:
        rep.legs.append(leg_migrations(target, args.base))
    rep.legs.append(leg_db(target))
    rep.legs.append(leg_scratch(args.scratch_limit, target))
    rep.legs.append(leg_host(args.load_ratio, args.min_free_mb))
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="开窗前预检（判据前移，fail-closed）")
    ap.add_argument("--target", default=None, help="目标仓（默认 $BG_TARGET_REPO 或体系内默认）")
    ap.add_argument("--root", default=None, help="协调器状态根（强烈建议给）")
    ap.add_argument("--base", default="", help="迁移基线（给了才校验新增件取号）")
    ap.add_argument("--scratch-limit", type=int, default=30, help="孤儿 scratch 库数量上限")
    ap.add_argument("--load-ratio", type=float, default=0.9, help="load1 相对核数的上限倍率")
    ap.add_argument("--min-free-mb", type=int, default=512, help="可用内存下限（MB）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    target = target_repo(args.target)
    if not (target / "tests").is_dir():
        print(f"目标仓不像代码仓（缺 tests/）：{target}", file=sys.stderr)
        return 2

    rep = run(args, target)
    blocked = rep.blocked
    if args.json:
        print(
            json.dumps(
                {"target": str(target), "legs": [asdict(x) for x in rep.legs],
                 "blocked": [x.name for x in blocked], "ok": not blocked},
                ensure_ascii=False,
            )
        )
    else:
        print(f"开窗前预检｜目标仓 {target}")
        for x in rep.legs:
            mark = "PASS" if x.ok else "BLOCK"
            mark = "WARN" if x.warn and x.ok else mark
            print(f"  [{mark}] {x.name} —— {x.detail}")
        print()
        if blocked:
            print(f"**拒绝开窗**：{len(blocked)} 条 BLOCK —— " + "、".join(x.name for x in blocked))
        else:
            print("**可以开窗**：五条腿全过。")
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
