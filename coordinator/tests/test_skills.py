"""技能也是规范面：**前沿元数据要能被解析、引用的文件要真有、结构要能被扫到**。

约定（DSH 的 skill-filesystem provider）：技能是 `<根>/.dsh/skills/<名>/SKILL.md`
或顶层扁平 `<名>.md`，前沿元数据（YAML）至少要有 `description`；
**嵌套的 `**/SKILL.md` 故意不被扫描**——所以写错层级会静默不生效，必须由这道闸喊出来。

技能装的是**流程**（什么时候做、做完怎么判、被判了怎么办）；
判据仍在工具与测试里——技能不许成为判据的第二处落点。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

MAWS = Path(__file__).resolve().parents[2]
COORD = MAWS / "coordinator"
ROOTS = (MAWS / ".dsh" / "skills",)


def _skills() -> list[tuple[Path, str]]:
    """（SKILL.md 路径, 正文）；只认扫描器认的层级。"""
    out: list[tuple[Path, str]] = []
    for root in ROOTS:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/SKILL.md")):
            out.append((path, path.read_text(encoding="utf-8")))
        for path in sorted(root.glob("*.md")):
            out.append((path, path.read_text(encoding="utf-8")))
    return out


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}
    fields: dict[str, str] = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip().strip('"')
    return fields


def test_at_least_one_skill_exists() -> None:
    assert _skills(), "一个技能都没有——目录层级写错了吗（嵌套 SKILL.md 不被扫描）"


@pytest.mark.parametrize("path,text", _skills(), ids=lambda x: getattr(x, "name", ""))
def test_skill_has_description_and_matching_name(path: Path, text: str) -> None:
    """`description` 是**唯一**能进目录的字段——缺了技能就不会被看见。"""
    fm = _frontmatter(text)
    assert fm.get("description"), f"{path} 缺 description（会话目录靠它展示）"
    if fm.get("name"):
        assert fm["name"] == path.parent.name, f"{path} 的 name 与目录名不一致"


@pytest.mark.parametrize("path,text", _skills(), ids=lambda x: getattr(x, "name", ""))
def test_skill_referenced_paths_exist(path: Path, text: str) -> None:
    """技能里点名的仓内文件必须存在——技能是最经不起死引用的文档。"""
    refs = set(
        re.findall(r"`((?:tools|bg_coordinator|rules|agents|bindings|tests)/[A-Za-z0-9_./-]+)`", text)
    )
    missing = [
        r for r in sorted(refs) if not (COORD / r).exists() and not (MAWS / r).exists()
    ]
    assert not missing, f"{path.name} 引用了不存在的文件：{missing}"
