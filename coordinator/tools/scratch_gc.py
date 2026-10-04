#!/usr/bin/env python3
"""孤儿 scratch 库回收——**判据与执行都在本体系**（dba 面）。

测试的 scratch 库名沿 `bg_` 命名空间（`bg_<族>_<sha8>_<pid>_<rand8>`——**内嵌 pid 段**）。
用例 teardown 只 DROP 自己的库，异常退出会残留；而**前缀扫描式清理不许写在测试里**
（它会误删别人正在用的库），所以回收职责归 **dba**。

## 判据（折自取样件 `ref/tools/scripts/dba/mysql_test_db_gc.sh`）

- ① **命名空间**：名字命中 `bg_` 族（或 `--prefix` 显式注入）才进判——非 `bg_` 一律不动。
- ② **资产豁免**：`broker_gateway`／`broker_gateway_dev`／`bg_tmpl_*` 永不回收。
  模板池是长生命周期资产：命名空间一放宽，不显式豁免就会把 `build` 的产物吃掉。
- ③ **内嵌 pid 族**：`kill -0 <pid>` 失败 ⇒ 孤儿；**存活 ⇒ 跳过**（失败方向只「少删」）。
- ④ **无 pid 族**：年龄 TTL ＋ 无连接 ＋ 无锁，**三者齐备且显式 `--include-nopid`** 才回收。
- ⑤ **DROP 前二次判定**：连接／锁／pid 复活 ⇒ RACE-SKIP（判定与动手之间有时间差）。
- ⑥ **年龄一律 SQL 侧同期比较**：取回本地再比会错位 8 小时（实例 `time_zone`＝CST），
  实测 30min TTL 退化成 **8.5h**——库就是这么堆起来的。
- ⑦ **DROP 会话显式 `lock_wait_timeout`**（默认 5s）：实例默认 **1 年**，
  被未关闭事务的元数据锁挂住就长期占库。

## 用法

    python3 tools/scratch_gc.py                  # **默认 dry-run**：只列判定，不删
    python3 tools/scratch_gc.py --apply          # 真删孤儿库
    python3 tools/scratch_gc.py --include-nopid  # 追加认领无 pid 族（须显式）
    python3 tools/scratch_gc.py --prefix bg_maws --prefix bg_f2   # 只认这些前缀

退出码：`0` 无孤儿／`--apply` 全部成功；`1` 有孤儿但未 `--apply`，或有删除失败；`2` 入参或环境错误。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

SOCKET = os.environ.get("BG_MYSQL_SOCKET", "/var/run/mysqld/mysqld.sock")

#: 命名空间默认 = 现行 `bg_` 全族（旧族可经 `--prefix` 注入）
DEFAULT_NAMESPACE = "bg_"

#: **资产豁免**：命中即永不回收，与命名空间无关
PROTECTED_NAMES: frozenset[str] = frozenset({"broker_gateway", "broker_gateway_dev"})
PROTECTED_PREFIXES: tuple[str, ...] = ("bg_tmpl_",)

SYSTEM_SCHEMAS: frozenset[str] = frozenset(
    {"information_schema", "mysql", "performance_schema", "sys"}
)

#: 名字尾部的 `_<pid>_<rand8>`——<pid> 段是活性判据的锚点
_PID_TAIL = re.compile(r"_(\d+)_[0-9a-fA-F]{8}$")


# ---------------------------------------------------------------------------
# 判据（纯函数：输入事实，输出判定——可脱离数据库测试）
# ---------------------------------------------------------------------------


@dataclass
class Fact:
    """一个库的**事实**：名字、年龄（分钟，SQL 侧算好；None = 未知）、占用情况、属主 pid。"""

    name: str
    age_min: int | None
    connections: int
    locked: bool
    owner_pid: int | None


@dataclass
class Verdict:
    name: str
    action: str  # reap | skip
    reason: str = ""


def parse_owner_pid(name: str) -> int | None:
    """从库名取内嵌 pid；无 pid 族返回 None。"""
    m = _PID_TAIL.search(name)
    return int(m.group(1)) if m else None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但不属于我——视为活着（失败方向：少删）
    return True


def is_protected(name: str) -> bool:
    return name in PROTECTED_NAMES or any(name.startswith(p) for p in PROTECTED_PREFIXES)


def in_namespace(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name.startswith(p) for p in prefixes)


def judge(
    facts: list[Fact],
    *,
    prefixes: tuple[str, ...],
    include_nopid: bool,
    ttl_min: int,
    alive=None,
) -> list[Verdict]:
    """判据本体——**纯函数**，`alive` 可注入（测试用）。

    顺序刻意如此：**资产豁免先于一切**（它是最强的保证，也该最先被说出来），
    命名空间其次；pid 活性**先于**年龄（有 pid 就以 pid 为准，年龄只在无 pid 族上兜底）。
    """
    alive = alive or pid_alive
    out: list[Verdict] = []
    for f in facts:
        if is_protected(f.name):
            out.append(Verdict(f.name, "skip", "资产豁免（保护名单／模板池）——永不回收"))
            continue
        if not in_namespace(f.name, prefixes):
            out.append(Verdict(f.name, "skip", "不在命名空间内——非 bg_ 族一律不动"))
            continue
        if f.connections > 0:
            out.append(Verdict(f.name, "skip", f"有 {f.connections} 个活动连接"))
            continue
        if f.locked:
            out.append(Verdict(f.name, "skip", "有元数据锁"))
            continue
        pid = f.owner_pid if f.owner_pid is not None else parse_owner_pid(f.name)
        if pid is not None:
            if alive(pid):
                out.append(Verdict(f.name, "skip", f"属主 pid {pid} 仍存活"))
            else:
                out.append(Verdict(f.name, "reap", f"属主 pid {pid} 已死 ⇒ 孤儿"))
            continue
        # 无 pid 族：没有活性判据，只能年龄 ＋ 显式授权
        if not include_nopid:
            out.append(Verdict(f.name, "skip", "无 pid 族，未给 --include-nopid——不认领"))
        elif f.age_min is None:
            out.append(Verdict(f.name, "skip", "年龄未知（空库／无 create_time）——不认领"))
        elif f.age_min < ttl_min:
            out.append(Verdict(f.name, "skip", f"年龄 {f.age_min}min < TTL {ttl_min}min"))
        else:
            out.append(Verdict(f.name, "reap", f"无 pid 族且年龄 {f.age_min}min ≥ TTL {ttl_min}min"))
    return out


# ---------------------------------------------------------------------------
# 事实采集与执行（薄 I/O 层）
# ---------------------------------------------------------------------------


def _connect(db: str | None = None, *, lock_wait_s: int | None = None):
    import pymysql  # noqa: PLC0415 - 只在真正连库时才要求这个依赖

    conn = pymysql.connect(unix_socket=SOCKET, user="root", database=db, autocommit=True)
    if lock_wait_s is not None:
        with conn.cursor() as cur:
            cur.execute(f"SET SESSION lock_wait_timeout={int(lock_wait_s)}")
    return conn


def collect_facts(prefixes: tuple[str, ...]) -> list[Fact]:
    """一次查询把**年龄（SQL 侧）／连接数／锁**全部拿到——不在本地做时间换算。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW DATABASES")
            names = [r[0] for r in cur.fetchall()]
            facts: list[Fact] = []
            for name in sorted(names):
                if name in SYSTEM_SCHEMAS:
                    continue
                if not in_namespace(name, prefixes):
                    continue  # 只查要判的，别把系统库与业务库都算进来
                cur.execute(
                    "SELECT TIMESTAMPDIFF(MINUTE, MIN(create_time), NOW()) "
                    "FROM information_schema.tables WHERE table_schema = %s",
                    (name,),
                )
                row = cur.fetchone()
                age = int(row[0]) if row and row[0] is not None else None
                cur.execute(
                    "SELECT COUNT(*) FROM information_schema.processlist WHERE db = %s",
                    (name,),
                )
                conns = int((cur.fetchone() or [0])[0])
                cur.execute(
                    "SELECT COUNT(*) FROM performance_schema.metadata_locks "
                    "WHERE object_schema = %s AND lock_status = 'PENDING'",
                    (name,),
                )
                locked = int((cur.fetchone() or [0])[0]) > 0
                facts.append(
                    Fact(
                        name=name,
                        age_min=age,
                        connections=conns,
                        locked=locked,
                        owner_pid=parse_owner_pid(name),
                    )
                )
            return facts
    finally:
        conn.close()


