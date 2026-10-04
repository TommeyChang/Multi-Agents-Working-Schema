"""目标仓——**本体系只有这一处定义「被服务的代码仓」**。

协调器与它的工具都作用于某个代码仓（跑它的测试、装它的视图、看它的 diff），
但那是**工作对象**，不是本体系的一部分。把「目标仓在哪」写死在三四个脚本里，
就会出现三份不一致的默认值——改一处忘一处，最后没人知道到底在服务谁。

解析顺序（**只认这三处，不猜**）：

1. 命令行显式给的（`--repo`／`--target`）；
2. 环境变量 `$BG_TARGET_REPO`；
3. 体系内默认值（`coordinator/../../futures-broker-gateway`，即工作区里的主检出）。
"""

from __future__ import annotations

import os
from pathlib import Path

#: 目标仓的环境变量名（与 `--repo`／`--target` 等价）
TARGET_REPO_ENV = "BG_TARGET_REPO"

#: 体系内默认值——相对 `coordinator/` 目录
DEFAULT_TARGET = "../../futures-broker-gateway"


def default_target_repo() -> Path:
    """体系内默认目标仓（不检查存在——调用方负责给出可读的错误）。"""
    return (Path(__file__).resolve().parents[1] / DEFAULT_TARGET).resolve()


def target_repo(given: str | None = None) -> Path:
    """解析目标仓：显式 → 环境变量 → 默认。返回绝对路径（不一定存在）。"""
    if given:
        return Path(given).expanduser().resolve()
    env = os.environ.get(TARGET_REPO_ENV)
    if env:
        return Path(env).expanduser().resolve()
    return default_target_repo()
