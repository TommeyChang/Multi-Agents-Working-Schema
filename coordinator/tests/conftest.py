"""共用夹具。"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bg_coordinator.models import Actor, Line, Role  # noqa: E402
from bg_coordinator.storage import Store  # noqa: E402


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    """状态根在临时目录——**绝不碰真实工作区**。"""
    s = Store(root=tmp_path / "coordinator", repo=None)
    s.init()
    yield s
    # 临时目录由 pytest 清理


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    return r


def actor(role: Role = Role.TECH_LEAD, name: str = "tl-D", line: Line | None = Line.D) -> Actor:
    return Actor(role=role, name=name, line=line)


def budgeted(state, budget: int = 8):
    """给测试状态批一套预算（**内置线各 8 个在办**）。

    默认口径是「**未批预算的线派不了活**」（`Quota.enabled=True`）——
    凡是要走到 `claim-dev` 的测试都必须先批。这不是测试的麻烦，
    而是把线上口径照搬进测试；**要测"未批预算被拒"的用例别用它**，直接用 `State()`。
    """
    from bg_coordinator.models import Line as _Line
    from bg_coordinator.readiness import Quota

    state.quota = Quota(enabled=True, per_line={ln.value: budget for ln in _Line})
    return state
