"""回复风格闸：**硬指标要能量、能拦**（用户 2026-10-04 把建议改成硬指标）。

三条：① 阈值与 `AGENT.md §九` 一致（文档↔机器对账）；② 超标必红（负例，防空绿）；
③ 边界（正好等于上限）不报。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

COORD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(COORD))
_SPEC = importlib.util.spec_from_file_location("style_check", COORD / "tools" / "style_check.py")
assert _SPEC and _SPEC.loader
sc = importlib.util.module_from_spec(_SPEC)
sys.modules["style_check"] = sc
_SPEC.loader.exec_module(sc)

MAWS = COORD.parent


def test_limits_match_the_doc() -> None:
    """阈值只许有一处真相：`AGENT.md §九` 与工具常量必须写得一样。"""
    text = (MAWS / "AGENT.md").read_text(encoding="utf-8")
    section = text.split("## 九、回复风格", 1)[1].split("\n## ", 1)[0]
    assert "≤10 行" in section and "≤120 字" in section and "≤1500 字" in section
    assert sc.STYLE == {"lines": 10, "line_chars": 120, "total_chars": 1500}


def test_over_limit_is_red() -> None:
    """三个维度各造一个超限样本——**判据要有齿**。"""
    assert sc.measure("x" * 121)["over"], "超长行没报"
    assert sc.measure("\n".join(["一"] * 11))["over"], "超行数没报"
    assert sc.measure("字" * 1501)["over"], "超总字数没报"


def test_at_limit_is_green() -> None:
    """正好等于上限**不算超**（上限是边界，不是禁区）。"""
    m = sc.measure("\n".join(["一" * 120] * 10))
    assert m["ok"], m["over"]


def test_cli_reports_and_exits(tmp_path: Path) -> None:
    good = tmp_path / "good.md"
    good.write_text("一行。\n", encoding="utf-8")
    assert sc.main(["--file", str(good)]) == 0
    bad = tmp_path / "bad.md"
    bad.write_text("字" * 1501, encoding="utf-8")
    assert sc.main(["--file", str(bad)]) == 1
