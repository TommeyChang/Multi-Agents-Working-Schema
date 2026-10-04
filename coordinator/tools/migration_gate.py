#!/usr/bin/env python3
"""迁移闸——**本体系自持**：静态读目标仓的迁移图，判它合不合规。

含 `alembic/versions/**` 改动的条目，合入前必过这道闸。判据：

| # | 判据 | 为什么 |
|---|---|---|
| 1 | `revision` **唯一** | 撞号之后 downgrade／定位全是掷骰子 |
| 2 | `down_revision` **指向的件存在** | 悬空父节点＝图断了，迁移链跑不到头 |
| 3 | **恰有一个 head**（没人以它为父） | 两个 head ⇒ 谁也不知道该升到哪个 |
| 4 | **从 head 全可达** | 有孤儿件＝那件永远不会被执行（静默失效） |
| 5 | 新增件**取号单调**：编号 > 基线最大号，且父节点 == 基线 head | 号是资源；插队与「重新分叉」都是事故 |
| 6 | （可选）与协调器**簿记比对** | 号在账上却无件、或件在盘上却无账 |

## 它不做什么

**不跑迁移、不连库、不改文件**——静态判据归本体系，**执行归 dba**。
也不是"迁移正确性"的证明：它只保证**图与号**这两件机械事实。

用法：

    python3 tools/migration_gate.py                        # 用体系内默认目标仓
    python3 tools/migration_gate.py --base origin/main      # 校验相对基线的新增件
    python3 tools/migration_gate.py --root <协调器状态根>    # 追加簿记比对
    python3 tools/migration_gate.py --json

退出码：`0` 全过；`1` 有 BLOCK；`2` 用法或环境错误。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_COORDINATOR_ROOT = Path(__file__).resolve().parents[1]
if str(_COORDINATOR_ROOT) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_ROOT))

from bg_coordinator.target import target_repo  # noqa: E402

VERSIONS_DIR = "alembic/versions"

_REVISION = re.compile(r"^revision(?:\s*:\s*[^=\n]+)?\s*=\s*['\"]([^'\"]+)['\"]", re.M)
_DOWN_REVISION = re.compile(r"^down_revision(?:\s*:\s*[^=\n]+)?\s*=\s*(.+)$", re.M)
_QUOTED = re.compile(r"['\"]([^'\"]+)['\"]")
_NUMERIC = re.compile(r"^(\d+)")


@dataclass
class Migration:
    path: str
    revision: str
    downs: tuple[str, ...]

    @property
    def number(self) -> int | None:
        m = _NUMERIC.match(self.revision)
        return int(m.group(1)) if m else None


def _parse_downs(text: str) -> tuple[str, ...]:
    """取 `down_revision` 的值：单值或元组，`None` 即无父。"""
    m = _DOWN_REVISION.search(text)
    if not m:
        return ()
    rhs = m.group(1).strip()
    if rhs[:1] in "([":
        return tuple(_QUOTED.findall(rhs))
    q = _QUOTED.search(rhs)
    return (q.group(1),) if q else ()


def parse_migration(path: Path, rel: str) -> Migration | None:
    """正则取 `revision`／`down_revision`——**不 import 迁移件**（那是执行任意代码）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    rev = _REVISION.search(text)
    if not rev:
        return None
    return Migration(path=rel, revision=rev.group(1), downs=_parse_downs(text))


def load_migrations(target: Path) -> list[Migration]:
    root = target / VERSIONS_DIR
    if not root.is_dir():
        return []
    out: list[Migration] = []
    for f in sorted(root.rglob("*.py")):
        if f.name.startswith("__"):
            continue
        m = parse_migration(f, str(f.relative_to(target)))
        if m:
            out.append(m)
    return out


def _git_show_migrations(target: Path, base: str) -> tuple[list[Migration], str]:
    """基线里的迁移图——`git show <base>:<path>` 逐个取，不切工作区。"""
    ls = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", base, VERSIONS_DIR],
        cwd=target,
        capture_output=True,
        text=True,
        check=False,
    )
    if ls.returncode != 0:
        return [], f"基线不可用：{base}"
    out: list[Migration] = []
    for rel in [ln.strip() for ln in ls.stdout.splitlines() if ln.strip().endswith(".py")]:
        show = subprocess.run(
            ["git", "show", f"{base}:{rel}"], cwd=target, capture_output=True, text=True, check=False
        )
        if show.returncode != 0:
            continue
        rev = _REVISION.search(show.stdout)
        if not rev:
            continue
        downs = _parse_downs(show.stdout)
        out.append(Migration(path=rel, revision=rev.group(1), downs=downs))
    return out, ""


