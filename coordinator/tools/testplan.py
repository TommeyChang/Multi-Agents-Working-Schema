#!/usr/bin/env python3
"""测试分级判据（外壳）：这条改动**至少要跑到哪一级**。

用户口径：「开发过程中的测试分多级，不能全量跑。」
级与判据在 `bg_coordinator/testplan.py`；本工具只负责取数（改动文件、绑定）与打印。

用法：

    python3 tools/testplan.py --target <仓> --base origin/main      # 从 git 取改动
    python3 tools/testplan.py --files auth/x.py data_access/y.py    # 直接给文件
    python3 tools/testplan.py --target <仓> --scope affected        # 只核"这一级够不够"
    python3 tools/testplan.py --json

退出码：`0` 判得出且（如给了 `--scope`）达标；`1` 判不出或不够；`2` 用法错。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_COORD = Path(__file__).resolve().parents[1]
if str(_COORD) not in sys.path:
    sys.path.insert(0, str(_COORD))

from bg_coordinator import testplan as tp  # noqa: E402
from bg_coordinator.binding import load as load_binding  # noqa: E402
from bg_coordinator.binding import locate as locate_binding  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="这条改动至少要跑到哪一级")
    ap.add_argument("--target", default=None, help="目标仓（取 git 改动用）")
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--files", nargs="*", default=[], help="直接给改动文件（不走 git）")
    ap.add_argument("--scope", default="", help="申报/实测的级别，核它够不够")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    repo = Path(args.target).resolve() if args.target else None
    block: dict = {}
    path = None
    if repo is not None:
        _src, path = locate_binding(repo)  # 有目标仓才按主干标记定位
    if path is None:
        # 退一步：体系里**只登记了一份**绑定时就认它（多份则必须给 --target，不许猜）
        from bg_coordinator.binding import bindings_dir

        cands = sorted((bindings_dir() or _COORD.parent / "bindings").glob("*.md"))
        path = cands[0] if len(cands) == 1 else None
    data, _err = load_binding(path) if path else ({}, "")
    if data:
        block = dict(data.get("testplan") or {})
    domains, public, budget = tp.load(block)

    files = tp.normalize(args.files) or (tp.changed_in_repo(repo, args.base) if repo else [])
    need, why = tp.required_scope(files, domains, public)
    ok = need is not None and (not args.scope or tp.meets(need, args.scope))
    if args.json:
        print(json.dumps({"files": len(files), "required": need, "reasons": why,
                          "scope": args.scope, "ok": ok, "budget": budget},
                         ensure_ascii=False))
        return 0 if ok else 1

    print(f"改动 {len(files)} 个文件｜至少要跑到 **{need or '判不了（先补绑定）'}** 级")
    for r in why:
        print(f"  · {r}")
    if budget:
        print("  时长预算：" + "、".join(f"{k} ≤{v}s" for k, v in budget.items()))
    if args.scope:
        print(f"  申报 {args.scope} ⇒ " + ("够" if tp.meets(need, args.scope) else "**不够，升级**"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
