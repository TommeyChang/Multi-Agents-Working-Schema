#!/usr/bin/env python3
"""存活探针——**只告警，不重启**（ops 面）。

## 它解决什么

进程活着、socket 仍 LISTEN、journal 无 ERROR，但**直连 `/health` 超时**——
这种"卡死"过去只能靠人发现、靠人重启。本工具把它变成**可自动发现的事件**：

1. **直连**目标 URL（绕开反代——反代有自己的 502/504 语义，不反映应用存活）；
2. 连续失败达阈值（默认 3）⇒ 判定**卡死**，写结构化告警行 ＋ 落状态文件；
3. 恢复即清零并记一条恢复。

## 红线：本工具**没有任何重启路径**

自动重启是**单独授权**的事：未授权就开启＝违规。这条不靠自觉——
**代码里没有 `systemctl`／`restart`／`kill`**，且由 `tests/test_ops_tools.py` 静态钉住。
探针的职责边界就是"发现并留痕"，动手是另一个决定。

## 用法

    python3 tools/probe.py --url http://127.0.0.1:18080/health
    python3 tools/probe.py --url ... --fail-threshold 3 --state-dir /var/lib/bg-probe
    python3 tools/probe.py --url ... --json

状态文件（记连续失败数）默认落在 `<本体系>/.state/probe-<url 摘要>.json`；
写不进去就退到临时目录——**探针不该因为写不了状态就罢工**。

退出码：`0` 本次探活成功（或已恢复）；`1` **已判定卡死**（告警）；`2` 用法或环境错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_COORDINATOR_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_STATE_DIR = _COORDINATOR_ROOT / ".state"


def state_path(url: str, state_dir: Path | None) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    base = state_dir or _DEFAULT_STATE_DIR
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        base = Path("/tmp")  # 只读 HOME／沙箱下退到临时目录
    return base / f"probe-{digest}.json"


def load(state: Path) -> dict:
    try:
        return json.loads(state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"failures": 0, "alive": True}


def save(state: Path, payload: dict) -> str:
    try:
        state.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return ""
    except OSError as exc:
        return f"状态写不进去（{str(exc)[:80]}）——本次判定仍有效"


def probe_once(url: str, timeout: float) -> tuple[bool, str]:
    """直连探一次。**任何异常都算失败**（连不上、超时、非 2xx）——fail closed。"""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 只探给定 URL
            code = resp.status
            return (200 <= code < 300), f"HTTP {code}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - 探针的意义就是把失败说清楚
        return False, f"{type(exc).__name__}: {str(exc)[:100]}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="存活探针（只告警不重启）")
    ap.add_argument("--url", required=True, help="直连探活地址，如 http://127.0.0.1:18080/health")
    ap.add_argument("--timeout", type=float, default=5.0, help="单次请求超时秒")
    ap.add_argument("--fail-threshold", type=int, default=3, help="连续失败达此数即判卡死")
    ap.add_argument("--state-dir", default=None, help="状态目录（默认 <本体系>/.state）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.fail_threshold < 1:
        print("--fail-threshold 须 ≥ 1", file=sys.stderr)
        return 2

    state = state_path(args.url, Path(args.state_dir) if args.state_dir else None)
    prev = load(state)
    ok, detail = probe_once(args.url, args.timeout)
    failures = 0 if ok else int(prev.get("failures", 0)) + 1
    judged_dead = failures >= args.fail_threshold
    note = save(
        state,
        {
            "url": args.url,
            "failures": failures,
            "alive": ok or not judged_dead,
            "last_detail": detail,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
    )

    if args.json:
        print(
            json.dumps(
                {
                    "url": args.url,
                    "ok": ok,
                    "detail": detail,
                    "failures": failures,
                    "judged_dead": judged_dead,
                    "state": str(state),
                    "note": note,
                },
                ensure_ascii=False,
            )
        )
    elif ok:
        print(f"探活成功：{args.url}（{detail}）" + ("；连续失败已清零" if prev.get("failures") else ""))
    elif judged_dead:
        # **结构化告警行**（可被 journal／日志采集直接抓）
        print(
            f"ALERT probe_stalled url={args.url} failures={failures} "
            f"threshold={args.fail_threshold} detail={detail}（**只告警，不重启**）",
            file=sys.stderr,
        )
    else:
        print(f"探活失败 {failures}/{args.fail_threshold}：{args.url}（{detail}）", file=sys.stderr)

    if note:
        print(f"提示：{note}", file=sys.stderr)
    return 1 if judged_dead else 0


if __name__ == "__main__":
    sys.exit(main())
