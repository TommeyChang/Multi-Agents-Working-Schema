#!/usr/bin/env python3
"""复杂度闸——**相对基线不退化**：radon 出数，判据在本体系。

## 为什么是"不退化"而不是"绝对阈值"

实测真仓存量（316 文件／3799 块）：**A 3278、B 362、C 133、D 22、E 3、F 1**。
一刀切阈值（例：不许有 C）会把 ~159 个**存量**块当场染红——闸立刻变噪声，
然后被集体绕过。**假红比没闸更坏**，所以首版判的是"**新欠的债**"：
本分支不许把复杂度推到档位以上，也不许把已有块推得更差。
存量债要靠单独的重构条目还，不靠闸来"宣布"。

## 判据：三份对照，与迁移 `[6]` 同一套归属纪律

对照系是**分叉点树**（`merge-base(base, HEAD)`），不是基线树：

| 情形 | 结论 |
|---|---|
| 块在分叉点没有，工作区有（新增） | 档位 ≥ floor ⇒ **BLOCK**；≥ warn_at ⇒ WARN |
| 块两边都有，**等级变差** | 新等级 ≥ floor ⇒ **BLOCK**；否则 ⇒ WARN |
| 块两边都有，等级没变差但复杂度上升（且 ≥ warn_at） | WARN（留痕，不拦） |
| 块变小／消失 | 不判——代码变简单是好事 |
| 分叉点不可判（无共同祖先／浅克隆） | **只提示不拦** |

**为什么对照分叉点而不是基线**：落后于基线时，基线那边的改动会被算成"本分支的"——
那正是主仓在迁移面打过两轮补丁的假红形态（T-D-136／T-D-157）。分叉点对照天然免疫：
本分支没动过的东西，两边一模一样。

键 = `路径::类型::限定名`（方法带类名）——**行号会漂，名字不会**。

## 边界

- 判据只看**顶层块**（函数／方法／类）；`closures`（嵌套函数）计入其父块，不单独判。
- radon **不在**跑这个工具的解释器里 ⇒ 退出码 2（**缺它即红**：声明了就得跑）。
- 本闸在**分支侧**判（dev／TL 在自己的 worktree 里跑 `tools/gate.py`，证据入档）；
  合并闸不重判复杂度——合入的判据是"这条分支的门禁证据里有没有它"。

用法：

    python3 tools/complexity.py --target <仓>                    # 口径从工程绑定取
    python3 tools/complexity.py --target <仓> --base origin/main --paths auth broker_gateway
    python3 tools/complexity.py --target <仓> --floor C --warn-at B --json

退出码：`0` 全过；`1` 有 BLOCK；`2` 用法或环境错误（含 radon 不可用）。
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

_COORDINATOR_ROOT = Path(__file__).resolve().parents[1]
if str(_COORDINATOR_ROOT) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_ROOT))

from bg_coordinator.binding import load as load_binding  # noqa: E402
from bg_coordinator.binding import locate as locate_binding  # noqa: E402
from bg_coordinator.target import target_repo  # noqa: E402

#: radon 的六档（A 最好）——`floor`／`warn_at` 都取这里的值
RANKS = "ABCDEF"

#: 口径兜底（正经来源是绑定 `gate.complexity`）：C 起拦、B 起告警
FALLBACK_FLOOR = "C"
FALLBACK_WARN_AT = "B"


def severity(rank: str) -> int:
    """档位 → 严重度（越大越差）；不认识的档位当最差，宁严不松。"""
    return RANKS.index(rank) if rank in RANKS else len(RANKS)


@dataclass(frozen=True)
class Block:
    """一个可判的块（函数／方法／类）。"""

    path: str
    type: str
    qualname: str
    rank: str
    complexity: int

    @property
    def key(self) -> str:
        return f"{self.path}::{self.type}::{self.qualname}"


def flatten(data: dict, only_paths: set[str] | None = None) -> dict[str, Block]:
    """radon JSON → 键 → 块（只取顶层项；`only_paths` 用来只留盘上还有的文件）。"""
    out: dict[str, Block] = {}
    for path, items in (data or {}).items():
        if only_paths is not None and path not in only_paths:
            continue
        for item in items or []:
            cls = item.get("classname") or ""
            name = f"{cls}.{item.get('name', '')}" if cls else str(item.get("name", ""))
            block = Block(
                path=path,
                type=str(item.get("type", "")),
                qualname=name,
                rank=str(item.get("rank", "F")),
                complexity=int(item.get("complexity", 0)),
            )
            out[block.key] = block
    return out


def run_radon(radon: list[str], cwd: Path, paths: list[str]) -> tuple[dict | None, str]:
    """跑 `radon cc -j`（**不 import radon**：判据留在本件，radon 只出数）。"""
    try:
        proc = subprocess.run(
            [*radon, "cc", "-j", *paths],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:  # 命令根本不存在（缺 radon）⇒ 干净报错，不是 traceback
        return None, f"命令跑不起来：{exc}"
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout).strip()[:300]
    try:
        return json.loads(proc.stdout or "{}"), ""
    except json.JSONDecodeError as exc:
        return None, f"radon 输出不是 JSON：{exc}"


def fork_ref(target: Path, base: str, ref: str) -> tuple[str, str]:
    """分叉点（`merge-base`）；判不了 ⇒ `("", 原因)`——**宁可少判，不误杀**。"""
    try:
        proc = subprocess.run(
            ["git", "merge-base", base, ref], cwd=target, capture_output=True, text=True, check=False
        )
    except OSError as exc:
        return "", f"git 跑不起来（{exc}）"
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not sha:
        return "", f"分叉点不可判（{base} 与 {ref} 无共同祖先／浅克隆）"
    return sha, ""


def fork_tree(target: Path, sha: str, paths: list[str]) -> tuple[Path | None, str]:
    """把分叉点的目标面**导出成临时树**（`git archive` → stdlib `tarfile`），再判。

    只调一次 git、不切工作区、不建 worktree：比逐个 `git show` 快，也不留痕。
    """
    try:
        proc = subprocess.run(
            ["git", "archive", "--format=tar", sha, "--", *paths],
            cwd=target,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return None, f"git 跑不起来（{exc}）"
    if proc.returncode != 0:
        return None, f"分叉点树导出失败：{proc.stderr.decode('utf-8', 'ignore').strip()[:160]}"
    tmp = Path(tempfile.mkdtemp(prefix="maws-cx-"))
    try:
        with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
            try:
                tar.extractall(tmp, filter="data")  # py3.12+：只解普通文件，不跟随链接
            except TypeError:  # 老解释器没有 filter 参数（内容来自本地 git 对象，可信）
                tar.extractall(tmp)
    except tarfile.TarError as exc:
        return None, f"分叉点树解包失败：{exc}"
    return tmp, ""


def judge(current: dict[str, Block], baseline: dict[str, Block], floor: str, warn_at: str):
    """ratchet 判据本体（**纯函数**）：返回 `(blocks, warns)`。"""
    blocks: list[str] = []
    warns: list[str] = []
    for key, now in sorted(current.items()):
        where = f"{now.path}:{now.qualname}"
        base = baseline.get(key)
        if base is None:
            if severity(now.rank) >= severity(floor):
                blocks.append(
                    f"[新增] {where} 新增块既已 {now.rank} 级（复杂度 {now.complexity}，"
                    f"档位 ≥ {floor}）——拆小或先立重构条目"
                )
            elif severity(now.rank) >= severity(warn_at):
                warns.append(f"[新增] {where} 新增块 {now.rank} 级（复杂度 {now.complexity}）")
            continue
        if severity(now.rank) > severity(base.rank):
            line = (
                f"[变差] {where} 由 {base.rank}（{base.complexity}）"
                f"变成 {now.rank}（{now.complexity}）"
            )
            if severity(now.rank) >= severity(floor):
                blocks.append(f"{line}——已到档位 ≥ {floor}，拆小或先立重构条目")
            else:
                warns.append(f"{line}——仍在档位之下")
        elif now.complexity > base.complexity and severity(now.rank) >= severity(warn_at):
            warns.append(
                f"[上升] {where} 复杂度 {base.complexity} → {now.complexity}"
                f"（等级仍为 {now.rank}）"
            )
    return blocks, warns


def distribution(blocks: dict[str, Block]) -> dict[str, int]:
    """档位分布——**绿的跑也要留这份数**，它才是"存量债有多大"的唯一证据。"""
    out = dict.fromkeys(RANKS, 0)
    for b in blocks.values():
        out[b.rank if b.rank in RANKS else "F"] += 1
    return out


def binding_complexity(target: Path) -> dict:
    """工程绑定 §门禁 的 `complexity` 落点（目录／基线／档位）；没有 ⇒ 空 dict。"""
    _src, path = locate_binding(target)
    if path is None:
        return {}
    data, _err = load_binding(path)
    if not data:
        return {}
    gate = data.get("gate") if isinstance(data.get("gate"), dict) else {}
    conf = gate.get("complexity")
    return conf if isinstance(conf, dict) else {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="复杂度闸（radon 出数，ratchet 判据在本体系）")
    ap.add_argument("--target", default=None, help="目标仓（默认 $BG_TARGET_REPO 或体系内默认）")
    ap.add_argument("--base", default="", help="基线（不给则取绑定 §门禁 complexity.base）")
    ap.add_argument("--paths", nargs="*", default=[], help="要判的路径（不给则取绑定的 paths）")
    ap.add_argument("--floor", default="", help=f"拦的档位（默认绑定或 {FALLBACK_FLOOR}）")
    ap.add_argument(
        "--warn-at", dest="warn_at", default="", help=f"告警档位（默认绑定或 {FALLBACK_WARN_AT}）"
    )
    ap.add_argument(
        "--radon", default="", help="radon 命令（默认用本解释器 `-m radon`；测试可注入桩）"
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    target = target_repo(args.target)
    conf = binding_complexity(target)
    base = args.base or str(conf.get("base") or "")
    paths = list(args.paths) or [str(p) for p in (conf.get("paths") or [])]
    floor = (args.floor or str(conf.get("floor") or FALLBACK_FLOOR)).upper()
    warn_at = (args.warn_at or str(conf.get("warn_at") or FALLBACK_WARN_AT)).upper()

    if not paths:
        print(
            "未声明要判的路径——`--paths` 没给，工程绑定 §门禁 `complexity.paths` 也没有："
            "判据落点不明，拒绝猜",
            file=sys.stderr,
        )
        return 2
    if floor not in RANKS or warn_at not in RANKS:
        print(f"档位只能是 {list(RANKS)}：floor={floor} warn_at={warn_at}", file=sys.stderr)
        return 2
    if not base:
        print(
            "未声明基线——`--base` 没给，工程绑定 §门禁 `complexity.base` 也没有："
            "没有对照系就判不出「新欠的债」",
            file=sys.stderr,
        )
        return 2

    radon = args.radon.split() if args.radon else [sys.executable, "-m", "radon"]
    current, err = run_radon(radon, target, paths)
    if current is None:
        print(f"radon 不可用（{' '.join(radon)}）：{err}", file=sys.stderr)
        print(
            "本工程声明了复杂度闸 ⇒ **缺它即红**（不是跳过）。"
            "装法见绑定 §门禁 complexity（本工程：产品仓 dev 依赖里已声明 radon）",
            file=sys.stderr,
        )
        return 2
    now = flatten(current)

    notes: list[str] = []
    # `baseline is None` 的语义是**判不了**（分叉点不可判／树导不出／那边 radon 失败），
    # 与"分叉点上确实一个块都没有"（`{}`，那说明全是新增）必须分清——
    # 混成一个值就会出现两种错：判不了时把所有块当新增（假红）、或该判时全放行（漏判）。
    baseline: dict[str, Block] | None = None
    sha, why = fork_ref(target, base, "HEAD")
    if not sha:
        notes.append(f"{why} ⇒ 只报分布，不判退化")
    else:
        tmp, terr = fork_tree(target, sha, paths)
        if tmp is None:
            notes.append(f"{terr} ⇒ 只报分布，不判退化")
        else:
            try:
                fork_data, ferr = run_radon(radon, tmp, paths)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            if fork_data is None:
                notes.append(f"分叉点树上的 radon 失败（{ferr}）⇒ 只报分布，不判退化")
            else:
                baseline = flatten(fork_data, only_paths={b.path for b in now.values()})

    blocks, warns = ([], []) if baseline is None else judge(now, baseline, floor, warn_at)
    dist = distribution(now)

    if args.json:
        print(
            json.dumps(
                {
                    "target": str(target),
                    "base": base,
                    "fork": sha,
                    "paths": paths,
                    "floor": floor,
                    "warn_at": warn_at,
                    "blocks_total": len(now),
                    "distribution": dist,
                    "blocked": blocks,
                    "warns": warns,
                    "notes": notes,
                    "ok": not blocks,
                },
                ensure_ascii=False,
            )
        )
    else:
        print(
            f"复杂度闸（ratchet）｜目标仓 {target}｜基 {base}"
            + (f"@{sha[:8]}" if sha else "（分叉点不可判）")
            + f"｜块 {len(now)}｜档位 拦≥{floor} 告警≥{warn_at}"
        )
        print("  分布 " + " ".join(f"{r}:{dist[r]}" for r in RANKS))
        for n in notes:
            print(f"  提示 {n}")
        for w in warns:
            print(f"  WARN {w}")
        for b in blocks:
            print(f"  BLOCK {b}")
        print(f"  结论 {'放行' if not blocks else '拒绝'}：BLOCK {len(blocks)}，WARN {len(warns)}")
    return 1 if blocks else 0


if __name__ == "__main__":
    sys.exit(main())
