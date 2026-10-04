"""文档链接与清单：**指向的文件必须存在，存在的文件必须被指向**。

由来：为了控制固定上下文，我们把"查阅型小节"从主规则文件里拆成**分册**——
拆分会留下死引用（主文件指向一个不存在的分册、或分册没人指向而变成孤儿）。
散文的引用没有编译器管，所以这里给它一个：

① **引用必须落地**：文档里出现的 `rules/x.md`、`agents/x.md`、`bindings/x.md` 路径必须真存在；
② **不许有孤儿分册**：`rules/*.md` 每一份都得在 `AGENT.md` §三 的读表里出现
   （否则"该读什么"就不再完整，读者不知道它在那儿）；
③ **AGENT.md §三 列的文件必须存在**（反向）。

这三条一起，把"拆完就没人知道"这类损失堵住。
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest

MAWS = Path(__file__).resolve().parents[2]

#: 文档里出现的"仓内路径"：只管这三类（它们是体系的规范面）
_PATH_RE = re.compile(r"(?:rules|agents|bindings)/[A-Za-z0-9_.\-]+\.md")

#: 扫描面：所有体系文档（含分册）
def _docs() -> list[Path]:
    return [
        MAWS / "AGENT.md",
        MAWS / "README.md",
        *(MAWS / "rules").glob("*.md"),
        *(MAWS / "agents").glob("*.md"),
        *(MAWS / "bindings").glob("*.md"),
    ]


def test_every_referenced_doc_exists() -> None:
    """**引用必须落地**——死引用等于把读者送进空气里。"""
    missing: list[str] = []
    for doc in _docs():
        if not doc.is_file():
            continue
        for ref in sorted(set(_PATH_RE.findall(doc.read_text(encoding="utf-8")))):
            if not (MAWS / ref).is_file():
                missing.append(f"{doc.relative_to(MAWS)} → {ref}")
    assert not missing, "引用了不存在的文档：\n  " + "\n  ".join(missing)


def test_agent_doc_lists_every_rule_file() -> None:
    """**不许有孤儿分册**：`rules/*.md` 每一份都要出现在 `AGENT.md` §三 的读表里。"""
    agent = (MAWS / "AGENT.md").read_text(encoding="utf-8")
    section = agent.split("## 三、读什么", 1)[1].split("## 四、", 1)[0]
    # 表里既可能写全路径（`rules/X.md`）也可能写通配（`rules/*-details.md`）——
    # 判据只看**文件名这一层**：能被某个条目匹配到即算收录。
    pats = [pat.rsplit("/", 1)[-1] for pat in re.findall(r"`([^`]*\.md)`", section)]
    orphans = [
        p.name
        for p in sorted((MAWS / "rules").glob("*.md"))
        if not any(fnmatch.fnmatch(p.name, pat) for pat in pats)
    ]
    assert not orphans, f"这些规则文件没被 AGENT.md §三 收录：{orphans}"


def test_agent_doc_listed_files_exist() -> None:
    """**反向**：§三 读表里列的文件也必须存在（列了却没有 = 读者找不到）。"""
    agent = (MAWS / "AGENT.md").read_text(encoding="utf-8")
    section = agent.split("## 三、读什么", 1)[1].split("## 四、", 1)[0]
    listed = set(re.findall(r"`([a-z]+/[A-Za-z0-9_.\-]+\.md)`", section))
    missing = [ref for ref in sorted(listed) if not (MAWS / ref).is_file()]
    assert not missing, f"§三 列了不存在的文件：{missing}"


@pytest.mark.parametrize("name", ["COORDINATION.md", "WORKSPACE.md", "SUBAGENT.md", "RISKS.md"])
def test_rule_files_keep_their_pointer_to_details(name: str) -> None:
    """**分册要从主文件指得到**：主文件末尾必须有"查阅面在分册"的指针。

    没有指针的分册 = 孤儿；读者会以为"没写"而不是"在别处"。
    """
    text = (MAWS / "rules" / name).read_text(encoding="utf-8")
    if (MAWS / "rules" / f"{name[:-3]}-details.md").is_file():
        assert f"{name[:-3]}-details.md" in text, f"{name} 没指向它的分册"
