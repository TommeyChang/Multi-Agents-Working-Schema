"""自持性——**本体系不引用外部工具**，这条要被机器钉住。

## 判据：什么叫「引用」

| | 是引用（禁） | 不是引用（许） |
|---|---|---|
| 行为 | 调别仓的脚本、依赖它的 venv／路径存在 | 驱动**目标仓自己**的 ruff／pytest／alembic（那是负载） |
| 权威 | "以它为准"、"先看它" | 记录**历史出处**（"改编自…"）——不产生依赖 |

本体系只准依赖：① 标准库；② 系统工具 `git`；③ 目标仓的**内容**（测试、迁移件）。
目标仓的**脚本**一个都不许调。

## 为什么值得一条测试

"不引用外部"靠人自觉，三个月后就会长出一条 `subprocess.run(["scripts/tools/gate.sh"])`——
而它坏的时候，本体系是在**别人删掉那个文件**的那一刻才发现。所以钉住。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

COORD = Path(__file__).resolve().parents[1]
TOOLS = COORD / "tools"
PKG = COORD / "bg_coordinator"

#: 标准库白名单之外，本体系只准 import 自己
_ALLOWED_THIRD_PARTY_IN_CORE: frozenset[str] = frozenset()

#: 外部解释器／绝对路径／重复的默认值——工具里出现即违规（这是**行为**上的耦合）。
#: 注意：`<目标仓>/.venv/bin/python` 是合法的（在负载自己的环境里跑它自己的东西）；
#: 非法的是**指向某个具体外部仓**的绝对路径，以及把「目标仓默认值」抄第二份。
_FORBIDDEN_IN_TOOLS = (
    "/opt/futures-broker-gateway",
    "futures-broker-gateway/.venv",
    "../../futures-broker-gateway",  # 默认值只准在 bg_coordinator/target.py 定义一次
)

#: 体系外的工作区绝对路径
_ABS_OUTSIDE_RE = re.compile(r"/root/|/opt/")

#: 「在调别仓的脚本」的形状——命令行里带外部脚本路径
_INVOCATION_RE = re.compile(
    r"(bash|sh|python3?)\s+\S*scripts/(tools|ops|dba)/"
    r"|\[\s*['\"]\S*scripts/(tools|ops|dba)/"
)

#: 规范面（agent 照着做的那些文件）
_NORMATIVE = [
    COORD.parent / "AGENT.md",
    *(COORD.parent / "rules").glob("*.md"),
    *(COORD.parent / "agents").glob("*.md"),
]

#: 规范面里禁的：外部门禁脚本名（本体系有自己的 gate.py），以及具体的**外部工具文件**
#: 注意：只提目录（角色工作范围，如「`scripts/ops/` 归 ops」）不算引用——
#: 引用是"去跑那个文件"，所以判据要求**文件名带扩展名**。
_FORBIDDEN_IN_NORMATIVE = ("gate.sh", "conftest_mysql")
_EXTERNAL_TOOL_FILE_RE = re.compile(r"scripts/(tools|ops|dba)/[\w.-]+\.(py|sh)")


def _tool_files() -> list[Path]:
    return [p for p in sorted(TOOLS.iterdir()) if p.suffix in {".py", ".sh"}]


def test_tools_do_not_reference_external_tools() -> None:
    """工具里不得出现外部工具路径／别人的解释器。"""
    offenders: list[str] = []
    for path in _tool_files():
        text = path.read_text(encoding="utf-8")
        for needle in _FORBIDDEN_IN_TOOLS:
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
        for m in _INVOCATION_RE.finditer(text):
            offenders.append(f"{path.name}: 调用外部脚本 {m.group(0).strip()}")
        for m in _ABS_OUTSIDE_RE.finditer(text):
            offenders.append(f"{path.name}: 体系外绝对路径 {m.group(0)}")
    assert not offenders, "工具引用了外部工具：\n  " + "\n  ".join(offenders)


def test_target_default_is_defined_once() -> None:
    """**目标仓默认值只有一处定义**（`bg_coordinator/target.py`）。

    抄成三份就会出现"改一处忘一处"——最后没人知道到底在服务谁。
    """
    target_mod = PKG / "target.py"
    assert "../../futures-broker-gateway" in target_mod.read_text(encoding="utf-8")
    for path in _tool_files():
        assert "../../futures-broker-gateway" not in path.read_text(encoding="utf-8"), (
            f"{path.name} 又抄了一份目标仓默认值——请用 target_repo()"
        )


def test_shell_tools_run_on_plain_python3() -> None:
    """shell 入口的解释器自持：不得把别人的 venv 当默认。"""
    for name in ("smoke.sh", "watch.sh"):
        text = (TOOLS / name).read_text(encoding="utf-8")
        assert "BG_COORDINATOR_PYTHON" in text, f"{name} 应当允许覆盖解释器"
        assert "python3" in text, f"{name} 应当有自己的 python3 兜底"
        assert ".venv" not in text, f"{name} 不得依赖外部 venv"


def test_package_imports_are_stdlib_or_self() -> None:
    """协调器核心只准 import 标准库或自己——多一个第三方依赖就多一处环境耦合。"""
    stdlib = set(sys.stdlib_module_names)
    offenders: list[str] = []
    for path in sorted(PKG.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [] if node.level else [(node.module or "").split(".")[0]]
            else:
                continue
            for n in names:
                if not n or n in stdlib or n == "bg_coordinator":
                    continue
                if n in _ALLOWED_THIRD_PARTY_IN_CORE:
                    continue
                offenders.append(f"{path.name}: {n}")
    assert not offenders, "核心包出现外部依赖：\n  " + "\n  ".join(offenders)


def test_normative_docs_reference_only_in_house_tools() -> None:
    """**规范面**（AGENT／rules／agents）只准引用本体系的工具。

    出处类文档（`coordinator/tools/README.md`、`ref/**`、`DESIGN.md`）不在此列——
    它们记的是历史与取样，不产生依赖。
    """
    offenders: list[str] = []
    for path in _NORMATIVE:
        text = path.read_text(encoding="utf-8")
        for needle in _FORBIDDEN_IN_NORMATIVE:
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
        for m in _EXTERNAL_TOOL_FILE_RE.finditer(text):
            offenders.append(f"{path.name}: 外部工具文件 {m.group(0)}")
    assert not offenders, "规范面引用了外部工具：\n  " + "\n  ".join(offenders)


def test_tools_directory_has_no_external_shebang_dependency() -> None:
    """`#!` 不许指向体系外的绝对路径。"""
    for path in _tool_files():
        first = path.read_text(encoding="utf-8").splitlines()[0]
        if first.startswith("#!"):
            assert not re.search(r"/root/|/opt/|\.venv", first), f"{path.name} 的 shebang 指向体系外：{first}"


def test_every_tool_has_a_way_to_be_run_by_itself() -> None:
    """每个工具都要能自证用法与失败原因——不许有"只能被人 import 才活"的半成品。"""
    for path in sorted(TOOLS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "def main(" in text and "__main__" in text, f"{path.name} 缺 main()/__main__ 入口"
        assert "argparse" in text, f"{path.name} 缺命令行参数（用法要能自证）"
