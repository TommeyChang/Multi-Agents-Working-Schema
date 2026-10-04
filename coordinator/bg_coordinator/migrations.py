"""迁移链判据——**系统模块**：静态读图的机械事实，不连库、不执行迁移。

## 为什么在系统侧而不是工具侧

同一条判据要在**三个收口点**用（放号／提交／合并）。
判据只写一处，否则三个点上的口径会各自漂移——而迁移恰恰是最不能漂的东西：
链位一旦分叉，两个迁移件都"合法"却谁也升不到头。

## 判据

| # | 判据 | 为什么 |
|---|---|---|
| 1 | `revision` **唯一** | 撞号之后 downgrade／定位全是掷骰子 |
| 2 | 父节点**指向的件存在** | 悬空父节点＝链断了 |
| 3 | **恰有一个 head** | 两个 head ⇒ 谁也不知道该升到哪个 |
| 4 | 从 head **全可达** | 孤儿件＝永远不会被执行（静默失效） |
| 5 | 新增件**取号单调**且父节点 == 基线 head | 号是资源；插队与"重新分叉"都是事故 |

**它不管的事**：不跑迁移、不改文件、不判降级保真——那些是执行面与工程口径。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

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


def parse_downs(text: str) -> tuple[str, ...]:
    """取 `down_revision` 的值：单值或元组，`None` 即无父。"""
    m = _DOWN_REVISION.search(text)
    if not m:
        return ()
    rhs = m.group(1).strip()
    if rhs[:1] in "([":
        return tuple(_QUOTED.findall(rhs))
    q = _QUOTED.search(rhs)
    return (q.group(1),) if q else ()


def parse_text(text: str, rel: str) -> Migration | None:
    """从**文本**解析一个迁移件——不 import（那是执行任意代码）。"""
    rev = _REVISION.search(text)
    if not rev:
        return None
    return Migration(path=rel, revision=rev.group(1), downs=parse_downs(text))


def parse_file(path: Path, rel: str) -> Migration | None:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    return parse_text(text, rel)


def load_migrations(target: Path, versions_dir: str) -> list[Migration]:
    root = target / versions_dir
    if not root.is_dir():
        return []
    out: list[Migration] = []
    for f in sorted(root.rglob("*.py")):
        if f.name.startswith("__"):
            continue
        m = parse_file(f, str(f.relative_to(target)))
        if m:
            out.append(m)
    return out


def load_from_git(target: Path, base: str, versions_dir: str) -> tuple[list[Migration], str]:
    """基线里的迁移图——`git show <base>:<path>` 逐个取，**不切工作区**。"""
    ls = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", base, versions_dir],
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
        m = parse_text(show.stdout, rel)
        if m:
            out.append(m)
    return out, ""


def heads_of(rows: list[Migration]) -> list[str]:
    parents = {d for r in rows for d in r.downs}
    return sorted(r.revision for r in rows if r.revision not in parents)


def judge(rows: list[Migration], base_rows: list[Migration], versions_dir: str = "") -> list[str]:
    """返回 BLOCK 清单（空 = 全过）。**纯函数**：三个收口点共用同一条判据。"""
    blocks: list[str] = []
    if not rows:
        where = versions_dir or "迁移目录"
        return [f"{where} 里没有可解析的迁移件"]

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
        blocks.append(f"[3] 有 {len(heads)} 个 head：{heads}——**链位分叉**，升级目标不唯一")
    elif not heads:
        blocks.append("[3] 没有 head——图里有环或全部互为父节点")

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
                continue  # 基线里已有的件不判（历史件只受 [1]~[4] 约束）
            if r.number is None:
                blocks.append(f"[5] 新增件 {r.path} 的 revision 不以数字开头：{r.revision}")
                continue
            if r.number <= base_max:
                blocks.append(f"[5] 新增件 {r.path} 取号 {r.revision} 不大于基线最大号 {base_max}——插队")
            if len(base_head) == 1 and r.downs != (base_head[0],):
                blocks.append(
                    f"[5] 新增件 {r.path} 的父节点 {list(r.downs)} ≠ 基线 head {base_head[0]}"
                    "——**重新分叉**（别人先落地了，你要把父节点接到新 head 上）"
                )
    return blocks
