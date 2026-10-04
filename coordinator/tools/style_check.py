#!/usr/bin/env python3
"""回复风格闸（2026-10-04 用户定：**硬指标**，不再是建议）。

判据（六条里的可量化四条，阈值在 `STYLE`，一处权威）：

    ≤10 行 ／ 每行 ≤120 字 ／ 单轮 ≤1500 字 ／ 发送前自检

用法：

    python3 tools/style_check.py --file reply.md      # 校一份草稿
    cat reply.md | python3 tools/style_check.py -      # 管道
    python3 tools/style_check.py --file x --json

退出码：`0` 达标；`1` 超限（**先删再发**）；`2` 用法错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

#: 阈值（字符数，不引分词器）——**一处权威**：AGENT.md §九 的六条与这里必须一致
STYLE: dict[str, int] = {"lines": 10, "line_chars": 120, "total_chars": 1500}


def measure(text: str) -> dict:
    """人读的四个数：行数、最长行字数、总字数、超限项。"""
    lines = text.rstrip("\n").splitlines()
    widths = [len(ln) for ln in lines]
    over = []
    if len(lines) > STYLE["lines"]:
        over.append(f"行数 {len(lines)} > {STYLE['lines']}")
    if widths and max(widths) > STYLE["line_chars"]:
        i = widths.index(max(widths))
        over.append(f"最长行 {max(widths)} 字 > {STYLE['line_chars']}（第 {i + 1} 行）")
    total = len(text)
    if total > STYLE["total_chars"]:
        over.append(f"总字数 {total} > {STYLE['total_chars']}")
    return {"lines": len(lines), "max_line": max(widths) if widths else 0,
            "total": total, "over": over, "ok": not over}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="回复风格闸（硬指标）")
    ap.add_argument("path", nargs="?", default="-", help="要校的文件（默认 stdin）")
    ap.add_argument("--file", default="", help="同 path（两者取其一）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        src = args.file or args.path
        text = sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"读不到：{exc}", file=sys.stderr)
        return 2
    m = measure(text)
    if args.json:
        print(json.dumps({**m, "limits": STYLE}, ensure_ascii=False))
    else:
        print(f"回复风格｜行 {m['lines']}/{STYLE['lines']}｜最长行 {m['max_line']}/{STYLE['line_chars']}"
              f"｜总字 {m['total']}/{STYLE['total_chars']}")
        print("  结论 " + ("达标" if m["ok"] else "**超标，先删再发**：" + "；".join(m["over"])))
    return 0 if m["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
