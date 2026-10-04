#!/usr/bin/env python3
"""触库工作单元——**真实形态**：建 scratch 库 → 全链迁移 → 删库。

## 它是什么

预算标定器（`calibrate.py`）的 `db` 维需要一个**单元**：
「一个 dev 子代理连一次库」到底花多少、并发起来会不会互相踩。
本件提供那个单元，用的是**目标仓自己的迁移链**，不是合成的建表语句。

## 为什么不能用合成负载

触库的代价大头是**全链迁移 + DDL 元数据锁竞争**，不是 `CREATE TABLE` 本身。
合成语句量不到这个：它既没有迁移链的长度，也没有并发下的锁等待。
（主检出自己的口径：并发迁移在 8 路下实测耗时增加约 264%。）
所以单元 = 建库 ＋ `alembic upgrade head`（全链）＋ 删库。

## 从哪来（改编自主检出 `tools/tools/isolation_bench.py`）

**保留**：scratch 库建/删、全链迁移调用方式（临时 `script_location` 副本）、
前缀守卫、残留清理、保护名单、机读输出。

**删掉（连同理由）**：

- 三种隔离原语（`truncate`／`rollback`／`none`）与用例体读写——
  那是「隔离原语选型」的问题，不是「并行预算」的问题；
- `import tests.conftest_mysql`——会把本体系绑到主检出的测试内部。
  改为本地生成**同形状**库名：`bg_maws_<sha1(repo)[:8]>_<pid>_<rand8>`，
  **无 `__`、内嵌 pid 段**——这样 dba 的 GC（按 `bg_` 前缀 ＋ 年龄 ＋ pid 活性判）
  仍认得并能回收本工具留下的残留，不必为本工具加特例；
- 表数敏感性开关与 F2 spike 的报告框架。

**本件不做阶梯**：阶梯与拐点判据归 `calibrate.py`——一处口径，不重复实现。

## 用法

    python3 tools/db_ladder.py --check        # 只探可用性（连得上 ＋ alembic 在）
    python3 tools/db_ladder.py --unit         # 跑一次单元（标定器作单元命令用它）

退出码：`0` 成功；`1` 单元失败（连不上／迁移失败／删库失败）；`2` 用法或环境错误。

## 安全边界（硬约束）

- **只** CREATE／DROP 本工具前缀（`bg_maws_`）的库；drop 前逐库校验前缀，不符即拒；
- `broker_gateway`／`broker_gateway_dev` 与模板池 `bg_tmpl*` **永不触碰**；
- 单元无论成功、失败还是抛异常，`finally` 里都把自己建的库删掉；
- **不吃别人正在跑的 scratch**：本工具只删自己这次建的那一个库。
  按前缀回收残留**不在这里**——那是 dba 的活，判据与执行都在 `scratch_gc.py`。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path

_COORDINATOR_ROOT = Path(__file__).resolve().parents[1]
if str(_COORDINATOR_ROOT) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_ROOT))

from bg_coordinator.target import target_repo  # noqa: E402

#: MySQL socket（与主检出 conftest 同口径，可用环境变量覆盖）
SOCKET = os.environ.get("BG_MYSQL_SOCKET", "/var/run/mysqld/mysqld.sock")

#: 本工具的库名前缀。**以 `bg_` 开头**是刻意的：dba 的 GC 按 `bg_` 族判回收，
#: 这样残留不会变成没人认领的孤儿库。
SCRATCH_PREFIX = "bg_maws"
#: 保护名单：无论什么前缀都不许删（与主检出 dba 面口径一致）
PROTECTED_NAMES: frozenset[str] = frozenset({"broker_gateway", "broker_gateway_dev"})
PROTECTED_PREFIXES: tuple[str, ...] = ("bg_tmpl",)

SYSTEM_SCHEMAS: frozenset[str] = frozenset(
    {"information_schema", "mysql", "performance_schema", "sys"}
)


# ---------------------------------------------------------------------------
# 库名与连接
# ---------------------------------------------------------------------------


def scratch_name(repo: Path) -> str:
    """进程级唯一 scratch 库名——**形状与主检出 conftest 同族**。

    `bg_<前缀>_<sha1(repo)[:8]>_<pid>_<rand8>`：内嵌 pid 段（GC 的活性判据靠它）、
    **永不含 `__`**（双下划线会破坏形状判据）。
    """
    digest = hashlib.sha1(str(repo).encode("utf-8")).hexdigest()[:8]
    name = f"{SCRATCH_PREFIX}_{digest}_{os.getpid()}_{secrets.token_hex(4)}"
    assert "__" not in name, "库名不得含双下划线（形状不变量）"
    return name


def mysql_url(db: str) -> str:
    """SQLAlchemy URL——迁移链要的是这个形状（与主检出 conftest 一致）。"""
    return f"mysql+pymysql://root@localhost/{db}?unix_socket={SOCKET}"


def _connect(db: str | None = None):  # noqa: ANN202 - pymysql 连接对象
    import pymysql  # noqa: PLC0415 - 只在真正连库时才要求这个依赖

    return pymysql.connect(unix_socket=SOCKET, user="root", database=db, autocommit=False)


def list_databases() -> list[str]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW DATABASES")
            names = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()
    return sorted(n for n in names if n not in SYSTEM_SCHEMAS)


# ---------------------------------------------------------------------------
# 建／删（带守卫）
# ---------------------------------------------------------------------------


def _is_ours(name: str) -> bool:
    return name.startswith(f"{SCRATCH_PREFIX}_")


def _is_protected(name: str) -> bool:
    return name in PROTECTED_NAMES or any(name.startswith(p) for p in PROTECTED_PREFIXES)


def create_scratch(db: str) -> None:
    if not _is_ours(db):
        raise RuntimeError(f"拒绝创建非本工具前缀的库：{db}")
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE `{db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
        conn.commit()
    finally:
        conn.close()


def drop_scratch(db: str) -> None:
    """只允许删**本工具前缀**且**不在保护名单**的库——防误伤的最后一道闸。"""
    if _is_protected(db):
        raise RuntimeError(f"拒绝删除保护库：{db}")
    if not _is_ours(db):
        raise RuntimeError(f"拒绝删除非本工具前缀的库：{db}")
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{db}`")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 全链迁移
# ---------------------------------------------------------------------------


def migrate_head(repo: Path, db: str) -> float:
    """跑目标仓 `alembic upgrade head`（全链），返回墙钟秒数。

    迁移目录**复制到临时目录**再跑：`alembic.ini` 的 `script_location` 是相对的，
    而在同一个 checkout 上并发跑迁移会互相踩 `__pycache__` 与版本表锁。
    一次性目录让每个单元互不干扰（与主检出 conftest 的做法一致）。
    """
    from alembic import command as alembic_command  # noqa: PLC0415
    from alembic.config import Config as AlembicConfig  # noqa: PLC0415

    dest = Path(tempfile.mkdtemp(prefix=f"maws-mig-{db}-"))
    try:
        shutil.copytree(repo / "alembic", dest / "alembic")
        ini = dest / "alembic.ini"
        ini.write_text((repo / "alembic.ini").read_text(encoding="utf-8"), encoding="utf-8")
        conf = AlembicConfig(str(ini))
        conf.set_main_option("script_location", str(dest / "alembic"))
        # 迁移件 import 目标仓根目录下的模块（如 `alembic_metadata`）。
        # pytest 跑测试时靠 rootdir 把仓库根放进 `sys.path`；这里没有 pytest，
        # 得自己放——否则迁移一开头就 ModuleNotFoundError（实测踩过）。
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))
        # 目标仓的 settings 需要这两个变量（主检出口径）；换目标仓时按需调整。
        os.environ.setdefault("TQ_AUTH_USER", "test")
        os.environ.setdefault("TQ_AUTH_PASS", "test")
        os.environ["BG_STORE_URL"] = mysql_url(db)
        # 迁移链会往 stdout 打一整墙 JSON 日志（36 条升级），
        # 淹没 `--json` 的机读输出。单元只关心耗时与退出码——这里把噪音压掉。
        logging.disable(logging.INFO)
        started = time.perf_counter()
        try:
            alembic_command.upgrade(conf, "head")
        finally:
            logging.disable(logging.NOTSET)
        return time.perf_counter() - started
    finally:
        shutil.rmtree(dest, ignore_errors=True)


# ---------------------------------------------------------------------------
# 单元／探测
# ---------------------------------------------------------------------------


def check(repo: Path) -> tuple[bool, str]:
    """可用性：socket 在、pymysql 在、目标仓有迁移链。"""
    if not Path(SOCKET).exists():
        return False, f"MySQL socket 不存在：{SOCKET}"
    if not (repo / "alembic.ini").exists() or not (repo / "alembic").is_dir():
        return False, f"目标仓缺迁移链：{repo}/alembic.ini"
    try:
        import pymysql  # noqa: F401, PLC0415
    except ImportError as exc:
        return False, f"缺 pymysql：{exc}"
    try:
        list_databases()
    except Exception as exc:  # noqa: BLE001 - 探测的意义就是把失败说清楚
        return False, f"连不上 MySQL：{str(exc)[:120]}"
    return True, ""


def run_unit(repo: Path) -> tuple[bool, str]:
    """跑一次单元：建库 → 全链迁移 → 删库。**失败方向只「少留」，不留孤儿。**"""
    db = scratch_name(repo)
    created = False
    err = ""
    try:
        create_scratch(db)
        created = True
        migrate_head(repo, db)
    except Exception as exc:  # noqa: BLE001 - 单元失败要把原因原样带出去
        err = f"{type(exc).__name__}: {str(exc)[:200]}"
    if created:
        try:
            drop_scratch(db)
        except Exception as exc:  # noqa: BLE001 - 删不掉要报（残留会污染后续单元）
            note = f"删库失败（残留 {db}，用 scratch_gc.py 回收）：{str(exc)[:120]}"
            err = f"{err}；{note}" if err else note
    return err == "", err


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="触库工作单元（建库＋全链迁移＋删库）")
    ap.add_argument("--repo", default=None, help="目标仓（默认 $BG_TARGET_REPO 或体系内默认）")
    ap.add_argument("--unit", action="store_true", help="跑一次单元")
    ap.add_argument("--check", action="store_true", help="只探可用性")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    repo = target_repo(args.repo)

    if args.check:
        ok, why = check(repo)
        if args.json:
            print(json.dumps({"ok": ok, "why": why, "socket": SOCKET, "repo": str(repo)}))
        elif not ok:
            print(f"触库单元不可用：{why}", file=sys.stderr)
        return 0 if ok else 2

    if not args.unit:
        print("要指定动作：--unit ／ --check", file=sys.stderr)
        return 2

    ok, why = run_unit(repo)
    if args.json:
        print(json.dumps({"ok": ok, "why": why, "repo": str(repo)}))
    elif not ok:
        print(f"触库单元失败：{why}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
