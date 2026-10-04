#!/usr/bin/env python3
"""**用户口径台账**——每条口径写在哪、**由谁核**、还是只有文字。

用户口径：「规则性的，最好用工具来解决，才能够真正量化，单纯靠文字，约束力太小了。」

本件就是那句话的工具化：把"这条规矩谁核它"变成**一屏可读、可数、可拦**的东西。

- 有闸的：`judge` 是仓内文件（工具或测试），能复跑；
- 没闸的：`judge` 是 `文字`——**欠账**，且**欠账有上限**（`TEXT_ONLY_MAX`，棘轮）：
  想新加一条纯文字口径，就得先给旧的一条配闸。

## 用法

    python3 tools/rule_coverage.py            # 全部口径 ＋ 欠账清单
    python3 tools/rule_coverage.py --text     # 只看还没闸的（欠账）
    python3 tools/rule_coverage.py --json

退出码：`0` 无缺口且欠账不超上限；`1` 有缺口或欠账超上限；`2` 用法错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_COORDINATOR_ROOT = Path(__file__).resolve().parents[1]
if str(_COORDINATOR_ROOT) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_ROOT))

from bg_coordinator import rulings as rul  # noqa: E402
from bg_coordinator.binding import maws_root  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="用户口径台账（谁核它／还欠几条闸）")
    ap.add_argument("--root", default="", help="体系根（默认按包位置推）")
    ap.add_argument("--text", action="store_true", help="只看还没闸的（欠账）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve() if args.root else maws_root()
    if not (root / "AGENT.md").is_file():
        print(f"不像体系根（缺 AGENT.md）：{root}", file=sys.stderr)
        return 2

    rep = rul.report(root)
    over = len(rep["text_only"]) > rep["text_only_max"]
    if args.json:
        print(json.dumps({**rep, "ok": not rep["gaps"] and not over}, ensure_ascii=False))
        return 1 if (rep["gaps"] or over) else 0

    rows = rep["text_only"] if args.text else rep["rows"]
    print(f"用户口径台账｜共 {rep['total']} 条｜有闸 {rep['total'] - len(rep['text_only'])}"
          f"｜**只有文字 {len(rep['text_only'])}（上限 {rep['text_only_max']}）**")
    for r in rows:
        mark = "文字" if r["judge"] == rul.TEXT else "闸  "
        flag = " ⚠缺口" if r["missing_doc"] else (" ⚠兑现者不存在" if r["missing_judge"] else "")
        print(f"  [{mark}] {r['token'][:26]:<28} @{r['path']:<24} ← {r['judge']}{flag}")
    if rep["gaps"]:
        print("  缺口：" + "；".join(f"{g['token']}（{g['path']}）" for g in rep["gaps"]))
    if over:
        print(f"  **欠账超上限**：{len(rep['text_only'])} > {rep['text_only_max']}"
              "——加一条纯文字口径前，先给旧的一条配上闸")
    return 1 if (rep["gaps"] or over) else 0


if __name__ == "__main__":
    sys.exit(main())
