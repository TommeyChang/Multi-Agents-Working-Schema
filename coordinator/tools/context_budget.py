#!/usr/bin/env python3
"""固定上下文预算闸——**冷启动要读的东西，规模可见、可拦**。

## 判据在哪

预算常量与判据在 `bg_coordinator/context.py`（系统模块，**一处权威**）；
本件只是外壳：现算规模、对照预算、报。

**为什么要有它**：`AGENT.md` §三 那张体积表是**手写的**，于是漂了——
写"约 1.3k tokens"实测 ≈2.9k，写"角色 0.7~1.3k"实测 0.8~2.6k。
手写的实测值必然过期；按过期数字做预算的会话会误判自己还剩多少余地。
所以：**政策（允许多大）进常量与文档，事实（现在多大）每次现算、不进文档。**

## 用法

    python3 tools/context_budget.py            # 人读：逐文件 ＋ 预算对照
    python3 tools/context_budget.py --json     # 机器读
    python3 tools/context_budget.py --root <体系根>

退出码：`0` 全在预算内；`1` 有超预算项；`2` 用法错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_COORDINATOR_ROOT = Path(__file__).resolve().parents[1]
if str(_COORDINATOR_ROOT) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_ROOT))

from bg_coordinator import context as ctx  # noqa: E402
from bg_coordinator.binding import maws_root  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="固定上下文预算闸（冷启动规模）")
    ap.add_argument("--root", default="", help="体系根（默认按包位置推）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve() if args.root else maws_root()
    if not (root / "AGENT.md").is_file():
        print(f"不像体系根（缺 AGENT.md）：{root}", file=sys.stderr)
        return 2

    m = ctx.measure(root)
    over = ctx.violations(m)
    if args.json:
        print(json.dumps({**m, "over": over, "ok": not over}, ensure_ascii=False))
        return 1 if over else 0

    b = m["budgets"]
    print(f"固定上下文｜体系根 {root}")
    print(f"  冷启动必读（预算 AGENT ≤{b['agent_max']}｜角色 ≤{b['role_max']}）")
    for name, size in m["cold"].items():
        mark = "超" if size > b["agent_max"] else "ok"
        tok = ctx.est_tokens((root / name).read_text(encoding="utf-8"))
        print(f"    [{mark}] {name:<28}{size:>6} 字符 ≈{tok:>5} tokens")
    for name, size in sorted(m["roles"].items(), key=lambda kv: -kv[1]):
        mark = "超" if size > b["role_max"] else "ok"
        print(f"    [{mark}] {name:<28}{size:>6} 字符")
    print(f"  冷启动合计（AGENT ＋ 最重角色 {m['cold_start_worst_role']}）"
          f"{m['cold_start_max']} 字符 / 预算 {b['cold_start_max']}")
    print(f"  常规轮次读的面合计 {m['routine_total']} 字符 / 预算 {b['routine_max']}"
          f"（{'、'.join(m['routine'])}）")
    print(f"  按需面 rules/*.md 合计 {m['rules_total']} 字符 / 预算 {b['rules_total_max']}")
    for name, size in sorted(m["rules"].items(), key=lambda kv: -kv[1]):
        print(f"      {name:<28}{size:>6} 字符")
    print("  按角色读面（预算是 ROLE_READ_MAX；含 AGENT ＋ 本角色文件 ＋ 它读的各节）")
    for role, v in sorted(m["per_role"].items(), key=lambda kv: kv[1]["chars"]):
        cap = b.get("role_read_max", {}).get(role, 0)
        mark = "超" if cap and v["chars"] > cap else "ok"
        print(f"    [{mark}] {role:<10}{v['chars']:>6} / {cap}")
    print("  结论 " + ("**超预算**：" + "；".join(over) if over else "全在预算内"))
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())
