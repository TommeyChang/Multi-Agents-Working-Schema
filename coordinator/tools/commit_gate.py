#!/usr/bin/env python3
"""提交期「界内」闸（**只读**）：把改动文件集判到某条目的白名单／不动清单上。

## 为什么有它

协调器的不变量五「界内」（`Multi-Agents-Working-Schema/rules/COORDINATION.md`「四、不变量」）规定
**实际改动必须落在条目白名单内**，而它原本只在**交付时与合并时各判一次**。
那时越界已经发生：改错的文件进了 worktree、进了 commit，纠正成本高。
本工具把同一条判定**前移到 commit 之前**——dev／TL 在自己的 worktree 里先跑一次，
越界在提交那一刻就被挡住，而不是等交付评审才发现。

## 保留的判据（KEEP）

对照主仓原件
`ref/tools/scripts/tools/git_hooks/precommit_checks/_checks.py::check_commit_scope`，
只保留 MAWS 用得到的四条；BLOCK／WARN 的分级与「BLOCK 即非零退出」的语义照搬：

- **BLOCK**：改动文件不在条目的修改范围白名单内（不变量五的正文）。
- **BLOCK**：改动文件命中条目的不动清单 `frozen`（本条目明令不得改动的文件）。
- **WARN**：改动了**设计面**（`validators.touches_design_surface`：`ports.py`／
  `contract.py`／`protocol.py`／`spec.py` 型命名），但本批改动里**没有文档**
  （`validators.changed_docs`）。MAWS 口径是"文档修订不是必做动作，但一旦改了
  口径，就得与描述它的面同批改"，故**告警而不阻断**。
- **WARN**：条目的白名单为空——没有任何可判据，改动是否"界内"无从谈起。

## 删除的判据（DROP，逐条说明为什么主仓需要而 MAWS 不需要）

主仓的 `precommit_checks` 是一整套**仓库专属**的提交闸；本工具只借它的"判定形状"
（BLOCK／WARN ＋ 违规模型 ＋ 改动集对允许集的判定）。以下判据**刻意不移植**，
也**不另造替代品**——它们要么依赖主仓的目录结构，要么在 MAWS 里没有对应物：

- **C1 取号与 registry 同步**（`check_number_pregistration`）：主仓 `todo/registry.md`
  的号段账本；MAWS 的号由协调器在**同一持锁事务**内分配，没有"先取号再登记"这一步。
- **C2 迁移 revision 撞号**（`find_revision_collisions`）：主仓 Alembic 迁移件
  `revision` 的唯一性；MAWS 不管理迁移件。
- **C4 DDL 标记扫描**（`check_migration_rewrite`）：`op.create_table` 等 `DDL_MARKERS`
  的"落库后就地改写"告警——`DDL_MARKERS`／`MIGRATION_DIR` 都是主仓专属路径与符号。
- **row-drop 标记**（`check_row_drop`／`row_drop_declarations`）：主仓
  `todo/inbox`／`todo/lines` 的"数据行消失"防整档重写闸；MAWS 的需求入口是协调器
  状态，不存在 markdown 行被陈旧副本吃掉的问题。
- **`SCOPE_GATE_SHARED_FILES`／`SCOPE_GATE_SHARED_PREFIXES` 主仓路径表**：
  主仓的共享面（`tests/README.md`、`todo/`）是主仓追溯体系的产物；MAWS 的白名单
  来源是协调器的类型化字段，不需要"共享面豁免表"。
- **提交 message／merge 豁免逻辑**（`docs-scope:`、`row-drop:`、`_same_as_merge_parent`、
  `MERGE_HEAD` 面）：主仓把范围声明写进 commit message，本工具的白名单是条目字段，
  无须从 message 反解，也没有"与合并对方逐字节一致即豁免"这一口径。
- **钩子安装**（`precommit_checks.py` 入口）：本工具是**人手动调用的只读检查**，
  不安装、不注册任何 git hook。

## 接口

    python3 tools/commit_gate.py --task <TASK-ID> [--root <coordinator state root>] \\
            [--repo <path>] [--staged] [--range <BASE..HEAD|BASE...HEAD>] [--json]

- `--root` 只认显式参数或环境变量 `$BG_COORDINATOR_ROOT`，**绝不猜默认值**；
- `--repo` 默认当前目录，**不是 git 仓库即拒绝运行**（退出 2）；
- `--staged`（默认）取 `git diff --name-only --cached`；`--range` 取
  `git diff --name-only <range>`；两者互斥，同时给出是用法错误（退出 2）；
- 输出：逐条 `BLOCK <path> —— <reason>` / `WARN <path> —— <reason>`，末尾一行汇总；
  `--json` 时改输出机器可读对象，键为 `task`／`blocked`／`warns`／`files`／`ok`
  （`blocked`／`warns` 每项是 `{"path", "reason"}`）。

## 退出码

- `0` 无 BLOCK（只有 WARN 也是 0）；
- `1` 至少一条 BLOCK；
- `2` 用法／环境错误（条目不存在、repo 不是 git 仓库、参数冲突等）。

## 只读保证

只用 `Store(root=...).load_state(strict=False)` 读状态；**不调用** `transact`／
`publish` 等任何写动词，也不写任何文件。错误一律走 stderr；stdout 只在成功判定、
或 `--json`（含错误路径的 `ok=false` JSON）时使用。

## 匹配语义

`frozen` 与 `whitelist` **共用** `validators.whitelist_covers` 的匹配语义（目录前缀、
`*`／`**`，且 `*` 不跨 `/`）。刻意不另造第二套 glob 方言——否则两处纪律迟早漂移。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

# `tools/` 的父目录 = 协调器根。以脚本方式运行时 `sys.path[0]` 是 `tools/`，
# 不插这一下就 import 不到 `bg_coordinator`（包在协调器根下）。
_COORDINATOR_ROOT = Path(__file__).resolve().parents[1]
if str(_COORDINATOR_ROOT) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_ROOT))

from bg_coordinator import migrations as mig  # noqa: E402
from bg_coordinator.binding import load as load_binding  # noqa: E402
from bg_coordinator.binding import locate as locate_binding  # noqa: E402
from bg_coordinator.models import Task  # noqa: E402
from bg_coordinator.storage import Store, StoreError  # noqa: E402
from bg_coordinator.validators import (  # noqa: E402
    changed_docs,
    touches_design_surface,
    whitelist_covers,
)

#: 判定级别——与主仓 `_model.py` 同字面量，便于人读与对账。
BLOCK = "BLOCK"
WARN = "WARN"

#: 状态根环境变量名（与协调器 CLI 同源：`bg_coordinator.cli` 也认它）。
_ROOT_ENV = "BG_COORDINATOR_ROOT"


@dataclass(frozen=True)
class Finding:
    """一条判定发现；`level` 为 BLOCK 时整体判定不通过。"""

    level: str
    path: str
    reason: str


@dataclass
class Verdict:
    """一次提交判定的完整结果——纯数据，便于 `--json` 与测试直接断言。"""

    task: str
    files: list[str] = field(default_factory=list)
    blocked: list[Finding] = field(default_factory=list)
    warns: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """无 BLOCK 即通过——WARN 只留痕，不阻断。"""
        return not self.blocked


def judge(
    task_id: str, task: Task, changed_files: list[str], repo: Path | None = None
) -> Verdict:
    """把「改动文件集」判到「条目的白名单／不动清单／设计面／**迁移链**」上。

    **前三条是纯函数**：不读盘、不写盘，输入齐了就出结果。
    **迁移链**要读那个仓的迁移件与基线（`repo` 为空则跳过——调用方在 `main` 里总会给）。

    两条刻意的顺序／边界选择：

    1. **frozen 优先于白名单**：一个文件同时命中二者时，报"命中不动清单"——
       它比"不在白名单内"更具体，也更接近真实违规意图；且 frozen 的 BLOCK
       不因白名单为空而消失（不动清单可独立判定）。
    2. **空白名单只 WARN 不 BLOCK**：`whitelist_covers(files, [])` 会把**所有**
       文件报成"未覆盖"，若照单全收就会把"没登记范围"误判成"全部越界"。
       MAWS 的规则是"无白名单不成条目"，即问题出在条目本身，故留痕告警。
    """
    files = sorted(dict.fromkeys(changed_files))  # 去重 + 稳定顺序，输出可复现
    blocked: list[Finding] = []
    warns: list[Finding] = []

    # 不动清单：`whitelist_covers` 返回的是**未被覆盖**的文件，故"命中"要取补集。
    # 空清单 = 明确声明"无"，直接跳过（不能拿补集反推出"全体命中"）。
    frozen_hits: set[str] = set()
    if task.frozen:
        not_frozen = set(whitelist_covers(files, list(task.frozen))[1])
        frozen_hits = set(files) - not_frozen

    if task.whitelist:
        uncovered = set(whitelist_covers(files, list(task.whitelist))[1])
    else:
        uncovered = set()
        warns.append(
            Finding(
                WARN,
                task_id,
                "条目白名单为空——没有任何可判据，改动是否界内无从谈起（先 define 补全四要素）",
            )
        )

    for path in files:
        if path in frozen_hits:
            blocked.append(
                Finding(BLOCK, path, "命中不动清单（frozen）——本条目明令不得改动该文件")
            )
        elif path in uncovered:
            blocked.append(
                Finding(BLOCK, path, "不在条目白名单内——疑似收编他人在途改动，或超出本条目范围")
            )

    # **迁移链（提交时收口）**：改动里含迁移件 ⇒ 必须过链位判定。
    #
    # 为什么提交时就要判：放号只保证**号唯一**，保证不了**链位唯一**。
    # 两个 DBA 各自合法地取号、各自把父节点接到当时的 head 上，合并起来就是**两个 head**——
    # 迁升级目标不唯一。等到合并才发现，返工成本最高（分支已推、评审已过、窗口已排）。
    # 判据与合并闸共用 `bg_coordinator/migrations.py`（**同一处口径**）。
    blocks = migration_blocks(files, repo) if repo is not None else []
    for reason in blocks:
        blocked.append(Finding(BLOCK, task_id, reason))

    # 设计面：改了契约／口径的成文表达，就得同批把描述它的文档也改了。
    surface = touches_design_surface(files)
    if surface and not changed_docs(files):
        for path in surface:
            warns.append(
                Finding(
                    WARN,
                    path,
                    "改动了设计面（契约／口径）但本批没有文档——改口径须同批改 docs/",
                )
            )

    return Verdict(task=task_id, files=files, blocked=blocked, warns=warns)


def migration_blocks(files: list[str], repo: Path) -> list[str]:
    """含迁移件时的链位判定——判据在 `bg_coordinator/migrations.py`，这里只负责取数与措辞。

    迁移目录与基线**都从工程绑定取**（工程落点归工程）：`migrations.dir`／`migrations.base`。
    取不到目录 ⇒ 判定无法进行 ⇒ **BLOCK**（宁可不放行，也不假装通过）。
    """
    _src, path = locate_binding(repo)
    data: dict = {}
    if path is not None:
        loaded, _err = load_binding(path)
        data = loaded or {}
    mconf = data.get("migrations") if isinstance(data.get("migrations"), dict) else {}
    vdir = str(mconf.get("dir") or "")
    if not vdir:
        return []  # 没有迁移目录的声明 ⇒ 本工程没有"迁移件"这类改动，不适用
    if not any(f.startswith(vdir.rstrip("/") + "/") for f in files):
        return []  # 本批没有迁移件
    base = str(mconf.get("base") or "")
    rows = mig.load_migrations(repo, vdir)
    if not base:
        return [
            "本批含迁移件，但绑定 §迁移 没声明 `base`——**拿不到基线就无法确认链位**，"
            "这正是提交时要收的口（补 base，或改用 --base 指定）"
        ]
    base_rows, err = mig.load_from_git(repo, base, vdir)
    if err:
        return [f"本批含迁移件，但基线不可用（{err}）——链位无从确认，拒绝提交"]
    return [f"迁移链：{b}" for b in mig.judge(rows, base_rows, vdir)]


def _payload(verdict: Verdict) -> dict[str, object]:
    """`--json` 的机器可读形态——键固定，便于消费方按名取值。"""
    return {
        "task": verdict.task,
        "blocked": [{"path": f.path, "reason": f.reason} for f in verdict.blocked],
        "warns": [{"path": f.path, "reason": f.reason} for f in verdict.warns],
        "files": list(verdict.files),
        "ok": verdict.ok,
    }


def _print_text(verdict: Verdict) -> None:
    """人读输出：BLOCK 在前、WARN 在后，末尾一行汇总。"""
    for finding in [*verdict.blocked, *verdict.warns]:
        print(f"{finding.level} {finding.path} —— {finding.reason}")
    outcome = "放行" if verdict.ok else "拒绝提交"
    print(
        f"合计 {len(verdict.files)} 个改动文件：BLOCK {len(verdict.blocked)}，"
        f"WARN {len(verdict.warns)} —— {outcome}。"
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """跑一条 git 命令（调用方只传只读子命令）。

    `-C` 指定仓库，故调用方不必改进程 cwd——本工具绝不改变也绝不写工作区。
    """
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def is_git_repo(repo: Path) -> bool:
    """`repo` 是不是（存在的）git 仓库。

    只问 git 本身，不用"目录里有没有 `.git`"这种近似判据——后者会漏掉
    worktree／子目录调用，也会把坏仓库当成好仓库。
    """
    if not repo.is_dir():
        return False
    return _git(repo, "rev-parse", "--git-dir").returncode == 0


def diff_files(repo: Path, args: list[str]) -> tuple[list[str] | None, str]:
    """取 diff 的改动文件集；git 失败 → `(None, stderr)`。

    只用 `--name-only`：本闸判的是"哪些文件被碰了"，不看内容——内容判定是主仓
    那套判据的活，本工具刻意不碰。
    """
    proc = _git(repo, "diff", "--name-only", *args)
    if proc.returncode != 0:
        return None, proc.stderr.strip()
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()], ""


class _UsageError(Exception):
    """argparse 的用法错误——抛出而非 `sys.exit`，让 `main()` 能把 2 当返回值。"""


class _Parser(argparse.ArgumentParser):
    """`main(argv) -> int` 契约要求退出码是返回值，故把 `error()` 改成抛异常。"""

    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="commit_gate.py",
        description="提交期「界内」闸（只读）：把改动文件集判到某条目的白名单／不动清单上。",
    )
    parser.add_argument("--task", required=True, metavar="TASK-ID", help="协调器条目号，如 T-D-12")
    parser.add_argument(
        "--root",
        default=None,
        metavar="DIR",
        help=f"协调器状态根；省略时取 ${_ROOT_ENV}（绝不猜默认值）",
    )
    parser.add_argument("--repo", default=None, metavar="PATH", help="要检查的 git 仓库（默认当前目录）")
    parser.add_argument("--staged", action="store_true", help="判暂存区改动（默认行为）")
    parser.add_argument(
        "--range",
        dest="range_spec",
        default=None,
        metavar="BASE..HEAD",
        help="判某个 diff 区间的改动集（与 --staged 互斥）",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    return parser


def _fail(code: int, message: str, emit_json: bool, task: str = "") -> int:
    """错误路径：**一律 stderr**；`--json` 时才在 stdout 补一份 `ok=false` 的 JSON。"""
    print(f"commit_gate: {message}", file=sys.stderr)
    if emit_json:
        print(
            json.dumps(
                {"task": task, "blocked": [], "warns": [], "files": [], "ok": False},
                ensure_ascii=False,
            )
        )
    return code


def main(argv: list[str] | None = None) -> int:
    """命令行入口；返回退出码（0 通过／1 有 BLOCK／2 用法或环境错误）。"""
    raw = list(sys.argv[1:] if argv is None else argv)
    want_json = "--json" in raw  # 解析失败时也要知道要不要出 JSON，故先扫一眼原始 argv
    parser = build_parser()
    try:
        args = parser.parse_args(raw)
    except _UsageError as exc:
        return _fail(2, f"参数错误：{exc}", want_json)
    except SystemExit as exc:  # --help：argparse 直接 sys.exit(0)
        code = exc.code
        return code if isinstance(code, int) else 0

    if args.staged and args.range_spec:
        return _fail(2, "--staged 与 --range 互斥：一次只判一个改动集", want_json, task=args.task)

    root_raw = args.root or os.environ.get(_ROOT_ENV, "")
    if not root_raw.strip():
        return _fail(
            2,
            f"未给出协调器状态根——须显式 --root，或设置 ${_ROOT_ENV}（不猜默认值）",
            want_json,
            task=args.task,
        )
    root = Path(root_raw)
    repo = Path(args.repo) if args.repo else Path.cwd()

    try:
        repo_is_git = is_git_repo(repo)
    except FileNotFoundError:
        return _fail(2, "找不到 git 可执行文件——本工具需要 git", want_json, task=args.task)
    if not repo_is_git:
        return _fail(
            2,
            f"{repo} 不是 git 仓库——--repo 须指向仓库根（或其子目录）；已拒绝运行",
            want_json,
            task=args.task,
        )

    try:
        state = Store(root=root).load_state(strict=False)  # 只读：绝不 transact／publish
    except (StoreError, OSError, ValueError) as exc:
        return _fail(2, f"读取协调器状态失败：{exc}", want_json, task=args.task)

    task = state.tasks.get(args.task)
    if task is None:
        return _fail(2, f"条目 {args.task} 不存在于 {root}", want_json, task=args.task)

    diff_args = ["--cached"] if args.range_spec is None else [args.range_spec]
    try:
        files, git_err = diff_files(repo, diff_args)
    except FileNotFoundError:  # pragma: no cover - is_git_repo 已探过 git 存在
        return _fail(2, "找不到 git 可执行文件——本工具需要 git", want_json, task=args.task)
    if files is None:
        return _fail(2, f"git diff 失败：{git_err or '（无 stderr）'}", want_json, task=args.task)

    verdict = judge(args.task, task, files, repo)
    if args.json:
        print(json.dumps(_payload(verdict), ensure_ascii=False))
    else:
        _print_text(verdict)
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
