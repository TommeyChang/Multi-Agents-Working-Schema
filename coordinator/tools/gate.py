#!/usr/bin/env python3
"""门禁入口——**本体系自持**，产出的正是 `coord deliver` 要的证据四件套。

## 它做什么

按范围跑目标仓的门禁，并把**命令／退出码／摘要数字／原始输出落盘路径**一起给出：

| 范围 | 内容 | 什么时候用 |
|---|---|---|
| `static` | ruff ＋ 复杂度闸（绑定声明过）＋ 调用方给的静态腿 | 每次提交前 |
| `affected` | static ＋ **按影响面**的 pytest（快跑通道 `-m "not db"`） | 交付前自测（默认） |
| `domain` | static ＋ 指定域全跑（快跑通道） | 跨文件、跨子目录的改动 |
| `full` | ruff ＋ 复杂度闸 ＋ 目标仓**全量** pytest | **发布前**（这条不因任何收窄工具改变） |

**声明过的腿缺工具就跑红**：复杂度闸要 radon，radon 不在目标仓解释器里 ⇒ 这条腿退出 2
⇒ 门禁红（`--no-complexity` 是显式逃生口，证据里会少一条腿，评审判据看得见）。

**范围由本体系自己算**（`impact_scope.py`：直连 → 同名 → 退回整域），不借外部脚本。
目标仓只提供**负载**：它的 ruff、它的 pytest、它的测试文件——本体系不重写它们，
也**不引用别仓的门禁脚本**。

## 为什么把证据做出统一形状

`coord deliver` 要求 `--gate-cmd／--gate-exit／--evidence`；靠人自己拼，
就会出现"命令与落盘日志不是同一次跑"的假证据。这里一次跑完、一次落盘，
**落盘的是原始输出**，回报只给路径与数字——证据可核，回报体积有界。

## 用法

    python3 tools/gate.py --changed data_access/kline/follow.py   # 受影响面
    python3 tools/gate.py --base origin/main                      # 同上，改动由 git diff 推
    python3 tools/gate.py --domain auth data_access               # 指定域
    python3 tools/gate.py --scope full                            # 发布前全量
    python3 tools/gate.py --static --static-tests tests/infra/test_x.py

退出码：`0` 全绿；`1` 有腿红；`2` 用法或环境错误（目标仓不存在、无可跑范围等）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# `tools/` 的父目录 = 协调器根：以脚本方式运行时 `sys.path[0]` 是 `tools/`，
# 包 import 会失败——入口自己把根放进去，不要求调用方设 PYTHONPATH。
_COORDINATOR_ROOT = Path(__file__).resolve().parents[1]
if str(_COORDINATOR_ROOT) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_ROOT))

from bg_coordinator.binding import load as load_binding  # noqa: E402
from bg_coordinator.binding import locate as locate_binding  # noqa: E402
from bg_coordinator.target import target_repo  # noqa: E402

#: 快跑通道的手册标记：真库档按目标仓自己的纪律**不得并发**，故不进门禁的日常范围
FAST_MARKER = "not db"

DEFAULT_LOG_DIR_NAME = ".evidence"

_PYTEST_TALLY = re.compile(r"(\d+) (passed|failed|error|errors|skipped)")

#: 本体系自己的工具目录（复杂度闸按脚本跑到**目标仓的解释器**里：判据在本体系、
#: 工具链在目标仓——与 ruff／pytest 同一条分工）
TOOLS = Path(__file__).resolve().parent


def complexity_declared(target: Path) -> bool:
    """目标仓的工程绑定有没有声明复杂度闸（工程落点归工程）。

    **声明了才加腿**：没声明的工程不该被塞一条它没认过的闸；
    声明了而 radon 不在 ⇒ 腿红（fail closed）——"必须使用"就落在这里。
    """
    _src, path = locate_binding(target)
    if path is None:
        return False
    data, _err = load_binding(path)
    if not data:
        return False
    gate = data.get("gate") if isinstance(data.get("gate"), dict) else {}
    conf = gate.get("complexity")
    return bool(isinstance(conf, dict) and conf)


@dataclass
class Leg:
    name: str
    cmd: list[str]
    exit: int
    out: str = ""
    cases: str = ""

    @property
    def ok(self) -> bool:
        return self.exit == 0


@dataclass
class GateRun:
    scope: str
    target: Path
    legs: list[Leg] = field(default_factory=list)
    log: Path | None = None

    @property
    def exit(self) -> int:
        return 0 if all(leg.ok for leg in self.legs) else 1


# ---------------------------------------------------------------------------
# 解释器与目标仓
# ---------------------------------------------------------------------------


def pick_python(target: Path, given: str | None = None) -> str:
    """跑目标仓门禁的解释器：显式 → 目标仓 venv → 本进程解释器。

    目标仓的 pytest／ruff 依赖都在它自己的 venv 里；这不是"借外部环境"，
    而是**在负载自己的环境里跑它自己的东西**。找不到 venv 就退回本进程，
    由退出码说话（缺依赖 ⇒ 腿红，而不是静默跳过）。
    """
    if given:
        # 相对路径**必须先落地成绝对路径**：命令是在目标仓的 cwd 里跑的，
        # `../../x/.venv/bin/python` 到那里就解析成别的地方 → 退出码 127（实测踩过）。
        #
        # **但只做 absolutize，绝不做 resolve**：venv 的 `bin/python` 是指向基础解释器的
        # **符号链接**，把链接解析掉就等于绕过 `pyvenv.cfg`——site-packages 全丢，
        # 连 ruff／pytest 都 import 不到（实测：解析后 `No module named ruff`）。
        # 裸名（`python3`）保持原样，让它走 PATH。
        if "/" in given or "\\" in given:
            return os.path.abspath(os.path.expanduser(given))
        return given
    # 注意：这是**目标仓自己的** venv（相对它解析），不是本体系借别人的环境——
    # 目标仓的 ruff／pytest 依赖都在它自己那儿，这是"在负载自己的环境里跑它自己的东西"。
    venv = target / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


# ---------------------------------------------------------------------------
# 范围
# ---------------------------------------------------------------------------


def changed_from_git(target: Path, base: str) -> tuple[list[str], str]:
    """`BASE...HEAD` ＋ 工作树未提交改动——dev 常在提交前跑本模式。"""
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=target,
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        return [], f"基线不可用：{base}"
    files = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    st = subprocess.run(
        ["git", "status", "--porcelain"], cwd=target, capture_output=True, text=True, check=False
    )
    for ln in st.stdout.splitlines():
        name = ln[3:].strip()
        if name:
            files.append(name)
    return sorted(set(files)), ""


def impact_tests(target: Path, changed: list[str], python: str) -> tuple[list[str], str, bool]:
    """用本体系的 `impact_scope` 算受影响测试文件。

    返回 `(测试文件, 说明, 是否全域)`。**它是估计，不保证零漏测**（静态 import
    反演看不到动态 import 与间接依赖）——所以 `--scope full` 永远还在。
    """
    script = _COORDINATOR_ROOT / "tools" / "impact_scope.py"
    out = subprocess.run(
        [python, str(script), "--repo", str(target), "--json", "--changed", *changed],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        return [], f"影响面估计失败：{(out.stderr or '').strip()[:160]}", False
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        return [], f"影响面输出无法解析：{exc}", False
    files: list[str] = []
    for imp in data.get("impacts", []):
        if imp.get("scope") == "all-domains":
            return [], "共享面 ⇒ 全域", True
        if imp.get("scope") == "none":
            continue
        files.extend(imp.get("files") or [])
    case_sum = sum(int(i.get("cases") or 0) for i in data.get("impacts", []))
    note = f"影响面 {len(set(files))} 个测试文件、约 {case_sum} 例"
    return sorted(set(files)), note, False


# ---------------------------------------------------------------------------
# 跑
# ---------------------------------------------------------------------------


def run_leg(name: str, cmd: list[str], target: Path, timeout: float) -> Leg:
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd, cwd=target, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return Leg(name, cmd, 124, f"超时（{timeout}s）")
    except OSError as exc:
        return Leg(name, cmd, 127, str(exc))
    out = (proc.stdout or "") + (proc.stderr or "")
    tally = " ".join(f"{n} {kind}" for n, kind in _PYTEST_TALLY.findall(out))
    if not tally:
        tally = f"{time.perf_counter() - started:.1f}s"
    return Leg(name, cmd, proc.returncode, out, tally)


def build_plan(
    args: argparse.Namespace, target: Path, python: str
) -> tuple[list[tuple[str, list[str]]], str]:
    """按范围给出腿清单 `[(名字, 命令)]` 与一句说明。"""
    legs: list[tuple[str, list[str]]] = []
    if not args.no_ruff:
        legs.append(("ruff", [python, "-m", "ruff", "check", "."]))
    if not args.no_complexity and complexity_declared(target):
        # 声明过的复杂度闸 ⇒ 每条范围都跑（静态、快、判据在 tools/complexity.py）。
        # 缺 radon ⇒ 退出码 2 ⇒ 这条腿红：**缺它不放行**，不是静默跳过。
        legs.append(
            (
                "复杂度闸（radon ratchet）",
                [python, str(TOOLS / "complexity.py"), "--target", str(target), "--json"],
            )
        )

    # 说明里点名"这一跑到底跑了哪些腿"——证据的摘要行要能自证覆盖面
    cx = "＋复杂度闸（ratchet）" if any("complexity.py" in " ".join(c) for _, c in legs) else ""

    marker = [] if args.scope == "full" else ["-m", FAST_MARKER]
    if args.scope == "full":
        legs.append(("全量 pytest（tests/）", [python, "-m", "pytest", "-q", *marker, "tests/"]))
        return legs, f"发布前口径：ruff{cx} ＋ 目标仓全量"

    if args.scope == "domain":
        for dom in args.domain:
            d = target / "tests" / dom
            if not d.is_dir():
                raise SystemExit(f"域不存在：{d}")
            legs.append((f"域 tests/{dom}", [python, "-m", "pytest", "-q", *marker, f"tests/{dom}"]))
        return legs, f"指定域：{', '.join(args.domain)}{cx}"

    if args.scope == "static":
        for extra in args.static_tests:
            legs.append((f"静态腿 {extra}", [python, "-m", "pytest", "-q", *marker, extra]))
        return legs, f"静态口径：ruff{cx}（＋调用方给的静态腿）"

    # affected：范围由本体系算
    changed = list(args.changed)
    note = f"显式给出 {len(changed)} 个改动文件"
    if not changed and args.base:
        changed, err = changed_from_git(target, args.base)
        if err:
            raise SystemExit(err)
        note = f"相对 {args.base} 推出 {len(changed)} 个改动文件"
    if not changed:
        raise SystemExit("affected 口径需要 --changed 或 --base（拿不到改动面就算不出影响面）")

    files, why, all_domains = impact_tests(target, changed, python)
    if all_domains:
        legs.append(("全域 pytest（共享面）", [python, "-m", "pytest", "-q", *marker, "tests/"]))
        return legs, f"{note}；{why}{cx}"
    if not files:
        return legs, f"{note}；{why}{cx}（无可跑测试 ⇒ 只跑静态腿）"
    legs.append(("受影响面 pytest", [python, "-m", "pytest", "-q", *marker, *files]))
    return legs, f"{note}；{why}{cx}"


def _write_log(run: GateRun, log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime())
    path = log_dir / f"gate-{run.scope}-{stamp}.log"
    parts = [f"# 门禁原始输出 · 范围 {run.scope} · 目标仓 {run.target}\n"]
    for leg in run.legs:
        parts.append(f"\n=== [gate] {leg.name} ===\n$ {' '.join(leg.cmd)}\n{leg.out}\n")
        parts.append(f"--- 退出码 {leg.exit} · {leg.cases}\n")
    path.write_text("".join(parts), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="门禁入口（本体系自持，产出证据四件套）")
    ap.add_argument("--target", default=None, help="目标仓（默认 $BG_TARGET_REPO 或体系内默认）")
    ap.add_argument("--python", default=None, help="跑门禁的解释器（默认目标仓 venv）")
    ap.add_argument(
        "--scope", choices=["affected", "static", "domain", "full"], default=None,
        help="范围；默认 affected（给了 --domain 则 domain）",
    )
    ap.add_argument("--changed", nargs="*", default=[], help="改动文件（affected 口径）")
    ap.add_argument("--base", default="", help="基线（affected 口径；用它推改动文件）")
    ap.add_argument("--domain", nargs="*", default=[], help="域（domain 口径）")
    ap.add_argument("--static-tests", nargs="*", default=[], help="静态腿（static 口径）")
    ap.add_argument("--no-ruff", action="store_true", help="不跑 ruff")
    ap.add_argument(
        "--no-complexity",
        action="store_true",
        help="不跑复杂度闸（**绑定声明过它就该跑**；这条是显式逃生口，证据里会少一条腿）",
    )
    ap.add_argument("--log-dir", default=None, help="原始输出落盘目录（默认 <本体系>/.evidence）")
    ap.add_argument("--timeout", type=float, default=1800.0, help="单腿超时秒")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    target = target_repo(args.target)
    if not (target / "tests").is_dir():
        print(f"目标仓不像代码仓（缺 tests/）：{target}", file=sys.stderr)
        return 2
    python = pick_python(target, args.python)

    scope = args.scope or ("domain" if args.domain else "affected")
    args.scope = scope
    try:
        plan, note = build_plan(args, target, python)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2

    run = GateRun(scope=scope, target=target)
    for name, cmd in plan:
        leg = run_leg(name, cmd, target, args.timeout)
        run.legs.append(leg)
        mark = "PASS" if leg.ok else "FAIL"
        print(f"  [{mark}] {name} —— 退出码 {leg.exit} · {leg.cases}")

    log_dir = (
        Path(args.log_dir).resolve() if args.log_dir else _COORDINATOR_ROOT / DEFAULT_LOG_DIR_NAME
    )
    run.log = _write_log(run, log_dir)

    summary = note + "；" + "，".join(f"{leg.name}({leg.exit})" for leg in run.legs)
    if args.json:
        print(
            json.dumps(
                {
                    "scope": run.scope,
                    "target": str(run.target),
                    "note": note,
                    "exit": run.exit,
                    "log": str(run.log),
                    "legs": [
                        {"name": leg.name, "cmd": leg.cmd, "exit": leg.exit, "cases": leg.cases}
                        for leg in run.legs
                    ],
                },
                ensure_ascii=False,
            )
        )
    else:
        cmd_str = "&&".join(" ".join(leg.cmd) for leg in run.legs)
        print(f"\n范围 {run.scope}｜目标仓 {run.target}")
        print(f"  说明    {note}")
        print(f"  命令    {cmd_str}")
        print(f"  退出码  {run.exit}")
        print(f"  摘要    {summary}")
        print(f"  原始输出 {run.log}")
        print("\n可直接登记：")
        print(
            f"  coord deliver --id <条目号> --role <角色> --commit <sha> "
            f"--gate-cmd '{cmd_str}' --gate-exit {run.exit} --evidence {run.log} …"
        )
    return run.exit


if __name__ == "__main__":
    sys.exit(main())
