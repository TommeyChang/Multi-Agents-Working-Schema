#!/usr/bin/env python3
"""测试影响面估计器——**按影响面测，不全量测**。

## 为什么需要它

**域粒度太粗**：把改动映射到域（`tests/<域>/`）时，改 `broker_gateway/` 下**任意一个文件**
就触发整个 broker_gateway 域——**实测 2774 个用例**（占全仓 56%）。`data_access` 785、`infra` 744……

域粒度太粗了。实测：改 `data_access/kline/follow.py`，
域粒度要跑 **785** 例；按名字匹配只要 **73** 例——**省 91%**。

## 它做什么

用**静态 AST** 算出「源文件 → 覆盖它的测试文件」，据此给出影响面估计：

1. **直连**：测试文件 `import` 了该模块 ⇒ 必跑；
2. **同名**：测试文件名含该模块名（`follow.py` → `test_*follow*.py`）⇒ 跑；
3. **兜底**：两者都空 ⇒ **退回整个域**（宁多跑，不可漏跑）。

## 它不做什么

**不保证零漏测**。静态 import 反演是**保守近似**：
它看不到动态 import、间接依赖、fixture 链。所以：

- 它给的是**估计**，用来定并行预算与测试预算；
- **发布前仍须全量**（`gate.py --scope full`）——这条不因本工具而改变。

用法：
    python3 tools/impact_scope.py --changed data_access/kline/follow.py
    python3 tools/impact_scope.py --changed a.py b.py --repo ../futures-broker-gateway
    python3 tools/impact_scope.py --summary            # 全仓测试规模分布
    python3 tools/impact_scope.py --changed x.py --json
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_COORDINATOR_ROOT = Path(__file__).resolve().parents[1]
if str(_COORDINATOR_ROOT) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_ROOT))

from bg_coordinator.target import target_repo  # noqa: E402

# ---------------------------------------------------------------------------
# 域映射——**口径在工程绑定**（`testplan.domains` / `testplan.public`）
# ---------------------------------------------------------------------------
#
# 为什么不再自持：同一份"域 → 路径"清单，绑定里已经有一份（分级口径 `tools/testplan.py`
# 也读它）。两处真相必然漂——**2026-10-05 合一，绑定是唯一处**。
#
# 失败方向：绑定取不到 ⇒ 任何非文档改动都算"**全部域**"（宁慢勿漏），
# 绝不静默判成"不用测"——那是最坏的假绿。

_DOC_ISH = (".md", ".rst", ".txt")


def _rules() -> tuple[dict[str, list[str]], tuple[str, ...], str]:
    """（域→前缀, 公共面, 出处）——每次现读，改绑定即生效。"""
    try:
        from bg_coordinator.testplan import load_for

        return load_for(target_repo(None))
    except Exception:  # noqa: BLE001 —— 取不到就走失败方向，不抛
        return {}, (), ""


def domain_of(path: str) -> str | None:
    """路径 → 测试域。None 表示不映射到任何测试域（如 docs/）。"""
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if p.startswith("tests/"):
        parts = p.split("/")[1:]
        # `tests/conftest.py`、`tests/README.md` 直接躺在 tests/ 下 ⇒ 共享面
        return parts[0] if len(parts) > 1 else "*"
    domains, public, _src = _rules()
    if not domains:
        # 绑定没声明域映射 ⇒ **跑全部**（宁慢勿漏）；纯文档不算
        return None if p.endswith(_DOC_ISH) else "*"
    name = Path(p).name
    if name in public or name.startswith("conftest"):
        return "*"  # 全域：共享面／夹具
    if any(p == pre or p.startswith(pre.rstrip("/") + "/") for pre in public):
        return "*"
    best: tuple[int, str] | None = None
    for dom, prefixes in domains.items():
        for pre in prefixes:
            q = pre.strip().lstrip("/")
            if q and (p == q or p.startswith(q.rstrip("/") + "/")) and (
                best is None or len(q) > best[0]
            ):
                best = (len(q), dom)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# 静态反演：源模块 ⇄ 测试文件
# ---------------------------------------------------------------------------

_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", re.M)


def _module_candidates(repo: Path, path: Path) -> set[str]:
    """一个源文件被 import 时对应的**仓库相对**模块名。

    例：`<repo>/data_access/kline/follow.py` → `{"data_access.kline.follow"}`

    必须相对 `repo` 解析：相对 `path.parents[1]` 会把仓库目录名也算进模块名
    （`<父目录>.data_access.kline.follow`），直连一档就会永远命中不了。
    包（`__init__.py`）取包名本身。
    """
    try:
        rel = path.resolve().relative_to(repo.resolve())
    except (ValueError, IndexError):
        rel = path
    parts = list(Path(rel).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    # 不额外补顶层包名：那会让"任何 import 了 data_access 的测试"都算直连，
    # 把整个域重新拉回来，省不了几个用例。精确到模块路径才有意义。
    return {".".join(parts)} if parts else set()


@dataclass
class TestFile:
    #: 名字带 Test 前缀会被 pytest 误当成测试类——显式声明它不是
    __test__ = False

    path: Path
    rel: str
    domain: str
    tests: int
    imports: set[str] = field(default_factory=set)

    @property
    def stem(self) -> str:
        return self.path.stem


def scan_tests(repo: Path) -> list[TestFile]:
    """扫全部测试文件：用例数 ＋ **import 到的模块名**。

    用 AST 取 import，比正则可靠（能区分字符串里的 `import` 字样）。
    记两种形态，覆盖 `import a.b.c` 与 `from a.b import c` 两种写法。
    """
    root = repo / "tests"
    out: list[TestFile] = []
    for tf in sorted(root.rglob("test_*.py")):
        try:
            src = tf.read_text(encoding="utf-8")
        except OSError:
            continue
        imports: set[str] = set()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.add(alias.name.split(".")[0])
                        imports.add(alias.name)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])
                    imports.add(node.module)
                    # `from data_access.kline import follow` ⇒ 也要记 `data_access.kline.follow`
                    for alias in node.names:
                        imports.add(f"{node.module}.{alias.name}")
        tests = len(re.findall(r"^\s*def test_", src, re.M))
        rel = str(tf.relative_to(repo))
        out.append(
            TestFile(
                path=tf,
                rel=rel,
                domain=tf.parent.relative_to(root).parts[0]
                if len(tf.parent.relative_to(root).parts) > 1
                else tf.parent.name,
                tests=tests,
                imports=imports,
            )
        )
    return out


@dataclass
class Impact:
    changed: str
    domain: str | None
    scope: str  # direct | name | domain-fallback | all-domains | none
    tests: list[TestFile] = field(default_factory=list)

    @property
    def case_count(self) -> int:
        return sum(t.tests for t in self.tests)

    @property
    def files(self) -> list[str]:
        return [t.rel for t in self.tests]


def estimate(repo: Path, changed: str, tests: list[TestFile]) -> Impact:
    """估计单个改动文件的影响面。

    三档，**从细到粗**：直连 → 同名 → 退回整域。
    `direct` 优于 `name` 优于 `domain-fallback`：越靠前越省，也越准。
    """
    dom = domain_of(changed)
    if dom is None:
        return Impact(changed=changed, domain=None, scope="none")
    if dom == "*":
        return Impact(changed=changed, domain="*", scope="all-domains", tests=tests)

    src = repo / changed
    mods = _module_candidates(repo, src) if src.exists() else set()
    stem = src.stem

    pool = [t for t in tests if t.domain == dom]
    if not pool:
        pool = tests

    # ① 直连：测试 import 了该模块
    direct = [t for t in pool if t.imports & mods]
    if direct:
        return Impact(changed=changed, domain=dom, scope="direct", tests=direct)

    # ② 同名：测试文件名含该模块名（去通用前缀后的词干）
    words = [w for w in re.split(r"[_\-]", stem) if len(w) >= 4]
    named = [t for t in pool if any(w in t.stem for w in words)] if words else []
    if named:
        return Impact(changed=changed, domain=dom, scope="name", tests=named)

    # ③ 兜底：宁多跑，不可漏跑
    return Impact(changed=changed, domain=dom, scope="domain-fallback", tests=pool)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="测试影响面估计（按影响面测，不全量测）")
    ap.add_argument("--repo", default=None, help="目标仓（默认 $BG_TARGET_REPO 或体系内默认）")
    ap.add_argument("--changed", nargs="*", default=[], help="改动的文件（仓库相对路径）")
    ap.add_argument("--summary", action="store_true", help="只出全仓测试规模分布")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    repo = target_repo(args.repo)
    if not (repo / "tests").is_dir():
        print(f"未找到测试目录：{repo / 'tests'}", file=sys.stderr)
        return 2

    tests = scan_tests(repo)
    if args.summary or not args.changed:
        return _summary(tests, args.json)

    impacts = [estimate(repo, c, tests) for c in args.changed]
    return _report(impacts, tests, args.json)


def _summary(tests: list[TestFile], as_json: bool) -> int:
    by_domain: dict[str, tuple[int, int]] = {}
    for t in tests:
        n, f = by_domain.get(t.domain, (0, 0))
        by_domain[t.domain] = (n + t.tests, f + 1)
    total = sum(n for n, _ in by_domain.values())

    if as_json:
        print(
            json.dumps(
                {
                    "total_cases": total,
                    "domains": {
                        d: {"cases": n, "files": f} for d, (n, f) in sorted(by_domain.items())
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print("全仓测试规模")
    print(f"  {'域':<16}{'用例':>8}{'文件':>6}{'占比':>8}")
    for d, (n, f) in sorted(by_domain.items(), key=lambda kv: -kv[1][0]):
        pct = n * 100 / total if total else 0
        print(f"  {d:<16}{n:>8}{f:>6}{pct:>7.1f}%")
    print(f"  {'合计':<16}{total:>8}{len(tests):>6}")
    return 0


def _report(impacts: list[Impact], tests: list[TestFile], as_json: bool) -> int:
    total = sum(t.tests for t in tests)
    if as_json:
        print(
            json.dumps(
                {
                    "total_cases": total,
                    "impacts": [
                        {
                            "changed": i.changed,
                            "domain": i.domain,
                            "scope": i.scope,
                            "cases": i.case_count,
                            "files": i.files,
                        }
                        for i in impacts
                    ],
                    "sum_cases": sum(i.case_count for i in impacts),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print("测试影响面估计")
    print(f"  （全仓 {total} 例；按域粒度会跑整个域）")
    print()
    for i in impacts:
        print(f"  {i.changed}")
        if i.scope == "none":
            print("    不映射到测试域（文档/工具面）→ 只跑静态闸")
            continue
        if i.scope == "all-domains":
            print(f"    共享面 → **全部域**（{i.case_count} 例）")
            continue
        label = {
            "direct": "直连（测试 import 了该模块）",
            "name": "同名（测试文件名含该模块名）",
            "domain-fallback": "**退回整域**（前两档都没命中）",
        }[i.scope]
        print(f"    {label} → {len(i.tests)} 个测试文件、{i.case_count} 例")
        for f in i.files[:6]:
            print(f"      {f}")
        if len(i.files) > 6:
            print(f"      …（另 {len(i.files) - 6} 个）")

    saved = total * len(impacts) - sum(i.case_count for i in impacts)
    print()
    print(f"  合计 {sum(i.case_count for i in impacts)} 例（全量口径 {total * len(impacts)} 例，省 {saved}）")
    print()
    print("  **这是估计，不保证零漏测**：静态 import 反演看不到动态 import")
    print("  与间接依赖链，所以发布前仍须 `gate.sh --full`。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