def recheck_and_drop(name: str, *, lock_wait_s: int) -> tuple[bool, str]:
    """**DROP 前二次判定**：判定与动手之间别人可能刚连上（RACE-SKIP）。"""
    conn = _connect(lock_wait_s=lock_wait_s)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM information_schema.processlist WHERE db = %s", (name,))
            if int((cur.fetchone() or [0])[0]) > 0:
                return False, "RACE-SKIP：二次判定发现活动连接"
        pid = parse_owner_pid(name)
        if pid is not None and pid_alive(pid):
            return False, "RACE-SKIP：属主 pid 复活"
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{name}`")
        return True, ""
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    """参数装配独立出来——**默认值必须可测**（"默认 dry-run"是一条安全承诺）。"""
    ap = argparse.ArgumentParser(description="孤儿 scratch 库回收（dba 面；默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真删；不给就是 dry-run（只列判定）")
    ap.add_argument("--include-nopid", action="store_true", help="追加认领无 pid 族（须显式）")
    ap.add_argument("--prefix", action="append", default=[], help="命名空间前缀（可重复）")
    ap.add_argument("--protect", action="append", default=[], help="追加保护名单（可重复）")
    ap.add_argument("--ttl-min", type=int, default=60, help="无 pid 族的年龄 TTL（分钟）")
    ap.add_argument("--lock-wait", type=int, default=5, help="DROP 会话 lock_wait_timeout（秒）")
    ap.add_argument("--json", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    prefixes = tuple(args.prefix or [DEFAULT_NAMESPACE])
    if any(p.strip() == "" for p in prefixes):
        print("--prefix 不得为空", file=sys.stderr)
        return 2
    global PROTECTED_NAMES
    if args.protect:
        PROTECTED_NAMES = PROTECTED_NAMES | frozenset(args.protect)

    if not Path(SOCKET).exists():
        print(f"MySQL socket 不存在：{SOCKET}", file=sys.stderr)
        return 2
    try:
        facts = collect_facts(prefixes)
    except Exception as exc:  # noqa: BLE001 - 连不上要说清楚
        print(f"连库失败：{str(exc)[:160]}", file=sys.stderr)
        return 2

    verdicts = judge(
        facts, prefixes=prefixes, include_nopid=args.include_nopid, ttl_min=args.ttl_min
    )
    orphanes = [v for v in verdicts if v.action == "reap"]

    dropped: list[str] = []
    failed: list[str] = []
    if args.apply:
        for v in orphanes:
            ok, why = recheck_and_drop(v.name, lock_wait_s=args.lock_wait)
            (dropped if ok else failed).append(v.name if ok else f"{v.name}（{why}）")

    if args.json:
        print(
            json.dumps(
                {
                    "prefixes": list(prefixes),
                    "orphans": [asdict(v) for v in orphanes],
                    "skipped": [asdict(v) for v in verdicts if v.action == "skip"],
                    "applied": args.apply,
                    "dropped": dropped,
                    "failed": failed,
                },
                ensure_ascii=False,
            )
        )
    else:
        print(f"scratch 回收｜命名空间 {list(prefixes)}｜{'apply' if args.apply else 'dry-run'}")
        for v in verdicts:
            mark = "可回收" if v.action == "reap" else "跳过"
            print(f"  [{mark}] {v.name} —— {v.reason}")
        if args.apply:
            print(f"  已删 {len(dropped)} 个" + (f"；失败 {failed}" if failed else ""))
        elif orphanes:
            print(f"  共 {len(orphanes)} 个孤儿未删（加 --apply 才动手）")

    if failed:
        return 1
    if orphanes and not args.apply:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
