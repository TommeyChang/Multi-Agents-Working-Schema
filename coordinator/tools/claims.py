#!/usr/bin/env python3
"""自称与事实——**把"事故处置条款"变成可核的判据**（静态，不连库、不跑测试）。

## 为什么要这一件

事故处置的经验最后都会沉淀成一句硬要求写在文档里，例如：

> 并发用例必须在 MySQL 载体跑（SQLite 忽略 FOR UPDATE，**不得冒充并发正例**）。

**条款拦不住任何东西**——它要求的是"每写一个并发用例时，作者记得换载体"。
可靠的形态是把它变成一条**可从代码里核掉的判据**：*自称*（测试名／docstring 里的
并发词）与*事实*（它跑在哪个载体上）必须对上。

## 第一批判据：并发正例必须在 MySQL 载体

| 项 | 判据 |
|---|---|
| **自称** | 测试函数名或 docstring 含并发词（并发／concurrent／race／竞争／争用／锁／lock／交错） |
| **依赖真锁** | 引用锁语义（`for_update`／`FOR UPDATE`）——**这类断言的正确性依赖真锁** |
| **载体** | ① 标了 `db` marker；或 ② 用了真库夹具（仓里的采集钩子会给它自动打标）⇒ 视为真库载体 |
| **判定** | 自称并发 ∧ 依赖真锁 ∧ 两者皆无 ⇒ **BLOCK：断言依赖真锁，却跑在忽略它的载体上** |

**为什么判据要收窄到"依赖真锁"**：不能把"文件里没写 marker"当成"不在真库"——
本仓的 `db` marker 由采集期钩子按**夹具闭包**自动打（`tests/conftest.py`），
文件里看不出来。实测第一版把一条用 `mysql_scratch` 的用例误判为"跑在 SQLite"。
**假红比没闸更坏**：一处误报就足以让人整条闸都不看。

**它不管的事**：不跑测试、不连库、不判"这个并发用例写得对不对"。
它只判一件机械事实：**依赖真锁的断言，是否跑在真载体上**。

## 用法

    python3 tools/claims.py                       # 用体系内默认目标仓
    python3 tools/claims.py --json
    python3 tools/claims.py --check concurrency-carrier   # 显式指定判据（当前只有这条）

退出码：`0` 全过；`1` 有 BLOCK；`2` 用法或环境错误（目标仓／绑定缺失）。
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

_COORDINATOR_ROOT = Path(__file__).resolve().parents[1]
if str(_COORDINATOR_ROOT) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_ROOT))

from bg_coordinator.binding import load as load_binding  # noqa: E402
from bg_coordinator.binding import locate as locate_binding  # noqa: E402
from bg_coordinator.target import target_repo  # noqa: E402

#: 自称并发的词——**中英都要**（这仓的测试名与 docstring 中英混用）。
#:
#: 英文一律用**词形正则**，不用子串：子串匹配把 `blocks`／`blocked` 里的 `lock`
#: 也当成了并发词——实测第一跑就误报 5 条（`deny_states_block_delivery` 中招）。
#: **假红比没闸更坏**：一处误报就足以让人整条闸都不看。
_CONCURRENCY_CJK = ("并发", "并发正例", "竞争", "争用", "交错", "同时")
_CONCURRENCY_EN_RE = re.compile(
    r"concurren\w*|\brace\b|\braces\b|\bracing\b|interleav\w*|deadlock\w*"
    r"|(?<![a-z])lock(s|ed|ing)?\b",
    re.I,
)

#: 锁语义的信号（即使没写并发词，用到它就意味着"这条断言依赖真锁"）
LOCK_SIGNALS = ("for_update", "FOR UPDATE", "with_for_update", "select_for_update")

#: 默认的真库夹具名（正经来源是绑定 §测试 的 `db_fixtures`）
FALLBACK_DB_FIXTURES = ("mysql_scratch", "db_session", "session_factory")

DEFAULT_DB_MARKER = "db"


@dataclass
class Finding:
    file: str
    line: int
    test: str
    reason: str


def _binding_tests(target: Path) -> dict:
    """测试口径**从工程绑定取**（markers／db_fixtures／substrate 都是工程落点）。"""
    _src, path = locate_binding(target)
    if path is None:
        return {}
    data, _err = load_binding(path)
    if not data:
        return {}
    tests = data.get("tests")
    return tests if isinstance(tests, dict) else {}


def _test_files(target: Path) -> list[Path]:
    root = target / "tests"
    return sorted(p for p in root.rglob("test_*.py")) if root.is_dir() else []


def _module_marked_db(tree: ast.Module, marker: str) -> bool:
    """文件级 `pytestmark = pytest.mark.db`（含列表形式）。"""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            continue
        src = ast.dump(node.value)
        if f"'{marker}'" in src or f'"{marker}"' in src:
            return True
    return False


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    out: list[str] = []
    for dec in node.decorator_list:
        out.append(ast.unparse(dec))
    return out


def scan_file(path: Path, rel: str, *, marker: str, db_fixtures: tuple[str, ...]) -> list[Finding]:
    """扫一个测试文件，返回"自称并发却不在真库载体"的条目。"""
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return []  # 坏文件不该让估计工具崩（它不是门禁本体）
    module_marked = _module_marked_db(tree, marker)
    out: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue
        body_src = ast.unparse(node)
        doc = ast.get_docstring(node) or ""
        claim_text = node.name + " " + doc
        claims = any(w in claim_text for w in _CONCURRENCY_CJK) or bool(
            _CONCURRENCY_EN_RE.search(claim_text)
        )
        needs_real_lock = any(sig in body_src for sig in LOCK_SIGNALS)
        if not (claims and needs_real_lock):
            continue
        on_real_carrier = module_marked or any(marker in dec for dec in _decorator_names(node))
        on_real_carrier = on_real_carrier or any(f in body_src for f in db_fixtures)
        if not on_real_carrier:
            out.append(
                Finding(
                    file=rel,
                    line=node.lineno,
                    test=node.name,
                    reason=(
                        "自称并发且**依赖真锁**（`FOR UPDATE`），却既没标真库、也没用真库夹具——"
                        "SQLite 忽略 FOR UPDATE，这条不能算并发正例："
                        f"加 `@pytest.mark.{marker}` 或换成真库夹具"
                    ),
                )
            )
    return out


def check(target: Path) -> tuple[list[Finding], dict]:
    """跑第一批判据；返回 (BLOCK 清单, 口径说明)。"""
    tests = _binding_tests(target)
    marker = str(tests.get("db_marker") or DEFAULT_DB_MARKER)
    declared = tests.get("db_fixtures")
    db_fixtures = (
        tuple(str(x) for x in declared) if isinstance(declared, list) and declared
        else FALLBACK_DB_FIXTURES
    )
    findings: list[Finding] = []
    files = _test_files(target)
    for path in files:
        findings.extend(
            scan_file(path, str(path.relative_to(target)), marker=marker, db_fixtures=db_fixtures)
        )
    info = {
        "target": str(target),
        "marker": marker,
        "db_fixtures": list(db_fixtures),
        "test_files": len(files),
        "binding_tests_declared": bool(tests),
    }
    return findings, info


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="自称与事实：把条款变成可核判据（静态）")
    ap.add_argument("--target", default=None, help="目标仓（默认 $BG_TARGET_REPO 或体系内默认）")
    ap.add_argument(
        "--check", default="concurrency-carrier", choices=["concurrency-carrier"],
        help="判据名（当前只有并发载体这一条）",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    target = target_repo(args.target)
    if not (target / "tests").is_dir():
        print(f"目标仓缺 tests/：{target}", file=sys.stderr)
        return 2

    findings, info = check(target)
    if args.json:
        print(json.dumps({**info, "blocked": [asdict(f) for f in findings]}, ensure_ascii=False))
    else:
        print(f"自称与事实｜判据 {args.check}｜目标仓 {target}")
        print(f"  测试文件 {info['test_files']}｜真库标记 {info['marker']}｜"
              f"真库夹具 {len(info['db_fixtures'])} 个")
        if not info["binding_tests_declared"]:
            print("  ⚠ 绑定 §测试 未声明——用的是兜底夹具名单（口径归工程，建议补上）")
        for f in findings:
            print(f"  BLOCK {f.file}:{f.line} {f.test}——{f.reason}")
        if not findings:
            print("  全过：自称并发的用例都在真库载体上")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