# ---------------------------------------------------------------------------
# 判据
# ---------------------------------------------------------------------------


def heads_of(rows: list[Migration]) -> list[str]:
    parents = {d for r in rows for d in r.downs}
    return sorted(r.revision for r in rows if r.revision not in parents)


def judge(rows: list[Migration], base_rows: list[Migration]) -> list[str]:
    """返回 BLOCK 清单（空 = 全过）。"""
    blocks: list[str] = []
    if not rows:
        return [f"迁移目录里没有可解析的迁移件：{VERSIONS_DIR}"]

    seen: dict[str, str] = {}
    for r in rows:
        if r.revision in seen:
            blocks.append(f"[1] revision 撞号 {r.revision}：{seen[r.revision]} 与 {r.path}")
        else:
            seen[r.revision] = r.path
    known = set(seen)
    for r in rows:
        for d in r.downs:
            if d not in known:
                blocks.append(f"[2] {r.path} 的 down_revision={d} 在盘上不存在（悬空父节点）")

    heads = heads_of(rows)
    if len(heads) > 1:
        blocks.append(f"[3] 有 {len(heads)} 个 head：{heads}——升级目标不唯一")
    elif not heads:
        blocks.append("[3] 没有 head——图里有环或全部互为父节点")

    # 从 head 沿 downs 走，看能不能覆盖全部（不能被覆盖的就是孤儿）
    if len(heads) == 1:
        by_rev = {r.revision: r for r in rows}
        stack, reachable = [heads[0]], set()
        while stack:
            cur = stack.pop()
            if cur in reachable or cur not in by_rev:
                continue
            reachable.add(cur)
            stack.extend(by_rev[cur].downs)
        orphans = sorted(known - reachable)
        if orphans:
            blocks.append(f"[4] 从 head 不可达（永远不会被执行）：{orphans}")

    if base_rows:
        base_nums = [r.number for r in base_rows if r.number is not None]
        base_max = max(base_nums) if base_nums else 0
        base_head = heads_of(base_rows)
        base_revs = {r.revision for r in base_rows}
        for r in rows:
            if r.revision in base_revs:
                continue
            if r.number is None:
                blocks.append(f"[5] 新增件 {r.path} 的 revision 不以数字开头：{r.revision}")
                continue
            if r.number <= base_max:
                blocks.append(
                    f"[5] 新增件 {r.path} 取号 {r.revision} 不大于基线最大号 {base_max}——插队"
                )
            if len(base_head) == 1 and r.downs != (base_head[0],):
                blocks.append(
                    f"[5] 新增件 {r.path} 的 down_revision={list(r.downs)} 不等于基线 head "
                    f"{base_head[0]}——重新分叉"
                )
    return blocks


def ledger_crosscheck(target: Path, rows: list[Migration], root: str) -> list[str]:
    """与协调器号账目比对：件在盘上却无账 ⇒ 未取号；账上有 pending 却已有件 ⇒ 该落物。"""
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
    ap.add_argument("--base", default="", help="基线（给了才校验新增件的取号与父节点）")
    ap.add_argument("--root", default="", help="协调器状态根（给了才追加簿记比对）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    target = target_repo(args.target)
    rows = load_migrations(target)
    base_rows: list[Migration] = []
    if args.base:
        base_rows, err = _git_show_migrations(target, args.base)
        if err:
            print(err, file=sys.stderr)
            return 2

    blocks = judge(rows, base_rows)
    if args.root:
        try:
            blocks += ledger_crosscheck(target, rows, args.root)
        except Exception as exc:  # noqa: BLE001 - 读不到状态要说清楚，不崩
            print(f"簿记比对跳过：{str(exc)[:160]}", file=sys.stderr)

    if args.json:
        print(
            json.dumps(
                {
                    "target": str(target),
                    "migrations": len(rows),
                    "heads": heads_of(rows),
                    "blocked": blocks,
                    "ok": not blocks,
                },
                ensure_ascii=False,
            )
        )
    else:
        print(f"迁移闸｜目标仓 {target}｜件数 {len(rows)}｜head {heads_of(rows)}")
        if blocks:
            for b in blocks:
                print(f"  BLOCK {b}")
        else:
            print("  全过：号唯一、父节点齐、单 head、全可达" + ("、新增件取号合规" if args.base else ""))
    return 1 if blocks else 0


if __name__ == "__main__":
    sys.exit(main())
