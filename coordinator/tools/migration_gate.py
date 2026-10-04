#!/usr/bin/env python3
"""迁移闸——**本体系自持**：静态读目标仓的迁移图，判它合不合规。

**判据不在这里**：它在 `bg_coordinator/migrations.py`（系统模块）——
因为同一条判据要在**三个收口点**用：**放号**（协调器号段）、**提交**（`commit_gate.py`）、
**合并**（`merge.py` 的链位闸）。判据只写一处，否则三个点上的口径会各自漂移——
而迁移恰恰最不能漂：链位一旦分叉，两个迁移件都"合法"却谁也升不到头。

本件是它的**命令行外壳**：定位目标仓、读图、判、报。判据本身不连库、不执行迁移。

用法：

    python3 tools/migration_gate.py                        # 用体系内默认目标仓
    python3 tools/migration_gate.py --base origin/main      # 校验相对基线的新增件（收口的关键一条）
    python3 tools/migration_gate.py --root <协调器状态根>    # 追加簿记比对
    python3 tools/migration_gate.py --json

退出码：`0` 全过；`1` 有 BLOCK；`2` 用法或环境错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_COORDINATOR_ROOT = Path(__file__).resolve().parents[1]
if str(_COORDINATOR_ROOT) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_ROOT))

from bg_coordinator import migrations as mig  # noqa: E402
from bg_coordinator.binding import load as load_binding  # noqa: E402
from bg_coordinator.binding import locate as locate_binding  # noqa: E402
from bg_coordinator.target import target_repo  # noqa: E402

#: 迁移目录的兜底值（正经来源是工程绑定 §迁移 的 `dir`）
FALLBACK_VERSIONS_DIR = "alembic/versions"


def versions_dir_of(target: Path) -> str:
    """迁移目录**从工程绑定取**（工程落点归工程）；取不到用兜底值。"""
    _src, path = locate_binding(target)
    if path is not None:
        data, _err = load_binding(path)
        if data:
            m = data.get("migrations")
            if isinstance(m, dict) and isinstance(m.get("dir"), str) and m["dir"].strip():
                return str(m["dir"])
    return FALLBACK_VERSIONS_DIR


def base_of(target: Path, given: str) -> str:
    """基线的正经来源是绑定 §迁移 的 `base`（逐工程不同：有的用 origin/main，有的用别的）。"""
    if given:
        return given
    _src, path = locate_binding(target)
    if path is not None:
        data, _err = load_binding(path)
        if data:
            m = data.get("migrations")
            if isinstance(m, dict) and isinstance(m.get("base"), str) and m["base"].strip():
                return str(m["base"])
    return ""


def ledger_crosscheck(rows: list[mig.Migration], root: str) -> list[str]:
    """与协调器号账目比对：件在盘上却无账 ⇒ 未取号；账上 pending 却已有件 ⇒ 该落物。"""
    from bg_coordinator.storage import Store  # noqa: PLC0415

    state = Store(root=Path(root)).load_state(strict=False)
    alloc = {
        int(e.get("number")): e
        for e in state.allocations
        if str(e.get("family")) == "alembic" and e.get("number") is not None
    }
    notes: list[str] = []
    for r in rows:
        if r.number is None:
            continue
        entry = alloc.get(r.number)
        if entry is None:
            notes.append(f"[6] 迁移件 {r.path}（号 {r.number}）在协调器账上查无记录——未取号")
        elif str(entry.get("state")) == "pending" and not str(entry.get("id") or ""):
            notes.append(f"[6] 号 {r.number} 账上是待建，但件已在盘上（{r.path}）——该落物")
    return notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="迁移闸（本体系自持：静态判图与号）")
    ap.add_argument("--target", default=None, help="目标仓（默认 $BG_TARGET_REPO 或体系内默认）")
    ap.add_argument("--base", default="", help="基线（不给则取绑定 §迁移 的 base；两者皆无则不判新增件）")
    ap.add_argument("--root", default="", help="协调器状态根（给了才追加簿记比对）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    target = target_repo(args.target)
    vdir = versions_dir_of(target)
    base = base_of(target, args.base)
    rows = mig.load_migrations(target, vdir)
    base_rows: list[mig.Migration] = []
    if base:
        base_rows, err = mig.load_from_git(target, base, vdir)
        if err:
            print(err, file=sys.stderr)
            return 2

    blocks = mig.judge(rows, base_rows, vdir)
    if args.root:
        try:
            blocks += ledger_crosscheck(rows, args.root)
        except Exception as exc:  # noqa: BLE001 - 读不到状态要说清楚，不崩
            print(f"簿记比对跳过：{str(exc)[:160]}", file=sys.stderr)

    if args.json:
        print(
            json.dumps(
                {
                    "target": str(target),
                    "versions_dir": vdir,
                    "base": base,
                    "migrations": len(rows),
                    "heads": mig.heads_of(rows),
                    "blocked": blocks,
                    "ok": not blocks,
                },
                ensure_ascii=False,
            )
        )
    else:
        print(
            f"迁移闸｜目标仓 {target}｜目录 {vdir}｜件数 {len(rows)}｜head {mig.heads_of(rows)}"
            + (f"｜基线 {base}" if base else "")
        )
        if blocks:
            for b in blocks:
                print(f"  BLOCK {b}")
        else:
            print("  全过：号唯一、父节点齐、单 head、全可达" + ("、新增件取号合规" if base else ""))
    return 1 if blocks else 0


if __name__ == "__main__":
    sys.exit(main())
